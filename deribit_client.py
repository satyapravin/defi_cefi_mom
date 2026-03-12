from __future__ import annotations

import asyncio
import json
import time
from typing import Awaitable, Callable, Optional

import websockets

from config import DeribitConfig
from logger import setup_logger
from models import Direction, OrderRequest

logger = setup_logger()


class DeribitClient:
    def __init__(self, config: DeribitConfig):
        self._config = config
        self._ws = None
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expiry: float = 0
        self._request_id: int = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._subscriptions: dict[str, Callable[[dict], Awaitable[None]]] = {}
        self._recv_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._refresh_task: asyncio.Task | None = None
        self._running = False

        self._book_data: dict[str, dict] = {}

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def connect(self) -> None:
        self._ws = await websockets.connect(self._config.ws_url)
        self._running = True
        self._recv_task = asyncio.create_task(self._recv_loop())
        await self._authenticate()
        logger.info("Deribit connected and authenticated")

    async def disconnect(self) -> None:
        self._running = False
        if self._refresh_task:
            self._refresh_task.cancel()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._recv_task:
            self._recv_task.cancel()
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def _authenticate(self) -> None:
        result = await self._send_request(
            "public/auth",
            {
                "grant_type": "client_credentials",
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret,
            },
        )
        self._access_token = result["access_token"]
        self._refresh_token = result["refresh_token"]
        expires_in = result.get("expires_in", 900)
        self._token_expiry = time.time() + expires_in

        if self._refresh_task:
            self._refresh_task.cancel()
        self._refresh_task = asyncio.create_task(
            self._token_refresh_loop(expires_in)
        )

    async def _token_refresh_loop(self, expires_in: int) -> None:
        while self._running:
            await asyncio.sleep(max(expires_in - 30, 10))
            try:
                result = await self._send_request(
                    "public/auth",
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": self._refresh_token,
                    },
                )
                self._access_token = result["access_token"]
                self._refresh_token = result["refresh_token"]
                expires_in = result.get("expires_in", 900)
                self._token_expiry = time.time() + expires_in
                logger.info("Deribit token refreshed")
            except Exception as e:
                logger.error("Token refresh failed", error=str(e))

    async def _send_request(self, method: str, params: dict) -> dict:
        req_id = self._next_id()
        msg = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future
        await self._ws.send(json.dumps(msg))
        result = await asyncio.wait_for(future, timeout=10.0)
        return result

    async def _recv_loop(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)

                if "id" in msg and msg["id"] in self._pending:
                    future = self._pending.pop(msg["id"])
                    if "error" in msg:
                        future.set_exception(
                            RuntimeError(
                                f"Deribit error: {msg['error'].get('message', msg['error'])}"
                            )
                        )
                    else:
                        future.set_result(msg.get("result", {}))

                elif msg.get("method") == "subscription":
                    params = msg.get("params", {})
                    channel = params.get("channel", "")
                    data = params.get("data", {})

                    if channel.startswith("book."):
                        instrument = channel.split(".")[1]
                        self._book_data[instrument] = data

                    if channel in self._subscriptions:
                        try:
                            await self._subscriptions[channel](data)
                        except Exception as e:
                            logger.error(
                                "Subscription callback error",
                                channel=channel,
                                error=str(e),
                            )

        except websockets.exceptions.ConnectionClosed:
            if self._running:
                logger.warning("Deribit WebSocket closed unexpectedly")
                await self._reconnect()
        except Exception as e:
            if self._running:
                logger.error("Deribit recv loop error", error=str(e))
                await self._reconnect()

    async def _reconnect(self) -> None:
        logger.info("Attempting Deribit reconnect")
        await asyncio.sleep(5)
        try:
            self._ws = await websockets.connect(self._config.ws_url)
            self._recv_task = asyncio.create_task(self._recv_loop())
            await self._authenticate()

            channels = list(self._subscriptions.keys())
            if channels:
                await self._send_request(
                    "public/subscribe" if not channels[0].startswith("user.") else "private/subscribe",
                    {"channels": channels},
                )
            logger.info("Deribit reconnected", channels=len(channels))
        except Exception as e:
            logger.error("Deribit reconnect failed", error=str(e))

    async def _subscribe(self, channels: list[str], callback: Callable[[dict], Awaitable[None]], private: bool = False) -> None:
        for ch in channels:
            self._subscriptions[ch] = callback
        method = "private/subscribe" if private else "public/subscribe"
        await self._send_request(method, {"channels": channels})

    async def subscribe_book(
        self,
        instrument: str,
        callback: Callable[[dict], Awaitable[None]],
    ) -> None:
        channel = f"book.{instrument}.100ms"
        await self._subscribe([channel], callback, private=False)

    async def subscribe_orders(
        self,
        instrument: str,
        callback: Callable[[dict], Awaitable[None]],
    ) -> None:
        channel = f"user.orders.{instrument}.raw"
        await self._subscribe([channel], callback, private=True)

    async def subscribe_trades(
        self,
        instrument: str,
        callback: Callable[[dict], Awaitable[None]],
    ) -> None:
        channel = f"user.trades.{instrument}.raw"
        await self._subscribe([channel], callback, private=True)

    async def subscribe_portfolio(
        self,
        currency: str,
        callback: Callable[[dict], Awaitable[None]],
    ) -> None:
        channel = f"user.portfolio.{currency}"
        await self._subscribe([channel], callback, private=True)

    async def place_order(self, request: OrderRequest) -> str:
        method = "private/buy" if request.direction == Direction.LONG else "private/sell"
        params = {
            "instrument_name": request.instrument,
            "amount": request.size,
            "type": "limit",
            "price": request.limit_price,
            "post_only": request.post_only,
            "reduce_only": request.reduce_only,
        }
        if request.label:
            params["label"] = request.label

        result = await self._send_request(method, params)
        order = result.get("order", {})
        order_id = order.get("order_id", "")
        logger.info(
            "Order placed",
            order_id=order_id,
            direction=request.direction.name,
            size=request.size,
            price=request.limit_price,
            instrument=request.instrument,
        )
        return order_id

    async def place_market_order(
        self, instrument: str, direction: Direction, size: float, reduce_only: bool = True
    ) -> str:
        method = "private/buy" if direction == Direction.LONG else "private/sell"
        params = {
            "instrument_name": instrument,
            "amount": size,
            "type": "market",
            "reduce_only": reduce_only,
        }
        result = await self._send_request(method, params)
        order = result.get("order", {})
        order_id = order.get("order_id", "")
        logger.info(
            "Market order placed",
            order_id=order_id,
            direction=direction.name,
            size=size,
            instrument=instrument,
        )
        return order_id

    async def cancel_order(self, order_id: str) -> None:
        await self._send_request("private/cancel", {"order_id": order_id})
        logger.info("Order cancelled", order_id=order_id)

    async def cancel_all(self, instrument: str) -> None:
        await self._send_request(
            "private/cancel_all_by_instrument",
            {"instrument_name": instrument},
        )
        logger.info("All orders cancelled", instrument=instrument)

    async def get_position(self, instrument: str) -> Optional[dict]:
        try:
            result = await self._send_request(
                "private/get_position",
                {"instrument_name": instrument},
            )
            return result
        except RuntimeError:
            return None

    async def get_account_summary(self, currency: str) -> dict:
        return await self._send_request(
            "private/get_account_summary",
            {"currency": currency},
        )

    def get_mid_price(self, instrument: str) -> Optional[float]:
        book = self._book_data.get(instrument)
        if not book:
            return None
        best_bid = self.get_best_bid(instrument)
        best_ask = self.get_best_ask(instrument)
        if best_bid is not None and best_ask is not None:
            return (best_bid + best_ask) / 2.0
        return None

    def get_best_bid(self, instrument: str) -> Optional[float]:
        book = self._book_data.get(instrument)
        if not book:
            return None
        bids = book.get("bids", [])
        if bids:
            return float(bids[0][0]) if isinstance(bids[0], list) else float(bids[0].get("price", 0))
        return None

    def get_best_ask(self, instrument: str) -> Optional[float]:
        book = self._book_data.get(instrument)
        if not book:
            return None
        asks = book.get("asks", [])
        if asks:
            return float(asks[0][0]) if isinstance(asks[0], list) else float(asks[0].get("price", 0))
        return None

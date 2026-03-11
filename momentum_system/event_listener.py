from __future__ import annotations

import asyncio
import math
from typing import Awaitable, Callable

from web3 import AsyncWeb3, WebSocketProvider
from web3.types import LogReceipt

from config import Config, PairConfig
from database import Database
from logger import setup_logger
from models import FeeTier, SwapEvent

logger = setup_logger()

SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"


class EventListener:
    def __init__(
        self,
        config: Config,
        database: Database,
        on_event: Callable[[SwapEvent], Awaitable[None]],
    ):
        self._config = config
        self._database = database
        self._on_event = on_event
        self._w3: AsyncWeb3 | None = None
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._last_price: dict[str, float] = {}

        self._pool_map: dict[str, tuple[str, FeeTier, PairConfig]] = {}
        for pair_cfg in config.pairs:
            for tier_name, pool_cfg in [
                ("bp30", pair_cfg.pools.bp30),
                ("bp5", pair_cfg.pools.bp5),
                ("bp1", pair_cfg.pools.bp1),
            ]:
                addr = pool_cfg.address.lower()
                self._pool_map[addr] = (
                    pair_cfg.name,
                    FeeTier(tier_name),
                    pair_cfg,
                )

    async def start(self) -> None:
        self._running = True
        reconnect_attempts = 0
        max_attempts = self._config.infrastructure.max_reconnect_attempts

        while self._running and reconnect_attempts <= max_attempts:
            try:
                provider = WebSocketProvider(self._config.infrastructure.rpc_url)
                self._w3 = AsyncWeb3(provider)
                connected = await self._w3.is_connected()
                if not connected:
                    raise ConnectionError("Failed to connect to RPC")

                logger.info(
                    "Connected to RPC",
                    chain_id=self._config.infrastructure.chain_id,
                )
                reconnect_attempts = 0

                pool_addresses = list(self._pool_map.keys())
                log_filter = await self._w3.eth.filter({
                    "topics": [SWAP_TOPIC],
                    "address": [
                        AsyncWeb3.to_checksum_address(a) for a in pool_addresses
                    ],
                })

                while self._running:
                    try:
                        logs = await log_filter.get_new_entries()
                        for log_entry in logs:
                            await self._process_log(log_entry)
                    except Exception as e:
                        if not self._running:
                            break
                        logger.warning("Error polling logs", error=str(e))
                        raise

                    await asyncio.sleep(1)

            except Exception as e:
                if not self._running:
                    break
                reconnect_attempts += 1
                logger.warning(
                    "WebSocket disconnected, reconnecting",
                    attempt=reconnect_attempts,
                    error=str(e),
                )
                await self._handle_reconnect()

        if reconnect_attempts > max_attempts:
            logger.error("Max reconnect attempts exceeded")
            raise ConnectionError("Max reconnect attempts exceeded")

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()

    async def _process_log(self, log_entry: LogReceipt) -> None:
        pool_address = log_entry["address"].lower()
        if pool_address not in self._pool_map:
            return

        pair_name, fee_tier, pair_config = self._pool_map[pool_address]

        try:
            event = self._parse_swap_log(
                log_entry, pair_name, fee_tier, pool_address, pair_config
            )
        except Exception as e:
            logger.error("Failed to parse swap log", error=str(e), tx=log_entry.get("transactionHash", b"").hex())
            return

        logger.debug(
            "swap_event",
            pair=pair_name,
            tier=fee_tier.value,
            price=event.price,
            log_return=event.log_return,
        )

        await self._database.insert_swap_event(event)
        await self._on_event(event)

    def _parse_swap_log(
        self,
        log: LogReceipt,
        pair_name: str,
        fee_tier: FeeTier,
        pool_address: str,
        pair_config: PairConfig,
    ) -> SwapEvent:
        data = log["data"]
        if isinstance(data, str):
            data = bytes.fromhex(data[2:]) if data.startswith("0x") else bytes.fromhex(data)

        amount0 = int.from_bytes(data[0:32], "big", signed=True)
        amount1 = int.from_bytes(data[32:64], "big", signed=True)
        sqrt_price_x96 = int.from_bytes(data[64:96], "big", signed=False)
        liquidity = int.from_bytes(data[96:128], "big", signed=False)
        tick = int.from_bytes(data[128:160], "big", signed=True)

        price = self._compute_price(
            sqrt_price_x96,
            pair_config.token0_decimals,
            pair_config.token1_decimals,
            pair_config.invert_price,
        )

        log_return, direction = self._compute_log_return(pool_address, price)

        tx_hash = log["transactionHash"]
        if isinstance(tx_hash, bytes):
            tx_hash = tx_hash.hex()
        if not tx_hash.startswith("0x"):
            tx_hash = "0x" + tx_hash

        block_number = log["blockNumber"]
        block_timestamp = 0  # Will be filled by block lookup if available

        return SwapEvent(
            pair_name=pair_name,
            fee_tier=fee_tier,
            block_number=block_number,
            block_timestamp=block_timestamp,
            transaction_hash=tx_hash,
            pool_address=pool_address,
            sqrt_price_x96=sqrt_price_x96,
            tick=tick,
            liquidity=liquidity,
            amount0=amount0,
            amount1=amount1,
            price=price,
            log_return=log_return,
            direction=direction,
        )

    @staticmethod
    def _compute_price(
        sqrt_price_x96: int,
        token0_decimals: int,
        token1_decimals: int,
        invert: bool,
    ) -> float:
        p_raw = (sqrt_price_x96 / (2**96)) ** 2
        p = p_raw * 10 ** (token0_decimals - token1_decimals)
        if invert:
            p = 1.0 / p if p != 0 else 0.0
        return p

    def _compute_log_return(
        self, pool_address: str, price: float
    ) -> tuple[float | None, int | None]:
        prev = self._last_price.get(pool_address)
        self._last_price[pool_address] = price

        if prev is None or prev <= 0 or price <= 0:
            return None, None

        r = math.log(price) - math.log(prev)
        direction = 1 if r > 0 else (-1 if r < 0 else 0)
        return r, direction

    async def _handle_reconnect(self) -> None:
        delay = self._config.infrastructure.reconnect_delay_seconds
        await asyncio.sleep(delay)

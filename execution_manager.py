from __future__ import annotations

import asyncio
import math
import time
import uuid
from typing import Callable, Optional

from config import Config
from database import Database
from deribit_client import DeribitClient
from logger import setup_logger
from models import (
    Direction,
    OrderRequest,
    OrderState,
    OrderStatus,
    Position,
    Regime,
    SignalTransition,
    TradeSignal,
    TradeRecord,
)
from risk_manager import RiskManager

logger = setup_logger()

TICK_SIZES = {
    "ETH-PERPETUAL": 0.5,
    "BTC-PERPETUAL": 0.5,
    "ETH_USDC-PERPETUAL": 0.01,
    "BTC_USDC-PERPETUAL": 0.01,
}

MIN_ORDER_SIZES = {
    "ETH_USDC-PERPETUAL": 0.001,
    "BTC_USDC-PERPETUAL": 0.0001,
}


def _round_to_tick(price: float, instrument: str) -> float:
    tick = TICK_SIZES.get(instrument, 0.5)
    return round(price / tick) * tick


class ExecutionManager:
    def __init__(
        self,
        config: Config,
        database: Database,
        deribit: DeribitClient,
        risk_manager: RiskManager,
    ):
        self._config = config
        self._database = database
        self._deribit = deribit
        self._risk = risk_manager
        self._mode = config.system.mode

        self._open_orders: dict[str, OrderState] = {}
        self._positions: dict[str, Position] = {}
        self._stale_tasks: dict[str, asyncio.Task] = {}
        self._tp_orders: dict[str, str] = {}
        self._on_position_closed: Optional[Callable[[str], None]] = None
        self._exit_fee_rate: float = 0.0005

    async def on_trade_signal(self, signal: TradeSignal) -> None:
        pair = signal.pair_name
        logger.info(
            "Trade signal received",
            pair=pair,
            transition=signal.transition.value,
            direction=signal.direction.name,
            strength=round(signal.signal_strength, 2),
            regime=signal.regime.value,
            bp30_count=signal.bp30_count,
        )

        if signal.transition == SignalTransition.ENTRY:
            await self._handle_signal_entry(pair, signal)

    async def _handle_signal_entry(
        self, pair: str, signal: TradeSignal
    ) -> None:
        pair_cfg = self._config.get_pair(pair)
        instrument = pair_cfg.deribit_instrument
        ex = pair_cfg.execution
        risk = pair_cfg.risk

        direction = signal.direction

        if self._mode in ("live", "paper"):
            if direction == Direction.LONG:
                limit_price = self._deribit.get_best_bid(instrument)
            else:
                limit_price = self._deribit.get_best_ask(instrument)

            if limit_price is None:
                logger.warning("No book data, skipping entry", pair=pair)
                return

            limit_price = _round_to_tick(limit_price, instrument)
            size = risk.trade_size_eth
            min_size = MIN_ORDER_SIZES.get(instrument, 0.001)
            size = max(min_size, round(size / min_size) * min_size)
        else:
            mid = self._deribit.get_mid_price(instrument)
            if mid is None:
                logger.warning("No mid price, skipping entry", pair=pair)
                return
            limit_price = self._compute_limit_price(
                mid, direction, signal.signal_strength, ex
            )
            limit_price = _round_to_tick(limit_price, instrument)
            size = max(
                1,
                round(
                    signal.signal_strength
                    * signal.regime_multiplier
                    * risk.max_position_contracts
                ),
            )

        request = OrderRequest(
            pair_name=pair,
            instrument=instrument,
            direction=direction,
            size=float(size),
            limit_price=limit_price,
            post_only=ex.post_only,
            label=f"bp30_entry_{uuid.uuid4().hex[:8]}",
        )

        approved, reason = await self._risk.approve_order(request)
        if not approved:
            logger.warning("Order rejected by risk", pair=pair, reason=reason)
            return

        if self._mode == "live":
            order_id = await self._deribit.place_order(request)
        else:
            order_id = f"paper_{uuid.uuid4().hex[:8]}"
            logger.info(
                "Paper: bp30 entry",
                order_id=order_id,
                pair=pair,
                direction=direction.name,
                size=size,
                price=limit_price,
                strength=round(signal.signal_strength, 2),
                regime=signal.regime.value,
            )

        order_state = OrderState(
            order_id=order_id,
            request=request,
            status=OrderStatus.PLACED,
            placed_at=time.time(),
        )
        self._open_orders[order_id] = order_state
        await self._database.insert_order(order_state)

        task = asyncio.create_task(self._stale_check(order_id, pair))
        self._stale_tasks[order_id] = task

    def set_exit_fee_rate(self, fee_bps: float) -> None:
        self._exit_fee_rate = fee_bps / 10000

    def set_on_position_closed(
        self, callback: Callable[[str], None]
    ) -> None:
        self._on_position_closed = callback

    async def on_forced_exit(self, pair_name: str, reason: str) -> None:
        logger.info("Forced exit", pair=pair_name, reason=reason)
        pair_cfg = self._config.get_pair(pair_name)
        instrument = pair_cfg.deribit_instrument

        self._tp_orders.pop(pair_name, None)
        await self._cancel_pair_orders(pair_name)

        pos = self._positions.get(pair_name)
        if pos and pos.size > 0:
            close_dir = (
                Direction.SHORT
                if pos.direction == Direction.LONG
                else Direction.LONG
            )
            mid = self._deribit.get_mid_price(instrument)

            if self._mode == "live":
                try:
                    await self._deribit.place_market_order(
                        instrument, close_dir, pos.size, reduce_only=True
                    )
                except Exception as e:
                    logger.error("Forced exit market order failed", pair=pair_name, error=str(e))
            else:
                logger.info(
                    "Paper: market close",
                    pair=pair_name,
                    direction=close_dir.name,
                    size=pos.size,
                )

            exit_price = mid if mid else pos.entry_price
            await self._record_trade(pos, exit_price, reason)
            self._positions.pop(pair_name, None)
            self._risk.update_position(pair_name, None)

            if self._on_position_closed:
                self._on_position_closed(pair_name)

    @staticmethod
    def _compute_limit_price(
        mid: float,
        direction: Direction,
        signal_magnitude: float,
        ex,
    ) -> float:
        delta_bps = ex.offset_base_bps + ex.offset_conviction_bps * (
            1 - signal_magnitude
        )
        offset = mid * delta_bps / 10000
        if direction == Direction.LONG:
            return mid - offset
        else:
            return mid + offset

    async def _place_tp_order(self, pair: str, pos: Position) -> None:
        """Place a GTC reduce_only limit order at the TP price after entry fill."""
        pair_cfg = self._config.get_pair(pair)
        instrument = pair_cfg.deribit_instrument
        risk = pair_cfg.risk

        if pos.direction == Direction.LONG:
            tp_price = pos.entry_price * (1 + risk.take_profit_bps / 10000)
            close_dir = Direction.SHORT
        else:
            tp_price = pos.entry_price * (1 - risk.take_profit_bps / 10000)
            close_dir = Direction.LONG

        tp_price = _round_to_tick(tp_price, instrument)

        request = OrderRequest(
            pair_name=pair,
            instrument=instrument,
            direction=close_dir,
            size=pos.size,
            limit_price=tp_price,
            post_only=True,
            reduce_only=True,
            label=f"bp30_tp_{uuid.uuid4().hex[:8]}",
        )

        if self._mode == "live":
            try:
                order_id = await self._deribit.place_order(request)
            except Exception as e:
                logger.error("TP order placement failed", pair=pair, error=str(e))
                return
        else:
            order_id = f"paper_tp_{uuid.uuid4().hex[:8]}"
            logger.info(
                "Paper: TP order placed",
                order_id=order_id,
                pair=pair,
                direction=close_dir.name,
                size=pos.size,
                tp_price=tp_price,
                entry_price=pos.entry_price,
            )

        order_state = OrderState(
            order_id=order_id,
            request=request,
            status=OrderStatus.PLACED,
            placed_at=time.time(),
        )
        self._open_orders[order_id] = order_state
        self._tp_orders[pair] = order_id
        await self._database.insert_order(order_state)

    async def _stale_check(self, order_id: str, pair_name: str) -> None:
        pair_cfg = self._config.get_pair(pair_name)
        await asyncio.sleep(pair_cfg.execution.stale_order_seconds)

        order = self._open_orders.get(order_id)
        if order and order.status in (OrderStatus.PLACED, OrderStatus.PENDING):
            if self._mode == "live":
                try:
                    await self._deribit.cancel_order(order_id)
                except Exception as e:
                    logger.warning("Stale cancel failed", order_id=order_id, error=str(e))

            order.status = OrderStatus.CANCELLED
            order.cancel_reason = "stale"
            await self._database.update_order(
                order_id, status="cancelled", cancel_reason="stale"
            )
            logger.info("Stale order cancelled", order_id=order_id, pair=pair_name)
            self._open_orders.pop(order_id, None)

            if pair_name not in self._positions and self._on_position_closed:
                self._on_position_closed(pair_name)

    async def _on_book_update(self, data: dict) -> None:
        pass

    async def _on_order_update(self, data: dict) -> None:
        orders = data if isinstance(data, list) else [data]
        for order_data in orders:
            order_id = order_data.get("order_id", "")
            status = order_data.get("order_state", "")
            filled_amount = order_data.get("filled_amount", 0)

            tracked = self._open_orders.get(order_id)
            if not tracked:
                continue

            if status == "filled":
                tracked.status = OrderStatus.FILLED
                tracked.filled_size = filled_amount
                tracked.filled_at = time.time()
                tracked.fill_price = order_data.get("average_price", tracked.request.limit_price)

                await self._database.update_order(
                    order_id,
                    status="filled",
                    filled_size=filled_amount,
                    filled_at=tracked.filled_at,
                    fill_price=tracked.fill_price,
                )

                pair = tracked.request.pair_name
                is_tp_fill = self._tp_orders.get(pair) == order_id

                if is_tp_fill:
                    pos = self._positions.get(pair)
                    if pos:
                        exit_price = tracked.fill_price or tracked.request.limit_price
                        await self._record_trade(pos, exit_price, "take_profit")
                        self._positions.pop(pair, None)
                        self._risk.update_position(pair, None)
                        if self._on_position_closed:
                            self._on_position_closed(pair)
                    self._tp_orders.pop(pair, None)
                    self._open_orders.pop(order_id, None)
                else:
                    self._update_position_from_fill(tracked)
                    self._open_orders.pop(order_id, None)

                    stale = self._stale_tasks.pop(order_id, None)
                    if stale:
                        stale.cancel()

                    if not tracked.request.reduce_only:
                        pos = self._positions.get(pair)
                        if pos:
                            await self._place_tp_order(pair, pos)

            elif status == "cancelled":
                tracked.status = OrderStatus.CANCELLED
                reason = order_data.get("cancel_reason", "external")
                tracked.cancel_reason = reason
                await self._database.update_order(
                    order_id, status="cancelled", cancel_reason=reason
                )
                if reason != "stale":
                    logger.warning("Order cancelled externally", order_id=order_id, reason=reason)
                self._open_orders.pop(order_id, None)

                pair = tracked.request.pair_name
                if self._tp_orders.get(pair) == order_id:
                    self._tp_orders.pop(pair, None)

    async def _on_trade_update(self, data: dict) -> None:
        trades = data if isinstance(data, list) else [data]
        for trade in trades:
            logger.info(
                "Trade executed",
                order_id=trade.get("order_id"),
                price=trade.get("price"),
                amount=trade.get("amount"),
                direction=trade.get("direction"),
            )

    def _update_position_from_fill(self, order: OrderState) -> None:
        pair = order.request.pair_name
        fill_price = order.fill_price or order.request.limit_price
        fill_size = order.filled_size or order.request.size

        pos = self._positions.get(pair)
        if pos is None:
            if order.request.reduce_only:
                return
            self._positions[pair] = Position(
                pair_name=pair,
                instrument=order.request.instrument,
                direction=order.request.direction,
                size=fill_size,
                entry_price=fill_price,
                entry_time=time.time(),
                signal_at_entry=0.0,
            )
        else:
            if order.request.direction == pos.direction:
                total = pos.size + fill_size
                pos.entry_price = (
                    pos.entry_price * pos.size + fill_price * fill_size
                ) / total
                pos.size = total
            else:
                if fill_size >= pos.size:
                    self._positions.pop(pair, None)
                    self._risk.update_position(pair, None)
                    return
                pos.size -= fill_size

        updated_pos = self._positions.get(pair)
        if updated_pos:
            self._risk.update_position(
                pair,
                {
                    "size": updated_pos.size,
                    "direction": updated_pos.direction.value,
                    "entry_price": updated_pos.entry_price,
                    "entry_time": updated_pos.entry_time,
                },
            )
        else:
            self._risk.update_position(pair, None)

    async def _record_trade(
        self, pos: Position, exit_price: float, reason: str
    ) -> None:
        pair_cfg = self._config.get_pair(pos.pair_name)
        ex = pair_cfg.execution
        entry_fee_rate = ex.maker_fee_bps / 10000
        exit_fee_rate = ex.maker_fee_bps / 10000 if reason == "take_profit" else ex.taker_fee_bps / 10000

        direction_val = pos.direction.value
        gross_pnl = direction_val * (exit_price - pos.entry_price) * pos.size
        fees = (abs(pos.size * pos.entry_price) * entry_fee_rate
                + abs(pos.size * exit_price) * exit_fee_rate)
        net_pnl = gross_pnl - fees

        trade = TradeRecord(
            pair_name=pos.pair_name,
            instrument=pos.instrument,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            size=pos.size,
            entry_time=pos.entry_time,
            exit_time=time.time(),
            gross_pnl_usd=gross_pnl,
            fees_usd=fees,
            net_pnl_usd=net_pnl,
            signal_at_entry=pos.signal_at_entry,
            exit_reason=reason,
        )
        await self._database.insert_trade(trade)
        logger.info(
            "Trade recorded",
            pair=pos.pair_name,
            direction=pos.direction.name,
            pnl=round(net_pnl, 2),
            reason=reason,
        )

    async def _cancel_pair_orders(self, pair_name: str) -> None:
        pair_cfg = self._config.get_pair(pair_name)
        instrument = pair_cfg.deribit_instrument

        to_cancel = [
            oid
            for oid, o in self._open_orders.items()
            if o.request.pair_name == pair_name
            and o.status in (OrderStatus.PLACED, OrderStatus.PENDING)
        ]

        if self._mode == "live" and to_cancel:
            try:
                await self._deribit.cancel_all(instrument)
            except Exception as e:
                logger.warning("Cancel all failed", pair=pair_name, error=str(e))

        for oid in to_cancel:
            order = self._open_orders.pop(oid, None)
            if order:
                order.status = OrderStatus.CANCELLED
                order.cancel_reason = "signal_exit"
                await self._database.update_order(
                    oid, status="cancelled", cancel_reason="signal_exit"
                )
            stale = self._stale_tasks.pop(oid, None)
            if stale:
                stale.cancel()

    def get_position(self, pair_name: str) -> Optional[Position]:
        return self._positions.get(pair_name)

    def get_open_orders(self, pair_name: str) -> list[OrderState]:
        return [
            o
            for o in self._open_orders.values()
            if o.request.pair_name == pair_name
            and o.status in (OrderStatus.PLACED, OrderStatus.PENDING)
        ]

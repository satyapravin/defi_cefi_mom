from __future__ import annotations

import asyncio
import math
import time
import uuid
from typing import Optional

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
    SignalState,
    SignalTransition,
    TradeRecord,
)
from risk_manager import RiskManager

logger = setup_logger()

TICK_SIZES = {
    "ETH-PERPETUAL": 0.5,
    "BTC-PERPETUAL": 0.5,
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

    async def on_signal(self, state: SignalState) -> None:
        pair = state.pair_name
        logger.info(
            "Signal received",
            pair=pair,
            transition=state.transition.value,
            signal=round(state.combined_signal, 4),
        )

        handlers = {
            SignalTransition.ENTRY: self._handle_entry,
            SignalTransition.EXIT: self._handle_exit,
            SignalTransition.REVERSAL: self._handle_reversal,
            SignalTransition.SCALE: self._handle_scale,
            SignalTransition.REDUCE: self._handle_reduce,
        }

        handler = handlers.get(state.transition)
        if handler:
            await handler(pair, state)

    async def on_forced_exit(self, pair_name: str, reason: str) -> None:
        logger.info("Forced exit", pair=pair_name, reason=reason)
        pair_cfg = self._config.get_pair(pair_name)
        instrument = pair_cfg.deribit_instrument

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
                await self._deribit.place_market_order(
                    instrument, close_dir, pos.size, reduce_only=True
                )
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

    async def _handle_entry(self, pair: str, state: SignalState) -> None:
        pair_cfg = self._config.get_pair(pair)
        instrument = pair_cfg.deribit_instrument
        ex = pair_cfg.execution
        risk = pair_cfg.risk

        mid = self._deribit.get_mid_price(instrument)
        if mid is None:
            logger.warning("No mid price available, skipping entry", pair=pair)
            return

        direction = Direction.LONG if state.combined_signal > 0 else Direction.SHORT
        limit_price = self._compute_limit_price(
            mid, direction, abs(state.combined_signal), ex
        )
        limit_price = _round_to_tick(limit_price, instrument)

        size = max(1, round(abs(state.combined_signal) * risk.max_position_contracts))

        request = OrderRequest(
            pair_name=pair,
            instrument=instrument,
            direction=direction,
            size=float(size),
            limit_price=limit_price,
            post_only=ex.post_only,
            label=f"entry_{uuid.uuid4().hex[:8]}",
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
                "Paper: order placed",
                order_id=order_id,
                pair=pair,
                direction=direction.name,
                size=size,
                price=limit_price,
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

    async def _handle_exit(self, pair: str, state: SignalState) -> None:
        pair_cfg = self._config.get_pair(pair)
        instrument = pair_cfg.deribit_instrument

        await self._cancel_pair_orders(pair)

        pos = self._positions.get(pair)
        if pos and pos.size > 0:
            close_dir = (
                Direction.SHORT
                if pos.direction == Direction.LONG
                else Direction.LONG
            )
            mid = self._deribit.get_mid_price(instrument)

            if self._mode == "live":
                await self._deribit.place_market_order(
                    instrument, close_dir, pos.size, reduce_only=True
                )
            else:
                logger.info(
                    "Paper: market close",
                    pair=pair,
                    direction=close_dir.name,
                    size=pos.size,
                )

            exit_price = mid if mid else pos.entry_price
            await self._record_trade(pos, exit_price, "signal_exit")
            self._positions.pop(pair, None)
            self._risk.update_position(pair, None)

    async def _handle_reversal(self, pair: str, state: SignalState) -> None:
        exit_state = SignalState(
            pair_name=state.pair_name,
            timestamp=state.timestamp,
            conviction_30=state.conviction_30,
            trend_state_30=state.trend_state_30,
            momentum_5=state.momentum_5,
            autocorrelation_5=state.autocorrelation_5,
            momentum_flag_5=state.momentum_flag_5,
            combined_signal=0.0,
            transition=SignalTransition.EXIT,
            intensity_30=state.intensity_30,
        )
        await self._handle_exit(pair, exit_state)
        await self._handle_entry(pair, state)

    async def _handle_scale(self, pair: str, state: SignalState) -> None:
        pair_cfg = self._config.get_pair(pair)
        risk = pair_cfg.risk
        instrument = pair_cfg.deribit_instrument
        ex = pair_cfg.execution

        target_size = max(
            1, round(abs(state.combined_signal) * risk.max_position_contracts)
        )
        pos = self._positions.get(pair)
        current_size = pos.size if pos else 0

        if target_size <= current_size:
            return

        diff = target_size - current_size
        mid = self._deribit.get_mid_price(instrument)
        if mid is None:
            return

        direction = Direction.LONG if state.combined_signal > 0 else Direction.SHORT
        limit_price = self._compute_limit_price(
            mid, direction, abs(state.combined_signal), ex
        )
        limit_price = _round_to_tick(limit_price, instrument)

        request = OrderRequest(
            pair_name=pair,
            instrument=instrument,
            direction=direction,
            size=float(diff),
            limit_price=limit_price,
            post_only=ex.post_only,
            label=f"scale_{uuid.uuid4().hex[:8]}",
        )

        approved, reason = await self._risk.approve_order(request)
        if not approved:
            logger.warning("Scale order rejected", pair=pair, reason=reason)
            return

        if self._mode == "live":
            order_id = await self._deribit.place_order(request)
        else:
            order_id = f"paper_{uuid.uuid4().hex[:8]}"
            logger.info("Paper: scale order", order_id=order_id, size=diff)

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

    async def _handle_reduce(self, pair: str, state: SignalState) -> None:
        pair_cfg = self._config.get_pair(pair)
        risk = pair_cfg.risk
        instrument = pair_cfg.deribit_instrument
        ex = pair_cfg.execution

        target_size = max(
            1, round(abs(state.combined_signal) * risk.max_position_contracts)
        )
        pos = self._positions.get(pair)
        if not pos or pos.size <= target_size:
            return

        diff = pos.size - target_size
        mid = self._deribit.get_mid_price(instrument)
        if mid is None:
            return

        close_dir = (
            Direction.SHORT if pos.direction == Direction.LONG else Direction.LONG
        )
        limit_price = self._compute_limit_price(
            mid, close_dir, abs(state.combined_signal), ex
        )
        limit_price = _round_to_tick(limit_price, instrument)

        request = OrderRequest(
            pair_name=pair,
            instrument=instrument,
            direction=close_dir,
            size=float(diff),
            limit_price=limit_price,
            post_only=ex.post_only,
            reduce_only=True,
            label=f"reduce_{uuid.uuid4().hex[:8]}",
        )

        if self._mode == "live":
            order_id = await self._deribit.place_order(request)
        else:
            order_id = f"paper_{uuid.uuid4().hex[:8]}"
            logger.info("Paper: reduce order", order_id=order_id, size=diff)

        order_state = OrderState(
            order_id=order_id,
            request=request,
            status=OrderStatus.PLACED,
            placed_at=time.time(),
        )
        self._open_orders[order_id] = order_state
        await self._database.insert_order(order_state)

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

                self._update_position_from_fill(tracked)
                self._open_orders.pop(order_id, None)

                stale = self._stale_tasks.pop(order_id, None)
                if stale:
                    stale.cancel()

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
        direction_val = pos.direction.value
        gross_pnl = direction_val * (exit_price - pos.entry_price) * pos.size
        fees = abs(pos.size * exit_price) * 0.0005  # estimated taker fee
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

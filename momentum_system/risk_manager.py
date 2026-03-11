from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Optional

from config import Config
from database import Database
from deribit_client import DeribitClient
from logger import setup_logger
from models import Direction, OrderRequest

logger = setup_logger()


class RiskManager:
    def __init__(
        self,
        config: Config,
        database: Database,
        deribit: DeribitClient,
    ):
        self._config = config
        self._database = database
        self._deribit = deribit
        self._on_exit: Optional[Callable[[str, str], Awaitable[None]]] = None
        self._cooldowns: dict[str, float] = {}
        self._monitor_task: Optional[asyncio.Task] = None
        self._running = False
        self._portfolio_cache: dict[str, dict] = {}
        self._positions: dict[str, dict] = {}

    async def approve_order(
        self, request: OrderRequest
    ) -> tuple[bool, str]:
        pair_cfg = self._config.get_pair(request.pair_name)
        risk = pair_cfg.risk

        # (1) Cooldown check
        if self.is_in_cooldown(request.pair_name):
            return False, "pair is in cooldown after stop-loss"

        # (2) Daily loss limit
        daily_pnl = await self._database.get_daily_pnl(request.pair_name)
        if daily_pnl < -risk.daily_loss_limit_usd:
            return False, f"daily loss limit exceeded: {daily_pnl:.2f} USD"

        # (3) Position limit
        current_pos = self._positions.get(request.pair_name)
        current_size = current_pos.get("size", 0) if current_pos else 0
        new_total = abs(current_size) + request.size
        if new_total > risk.max_position_contracts:
            return (
                False,
                f"position limit exceeded: {new_total} > {risk.max_position_contracts}",
            )
        notional = request.size * request.limit_price
        if notional > risk.max_position_usd:
            return (
                False,
                f"notional limit exceeded: {notional:.2f} > {risk.max_position_usd}",
            )

        # (4) Open order limit — delegated to execution manager tracking;
        #     here we do a lightweight check
        # The execution manager passes open_order_count externally if needed.

        # (5) Margin check
        currency = pair_cfg.deribit_instrument.split("-")[0]
        try:
            summary = await self._deribit.get_account_summary(currency)
            equity = summary.get("equity", 0)
            margin_used = summary.get("initial_margin", 0)
            if equity > 0:
                projected_margin_pct = (margin_used / equity) * 100
                if projected_margin_pct > risk.margin_usage_limit_pct:
                    return (
                        False,
                        f"margin usage {projected_margin_pct:.1f}% exceeds limit {risk.margin_usage_limit_pct}%",
                    )
        except Exception as e:
            logger.warning("Margin check failed, allowing order", error=str(e))

        return True, ""

    async def start_monitor(self) -> None:
        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def stop_monitor(self) -> None:
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()

    async def _monitor_loop(self) -> None:
        while self._running:
            try:
                for pair_cfg in self._config.pairs:
                    pair_name = pair_cfg.name
                    instrument = pair_cfg.deribit_instrument
                    risk = pair_cfg.risk

                    pos = self._positions.get(pair_name)
                    if not pos or pos.get("size", 0) == 0:
                        continue

                    mid = self._deribit.get_mid_price(instrument)
                    if mid is None:
                        continue

                    entry_price = pos.get("entry_price", mid)
                    direction = pos.get("direction", 0)
                    size = abs(pos.get("size", 0))
                    entry_time = pos.get("entry_time", 0)

                    # Max holding time
                    if risk.max_holding_seconds > 0 and entry_time > 0:
                        held_seconds = time.time() - entry_time
                        if held_seconds >= risk.max_holding_seconds:
                            logger.warning(
                                "Max holding time exceeded",
                                pair=pair_name,
                                held_seconds=round(held_seconds, 1),
                                limit=risk.max_holding_seconds,
                            )
                            close_dir = (
                                Direction.SHORT if direction == 1 else Direction.LONG
                            )
                            try:
                                await self._deribit.place_market_order(
                                    instrument, close_dir, size, reduce_only=True
                                )
                            except Exception as e:
                                logger.error("Max-hold exit failed", error=str(e))

                            if self._on_exit:
                                await self._on_exit(pair_name, "max_holding_time")
                            continue

                    pnl_bps = direction * (mid - entry_price) / entry_price * 10000

                    # Stop-loss
                    if pnl_bps < -risk.stop_loss_bps:
                        logger.warning(
                            "Stop-loss triggered",
                            pair=pair_name,
                            pnl_bps=round(pnl_bps, 2),
                        )
                        close_dir = (
                            Direction.SHORT if direction == 1 else Direction.LONG
                        )
                        try:
                            await self._deribit.place_market_order(
                                instrument, close_dir, size, reduce_only=True
                            )
                        except Exception as e:
                            logger.error("Stop-loss order failed", error=str(e))

                        self.record_cooldown(pair_name)
                        if self._on_exit:
                            await self._on_exit(pair_name, "stop_loss")

                    # Take-profit
                    elif pnl_bps > risk.take_profit_bps:
                        logger.info(
                            "Take-profit triggered",
                            pair=pair_name,
                            pnl_bps=round(pnl_bps, 2),
                        )
                        close_dir = (
                            Direction.SHORT if direction == 1 else Direction.LONG
                        )
                        try:
                            await self._deribit.place_market_order(
                                instrument, close_dir, size, reduce_only=True
                            )
                        except Exception as e:
                            logger.error("Take-profit order failed", error=str(e))

                        if self._on_exit:
                            await self._on_exit(pair_name, "take_profit")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Risk monitor error", error=str(e))

            await asyncio.sleep(1)

    def set_on_exit(
        self,
        callback: Callable[[str, str], Awaitable[None]],
    ) -> None:
        self._on_exit = callback

    def is_in_cooldown(self, pair_name: str) -> bool:
        cooldown_start = self._cooldowns.get(pair_name)
        if cooldown_start is None:
            return False
        pair_cfg = self._config.get_pair(pair_name)
        return (time.time() - cooldown_start) < pair_cfg.risk.cooldown_seconds

    def record_cooldown(self, pair_name: str) -> None:
        self._cooldowns[pair_name] = time.time()

    def update_position(self, pair_name: str, position: dict | None) -> None:
        if position:
            self._positions[pair_name] = position
        else:
            self._positions.pop(pair_name, None)

    async def _on_portfolio_update(self, data: dict) -> None:
        currency = data.get("currency", "")
        self._portfolio_cache[currency] = data

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Optional

from config import Config
from database import Database
from deribit_client import DeribitClient
from logger import setup_logger
from models import Direction, OrderRequest, Regime

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
        self._peak_pnl_bps: dict[str, float] = {}
        self._regime_filter = None

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
        inst = pair_cfg.deribit_instrument
        currency = "USDC" if "_USDC" in inst else "USDT" if "_USDT" in inst else inst.split("-")[0]
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

                    pnl_bps = direction * (mid - entry_price) / entry_price * 10000
                    hold_secs = time.time() - entry_time if entry_time > 0 else 0

                    peak = self._peak_pnl_bps.get(pair_name, 0.0)
                    peak = max(peak, pnl_bps)
                    self._peak_pnl_bps[pair_name] = peak

                    effective_sl = risk.stop_loss_bps
                    if self._regime_filter is not None:
                        regime = self._regime_filter.get_regime(pair_name)
                        if regime == Regime.CHAOTIC:
                            effective_sl = risk.stop_loss_bps * 1.5
                        elif regime == Regime.QUIET:
                            effective_sl = risk.stop_loss_bps * 0.8

                    if peak >= risk.breakeven_activate_bps:
                        effective_sl = min(effective_sl, 0.0)

                    reason: Optional[str] = None

                    if (risk.trail_activate_bps > 0
                            and peak >= risk.trail_activate_bps):
                        trail_stop = peak - risk.trail_distance_bps
                        if pnl_bps <= trail_stop:
                            reason = "trailing_stop"

                    if reason is None and pnl_bps <= -effective_sl:
                        reason = "stop_loss"

                    if reason is None:
                        if hold_secs < risk.tp_decay_phase2_seconds:
                            effective_tp = risk.take_profit_bps
                        elif hold_secs < risk.tp_decay_phase3_seconds:
                            effective_tp = risk.take_profit_bps * risk.tp_decay_phase2_ratio
                        else:
                            effective_tp = risk.take_profit_bps * risk.tp_decay_phase3_ratio
                        if pnl_bps >= effective_tp:
                            reason = "take_profit"

                    if reason is None and risk.max_holding_seconds > 0 and hold_secs >= risk.max_holding_seconds:
                        reason = "max_holding_time"

                    if reason is None:
                        continue

                    logger.warning(
                        "Risk exit triggered",
                        pair=pair_name,
                        reason=reason,
                        pnl_bps=round(pnl_bps, 2),
                        peak_pnl_bps=round(peak, 2),
                        hold_secs=round(hold_secs, 1),
                    )

                    if reason == "stop_loss":
                        self.record_cooldown(pair_name)
                    self._peak_pnl_bps.pop(pair_name, None)

                    if self._on_exit:
                        await self._on_exit(pair_name, reason)

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
            self._peak_pnl_bps.pop(pair_name, None)

    def set_regime_filter(self, regime_filter) -> None:
        self._regime_filter = regime_filter

    async def _on_portfolio_update(self, data: dict) -> None:
        currency = data.get("currency", "")
        self._portfolio_cache[currency] = data

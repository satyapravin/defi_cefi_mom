from __future__ import annotations

import asyncio
import bisect
import math
from typing import Optional

import numpy as np
from pydantic import BaseModel

from config import Config, load_config
from database import Database
from logger import setup_logger
from models import (
    Direction,
    FeeTier,
    Regime,
    SignalTransition,
    SwapEvent,
    TradeRecord,
    TradeSignal,
)
from regime_filter import RegimeFilter
from signal_30bps import BP30SignalEngine

logger = setup_logger()


class BacktestResult(BaseModel):
    pair_name: str
    start_time: float
    end_time: float
    total_signals: int
    filled_signals: int
    fill_rate: float
    total_trades: int
    win_rate: float
    avg_gross_return_bps: float
    avg_net_return_bps: float
    total_pnl_usd: float
    max_drawdown_usd: float
    max_drawdown_bps: float
    sharpe_ratio: float
    avg_holding_seconds: float
    trades: list[TradeRecord]
    equity_curve: list[tuple[float, float]]
    alpha_decay_curve: list[tuple[int, float]] = []
    regime_distribution: dict[str, int] = {}


class _SimulatedPosition:
    __slots__ = (
        "direction", "size", "entry_price", "entry_time",
        "signal_at_entry", "peak_pnl_bps",
    )

    def __init__(
        self,
        direction: Direction,
        size: float,
        entry_price: float,
        entry_time: float,
        signal_at_entry: float,
    ):
        self.direction = direction
        self.size = size
        self.entry_price = entry_price
        self.entry_time = entry_time
        self.signal_at_entry = signal_at_entry
        self.peak_pnl_bps = 0.0


class BacktestEngine:
    def __init__(
        self,
        config: Config,
        database: Database,
        execution_lag_blocks: int = 1,
        min_signal_strength: float = 0.0,
        entry_mode: str = "maker",
        entry_fee_bps: float = 0.0,
        exit_fee_bps: float = 5.0,
    ):
        self._config = config
        self._database = database
        self._execution_lag_blocks = execution_lag_blocks
        self._min_signal_strength = min_signal_strength
        self._entry_mode = entry_mode
        self._entry_fee_rate = entry_fee_bps / 10000
        self._exit_fee_rate = exit_fee_bps / 10000

    async def run_backtest(
        self,
        pair_name: str,
        start_timestamp: int,
        end_timestamp: int,
    ) -> BacktestResult:
        pair_cfg = self._config.get_pair(pair_name)
        ex = pair_cfg.execution
        risk = pair_cfg.risk

        events = await self._database.get_all_swap_events_in_range(
            pair_name, start_timestamp, end_timestamp
        )

        empty = BacktestResult(
            pair_name=pair_name,
            start_time=float(start_timestamp),
            end_time=float(end_timestamp),
            total_signals=0,
            filled_signals=0,
            fill_rate=0.0,
            total_trades=0,
            win_rate=0.0,
            avg_gross_return_bps=0.0,
            avg_net_return_bps=0.0,
            total_pnl_usd=0.0,
            max_drawdown_usd=0.0,
            max_drawdown_bps=0.0,
            sharpe_ratio=0.0,
            avg_holding_seconds=0.0,
            trades=[],
            equity_curve=[],
        )

        if not events:
            return empty

        logger.info(
            "Backtest starting",
            pair=pair_name,
            swap_events=len(events),
        )

        regime_filter = RegimeFilter(self._config)

        ref_events = [e for e in events if e.fee_tier == FeeTier.BP5]
        ref_prices = {e.block_timestamp: e.price for e in ref_events}
        ref_timestamps = sorted(ref_prices.keys())

        trades: list[TradeRecord] = []
        position: Optional[_SimulatedPosition] = None
        filled_count = 0
        signal_count = 0
        max_hold = risk.max_holding_seconds
        last_loss_time = 0.0
        regime_counts: dict[str, int] = {"quiet": 0, "active": 0, "chaotic": 0}

        def _try_close_at_price(price: float, ts: float) -> None:
            """O(1) SL/TP/max-hold check with trailing stop, time-decay TP,
            breakeven stop, and regime-aware SL."""
            nonlocal position, last_loss_time

            if position is None:
                return

            pnl_bps = (
                position.direction.value
                * (price - position.entry_price)
                / position.entry_price
                * 10000
            )
            position.peak_pnl_bps = max(position.peak_pnl_bps, pnl_bps)
            hold_secs = ts - position.entry_time

            regime = regime_filter.get_regime(pair_name)
            if regime == Regime.CHAOTIC:
                effective_sl = risk.stop_loss_bps * 1.5
            elif regime == Regime.QUIET:
                effective_sl = risk.stop_loss_bps * 0.8
            else:
                effective_sl = risk.stop_loss_bps

            if position.peak_pnl_bps >= risk.breakeven_activate_bps:
                effective_sl = min(effective_sl, 0.0)

            reason = None

            if (risk.trail_activate_bps > 0
                    and position.peak_pnl_bps >= risk.trail_activate_bps):
                trail_stop = position.peak_pnl_bps - risk.trail_distance_bps
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

            if reason is None and max_hold > 0 and hold_secs >= max_hold:
                reason = "max_holding_time"

            if reason is None:
                return

            trade = self._close_position(
                position, price, ts,
                reason, pair_name, pair_cfg.deribit_instrument,
            )
            trades.append(trade)
            if trade.net_pnl_usd < 0:
                last_loss_time = ts
            position = None
            signal_engine.notify_position_closed(pair_name)

        async def on_signal(sig: TradeSignal) -> None:
            """Process signal inline during event replay."""
            nonlocal position, filled_count, signal_count
            signal_count += 1

            ref_price_at_sig = self._get_ref_price(sig.timestamp, ref_prices, ref_timestamps)
            if ref_price_at_sig is not None:
                _try_close_at_price(ref_price_at_sig, sig.timestamp)

            if sig.transition != SignalTransition.ENTRY:
                return
            if position is not None:
                return
            if last_loss_time > 0 and sig.timestamp - last_loss_time < risk.cooldown_seconds:
                return

            price = self._get_ref_price(sig.timestamp, ref_prices, ref_timestamps)
            if price is None:
                return

            if sig.signal_strength < self._min_signal_strength:
                signal_engine.notify_position_closed(pair_name)
                return

            if sig.regime_multiplier <= 0:
                signal_engine.notify_position_closed(pair_name)
                return

            direction = sig.direction
            size = max(
                1,
                round(
                    sig.signal_strength
                    * sig.regime_multiplier
                    * risk.max_position_contracts
                ),
            )

            if self._entry_mode == "maker":
                limit_price = self._compute_limit_price(
                    price, direction, sig.signal_strength, ex
                )
                fill_price, fill_time = self._scan_for_fill(
                    direction, limit_price, sig.timestamp,
                    ex.stale_order_seconds, ref_events,
                )
                if fill_price is None:
                    signal_engine.notify_position_closed(pair_name)
                    return
            else:
                fill_price, fill_time = price, sig.timestamp

            position = _SimulatedPosition(
                direction=direction,
                size=float(size),
                entry_price=fill_price,
                entry_time=fill_time,
                signal_at_entry=sig.signal_strength,
            )
            filled_count += 1

        signal_engine = BP30SignalEngine(
            self._config,
            regime_filter,
            on_signal,
        )

        for event in events:
            regime_filter.on_swap(event)

            if event.fee_tier == FeeTier.BP5:
                _try_close_at_price(event.price, float(event.block_timestamp))

            await signal_engine.on_swap(event)

            regime = regime_filter.get_regime(pair_name)
            regime_counts[regime.value] += 1

        if position is not None and ref_events:
            last_price = ref_events[-1].price
            last_ts = float(ref_events[-1].block_timestamp)
            trade = self._close_position(
                position, last_price, last_ts, "end_of_backtest",
                pair_name, pair_cfg.deribit_instrument,
            )
            trades.append(trade)

        alpha_decay = self._compute_alpha_decay(events)

        actual_start = events[0].block_timestamp if events else start_timestamp
        actual_end = events[-1].block_timestamp if events else end_timestamp

        result = self._compute_metrics(
            pair_name, actual_start, actual_end,
            signal_count, filled_count, trades, alpha_decay,
        )
        result.regime_distribution = regime_counts
        return result

    @staticmethod
    def _compute_limit_price(mid: float, direction: Direction, signal_mag: float, ex) -> float:
        delta_bps = ex.offset_base_bps + ex.offset_conviction_bps * (1 - signal_mag)
        offset = mid * delta_bps / 10000
        return mid - offset if direction == Direction.LONG else mid + offset

    @staticmethod
    def _get_ref_price(
        timestamp: float, ref_prices: dict[int, float], sorted_ts: list[int]
    ) -> Optional[float]:
        if not sorted_ts:
            return None
        ts = int(timestamp)
        idx = bisect.bisect_right(sorted_ts, ts) - 1
        if idx >= 0:
            return ref_prices[sorted_ts[idx]]
        return None

    @staticmethod
    def _scan_for_fill(
        direction: Direction,
        limit_price: float,
        signal_time: float,
        stale_seconds: float,
        ref_events: list[SwapEvent],
        _ts_cache: dict = {},
    ) -> tuple[Optional[float], float]:
        cutoff = signal_time + stale_seconds

        cache_id = id(ref_events)
        if cache_id not in _ts_cache:
            _ts_cache[cache_id] = [e.block_timestamp for e in ref_events]
        ts_list = _ts_cache[cache_id]

        lo = bisect.bisect_left(ts_list, int(signal_time))

        for i in range(lo, len(ref_events)):
            e = ref_events[i]
            if e.block_timestamp > cutoff:
                break
            if direction == Direction.LONG and e.price <= limit_price:
                return limit_price, float(e.block_timestamp)
            if direction == Direction.SHORT and e.price >= limit_price:
                return limit_price, float(e.block_timestamp)
        return None, 0.0

    def _close_position(
        self,
        pos: _SimulatedPosition,
        exit_price: float,
        exit_time: float,
        reason: str,
        pair_name: str,
        instrument: str,
    ) -> TradeRecord:
        gross = pos.direction.value * (exit_price - pos.entry_price) * pos.size
        fees = (abs(pos.size * pos.entry_price) * self._entry_fee_rate
                + abs(pos.size * exit_price) * self._exit_fee_rate)
        return TradeRecord(
            pair_name=pair_name,
            instrument=instrument,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            size=pos.size,
            entry_time=pos.entry_time,
            exit_time=exit_time,
            gross_pnl_usd=gross,
            fees_usd=fees,
            net_pnl_usd=gross - fees,
            signal_at_entry=pos.signal_at_entry,
            exit_reason=reason,
        )

    @staticmethod
    def _compute_alpha_decay(events: list[SwapEvent]) -> list[tuple[int, float]]:
        bp30_events = [e for e in events if e.fee_tier == FeeTier.BP30 and e.log_return is not None]
        bp5_events = [e for e in events if e.fee_tier == FeeTier.BP5]

        if not bp30_events or not bp5_events:
            return []

        horizons = [1, 2, 5, 10, 20, 50, 100]
        continuations: dict[int, list[float]] = {h: [] for h in horizons}

        ref_timestamps = [e.block_timestamp for e in bp5_events]
        ref_prices = [e.price for e in bp5_events]

        for ev30 in bp30_events:
            trigger_ts = ev30.block_timestamp
            d_k = 1 if ev30.log_return > 0 else -1

            start_idx = bisect.bisect_left(ref_timestamps, trigger_ts)
            if start_idx >= len(ref_timestamps):
                continue

            trigger_price_log = math.log(ref_prices[start_idx])

            for h in horizons:
                idx = start_idx + h
                if idx < len(ref_prices):
                    future_price_log = math.log(ref_prices[idx])
                    cont = d_k * (future_price_log - trigger_price_log) * 10000
                    continuations[h].append(cont)

        curve = []
        for h in horizons:
            vals = continuations[h]
            avg = float(np.mean(vals)) if vals else 0.0
            curve.append((h, avg))

        return curve

    @staticmethod
    def _compute_metrics(
        pair_name: str,
        start_ts: int,
        end_ts: int,
        total_signals: int,
        filled_signals: int,
        trades: list[TradeRecord],
        alpha_decay: list[tuple[int, float]],
    ) -> BacktestResult:
        total_trades = len(trades)
        wins = sum(1 for t in trades if t.net_pnl_usd > 0)
        win_rate = wins / total_trades if total_trades else 0.0
        fill_rate = filled_signals / total_signals if total_signals else 0.0

        gross_returns_bps = []
        net_returns_bps = []
        holding_times = []
        for t in trades:
            if t.entry_price > 0:
                ret_bps = t.direction.value * (t.exit_price - t.entry_price) / t.entry_price * 10000
                gross_returns_bps.append(ret_bps)
                net_ret_bps = ret_bps - (t.fees_usd / (t.size * t.entry_price) * 10000 if t.size * t.entry_price > 0 else 0)
                net_returns_bps.append(net_ret_bps)
            holding_times.append(t.exit_time - t.entry_time)

        avg_gross = float(np.mean(gross_returns_bps)) if gross_returns_bps else 0.0
        avg_net = float(np.mean(net_returns_bps)) if net_returns_bps else 0.0
        avg_holding = float(np.mean(holding_times)) if holding_times else 0.0

        total_pnl = sum(t.net_pnl_usd for t in trades)

        equity_curve: list[tuple[float, float]] = []
        cum_pnl = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in sorted(trades, key=lambda x: x.exit_time):
            cum_pnl += t.net_pnl_usd
            equity_curve.append((t.exit_time, cum_pnl))
            peak = max(peak, cum_pnl)
            dd = peak - cum_pnl
            max_dd = max(max_dd, dd)

        if net_returns_bps and len(net_returns_bps) > 1:
            arr = np.array(net_returns_bps)
            mean_ret = float(arr.mean())
            std_ret = float(arr.std(ddof=1))
            backtest_span = end_ts - start_ts
            if std_ret > 0 and backtest_span > 0:
                trades_per_year = total_trades / (backtest_span / (365.25 * 24 * 3600))
                sharpe = (mean_ret / std_ret) * math.sqrt(trades_per_year)
            else:
                sharpe = 0.0
        else:
            sharpe = 0.0

        max_dd_bps = 0.0
        if trades and trades[0].entry_price > 0:
            notional = trades[0].size * trades[0].entry_price
            if notional > 0:
                max_dd_bps = max_dd / notional * 10000

        return BacktestResult(
            pair_name=pair_name,
            start_time=float(start_ts),
            end_time=float(end_ts),
            total_signals=total_signals,
            filled_signals=filled_signals,
            fill_rate=fill_rate,
            total_trades=total_trades,
            win_rate=win_rate,
            avg_gross_return_bps=avg_gross,
            avg_net_return_bps=avg_net,
            total_pnl_usd=total_pnl,
            max_drawdown_usd=max_dd,
            max_drawdown_bps=max_dd_bps,
            sharpe_ratio=sharpe,
            avg_holding_seconds=avg_holding,
            trades=trades,
            equity_curve=equity_curve,
            alpha_decay_curve=alpha_decay,
        )


async def main() -> None:
    config = load_config("config.yaml")
    db = Database(config.system.database_path)
    await db.initialize()

    engine = BacktestEngine(config, db, execution_lag_blocks=1)
    result = await engine.run_backtest(
        pair_name="ETH-USDC",
        start_timestamp=0,
        end_timestamp=int(2e9),
    )

    print(f"Signals emitted: {result.total_signals}")
    print(f"Fill rate: {result.fill_rate:.1%}")
    print(f"Win rate: {result.win_rate:.1%}")
    print(f"Avg net return: {result.avg_net_return_bps:.1f} bps")
    print(f"Sharpe: {result.sharpe_ratio:.2f}")
    print(f"Max DD: ${result.max_drawdown_usd:.2f}")
    print(f"Total PnL: ${result.total_pnl_usd:,.2f}")
    print(f"Total trades: {result.total_trades}")
    print(f"Avg holding time: {result.avg_holding_seconds:.0f}s")

    if result.alpha_decay_curve:
        print("\nAlpha Decay Curve:")
        for horizon, continuation in result.alpha_decay_curve:
            print(f"  +{horizon} events: {continuation:.2f} bps")

    if result.trades:
        print(f"\nExit reasons:")
        reasons: dict[str, list[float]] = {}
        for t in result.trades:
            reasons.setdefault(t.exit_reason, []).append(t.net_pnl_usd)
        for reason, pnls in sorted(reasons.items()):
            avg_pnl = sum(pnls) / len(pnls)
            print(f"  {reason}: {len(pnls)} trades, avg PnL ${avg_pnl:+.2f}")


if __name__ == "__main__":
    asyncio.run(main())

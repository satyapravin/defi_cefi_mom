from __future__ import annotations

import asyncio
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
    SignalState,
    SignalTransition,
    SwapEvent,
    TradeRecord,
)
from signal_engine import SignalEngine

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


class _SimulatedPosition:
    __slots__ = ("direction", "size", "entry_price", "entry_time", "signal_at_entry")

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


class BacktestEngine:
    def __init__(
        self,
        config: Config,
        database: Database,
        execution_lag_blocks: int = 1,
    ):
        self._config = config
        self._database = database
        self._execution_lag_blocks = execution_lag_blocks

    async def run(
        self,
        pair_name: str,
        start_timestamp: int,
        end_timestamp: int,
    ) -> BacktestResult:
        pair_cfg = self._config.get_pair(pair_name)
        ex = pair_cfg.execution
        risk = pair_cfg.risk
        signal_cfg = pair_cfg.signal

        events = await self._database.get_all_swap_events_in_range(
            pair_name, start_timestamp, end_timestamp
        )

        if not events:
            return BacktestResult(
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

        logger.info(
            "Backtest starting",
            pair=pair_name,
            events=len(events),
            start=start_timestamp,
            end=end_timestamp,
        )

        bp1_events = [e for e in events if e.fee_tier == FeeTier.BP1]
        bp1_prices = {e.block_timestamp: e.price for e in bp1_events}
        bp1_timestamps = sorted(bp1_prices.keys())

        signals_emitted: list[SignalState] = []

        async def capture_signal(sig: SignalState) -> None:
            signals_emitted.append(sig)

        signal_engine = SignalEngine(self._config, self._database, capture_signal)

        for event in events:
            await signal_engine.on_event(event)

        trades: list[TradeRecord] = []
        position: Optional[_SimulatedPosition] = None
        filled_count = 0
        max_hold = risk.max_holding_seconds

        for sig in signals_emitted:
            # Force-exit if position has exceeded max holding time
            if position is not None and max_hold > 0:
                if sig.timestamp - position.entry_time >= max_hold:
                    exit_price = self._get_bp1_price(
                        sig.timestamp, bp1_prices, bp1_timestamps
                    )
                    if exit_price is not None:
                        trade = self._close_position(
                            position, exit_price, sig.timestamp,
                            "max_holding_time", pair_name, pair_cfg.deribit_instrument,
                        )
                        trades.append(trade)
                    position = None

            if sig.transition == SignalTransition.ENTRY:
                if position is not None:
                    continue

                ref_price = self._get_bp1_price(sig.timestamp, bp1_prices, bp1_timestamps)
                if ref_price is None:
                    continue

                direction = Direction.LONG if sig.combined_signal > 0 else Direction.SHORT
                limit_price = self._compute_limit_price(
                    ref_price, direction, abs(sig.combined_signal), ex
                )

                fill_price, fill_time = self._scan_for_fill(
                    direction, limit_price, sig.timestamp,
                    ex.stale_order_seconds, bp1_events,
                )

                if fill_price is not None:
                    size = max(1, round(abs(sig.combined_signal) * risk.max_position_contracts))
                    position = _SimulatedPosition(
                        direction=direction,
                        size=float(size),
                        entry_price=fill_price,
                        entry_time=fill_time,
                        signal_at_entry=sig.combined_signal,
                    )
                    filled_count += 1

            elif sig.transition in (SignalTransition.EXIT, SignalTransition.REVERSAL):
                if position is not None:
                    exit_price = self._get_bp1_price_with_lag(
                        sig.timestamp, bp1_events
                    )
                    if exit_price is None:
                        exit_price = self._get_bp1_price(
                            sig.timestamp, bp1_prices, bp1_timestamps
                        )
                    if exit_price is None:
                        continue

                    trade = self._close_position(
                        position, exit_price, sig.timestamp,
                        "signal_reversal" if sig.transition == SignalTransition.REVERSAL else "signal_exit",
                        pair_name, pair_cfg.deribit_instrument,
                    )
                    trades.append(trade)
                    position = None

                if sig.transition == SignalTransition.REVERSAL:
                    ref_price = self._get_bp1_price(
                        sig.timestamp, bp1_prices, bp1_timestamps
                    )
                    if ref_price is None:
                        continue
                    direction = Direction.LONG if sig.combined_signal > 0 else Direction.SHORT
                    limit_price = self._compute_limit_price(
                        ref_price, direction, abs(sig.combined_signal), ex,
                    )
                    fill_price, fill_time = self._scan_for_fill(
                        direction, limit_price, sig.timestamp,
                        ex.stale_order_seconds, bp1_events,
                    )
                    if fill_price is not None:
                        size = max(1, round(abs(sig.combined_signal) * risk.max_position_contracts))
                        position = _SimulatedPosition(
                            direction=direction,
                            size=float(size),
                            entry_price=fill_price,
                            entry_time=fill_time,
                            signal_at_entry=sig.combined_signal,
                        )
                        filled_count += 1

        # Close any open position at the last available bp1 price
        if position is not None and bp1_events:
            last_price = bp1_events[-1].price
            last_ts = float(bp1_events[-1].block_timestamp)
            trade = self._close_position(
                position, last_price, last_ts, "end_of_backtest",
                pair_name, pair_cfg.deribit_instrument,
            )
            trades.append(trade)

        # Alpha decay curve
        alpha_decay = self._compute_alpha_decay(events)

        # Compute metrics
        result = self._compute_metrics(
            pair_name, start_timestamp, end_timestamp,
            len(signals_emitted), filled_count, trades, alpha_decay,
        )
        return result

    @staticmethod
    def _compute_limit_price(mid: float, direction: Direction, signal_mag: float, ex) -> float:
        delta_bps = ex.offset_base_bps + ex.offset_conviction_bps * (1 - signal_mag)
        offset = mid * delta_bps / 10000
        return mid - offset if direction == Direction.LONG else mid + offset

    @staticmethod
    def _get_bp1_price(
        timestamp: float, bp1_prices: dict[int, float], sorted_ts: list[int]
    ) -> Optional[float]:
        if not sorted_ts:
            return None
        ts = int(timestamp)
        best = None
        for t in sorted_ts:
            if t <= ts:
                best = bp1_prices[t]
            else:
                break
        return best if best is not None else bp1_prices.get(sorted_ts[0])

    def _get_bp1_price_with_lag(
        self, timestamp: float, bp1_events: list[SwapEvent]
    ) -> Optional[float]:
        ts = int(timestamp)
        count = 0
        for e in bp1_events:
            if e.block_timestamp >= ts:
                count += 1
                if count >= self._execution_lag_blocks:
                    return e.price
        return None

    @staticmethod
    def _scan_for_fill(
        direction: Direction,
        limit_price: float,
        signal_time: float,
        stale_seconds: float,
        bp1_events: list[SwapEvent],
    ) -> tuple[Optional[float], float]:
        cutoff = signal_time + stale_seconds
        for e in bp1_events:
            if e.block_timestamp < signal_time:
                continue
            if e.block_timestamp > cutoff:
                break
            if direction == Direction.LONG and e.price <= limit_price:
                return limit_price, float(e.block_timestamp)
            if direction == Direction.SHORT and e.price >= limit_price:
                return limit_price, float(e.block_timestamp)
        return None, 0.0

    @staticmethod
    def _close_position(
        pos: _SimulatedPosition,
        exit_price: float,
        exit_time: float,
        reason: str,
        pair_name: str,
        instrument: str,
    ) -> TradeRecord:
        gross = pos.direction.value * (exit_price - pos.entry_price) * pos.size
        fees = abs(pos.size * exit_price) * 0.0005
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
        bp1_events = [e for e in events if e.fee_tier == FeeTier.BP1]

        if not bp30_events or not bp1_events:
            return []

        horizons = [1, 2, 5, 10, 20, 50, 100]
        continuations: dict[int, list[float]] = {h: [] for h in horizons}

        bp1_idx_by_ts: list[tuple[int, float]] = [
            (e.block_timestamp, e.price) for e in bp1_events
        ]

        for ev30 in bp30_events:
            trigger_ts = ev30.block_timestamp
            d_k = 1 if ev30.log_return > 0 else -1

            trigger_price_log = None
            start_idx = None
            for i, (ts, p) in enumerate(bp1_idx_by_ts):
                if ts >= trigger_ts:
                    trigger_price_log = math.log(p)
                    start_idx = i
                    break

            if trigger_price_log is None or start_idx is None:
                continue

            for h in horizons:
                idx = start_idx + h
                if idx < len(bp1_idx_by_ts):
                    future_price_log = math.log(bp1_idx_by_ts[idx][1])
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

        # Equity curve and drawdown
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

        # Sharpe ratio (annualized, on per-trade returns)
        if net_returns_bps and len(net_returns_bps) > 1:
            arr = np.array(net_returns_bps)
            mean_ret = float(arr.mean())
            std_ret = float(arr.std(ddof=1))
            if std_ret > 0 and avg_holding > 0:
                trades_per_year = 365.25 * 24 * 3600 / avg_holding
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
    result = await engine.run(
        pair_name="ETH-USDC",
        start_timestamp=1700000000,
        end_timestamp=1710000000,
    )

    print(f"Signals: {result.total_signals}")
    print(f"Fill rate: {result.fill_rate:.1%}")
    print(f"Win rate: {result.win_rate:.1%}")
    print(f"Avg net return: {result.avg_net_return_bps:.1f} bps")
    print(f"Sharpe: {result.sharpe_ratio:.2f}")
    print(f"Max DD: {result.max_drawdown_usd:.2f} USD")
    print(f"Total PnL: {result.total_pnl_usd:.2f} USD")
    print(f"Total trades: {result.total_trades}")

    if result.alpha_decay_curve:
        print("\nAlpha Decay Curve:")
        for horizon, continuation in result.alpha_decay_curve:
            print(f"  +{horizon} events: {continuation:.2f} bps")


if __name__ == "__main__":
    asyncio.run(main())

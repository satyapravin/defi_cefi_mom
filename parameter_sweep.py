"""Parameter sensitivity analysis and walk-forward validation.

Usage:
    python parameter_sweep.py                          # run all sweeps
    python parameter_sweep.py --param conviction_halflife_seconds
    python parameter_sweep.py --walk-forward
    python parameter_sweep.py --param trend_entry_threshold --walk-forward
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np
from pydantic import BaseModel

from backtest import BacktestEngine, BacktestResult
from config import Config, PairConfig, load_config
from database import Database
from logger import setup_logger

logger = setup_logger()


# ---------------------------------------------------------------------------
# Parameter definitions
# ---------------------------------------------------------------------------

@dataclass
class ParamSpec:
    """One sweepable parameter: where it lives in the config and what values
    to try."""
    name: str
    section: str  # e.g. "toxicity", "risk", "regime", "execution"
    values: list[float]
    description: str = ""


DEFAULT_SWEEPS: list[ParamSpec] = [
    ParamSpec(
        name="window_seconds",
        section="bp30_signal",
        values=[60, 90, 120, 180, 300],
        description="Rolling window for 30bps swap cluster detection",
    ),
    ParamSpec(
        name="min_cluster_swaps",
        section="bp30_signal",
        values=[2, 3, 4, 5, 7],
        description="Minimum number of 30bps swaps required in window to trigger signal",
    ),
    ParamSpec(
        name="direction_ratio",
        section="bp30_signal",
        values=[0.55, 0.6, 0.65, 0.7, 0.8],
        description="Weighted direction agreement threshold for signal emission",
    ),
    ParamSpec(
        name="decay_alpha",
        section="bp30_signal",
        values=[0.001, 0.005, 0.01, 0.02, 0.05],
        description="Exponential decay rate for recent-event weighting",
    ),
    ParamSpec(
        name="bp5_coherence_window_seconds",
        section="bp30_signal",
        values=[30, 60, 120, 180],
        description="BP5 cross-tier coherence lookback window",
    ),
    ParamSpec(
        name="stop_loss_bps",
        section="risk",
        values=[20, 30, 40, 50, 60],
        description="Stop-loss threshold in basis points",
    ),
    ParamSpec(
        name="take_profit_bps",
        section="risk",
        values=[20, 40, 60, 80, 100],
        description="Take-profit threshold in basis points",
    ),
]


# ---------------------------------------------------------------------------
# Sweep result models
# ---------------------------------------------------------------------------

class SweepPoint(BaseModel):
    param_value: float
    total_trades: int
    win_rate: float
    sharpe_ratio: float
    total_pnl_usd: float
    avg_net_return_bps: float
    max_drawdown_usd: float
    fill_rate: float
    avg_holding_seconds: float


class SweepResult(BaseModel):
    param_name: str
    section: str
    description: str
    default_value: float
    points: list[SweepPoint]
    best_sharpe_value: float
    plateau_range: tuple[float, float]
    sensitivity: str  # "low", "medium", "high"


class WalkForwardFold(BaseModel):
    fold: int
    train_start: float
    train_end: float
    test_start: float
    test_end: float
    best_param_value: float
    train_sharpe: float
    test_sharpe: float
    test_pnl: float
    test_trades: int


class WalkForwardResult(BaseModel):
    param_name: str
    folds: list[WalkForwardFold]
    avg_test_sharpe: float
    avg_train_sharpe: float
    sharpe_decay_ratio: float
    chosen_values: list[float]
    stable: bool


# ---------------------------------------------------------------------------
# Core sweep engine
# ---------------------------------------------------------------------------

def _apply_param(config: Config, pair_name: str, spec: ParamSpec, value: float) -> Config:
    """Return a deep copy of *config* with one parameter changed."""
    cfg_copy = config.model_copy(deep=True)
    pair_cfg = cfg_copy.get_pair(pair_name)

    section_map = {
        "bp30_signal": pair_cfg.bp30_signal,
        "risk": pair_cfg.risk,
        "regime": pair_cfg.regime,
        "execution": pair_cfg.execution,
    }

    section_obj = section_map[spec.section]
    setattr(section_obj, spec.name, value)
    return cfg_copy


def _get_default_value(config: Config, pair_name: str, spec: ParamSpec) -> float:
    pair_cfg = config.get_pair(pair_name)
    section_map = {
        "bp30_signal": pair_cfg.bp30_signal,
        "risk": pair_cfg.risk,
        "regime": pair_cfg.regime,
        "execution": pair_cfg.execution,
    }
    return getattr(section_map[spec.section], spec.name)


async def run_single_sweep(
    config: Config,
    db: Database,
    pair_name: str,
    spec: ParamSpec,
    start_ts: int,
    end_ts: int,
) -> SweepResult:
    """Sweep one parameter across its value range and return results."""
    default_val = _get_default_value(config, pair_name, spec)
    points: list[SweepPoint] = []

    for val in spec.values:
        modified = _apply_param(config, pair_name, spec, val)
        engine = BacktestEngine(modified, db)
        result = await engine.run_backtest(pair_name, start_ts, end_ts)

        points.append(SweepPoint(
            param_value=val,
            total_trades=result.total_trades,
            win_rate=result.win_rate,
            sharpe_ratio=result.sharpe_ratio,
            total_pnl_usd=result.total_pnl_usd,
            avg_net_return_bps=result.avg_net_return_bps,
            max_drawdown_usd=result.max_drawdown_usd,
            fill_rate=result.fill_rate,
            avg_holding_seconds=result.avg_holding_seconds,
        ))

        logger.info(
            "Sweep point",
            param=spec.name,
            value=val,
            trades=result.total_trades,
            sharpe=round(result.sharpe_ratio, 2),
            pnl=round(result.total_pnl_usd, 2),
        )

    best_sharpe_val = _find_best(points)
    plateau = _find_plateau(points)
    sensitivity = _classify_sensitivity(points)

    return SweepResult(
        param_name=spec.name,
        section=spec.section,
        description=spec.description,
        default_value=default_val,
        points=points,
        best_sharpe_value=best_sharpe_val,
        plateau_range=plateau,
        sensitivity=sensitivity,
    )


def _find_best(points: list[SweepPoint]) -> float:
    """Value that produces the highest Sharpe, with a minimum trade count."""
    valid = [p for p in points if p.total_trades >= 5]
    if not valid:
        valid = points
    return max(valid, key=lambda p: p.sharpe_ratio).param_value


def _find_plateau(points: list[SweepPoint]) -> tuple[float, float]:
    """Find the range of parameter values where Sharpe is within 80% of
    the peak.  A wide plateau indicates robustness."""
    if not points:
        return (0.0, 0.0)

    peak_sharpe = max(p.sharpe_ratio for p in points)
    threshold = peak_sharpe * 0.8 if peak_sharpe > 0 else peak_sharpe * 1.2

    in_plateau = [
        p.param_value for p in points
        if (p.sharpe_ratio >= threshold if peak_sharpe > 0
            else p.sharpe_ratio <= threshold)
    ]

    if not in_plateau:
        return (points[0].param_value, points[-1].param_value)
    return (min(in_plateau), max(in_plateau))


def _classify_sensitivity(points: list[SweepPoint]) -> str:
    """Classify how sensitive performance is to this parameter."""
    if len(points) < 2:
        return "low"

    sharpes = [p.sharpe_ratio for p in points]
    s_range = max(sharpes) - min(sharpes)
    s_std = float(np.std(sharpes))

    if s_std < 0.3:
        return "low"
    elif s_std < 1.0:
        return "medium"
    return "high"


# ---------------------------------------------------------------------------
# Walk-forward validation
# ---------------------------------------------------------------------------

async def run_walk_forward(
    config: Config,
    db: Database,
    pair_name: str,
    spec: ParamSpec,
    start_ts: int,
    end_ts: int,
    n_folds: int = 5,
    train_ratio: float = 0.7,
) -> WalkForwardResult:
    """Rolling walk-forward: for each fold, train on the first *train_ratio*
    of the window, pick the best parameter value, then test on the remainder.
    The window slides by (1/n_folds) of the total span each fold."""
    total_span = end_ts - start_ts
    fold_step = total_span // n_folds
    train_len = int(fold_step * train_ratio / (1 - train_ratio))

    folds: list[WalkForwardFold] = []

    for i in range(n_folds):
        test_start = start_ts + i * fold_step
        test_end = test_start + fold_step
        train_start = max(start_ts, test_start - train_len)
        train_end = test_start

        if train_end <= train_start:
            continue

        best_val = spec.values[0]
        best_train_sharpe = -999.0

        for val in spec.values:
            modified = _apply_param(config, pair_name, spec, val)
            engine = BacktestEngine(modified, db)
            result = await engine.run_backtest(pair_name, train_start, train_end)
            if result.sharpe_ratio > best_train_sharpe and result.total_trades >= 3:
                best_train_sharpe = result.sharpe_ratio
                best_val = val

        modified = _apply_param(config, pair_name, spec, best_val)
        engine = BacktestEngine(modified, db)
        train_result = await engine.run_backtest(pair_name, train_start, train_end)
        test_result = await engine.run_backtest(pair_name, test_start, test_end)

        folds.append(WalkForwardFold(
            fold=i + 1,
            train_start=float(train_start),
            train_end=float(train_end),
            test_start=float(test_start),
            test_end=float(test_end),
            best_param_value=best_val,
            train_sharpe=train_result.sharpe_ratio,
            test_sharpe=test_result.sharpe_ratio,
            test_pnl=test_result.total_pnl_usd,
            test_trades=test_result.total_trades,
        ))

        logger.info(
            "Walk-forward fold",
            param=spec.name,
            fold=i + 1,
            best_val=best_val,
            train_sharpe=round(train_result.sharpe_ratio, 2),
            test_sharpe=round(test_result.sharpe_ratio, 2),
            test_trades=test_result.total_trades,
        )

    avg_train = float(np.mean([f.train_sharpe for f in folds])) if folds else 0.0
    avg_test = float(np.mean([f.test_sharpe for f in folds])) if folds else 0.0
    decay = avg_test / avg_train if avg_train != 0 else 0.0
    chosen = [f.best_param_value for f in folds]
    unique_chosen = len(set(chosen))

    return WalkForwardResult(
        param_name=spec.name,
        folds=folds,
        avg_test_sharpe=avg_test,
        avg_train_sharpe=avg_train,
        sharpe_decay_ratio=decay,
        chosen_values=chosen,
        stable=unique_chosen <= len(spec.values) * 0.5 and decay > 0.5,
    )


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def print_sweep_report(results: list[SweepResult]) -> None:
    print("\n" + "=" * 80)
    print("  PARAMETER SENSITIVITY REPORT")
    print("=" * 80)

    for r in results:
        print(f"\n{'─' * 70}")
        print(f"  {r.param_name} ({r.section})")
        print(f"  {r.description}")
        print(f"  Default: {r.default_value}  │  Best Sharpe at: {r.best_sharpe_value}")
        print(f"  Sensitivity: {r.sensitivity.upper()}  │  Plateau: {r.plateau_range[0]} – {r.plateau_range[1]}")
        print(f"{'─' * 70}")
        print(f"  {'Value':>10}  {'Trades':>7}  {'WinRate':>8}  {'Sharpe':>7}  {'PnL $':>10}  {'AvgRet':>8}  {'MaxDD $':>9}")
        print(f"  {'─'*10}  {'─'*7}  {'─'*8}  {'─'*7}  {'─'*10}  {'─'*8}  {'─'*9}")

        for p in r.points:
            marker = " ◄" if p.param_value == r.best_sharpe_value else ""
            print(
                f"  {p.param_value:>10.4g}  {p.total_trades:>7}  {p.win_rate:>7.1%}"
                f"  {p.sharpe_ratio:>7.2f}  {p.total_pnl_usd:>10.2f}"
                f"  {p.avg_net_return_bps:>7.1f}bp  {p.max_drawdown_usd:>9.2f}{marker}"
            )

    print("\n" + "=" * 80)
    print("  SUMMARY")
    print("=" * 80)

    by_sensitivity = {"high": [], "medium": [], "low": []}
    for r in results:
        by_sensitivity[r.sensitivity].append(r.param_name)

    if by_sensitivity["high"]:
        print(f"\n  HIGH sensitivity (tune carefully):  {', '.join(by_sensitivity['high'])}")
    if by_sensitivity["medium"]:
        print(f"  MEDIUM sensitivity (worth tuning):  {', '.join(by_sensitivity['medium'])}")
    if by_sensitivity["low"]:
        print(f"  LOW sensitivity (leave at default): {', '.join(by_sensitivity['low'])}")

    print("\n  Recommended values (mid-plateau, not peak):")
    for r in results:
        mid = (r.plateau_range[0] + r.plateau_range[1]) / 2
        change = "" if abs(mid - r.default_value) < 1e-9 else f"  (default: {r.default_value})"
        print(f"    {r.param_name}: {mid:.4g}{change}")
    print()


def print_walk_forward_report(results: list[WalkForwardResult]) -> None:
    print("\n" + "=" * 80)
    print("  WALK-FORWARD VALIDATION REPORT")
    print("=" * 80)

    for r in results:
        status = "STABLE" if r.stable else "UNSTABLE ⚠"
        print(f"\n{'─' * 70}")
        print(f"  {r.param_name}  │  {status}")
        print(f"  Avg train Sharpe: {r.avg_train_sharpe:.2f}  │  Avg test Sharpe: {r.avg_test_sharpe:.2f}")
        print(f"  Decay ratio: {r.sharpe_decay_ratio:.2f}  (1.0 = no decay, <0.5 = overfit)")
        print(f"  Chosen values across folds: {r.chosen_values}")
        print(f"{'─' * 70}")
        print(f"  {'Fold':>5}  {'BestVal':>8}  {'TrainSh':>8}  {'TestSh':>8}  {'TestPnL':>10}  {'TestTrades':>11}")

        for f in r.folds:
            print(
                f"  {f.fold:>5}  {f.best_param_value:>8.4g}"
                f"  {f.train_sharpe:>8.2f}  {f.test_sharpe:>8.2f}"
                f"  {f.test_pnl:>10.2f}  {f.test_trades:>11}"
            )

    print("\n" + "=" * 80)
    print("  WALK-FORWARD SUMMARY")
    print("=" * 80)

    overfit = [r.param_name for r in results if not r.stable]
    robust = [r.param_name for r in results if r.stable]

    if robust:
        print(f"\n  Robust (consistent out-of-sample): {', '.join(robust)}")
    if overfit:
        print(f"  Overfit risk (unstable OOS):        {', '.join(overfit)}")
    print()


def save_results(
    sweep_results: list[SweepResult],
    wf_results: list[WalkForwardResult],
    output_dir: str = "sweep_results",
) -> str:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())

    data = {
        "timestamp": ts,
        "sweeps": [r.model_dump() for r in sweep_results],
        "walk_forward": [r.model_dump() for r in wf_results],
    }

    path = out / f"sweep_{ts}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)

    logger.info("Results saved", path=str(path))
    return str(path)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Parameter sensitivity analysis and walk-forward validation"
    )
    parser.add_argument(
        "--param", type=str, default=None,
        help="Sweep a single parameter (e.g. window_seconds). "
             "Omit to sweep all.",
    )
    parser.add_argument(
        "--walk-forward", action="store_true",
        help="Run walk-forward validation in addition to sensitivity sweep.",
    )
    parser.add_argument(
        "--pair", type=str, default="ETH-USDC",
        help="Trading pair name.",
    )
    parser.add_argument(
        "--start", type=int, default=1700000000,
        help="Backtest start timestamp.",
    )
    parser.add_argument(
        "--end", type=int, default=1710000000,
        help="Backtest end timestamp.",
    )
    parser.add_argument(
        "--folds", type=int, default=5,
        help="Number of walk-forward folds.",
    )
    parser.add_argument(
        "--config", type=str, default="config.yaml",
        help="Path to config file.",
    )
    parser.add_argument(
        "--output", type=str, default="sweep_results",
        help="Directory for JSON output.",
    )

    args = parser.parse_args()

    config = load_config(args.config)
    db = Database(config.system.database_path)
    await db.initialize()

    if args.param:
        specs = [s for s in DEFAULT_SWEEPS if s.name == args.param]
        if not specs:
            print(f"Unknown parameter: {args.param}")
            print(f"Available: {', '.join(s.name for s in DEFAULT_SWEEPS)}")
            return
    else:
        specs = DEFAULT_SWEEPS

    sweep_results: list[SweepResult] = []
    wf_results: list[WalkForwardResult] = []

    print(f"\nSweeping {len(specs)} parameter(s) over {args.pair}")
    print(f"Time range: {args.start} – {args.end}\n")

    for spec in specs:
        print(f"▸ Sweeping {spec.name} ({len(spec.values)} values)...")
        result = await run_single_sweep(
            config, db, args.pair, spec, args.start, args.end
        )
        sweep_results.append(result)

    print_sweep_report(sweep_results)

    if args.walk_forward:
        print(f"\nRunning walk-forward validation ({args.folds} folds)...\n")
        for spec in specs:
            print(f"▸ Walk-forward: {spec.name}...")
            wf = await run_walk_forward(
                config, db, args.pair, spec,
                args.start, args.end, n_folds=args.folds,
            )
            wf_results.append(wf)

        print_walk_forward_report(wf_results)

    path = save_results(sweep_results, wf_results, args.output)
    print(f"Results saved to {path}")


if __name__ == "__main__":
    asyncio.run(main())

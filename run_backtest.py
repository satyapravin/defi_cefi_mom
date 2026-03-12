"""Standalone backtest runner against real data.

Supports both single-run and parameter sweep modes:
    python run_backtest.py                     # single run with defaults
    python run_backtest.py --sweep             # sweep direction_ratio & min_cluster_swaps
"""

import argparse
import asyncio
import math
import numpy as np

from config import (
    BP30SignalConfig, Config, DeribitConfig, ExecutionConfig,
    InfraConfig, PairConfig, PoolConfig, PoolsConfig,
    RegimeConfig, RiskConfig, SystemConfig,
)
from database import Database
from backtest import BacktestEngine


def make_config(
    direction_ratio: float = 0.7,
    min_cluster_swaps: int = 3,
    window_seconds: float = 120,
    decay_alpha: float = 0.01,
    stop_loss_bps: float = 40,
    take_profit_bps: float = 40,
) -> Config:
    return Config(
        system=SystemConfig(mode="backtest", database_path="data/events.db"),
        infrastructure=InfraConfig(rpc_url="wss://dummy"),
        deribit=DeribitConfig(),
        pairs=[PairConfig(
            name="ETH-USDC",
            deribit_instrument="ETH-PERPETUAL",
            token0="0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
            token1="0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
            token0_decimals=18, token1_decimals=6,
            pools=PoolsConfig(
                bp30=PoolConfig(address="0xc473e2aee3441bf9240be85eb122abb059a3b57c", fee=3000),
                bp5=PoolConfig(address="0xC6962004f452bE9203591991D15f6b388e09E8D0", fee=500),
                bp1=PoolConfig(address="0x6f38e884725a116c9c7fbf208e79fe8828a2595f", fee=100),
            ),
            bp30_signal=BP30SignalConfig(
                window_seconds=window_seconds,
                min_cluster_swaps=min_cluster_swaps,
                direction_ratio=direction_ratio,
                decay_alpha=decay_alpha,
                bp5_coherence_window_seconds=60,
                bp5_coherence_min_events=5,
            ),
            execution=ExecutionConfig(
                offset_base_bps=2.0,
                offset_conviction_bps=6.0,
                stale_order_seconds=45,
                post_only=True,
            ),
            risk=RiskConfig(
                max_position_usd=50000,
                max_position_contracts=100,
                stop_loss_bps=stop_loss_bps,
                take_profit_bps=take_profit_bps,
                cooldown_seconds=120,
                daily_loss_limit_usd=500,
                max_holding_seconds=600,
            ),
            regime=RegimeConfig(
                vol_window_seconds=300,
                vol_quiet_threshold=0.00004,
                vol_chaotic_threshold=0.00006,
                intensity_window_seconds=60,
                chaotic_multiplier=0.3,
                active_multiplier=1.5,
                quiet_multiplier=1.0,
                acf_window_events=50,
                acf_trending_threshold=0.10,
                acf_mean_revert_threshold=-0.10,
                acf_trending_multiplier=1.3,
                acf_mean_revert_multiplier=0.3,
            ),
        )],
    )


async def run():
    config = make_config()
    db = Database(config.system.database_path)
    await db.initialize()

    engine = BacktestEngine(config, db, execution_lag_blocks=1)
    result = await engine.run_backtest(
        pair_name="ETH-USDC",
        start_timestamp=0,
        end_timestamp=int(2e9),
    )

    print("=" * 65)
    print("  BACKTEST RESULTS - ETH-USDC (~24h real data)")
    print("  Enhanced: exp-decay + cross-tier coherence + ACF regime")
    print("=" * 65)
    print()
    print(f"  Signals emitted:     {result.total_signals}")
    print(f"  Signals filled:      {result.filled_signals}")
    print(f"  Fill rate:           {result.fill_rate:.1%}")
    print(f"  Total trades:        {result.total_trades}")
    print()
    print(f"  Win rate:            {result.win_rate:.1%}")
    print(f"  Avg gross return:    {result.avg_gross_return_bps:+.2f} bps")
    print(f"  Avg net return:      {result.avg_net_return_bps:+.2f} bps")
    print(f"  Total PnL:           ${result.total_pnl_usd:+,.2f}")
    print(f"  Max drawdown:        ${result.max_drawdown_usd:.2f} ({result.max_drawdown_bps:.1f} bps)")
    print(f"  Sharpe ratio:        {result.sharpe_ratio:.2f}")
    print(f"  Avg holding time:    {result.avg_holding_seconds:.0f}s")
    print()

    if result.alpha_decay_curve:
        print("  Alpha Decay Curve (signed continuation in bps):")
        for horizon, cont in result.alpha_decay_curve:
            bar = "+" * max(0, int(cont * 10)) if cont > 0 else "-" * max(0, int(-cont * 10))
            print(f"    +{horizon:>3d} BP5 events:  {cont:+.3f} bps  {bar}")
        print()

    if result.regime_distribution:
        total_regime = sum(result.regime_distribution.values())
        print("  Regime Distribution:")
        for regime, count in sorted(result.regime_distribution.items()):
            pct = count / total_regime * 100 if total_regime > 0 else 0
            print(f"    {regime:>8s}: {count:>6d} events ({pct:.1f}%)")
        print()

    if result.trades:
        print("  Exit Reasons:")
        reasons: dict[str, list] = {}
        for t in result.trades:
            reasons.setdefault(t.exit_reason, []).append(t)
        for reason, tds in sorted(reasons.items()):
            n = len(tds)
            avg_pnl = sum(t.net_pnl_usd for t in tds) / n
            wins = sum(1 for t in tds if t.net_pnl_usd > 0)
            avg_hold = sum(t.exit_time - t.entry_time for t in tds) / n
            print(f"    {reason:>18s}: {n:>3d} trades, win {wins/n:.0%}, avg PnL ${avg_pnl:+.2f}, avg hold {avg_hold:.0f}s")
        print()

        longs = [t for t in result.trades if t.direction.value == 1]
        shorts = [t for t in result.trades if t.direction.value == -1]
        if longs:
            l_pnl = sum(t.net_pnl_usd for t in longs)
            l_wr = sum(1 for t in longs if t.net_pnl_usd > 0) / len(longs)
            print(f"  LONG trades:  {len(longs):>3d}, win rate {l_wr:.0%}, total PnL ${l_pnl:+.2f}")
        if shorts:
            s_pnl = sum(t.net_pnl_usd for t in shorts)
            s_wr = sum(1 for t in shorts if t.net_pnl_usd > 0) / len(shorts)
            print(f"  SHORT trades: {len(shorts):>3d}, win rate {s_wr:.0%}, total PnL ${s_pnl:+.2f}")
        print()

        pnls = [t.net_pnl_usd for t in result.trades]
        arr = np.array(pnls)
        print(f"  PnL Distribution:")
        print(f"    Mean:    ${arr.mean():+.2f}")
        print(f"    Median:  ${np.median(arr):+.2f}")
        print(f"    Std:     ${arr.std():.2f}")
        print(f"    Min:     ${arr.min():+.2f}")
        print(f"    Max:     ${arr.max():+.2f}")
        print(f"    P25:     ${np.percentile(arr, 25):+.2f}")
        print(f"    P75:     ${np.percentile(arr, 75):+.2f}")

        if result.equity_curve:
            eq = [e[1] for e in result.equity_curve]
            print()
            print(f"  Equity Curve:")
            print(f"    Start:   ${eq[0]:+.2f}")
            print(f"    End:     ${eq[-1]:+.2f}")
            print(f"    Peak:    ${max(eq):+.2f}")
            print(f"    Trough:  ${min(eq):+.2f}")

        # Signal strength distribution across trades
        strengths = [t.signal_at_entry for t in result.trades]
        s_arr = np.array(strengths)
        print()
        print(f"  Signal Strength at Entry:")
        print(f"    Mean:    {s_arr.mean():.3f}")
        print(f"    Min:     {s_arr.min():.3f}")
        print(f"    Max:     {s_arr.max():.3f}")

    else:
        print("  No trades executed.")


async def run_sweep():
    """Sweep lower thresholds to increase signal frequency."""
    db = Database("data/events.db")
    await db.initialize()

    direction_ratios = [0.55, 0.6, 0.65, 0.7, 0.8]
    min_clusters = [2, 3, 4]
    windows = [60, 120, 180]

    print("=" * 110)
    print("  PARAMETER SWEEP: direction_ratio x min_cluster_swaps x window_seconds")
    print("=" * 110)
    print(f"  {'dir_ratio':>10}  {'min_swaps':>10}  {'window':>7}  {'signals':>8}  {'fills':>6}  {'trades':>7}  {'win%':>6}  {'avg_net':>8}  {'PnL':>10}  {'sharpe':>7}  {'hold_s':>7}")
    print(f"  {'─'*10}  {'─'*10}  {'─'*7}  {'─'*8}  {'─'*6}  {'─'*7}  {'─'*6}  {'─'*8}  {'─'*10}  {'─'*7}  {'─'*7}")

    results = []
    for dr in direction_ratios:
        for mc in min_clusters:
            for ws in windows:
                config = make_config(
                    direction_ratio=dr,
                    min_cluster_swaps=mc,
                    window_seconds=ws,
                )
                engine = BacktestEngine(config, db, execution_lag_blocks=1)
                r = await engine.run_backtest("ETH-USDC", 0, int(2e9))

                marker = ""
                if r.total_trades > 0 and r.total_pnl_usd > 0:
                    marker = " *"
                print(
                    f"  {dr:>10.2f}  {mc:>10d}  {ws:>7.0f}"
                    f"  {r.total_signals:>8}  {r.filled_signals:>6}  {r.total_trades:>7}"
                    f"  {r.win_rate:>5.0%}  {r.avg_net_return_bps:>+7.1f}bp"
                    f"  ${r.total_pnl_usd:>+9.2f}  {r.sharpe_ratio:>7.2f}"
                    f"  {r.avg_holding_seconds:>6.0f}s{marker}"
                )
                results.append({
                    "dir_ratio": dr, "min_swaps": mc, "window": ws,
                    "signals": r.total_signals, "fills": r.filled_signals,
                    "trades": r.total_trades, "win_rate": r.win_rate,
                    "avg_net_bps": r.avg_net_return_bps, "pnl": r.total_pnl_usd,
                    "sharpe": r.sharpe_ratio, "hold_s": r.avg_holding_seconds,
                })

    print()
    profitable = [r for r in results if r["trades"] > 0 and r["pnl"] > 0]
    if profitable:
        best = max(profitable, key=lambda x: x["pnl"])
        print(f"  BEST (by PnL):  dir_ratio={best['dir_ratio']}, min_swaps={best['min_swaps']}, "
              f"window={best['window']}s  →  {best['trades']} trades, ${best['pnl']:+.2f} PnL, "
              f"sharpe={best['sharpe']:.2f}")

        most_trades = max(profitable, key=lambda x: x["trades"])
        print(f"  MOST ACTIVE:    dir_ratio={most_trades['dir_ratio']}, min_swaps={most_trades['min_swaps']}, "
              f"window={most_trades['window']}s  →  {most_trades['trades']} trades, ${most_trades['pnl']:+.2f} PnL, "
              f"sharpe={most_trades['sharpe']:.2f}")

        best_sharpe = max(profitable, key=lambda x: x["sharpe"])
        print(f"  BEST SHARPE:    dir_ratio={best_sharpe['dir_ratio']}, min_swaps={best_sharpe['min_swaps']}, "
              f"window={best_sharpe['window']}s  →  {best_sharpe['trades']} trades, ${best_sharpe['pnl']:+.2f} PnL, "
              f"sharpe={best_sharpe['sharpe']:.2f}")
    else:
        print("  No profitable parameter combinations found.")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sweep", action="store_true", help="Run parameter sweep")
    args = parser.parse_args()

    if args.sweep:
        asyncio.run(run_sweep())
    else:
        asyncio.run(run())

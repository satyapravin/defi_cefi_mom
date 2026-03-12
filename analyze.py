"""Data analysis and visualization dashboard for the momentum system.

Generates publication-quality charts from the database and/or backtest
results.  All plots are saved as PNG files into an output directory.

Usage:
    # Full dashboard from the database (runs a backtest internally)
    python analyze.py

    # Specify pair, time range, output directory
    python analyze.py --pair ETH-USDC --hours 24 --out plots/

    # Only data plots (skip backtest — useful when data is loaded but
    # you don't want to wait for a full backtest run)
    python analyze.py --data-only

Charts produced:
    1. Price timeline across fee tiers
    2. Swap event rate heatmap (events per 5-min bin)
    3. Fee tier volume comparison
    4. Equity curve (from backtest)
    5. Alpha decay curve (from backtest)
    6. Trade PnL distribution (from backtest)
    7. Regime distribution pie chart (from backtest)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

plt.rcParams.update({
    "figure.facecolor": "#1a1a2e",
    "axes.facecolor": "#16213e",
    "axes.edgecolor": "#e0e0e0",
    "axes.labelcolor": "#e0e0e0",
    "text.color": "#e0e0e0",
    "xtick.color": "#b0b0b0",
    "ytick.color": "#b0b0b0",
    "grid.color": "#2a2a4a",
    "grid.alpha": 0.6,
    "figure.figsize": (14, 6),
    "font.size": 11,
    "axes.titlesize": 14,
    "axes.titleweight": "bold",
})

TIER_COLORS = {"bp30": "#ff6b6b", "bp5": "#ffd93d", "bp1": "#6bcb77"}
TIER_LABELS = {"bp30": "30 bps", "bp5": "5 bps", "bp1": "1 bps"}


def _ts_to_dt(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc)


async def _fetch_swap_data(engine, pair: str, start_ts: int, end_ts: int):
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            """SELECT fee_tier, block_timestamp, price, log_return,
                      amount0, amount1
               FROM swap_events
               WHERE pair_name = :pair
                 AND block_timestamp >= :s AND block_timestamp <= :e
               ORDER BY block_timestamp"""
        ), {"pair": pair, "s": start_ts, "e": end_ts})).fetchall()
    return rows


def plot_price_timeline(swap_rows, out_dir: Path) -> str:
    """Chart 1: Price over time, coloured by fee tier."""
    fig, ax = plt.subplots()
    for tier in ("bp1", "bp5", "bp30"):
        data = [(r[1], r[2]) for r in swap_rows if r[0] == tier and r[2] and r[2] > 0]
        if not data:
            continue
        ts, prices = zip(*data)
        dts = [_ts_to_dt(t) for t in ts]
        ax.plot(dts, prices, color=TIER_COLORS[tier], label=TIER_LABELS[tier],
                alpha=0.8, linewidth=0.6)
    ax.set_title("Price Timeline by Fee Tier")
    ax.set_ylabel("Price (USD)")
    ax.legend(loc="upper left")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.autofmt_xdate()
    ax.grid(True)
    path = out_dir / "01_price_timeline.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return str(path)


def plot_event_rate_heatmap(swap_rows, out_dir: Path) -> str:
    """Chart 2: Event rate heatmap (5-min bins, by tier)."""
    if not swap_rows:
        return ""
    min_ts = min(r[1] for r in swap_rows)
    max_ts = max(r[1] for r in swap_rows)
    bin_width = 300  # 5 minutes
    n_bins = max(1, (max_ts - min_ts) // bin_width + 1)

    tiers = ["bp30", "bp5", "bp1"]
    matrix = np.zeros((len(tiers), n_bins))
    for r in swap_rows:
        tier_idx = tiers.index(r[0]) if r[0] in tiers else -1
        if tier_idx < 0:
            continue
        b = min((r[1] - min_ts) // bin_width, n_bins - 1)
        matrix[tier_idx, b] += 1

    fig, ax = plt.subplots(figsize=(14, 4))
    im = ax.imshow(matrix, aspect="auto", cmap="inferno", interpolation="nearest")
    ax.set_yticks(range(len(tiers)))
    ax.set_yticklabels([TIER_LABELS[t] for t in tiers])
    ax.set_xlabel("Time (5-min bins)")
    ax.set_title("Swap Event Rate Heatmap")

    n_labels = min(8, n_bins)
    tick_positions = np.linspace(0, n_bins - 1, n_labels, dtype=int)
    tick_labels = [_ts_to_dt(min_ts + int(p) * bin_width).strftime("%H:%M") for p in tick_positions]
    ax.set_xticks(tick_positions)
    ax.set_xticklabels(tick_labels)

    fig.colorbar(im, ax=ax, label="Events per 5 min", shrink=0.8)
    path = out_dir / "02_event_rate_heatmap.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return str(path)


def plot_tier_volume(swap_rows, out_dir: Path) -> str:
    """Chart 3: Swap count and estimated USD volume by tier."""
    counts: dict[str, int] = {}
    usd_vol: dict[str, float] = {}
    for r in swap_rows:
        tier = r[0]
        price = r[2] or 0
        counts[tier] = counts.get(tier, 0) + 1
        try:
            amt1_usd = abs(int(r[5] or 0)) / 1e6
        except (ValueError, OverflowError):
            amt1_usd = 0
        usd_vol[tier] = usd_vol.get(tier, 0) + amt1_usd

    tiers = ["bp30", "bp5", "bp1"]
    labels = [TIER_LABELS[t] for t in tiers]
    colors = [TIER_COLORS[t] for t in tiers]
    count_vals = [counts.get(t, 0) for t in tiers]
    vol_vals = [usd_vol.get(t, 0) for t in tiers]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    bars1 = ax1.bar(labels, count_vals, color=colors, edgecolor="#ffffff", linewidth=0.5)
    ax1.set_title("Swap Count by Tier")
    ax1.set_ylabel("Number of swaps")
    for bar, v in zip(bars1, count_vals):
        ax1.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                 f"{v:,}", ha="center", va="bottom", fontsize=9)
    ax1.grid(True, axis="y")

    bars2 = ax2.bar(labels, vol_vals, color=colors, edgecolor="#ffffff", linewidth=0.5)
    ax2.set_title("Estimated USDC Volume by Tier")
    ax2.set_ylabel("USDC Volume")
    for bar, v in zip(bars2, vol_vals):
        label = f"${v:,.0f}" if v < 1e6 else f"${v/1e6:,.1f}M"
        ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                 label, ha="center", va="bottom", fontsize=9)
    ax2.grid(True, axis="y")

    fig.suptitle("Volume Analysis by Fee Tier", fontsize=14, fontweight="bold")
    fig.tight_layout()
    path = out_dir / "03_tier_volume.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return str(path)


def plot_equity_curve(equity_curve, out_dir: Path) -> str:
    """Chart 4: Cumulative PnL equity curve from backtest."""
    if not equity_curve:
        return ""
    times, pnls = zip(*equity_curve)
    dts = [_ts_to_dt(t) for t in times]

    fig, ax = plt.subplots()
    ax.fill_between(dts, 0, pnls, alpha=0.3, color="#6bcb77")
    ax.plot(dts, pnls, color="#6bcb77", linewidth=1.5)
    ax.axhline(0, color="#e0e0e0", linewidth=0.5, linestyle="--")
    ax.set_title("Equity Curve (Cumulative PnL)")
    ax.set_ylabel("PnL (USD)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.autofmt_xdate()
    ax.grid(True)
    path = out_dir / "04_equity_curve.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return str(path)


def plot_alpha_decay(alpha_curve, out_dir: Path) -> str:
    """Chart 5: Alpha decay curve (continuation in bps vs horizon)."""
    if not alpha_curve:
        return ""
    horizons, continuations = zip(*alpha_curve)

    fig, ax = plt.subplots(figsize=(10, 5))
    colors_bar = ["#6bcb77" if c >= 0 else "#ff6b6b" for c in continuations]
    ax.bar([str(h) for h in horizons], continuations, color=colors_bar, edgecolor="#ffffff", linewidth=0.5)
    ax.axhline(0, color="#e0e0e0", linewidth=0.5, linestyle="--")
    ax.set_title("Alpha Decay: 30bps Signal Continuation")
    ax.set_xlabel("Horizon (bp5 events after trigger)")
    ax.set_ylabel("Avg continuation (bps)")
    ax.grid(True, axis="y")
    path = out_dir / "05_alpha_decay.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return str(path)


def plot_trade_pnl_distribution(trades, out_dir: Path) -> str:
    """Chart 6: Histogram of per-trade PnL."""
    if not trades:
        return ""
    pnls = [t.net_pnl_usd for t in trades]

    fig, ax = plt.subplots()
    ax.hist(pnls, bins=min(50, len(pnls)), color="#ffd93d", edgecolor="#1a1a2e",
            alpha=0.85)
    ax.axvline(0, color="#ff6b6b", linewidth=1.5, linestyle="--")
    mean_pnl = np.mean(pnls)
    ax.axvline(mean_pnl, color="#6bcb77", linewidth=1.5, linestyle="-",
               label=f"Mean: ${mean_pnl:+.2f}")
    ax.set_title("Trade PnL Distribution")
    ax.set_xlabel("Net PnL (USD)")
    ax.set_ylabel("Count")
    ax.legend()
    ax.grid(True, axis="y")
    path = out_dir / "06_trade_pnl_dist.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return str(path)


def plot_regime_distribution(regime_dist, out_dir: Path) -> str:
    """Chart 8: Pie chart of regime distribution."""
    if not regime_dist or all(v == 0 for v in regime_dist.values()):
        return ""

    labels = []
    sizes = []
    colors_pie = {"quiet": "#6bcb77", "active": "#ffd93d", "chaotic": "#ff6b6b"}
    for regime in ("quiet", "active", "chaotic"):
        count = regime_dist.get(regime, 0)
        if count > 0:
            labels.append(f"{regime.title()}\n({count:,})")
            sizes.append(count)

    fig, ax = plt.subplots(figsize=(7, 7))
    cs = [colors_pie.get(l.split("\n")[0].lower(), "#888") for l in labels]
    wedges, texts, autotexts = ax.pie(
        sizes, labels=labels, colors=cs, autopct="%1.1f%%",
        startangle=90, textprops={"color": "#e0e0e0"},
    )
    for at in autotexts:
        at.set_fontweight("bold")
    ax.set_title("Regime Distribution")
    path = out_dir / "08_regime_distribution.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return str(path)


async def generate_dashboard(
    db_path: str,
    pair: str = "ETH-USDC",
    hours: float = 24,
    out_dir: str = "plots",
    data_only: bool = False,
    config_path: str = "config.yaml",
) -> list[str]:
    """Generate all dashboard charts and return list of output file paths."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)

    async with engine.connect() as conn:
        ts_row = (await conn.execute(text(
            "SELECT MIN(block_timestamp), MAX(block_timestamp) FROM swap_events WHERE pair_name = :p"
        ), {"p": pair})).fetchone()

    if not ts_row or ts_row[0] is None:
        print("  No swap data found in database. Nothing to plot.")
        await engine.dispose()
        return []

    end_ts = ts_row[1]
    start_ts = max(ts_row[0], int(end_ts - hours * 3600))

    print(f"  Fetching data for {pair} over {hours:.0f}h window...")
    swap_rows = await _fetch_swap_data(engine, pair, start_ts, end_ts)
    await engine.dispose()

    print(f"  Loaded {len(swap_rows):,} swaps")

    produced: list[str] = []

    if swap_rows:
        produced.append(plot_price_timeline(swap_rows, out))
        produced.append(plot_event_rate_heatmap(swap_rows, out))
        produced.append(plot_tier_volume(swap_rows, out))

    if not data_only:
        try:
            from config import load_config
            from database import Database
            from backtest import BacktestEngine

            config = load_config(config_path)
            db = Database(db_path)
            await db.initialize()
            bt = BacktestEngine(config, db)

            print("  Running backtest for visualization...")
            result = await bt.run_backtest(pair, start_ts, end_ts)

            if result.equity_curve:
                produced.append(plot_equity_curve(result.equity_curve, out))
            if result.alpha_decay_curve:
                produced.append(plot_alpha_decay(result.alpha_decay_curve, out))
            if result.trades:
                produced.append(plot_trade_pnl_distribution(result.trades, out))
            if result.regime_distribution:
                produced.append(plot_regime_distribution(result.regime_distribution, out))

            print(f"  Backtest: {result.total_trades} trades, "
                  f"Sharpe {result.sharpe_ratio:.2f}, "
                  f"PnL ${result.total_pnl_usd:+,.2f}")
        except Exception as e:
            print(f"  Backtest skipped: {e}")

    produced = [p for p in produced if p]
    print(f"\n  Generated {len(produced)} charts in {out}/")
    for p in produced:
        print(f"    {p}")

    return produced


async def main() -> None:
    parser = argparse.ArgumentParser(description="Generate analysis dashboard")
    parser.add_argument("--db", type=str, default=None, help="Path to SQLite database")
    parser.add_argument("--pair", type=str, default="ETH-USDC")
    parser.add_argument("--hours", type=float, default=24, help="Lookback window in hours")
    parser.add_argument("--out", type=str, default="plots", help="Output directory")
    parser.add_argument("--data-only", action="store_true",
                        help="Skip backtest, only produce data charts")
    parser.add_argument("--config", type=str, default="config.yaml")
    args = parser.parse_args()

    db_path = args.db
    if db_path is None:
        try:
            from config import load_config
            config = load_config(args.config)
            db_path = config.system.database_path
        except Exception:
            db_path = "data/events.db"

    if not Path(db_path).exists():
        print(f"ERROR: Database not found at {db_path}")
        sys.exit(1)

    await generate_dashboard(
        db_path=db_path,
        pair=args.pair,
        hours=args.hours,
        out_dir=args.out,
        data_only=args.data_only,
        config_path=args.config,
    )


if __name__ == "__main__":
    asyncio.run(main())

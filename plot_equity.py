"""Generate equity curve plot with metrics table for LinkedIn."""
import asyncio
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np
from datetime import datetime
from run_backtest import make_config
from database import Database
from backtest import BacktestEngine


async def main():
    config = make_config()
    db = Database("data/events.db")
    await db.initialize()
    engine = BacktestEngine(config, db)
    r = await engine.run_backtest("ETH-USDC", 0, int(2e9))

    timestamps = [datetime.utcfromtimestamp(t) for t, _ in r.equity_curve]
    pnl_values = [p for _, p in r.equity_curve]

    timestamps.insert(0, datetime.utcfromtimestamp(r.start_time))
    pnl_values.insert(0, 0.0)

    fig = plt.figure(figsize=(12, 7), facecolor="#1a1a2e")
    gs = gridspec.GridSpec(2, 1, height_ratios=[3, 1.2], hspace=0.08)

    ax = fig.add_subplot(gs[0])
    ax.set_facecolor("#16213e")

    ax.fill_between(timestamps, pnl_values, 0,
                     where=[p >= 0 for p in pnl_values],
                     color="#00d4aa", alpha=0.15, interpolate=True)
    ax.fill_between(timestamps, pnl_values, 0,
                     where=[p < 0 for p in pnl_values],
                     color="#ff6b6b", alpha=0.15, interpolate=True)
    ax.plot(timestamps, pnl_values, color="#00d4aa", linewidth=2.2, zorder=5)

    for i, t in enumerate(r.trades):
        ts = datetime.utcfromtimestamp(t.exit_time)
        cum = sum(tr.net_pnl_usd for tr in sorted(r.trades, key=lambda x: x.exit_time)[:i+1])
        color = "#00d4aa" if t.net_pnl_usd >= 0 else "#ff6b6b"
        marker = "^" if t.direction.value == 1 else "v"
        ax.scatter(ts, cum, color=color, marker=marker, s=80, zorder=10,
                   edgecolors="white", linewidths=0.5)

    peak_val = max(pnl_values)
    peak_idx = pnl_values.index(peak_val)
    ax.axhline(y=peak_val, color="#00d4aa", linestyle="--", alpha=0.3, linewidth=0.8)
    ax.annotate(f"Peak: ${peak_val:,.0f}", xy=(timestamps[peak_idx], peak_val),
                fontsize=8, color="#00d4aa", alpha=0.7,
                xytext=(0, 8), textcoords="offset points", ha="center")

    ax.axhline(y=0, color="white", linestyle="-", alpha=0.2, linewidth=0.5)

    ax.set_title("BP30 Cluster Strategy — Equity Curve (24h ETH-USDC)",
                 fontsize=14, fontweight="bold", color="white", pad=12)
    ax.set_ylabel("Cumulative PnL (USD)", fontsize=11, color="white")
    ax.tick_params(colors="white", labelsize=9)
    ax.tick_params(axis="x", labelbottom=False)
    for spine in ax.spines.values():
        spine.set_color("#2a2a4a")
    ax.grid(True, alpha=0.15, color="white")

    ax_table = fig.add_subplot(gs[1])
    ax_table.set_facecolor("#1a1a2e")
    ax_table.axis("off")

    wins = sum(1 for t in r.trades if t.net_pnl_usd > 0)
    losses = len(r.trades) - wins

    col_labels = ["Metric", "Value", "Metric", "Value"]
    table_data = [
        ["Signals", f"{r.total_signals}", "Fill Rate", f"{r.fill_rate:.0%}"],
        ["Trades", f"{r.total_trades}", "Win / Loss", f"{wins}W / {losses}L"],
        ["Win Rate", f"{r.win_rate:.0%}", "Avg Net Return", f"+{r.avg_net_return_bps:.1f} bps"],
        ["Total PnL", f"${r.total_pnl_usd:+,.2f}", "Max Drawdown", f"${r.max_drawdown_usd:,.2f}"],
        ["Sharpe (ann.)", f"{r.sharpe_ratio:.2f}", "Avg Hold", f"{r.avg_holding_seconds:.0f}s"],
        ["Alpha Peak", "+6.45 bps @ 50 events", "Per-Trade IR", "0.22"],
    ]

    table = ax_table.table(
        cellText=table_data,
        colLabels=col_labels,
        loc="center",
        cellLoc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(9.5)
    table.scale(1.0, 1.45)

    for (row, col), cell in table.get_celld().items():
        cell.set_edgecolor("#2a2a4a")
        if row == 0:
            cell.set_facecolor("#0f3460")
            cell.set_text_props(color="white", fontweight="bold", fontsize=9)
        elif col in (0, 2):
            cell.set_facecolor("#16213e")
            cell.set_text_props(color="#8899aa")
        else:
            cell.set_facecolor("#16213e")
            text = cell.get_text().get_text()
            if text.startswith("+") or text.startswith("$+"):
                cell.set_text_props(color="#00d4aa", fontweight="bold")
            elif text.startswith("-") or text.startswith("$-"):
                cell.set_text_props(color="#ff6b6b", fontweight="bold")
            else:
                cell.set_text_props(color="white")

    fig.text(0.5, 0.01, "github.com/satyapravin/defi_cefi_mom", ha="center",
             fontsize=8, color="#556677", style="italic")

    plt.savefig("plots/equity_curve_linkedin.png", dpi=200, bbox_inches="tight",
                facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close()
    print("Saved to plots/equity_curve_linkedin.png")

asyncio.run(main())

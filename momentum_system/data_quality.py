"""Data quality checker for the momentum system database.

Connects to the SQLite database and reports:
  - Event counts by type, fee tier, and pair
  - Time coverage (first/last event, total duration)
  - Block range coverage and gap detection
  - Price sanity checks (zeros, NaNs, extreme values)
  - Event rate statistics over time
  - Liquidity event breakdown (mint vs burn)

Usage:
    python data_quality.py                        # default DB path from config
    python data_quality.py --db data/events.db    # explicit DB path
    python data_quality.py --json                 # machine-readable output
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


@dataclass
class TierStats:
    fee_tier: str
    count: int = 0
    min_block: int = 0
    max_block: int = 0
    min_ts: int = 0
    max_ts: int = 0
    avg_price: float = 0.0
    min_price: float = 0.0
    max_price: float = 0.0
    zero_price_count: int = 0
    null_return_count: int = 0
    extreme_return_count: int = 0


@dataclass
class LiquidityStats:
    fee_tier: str
    mints: int = 0
    burns: int = 0
    min_block: int = 0
    max_block: int = 0


@dataclass
class GapInfo:
    gap_start_block: int
    gap_end_block: int
    gap_blocks: int
    gap_start_ts: int = 0
    gap_end_ts: int = 0


@dataclass
class QualityReport:
    db_path: str
    pair_name: str
    swap_tiers: list[TierStats] = field(default_factory=list)
    liquidity_tiers: list[LiquidityStats] = field(default_factory=list)
    total_swaps: int = 0
    total_mints: int = 0
    total_burns: int = 0
    total_flow_buckets: int = 0
    first_event_ts: int = 0
    last_event_ts: int = 0
    coverage_hours: float = 0.0
    block_gaps: list[GapInfo] = field(default_factory=list)
    events_per_hour: float = 0.0
    events_per_minute: float = 0.0
    price_std: float = 0.0
    ok: bool = True
    warnings: list[str] = field(default_factory=list)


async def run_quality_check(db_path: str, pair_name: str) -> QualityReport:
    """Analyse the database and return a structured quality report."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    report = QualityReport(db_path=db_path, pair_name=pair_name)

    async with engine.connect() as conn:
        # ---- Swap event stats per tier ----
        rows = (await conn.execute(text(
            """SELECT fee_tier,
                      COUNT(*) as cnt,
                      MIN(block_number), MAX(block_number),
                      MIN(block_timestamp), MAX(block_timestamp),
                      AVG(price), MIN(price), MAX(price),
                      SUM(CASE WHEN price = 0 OR price IS NULL THEN 1 ELSE 0 END),
                      SUM(CASE WHEN log_return IS NULL THEN 1 ELSE 0 END),
                      SUM(CASE WHEN ABS(COALESCE(log_return, 0)) > 0.05 THEN 1 ELSE 0 END)
               FROM swap_events
               WHERE pair_name = :pair
               GROUP BY fee_tier
               ORDER BY fee_tier"""
        ), {"pair": pair_name})).fetchall()

        for r in rows:
            ts = TierStats(
                fee_tier=r[0], count=r[1],
                min_block=r[2], max_block=r[3],
                min_ts=r[4], max_ts=r[5],
                avg_price=r[6] or 0, min_price=r[7] or 0, max_price=r[8] or 0,
                zero_price_count=r[9], null_return_count=r[10],
                extreme_return_count=r[11],
            )
            report.swap_tiers.append(ts)
            report.total_swaps += ts.count
            if ts.zero_price_count > 0:
                report.warnings.append(f"{ts.fee_tier}: {ts.zero_price_count} zero/null prices")
            if ts.extreme_return_count > ts.count * 0.01:
                report.warnings.append(
                    f"{ts.fee_tier}: {ts.extreme_return_count} extreme returns (>5%)"
                )

        # ---- Liquidity event stats ----
        liq_rows = (await conn.execute(text(
            """SELECT fee_tier, action, COUNT(*),
                      MIN(block_number), MAX(block_number)
               FROM liquidity_events
               WHERE pair_name = :pair
               GROUP BY fee_tier, action
               ORDER BY fee_tier, action"""
        ), {"pair": pair_name})).fetchall()

        liq_by_tier: dict[str, LiquidityStats] = {}
        for r in liq_rows:
            tier = r[0]
            if tier not in liq_by_tier:
                liq_by_tier[tier] = LiquidityStats(fee_tier=tier)
            ls = liq_by_tier[tier]
            if r[1] == "mint":
                ls.mints = r[2]
                report.total_mints += r[2]
            else:
                ls.burns = r[2]
                report.total_burns += r[2]
            ls.min_block = min(ls.min_block, r[3]) if ls.min_block else r[3]
            ls.max_block = max(ls.max_block, r[4])
        report.liquidity_tiers = list(liq_by_tier.values())

        # ---- Flow buckets (optional table) ----
        try:
            bucket_row = (await conn.execute(text(
                "SELECT COUNT(*) FROM flow_buckets WHERE pair_name = :pair"
            ), {"pair": pair_name})).fetchone()
            report.total_flow_buckets = bucket_row[0] if bucket_row else 0
        except Exception:
            report.total_flow_buckets = 0

        # ---- Time coverage ----
        ts_row = (await conn.execute(text(
            """SELECT MIN(block_timestamp), MAX(block_timestamp)
               FROM swap_events WHERE pair_name = :pair"""
        ), {"pair": pair_name})).fetchone()

        if ts_row and ts_row[0] is not None:
            report.first_event_ts = ts_row[0]
            report.last_event_ts = ts_row[1]
            span_seconds = max(1, ts_row[1] - ts_row[0])
            report.coverage_hours = span_seconds / 3600
            report.events_per_hour = report.total_swaps / report.coverage_hours
            report.events_per_minute = report.total_swaps / (span_seconds / 60)

        # ---- Block gap detection (10+ minute gaps in bp5 data) ----
        gap_rows = (await conn.execute(text(
            """WITH ordered AS (
                 SELECT block_number, block_timestamp,
                        LAG(block_number) OVER (ORDER BY block_number) as prev_block,
                        LAG(block_timestamp) OVER (ORDER BY block_number) as prev_ts
                 FROM swap_events
                 WHERE pair_name = :pair AND fee_tier = 'bp5'
               )
               SELECT prev_block, block_number, block_number - prev_block,
                      prev_ts, block_timestamp
               FROM ordered
               WHERE block_number - prev_block > 2400
               ORDER BY block_number - prev_block DESC
               LIMIT 20"""
        ), {"pair": pair_name})).fetchall()

        for r in gap_rows:
            report.block_gaps.append(GapInfo(
                gap_start_block=r[0], gap_end_block=r[1],
                gap_blocks=r[2], gap_start_ts=r[3], gap_end_ts=r[4],
            ))
        if len(report.block_gaps) > 5:
            report.warnings.append(f"{len(report.block_gaps)} significant block gaps detected")

        # ---- Price standard deviation ----
        std_row = (await conn.execute(text(
            """SELECT AVG(price), AVG(price * price)
               FROM swap_events
               WHERE pair_name = :pair AND fee_tier = 'bp5' AND price > 0"""
        ), {"pair": pair_name})).fetchone()
        if std_row and std_row[0] is not None:
            mean = std_row[0]
            mean_sq = std_row[1]
            variance = max(0, mean_sq - mean * mean)
            report.price_std = math.sqrt(variance)

    await engine.dispose()

    report.ok = len(report.warnings) == 0
    return report


def _ts_to_str(ts: int) -> str:
    if ts == 0:
        return "N/A"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def print_report(report: QualityReport) -> None:
    """Pretty-print the quality report to stdout."""
    print()
    print("  ┌─────────────────────────────────────────────────────────┐")
    print("  │  Data Quality Report                                    │")
    print("  ├─────────────────────────────────────────────────────────┤")
    print(f"  │  Database:   {report.db_path:<43}│")
    print(f"  │  Pair:       {report.pair_name:<43}│")
    print(f"  │  Status:     {'✓ OK' if report.ok else '⚠ WARNINGS':<43}│")
    print("  └─────────────────────────────────────────────────────────┘")

    print("\n  ── Event Counts ──────────────────────────────────────────")
    print(f"  {'Type':<12} {'Count':>10} {'First Block':>14} {'Last Block':>14}")
    print(f"  {'─'*12} {'─'*10} {'─'*14} {'─'*14}")
    for t in report.swap_tiers:
        print(f"  swap/{t.fee_tier:<6} {t.count:>10,} {t.min_block:>14,} {t.max_block:>14,}")
    for t in report.liquidity_tiers:
        total = t.mints + t.burns
        print(f"  liq/{t.fee_tier:<7}{total:>10,} {t.min_block:>14,} {t.max_block:>14,}")
    print(f"  {'─'*52}")
    print(f"  {'Total swaps':<12} {report.total_swaps:>10,}")
    print(f"  {'Total mints':<12} {report.total_mints:>10,}")
    print(f"  {'Total burns':<12} {report.total_burns:>10,}")
    print(f"  {'Flow buckets':<12} {report.total_flow_buckets:>10,}")

    print("\n  ── Time Coverage ─────────────────────────────────────────")
    print(f"  First event:  {_ts_to_str(report.first_event_ts)}")
    print(f"  Last event:   {_ts_to_str(report.last_event_ts)}")
    print(f"  Span:         {report.coverage_hours:.1f} hours")
    print(f"  Avg rate:     {report.events_per_hour:,.0f} swaps/hour  ({report.events_per_minute:,.1f} swaps/min)")

    if report.swap_tiers:
        print("\n  ── Price Sanity (by tier) ─────────────────────────────────")
        print(f"  {'Tier':<8} {'Avg':>10} {'Min':>10} {'Max':>10} {'Zero':>6} {'Extreme':>8}")
        print(f"  {'─'*8} {'─'*10} {'─'*10} {'─'*10} {'─'*6} {'─'*8}")
        for t in report.swap_tiers:
            print(
                f"  {t.fee_tier:<8} {t.avg_price:>10,.2f} {t.min_price:>10,.2f} "
                f"{t.max_price:>10,.2f} {t.zero_price_count:>6} {t.extreme_return_count:>8}"
            )
        if report.price_std > 0:
            print(f"\n  bp5 price σ: {report.price_std:,.2f}")

    if report.liquidity_tiers:
        print("\n  ── Liquidity Breakdown ────────────────────────────────────")
        print(f"  {'Tier':<8} {'Mints':>8} {'Burns':>8} {'Ratio':>8}")
        print(f"  {'─'*8} {'─'*8} {'─'*8} {'─'*8}")
        for t in report.liquidity_tiers:
            total = t.mints + t.burns
            ratio = t.mints / t.burns if t.burns > 0 else float("inf")
            print(f"  {t.fee_tier:<8} {t.mints:>8,} {t.burns:>8,} {ratio:>8.2f}")

    if report.block_gaps:
        print(f"\n  ── Block Gaps (>{2400} blocks ≈ 10 min, bp5 tier) ────────")
        print(f"  {'From Block':>14} {'To Block':>14} {'Gap (blocks)':>14} {'Gap (min)':>10}")
        print(f"  {'─'*14} {'─'*14} {'─'*14} {'─'*10}")
        for g in report.block_gaps[:10]:
            gap_min = (g.gap_end_ts - g.gap_start_ts) / 60 if g.gap_end_ts > g.gap_start_ts else 0
            print(f"  {g.gap_start_block:>14,} {g.gap_end_block:>14,} {g.gap_blocks:>14,} {gap_min:>10.1f}")
        if len(report.block_gaps) > 10:
            print(f"  ... and {len(report.block_gaps) - 10} more gaps")

    if report.warnings:
        print("\n  ── Warnings ──────────────────────────────────────────────")
        for w in report.warnings:
            print(f"  ⚠  {w}")

    print()


def report_to_dict(report: QualityReport) -> dict:
    return {
        "db_path": report.db_path,
        "pair_name": report.pair_name,
        "ok": report.ok,
        "total_swaps": report.total_swaps,
        "total_mints": report.total_mints,
        "total_burns": report.total_burns,
        "total_flow_buckets": report.total_flow_buckets,
        "first_event_ts": report.first_event_ts,
        "last_event_ts": report.last_event_ts,
        "coverage_hours": round(report.coverage_hours, 2),
        "events_per_hour": round(report.events_per_hour, 1),
        "swap_tiers": [
            {
                "fee_tier": t.fee_tier, "count": t.count,
                "avg_price": round(t.avg_price, 2),
                "min_price": round(t.min_price, 2),
                "max_price": round(t.max_price, 2),
                "zero_prices": t.zero_price_count,
                "extreme_returns": t.extreme_return_count,
            }
            for t in report.swap_tiers
        ],
        "liquidity_tiers": [
            {"fee_tier": t.fee_tier, "mints": t.mints, "burns": t.burns}
            for t in report.liquidity_tiers
        ],
        "block_gaps": len(report.block_gaps),
        "warnings": report.warnings,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Check data quality in the momentum DB")
    parser.add_argument("--db", type=str, default=None, help="Path to SQLite database")
    parser.add_argument("--pair", type=str, default="ETH-USDC", help="Pair name")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--config", type=str, default="config.yaml", help="Config file")
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

    report = await run_quality_check(db_path, args.pair)

    if args.json:
        print(json.dumps(report_to_dict(report), indent=2))
    else:
        print_report(report)

    sys.exit(0 if report.ok else 1)


if __name__ == "__main__":
    asyncio.run(main())

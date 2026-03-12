"""Fetch historical Uniswap V3 events from an Arbitrum RPC and populate the DB.

Fetches Swap, Mint, and Burn events across all configured pool contracts,
resolves block timestamps, and inserts everything into the SQLite database
so that backtesting and parameter sweeps have data to work with.

Usage:
    # By date range (UTC)
    python historical_loader.py --rpc-url wss://arb-mainnet.g.alchemy.com/v2/KEY \
        --start-date 2024-01-01 --end-date 2024-02-01

    # By block range
    python historical_loader.py --rpc-url wss://arb-mainnet.g.alchemy.com/v2/KEY \
        --start-block 170000000 --end-block 180000000

    # Resume from last loaded block
    python historical_loader.py --rpc-url wss://arb-mainnet.g.alchemy.com/v2/KEY \
        --start-date 2024-01-01 --end-date 2024-02-01 --resume

    # HTTP RPC (recommended for large fetches)
    python historical_loader.py --rpc-url https://arb-mainnet.g.alchemy.com/v2/KEY \
        --start-date 2024-01-01 --end-date 2024-02-01
"""

from __future__ import annotations

import asyncio
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from web3 import AsyncHTTPProvider, AsyncWeb3, WebSocketProvider
from web3.types import LogReceipt

from config import Config, PairConfig, load_config
from database import Database
from logger import setup_logger
from models import FeeTier, LiquidityAction, LiquidityEvent, SwapEvent

logger = setup_logger()

SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
MINT_TOPIC = "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde"
BURN_TOPIC = "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c"

ARBITRUM_BLOCK_TIME_SECONDS = 0.25
RPC_TIMEOUT_SECONDS = 30


# ---------------------------------------------------------------------------
# Block timestamp cache
# ---------------------------------------------------------------------------

class _BlockTimestampCache:
    """Batch-fetches and caches block timestamps to avoid per-event RPC calls."""

    def __init__(self, w3: AsyncWeb3, batch_size: int = 50):
        self._w3 = w3
        self._cache: dict[int, int] = {}
        self._batch_size = batch_size

    async def get(self, block_number: int) -> int:
        if block_number in self._cache:
            return self._cache[block_number]
        block = await asyncio.wait_for(
            self._w3.eth.get_block(block_number),
            timeout=RPC_TIMEOUT_SECONDS,
        )
        ts = int(block["timestamp"])
        self._cache[block_number] = ts
        return ts

    async def prefetch(self, block_numbers: set[int]) -> None:
        missing = sorted(block_numbers - set(self._cache.keys()))
        for i in range(0, len(missing), self._batch_size):
            batch = missing[i : i + self._batch_size]
            tasks = [
                asyncio.wait_for(
                    self._w3.eth.get_block(bn),
                    timeout=RPC_TIMEOUT_SECONDS,
                )
                for bn in batch
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for bn, result in zip(batch, results):
                if isinstance(result, Exception):
                    logger.warning("Failed to fetch block", block=bn, error=str(result))
                    continue
                self._cache[bn] = int(result["timestamp"])


# ---------------------------------------------------------------------------
# Log parser (reuses EventListener logic as standalone functions)
# ---------------------------------------------------------------------------

def _to_bytes(data) -> bytes:
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return bytes.fromhex(data[2:]) if data.startswith("0x") else bytes.fromhex(data)
    return bytes(data)


def _to_hex(val) -> str:
    if isinstance(val, bytes):
        h = val.hex()
    elif isinstance(val, str):
        h = val
    else:
        h = str(val)
    return h if h.startswith("0x") else "0x" + h


def _decode_int24(topic) -> int:
    raw = _to_bytes(topic) if not isinstance(topic, bytes) else topic
    val = int.from_bytes(raw[-3:], "big", signed=False)
    if val >= 0x800000:
        val -= 0x1000000
    return val


def compute_price(
    sqrt_price_x96: int,
    token0_decimals: int,
    token1_decimals: int,
    invert: bool,
) -> float:
    p_raw = (sqrt_price_x96 / (2**96)) ** 2
    p = p_raw * 10 ** (token0_decimals - token1_decimals)
    if invert:
        p = 1.0 / p if p != 0 else 0.0
    return p


def parse_swap_log(
    log: LogReceipt,
    pair_name: str,
    fee_tier: FeeTier,
    pool_address: str,
    pair_config: PairConfig,
    block_timestamp: int,
    prev_price: Optional[float],
) -> tuple[SwapEvent, float]:
    """Parse a raw Swap log into a SwapEvent.  Returns (event, price) so
    the caller can track the last price for log-return computation."""
    data = _to_bytes(log["data"])

    amount0 = int.from_bytes(data[0:32], "big", signed=True)
    amount1 = int.from_bytes(data[32:64], "big", signed=True)
    sqrt_price_x96 = int.from_bytes(data[64:96], "big", signed=False)
    liquidity = int.from_bytes(data[96:128], "big", signed=False)
    tick = int.from_bytes(data[128:160], "big", signed=True)

    price = compute_price(
        sqrt_price_x96,
        pair_config.token0_decimals,
        pair_config.token1_decimals,
        pair_config.invert_price,
    )

    log_return = None
    direction = None
    if prev_price is not None and prev_price > 0 and price > 0:
        r = math.log(price) - math.log(prev_price)
        log_return = r
        direction = 1 if r > 0 else (-1 if r < 0 else 0)

    return SwapEvent(
        pair_name=pair_name,
        fee_tier=fee_tier,
        block_number=log["blockNumber"],
        block_timestamp=block_timestamp,
        transaction_hash=_to_hex(log["transactionHash"]),
        pool_address=pool_address,
        sqrt_price_x96=sqrt_price_x96,
        tick=tick,
        liquidity=liquidity,
        amount0=amount0,
        amount1=amount1,
        price=price,
        log_return=log_return,
        direction=direction,
    ), price


def parse_liquidity_log(
    log: LogReceipt,
    topic0: str,
    pair_name: str,
    fee_tier: FeeTier,
    pool_address: str,
    block_timestamp: int,
    current_tick: int,
) -> Optional[LiquidityEvent]:
    topics = log.get("topics", [])

    if topic0 == MINT_TOPIC:
        action = LiquidityAction.MINT
        if len(topics) < 4:
            return None
        tick_lower = _decode_int24(topics[2])
        tick_upper = _decode_int24(topics[3])
        data = _to_bytes(log["data"])
        amount = int.from_bytes(data[32:64], "big", signed=False)
        amount0 = int.from_bytes(data[64:96], "big", signed=False)
        amount1 = int.from_bytes(data[96:128], "big", signed=False)
    elif topic0 == BURN_TOPIC:
        action = LiquidityAction.BURN
        if len(topics) < 4:
            return None
        tick_lower = _decode_int24(topics[2])
        tick_upper = _decode_int24(topics[3])
        data = _to_bytes(log["data"])
        amount = int.from_bytes(data[0:32], "big", signed=False)
        amount0 = int.from_bytes(data[32:64], "big", signed=False)
        amount1 = int.from_bytes(data[64:96], "big", signed=False)
    else:
        return None

    return LiquidityEvent(
        pair_name=pair_name,
        fee_tier=fee_tier,
        block_number=log["blockNumber"],
        block_timestamp=block_timestamp,
        transaction_hash=_to_hex(log["transactionHash"]),
        pool_address=pool_address,
        action=action,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        amount=amount,
        amount0=amount0,
        amount1=amount1,
        current_tick=current_tick,
    )


# ---------------------------------------------------------------------------
# Progress tracker
# ---------------------------------------------------------------------------

@dataclass
class _Progress:
    total_blocks: int = 0
    blocks_done: int = 0
    swaps_loaded: int = 0
    mints_loaded: int = 0
    burns_loaded: int = 0
    errors: int = 0
    start_time: float = field(default_factory=time.time)

    def report(self, current_block: int, end_block: int) -> None:
        elapsed = time.time() - self.start_time
        pct = self.blocks_done / self.total_blocks * 100 if self.total_blocks else 0
        rate = self.blocks_done / elapsed if elapsed > 0 else 0
        remaining = (self.total_blocks - self.blocks_done) / rate if rate > 0 else 0

        eta_min = remaining / 60
        print(
            f"\r  [{pct:5.1f}%] Block {current_block:,} / {end_block:,}"
            f"  |  Swaps: {self.swaps_loaded:,}  Mints: {self.mints_loaded:,}"
            f"  Burns: {self.burns_loaded:,}  Errors: {self.errors}"
            f"  |  {rate:,.0f} blocks/s  ETA: {eta_min:.1f} min",
            end="", flush=True,
        )


# ---------------------------------------------------------------------------
# Core loader
# ---------------------------------------------------------------------------

async def load_historical(
    config: Config,
    db: Database,
    rpc_url: str,
    start_block: int,
    end_block: int,
    batch_size: int = 2000,
    max_retries: int = 5,
) -> _Progress:
    """Fetch all Swap/Mint/Burn events from start_block to end_block and
    insert them into the database."""

    if rpc_url.startswith("http"):
        w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url))
    else:
        w3 = AsyncWeb3(WebSocketProvider(rpc_url))

    connected = await w3.is_connected()
    if not connected:
        raise ConnectionError(f"Cannot connect to RPC: {rpc_url}")

    chain_id = await w3.eth.chain_id
    logger.info("Connected to RPC", chain_id=chain_id, rpc=rpc_url[:50] + "...")

    pool_map: dict[str, tuple[str, FeeTier, PairConfig]] = {}
    for pair_cfg in config.pairs:
        for tier_name, pool_cfg in [
            ("bp30", pair_cfg.pools.bp30),
            ("bp5", pair_cfg.pools.bp5),
            ("bp1", pair_cfg.pools.bp1),
        ]:
            addr = pool_cfg.address.lower()
            pool_map[addr] = (pair_cfg.name, FeeTier(tier_name), pair_cfg)

    pool_addresses = [AsyncWeb3.to_checksum_address(a) for a in pool_map.keys()]

    ts_cache = _BlockTimestampCache(w3)
    last_price: dict[str, float] = {}
    last_tick: dict[str, int] = {}

    progress = _Progress(total_blocks=end_block - start_block)

    print(f"\n  Loading blocks {start_block:,} → {end_block:,} ({progress.total_blocks:,} blocks)")
    print(f"  Pools: {len(pool_addresses)}  |  Batch size: {batch_size:,}\n")

    current = start_block
    while current <= end_block:
        chunk_end = min(current + batch_size - 1, end_block)
        retries = 0

        while retries <= max_retries:
            try:
                swap_logs = await asyncio.wait_for(
                    w3.eth.get_logs({
                        "fromBlock": current,
                        "toBlock": chunk_end,
                        "address": pool_addresses,
                        "topics": [SWAP_TOPIC],
                    }),
                    timeout=RPC_TIMEOUT_SECONDS,
                )

                liq_logs = await asyncio.wait_for(
                    w3.eth.get_logs({
                        "fromBlock": current,
                        "toBlock": chunk_end,
                        "address": pool_addresses,
                        "topics": [[MINT_TOPIC, BURN_TOPIC]],
                    }),
                    timeout=RPC_TIMEOUT_SECONDS,
                )
                break
            except asyncio.TimeoutError:
                retries += 1
                wait = min(2 ** retries, 15)
                logger.warning(
                    "RPC call timed out, retrying",
                    attempt=retries, timeout=RPC_TIMEOUT_SECONDS,
                )
                await asyncio.sleep(wait)
                continue
            except Exception as e:
                retries += 1
                if retries > max_retries:
                    logger.error(
                        "Max retries exceeded for block range",
                        start=current, end=chunk_end, error=str(e),
                    )
                    progress.errors += 1
                    break

                err_str = str(e).lower()
                if "too many" in err_str or "rate" in err_str or "limit" in err_str:
                    wait = min(2 ** retries, 30)
                    logger.warning("Rate limited, backing off", seconds=wait)
                    await asyncio.sleep(wait)
                elif "range" in err_str or "block" in err_str:
                    batch_size = max(batch_size // 2, 100)
                    chunk_end = min(current + batch_size - 1, end_block)
                    logger.warning("Block range too large, reducing batch", new_size=batch_size)
                else:
                    wait = min(2 ** retries, 10)
                    logger.warning("RPC error, retrying", attempt=retries, error=str(e))
                    await asyncio.sleep(wait)
        else:
            current = chunk_end + 1
            progress.blocks_done = current - start_block
            continue

        all_logs = list(swap_logs) + list(liq_logs)
        if all_logs:
            block_numbers = {log["blockNumber"] for log in all_logs}
            await ts_cache.prefetch(block_numbers)

        for log in swap_logs:
            pool_addr = log["address"].lower()
            if pool_addr not in pool_map:
                continue

            pair_name, fee_tier, pair_cfg = pool_map[pool_addr]
            try:
                block_ts = await ts_cache.get(log["blockNumber"])
                prev = last_price.get(pool_addr)

                event, price = parse_swap_log(
                    log, pair_name, fee_tier, pool_addr,
                    pair_cfg, block_ts, prev,
                )
                last_price[pool_addr] = price
                last_tick[pool_addr] = event.tick

                await db.insert_swap_event(event)
                progress.swaps_loaded += 1
            except Exception as e:
                progress.errors += 1
                logger.debug("Failed to parse swap", error=str(e))

        for log in liq_logs:
            pool_addr = log["address"].lower()
            if pool_addr not in pool_map:
                continue

            pair_name, fee_tier, pair_cfg = pool_map[pool_addr]
            topics = log.get("topics", [])
            if not topics:
                continue

            topic0 = topics[0]
            if isinstance(topic0, bytes):
                topic0 = "0x" + topic0.hex()

            try:
                block_ts = await ts_cache.get(log["blockNumber"])
                tick = last_tick.get(pool_addr, 0)

                event = parse_liquidity_log(
                    log, topic0, pair_name, fee_tier,
                    pool_addr, block_ts, tick,
                )
                if event is not None:
                    await db.insert_liquidity_event(event)
                    if event.action == LiquidityAction.MINT:
                        progress.mints_loaded += 1
                    else:
                        progress.burns_loaded += 1
            except Exception as e:
                progress.errors += 1
                logger.debug("Failed to parse liquidity log", error=str(e))

        current = chunk_end + 1
        progress.blocks_done = current - start_block
        progress.report(current, end_block)

    print()
    return progress


# ---------------------------------------------------------------------------
# Helpers: date ↔ block conversion
# ---------------------------------------------------------------------------

async def date_to_block(w3: AsyncWeb3, target_date: str) -> int:
    """Binary search for the block closest to a UTC date string (YYYY-MM-DD)."""
    target_ts = int(datetime.strptime(target_date, "%Y-%m-%d")
                     .replace(tzinfo=timezone.utc).timestamp())

    latest = await w3.eth.block_number
    lo, hi = 0, latest

    for _ in range(50):
        if lo >= hi:
            break
        mid = (lo + hi) // 2
        block = await asyncio.wait_for(
            w3.eth.get_block(mid), timeout=RPC_TIMEOUT_SECONDS,
        )
        block_ts = int(block["timestamp"])
        if block_ts < target_ts:
            lo = mid + 1
        else:
            hi = mid

    return lo


async def get_max_loaded_block(db: Database, pair_name: str) -> int:
    """Return the highest block_number already in swap_events for resume."""
    from sqlalchemy import text
    async with db._engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT COALESCE(MAX(block_number), 0) FROM swap_events "
                "WHERE pair_name = :pair_name"
            ),
            {"pair_name": pair_name},
        )
        row = result.fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Fetch historical Uniswap V3 events and populate the database"
    )
    parser.add_argument(
        "--rpc-url", type=str, required=True,
        help="Arbitrum RPC URL (HTTP or WebSocket). E.g. https://arb-mainnet.g.alchemy.com/v2/KEY",
    )
    parser.add_argument("--start-date", type=str, default=None, help="Start date UTC (YYYY-MM-DD)")
    parser.add_argument("--end-date", type=str, default=None, help="End date UTC (YYYY-MM-DD)")
    parser.add_argument("--start-block", type=int, default=None, help="Start block number")
    parser.add_argument("--end-block", type=int, default=None, help="End block number")
    parser.add_argument(
        "--batch-size", type=int, default=2000,
        help="Blocks per eth_getLogs request (auto-reduced if RPC rejects). Default: 2000",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume from the last loaded block in the database",
    )
    parser.add_argument("--config", type=str, default="config.yaml", help="Path to config file")
    parser.add_argument("--pair", type=str, default=None, help="Pair name (default: first in config)")

    args = parser.parse_args()

    config = load_config(args.config)
    pair_name = args.pair or config.pairs[0].name

    db = Database(config.system.database_path)
    await db.initialize()

    rpc_url = args.rpc_url

    if rpc_url.startswith("http"):
        w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url))
    else:
        w3 = AsyncWeb3(WebSocketProvider(rpc_url))

    if not await w3.is_connected():
        print(f"ERROR: Cannot connect to RPC: {rpc_url}")
        sys.exit(1)

    if args.start_block is not None:
        start_block = args.start_block
    elif args.start_date is not None:
        print(f"  Resolving start date {args.start_date} to block number...")
        start_block = await date_to_block(w3, args.start_date)
        print(f"  → Block {start_block:,}")
    else:
        print("ERROR: Provide either --start-date or --start-block")
        sys.exit(1)

    if args.end_block is not None:
        end_block = args.end_block
    elif args.end_date is not None:
        print(f"  Resolving end date {args.end_date} to block number...")
        end_block = await date_to_block(w3, args.end_date)
        print(f"  → Block {end_block:,}")
    else:
        end_block = await w3.eth.block_number
        print(f"  No end specified, using latest block: {end_block:,}")

    if args.resume:
        max_block = await get_max_loaded_block(db, pair_name)
        if max_block > start_block:
            print(f"  Resuming from block {max_block + 1:,} (previously loaded up to {max_block:,})")
            start_block = max_block + 1

    if start_block >= end_block:
        print("  Nothing to load (start >= end). Database may already be up to date.")
        return

    total_blocks = end_block - start_block
    est_days = total_blocks * ARBITRUM_BLOCK_TIME_SECONDS / 86400

    print(f"\n  ┌─────────────────────────────────────────────┐")
    print(f"  │  Historical Data Loader                      │")
    print(f"  ├─────────────────────────────────────────────┤")
    print(f"  │  Pair:    {pair_name:<35}│")
    print(f"  │  Blocks:  {start_block:>12,} → {end_block:<12,}    │")
    print(f"  │  Span:    {total_blocks:>12,} blocks (~{est_days:.1f} days)  │")
    print(f"  │  Batch:   {args.batch_size:>12,} blocks/request       │")
    print(f"  └─────────────────────────────────────────────┘")

    t0 = time.time()
    progress = await load_historical(
        config, db, rpc_url, start_block, end_block, args.batch_size,
    )
    elapsed = time.time() - t0

    print(f"\n  ┌─────────────────────────────────────────────┐")
    print(f"  │  Load Complete                                │")
    print(f"  ├─────────────────────────────────────────────┤")
    print(f"  │  Swaps loaded:   {progress.swaps_loaded:>10,}                 │")
    print(f"  │  Mints loaded:   {progress.mints_loaded:>10,}                 │")
    print(f"  │  Burns loaded:   {progress.burns_loaded:>10,}                 │")
    print(f"  │  Parse errors:   {progress.errors:>10,}                 │")
    print(f"  │  Time elapsed:   {elapsed:>10.1f}s                │")
    print(f"  │  Database:       {config.system.database_path:<25}│")
    print(f"  └─────────────────────────────────────────────┘\n")


if __name__ == "__main__":
    asyncio.run(main())

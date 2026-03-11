from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from models import (
    FeeTier,
    SwapEvent,
    SignalState,
    OrderState,
    TradeRecord,
    Direction,
    OrderStatus,
    SignalTransition,
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS swap_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_name TEXT NOT NULL,
    fee_tier TEXT NOT NULL,
    block_number INTEGER NOT NULL,
    block_timestamp INTEGER NOT NULL,
    transaction_hash TEXT NOT NULL,
    pool_address TEXT NOT NULL,
    sqrt_price_x96 TEXT NOT NULL,
    tick INTEGER NOT NULL,
    liquidity TEXT NOT NULL,
    amount0 TEXT NOT NULL,
    amount1 TEXT NOT NULL,
    price REAL NOT NULL,
    log_return REAL,
    direction INTEGER,
    created_at REAL NOT NULL,
    UNIQUE(transaction_hash, pool_address)
);

CREATE TABLE IF NOT EXISTS signal_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_name TEXT NOT NULL,
    timestamp REAL NOT NULL,
    conviction_30 REAL NOT NULL,
    trend_state_30 INTEGER NOT NULL,
    momentum_5 REAL NOT NULL,
    autocorrelation_5 REAL NOT NULL,
    momentum_flag_5 INTEGER NOT NULL,
    combined_signal REAL NOT NULL,
    transition TEXT NOT NULL,
    intensity_30 REAL NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS order_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    pair_name TEXT NOT NULL,
    instrument TEXT NOT NULL,
    direction INTEGER NOT NULL,
    size REAL NOT NULL,
    limit_price REAL NOT NULL,
    status TEXT NOT NULL,
    placed_at REAL NOT NULL,
    filled_at REAL,
    fill_price REAL,
    filled_size REAL NOT NULL DEFAULT 0,
    cancel_reason TEXT,
    label TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_name TEXT NOT NULL,
    instrument TEXT NOT NULL,
    direction INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    size REAL NOT NULL,
    entry_time REAL NOT NULL,
    exit_time REAL NOT NULL,
    gross_pnl_usd REAL NOT NULL,
    fees_usd REAL NOT NULL,
    net_pnl_usd REAL NOT NULL,
    signal_at_entry REAL NOT NULL,
    exit_reason TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_swap_pair_tier
    ON swap_events(pair_name, fee_tier, block_timestamp);

CREATE INDEX IF NOT EXISTS idx_signal_pair
    ON signal_log(pair_name, timestamp);

CREATE INDEX IF NOT EXISTS idx_trade_pair
    ON trade_log(pair_name, exit_time);
"""


class Database:
    def __init__(self, db_path: str):
        self._db_path = db_path
        parent = Path(db_path).parent
        parent.mkdir(parents=True, exist_ok=True)
        self._engine: AsyncEngine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            echo=False,
        )

    async def initialize(self) -> None:
        async with self._engine.begin() as conn:
            for statement in _SCHEMA_SQL.strip().split(";"):
                stmt = statement.strip()
                if stmt:
                    await conn.execute(text(stmt))

    async def insert_swap_event(self, event: SwapEvent) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    """INSERT OR IGNORE INTO swap_events
                    (pair_name, fee_tier, block_number, block_timestamp,
                     transaction_hash, pool_address, sqrt_price_x96, tick,
                     liquidity, amount0, amount1, price, log_return,
                     direction, created_at)
                    VALUES
                    (:pair_name, :fee_tier, :block_number, :block_timestamp,
                     :tx_hash, :pool_address, :sqrt_price_x96, :tick,
                     :liquidity, :amount0, :amount1, :price, :log_return,
                     :direction, :created_at)"""
                ),
                {
                    "pair_name": event.pair_name,
                    "fee_tier": event.fee_tier.value,
                    "block_number": event.block_number,
                    "block_timestamp": event.block_timestamp,
                    "tx_hash": event.transaction_hash,
                    "pool_address": event.pool_address,
                    "sqrt_price_x96": str(event.sqrt_price_x96),
                    "tick": event.tick,
                    "liquidity": str(event.liquidity),
                    "amount0": str(event.amount0),
                    "amount1": str(event.amount1),
                    "price": event.price,
                    "log_return": event.log_return,
                    "direction": event.direction,
                    "created_at": time.time(),
                },
            )

    async def insert_signal_state(self, state: SignalState) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    """INSERT INTO signal_log
                    (pair_name, timestamp, conviction_30, trend_state_30,
                     momentum_5, autocorrelation_5, momentum_flag_5,
                     combined_signal, transition, intensity_30, created_at)
                    VALUES
                    (:pair_name, :timestamp, :conviction_30, :trend_state_30,
                     :momentum_5, :autocorrelation_5, :momentum_flag_5,
                     :combined_signal, :transition, :intensity_30, :created_at)"""
                ),
                {
                    "pair_name": state.pair_name,
                    "timestamp": state.timestamp,
                    "conviction_30": state.conviction_30,
                    "trend_state_30": state.trend_state_30,
                    "momentum_5": state.momentum_5,
                    "autocorrelation_5": state.autocorrelation_5,
                    "momentum_flag_5": int(state.momentum_flag_5),
                    "combined_signal": state.combined_signal,
                    "transition": state.transition.value,
                    "intensity_30": state.intensity_30,
                    "created_at": time.time(),
                },
            )

    async def insert_order(self, order: OrderState) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    """INSERT INTO order_log
                    (order_id, pair_name, instrument, direction, size,
                     limit_price, status, placed_at, filled_at, fill_price,
                     filled_size, cancel_reason, label, created_at)
                    VALUES
                    (:order_id, :pair_name, :instrument, :direction, :size,
                     :limit_price, :status, :placed_at, :filled_at,
                     :fill_price, :filled_size, :cancel_reason, :label,
                     :created_at)"""
                ),
                {
                    "order_id": order.order_id,
                    "pair_name": order.request.pair_name,
                    "instrument": order.request.instrument,
                    "direction": order.request.direction.value,
                    "size": order.request.size,
                    "limit_price": order.request.limit_price,
                    "status": order.status.value,
                    "placed_at": order.placed_at,
                    "filled_at": order.filled_at,
                    "fill_price": order.fill_price,
                    "filled_size": order.filled_size,
                    "cancel_reason": order.cancel_reason,
                    "label": order.request.label,
                    "created_at": time.time(),
                },
            )

    async def update_order(self, order_id: str, **kwargs) -> None:
        if not kwargs:
            return
        set_clauses = ", ".join(f"{k} = :{k}" for k in kwargs)
        params = {**kwargs, "order_id": order_id}
        async with self._engine.begin() as conn:
            await conn.execute(
                text(f"UPDATE order_log SET {set_clauses} WHERE order_id = :order_id"),
                params,
            )

    async def insert_trade(self, trade: TradeRecord) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    """INSERT INTO trade_log
                    (pair_name, instrument, direction, entry_price, exit_price,
                     size, entry_time, exit_time, gross_pnl_usd, fees_usd,
                     net_pnl_usd, signal_at_entry, exit_reason, created_at)
                    VALUES
                    (:pair_name, :instrument, :direction, :entry_price,
                     :exit_price, :size, :entry_time, :exit_time,
                     :gross_pnl_usd, :fees_usd, :net_pnl_usd,
                     :signal_at_entry, :exit_reason, :created_at)"""
                ),
                {
                    "pair_name": trade.pair_name,
                    "instrument": trade.instrument,
                    "direction": trade.direction.value,
                    "entry_price": trade.entry_price,
                    "exit_price": trade.exit_price,
                    "size": trade.size,
                    "entry_time": trade.entry_time,
                    "exit_time": trade.exit_time,
                    "gross_pnl_usd": trade.gross_pnl_usd,
                    "fees_usd": trade.fees_usd,
                    "net_pnl_usd": trade.net_pnl_usd,
                    "signal_at_entry": trade.signal_at_entry,
                    "exit_reason": trade.exit_reason,
                    "created_at": time.time(),
                },
            )

    async def get_swap_events(
        self,
        pair_name: str,
        fee_tier: FeeTier,
        since_timestamp: Optional[int] = None,
        limit: int = 500,
    ) -> list[SwapEvent]:
        query = """SELECT pair_name, fee_tier, block_number, block_timestamp,
                          transaction_hash, pool_address, sqrt_price_x96, tick,
                          liquidity, amount0, amount1, price, log_return,
                          direction
                   FROM swap_events
                   WHERE pair_name = :pair_name AND fee_tier = :fee_tier"""
        params: dict = {"pair_name": pair_name, "fee_tier": fee_tier.value}
        if since_timestamp is not None:
            query += " AND block_timestamp >= :since"
            params["since"] = since_timestamp
        query += " ORDER BY block_timestamp ASC LIMIT :limit"
        params["limit"] = limit

        async with self._engine.connect() as conn:
            result = await conn.execute(text(query), params)
            rows = result.fetchall()

        events: list[SwapEvent] = []
        for r in rows:
            events.append(
                SwapEvent(
                    pair_name=r[0],
                    fee_tier=FeeTier(r[1]),
                    block_number=r[2],
                    block_timestamp=r[3],
                    transaction_hash=r[4],
                    pool_address=r[5],
                    sqrt_price_x96=int(r[6]),
                    tick=r[7],
                    liquidity=int(r[8]),
                    amount0=int(r[9]),
                    amount1=int(r[10]),
                    price=r[11],
                    log_return=r[12],
                    direction=r[13],
                )
            )
        return events

    async def get_daily_pnl(self, pair_name: str, since_hours: int = 24) -> float:
        cutoff = time.time() - since_hours * 3600
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(
                    """SELECT COALESCE(SUM(net_pnl_usd), 0)
                       FROM trade_log
                       WHERE pair_name = :pair_name AND exit_time >= :cutoff"""
                ),
                {"pair_name": pair_name, "cutoff": cutoff},
            )
            row = result.fetchone()
        return float(row[0]) if row else 0.0

    async def get_recent_trades(
        self, pair_name: str, limit: int = 100
    ) -> list[TradeRecord]:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(
                    """SELECT pair_name, instrument, direction, entry_price,
                              exit_price, size, entry_time, exit_time,
                              gross_pnl_usd, fees_usd, net_pnl_usd,
                              signal_at_entry, exit_reason
                       FROM trade_log
                       WHERE pair_name = :pair_name
                       ORDER BY exit_time DESC LIMIT :limit"""
                ),
                {"pair_name": pair_name, "limit": limit},
            )
            rows = result.fetchall()

        return [
            TradeRecord(
                pair_name=r[0],
                instrument=r[1],
                direction=Direction(r[2]),
                entry_price=r[3],
                exit_price=r[4],
                size=r[5],
                entry_time=r[6],
                exit_time=r[7],
                gross_pnl_usd=r[8],
                fees_usd=r[9],
                net_pnl_usd=r[10],
                signal_at_entry=r[11],
                exit_reason=r[12],
            )
            for r in rows
        ]

    async def get_all_swap_events_in_range(
        self,
        pair_name: str,
        start_timestamp: int,
        end_timestamp: int,
    ) -> list[SwapEvent]:
        """Fetch all swap events across all fee tiers in a time range,
        ordered by block_timestamp. Used by the backtester."""
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(
                    """SELECT pair_name, fee_tier, block_number, block_timestamp,
                              transaction_hash, pool_address, sqrt_price_x96,
                              tick, liquidity, amount0, amount1, price,
                              log_return, direction
                       FROM swap_events
                       WHERE pair_name = :pair_name
                         AND block_timestamp >= :start_ts
                         AND block_timestamp <= :end_ts
                       ORDER BY block_timestamp ASC, id ASC"""
                ),
                {
                    "pair_name": pair_name,
                    "start_ts": start_timestamp,
                    "end_ts": end_timestamp,
                },
            )
            rows = result.fetchall()

        return [
            SwapEvent(
                pair_name=r[0],
                fee_tier=FeeTier(r[1]),
                block_number=r[2],
                block_timestamp=r[3],
                transaction_hash=r[4],
                pool_address=r[5],
                sqrt_price_x96=int(r[6]),
                tick=r[7],
                liquidity=int(r[8]),
                amount0=int(r[9]),
                amount1=int(r[10]),
                price=r[11],
                log_return=r[12],
                direction=r[13],
            )
            for r in rows
        ]

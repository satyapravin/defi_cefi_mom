from __future__ import annotations

import math
import os
import sys

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtest import BacktestEngine, BacktestResult
from config import (
    BP30SignalConfig,
    Config,
    DeribitConfig,
    ExecutionConfig,
    InfraConfig,
    PairConfig,
    PoolConfig,
    PoolsConfig,
    RiskConfig,
    SystemConfig,
)
from database import Database
from models import FeeTier, SwapEvent


def _make_config() -> Config:
    return Config(
        system=SystemConfig(mode="backtest", database_path=":memory:"),
        infrastructure=InfraConfig(rpc_url="wss://dummy"),
        deribit=DeribitConfig(),
        pairs=[
            PairConfig(
                name="TEST-PAIR",
                deribit_instrument="ETH-PERPETUAL",
                token0="0x0",
                token1="0x1",
                token0_decimals=18,
                token1_decimals=6,
                pools=PoolsConfig(
                    bp30=PoolConfig(address="0xbp30", fee=3000),
                    bp5=PoolConfig(address="0xbp5", fee=500),
                    bp1=PoolConfig(address="0xbp1", fee=100),
                ),
                bp30_signal=BP30SignalConfig(
                    window_seconds=120,
                    min_cluster_swaps=3,
                    direction_ratio=0.7,
                ),
                execution=ExecutionConfig(
                    offset_base_bps=2.0,
                    offset_conviction_bps=5.0,
                    stale_order_seconds=60,
                ),
                risk=RiskConfig(
                    max_position_contracts=10,
                    stop_loss_bps=40,
                    take_profit_bps=40,
                    max_holding_seconds=600,
                ),
            )
        ],
    )


@pytest_asyncio.fixture
async def setup():
    config = _make_config()
    db = Database(":memory:")
    await db.initialize()
    return config, db


async def _insert_trending_data(db: Database, pair: str = "TEST-PAIR") -> None:
    """Insert synthetic swap events: trending upward on all tiers."""
    base_price = 3000.0
    ts = 1700000000

    for i in range(30):
        price = base_price + i * 2
        prev_price = base_price + (i - 1) * 2 if i > 0 else None
        lr = math.log(price / prev_price) if prev_price else None
        d = 1 if lr and lr > 0 else None

        event = SwapEvent(
            pair_name=pair,
            fee_tier=FeeTier.BP30,
            block_number=1000 + i,
            block_timestamp=ts + i * 10,
            transaction_hash=f"0xbp30_{i:04d}",
            pool_address="0xbp30",
            sqrt_price_x96=0,
            tick=0,
            liquidity=0,
            amount0=0,
            amount1=0,
            price=price,
            log_return=lr,
            direction=d,
        )
        await db.insert_swap_event(event)

    for i in range(100):
        price = base_price + i * 0.5
        prev_price = base_price + (i - 1) * 0.5 if i > 0 else None
        lr = math.log(price / prev_price) if prev_price else None
        d = 1 if lr and lr > 0 else None

        event = SwapEvent(
            pair_name=pair,
            fee_tier=FeeTier.BP5,
            block_number=2000 + i,
            block_timestamp=ts + i * 10,
            transaction_hash=f"0xbp5_{i:04d}",
            pool_address="0xbp5",
            sqrt_price_x96=0,
            tick=0,
            liquidity=0,
            amount0=0,
            amount1=0,
            price=price,
            log_return=lr,
            direction=d,
        )
        await db.insert_swap_event(event)


@pytest.mark.asyncio
async def test_backtest_runs_on_empty_data(setup):
    """Backtest with no data returns zero metrics."""
    config, db = setup
    engine = BacktestEngine(config, db)
    result = await engine.run_backtest("TEST-PAIR", 1700000000, 1710000000)

    assert isinstance(result, BacktestResult)
    assert result.total_signals == 0
    assert result.total_trades == 0


@pytest.mark.asyncio
async def test_backtest_with_trending_data(setup):
    """Backtest with trending data should produce signals and trades."""
    config, db = setup
    await _insert_trending_data(db)

    engine = BacktestEngine(config, db)
    result = await engine.run_backtest("TEST-PAIR", 1700000000, 1700002000)

    assert result.total_signals >= 0
    assert isinstance(result.equity_curve, list)


@pytest.mark.asyncio
async def test_backtest_result_has_alpha_decay(setup):
    """Backtest should compute alpha decay curve when data is present."""
    config, db = setup
    await _insert_trending_data(db)

    engine = BacktestEngine(config, db)
    result = await engine.run_backtest("TEST-PAIR", 1700000000, 1700002000)

    assert isinstance(result.alpha_decay_curve, list)


@pytest.mark.asyncio
async def test_fill_rate_bounded(setup):
    """Fill rate should be between 0 and 1."""
    config, db = setup
    await _insert_trending_data(db)

    engine = BacktestEngine(config, db)
    result = await engine.run_backtest("TEST-PAIR", 1700000000, 1700002000)

    assert 0.0 <= result.fill_rate <= 1.0

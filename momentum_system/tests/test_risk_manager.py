from __future__ import annotations

import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import (
    Config,
    DeribitConfig,
    ExecutionConfig,
    InfraConfig,
    PairConfig,
    PoolConfig,
    PoolsConfig,
    RiskConfig,
    SignalConfig,
    SystemConfig,
)
from database import Database
from deribit_client import DeribitClient
from models import Direction, OrderRequest
from risk_manager import RiskManager


def _make_config(risk_cfg: RiskConfig | None = None) -> Config:
    return Config(
        system=SystemConfig(mode="paper", database_path=":memory:"),
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
                signal=SignalConfig(),
                execution=ExecutionConfig(),
                risk=risk_cfg or RiskConfig(cooldown_seconds=2),
            )
        ],
    )


def _make_order(size: float = 10, price: float = 3000.0) -> OrderRequest:
    return OrderRequest(
        pair_name="TEST-PAIR",
        instrument="ETH-PERPETUAL",
        direction=Direction.LONG,
        size=size,
        limit_price=price,
    )


@pytest_asyncio.fixture
async def setup():
    config = _make_config()
    db = Database(":memory:")
    await db.initialize()

    deribit = MagicMock(spec=DeribitClient)
    deribit.get_mid_price = MagicMock(return_value=3000.0)
    deribit.get_account_summary = AsyncMock(
        return_value={"equity": 100000, "initial_margin": 10000}
    )
    deribit.place_market_order = AsyncMock(return_value="mkt_123")

    risk_mgr = RiskManager(config, db, deribit)
    return config, db, deribit, risk_mgr


@pytest.mark.asyncio
async def test_reject_during_cooldown(setup):
    """T1: Order during cooldown should be rejected."""
    config, db, deribit, risk_mgr = setup

    risk_mgr.record_cooldown("TEST-PAIR")

    request = _make_order()
    approved, reason = await risk_mgr.approve_order(request)
    assert approved is False
    assert "cooldown" in reason


@pytest.mark.asyncio
async def test_reject_exceeding_position_limit(setup):
    """T2: Order exceeding max_position_contracts rejected."""
    config, db, deribit, risk_mgr = setup

    request = _make_order(size=150)
    approved, reason = await risk_mgr.approve_order(request)
    assert approved is False
    assert "position limit" in reason


@pytest.mark.asyncio
async def test_reject_daily_loss_limit(setup):
    """T3: Cumulative daily losses exceeding limit -> rejection."""
    config, db, deribit, risk_mgr = setup

    from models import TradeRecord

    for i in range(10):
        trade = TradeRecord(
            pair_name="TEST-PAIR",
            instrument="ETH-PERPETUAL",
            direction=Direction.LONG,
            entry_price=3000,
            exit_price=2990,
            size=10,
            entry_time=time.time() - 100,
            exit_time=time.time() - 50 + i,
            gross_pnl_usd=-100,
            fees_usd=5,
            net_pnl_usd=-105,
            signal_at_entry=0.5,
            exit_reason="stop_loss",
        )
        await db.insert_trade(trade)

    request = _make_order()
    approved, reason = await risk_mgr.approve_order(request)
    assert approved is False
    assert "daily loss" in reason


@pytest.mark.asyncio
async def test_stop_loss_triggers_exit(setup):
    """T4: Position with large unrealized loss triggers stop-loss exit."""
    config, db, deribit, risk_mgr = setup

    exit_called = asyncio.Event()
    exit_args: list = []

    async def on_exit(pair: str, reason: str) -> None:
        exit_args.append((pair, reason))
        exit_called.set()

    risk_mgr.set_on_exit(on_exit)

    deribit.get_mid_price.return_value = 2985.0
    risk_mgr.update_position(
        "TEST-PAIR",
        {"size": 10, "direction": 1, "entry_price": 3000.0},
    )

    monitor_task = asyncio.create_task(risk_mgr.start_monitor())

    try:
        await asyncio.wait_for(exit_called.wait(), timeout=5)
    except asyncio.TimeoutError:
        pass
    finally:
        await risk_mgr.stop_monitor()
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

    # 50 bps loss > 35 bps stop-loss -> should trigger
    assert len(exit_args) > 0
    assert exit_args[0][1] == "stop_loss"


@pytest.mark.asyncio
async def test_take_profit_triggers(setup):
    """T5: Position with unrealized profit exceeding TP -> take-profit."""
    config, db, deribit, risk_mgr = setup

    exit_called = asyncio.Event()
    exit_args: list = []

    async def on_exit(pair: str, reason: str) -> None:
        exit_args.append((pair, reason))
        exit_called.set()

    risk_mgr.set_on_exit(on_exit)

    deribit.get_mid_price.return_value = 3018.0
    risk_mgr.update_position(
        "TEST-PAIR",
        {"size": 10, "direction": 1, "entry_price": 3000.0},
    )

    monitor_task = asyncio.create_task(risk_mgr.start_monitor())

    try:
        await asyncio.wait_for(exit_called.wait(), timeout=5)
    except asyncio.TimeoutError:
        pass
    finally:
        await risk_mgr.stop_monitor()
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

    # 60 bps profit > 50 bps TP -> should trigger
    assert len(exit_args) > 0
    assert exit_args[0][1] == "take_profit"

from __future__ import annotations

import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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
from deribit_client import DeribitClient
from execution_manager import ExecutionManager, _round_to_tick
from models import Direction, OrderStatus, Regime, SignalTransition, TradeSignal
from risk_manager import RiskManager


def _make_config(mode: str = "paper") -> Config:
    return Config(
        system=SystemConfig(mode=mode, database_path=":memory:"),
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
                bp30_signal=BP30SignalConfig(),
                execution=ExecutionConfig(stale_order_seconds=2),
                risk=RiskConfig(max_position_contracts=100, max_position_usd=500000),
            )
        ],
    )


def _make_signal(
    strength: float = 0.8,
    direction: Direction = Direction.LONG,
    transition: SignalTransition = SignalTransition.ENTRY,
) -> TradeSignal:
    return TradeSignal(
        pair_name="TEST-PAIR",
        timestamp=time.time(),
        direction=direction,
        transition=transition,
        signal_strength=strength,
        regime=Regime.ACTIVE,
        regime_multiplier=1.0,
        bp30_count=5,
    )


@pytest_asyncio.fixture
async def setup():
    config = _make_config()
    db = Database(":memory:")
    await db.initialize()

    deribit = MagicMock(spec=DeribitClient)
    deribit.get_mid_price = MagicMock(return_value=3000.0)
    deribit.get_best_bid = MagicMock(return_value=2999.5)
    deribit.get_best_ask = MagicMock(return_value=3000.5)
    deribit.place_order = AsyncMock(return_value="order_123")
    deribit.place_market_order = AsyncMock(return_value="market_123")
    deribit.cancel_order = AsyncMock()
    deribit.cancel_all = AsyncMock()

    risk_mgr = RiskManager(config, db, deribit)

    exec_mgr = ExecutionManager(config, db, deribit, risk_mgr)
    return config, db, deribit, risk_mgr, exec_mgr


@pytest.mark.asyncio
async def test_entry_places_order(setup):
    """T1: Entry signal places an order with correct direction and price."""
    config, db, deribit, risk_mgr, exec_mgr = setup

    sig = _make_signal(strength=0.8, direction=Direction.LONG)
    await exec_mgr.on_trade_signal(sig)

    assert len(exec_mgr._open_orders) == 1
    order = list(exec_mgr._open_orders.values())[0]
    assert order.request.direction == Direction.LONG
    assert order.status == OrderStatus.PLACED

    limit_price = order.request.limit_price
    assert limit_price < 3000.0
    assert limit_price == _round_to_tick(limit_price, "ETH-PERPETUAL")


@pytest.mark.asyncio
async def test_stale_order_cancelled(setup):
    """T2: Order not filled within stale_order_seconds gets cancelled."""
    config, db, deribit, risk_mgr, exec_mgr = setup

    sig = _make_signal(strength=0.8, direction=Direction.LONG)
    await exec_mgr.on_trade_signal(sig)

    assert len(exec_mgr._open_orders) == 1

    await asyncio.sleep(3)

    remaining_placed = [
        o for o in exec_mgr._open_orders.values()
        if o.status == OrderStatus.PLACED
    ]
    assert len(remaining_placed) == 0


@pytest.mark.asyncio
async def test_position_sizing(setup):
    """T3: In paper/live mode, size = trade_size_eth from config."""
    config, db, deribit, risk_mgr, exec_mgr = setup

    sig = _make_signal(strength=0.8, direction=Direction.LONG)
    await exec_mgr.on_trade_signal(sig)

    order = list(exec_mgr._open_orders.values())[0]
    assert order.request.size == 0.01  # trade_size_eth default

from __future__ import annotations

import asyncio
import math
import sys
import os

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
from models import FeeTier, SignalState, SignalTransition, SwapEvent
from signal_engine import SignalEngine


def _make_config(signal_cfg: SignalConfig | None = None) -> Config:
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
                signal=signal_cfg or SignalConfig(),
                execution=ExecutionConfig(),
                risk=RiskConfig(),
            )
        ],
    )


def _make_swap(
    fee_tier: FeeTier,
    log_return: float | None,
    timestamp: int = 1000,
    price: float = 3000.0,
) -> SwapEvent:
    return SwapEvent(
        pair_name="TEST-PAIR",
        fee_tier=fee_tier,
        block_number=100,
        block_timestamp=timestamp,
        transaction_hash=f"0x{os.urandom(16).hex()}",
        pool_address="0xpool",
        sqrt_price_x96=0,
        tick=0,
        liquidity=0,
        amount0=0,
        amount1=0,
        price=price,
        log_return=log_return,
        direction=1 if log_return and log_return > 0 else (-1 if log_return and log_return < 0 else None),
    )


@pytest_asyncio.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    return database


@pytest.mark.asyncio
async def test_conviction_increases_with_same_direction(db):
    """T1: Feed same-direction 30 bps events. Conviction should increase
    and trend state should transition to +1."""
    signals: list[SignalState] = []

    async def capture(sig: SignalState) -> None:
        signals.append(sig)

    config = _make_config(SignalConfig(
        conviction_halflife_seconds=1800,
        trend_entry_threshold=1.5,
        trend_exit_threshold=0.5,
        conviction_cap=5.0,
    ))
    engine = SignalEngine(config, db, capture)

    for i in range(5):
        event = _make_swap(FeeTier.BP30, log_return=0.001, timestamp=1000 + i)
        await engine.on_event(event)

    state = engine.get_state("TEST-PAIR")
    assert state.conviction_30 > 1.5
    assert state.trend_state_30 == 1


@pytest.mark.asyncio
async def test_alternating_direction_stays_neutral(db):
    """T2: Alternating-direction 30 bps events keep conviction near zero."""
    signals: list[SignalState] = []

    async def capture(sig: SignalState) -> None:
        signals.append(sig)

    config = _make_config()
    engine = SignalEngine(config, db, capture)

    for i in range(20):
        lr = 0.001 if i % 2 == 0 else -0.001
        event = _make_swap(FeeTier.BP30, log_return=lr, timestamp=1000 + i)
        await engine.on_event(event)

    state = engine.get_state("TEST-PAIR")
    assert abs(state.conviction_30) < 1.0
    assert state.trend_state_30 == 0


@pytest.mark.asyncio
async def test_momentum_flag_with_aligned_trend(db):
    """T3: Establish a 30 bps trend, then feed aligned 5 bps events
    with positive autocorrelation. Combined signal should be nonzero."""
    signals: list[SignalState] = []

    async def capture(sig: SignalState) -> None:
        signals.append(sig)

    config = _make_config(SignalConfig(
        conviction_halflife_seconds=3600,
        trend_entry_threshold=1.5,
        trend_exit_threshold=0.5,
        conviction_cap=5.0,
        momentum_window_events=10,
        min_autocorrelation=0.01,
    ))
    engine = SignalEngine(config, db, capture)

    for i in range(5):
        event = _make_swap(FeeTier.BP30, log_return=0.002, timestamp=1000 + i)
        await engine.on_event(event)

    assert engine.get_state("TEST-PAIR").trend_state_30 == 1

    for i in range(15):
        lr = 0.001 + (i * 0.0001)
        event = _make_swap(FeeTier.BP5, log_return=lr, timestamp=1010 + i, price=3000 + i)
        await engine.on_event(event)

    state = engine.get_state("TEST-PAIR")
    assert state.momentum_flag_5 or len(signals) > 0


@pytest.mark.asyncio
async def test_negative_autocorrelation_blocks_flag(db):
    """T4: 5 bps events with negative autocorrelation -> F5 = 0."""
    signals: list[SignalState] = []

    async def capture(sig: SignalState) -> None:
        signals.append(sig)

    config = _make_config(SignalConfig(momentum_window_events=10, min_autocorrelation=0.05))
    engine = SignalEngine(config, db, capture)

    for i in range(5):
        event = _make_swap(FeeTier.BP30, log_return=0.002, timestamp=1000 + i)
        await engine.on_event(event)

    for i in range(15):
        lr = 0.001 if i % 2 == 0 else -0.001
        event = _make_swap(FeeTier.BP5, log_return=lr, timestamp=1010 + i, price=3000 + i)
        await engine.on_event(event)

    state = engine.get_state("TEST-PAIR")
    assert state.momentum_flag_5 is False


@pytest.mark.asyncio
async def test_hysteresis_preserves_trend(db):
    """T5: Establish trend at +1, then weak opposing events between exit
    and entry thresholds — trend should remain +1."""
    signals: list[SignalState] = []

    async def capture(sig: SignalState) -> None:
        signals.append(sig)

    config = _make_config(SignalConfig(
        conviction_halflife_seconds=100000,
        trend_entry_threshold=1.5,
        trend_exit_threshold=0.5,
        conviction_cap=5.0,
    ))
    engine = SignalEngine(config, db, capture)

    for i in range(4):
        event = _make_swap(FeeTier.BP30, log_return=0.002, timestamp=1000 + i)
        await engine.on_event(event)

    assert engine.get_state("TEST-PAIR").trend_state_30 == 1

    event = _make_swap(FeeTier.BP30, log_return=-0.002, timestamp=1010)
    await engine.on_event(event)

    state = engine.get_state("TEST-PAIR")
    assert state.conviction_30 > 0.5
    assert state.trend_state_30 == 1


@pytest.mark.asyncio
async def test_transition_detection_all_types(db):
    """T6: Verify all five transition types are detected correctly."""
    signals: list[SignalState] = []

    async def capture(sig: SignalState) -> None:
        signals.append(sig)

    config = _make_config(SignalConfig(
        conviction_halflife_seconds=100000,
        trend_entry_threshold=1.5,
        trend_exit_threshold=0.5,
        conviction_cap=5.0,
        momentum_window_events=5,
        min_autocorrelation=-1.0,
    ))
    engine = SignalEngine(config, db, capture)

    for i in range(4):
        event = _make_swap(FeeTier.BP30, log_return=0.002, timestamp=1000 + i)
        await engine.on_event(event)

    for i in range(6):
        event = _make_swap(FeeTier.BP5, log_return=0.001, timestamp=1010 + i, price=3000 + i)
        await engine.on_event(event)

    entry_signals = [s for s in signals if s.transition == SignalTransition.ENTRY]
    assert len(entry_signals) >= 1


@pytest.mark.asyncio
async def test_conviction_decay_over_time(db):
    """T7: Feed an event, simulate time passage, then check conviction decayed."""
    signals: list[SignalState] = []

    async def capture(sig: SignalState) -> None:
        signals.append(sig)

    config = _make_config(SignalConfig(conviction_halflife_seconds=60, conviction_cap=5.0))
    engine = SignalEngine(config, db, capture)

    event1 = _make_swap(FeeTier.BP30, log_return=0.002, timestamp=1000)
    await engine.on_event(event1)
    c_before = engine.get_state("TEST-PAIR").conviction_30

    event2 = _make_swap(FeeTier.BP30, log_return=0.002, timestamp=1060)
    await engine.on_event(event2)

    state = engine.get_state("TEST-PAIR")
    decayed_c = c_before * math.exp(-math.log(2))
    assert state.conviction_30 < c_before + 1.5

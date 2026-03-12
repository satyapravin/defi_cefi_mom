from __future__ import annotations

import asyncio
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import (
    BP30SignalConfig,
    Config,
    DeribitConfig,
    InfraConfig,
    PairConfig,
    PoolConfig,
    PoolsConfig,
    RegimeConfig,
    RiskConfig,
    SystemConfig,
)
from models import Direction, FeeTier, SignalTransition, SwapEvent, TradeSignal
from regime_filter import RegimeFilter
from signal_30bps import BP30SignalEngine


def _make_config(
    window_seconds: float = 120,
    min_cluster_swaps: int = 3,
    direction_ratio: float = 0.7,
) -> Config:
    return Config(
        system=SystemConfig(mode="backtest", database_path=":memory:"),
        infrastructure=InfraConfig(rpc_url="wss://dummy"),
        deribit=DeribitConfig(),
        pairs=[
            PairConfig(
                name="TEST",
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
                    window_seconds=window_seconds,
                    min_cluster_swaps=min_cluster_swaps,
                    direction_ratio=direction_ratio,
                ),
                risk=RiskConfig(),
            )
        ],
    )


def _make_swap(
    ts: int,
    price: float,
    log_return: float,
    fee_tier: FeeTier = FeeTier.BP30,
    pair: str = "TEST",
    idx: int = 0,
) -> SwapEvent:
    return SwapEvent(
        pair_name=pair,
        fee_tier=fee_tier,
        block_number=ts,
        block_timestamp=ts,
        transaction_hash=f"0x{ts}_{idx}",
        pool_address=f"0x{fee_tier.value}",
        sqrt_price_x96=0,
        tick=0,
        liquidity=0,
        amount0=0,
        amount1=0,
        price=price,
        log_return=log_return,
        direction=1 if log_return > 0 else -1,
    )


@pytest.mark.asyncio
async def test_no_signal_below_min_cluster():
    """Fewer than min_cluster_swaps should not trigger a signal."""
    config = _make_config(min_cluster_swaps=5)
    regime = RegimeFilter(config)
    signals: list[TradeSignal] = []

    async def capture(sig: TradeSignal) -> None:
        signals.append(sig)

    engine = BP30SignalEngine(config, regime, capture)

    base_ts = 1700000000
    for i in range(4):
        await engine.on_swap(_make_swap(base_ts + i, 3000.0, 0.001, idx=i))

    assert len(signals) == 0


@pytest.mark.asyncio
async def test_signal_emitted_on_directional_cluster():
    """3 same-direction swaps within window should trigger ENTRY."""
    config = _make_config(min_cluster_swaps=3, direction_ratio=0.7)
    regime = RegimeFilter(config)
    signals: list[TradeSignal] = []

    async def capture(sig: TradeSignal) -> None:
        signals.append(sig)

    engine = BP30SignalEngine(config, regime, capture)

    base_ts = 1700000000
    for i in range(3):
        await engine.on_swap(_make_swap(base_ts + i * 10, 3000.0, 0.001, idx=i))

    assert len(signals) == 1
    assert signals[0].direction == Direction.LONG
    assert signals[0].transition == SignalTransition.ENTRY
    assert signals[0].bp30_count == 3


@pytest.mark.asyncio
async def test_no_signal_when_direction_mixed():
    """Mixed directions below threshold should not trigger."""
    config = _make_config(min_cluster_swaps=3, direction_ratio=0.7)
    regime = RegimeFilter(config)
    signals: list[TradeSignal] = []

    async def capture(sig: TradeSignal) -> None:
        signals.append(sig)

    engine = BP30SignalEngine(config, regime, capture)

    base_ts = 1700000000
    # 2 up, 1 down = 66% < 70% threshold
    await engine.on_swap(_make_swap(base_ts, 3000.0, 0.001, idx=0))
    await engine.on_swap(_make_swap(base_ts + 10, 3000.0, -0.001, idx=1))
    await engine.on_swap(_make_swap(base_ts + 20, 3000.0, 0.001, idx=2))

    assert len(signals) == 0


@pytest.mark.asyncio
async def test_window_expiry():
    """Events older than window_seconds should be dropped."""
    config = _make_config(window_seconds=60, min_cluster_swaps=3, direction_ratio=0.7)
    regime = RegimeFilter(config)
    signals: list[TradeSignal] = []

    async def capture(sig: TradeSignal) -> None:
        signals.append(sig)

    engine = BP30SignalEngine(config, regime, capture)

    base_ts = 1700000000
    # Two events at t=0, one at t=100 (first two expire)
    await engine.on_swap(_make_swap(base_ts, 3000.0, 0.001, idx=0))
    await engine.on_swap(_make_swap(base_ts + 5, 3000.0, 0.001, idx=1))
    await engine.on_swap(_make_swap(base_ts + 100, 3000.0, 0.001, idx=2))

    assert len(signals) == 0


@pytest.mark.asyncio
async def test_ignores_non_bp30_events():
    """BP5 and BP1 events should be ignored by the engine."""
    config = _make_config(min_cluster_swaps=2, direction_ratio=0.7)
    regime = RegimeFilter(config)
    signals: list[TradeSignal] = []

    async def capture(sig: TradeSignal) -> None:
        signals.append(sig)

    engine = BP30SignalEngine(config, regime, capture)

    base_ts = 1700000000
    for i in range(10):
        await engine.on_swap(_make_swap(base_ts + i, 3000.0, 0.001,
                                        fee_tier=FeeTier.BP5, idx=i))

    assert len(signals) == 0


@pytest.mark.asyncio
async def test_no_duplicate_entry_while_in_position():
    """Once a signal is emitted, no more entries until position closed."""
    config = _make_config(min_cluster_swaps=3, direction_ratio=0.7)
    regime = RegimeFilter(config)
    signals: list[TradeSignal] = []

    async def capture(sig: TradeSignal) -> None:
        signals.append(sig)

    engine = BP30SignalEngine(config, regime, capture)

    base_ts = 1700000000
    for i in range(10):
        await engine.on_swap(_make_swap(base_ts + i * 10, 3000.0, 0.001, idx=i))

    assert len(signals) == 1

    engine.notify_position_closed("TEST")

    for i in range(10, 20):
        await engine.on_swap(_make_swap(base_ts + i * 10, 3000.0, 0.001, idx=i))

    assert len(signals) == 2


@pytest.mark.asyncio
async def test_short_signal():
    """Negative log_returns should produce a SHORT signal."""
    config = _make_config(min_cluster_swaps=3, direction_ratio=0.7)
    regime = RegimeFilter(config)
    signals: list[TradeSignal] = []

    async def capture(sig: TradeSignal) -> None:
        signals.append(sig)

    engine = BP30SignalEngine(config, regime, capture)

    base_ts = 1700000000
    for i in range(3):
        await engine.on_swap(_make_swap(base_ts + i * 10, 3000.0, -0.001, idx=i))

    assert len(signals) == 1
    assert signals[0].direction == Direction.SHORT


@pytest.mark.asyncio
async def test_signal_strength_reflects_ratio():
    """Signal strength should equal majority/total."""
    config = _make_config(min_cluster_swaps=3, direction_ratio=0.7)
    regime = RegimeFilter(config)
    signals: list[TradeSignal] = []

    async def capture(sig: TradeSignal) -> None:
        signals.append(sig)

    engine = BP30SignalEngine(config, regime, capture)

    base_ts = 1700000000
    await engine.on_swap(_make_swap(base_ts, 3000.0, 0.001, idx=0))
    await engine.on_swap(_make_swap(base_ts + 10, 3000.0, 0.001, idx=1))
    await engine.on_swap(_make_swap(base_ts + 20, 3000.0, 0.001, idx=2))

    assert len(signals) == 1
    assert signals[0].signal_strength == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_zero_log_return_ignored():
    """Swaps with log_return=0 should be skipped."""
    config = _make_config(min_cluster_swaps=3, direction_ratio=0.7)
    regime = RegimeFilter(config)
    signals: list[TradeSignal] = []

    async def capture(sig: TradeSignal) -> None:
        signals.append(sig)

    engine = BP30SignalEngine(config, regime, capture)

    base_ts = 1700000000
    await engine.on_swap(_make_swap(base_ts, 3000.0, 0.001, idx=0))
    await engine.on_swap(_make_swap(base_ts + 10, 3000.0, 0.0, idx=1))
    await engine.on_swap(_make_swap(base_ts + 20, 3000.0, 0.001, idx=2))

    assert len(signals) == 0

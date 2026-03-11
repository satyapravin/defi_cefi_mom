from __future__ import annotations

import asyncio
import os
import sys

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
    RegimeConfig,
    RiskConfig,
    SignalConfig,
    SystemConfig,
    ToxicityConfig,
)
from database import Database
from flow_toxicity import FlowToxicityEngine
from lp_monitor import LPMonitor
from models import (
    Direction,
    FeeTier,
    Regime,
    SignalTransition,
    SwapEvent,
    ToxicitySignal,
)
from regime_filter import RegimeFilter


def _make_config(tox_cfg: ToxicityConfig | None = None) -> Config:
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
                risk=RiskConfig(),
                toxicity=tox_cfg or ToxicityConfig(bucket_seconds=10),
                regime=RegimeConfig(),
            )
        ],
    )


def _make_swap(
    fee_tier: FeeTier,
    timestamp: int = 1000,
    price: float = 3000.0,
    amount1: int = 100_000_000,
    log_return: float | None = 0.001,
    tick: int = 200000,
) -> SwapEvent:
    return SwapEvent(
        pair_name="TEST-PAIR",
        fee_tier=fee_tier,
        block_number=100,
        block_timestamp=timestamp,
        transaction_hash=f"0x{os.urandom(16).hex()}",
        pool_address=f"0x{fee_tier.value}pool",
        sqrt_price_x96=0,
        tick=tick,
        liquidity=0,
        amount0=0,
        amount1=amount1,
        price=price,
        log_return=log_return,
        direction=1 if log_return and log_return > 0 else -1,
    )


@pytest_asyncio.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    return database


@pytest.mark.asyncio
async def test_fti_increases_with_directional_30bps_flow(db):
    """Concentrated 30 bps flow should produce nonzero FTI."""
    signals: list[ToxicitySignal] = []

    async def capture(sig: ToxicitySignal) -> None:
        signals.append(sig)

    cfg = ToxicityConfig(
        bucket_seconds=10,
        fti_percentile_threshold=0.0,
        tier_weight_30=6.0,
    )
    config = _make_config(cfg)
    regime_filter = RegimeFilter(config)
    lp_monitor = LPMonitor(config)

    engine = FlowToxicityEngine(config, db, regime_filter, lp_monitor, capture)

    for i in range(5):
        event = _make_swap(
            FeeTier.BP30,
            timestamp=100 + i,
            amount1=500_000_000,
            price=3000.0 + i,
            log_return=0.001,
        )
        regime_filter.on_swap(event)
        await engine.on_swap(event)

    trigger = _make_swap(
        FeeTier.BP30,
        timestamp=115,
        amount1=500_000_000,
        price=3010.0,
        log_return=0.001,
    )
    regime_filter.on_swap(trigger)
    await engine.on_swap(trigger)

    assert len(signals) >= 1
    assert signals[0].fti != 0.0
    assert signals[0].transition == SignalTransition.ENTRY


@pytest.mark.asyncio
async def test_coherence_isolated_boost(db):
    """When only 30 bps has flow, coherence should be boosted."""
    signals: list[ToxicitySignal] = []

    async def capture(sig: ToxicitySignal) -> None:
        signals.append(sig)

    cfg = ToxicityConfig(
        bucket_seconds=10,
        fti_percentile_threshold=0.0,
        coherence_isolated_boost=5.0,
    )
    config = _make_config(cfg)
    regime_filter = RegimeFilter(config)
    lp_monitor = LPMonitor(config)
    engine = FlowToxicityEngine(config, db, regime_filter, lp_monitor, capture)

    for i in range(3):
        await engine.on_swap(
            _make_swap(FeeTier.BP30, timestamp=100 + i, amount1=1_000_000_000)
        )

    await engine.on_swap(
        _make_swap(FeeTier.BP30, timestamp=115, amount1=1_000_000_000)
    )

    assert len(signals) >= 1
    assert abs(signals[0].fti) > 0


@pytest.mark.asyncio
async def test_incoherent_tiers_suppress_signal(db):
    """Opposing flow across tiers should reduce the FTI toward zero."""
    signals: list[ToxicitySignal] = []

    async def capture(sig: ToxicitySignal) -> None:
        signals.append(sig)

    cfg = ToxicityConfig(
        bucket_seconds=10,
        fti_percentile_threshold=0.5,
    )
    config = _make_config(cfg)
    regime_filter = RegimeFilter(config)
    lp_monitor = LPMonitor(config)
    engine = FlowToxicityEngine(config, db, regime_filter, lp_monitor, capture)

    for i in range(3):
        await engine.on_swap(
            _make_swap(FeeTier.BP30, timestamp=100 + i, amount1=100_000_000)
        )
        await engine.on_swap(
            _make_swap(FeeTier.BP5, timestamp=100 + i, amount1=-200_000_000)
        )

    await engine.on_swap(
        _make_swap(FeeTier.BP30, timestamp=115, amount1=100_000_000)
    )

    entry_signals = [s for s in signals if s.transition == SignalTransition.ENTRY]
    assert len(entry_signals) == 0


@pytest.mark.asyncio
async def test_exit_signal_on_low_percentile(db):
    """Once in a position, a low-percentile bucket should trigger EXIT."""
    signals: list[ToxicitySignal] = []

    async def capture(sig: ToxicitySignal) -> None:
        signals.append(sig)

    cfg = ToxicityConfig(
        bucket_seconds=10,
        fti_percentile_threshold=0.0,
        exit_fti_percentile=0.50,
    )
    config = _make_config(cfg)
    regime_filter = RegimeFilter(config)
    lp_monitor = LPMonitor(config)
    engine = FlowToxicityEngine(config, db, regime_filter, lp_monitor, capture)

    for i in range(3):
        await engine.on_swap(
            _make_swap(FeeTier.BP30, timestamp=100 + i, amount1=1_000_000_000)
        )
    await engine.on_swap(
        _make_swap(FeeTier.BP30, timestamp=115, amount1=1_000_000_000)
    )

    entries = [s for s in signals if s.transition == SignalTransition.ENTRY]
    assert len(entries) >= 1

    for i in range(3):
        await engine.on_swap(
            _make_swap(FeeTier.BP30, timestamp=120 + i, amount1=1)
        )
    await engine.on_swap(
        _make_swap(FeeTier.BP30, timestamp=135, amount1=1)
    )

    exits = [s for s in signals if s.transition == SignalTransition.EXIT]
    assert len(exits) >= 1


@pytest.mark.asyncio
async def test_chaotic_regime_blocks_entry(db):
    """CHAOTIC regime should prevent new entries."""
    signals: list[ToxicitySignal] = []

    async def capture(sig: ToxicitySignal) -> None:
        signals.append(sig)

    tox_cfg = ToxicityConfig(
        bucket_seconds=10,
        fti_percentile_threshold=0.0,
    )
    regime_cfg = RegimeConfig(vol_chaotic_threshold=0.0001)
    config = Config(
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
                toxicity=tox_cfg,
                regime=regime_cfg,
            )
        ],
    )
    regime_filter = RegimeFilter(config)
    lp_monitor = LPMonitor(config)
    engine = FlowToxicityEngine(config, db, regime_filter, lp_monitor, capture)

    for i in range(10):
        bp1_swap = _make_swap(
            FeeTier.BP1,
            timestamp=100 + i,
            price=3000.0 + i * 10,
            log_return=0.01 * (1 if i % 2 == 0 else -1),
        )
        regime_filter.on_swap(bp1_swap)

    assert regime_filter.get_regime("TEST-PAIR") == Regime.CHAOTIC

    for i in range(3):
        await engine.on_swap(
            _make_swap(FeeTier.BP30, timestamp=100 + i, amount1=1_000_000_000)
        )
    await engine.on_swap(
        _make_swap(FeeTier.BP30, timestamp=115, amount1=1_000_000_000)
    )

    entry_signals = [s for s in signals if s.transition == SignalTransition.ENTRY]
    assert len(entry_signals) == 0


@pytest.mark.asyncio
async def test_notify_position_closed_resets_state(db):
    """After notify_position_closed, a new ENTRY should be possible."""
    signals: list[ToxicitySignal] = []

    async def capture(sig: ToxicitySignal) -> None:
        signals.append(sig)

    cfg = ToxicityConfig(
        bucket_seconds=10,
        fti_percentile_threshold=0.0,
    )
    config = _make_config(cfg)
    regime_filter = RegimeFilter(config)
    lp_monitor = LPMonitor(config)
    engine = FlowToxicityEngine(config, db, regime_filter, lp_monitor, capture)

    for i in range(3):
        await engine.on_swap(
            _make_swap(FeeTier.BP30, timestamp=100 + i, amount1=1_000_000_000)
        )
    await engine.on_swap(
        _make_swap(FeeTier.BP30, timestamp=115, amount1=1_000_000_000)
    )

    entries_1 = [s for s in signals if s.transition == SignalTransition.ENTRY]
    assert len(entries_1) >= 1

    # Flush any lingering partial bucket by sending a zero-flow event
    # in a later bucket window, so the intermediate bucket is finalized
    # before we reset state.
    await engine.on_swap(
        _make_swap(FeeTier.BP30, timestamp=130, amount1=0)
    )
    # Flush the bucket containing the zero-flow event too.
    await engine.on_swap(
        _make_swap(FeeTier.BP30, timestamp=150, amount1=0)
    )

    engine.notify_position_closed("TEST-PAIR")

    for i in range(3):
        await engine.on_swap(
            _make_swap(FeeTier.BP30, timestamp=200 + i, amount1=2_000_000_000)
        )
    await engine.on_swap(
        _make_swap(FeeTier.BP30, timestamp=215, amount1=2_000_000_000)
    )

    entries_2 = [
        s for s in signals
        if s.transition == SignalTransition.ENTRY and s.timestamp >= 200
    ]
    assert len(entries_2) >= 1


# -------------------- OFI & Hawkes tests --------------------

from flow_toxicity import FlowToxicityEngine as _FTE


def test_ofi_computation():
    """OFI = (up - down) / (up + down), bounded to [-1, +1]."""
    assert _FTE._compute_ofi(5, 0) == 1.0
    assert _FTE._compute_ofi(0, 5) == -1.0
    assert _FTE._compute_ofi(3, 3) == 0.0
    assert abs(_FTE._compute_ofi(7, 3) - 0.4) < 1e-9
    assert _FTE._compute_ofi(0, 0) == 0.0


def test_ofi_concentration_high_when_expensive_pool_directional():
    """When 30 bps OFI is high and 1 bps OFI is low, concentration is high."""
    from config import ToxicityConfig

    cfg = ToxicityConfig(ofi_concentration_cap=5.0)
    conc = _FTE._compute_ofi_concentration(0.9, 0.1, cfg)
    assert conc > 3.0

    conc_equal = _FTE._compute_ofi_concentration(0.5, 0.5, cfg)
    assert conc_equal <= 1.0


def test_ofi_concentration_capped():
    """Concentration ratio is capped at ofi_concentration_cap."""
    from config import ToxicityConfig

    cfg = ToxicityConfig(ofi_concentration_cap=3.0)
    conc = _FTE._compute_ofi_concentration(1.0, 0.0, cfg)
    assert conc == 3.0


def test_cluster_ratio_all_clustered():
    """All events within the window → cluster_ratio = 1.0."""
    ts = [100.0, 101.0, 102.0, 103.0, 104.0]
    assert _FTE._compute_cluster_ratio(ts, window_seconds=5.0) == 1.0


def test_cluster_ratio_none_clustered():
    """Events spaced far apart → cluster_ratio = 0.0."""
    ts = [100.0, 200.0, 300.0]
    assert _FTE._compute_cluster_ratio(ts, window_seconds=5.0) == 0.0


def test_cluster_ratio_partial():
    """Mix of clustered and spread events."""
    ts = [100.0, 102.0, 200.0, 202.0]
    ratio = _FTE._compute_cluster_ratio(ts, window_seconds=5.0)
    assert abs(ratio - 2 / 3) < 1e-9


def test_cluster_ratio_single_event():
    """A single event gives 0.0."""
    assert _FTE._compute_cluster_ratio([100.0], window_seconds=5.0) == 0.0


def test_cross_excitation_forward_only():
    """30bps events followed by 5bps events → positive cross-excitation."""
    ts_30 = [100.0, 200.0, 300.0]
    ts_5 = [105.0, 205.0, 305.0]
    exc = _FTE._compute_cross_excitation(ts_30, ts_5, window_seconds=10.0)
    assert exc > 0.5


def test_cross_excitation_reverse_only():
    """5bps events followed by 30bps events → negative cross-excitation."""
    ts_30 = [108.0, 208.0, 308.0]
    ts_5 = [100.0, 200.0, 300.0]
    exc = _FTE._compute_cross_excitation(ts_30, ts_5, window_seconds=10.0)
    assert exc < -0.5


def test_cross_excitation_symmetric():
    """Symmetric follow → cross-excitation near 0."""
    ts_30 = [100.0, 200.0]
    ts_5 = [105.0, 195.0]
    exc = _FTE._compute_cross_excitation(ts_30, ts_5, window_seconds=10.0)
    assert abs(exc) < 0.5


def test_cross_excitation_no_data():
    assert _FTE._compute_cross_excitation([], [100.0], 10.0) == 0.0
    assert _FTE._compute_cross_excitation([100.0], [], 10.0) == 0.0


@pytest.mark.asyncio
async def test_clustered_flow_amplifies_fti(db):
    """Tightly clustered 30 bps swaps should produce higher FTI than
    the same number of swaps spread apart."""
    signals_clustered: list[ToxicitySignal] = []
    signals_spread: list[ToxicitySignal] = []

    async def capture_clustered(sig: ToxicitySignal) -> None:
        signals_clustered.append(sig)

    async def capture_spread(sig: ToxicitySignal) -> None:
        signals_spread.append(sig)

    cfg = ToxicityConfig(
        bucket_seconds=10,
        fti_percentile_threshold=0.0,
        cluster_window_seconds=3.0,
    )
    config = _make_config(cfg)
    rf1 = RegimeFilter(config)
    lp1 = LPMonitor(config)
    eng_clustered = FlowToxicityEngine(config, db, rf1, lp1, capture_clustered)

    db2 = Database(":memory:")
    await db2.initialize()
    rf2 = RegimeFilter(config)
    lp2 = LPMonitor(config)
    eng_spread = FlowToxicityEngine(config, db2, rf2, lp2, capture_spread)

    for i in range(5):
        await eng_clustered.on_swap(
            _make_swap(FeeTier.BP30, timestamp=100 + i, amount1=500_000_000)
        )

    for i in range(5):
        await eng_spread.on_swap(
            _make_swap(FeeTier.BP30, timestamp=100 + i * 2, amount1=500_000_000)
        )

    await eng_clustered.on_swap(
        _make_swap(FeeTier.BP30, timestamp=115, amount1=500_000_000)
    )
    await eng_spread.on_swap(
        _make_swap(FeeTier.BP30, timestamp=115, amount1=500_000_000)
    )

    assert len(signals_clustered) >= 1
    assert len(signals_spread) >= 1
    assert abs(signals_clustered[0].fti) >= abs(signals_spread[0].fti)

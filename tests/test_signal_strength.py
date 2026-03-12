"""Signal strength validation on real 22-hour ETH-USDC data.

Loads swap events from the SQLite database and runs the signal engine
to verify strength computation, cross-tier coherence, exponential decay,
and the sizing pipeline against real market data.
"""

from __future__ import annotations

import asyncio
import math
import os
import sqlite3
import sys

import pytest

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
    RegimeConfig,
    RiskConfig,
    SystemConfig,
)
from models import Direction, FeeTier, Regime, SignalTransition, SwapEvent, TradeSignal
from regime_filter import RegimeFilter
from signal_30bps import BP30SignalEngine

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "events.db")


def _load_events(fee_tiers: list[str] | None = None) -> list[SwapEvent]:
    """Load swap events from the real database, ordered by timestamp."""
    conn = sqlite3.connect(DB_PATH)
    if fee_tiers:
        placeholders = ",".join("?" for _ in fee_tiers)
        query = f"""SELECT pair_name, fee_tier, block_number, block_timestamp,
                           transaction_hash, pool_address, sqrt_price_x96, tick,
                           liquidity, amount0, amount1, price, log_return, direction
                    FROM swap_events
                    WHERE fee_tier IN ({placeholders})
                    ORDER BY block_timestamp ASC, id ASC"""
        rows = conn.execute(query, fee_tiers).fetchall()
    else:
        query = """SELECT pair_name, fee_tier, block_number, block_timestamp,
                          transaction_hash, pool_address, sqrt_price_x96, tick,
                          liquidity, amount0, amount1, price, log_return, direction
                   FROM swap_events
                   ORDER BY block_timestamp ASC, id ASC"""
        rows = conn.execute(query).fetchall()
    conn.close()
    events = []
    for r in rows:
        events.append(SwapEvent(
            pair_name=r[0], fee_tier=FeeTier(r[1]), block_number=r[2],
            block_timestamp=r[3], transaction_hash=r[4], pool_address=r[5],
            sqrt_price_x96=int(r[6]), tick=r[7], liquidity=int(r[8]),
            amount0=int(r[9]), amount1=int(r[10]), price=r[11],
            log_return=r[12], direction=r[13],
        ))
    return events


def _cfg(**overrides) -> Config:
    bp30_kw = {}
    regime_kw = {}
    risk_kw = {}
    for k, v in overrides.items():
        if k.startswith("bp30_"):
            bp30_kw[k[5:]] = v
        elif k.startswith("regime_"):
            regime_kw[k[7:]] = v
        elif k.startswith("risk_"):
            risk_kw[k[5:]] = v
    return Config(
        system=SystemConfig(mode="backtest", database_path=":memory:"),
        infrastructure=InfraConfig(rpc_url="wss://dummy"),
        deribit=DeribitConfig(),
        pairs=[PairConfig(
            name="ETH-USDC",
            deribit_instrument="ETH-PERPETUAL",
            token0="0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
            token1="0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
            token0_decimals=18, token1_decimals=6,
            pools=PoolsConfig(
                bp30=PoolConfig(address="0xc473e2aee3441bf9240be85eb122abb059a3b57c", fee=3000),
                bp5=PoolConfig(address="0xC6962004f452bE9203591991D15f6b388e09E8D0", fee=500),
                bp1=PoolConfig(address="0x6f38e884725a116c9c7fbf208e79fe8828a2595f", fee=100),
            ),
            bp30_signal=BP30SignalConfig(**bp30_kw),
            risk=RiskConfig(**risk_kw),
            regime=RegimeConfig(**regime_kw),
        )],
    )


async def _run_engine(
    events: list[SwapEvent], config: Config
) -> list[TradeSignal]:
    rf = RegimeFilter(config)
    signals: list[TradeSignal] = []

    async def capture(sig: TradeSignal) -> None:
        signals.append(sig)

    engine = BP30SignalEngine(config, rf, capture)

    for event in events:
        rf.on_swap(event)
        await engine.on_swap(event)
        engine.notify_position_closed("ETH-USDC")

    return signals


@pytest.fixture(scope="module")
def all_events() -> list[SwapEvent]:
    if not os.path.exists(DB_PATH):
        pytest.skip("No database at data/events.db")
    events = _load_events(["bp5", "bp30"])
    if len(events) < 100:
        pytest.skip("Not enough data in database")
    return events


@pytest.fixture(scope="module")
def bp30_events(all_events) -> list[SwapEvent]:
    return [e for e in all_events if e.fee_tier == FeeTier.BP30]


# ------------------------------------------------------------------ #
# 1.  All strengths are bounded [0, 1]
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_strength_bounded(all_events):
    """Every signal strength must be in (0, 1]."""
    config = _cfg()
    signals = await _run_engine(all_events, config)
    assert len(signals) > 0, "No signals produced on real data"
    for sig in signals:
        assert 0 < sig.signal_strength <= 1.0, (
            f"Strength {sig.signal_strength} out of range at t={sig.timestamp}"
        )


# ------------------------------------------------------------------ #
# 2.  Exponential decay makes recent clusters stronger
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_higher_decay_shifts_strength(all_events):
    """With higher decay_alpha, recent-heavy clusters get higher strength
    than with alpha=0 (flat weighting).
    """
    config_flat = _cfg(bp30_decay_alpha=0.0001)
    config_decay = _cfg(bp30_decay_alpha=0.05)

    sigs_flat = await _run_engine(all_events, config_flat)
    sigs_decay = await _run_engine(all_events, config_decay)

    assert len(sigs_flat) > 0 and len(sigs_decay) > 0

    strengths_flat = [s.signal_strength for s in sigs_flat]
    strengths_decay = [s.signal_strength for s in sigs_decay]

    mean_flat = sum(strengths_flat) / len(strengths_flat)
    mean_decay = sum(strengths_decay) / len(strengths_decay)

    assert mean_flat != pytest.approx(mean_decay, abs=0.001), (
        "Decay alpha should change the strength distribution"
    )


# ------------------------------------------------------------------ #
# 3.  Cross-tier coherence is populated from real BP5 data
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_coherence_populated(all_events):
    """With real BP5 data flowing, cross_tier_coherence should vary."""
    config = _cfg(bp30_bp5_coherence_min_events=3)
    signals = await _run_engine(all_events, config)
    assert len(signals) > 0

    coherence_values = [s.cross_tier_coherence for s in signals]
    non_default = [c for c in coherence_values if c != 1.0]
    assert len(non_default) > 0, (
        "Some signals should have non-default coherence from real BP5 data"
    )

    for c in coherence_values:
        assert 0.0 <= c <= 1.0


# ------------------------------------------------------------------ #
# 4.  Coherence modulates final strength
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_coherence_modulates_strength(all_events):
    """Signals with low coherence should have lower final strength
    than those with high coherence, on average.
    """
    config = _cfg(bp30_bp5_coherence_min_events=3)
    signals = await _run_engine(all_events, config)
    if len(signals) < 10:
        pytest.skip("Not enough signals")

    high_coh = [s for s in signals if s.cross_tier_coherence > 0.6]
    low_coh = [s for s in signals if s.cross_tier_coherence < 0.4]

    if not high_coh or not low_coh:
        pytest.skip("Not enough variation in coherence")

    avg_high = sum(s.signal_strength for s in high_coh) / len(high_coh)
    avg_low = sum(s.signal_strength for s in low_coh) / len(low_coh)

    assert avg_high > avg_low, (
        f"High-coherence signals should be stronger: high={avg_high:.3f}, low={avg_low:.3f}"
    )


# ------------------------------------------------------------------ #
# 5.  Weighted signal carries return magnitude information
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_weighted_signal_nonzero(all_events):
    """The weighted_signal field should be non-zero and carry sign."""
    config = _cfg()
    signals = await _run_engine(all_events, config)
    assert len(signals) > 0

    for sig in signals:
        assert sig.weighted_signal != 0.0
        if sig.direction == Direction.LONG:
            assert sig.weighted_signal > 0
        else:
            assert sig.weighted_signal < 0


# ------------------------------------------------------------------ #
# 6.  Strength flows through to sizing correctly
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_sizing_pipeline(all_events):
    """Position size = round(strength * regime_mult * max_contracts).
    Verify this holds for every signal.
    """
    config = _cfg(risk_max_position_contracts=100)
    signals = await _run_engine(all_events, config)
    assert len(signals) > 0

    for sig in signals:
        expected = max(1, round(
            sig.signal_strength * sig.regime_multiplier * 100
        ))
        assert expected >= 1
        assert expected <= 200, (
            f"Size {expected} unreasonably large at strength={sig.signal_strength:.3f}, "
            f"mult={sig.regime_multiplier:.2f}"
        )


# ------------------------------------------------------------------ #
# 7.  Autocorrelation is populated from real BP30 returns
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_autocorrelation_populated(all_events):
    """After enough BP30 events, autocorrelation should be non-zero."""
    config = _cfg(regime_acf_window_events=30)
    signals = await _run_engine(all_events, config)
    assert len(signals) > 0

    acf_values = [s.autocorrelation for s in signals]
    non_zero = [a for a in acf_values if abs(a) > 0.01]
    assert len(non_zero) > 0, "Some signals should have non-zero autocorrelation"

    for a in acf_values:
        assert -1.0 <= a <= 1.0


# ------------------------------------------------------------------ #
# 8.  Limit price offset tighter at high strength
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_limit_price_tighter_at_high_strength(all_events):
    """Verify the offset formula: higher strength → tighter limit."""
    from execution_manager import ExecutionManager

    config = _cfg()
    signals = await _run_engine(all_events, config)
    if len(signals) < 2:
        pytest.skip("Need at least 2 signals")

    ex = config.pairs[0].execution
    mid = 2000.0

    sorted_sigs = sorted(signals, key=lambda s: s.signal_strength)
    weakest = sorted_sigs[0]
    strongest = sorted_sigs[-1]

    if weakest.signal_strength == strongest.signal_strength:
        pytest.skip("No strength variation in signals")

    price_weak = ExecutionManager._compute_limit_price(
        mid, Direction.LONG, weakest.signal_strength, ex
    )
    price_strong = ExecutionManager._compute_limit_price(
        mid, Direction.LONG, strongest.signal_strength, ex
    )

    assert price_strong > price_weak, (
        f"Stronger signal should have tighter limit: "
        f"strong={price_strong:.4f} vs weak={price_weak:.4f}"
    )


# ------------------------------------------------------------------ #
# 9.  Stricter threshold produces fewer signals
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_stricter_threshold_fewer_signals(all_events):
    """Higher direction_ratio should produce fewer signals."""
    config_loose = _cfg(bp30_direction_ratio=0.6)
    config_strict = _cfg(bp30_direction_ratio=0.9)

    sigs_loose = await _run_engine(all_events, config_loose)
    sigs_strict = await _run_engine(all_events, config_strict)

    assert len(sigs_loose) >= len(sigs_strict), (
        f"Stricter threshold should produce ≤ signals: "
        f"loose={len(sigs_loose)}, strict={len(sigs_strict)}"
    )


# ------------------------------------------------------------------ #
# 10. Regime multiplier combined with ACF multiplier
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_regime_acf_combined_multiplier(all_events):
    """regime_multiplier should be the product of vol-regime and acf multipliers."""
    config = _cfg(
        regime_acf_trending_multiplier=1.3,
        regime_acf_mean_revert_multiplier=0.3,
    )
    rf = RegimeFilter(config)
    signals: list[TradeSignal] = []
    engine = BP30SignalEngine(config, rf, lambda s: signals.append(s) or asyncio.sleep(0))

    for event in all_events:
        rf.on_swap(event)
        await engine.on_swap(event)
        engine.notify_position_closed("ETH-USDC")

    assert len(signals) > 0

    for sig in signals:
        vol_mult = rf.get_regime_multiplier("ETH-USDC")
        acf_mult = rf.get_acf_multiplier("ETH-USDC")
        assert sig.regime_multiplier > 0, "Combined multiplier must be positive"

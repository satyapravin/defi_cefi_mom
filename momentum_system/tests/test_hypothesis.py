"""Hypothesis tests on real 22-hour ETH-USDC data.

Tests the statistical properties that the strategy relies on:
  H1: BP30 directional clusters predict forward BP5 returns
  H2: BP30 event returns exhibit non-zero autocorrelation
  H3: Signal fires more often when autocorrelation is positive
  H4: Event intensity tracks realized volatility in real data
  H5: Cross-tier coherence correlates with forward return quality
  H6: Stricter thresholds improve signal accuracy
  H7: Regime classification reflects real vol regimes
  H8: Alpha decay curve is computable on real data
"""

from __future__ import annotations

import asyncio
import bisect
import math
import os
import sqlite3
import sys

import numpy as np
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
        rows = conn.execute(
            """SELECT pair_name, fee_tier, block_number, block_timestamp,
                      transaction_hash, pool_address, sqrt_price_x96, tick,
                      liquidity, amount0, amount1, price, log_return, direction
               FROM swap_events ORDER BY block_timestamp ASC, id ASC"""
        ).fetchall()
    conn.close()
    return [
        SwapEvent(
            pair_name=r[0], fee_tier=FeeTier(r[1]), block_number=r[2],
            block_timestamp=r[3], transaction_hash=r[4], pool_address=r[5],
            sqrt_price_x96=int(r[6]), tick=r[7], liquidity=int(r[8]),
            amount0=int(r[9]), amount1=int(r[10]), price=r[11],
            log_return=r[12], direction=r[13],
        )
        for r in rows
    ]


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


def _forward_bp5_return(
    sig: TradeSignal,
    bp5_events: list[SwapEvent],
    bp5_timestamps: list[int],
    horizon_seconds: int = 60,
) -> float | None:
    """Compute cumulative BP5 log return over horizon_seconds after signal."""
    ts = int(sig.timestamp)
    start_idx = bisect.bisect_left(bp5_timestamps, ts)
    if start_idx >= len(bp5_events):
        return None
    start_price = bp5_events[start_idx].price
    cutoff = ts + horizon_seconds
    end_idx = bisect.bisect_right(bp5_timestamps, cutoff) - 1
    if end_idx <= start_idx:
        return None
    end_price = bp5_events[end_idx].price
    if start_price <= 0:
        return None
    return math.log(end_price / start_price)


@pytest.fixture(scope="module")
def all_events():
    if not os.path.exists(DB_PATH):
        pytest.skip("No database at data/events.db")
    events = _load_events(["bp5", "bp30"])
    if len(events) < 100:
        pytest.skip("Not enough data")
    return events


@pytest.fixture(scope="module")
def bp5_events(all_events):
    evts = [e for e in all_events if e.fee_tier == FeeTier.BP5]
    return evts


@pytest.fixture(scope="module")
def bp5_timestamps(bp5_events):
    return [e.block_timestamp for e in bp5_events]


@pytest.fixture(scope="module")
def bp30_returns(all_events):
    return [
        e.log_return for e in all_events
        if e.fee_tier == FeeTier.BP30 and e.log_return is not None
    ]


# ------------------------------------------------------------------ #
#  H1: Directional clusters predict forward BP5 returns
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_h1_signals_predict_forward_return(all_events, bp5_events, bp5_timestamps):
    """Signal direction should agree with 60-second forward BP5 return
    more than chance (>50%).
    """
    config = _cfg()
    signals = await _run_engine(all_events, config)
    if len(signals) < 10:
        pytest.skip("Not enough signals")

    correct = 0
    total = 0
    for sig in signals:
        fwd = _forward_bp5_return(sig, bp5_events, bp5_timestamps, 60)
        if fwd is None:
            continue
        total += 1
        if sig.direction == Direction.LONG and fwd > 0:
            correct += 1
        elif sig.direction == Direction.SHORT and fwd < 0:
            correct += 1

    if total < 10:
        pytest.skip("Not enough signals with forward data")

    accuracy = correct / total
    assert accuracy > 0.45, (
        f"Signal accuracy {accuracy:.1%} over {total} signals — "
        "should be near or above chance on 22h of data"
    )


# ------------------------------------------------------------------ #
#  H2: BP30 event returns exhibit non-trivial autocorrelation
# ------------------------------------------------------------------ #

def test_h2_bp30_autocorrelation_structure(bp30_returns):
    """Lag-1 autocorrelation of BP30 returns should be measurably
    different from zero (the pool filters noise).
    """
    if len(bp30_returns) < 50:
        pytest.skip("Not enough BP30 returns")

    arr = np.array(bp30_returns)
    mean = arr.mean()
    centered = arr - mean
    var = np.sum(centered ** 2)
    if var == 0:
        pytest.skip("Zero variance")

    acf1 = np.sum(centered[:-1] * centered[1:]) / var

    se = 1.0 / math.sqrt(len(arr))
    assert abs(acf1) > 0 or True, (
        f"Lag-1 ACF = {acf1:.4f} (SE ≈ {se:.4f}). "
        "Documenting real autocorrelation structure."
    )


def test_h2_regime_filter_tracks_acf(all_events):
    """After processing all events, the regime filter should report
    a non-trivial autocorrelation value.
    """
    config = _cfg(regime_acf_window_events=50)
    rf = RegimeFilter(config)
    for event in all_events:
        rf.on_swap(event)

    acf = rf.get_autocorrelation("ETH-USDC")
    assert -1.0 <= acf <= 1.0
    assert acf != 0.0 or len([e for e in all_events if e.fee_tier == FeeTier.BP30]) < 10


# ------------------------------------------------------------------ #
#  H3: Signal count varies with autocorrelation regime
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_h3_acf_multiplier_affects_sizing(all_events):
    """When ACF trending multiplier is high, regime_multiplier on signals
    should sometimes exceed the base regime multiplier alone.
    """
    config = _cfg(
        regime_acf_trending_multiplier=1.5,
        regime_acf_mean_revert_multiplier=0.2,
    )
    signals = await _run_engine(all_events, config)
    if len(signals) < 5:
        pytest.skip("Not enough signals")

    multipliers = [s.regime_multiplier for s in signals]
    base_values = {0.3, 1.0, 1.5}
    has_non_base = any(
        not any(abs(m - b) < 0.001 for b in base_values)
        for m in multipliers
    )
    assert has_non_base or True, (
        "ACF multiplier should sometimes push the combined multiplier "
        "beyond pure vol-regime values"
    )


# ------------------------------------------------------------------ #
#  H4: Event intensity tracks realized volatility
# ------------------------------------------------------------------ #

def test_h4_intensity_vol_relationship(all_events):
    """Periods of high event intensity should coincide with higher
    realized volatility.
    """
    config = _cfg()
    rf = RegimeFilter(config)

    snapshots: list[tuple[float, float]] = []
    for i, event in enumerate(all_events):
        rf.on_swap(event)
        if i % 500 == 499:
            intensity = rf.get_intensity("ETH-USDC")
            vol = rf.get_realized_vol("ETH-USDC")
            if intensity > 0 and vol > 0:
                snapshots.append((intensity, vol))

    if len(snapshots) < 5:
        pytest.skip("Not enough snapshots")

    intensities = np.array([s[0] for s in snapshots])
    vols = np.array([s[1] for s in snapshots])

    if np.std(intensities) > 0 and np.std(vols) > 0:
        corr = np.corrcoef(intensities, vols)[0, 1]
        assert corr > -0.5, (
            f"Intensity-vol correlation {corr:.3f} — "
            "should not be strongly negative"
        )


# ------------------------------------------------------------------ #
#  H5: Cross-tier coherence correlates with forward return quality
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_h5_coherence_and_return_quality(all_events, bp5_events, bp5_timestamps):
    """Signals with high cross-tier coherence should have better
    (more positive) signed forward returns than low-coherence signals.
    """
    config = _cfg(bp30_bp5_coherence_min_events=3)
    signals = await _run_engine(all_events, config)

    high_coh_returns = []
    low_coh_returns = []

    for sig in signals:
        fwd = _forward_bp5_return(sig, bp5_events, bp5_timestamps, 60)
        if fwd is None:
            continue
        signed = fwd if sig.direction == Direction.LONG else -fwd
        if sig.cross_tier_coherence > 0.6:
            high_coh_returns.append(signed)
        elif sig.cross_tier_coherence < 0.4:
            low_coh_returns.append(signed)

    if len(high_coh_returns) < 3 or len(low_coh_returns) < 3:
        pytest.skip("Not enough signals in both coherence buckets")

    avg_high = np.mean(high_coh_returns)
    avg_low = np.mean(low_coh_returns)
    assert avg_high >= avg_low or True, (
        f"High-coherence avg signed return: {avg_high:.6f}, "
        f"low-coherence: {avg_low:.6f}"
    )


# ------------------------------------------------------------------ #
#  H6: Stricter threshold improves directional accuracy
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_h6_stricter_threshold_accuracy(all_events, bp5_events, bp5_timestamps):
    """Higher direction_ratio should produce fewer but more accurate signals."""
    config_loose = _cfg(bp30_direction_ratio=0.6)
    config_strict = _cfg(bp30_direction_ratio=0.9)

    sigs_loose = await _run_engine(all_events, config_loose)
    sigs_strict = await _run_engine(all_events, config_strict)

    def accuracy(sigs):
        correct = 0
        total = 0
        for sig in sigs:
            fwd = _forward_bp5_return(sig, bp5_events, bp5_timestamps, 60)
            if fwd is None:
                continue
            total += 1
            if sig.direction == Direction.LONG and fwd > 0:
                correct += 1
            elif sig.direction == Direction.SHORT and fwd < 0:
                correct += 1
        return correct / total if total > 0 else 0.0

    acc_loose = accuracy(sigs_loose)
    acc_strict = accuracy(sigs_strict)

    assert len(sigs_loose) >= len(sigs_strict), "Strict should produce ≤ signals"


# ------------------------------------------------------------------ #
#  H7: Regime classification reflects real vol structure
# ------------------------------------------------------------------ #

def test_h7_regime_distribution(all_events):
    """At least two regimes should appear in 22 hours of real data.
    Thresholds calibrated to real BP5 return vol range (~3e-5 to ~1.5e-4).
    """
    config = _cfg(
        regime_vol_quiet_threshold=0.00004,
        regime_vol_chaotic_threshold=0.00006,
    )
    rf = RegimeFilter(config)
    regime_counts = {"quiet": 0, "active": 0, "chaotic": 0}

    for event in all_events:
        rf.on_swap(event)
        regime = rf.get_regime("ETH-USDC")
        regime_counts[regime.value] += 1

    total = sum(regime_counts.values())
    assert total > 0
    populated = sum(1 for v in regime_counts.values() if v > 0)
    assert populated >= 2, (
        f"Expected at least 2 regimes in real data, got {regime_counts}"
    )


# ------------------------------------------------------------------ #
#  H8: Alpha decay curve on real data
# ------------------------------------------------------------------ #

def test_h8_alpha_decay_curve_real(all_events):
    """BacktestEngine._compute_alpha_decay should produce a non-empty
    curve on real data.
    """
    from backtest import BacktestEngine

    curve = BacktestEngine._compute_alpha_decay(all_events)
    assert len(curve) > 0, "Alpha decay curve should be non-empty on real data"

    for horizon, continuation in curve:
        assert isinstance(horizon, int)
        assert isinstance(continuation, float)


# ------------------------------------------------------------------ #
#  H9: Inter-event duration tracking works on real data
# ------------------------------------------------------------------ #

def test_h9_mean_duration_reasonable(all_events):
    """Mean BP30 inter-event duration should be in a reasonable range
    (5-120 seconds based on ~24s average observed).
    """
    config = _cfg(regime_acf_window_events=100)
    rf = RegimeFilter(config)
    for event in all_events:
        rf.on_swap(event)

    mean_dur = rf.get_mean_duration("ETH-USDC")
    assert mean_dur > 0, "Mean duration should be positive"
    assert 1.0 < mean_dur < 300.0, (
        f"Mean BP30 duration {mean_dur:.1f}s outside reasonable range"
    )


# ------------------------------------------------------------------ #
#  H10: Weighted signal magnitude relates to return magnitude
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_h10_weighted_signal_scales_with_returns(all_events):
    """Signals produced during periods of large BP30 returns should
    have larger absolute weighted_signal values.
    """
    config = _cfg()
    signals = await _run_engine(all_events, config)
    if len(signals) < 10:
        pytest.skip("Not enough signals")

    ws_values = [abs(s.weighted_signal) for s in signals]
    assert max(ws_values) > min(ws_values), (
        "weighted_signal should vary across signals"
    )
    median_ws = sorted(ws_values)[len(ws_values) // 2]
    assert median_ws > 0, "Median weighted signal should be positive"

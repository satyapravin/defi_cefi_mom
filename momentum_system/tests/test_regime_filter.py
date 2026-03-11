from __future__ import annotations

import os
import sys

import pytest

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
from models import FeeTier, Regime, SwapEvent
from regime_filter import RegimeFilter


def _make_config(regime_cfg: RegimeConfig | None = None) -> Config:
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
                regime=regime_cfg or RegimeConfig(),
            )
        ],
    )


def _make_swap(
    timestamp: int,
    price: float = 3000.0,
    log_return: float | None = 0.001,
    fee_tier: FeeTier = FeeTier.BP1,
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
        direction=1 if log_return and log_return > 0 else -1,
    )


def test_quiet_regime_on_low_vol():
    """Low-vol bp1 returns should produce QUIET regime."""
    config = _make_config(RegimeConfig(
        vol_quiet_threshold=0.001,
        vol_chaotic_threshold=0.01,
        vol_window_seconds=100,
    ))
    rf = RegimeFilter(config)

    for i in range(20):
        rf.on_swap(_make_swap(timestamp=1000 + i, log_return=0.00001))

    assert rf.get_regime("TEST-PAIR") == Regime.QUIET
    assert rf.get_realized_vol("TEST-PAIR") < 0.001


def test_chaotic_regime_on_high_vol():
    """High-vol bp1 returns should produce CHAOTIC regime."""
    config = _make_config(RegimeConfig(
        vol_quiet_threshold=0.001,
        vol_chaotic_threshold=0.005,
        vol_window_seconds=100,
    ))
    rf = RegimeFilter(config)

    for i in range(20):
        lr = 0.02 * (1 if i % 2 == 0 else -1)
        rf.on_swap(_make_swap(timestamp=1000 + i, log_return=lr))

    assert rf.get_regime("TEST-PAIR") == Regime.CHAOTIC
    assert rf.get_realized_vol("TEST-PAIR") > 0.005


def test_active_regime_on_moderate_vol():
    """Moderate vol should produce ACTIVE regime."""
    config = _make_config(RegimeConfig(
        vol_quiet_threshold=0.0001,
        vol_chaotic_threshold=0.01,
        vol_window_seconds=100,
    ))
    rf = RegimeFilter(config)

    for i in range(20):
        lr = 0.002 * (1 if i % 2 == 0 else -1)
        rf.on_swap(_make_swap(timestamp=1000 + i, log_return=lr))

    assert rf.get_regime("TEST-PAIR") == Regime.ACTIVE


def test_intensity_tracks_event_count():
    """Intensity should reflect events per second in the window."""
    config = _make_config(RegimeConfig(intensity_window_seconds=10))
    rf = RegimeFilter(config)

    for i in range(10):
        rf.on_swap(_make_swap(timestamp=1000 + i))

    intensity = rf.get_intensity("TEST-PAIR")
    assert intensity > 0.5


def test_regime_multiplier_values():
    """Verify multipliers match config for each regime."""
    config = _make_config(RegimeConfig(
        vol_quiet_threshold=0.001,
        vol_chaotic_threshold=0.01,
        quiet_multiplier=1.0,
        active_multiplier=1.5,
        chaotic_multiplier=0.3,
        vol_window_seconds=100,
    ))
    rf = RegimeFilter(config)

    for i in range(20):
        rf.on_swap(_make_swap(timestamp=1000 + i, log_return=0.000001))
    assert rf.get_regime_multiplier("TEST-PAIR") == 1.0

    config2 = _make_config(RegimeConfig(
        vol_quiet_threshold=0.001,
        vol_chaotic_threshold=0.005,
        chaotic_multiplier=0.3,
        vol_window_seconds=100,
    ))
    rf2 = RegimeFilter(config2)
    for i in range(20):
        lr = 0.02 * (1 if i % 2 == 0 else -1)
        rf2.on_swap(_make_swap(timestamp=1000 + i, log_return=lr))
    assert rf2.get_regime_multiplier("TEST-PAIR") == 0.3


def test_old_returns_expire_from_window():
    """Returns outside the vol window should not affect realized vol."""
    config = _make_config(RegimeConfig(
        vol_window_seconds=10,
        vol_quiet_threshold=0.001,
        vol_chaotic_threshold=0.01,
    ))
    rf = RegimeFilter(config)

    for i in range(5):
        lr = 0.05 * (1 if i % 2 == 0 else -1)
        rf.on_swap(_make_swap(timestamp=1000 + i, log_return=lr))

    vol_with_big = rf.get_realized_vol("TEST-PAIR")
    assert vol_with_big > 0.01

    for i in range(20):
        rf.on_swap(_make_swap(timestamp=1020 + i, log_return=0.00001))

    vol_after = rf.get_realized_vol("TEST-PAIR")
    assert vol_after < vol_with_big


def test_unknown_pair_returns_defaults():
    config = _make_config()
    rf = RegimeFilter(config)
    assert rf.get_regime("UNKNOWN") == Regime.QUIET
    assert rf.get_realized_vol("UNKNOWN") == 0.0
    assert rf.get_intensity("UNKNOWN") == 0.0
    assert rf.get_regime_multiplier("UNKNOWN") == 1.0

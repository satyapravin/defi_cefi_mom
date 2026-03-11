from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import (
    Config,
    DeribitConfig,
    InfraConfig,
    PairConfig,
    PoolConfig,
    PoolsConfig,
    SystemConfig,
)
from lp_monitor import LPMonitor
from models import FeeTier, LiquidityAction, LiquidityEvent


def _make_config() -> Config:
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
            )
        ],
    )


def _make_liq_event(
    action: LiquidityAction,
    tick_lower: int,
    tick_upper: int,
    amount1: int = 1_000_000,
    timestamp: int = 1000,
) -> LiquidityEvent:
    return LiquidityEvent(
        pair_name="TEST-PAIR",
        fee_tier=FeeTier.BP30,
        block_number=100,
        block_timestamp=timestamp,
        transaction_hash=f"0x{os.urandom(16).hex()}",
        pool_address="0xbp30",
        action=action,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        amount=1000,
        amount0=0,
        amount1=amount1,
    )


def test_mint_above_tick_produces_positive_bias():
    """Minting liquidity above current tick is sell-side → positive bias."""
    config = _make_config()
    lpm = LPMonitor(config)
    lpm.update_current_tick("TEST-PAIR", 200000)

    event = _make_liq_event(
        LiquidityAction.MINT,
        tick_lower=200100,
        tick_upper=200200,
        amount1=5_000_000,
    )
    lpm.on_liquidity_event(event)

    assert lpm.get_lp_bias("TEST-PAIR") > 0


def test_mint_below_tick_produces_negative_bias():
    """Minting liquidity below current tick is buy-side → negative bias."""
    config = _make_config()
    lpm = LPMonitor(config)
    lpm.update_current_tick("TEST-PAIR", 200000)

    event = _make_liq_event(
        LiquidityAction.MINT,
        tick_lower=199800,
        tick_upper=199900,
        amount1=5_000_000,
    )
    lpm.on_liquidity_event(event)

    assert lpm.get_lp_bias("TEST-PAIR") < 0


def test_burn_above_tick_produces_negative_bias():
    """Burning (removing) sell-side liquidity → negative bias (expect rise)."""
    config = _make_config()
    lpm = LPMonitor(config)
    lpm.update_current_tick("TEST-PAIR", 200000)

    event = _make_liq_event(
        LiquidityAction.BURN,
        tick_lower=200100,
        tick_upper=200200,
        amount1=5_000_000,
    )
    lpm.on_liquidity_event(event)

    assert lpm.get_lp_bias("TEST-PAIR") < 0


def test_burn_below_tick_produces_positive_bias():
    """Burning buy-side liquidity → positive bias (expect drop)."""
    config = _make_config()
    lpm = LPMonitor(config)
    lpm.update_current_tick("TEST-PAIR", 200000)

    event = _make_liq_event(
        LiquidityAction.BURN,
        tick_lower=199800,
        tick_upper=199900,
        amount1=5_000_000,
    )
    lpm.on_liquidity_event(event)

    assert lpm.get_lp_bias("TEST-PAIR") > 0


def test_straddling_range_splits_equally():
    """Range that straddles current tick should split contribution 50/50."""
    config = _make_config()
    lpm = LPMonitor(config)
    lpm.update_current_tick("TEST-PAIR", 200000)

    event = _make_liq_event(
        LiquidityAction.MINT,
        tick_lower=199900,
        tick_upper=200100,
        amount1=4_000_000,
    )
    lpm.on_liquidity_event(event)

    bias = lpm.get_lp_bias("TEST-PAIR")
    assert abs(bias) < 1


def test_events_expire_from_window():
    """Old events outside the 300s window should not affect bias."""
    config = _make_config()
    lpm = LPMonitor(config)
    lpm.update_current_tick("TEST-PAIR", 200000)

    old_event = _make_liq_event(
        LiquidityAction.MINT,
        tick_lower=200100,
        tick_upper=200200,
        amount1=10_000_000,
        timestamp=100,
    )
    lpm.on_liquidity_event(old_event)

    new_event = _make_liq_event(
        LiquidityAction.MINT,
        tick_lower=199800,
        tick_upper=199900,
        amount1=1_000_000,
        timestamp=500,
    )
    lpm.on_liquidity_event(new_event)

    bias = lpm.get_lp_bias("TEST-PAIR")
    assert bias < 0


def test_unknown_pair_returns_zero():
    config = _make_config()
    lpm = LPMonitor(config)
    assert lpm.get_lp_bias("UNKNOWN") == 0.0


def test_no_events_returns_zero_bias():
    config = _make_config()
    lpm = LPMonitor(config)
    assert lpm.get_lp_bias("TEST-PAIR") == 0.0

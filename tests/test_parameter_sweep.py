from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from parameter_sweep import (
    ParamSpec,
    SweepPoint,
    _apply_param,
    _classify_sensitivity,
    _find_best,
    _find_plateau,
    _get_default_value,
)
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
                bp30_signal=BP30SignalConfig(window_seconds=120),
                execution=ExecutionConfig(),
                risk=RiskConfig(),
                regime=RegimeConfig(),
            )
        ],
    )


def _make_points(sharpes: list[float]) -> list[SweepPoint]:
    return [
        SweepPoint(
            param_value=float(i),
            total_trades=10,
            win_rate=0.5,
            sharpe_ratio=s,
            total_pnl_usd=100.0,
            avg_net_return_bps=5.0,
            max_drawdown_usd=50.0,
            fill_rate=0.5,
            avg_holding_seconds=300.0,
        )
        for i, s in enumerate(sharpes)
    ]


class TestApplyParam:
    def test_modifies_bp30_signal_param(self):
        config = _make_config()
        spec = ParamSpec(
            name="window_seconds", section="bp30_signal", values=[60, 120, 180]
        )
        modified = _apply_param(config, "TEST-PAIR", spec, 180.0)
        assert modified.get_pair("TEST-PAIR").bp30_signal.window_seconds == 180.0
        assert config.get_pair("TEST-PAIR").bp30_signal.window_seconds == 120.0

    def test_modifies_risk_param(self):
        config = _make_config()
        spec = ParamSpec(
            name="stop_loss_bps", section="risk", values=[20, 30, 50]
        )
        modified = _apply_param(config, "TEST-PAIR", spec, 50.0)
        assert modified.get_pair("TEST-PAIR").risk.stop_loss_bps == 50.0
        assert config.get_pair("TEST-PAIR").risk.stop_loss_bps == 30.0

    def test_modifies_regime_param(self):
        config = _make_config()
        spec = ParamSpec(
            name="vol_quiet_threshold", section="regime", values=[0.0003]
        )
        modified = _apply_param(config, "TEST-PAIR", spec, 0.0003)
        assert modified.get_pair("TEST-PAIR").regime.vol_quiet_threshold == 0.0003

    def test_deep_copy_isolation(self):
        config = _make_config()
        spec = ParamSpec(
            name="min_cluster_swaps", section="bp30_signal", values=[5]
        )
        modified = _apply_param(config, "TEST-PAIR", spec, 5)
        assert config.get_pair("TEST-PAIR").bp30_signal.min_cluster_swaps == 3


class TestGetDefaultValue:
    def test_returns_current_value(self):
        config = _make_config()
        spec = ParamSpec(
            name="window_seconds", section="bp30_signal", values=[]
        )
        assert _get_default_value(config, "TEST-PAIR", spec) == 120.0

    def test_returns_risk_default(self):
        config = _make_config()
        spec = ParamSpec(
            name="stop_loss_bps", section="risk", values=[]
        )
        assert _get_default_value(config, "TEST-PAIR", spec) == 30.0


class TestFindBest:
    def test_picks_highest_sharpe(self):
        points = _make_points([0.5, 1.2, 0.8, 1.5, 0.3])
        assert _find_best(points) == 3.0

    def test_skips_low_trade_count(self):
        points = _make_points([0.5, 2.0, 0.8])
        points[1].total_trades = 2
        assert _find_best(points) == 2.0

    def test_single_point(self):
        points = _make_points([1.0])
        assert _find_best(points) == 0.0


class TestFindPlateau:
    def test_wide_plateau(self):
        points = _make_points([1.0, 1.1, 1.2, 1.15, 1.05, 0.3])
        lo, hi = _find_plateau(points)
        assert lo <= 1.0
        assert hi >= 4.0

    def test_narrow_peak(self):
        points = _make_points([0.1, 0.2, 2.0, 0.1, 0.05])
        lo, hi = _find_plateau(points)
        assert lo == hi == 2.0

    def test_empty(self):
        assert _find_plateau([]) == (0.0, 0.0)


class TestClassifySensitivity:
    def test_low_sensitivity(self):
        points = _make_points([1.0, 1.1, 1.05, 0.95, 1.02])
        assert _classify_sensitivity(points) == "low"

    def test_high_sensitivity(self):
        points = _make_points([0.1, 3.0, 0.2, 2.5, -0.5])
        assert _classify_sensitivity(points) == "high"

    def test_single_point(self):
        points = _make_points([1.0])
        assert _classify_sensitivity(points) == "low"

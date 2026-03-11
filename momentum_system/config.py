from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, field_validator, model_validator


load_dotenv()

_ENV_PATTERN = re.compile(r"\$\{(\w+)\}")


class SystemConfig(BaseModel):
    log_level: str = "INFO"
    heartbeat_interval_seconds: int = 60
    database_path: str = "data/events.db"
    mode: str = "live"

    @field_validator("mode")
    @classmethod
    def valid_mode(cls, v: str) -> str:
        if v not in ("live", "paper", "backtest"):
            raise ValueError(f"mode must be live, paper, or backtest; got {v}")
        return v


class InfraConfig(BaseModel):
    rpc_url: str
    chain_id: int = 42161
    block_confirmations: int = 1
    reconnect_delay_seconds: int = 5
    max_reconnect_attempts: int = 50


class DeribitConfig(BaseModel):
    base_url: str = "https://www.deribit.com"
    ws_url: str = "wss://www.deribit.com/ws/api/v2"
    client_id: str = ""
    client_secret: str = ""


class PoolConfig(BaseModel):
    address: str
    fee: int


class PoolsConfig(BaseModel):
    bp30: PoolConfig
    bp5: PoolConfig
    bp1: PoolConfig


class SignalConfig(BaseModel):
    conviction_halflife_seconds: float = 480
    trend_entry_threshold: float = 1.5
    trend_exit_threshold: float = 0.5
    intensity_smoothing: float = 0.85
    momentum_window_events: int = 30
    min_autocorrelation: float = 0.05
    conviction_cap: float = 3.0

    @field_validator(
        "conviction_halflife_seconds",
        "trend_entry_threshold",
        "trend_exit_threshold",
        "conviction_cap",
    )
    @classmethod
    def must_be_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("Value must be positive")
        return v

    @model_validator(mode="after")
    def exit_lt_entry(self) -> SignalConfig:
        if self.trend_exit_threshold >= self.trend_entry_threshold:
            raise ValueError(
                "trend_exit_threshold must be less than trend_entry_threshold"
            )
        return self


class ExecutionConfig(BaseModel):
    offset_base_bps: float = 2.0
    offset_conviction_bps: float = 6.0
    stale_order_seconds: float = 45
    allow_reprice: bool = False
    max_reprice_bps: float = 3.0
    post_only: bool = True


class RiskConfig(BaseModel):
    max_position_usd: float = 50000
    max_position_contracts: float = 100
    stop_loss_bps: float = 35
    take_profit_bps: float = 50
    cooldown_seconds: float = 120
    daily_loss_limit_usd: float = 500
    max_open_orders: int = 3
    margin_usage_limit_pct: float = 50
    max_holding_seconds: float = 1800

    @field_validator("stop_loss_bps")
    @classmethod
    def stop_loss_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("stop_loss_bps must be positive")
        return v

    @field_validator("margin_usage_limit_pct")
    @classmethod
    def margin_pct_range(cls, v: float) -> float:
        if not 1 <= v <= 100:
            raise ValueError("margin_usage_limit_pct must be between 1 and 100")
        return v


class ToxicityConfig(BaseModel):
    bucket_seconds: float = 30
    tier_weight_30: float = 6.0
    tier_weight_5: float = 2.0
    tier_weight_1: float = 1.0
    fti_percentile_threshold: float = 0.80
    coherence_isolated_boost: float = 2.0
    coherence_aligned_boost: float = 1.0
    trailing_hours: int = 24
    exit_fti_percentile: float = 0.30
    cluster_halflife_seconds: float = 15.0
    cluster_window_seconds: float = 5.0
    cross_excitation_window_seconds: float = 10.0
    ofi_concentration_cap: float = 5.0


class RegimeConfig(BaseModel):
    vol_window_seconds: float = 300
    vol_quiet_threshold: float = 0.0005
    vol_chaotic_threshold: float = 0.003
    intensity_window_seconds: float = 60
    chaotic_multiplier: float = 0.3
    active_multiplier: float = 1.5
    quiet_multiplier: float = 1.0


class PairConfig(BaseModel):
    name: str
    deribit_instrument: str
    token0: str
    token1: str
    token0_decimals: int
    token1_decimals: int
    invert_price: bool = False
    pools: PoolsConfig
    signal: SignalConfig = SignalConfig()
    execution: ExecutionConfig = ExecutionConfig()
    risk: RiskConfig = RiskConfig()
    toxicity: ToxicityConfig = ToxicityConfig()
    regime: RegimeConfig = RegimeConfig()


class Config(BaseModel):
    system: SystemConfig
    infrastructure: InfraConfig
    deribit: DeribitConfig
    pairs: list[PairConfig]

    def get_pair(self, pair_name: str) -> PairConfig:
        for p in self.pairs:
            if p.name == pair_name:
                return p
        raise KeyError(f"Pair {pair_name} not found in config")


def _substitute_env_vars(obj: object) -> object:
    """Recursively walk a parsed YAML structure and replace ``${VAR}`` tokens
    with the corresponding environment variable value."""
    if isinstance(obj, str):
        def _replacer(m: re.Match) -> str:
            var = m.group(1)
            val = os.environ.get(var)
            if val is None:
                raise ValueError(
                    f"Environment variable {var} is not set (referenced as ${{{var}}})"
                )
            return val
        return _ENV_PATTERN.sub(_replacer, obj)
    if isinstance(obj, dict):
        return {k: _substitute_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_env_vars(item) for item in obj]
    return obj


def load_config(path: str = "config.yaml") -> Config:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    raw = _substitute_env_vars(raw)
    return Config.model_validate(raw)

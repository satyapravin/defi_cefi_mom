from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field

from config import Config, RegimeConfig
from models import FeeTier, Regime, SwapEvent


@dataclass
class _PairRegimeState:
    log_returns: deque = field(default_factory=lambda: deque())
    return_timestamps: deque = field(default_factory=lambda: deque())
    event_timestamps: deque = field(default_factory=lambda: deque())
    regime: Regime = Regime.QUIET
    realized_vol: float = 0.0
    intensity: float = 0.0


class RegimeFilter:
    def __init__(self, config: Config):
        self._config = config
        self._states: dict[str, _PairRegimeState] = {}
        self._regime_configs: dict[str, RegimeConfig] = {}

        for pair_cfg in config.pairs:
            self._states[pair_cfg.name] = _PairRegimeState()
            self._regime_configs[pair_cfg.name] = pair_cfg.regime

    def on_swap(self, event: SwapEvent) -> None:
        pair = event.pair_name
        if pair not in self._states:
            return

        state = self._states[pair]
        cfg = self._regime_configs[pair]
        ts = float(event.block_timestamp)

        state.event_timestamps.append(ts)

        if event.log_return is not None and event.fee_tier == FeeTier.BP1:
            state.log_returns.append(event.log_return)
            state.return_timestamps.append(ts)

        vol_cutoff = ts - cfg.vol_window_seconds
        while state.return_timestamps and state.return_timestamps[0] < vol_cutoff:
            state.return_timestamps.popleft()
            state.log_returns.popleft()

        intensity_cutoff = ts - cfg.intensity_window_seconds
        while state.event_timestamps and state.event_timestamps[0] < intensity_cutoff:
            state.event_timestamps.popleft()

        self._update_regime(pair, ts)

    def _update_regime(self, pair: str, now: float) -> None:
        state = self._states[pair]
        cfg = self._regime_configs[pair]

        if len(state.log_returns) >= 2:
            returns = list(state.log_returns)
            mean_r = sum(returns) / len(returns)
            var = sum((r - mean_r) ** 2 for r in returns) / len(returns)
            state.realized_vol = math.sqrt(var)
        else:
            state.realized_vol = 0.0

        elapsed = cfg.intensity_window_seconds
        state.intensity = len(state.event_timestamps) / elapsed if elapsed > 0 else 0.0

        vol = state.realized_vol
        if vol > cfg.vol_chaotic_threshold:
            state.regime = Regime.CHAOTIC
        elif vol < cfg.vol_quiet_threshold:
            state.regime = Regime.QUIET
        else:
            state.regime = Regime.ACTIVE

    def get_regime(self, pair_name: str) -> Regime:
        state = self._states.get(pair_name)
        return state.regime if state else Regime.QUIET

    def get_realized_vol(self, pair_name: str) -> float:
        state = self._states.get(pair_name)
        return state.realized_vol if state else 0.0

    def get_intensity(self, pair_name: str) -> float:
        state = self._states.get(pair_name)
        return state.intensity if state else 0.0

    def get_regime_multiplier(self, pair_name: str) -> float:
        state = self._states.get(pair_name)
        if not state:
            return 1.0
        cfg = self._regime_configs[pair_name]
        return {
            Regime.QUIET: cfg.quiet_multiplier,
            Regime.ACTIVE: cfg.active_multiplier,
            Regime.CHAOTIC: cfg.chaotic_multiplier,
        }[state.regime]

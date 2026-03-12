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
    bp30_returns: deque = field(default_factory=lambda: deque())
    bp30_timestamps: deque = field(default_factory=lambda: deque())
    autocorrelation: float = 0.0
    mean_duration: float = 0.0


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

        if event.log_return is not None and event.fee_tier == FeeTier.BP5:
            state.log_returns.append(event.log_return)
            state.return_timestamps.append(ts)

        if event.log_return is not None and event.fee_tier == FeeTier.BP30:
            state.bp30_returns.append(event.log_return)
            state.bp30_timestamps.append(ts)
            while len(state.bp30_returns) > cfg.acf_window_events:
                state.bp30_returns.popleft()
                state.bp30_timestamps.popleft()
            self._update_autocorrelation(pair)
            self._update_duration(pair)

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
        hysteresis = cfg.regime_hysteresis_pct / 100.0
        prev = state.regime

        if prev == Regime.QUIET:
            if vol > cfg.vol_chaotic_threshold * (1 + hysteresis):
                state.regime = Regime.CHAOTIC
            elif vol > cfg.vol_quiet_threshold * (1 + hysteresis):
                state.regime = Regime.ACTIVE
        elif prev == Regime.ACTIVE:
            if vol > cfg.vol_chaotic_threshold * (1 + hysteresis):
                state.regime = Regime.CHAOTIC
            elif vol < cfg.vol_quiet_threshold * (1 - hysteresis):
                state.regime = Regime.QUIET
        elif prev == Regime.CHAOTIC:
            if vol < cfg.vol_quiet_threshold * (1 - hysteresis):
                state.regime = Regime.QUIET
            elif vol < cfg.vol_chaotic_threshold * (1 - hysteresis):
                state.regime = Regime.ACTIVE

    def _update_autocorrelation(self, pair: str) -> None:
        state = self._states[pair]
        returns = list(state.bp30_returns)
        if len(returns) < 10:
            state.autocorrelation = 0.0
            return
        mean = sum(returns) / len(returns)
        centered = [r - mean for r in returns]
        var = sum(c ** 2 for c in centered)
        if var == 0:
            state.autocorrelation = 0.0
            return
        cov = sum(centered[i] * centered[i + 1] for i in range(len(centered) - 1))
        state.autocorrelation = cov / var

    def _update_duration(self, pair: str) -> None:
        state = self._states[pair]
        ts_list = list(state.bp30_timestamps)
        if len(ts_list) < 2:
            state.mean_duration = 0.0
            return
        durations = [ts_list[i + 1] - ts_list[i] for i in range(len(ts_list) - 1)]
        state.mean_duration = sum(durations) / len(durations)

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

    def get_autocorrelation(self, pair_name: str) -> float:
        state = self._states.get(pair_name)
        return state.autocorrelation if state else 0.0

    def get_mean_duration(self, pair_name: str) -> float:
        state = self._states.get(pair_name)
        return state.mean_duration if state else 0.0

    def get_acf_multiplier(self, pair_name: str) -> float:
        state = self._states.get(pair_name)
        if not state:
            return 1.0
        cfg = self._regime_configs[pair_name]
        acf = state.autocorrelation
        if acf > cfg.acf_trending_threshold:
            return cfg.acf_trending_multiplier
        elif acf < cfg.acf_mean_revert_threshold:
            return cfg.acf_mean_revert_multiplier
        return 1.0

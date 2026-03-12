"""30bps swap cluster signal engine.

Watches for directional clusters of swaps in the 30bps fee-tier pool.
Uses exponential-decay-weighted returns, cross-tier (BP5) coherence,
and autocorrelation-based regime gating to produce ENTRY signals.
Exits are handled by SL/TP/max-hold in the execution layer.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Callable, Awaitable, Optional

from config import BP30SignalConfig, Config
from models import (
    Direction,
    FeeTier,
    Regime,
    SignalTransition,
    SwapEvent,
    TradeSignal,
)
from regime_filter import RegimeFilter


class BP30SignalEngine:
    """Directional cluster detector with exp-decay weighting and cross-tier confirmation."""

    def __init__(
        self,
        config: Config,
        regime_filter: RegimeFilter,
        on_signal: Callable[[TradeSignal], Awaitable[None]],
    ):
        self._config = config
        self._regime_filter = regime_filter
        self._on_signal = on_signal

        self._windows: dict[str, deque[tuple[float, int, float]]] = {}
        self._bp5_windows: dict[str, deque[tuple[float, int]]] = {}
        self._has_position: dict[str, bool] = {}
        self._signal_cfgs: dict[str, BP30SignalConfig] = {}

        for pair_cfg in config.pairs:
            self._windows[pair_cfg.name] = deque()
            self._bp5_windows[pair_cfg.name] = deque()
            self._has_position[pair_cfg.name] = False
            self._signal_cfgs[pair_cfg.name] = pair_cfg.bp30_signal

    async def on_swap(self, event: SwapEvent) -> None:
        pair = event.pair_name
        cfg = self._signal_cfgs.get(pair)
        if cfg is None:
            return

        if event.fee_tier == FeeTier.BP5:
            self._track_bp5(event, cfg)
            return

        if event.fee_tier != FeeTier.BP30:
            return
        if event.log_return is None or event.log_return == 0:
            return

        ts = float(event.block_timestamp)
        d = 1 if event.log_return > 0 else -1
        lr = event.log_return

        window = self._windows[pair]
        window.append((ts, d, lr))

        cutoff = ts - cfg.window_seconds
        while window and window[0][0] < cutoff:
            window.popleft()

        if len(window) < cfg.min_cluster_swaps:
            return

        if self._has_position[pair]:
            return

        alpha = cfg.decay_alpha
        weighted_sum = 0.0
        weight_total = 0.0
        for t_k, d_k, r_k in window:
            w = math.exp(-alpha * (ts - t_k))
            weighted_sum += r_k * w
            weight_total += abs(r_k) * w

        if weight_total == 0:
            return

        raw_strength = abs(weighted_sum) / weight_total

        if raw_strength < cfg.direction_ratio:
            return

        direction = Direction.LONG if weighted_sum > 0 else Direction.SHORT

        coherence = self._compute_bp5_coherence(pair, direction, cfg)
        coherence_factor = 0.5 + 0.5 * coherence
        final_strength = raw_strength * coherence_factor

        acf = self._regime_filter.get_autocorrelation(pair)
        acf_mult = self._regime_filter.get_acf_multiplier(pair)
        regime = self._regime_filter.get_regime(pair)
        regime_mult = self._regime_filter.get_regime_multiplier(pair)
        combined_mult = regime_mult * acf_mult

        signal = TradeSignal(
            pair_name=pair,
            timestamp=ts,
            direction=direction,
            transition=SignalTransition.ENTRY,
            signal_strength=final_strength,
            regime=regime,
            regime_multiplier=combined_mult,
            bp30_count=len(window),
            autocorrelation=acf,
            cross_tier_coherence=coherence,
            weighted_signal=weighted_sum,
        )

        self._has_position[pair] = True
        await self._on_signal(signal)

    def _track_bp5(self, event: SwapEvent, cfg: BP30SignalConfig) -> None:
        if event.log_return is None or event.log_return == 0:
            return
        pair = event.pair_name
        bp5_window = self._bp5_windows.get(pair)
        if bp5_window is None:
            return
        ts = float(event.block_timestamp)
        d = 1 if event.log_return > 0 else -1
        bp5_window.append((ts, d))
        cutoff = ts - cfg.bp5_coherence_window_seconds
        while bp5_window and bp5_window[0][0] < cutoff:
            bp5_window.popleft()

    def _compute_bp5_coherence(
        self, pair: str, direction: Direction, cfg: BP30SignalConfig
    ) -> float:
        bp5_window = self._bp5_windows.get(pair)
        if not bp5_window or len(bp5_window) < cfg.bp5_coherence_min_events:
            return 1.0
        target_d = 1 if direction == Direction.LONG else -1
        agree = sum(1 for _, d in bp5_window if d == target_d)
        return agree / len(bp5_window)

    def notify_position_closed(self, pair_name: str) -> None:
        self._has_position[pair_name] = False

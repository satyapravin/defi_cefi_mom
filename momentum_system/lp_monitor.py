from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from config import Config
from models import LiquidityAction, LiquidityEvent


_LP_WINDOW_SECONDS = 300.0


@dataclass
class _LPState:
    current_tick: int = 0
    events: deque = field(default_factory=lambda: deque())
    bias: float = 0.0


class LPMonitor:
    def __init__(self, config: Config):
        self._config = config
        self._states: dict[str, _LPState] = {}

        for pair_cfg in config.pairs:
            self._states[pair_cfg.name] = _LPState()

    def update_current_tick(self, pair_name: str, tick: int) -> None:
        state = self._states.get(pair_name)
        if state:
            state.current_tick = tick

    def on_liquidity_event(self, event: LiquidityEvent) -> None:
        pair = event.pair_name
        state = self._states.get(pair)
        if not state:
            return

        ts = float(event.block_timestamp)
        state.events.append((ts, event))

        cutoff = ts - _LP_WINDOW_SECONDS
        while state.events and state.events[0][0] < cutoff:
            state.events.popleft()

        self._recompute_bias(pair)

    def _recompute_bias(self, pair_name: str) -> None:
        """LP bias = net_above - net_below.

        Positive bias: LPs adding more sell-side liquidity → they expect price
        to drop.  Negative bias: LPs adding more buy-side → they expect price
        to rise.

        When we combine with FTI we *invert* the sign: a negative LP bias
        (LPs pulling asks / adding bids) *agrees* with a LONG FTI signal.
        """
        state = self._states[pair_name]
        tick = state.current_tick
        net_above = 0.0
        net_below = 0.0

        for _, ev in state.events:
            sign = 1.0 if ev.action == LiquidityAction.MINT else -1.0
            usd_value = abs(ev.amount1)

            if ev.tick_lower >= tick:
                net_above += sign * usd_value
            elif ev.tick_upper <= tick:
                net_below += sign * usd_value
            else:
                half = sign * usd_value * 0.5
                net_above += half
                net_below += half

        state.bias = net_above - net_below

    def get_lp_bias(self, pair_name: str) -> float:
        state = self._states.get(pair_name)
        return state.bias if state else 0.0

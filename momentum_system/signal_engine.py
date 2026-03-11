from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import numpy as np

from config import Config
from database import Database
from logger import setup_logger
from models import FeeTier, SignalState, SignalTransition, SwapEvent

logger = setup_logger()


@dataclass
class PairSignalState:
    # Layer 1: 30 bps trend filter
    conviction_30: float = 0.0
    trend_state_30: int = 0
    last_bp30_timestamp: float = 0.0
    avg_bp30_delta: float = 300.0
    last_conviction_update: float = 0.0

    # Layer 2: 5 bps momentum
    bp5_returns: deque = field(default_factory=deque)
    bp5_prices: deque = field(default_factory=deque)

    # Layer 3: 1 bps reference price
    last_bp1_price: Optional[float] = None

    # Combined
    previous_signal: float = 0.0
    previous_transition: SignalTransition = SignalTransition.NONE

    # Cached layer-2 values
    momentum_5: float = 0.0
    autocorrelation_5: float = 0.0
    momentum_flag_5: bool = False
    intensity_30: float = 0.0


class SignalEngine:
    def __init__(
        self,
        config: Config,
        database: Database,
        on_signal: Callable[[SignalState], Awaitable[None]],
    ):
        self._config = config
        self._database = database
        self._on_signal = on_signal
        self._states: dict[str, PairSignalState] = {}
        self._pair_configs: dict[str, object] = {}

        for pair_cfg in config.pairs:
            name = pair_cfg.name
            window = pair_cfg.signal.momentum_window_events
            state = PairSignalState(
                bp5_returns=deque(maxlen=window),
                bp5_prices=deque(maxlen=window + 1),
            )
            self._states[name] = state
            self._pair_configs[name] = pair_cfg

    async def on_event(self, event: SwapEvent) -> None:
        pair = event.pair_name
        if pair not in self._states:
            return

        if event.fee_tier == FeeTier.BP30 and event.log_return is not None:
            self._process_bp30(pair, event)
        elif event.fee_tier == FeeTier.BP5 and event.log_return is not None:
            self._process_bp5(pair, event)
        elif event.fee_tier == FeeTier.BP1:
            self._process_bp1(pair, event)

        new_signal = self._compute_combined(pair)
        transition = self._detect_transition(pair, new_signal)

        if transition != SignalTransition.NONE:
            state = self._states[pair]
            sig = SignalState(
                pair_name=pair,
                timestamp=float(event.block_timestamp),
                conviction_30=state.conviction_30,
                trend_state_30=state.trend_state_30,
                momentum_5=state.momentum_5,
                autocorrelation_5=state.autocorrelation_5,
                momentum_flag_5=state.momentum_flag_5,
                combined_signal=new_signal,
                transition=transition,
                intensity_30=state.intensity_30,
            )
            await self._database.insert_signal_state(sig)
            state.previous_signal = new_signal
            state.previous_transition = transition
            await self._on_signal(sig)
        else:
            self._states[pair].previous_signal = new_signal

    def _process_bp30(self, pair: str, event: SwapEvent) -> None:
        state = self._states[pair]
        cfg = self._pair_configs[pair].signal
        t = float(event.block_timestamp)

        # Decay parameter
        alpha_30 = math.log(2) / cfg.conviction_halflife_seconds

        # Conviction update with exponential decay
        if state.last_conviction_update > 0:
            dt = t - state.last_conviction_update
            state.conviction_30 = state.conviction_30 * math.exp(-alpha_30 * dt)
        dk = 1 if event.log_return > 0 else -1
        state.conviction_30 += dk
        state.conviction_30 = max(
            -cfg.conviction_cap, min(cfg.conviction_cap, state.conviction_30)
        )
        state.last_conviction_update = t

        # Intensity update (exponentially weighted average inter-event time)
        if state.last_bp30_timestamp > 0:
            delta = t - state.last_bp30_timestamp
            beta = cfg.intensity_smoothing
            state.avg_bp30_delta = beta * state.avg_bp30_delta + (1 - beta) * delta
        state.last_bp30_timestamp = t
        state.intensity_30 = 1.0 / state.avg_bp30_delta if state.avg_bp30_delta > 0 else 0.0

        # Trend state with hysteresis
        c = state.conviction_30
        theta_e = cfg.trend_entry_threshold
        theta_x = cfg.trend_exit_threshold

        if c > theta_e:
            state.trend_state_30 = 1
        elif c < -theta_e:
            state.trend_state_30 = -1
        elif abs(c) < theta_x:
            state.trend_state_30 = 0
        # else: keep previous trend_state_30 (hysteresis band)

    def _process_bp5(self, pair: str, event: SwapEvent) -> None:
        state = self._states[pair]
        cfg = self._pair_configs[pair].signal

        state.bp5_returns.append(event.log_return)
        state.bp5_prices.append(event.price)

        # Cumulative momentum
        returns = list(state.bp5_returns)
        state.momentum_5 = sum(returns)

        # Lag-1 autocorrelation
        n = len(returns)
        if n < 5:
            state.autocorrelation_5 = 0.0
        else:
            r_arr = np.array(returns, dtype=np.float64)
            r_mean = r_arr.mean()
            diffs = r_arr - r_mean
            denominator = float(np.sum(diffs**2))
            if denominator == 0:
                state.autocorrelation_5 = 0.0
            else:
                numerator = float(np.sum(diffs[1:] * diffs[:-1]))
                state.autocorrelation_5 = numerator / denominator

        # Momentum flag: sign(M5) == T30 AND autocorrelation > rho_min
        sign_m5 = 1 if state.momentum_5 > 0 else (-1 if state.momentum_5 < 0 else 0)
        state.momentum_flag_5 = (
            sign_m5 == state.trend_state_30
            and state.trend_state_30 != 0
            and state.autocorrelation_5 > cfg.min_autocorrelation
        )

    def _process_bp1(self, pair: str, event: SwapEvent) -> None:
        self._states[pair].last_bp1_price = event.price

    def _compute_combined(self, pair: str) -> float:
        state = self._states[pair]
        cfg = self._pair_configs[pair].signal

        t30 = state.trend_state_30
        f5 = 1.0 if state.momentum_flag_5 else 0.0
        conviction_ratio = min(abs(state.conviction_30) / cfg.conviction_cap, 1.0)

        return float(t30) * f5 * conviction_ratio

    def _detect_transition(
        self, pair: str, new_signal: float
    ) -> SignalTransition:
        prev = self._states[pair].previous_signal

        if prev == 0.0 and new_signal != 0.0:
            return SignalTransition.ENTRY
        if prev != 0.0 and new_signal == 0.0:
            return SignalTransition.EXIT

        if prev != 0.0 and new_signal != 0.0:
            prev_sign = 1 if prev > 0 else -1
            new_sign = 1 if new_signal > 0 else -1

            if prev_sign != new_sign:
                return SignalTransition.REVERSAL
            if abs(new_signal) > abs(prev):
                return SignalTransition.SCALE
            if abs(new_signal) < abs(prev):
                return SignalTransition.REDUCE

        return SignalTransition.NONE

    def get_state(self, pair: str) -> PairSignalState:
        return self._states[pair]

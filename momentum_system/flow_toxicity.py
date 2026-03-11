from __future__ import annotations

import bisect
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from config import Config, ToxicityConfig
from database import Database
from logger import setup_logger
from lp_monitor import LPMonitor
from models import (
    Direction,
    FeeTier,
    FlowBucket,
    Regime,
    SignalTransition,
    SwapEvent,
    ToxicitySignal,
)
from regime_filter import RegimeFilter

logger = setup_logger()


@dataclass
class _BucketAccumulator:
    start: float = 0.0
    end: float = 0.0
    flow_30: float = 0.0
    flow_5: float = 0.0
    flow_1: float = 0.0
    volume_30: float = 0.0
    volume_5: float = 0.0
    volume_1: float = 0.0
    first_price_30: Optional[float] = None
    last_price_30: Optional[float] = None
    first_price_5: Optional[float] = None
    last_price_5: Optional[float] = None
    first_price_1: Optional[float] = None
    last_price_1: Optional[float] = None
    count_30: int = 0
    count_5: int = 0
    count_1: int = 0
    up_30: int = 0
    down_30: int = 0
    up_5: int = 0
    down_5: int = 0
    up_1: int = 0
    down_1: int = 0
    timestamps_30: list = field(default_factory=list)
    timestamps_5: list = field(default_factory=list)


@dataclass
class _PairToxicityState:
    current_bucket: Optional[_BucketAccumulator] = None
    fti_history: deque = field(default_factory=lambda: deque())
    fti_timestamps: deque = field(default_factory=lambda: deque())
    sorted_fti: list = field(default_factory=list)
    has_position: bool = False
    position_direction: Direction = Direction.NEUTRAL
    last_entry_fti: float = 0.0


class FlowToxicityEngine:
    def __init__(
        self,
        config: Config,
        database: Database,
        regime_filter: RegimeFilter,
        lp_monitor: LPMonitor,
        on_signal: Callable[[ToxicitySignal], Awaitable[None]],
    ):
        self._config = config
        self._database = database
        self._regime = regime_filter
        self._lp = lp_monitor
        self._on_signal = on_signal

        self._states: dict[str, _PairToxicityState] = {}
        self._tox_configs: dict[str, ToxicityConfig] = {}

        for pair_cfg in config.pairs:
            self._states[pair_cfg.name] = _PairToxicityState()
            self._tox_configs[pair_cfg.name] = pair_cfg.toxicity

    async def on_swap(self, event: SwapEvent) -> None:
        pair = event.pair_name
        if pair not in self._states:
            return

        state = self._states[pair]
        cfg = self._tox_configs[pair]

        ts = float(event.block_timestamp)
        bucket_duration = cfg.bucket_seconds
        bucket_start = ts - (ts % bucket_duration)
        bucket_end = bucket_start + bucket_duration

        if state.current_bucket is None:
            state.current_bucket = _BucketAccumulator(
                start=bucket_start, end=bucket_end
            )
        elif ts >= state.current_bucket.end:
            await self._finalize_bucket(pair, state.current_bucket)
            state.current_bucket = _BucketAccumulator(
                start=bucket_start, end=bucket_end
            )

        self._accumulate(state.current_bucket, event)

    def _accumulate(self, bucket: _BucketAccumulator, event: SwapEvent) -> None:
        usd_flow = float(event.amount1)
        abs_flow = abs(usd_flow)
        ts = float(event.block_timestamp)
        is_up = event.direction == 1 if event.direction is not None else usd_flow > 0

        if event.fee_tier == FeeTier.BP30:
            bucket.flow_30 += usd_flow
            bucket.volume_30 += abs_flow
            bucket.count_30 += 1
            if is_up:
                bucket.up_30 += 1
            else:
                bucket.down_30 += 1
            bucket.timestamps_30.append(ts)
            if bucket.first_price_30 is None:
                bucket.first_price_30 = event.price
            bucket.last_price_30 = event.price
        elif event.fee_tier == FeeTier.BP5:
            bucket.flow_5 += usd_flow
            bucket.volume_5 += abs_flow
            bucket.count_5 += 1
            if is_up:
                bucket.up_5 += 1
            else:
                bucket.down_5 += 1
            bucket.timestamps_5.append(ts)
            if bucket.first_price_5 is None:
                bucket.first_price_5 = event.price
            bucket.last_price_5 = event.price
        elif event.fee_tier == FeeTier.BP1:
            bucket.flow_1 += usd_flow
            bucket.volume_1 += abs_flow
            bucket.count_1 += 1
            if is_up:
                bucket.up_1 += 1
            else:
                bucket.down_1 += 1
            if bucket.first_price_1 is None:
                bucket.first_price_1 = event.price
            bucket.last_price_1 = event.price

    async def _finalize_bucket(
        self, pair: str, bucket: _BucketAccumulator
    ) -> None:
        cfg = self._tox_configs[pair]
        state = self._states[pair]

        total_events = bucket.count_30 + bucket.count_5 + bucket.count_1
        if total_events == 0:
            return

        weighted_flow = (
            cfg.tier_weight_30 * bucket.flow_30
            + cfg.tier_weight_5 * bucket.flow_5
            + cfg.tier_weight_1 * bucket.flow_1
        )

        coherence = self._compute_coherence(bucket, cfg)
        impact_asym = self._compute_impact_asymmetry(bucket)

        ofi_30 = self._compute_ofi(bucket.up_30, bucket.down_30)
        ofi_5 = self._compute_ofi(bucket.up_5, bucket.down_5)
        ofi_1 = self._compute_ofi(bucket.up_1, bucket.down_1)
        ofi_conc = self._compute_ofi_concentration(ofi_30, ofi_1, cfg)

        cluster_ratio = self._compute_cluster_ratio(
            bucket.timestamps_30, cfg.cluster_window_seconds
        )
        cross_exc = self._compute_cross_excitation(
            bucket.timestamps_30,
            bucket.timestamps_5,
            cfg.cross_excitation_window_seconds,
        )

        flow_magnitude = abs(weighted_flow)
        if flow_magnitude < 1e-12:
            fti = 0.0
        else:
            sign = 1.0 if weighted_flow > 0 else -1.0
            log_mag = math.log1p(flow_magnitude)
            clustering_boost = 1.0 + cluster_ratio
            fti = sign * log_mag * coherence * impact_asym * ofi_conc * clustering_boost

        self._record_fti(state, fti, bucket.end, cfg)
        percentile = self._compute_percentile(state, abs(fti))

        regime = self._regime.get_regime(pair)
        regime_mult = self._regime.get_regime_multiplier(pair)
        lp_bias = self._lp.get_lp_bias(pair)

        signal = self._decide_signal(
            pair, fti, percentile, regime, regime_mult, lp_bias, bucket.end
        )

        if signal is not None:
            flow_bucket = FlowBucket(
                pair_name=pair,
                bucket_start=bucket.start,
                bucket_end=bucket.end,
                flow_30=bucket.flow_30,
                flow_5=bucket.flow_5,
                flow_1=bucket.flow_1,
                volume_30=bucket.volume_30,
                volume_5=bucket.volume_5,
                volume_1=bucket.volume_1,
                price_move_30=self._price_move(bucket.first_price_30, bucket.last_price_30),
                price_move_5=self._price_move(bucket.first_price_5, bucket.last_price_5),
                price_move_1=self._price_move(bucket.first_price_1, bucket.last_price_1),
                event_count_30=bucket.count_30,
                event_count_5=bucket.count_5,
                event_count_1=bucket.count_1,
                ofi_30=ofi_30,
                ofi_5=ofi_5,
                ofi_1=ofi_1,
                cluster_ratio_30=cluster_ratio,
                cross_excitation=cross_exc,
            )
            await self._database.insert_flow_bucket(flow_bucket)

            logger.info(
                "Toxicity signal",
                pair=pair,
                fti=round(fti, 4),
                pctl=round(percentile, 2),
                regime=regime.value,
                lp_bias=round(lp_bias, 2),
                ofi_conc=round(ofi_conc, 2),
                cluster=round(cluster_ratio, 2),
                cross_exc=round(cross_exc, 2),
                transition=signal.transition.value,
            )
            await self._on_signal(signal)

    @staticmethod
    def _compute_coherence(
        bucket: _BucketAccumulator, cfg: ToxicityConfig
    ) -> float:
        signs = []
        has_flow = []
        for flow in (bucket.flow_30, bucket.flow_5, bucket.flow_1):
            if abs(flow) > 1e-9:
                signs.append(1 if flow > 0 else -1)
                has_flow.append(True)
            else:
                signs.append(0)
                has_flow.append(False)

        n_active = sum(has_flow)

        if n_active == 0:
            return 0.0

        # Only 30 bps has flow — isolated informed trading
        if has_flow[0] and not has_flow[1] and not has_flow[2]:
            return cfg.coherence_isolated_boost

        if n_active == 1:
            return cfg.coherence_aligned_boost

        # All active tiers agree on direction
        active_signs = [s for s, a in zip(signs, has_flow) if a]
        if len(set(active_signs)) == 1:
            return cfg.coherence_aligned_boost

        # 30 bps and 5 bps agree, 1 bps disagrees
        if has_flow[0] and has_flow[1] and signs[0] == signs[1]:
            return 0.5

        return 0.0

    @staticmethod
    def _compute_impact_asymmetry(bucket: _BucketAccumulator) -> float:
        """Ratio of price impact per dollar in 30 bps vs 5 bps pool.

        High ratio means someone is paying up for urgency in the expensive pool
        without proportional activity in the cheap pool — classic toxic flow.
        """
        impact_30 = 0.0
        if bucket.volume_30 > 0 and bucket.first_price_30 and bucket.last_price_30:
            impact_30 = abs(bucket.last_price_30 - bucket.first_price_30) / bucket.volume_30

        impact_5 = 0.0
        if bucket.volume_5 > 0 and bucket.first_price_5 and bucket.last_price_5:
            impact_5 = abs(bucket.last_price_5 - bucket.first_price_5) / bucket.volume_5

        if impact_5 > 1e-18:
            ratio = impact_30 / impact_5
            return min(ratio, 5.0)

        if impact_30 > 0:
            return 3.0

        return 1.0

    @staticmethod
    def _compute_ofi(up: int, down: int) -> float:
        total = up + down
        if total == 0:
            return 0.0
        return (up - down) / total

    @staticmethod
    def _compute_ofi_concentration(
        ofi_30: float, ofi_1: float, cfg: ToxicityConfig
    ) -> float:
        """How much more directional is the expensive pool vs. the cheap pool.

        High ratio means the 30 bps pool is strongly one-sided while the 1 bps
        pool is noisy — signature of informed flow isolated in the expensive tier.
        Returns 1.0 when no meaningful difference exists.
        """
        abs_ofi_1 = max(abs(ofi_1), 0.1)
        ratio = abs(ofi_30) / abs_ofi_1
        return min(ratio, cfg.ofi_concentration_cap)

    @staticmethod
    def _compute_cluster_ratio(
        timestamps: list[float], window_seconds: float
    ) -> float:
        """Fraction of 30 bps events that occur within *window_seconds* of
        another 30 bps event.  High ratio = burst arrival = informed order
        splitting.  This is a parameter-free proxy for Hawkes self-excitation.
        """
        n = len(timestamps)
        if n < 2:
            return 0.0
        clustered = 0
        for i in range(1, n):
            if timestamps[i] - timestamps[i - 1] <= window_seconds:
                clustered += 1
        return clustered / (n - 1)

    @staticmethod
    def _compute_cross_excitation(
        ts_30: list[float],
        ts_5: list[float],
        window_seconds: float,
    ) -> float:
        """Measures directional information flow from 30 bps → 5 bps.

        For each 30 bps event, check if a 5 bps event follows within *window*.
        Then do the reverse (5 bps → 30 bps).  Asymmetry = informed flow
        propagating from the expensive pool outward.

        Returns a value in [-1, 1]:
          +1 = purely 30bps→5bps (information flowing from expensive to cheap)
          -1 = purely 5bps→30bps (noise leading expensive — unlikely for informed)
           0 = symmetric or no data
        """
        if not ts_30 or not ts_5:
            return 0.0

        def _follow_count(source: list[float], target: list[float], w: float) -> int:
            count = 0
            j = 0
            for s in source:
                while j < len(target) and target[j] < s:
                    j += 1
                if j < len(target) and target[j] - s <= w:
                    count += 1
            return count

        fwd = _follow_count(ts_30, ts_5, window_seconds)
        rev = _follow_count(ts_5, ts_30, window_seconds)

        total = fwd + rev
        if total == 0:
            return 0.0
        return (fwd - rev) / total

    def _record_fti(
        self,
        state: _PairToxicityState,
        fti: float,
        ts: float,
        cfg: ToxicityConfig,
    ) -> None:
        abs_fti = abs(fti)
        state.fti_history.append(abs_fti)
        state.fti_timestamps.append(ts)
        bisect.insort(state.sorted_fti, abs_fti)

        cutoff = ts - cfg.trailing_hours * 3600
        while state.fti_timestamps and state.fti_timestamps[0] < cutoff:
            state.fti_timestamps.popleft()
            old_val = state.fti_history.popleft()
            idx = bisect.bisect_left(state.sorted_fti, old_val)
            if idx < len(state.sorted_fti) and state.sorted_fti[idx] == old_val:
                state.sorted_fti.pop(idx)

    @staticmethod
    def _compute_percentile(
        state: _PairToxicityState, abs_fti: float
    ) -> float:
        n = len(state.sorted_fti)
        if n < 2:
            return 0.5
        pos = bisect.bisect_right(state.sorted_fti, abs_fti)
        return pos / n

    def _decide_signal(
        self,
        pair: str,
        fti: float,
        percentile: float,
        regime: Regime,
        regime_mult: float,
        lp_bias: float,
        timestamp: float,
    ) -> Optional[ToxicitySignal]:
        state = self._states[pair]
        cfg = self._tox_configs[pair]

        direction = Direction.LONG if fti > 0 else Direction.SHORT
        coherence_val = percentile

        if not state.has_position and abs(fti) < 1e-9:
            return None

        if state.has_position:
            if percentile < cfg.exit_fti_percentile:
                state.has_position = False
                state.position_direction = Direction.NEUTRAL
                return ToxicitySignal(
                    pair_name=pair,
                    timestamp=timestamp,
                    fti=fti,
                    fti_percentile=percentile,
                    direction=state.position_direction,
                    regime=regime,
                    lp_bias=lp_bias,
                    coherence=coherence_val,
                    transition=SignalTransition.EXIT,
                    regime_multiplier=regime_mult,
                )

            if direction != state.position_direction and percentile >= cfg.fti_percentile_threshold:
                state.position_direction = direction
                state.last_entry_fti = fti
                return ToxicitySignal(
                    pair_name=pair,
                    timestamp=timestamp,
                    fti=fti,
                    fti_percentile=percentile,
                    direction=direction,
                    regime=regime,
                    lp_bias=lp_bias,
                    coherence=coherence_val,
                    transition=SignalTransition.REVERSAL,
                    regime_multiplier=regime_mult,
                )

            return None

        if percentile < cfg.fti_percentile_threshold:
            return None

        if regime == Regime.CHAOTIC:
            return None

        # LP bias agreement: negative LP bias (LPs pulling asks) agrees with
        # LONG; positive LP bias (LPs pulling bids) agrees with SHORT.
        # If LP bias is zero (no data), skip this check.
        if abs(lp_bias) > 1e-6:
            if direction == Direction.LONG and lp_bias > 0:
                return None
            if direction == Direction.SHORT and lp_bias < 0:
                return None

        state.has_position = True
        state.position_direction = direction
        state.last_entry_fti = fti

        return ToxicitySignal(
            pair_name=pair,
            timestamp=timestamp,
            fti=fti,
            fti_percentile=percentile,
            direction=direction,
            regime=regime,
            lp_bias=lp_bias,
            coherence=coherence_val,
            transition=SignalTransition.ENTRY,
            regime_multiplier=regime_mult,
        )

    def notify_position_closed(self, pair_name: str) -> None:
        state = self._states.get(pair_name)
        if state:
            state.has_position = False
            state.position_direction = Direction.NEUTRAL

    @staticmethod
    def _price_move(
        first: Optional[float], last: Optional[float]
    ) -> float:
        if first and last and first > 0:
            return (last - first) / first
        return 0.0

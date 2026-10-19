"""Causal aggregation of intraday candles into the daily context the THURISAZ filter needs.

The filter must never see a daily bar that has not closed yet, so for every
intraday bar ``i`` we look up the daily state as it stood at the close of the
**last completed UTC day strictly before** ``i``'s own day. A partial first or
last day is discarded rather than silently producing a wrong high/low.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from nornweave_forex.config import DAY_MS, ConfigError, StrategyParams, bars_per_day, interval_ms
from nornweave_forex.indicators.core import ema
from nornweave_forex.indicators.swings import sliding_pivots
from nornweave_forex.strategy.dow import BreakoutDow, DowTracker
from nornweave_forex.strategy.fibonacci import FibLeg
from nornweave_forex.types import CandleArrays, DowTrend

__all__ = ["DailyState", "DailyContext", "build_daily_context", "aggregate_daily"]


@dataclass(frozen=True, slots=True)
class DailyState:
    """Everything the daily overlay knows at the close of one completed UTC day."""

    ts: int
    dow: DowTrend
    ema_slope: int  # +1 rising, -1 falling, 0 flat / unknown
    leg: FibLeg | None

    @property
    def trend(self) -> DowTrend:
        return self.dow


@dataclass(slots=True)
class DailyContext:
    """Daily bars plus the per-intraday-bar lookup table."""

    daily: CandleArrays
    states: list[DailyState]
    complete_mask: np.ndarray
    bar_to_state: np.ndarray  # index into `states`, or -1 when no completed day precedes the bar

    def __len__(self) -> int:
        return int(self.bar_to_state.shape[0])

    def state_for_bar(self, index: int) -> DailyState | None:
        slot = int(self.bar_to_state[index])
        if slot < 0:
            return None
        return self.states[slot]

    @property
    def completed_days(self) -> int:
        return int(self.complete_mask.sum())


def aggregate_daily(candles: CandleArrays, interval: str) -> tuple[CandleArrays, np.ndarray, np.ndarray]:
    """Group intraday bars into UTC calendar days.

    Returns ``(daily, expected_bars_per_day, complete_mask)`` where
    ``complete_mask[d]`` marks days whose bar count matches expectation.
    """
    if len(candles) == 0:
        empty = CandleArrays(
            ts=np.empty(0, np.int64),
            open=np.empty(0),
            high=np.empty(0),
            low=np.empty(0),
            close=np.empty(0),
            volume=np.empty(0),
        )
        return empty, np.empty(0), np.zeros(0, dtype=bool)

    expected = bars_per_day(interval)
    if expected < 1:
        raise ConfigError(
            f"interval {interval!r} is longer than a UTC day; the THURISAZ daily filter "
            "cannot be built from it"
        )

    day_of_bar = candles.ts // DAY_MS
    change = np.flatnonzero(np.diff(day_of_bar)) + 1
    starts = np.concatenate(([0], change))
    stops = np.concatenate((change, [len(candles)]))
    counts = stops - starts

    daily = CandleArrays(
        ts=(day_of_bar[starts] * DAY_MS).astype(np.int64),
        open=candles.open[starts].astype(np.float64),
        high=np.maximum.reduceat(candles.high, starts).astype(np.float64),
        low=np.minimum.reduceat(candles.low, starts).astype(np.float64),
        close=candles.close[stops - 1].astype(np.float64),
        volume=np.add.reduceat(candles.volume, starts).astype(np.float64),
    )
    complete = counts == int(round(expected))
    return daily, counts.astype(np.int64), complete


def _range_leg(daily: CandleArrays, index: int, lookback: int) -> FibLeg | None:
    """Swing leg from the extremes of the last ``lookback`` completed daily bars."""
    start = max(0, index - lookback + 1)
    window_high = daily.high[start : index + 1]
    window_low = daily.low[start : index + 1]
    if window_high.size < 3:
        return None
    hi = int(np.argmax(window_high))
    lo = int(np.argmin(window_low))
    if hi == lo:
        return None
    if hi > lo:  # the high happened later -> the current leg runs up
        first, last = float(window_low[lo]), float(window_high[hi])
    else:
        first, last = float(window_high[hi]), float(window_low[lo])
    if first == last:
        return None
    return FibLeg.build(first, last, 0.382, 0.618)


def build_daily_context(
    candles: CandleArrays,
    interval: str,
    strategy: StrategyParams,
) -> DailyContext:
    """Precompute the daily overlay for every intraday bar, strictly causally."""
    daily, _counts, complete = aggregate_daily(candles, interval)
    n_daily = len(daily)
    states: list[DailyState] = []

    if n_daily == 0:
        return DailyContext(
            daily=daily,
            states=states,
            complete_mask=complete,
            bar_to_state=np.full(len(candles), -1, dtype=np.int64),
        )

    ema_vals = ema(daily.close, strategy.resolved_daily_ema_period)
    focus = strategy.daily_focus_level
    pivots = sliding_pivots(daily.high, daily.low, focus, focus, strict=strategy.pivot_strict)
    delay = strategy.daily_pivot_delay
    breakout = strategy.dow_mode == "breakout"
    tracker = (
        BreakoutDow(pivots, delay=delay)
        if breakout
        else DowTracker(persist=strategy.dow_neutral_mode == "persist")
    )

    for d in range(n_daily):
        if breakout:
            tracker.update(d, float(daily.close[d]))
        else:
            tracker.update(pivots, d, delay=delay)

        slope = 0
        if d > 0 and np.isfinite(ema_vals[d]) and np.isfinite(ema_vals[d - 1]):
            if ema_vals[d] > ema_vals[d - 1]:
                slope = 1
            elif ema_vals[d] < ema_vals[d - 1]:
                slope = -1

        if strategy.daily_fib_mode == "range":
            leg = _range_leg(daily, d, strategy.daily_range_lookback)
            if leg is not None:
                leg = FibLeg.build(
                    leg.start, leg.end, strategy.fib_shallow, strategy.fib_deep
                )
        else:
            raw = tracker.leg()
            if raw is None:
                leg = None
            else:
                first, last, _direction = raw
                leg = FibLeg.build(first, last, strategy.fib_shallow, strategy.fib_deep)

        states.append(DailyState(ts=int(daily.ts[d]), dow=tracker.trend, ema_slope=slope, leg=leg))

    # Map each intraday bar to the last *completed* day strictly before its own day.
    complete_positions = np.flatnonzero(complete)
    bar_to_state = np.full(len(candles), -1, dtype=np.int64)
    if complete_positions.size:
        complete_ts = daily.ts[complete_positions]
        day_start = (candles.ts // DAY_MS) * DAY_MS
        pos = np.searchsorted(complete_ts, day_start, side="left") - 1
        valid = pos >= 0
        if valid.any():
            bar_to_state[valid] = complete_positions[pos[valid]]

    return DailyContext(
        daily=daily,
        states=states,
        complete_mask=complete,
        bar_to_state=bar_to_state,
    )


def resolve_daily_trend(state: DailyState, mode: str) -> DowTrend:
    """Combine the daily Dow read and the daily EMA slope per ``daily_trend_mode``."""
    if mode == "dow":
        return state.dow
    if mode == "ema":
        if state.ema_slope > 0:
            return DowTrend.BULLISH
        if state.ema_slope < 0:
            return DowTrend.BEARISH
        return DowTrend.NEUTRAL
    # dow_then_ema
    if state.dow is not DowTrend.NEUTRAL:
        return state.dow
    if state.ema_slope > 0:
        return DowTrend.BULLISH
    if state.ema_slope < 0:
        return DowTrend.BEARISH
    return DowTrend.NEUTRAL


def interval_guard(interval: str) -> int:
    """Small helper kept next to the aggregation logic for validation messages."""
    return interval_ms(interval)

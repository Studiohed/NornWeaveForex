"""Dow-theory structure read: higher highs & higher lows vs lower highs & lower lows.

The structure is built exclusively from *confirmed* pivots. A pivot on bar ``i``
only becomes part of the structure once bar ``i + right`` has closed, which is
what ``DowTracker.update`` enforces.
"""

from __future__ import annotations

from typing import Iterable

import numpy as np

from nornweave_forex.indicators.swings import Pivots, sliding_pivots
from nornweave_forex.types import DowTrend

__all__ = ["DowTracker", "BreakoutDow", "dow_trend_series", "PivotPoint"]

DOW_MODES = ("structure", "breakout")

#: Numeric encoding used by :func:`dow_trend_series` (1 bullish, -1 bearish, 0 neutral).
TREND_CODES = {DowTrend.BULLISH: 1, DowTrend.BEARISH: -1, DowTrend.NEUTRAL: 0}


class PivotPoint:
    """A confirmed swing point."""

    __slots__ = ("bar", "price", "kind")

    def __init__(self, bar: int, price: float, kind: str) -> None:
        self.bar = int(bar)
        self.price = float(price)
        self.kind = kind  # "high" | "low"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PivotPoint(bar={self.bar}, price={self.price:.8g}, kind={self.kind!r})"


class DowTracker:
    """Incrementally maintains the confirmed pivot sequence and the trend read.

    Feed it the *pivot event* for the bar that just became confirmable::

        tracker.publish(bar=i - right, price=high[i - right], kind="high")

    ``update`` wraps this for a full-array pass.

    When ``persist`` is set, an ambiguous (neutral) structure does **not** reset
    the trend: the last decisive read is held until the pivots actually confirm a
    reversal. That is how a Pine ``var trend`` behaves, and it blocks far fewer
    bars than treating neutral as a veto.
    """

    __slots__ = (
        "highs",
        "lows",
        "trend",
        "raw",
        "persist",
        "last_reversal_bar",
        "last_reversal_from",
    )

    def __init__(self, persist: bool = False) -> None:
        self.highs: list[PivotPoint] = []
        self.lows: list[PivotPoint] = []
        self.trend: DowTrend = DowTrend.NEUTRAL
        self.raw: DowTrend = DowTrend.NEUTRAL
        self.persist: bool = bool(persist)
        self.last_reversal_bar: int = -1
        self.last_reversal_from: DowTrend = DowTrend.NEUTRAL

    # -- mutation ----------------------------------------------------------- #
    def publish(self, bar: int, price: float, kind: str, confirm_bar: int) -> DowTrend:
        """Register a confirmed pivot and recompute the trend."""
        point = PivotPoint(bar, price, kind)
        if kind == "high":
            self.highs.append(point)
        elif kind == "low":
            self.lows.append(point)
        else:
            raise ValueError(f"kind must be 'high' or 'low', got {kind!r}")

        previous = self.trend
        self.raw = self._read()
        if not (self.persist and self.raw is DowTrend.NEUTRAL):
            self.trend = self.raw
        if self.trend is not previous and self.trend is not DowTrend.NEUTRAL:
            self.last_reversal_bar = confirm_bar
            self.last_reversal_from = previous
        return self.trend

    def _read(self) -> DowTrend:
        """HH & HL -> bullish; LH & LL -> bearish; anything else -> neutral."""
        if len(self.highs) < 2 or len(self.lows) < 2:
            return DowTrend.NEUTRAL
        h_last, h_prev = self.highs[-1].price, self.highs[-2].price
        l_last, l_prev = self.lows[-1].price, self.lows[-2].price
        if h_last > h_prev and l_last > l_prev:
            return DowTrend.BULLISH
        if h_last < h_prev and l_last < l_prev:
            return DowTrend.BEARISH
        return DowTrend.NEUTRAL

    # -- convenience -------------------------------------------------------- #
    @property
    def last_high(self) -> PivotPoint | None:
        return self.highs[-1] if self.highs else None

    @property
    def last_low(self) -> PivotPoint | None:
        return self.lows[-1] if self.lows else None

    def leg(self) -> tuple[float, float, int] | None:
        """The most recent confirmed swing leg as ``(start_price, end_price, direction)``.

        ``direction`` is ``+1`` for an up leg and ``-1`` for a down leg. ``None``
        until one confirmed high and one confirmed low exist.
        """
        if not self.highs or not self.lows:
            return None
        h, l = self.highs[-1], self.lows[-1]
        if h.bar > l.bar:
            return l.price, h.price, 1
        return h.price, l.price, -1

    def update(self, pivots: Pivots, index: int, *, delay: int = 0) -> DowTrend:
        """Confirm whatever pivot becomes knowable at the close of bar ``index``."""
        event = pivots.event_at(index, delay)
        if event is not None:
            bar, price, kind = event
            return self.publish(bar, price, kind, confirm_bar=index)
        return self.trend

    def clone(self) -> "DowTracker":
        other = DowTracker(persist=self.persist)
        other.highs = [PivotPoint(p.bar, p.price, p.kind) for p in self.highs]
        other.lows = [PivotPoint(p.bar, p.price, p.kind) for p in self.lows]
        other.trend = self.trend
        other.raw = self.raw
        other.last_reversal_bar = self.last_reversal_bar
        other.last_reversal_from = self.last_reversal_from
        return other


def dow_trend_series(
    high,
    low,
    left: int,
    right: int,
    *,
    mode: str = "structure",
    close=None,
    persist: bool = False,
    strict: bool = True,
    pivots: Pivots | None = None,
    reversals: list[int] | None = None,
) -> np.ndarray:
    """Trend read as *known at the close of each bar*, encoded as 1/0/-1.

    Two genuinely different reads live behind this one function:

    * ``mode="structure"`` — the Dow-theory comparison of consecutive confirmed
      pivots (higher high + higher low vs lower high + lower low). ``persist=True``
      holds the last decisive read through ambiguous stretches.
    * ``mode="breakout"`` — what the FEHU Pine source actually does: a persistent
      ``var`` trend that flips only when price *closes* through the most recent
      confirmed swing level. Requires ``close``.

    ``reversals``, when supplied, is filled with the bar index at which each
    reversal became known.
    """
    high = np.asarray(high, dtype=np.float64)
    low = np.asarray(low, dtype=np.float64)
    n = high.size
    out = np.zeros(n, dtype=np.int8)
    piv = pivots if pivots is not None else sliding_pivots(high, low, left, right, strict=strict)
    last_rev = -1

    if mode == "breakout":
        if close is None:
            raise ValueError("dow mode 'breakout' needs the close series")
        close = np.asarray(close, dtype=np.float64)
        breaker = BreakoutDow(piv)
        for i in range(n):
            breaker.update(i, float(close[i]))
            out[i] = TREND_CODES[breaker.trend]
            if reversals is not None and breaker.last_reversal_bar != last_rev:
                last_rev = breaker.last_reversal_bar
                if last_rev >= 0:
                    reversals.append(last_rev)
        return out

    if mode != "structure":
        raise ValueError(f"unknown dow mode {mode!r}; expected one of {DOW_MODES}")

    tracker = DowTracker(persist=persist)
    for i in range(n):
        tracker.update(piv, i)
        out[i] = TREND_CODES[tracker.trend]
        if reversals is not None and tracker.last_reversal_bar != last_rev:
            last_rev = tracker.last_reversal_bar
            if last_rev >= 0:
                reversals.append(last_rev)
    return out


class BreakoutDow:
    """The Dow read the FEHU Pine source actually implements.

    Translated line for line from::

        lastSwingHigh := isSwingHigh ? high[swingLen] : lastSwingHigh
        lastSwingLow  := isSwingLow  ? low[swingLen]  : lastSwingLow
        var int trend = 0
        if not na(lastSwingHigh) and close > lastSwingHigh and trend != 1
            trend := 1
        if not na(lastSwingLow)  and close < lastSwingLow  and trend != -1
            trend := -1

    The trend is a persistent variable: it never returns to neutral once set, and it
    flips only when price **closes** through the most recent confirmed swing level.
    That is a breakout read, not a higher-high / higher-low comparison, and it is the
    single largest behavioural difference between this project's first
    reconstruction and the original.
    """

    __slots__ = (
        "pivots",
        "delay",
        "last_high",
        "last_low",
        "last_high_bar",
        "last_low_bar",
        "trend",
        "last_reversal_bar",
    )

    def __init__(self, pivots: Pivots, delay: int = 0) -> None:
        self.pivots = pivots
        self.delay = int(delay)
        self.last_high: float | None = None
        self.last_low: float | None = None
        self.last_high_bar: int = -1
        self.last_low_bar: int = -1
        self.trend: DowTrend = DowTrend.NEUTRAL
        self.last_reversal_bar: int = -1

    def update(self, index: int, close: float) -> DowTrend:
        """Advance one bar. Pine updates the swing levels *before* testing the trend."""
        event = self.pivots.event_at(index, self.delay)
        if event is not None:
            bar, price, kind = event
            if kind == "high":
                self.last_high, self.last_high_bar = price, bar
            else:
                self.last_low, self.last_low_bar = price, bar

        previous = self.trend
        if self.last_high is not None and close > self.last_high and self.trend is not DowTrend.BULLISH:
            self.trend = DowTrend.BULLISH
        if self.last_low is not None and close < self.last_low and self.trend is not DowTrend.BEARISH:
            self.trend = DowTrend.BEARISH
        if self.trend is not previous:
            self.last_reversal_bar = index
        return self.trend

    def leg(self) -> tuple[float, float, int] | None:
        """Most recent confirmed swing leg as ``(start_price, end_price, direction)``."""
        if self.last_high is None or self.last_low is None:
            return None
        if self.last_high_bar > self.last_low_bar:
            return self.last_low, self.last_high, 1
        return self.last_high, self.last_low, -1

    @property
    def code(self) -> int:
        return TREND_CODES[self.trend]

"""THURISAZ daily-Fibonacci retracement filter.

Geometry
--------
A daily swing leg runs from ``leg_start`` to ``leg_end``. The *depth* of the
current retracement is measured **from the leg's end**, normalised by the leg's
range::

    depth = |price - leg_end| / |leg_end - leg_start|

* ``depth <  0.382`` -> SHALLOW: the retracement has barely started, price sits
  near the leg's most recent extreme.
* ``0.382 <= depth <= 0.618`` -> MID: the "landing zone" where pullbacks
  frequently stall and reverse. **Entry is forbidden here.**
* ``depth >  0.618`` -> DEEP: price has retraced most of the leg; a reversal
  area. Entry is allowed with the standard ATR targets.

Retracement levels are offset **against** the leg direction, so for an up leg
``0.382`` sits below the high, and for a down leg it sits above the low::

    level(p) = leg_end - leg_direction * p * |leg_end - leg_start|

Note that the daily filter is only consulted when the intraday trend *diverges*
from the daily trend (see :mod:`nornweave_forex.strategy.engine`). In that
counter-trend case the leg end is always the extreme the bounce/retracement
started from, so the 0.382 level is a valid (if modest) target in the trade's
direction rather than a level behind the entry.
"""

from __future__ import annotations

from dataclasses import dataclass

from nornweave_forex.types import Zone

__all__ = ["FibLeg", "Zone", "zone_for_depth", "evaluate_fib"]


@dataclass(frozen=True, slots=True)
class FibLeg:
    """A resolved swing leg plus the Fibonacci levels derived from it."""

    start: float
    end: float
    direction: int  # +1 up leg, -1 down leg
    level_shallow: float  # the 0.382 retracement level
    level_deep: float  # the 0.618 retracement level

    @property
    def range(self) -> float:
        return abs(self.end - self.start)

    @classmethod
    def build(cls, start: float, end: float, shallow: float = 0.382, deep: float = 0.618) -> "FibLeg":
        if end == start:
            raise ValueError("a Fibonacci leg must have a non-zero range")
        direction = 1 if end > start else -1
        rng = abs(end - start)
        return cls(
            start=float(start),
            end=float(end),
            direction=direction,
            level_shallow=float(end) - direction * shallow * rng,
            level_deep=float(end) - direction * deep * rng,
        )

    def depth(self, price: float) -> float:
        """Retracement depth in ``[0, 1]``; values above 1 are clamped.

        A price beyond the *start* of the leg means the whole leg was retraced,
        which the strategy treats as the deepest possible bucket.
        """
        d = abs(float(price) - self.end) / self.range
        return float(min(max(d, 0.0), 1.0))

    def level(self, ratio: float) -> float:
        return self.end - self.direction * ratio * self.range


def zone_for_depth(depth: float, shallow: float = 0.382, deep: float = 0.618) -> Zone:
    """Classify a retracement depth into the brief's three zones."""
    if depth < shallow:
        return Zone.SHALLOW
    if depth <= deep:
        return Zone.MID
    return Zone.DEEP


def evaluate_fib(
    leg: FibLeg,
    price: float,
    shallow: float = 0.382,
    deep: float = 0.618,
) -> tuple[float, Zone]:
    """Return ``(depth, zone)`` for ``price`` against ``leg``."""
    depth = leg.depth(price)
    return depth, zone_for_depth(depth, shallow, deep)

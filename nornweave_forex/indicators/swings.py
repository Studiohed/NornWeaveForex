"""Swing (pivot) detection driven by the strategy's *Focus Level*.

A bar ``i`` is a pivot high when its high is **strictly** greater than each of
the ``left`` bars before it and each of the ``right`` bars after it. A pivot is
therefore only *knowable* ``right`` bars later — the backtester and the live
engine both respect that delay, which is what keeps the strategy free of
look-ahead bias.

The strategy maps ``Focus Level`` onto ``left`` and ``right`` symmetrically,
matching the usual ``ta.pivothigh(focus, focus)`` idiom.
"""

from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

__all__ = ["pivot_high_mask", "pivot_low_mask", "sliding_pivots", "Pivots"]


def _validate(left: int, right: int) -> None:
    if left < 1 or right < 1:
        raise ValueError("left and right must both be >= 1")


def pivot_high_mask(high, left: int, right: int, *, strict: bool = True) -> np.ndarray:
    """Boolean mask that is ``True`` on bars that are pivot highs.

    ``strict=True`` requires the pivot to dominate every bar in the window.
    ``strict=False`` allows ties, which is what ``high[p] == ta.highest(high, 2r+1)``
    in Pine actually tests — used by the FEHU port.
    """
    _validate(left, right)
    h = np.asarray(high, dtype=np.float64)
    n = h.size
    mask = np.zeros(n, dtype=bool)
    window = left + right + 1
    if n < window:
        return mask

    win = sliding_window_view(h, window)
    centers = win[:, left]
    if strict:
        left_ok = (centers[:, None] > win[:, :left]).all(axis=1)
        right_ok = (centers[:, None] > win[:, left + 1 :]).all(axis=1)
    else:
        left_ok = (centers[:, None] >= win[:, :left]).all(axis=1)
        right_ok = (centers[:, None] >= win[:, left + 1 :]).all(axis=1)
    mask[left : left + win.shape[0]] = left_ok & right_ok
    return mask


def pivot_low_mask(low, left: int, right: int, *, strict: bool = True) -> np.ndarray:
    """Boolean mask that is ``True`` on bars that are pivot lows."""
    _validate(left, right)
    lo = np.asarray(low, dtype=np.float64)
    n = lo.size
    mask = np.zeros(n, dtype=bool)
    window = left + right + 1
    if n < window:
        return mask

    win = sliding_window_view(lo, window)
    centers = win[:, left]
    if strict:
        left_ok = (centers[:, None] < win[:, :left]).all(axis=1)
        right_ok = (centers[:, None] < win[:, left + 1 :]).all(axis=1)
    else:
        left_ok = (centers[:, None] <= win[:, :left]).all(axis=1)
        right_ok = (centers[:, None] <= win[:, left + 1 :]).all(axis=1)
    mask[left : left + win.shape[0]] = left_ok & right_ok
    return mask


class Pivots:
    """Random-access helper over the two pivot masks."""

    __slots__ = ("_high_mask", "_low_mask", "_high", "_low", "right")

    def __init__(self, high, low, left: int, right: int, *, strict: bool = True) -> None:
        self._high = np.asarray(high, dtype=np.float64)
        self._low = np.asarray(low, dtype=np.float64)
        self._high_mask = pivot_high_mask(self._high, left, right, strict=strict)
        self._low_mask = pivot_low_mask(self._low, left, right, strict=strict)
        self.right = int(right)

    @property
    def high_mask(self) -> np.ndarray:
        return self._high_mask

    @property
    def low_mask(self) -> np.ndarray:
        return self._low_mask

    def is_pivot_high(self, index: int) -> bool:
        return bool(self._high_mask[index])

    def is_pivot_low(self, index: int) -> bool:
        return bool(self._low_mask[index])

    def price_at(self, index: int) -> float:
        """Price of the pivot registered on ``index`` (high if both masks agree, else the set one)."""
        if self._high_mask[index]:
            return float(self._high[index])
        return float(self._low[index])

    def event_at(self, confirm_index: int, delay: int = 0) -> tuple[int, float, str] | None:
        """Pivot becoming known at ``confirm_index``.

        Returns ``(pivot_bar, price, "high" | "low")`` or ``None``. ``pivot_bar`` is
        ``confirm_index - right - delay``; THURISAZ reads daily pivots with
        ``ta.pivothigh(...)[1]``, i.e. one extra bar of delay.
        """
        pivot_bar = confirm_index - self.right - int(delay)
        if pivot_bar < 0:
            return None
        if self._high_mask[pivot_bar]:
            return pivot_bar, float(self._high[pivot_bar]), "high"
        if self._low_mask[pivot_bar]:
            return pivot_bar, float(self._low[pivot_bar]), "low"
        return None


def sliding_pivots(high, low, left: int, right: int, *, strict: bool = True) -> Pivots:
    """Convenience constructor."""
    return Pivots(high, low, left, right, strict=strict)

"""EMA, Wilder ATR and Wilder ADX.

Implemented in NumPy and verified against hand-computed reference values in
``tests/test_indicators.py``. Seeding matches Pine Script's ``ta.ema``/``ta.rma``
(seed = simple mean of the first ``period`` observations), so an equivalent
TradingView script produces the same numbers.

Conventions
-----------
* Series are 1-D ``float64`` arrays ordered oldest -> newest.
* ``NaN`` means "not enough history yet".
* ``true_range`` / ``dmi`` return a defined value for bar 0, exactly like
  Pine's ``ta.tr(true)`` / ``ta.dmi`` which treat the first bar's range as
  ``high - low``.
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "ema",
    "rma",
    "sma",
    "true_range",
    "atr",
    "dmi",
    "adx",
    "rising_streak",
    "falling_streak",
    "rolling_percentile_nearest_rank",
]


def _as_f64(values) -> np.ndarray:
    arr = np.asarray(values, dtype=np.float64)
    if arr.ndim != 1:
        raise ValueError(f"expected a 1-D series, got shape {arr.shape}")
    return arr


def rma(values, period: int) -> np.ndarray:
    """Wilder's smoothing (RMA / SMMA / Wilder moving average).

    Handles a leading NaN run, which is what the ADX calculation needs: the
    seed is taken as the simple mean of the first ``period`` *finite* values.
    """
    values = _as_f64(values)
    n = values.size
    out = np.full(n, np.nan, dtype=np.float64)
    period = int(period)
    if period < 1 or n < period:
        return out

    finite = np.flatnonzero(np.isfinite(values))
    if finite.size < period:
        return out
    start = int(finite[0])
    if int(finite[period - 1]) != start + period - 1:
        return out  # a NaN hole in the seed window: refuse rather than guess

    out[start + period - 1] = float(values[start : start + period].mean())
    prev = out[start + period - 1]
    alpha = 1.0 / period
    for i in range(start + period, n):
        v = values[i]
        if np.isfinite(v):
            prev = prev + alpha * (v - prev)
            out[i] = prev
        else:
            out[i] = prev
    return out


def ema(values, period: int) -> np.ndarray:
    """Exponential moving average seeded with the SMA of the first ``period`` bars."""
    values = _as_f64(values)
    n = values.size
    out = np.full(n, np.nan, dtype=np.float64)
    period = int(period)
    if period < 1 or n < period:
        return out

    alpha = 2.0 / (period + 1.0)
    prev = float(values[:period].mean())
    out[period - 1] = prev
    for i in range(period, n):
        prev = prev + alpha * (float(values[i]) - prev)
        out[i] = prev
    return out


def true_range(high, low, close) -> np.ndarray:
    """Wilder's true range. Bar 0 falls back to ``high - low`` (Pine convention)."""
    high = _as_f64(high)
    low = _as_f64(low)
    close = _as_f64(close)
    if not (high.size == low.size == close.size):
        raise ValueError("high/low/close must have the same length")

    prev_close = np.empty_like(close)
    prev_close[0] = close[0]
    prev_close[1:] = close[:-1]
    return np.maximum.reduce(
        [high - low, np.abs(high - prev_close), np.abs(low - prev_close)]
    )


def atr(high, low, close, period: int = 14) -> np.ndarray:
    """Wilder's Average True Range."""
    return rma(true_range(high, low, close), period)


def dmi(high, low, close, period: int = 14) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return ``(plus_di, minus_di, dx)`` for Wilder's DMI.

    The first bar has no previous bar, so both directional movements are 0.
    """
    high = _as_f64(high)
    low = _as_f64(low)
    close = _as_f64(close)
    if not (high.size == low.size == close.size):
        raise ValueError("high/low/close must have the same length")

    n = high.size
    plus_dm = np.zeros(n, dtype=np.float64)
    minus_dm = np.zeros(n, dtype=np.float64)
    if n > 1:
        up = high[1:] - high[:-1]
        down = low[:-1] - low[1:]
        plus_dm[1:] = np.where((up > down) & (up > 0.0), up, 0.0)
        minus_dm[1:] = np.where((down > up) & (down > 0.0), down, 0.0)

    tr_rma = rma(true_range(high, low, close), period)
    with np.errstate(invalid="ignore", divide="ignore"):
        plus_di = np.where(tr_rma > 0, 100.0 * rma(plus_dm, period) / tr_rma, np.nan)
        minus_di = np.where(tr_rma > 0, 100.0 * rma(minus_dm, period) / tr_rma, np.nan)
        denom = plus_di + minus_di
        dx = np.where(denom > 0, 100.0 * np.abs(plus_di - minus_di) / denom, np.nan)
    return plus_di, minus_di, dx


def adx(high, low, close, period: int = 14) -> np.ndarray:
    """Wilder's Average Directional Index. First finite value at index ``2*period - 2``."""
    _, _, dx = dmi(high, low, close, period)
    return rma(dx, period)


def _step_streak(values: np.ndarray, *, rising: bool) -> np.ndarray:
    values = _as_f64(values)
    n = values.size
    out = np.zeros(n, dtype=np.int64)
    for i in range(1, n):
        a, b = values[i], values[i - 1]
        if not (np.isfinite(a) and np.isfinite(b)):
            out[i] = 0
            continue
        if (a > b) if rising else (a < b):
            out[i] = out[i - 1] + 1
        else:
            out[i] = 0
    return out


def rising_streak(values) -> np.ndarray:
    """Consecutive strictly-increasing steps ending at each index.

    ``ta.rising(src, n)`` is exactly ``rising_streak(src)[i] >= n``, which is
    *not* the same as ``src[i] > src[i - n]`` for ``n > 1``.
    """
    return _step_streak(values, rising=True)


def falling_streak(values) -> np.ndarray:
    """Consecutive strictly-decreasing steps ending at each index."""
    return _step_streak(values, rising=False)


def sma(values, period: int) -> np.ndarray:
    """Simple moving average; NaN until ``period`` observations exist."""
    values = _as_f64(values)
    n = values.size
    out = np.full(n, np.nan, dtype=np.float64)
    period = int(period)
    if period < 1 or n < period:
        return out
    finite = np.isfinite(values).astype(np.float64)
    if finite.sum() < period:
        return out
    cumulative = np.concatenate(([0.0], np.cumsum(np.where(np.isfinite(values), values, 0.0))))
    counts = np.concatenate(([0.0], np.cumsum(finite)))
    window_sum = cumulative[period:] - cumulative[:-period]
    window_count = counts[period:] - counts[:-period]
    with np.errstate(invalid="ignore", divide="ignore"):
        means = np.where(window_count == period, window_sum / period, np.nan)
    out[period - 1 :] = means
    return out


def rolling_percentile_nearest_rank(values, length: int, percentage: float) -> np.ndarray:
    """Pine's ``ta.percentile_nearest_rank``.

    Sorts the last ``length`` values and takes the one at rank
    ``ceil(percentage / 100 * length)`` (1-based). A window containing ``NaN``
    yields ``NaN``, matching Pine's behaviour on a not-yet-ready series.
    """
    values = _as_f64(values)
    n = values.size
    out = np.full(n, np.nan, dtype=np.float64)
    length = int(length)
    if length < 1 or n < length:
        return out
    rank = int(np.ceil(float(percentage) / 100.0 * length))
    rank = min(max(rank, 1), length)

    for i in range(length - 1, n):
        window = values[i - length + 1 : i + 1]
        if not np.all(np.isfinite(window)):
            continue
        out[i] = np.sort(window)[rank - 1]
    return out

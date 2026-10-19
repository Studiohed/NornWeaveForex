"""Synthetic H2 candles for the demo run and for tests that need a realistic series.

Forex-specific rather than crypto-specific:

* **Sessions, not 24/7.** Bars are generated on the interval grid from a Monday
  00:00 UTC open and skip Saturday/Sunday, so the series contains real weekend
  gaps (Friday 22:00 -> Monday 00:00 for H2) that the backtester and the live
  feed both see.
* **Gaps.** The first bar after a weekend opens away from the previous close,
  which is what makes gap-through-stop behaviour reachable in tests.
* **Regimes.** A four-phase cycle (trend up / chop / trend down / chop) gives the
  Dow structure, ADX and ATR filters something to bite on, so the demo actually
  opens and closes positions rather than sitting flat.
* **Deterministic per symbol.** The RNG seed is derived from the symbol name, so
  EURUSD and USDJPY get different paths while every run of the same symbol is
  byte-identical.
* **Digits.** OHLC is rounded to the pair's ``digits`` (5 for FX, 3 for JPY) and
  the high/low ordering is repaired after rounding, so a bar is always a legal
  bar.

The output is a :class:`~nornweave_forex.types.CandleArrays`, i.e. exactly what
the backtester and the CLI consume.
"""

from __future__ import annotations

import zlib

import numpy as np

from nornweave_forex.types import CandleArrays

__all__ = ["synthetic_candles"]

_DAY_MS = 86_400_000
#: Python weekdays on which the FX market is closed.
_WEEKEND = (5, 6)


def _symbol_seed(symbol: str, seed: int) -> int:
    """Deterministic per-symbol RNG seed (``hash()`` is salted per process)."""
    checksum = zlib.crc32(str(symbol).upper().encode("ascii", errors="ignore"))
    return (int(seed) * 1_000_003 + int(checksum)) & 0x7FFFFFFF


def _python_weekday(day_index: int) -> int:
    """Python weekday for a day index counted from 1970-01-01 (a Thursday)."""
    return (int(day_index) + 3) % 7


def _session_timestamps(bars: int, interval_minutes: int, start_ts: int) -> np.ndarray:
    """Bar-open timestamps on the interval grid, skipping the FX weekend."""
    step = int(interval_minutes) * 60_000
    if step <= 0:
        raise ValueError("interval_minutes must be positive")
    # Align to the interval grid, then to the next Monday 00:00 UTC.
    ts = (int(start_ts) // step) * step
    day_index = ts // _DAY_MS
    days_to_monday = (4 - day_index) % 7  # 1970-01-01 was Thursday, so day 4 is Monday
    cursor = (day_index + days_to_monday) * _DAY_MS

    out = np.empty(int(bars), dtype=np.int64)
    filled = 0
    # Weekends are 2/7 of the calendar, so this cannot loop for long.
    while filled < int(bars):
        if _python_weekday(cursor // _DAY_MS) not in _WEEKEND:
            out[filled] = cursor
            filled += 1
        cursor += step
    return out


def synthetic_candles(
    symbol: str,
    *,
    bars: int = 2000,
    interval_minutes: int = 120,
    start_ts: int = 1_600_000_000_000,
    seed: int = 42,
) -> CandleArrays:
    """Generate a deterministic, session-aware OHLCV series for ``symbol``."""
    bars = int(bars)
    if bars < 10:
        raise ValueError(f"need at least 10 bars, got {bars}")
    interval_minutes = int(interval_minutes)
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be positive")

    symbol = str(symbol).upper()
    jpy = "JPY" in symbol
    digits = 3 if jpy else 5
    point = 10.0 ** (-digits)  # 5 digits -> 0.00001, 3 digits -> 0.001
    base = 110.0 if jpy else 1.10

    rng = np.random.default_rng(_symbol_seed(symbol, seed))
    ts = _session_timestamps(bars, interval_minutes, start_ts)

    # -- price path: alternating trend/chop regimes ---------------------------
    # Fractions of price per bar. H2 EURUSD is ~8-12 pips (0.08-0.11%) of noise
    # with a trend of a few pips per bar; the cycle gives the Dow structure
    # filter clean swings to confirm, and a gentle pull back to the anchor keeps
    # a year of synthetic data inside a realistic FX range instead of drifting
    # to 1.30 or 0.90.
    regime_bars = max(24, bars // 10)
    phase = (np.arange(bars) // regime_bars) % 4
    vol = np.where(phase % 2 == 1, 0.0012, 0.0008)
    drift = np.where(phase == 0, 0.00015, np.where(phase == 2, -0.00015, 0.0))
    shocks = vol * rng.standard_normal(bars)
    log_price = np.empty(bars, dtype=np.float64)
    level = 0.0
    for i in range(bars):
        level += float(drift[i]) + float(shocks[i])
        level -= 0.0006 * level  # slow mean reversion around the anchor price
        log_price[i] = level
    close = base * np.exp(log_price)

    # -- opens, with a gap after every weekend --------------------------------
    prev_close = np.empty(bars, dtype=np.float64)
    prev_close[0] = base
    prev_close[1:] = close[:-1]
    step_ms = interval_minutes * 60_000
    weekend_gap = np.zeros(bars, dtype=bool)
    weekend_gap[1:] = (ts[1:] - ts[:-1]) > step_ms * 3 // 2
    gap = 1.0 + rng.standard_normal(bars) * 0.0015 * np.where(weekend_gap, 1.0, 0.35)
    open_ = prev_close * gap

    # -- wicks: flat high/low noise scaled to price ---------------------------
    body_high = np.maximum(open_, close)
    body_low = np.minimum(open_, close)
    wick = np.abs(rng.standard_normal(bars)) * vol * 0.6
    high = body_high * (1.0 + wick)
    low = body_low * (1.0 - wick)

    # -- round to the pair's digits and repair the OHLC ordering ---------------
    open_ = np.round(open_, digits)
    close = np.round(close, digits)
    high = np.round(high, digits)
    low = np.round(low, digits)
    high = np.maximum(high, np.maximum(open_, close) + point)
    low = np.minimum(low, np.minimum(open_, close) - point)
    low = np.maximum(low, point)
    high = np.maximum(high, low + point)

    volume = rng.uniform(500.0, 1500.0, bars)

    return CandleArrays(
        ts=ts.astype(np.int64),
        open=open_.astype(np.float64),
        high=high.astype(np.float64),
        low=low.astype(np.float64),
        close=close.astype(np.float64),
        volume=volume.astype(np.float64),
    )

"""Synthetic-data generator tests: sessions, digits, gaps, determinism.

The generator is what makes the demo and the offline tests possible, so its
Forex-specific contract is pinned here:

* no Saturday/Sunday bars, and the timestamp grid is aligned to the interval;
* OHLC is rounded to 5 digits (FX) or 3 digits (JPY) and remains a legal bar;
* the first bar after a weekend opens away from the prior close (a real gap);
* the same symbol+seed produces byte-identical series while different symbols
  produce different paths (the seed is derived from the symbol name, not
  Python's salted ``hash()``).
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from nornweave_forex.data.synthetic import synthetic_candles

_STEP_MS = 120 * 60_000


def _weekday(ts_ms: np.ndarray) -> np.ndarray:
    """Python weekday (Mon=0) for millisecond timestamps (1970-01-01 = Thursday)."""
    days = np.asarray(ts_ms, dtype=np.int64) // 86_400_000
    return (days + 3) % 7


def test_eurusd_is_5_digit_rounded_and_structurally_valid() -> None:
    candles = synthetic_candles("EURUSD", bars=300, interval_minutes=120)
    assert len(candles) == 300
    assert candles.validate() == []
    for values in (candles.open, candles.high, candles.low, candles.close):
        assert np.allclose(values, np.round(values, 5))
    assert np.all(candles.volume > 0)


def test_usdjpy_is_3_digit_rounded() -> None:
    candles = synthetic_candles("USDJPY", bars=300, interval_minutes=120)
    assert candles.validate() == []
    for values in (candles.open, candles.high, candles.low, candles.close):
        assert np.allclose(values, np.round(values, 3))
    # A JPY pair trades around 110, not around 1.10.
    assert 50.0 < float(np.mean(candles.close)) < 200.0


def test_ohlc_ordering_holds_after_rounding() -> None:
    for symbol in ("EURUSD", "GBPUSD", "USDJPY"):
        candles = synthetic_candles(symbol, bars=250, interval_minutes=120)
        assert np.all(candles.high >= np.maximum(candles.open, candles.close))
        assert np.all(candles.low <= np.minimum(candles.open, candles.close))
        assert np.all(candles.high > candles.low)
        assert np.all(np.isfinite(candles.open))
        assert np.all(np.isfinite(candles.high))
        assert np.all(np.isfinite(candles.low))
        assert np.all(np.isfinite(candles.close))


def test_no_weekend_bars_and_timestamps_are_aligned() -> None:
    candles = synthetic_candles("EURUSD", bars=300, interval_minutes=120)
    weekdays = _weekday(candles.ts)
    assert not np.any(np.isin(weekdays, (5, 6))), "FX is closed Saturday/Sunday"
    assert np.all(np.diff(candles.ts) > 0)
    assert np.all(candles.ts % _STEP_MS == 0)
    # The series starts on a Monday 00:00 UTC session open.
    assert int(weekdays[0]) == 0
    assert int(candles.ts[0] % 86_400_000) == 0


def test_weekend_gap_opens_away_from_the_previous_close() -> None:
    candles = synthetic_candles("EURUSD", bars=300, interval_minutes=120)
    gaps = np.flatnonzero(np.diff(candles.ts) > _STEP_MS * 3 // 2)
    assert gaps.size >= 4, "300 H2 bars should span several weekends"
    for index in gaps:
        # ``index`` is the last Friday bar; index+1 opens after the weekend.
        assert candles.open[index + 1] != candles.close[index]
        assert np.isfinite(candles.open[index + 1])
        # The gap is a real move, not rounding noise.
        assert abs(float(candles.open[index + 1]) - float(candles.close[index])) >= 10 ** -5


def test_generator_is_deterministic_per_symbol_and_seed() -> None:
    first = synthetic_candles("EURUSD", bars=200, interval_minutes=120)
    second = synthetic_candles("EURUSD", bars=200, interval_minutes=120)
    for name in ("ts", "open", "high", "low", "close", "volume"):
        assert np.array_equal(getattr(first, name), getattr(second, name))

    other_seed = synthetic_candles("EURUSD", bars=200, interval_minutes=120, seed=7)
    assert not np.array_equal(first.close, other_seed.close)


def test_different_symbols_get_different_paths() -> None:
    eurusd = synthetic_candles("EURUSD", bars=200, interval_minutes=120)
    gbpusd = synthetic_candles("GBPUSD", bars=200, interval_minutes=120)
    assert not np.array_equal(eurusd.close, gbpusd.close)
    # Both are still plausible EUR/GBP-like prices.
    assert 0.5 < float(np.mean(eurusd.close)) < 2.0
    assert 0.5 < float(np.mean(gbpusd.close)) < 2.0


def test_rejects_a_series_too_short_to_be_useful() -> None:
    with pytest.raises(ValueError):
        synthetic_candles("EURUSD", bars=5)
    with pytest.raises(ValueError):
        synthetic_candles("EURUSD", bars=100, interval_minutes=0)


def test_weekend_gap_length_is_two_days() -> None:
    """The Friday->Monday jump is ~49-50 hours for an H2 series (no Saturday bars)."""
    candles = synthetic_candles("EURUSD", bars=300, interval_minutes=120)
    gaps = np.flatnonzero(np.diff(candles.ts) > _STEP_MS * 3 // 2)
    for index in gaps:
        delta_hours = (int(candles.ts[index + 1]) - int(candles.ts[index])) / 3_600_000
        assert delta_hours == pytest.approx(50.0), (
            f"Friday {dt.datetime.fromtimestamp(candles.ts[index] / 1000, dt.timezone.utc)} "
            f"-> Monday should be 48h of weekend + 2h bar = 50h, got {delta_hours}h"
        )

"""THURISAZ's Auto Calibration: strategy parameters that change bar by bar.

FEHU used fixed numbers typed into the settings panel. URUZ introduced *Focus Level*
and THURISAZ adds Auto Calibration, which derives three of the four headline numbers
from the chart itself rather than from an input:

    adxThreshold = ta.percentile_nearest_rank(adxValue, 300, 50)   // rolling median
    volRatio     = atrPct / ta.sma(atrPct, 200)
    volAdj       = clamp(1 - volRatio, -1, 1)
    atrFactor    = 3.8 + volAdj * 0.8
    stopLoss     = clamp(-(ta.sma(atrPct, 50) * 3.5), -20, -5)

Read that as three separate ideas:

* **ADX threshold becomes a rolling median of ADX itself.** In a choppy year ADX
  rarely exceeds 20, so a fixed 20.5 would block everything; the median keeps the
  filter at "better than typical for *this* market", which is what makes the strategy
  transferable to a new symbol without re-tuning.
* **ATR factor leans against volatility.** When current ATR is above its 200-bar
  average the factor shrinks (toward 3.0) and targets come closer; when volatility is
  unusually low it stretches (toward 4.6). It is a mean-reversion tilt on target
  distance, not a trend filter.
* **The stop is volatility-scaled**, clamped to [-20%, -5%], replacing FEHU's fixed
  -10%.

Every expression has an explicit ``na`` fallback in the source, reproduced here, so a
not-yet-warm series degrades to the FEHU values rather than blocking trading.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from nornweave_forex.config import TuningParams
from nornweave_forex.indicators.core import atr, rolling_percentile_nearest_rank, sma

__all__ = ["TuningSeries", "auto_calibrate", "PERCENT"]


@dataclass(slots=True)
class TuningSeries:
    """Per-bar parameter values, aligned to the candle series."""

    adx_threshold: np.ndarray
    atr_factor: np.ndarray
    stop_loss_pct: np.ndarray

    def __len__(self) -> int:
        return int(self.adx_threshold.shape[0])

    def at(self, index: int) -> tuple[float, float, float]:
        return (
            float(self.adx_threshold[index]),
            float(self.atr_factor[index]),
            float(self.stop_loss_pct[index]),
        )


#: The source expresses the stop in percent and the engine wants a fraction.
PERCENT = 100.0


def auto_calibrate(
    high,
    low,
    close,
    adx_values: np.ndarray,
    params: TuningParams,
) -> TuningSeries:
    """Compute the three auto-calibrated series from the chart."""
    high = np.asarray(high, dtype=np.float64)
    low = np.asarray(low, dtype=np.float64)
    close = np.asarray(close, dtype=np.float64)
    adx_values = np.asarray(adx_values, dtype=np.float64)
    n = close.size
    if not (high.size == low.size == close.size == adx_values.size == n):
        raise ValueError("auto_calibrate needs equally long high/low/close/adx series")

    # -- ADX threshold: rolling nearest-rank percentile of ADX -----------------
    percentile = rolling_percentile_nearest_rank(
        adx_values, params.adx_window, params.adx_percentile
    )
    adx_threshold = np.where(np.isfinite(percentile), percentile, params.adx_fallback)

    # -- ATR factor: lean against unusually high volatility --------------------
    with np.errstate(invalid="ignore", divide="ignore"):
        atr_pct = atr(high, low, close, params.atr_period) / close * PERCENT
    atr_pct_avg = sma(atr_pct, params.atr_average_window)
    ready = np.isfinite(atr_pct_avg) & (atr_pct_avg > 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        vol_ratio = np.where(ready, atr_pct / atr_pct_avg, 1.0)
    vol_ratio = np.where(np.isfinite(vol_ratio), vol_ratio, 1.0)
    vol_adj = np.clip(1.0 - vol_ratio, -1.0, 1.0)
    atr_factor = params.atr_factor_base + vol_adj * params.atr_factor_span

    # -- Stop: volatility-scaled, clamped --------------------------------------
    atr_pct_smooth = sma(atr_pct, params.stop_basis_window)
    fallback = params.stop_fallback_pct * PERCENT
    raw_sl = np.where(
        np.isfinite(atr_pct_smooth), -(atr_pct_smooth * params.stop_multiple), fallback
    )
    stop_percent = np.clip(raw_sl, params.stop_floor_pct * PERCENT, params.stop_cap_pct * PERCENT)
    stop_loss_pct = stop_percent / PERCENT

    return TuningSeries(
        adx_threshold=adx_threshold,
        atr_factor=atr_factor,
        stop_loss_pct=stop_loss_pct,
    )

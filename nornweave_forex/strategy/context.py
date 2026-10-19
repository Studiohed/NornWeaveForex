"""Indicator precomputation and per-bar signal context, shared by every engine.

The backtester reads this by index over a whole history; the live runner reads the
last index of a rolling window. Both must produce the *same numbers* for the same
bar, so the arithmetic exists only here.

One caveat worth stating plainly: EMA and Wilder smoothing are seeded at the first
bar of whatever series they are given, so a live window shorter than the backtest
history yields slightly different values. With a few hundred bars of warm-up the
seed's influence decays to nothing (for EMA(65) it is below 1e-8 after 600 bars),
but the runner should always fetch at least ``backtest.preroll_bars`` of history.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from nornweave_forex.config import StrategyParams, TuningParams
from nornweave_forex.indicators.core import adx, atr, ema, falling_streak, rising_streak
from nornweave_forex.strategy.daily import DailyContext, build_daily_context
from nornweave_forex.strategy.dow import dow_trend_series
from nornweave_forex.strategy.engine import SignalContext
from nornweave_forex.strategy.tuning import TuningSeries, auto_calibrate
from nornweave_forex.types import CandleArrays, DowTrend

__all__ = ["IndicatorSet", "build_indicators", "DOW_BY_CODE"]

DOW_BY_CODE = {1: DowTrend.BULLISH, 0: DowTrend.NEUTRAL, -1: DowTrend.BEARISH}


def _first_finite(*arrays: np.ndarray) -> int:
    first = 0
    for arr in arrays:
        finite = np.flatnonzero(np.isfinite(arr))
        if finite.size == 0:
            raise ValueError("indicator has no finite values; the series is too short")
        first = max(first, int(finite[0]))
    return first


@dataclass(slots=True)
class IndicatorSet:
    """Every derived series the strategy needs, plus the index where it becomes valid."""

    candles: CandleArrays
    ema: np.ndarray
    adx: np.ndarray
    atr: np.ndarray
    dow_codes: np.ndarray
    daily: DailyContext
    lookback: int
    ready_index: int
    rise_streak: np.ndarray | None = None
    fall_streak: np.ndarray | None = None
    delta: np.ndarray | None = None
    #: Auto Calibration series; ``None`` when the static parameters are used.
    tuning: TuningSeries | None = None

    def __len__(self) -> int:
        return len(self.candles)

    def context_at(self, index: int) -> SignalContext:
        candles = self.candles
        ema_ref = (
            float(self.ema[index - self.lookback]) if index >= self.lookback else float("nan")
        )
        adx_threshold = None
        if self.tuning is not None:
            adx_threshold = float(self.tuning.adx_threshold[index])
        return SignalContext(
            index=index,
            ts=int(candles.ts[index]),
            close=float(candles.close[index]),
            ema=float(self.ema[index]),
            ema_ref=ema_ref,
            adx=float(self.adx[index]),
            atr=float(self.atr[index]),
            dow=DOW_BY_CODE[int(self.dow_codes[index])],
            daily=self.daily.state_for_bar(index),
            footprint_delta=float(self.delta[index]) if self.delta is not None else None,
            ema_rise_streak=(
                int(self.rise_streak[index]) if self.rise_streak is not None else None
            ),
            ema_fall_streak=(
                int(self.fall_streak[index]) if self.fall_streak is not None else None
            ),
            adx_threshold=adx_threshold,
        )


def build_indicators(
    candles: CandleArrays,
    interval: str,
    strategy: StrategyParams,
    delta: np.ndarray | None = None,
    tuning: TuningParams | None = None,
) -> IndicatorSet:
    """Compute everything the strategy reads, in one pass."""
    if len(candles) == 0:
        raise ValueError("cannot build indicators for an empty series")

    ema_vals = ema(candles.close, strategy.ema_period)
    adx_vals = adx(candles.high, candles.low, candles.close, strategy.adx_period)
    atr_vals = atr(candles.high, candles.low, candles.close, strategy.atr_period)
    dow_codes = dow_trend_series(
        candles.high,
        candles.low,
        strategy.resolved_dow_left,
        strategy.resolved_dow_right,
        mode=strategy.dow_mode,
        close=candles.close,
        persist=(strategy.dow_neutral_mode == "persist"),
        strict=strategy.pivot_strict,
    )
    daily = build_daily_context(candles, interval, strategy)

    rise: np.ndarray | None = None
    fall: np.ndarray | None = None
    if strategy.ema_slope_mode == "rising":
        rise = rising_streak(ema_vals)
        fall = falling_streak(ema_vals)

    ready = _first_finite(ema_vals, adx_vals, atr_vals)
    if strategy.ema_slope_mode == "rising":
        # ta.rising(ema, n) needs n consecutive valid steps after the EMA seed.
        ready += strategy.ema_slope_lookback
    else:
        ready = max(ready, strategy.ema_slope_lookback)

    tuning_series: TuningSeries | None = None
    if tuning is not None and tuning.enabled:
        tuning_series = auto_calibrate(
            candles.high, candles.low, candles.close, adx_vals, tuning
        )

    return IndicatorSet(
        candles=candles,
        ema=ema_vals,
        adx=adx_vals,
        atr=atr_vals,
        dow_codes=dow_codes,
        daily=daily,
        lookback=strategy.ema_slope_lookback,
        ready_index=ready,
        rise_streak=rise,
        fall_streak=fall,
        delta=delta,
        tuning=tuning_series,
    )

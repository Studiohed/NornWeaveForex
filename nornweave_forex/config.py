"""Typed, YAML-loadable settings for the whole stack.

Every knob that the strategy brief mentions lives here, with the brief's
defaults. Unknown keys raise :class:`ConfigError` instead of being ignored, so a
typo in a config file can never silently disable a filter.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping

import yaml

__all__ = [
    "ConfigError",
    "StrategyParams",
    "RiskParams",
    "TuningParams",
    "BacktestParams",
    "Config",
    "INTERVAL_MINUTES",
    "interval_ms",
    "bars_per_day",
    "recommended_preset",
    "recommended_risk_preset",
    "preset_table",
]

DAY_MS = 86_400_000

INTERVAL_MINUTES: dict[str, int] = {
    "1": 1,
    "3": 3,
    "5": 5,
    "15": 15,
    "30": 30,
    "60": 60,
    "120": 120,
    "240": 240,
    "360": 360,
    "720": 720,
    "D": 1440,
    "W": 10080,
    "M": 43200,
}


class ConfigError(ValueError):
    """Raised when a config file is malformed or internally inconsistent."""


def interval_ms(interval: str) -> int:
    key = str(interval).upper()
    if key not in INTERVAL_MINUTES:
        raise ConfigError(
            f"unsupported interval {interval!r}; use one of {sorted(INTERVAL_MINUTES)}"
        )
    return INTERVAL_MINUTES[key] * 60_000


def bars_per_day(interval: str) -> float:
    return DAY_MS / interval_ms(interval)


def _reject_unknown(cls: type, data: Mapping[str, Any], path: str) -> None:
    known = {f.name for f in fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(
            f"{path}: unknown key(s) {unknown}. Valid keys: {sorted(known)}"
        )


def _coerce_tuple(value: Any, path: str) -> tuple:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    raise ConfigError(f"{path}: expected a list, got {type(value).__name__}")


# --------------------------------------------------------------------------- #
# Strategy
# --------------------------------------------------------------------------- #
@dataclass
class StrategyParams:
    """The three core filters plus the THURISAZ daily-Fibonacci overlay."""

    # -- Focus Level drives swing sensitivity and the EMA period -------------
    focus_level: int = 13
    ema_scale_ratio: float = 5.0
    # EMA slope is "rising" when ema[i] > ema[i - ema_slope_lookback]
    ema_slope_lookback: int = 1
    # "compare" -> ema[i] > ema[i - lookback]
    # "rising"  -> ta.rising(ema, lookback): `lookback` consecutive rises
    ema_slope_mode: str = "compare"

    # -- Filter 2: Dow structure --------------------------------------------
    require_dow_structure: bool = True
    # "structure" -> higher-high/higher-low comparison of consecutive confirmed pivots
    # "breakout"  -> persistent trend that flips when price CLOSES through the last
    #                confirmed swing level (what the FEHU Pine source does)
    dow_mode: str = "structure"
    # "block"   -> an ambiguous read forbids entry (conservative, "structure" only)
    # "persist" -> hold the last decisive read through neutral stretches
    dow_neutral_mode: str = "block"
    # Strict pivot dominance; the FEHU source uses `== ta.highest(...)`, which allows ties
    pivot_strict: bool = True
    # Exit the whole position when the trend turns against it
    exit_on_dow_reversal: bool = True
    # "dow_opposite" -> opposite confirmed Dow trend
    # "ema_cross"    -> close crosses the strategy EMA against the position
    # "none"         -> never exit on a trend read alone
    reversal_rule: str = "dow_opposite"
    # Also exit when Dow structure becomes NEUTRAL (only for "dow_opposite")
    exit_on_dow_neutral: bool = False
    dow_left: int | None = None  # default: focus_level
    dow_right: int | None = None  # default: focus_level

    # -- Filter 3: ADX -------------------------------------------------------
    adx_period: int = 14
    adx_threshold: float = 20.5

    # -- ATR / profit targets ------------------------------------------------
    atr_period: int = 14

    # -- THURISAZ: daily Fibonacci overlay -----------------------------------
    daily_filter_enabled: bool = True
    # "pivot_leg" = Fib over the last confirmed daily swing leg (brief's "daily swing")
    # "range"     = Fib over the last N completed daily bars' high/low extremes
    daily_fib_mode: str = "pivot_leg"
    daily_focus_level: int = 5
    daily_range_lookback: int = 20
    # THURISAZ reads daily pivots as `ta.pivothigh(...)[1]`, one extra bar of delay
    daily_pivot_delay: int = 0
    daily_ema_period: int | None = None  # default: same as the intraday EMA period
    daily_trend_mode: str = "dow_then_ema"  # dow | ema | dow_then_ema
    fib_shallow: float = 0.382
    fib_deep: float = 0.618
    # What an *undecided* daily trend means for entries:
    # "allow" -> permit both directions (THURISAZ's `dTrend == 0` branch)
    # "block" -> still run the Fibonacci zone check (the conservative first reading)
    daily_neutral_policy: str = "block"
    # What to do before enough completed daily history exists
    daily_missing_policy: str = "block"  # block | allow

    # -- Optional footprint-delta filter ------------------------------------
    footprint_enabled: bool = False
    footprint_window: int = 1
    footprint_threshold: float = 0.0  # in base-asset units of net taker delta
    footprint_missing_policy: str = "skip"  # skip | block

    @property
    def ema_period(self) -> int:
        return max(2, int(round(self.focus_level * self.ema_scale_ratio)))

    @property
    def resolved_daily_ema_period(self) -> int:
        return int(self.daily_ema_period or self.ema_period)

    @property
    def resolved_dow_left(self) -> int:
        return int(self.dow_left if self.dow_left is not None else self.focus_level)

    @property
    def resolved_dow_right(self) -> int:
        return int(self.dow_right if self.dow_right is not None else self.focus_level)

    def __post_init__(self) -> None:
        self.focus_level = int(self.focus_level)
        self.ema_scale_ratio = float(self.ema_scale_ratio)
        self.ema_slope_lookback = int(self.ema_slope_lookback)
        self.adx_period = int(self.adx_period)
        self.adx_threshold = float(self.adx_threshold)
        self.atr_period = int(self.atr_period)
        self.daily_focus_level = int(self.daily_focus_level)
        self.daily_range_lookback = int(self.daily_range_lookback)
        self.daily_pivot_delay = int(self.daily_pivot_delay)
        self.fib_shallow = float(self.fib_shallow)
        self.fib_deep = float(self.fib_deep)
        self.footprint_window = int(self.footprint_window)
        self.footprint_threshold = float(self.footprint_threshold)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None, path: str = "strategy") -> "StrategyParams":
        if not data:
            return cls()
        _reject_unknown(cls, data, path)
        return cls(**dict(data))

    def validate(self) -> None:
        if self.focus_level < 2:
            raise ConfigError("strategy.focus_level must be >= 2")
        if self.ema_scale_ratio <= 0:
            raise ConfigError("strategy.ema_scale_ratio must be > 0")
        if self.ema_slope_lookback < 1:
            raise ConfigError("strategy.ema_slope_lookback must be >= 1")
        if self.ema_slope_mode not in ("compare", "rising"):
            raise ConfigError("strategy.ema_slope_mode must be 'compare' or 'rising'")
        if self.dow_neutral_mode not in ("block", "persist"):
            raise ConfigError("strategy.dow_neutral_mode must be 'block' or 'persist'")
        if self.dow_mode not in ("structure", "breakout"):
            raise ConfigError("strategy.dow_mode must be 'structure' or 'breakout'")
        if not self.pivot_strict and self.dow_mode != "breakout":
            raise ConfigError(
                "strategy.pivot_strict=False (ties allowed) is only meaningful for "
                "dow_mode 'breakout'; set dow_mode='breakout' or leave pivot_strict=True"
            )
        if self.reversal_rule not in ("dow_opposite", "ema_cross", "none"):
            raise ConfigError(
                "strategy.reversal_rule must be 'dow_opposite', 'ema_cross' or 'none'"
            )
        if self.adx_period < 2:
            raise ConfigError("strategy.adx_period must be >= 2")
        if self.atr_period < 2:
            raise ConfigError("strategy.atr_period must be >= 2")
        if not 0.0 < self.fib_shallow < self.fib_deep < 1.0:
            raise ConfigError(
                "strategy.fib_shallow/fib_deep must satisfy 0 < shallow < deep < 1"
            )
        if self.daily_fib_mode not in ("pivot_leg", "range"):
            raise ConfigError("strategy.daily_fib_mode must be 'pivot_leg' or 'range'")
        if self.daily_trend_mode not in ("dow", "ema", "dow_then_ema"):
            raise ConfigError("strategy.daily_trend_mode must be dow|ema|dow_then_ema")
        if self.daily_missing_policy not in ("block", "allow"):
            raise ConfigError("strategy.daily_missing_policy must be 'block' or 'allow'")
        if self.daily_neutral_policy not in ("block", "allow"):
            raise ConfigError("strategy.daily_neutral_policy must be 'block' or 'allow'")
        if self.daily_pivot_delay < 0:
            raise ConfigError("strategy.daily_pivot_delay must be >= 0")
        if self.footprint_missing_policy not in ("skip", "block"):
            raise ConfigError("strategy.footprint_missing_policy must be 'skip' or 'block'")
        if self.footprint_window < 1:
            raise ConfigError("strategy.footprint_window must be >= 1")
        # The daily leg needs at least two confirmed pivots to exist
        if self.daily_fib_mode == "pivot_leg" and self.daily_focus_level < 2:
            raise ConfigError("strategy.daily_focus_level must be >= 2")


# --------------------------------------------------------------------------- #
# Risk / exits
# --------------------------------------------------------------------------- #
@dataclass
class RiskParams:
    """Multi-stage exit ladder, protective stop and position sizing."""

    # TP1 = entry + direction * ATR * atr_factor * tp_atr_multiples[0]
    atr_factor: float = 3.8
    tp_atr_multiples: tuple[float, ...] = (1.0, 2.0, 3.0)
    tp_weights: tuple[float, ...] = (0.30, 0.30, 0.30)
    # What the target prices are measured from:
    # "entry"        -> the actual fill price (internally consistent)
    # "signal_close" -> the signal bar's close, which is what FEHU does: the entry
    #                   fills at the next bar's open but `tp1 := close + atr`
    tp_anchor: str = "entry"
    # What `tp_weights` are a fraction of when a target fills:
    # "initial" -> the original position size (30% + 30% + 30% = 90% taken off)
    # "current" -> the size still open, which is how Pine's `qty_percent` reads
    #              (30% + 21% + 14.7% = 65.7% taken off, leaving a 34% runner)
    tp_weight_basis: str = "initial"

    # Fixed percentage stop from entry; negative. On FX this is a fraction of
    # price, so -0.01 is ~100 pips on EURUSD. The crypto default of -0.05 would be
    # ~550 pips: effectively no stop at all.
    stop_loss_pct: float = -0.01
    # Move the stop to break-even once this much profit (as a fraction) was seen
    be_trigger_pct: float = 0.015
    # "profit_pct" -> arm once unrealised profit reaches be_trigger_pct
    # "after_tp1"  -> arm once the first target has filled
    be_mode: str = "profit_pct"
    # When the protective stop is evaluated:
    # "intrabar" -> the bar's low/high touching the stop exits immediately (pessimistic)
    # "close"    -> only a close beyond the stop exits; wicks that recover do not
    stop_trigger: str = "intrabar"
    # Where exit orders fill:
    # "level"     -> a target fills at its own price (a resting limit order)
    # "bar_close" -> every exit is a market order at the bar's close, and only the
    #                *trigger* is intrabar. This is what the FEHU Pine does with
    #                `strategy.close()`, and it moves results a lot.
    exit_fill: str = "level"
    # Order of the two exit families inside one bar:
    # "tp_first"    -> targets, then the protective exits (this engine's default)
    # "exits_first" -> protective exits, then targets, which is the FEHU source's
    #                  order and matters whenever both fire on the same bar
    exit_order: str = "tp_first"
    # What arms the break-even stop:
    # "extreme" -> the bar's favourable high/low reached the trigger (default)
    # "close"   -> only a close beyond the trigger arms it (FEHU uses close-based PnL)
    be_arm_on: str = "extreme"
    # Offset of the break-even stop beyond entry; FEHU uses a 0.1% buffer
    be_buffer_pct: float = 0.0
    # How the break-even stop itself triggers:
    # "follow"   -> use stop_trigger
    # "intrabar" -> a wick through it exits on that bar (FEHU's BE check)
    # "close"    -> only a close through it exits
    be_exit_trigger: str = "follow"

    # "risk"      -> qty = equity * risk_per_trade_pct / |entry - stop|
    # "notional"  -> notional = equity * notional_pct * leverage
    # "equity_pct"-> notional = equity * equity_pct (Pine strategy.percent_of_equity)
    size_mode: str = "risk"
    risk_per_trade_pct: float = 0.01
    notional_pct: float = 0.25
    equity_pct: float = 1.0
    leverage: float = 30.0

    # -- exchange minimum handling ------------------------------------------ #
    # On a small account the strategy's own size can fall below the exchange's
    # minimum order. ``min_notional`` rounds the order *up* to that floor, and
    # ``max_risk_per_trade_pct`` bounds what the rounding-up is allowed to cost:
    # if the floor would risk more than this fraction of equity, the trade is
    # refused instead. Without the cap, rounding up is a slow-motion martingale —
    # each loss shrinks equity, which makes the next minimum-size order a larger
    # share of the account.
    min_notional: float = 0.0
    max_risk_per_trade_pct: float = 0.0  # 0 disables the cap

    # -- portfolio cap (Forex multi-symbol) --------------------------------- #
    # Upper bound on the sum of all open position risks as a fraction of
    # equity. A new entry is refused when it would push the total past this.
    # Defaults to ``risk_per_trade_pct * 5`` so two positions at default risk
    # fit comfortably; override for tighter control.
    portfolio_risk_pct: float = 0.05

    def __post_init__(self) -> None:
        self.atr_factor = float(self.atr_factor)
        self.tp_atr_multiples = _coerce_tuple(self.tp_atr_multiples, "risk.tp_atr_multiples")
        self.tp_weights = _coerce_tuple(self.tp_weights, "risk.tp_weights")
        self.tp_atr_multiples = tuple(float(x) for x in self.tp_atr_multiples)
        self.tp_weights = tuple(float(x) for x in self.tp_weights)
        self.stop_loss_pct = float(self.stop_loss_pct)
        self.be_trigger_pct = float(self.be_trigger_pct)
        self.risk_per_trade_pct = float(self.risk_per_trade_pct)
        self.notional_pct = float(self.notional_pct)
        self.equity_pct = float(self.equity_pct)
        self.leverage = float(self.leverage)
        self.be_buffer_pct = float(self.be_buffer_pct)
        self.portfolio_risk_pct = float(self.portfolio_risk_pct)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None, path: str = "risk") -> "RiskParams":
        if not data:
            return cls()
        _reject_unknown(cls, data, path)
        return cls(**dict(data))

    def validate(self) -> None:
        if self.stop_loss_pct >= 0:
            raise ConfigError("risk.stop_loss_pct must be negative, e.g. -0.05")
        if self.min_notional < 0:
            raise ConfigError("risk.min_notional must be >= 0")
        if self.min_notional > 0 and self.max_risk_per_trade_pct <= 0:
            raise ConfigError(
                "risk.min_notional is set but risk.max_risk_per_trade_pct is not: "
                "rounding an order up to the exchange minimum without a risk cap lets "
                "the position grow as a share of the account every time it loses"
            )
        if self.max_risk_per_trade_pct < 0 or self.max_risk_per_trade_pct > 1:
            raise ConfigError("risk.max_risk_per_trade_pct must be a fraction in (0, 1]")
        if not 0 < self.portfolio_risk_pct <= 1:
            raise ConfigError("risk.portfolio_risk_pct must be a fraction in (0, 1]")
        if self.portfolio_risk_pct < self.risk_per_trade_pct:
            raise ConfigError(
                "risk.portfolio_risk_pct must be >= risk.risk_per_trade_pct — "
                "otherwise even a single position would breach the portfolio cap"
            )
        if self.be_trigger_pct <= 0:
            raise ConfigError("risk.be_trigger_pct must be positive, e.g. 0.09")
        if self.be_mode not in ("profit_pct", "after_tp1"):
            raise ConfigError("risk.be_mode must be 'profit_pct' or 'after_tp1'")
        if self.stop_trigger not in ("intrabar", "close"):
            raise ConfigError("risk.stop_trigger must be 'intrabar' or 'close'")
        if self.exit_fill not in ("level", "bar_close"):
            raise ConfigError("risk.exit_fill must be 'level' or 'bar_close'")
        if self.exit_order not in ("tp_first", "exits_first"):
            raise ConfigError("risk.exit_order must be 'tp_first' or 'exits_first'")
        if self.be_arm_on not in ("extreme", "close"):
            raise ConfigError("risk.be_arm_on must be 'extreme' or 'close'")
        if self.be_exit_trigger not in ("follow", "intrabar", "close"):
            raise ConfigError("risk.be_exit_trigger must be 'follow', 'intrabar' or 'close'")
        if self.be_buffer_pct < 0:
            raise ConfigError("risk.be_buffer_pct must be >= 0")
        if self.atr_factor <= 0:
            raise ConfigError("risk.atr_factor must be > 0")
        if len(self.tp_atr_multiples) != len(self.tp_weights):
            raise ConfigError("risk.tp_atr_multiples and risk.tp_weights must be the same length")
        if self.tp_anchor not in ("entry", "signal_close"):
            raise ConfigError("risk.tp_anchor must be 'entry' or 'signal_close'")
        if self.tp_weight_basis not in ("initial", "current"):
            raise ConfigError("risk.tp_weight_basis must be 'initial' or 'current'")
        if not self.tp_atr_multiples:
            raise ConfigError("risk.tp_atr_multiples must not be empty")
        if any(m <= 0 for m in self.tp_atr_multiples):
            raise ConfigError("risk.tp_atr_multiples must all be > 0")
        if any(w <= 0 for w in self.tp_weights):
            raise ConfigError("risk.tp_weights must all be > 0")
        if sum(self.tp_weights) > 1.0 + 1e-9:
            raise ConfigError(
                f"risk.tp_weights sum to {sum(self.tp_weights):.4f}; must be <= 1.0"
            )
        if self.size_mode not in ("risk", "notional", "equity_pct"):
            raise ConfigError("risk.size_mode must be 'risk', 'notional' or 'equity_pct'")
        if self.size_mode == "risk" and self.risk_per_trade_pct <= 0:
            raise ConfigError("risk.risk_per_trade_pct must be > 0")
        if self.size_mode == "notional" and self.notional_pct <= 0:
            raise ConfigError("risk.notional_pct must be > 0")
        if self.size_mode == "equity_pct" and self.equity_pct <= 0:
            raise ConfigError("risk.equity_pct must be > 0")
        if self.leverage <= 0:
            raise ConfigError("risk.leverage must be > 0")


# --------------------------------------------------------------------------- #
# Portfolio (multi-symbol, Forex-specific)
# --------------------------------------------------------------------------- #
@dataclass
class PortfolioParams:
    """How many pairs may hold a position at once, and which pairs to watch."""

    #: The 8 majors we scan every bar.
    symbols: tuple[str, ...] = (
        "EURUSD", "GBPUSD", "USDJPY", "AUDUSD",
        "USDCAD", "USDCHF", "NZDUSD", "EURJPY",
    )
    #: Upper bound on simultaneously open positions.
    max_positions: int = 2

    def __post_init__(self) -> None:
        self.symbols = _coerce_tuple(self.symbols, "portfolio.symbols")
        self.symbols = tuple(str(s).upper() for s in self.symbols)
        self.max_positions = int(self.max_positions)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None, path: str = "portfolio") -> "PortfolioParams":
        if not data:
            return cls()
        _reject_unknown(cls, data, path)
        return cls(**dict(data))

    def validate(self) -> None:
        if not self.symbols:
            raise ConfigError("portfolio.symbols must not be empty")
        if len(set(self.symbols)) != len(self.symbols):
            raise ConfigError(
                f"portfolio.symbols must be unique, got {list(self.symbols)}"
            )
        if self.max_positions < 1:
            raise ConfigError("portfolio.max_positions must be >= 1")
        if self.max_positions > len(self.symbols):
            raise ConfigError(
                f"portfolio.max_positions ({self.max_positions}) exceeds the number of "
                f"watched symbols ({len(self.symbols)})"
            )


# --------------------------------------------------------------------------- #
# Backtest
# --------------------------------------------------------------------------- #
@dataclass
class BacktestParams:
    initial_equity: float = 10_000.0
    #: MT5 Forex accounts are usually spread-only; commission is 0. The backtester
    #: charges the spread explicitly, so the default fee must stay at zero — the
    #: crypto default of 0.055% would silently double-count execution cost.
    taker_fee: float = 0.0
    maker_fee: float = 0.0
    slippage_bps: float = 2.0  # applied to market (entry / stop / reversal) fills
    # Which extreme of a bar is assumed to be touched first when a bar spans
    # both the stop and a target. "sl_first" is the conservative default.
    intrabar_priority: str = "sl_first"  # sl_first | tp_first
    #: Accrue overnight swap (tripled on the broker's rollover day). Forex has no
    #: funding rate; this replaces the crypto engine's ``funding_enabled``.
    swap_enabled: bool = True
    warmup_bars: int = 300
    # Bars of extra history requested before the start date so indicators are warm
    preroll_bars: int = 600

    def __post_init__(self) -> None:
        self.initial_equity = float(self.initial_equity)
        self.taker_fee = float(self.taker_fee)
        self.maker_fee = float(self.maker_fee)
        self.slippage_bps = float(self.slippage_bps)
        self.swap_enabled = bool(self.swap_enabled)
        self.warmup_bars = int(self.warmup_bars)
        self.preroll_bars = int(self.preroll_bars)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None, path: str = "backtest") -> "BacktestParams":
        if not data:
            return cls()
        _reject_unknown(cls, data, path)
        return cls(**dict(data))

    def validate(self) -> None:
        if self.initial_equity <= 0:
            raise ConfigError("backtest.initial_equity must be > 0")
        if not 0 <= self.taker_fee < 0.01:
            raise ConfigError("backtest.taker_fee must be in [0, 0.01)")
        if not 0 <= self.maker_fee < 0.01:
            raise ConfigError("backtest.maker_fee must be in [0, 0.01)")
        if self.slippage_bps < 0:
            raise ConfigError("backtest.slippage_bps must be >= 0")
        if self.intrabar_priority not in ("sl_first", "tp_first"):
            raise ConfigError("backtest.intrabar_priority must be 'sl_first' or 'tp_first'")
        if self.warmup_bars < 0:
            raise ConfigError("backtest.warmup_bars must be >= 0")
        if self.preroll_bars < 0:
            raise ConfigError("backtest.preroll_bars must be >= 0")


# --------------------------------------------------------------------------- #
# Auto calibration (URUZ / THURISAZ)
# --------------------------------------------------------------------------- #
@dataclass
class TuningParams:
    """THURISAZ's Auto Calibration: ADX threshold, ATR factor and stop derived
    from the chart instead of from the settings panel.

    Defaults mirror the Pine source exactly. See :mod:`nornweave_forex.strategy.tuning`
    for what each expression is doing.
    """

    enabled: bool = False

    # ADX threshold = nearest-rank percentile of ADX over this window
    adx_window: int = 300
    adx_percentile: float = 50.0
    adx_fallback: float = 20.5

    # Volatility ratio: ATR(period) as a % of price, against its own average
    atr_period: int = 14
    atr_average_window: int = 200
    atr_factor_base: float = 3.8
    atr_factor_span: float = 0.8

    # Stop = -(smoothed ATR% * multiple), clamped between floor and cap
    stop_basis_window: int = 50
    stop_multiple: float = 3.5
    stop_floor_pct: float = -0.03
    stop_cap_pct: float = -0.005
    stop_fallback_pct: float = -0.01

    def __post_init__(self) -> None:
        self.enabled = bool(self.enabled)
        self.adx_window = int(self.adx_window)
        self.adx_percentile = float(self.adx_percentile)
        self.adx_fallback = float(self.adx_fallback)
        self.atr_period = int(self.atr_period)
        self.atr_average_window = int(self.atr_average_window)
        self.atr_factor_base = float(self.atr_factor_base)
        self.atr_factor_span = float(self.atr_factor_span)
        self.stop_basis_window = int(self.stop_basis_window)
        self.stop_multiple = float(self.stop_multiple)
        self.stop_floor_pct = float(self.stop_floor_pct)
        self.stop_cap_pct = float(self.stop_cap_pct)
        self.stop_fallback_pct = float(self.stop_fallback_pct)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None, path: str = "tuning") -> "TuningParams":
        if not data:
            return cls()
        _reject_unknown(cls, data, path)
        return cls(**dict(data))

    def validate(self) -> None:
        if self.adx_window < 2:
            raise ConfigError("tuning.adx_window must be >= 2")
        if not 0.0 < self.adx_percentile <= 100.0:
            raise ConfigError("tuning.adx_percentile must be in (0, 100]")
        if self.atr_period < 2:
            raise ConfigError("tuning.atr_period must be >= 2")
        if self.atr_average_window < 2:
            raise ConfigError("tuning.atr_average_window must be >= 2")
        if self.atr_factor_base <= 0:
            raise ConfigError("tuning.atr_factor_base must be > 0")
        if self.atr_factor_span < 0:
            raise ConfigError("tuning.atr_factor_span must be >= 0")
        if self.stop_basis_window < 2:
            raise ConfigError("tuning.stop_basis_window must be >= 2")
        if self.stop_multiple <= 0:
            raise ConfigError("tuning.stop_multiple must be > 0")
        if not self.stop_floor_pct < self.stop_cap_pct < 0:
            raise ConfigError(
                "tuning stop bounds must satisfy floor < cap < 0, "
                "e.g. floor -0.20 and cap -0.05"
            )
        if not self.stop_floor_pct <= self.stop_fallback_pct <= self.stop_cap_pct:
            raise ConfigError(
                "tuning.stop_fallback_pct must lie between the floor and the cap"
            )


# --------------------------------------------------------------------------- #
# Root config
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    #: Legacy single-symbol field, kept because the ported strategy modules and
    #: tooling still read it. The live and backtest engines iterate
    #: :attr:`portfolio.symbols` instead.
    symbol: str = "EURUSD"
    interval: str = "120"
    strategy: StrategyParams = field(default_factory=StrategyParams)
    risk: RiskParams = field(default_factory=RiskParams)
    tuning: TuningParams = field(default_factory=TuningParams)
    backtest: BacktestParams = field(default_factory=BacktestParams)
    portfolio: PortfolioParams = field(default_factory=PortfolioParams)
    start: str | None = None  # ISO date, inclusive
    end: str | None = None  # ISO date, exclusive
    data_file: str | None = None  # local CSV/parquet override
    label: str | None = None

    @property
    def symbols(self) -> tuple[str, ...]:
        """The watched pairs. Convenience alias for ``portfolio.symbols``."""
        return tuple(self.portfolio.symbols)

    def __post_init__(self) -> None:
        self.symbol = str(self.symbol).upper()
        self.interval = str(self.interval).upper()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], path: str = "config") -> "Config":
        if not isinstance(data, Mapping):
            raise ConfigError(f"{path}: expected a mapping at the top level")
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ConfigError(f"{path}: unknown top-level key(s) {unknown}. Valid: {sorted(known)}")
        payload = dict(data)
        payload["strategy"] = StrategyParams.from_dict(payload.get("strategy"), "strategy")
        payload["risk"] = RiskParams.from_dict(payload.get("risk"), "risk")
        payload["tuning"] = TuningParams.from_dict(payload.get("tuning"), "tuning")
        payload["backtest"] = BacktestParams.from_dict(payload.get("backtest"), "backtest")
        payload["portfolio"] = PortfolioParams.from_dict(payload.get("portfolio"), "portfolio")
        return cls(**payload)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Config":
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"config file not found: {p}")
        with p.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        cfg = cls.from_dict(raw, path=str(p))
        cfg.validate()
        return cfg

    def validate(self) -> None:
        # Intraday-only: build_daily_context aggregates to daily bars, so weekly
        # and monthly intervals have no daily series to build.
        if interval_ms(self.interval) > DAY_MS:
            raise ConfigError(
                f"interval {self.interval!r} is longer than a day; the daily overlay "
                "needs an intraday series"
            )
        self.strategy.validate()
        self.risk.validate()
        self.tuning.validate()
        self.backtest.validate()
        self.portfolio.validate()
        if self.risk.portfolio_risk_pct + 1e-12 < (
            self.risk.risk_per_trade_pct * self.portfolio.max_positions
        ):
            raise ConfigError(
                f"risk.portfolio_risk_pct ({self.risk.portfolio_risk_pct:.2%}) cannot host "
                f"{self.portfolio.max_positions} positions at "
                f"{self.risk.risk_per_trade_pct:.2%} each"
            )
        if self.symbol not in self.portfolio.symbols:
            raise ConfigError(
                f"config.symbol {self.symbol!r} is not in portfolio.symbols "
                f"{list(self.portfolio.symbols)}"
            )

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)

    def describe(self) -> str:
        s, r, b = self.strategy, self.risk, self.backtest
        lines = [
            f"symbols={list(self.portfolio.symbols)} interval={self.interval} "
            f"max_positions={self.portfolio.max_positions}",
            f"focus_level={s.focus_level} -> ema_period={s.ema_period} "
            f"(scale {s.ema_scale_ratio})",
            f"adx({s.adx_period}) > {s.adx_threshold} | atr({s.atr_period}) x {r.atr_factor}",
            f"stop={r.stop_loss_pct:.2%} be_trigger={r.be_trigger_pct:.2%}",
            f"tp_weights={r.tp_weights} tp_atr_multiples={r.tp_atr_multiples}",
            f"daily_filter={s.daily_filter_enabled} mode={s.daily_fib_mode} "
            f"focus={s.daily_focus_level}",
            f"fee={b.taker_fee:.4%} slippage={b.slippage_bps}bps equity={b.initial_equity:,.2f} "
            f"swap={'on' if b.swap_enabled else 'off'}",
        ]
        if self.tuning.enabled:
            lines.append(
                f"auto_calibration=ON  adx_median({self.tuning.adx_window})  "
                f"atr_factor={self.tuning.atr_factor_base}+/-{self.tuning.atr_factor_span}  "
                f"stop=clamp(-({self.tuning.stop_basis_window}-bar ATR% x "
                f"{self.tuning.stop_multiple}), {self.tuning.stop_floor_pct:.0%}, "
                f"{self.tuning.stop_cap_pct:.0%})"
            )
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Presets for the Forex majors
# --------------------------------------------------------------------------- #
# Ranges are price-fraction based, like the strategy itself: a "sl" of -0.010 on
# EURUSD is ~100 pips, on a 150-yen pair it is ~15 pips. Majors get a tighter
# stop and ADX band than crosses because their spread and trend persistence
# differ; the timeframe ADX value is clamped into the class's own range so a
# recommendation never leaves the interval stated for that class.
_ASSET_PRESETS: dict[str, dict[str, tuple[float, float]]] = {
    "major": {
        "ema": (55, 80),
        "atr": (2.5, 4.0),
        "sl": (-0.012, -0.006),
        "adx": (20, 25),
        "be": (0.008, 0.015),
    },
    "cross": {
        "ema": (45, 65),
        "atr": (3.0, 4.5),
        "sl": (-0.015, -0.008),
        "adx": (22, 27),
        "be": (0.010, 0.018),
    },
}

_TIMEFRAME_ADX: dict[str, float] = {
    "scalp": 17.5,  # 1-5m charts
    "intraday": 20.5,  # 15m-1h charts
    "swing": 22.5,  # 2-4h charts
}


def _mid(bounds: tuple[float, float]) -> float:
    low, high = bounds
    return (low + high) / 2.0


def _check_preset_args(asset_class: str, timeframe: str) -> None:
    if asset_class not in _ASSET_PRESETS:
        raise ConfigError(f"asset_class must be one of {sorted(_ASSET_PRESETS)}")
    if timeframe not in _TIMEFRAME_ADX:
        raise ConfigError(f"timeframe must be one of {sorted(_TIMEFRAME_ADX)}")


def recommended_preset(
    asset_class: str = "major",
    timeframe: str = "swing",
    focus_level: int = 13,
) -> StrategyParams:
    """Mid-points of the Forex preset ranges, as a starting config.

    ``ema_scale_ratio`` is chosen so the resulting ``ema_period`` equals the
    asset class's recommended EMA length.
    """
    _check_preset_args(asset_class, timeframe)
    preset = _ASSET_PRESETS[asset_class]

    adx_low, adx_high = preset["adx"]
    adx = min(max(_TIMEFRAME_ADX[timeframe], adx_low), adx_high)

    params = StrategyParams(focus_level=focus_level, adx_threshold=adx)
    params.ema_scale_ratio = round(_mid(preset["ema"]) / focus_level, 4)
    return params


def recommended_risk_preset(asset_class: str = "major") -> RiskParams:
    """Mid-points of the preset ATR-factor / stop-loss / break-even ranges."""
    _check_preset_args(asset_class, "swing")
    preset = _ASSET_PRESETS[asset_class]
    return RiskParams(
        atr_factor=round(_mid(preset["atr"]), 2),
        stop_loss_pct=round(_mid(preset["sl"]), 4),
        be_trigger_pct=round(_mid(preset["be"]), 4),
    )


def preset_table() -> list[dict[str, float | str]]:
    """Flattened preset table for display."""
    rows: list[dict[str, float | str]] = []
    for asset in _ASSET_PRESETS:
        for timeframe in _TIMEFRAME_ADX:
            strategy = recommended_preset(asset, timeframe)
            risk = recommended_risk_preset(asset)
            rows.append(
                {
                    "asset_class": asset,
                    "timeframe": timeframe,
                    "ema_period": strategy.ema_period,
                    "focus_level": strategy.focus_level,
                    "ema_scale_ratio": strategy.ema_scale_ratio,
                    "adx_threshold": strategy.adx_threshold,
                    "atr_factor": risk.atr_factor,
                    "stop_loss_pct": risk.stop_loss_pct,
                    "be_trigger_pct": risk.be_trigger_pct,
                }
            )
    return rows

"""The NORN WEAVE entry engine.

All three core filters must agree, and the THURISAZ daily overlay must permit the
trade. Every decision returns a ``checks`` mapping so a rejected bar can be
explained without re-running the strategy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from nornweave_forex.config import StrategyParams
from nornweave_forex.strategy.daily import DailyState, resolve_daily_trend
from nornweave_forex.strategy.fibonacci import FibLeg, evaluate_fib
from nornweave_forex.types import Direction, DowTrend, Zone

__all__ = ["SignalContext", "EntrySignal", "SignalEngine", "SIGNAL_CHECKS"]


SIGNAL_CHECKS = (
    "ema_slope",
    "dow_structure",
    "adx",
    "daily_fib",
    "footprint",
    "atr_ready",
)


@dataclass(frozen=True, slots=True)
class SignalContext:
    """Everything the engine may look at, all of it known at the close of bar ``index``."""

    index: int
    ts: int
    close: float
    ema: float
    ema_ref: float  # EMA at index - ema_slope_lookback
    adx: float
    atr: float
    dow: DowTrend
    daily: DailyState | None
    footprint_delta: float | None = None
    #: consecutive rising / falling EMA steps ending at this bar, for
    #: ``ema_slope_mode: "rising"`` (``ta.rising``). ``None`` when not precomputed.
    ema_rise_streak: int | None = None
    ema_fall_streak: int | None = None
    #: Per-bar ADX threshold from Auto Calibration; ``None`` uses the static setting.
    adx_threshold: float | None = None

    @property
    def ema_rising(self) -> bool:
        return bool(np.isfinite(self.ema) and np.isfinite(self.ema_ref) and self.ema > self.ema_ref)

    @property
    def ema_falling(self) -> bool:
        return bool(np.isfinite(self.ema) and np.isfinite(self.ema_ref) and self.ema < self.ema_ref)


@dataclass(frozen=True, slots=True)
class EntrySignal:
    direction: Direction
    allowed: bool
    checks: dict[str, bool]
    blockers: tuple[str, ...]
    tp1_override: float | None
    atr: float
    dow: DowTrend
    daily_zone: Zone = Zone.UNKNOWN
    daily_depth: float | None = None

    def explain(self) -> str:
        verdict = "ALLOW" if self.allowed else "BLOCK"
        return (
            f"{verdict} {self.direction.label} checks="
            + ",".join(f"{k}={'Y' if v else 'N'}" for k, v in self.checks.items())
            + (f" blockers={list(self.blockers)}" if self.blockers else "")
        )

    def as_dict(self) -> dict:
        return {
            "direction": self.direction.label,
            "allowed": self.allowed,
            "checks": dict(self.checks),
            "blockers": list(self.blockers),
            "tp1_override": self.tp1_override,
            "atr": self.atr,
            "dow": self.dow.value,
            "daily_zone": self.daily_zone.value,
            "daily_depth": self.daily_depth,
        }


@dataclass(slots=True)
class _Gate:
    ok: bool
    tp1_override: float | None = None
    zone: Zone = Zone.UNKNOWN
    depth: float | None = None
    blocker: str | None = None


class SignalEngine:
    """Stateless-per-bar evaluator; safe to reuse across bars and instruments."""

    __slots__ = ("params",)

    def __init__(self, params: StrategyParams) -> None:
        params.validate()
        self.params = params

    # -- individual filters -------------------------------------------------- #
    def _adx_ok(self, ctx: SignalContext) -> bool:
        threshold = (
            ctx.adx_threshold if ctx.adx_threshold is not None else self.params.adx_threshold
        )
        return bool(np.isfinite(ctx.adx) and np.isfinite(threshold) and ctx.adx > threshold)

    def _atr_ready(self, ctx: SignalContext) -> bool:
        return bool(np.isfinite(ctx.atr) and ctx.atr > 0)

    def _slope(self, ctx: SignalContext) -> tuple[bool, bool]:
        """``(rising, falling)`` for the configured EMA-slope test.

        ``compare`` uses ``ema[i] vs ema[i - lookback]``; ``rising`` reproduces
        Pine's ``ta.rising(ema, lookback)``, which requires *lookback* consecutive
        rises and is strictly stronger for ``lookback > 1``.
        """
        p = self.params
        if p.ema_slope_mode == "rising":
            up, down = ctx.ema_rise_streak, ctx.ema_fall_streak
            if up is None or down is None:
                # Streaks were not precomputed: fall back to the comparison rather
                # than silently blocking every entry.
                return ctx.ema_rising, ctx.ema_falling
            n = p.ema_slope_lookback
            return bool(up >= n), bool(down >= n)
        return ctx.ema_rising, ctx.ema_falling

    def _footprint_ok(self, ctx: SignalContext, direction: Direction) -> bool:
        p = self.params
        if not p.footprint_enabled:
            return True
        if ctx.footprint_delta is None:
            return p.footprint_missing_policy == "skip"
        if direction is Direction.LONG:
            return ctx.footprint_delta > p.footprint_threshold
        if direction is Direction.SHORT:
            return ctx.footprint_delta < -p.footprint_threshold
        return False

    def _daily_gate(self, ctx: SignalContext, direction: Direction) -> _Gate:
        p = self.params
        if not p.daily_filter_enabled:
            return _Gate(ok=True)

        if ctx.daily is None:
            # No completed daily bar precedes this bar: nothing to compare against.
            return _Gate(
                ok=p.daily_missing_policy == "allow",
                blocker=None if p.daily_missing_policy == "allow" else "daily_history_missing",
            )

        daily_trend = resolve_daily_trend(ctx.daily, p.daily_trend_mode)

        if daily_trend is DowTrend.NEUTRAL:
            # THURISAZ: `dFiboAllow = dSameDir or dTrend == 0 or (anti and not mid)`.
            # An undecided daily trend permits both directions outright.
            if p.daily_neutral_policy == "allow":
                return _Gate(ok=True)
        elif daily_trend.direction is direction:
            # The overlay only applies when the two timeframes disagree.
            return _Gate(ok=True)

        leg: FibLeg | None = ctx.daily.leg
        if leg is None:
            return _Gate(
                ok=p.daily_missing_policy == "allow",
                blocker=None if p.daily_missing_policy == "allow" else "daily_leg_missing",
            )

        depth, zone = evaluate_fib(leg, ctx.close, p.fib_shallow, p.fib_deep)
        if zone is Zone.MID:
            return _Gate(ok=False, zone=zone, depth=depth, blocker="daily_fib_mid_zone")
        if zone is Zone.SHALLOW:
            return _Gate(ok=True, tp1_override=leg.level(p.fib_shallow), zone=zone, depth=depth)
        return _Gate(ok=True, zone=zone, depth=depth)

    # -- public API ---------------------------------------------------------- #
    def evaluate(self, ctx: SignalContext) -> EntrySignal:
        p = self.params
        atr_ready = self._atr_ready(ctx)

        adx_ok = self._adx_ok(ctx)
        ema_up, ema_down = self._slope(ctx)

        if p.require_dow_structure:
            dow_long = ctx.dow is DowTrend.BULLISH
            dow_short = ctx.dow is DowTrend.BEARISH
        else:
            dow_long = dow_short = True

        foot_long = self._footprint_ok(ctx, Direction.LONG)
        foot_short = self._footprint_ok(ctx, Direction.SHORT)
        gate_long = self._daily_gate(ctx, Direction.LONG)
        gate_short = self._daily_gate(ctx, Direction.SHORT)

        long_checks = {
            "ema_slope": ema_up,
            "dow_structure": dow_long,
            "adx": adx_ok,
            "daily_fib": gate_long.ok,
            "footprint": foot_long,
            "atr_ready": atr_ready,
        }
        short_checks = {
            "ema_slope": ema_down,
            "dow_structure": dow_short,
            "adx": adx_ok,
            "daily_fib": gate_short.ok,
            "footprint": foot_short,
            "atr_ready": atr_ready,
        }

        long_ok = all(long_checks.values())
        short_ok = all(short_checks.values())

        if long_ok:
            return self._signal(Direction.LONG, long_checks, gate_long, ctx)
        if short_ok:
            return self._signal(Direction.SHORT, short_checks, gate_short, ctx)

        # Nothing passed: report whichever side came closer, with its blockers.
        if sum(long_checks.values()) >= sum(short_checks.values()):
            checks, gate, attempted = long_checks, gate_long, Direction.LONG
        else:
            checks, gate, attempted = short_checks, gate_short, Direction.SHORT

        blockers = tuple(name for name, ok in checks.items() if not ok)
        if gate.blocker and not checks["daily_fib"]:
            blockers = tuple(dict.fromkeys(blockers + (gate.blocker,)))
        return EntrySignal(
            direction=Direction.FLAT,
            allowed=False,
            checks=checks,
            blockers=blockers,
            tp1_override=None,
            atr=float(ctx.atr) if np.isfinite(ctx.atr) else 0.0,
            dow=ctx.dow,
            daily_zone=gate.zone,
            daily_depth=gate.depth,
        )

    def _signal(
        self,
        direction: Direction,
        checks: dict[str, bool],
        gate: _Gate,
        ctx: SignalContext,
    ) -> EntrySignal:
        return EntrySignal(
            direction=direction,
            allowed=True,
            checks=checks,
            blockers=(),
            tp1_override=gate.tp1_override,
            atr=float(ctx.atr),
            dow=ctx.dow,
            daily_zone=gate.zone,
            daily_depth=gate.depth,
        )

    # -- exit-side structure read -------------------------------------------- #
    def reversal_exit(
        self,
        direction: Direction,
        dow: DowTrend,
        close: float | None = None,
        ema: float | None = None,
    ) -> str | None:
        """Reason the trend says to leave ``direction``, or ``None`` to stay.

        The flavour is chosen by ``strategy.reversal_rule``; ``close`` and ``ema``
        are only needed for ``ema_cross``.
        """
        p = self.params
        if direction is Direction.FLAT or not p.exit_on_dow_reversal:
            return None

        if p.reversal_rule == "none":
            return None

        if p.reversal_rule == "ema_cross":
            if close is None or ema is None:
                return None
            if not (np.isfinite(close) and np.isfinite(ema)):
                return None
            if direction is Direction.LONG and close < ema:
                return "ema_cross"
            if direction is Direction.SHORT and close > ema:
                return "ema_cross"
            return None

        # dow_opposite
        if dow is DowTrend.NEUTRAL:
            return "dow_neutral" if p.exit_on_dow_neutral else None
        if dow.direction is not direction:
            return "dow_reversal"
        return None

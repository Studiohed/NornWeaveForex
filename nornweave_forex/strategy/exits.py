"""The multi-stage exit ladder: TP1/TP2/TP3, fixed-percentage stop, break-even stop."""

from __future__ import annotations

from dataclasses import dataclass, field

from nornweave_forex.config import RiskParams
from nornweave_forex.types import Direction

__all__ = ["ExitLadder", "build_exit_ladder"]


@dataclass(frozen=True, slots=True)
class ExitLadder:
    """Immutable plan computed once, at entry, from the signal bar's ATR."""

    direction: Direction
    entry: float
    atr: float
    stop: float
    tp_prices: tuple[float, ...]
    tp_weights: tuple[float, ...]
    be_trigger_price: float
    #: Where the stop moves once break-even arms. FEHU places it 0.1% *beyond* entry.
    be_stop: float = 0.0
    tp1_overridden: bool = False
    override_rejected: str | None = None
    #: The values actually used, which Auto Calibration may have overridden.
    atr_factor: float = 0.0
    stop_loss_pct: float = 0.0

    @property
    def stop_distance(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def risk_reward(self) -> tuple[float, ...]:
        dist = self.stop_distance
        if dist <= 0:
            return tuple(0.0 for _ in self.tp_prices)
        return tuple(abs(tp - self.entry) / dist for tp in self.tp_prices)

    def as_dict(self) -> dict:
        return {
            "direction": self.direction.label,
            "entry": self.entry,
            "stop": self.stop,
            "tp_prices": list(self.tp_prices),
            "tp_weights": list(self.tp_weights),
            "be_trigger_price": self.be_trigger_price,
            "be_stop": self.be_stop,
            "atr_factor": self.atr_factor,
            "stop_loss_pct": self.stop_loss_pct,
            "tp1_overridden": self.tp1_overridden,
            "override_rejected": self.override_rejected,
            "risk_reward": list(self.risk_reward),
        }


def build_exit_ladder(
    direction: Direction,
    entry: float,
    atr: float,
    risk: RiskParams,
    tp1_override: float | None = None,
    break_even_stop: float | None = None,
    tp_anchor_price: float | None = None,
    atr_factor: float | None = None,
    stop_loss_pct: float | None = None,
) -> ExitLadder:
    """Build the ladder for a new position.

    ``stop_loss_pct`` is stored negative (e.g. -0.05); the stop is placed
    ``|stop_loss_pct|`` away from entry in the adverse direction, and the TP
    levels ``entry + direction * ATR * atr_factor * multiple``.

    ``tp1_override`` is the THURISAZ daily-0.382 level used when the price sits in
    the shallow retracement zone. It is only accepted when it lies strictly
    beyond the entry and inside the ladder (below TP2 for a long, above TP2 for a
    short); otherwise the standard ATR target is kept and the rejection reason is
    recorded on the ladder.

    ``risk.be_buffer_pct`` offsets the break-even stop beyond entry, which is what
    the FEHU source does with ``BE_BUFFER = 0.1``.

    ``atr_factor`` and ``stop_loss_pct`` override the static settings for this one
    ladder. THURISAZ's Auto Calibration passes a value computed from the chart on the
    bar the trade was signalled.
    """
    if direction is Direction.FLAT:
        raise ValueError("cannot build an exit ladder for a flat direction")
    if not entry > 0:
        raise ValueError(f"entry price must be positive, got {entry}")
    if not atr > 0:
        raise ValueError(f"ATR must be positive, got {atr}")

    factor = float(atr_factor) if atr_factor is not None else float(risk.atr_factor)
    stop_value = (
        float(stop_loss_pct) if stop_loss_pct is not None else float(risk.stop_loss_pct)
    )
    if factor <= 0:
        raise ValueError(f"ATR factor must be positive, got {factor}")
    if stop_value >= 0:
        raise ValueError(f"stop loss must be negative, got {stop_value}")

    sign = direction.sign
    stop_pct = abs(stop_value)
    stop = float(break_even_stop if break_even_stop is not None else entry * (1.0 - sign * stop_pct))

    # FEHU anchors its targets to the signal bar's close while the entry fills at the
    # next bar's open, so the two can legitimately differ by the overnight gap.
    anchor = float(tp_anchor_price) if tp_anchor_price is not None else float(entry)

    tp_prices = [float(anchor + sign * atr * factor * mult) for mult in risk.tp_atr_multiples]

    overridden = False
    rejected: str | None = None
    if tp1_override is not None and len(tp_prices) >= 2:
        beyond_entry = sign * (tp1_override - entry) > 0
        inside_ladder = sign * (tp_prices[1] - tp1_override) > 0
        if beyond_entry and inside_ladder:
            tp_prices[0] = float(tp1_override)
            overridden = True
        elif not beyond_entry:
            rejected = "tp1_override_not_beyond_entry"
        else:
            rejected = "tp1_override_beyond_tp2"
    elif tp1_override is not None:
        rejected = "tp1_override_needs_at_least_two_targets"

    be_trigger = float(entry * (1.0 + sign * risk.be_trigger_pct))
    be_stop = float(entry * (1.0 + sign * risk.be_buffer_pct))

    return ExitLadder(
        direction=direction,
        entry=float(entry),
        atr=float(atr),
        stop=stop,
        tp_prices=tuple(tp_prices),
        tp_weights=tuple(float(w) for w in risk.tp_weights),
        be_trigger_price=be_trigger,
        be_stop=be_stop,
        tp1_overridden=overridden,
        override_rejected=rejected,
        atr_factor=factor,
        stop_loss_pct=-stop_pct,
    )

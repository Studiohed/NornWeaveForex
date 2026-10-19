"""Bar-level execution rules, shared by the backtester and any live executor.

Why this module exists
----------------------
How a bar that spans *both* the protective stop and a profit target is resolved is
the single most consequential assumption in this project: switching from
"the stop came first" to "the target came first" changed measured return by a factor
of two on DOGE 2H. That rule therefore has to exist **exactly once**. If the
backtester and the live executor each implemented it, they would drift, and the
drift would only show up as real money.

Everything here is a pure function of numbers. No candles, no broker, no I/O, so it
can be tested exhaustively and reused unchanged by a paper broker or a real one.
"""

from __future__ import annotations

from dataclasses import dataclass

from nornweave_forex.types import Direction, ExitReason

__all__ = [
    "ExitPlan",
    "PositionView",
    "plan_intrabar_exits",
    "stop_breached_on_close",
    "break_even_should_arm",
    "effective_stop_trigger",
    "tp_reason",
]

_TP_REASONS = (ExitReason.TAKE_PROFIT_1, ExitReason.TAKE_PROFIT_2, ExitReason.TAKE_PROFIT_3)


def tp_reason(level: int) -> ExitReason:
    """Reason label for take-profit ``level`` (0-based); deeper levels reuse TP3."""
    return _TP_REASONS[level] if level < len(_TP_REASONS) else ExitReason.TAKE_PROFIT_3


@dataclass(frozen=True, slots=True)
class ExitPlan:
    """One intended execution: what, at what price, for how much."""

    reason: ExitReason
    price: float
    qty: float
    #: Market orders suffer slippage; resting limit targets do not.
    slippage: bool
    #: Take-profit rung index (0-based) for target exits, else ``None``.
    level: int | None = None


@dataclass(frozen=True, slots=True)
class PositionView:
    """The subset of position state the bar rules need. Direction-agnostic."""

    direction: Direction
    entry_price: float
    qty: float
    initial_qty: float
    stop: float
    tp_prices: tuple[float, ...]
    tp_weights: tuple[float, ...]
    tps_hit: tuple[bool, ...]
    be_active: bool = False

    @property
    def long(self) -> bool:
        return self.direction is Direction.LONG

    @property
    def flat(self) -> bool:
        return self.qty <= 0


def effective_stop_trigger(
    *, be_active: bool, stop_trigger: str, be_exit_trigger: str = "follow"
) -> str:
    """Whether the protective stop is an intrabar or a close-triggered event.

    The FEHU source is asymmetric here and it is not a typo: the protective stop is
    evaluated from **close** (``PnL < stopLoss`` where ``PnL`` comes from ``close``),
    while the break-even stop is evaluated from the bar's **extreme**
    (``low <= bePrice``). Both then exit with ``strategy.close()``, i.e. at market on
    the bar's close.
    """
    if be_active and be_exit_trigger != "follow":
        return be_exit_trigger
    return stop_trigger


def plan_intrabar_exits(
    position: PositionView,
    *,
    bar_open: float,
    bar_high: float,
    bar_low: float,
    bar_close: float | None = None,
    intrabar_priority: str = "sl_first",
    stop_trigger: str = "intrabar",
    exit_fill: str = "level",
    be_exit_trigger: str = "follow",
    tp_weight_basis: str = "initial",
) -> tuple[ExitPlan, ...]:
    """Exits whose price is knowable from the bar's OHLC, in execution order.

    ``intrabar_priority`` decides the ordering when both the stop and a target are
    inside the bar's range:

    * ``sl_first`` — assume the adverse extreme came first. Conservative: it can
      only ever make a backtest worse than reality.
    * ``tp_first``  — assume the favourable extreme came first.

    ``stop_trigger`` decides *whether* the protective stop is an intrabar event at
    all. With ``close`` the stop is not evaluated here; the caller checks
    :func:`stop_breached_on_close` on the closed bar instead.

    ``exit_fill`` decides where the fills happen:

    * ``level`` — a target is a resting limit order and fills at its own price.
    * ``bar_close`` — every exit is a market order at the bar's close and only the
      *trigger* is intrabar. This is what the FEHU Pine does with ``strategy.close()``,
      and it moves measured results more than most parameters do.
    """
    if position.flat or position.direction is Direction.FLAT:
        return ()

    long = position.long
    stop = position.stop
    qty_left = position.qty
    tps_hit = list(position.tps_hit)
    exits: list[ExitPlan] = []
    at_close = exit_fill == "bar_close"
    if at_close and bar_close is None:
        raise ValueError("exit_fill='bar_close' needs the bar's close price")

    trigger = effective_stop_trigger(
        be_active=position.be_active,
        stop_trigger=stop_trigger,
        be_exit_trigger=be_exit_trigger,
    )

    def stop_hit() -> bool:
        if trigger == "close":
            return False  # decided at the bar close by the caller, not here
        return bar_low <= stop if long else bar_high >= stop

    def fire_stop() -> None:
        nonlocal qty_left
        reason = ExitReason.BREAK_EVEN_STOP if position.be_active else ExitReason.STOP_LOSS
        if at_close:
            price = float(bar_close)
        else:
            # A gap past the stop fills at the open, which is worse than the stop price.
            price = min(bar_open, stop) if long else max(bar_open, stop)
        exits.append(ExitPlan(reason, price, qty_left, True))
        qty_left = 0.0

    def fire_targets() -> None:
        nonlocal qty_left
        for level, target in enumerate(position.tp_prices):
            if qty_left <= 0:
                return
            if level < len(tps_hit) and tps_hit[level]:
                continue
            reached = bar_high >= target if long else bar_low <= target
            if not reached:
                continue
            if at_close:
                price = float(bar_close)
            else:
                # A gap through the target fills the resting limit at the open (better).
                price = max(bar_open, target) if long else min(bar_open, target)
            # "current" reads Pine's `qty_percent` as a fraction of what is still open.
            basis = qty_left if tp_weight_basis == "current" else position.initial_qty
            portion = min(basis * position.tp_weights[level], qty_left)
            if portion <= 0:
                continue
            # A resting limit target suffers no slippage; a market-at-close exit does.
            exits.append(
                ExitPlan(tp_reason(level), price, portion, at_close, level)
            )
            qty_left -= portion
            if level < len(tps_hit):
                tps_hit[level] = True

    check_stop_intrabar = trigger == "intrabar"
    if check_stop_intrabar and intrabar_priority == "sl_first":
        if stop_hit():
            fire_stop()
        else:
            fire_targets()
    else:
        fire_targets()
        if qty_left > 0 and check_stop_intrabar and stop_hit():
            fire_stop()

    return tuple(exits)


def stop_breached_on_close(direction: Direction, stop: float, close: float) -> bool:
    """Whether a closed bar ended beyond the protective stop.

    Used only when ``stop_trigger == "close"``. Remember that in that mode nothing
    is resting on the exchange: the caller is responsible for actually getting out.
    """
    if direction is Direction.LONG:
        return close <= stop
    if direction is Direction.SHORT:
        return close >= stop
    return False


def break_even_should_arm(
    *,
    be_mode: str,
    direction: Direction,
    bar_high: float,
    bar_low: float,
    be_trigger_price: float,
    tps_hit: tuple[bool, ...],
    arm_on: str = "extreme",
    bar_close: float | None = None,
) -> bool:
    """Whether the break-even stop should be armed from this closed bar.

    Arming is decided at the close and takes effect from the next bar, which removes
    any need to guess the intrabar ordering of "the trigger was reached" versus
    "the stop was reached".

    ``arm_on="close"`` reproduces the FEHU source, where the trigger is
    ``PnL >= beThresh`` with ``PnL`` computed from the bar's **close** rather than
    from the bar's extreme.
    """
    if be_mode == "after_tp1":
        return bool(tps_hit) and bool(tps_hit[0])

    if arm_on == "close":
        if bar_close is None:
            raise ValueError("be_arm_on='close' needs the bar's close price")
        if direction is Direction.LONG:
            return float(bar_close) >= be_trigger_price
        if direction is Direction.SHORT:
            return float(bar_close) <= be_trigger_price
        return False

    if direction is Direction.LONG:
        return bar_high >= be_trigger_price
    if direction is Direction.SHORT:
        return bar_low <= be_trigger_price
    return False

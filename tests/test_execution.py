"""Exhaustive tests for the shared bar rules.

Ported verbatim from the reference project's ``tests/test_execution.py`` — the only
change is the package name (``nornweave`` -> ``nornweave_forex``), because
``execution.py`` is byte-identical to the source.

This is the highest-leverage code in the project: the backtester and any live
executor both depend on it, and the ordering assumption it encodes moved measured
return by a factor of two. It is a pure function of numbers, so every branch is
enumerated here rather than sampled.
"""

from __future__ import annotations

import pytest

from nornweave_forex.execution import (
    ExitPlan,
    PositionView,
    break_even_should_arm,
    effective_stop_trigger,
    plan_intrabar_exits,
    stop_breached_on_close,
    tp_reason,
)
from nornweave_forex.types import Direction, ExitReason


def long_position(**overrides) -> PositionView:
    base = dict(
        direction=Direction.LONG,
        entry_price=100.0,
        qty=100.0,
        initial_qty=100.0,
        stop=95.0,
        tp_prices=(103.0, 106.0, 109.0),
        tp_weights=(0.3, 0.3, 0.3),
        tps_hit=(False, False, False),
        be_active=False,
    )
    base.update(overrides)
    return PositionView(**base)


def short_position(**overrides) -> PositionView:
    base = dict(
        direction=Direction.SHORT,
        entry_price=100.0,
        qty=100.0,
        initial_qty=100.0,
        stop=105.0,
        tp_prices=(97.0, 94.0, 91.0),
        tp_weights=(0.3, 0.3, 0.3),
        tps_hit=(False, False, False),
        be_active=False,
    )
    base.update(overrides)
    return PositionView(**base)


def plan(position, o, h, low, **kw):
    return plan_intrabar_exits(
        position, bar_open=o, bar_high=h, bar_low=low, **kw
    )


# --------------------------------------------------------------------------- #
# basic shapes
# --------------------------------------------------------------------------- #
def test_flat_position_plans_nothing():
    assert plan(long_position(qty=0.0), 100.0, 110.0, 90.0) == ()
    assert plan(long_position(direction=Direction.FLAT), 100.0, 110.0, 90.0) == ()


def test_bar_inside_the_range_does_nothing():
    assert plan(long_position(), 100.0, 102.0, 96.0) == ()


def test_single_target_fill_is_a_partial_limit_exit():
    exits = plan(long_position(), 100.0, 103.5, 99.0)
    assert len(exits) == 1
    assert exits[0] == ExitPlan(ExitReason.TAKE_PROFIT_1, 103.0, pytest.approx(30.0), False, 0)


def test_all_three_targets_fill_in_order():
    exits = plan(long_position(), 100.0, 110.0, 99.0)
    assert [e.reason for e in exits] == [
        ExitReason.TAKE_PROFIT_1,
        ExitReason.TAKE_PROFIT_2,
        ExitReason.TAKE_PROFIT_3,
    ]
    assert [e.level for e in exits] == [0, 1, 2]
    assert sum(e.qty for e in exits) == pytest.approx(90.0)  # 10% runner stays
    assert all(not e.slippage for e in exits)


def test_already_hit_target_is_skipped():
    exits = plan(long_position(tps_hit=(True, False, False)), 100.0, 110.0, 99.0)
    assert [e.level for e in exits] == [1, 2]


def test_gap_through_a_target_fills_at_the_open_not_the_target():
    # Opened at 105, above TP1 (103) and TP2 (106)? 105 is below 106, so only TP1.
    exits = plan(long_position(), 105.0, 105.5, 104.0)
    assert len(exits) == 1
    assert exits[0].price == pytest.approx(105.0)  # the open, i.e. price improvement


# --------------------------------------------------------------------------- #
# stop behaviour
# --------------------------------------------------------------------------- #
def test_stop_at_the_stop_price_when_not_gapped():
    exits = plan(long_position(), 100.0, 100.5, 94.0)
    assert exits == (ExitPlan(ExitReason.STOP_LOSS, 95.0, 100.0, True),)


def test_gap_below_the_stop_fills_at_the_open():
    exits = plan(long_position(), 92.0, 93.0, 91.0)
    assert exits[0].price == pytest.approx(92.0)
    assert exits[0].qty == pytest.approx(100.0)


def test_break_even_armed_stop_reports_the_break_even_reason():
    exits = plan(long_position(be_active=True, stop=100.0), 100.0, 100.5, 99.0)
    assert exits[0].reason is ExitReason.BREAK_EVEN_STOP


def test_close_trigger_ignores_the_intrabar_stop():
    """With stop_trigger=close a wick through the stop plans nothing."""
    exits = plan(long_position(), 100.0, 100.5, 90.0, stop_trigger="close")
    assert exits == ()


# --------------------------------------------------------------------------- #
# the ordering assumption
# --------------------------------------------------------------------------- #
def test_bar_spanning_stop_and_targets_resolves_by_priority():
    """The bar touches both sides; the priority setting decides the outcome."""
    position = long_position()
    assert plan(position, 100.0, 110.0, 90.0, intrabar_priority="sl_first") == (
        ExitPlan(ExitReason.STOP_LOSS, 95.0, 100.0, True),
    )

    exits = plan(position, 100.0, 110.0, 90.0, intrabar_priority="tp_first")
    # Targets fill first, then the stop takes only what is left (the 10% runner).
    assert [e.reason for e in exits] == [
        ExitReason.TAKE_PROFIT_1,
        ExitReason.TAKE_PROFIT_2,
        ExitReason.TAKE_PROFIT_3,
        ExitReason.STOP_LOSS,
    ]
    assert exits[-1].qty == pytest.approx(10.0)


def test_tp_first_does_not_double_count_quantity():
    exits = plan(long_position(), 100.0, 110.0, 90.0, intrabar_priority="tp_first")
    assert sum(e.qty for e in exits) == pytest.approx(100.0)


# --------------------------------------------------------------------------- #
# shorts mirror longs
# --------------------------------------------------------------------------- #
def test_short_stop_gap_fills_at_the_open():
    exits = plan(short_position(), 108.0, 109.0, 107.0)
    assert exits[0].reason is ExitReason.STOP_LOSS
    assert exits[0].price == pytest.approx(108.0)


def test_short_targets_fill_downward():
    exits = plan(short_position(), 100.0, 101.0, 90.0)
    assert [e.price for e in exits] == [97.0, 94.0, 91.0]
    assert all(e.reason is not ExitReason.STOP_LOSS for e in exits)


def test_short_tp_first_ordering():
    exits = plan(short_position(), 100.0, 106.0, 90.0, intrabar_priority="tp_first")
    assert exits[-1].reason is ExitReason.STOP_LOSS
    assert exits[-1].qty == pytest.approx(10.0)


def test_weights_that_do_not_leave_a_runner_close_the_position():
    full = long_position(tp_weights=(0.5, 0.5, 0.0))
    exits = plan(full, 100.0, 106.5, 99.0)
    assert sum(e.qty for e in exits) == pytest.approx(100.0)


def test_quantity_never_exceeds_the_remaining_position():
    exits = plan(long_position(tp_weights=(0.9, 0.9, 0.9)), 100.0, 110.0, 99.0)
    assert sum(e.qty for e in exits) <= 100.0 + 1e-9


# --------------------------------------------------------------------------- #
# close-breach and break-even helpers
# --------------------------------------------------------------------------- #
def test_stop_breached_on_close_is_directional():
    assert stop_breached_on_close(Direction.LONG, 95.0, 94.9)
    assert not stop_breached_on_close(Direction.LONG, 95.0, 95.1)
    assert stop_breached_on_close(Direction.SHORT, 105.0, 105.1)
    assert not stop_breached_on_close(Direction.SHORT, 105.0, 104.9)
    assert not stop_breached_on_close(Direction.FLAT, 95.0, 1.0)


def test_break_even_arms_on_the_favourable_extreme():
    assert break_even_should_arm(
        be_mode="profit_pct",
        direction=Direction.LONG,
        bar_high=109.0,
        bar_low=100.0,
        be_trigger_price=108.0,
        tps_hit=(False, False, False),
    )
    assert not break_even_should_arm(
        be_mode="profit_pct",
        direction=Direction.LONG,
        bar_high=107.0,
        bar_low=100.0,
        be_trigger_price=108.0,
        tps_hit=(False, False, False),
    )
    assert break_even_should_arm(
        be_mode="profit_pct",
        direction=Direction.SHORT,
        bar_high=100.0,
        bar_low=92.0,
        be_trigger_price=93.0,
        tps_hit=(False, False, False),
    )


def test_break_even_after_tp1_mode_ignores_the_price_trigger():
    assert break_even_should_arm(
        be_mode="after_tp1",
        direction=Direction.LONG,
        bar_high=100.0,
        bar_low=100.0,
        be_trigger_price=999.0,
        tps_hit=(True, False, False),
    )
    assert not break_even_should_arm(
        be_mode="after_tp1",
        direction=Direction.LONG,
        bar_high=999.0,
        bar_low=999.0,
        be_trigger_price=1.0,
        tps_hit=(False, True, True),
    )


def test_tp_reason_saturates_beyond_the_third_rung():
    assert tp_reason(0) is ExitReason.TAKE_PROFIT_1
    assert tp_reason(1) is ExitReason.TAKE_PROFIT_2
    assert tp_reason(2) is ExitReason.TAKE_PROFIT_3
    assert tp_reason(9) is ExitReason.TAKE_PROFIT_3


# --------------------------------------------------------------------------- #
# exit_fill = "bar_close" — what the FEHU source does with strategy.close()
# --------------------------------------------------------------------------- #
def test_bar_close_fill_prices_every_exit_at_the_close():
    exits = plan(
        long_position(),
        100.0,
        110.0,
        90.0,
        bar_close=104.0,
        stop_trigger="close",
        exit_fill="bar_close",
    )
    assert [e.reason for e in exits] == [
        ExitReason.TAKE_PROFIT_1,
        ExitReason.TAKE_PROFIT_2,
        ExitReason.TAKE_PROFIT_3,
    ]
    assert all(e.price == pytest.approx(104.0) for e in exits)
    # A market-at-close exit suffers slippage, unlike a resting limit target.
    assert all(e.slippage for e in exits)


def test_bar_close_fill_still_triggers_intrabar():
    """The trigger is the bar's extreme; only the fill moves to the close."""
    exits = plan(
        long_position(be_active=True, stop=100.0),
        100.0,
        100.5,
        99.0,
        bar_close=103.0,
        exit_fill="bar_close",
    )
    assert len(exits) == 1
    assert exits[0].reason is ExitReason.BREAK_EVEN_STOP
    assert exits[0].price == pytest.approx(103.0)


def test_bar_close_fill_needs_the_close_price():
    with pytest.raises(ValueError, match="needs the bar's close"):
        plan(long_position(), 100.0, 110.0, 90.0, exit_fill="bar_close")


# --------------------------------------------------------------------------- #
# tp_weight_basis
# --------------------------------------------------------------------------- #
def test_initial_weight_basis_takes_thirty_percent_of_the_original_each_time():
    exits = plan(long_position(), 100.0, 110.0, 99.0, stop_trigger="close")
    assert [e.qty for e in exits] == pytest.approx([30.0, 30.0, 30.0])


def test_current_weight_basis_compounds_off_the_remaining_size():
    exits = plan(
        long_position(), 100.0, 110.0, 99.0, stop_trigger="close", tp_weight_basis="current"
    )
    # 30% of 100, then 30% of 70, then 30% of 49 -> a 34.3% runner survives.
    assert [e.qty for e in exits] == pytest.approx([30.0, 21.0, 14.7])
    assert sum(e.qty for e in exits) == pytest.approx(65.7)


# --------------------------------------------------------------------------- #
# effective_stop_trigger and the close-armed break-even
# --------------------------------------------------------------------------- #
def test_effective_stop_trigger_follows_the_position_state():
    assert (
        effective_stop_trigger(be_active=False, stop_trigger="close", be_exit_trigger="intrabar")
        == "close"
    )
    # Pine's break-even check is `low <= bePrice`, i.e. intrabar, even though the
    # protective stop is close-triggered.
    assert (
        effective_stop_trigger(be_active=True, stop_trigger="close", be_exit_trigger="intrabar")
        == "intrabar"
    )
    assert (
        effective_stop_trigger(be_active=True, stop_trigger="close", be_exit_trigger="follow")
        == "close"
    )


def test_close_armed_break_even_ignores_a_wick_through_the_trigger():
    common = dict(
        be_mode="profit_pct",
        direction=Direction.LONG,
        be_trigger_price=110.0,
        tps_hit=(False, False, False),
        arm_on="close",
    )
    # the high reached 120, but the bar closed back at 105
    assert not break_even_should_arm(bar_high=120.0, bar_low=99.0, bar_close=105.0, **common)
    assert break_even_should_arm(bar_high=120.0, bar_low=99.0, bar_close=115.0, **common)


def test_close_armed_break_even_mirrors_for_shorts():
    common = dict(
        be_mode="profit_pct",
        direction=Direction.SHORT,
        be_trigger_price=90.0,
        tps_hit=(False, False, False),
        arm_on="close",
    )
    assert break_even_should_arm(bar_high=101.0, bar_low=80.0, bar_close=88.0, **common)
    assert not break_even_should_arm(bar_high=101.0, bar_low=80.0, bar_close=95.0, **common)


def test_close_armed_break_even_needs_the_close():
    with pytest.raises(ValueError, match="needs the bar's close"):
        break_even_should_arm(
            be_mode="profit_pct",
            direction=Direction.LONG,
            bar_high=1.0,
            bar_low=1.0,
            be_trigger_price=1.0,
            tps_hit=(False, False, False),
            arm_on="close",
        )

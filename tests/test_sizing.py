"""Forex position-sizing tests.

``qty`` is lots, risk is measured in pips through the broker's
``pip_value_per_lot``, and margin is the account-currency notional
(``InstrumentSpec.notional_account``) divided by leverage. These tests lock down
the numbers a live entry is sized from: the risk-based lot amount, the portfolio
cap, the broker minimum, and the two margin caps (broker ``free_margin`` when
available, otherwise the documented ``equity * leverage`` fallback).
"""

from __future__ import annotations

import dataclasses

import pytest

from nornweave_forex.config import RiskParams
from nornweave_forex.risk.sizing import SizeDecision, position_size

EQUITY = 10_000.0
#: 1.10 -> 1.09 on a 5-digit EURUSD pair is exactly 100 pips.
EURUSD_ENTRY = 1.10
EURUSD_STOP_100_PIPS = 1.09


def _risk(**overrides) -> RiskParams:
    """Default RiskParams (1% per trade, 5% portfolio, 30x leverage) with overrides."""
    return RiskParams(**overrides)


def _decision(**kwargs) -> SizeDecision:
    kwargs.setdefault("equity", EQUITY)
    kwargs.setdefault("entry", EURUSD_ENTRY)
    kwargs.setdefault("stop", EURUSD_STOP_100_PIPS)
    kwargs.setdefault("risk", _risk())
    return position_size(**kwargs)


# --------------------------------------------------------------------------- #
# Risk-based sizing
# --------------------------------------------------------------------------- #
def test_eurusd_one_percent_one_hundred_pip_stop_is_tenth_of_a_lot(eurusd_spec) -> None:
    """$100 risk / (100 pips * $10/pip) = 0.10 lots."""
    decision = _decision(instrument=eurusd_spec)

    assert decision.ok
    assert decision.lots == pytest.approx(0.10)
    assert decision.risk_amount == pytest.approx(100.0)
    # 0.10 lots * 100,000 units = EUR 10,000, i.e. USD 11,000 at 1.10.
    assert decision.notional_base == pytest.approx(10_000.0)
    assert eurusd_spec.notional_account(EURUSD_ENTRY, decision.lots) == pytest.approx(11_000.0)
    # Margin uses the account-currency notional, not base-currency units.
    assert decision.margin_required == pytest.approx(11_000.0 / _risk().leverage)


def test_usdjpy_pip_value_changes_the_lot_amount(eurusd_spec, usdjpy_spec) -> None:
    """A JPY pip is worth ~$6.667, so the same $ risk needs *more* lots.

    (The task brief said "fewer lots"; the direction is the other way round:
    lower pip value -> more lots for a fixed dollar risk. The test asserts the
    relationship the math actually requires, plus the pip-value usage.)
    """
    eurusd = _decision(instrument=eurusd_spec)
    usdjpy = _decision(entry=150.0, stop=149.0, instrument=usdjpy_spec)  # 100 pips

    assert usdjpy_spec.pip_value_per_lot < eurusd_spec.pip_value_per_lot
    assert usdjpy.ok and eurusd.ok
    assert usdjpy.lots > eurusd.lots
    # Risk is the same $100 target, floored to the 0.01 lot step.
    assert usdjpy.risk_amount == pytest.approx(
        usdjpy.lots * 100 * usdjpy_spec.pip_value_per_lot
    )
    assert 90.0 < usdjpy.risk_amount <= 100.0
    # ... and it is not accidentally using the USD-quoted $10/pip.
    assert usdjpy.risk_amount != pytest.approx(usdjpy.lots * 1000.0)


# --------------------------------------------------------------------------- #
# Portfolio cap
# --------------------------------------------------------------------------- #
def test_portfolio_cap_reduces_the_allowed_risk(eurusd_spec) -> None:
    # 5% of 10k = $500 budget; $450 already at risk leaves $50.
    decision = _decision(instrument=eurusd_spec, open_risk=450.0)

    assert decision.ok
    assert decision.risk_amount == pytest.approx(50.0)
    assert decision.lots == pytest.approx(0.05)
    assert 450.0 + decision.risk_amount <= 500.0 + 1e-9


def test_portfolio_risk_exhausted_is_rejected(eurusd_spec) -> None:
    decision = _decision(instrument=eurusd_spec, open_risk=500.0)

    assert decision.rejected == "portfolio_risk_exhausted"
    assert decision.lots == 0.0
    assert not decision.ok


@pytest.mark.parametrize("open_risk", [0.0, 100.0, 250.0, 400.0, 450.0, 490.0])
def test_portfolio_cap_is_never_exceeded(eurusd_spec, open_risk: float) -> None:
    decision = _decision(instrument=eurusd_spec, open_risk=open_risk)
    if decision.ok:
        assert open_risk + decision.risk_amount <= 500.0 + 1e-6


# --------------------------------------------------------------------------- #
# Broker minimum lot
# --------------------------------------------------------------------------- #
def test_tiny_risk_is_bumped_up_to_min_lot(eurusd_spec) -> None:
    # $10 risk with a 200-pip stop = 0.005 lots -> bumped to the 0.01 minimum ($20 risk).
    decision = position_size(
        equity=1_000.0, entry=1.10, stop=1.08, instrument=eurusd_spec, risk=_risk()
    )

    assert decision.ok
    assert decision.lots == pytest.approx(0.01)
    assert decision.risk_amount == pytest.approx(20.0)


def test_min_lot_bump_above_max_risk_cap_is_rejected(eurusd_spec) -> None:
    risk = _risk(max_risk_per_trade_pct=0.01)  # cap = $10 on 1k equity
    decision = position_size(
        equity=1_000.0, entry=1.10, stop=1.08, instrument=eurusd_spec, risk=risk
    )

    assert decision.rejected == "min_lot_risk_above_cap"
    assert decision.lots == 0.0


def test_min_lot_bump_above_remaining_portfolio_is_rejected(eurusd_spec) -> None:
    # 5% of 1k = $50 budget, $49 already at risk -> only $1 of budget remains,
    # but the minimum lot would risk $20.
    decision = position_size(
        equity=1_000.0,
        entry=1.10,
        stop=1.08,
        instrument=eurusd_spec,
        risk=_risk(),
        open_risk=49.0,
    )

    assert decision.rejected == "min_lot_risk_above_portfolio"
    assert decision.lots == 0.0


# --------------------------------------------------------------------------- #
# Margin caps
# --------------------------------------------------------------------------- #
def test_free_margin_cap_scales_the_lots(eurusd_spec) -> None:
    uncapped = _decision(instrument=eurusd_spec, free_margin=1_000_000.0)
    capped = _decision(instrument=eurusd_spec, free_margin=100.0)

    assert uncapped.lots == pytest.approx(0.10)
    assert capped.ok
    assert capped.lots < uncapped.lots
    assert capped.lots == pytest.approx(0.02)
    assert capped.margin_required <= 100.0 + 1e-9
    assert (
        eurusd_spec.notional_account(EURUSD_ENTRY, capped.lots) / _risk().leverage
        <= 100.0 + 1e-9
    )


def test_margin_cap_below_min_lot_is_insufficient_margin(eurusd_spec) -> None:
    # Uncapped margin is ~$366.67; $10 of free margin scales 0.10 lots to ~0.0027.
    decision = _decision(instrument=eurusd_spec, free_margin=10.0)

    assert decision.rejected == "insufficient_margin"
    assert decision.lots == 0.0


def test_fallback_cap_is_equity_times_leverage(eurusd_spec) -> None:
    # No broker number: cap notional at 10k * 0.5 = $5,000. The 0.10-lot idea is
    # $11,000 notional, so it scales to the 0.04 lot step (=$4,400 notional).
    decision = _decision(instrument=eurusd_spec, risk=_risk(leverage=0.5))

    assert decision.ok
    assert decision.lots == pytest.approx(0.04)
    assert eurusd_spec.notional_account(EURUSD_ENTRY, decision.lots) <= 5_000.0 + 1e-9


def test_max_lot_clamp_leaves_a_consistent_decision(eurusd_spec) -> None:
    # 10-pip stop -> risk-sized 1.00 lots, clamped to the broker's 0.05 max.
    small_max = dataclasses.replace(eurusd_spec, max_lot=0.05)
    decision = _decision(entry=1.10, stop=1.099, instrument=small_max)

    assert decision.ok
    assert decision.lots == pytest.approx(0.05)
    assert decision.notional_base == pytest.approx(0.05 * 100_000.0)
    assert decision.risk_amount == pytest.approx(5.0)  # 10 pips * $10/pip * 0.05
    assert decision.margin_required == pytest.approx(
        small_max.notional_account(1.10, 0.05) / _risk().leverage
    )


# --------------------------------------------------------------------------- #
# Input rejections
# --------------------------------------------------------------------------- #
def test_rejections_for_bad_inputs(eurusd_spec) -> None:
    cases = [
        ({"equity": 0.0}, "non_positive_equity"),
        ({"equity": -1.0}, "non_positive_equity"),
        ({"entry": 0.0}, "non_positive_entry"),
        ({"stop": EURUSD_ENTRY}, "zero_stop_distance"),
        ({"instrument": dataclasses.replace(eurusd_spec, pip=0.0)}, "invalid_pip"),
        (
            {"instrument": dataclasses.replace(eurusd_spec, pip_value_per_lot=0.0)},
            "invalid_pip_value",
        ),
    ]
    for overrides, expected in cases:
        kwargs = {"instrument": eurusd_spec, **overrides}
        decision = _decision(**kwargs)
        assert decision.rejected == expected, overrides
        assert decision.lots == 0.0
        assert not decision.ok

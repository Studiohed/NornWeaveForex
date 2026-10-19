"""Forex instrument and money-math tests.

The whole point of this file is the unit conversion: after the port, ``qty`` is
lots, so a price difference must go through ``pip_value_per_lot`` (which MT5
denominates in account currency). A 10-pip move on 0.10 lots of EURUSD is $10,
not $0.001 — that factor of ``contract_size`` is the bug these tests lock down.
"""

from __future__ import annotations

import pytest

from nornweave_forex.instrument import (
    InstrumentSpec,
    default_instrument,
    pip_size,
    quote_currency,
)


def test_pip_size_5_digit_and_3_digit() -> None:
    assert pip_size(5, 0.00001) == pytest.approx(0.0001)
    assert pip_size(3, 0.001) == pytest.approx(0.01)
    assert pip_size(4, 0.0001) == pytest.approx(0.0001)
    assert pip_size(2, 0.01) == pytest.approx(0.01)


def test_quote_currency() -> None:
    assert quote_currency("EURUSD") == "USD"
    assert quote_currency("usdjpy") == "JPY"
    assert quote_currency("EURJPY") == "JPY"


def test_default_instrument_pip_values_are_not_hardcoded() -> None:
    eurusd = default_instrument("EURUSD")
    usdjpy = default_instrument("USDJPY")
    eurjpy = default_instrument("EURJPY")
    usdcad = default_instrument("USDCAD")

    # USD-quoted: $10 per pip per standard lot.
    assert eurusd.pip_value_per_lot == pytest.approx(10.0)
    # JPY-quoted: 1000 / USDJPY at the reference 150 rate, not 10.
    assert usdjpy.pip_value_per_lot == pytest.approx(1000.0 / 150.0)
    assert eurjpy.pip_value_per_lot == pytest.approx(1000.0 / 150.0)
    assert usdcad.pip_value_per_lot == pytest.approx(10.0 / 1.36, rel=1e-6)
    assert eurusd.contract_size == 100_000.0


def test_pnl_money_eurusd_ten_pips() -> None:
    spec = default_instrument("EURUSD")
    # Long 0.10 lots, price 1.10000 -> 1.10100 = 10 pips.
    assert spec.pnl_money(1.10000, 1.10100, 0.10, +1) == pytest.approx(10.0)
    # The same move against a short is a $10 loss.
    assert spec.pnl_money(1.10000, 1.10100, 0.10, -1) == pytest.approx(-10.0)


def test_pnl_money_usdjpy_ten_pips() -> None:
    spec = default_instrument("USDJPY")
    # 150.000 -> 150.100 is 10 pips; 0.1 lot * 6.667/pip = $6.667.
    pnl = spec.pnl_money(150.000, 150.100, 0.10, +1)
    assert pnl == pytest.approx(100.0 / 15.0, rel=1e-9)
    # Sanity: it must be materially less than the USD-quoted $10.
    assert pnl < 9.0


def test_notional_account_matches_contract_times_price() -> None:
    spec = default_instrument("EURUSD")
    # 0.10 lots at 1.10 = EUR 10,000 = USD 11,000.
    assert spec.notional_account(1.10, 0.10) == pytest.approx(11_000.0)
    usdjpy = default_instrument("USDJPY")
    # 0.10 lots of USDJPY at 150 = USD 10,000 notional (base is USD).
    assert usdjpy.notional_account(150.0, 0.10) == pytest.approx(10_000.0)


def test_swap_money_currency_and_points_modes() -> None:
    currency = InstrumentSpec(
        symbol="EURUSD", digits=5, point=0.00001, pip=0.0001,
        pip_value_per_lot=10.0, contract_size=100_000.0, lot_step=0.01,
        min_lot=0.01, max_lot=50.0, swap_long=-5.0, swap_short=2.0,
    )
    assert currency.swap_money(sign=+1, lots=1.0) == pytest.approx(-5.0)
    assert currency.swap_money(sign=+1, lots=1.0, tripled=True) == pytest.approx(-15.0)
    assert currency.swap_money(sign=-1, lots=0.5) == pytest.approx(1.0)

    points = InstrumentSpec(
        symbol="EURUSD", digits=5, point=0.00001, pip=0.0001,
        pip_value_per_lot=10.0, contract_size=100_000.0, lot_step=0.01,
        min_lot=0.01, max_lot=50.0, swap_long=-30.0, swap_short=10.0,
        swap_in_points=True,
    )
    # -30 points = -3 pips = -$30 per lot; 0.1 lot = -$3.
    assert points.swap_money(sign=+1, lots=0.1) == pytest.approx(-3.0)


def test_round_lots_floors_to_step() -> None:
    spec = default_instrument("EURUSD")
    assert spec.round_lots(0.129) == pytest.approx(0.12)
    assert spec.round_lots(0.1299) == pytest.approx(0.12)
    assert spec.round_lots(0.0) == 0.0
    assert spec.round_lots(-1.0) == 0.0


def test_round_lots_to_meet_clears_minimum() -> None:
    spec = default_instrument("EURUSD")
    assert spec.round_lots_to_meet(0.001) == pytest.approx(0.01)
    assert spec.round_lots_to_meet(0.129) == pytest.approx(0.12)


def test_accepts_enforces_min_except_when_reducing() -> None:
    spec = default_instrument("EURUSD")
    assert spec.accepts(0.01, 1.10) == (True, None)
    ok, why = spec.accepts(0.001, 1.10)
    assert not ok and why == "lots_below_min"
    # A partial close below min_lot must not be blocked locally.
    assert spec.accepts(0.001, 1.10, reduce_only=True) == (True, None)
    ok, why = spec.accepts(0.0, 1.10)
    assert not ok and why == "lots_below_zero"


def test_round_price_uses_digits() -> None:
    assert default_instrument("EURUSD").round_price(1.234567) == 1.23457
    assert default_instrument("USDJPY").round_price(150.12349) == 150.123


def test_from_mt5_converts_trade_tick_value_to_pip_value() -> None:
    spec = InstrumentSpec.from_mt5(
        {
            "symbol": "EURUSD",
            "digits": 5,
            "point": 0.00001,
            "trade_tick_value": 1.0,
            "trade_tick_size": 0.00001,
            "trade_contract_size": 100_000.0,
            "volume_min": 0.01,
            "volume_max": 50.0,
            "volume_step": 0.01,
            "swap_long": -5.0,
            "swap_short": 2.0,
            "swap_rollover3days": 3,  # MT5 enum: Wednesday
            "swap_mode": 1,           # POINTS
            "spread": 10,
            "currency_profit": "USD",
        }
    )
    assert spec.pip_value_per_lot == pytest.approx(10.0)
    assert spec.swap_triple_day == 2  # Python weekday: Wednesday
    assert spec.swap_in_points is True
    assert spec.quote_currency == "USD"


def test_from_mt5_usdjpy_pip_value() -> None:
    spec = InstrumentSpec.from_mt5(
        {
            "symbol": "USDJPY",
            "digits": 3,
            "point": 0.001,
            "trade_tick_value": 0.6667,  # ~100 JPY per tick, /150
            "trade_tick_size": 0.001,
            "swap_rollover3days": 3,
            "spread": 18,
        }
    )
    # One pip = ten ticks.
    assert spec.pip_value_per_lot == pytest.approx(6.667, rel=1e-3)
    assert spec.pip == pytest.approx(0.01)

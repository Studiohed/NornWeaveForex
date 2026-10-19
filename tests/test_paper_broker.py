"""PaperBroker: Forex lot rounding, rejections and fee bookkeeping."""

from __future__ import annotations

import pytest

from nornweave_forex.instrument import default_instrument
from nornweave_forex.live.broker import OrderRejected, OrderRequest, PaperBroker
from nornweave_forex.types import Direction


def _order(**overrides) -> OrderRequest:
    base = dict(
        client_id="EURUSD-test",
        symbol="EURUSD",
        direction=Direction.LONG,
        qty=0.10,
        reference_price=1.10000,
        slippage=False,
        reduce_only=False,
        reason="entry",
        ts=1_750_000_000_000,
    )
    base.update(overrides)
    return OrderRequest(**base)


def _broker(**kwargs) -> PaperBroker:
    return PaperBroker(
        instruments={"EURUSD": default_instrument("EURUSD")},
        **kwargs,
    )


def test_paper_broker_returns_fill_in_lots() -> None:
    fill = _broker().execute(_order())
    assert fill.qty == pytest.approx(0.10)
    assert fill.price == pytest.approx(1.10)
    assert fill.client_id == "EURUSD-test"


def test_paper_broker_rounds_lots_to_step_and_meets_minimum() -> None:
    fill = _broker().execute(_order(qty=0.129))
    assert fill.qty == pytest.approx(0.12)
    # 0.001 rounds down to zero -> bump up to the broker minimum.
    fill = _broker().execute(_order(qty=0.001))
    assert fill.qty == pytest.approx(0.01)


def test_paper_broker_rejects_zero_quantity() -> None:
    with pytest.raises(OrderRejected):
        _broker().execute(_order(qty=0.0))


def test_paper_broker_rejects_too_large() -> None:
    with pytest.raises(OrderRejected):
        _broker().execute(_order(qty=100.0))


def test_slippage_moves_price_against_the_order() -> None:
    broker = _broker(slippage_bps=10.0)  # 0.1%
    buy = broker.execute(_order(qty=0.10, slippage=True))
    sell = broker.execute(_order(qty=0.10, direction=Direction.SHORT,
                                 reduce_only=False, slippage=True))
    assert buy.price > 1.10
    assert sell.price < 1.10


def test_fee_is_account_currency_notional_times_rate() -> None:
    broker = _broker(fee_rate=0.0001)  # 1 bp
    fill = broker.execute(_order(qty=0.10))
    # 0.10 lots * 1.10 = $11,000 notional -> $1.10 fee at 1 bp.
    assert fill.fee == pytest.approx(1.10)


def test_reduce_only_below_one_step_rounds_to_zero_and_rejects() -> None:
    # A remainder smaller than one lot step cannot be sent to the broker; the
    # runner must close the rest with a full-size order instead of silently
    # reporting a partial exit that never happened.
    with pytest.raises(OrderRejected):
        _broker().execute(_order(qty=0.001, reduce_only=True))

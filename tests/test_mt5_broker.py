"""MT5Broker tests backed by a fake RPyC client (no MetaTrader5 required).

These lock down the order request that actually reaches ``order_send`` — the
price field MT5 requires on Instant-execution accounts, the zero SL/TP on entry
(the disaster stop is attached by the hook), the un-sliced deterministic
comment, the lot-rounded volume, and the reduce-only clamp/ticket targeting.
They also pin the retry policy: a lost ``order_send`` reply must propagate as
``MT5ConnectionLost`` after exactly one send, never be retried into a duplicate
order.
"""

from __future__ import annotations

import pytest

from nornweave_forex.broker.mt5_broker import MAGIC, MT5Broker
from nornweave_forex.broker.mt5_client import (
    READ_ONLY_METHODS,
    MT5Client,
    MT5ClientError,
    MT5ConnectionLost,
    MT5Endpoint,
)
from nornweave_forex.live.broker import OrderRejected, OrderRequest
from nornweave_forex.live.state import OpenPositionState
from nornweave_forex.types import Direction

TICK = {"bid": 1.10000, "ask": 1.10005}
ENTRY_CLIENT_ID = "EURUSD-1730419200000-entry"          # 26 chars
MAX_CLIENT_ID = "EURUSD-1730419200000-exit-tp-12"       # exactly 31 chars


def _position(ticket: int = 555, volume: float = 0.03, magic: int = MAGIC,
              raw_type: int = 0) -> dict:
    return {
        "ticket": ticket,
        "symbol": "EURUSD",
        "type": raw_type,  # 0 = buy / long
        "volume": volume,
        "price_open": 1.1,
        "price_current": 1.1,
        "sl": 0.0,
        "tp": 0.0,
        "profit": 0.0,
        "swap": 0.0,
        "magic": magic,
        "comment": "",
        "time_ms": 0,
    }


class FakeClient:
    """The subset of :class:`MT5Client` that :class:`MT5Broker` calls."""

    def __init__(self, *, tick: dict | None = None, positions: list[dict] | None = None,
                 send_results: list[dict] | None = None,
                 send_exc: BaseException | None = None,
                 modify_result: dict | None = None,
                 init_info: dict | None = None) -> None:
        self.tick = dict(tick or TICK)
        self.positions = list(positions or [])
        self.send_results = list(send_results or [{"retcode": 10009}])
        self.send_exc = send_exc
        self.modify_result = dict(modify_result or {"retcode": 10009, "comment": "ok"})
        self.init_info = dict(init_info or {
            "login": 1, "equity": 10_000.0, "margin_free": 9_000.0,
            "leverage": 100, "margin_mode": 2,
        })
        self.sent: list[dict] = []
        self.modified: list[tuple[int, float, float]] = []
        self.init_calls = 0

    def initialize(self, **kwargs) -> dict:
        self.init_calls += 1
        return dict(self.init_info)

    def symbol_tick(self, symbol: str) -> dict:
        return dict(self.tick)

    def positions_get(self, symbol: str | None = None) -> list[dict]:
        return [dict(p) for p in self.positions]

    def order_send(self, request: dict) -> dict:
        self.sent.append(dict(request))
        if self.send_exc is not None:
            raise self.send_exc
        if len(self.send_results) > 1:
            return dict(self.send_results.pop(0))
        return dict(self.send_results[0])

    def order_modify_sltp(self, ticket: int, sl: float, tp: float) -> dict:
        self.modified.append((int(ticket), float(sl), float(tp)))
        return dict(self.modify_result)


def make_broker(spec, client: FakeClient | None = None) -> tuple[MT5Broker, FakeClient]:
    client = client or FakeClient(
        send_results=[{"retcode": 10009, "order": 555, "volume": 0.1,
                       "price": 1.10005, "comment": "done"}]
    )
    return MT5Broker(client, {"EURUSD": spec}), client


def entry_order(*, direction: Direction = Direction.LONG, qty: float = 0.103,
                client_id: str = ENTRY_CLIENT_ID) -> OrderRequest:
    return OrderRequest(
        client_id=client_id,
        symbol="EURUSD",
        direction=direction,
        qty=qty,
        reference_price=1.10,
        slippage=True,
        reduce_only=False,
        reason="entry",
        ts=1730419200000,
    )


def close_order(qty: float = 0.5) -> OrderRequest:
    return OrderRequest(
        client_id="EURUSD-1730419200000-exit-stop_loss",
        symbol="EURUSD",
        direction=Direction.LONG,
        qty=qty,
        reference_price=1.10,
        slippage=True,
        reduce_only=True,
        reason="stop_loss",
        ts=1730419200000,
    )


def opened_position(**overrides) -> OpenPositionState:
    values = dict(
        symbol="EURUSD",
        direction=Direction.LONG,
        entry_ts=1730419200000,
        entry_price=1.10,
        initial_qty=0.10,
        qty=0.10,
        stop=1.09,
        tp_prices=[1.12, 1.13],
        tp_weights=[0.5, 0.5],
        tps_hit=[False, False],
    )
    values.update(overrides)
    return OpenPositionState(**values)


# --------------------------------------------------------------------------- #
# Entry request shape
# --------------------------------------------------------------------------- #
def test_entry_request_has_price_sltp_magic_comment_and_rounded_volume(eurusd_spec) -> None:
    broker, fake = make_broker(eurusd_spec)

    fill = broker.execute(entry_order(qty=0.103))

    request = fake.sent[-1]
    assert request["action"] == 1                      # TRADE_ACTION_DEAL
    assert request["price"] == pytest.approx(1.10005)  # ask for a Buy
    assert request["sl"] == 0.0 and request["tp"] == 0.0
    assert request["magic"] == MAGIC
    assert request["comment"] == ENTRY_CLIENT_ID
    assert len(request["comment"]) <= 31
    assert request["volume"] == pytest.approx(0.10)    # 0.103 floored to lot_step
    assert request["type"] == 0                        # ORDER_TYPE_BUY
    assert request["type_time"] == 0                   # ORDER_TIME_GTC
    assert request["type_filling"] == 1                # IOC first
    assert request["deviation"] > 0
    assert "position" not in request

    assert fill.qty == pytest.approx(0.10)
    assert fill.price == pytest.approx(1.10005)
    assert fill.client_id == ENTRY_CLIENT_ID


def test_sell_entry_prices_at_bid_and_uses_sell_type(eurusd_spec) -> None:
    broker, fake = make_broker(eurusd_spec)

    broker.execute(entry_order(direction=Direction.SHORT, qty=0.129))

    request = fake.sent[-1]
    assert request["price"] == pytest.approx(1.10000)  # bid for a Sell
    assert request["type"] == 1                        # ORDER_TYPE_SELL
    assert request["volume"] == pytest.approx(0.12)


def test_comment_is_exactly_31_characters_and_not_sliced(eurusd_spec) -> None:
    assert len(MAX_CLIENT_ID) == 31
    broker, fake = make_broker(eurusd_spec)

    broker.execute(entry_order(client_id=MAX_CLIENT_ID))

    assert fake.sent[-1]["comment"] == MAX_CLIENT_ID


def test_entry_binds_ticket_from_result_order(eurusd_spec) -> None:
    broker, fake = make_broker(eurusd_spec)

    broker.execute(entry_order())

    assert len(fake.sent) == 1
    assert broker._tickets["EURUSD"] == 555


# --------------------------------------------------------------------------- #
# Reduce-only: volume clamp + ticket targeting
# --------------------------------------------------------------------------- #
def test_reduce_only_clamps_volume_before_send_and_targets_ticket(eurusd_spec) -> None:
    broker, fake = make_broker(eurusd_spec)
    fake.positions = [_position(ticket=555, volume=0.03)]
    fake.send_results = [{"retcode": 10009, "order": 999, "volume": 0.03,
                          "price": 1.10, "comment": "done"}]

    fill = broker.execute(close_order(qty=0.5))

    request = fake.sent[-1]
    assert request["volume"] == pytest.approx(0.03)    # clamped to the held lots
    assert request["position"] == 555                  # the bound/matched ticket
    assert request["price"] == pytest.approx(1.10000)  # bid: closing a long
    assert request["type"] == 1
    assert fill.qty == pytest.approx(0.03)


def test_ambiguous_positions_are_rejected_not_guessed(eurusd_spec) -> None:
    broker, fake = make_broker(eurusd_spec)
    fake.positions = [_position(ticket=555), _position(ticket=556)]

    with pytest.raises(OrderRejected, match="refusing to guess"):
        broker.execute(close_order(qty=0.1))
    assert fake.sent == []


def test_reduce_only_without_any_position_is_rejected(eurusd_spec) -> None:
    broker, fake = make_broker(eurusd_spec)

    with pytest.raises(OrderRejected, match="no open position"):
        broker.execute(close_order(qty=0.1))
    assert fake.sent == []


# --------------------------------------------------------------------------- #
# Result handling
# --------------------------------------------------------------------------- #
def test_fill_qty_comes_from_the_broker_result_volume(eurusd_spec) -> None:
    broker, fake = make_broker(eurusd_spec)
    fake.send_results = [{"retcode": 10010, "order": 556, "volume": 0.07,
                          "price": 1.10005, "comment": "partial"}]

    fill = broker.execute(entry_order(qty=0.103))

    assert fill.qty == pytest.approx(0.07)  # not the requested 0.10


def test_non_done_retcode_maps_to_order_rejected_with_text(eurusd_spec) -> None:
    broker, fake = make_broker(eurusd_spec)
    fake.send_results = [{"retcode": 10018, "comment": "Market closed"}]

    with pytest.raises(OrderRejected, match="10018"):
        broker.execute(entry_order())
    with pytest.raises(OrderRejected, match="market closed"):
        broker.execute(entry_order())


def test_price_changed_retry_is_bounded(eurusd_spec) -> None:
    broker, fake = make_broker(eurusd_spec)
    fake.send_results = [
        {"retcode": 10020, "comment": "price changed"},
        {"retcode": 10021, "comment": "price off"},
        {"retcode": 10009, "order": 777, "volume": 0.1, "price": 1.10005,
         "comment": "done"},
    ]

    fill = broker.execute(entry_order())

    assert len(fake.sent) == 3
    assert all(request["price"] == pytest.approx(1.10005) for request in fake.sent)
    assert fill.qty == pytest.approx(0.10)


def test_invalid_fill_retries_other_filling_modes(eurusd_spec) -> None:
    broker, fake = make_broker(eurusd_spec)
    fake.send_results = [
        {"retcode": 10030, "comment": "invalid fill"},
        {"retcode": 10009, "order": 888, "volume": 0.1, "price": 1.10005,
         "comment": "done"},
    ]

    broker.execute(entry_order())

    assert [request["type_filling"] for request in fake.sent] == [1, 0]  # IOC -> FOK
    assert broker._filling["EURUSD"] == 0


# --------------------------------------------------------------------------- #
# Transport failures
# --------------------------------------------------------------------------- #
def test_lost_order_send_reply_is_not_retried_and_propagates(eurusd_spec) -> None:
    fake = FakeClient(send_exc=MT5ConnectionLost("lost reply after send"))
    broker, _ = make_broker(eurusd_spec, fake)

    with pytest.raises(MT5ConnectionLost):
        broker.execute(entry_order())

    assert len(fake.sent) == 1  # exactly one send: no duplicate order


# --------------------------------------------------------------------------- #
# Lifecycle hooks
# --------------------------------------------------------------------------- #
def test_on_position_opened_binds_ticket_and_sends_disaster_stop_and_tp(eurusd_spec) -> None:
    broker, fake = make_broker(eurusd_spec)
    broker.execute(entry_order())
    assert broker._tickets["EURUSD"] == 555

    position = opened_position(mt5_ticket=0)
    broker.on_position_opened(position)

    assert position.mt5_ticket == 555
    assert len(fake.modified) == 1
    ticket, sl, tp = fake.modified[0]
    assert ticket == 555
    assert sl == pytest.approx(1.07)   # entry - 3 * (entry - strategy stop)
    assert tp == pytest.approx(1.12)   # first unhit ladder rung


def test_on_position_opened_binds_by_unique_match_without_prior_order(eurusd_spec) -> None:
    broker, fake = make_broker(eurusd_spec)
    fake.positions = [_position(ticket=777)]

    position = opened_position()
    broker.on_position_opened(position)

    assert position.mt5_ticket == 777
    assert fake.modified[0][0] == 777


def test_on_position_opened_refuses_ambiguous_ticket_binding(eurusd_spec) -> None:
    broker, fake = make_broker(eurusd_spec)
    fake.positions = [_position(ticket=555), _position(ticket=556)]

    with pytest.raises(OrderRejected, match="cannot bind MT5 ticket"):
        broker.on_position_opened(opened_position())


# --------------------------------------------------------------------------- #
# Account model
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("margin_mode", [0, 1])  # netting, exchange
def test_initialize_refuses_non_hedging_accounts(eurusd_spec, margin_mode: int) -> None:
    fake = FakeClient(init_info={"login": 1, "equity": 10_000.0,
                                 "margin_free": 9_000.0, "leverage": 100,
                                 "margin_mode": margin_mode})
    broker, _ = make_broker(eurusd_spec, fake)

    with pytest.raises(RuntimeError, match="not a hedging account"):
        broker.initialize()


def test_initialize_accepts_hedging_account(eurusd_spec) -> None:
    broker, fake = make_broker(eurusd_spec)

    info = broker.initialize()

    assert fake.init_calls == 1
    assert info["margin_mode"] == 2
    assert broker._margin_mode == 2


# --------------------------------------------------------------------------- #
# Client retry policy (tiny fake connection, no network)
# --------------------------------------------------------------------------- #
def test_read_only_method_set_covers_reads_and_excludes_mutations() -> None:
    expected = {
        "ping", "account_info", "symbol_info", "symbol_select", "symbol_tick",
        "copy_rates_from_pos", "positions_get", "history_deals_get",
        "order_check", "terminal_info",
    }
    assert expected <= READ_ONLY_METHODS
    for mutation in ("order_send", "order_modify_sltp", "initialize", "shutdown"):
        assert mutation not in READ_ONLY_METHODS


class _FakeRoot:
    def __init__(self) -> None:
        self.read_calls = 0
        self.order_calls = 0

    def copy_rates_from_pos(self, *args):
        self.read_calls += 1
        if self.read_calls == 1:
            raise EOFError("simulated drop")
        return [{"ts": 1}]

    def order_send(self, request):
        self.order_calls += 1
        raise EOFError("simulated lost reply")


class _FakeConn:
    def __init__(self, root) -> None:
        self.root = root

    def close(self) -> None:
        pass


class _ProbeClient(MT5Client):
    def __init__(self, root) -> None:
        super().__init__(MT5Endpoint("127.0.0.1", 1), "secret", reconnect_backoff_s=0.0)
        self.root = root
        self.opens = 0

    def _open(self):
        self.opens += 1
        return _FakeConn(self.root)

    def close(self) -> None:
        pass


def test_read_only_call_is_retried_after_transport_drop() -> None:
    root = _FakeRoot()
    client = _ProbeClient(root)

    assert client.copy_rates_from_pos("EURUSD", 120, 0, 10) == [{"ts": 1}]
    assert root.read_calls == 2


def test_order_send_is_at_most_once_after_transport_drop() -> None:
    root = _FakeRoot()
    client = _ProbeClient(root)

    with pytest.raises(MT5ConnectionLost):
        client.order_send({"symbol": "EURUSD"})
    assert root.order_calls == 1


def test_non_transport_remote_error_becomes_mt5_client_error(eurusd_spec) -> None:
    broker, fake = make_broker(eurusd_spec)
    fake.send_exc = MT5ClientError("remote failure")

    with pytest.raises(OrderRejected, match="order_send failed"):
        broker.execute(entry_order())

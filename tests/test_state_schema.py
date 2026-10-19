"""The live state file's schema, and what happens when it does not match the code.

Written because the shape of the state changed repeatedly while the port was
being built (single position -> ``positions`` mapping, crypto ``funding`` ->
Forex ``swap_accrued``, account-currency money fields). Every change survived
only because the new fields happened to have defaults: a rename instead of an
addition would fail to load, the runner would quarantine the file and start from
scratch while MT5 still held positions.

What is locked here:
* the JSON contract (schema version, multi-symbol positions, money metadata);
* a newer schema is *refused*, never quarantined or silently downgraded;
* optional keys may be absent (a resumed process, an older file) but unknown keys
  must still raise;
* ``client_id`` fits MT5's 31-character comment budget and is unique per
  ``(symbol, bar, purpose)`` — the crypto project shipped a bug where the id was
  cut from the right and three take-profits collapsed onto one id.

Arithmetic (``equity``, ``pnl_money``, ``book_realized``) is covered by
``tests/test_state_money.py`` and is intentionally not duplicated here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nornweave_forex.live.state import (
    SCHEMA_VERSION,
    ClosedTrade,
    LiveState,
    OpenPositionState,
    StateSchemaError,
    StateStore,
    client_id,
)
from nornweave_forex.types import Direction, ExitReason

MAJORS = (
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "AUDUSD",
    "USDCAD",
    "USDCHF",
    "NZDUSD",
    "EURJPY",
)

BARS = [1_792_080_000_000, 1_792_087_200_000, 0, 999_999_999_999_999]
PURPOSES = ["entry", "net-external"] + [f"exit-{r.value}" for r in ExitReason]

#: MT5 stores an order comment of at most 31 characters.
LINK_MAX = 31


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def position(symbol: str = "EURUSD", **overrides) -> OpenPositionState:
    base = dict(
        symbol=symbol,
        direction=Direction.LONG,
        entry_ts=1_792_080_000_000,
        entry_price=1.0850,
        initial_qty=0.10,
        qty=0.10,
        stop=1.0750,
        tp_prices=[1.0900, 1.0950, 1.1000],
        tp_weights=[0.30, 0.30, 0.30],
        tps_hit=[False, False, False],
    )
    base.update(overrides)
    return OpenPositionState(**base)


def closed_trade(**overrides) -> ClosedTrade:
    base = dict(
        symbol="EURUSD",
        direction=Direction.LONG,
        entry_ts=1_792_080_000_000,
        entry_price=1.0850,
        exit_ts=1_792_087_200_000,
        exit_price=1.0900,
        qty=0.10,
        gross_pnl=50.0,
        fees=0.0,
        swap=-1.50,
        net_pnl=48.50,
        reason="take_profit_2",
        bars_held=6,
        notional_account=10_850.0,
    )
    base.update(overrides)
    return ClosedTrade(**base)


def live_state(**overrides) -> LiveState:
    base = dict(
        symbols=["EURUSD", "USDJPY"],
        interval="120",
        cash=10_000.0,
    )
    base.update(overrides)
    return LiveState(**base)


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


# --------------------------------------------------------------------------- #
# the version is written
# --------------------------------------------------------------------------- #
def test_the_schema_version_is_at_least_two():
    # v2 added the Forex money metadata (pip/pip_value/contract_size, swap,
    # notional_account). v1 files predate the port and must not be produced now.
    assert SCHEMA_VERSION >= 2


def test_a_saved_file_declares_its_schema(tmp_path):
    store = StateStore(tmp_path / "s.json")
    store.save(live_state())
    raw = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert raw["schema"] == SCHEMA_VERSION


def test_saving_is_atomic_and_leaves_no_tmp_file(tmp_path):
    path = tmp_path / "s.json"
    StateStore(path).save(live_state())
    assert path.exists()
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_loading_a_missing_file_returns_none(tmp_path):
    assert StateStore(tmp_path / "absent.json").load() is None


# --------------------------------------------------------------------------- #
# round-trip: the JSON contract
# --------------------------------------------------------------------------- #
def test_a_round_trip_preserves_a_multi_symbol_portfolio(tmp_path):
    state = live_state(
        positions={
            "EURUSD": position("EURUSD"),
            "USDJPY": position("USDJPY", direction=Direction.SHORT, entry_price=150.10, stop=151.00),
        },
        symbol_last_bar_ts={"EURUSD": BARS[0], "USDJPY": BARS[1]},
    )
    store = StateStore(tmp_path / "s.json")
    store.save(state)
    back = store.load()

    assert sorted(back.positions) == ["EURUSD", "USDJPY"]
    assert back.positions["EURUSD"].direction is Direction.LONG
    assert back.positions["USDJPY"].direction is Direction.SHORT
    assert back.positions["USDJPY"].entry_price == pytest.approx(150.10)
    assert back.symbol_last_bar_ts == {"EURUSD": BARS[0], "USDJPY": BARS[1]}


def test_a_round_trip_preserves_the_forex_money_metadata(tmp_path):
    original = position(
        pip=0.0001,
        pip_value_per_lot=10.0,
        contract_size=100_000.0,
        swap_accrued=-2.25,
        gross_pnl=12.5,
        entry_fee=0.0,
        exit_fees=0.0,
        mt5_ticket=987654321,
        exchange_booked_qty=0.05,
        exchange_booked_pnl=6.25,
        entry_price=1.0850,
        stop=1.0750,
    )
    store = StateStore(tmp_path / "s.json")
    store.save(live_state(positions={"EURUSD": original}))
    back = store.load().positions["EURUSD"]

    assert back.pip == pytest.approx(0.0001)
    assert back.pip_value_per_lot == pytest.approx(10.0)
    assert back.contract_size == pytest.approx(100_000.0)
    assert back.swap_accrued == pytest.approx(-2.25)
    assert back.mt5_ticket == 987654321
    assert back.exchange_booked_pnl == pytest.approx(6.25)
    # The money conversion must survive the round trip, not just the fields.
    assert back.pnl_money(1.0900) == pytest.approx(original.pnl_money(1.0900))


def test_a_round_trip_preserves_the_closed_trade_notional(tmp_path):
    trade = closed_trade()
    store = StateStore(tmp_path / "s.json")
    store.save(live_state(trade_log=[trade], trades_closed=1, realized_pnl=48.5))
    back = store.load()

    assert back.trades_closed == 1
    assert back.realized_pnl == pytest.approx(48.5)
    loaded = back.trade_log[0]
    assert loaded.notional_account == pytest.approx(10_850.0)
    assert loaded.net_pct_of_notional == pytest.approx(48.5 / 10_850.0)
    assert loaded.swap == pytest.approx(-1.50)


def test_positions_are_written_keyed_by_symbol_with_a_numeric_direction(tmp_path):
    store = StateStore(tmp_path / "s.json")
    store.save(live_state(positions={"EURUSD": position()}))
    raw = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert list(raw["positions"]) == ["EURUSD"]
    assert raw["positions"]["EURUSD"]["direction"] == int(Direction.LONG)


# --------------------------------------------------------------------------- #
# a file from the future is refused, not discarded
# --------------------------------------------------------------------------- #
def test_a_newer_schema_is_refused_loudly_and_the_file_is_kept(tmp_path):
    """Quarantining would start from scratch and abandon whatever position it describes."""
    path = tmp_path / "s.json"
    payload = live_state(positions={"EURUSD": position()}).to_dict()
    payload["schema"] = SCHEMA_VERSION + 1
    write_json(path, payload)

    with pytest.raises(StateSchemaError, match="schema"):
        StateStore(path).load()

    assert path.exists(), "the file must be left alone, not moved aside"


def test_state_schema_error_is_not_a_value_error():
    """The runner quarantines ValueError and starts fresh. That is right for a corrupt
    file and catastrophic for one from a newer build."""
    assert not issubclass(StateSchemaError, ValueError)
    assert issubclass(StateSchemaError, RuntimeError)


def test_a_corrupt_file_still_raises_json_decode_error(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        StateStore(path).load()


def test_an_unknown_field_raises_rather_than_being_ignored(tmp_path):
    """``cls(**payload)`` refusing unknown keys is what makes the version check
    meaningful: a rename shows up as a failure instead of a silent default."""
    path = tmp_path / "s.json"
    payload = live_state().to_dict()
    payload["something_from_the_future"] = 1
    write_json(path, payload)
    with pytest.raises(TypeError):
        StateStore(path).load()


def test_an_unknown_position_field_raises(tmp_path):
    path = tmp_path / "s.json"
    payload = live_state(positions={"EURUSD": position()}).to_dict()
    payload["positions"]["EURUSD"]["funding_rate"] = 0.01  # crypto field, renamed here
    write_json(path, payload)
    with pytest.raises(TypeError):
        StateStore(path).load()


# --------------------------------------------------------------------------- #
# tolerance: optional keys may be absent
# --------------------------------------------------------------------------- #
def test_a_state_file_without_a_schema_is_treated_as_the_current_version(tmp_path):
    """Files written before versioning existed have no ``schema`` key."""
    path = tmp_path / "s.json"
    payload = live_state(positions={"EURUSD": position()}, trades_closed=2).to_dict()
    del payload["schema"]
    write_json(path, payload)

    back = StateStore(path).load()
    assert back.trades_closed == 2
    assert "EURUSD" in back.positions


def test_missing_optional_state_keys_get_defaults(tmp_path):
    path = tmp_path / "s.json"
    payload = live_state(cash=1234.5).to_dict()
    for key in (
        "symbol_last_bar_ts",
        "day",
        "day_start_equity",
        "day_realized",
        "trades_closed",
        "realized_pnl",
        "halted",
        "halt_reason",
        "executed_client_ids",
        "fill_log",
        "trade_log",
    ):
        payload.pop(key, None)
    write_json(path, payload)

    back = StateStore(path).load()
    assert back.cash == pytest.approx(1234.5)
    assert back.symbol_last_bar_ts == {}
    assert back.trades_closed == 0
    assert back.halted is False
    assert back.trade_log == []


def test_missing_optional_position_keys_get_defaults(tmp_path):
    path = tmp_path / "s.json"
    payload = live_state(positions={"EURUSD": position()}).to_dict()
    for key in (
        "be_trigger_price",
        "be_stop",
        "be_active",
        "tp1_overridden",
        "entry_fee",
        "exit_fees",
        "swap_accrued",
        "gross_pnl",
        "mt5_ticket",
        "exchange_booked_qty",
        "exchange_booked_pnl",
        "exchange_booked_gross",
        "pip",
        "pip_value_per_lot",
        "contract_size",
    ):
        payload["positions"]["EURUSD"].pop(key, None)
    write_json(path, payload)

    pos = StateStore(path).load().positions["EURUSD"]
    assert pos.be_active is False
    assert pos.swap_accrued == 0.0
    assert pos.pip == pytest.approx(0.0001)
    assert pos.pip_value_per_lot == pytest.approx(10.0)
    assert pos.contract_size == pytest.approx(100_000.0)


def test_a_closed_trade_without_notional_falls_back_for_the_percentage(tmp_path):
    """Schema v1 did not persist ``notional_account``; the percentage column must
    still be computable instead of dividing by zero."""
    minimal = {
        "symbol": "EURUSD",
        "direction": int(Direction.LONG),
        "entry_ts": 1,
        "entry_price": 1.10,
        "exit_ts": 2,
        "exit_price": 1.11,
        "qty": 0.10,
    }
    trade = ClosedTrade.from_dict(minimal)
    assert trade.notional_account == 0.0
    assert trade.net_pnl == 0.0
    assert trade.net_pct_of_notional == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# quarantine: explicit, and only for the genuinely unusable
# --------------------------------------------------------------------------- #
def test_quarantine_moves_the_file_aside(tmp_path):
    path = tmp_path / "s.json"
    store = StateStore(path)
    store.save(live_state())
    before = path.read_text(encoding="utf-8")

    moved = store.quarantine("foreign state")

    assert moved == tmp_path / "s.quarantined-foreign-state.json"
    assert moved.exists()
    assert moved.read_text(encoding="utf-8") == before
    assert not path.exists()


def test_quarantine_of_a_missing_file_is_a_noop(tmp_path):
    path = tmp_path / "absent.json"
    assert StateStore(path).quarantine("foreign") == path
    assert not path.exists()


def test_quarantine_sanitises_the_reason(tmp_path):
    path = tmp_path / "s.json"
    store = StateStore(path)
    store.save(live_state())
    moved = store.quarantine("bad/thing!")
    assert moved.name == "s.quarantined-bad-thing-.json"
    assert moved.exists()


# --------------------------------------------------------------------------- #
# client ids: unique, stable, and inside MT5's comment budget
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("purpose", PURPOSES)
def test_client_id_never_exceeds_the_mt5_comment_budget(purpose):
    for symbol in MAJORS:
        for bar in BARS:
            assert len(client_id(symbol, bar, purpose)) <= LINK_MAX


def test_client_id_is_stable_for_the_same_intent():
    """Stability is what makes a replayed bar safe."""
    for purpose in PURPOSES:
        assert client_id("EURUSD", BARS[0], purpose) == client_id("EURUSD", BARS[0], purpose)


def test_distinct_purposes_do_not_collide_on_one_bar():
    seen: dict[str, str] = {}
    for purpose in PURPOSES:
        cid = client_id("EURUSD", BARS[0], purpose)
        assert seen.get(cid, purpose) == purpose, (
            f"{purpose!r} and {seen[cid]!r} both produce {cid}"
        )
        seen[cid] = purpose


def test_different_bars_are_different_orders():
    for purpose in PURPOSES:
        assert client_id("EURUSD", BARS[0], purpose) != client_id("EURUSD", BARS[1], purpose)


def test_short_ids_stay_readable():
    """A digest is only needed when the plain form does not fit."""
    assert client_id("EURUSD", 0, "entry") == "EURUSD-0-entry"


def test_client_ids_are_unique_across_the_eight_majors():
    """The live runner keeps one global ``executed_client_ids`` list, and every pair
    closes on the same bar timestamps. Two pairs whose ids collide silently lose the
    second exit (``already_executed`` returns True and the order is skipped) — which
    is exactly the crypto collapse this id scheme is supposed to prevent."""
    collisions: dict[str, set[str]] = {}
    for bar in BARS:
        for purpose in PURPOSES:
            seen: dict[str, set[str]] = {}
            for symbol in MAJORS:
                cid = client_id(symbol, bar, purpose)
                seen.setdefault(cid, set()).add(symbol)
            for cid, symbols in seen.items():
                if len(symbols) > 1:
                    collisions[f"{bar}:{purpose}:{cid}"] = symbols
    assert not collisions, (
        "client_id collides across pairs, so the runner will drop one of their exits: "
        f"{collisions}"
    )

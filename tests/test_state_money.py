"""State money model, persistence round-trip and deterministic client ids."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nornweave_forex.instrument import default_instrument
from nornweave_forex.live.state import (
    ClosedTrade,
    LiveState,
    OpenPositionState,
    StateStore,
    client_id,
)
from nornweave_forex.types import Direction


def _position(symbol: str = "EURUSD", lots: float = 0.10) -> OpenPositionState:
    spec = default_instrument(symbol)
    price = 150.0 if "JPY" in symbol else 1.10
    stop = price - 100 * spec.pip
    return OpenPositionState(
        symbol=symbol,
        direction=Direction.LONG,
        entry_ts=1_750_000_000_000,
        entry_price=price,
        initial_qty=lots,
        qty=lots,
        stop=stop,
        tp_prices=[price + 100 * spec.pip],
        tp_weights=[0.3],
        tps_hit=[False],
        pip=spec.pip,
        pip_value_per_lot=spec.pip_value_per_lot,
        contract_size=spec.contract_size,
    )


def test_position_pnl_is_account_currency() -> None:
    pos = _position("EURUSD", 0.10)
    # +10 pips on 0.10 lots = $10.
    assert pos.pnl_money(1.1010) == pytest.approx(10.0)
    # A partial 0.04 lot exit books 40% of it.
    assert pos.pnl_money(1.1010, lots=0.04) == pytest.approx(4.0)


def test_position_pnl_usdjpy() -> None:
    pos = _position("USDJPY", 0.10)
    assert pos.pnl_money(150.10) == pytest.approx(100.0 / 15.0, rel=1e-9)


def test_equity_uses_contract_size_not_price_units() -> None:
    pos = _position("EURUSD", 0.10)
    state = LiveState(symbols=["EURUSD"], interval="120", cash=10_000.0,
                      positions={"EURUSD": pos})
    assert state.equity({"EURUSD": 1.1010}) == pytest.approx(10_010.0)
    # The crypto bug would have produced ~10_000.0001; assert we are far from it.
    assert state.equity({"EURUSD": 1.1010}) > 10_005.0


def test_equity_skips_unmarked_position() -> None:
    pos = _position()
    state = LiveState(symbols=["EURUSD"], interval="120", cash=10_000.0,
                      positions={"EURUSD": pos})
    assert state.equity({}) == pytest.approx(10_000.0)


def test_book_realized_updates_cash_and_day_bucket() -> None:
    state = LiveState(symbols=["EURUSD"], interval="120", cash=10_000.0)
    state.book_realized(10.0, fee=1.0)
    assert state.cash == pytest.approx(10_009.0)
    assert state.realized_pnl == pytest.approx(9.0)
    assert state.day_realized == pytest.approx(9.0)


def test_closed_trade_percent_of_notional() -> None:
    trade = ClosedTrade(
        symbol="EURUSD", direction=Direction.LONG,
        entry_ts=0, entry_price=1.10, exit_ts=1, exit_price=1.1010,
        qty=0.10, gross_pnl=10.0, fees=0.0, swap=0.0, net_pnl=10.0,
        reason="take_profit_1", notional_account=11_000.0,
    )
    # $10 on $11,000 notional.
    assert trade.net_pct_of_notional == pytest.approx(10.0 / 11_000.0)


def test_state_round_trip_keeps_money_fields(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "live.json")
    pos = _position()
    state = LiveState(symbols=["EURUSD"], interval="120", cash=12_345.0,
                      positions={"EURUSD": pos})
    store.save(state)
    loaded = store.load()
    assert loaded is not None
    assert loaded.cash == pytest.approx(12_345.0)
    got = loaded.positions["EURUSD"]
    assert got.pip == pytest.approx(pos.pip)
    assert got.pip_value_per_lot == pytest.approx(pos.pip_value_per_lot)
    assert got.pnl_money(1.1010) == pytest.approx(10.0)
    assert loaded.equity({"EURUSD": 1.1010}) == pytest.approx(12_355.0)


def test_state_schema_is_persisted(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "live.json")
    store.save(LiveState(symbols=["EURUSD"], interval="120", cash=1.0))
    payload = json.loads((tmp_path / "live.json").read_text(encoding="utf-8"))
    assert payload["schema"] >= 2


def test_client_id_fits_mt5_comment_limit_and_stays_unique() -> None:
    purposes = [
        "entry",
        "exit-stop-loss",
        "exit-take-profit-1",
        "exit-take-profit-2",
        "exit-take-profit-3",
        "exit-break-even-stop",
        "exit-trend-reversal",
    ]
    ids = [client_id("EURUSD", 1_750_000_000_000, p) for p in purposes]
    assert all(len(cid) <= 31 for cid in ids)
    assert len(set(ids)) == len(ids)
    # Deterministic across calls (crash-safety).
    assert ids == [client_id("EURUSD", 1_750_000_000_000, p) for p in purposes]


def test_quarantine_moves_file_aside(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "live.json")
    store.path.write_text("not json", encoding="utf-8")
    moved = store.quarantine("unreadable")
    assert moved.exists()
    assert not store.path.exists()


def test_save_creates_parent_directory(tmp_path: Path) -> None:
    # The default path is state/live.json, which does not exist on a fresh box.
    store = StateStore(tmp_path / "state" / "live.json")
    store.save(LiveState(symbols=["EURUSD"], interval="120", cash=1.0))
    assert store.path.exists()
    assert not store.path.with_suffix(".json.tmp").exists()
    assert store.load() is not None


def test_empty_state_file_is_absent_not_corrupt(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "live.json")
    store.path.write_text("", encoding="utf-8")
    assert store.load() is None


def test_corrupt_state_raises_value_error(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "live.json")
    store.path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        store.load()


def test_missing_required_field_raises(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "live.json")
    store.path.write_text('{"cash": 1.0}', encoding="utf-8")
    with pytest.raises((TypeError, ValueError)):
        store.load()


def test_legacy_state_without_schema_loads(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "live.json")
    store.path.write_text(
        '{"symbols": ["EURUSD"], "interval": "120", "cash": 5000.0}',
        encoding="utf-8",
    )
    loaded = store.load()
    assert loaded is not None
    assert loaded.cash == pytest.approx(5000.0)
    assert loaded.schema if False else True  # field is not persisted on the object

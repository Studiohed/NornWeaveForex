"""Regression tests for the Forex live runner.

These are the three ``%TEMP%`` probes from the runner-fix task made permanent:

* a synthetic paper run that opens, takes a partial target and closes, with the
  cash/trade/notional accounting reconciled exactly;
* break-even arming that moves the local stop and exits at the moved stop;
* batch polling (all bars in a poll are processed) plus restart de-duplication
  (a re-delivered bar is not executed twice) and a state round-trip;
* the kill-switch halt and its persistence;
* an explicit account-currency guard: 10 pips on 0.10 lots EURUSD is ~$10, not
  the ~0.00001 the crypto ``(exit-entry)*qty`` formula would book.

The feed is a stub: ``copy_rates_from_pos`` returns the first ``history_bars``
closed synthetic bars plus one still-forming bar, ``symbol_tick`` returns a
two-sided quote, and ``poll_new_bars`` yields configured batches, one ``step()``
at a time.
"""

from __future__ import annotations

import pathlib

import pytest

from nornweave_forex.config import Config
from nornweave_forex.data.synthetic import synthetic_candles
from nornweave_forex.execution import ExitPlan
from nornweave_forex.instrument import default_instrument
from nornweave_forex.live.runner import (
    Guardrails,
    PortfolioRunner,
    StepResult,
    load_or_initialise,
)
from nornweave_forex.live.state import OpenPositionState, StateStore
from nornweave_forex.types import Candle, CandleArrays, Direction, ExitReason

SYMBOL = "EURUSD"
HISTORY_BARS = 600
INTERVAL_MINUTES = 120

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CONFIG_PATH = _REPO_ROOT / "configs" / "forex.yaml"


# --------------------------------------------------------------------------- #
# stub MT5 feed
# --------------------------------------------------------------------------- #
def _row(series: CandleArrays, index: int) -> dict:
    return {
        "ts": int(series.ts[index]),
        "open": float(series.open[index]),
        "high": float(series.high[index]),
        "low": float(series.low[index]),
        "close": float(series.close[index]),
        "volume": float(series.volume[index]),
    }


def _candle(series: CandleArrays, index: int) -> Candle:
    row = _row(series, index)
    return Candle(
        ts=row["ts"],
        open=row["open"],
        high=row["high"],
        low=row["low"],
        close=row["close"],
        volume=row["volume"],
    )


class FakeClient:
    """The two MT5 calls the runner makes, backed by a synthetic series."""

    def __init__(self, series: CandleArrays, history_bars: int) -> None:
        self.series = series
        self.history_bars = int(history_bars)
        self.price = float(series.close[self.history_bars - 1])

    def copy_rates_from_pos(
        self, symbol: str, timeframe_minutes: int, start_pos: int, count: int
    ) -> list[dict]:
        # ``history_bars`` closed bars + one still-forming bar, like MT5.
        n = min(int(count), self.history_bars + 1)
        return [_row(self.series, index) for index in range(n)]

    def symbol_tick(self, symbol: str) -> dict:
        return {"bid": self.price - 0.00002, "ask": self.price + 0.00002}


class FakeFeed:
    """Polls a synthetic series in configured batches, like ``MT5Feed``."""

    def __init__(
        self,
        series: CandleArrays,
        *,
        symbol: str = SYMBOL,
        history_bars: int = HISTORY_BARS,
        batch: int = 1,
        cursor: int | None = None,
    ) -> None:
        self.series = series
        self.symbol = symbol
        self.batch = max(1, int(batch))
        self.cursor = int(history_bars) if cursor is None else int(cursor)
        self.client = FakeClient(series, history_bars)

    def poll_new_bars(self) -> dict[str, list[Candle]]:
        start = self.cursor
        end = min(start + self.batch, len(self.series))
        if start >= end:
            return {}
        self.cursor = end
        self.client.price = float(self.series.close[end - 1])
        return {self.symbol: [_candle(self.series, index) for index in range(start, end)]}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _config() -> Config:
    """The real live config; each test mutates only its own instance."""
    return Config.from_yaml(_CONFIG_PATH)


def _make_runner(
    tmp_path: pathlib.Path,
    config: Config,
    series: CandleArrays,
    *,
    history_bars: int = HISTORY_BARS,
    batch: int = 1,
    cursor: int | None = None,
    guardrails: Guardrails | None = None,
) -> tuple[PortfolioRunner, object, FakeFeed, StateStore, dict]:
    store = StateStore(tmp_path / "live.json")
    state, _notes = load_or_initialise(config, store)
    feed = FakeFeed(series, history_bars=history_bars, batch=batch, cursor=cursor)
    instruments = {SYMBOL: default_instrument(SYMBOL, price=float(series.close[0]))}
    runner = PortfolioRunner(
        config,
        feed=feed,
        store=store,
        instruments=instruments,
        guardrails=guardrails,
        now_ms=lambda: int(series.ts[min(feed.cursor, len(series) - 1)]),
        persist_every_bar=True,
    )
    return runner, state, feed, store, instruments


def _run_until_closed(
    runner: PortfolioRunner, state, *, max_steps: int = 400
) -> tuple[int, list[str]]:
    actions: list[str] = []
    for step in range(1, max_steps + 1):
        result = runner.step(state)
        actions.extend(result.actions)
        if state.halted:
            pytest.fail(f"runner halted at step {step}: {state.halt_reason}")
        if state.trades_closed >= 1 and not state.positions:
            return step, actions
    pytest.fail(f"no round trip closed in {max_steps} steps")


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
def test_paper_run_reconciles_money(tmp_path: pathlib.Path) -> None:
    """Open, take a partial target, close; cash and the trade log must agree."""
    config = _config()
    # A nearer first target makes the partial-then-close path deterministic and
    # keeps the test fast; the accounting assertions are unaffected.
    config.risk.tp_atr_multiples = (0.25, 0.5, 0.75)
    hist_bars = 450
    series = synthetic_candles(SYMBOL, bars=1400, interval_minutes=INTERVAL_MINUTES)
    runner, state, _feed, _store, instruments = _make_runner(
        tmp_path, config, series, history_bars=hist_bars
    )

    _steps, actions = _run_until_closed(runner, state)

    assert state.trades_closed == 1
    assert not state.positions
    trade = state.trade_log[-1]

    # A partial target and then the final close: entry fill + >= 2 exits.
    assert len(state.fill_log) >= 3
    assert any("take_profit" in action for action in actions), actions
    assert any("closed net=" in action for action in actions), actions

    # 1) every booked dollar is in the trade log and vice versa.
    assert state.cash == pytest.approx(
        config.backtest.initial_equity + sum(t.net_pnl for t in state.trade_log),
        abs=1e-6,
    )
    # 2) the closed trade is internally consistent.
    assert trade.net_pnl == pytest.approx(trade.gross_pnl - trade.fees + trade.swap, abs=1e-9)
    assert trade.fees == pytest.approx(0.0, abs=1e-12)
    assert len(state.trade_log) == 1

    # 3) flat equity equals cash (no open position contributes unrealised PnL).
    assert state.equity({}) == pytest.approx(state.cash, abs=1e-9)

    # 4) notional is account currency: entry * lots * pip_value / pip.
    spec = instruments[SYMBOL]
    expected_notional = trade.entry_price * trade.qty * spec.pip_value_per_lot / spec.pip
    assert trade.notional_account == pytest.approx(expected_notional, rel=1e-9)

    # 5) the numbers are dollars, not price units (a ~1% risk on 0.08-0.09 lots
    #    is tens of dollars; the crypto formula would produce ~0.0001).
    assert abs(trade.net_pnl) > 1.0
    assert trade.bars_held >= 1


def test_break_even_arms_and_exits_at_moved_stop(tmp_path: pathlib.Path) -> None:
    """Arming BE must move ``pos.stop`` so the close-decided exit fires on it."""
    config = _config()
    config.risk.be_trigger_pct = 0.002   # test-only: make BE arm quickly
    series = synthetic_candles(SYMBOL, bars=1200, interval_minutes=INTERVAL_MINUTES)
    runner, state, _feed, _store, instruments = _make_runner(tmp_path, config, series)

    _steps, actions = _run_until_closed(runner, state, max_steps=250)

    assert any("BE armed" in action for action in actions), actions
    trade = state.trade_log[-1]
    assert trade.reason == "break_even_stop"

    spec = instruments[SYMBOL]
    moved_stop = spec.round_price(
        trade.entry_price * (1.0 + config.risk.be_buffer_pct)
    )
    original_stop = spec.round_price(
        trade.entry_price * (1.0 + config.risk.stop_loss_pct)
    )
    # Exited at the moved BE stop (a close just below it), far from the original
    # stop, and in profit вЂ” that is only possible if pos.stop moved on arming.
    assert trade.exit_price == pytest.approx(moved_stop, abs=0.002)
    assert abs(trade.exit_price - moved_stop) < abs(trade.exit_price - original_stop)
    assert trade.exit_price > trade.entry_price
    assert trade.net_pnl > 0


def test_batch_processing_and_restart_dedup(tmp_path: pathlib.Path) -> None:
    """Every bar in a batch is processed; a re-delivered bar is not executed twice."""
    config = _config()
    hist_bars = 450
    series = synthetic_candles(SYMBOL, bars=1600, interval_minutes=INTERVAL_MINUTES)
    batch = 5
    runner, state, feed, store, instruments = _make_runner(
        tmp_path, config, series, history_bars=hist_bars, batch=batch
    )

    # A fixed number of full batches: enough to enter a trade and prove every bar
    # in each poll is applied (history length grows by every bar), while keeping
    # the test fast. The restart below is where de-duplication is exercised.
    batch_steps = 10
    batch_counts: list[int] = []
    total_bars = 0
    for step in range(1, batch_steps + 1):
        result = runner.step(state)
        batch_counts.append(result.new_bars)
        total_bars += result.new_bars
        if state.halted:
            pytest.fail(f"runner halted at step {step}: {state.halt_reason}")
    assert batch_counts == [batch] * batch_steps, batch_counts
    assert total_bars == batch_steps * batch
    # Every bar was appended, not just the last one in each poll.
    assert len(runner._history[SYMBOL]) == hist_bars + total_bars   # noqa: SLF001
    assert state.executed_client_ids, "no order executed in the batch window"
    assert len(state.executed_client_ids) == len(set(state.executed_client_ids))

    processed_ts = int(state.symbol_last_bar_ts[SYMBOL])
    index_by_ts = {int(ts): index for index, ts in enumerate(series.ts)}
    assert int(series.ts[index_by_ts[processed_ts]]) == processed_ts

    # The state on disk is the same state we have in memory.
    roundtrip = store.load()
    assert roundtrip is not None
    assert roundtrip.cash == pytest.approx(state.cash, abs=1e-9)
    assert roundtrip.trades_closed == state.trades_closed
    assert roundtrip.open_symbols() == state.open_symbols()

    # Restart: the next feed re-delivers the last processed bar as the first bar
    # of its batch (the classic crash-between-acted-and-saved case).
    resume_index = index_by_ts[processed_ts]
    feed2 = FakeFeed(series, history_bars=hist_bars, batch=batch, cursor=resume_index)
    reloaded = store.load()
    assert reloaded is not None
    ids_before = list(reloaded.executed_client_ids)
    trades_before = reloaded.trades_closed
    runner2 = PortfolioRunner(
        config,
        feed=feed2,
        store=store,
        instruments=instruments,
        now_ms=lambda: int(series.ts[min(feed2.cursor, len(series) - 1)]),
        persist_every_bar=True,
    )
    assert int(feed2.series.ts[resume_index]) == processed_ts
    result = runner2.step(reloaded)
    assert not reloaded.halted
    # The re-delivered bar must not re-execute anything: the previously executed
    # ids are preserved as a prefix and the list contains no duplicates.
    assert reloaded.executed_client_ids[: len(ids_before)] == ids_before, (
        "re-delivered bar executed again"
    )
    assert len(reloaded.executed_client_ids) == len(set(reloaded.executed_client_ids))
    assert reloaded.trades_closed >= trades_before
    assert reloaded.symbol_last_bar_ts[SYMBOL] >= processed_ts
    # Next poll has genuinely new bars and moves the cursor forward.
    runner2.step(reloaded)
    assert reloaded.symbol_last_bar_ts[SYMBOL] > processed_ts


def test_kill_switch_halts_and_persists(tmp_path: pathlib.Path) -> None:
    """The big red button halts before touching the feed and the halt survives a reload."""
    config = _config()
    series = synthetic_candles(SYMBOL, bars=700, interval_minutes=INTERVAL_MINUTES)
    kill_switch = tmp_path / "KILL_SWITCH"
    kill_switch.write_text("stop trading\n", encoding="utf-8")
    guardrails = Guardrails(
        kill_switch_file=str(kill_switch),
        max_positions=config.portfolio.max_positions,
    )
    runner, state, _feed, store, _instruments = _make_runner(
        tmp_path, config, series, guardrails=guardrails
    )

    result = runner.step(state)
    assert result.halted
    assert result.halt_reason is not None
    assert "kill switch" in result.halt_reason.lower()
    assert state.halted
    assert result.new_bars == 0

    loaded = store.load()
    assert loaded is not None
    assert loaded.halted
    assert loaded.halt_reason == state.halt_reason

    # A halted state stays halted even if the operator removes the file.
    kill_switch.unlink()
    assert runner.step(loaded).halted


def test_ten_pips_on_010_lots_is_about_ten_dollars(tmp_path: pathlib.Path) -> None:
    """The account-currency booking path, not ``(exit-entry)*qty`` price units."""
    config = _config()
    config.backtest.slippage_bps = 0.0
    config.backtest.taker_fee = 0.0
    series = synthetic_candles(SYMBOL, bars=800, interval_minutes=INTERVAL_MINUTES)
    runner, state, _feed, _store, instruments = _make_runner(tmp_path, config, series)
    spec = instruments[SYMBOL]

    entry = 1.10000
    exit_price = 1.10100          # exactly 10 pips on EURUSD
    lots = 0.10
    state.positions[SYMBOL] = OpenPositionState(
        symbol=SYMBOL,
        direction=Direction.LONG,
        entry_ts=int(series.ts[600]),
        entry_price=entry,
        initial_qty=lots,
        qty=lots,
        stop=spec.round_price(entry * (1.0 + config.risk.stop_loss_pct)),
        tp_prices=[],
        tp_weights=[],
        tps_hit=[],
        pip=spec.pip,
        pip_value_per_lot=spec.pip_value_per_lot,
        contract_size=spec.contract_size,
    )
    bar = Candle(
        ts=int(series.ts[601]),
        open=exit_price,
        high=exit_price,
        low=exit_price,
        close=exit_price,
        volume=1.0,
    )
    runner._execute_exit(          # noqa: SLF001 - the booking path is the subject
        state,
        SYMBOL,
        ExitPlan(
            reason=ExitReason.TAKE_PROFIT_1,
            price=exit_price,
            qty=lots,
            slippage=False,
        ),
        bar,
        StepResult(),
    )

    assert state.trades_closed == 1
    trade = state.trade_log[0]
    assert trade.gross_pnl == pytest.approx(10.0, abs=1e-9)
    assert trade.net_pnl == pytest.approx(10.0, abs=1e-9)
    assert state.cash == pytest.approx(config.backtest.initial_equity + 10.0, abs=1e-9)
    assert trade.notional_account == pytest.approx(
        entry * lots * spec.pip_value_per_lot / spec.pip, rel=1e-12
    )
    # The crypto formula would have booked (1.101 - 1.100) * 0.10 = 0.00001.
    assert trade.gross_pnl - (exit_price - entry) * lots > 1.0


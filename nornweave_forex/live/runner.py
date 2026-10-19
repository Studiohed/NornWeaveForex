"""The multi-symbol live loop, Forex edition.

One instance drives up to ``max_positions`` currency pairs. Per pair, per newly
closed bar, it does exactly what the crypto runner did, in the same order:

1. **Resolve the bar** against any open position using the shared rules in
   :mod:`nornweave_forex.execution` — the same function the backtester calls.
2. **Act on close-based decisions** (break-even arming, a close-triggered stop,
   a Dow reversal) at the bar's closing price.
3. **Open a position** if the signal allows, a slot is free and the portfolio
   risk budget has room.

Everything is persisted after every bar, and every order carries a deterministic
id, so a crash between "acted" and "saved" cannot double-execute.

The Forex port differs from the crypto reference in two places that matter:

* context comes from the shared :class:`IndicatorSet` (Dow + daily THURISAZ +
  tuning + streaks), never hand-assembled here;
* every money figure goes through ``InstrumentSpec``/``OpenPositionState`` pip
  values. ``qty`` is **lots**, so the crypto formula ``(exit - entry) * qty``
  would be wrong by ``contract_size`` (100,000 for EURUSD).
"""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from nornweave_forex.config import Config, ConfigError, interval_ms
from nornweave_forex.execution import (
    ExitPlan,
    break_even_should_arm,
    effective_stop_trigger,
    plan_intrabar_exits,
    stop_breached_on_close,
)
from nornweave_forex.instrument import InstrumentSpec
from nornweave_forex.live.broker import Broker, OrderRejected, OrderRequest, PaperBroker
from nornweave_forex.live.feed import MT5Feed
from nornweave_forex.live.state import (
    ClosedTrade,
    LiveState,
    OpenPositionState,
    StateStore,
    client_id,
)
from nornweave_forex.risk.sizing import position_size
from nornweave_forex.strategy.context import IndicatorSet, build_indicators
from nornweave_forex.strategy.engine import SignalContext, SignalEngine
from nornweave_forex.strategy.exits import build_exit_ladder
from nornweave_forex.types import Candle, CandleArrays, Direction, ExitReason

__all__ = ["Guardrails", "StepResult", "PortfolioRunner", "load_or_initialise", "utc_day"]

log = logging.getLogger(__name__)

#: How many consecutive guarded failures at one site halt the instance.
_SWALLOW_LIMIT = 3

#: Keep at most this many multiples of the warm-up window (in bars). Indicators are
#: re-derived on every new bar, so an uncapped list would grow for the life of the
#: process; three multiples keeps every indicator seeded while bounding work/memory.
_HISTORY_MULTIPLE = 3

#: The rollover hour used for the *intra-bar* swap estimate. The true value comes
#: from the broker on reconciliation (phase 2); this keeps equity honest between
#: rollovers only.
_ROLLOVER_HOUR_UTC = 21

#: Quantities are lots on a coarse decimal grid. Subtracting floats (0.09 - 0.02)
#: leaves 0.06999999999999999, and flooring that to the 0.01 lot grid silently
#: produces 0.06. Every quantity is cleaned back onto a 10-decimal grid before it
#: is rounded to the broker step, and "flat" uses this tolerance.
_LOT_EPS = 1e-9


def utc_day(ts_ms: int) -> str:
    return _dt.datetime.fromtimestamp(ts_ms / 1000.0, tz=_dt.timezone.utc).strftime("%Y-%m-%d")


def _candle_from_row(row: dict) -> Candle:
    return Candle(
        ts=int(row["ts"]),
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row.get("volume", 0.0)),
    )


def load_or_initialise(config: Config, store: StateStore) -> tuple[LiveState, list[str]]:
    """Load persisted state, quarantining an unreadable file rather than ignoring it."""
    notes: list[str] = []
    try:
        state = store.load()
    except (ValueError, OSError, TypeError) as exc:
        quarantined = store.quarantine("unreadable")
        notes.append(f"state file was unreadable ({exc}); moved to {quarantined}")
        state = None
    if state is None:
        state = LiveState(
            symbols=list(config.portfolio.symbols),
            interval=config.interval,
            cash=config.backtest.initial_equity,
            day_start_equity=config.backtest.initial_equity,
        )
        notes.append(f"initialised fresh state with {state.cash:,.2f}")
        return state, notes

    wrong_symbols = sorted(set(state.symbols) - set(config.portfolio.symbols))
    wrong_interval = state.interval != config.interval
    if wrong_symbols or wrong_interval:
        what = (
            f"symbols {wrong_symbols!r}" if wrong_symbols else f"interval {state.interval!r}"
        )
        quarantined = store.quarantine("foreign")
        raise ValueError(
            f"the state file {store.path} belongs to {what}, but this instance runs "
            f"{config.portfolio.symbols!r} {config.interval!r}. Moved to {quarantined}."
        )
    return state, notes


@dataclass(slots=True)
class Guardrails:
    """Hard limits that stop the runner rather than let it keep losing."""

    max_daily_loss_pct: float = 0.03
    stale_tolerance_intervals: int = 2
    kill_switch_file: str | None = "KILL_SWITCH"
    min_history_bars: int = 400
    max_positions: int = 2


@dataclass(slots=True)
class StepResult:
    new_bars: int = 0
    actions: list[str] = field(default_factory=list)
    halted: bool = False
    halt_reason: str | None = None
    equity: float = 0.0
    open_symbols: list[str] = field(default_factory=list)
    last_bar_ts: int = 0

    def summary(self) -> str:
        if self.halted:
            return f"HALTED: {self.halt_reason}"
        state = "flat" if not self.open_symbols else f"open: {','.join(self.open_symbols)}"
        head = f"{self.new_bars} new bar(s), {state}, equity {self.equity:,.2f}"
        return head + (" | " + "; ".join(self.actions) if self.actions else "")


class PortfolioRunner:
    """Drives a fixed list of symbols, up to ``max_positions`` open at once."""

    def __init__(
        self,
        config: Config,
        *,
        feed: MT5Feed,
        store: StateStore,
        broker: Broker | None = None,
        guardrails: Guardrails | None = None,
        instruments: dict[str, InstrumentSpec] | None = None,
        now_ms: Callable[[], int] | None = None,
        persist_every_bar: bool = True,
        notifier: Any = None,
    ) -> None:
        config.validate()
        self.config = config
        self.feed = feed
        self.store = store
        self.guardrails = guardrails or Guardrails(max_positions=config.portfolio.max_positions)
        self.instruments: dict[str, InstrumentSpec] = dict(instruments or {})
        self.broker = broker or PaperBroker(
            fee_rate=config.backtest.taker_fee,
            slippage_bps=config.backtest.slippage_bps,
            instruments=self.instruments,
        )
        self.engine = SignalEngine(config.strategy)
        self._now_ms = now_ms or (
            lambda: int(_dt.datetime.now(tz=_dt.timezone.utc).timestamp() * 1000)
        )
        self.persist_every_bar = persist_every_bar
        self.notifier = notifier   # phase 2: the source event flow is not ported yet
        self._dirty = False
        self._exit_rejections: dict[str, int] = {}
        self.max_exit_rejections = 5
        self._swallowed: dict[str, int] = {}
        self._save_failures = 0
        self._history: dict[str, CandleArrays] = {}
        self._indicators: dict[str, IndicatorSet] = {}
        warmup = self._warmup_bars()
        self._history_cap = max(_HISTORY_MULTIPLE * warmup, self.guardrails.min_history_bars + 50)

        if config.risk.exit_order != "tp_first":
            raise ConfigError(
                f"risk.exit_order is {config.risk.exit_order!r}, but the live runner "
                "implements only 'tp_first'. Set exit_order: tp_first for live runs."
            )

    # -- helpers ------------------------------------------------------------ #
    @staticmethod
    def _clean_lots(value: float) -> float:
        """Remove float artifacts from a lot quantity before rounding it."""
        return float(round(float(value), 10))

    def _warmup_bars(self) -> int:
        return max(self.config.backtest.preroll_bars, self.guardrails.min_history_bars)

    def _swallow(self, site: str, exc: Exception) -> None:
        self._swallowed[site] = self._swallowed.get(site, 0) + 1
        log.warning("%s failed (%d): %s", site, self._swallowed[site], exc)

    def _halt(self, state: LiveState, reason: str) -> None:
        state.halted = True
        state.halt_reason = reason
        log.error("HALTING: %s", reason)
        # A halt that is not on disk is forgotten on restart; persist immediately.
        self._dirty = True
        self._persist(state, force=True)

    def _persist(self, state: LiveState, *, force: bool = False) -> bool:
        """Save state; repeated failures are themselves a reason to stop trading."""
        if not force and not self._dirty:
            return True
        try:
            self.store.save(state)
            self._dirty = False
            self._save_failures = 0
            return True
        except Exception as exc:  # noqa: BLE001 - reported and escalated below
            self._save_failures += 1
            log.error("state save failed (%d): %s", self._save_failures, exc)
            if self._save_failures >= _SWALLOW_LIMIT and not state.halted:
                state.halted = True
                state.halt_reason = f"cannot persist live state: {exc}"
                log.error("HALTING: %s", state.halt_reason)
            return False

    def _guard_hook(self, hook, position: OpenPositionState, name: str, state: LiveState) -> bool:
        """Call a broker hook without letting its failure escape ``step()``.

        A one-off failure is a network blip; the same hook failing repeatedly means
        the broker no longer has our protective orders, which must halt.
        """
        try:
            hook(position)
            self._swallowed.pop(name, None)
            return True
        except Exception as exc:  # noqa: BLE001 - never crash the loop for a hook
            streak = self._swallowed.get(name, 0) + 1
            self._swallowed[name] = streak
            log.error("%s failed (%d in a row): %s", name, streak, exc)
            if streak >= _SWALLOW_LIMIT:
                self._halt(state, f"{name} failed {streak} times in a row: {exc}")
            return False

    def _fail_safe_entry(
        self, state: LiveState, symbol: str, position: OpenPositionState
    ) -> None:
        """The entry filled but its protective orders did not: flatten, then halt.

        The full source version also books the round trip from the exchange's own
        deal history; that needs reconciliation (phase 2), so this version records
        the incident and refuses to keep trading rather than inventing a price.
        """
        log.error("protective orders failed after entry on %s; attempting flatten", symbol)
        flattened = False
        try:
            self.broker.emergency_flatten()
            flattened = True
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            log.error("emergency flatten also failed: %s", exc)
        if flattened:
            state.positions.pop(symbol, None)
            state.trades_closed += 1
            detail = "position flattened; its PnL is NOT booked (reconcile manually)"
        else:
            detail = "POSITION MAY STILL BE OPEN at the broker"
        self._halt(state, f"entry protection failed on {symbol} ({detail})")

    # -- history / indicators ------------------------------------------------ #
    def _load_history(self, symbol: str) -> IndicatorSet | None:
        """Fetch warm-up history once, dropping the still-forming bar."""
        count = self._warmup_bars() + 5
        try:
            rows = self.feed.client.copy_rates_from_pos(
                symbol,
                interval_ms(self.config.interval) // 60_000,
                start_pos=0,
                count=count,
            )
        except Exception as exc:  # noqa: BLE001 - one dead pair must not stop the rest
            self._swallow(f"history:{symbol}", exc)
            return None
        if len(rows) < 2:
            log.warning("%s: only %d row(s) from the feed", symbol, len(rows))
            return None
        # The newest row is the still-forming bar.
        closed = [_candle_from_row(r) for r in rows[:-1]]
        if len(closed) < self.guardrails.min_history_bars:
            log.warning(
                "%s: only %d closed bars, need %d",
                symbol,
                len(closed),
                self.guardrails.min_history_bars,
            )
            return None
        candles = CandleArrays.from_candles(closed)
        if len(candles) > self._history_cap:
            candles = candles.slice(-self._history_cap)
        return self._install_history(symbol, candles)

    def _install_history(self, symbol: str, candles: CandleArrays) -> IndicatorSet | None:
        try:
            indicators = build_indicators(
                candles,
                self.config.interval,
                self.config.strategy,
                tuning=self.config.tuning,
            )
        except Exception as exc:  # noqa: BLE001 - bad data is reported, not fatal
            self._swallow(f"indicators:{symbol}", exc)
            return None
        self._history[symbol] = candles
        self._indicators[symbol] = indicators
        return indicators

    def _prepare_symbol(self, state: LiveState, symbol: str) -> bool:
        """Ensure history/indicators exist and seed the persisted bar cursor.

        Seeding to the newest *history* bar is what stops a cold start from
        replaying the feed's whole lookback window: bars already inside the
        fetched history are duplicates by timestamp and are skipped below.
        """
        if symbol in self._indicators:
            return True
        indicators = self._load_history(symbol)
        if indicators is None:
            return False
        if symbol not in state.symbol_last_bar_ts:
            state.symbol_last_bar_ts[symbol] = int(indicators.candles.ts[-1])
        return True

    def _apply_bar(self, symbol: str, bar: Candle) -> SignalContext | None:
        """Append ``bar`` once (never a duplicate) and return its context."""
        history = self._history.get(symbol)
        if history is None:
            return None
        if int(history.ts[-1]) >= int(bar.ts):
            # The fetched window already includes this bar (restart, or a feed
            # whose lookback overlaps the history). Evaluating the newest window
            # against an older bar would be look-ahead, so skip it and let the
            # caller advance the cursor: live must not replay already-known bars.
            return None
        candles = CandleArrays.from_candles(history.to_candles() + [bar])
        if len(candles) > self._history_cap:
            candles = candles.slice(-self._history_cap)
        if self._install_history(symbol, candles) is None:
            return None
        indicators = self._indicators.get(symbol)
        if indicators is None:
            return None
        index = len(indicators) - 1
        if index < indicators.ready_index:
            log.debug(
                "%s: %d bars, indicator ready at %d",
                symbol,
                len(indicators),
                indicators.ready_index,
            )
            return None
        return indicators.context_at(index)

    def _tuning_for(self, symbol: str, index: int) -> tuple[float | None, float | None]:
        indicators = self._indicators.get(symbol)
        if indicators is None or indicators.tuning is None:
            return None, None
        try:
            _threshold, atr_factor, stop_pct = indicators.tuning.at(index)
        except (IndexError, TypeError, ValueError) as exc:
            self._swallow(f"tuning:{symbol}", exc)
            return None, None
        return float(atr_factor), float(stop_pct)

    # -- prices -------------------------------------------------------------- #
    def _mark_price_for(self, symbol: str, direction: Direction) -> float | None:
        """The price a position would close at (bid for long, ask for short)."""
        try:
            tick = self.feed.client.symbol_tick(symbol)
            return float(tick["bid"] if direction is Direction.LONG else tick["ask"])
        except Exception as exc:  # noqa: BLE001 - mark is best effort
            self._swallow(f"tick:{symbol}", exc)
        # Fallback: the newest closed bar is a defensible mark and keeps equity
        # from silently dropping the position (which would blind the daily guard).
        history = self._history.get(symbol)
        if history is not None and len(history):
            return float(history.close[-1])
        return None

    def _equity(self, state: LiveState) -> float:
        marks: dict[str, float] = {}
        for symbol, position in state.positions.items():
            mark = self._mark_price_for(symbol, position.direction)
            if mark is not None:
                marks[symbol] = mark
        if not marks:
            return float(state.cash)
        return float(state.equity(marks))

    def _expected_price(self, reference: float, direction: Direction) -> float:
        side = "Buy" if direction is Direction.LONG else "Sell"
        try:
            return float(
                self.broker.expected_price(
                    reference_price=float(reference), side=side, slippage=True
                )
            )
        except Exception as exc:  # noqa: BLE001 - the reference price is a safe default
            self._swallow("expected_price", exc)
            return float(reference)

    # -- risk ---------------------------------------------------------------- #
    def _open_risk(self, state: LiveState) -> float:
        """Account-currency risk currently open, for the portfolio cap."""
        total = 0.0
        for position in state.positions.values():
            if position.pip <= 0 or position.pip_value_per_lot <= 0:
                continue
            total += (
                abs(position.entry_price - position.stop)
                / position.pip
                * position.pip_value_per_lot
                * position.qty
            )
        return total

    def _check_daily_loss(self, state: LiveState) -> None:
        if state.halted or state.day_start_equity <= 0:
            return
        limit = self.guardrails.max_daily_loss_pct * state.day_start_equity
        if limit > 0 and state.day_realized <= -limit:
            self._halt(
                state,
                f"daily loss limit hit: {state.day_realized:,.2f} <= -{limit:,.2f} "
                f"({self.guardrails.max_daily_loss_pct:.2%} of {state.day_start_equity:,.2f})",
            )

    # -- main loop ---------------------------------------------------------- #
    def step(self, state: LiveState) -> StepResult:
        """One iteration: poll, process every new bar, persist."""
        result = StepResult()
        if state.halted:
            result.halted = True
            result.halt_reason = state.halt_reason
            return result

        now = self._now_ms()
        if self.guardrails.kill_switch_file and Path(self.guardrails.kill_switch_file).exists():
            self._halt(state, "kill switch file present")
            result.halted = True
            result.halt_reason = state.halt_reason
            return result

        today = utc_day(now)
        if state.day != today:
            state.day = today
            state.day_start_equity = self._equity(state)
            state.day_realized = 0.0
            self._dirty = True

        try:
            new_bars = self.feed.poll_new_bars()
        except Exception as exc:  # noqa: BLE001 - retry next poll
            self._swallow("feed.poll", exc)
            new_bars = {}
        result.new_bars = sum(len(v) for v in new_bars.values())

        for symbol, bars in new_bars.items():
            if state.halted:
                break
            if not self._prepare_symbol(state, symbol):
                # Leave the cursor alone: the next poll's newest bar is processed
                # even though this one was dropped by an unavailable feed.
                log.warning("no history for %s; skipping %d bar(s)", symbol, len(bars))
                continue
            for bar in bars:
                if state.halted:
                    break
                if int(bar.ts) <= int(state.symbol_last_bar_ts.get(symbol, 0)):
                    continue    # already processed (restart / duplicate poll)
                context = self._apply_bar(symbol, bar)
                if context is None:
                    state.symbol_last_bar_ts[symbol] = int(bar.ts)
                    self._dirty = True
                    continue
                if symbol in state.positions:
                    self._process_bar(state, symbol, bar, context, result)
                else:
                    self._maybe_open(state, symbol, bar, context, result)
                state.symbol_last_bar_ts[symbol] = int(bar.ts)
                self._dirty = True
                self._check_daily_loss(state)
                if self.persist_every_bar:
                    self._persist(state)
            if state.halted:
                break

        result.equity = self._equity(state)
        result.open_symbols = state.open_symbols()
        if state.symbol_last_bar_ts:
            result.last_bar_ts = max(state.symbol_last_bar_ts.values())
        if state.halted:
            result.halted = True
            result.halt_reason = state.halt_reason
        self._persist(state)
        return result

    # -- bar processing ----------------------------------------------------- #
    def _process_bar(
        self,
        state: LiveState,
        symbol: str,
        bar: Candle,
        context: SignalContext,
        result: StepResult,
    ) -> None:
        position = state.positions.get(symbol)
        if position is None:
            return

        plans = plan_intrabar_exits(
            position.view(),
            bar_open=bar.open,
            bar_high=bar.high,
            bar_low=bar.low,
            bar_close=bar.close,
            intrabar_priority=self.config.backtest.intrabar_priority,
            stop_trigger=self.config.risk.stop_trigger,
            exit_fill=self.config.risk.exit_fill,
            be_exit_trigger=self.config.risk.be_exit_trigger,
            tp_weight_basis=self.config.risk.tp_weight_basis,
        )
        for plan in plans:
            if state.positions.get(symbol) is None:
                break
            self._execute_exit(state, symbol, plan, bar, result)

        # Break-even arming happens before the close-decided stop so that the
        # moved stop can trigger on this same bar, exactly as in the source.
        position = state.positions.get(symbol)
        if position is not None and position.qty > 0 and not position.be_active:
            if break_even_should_arm(
                be_mode=self.config.risk.be_mode,
                direction=position.direction,
                bar_high=bar.high,
                bar_low=bar.low,
                be_trigger_price=position.be_trigger_price,
                tps_hit=tuple(position.tps_hit),
                arm_on=self.config.risk.be_arm_on,
                bar_close=bar.close,
            ):
                be_stop = position.entry_price * (
                    1.0 + self.config.risk.be_buffer_pct * position.direction.sign
                )
                instrument = self.instruments.get(symbol)
                if instrument is not None:
                    be_stop = instrument.round_price(be_stop)
                position.be_active = True
                position.be_stop = be_stop
                position.stop = be_stop    # local exits follow the moved stop
                self._dirty = True
                result.actions.append(f"{symbol} BE armed @ {be_stop:g}")
                self._guard_hook(
                    self.broker.on_position_updated,
                    position,
                    f"on_position_updated:{symbol}",
                    state,
                )

        # Close-decided protective stop.
        position = state.positions.get(symbol)
        if position is not None and position.qty > 0:
            trigger = effective_stop_trigger(
                be_active=position.be_active,
                stop_trigger=self.config.risk.stop_trigger,
                be_exit_trigger=self.config.risk.be_exit_trigger,
            )
            if trigger == "close" and stop_breached_on_close(
                position.direction, position.stop, bar.close
            ):
                self._execute_exit(
                    state,
                    symbol,
                    ExitPlan(
                        reason=(
                            ExitReason.BREAK_EVEN_STOP
                            if position.be_active
                            else ExitReason.STOP_LOSS
                        ),
                        price=bar.close,
                        qty=position.qty,
                        slippage=True,
                    ),
                    bar,
                    result,
                )

        # Trend reversal, driven by the shared engine rule (honours
        # ``reversal_rule`` and ``exit_on_dow_neutral``).
        position = state.positions.get(symbol)
        if position is not None and position.qty > 0:
            reason = self.engine.reversal_exit(
                position.direction, context.dow, close=bar.close, ema=context.ema
            )
            if reason:
                self._execute_exit(
                    state,
                    symbol,
                    ExitPlan(
                        reason=ExitReason.TREND_REVERSAL,
                        price=bar.close,
                        qty=position.qty,
                        slippage=True,
                    ),
                    bar,
                    result,
                )

        # Overnight swap (intra-bar estimate; phase 2 books the broker's number).
        position = state.positions.get(symbol)
        if position is not None and position.qty > 0:
            spec = self.instruments.get(symbol)
            if spec is not None:
                position.swap_accrued += self._overnight_swap(spec, position, bar)
                self._dirty = True

    # -- execution ---------------------------------------------------------- #
    def _execute_exit(
        self,
        state: LiveState,
        symbol: str,
        plan: ExitPlan,
        bar: Candle,
        result: StepResult,
    ) -> None:
        position = state.positions.get(symbol)
        if position is None or position.qty <= 0 or plan.qty <= 0:
            return
        cid = client_id(symbol, bar.ts, f"exit-{plan.reason.value}")
        if state.already_executed(cid):
            return

        requested = self._clean_lots(min(float(plan.qty), position.qty))
        instrument = self.instruments.get(symbol)
        if instrument is not None:
            qty = instrument.round_lots(requested)
            if qty <= 0 and requested >= self._clean_lots(position.qty) - 1e-12:
                # A final close whose remainder is below one lot step: ask for the
                # whole remainder, which is the only thing the broker can accept.
                qty = instrument.round_lots(self._clean_lots(position.qty))
            if qty <= 0:
                # A partial rung smaller than one lot step cannot be executed.
                # Mark it handled so the same impossible rung is not re-planned.
                if plan.level is not None and 0 <= plan.level < len(position.tps_hit):
                    position.tps_hit[plan.level] = True
                    self._dirty = True
                log.debug(
                    "%s: skip exit %.4f lots (below lot step %.2f)",
                    symbol,
                    requested,
                    instrument.lot_step,
                )
                return
            qty = min(qty, position.qty)
        else:
            qty = requested

        request = OrderRequest(
            client_id=cid,
            symbol=symbol,
            direction=position.direction,
            qty=qty,
            reference_price=plan.price,
            slippage=plan.slippage,
            reduce_only=True,
            reason=plan.reason.value,
            ts=bar.ts,
        )
        try:
            fill = self.broker.execute(request)
        except OrderRejected as exc:
            self._exit_rejections[symbol] = self._exit_rejections.get(symbol, 0) + 1
            log.error("exit rejected for %s: %s", symbol, exc)
            if self._exit_rejections[symbol] >= self.max_exit_rejections:
                self._halt(
                    state,
                    f"exit rejected {self._exit_rejections[symbol]}x on {symbol}",
                )
            return

        self._exit_rejections[symbol] = 0
        filled = self._clean_lots(min(float(fill.qty), position.qty))
        if filled <= 0:
            log.error("exit filled non-positive quantity %.6f on %s", fill.qty, symbol)
            return

        gross = position.pnl_money(fill.price, lots=filled)
        fill.pnl = gross - fill.fee
        state.book_realized(gross, fill.fee)
        position.gross_pnl += gross
        position.exit_fees += fill.fee
        position.qty = self._clean_lots(position.qty - filled)
        state.record(fill)
        self._dirty = True
        if plan.level is not None and 0 <= plan.level < len(position.tps_hit):
            position.tps_hit[plan.level] = True
        result.actions.append(
            f"{symbol} {plan.reason.value} {filled:g}@{fill.price:g} pnl={fill.pnl:+.2f}"
        )

        if position.qty <= _LOT_EPS:
            position.qty = 0.0
            self._finalize_position(state, symbol, position, bar, plan.reason.value, result)
        else:
            self._guard_hook(
                self.broker.on_position_updated,
                position,
                f"on_position_updated:{symbol}",
                state,
            )

    def _vwap_exit_price(self, position: OpenPositionState) -> float:
        """Volume-weighted exit price recovered from the accumulated gross PnL."""
        denominator = (
            position.pip_value_per_lot * position.initial_qty * position.direction.sign
        )
        if position.pip <= 0 or abs(denominator) < 1e-15:
            return position.entry_price
        return position.entry_price + position.gross_pnl * position.pip / denominator

    def _finalize_position(
        self,
        state: LiveState,
        symbol: str,
        position: OpenPositionState,
        bar: Candle,
        reason: str,
        result: StepResult,
    ) -> None:
        """Book the round trip once the last lot is gone; never re-credit cash."""
        swap = float(position.swap_accrued)
        if swap:
            state.book_realized(swap)
        fees = position.entry_fee + position.exit_fees
        net = position.gross_pnl - fees + swap
        notional = 0.0
        if position.pip > 0:
            notional = (
                abs(position.entry_price)
                * position.initial_qty
                * position.pip_value_per_lot
                / position.pip
            )
        step_ms = interval_ms(self.config.interval)
        bars_held = max(0, int((int(bar.ts) - int(position.entry_ts)) // step_ms)) if step_ms else 0
        trade = ClosedTrade(
            symbol=symbol,
            direction=position.direction,
            entry_ts=position.entry_ts,
            entry_price=position.entry_price,
            exit_ts=int(bar.ts),
            exit_price=self._vwap_exit_price(position),
            qty=position.initial_qty,
            gross_pnl=position.gross_pnl,
            fees=fees,
            swap=swap,
            net_pnl=net,
            reason=reason,
            bars_held=bars_held,
            notional_account=notional,
        )
        state.remember_trade(trade)
        state.trades_closed += 1
        del state.positions[symbol]
        self._dirty = True
        result.actions.append(f"{symbol} closed net={net:+.2f} ({reason})")
        self._guard_hook(
            self.broker.on_position_closed,
            position,
            f"on_position_closed:{symbol}",
            state,
        )

    def _maybe_open(
        self,
        state: LiveState,
        symbol: str,
        bar: Candle,
        context: SignalContext,
        result: StepResult,
    ) -> None:
        if state.halted or state.open_count() >= self.guardrails.max_positions:
            return
        if symbol in state.positions:
            return

        instrument = self.instruments.get(symbol)
        if instrument is None:
            log.warning("no instrument spec for %s, cannot size", symbol)
            return

        signal = self.engine.evaluate(context)
        if not signal.allowed or signal.direction is Direction.FLAT or signal.atr <= 0:
            return

        cid = client_id(symbol, bar.ts, "entry")
        if state.already_executed(cid):
            return

        equity = self._equity(state)
        if equity <= 0:
            result.actions.append(f"{symbol} sizing rejected: non_positive_equity")
            return

        expected = self._expected_price(bar.close, signal.direction)
        atr_factor, stop_pct = self._tuning_for(symbol, context.index)
        anchor = bar.close if self.config.risk.tp_anchor == "signal_close" else None
        try:
            ladder = build_exit_ladder(
                signal.direction,
                expected,
                signal.atr,
                self.config.risk,
                signal.tp1_override,
                tp_anchor_price=anchor,
                atr_factor=atr_factor,
                stop_loss_pct=stop_pct,
            )
        except (ValueError, ZeroDivisionError) as exc:
            log.warning("cannot build the exit ladder for %s: %s", symbol, exc)
            return

        # Size off the *same* ladder the position will use; all of this precedes
        # any order, so a sizing/provider error cannot leave a broker position
        # half-managed.
        decision = position_size(
            equity=equity,
            entry=expected,
            stop=ladder.stop,
            instrument=instrument,
            risk=self.config.risk,
            open_risk=self._open_risk(state),
        )
        if not decision.ok or decision.lots <= 0:
            result.actions.append(f"{symbol} sizing rejected: {decision.rejected}")
            return
        qty = min(float(decision.lots), instrument.max_lot)
        if qty <= 0:
            return

        request = OrderRequest(
            client_id=cid,
            symbol=symbol,
            direction=signal.direction,
            qty=qty,
            reference_price=bar.close,
            slippage=True,
            reduce_only=False,
            reason=f"entry_{signal.direction.label}",
            ts=bar.ts,
        )
        try:
            fill = self.broker.execute(request)
        except OrderRejected as exc:
            log.warning("entry rejected for %s: %s", symbol, exc)
            return
        if fill.qty <= 0 or fill.price <= 0:
            self._halt(state, f"broker returned an invalid entry fill on {symbol}: {fill}")
            return

        # Re-anchor the ladder at the price actually paid. A failure here must not
        # leave the position unmanaged: fall back to the pre-trade ladder.
        try:
            ladder = build_exit_ladder(
                signal.direction,
                fill.price,
                signal.atr,
                self.config.risk,
                signal.tp1_override,
                tp_anchor_price=anchor,
                atr_factor=atr_factor,
                stop_loss_pct=stop_pct,
            )
        except (ValueError, ZeroDivisionError) as exc:
            log.error("could not re-anchor the ladder on %s: %s", symbol, exc)

        position = OpenPositionState(
            symbol=symbol,
            direction=signal.direction,
            entry_ts=bar.ts,
            entry_price=fill.price,
            initial_qty=fill.qty,
            qty=fill.qty,
            stop=ladder.stop,
            tp_prices=list(ladder.tp_prices),
            tp_weights=list(ladder.tp_weights),
            tps_hit=[False] * len(ladder.tp_prices),
            be_trigger_price=ladder.be_trigger_price,
            be_stop=ladder.be_stop,
            tp1_overridden=ladder.tp1_overridden,
            entry_fee=fill.fee,
            pip=instrument.pip,
            pip_value_per_lot=instrument.pip_value_per_lot,
            contract_size=instrument.contract_size,
        )
        state.positions[symbol] = position
        # Entry commission is booked exactly once, here. Exits book their own.
        state.book_realized(0.0, fill.fee)
        state.record(fill)
        self._dirty = True
        result.actions.append(
            f"{symbol} {signal.direction.label} {fill.qty:g} lots @ {fill.price:g} "
            f"stop {ladder.stop:g}"
        )
        if not self._guard_hook(
            self.broker.on_position_opened,
            position,
            f"on_position_opened:{symbol}",
            state,
        ):
            self._fail_safe_entry(state, symbol, position)

    # -- swap ---------------------------------------------------------------- #
    def _overnight_swap(
        self, spec: InstrumentSpec, position: OpenPositionState, bar: Candle
    ) -> float:
        """Signed account-currency swap for a bar that crosses the rollover hour."""
        minutes = interval_ms(self.config.interval) // 60_000
        bar_start = _dt.datetime.fromtimestamp(int(bar.ts) / 1000.0, tz=_dt.timezone.utc)
        bar_end = bar_start + _dt.timedelta(minutes=minutes)
        rollover = bar_start.replace(
            hour=_ROLLOVER_HOUR_UTC, minute=0, second=0, microsecond=0
        )
        if not (bar_start < rollover <= bar_end):
            return 0.0
        tripled = rollover.weekday() == spec.swap_triple_day
        return float(
            spec.swap_money(
                sign=position.direction.sign,
                lots=position.qty,
                tripled=tripled,
            )
        )

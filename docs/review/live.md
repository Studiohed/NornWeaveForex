# Audit — `nornweave_forex/live/*` (task-3)

**Scope:** `nornweave_forex/live/runner.py` (593 L), `state.py` (353 L), `slots.py` (105 L),
`notify.py` (213 L), `broker.py` (112 L). Compared against the reference implementation in
`D:\PROGRAMMING\NornWeave\nornweave\live\*` (runner 1313 L, state 452 L / schema 3,
slots 187 L, notify 429 L, broker 192 L incl. `PaperBroker`).

**Method:** full read of both trees; static comparison; runtime probes with
`.venv\Scripts\python.exe` (3.13.15, rpyc present, pytest 9.1.1) to confirm import and
signature failures. No production code was modified. Probe output is quoted where it is
the evidence.

**Verdict:** the live engine is **not runnable as committed**. `runner.py` cannot be
imported (`PaperBroker` missing, then `read_dow` missing), and after mechanically
supplying both, `load_or_initialise`, position sizing, the entry path, the exit path and
the break-even path each fail independently on wrong names/signatures. If those are fixed
one by one, the money math is still wrong by `contract_size` (100,000 for EURUSD) and a
live entry can execute on the broker *after* the code has already raised, leaving an
unprotected real position while the state file says "flat".

Counts: **10 BLOCKER, 20 MAJOR, 14 MINOR.** No tests protect any of this:
`tests/test_instrument.py` and `tests/test_sizing.py` are 0-byte files.

---

## Blockers

### B01 — `PaperBroker` is imported but does not exist → module import fails
`nornweave_forex/live/runner.py:37`
```python
from nornweave_forex.live.broker import Broker, OrderRejected, OrderRequest, PaperBroker
```
`live/broker.py:32` even lists it in `__all__`, but the class was never ported
(`broker.py` ends at line 112 with the `Broker` ABC/hooks). Probe:
`ImportError: cannot import name 'PaperBroker' from 'nornweave_forex.live.broker'`.
The runner's default broker (line 151) and the CLI `--paper` mode both depend on it.

**Fix:** implement `PaperBroker` for Forex in `live/broker.py` — fill buys at ask / sells at
bid from a spread source, round lots with `instrument.round_lots` (reduce) /
`round_lots_to_meet` (open), commission per lot rather than `notional * fee_rate`, and
remove the dangling `__all__` entry until it exists.

### B02 — `read_dow` does not exist → second import failure
`nornweave_forex/live/runner.py:50`, used at `:549`
```python
from nornweave_forex.strategy.dow import read_dow
...
dow = read_dow(history, idx, self.config.strategy)
```
`strategy/dow.py` exports only `dow_trend_series`, `DowTracker`, `BreakoutDow`; a repo-wide
search finds `read_dow` only inside `live/runner.py` and as an inner import in
`backtest/engine.py:396`. Probe: `ImportError: cannot import name 'read_dow'`. A third
import failure hidden behind B01.

**Fix:** do not build a context by hand; use the shared
`build_indicators(...)` + `IndicatorSet.context_at(idx)` (source `runner.py:1027`). This
also removes the duplicated Dow/EMA/ADX/ATR assembly that is already wrong in other ways
(see B03/B04/M03).

### B03 — `DailyState` is constructed with fields that do not exist → `TypeError`
`nornweave_forex/live/runner.py:554-559`
```python
daily = DailyState(
    trend=DowTrend.NEUTRAL,
    zone=Zone.UNKNOWN,
    depth=None,
    leg=None,
)
```
`strategy/daily.py:25-36` defines `DailyState(ts, dow, ema_slope, leg)`; `trend` is only a
read-only property and `zone`/`depth` do not exist. Probe:
`TypeError: DailyState.__init__() got an unexpected keyword argument 'trend'`. Any call to
`_build_context` (every new bar for a symbol) raises before any decision is made.

**Fix:** `daily = indicators.context_at(idx).daily` from the `IndicatorSet` (source does
exactly this), or at minimum `DailyState(ts=..., dow=DowTrend.NEUTRAL, ema_slope=0, leg=None)`.
Note the *semantic* bug underneath B03 is M03.

### B04 — `build_indicators` called with the wrong arity → `TypeError`
`nornweave_forex/live/runner.py:202, 296, 450`
```python
self._indicators[symbol] = build_indicators(candles, self.config.strategy)
```
Actual signature (`strategy/context.py:93`):
`build_indicators(candles, interval, strategy, delta=None, tuning=None)`. `interval` is
required, so the first `_load_history` / `_process_bar` / `_maybe_open` raises
`TypeError: build_indicators() missing 1 required positional argument: 'strategy'`.
Confirms the runner was never executed after the port.

**Fix:** `build_indicators(candles, self.config.interval, self.config.strategy, tuning=self.config.tuning)`
and keep the resulting `IndicatorSet` instead of the ad-hoc `dict[str, np.ndarray]`.

### B05 — `Config` has no `symbols` attribute → `AttributeError` in startup path
`nornweave_forex/live/runner.py:78, 86`
```python
symbols=list(config.symbols),
...
wrong_symbols = sorted(set(state.symbols) - set(config.symbols))
```
`Config` (`config.py:579-613`) has `.symbol` (crypto leftover) and `.portfolio.symbols`
(`PortfolioParams`, line 419); there is no `Config.symbols`. Probe:
`AttributeError: 'Config' object has no attribute 'symbols'`, so `load_or_initialise` —
the function the CLI calls before it can construct a runner — dies on a fresh state.

**Fix:** use `config.portfolio.symbols` in both places (and drop the dead `.symbol`
usage from the Forex path).

### B06 — PnL and equity ignore contract size / pip value (Forex money is 100,000× larger)
`nornweave_forex/live/runner.py:400`
```python
pos.gross_pnl += (fill.price - pos.entry_price) * fill.qty * pos.direction.sign
```
`nornweave_forex/live/state.py:283`
```python
unrealised += (mark - pos.entry_price) * pos.qty * pos.direction.sign
```
Both are the crypto formula (`price_diff × units`), but `qty` here is **lots**. One lot is
`contract_size` (100,000) base units, so the correct account-currency figure is
`price_diff / pip × lots × pip_value_per_lot` (equivalently
`price_diff × lots × contract_size` for USD-quoted pairs). Probe: 0.10 lots EURUSD on a
10-pip move gives `equity = 10000.0001`; it must be `10010.00` — off by 100,000×.
The same missing unit propagates to `ClosedTrade.gross_pnl/net_pnl` (`runner.py:416-419`),
`state.cash` (`:424`), `state.realized_pnl` (`:426`), the daily-loss guard (`:258-268`) and
therefore the only risk limiter in the port.

Note `runner.py:473-479` (`open_risk`) already uses the *correct* pip-value formula
(`risk_per_unit × lots × pip_value_per_lot / pip`), which proves the intended unit model
and makes the two wrong sites unambiguous.

**Fix:** introduce one money helper, e.g.
`money(instrument, entry, exit, lots) = (exit-entry)/instrument.pip * lots * instrument.pip_value_per_lot`,
and use it in `_execute_exit`, `state.equity`, `ClosedTrade` and the guard. `state.equity`
needs the per-symbol `InstrumentSpec` (or precomputed per-symbol monetary PnL) — passing
raw marks alone cannot be correct for JPY/cross pairs.

### B07 — Entry fee is charged twice; partial exits are never booked to cash
`nornweave_forex/live/runner.py:419` and `:424` vs `:539`
```python
# close (line 419)
net_pnl=pos.gross_pnl - pos.entry_fee - pos.exit_fees - pos.swap_accrued,
# close (line 424)
state.cash += trade.net_pnl
# entry (line 539)
state.cash -= fill.fee
```
`fill.fee` is deducted from `cash` at entry, then `net_pnl` subtracts `entry_fee` again when
the trade closes: every round trip charges the entry commission twice. Additionally, a
partial exit (`pos.qty > 0` branch) only mutates `pos.gross_pnl/exit_fees`; it never adds
`fill.pnl` to `cash`, `realized_pnl` or `day_realized`, so equity lags realized profit and
the daily-loss guard cannot see a losing partial fill. The source books `fill.pnl` on
*every* fill (`src runner.py:349-357`) and deducts the entry fee once (`:581-583`).

**Fix:** mirror the source: at entry only `state.realized_pnl -= fill.fee`/`cash -= fee`
(or not at all, and let the final `net_pnl` carry it once); on every exit compute
`fill.pnl = gross - fill.fee` and add it to `cash/realized_pnl/day_realized` immediately;
build `ClosedTrade` from the accumulated fills, not from a second fee subtraction.

### B08 — `build_exit_ladder` called with non-existent keywords, *after* the order is sent
`nornweave_forex/live/runner.py:515-523`
```python
ladder = build_exit_ladder(
    entry_price=fill.price,
    atr=signal.atr,
    direction=signal.direction,
    risk_params=self.config.risk,
    tp1_override=signal.tp1_override,
    atr_factor=None,
    stop_loss_pct=None,
)
```
Actual signature (`strategy/exits.py:60`):
`build_exit_ladder(direction, entry, atr, risk, tp1_override=None, break_even_stop=None,
tp_anchor_price=None, atr_factor=None, stop_loss_pct=None)` — `entry_price` and
`risk_params` do not exist, and `direction`/`atr` are positional-only-friendly but the
duplicate `entry_price` keyword raises `TypeError` before the call body runs.

This line sits **after** `fill = self.broker.execute(request)` (`:509`) and after
`state.record(fill)` (`:514`), but **before** `state.positions[symbol] = pos` (`:538`),
`self._dirty = True` (`:540`), the `on_position_opened` hook (`:543`) and the state save at
the end of `step()`. With a real `MT5Broker` the position is open on the account, has no
disaster stop placed, and the persisted state does not contain it: restart will treat the
account as flat. This is the worst state the source's `_fail_safe_entry` exists to prevent
(source `runner.py:631-693`).

**Fix:** call with `build_exit_ladder(signal.direction, expected_fill_price, signal.atr, self.config.risk, ...)`
*before* sending the order (source `_try_enter` does exactly this and re-anchors on the
fill), keep the kwargs that exist, and add the source fail-safe around
`on_position_opened` (B10).

### B09 — `break_even_should_arm` called with wrong/ missing kwargs → `TypeError` on every bar with a position
`nornweave_forex/live/runner.py:334-341`
```python
if break_even_should_arm(
    be_mode=self.config.risk.be_mode,
    direction=pos.direction,
    bar_high=bar.high, bar_low=bar.low,
    be_trigger_price=pos.be_trigger_price,
    close=bar.close,
    be_arm_on=self.config.risk.be_arm_on,
):
```
Actual signature (`execution.py:214`):
`break_even_should_arm(*, be_mode, direction, bar_high, bar_low, be_trigger_price,
tps_hit, arm_on="extreme", bar_close=None)`.
`tps_hit` is required and missing; `close=` and `be_arm_on=` are not parameters. Any
position processed on a new bar raises `TypeError` before the state is saved. The source
passes `tps_hit=tuple(position.tps_hit), arm_on=risk.be_arm_on, bar_close=close`.

**Fix:** `break_even_should_arm(..., tps_hit=tuple(pos.tps_hit), arm_on=self.config.risk.be_arm_on, bar_close=bar.close)`.

### B10 — Broker hook failures are swallowed; no fail-safe flatten, no escalation, no halt
`nornweave_forex/live/runner.py:542-545` (open), `:348-351` (BE update), `:429-437` (close/update)
```python
try:
    self.broker.on_position_opened(pos)
except Exception as exc:
    self._swallow(f"open_hook:{symbol}", exc)
```
and `_swallow` (`:176-178`) only logs:
```python
self._swallowed[site] = self._swallowed.get(site, 0) + 1
log.warning("%s failed (%d/%d): %s", site, self._swallowed[site], _SWALLOW_LIMIT, exc)
```
`on_position_opened` is where `MT5Broker` places the disaster stop and first TP
(`broker/mt5_broker.py:261-305`). If it raises, the position stays open at the broker with
**no resting protection**, the exception is swallowed, and the loop continues — `_SWALLOW_LIMIT`
is never compared with the counter, despite `_SWALLOW_LIMIT = 3` and the "(n/3)" log wording.
The source's `_fail_safe_entry` calls `broker.emergency_flatten()`, books the result, sets
`state.halted` and saves; `_guard_hook` halts after `_SWALLOW_LIMIT` consecutive failures
(source `runner.py:975-1011`).

**Fix:** port `_fail_safe_entry` and `_guard_hook` verbatim in spirit: on entry-hook
failure flatten immediately, persist the truth, halt; count consecutive failures and halt
when the limit is reached, saving state at that moment. Never let a naked broker position
be represented as a normal one.

---

## Major

### M01 — No broker reconciliation at all (positions may close/partially fill behind the bot)
`nornweave_forex/live/runner.py:218-282` (`step`) never calls `actual_size`, `closed_since`,
`positions_snapshot`, `protective_stop_price`; a grep finds zero call sites for all four in
the runner, although `broker.py:87-109` defines them and `broker/mt5_broker.py:370-414`
implements them (`actual_size` totals all magic positions, `closed_since` returns out-deals
with `pnl/swap/commission`).

Consequences: (a) the MT5 disaster stop can close a position between polls; the runner
keeps planning exits for it, each reduce-only order is rejected ("no open position",
`mt5_broker.py:230`) and after 5 rejections the instance halts; (b) a manual close/partial
close is never booked; (c) on startup with a broker position but a missing/lost state file
the runner thinks it is flat, can open `max_positions` new positions and leave the orphan
unmanaged. The source has `_reconcile_exchange_position` (source `runner.py:836-973`),
`_book_from_exchange` (`:744-834`), `_book_external_fill` (`:698-742`), and a startup
`broker.reconcile(existing.position)` in the CLI (source `cli.py:1234`). Also note the port
ABC has **no `reconcile` method**, so startup reconciliation cannot even be wired.

**Fix:** port `_reconcile_exchange_position` before exit planning in `step()`; add an
`exchange_booked_*` update path from `closed_since`; add `reconcile(position)` to the ABC
and call it from `_run_live` before constructing/looping the runner. Treat "local flat,
broker not flat" as a hard startup refusal.

### M02 — Feed-staleness guard imported but never called
`nornweave_forex/live/runner.py:38`
```python
from nornweave_forex.live.feed import MT5Feed, FeedError, feed_is_stale
```
No call anywhere in the file; `Guardrails.stale_tolerance_intervals` (`:105`) is also
unused. `feed.feed_is_stale(now_ms, last_bar_ts, interval_minutes)` exists (`feed.py:53`).
The source refuses to act on a stale feed (source `runner.py:1209-1216`), which matters
for Forex: weekend/holiday/server outages make the newest closed bar hours or days old and
the current port will happily trade it.

**Fix:** compare `last closed bar ts` (or `max(state.symbol_last_bar_ts.values())`) against
`now_ms` with `feed_is_stale`, return `StepResult(stale=True)` before any decision, and
persist nothing except state. Add the missing `stale` field to `StepResult` (the port's
`StepResult` has no `stale`/`flat`/`mark_price`, unlike source `:146-165`).

### M03 — Hardcoded `NEUTRAL/UNKNOWN` daily context disables the THURISAZ overlay (and blocks every entry)
`nornweave_forex/live/runner.py:554-559` (see B03 for the crash). Even after fixing the
constructor, a `DailyState` with `DowTrend.NEUTRAL`/`ema_slope=0`/`leg=None` means:
`SignalEngine._daily_gate` (`engine.py:154-189`) resolves the daily trend to `NEUTRAL`,
`daily_neutral_policy` defaults to **`block`** (`config.py:143`) and a `NEUTRAL` daily with
no leg returns `ok=False` (`daily_missing_policy` defaults to `block`, `config.py:145`) —
so with default config **no entry can ever pass** (`blockers=('daily_fib','daily_leg_missing')`).
When policies are set to `allow`, the filter becomes a no-op and every Fib retracement
decision, `tp1_override` and `daily_zone` seen in the backtest is lost — live/backtest
divergence in both directions.

**Fix:** build the real per-bar context via `IndicatorSet.context_at(idx)`, which carries
`daily.state_for_bar(index)`, the Dow code, the EMA slope and all filter inputs in one
place. Delete `_build_context` entirely.

### M04 — Only the last bar of a poll is processed; `act_on_last_bar_only` and persisted bar timestamps are dead
`nornweave_forex/live/runner.py:249-254`
```python
for symbol, bars in new_bars.items():
    if state.positions.get(symbol) is not None:
        self._process_bar(state, symbol, bars[-1], now, result)
    else:
        self._maybe_open(state, symbol, bars[-1], now, result)
    state.symbol_last_bar_ts[symbol] = bars[-1].ts
```
`self.act_on_last_bar_only = True` (`:164`) is never read; `state.symbol_last_bar_ts` is
written but never used to select/filter pending bars; `feed.mark_seen()`/`last_seen()`
(`feed.py:116-125`) are never called on resume. If a poll returns 5 new bars, 4 are
silently dropped — a stop breached in an intermediate bar is never executed locally (the
broker disaster stop is 3× wider, so the loss is not capped at the strategy stop). The
source iterates `pending = candles.ts > state.last_bar_ts` and, when
`act_on_last_bar_only`, records the skip explicitly (source `runner.py:1235-1243`).

**Fix:** persist `state.symbol_last_bar_ts` (already present), seed `feed.mark_seen` from
it on startup, iterate every pending bar per symbol, and only restrict to the most recent
bar when a `use_real_money`/`act_on_last_bar_only` flag is set — logging the skipped count.
At minimum never silently drop a bar that could contain a protective exit.

### M05 — History handling can evaluate indicators on a duplicated bar, and grows unbounded
`nornweave_forex/live/runner.py:180-203, 295-298, 449-452`
```python
rows = self.feed.client.copy_rates_from_pos(symbol, ..., start_pos=0, count=min_history+5)
...
for r in rows[:-1]  # drop the forming bar
...
new_history = CandleArrays.from_candles(history.to_candles() + [bar])
```
`poll_new_bars` also returns closed bars (`feed.py:104-113`), and on the first step for a
symbol `_load_history` already fetched the newest closed bar, so `history.to_candles() + [bar]`
duplicates it (the same bar is appended again on a restart when the feed's `_last_ts` is
reset to 0). Evaluating a duplicated H2 bar double-counts it in EMA/ADX/ATR and Dow pivots,
so live signals differ from the backtest. `self._history`/`self._indicators` are append-only
caches rebuilt from scratch on every bar (`build_indicators(new_history, ...)`), i.e.
quadratic work and a slow memory leak for a multi-week live run. `_indicators` is written
but never read.

**Fix:** keep one rolling `CandleArrays` per symbol and only append when
`bar.ts > history.ts[-1]`; compute indicators once per new bar with the shared
`IndicatorSet`; cap the window at `preroll_bars + warmup_bars`.

### M06 — Sizing equity is `cash` only; the risk stop differs from the ladder stop
`nornweave_forex/live/runner.py:463-488`
```python
equity = state.cash
...
stop = bar.close * (1 + self.config.risk.stop_loss_pct * signal.direction.sign)
...
decision = position_size(equity=equity, entry=bar.close, stop=stop, ...)
```
and later (`:531`) the position's actual stop is `ladder.stop`, built from `fill.price` and
`risk.stop_loss_pct` (ATR/tuning ignored here because `atr_factor=None, stop_loss_pct=None`
are passed). So (a) open positions/unrealized PnL are excluded from equity (a second entry
is sized as if the account were flat), (b) the stop that defines the risk is not the stop
that is actually placed, so the realized risk per trade differs from the configured
`risk_per_trade_pct`, and (c) the missing contract-size bug (B06) makes the whole account
currency figure meaningless. The source sizes from `state.equity(price)` and passes
`ladder.stop` to `position_size` (source `runner.py:507-513`).

**Fix:** compute equity with marks and correct money units; build the ladder from
`expected_price` *before* sizing; pass `ladder.stop` to `position_size`; re-anchor the
ladder on the fill only if the fill deviates.

### M07 — Daily-loss guard uses the wrong quantity, runs too late, and `day_realized` is dead
`nornweave_forex/live/runner.py:234-239, 256-268`; `state.py:243`
```python
day_pnl = equity_now - state.day_start_equity
if day_pnl < -state.day_start_equity * self.guardrails.max_daily_loss_pct:
```
`state.day_realized` is never written; the source's rule is "halt once the day's **realized**
loss exceeds X% of opening equity" (source `runner.py:288-296`) and it resets
`day_realized = 0.0` at rollover (`:281-286`). The port compares an equity delta
(unrealized + mark errors from M15 + contract-size bug B06) and only evaluates it after all
symbols have been processed — so new entries can be opened in the same step that breaches
the limit. On rollover (`:235-239`) `day_realized` is not reset because it is never used.

**Fix:** maintain `day_realized` on every fill (B07), reset it on day rollover, check the
limit after each close **and before each entry**, and use the source's
`_check_daily_loss` formulation.

### M08 — Halt and save ordering: a halt can be lost; save failures are swallowed forever
`nornweave_forex/live/runner.py:275-281`
```python
if self._dirty and (self.persist_every_bar or result.halted):
    try:
        self.store.save(state)
        self._dirty = False
    except Exception as exc:
        self._swallow("store.save", exc)
```
Kill-switch halt (`:227-232`), daily-loss halt (`:262`) and exit-rejection halt (`:391`)
mutate `state.halted` but do not save; the save block requires `self._dirty`, which is
false when no fill happened this step — so a daily-loss halt can vanish on restart and the
bot resumes trading the same day. Conversely `store.save` failures are counted but never
escalate (the counter is not compared to `_SWALLOW_LIMIT`), so the bot can keep trading
with a state file that is never updated. The source saves immediately on halt
(`src runner.py:344`, `:494`, `:692`, `:1007`) and lets `store.save` errors surface.

**Fix:** call `store.save(state)` immediately and unconditionally on every halt path
(and after `emergency_flatten`/reconciliation), verify it, and treat a repeated save
failure as fatal (halt + loud log) instead of `_swallow`.

### M09 — No `expected_price` use, no pre-trade ladder, no notional guard, and `config.tuning` ignored
`nornweave_forex/live/runner.py:469-537`; `Guardrails` (`:100-108`)
The ABC's `expected_price` (`broker.py:66-71`) is never called by the runner. The source
sizes/ladders against `broker.expected_price(...)` and caps the order by
`guardrails.max_position_notional_pct` with a re-rounding check (source `runner.py:502-557`).
The port's `Guardrails` has no `max_position_notional_pct` at all, and `_maybe_open` passes
`atr_factor=None, stop_loss_pct=None` even though `config.tuning.enabled` may require the
per-bar Auto Calibration values (source `:1127-1129`). On Forex the notional cap is less
central (margin is capped in `position_size`), but the pre-trade price/rounding and tuning
gaps directly change live fills versus the backtest.

**Fix:** port the `_try_enter` structure: expected price → ladder → `position_size` (with
`ladder.stop`) → notional/margin cap on the *actual rounded* lots → send → re-anchor ladder
on fill; consume `IndicatorSet.tuning.at(index)` for `atr_factor`/`stop_loss_pct`.

### M10 — Break-even arming never moves the local stop, so the BE exit never fires
`nornweave_forex/live/runner.py:342-345`
```python
pos.be_active = True
buffer = self.config.risk.be_buffer_pct * pos.entry_price
pos.be_stop = pos.entry_price + buffer * pos.direction.sign
```
`pos.stop` is left at the original stop. Everything the runner uses to decide exits reads
`pos.stop` (`plan_intrabar_exits(view)` via `PositionView.stop`, and
`stop_breached_on_close(pos.direction, pos.stop, bar.close)` at `:326`). The source sets
`position.stop = position.be_stop` when BE arms (source `runner.py:1090-1092`), so the
strategy stop really follows. Here the broker may get a BE stop through
`on_position_updated` (`mt5_broker.py:314-317` uses `be_stop` when `be_active`), but the
local engine keeps planning exits against the old, wider stop — the local and broker views
diverge and the "break-even stop" exit reason can never be produced by the close rule.
`PositionView.view()` does carry `be_active`, so `plan_intrabar_exits` can fire
`BREAK_EVEN_STOP` at the old `stop` price — the wrong price.

**Fix:** on arm, `pos.stop = pos.be_stop` (and persist), then call
`on_position_updated`; pass `tps_hit`/`arm_on`/`bar_close` correctly (B09).

### M11 — Notifications are completely dead
`nornweave_forex/live/runner.py:143,160`
```python
notifier: Any = None,
...
self.notifier = notifier
```
No other reference to `self.notifier` in the file: no `_emit`, no `_notify`, no
`halt_event`, no entry/exit/TP alert. The port's `notify.py` has no `take_profit_event`,
`external_event`, `notify_all` or `describe` (source `notify.py:212-292`), and `cli.py`
never constructs a notifier. The source's first documented outage (a position with no
protective orders for 5h43m) is exactly the failure the notifier was added for, and this
port cannot report a halt, a stop or a TP.

**Fix:** port the source event flow (`_emit` at the point of action, `_notify(result, state)`
at the end of `step`, halt event appended there, notifier failures isolated), then wire
`build_notifier(...)` in `_run_live`/CLI.

### M12 — `slots.py` is not used by the runner and is not a slot pool
`nornweave_forex/live/runner.py` has no `slots` import or parameter. `slots.py:35-105` is a
single advisory lock file (`SlotPool(path)` with `acquire/release/describe`) — not the
source's `SlotPool(directory, size)` with `held`, `try_acquire()`, `acquire() -> index`,
`occupancy()` (source `slots.py:103-187`). The runner's only cap is
`state.open_count() >= guardrails.max_positions` (`runner.py:440`), which is process-local;
two instances pointed at the same account/state will both trade. On Windows
`msvcrt.locking(..., LK_LOCK, 1)` (`slots.py:71`) is a blocking lock retried by the CRT, so
the `timeout_s` contract in `acquire` is not honoured the way the fcntl branch's `LOCK_NB`
would be.

**Fix:** port the source pool (directory of `slot-XX` files, one `flock` per position, index
returned), add `slots`/`slot_count` to the runner and CLI, claim a slot **before** sizing
and send, release it on close/flatten, and re-claim after restart if a position survived.
Since this port is single-process multi-symbol, also keep the in-process `max_positions`
check as a second line of defence.

### M13 — State schema versioning/migrations are missing; an older or foreign state file is quarantined into a fresh flat state
`nornweave_forex/live/state.py:46, 306-322` and `:309`
```python
SCHEMA_VERSION = 1
...
version = int(payload.pop("schema", SCHEMA_VERSION) or SCHEMA_VERSION)
```
Source schema is 3 with `_MIGRATIONS` (source `state.py:47, 59-94, 387-397`) and defaults a
missing `schema` to **1**, not to the current version. The port has no migrations. A state
file that carries `position`/`symbol`/`last_bar_ts` (the source shape, or any earlier port
shape) reaches `cls(**payload)` with unknown keys → `TypeError`, which
`load_or_initialise` (`runner.py:70-75`) treats as "unreadable", quarantines and replaces
with a fresh flat state — while the broker may still hold the position the file described.
The module docstring (`state.py:9-11`) explicitly says this is the failure it exists to
prevent. Conversely a `schema > 1` file raises `StateSchemaError`, which is a
`RuntimeError` and is *not* in the caught tuple — that one refuses, but with a traceback
rather than the intended clean message.

**Fix:** bump `SCHEMA_VERSION`, add an explicit `_MIGRATIONS` map, default missing
`schema` to 1, migrate old shapes (`position`→`positions`, drop `symbol`/`last_bar_ts`),
and in `load_or_initialise` catch `StateSchemaError` separately (refuse, save nothing) vs
`ValueError/OSError/TypeError/KeyError` (quarantine). Never start flat if the broker is not
flat.

### M14 — `StateStore.save` is not durable and cannot create its own directory
`nornweave_forex/live/state.py:338-344`
```python
tmp = self.path.with_suffix(self.path.suffix + ".tmp")
tmp.write_text(json.dumps(state.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
os.replace(tmp, self.path)
```
Source `save` (source `state.py:422-436`): `mkdir(parents=True, exist_ok=True)`, mode `0600`
via `os.open`, `flush` + `os.fsync` before `os.replace`, and returns the path. The port's
version: probe with `StateStore(<tmp>/sub/dir/live.json).save(...)` →
`FileNotFoundError: ... live.json.tmp`; the CLI default state path is `state/live.json`
(`cli.py:68`), so a fresh checkout without that directory silently fails to persist (the
failure is then swallowed by M08). No fsync means a power loss after `os.replace` can
leave an empty/truncated state even though the rename was atomic; `write_text` creates the
file `0644`, unlike the source's `0600`.

**Fix:** port the source `save` exactly (mkdir, 0600, fsync, return path) and let the
runner treat failure as a halt-worthy condition.

### M15 — `equity()` silently skips positions whose mark could not be fetched
`nornweave_forex/live/state.py:276-284`; `runner.py:205-215, 238, 257`
```python
mark = mark_prices.get(sym)
if mark is None:
    continue
unrealised += (mark - pos.entry_price) * pos.qty * pos.direction.sign
```
`_mark_price` returns `None` on any tick failure and the caller filters those symbols out
(`runner.py:239,258`). Equity then equals cash and the daily-loss guard either misses a
loss or, after the day rollover sets a too-low `day_start_equity`, can stop the bot
spuriously. The source's single-position `equity(mark)` has no such hole (and the mark is
the closed-bar close, not a live tick). Combined with B06 this makes every guardrail figure
untrustworthy.

**Fix:** require a mark for every open position; on a failed tick fall back to the last
closed-bar close from the feed (and log), or raise so `step()` records the failure; never
silently drop exposure from equity.

### M16 — `ClosedTrade.exit_price` and `bars_held` are wrong for multi-fill exits
`nornweave_forex/live/runner.py:408-422`
```python
exit_ts=bar.ts,
exit_price=fill.price,
...
bars_held=0,
```
The final `fill.price` is the price of the **last** partial fill, not the volume-weighted
average. The source deliberately recovers the VWAP from accumulated gross PnL
(`exit_price = entry_price + gross_pnl / (initial_qty * sign)`, source
`runner.py:424-433`) and computes `bars_held` from `entry_ts`/`exit_ts`. The port's trade
log therefore shows a fictional exit price (worse the more rungs hit) and every held
duration is 0, which feeds the dashboard/statistics/notifications.

**Fix:** port `_closed_trade`'s derivation (needs B06 so `gross_pnl` is true gross) and
compute `bars_held = (exit_ts - entry_ts) // interval_ms(interval)`.

### M17 — Trend-reversal exit bypasses `engine.reversal_exit`, ignoring `reversal_rule`/`exit_on_dow_neutral`
`nornweave_forex/live/runner.py:353-360`
```python
if pos.qty > 0 and self.config.strategy.exit_on_dow_reversal:
    opposite = pos.direction.opposite
    dow_direction = ctx.dow.direction
    if dow_direction is opposite and dow_direction is not Direction.FLAT:
```
This hardcodes the `dow_opposite` flavour. `RiskParams`/`StrategyParams` allow
`reversal_rule in {"dow_opposite","ema_cross","none"}` and `exit_on_dow_neutral`
(`config.py:110-116`, `engine.py:276-311`). With `ema_cross` configured, live will exit on
Dow flips that the backtest ignores (and vice versa); with `none` it still exits.

**Fix:** `reason = self.engine.reversal_exit(pos.direction, ctx.dow, close=bar.close, ema=ctx.ema)`
and map the returned string to `ExitReason.TREND_REVERSAL`, exactly as source
`runner.py:1107-1110` does.

### M18 — Swap model is an approximation that cannot agree with the broker
`nornweave_forex/live/runner.py:572-593`
```python
bar_start = datetime.fromtimestamp(bar.ts/1000, tz=UTC)
bar_end = bar_start + timedelta(minutes=interval_ms(...))
rollover = bar_start.replace(hour=21, minute=0, ...)
if bar_start < rollover <= bar_end:
    swap = spec.swap_long if ... else spec.swap_short
    if bar_start.weekday() == spec.swap_triple_day: swap *= 3
    return swap * pos.qty
```
(1) The rollover hour is broker-specific (the docstring admits it), not necessarily 21:00
UTC; (2) triple-swap day is taken from `InstrumentSpec.swap_triple_day` but detected from
`bar_start.weekday()`, not the day the rollover occurs; (3) the accrued swap never reaches
`state.cash`/`equity`, so intra-trade equity ignores it; (4) `MT5Broker.closed_since`
already returns the broker's real `swap` and `commission` (`mt5_broker.py:404-413`) and the
runner ignores both, so the estimate can be double-booked or sign-flipped at close. The
source accrues `funding` and books the exchange's number at reconciliation
(`_book_from_exchange`); the port has neither. PLAN.md lists the triple-swap day as an open
question — do not ship an estimate.

**Fix:** source rollover hour/day from MT5 symbol metadata; accrue into an equity-visible
`swap_accrued` only for intra-bar tracking; at close/partial close use `closed_since`'s
`swap`/`commission` as the authoritative figure, as the source does.

### M19 — Broker ABC cannot support this runner's reconciliation or Forex pricing
`nornweave_forex/live/broker.py:62-109`
- `actual_size(self) -> float | None` (`:91-93`) is documented as "total lots … across all
  our positions", and `mt5_broker.py:370-380` sums all magic positions. This runner keys
  positions by symbol (`state.positions: dict[str, OpenPositionState]`), so a single total
  is unusable — reconciliation needs per-symbol/per-ticket sizes.
- `positions_snapshot` (`:107-109`) exists even though the source Broker has no such
  method, and no code calls it: there is no startup reconciliation path (M01).
- `expected_price(*, reference_price, side, slippage)` (`:66-71`) has no bid/ask input.
  For Forex a market buy fills at ask and a sell at bid, so a paper/real broker cannot
  return a tradable price from one reference number; the MT5 implementation currently
  returns the reference unchanged (`mt5_broker.py:169-174`), which means the runner's
  sizing/ladder market-impact assumption is "no spread".
- There is no `reconcile(position)` method (source broker has it; source CLI uses it).

**Fix:** redesign the ABC for Forex: `expected_price(*, bid, ask, side, slippage)`,
`actual_size(symbol) -> float | None`, `positions_snapshot() -> list[PositionSnapshot]`
(typed, including ticket/symbol/lots/sl/tp), `reconcile(local_state) -> str | None`, and
make `closed_since` rows typed. Then implement `PaperBroker` (B01) against the same
interface, including spread, swap and per-lot commission.

### M20 — Notifier queue can evict a queued halt for a routine event
`nornweave_forex/live/notify.py:90-107`
```python
except queue.Full:
    if event.kind == "halt":
        self._queue.get_nowait()
        self._queue.put_nowait(event)
    else:
        dropped = self._queue.get_nowait()
        log.warning("notifier queue full; dropped %s event for %s", dropped.kind, ...)
        self._queue.put_nowait(event)
```
If the queue is full and the incoming event is routine, the **oldest** event is discarded
without checking whether it was a halt — so a queued halt can be replaced by a routine
entry/exit alert, breaking the module's stated "never drops a critical event" invariant.
(Probe forced the branch and confirmed the drop path runs; the source instead drops the
incoming routine event when full and only makes room for `event.critical`, source
`notify.py:328-342`.) The port `Event` has no `critical` field (`notify.py:54-63`), so stops
are not flagged either (source `exit_event` sets `critical=icon == DOWN`).

**Fix:** add `critical: bool` to `Event`, drop the *incoming* event when full and
non-critical, only replace an existing event for critical ones, and count drops.

---

## Minor

### m01 — Dead code / stale imports in the runner
`runner.py:38` (`FeedError`, `feed_is_stale` unused), `:59` (`_MAX_ACTIONS` never used),
`:164` (`act_on_last_bar_only` never read), `:167/202/298/452` (`_indicators` written
never read), `state.py:33` (`ExitReason` unused). Misleading to a port reviewer and hides
that the corresponding source behaviours are absent.
**Fix:** remove or implement each; an unused import is a missing feature in this port.

### m02 — `StateStore.quarantine` returns a path that may not exist and can overwrite evidence
`state.py:346-353`
```python
if not self.path.exists():
    return self.path          # caller logs "moved to <path>" though nothing moved
...
target = self.path.with_suffix(f".quarantined-{safe_reason}{self.path.suffix}")
os.replace(self.path, target)
```
Two successive corrupt files with the same reason overwrite the earlier quarantine (same
target path), destroying the evidence; the source returns `None` when there was nothing to
move (source `state.py:438-444`).
**Fix:** return `Path | None`, include a timestamp or counter in the target name, and log
only when a move happened.

### m03 — An empty state file is treated as corrupt (quarantined) rather than absent
`state.py:331-336` (`json.loads("")` raises; caught as ValueError by
`runner.py:72`). The source returns `None` for a blank file (source `state.py:417-419`),
which lets a first run or a truncated-at-creation file start cleanly. Minor behavioural
difference, but it produces a scary "unreadable" note on an empty file.
**Fix:** `if not text.strip(): return None`.

### m04 — `ClosedTrade.from_dict` raises `KeyError` for fields that older files lack, and that is not caught by the loader
`state.py:213-229` requires `direction/entry_ts/entry_price/exit_ts/exit_price/qty/gross_pnl/net_pnl`;
`fees`/`swap`/`reason`/`bars_held` are optional. `load_or_initialise` catches
`(ValueError, OSError, TypeError)` only (`runner.py:72`), so a `KeyError` escapes and kills
startup with a traceback instead of the intended quarantine/refusal. (Same class of bug as
M13.)
**Fix:** catch `KeyError` too; use `.get()` with defaults for added fields; add a migration.

### m05 — `ClosedTrade.net_pct_of_notional` is in the wrong units
`state.py:189-192`
```python
notional = self.entry_price * self.qty
return self.net_pnl / notional if notional > 0 else 0.0
```
`qty` is lots, so notional is `entry_price × qty × contract_size` (and converted to account
currency for crosses). Probe: 1 lot EURUSD at 1.1 with +$1,000 PnL reports **909.1%**; the
correct value is **0.91%**. Any dashboard/statistic built on this is off by 100,000×.
**Fix:** pass `contract_size`/`InstrumentSpec` into `ClosedTrade` (or store a correct
`notional_base` at entry) and divide by that.

### m06 — Triple-swap/rollover detection details
`runner.py:584-591`: rollover is computed from `bar_start.replace(hour=21)`, so a bar that
starts exactly at 21:00 (`bar_start == rollover`) does not accrue, while the triple day is
taken from `bar_start.weekday()` even though the rollover may be on the next calendar day
for a late bar. The actual rollover hour/day are broker metadata and PLAN.md lists the
triple day as unresolved.
**Fix:** use `bar_start < rollover < bar_end` semantics deliberately and read the
broker's rollover/triple-swap metadata at startup; test the boundary.

### m07 — Day boundary is UTC, not the broker's server/statement day
`runner.py:63-64` and `:234-239` use `utc_day`. MT5 statements and daily equity limits are
usually on server time; on a GMT+2/+3 broker the reset lands 2–3 h off the broker's day,
so daily-loss accounting and `day_start_equity` disagree with the account's own report.
**Fix:** make the day-boundary timezone/offset configurable per broker and use it in
`utc_day`/`_overnight_swap`/rollover.

### m08 — Notifier formatting/delivery gaps
`notify.py:46-51` `_price` always uses 5 decimals; JPY pairs quote 3 digits, so a stop at
`157.123` renders as `157.12300` (cosmetic but hard to read back). `TelegramNotifier`
(`:164-201`) has no retry (source retries twice, source `notify.py:371-398`), no
`disable_notification` distinction for critical events, no `describe()`, and `close()` only
joins a worker that exits after 1 s idle rather than draining with a sentinel. `RecordingNotifier`
keeps appending on the background thread, so tests that read `events` immediately after
`notify()` can race.
**Fix:** pass instrument digits to the event/renderer, add the critical-aware retry, and
make `RecordingNotifier` synchronous.

### m09 — `SlotPool.__enter__` ignores acquire failure; API is not the source pool
`slots.py:100-105`
```python
def __enter__(self) -> "SlotPool":
    self.acquire()
    return self
```
`acquire()` returns `False` on timeout, but the context manager returns `self` anyway, so
`with SlotPool(...)` proceeds without holding the lock. The module also has no
`size`/`held`/`try_acquire`/`occupancy` (see M12), and on Windows the blocking `LK_LOCK`
path makes the `timeout_s` parameter ineffective.
**Fix:** raise `SlotPoolUnusable` when `acquire()` fails inside `__enter__`; port the
multi-slot API; use `LK_NBLCK` + retry loop to honour the timeout.

### m10 — Broker docstrings/`__all__` do not match the implementation
`broker.py:11-13` claims `reduce_only` maps to MT5
`ORDER_TYPE_CLOSE_BY`/position-close semantics; `MT5Broker.execute` actually sends
`TRADE_ACTION_DEAL` with the opposite `type` and a `position` ticket
(`mt5_broker.py:222-234`), and `TRADE_ACTION_CLOSE_BY` is a different action. `__all__`
(`broker.py:32`) advertises the missing `PaperBroker` (B01). `expected_price`'s docstring
("the price an order would fill at") is not true for the MT5 implementation, which returns
the reference unchanged.
**Fix:** correct the docstrings and `__all__`; decide and document one close mechanism
(hedging accounts: opposite deal + `position` ticket is acceptable, `CLOSE_BY` is only for
hedged pairs and is not what the code does).

### m11 — `live/__init__.py` is empty, unlike the source package API
`nornweave_forex/live/__init__.py` is 0 bytes; the source exports `Broker`, `PaperBroker`,
`LiveRunner`, `Guardrails`, `StepResult`, `load_or_initialise`, state types, `feed_is_stale`
(source `live/__init__.py`, 1600 B). `from nornweave_forex.live import Broker` fails today,
and there is no package-level place documenting that `PaperBroker` is the missing piece.
**Fix:** add the exports after the port is real (do not export `PaperBroker` before it exists).

### m12 — `_mark_price` uses the live tick even in paper mode and fails silently
`runner.py:205-215`: it always calls `self.feed.client.symbol_tick(symbol)`, catches every
exception and returns `None`. In paper mode (once `PaperBroker` exists) the runner should
mark from the feed's closed-bar close, not from a live MT5 tick; with a failing tick the
exposure disappears from `equity()` (M15) instead of producing an error.
**Fix:** mark from the newest closed bar in the feed for paper, and use bid/ask only when a
live broker is attached; log and surface failures.

### m13 — `_load_history` bypasses the feed abstraction and under-fetches history
`runner.py:180-203` calls `self.feed.client.copy_rates_from_pos` directly with
`count=min_history_bars+5` (405), ignoring `MT5Feed.lookback` (`feed.py:76`) and the
`FeedError` plumbing; `context.py:1-12` says the runner should fetch at least
`backtest.preroll_bars` (600 default) so EMA/ADX seeding matches the backtest. It also
drops the last row assuming the last row is always forming (`rows[:-1]`), duplicating the
bar returned by `poll_new_bars` (M05).
**Fix:** add a `MT5Feed.history(symbol, count)` that returns closed bars only and use
`max(min_history_bars, backtest.preroll_bars)`.

### m14 — Persisted schema fields that no runner code maintains
`state.py:120-126` (`mt5_ticket`, `exchange_booked_qty/pnl/gross`) are never set by
`runner.py` (only `MT5Broker.on_position_opened` sets `mt5_ticket` as a side effect, and
`exchange_booked_*` are never written). They give the false impression that reconciliation
state is maintained, while M01 shows it is not. `state.py:243 day_realized` is likewise a
dead field.
**Fix:** wire them in the reconciliation port (M01), or remove them until the port lands;
add a test asserting they round-trip through save/load. Relatedly, `LiveFill.pnl` is never
set on execution fills by the port runner (`state.record(fill)` is called with the broker's
`pnl=0.0`; the source sets `fill.pnl` inside `_execute_exit`), so the fill log's PnL column
is always zero.

---

## Port gaps (source → port, by module)

| Source feature | Source location | Port status |
|---|---|---|
| `PaperBroker` (fill price, fee, instrument rounding) | `live/broker.py:129-192` | **missing** (B01) |
| `_fail_safe_entry` (flatten + book + halt on hook failure) | `runner.py:631-693` | missing (B10) |
| `_guard_hook` escalation after `_SWALLOW_LIMIT` | `runner.py:975-1011` | missing; `_swallow` logs only (B10) |
| `_reconcile_exchange_position` | `runner.py:836-973` | missing (M01) |
| `_book_from_exchange`, `_book_external_fill` | `runner.py:698-834` | missing; `closed_since` return fields unused (M01/M18) |
| Startup `broker.reconcile(position)` + orphan detection | `cli.py:1234-1324`, `broker` ABC | missing; port ABC has no `reconcile` (M19) |
| `feed_is_stale` guard | `runner.py:1209-1216` | imported, unused (M02) |
| `act_on_last_bar_only` / pending-bar replay | `runner.py:1221-1243` | flag set, unused (M04) |
| `state.last_bar_ts`-based resume + feed `mark_seen` | `runner.py:1218-1237`, `feed.py` | port has `symbol_last_bar_ts`, never used (M04) |
| `IndicatorSet.context_at` (Dow + daily + tuning + streaks + footprint) | `runner.py:1024-1029`, `context.py` | hand-rolled, wrong, daily hardcoded (B02/B03/B04/M03/M09/M17) |
| Auto Calibration `indicators.tuning.at(index)` | `runner.py:1127-1129` | ignored (M09) |
| Notional guardrail `max_position_notional_pct` + re-round check | `runner.py:518-557` | guardrail field absent (M09) |
| Slot pool (`size`, `held`, `try_acquire`, `occupancy`) + runner claim/release | `slots.py:103-187`, `runner.py:486-500`, `:1151-1179` | module replaced by single lock; runner never uses (M12) |
| Notifier events at the point of action (`_emit`, `_notify`, halt event) | `runner.py:1277-1298`, `notify.py` | notifier stored, never used (M11) |
| `take_profit_event`, `external_event`, `notify_all`, `describe` | `notify.py:212-292` | absent (M11/m08) |
| State schema 3 + `_MIGRATIONS` + newer-file refusal path | `state.py:47-94, 387-397` | schema 1, no migrations (M13) |
| Atomic/durable save: parent mkdir, `0600`, `fsync` | `state.py:422-436` | absent (M14) |
| `_closed_trade` VWAP exit price + `bars_held` | `runner.py:424-446` | last-fill price, `bars_held=0` (M16) |
| `engine.reversal_exit` (configurable rule) | `runner.py:1107-1110`, `engine.py:276-311` | hardcoded `dow_opposite` (M17) |
| `LiveRunner.run_forever` | `runner.py:1300-1313` | absent (CLI rolls its own loop) |
| Feature journal (`FeatureLog`) | `runner.py:461-475, 600-624` | absent (not in scope file list, but live diverges) |
| Package exports | `live/__init__.py` | empty (m11) |

Port line-count ratio confirms the gap rather than a refactor: runner 593/1313,
state 353/452, slots 105/187, notify 213/429, broker 112/192.

**Integration note (outside the five scope files, but blocks the live command):**
`cli.py:137-177` builds `instruments` only in the `if not args.paper:` branch, then passes
`instruments=instruments` unconditionally at `:175`; `nornweave-forex live --paper` raises
`UnboundLocalError` (and would still hit B01). The CLI also never builds a `SlotPool` or a
`Notifier`, and `_run_live` calls `runner.step(state)` in a loop without re-loading state
(`cli.py:186-193`). `tests/test_instrument.py` and `tests/test_sizing.py` are empty, so
there is no import smoke test that would have caught B01–B05.

---

## Recommended next steps (in order)

1. **Make it import and start.** Implement `PaperBroker` for Forex (B01); delete
   `read_dow`/`_build_context` and consume `build_indicators(candles, interval, strategy,
   tuning=...)` + `IndicatorSet.context_at` (B02/B03/B04); use
   `config.portfolio.symbols` (B05). Add a test that imports every `live` module and runs
   one `step()` on synthetic candles with a stub feed/broker — that single test would have
   caught all five.
2. **Fix the money model once.** Introduce `InstrumentSpec`-based money helpers and use
   them in `_execute_exit`, `state.equity`, `ClosedTrade`, `net_pct_of_notional` and the
   daily guard; book each fill's `pnl` exactly once (B06/B07/M05/M16). Add unit tests with
   EURUSD (contract 100k) and USDJPY (floating pip value).
3. **Restore the protective/fail-safe path before any real order.** Port
   `_fail_safe_entry`, `_guard_hook` escalation, `on_position_opened` handling and the
   pre-order ladder (`expected_price`/sizing/rounding) (B08/B10/M09/M10/M19).
4. **Port reconciliation and startup safety.** `_reconcile_exchange_position`,
   `_book_from_exchange`, `_book_external_fill`, a `Broker.reconcile`/`positions_snapshot`
   startup check, and refuse to trade on any local/flat-vs-broker mismatch (M01/M19).
5. **Persistence correctness.** Schema migration map, source-style `StateStore.save`
   (mkdir/0600/fsync), save on every halt, treat save failure as fatal, and fix the
   quarantine/empty-file/KeyError handling (M08/M13/M14/m02/m03/m04).
6. **Wire the safety rails that are currently decorative.** Feed staleness (M02), pending
   bars/restart resume (M04), `day_realized`-based daily guard (M07), slots pool (M12),
   notifier with critical-aware queue and TP/external events (M11/M20).
7. **Parity items after that:** swap/rollover from broker metadata and `closed_since`
   (M18), configurable day boundary (m07), tuning/streaks/footprint in the live context
   (M09/M03), trade-log VWAP (M16), and port the source live tests
   (`tests/test_live.py`, `test_notify.py`, `test_slots.py`, `test_exit_price.py`,
   `test_engine.py::test_closed_trade_pnl_reconciles_with_final_equity`) before enabling
   real money.

# Live runner fixes (task-5)

**Files changed:** `nornweave_forex/live/runner.py` (rewritten in place),
`docs/review/live-fixes.md` (this file). Nothing else was touched.

**Foundation used (read first, not modified):** `instrument.py`
(`pnl_money`, `notional_account`, `swap_money`, `default_instrument`),
`live/state.py` (`book_realized`, account-currency `equity`, `OpenPositionState`
pip metadata, `SCHEMA_VERSION=2`, 31-char `client_id`), `live/broker.py`
(`PaperBroker(instruments=...)`), `config.py` (`Config.symbols` alias),
`strategy/context.py` (`build_indicators(candles, interval, strategy, tuning=...)`
→ `IndicatorSet.context_at/ready_index`), `strategy/exits.py`
(`build_exit_ladder(direction, entry, atr, risk, ...)`), `execution.py`
(`break_even_should_arm(*, ..., tps_hit, arm_on, bar_close)`),
`strategy/engine.py` (`reversal_exit`, recovered from the source).

There is no `read_dow`; the runner no longer imports it and no longer builds
`DailyState`/`SignalContext` by hand.

---

## Summary

The runner is now importable, paper-runnable, and Forex-correct on money:

* one `IndicatorSet` per symbol (Dow + daily THURISAZ + tuning + streaks), built
  with `build_indicators(candles, config.interval, config.strategy, tuning=...)`
  and read through `context_at(index)` after an `index >= ready_index` check;
* money flows through `OpenPositionState.pnl_money` and `LiveState.book_realized`:
  every partial exit books `gross - fee`, the entry commission is charged once,
  swap is booked at the final close, and `state.cash` is never re-credited at
  close;
* the exit ladder is built and the position sized **before** `broker.execute`,
  then re-anchored on the actual fill;
* `OpenPositionState` freezes `pip` / `pip_value_per_lot` / `contract_size` from
  the instrument, so PnL and notional are in account currency rather than price
  units;
* break-even passes `tps_hit`/`arm_on`/`bar_close` and now moves `pos.stop` (so
  the close-decided BE exit actually fires);
* every bar returned by `poll_new_bars()` is processed (not just `bars[-1]`),
  duplicates are rejected by timestamp, and stored history is capped;
* trend reversal is delegated to `SignalEngine.reversal_exit`, honouring
  `reversal_rule` and `exit_on_dow_neutral`;
* sizing/equity use `state.equity(marks)` with tick → last-close → cash
  fallbacks, and the portfolio `open_risk` uses `pos.pip_value_per_lot / pos.pip`;
* halts are persisted immediately, repeated state-save failures halt the
  instance, and broker-hook failures escalate after `_SWALLOW_LIMIT` consecutive
  errors (entry-hook failure attempts `emergency_flatten` then halts).

## Changes mapped to the audit findings (docs/review/live.md)

| Finding | What changed |
|---|---|
| B01 `PaperBroker` missing | imports it; default broker is `PaperBroker(fee_rate=config.backtest.taker_fee, slippage_bps=..., instruments=self.instruments)` |
| B02 `read_dow` missing | removed; context comes from `IndicatorSet.context_at` |
| B03 fake `DailyState` | removed; daily/THURISAZ state is the real `DailyContext.state_for_bar` in the context |
| B04 `build_indicators` arity | now called `(candles, config.interval, config.strategy, tuning=config.tuning)` |
| B05 `Config.symbols` | `load_or_initialise` uses `config.portfolio.symbols` |
| B06 crypto PnL units | exits: `gross = pos.pnl_money(fill.price, lots=filled)`; equity is `state.equity(marks)` in account currency; `ClosedTrade.notional_account` uses the instrument pip value |
| B07 fee double-charge / partial booking | entry fee booked once with `book_realized(0.0, fill.fee)`; every exit books `book_realized(gross, fill.fee)`; no cash credit at final close; swap booked at final close |
| B08 ladder after the order / wrong kwargs | ladder + `position_size` happen before `broker.execute`; post-fill ladder re-anchored with `build_exit_ladder(direction, fill.price, ...)`; a re-anchor failure falls back to the pre-trade ladder |
| B09 `break_even_should_arm` kwargs | passes `tps_hit=tuple(...)`, `arm_on=...`, `bar_close=bar.close` |
| B10 swallowed hook failure | `_guard_hook` escalating after 3 consecutive failures; entry-hook failure goes to `_fail_safe_entry` (flatten attempt + halt); `_halt` saves immediately |
| M03 hardcoded neutral daily | real daily context from `IndicatorSet` (Fib/zone/tp1_override now reachable) |
| M04 only `bars[-1]` | every bar in every poll is processed; `state.symbol_last_bar_ts` filters duplicates; history load seeds the cursor so a cold start does not replay the lookback |
| M05 duplicate/uncapped history | append only when `bar.ts > history.ts[-1]`; a bar already inside the fetched window is skipped (prevents look-ahead); history capped at `3 * warmup` bars |
| M06 sizing equity / stop mismatch | `_equity(state)` for sizing; `position_size(..., stop=ladder.stop)` on the same ladder that is stored |
| M07 daily guard | uses `state.day_realized` vs `day_start_equity` (source formulation), reset at day rollover, checked after every bar |
| M08 halt/save ordering | `_halt` persists immediately; `_persist` escalates repeated save failures into a halt; save is attempted after every bar when `persist_every_bar` |
| M09 expected price / tuning / pre-trade math | `broker.expected_price` before sizing; `IndicatorSet.tuning.at(index)` feeds `atr_factor`/`stop_loss_pct`; all local math precedes the order |
| M10 BE never moved `pos.stop` | on arming, `pos.be_stop = be_stop; pos.stop = be_stop` (rounded via the instrument) |
| M16 closed-trade exit price / bars held | `exit_price` is the volume-weighted exit recovered from accumulated gross PnL; `bars_held` is computed from entry/exit timestamps |
| M17 hardcoded reversal rule | `SignalEngine.reversal_exit(position.direction, context.dow, close=..., ema=...)` |

Also addressed from the task text: per-position `pip`/`pip_value_per_lot`/
`contract_size`; missing-instrument guard (log + skip sizing, no crash);
`instruments=` passed to the default `PaperBroker`; equity fallback to cash when
no marks exist; tick → last-close mark fallback.

Not done in this pass, by instruction: broker reconciliation, slot pool, notifier
event flow (phase 2).

---

## Verification (exact commands and output)

Environment: `.venv\Scripts\python.exe` (Python 3.13.15), working directory
`D:\PROGRAMMING\NornWeaveForex`.

### 1. Import succeeds

```
PS> $env:PYTHONPATH = "D:\PROGRAMMING\NornWeaveForex"
PS> .venv\Scripts\python.exe -c "import nornweave_forex.live.runner; print('runner import OK')"
runner import OK
```

### 2. Paper run on synthetic candles (`%TEMP%\nwf_probe_paper.py`)

Stub feed: `client.copy_rates_from_pos` returns 600 closed synthetic H2 bars + one
forming bar; `client.symbol_tick` returns a two-sided quote; `poll_new_bars`
returns the next bar per step. Config: `Config.from_yaml("configs/forex.yaml")`.
State: `StateStore(%TEMP%/.../live.json)`, `persist_every_bar=True`.

```
init: ['initialised fresh state with 10,000.00']
initial equity: 10000.0
step   7 equity=  9998.83 | EURUSD long 0.09 lots @ 1.10635 stop 1.09529
step  59 equity= 10055.89 | EURUSD take_profit_1 0.02@1.11249 pnl=+12.28
step 113 equity=  9953.72 | EURUSD trend_reversal 0.07@1.09872 pnl=-53.41 | EURUSD closed net=-46.28 (trend_reversal)

--- summary ---
steps: 113 bars: 113
trades_closed: 1 open: []
cash: 9953.72 realized_pnl: -46.28 day_realized: -58.56
mark equity: 9953.72
trade: EURUSD long qty 0.09 entry 1.10635 exit 1.10178 gross -41.13 fees 0.0
       swap -5.15 net -46.28 notional 9957.15 bars 130 reason trend_reversal
  net_pct_of_notional: -0.4648 %
executed ids: 3 max len: 31

PROBE OK
```

Money check by hand: TP1 `61.4 pips × $10/lot × 0.02 lots = +$12.28`; reversal
`76.3 pips × $10/lot × 0.07 lots = −$53.41`; net `−41.13 + 0 (fees) − 5.15 (swap)
= −46.28`, and `cash = 10000 − 46.28 = 9953.72`. `notional = 1.10635 × 0.09 ×
100,000 = 9,957.15`. The old crypto formula would have reported `−0.0004` for the
whole trade and `$0.0001` equity moves; this is now account currency.

### 3. Break-even path + state round-trip (`%TEMP%\nwf_probe_be.py`)

Same stub; `risk.be_trigger_pct` tightened to 0.002 in the probe only.

```
step   1 | EURUSD long 0.08 lots @ 1.11397 stop 1.10283
step   3 | EURUSD BE armed @ 1.11508 | EURUSD break_even_stop 0.08@1.11489 pnl=+7.36 | EURUSD closed net=+7.36 (break_even_stop)
trade: break_even_stop entry 1.11397 exit 1.11489 net 7.36 bars 2
reloaded trades: 1 cash 10007.36 realized 7.36 positions []
PROBE2 OK
```

The exit reason is `break_even_stop` at `1.11489`, while the original stop was
`1.10283` — proof that the close-decided rule used the moved `pos.stop`. The
reloaded state has the same trade, cash and realised PnL as the in-memory state.

### 4. Batch processing + resume dedup (`%TEMP%\nwf_probe_batch.py`)

Stub returns 5 bars per poll and then a second feed re-delivers the last processed
bar as the first bar of the next batch (simulating restart).

```
phase A: steps 31 new_bars total 155 batch counts [5] trades 1 last ts 1607544000000
resume step 1: new_bars 5 actions [] executed ids 2 -> 2 last ts 1607572800000 old 1607544000000
resume step 2: new_bars 5 last ts 1607608800000
PROBE3 OK
```

All bars from every poll were processed (`new_bars == steps × batch`), and the
re-delivered bar was skipped: no extra order id, no duplicate execution.

No repository files were created by the probes; all three live under `%TEMP%`.

---

## Remaining gaps (phase 2 and known leftovers)

1. **Reconciliation is still absent** (audit M01/M19). The runner never calls
   `actual_size` / `closed_since` / `positions_snapshot` / `protective_stop_price`.
   A position closed by the MT5 disaster stop or by hand is still invisible until
   an exit is rejected. `_fail_safe_entry` flattens on an entry-hook failure but
   does not book that round trip (it halts with "PnL NOT booked").
2. **Slots are not wired** (M12): `max_positions` is enforced in-process only;
   `live/slots.py` is unused. Two processes sharing an account can exceed the cap.
3. **Notifier is not wired** (M11): `PortfolioRunner(notifier=...)` stores the
   object but nothing emits entry/exit/halt events; `notify.py` still lacks
   `take_profit_event`/`external_event`.
4. **Feed-staleness guard** (M02) is still not implemented; `feed_is_stale` is no
   longer imported and `Guardrails.stale_tolerance_intervals` remains unused.
   `feed.poll_new_bars()`' internal cursor means a bar dropped because history was
   unavailable is not re-delivered (logged as "skipping").
5. **State layer** (owned by the Lead, not edited here): `StateStore.save` still
   has no parent `mkdir`, `0600` mode or `fsync`; `LiveState.from_dict` defaults a
   missing `schema` to `SCHEMA_VERSION` (the source defaults to 1) and there are
   still no migrations; `ClosedTrade.from_dict` can raise `KeyError` which
   `load_or_initialise` does not catch; `quarantine` still returns a nonexistent
   path when there is nothing to move. The runner now halts after 3 consecutive
   save failures, so a missing `state/` directory becomes a visible halt instead
   of silent unsaved trading.
6. **Swap** is still an intra-bar estimate at a hard-coded 21:00 UTC rollover with
   `swap_triple_day` matched to the rollover weekday; the broker's authoritative
   `swap`/`commission` from `closed_since` is not booked. Accrued swap is not part
   of `equity()` while a position is open.
7. **Restart/catch-up policy:** a bar already inside the fetched history is skipped
   rather than replayed against an older window (that would be look-ahead), so a
   persisted cursor behind the feed fast-forwards to the newest history bar. There
   is no explicit `act_on_last_bar_only`/skip-count reporting (`StepResult` has no
   `stale`/`flat` flags yet).
8. **Sub-lot remainders:** partial rungs smaller than `lot_step` are skipped and
   marked handled; a final remainder below one lot step cannot be closed through
   the current `PaperBroker`/`MT5Broker` reduce path and would need a broker-level
   "close remaining" request.
9. **Equity marks:** a tick failure falls back to the last closed close; if no
   history exists, that position is omitted by `state.equity` (it skips missing
   marks). The fallback keeps the common case honest but is not a substitute for
   reconciliation.
10. **Day boundary** is still UTC wall clock (`utc_day(now)`), not the broker's
    statement day; `day_realized` resets on that boundary.
11. **Other known items not in this task:** the crypto-style
    `max_position_notional_pct` guardrail (Forex margin is handled in
    `risk/sizing.py`), the feature journal, and the source `LiveRunner.run_forever`.
12. **Tests:** the probes above are now committed as `tests/test_live_runner.py`
    (see the section below). Broker reconciliation, slots, the notifier and the
    phase-2 money paths still have no integration coverage.

---

## Committed regression tests (task-11)

`tests/test_live_runner.py` converts the three `%TEMP%` probes into a
self-contained pytest module. It uses a `FakeFeed`/`FakeClient` stub around
`synthetic_candles` (closed bars + one forming bar for
`copy_rates_from_pos`, a two-sided `symbol_tick`, batched `poll_new_bars`),
`Config.from_yaml("configs/forex.yaml")` with per-test overrides, a temporary
`StateStore`, and `default_instrument` specs. No production code changed.

Five tests:

1. `test_paper_run_reconciles_money` — a synthetic paper run opens, takes a
   partial target and closes (test-only overrides: a nearer TP ladder and a
   450-bar warm-up keep the partial-then-close path deterministic and fast).
   Asserts `cash == initial + Σ net_pnl` exactly,
   `net_pnl == gross - fees + swap`, flat `equity({}) == cash`,
   `notional_account == entry × lots × pip_value / pip`, and dollar-scale PnL.
2. `test_break_even_arms_and_exits_at_moved_stop` — asserts the `BE armed`
   action, exit reason `break_even_stop`, and an exit price near the moved BE
   stop (and far from the original stop), which is only reachable if arming
   moved `pos.stop`.
3. `test_batch_processing_and_restart_dedup` — 5-bar batches; every bar is
   appended to history and processed, state round-trips through `StateStore`,
   and a restart feed re-delivering the last processed bar adds no duplicate
   order id and still advances the cursor.
4. `test_kill_switch_halts_and_persists` — the kill-switch file halts before the
   feed is touched, the halt is written to disk, reloaded, and survives the file
   being removed.
5. `test_ten_pips_on_010_lots_is_about_ten_dollars` — drives `_execute_exit` with
   a known 10-pip EURUSD move on 0.10 lots: gross/net ≈ $10 and cash +$10, which
   the crypto `(exit-entry) × qty` formula would book as ≈ $0.00001.

Exact command (from the repo root; no `PYTHONPATH` needed):

```
PS> .venv\Scripts\python.exe -m pytest tests/test_live_runner.py -q
.....                                                                    [100%]
EXIT: 0
```

With the repo's `addopts = "-q"` cleared for a visible summary, the same run
reports `5 passed in ~5.4s` (in-test call durations: paper run ~2.3 s,
batch/restart ~1.5 s, the rest under 0.2 s). Pytest wall time can rise when
other agents load the host; the tests' own work stays around four seconds, inside
the <10 s budget.

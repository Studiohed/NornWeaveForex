# NornWeaveForex — code review (2026-10-19)

**Verdict.** The port was *scaffolded*, not working: `nornweave_forex.cli` and the live
runner failed at import, the backtester and runner were written against APIs that do
not exist in the (correctly copied) strategy modules, and every money calculation used
crypto units (`(exit - entry) * qty`) even though `qty` is now **lots**. After this
review pass the offline stack and the paper-mode runner are runnable and unit-tested;
the live MT5 path is hardened but still lacks reconciliation/safety rails before real
money.

## Method

Four read-only audits ran in parallel against the reference project
`D:\PROGRAMMING\NornWeave` (never modified):

| Area | Report | Findings |
|---|---|---|
| Core strategy port fidelity + config | [`docs/review/core.md`](core.md) | 12 blocker / 10 major / 9 minor |
| MT5 server, client, broker, instrument, sizing | [`docs/review/mt5.md`](mt5.md) | 3 blocker / 14 major / 16 minor |
| Live runner, state, slots, notify, broker ABC | [`docs/review/live.md`](live.md) | 10 blocker / 20 major / 14 minor |
| Backtest, metrics, CLI, dashboard, screener, tests | [`docs/review/offline.md`](offline.md) | 9 blocker / 17 major / 8 minor |

Evidence was gathered by reading the code, diffing/hashing it against the source,
and executing probes with `.venv\Scripts\python.exe` — not by inspection alone.

## Verified correct

* `types.py`, `features.py`, `indicators/core.py`, `indicators/swings.py` are
  **byte-identical** to the source; all `strategy/*.py`, `indicators/__init__.py`
  and `execution.py` are identical after the package-import rename only. The
  broker-agnostic strategy core was ported faithfully.
* The RPyC auth model works: pre-auth calls raise `PermissionError`, private
  attributes are unreachable, a wrong secret is rejected, and only `exposed_*`
  methods are callable (verified at runtime with rpyc 6.0.2).
* MT5 call shapes (`copy_rates_from_pos`, `symbol_info_tick`, `positions_get`,
  `history_deals_get`, retcodes) were mostly right; the defects were the order
  request, units and missing metadata.

## Blockers found and fixed in this pass

| # | Problem | Fix |
|---|---|---|
| B1 | `live/runner.py` imported `PaperBroker` (never defined) and `read_dow` (does not exist) → CLI and runner could not import | Forex `PaperBroker` implemented (lot rounding, rejects below min, account-currency fees); dead imports removed; runner ported to `IndicatorSet.context_at` |
| B2 | `build_indicators(candles, strategy)` / dict-style `indicators["ema"]` / fake `DailyState` → `TypeError` on the first bar in runner and backtester | Both consumers now build `IndicatorSet` with `interval` + `tuning` and read `context_at(idx)`; the daily/Dow filters are real, not stubbed |
| B3 | `build_exit_ladder`/`break_even_should_arm` called with non-existent kwargs; the ladder was built *after* the entry order was sent | Correct keyword/positional calls; ladder + sizing are computed **before** `broker.execute`, then rebased on the actual fill |
| B4 | Backtester filled a signal at the **same bar's open** whose close produced it (one full bar of look-ahead) | Pending entry fills at the **next** bar's open; proven by test |
| B5 | PnL, equity, cash, closed trades and the daily-loss guard omitted `contract_size`/pip value (~100,000× too small) | Single money conversion: `InstrumentSpec.pnl_money` / `OpenPositionState.pnl_money` (`price_diff / pip * pip_value_per_lot * lots`), used everywhere |
| B6 | Realised PnL, exit fees and swap never reached cash in the backtester; partial exits were never booked live | Cash is credited per fill (`LiveState.book_realized`, engine `fill_exit`); swap is credited/charged; `sum(net_pnl) == equity change` |
| B7 | `live --paper` crashed (`instruments` unbound) before the first bar | CLI always builds instruments; `PaperBroker(instruments=...)` used |
| B8 | MT5 client retried **every** call, including `order_send` — a lost reply could duplicate a live order | Read-only retry whitelist; mutations are at-most-once and raise `MT5ConnectionLost` for reconciliation |
| B9 | MT5 order request omitted `price` (dead variable), kept the pre-clamp volume, and could attach SL/TP to the wrong ticket | Price (ask/bid, digits-rounded), volume clamped before sending, ticket from `result["order"]`, ambiguous matches rejected |
| B10 | Deterministic `client_id` was truncated to 4-char symbol prefixes → `USDCAD`/`USDCHF` exits collided and one was dropped as a duplicate | Full 6-letter symbol kept, purpose hashed, id capped at MT5's 31-char comment; regression test added |
| B11 | `live --paper`/backtest had no runnable config (`configs/` empty) | `configs/forex.yaml` added and pinned by tests |

## Major issues resolved

* **Swap**: MT5's real field is `swap_rollover3days` (Sunday=0), not `swap_triple_day`;
  it is now converted to a Python weekday (Wednesday=2), `swap_mode` POINTS is
  converted to account currency, and the triple day no longer lands on Thursday.
* **Sizing margin**: replaced the `entry < 10` guess with
  `InstrumentSpec.notional_account / leverage`; the margin cap is always applied
  (`free_margin` when available, otherwise `equity * leverage`), and max-lot clamping
  keeps the decision consistent.
* **Default config**: crypto defaults removed — `symbol=EURUSD`, `taker_fee=0`,
  `swap_enabled`, FX stop/break-even/leverage defaults, FX major/cross presets,
  validation for intervals > 1 day, duplicate symbols, `max_positions`,
  symbol/portfolio membership and portfolio budget.
* **Live runner hardening**: real `IndicatorSet` context (Dow + daily + tuning),
  portfolio sizing from `state.equity(marks)`, all polled bars processed with a
  persisted cursor and duplicate/look-ahead guard, capped history, ladder and
  sizing before the order, post-fill re-anchoring, break-even moves `pos.stop`,
  `SignalEngine.reversal_exit`, VWAP exit price / `bars_held`, hook-failure
  escalation, fail-safe flatten + halt on a failed protective order, and halts
  persisted immediately.
* **State durability**: `StateStore.save` creates its parent directory, writes via
  `fsync` + rename, sets `0600`; empty/corrupt/missing-field files are handled
  explicitly; legacy files without `schema` load as v1.
* **Backtest accounting**: spread paid on both sides, per-symbol mark-to-market,
  trend-reversal exit, end-of-data settlement, exposure ≤ 100 %, one equity point per
  portfolio timestamp, correct `compute_metrics` arity, "Swap paid" label.
* **CLI**: `--help`, `demo`, `backtest`, cached-NPZ loading with synthetic fallback,
  `--start/--end`, fetch drops the forming bar, lazy live imports.

## Tests added (was 0 bytes / 0 tests)

| File | Tests | Covers |
|---|---|---|
| `tests/test_instrument.py` | 13 | pip size/values, `pnl_money` ($10 per 10 pips/0.1 lot EURUSD, $6.67 USDJPY), notional, swap modes, lot rounding, `accepts`, `from_mt5` metadata |
| `tests/test_state_money.py` | 12 | position/equity money math, `book_realized`, `ClosedTrade` notional %, schema round-trip, `client_id` length/uniqueness |
| `tests/test_paper_broker.py` | 7 | lot rounding/meets minimum, rejections, slippage, fee = notional × rate |
| `tests/test_execution.py` | 30 | verbatim port of the source's bar/exit-rule suite (`execution.py` unchanged) |
| `tests/test_state_schema.py` | 35 | schema ≥ 2, multi-symbol round-trip, quarantine, unknown keys, cross-pair id uniqueness |
| `tests/test_config.py` | 51 | shipped `configs/forex.yaml` pinned + every validation rule |
| `tests/test_imports.py` | 45 | every module imported, CLI parser built (tripwire for the B1 class) |
| `tests/test_sizing.py` | 18 | risk-based lots, portfolio cap, min-lot bump, margin scaling/fallback, rejections |
| `tests/test_mt5_broker.py` | 22 | request shape (`price`, `sl`/`tp`, 31-char comment), reduce-only clamp/ticket, partial fills, at-most-once `order_send`, netting refusal, SL/TP hooks |
| `tests/test_backtest_engine.py` | 9 | $ PnL and cash reconciliation, no look-ahead, spread both sides, swap/triple, per-symbol marks, END_OF_DATA |
| `tests/test_synthetic.py` | 9 | digits rounding, OHLC ordering, weekend gaps, deterministic seeds |
| `tests/test_live_runner.py` | 5 | paper run money reconciliation, BE moved stop, batch/restart dedup, kill-switch halt, 10-pip ≈ $10 guard |

Run: `.venv\Scripts\python.exe -m pytest -q` → **260 passed** in ~16 s.

## Remaining gaps (phase 2 — not fixed here)

1. **Live reconciliation & startup safety** — `positions_snapshot`, `closed_since`,
   `actual_size`, `protective_stop_price` exist on the broker but are not called; a
   local/flat-vs-broker mismatch must block trading, and a restart must re-attach
   tickets. `_fail_safe_entry` flattens and halts but cannot book the round trip
   without deal history.
2. **Feed/session semantics** — wire `feed_is_stale`, replay pending bars after a
   restart, normalise broker server time to UTC, model weekend/session gaps in the
   backtester and skip out-of-session live bars.
3. **Unused safety rails** — `slots.py` (single lock, not used), `notify.py` (never
   invoked), notional guardrail, `order_check` preflight (exposed but unwired),
   `filling_mode` seeding (currently learned on retcode 10030), swap from
   `history_deals_get` instead of the 21:00 UTC estimate.
4. **Dashboard** — 104 lines vs the source's 948; only `/api/state` + `/api/trades`.
5. **Parity extras** — `exit_order: exits_first`, Auto Calibration ladder overrides
   are wired in the runner now but the backtester still ignores `exit_order`.
6. **Full test port** — the source has 26 test files; backtest-engine, live-runner,
   notify, slots and dashboard tests are still to come.

## Reports

* [`docs/review/core.md`](core.md) — strategy/config audit
* [`docs/review/mt5.md`](mt5.md) — MT5/sizing audit
* [`docs/review/live.md`](live.md) — live engine audit
* [`docs/review/offline.md`](offline.md) — backtest/CLI/dashboard audit
* [`docs/review/mt5-fixes.md`](mt5-fixes.md) — MT5 hardening record
* [`docs/review/offline-fixes.md`](offline-fixes.md) — backtest/CLI fix record
* [`docs/review/live-fixes.md`](live-fixes.md) — live runner fix record
* [`docs/review/tests-fixes.md`](tests-fixes.md) — test port record

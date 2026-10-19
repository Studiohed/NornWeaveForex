# Offline stack audit вЂ” backtest, metrics, CLI, dashboard, screener, data, tests

Reviewer: `audit-offline` (task-4). Read-only audit; no production code modified.
Baseline: `D:\PROGRAMMING\NornWeave` (source of truth, untouched). Target: `D:\PROGRAMMING\NornWeaveForex`.
Interpreter: `.venv\Scripts\python.exe` (Python 3.13.15); `rpyc` present; `fastapi` present.

## Summary

The offline stack is **not runnable end to end**. The CLI cannot even print `--help`
(`ImportError: PaperBroker`), and the backtester contains a chain of six independent
API-drift defects before it reaches the money math: wrong `build_indicators` call,
dict access on an `IndicatorSet`, a non-existent `read_dow` import, wrong
`build_exit_ladder` keywords, wrong `break_even_should_arm` keywords, and a
`compute_metrics` call missing two required arguments. Because these fail on the first
bar/trade, nothing downstream (metrics, report, dashboard artifacts) has ever executed.

When the API drift is shimmed away (so the accounting can be observed), the Forex money
model is still wrong in four ways that each invalidate every reported number:

1. PnL is computed in **price units**, missing `contract_size` (100,000Г— too small),
   while fees *do* include `contract_size` вЂ” so costs are 100,000Г— the PnL scale.
2. Realized PnL and swap are **never credited to cash**; the equity curve ignores every
   closed trade.
3. Multi-symbol mark-to-market values **every position at the current event's
   `bar.close`**, i.e. a USDJPY position is priced at the EURUSD rate.
4. Triple-swap is booked on the wrong day (Thursday, not Wednesday).

Verified probes (details in each finding): a EURUSD TP fill with `pricediff=0.0038`
yields `engine_pnl=1.1e-05` where the account-currency amount is `$1.14`; a fully closed
stop trade had `net_pnl=-0.001` while the equity curve *rose*; a single-symbol run held
one position for 50 days with `funding=0.0`; Wednesday's 21:00 rollover charged `-5.0/lot`
and Thursday's charged `-15.0/lot`.

Severity counts: **9 BLOCKER, 17 MAJOR, 8 MINOR** (plus port gaps and a test matrix).
`tests/` currently runs **zero tests** (`pytest: no tests ran in 0.02s`); both test files
are 0 bytes and `configs/` is empty (no `forex.yaml`), so even after the import blocker
every data subcommand fails at config load.

## Blockers

### B1 вЂ” `PaperBroker` is declared but not defined; CLI import chain dies (`cli.py:30`, `live/runner.py:37`, `live/broker.py:32`)

`live/broker.py:32` declares it, but the class does not exist:

```python
__all__ = ["OrderRequest", "Broker", "PaperBroker", "OrderRejected"]   # broker.py:32
```

```
$ .venv\Scripts\python.exe -m nornweave_forex.cli --help
  File ".../live/runner.py", line 37, in <module>
    from nornweave_forex.live.broker import Broker, OrderRejected, OrderRequest, PaperBroker
ImportError: cannot import name 'PaperBroker' from 'nornweave_forex.live.broker'
```

Verified `defined: ['OrderRequest', 'Broker', 'OrderRejected']` vs
`declared: [..., 'PaperBroker', ...]`. `cli.py` imports `live.runner` at module scope, so
**every** command (including `--help`) fails; the script entry point
`nornweave-forex = nornweave_forex.cli:main` is dead.

Fix: implement `PaperBroker` (lot-based qty, `pip_value_per_lot`, static/typical spread,
`fee_rate` per-lot not per-notional, `slippage_bps` against the adverse side) вЂ” it is also
required for `live --paper`; or, until it exists, import it lazily inside `PortfolioRunner`
so the offline commands are not coupled to the live path. Add a CI smoke test
`python -m nornweave_forex.cli --help` в†’ exit 0.

### B2 вЂ” Both engines import a function that does not exist (`backtest/engine.py:396`, `live/runner.py:50`)

```python
from nornweave_forex.strategy.dow import read_dow      # engine.py:396, runner.py:50
dow = read_dow(candles, idx, self.config.strategy)      # engine.py:399 / runner.py:549
```

`strategy/dow.py:17` exports only `DowTracker, BreakoutDow, dow_trend_series, PivotPoint`.
Verified `ImportError: cannot import name 'read_dow' from 'nornweave_forex.strategy.dow'`.
This fires on the **first signal evaluation** in the backtester (and on import in the
runner, masked there by B1).

Fix: match the source design вЂ” build `dow_trend_series` once in `build_indicators`
(`strategy/context.py:107`) and read `IndicatorSet.dow_codes[idx]` (source
`backtest/engine.py:431-434`). Delete the per-bar `read_dow` path.

### B3 вЂ” `build_indicators` called with the wrong signature and its result used as a dict (`backtest/engine.py:158-161`, `400-410`)

```python
warm = candles.slice(0, idx + 1)
indicators[symbol] = build_indicators(warm, cfg.strategy)   # engine.py:160
ind = indicators[symbol]
...
ema = indicators["ema"]; adx=float(indicators["adx"][idx])  # engine.py:400-410
```

Actual signature (`strategy/context.py:93-99`):
`build_indicators(candles, interval, strategy, delta=None, tuning=None) -> IndicatorSet`.
Probe: `TypeError: build_indicators() missing 1 required positional argument: 'strategy'`.
Even with that patched, `IndicatorSet` is a dataclass, not a dict вЂ” `ind["ema"]` raises
`TypeError`. The source builds indicators **once** and uses
`IndicatorSet.context_at(i)` (source `backtest/engine.py:186-193, 519`), which is also the
only way the daily filter and `ready_index` are wired.

Fix: build one `IndicatorSet` per symbol before the event loop and call
`ind.context_at(idx)`; start at `max(warmup_bars, ind.ready_index)`; delete the per-bar
rebuild (see M17).

### B4 вЂ” `build_exit_ladder` called with crypto-era keyword names (`backtest/engine.py:270-278`, `live/runner.py:515-523`)

```python
ladder = build_exit_ladder(
    entry_price=entry_price, atr=pend.atr, direction=pend.direction,
    risk_params=cfg.risk, tp1_override=..., atr_factor=None, stop_loss_pct=None)
```

Actual signature (`strategy/exits.py:60-70`) is positional:
`build_exit_ladder(direction, entry, atr, risk, tp1_override=None, ...)`.
Probe: `TypeError: build_exit_ladder() got an unexpected keyword argument 'entry_price'`.
Every entry attempt dies here, so `entries_taken` can never exceed 0.

Fix: `build_exit_ladder(pend.direction, entry_price, pend.atr, cfg.risk, tp1_override=...)`
(both call sites).

### B5 вЂ” `break_even_should_arm` called with a removed keyword and without `tps_hit` (`backtest/engine.py:217-224`, `live/runner.py:334-341`)

```python
break_even_should_arm(be_mode=..., direction=..., bar_high=..., bar_low=...,
    be_trigger_price=..., close=bar.close, be_arm_on=...)          # engine.py:217
```

Actual signature (`execution.py:214-223`): `... be_trigger_price, tps_hit, arm_on=..., bar_close=None`.
Probe: `TypeError: break_even_should_arm() got an unexpected keyword argument 'close'`.
Any position that survives its first bar crashes the run; `be_mode="after_tp1"` cannot
work at all because `tps_hit` is never passed.

Fix: `break_even_should_arm(..., tps_hit=tuple(pos.tps_hit), arm_on=..., bar_close=bar.close)`
(both call sites).

### B6 вЂ” `compute_metrics` called with 3 of 5 required arguments (`backtest/engine.py:334`)

```python
metrics = compute_metrics(trades, eq, ts)          # engine.py:334
```

Actual signature (`backtest/metrics.py:140-147`):
`compute_metrics(trades, equity_curve, timestamps, initial_equity, interval, bars_in_market=0)`.
`initial_equity` and `interval` have no defaults, so `BacktestResult` is never constructed вЂ”
**any** successful run raises `TypeError` at the very end. `bars_in_market` is also omitted,
so `exposure_pct` would stay `0.0` even after the signature fix.

Fix: `compute_metrics(trades, eq, ts, cfg.backtest.initial_equity, cfg.interval, bars_in_market=bars_in_market)`
(source `backtest/engine.py:575-582`).

### B7 вЂ” Look-ahead: entry fills at the **signal bar's open** (`backtest/engine.py:233-313`)

```python
if symbol not in positions and symbol not in pending:
    ctx = self._build_context(...)                 # bar i's close
    signal = self.engine.evaluate(ctx)
    ... pending[symbol] = _PendingEntry(...)       # 241
...
if symbol in pending:                              # 255 вЂ” same iteration, same bar i
    entry_price = bar.open + spread_cost           # 262 вЂ” bar i's open
```

The module docstring (lines 15-17) claims the opposite ("signals at the close of bar `i`
fill at the open of bar `i + 1`"), and the source defers via `pending_entry` to the next
loop index (source `backtest/engine.py:450-470`). Verified: in a probe the first trade has
`entry_bar=200` = the first post-warmup bar, filled at `open[200]` using a signal computed
from `close[200]`. This is a look-ahead bias of one full bar on every trade вЂ” the single
most flattering possible bug in a backtest.

Fix: keep a pending entry per symbol and fill it on the **next** event for that symbol (or
mirror the source's two-phase `pending_exit`/`pending_entry` loop), recording
`entry_bar` = the fill bar.

### B8 вЂ” Realized PnL and swap never reach cash; equity curve ignores closed trades (`backtest/engine.py:189, 309, 323-330, 351-371`)

```python
def _fill_exit(self, pos, plan, bar_ts, cash: float) -> None:
    ...
    pos.trade.gross_pnl += pnl          # 366
    pos.qty -= portion                  # 367
# cash is a by-value float and is never incremented anywhere in _fill_exit
```

Entry only does `cash -= fee` (line 309). No realized PnL is ever added back, and the
end-of-data close (lines 323-330) updates only `pos.trade.gross_pnl`, not cash. The source
does `realized += gross` inside `execute_exit` and `realized -= fee` in `charge_fee`
(source `backtest/engine.py:239-264`).

Probe with `stop_loss_pct=-0.0005` (fully closed stop trade): `net_pnl=-0.001`,
`sum net_pnl` over trades `-0.1649`, but `equity_curve[-1]-equity_curve[0]=+0.0014`; once
the position is deleted the equity curve *loses* the unrealised loss instead of booking
the realized one. Consequences: net profit, drawdown, Sharpe, Calmar, yearly returns and
`final_equity` are all meaningless even after B6 is fixed.

Fix: make the run state mutable (`nonlocal cash` inside a nested exit helper, as in the
source) and credit `gross - fee` on every fill; on full close also subtract
`pos.swap_accrued` and copy it to `Trade.funding` (M2).

### B9 вЂ” Forex PnL is missing `contract_size`; fees include it (`backtest/engine.py:319, 329, 360, 289, 359`)

```python
unrealised += (bar.close - entry) * qty * sign                    # 319
pnl = (price - entry) * portion * sign                             # 360
fee = price * portion * instrument.contract_size * taker_fee       # 359
```

For lots, the account-currency PnL is
`(exit - entry) * lots * contract_size * quote_to_account_fx`. The engine computes the
crypto formula (`qty` in base units), but quantity is now **lots**, so PnL is 100,000Г—
too small вЂ” while the fee formula was correctly converted, so the fee/PnL ratio is off by
that same 100,000Г—. Verified on a EURUSD synthetic run:

```
fill take_profit_1 pricediff 0.0038  engine_pnl 1.1e-05  expected_$ 1.14  factor 100000.0
fill take_profit_2 pricediff 0.0076  engine_pnl 2.3e-05  expected_$ 2.28  factor 100000.0
```

With the default `taker_fee=0.00055` (M9) every entry is charged `price*lots*100000*0.00055`
(в‰€ $60 per lot) against PnL in cents вЂ” any run loses ~100% of equity by construction.

Fix: define one helper `account_pnl(price_a, price_b, lots, spec)` that uses
`contract_size` and the quote-currency conversion (for `XXXUSD`/`USDJPY`/crosses the
cleanest broker-truth route is `(b-a)/spec.pip * spec.pip_value_per_lot * lots`), and use
it for unrealised PnL, fills, terminal close and `_open_risk`. Also store risk in account
currency on the `Trade` (or derive it) so `Trade.r_multiple` stays consistent.

## Major

### M1 вЂ” Multi-symbol marks every position at the current event's price (`backtest/engine.py:316-321`)

```python
for sym2, pos2 in positions.items():
    inst2 = self.instruments[sym2]
    unrealised += (bar.close - pos2.trade.entry_price) * pos2.qty * pos2.trade.direction.sign
```

`bar` belongs to whichever symbol's event is being processed (`bar.close` of EURUSD used
for the open USDJPY position, and vice versa). Probe: two-symbol run's equity curve
oscillates `10000.0 в†’ 10001.1 в†’ 10000.0 в†’ 10001.1 вЂ¦` purely from the wrong marks.
`inst2` is also fetched and never used. Fix: keep `last_close: dict[str, float]` per
symbol, update it for the current symbol's bar, and mark each position with its own
symbol's last close (and its own contract size, see B9).

### M2 вЂ” Swap is computed and then thrown away (`backtest/engine.py:229-230, 373-383`)

```python
pos.swap_accrued += self._swap_for_bar(symbol, pos, bar_ts)   # 230
```

`_OpenPosition.swap_accrued` is never read again: not subtracted from `cash`, not added to
`Trade.funding` (line 191 just appends the trade), not shown in the report. Probe: a
position held **2020-09-30 в†’ 2020-11-19** reports `funding=0.0` (в‰€ 50 nights of
`swap_long=-5/lot` missing). The Forex-specific replacement for crypto funding is
therefore dead code. Fix: accrue into realized equity at each rollover (or mark it in the
unrealised calculation) and, on close, set `trade.funding = pos.swap_accrued` so
`metrics.total_funding` вЂ” renamed to swap вЂ” is real.

### M3 вЂ” Triple-swap day is off by one (`backtest/engine.py:380-381`, `live/runner.py:590-591`)

```python
if bar_start.weekday() == instrument.swap_triple_day:   # 380
    swap *= 3
```

`datetime.weekday()` is Monday=0 вЂ¦ Sunday=6, so `swap_triple_day=3` means **Thursday**.
`docs/PLAN.md` ("Wednesday's rollover books 3 days of swap") and
`instrument.py:75-76` ("3 = Wednesday is the most common") both intend Wednesday
(`weekday()==2`; MT5's `ENUM_DAY_OF_WEEK` also uses 3=Wednesday, which is likely where the
default came from). Verified probe:

```
Wednesday 20:00 bar swap/lot = -5.0   (weekday()= 2)   # expected -15.0
Thursday  20:00 bar swap/lot = -15.0  (weekday()= 3)   # expected -5.0
```

Fix: convert the broker/MT5 day to Python's convention
(`python_weekday = (swap_triple_day - 1) % 7`) in one shared helper used by backtest and
runner, and unit-test all seven days.

### M4 вЂ” Exit fills never pay the spread (`backtest/engine.py:8-10 vs 351-371`)

The docstring says "Every fill pays half the typical spread per side", but `_fill_exit`
only applies `slippage_bps` (zero by default); no spread is ever subtracted on exits вЂ”
stop, TP or end-of-data. Probe: stop fill price is exactly `1.11237` = the ladder stop.
Entry only charges the half-spread through the price (`entry_price = bar.open В± spread/2`,
line 259-266), so the modelled round trip is half the intended 1Г— spread, and a resting TP
pays nothing while a live TP would fill at bid/ask. Fix: apply `В±spread/2` on every market
exit (stop / close-triggered / end-of-data) and document that resting limits fill at their
level (no spread), matching what MT5 would do.

### M5 вЂ” No trend-reversal exit in the backtester (`backtest/engine.py` vs `live/runner.py:353-360`)

The live runner exits the whole position when the Dow read flips
(`config.strategy.exit_on_dow_reversal`), and the source engine does the same in
`close_decided_exits` (source `backtest/engine.py:431-437`). The Forex backtester has no
such branch at all: `positions` are only closed by intrabar plans, a close-triggered stop
or end-of-data. Result: the backtest measures a different strategy from the one that will
trade. Fix: add the close-decided reversal exit using `dow_codes[idx]` (B2/B3), with the
same next-bar-open convention as the source for `exit_fill=="level"`.

### M6 вЂ” `exit_fill="level"` ignores the engine's own next-bar convention (`backtest/engine.py:202-210`)

```python
plan = ExitPlan(reason=..., price=bar.close, qty=pos.qty, slippage=True)
self._fill_exit(pos, plan, bar_ts, cash)
```

A close-triggered stop fills at *this* bar's close even though the docstring and the source
convention (`pending_exit`, source lines 441-455, 513-516) defer close-based decisions to
the next bar's open when `exit_fill` is `level` (the default). Fix: implement
`pending_exit` exactly as in the source (`exit_fill=="bar_close"` в†’ fill at close;
otherwise fill at next bar open).

### M7 вЂ” End-of-data close is not a fill (`backtest/engine.py:323-330`)

```python
pos.trade.gross_pnl += (last_close - pos.trade.entry_price) * pos.qty * pos.trade.direction.sign
trades.append(pos.trade)
```

No `Fill`, no `ExitReason.END_OF_DATA`, no fees/slippage/spread, no cash update, no
`Trade.funding`, no equity-curve correction. Metrics then classify such a trade as
`"unknown"` in `exit_breakdown` and it is missing from the per-fill view
(probe: last trade `exit_bar=799`, `fills=2`, no terminal fill). Source closes it with
`execute_exit(..., END_OF_DATA, ...)` and sets `equity_curve[-1] = realized` (source
551-567). Fix: route through `_fill_exit` with an `END_OF_DATA` plan.

### M8 вЂ” Backtest never enforces margin/leverage (`backtest/engine.py:280-287`, `risk/sizing.py:91-105`)

`position_size(..., open_risk=...)` is called without `free_margin`; in `sizing.py` the
margin check is inside `if free_margin is not None`, and the docstring's promised fallback
("When `None`, the cap falls back to `equity * risk.leverage` against the notional") is not
implemented. Probe: on $10,000 equity with default `leverage=5`, a 0.05%-distance stop
sized **1.79 lots EURUSD = $198k notional** (в‰€ 20:1) and was accepted. Fix: implement the
fallback notional cap (`lots <= equity*leverage / (contract_size*price_in_account_ccy)`)
or pass `free_margin=equity` from both backtest and runner.

### M9 вЂ” Crypto risk/fee defaults are still in the Forex config (`config.py:263, 265, 451, 462, 581`)

```
Config(): symbol=DOGEUSDT category=linear taker_fee=0.00055
          stop_loss_pct=-0.05  be_trigger_pct=0.09
```

On EURUSD a 5% stop is ~550 pips and a 9% break-even trigger is ~1000 pips (never reached
on H2 bars); `taker_fee=0.00055` is 0.055% **of notional** (~$60/lot) whereas MT5 Forex
commission is ~$3.5/lot/side, and `funding_enabled=True` is crypto-only (unused by the
engine). The 5% `stop_loss_pct` also feeds `build_exit_ladder` throughout the backtest,
so every Forex position is sized against an absurdly wide stop. `conftest.py` repeats the
same pattern with `-0.02` (в‰€220 pips). Fix: add pip/ATR-denominated risk parameters
(`stop_pips` or ATR-based stop), per-lot commission (`commission_per_lot`), a `max_spread_points`
portfolio setting, and remove/rename funding for Forex. `docs/PLAN.md` already states
sizing must be "in pips".

### M10 вЂ” Sharpe/Sortino annualisation is wrong for a multi-symbol Forex curve (`metrics.py:232-243`)

```python
rets = np.diff(equity_curve) / np.maximum(equity_curve[:-1], 1e-12)
bars_per_year = _MINUTES_PER_YEAR / (interval_ms(interval) / 60_000.0)   # 365d, 24/7
```

The engine appends one equity point per `(symbol, bar)` event (engine.py:320-321), so with
two symbols the curve is twice as long and has duplicate timestamps; the returns series is
per-event, not per-bar, and includes one zero return per symbol per flat period. Sharpe
therefore changes when you add a pure watchlist symbol with no trades, and annualisation
uses 4,380 bars/year instead of the Forex calendar (~260 trading days в‰€ 3,120 H2 bars).
`exposure_pct` divides `bars_in_market` (per-symbol bars) by per-event points вЂ” it mixes
units (and is left at 0 while B6 exists). Fix: build one portfolio equity point per
timestamp using each symbol's last close (this also fixes M1 for metrics), compute returns
over that series, and annualise with trading days (or derive the period from the actual
timestamp spacing).

### M11 вЂ” `Trade.exit_bar` never set on normal exits (`backtest/engine.py:370-371`, `types.py:254-257`)

`_fill_exit` sets `pos.trade.exit_ts` but not `exit_bar`. Fully closed stops keep
`exit_bar=-1`, so `bars_held` returns 0 and `metrics.avg_bars_held` silently understates
(probe: stop-closed trade `exit_bar=-1`, `avg_bars_heldв‰€2.08` from other trades); any
exit-time analysis is impossible. Fix: pass the bar index into `_fill_exit` and set
`pos.trade.exit_bar` when `qty<=0` (as the source does at line 281).

### M12 вЂ” Screener is dead code and not configurable (`screener.py:35-67`)

`screener.py` is imported by nothing (grep: only its own module). There is no
`screen` CLI subcommand, `max_spread_points` is a function default rather than a
`Config`/`PortfolioParams` field, and the live runner has no spread gate at entry вЂ” the
only spread filter in the stack is unreachable. It also catches only `MT5ClientError`, so a
malformed `symbol_info`/`symbol_tick` payload (KeyError/TypeError) aborts the whole screen.
Fix: add `portfolio.max_spread_points`, call `screen_symbols` from `live`/`backtest`
startup (and/or a `screen` command that prints `symbol, spread, tradeable, reason`), and
treat any exception per symbol as `tradeable=False` with a reason.

### M13 вЂ” CLI `backtest` ignores its flags, cached data and the missing config (`cli.py:60-64, 104-115`)

```python
candles = synthetic_candles(symbol, bars=2000, interval_minutes=120)   # 109
```

`--start`/`--end` are parsed (62-63) and never referenced; `--symbols` only changes the
synthetic loop; `fetch` writes `data/candles/<SYM>.npz` (99-100) that nothing ever reads;
`--config` defaults to `configs/forex.yaml` while `configs/` is **empty** вЂ” verified
`ConfigError: config file not found: ...\configs\forex.yaml`, so `fetch`, `backtest`,
`live`, `demo` all fail at `_load_config` once B1 is fixed. `interval_minutes=120` is
hard-coded, so `cfg.interval` is ignored. Fix: ship `configs/forex.yaml`; load the npz
cache (fall back to synthetic only with an explicit `--synthetic`); honour
`--start/--end`; write the result JSON artifact (`--json`), which the dashboard/analysis
can consume.

### M14 вЂ” Dashboard lost nearly all observability and cannot be launched (`dashboard/app.py:55-104`)

Current routes are only `/`, `/api/state`, `/api/trades`. The source dashboard (948 lines)
exposes `/api/overview`, `/api/portfolio`, `/api/symbols`, `/api/position`,
`/api/chart/{symbol}`, `/api/strategy`, `/api/health` (source `dashboard/app.py:905-944`).
Lost functionality that matters for Forex:

- **No mark-to-market equity.** State JSON has no `equity` key (`live/state.py:286-306`),
  so `app.py:86` falls back to `cash`; open-position PnL, margin level and daily loss are
  invisible. (`LiveState.equity` also omits `contract_size`, but that is the live
  reviewer's file.)
- **No health/liveness panel** вЂ” no state-file age, last-bar timestamp per symbol, halted
  reason rendering (the API returns `halted`/`halt_reason` but the page ignores them), no
  feed staleness, which is exactly what a Forex bot needs across weekend gaps.
- **No chart/candles/markers** (`/api/chart/{symbol}` + history dir), no strategy/config
  view, no per-symbol portfolio view across the 8 majors.
- **Missing/corrupt state file is a hard 404** on every route (source returns a graceful
  empty overview; source `test_overview_is_graceful_without_a_state_file`), and the HTML
  page itself 200s but then JS throws.
- **No way to start it.** There is no `uvicorn`/`build_app` call anywhere in the package
  and no `dash` subcommand (source CLI had one), so `build_app` is only importable from
  Python вЂ” the dashboard is unreachable from the product.

Fix: add the missing read-only endpoints against the state file + a `dash` CLI command
(`uvicorn nornweave_forex.dashboard.app:...`), include `equity`/marks in the state
payload, render `halted`, and return a valid empty page when the state file is absent.

### M15 вЂ” `live --paper` is broken before it starts (`cli.py:137-177`)

`instruments` is defined only inside `if not args.paper:` (line 146) but used
unconditionally at line 175 (`instruments=instruments`) в†’ `UnboundLocalError` in paper
mode. Combined with B1 (no `PaperBroker`) there is no offline live path at all. Fix:
build the instrument map for both branches (`from_mt5` when connected, `_default_instrument`
otherwise) and make `PaperBroker` the default offline broker.

### M16 вЂ” Warmup ignores indicator validity (`backtest/engine.py:119, 126`)

`self._warmup = max(config.backtest.warmup_bars, 1)` is the only start gate. The source
starts at `max(bt.warmup_bars, indicators.ready_index)` (source line 193) and warns about
insufficient history; the Forex engine neither uses `IndicatorSet.ready_index` nor calls
`candles.validate()`, so the first simulated bars can read NaN ADX/ATR (emitted as silent
blockers or NaN sizing). Fix: with B3's single `IndicatorSet`, use
`max(warmup_bars, ind.ready_index)` per symbol and add `CandleArrays.validate()` problems
to `BacktestResult.warnings`.

### M17 вЂ” Per-bar full-history indicator rebuild is O(nВІ) (`backtest/engine.py:158-161`)

`build_indicators` (including `build_daily_context`'s daily aggregation) is recomputed on
`candles.slice(0, idx+1)` for **every bar of every symbol**: for the CLI's 8 pairs Г—
~1,700 post-warmup bars that is ~13,600 full-history builds. Even after correctness fixes,
`demo`/`backtest` will be effectively unusable. Fix: compute once per symbol (B3) and use
`context_at(idx)`; if streaming is required, only recompute the incremental tail.

## Minor

- **`metrics.py:102`** вЂ” report prints `Funding paid`; Forex has swap, and
  `Trade.funding` is never populated (M2), so the line is always `0.00`. Rename to `Swap
  paid` and populate from `swap_accrued`.
- **`metrics.py:245-253`** вЂ” `yearly_returns` attributes a trade's net PnL to its **entry**
  year and divides by the *starting* equity (no compounding). Copied from source, but
  misleading for multi-year FX portfolios; attribute on exit and divide by year-start
  equity.
- **`backtest/engine.py:362`** вЂ” every `Fill` gets `bar_index=0`; per-fill timing is lost.
  Pass the real index.
- **`backtest/engine.py:47, 40-41`** вЂ” `_DOW_BY_CODE`, `EntrySignal`, `Candle` and
  `Sequence` imports are unused (leftover crypto/refactor); lint noise that hides real
  drift.
- **`backtest/engine.py:385-391`** вЂ” `_open_risk` uses `trade.initial_stop` while the live
  runner uses the *current* stop (`runner.py:478`); after break-even the two disagree.
  Use the current ladder stop in both.
- **`backtest/engine.py:289-299`** вЂ” the entry fee is charged on
  `entry_price*lots*contract_size*taker_fee` (percentage-of-notional model). If a Forex
  `commission_per_lot` model is adopted (M9), this must change to
  `commission_per_lot*lots` per side.
- **`data/synthetic.py`** вЂ” see P3 for realism defects (continuous weekends, no gaps,
  shared seed across symbols, unrounded prices, non-bar-aligned `start_ts`).
- **`cli.py:213-215`** вЂ” `presets` prints a one-line pointer instead of the parameter
  tables the source command prints; make it read `Config().to_dict()`/presets.

## Port gaps vs source (offline scope)

**P3 вЂ” `data/synthetic.py` realism (cannot drive a Forex backtest meaningfully yet).**
`ts = np.arange(bars)*interval + start_ts` (line 30) is a continuous 24/7 calendar with no
Saturday/Sunday and no session breaks, so the PLAN's open question 3 ("backtester must
model Monday gaps on stops") cannot be exercised; `open_[1:] = close[:-1]` (lines 42-44)
means there is **never** a gap between bars; `seed=42` is the same default for every symbol
(line 26) and every draw scales by `base`, so EURUSD and USDJPY share the identical
normalised path вЂ” portfolio diversification, correlation and the 2-slot risk cap cannot be
tested; prices are never rounded to `digits` (fills call `round_price`, so the bar and the
fill disagree by design); `start_ts=1_600_000_000_000` is 2020-09-13 12:26:40 UTC, not on
a bar boundary, so generated timestamps do not align with broker bars or the 21:00 rollover
grid; the wick model `|N(0, 0.0008*base)|` adds a constant ~5-9 pips of high/low noise on
every bar with no volatility clustering. Fix: generate on a Monday-00:00 grid, skip
weekend bars (Fri 22:00 в†’ Sun 22:00 UTC), add gap opens after the weekend, per-symbol
digits rounding and seeds, and a volatility regime component.



| Area | Source (`D:\PROGRAMMING\NornWeave`) | Forex port | Consequence |
|---|---|---|---|
| CLI | 2,015 lines; commands: presets, notify, fetch, backtest, sweep, demo, parity, walk-forward, live, footprint, dash; `--set` overrides; `--json` artifacts | 256 lines; presets, fetch, backtest, live, demo | No parameter sweep/parity/walk-forward, no dashboard launcher, no machine-readable artifacts, no cache reuse, no `--set`. For Forex the highest-value lost items are **cached-data backtest**, **`dash`**, **JSON artifacts**, **`screen`**. |
| Dashboard | 948 lines, 9 routes, multi-state portfolio, chart markers, strategy state, health, graceful failures, no-write guarantee | 104 lines, 2 routes, single state file | Live/offline observability of a 2-slot FX portfolio (marks, margin, health, weekend staleness) is absent; see M14. |
| Screener | 625 lines: Bybit universe ranking by turnover, volatility profile/tradeability, CLI report, exclusion lists | 67 lines: fixed 8-major list + spread cap | The fixed list is the right Forex model, but the **spread filter is not wired in** (M12) and there is no volatility-spike guard to port the source's test_screener coverage. |
| Synthetic data | 112 lines: alternating regimes, overnight gaps (`open=prev_close*gap`), `synthetic_delta`, `synthetic_funding`, `synthetic_daily_series`, seed 7 | 58 lines: sine+noise, continuous 24/7 bars, no gaps, one shared seed for all symbols | Cannot exercise weekend/session gaps, Monday stop gaps, or portfolio diversification; see P3. |
| Metrics | 270 lines, same formulas | 261 lines, formulas essentially verbatim (only header/imports differ) | No Forex-specific metrics: per-symbol PnL, swap cost, pip-normalised expectancy, margin. The formulas also need the fixes in M10. |
| Tests | 25 files, ~250 test functions | 2 empty files, conftest only | Zero coverage of money math; see matrix. |
| Config | ships `configs/*.yaml` | `configs/` empty | Every CLI data command fails at config load; see M13. |
| Regression pinning | `test_regression.py` byte-stable pinned outputs | none | Money-math regressions (like B9) would be invisible. |

## Test porting matrix

Source tests live in `D:\PROGRAMMING\NornWeave\tests`; target directory is
`D:\PROGRAMMING\NornWeaveForex\tests`. "MT5 mocks" = fake `MT5Client` returning
`symbol_info`/`symbol_tick`/`copy_rates_from_pos` dicts (no rpyc connection);
"lots/pips" = expected values converted with `contract_size=100_000`,
`pip_value_per_lot=10`.

| Source test file | Tests | Forex target | What must change |
|---|---|---|---|
| `test_execution.py` | 30 | `tests/test_execution.py` | **Copy almost verbatim** (execution.py is byte-identical except the import package; verified diff). Only imports change; `qty=100.0` is direction-agnostic, so add lots-flavoured cases optionally. |
| `test_engine.py` | 36 | `tests/test_backtest_engine.py` | Port the assertions, not the fixture: `series` в†’ `{symbol: CandleArrays}` + `fx_config`/`instrument`; expected PnL in **$** (`pips*lots*$10`), not price units; keep `test_entry_is_at_the_next_bar_open...` (currently fails в†’ B7); add spread-on-exit, swap/triple-swap, multi-symbol marks; drop funding/footprint/delta tests; keep determinism, end-of-data, fee/slippage, metrics reconciliation. |
| `test_exits_sizing.py` | 22 | `tests/test_sizing.py` + `tests/test_exits.py` | Ladder-geometry tests **verbatim** (API unchanged). Sizing tests replaced: `risk_amount = stop_pips * pip_value_per_lot * lots`, min-lot floor via `accepts`, margin/leverage cap (M8); drop `size_mode`/`equity_pct`/`notional` tests (not implemented in Forex sizing). |
| `test_screener.py` | 13 | `tests/test_screener.py` | Replace Bybit volatility-event tests with fake-MT5 rows; assert spread в‰¤ cap tradeable, `inf`/error в†’ not tradeable, reason strings; keep the "unreadable symbol is skipped, not kept" case. |
| `test_state_schema.py` | 10 | `tests/test_state_schema.py` | **Close to verbatim** (state.py is a port of the same schema; `SCHEMA_VERSION`/migration table present). Verify Forex defaults (`swap_accrued`, `positions` lots). |
| `test_data.py` | 19 | `tests/test_data.py` | Port synthetic structural validity/determinism; drop CSV cache + funding tests; **add** weekend-gap/digit-rounding/bar-alignment tests for the Forex generator (P3). |
| `test_dashboard.py` | 70 | `tests/test_dashboard.py` | Only ~8 survive today (`/`, `/api/state`, `/api/trades`, 404 behaviour). The portfolio/mark/chart/health tests become the acceptance spec for the M14 rebuild (fake MT5 ticker + state dirs). |
| `test_cli.py` | 27 | `tests/test_cli.py` | Keep help/presets/demo/backtest/missing-config; drop sweep/parity/walk-forward/footprint; add `--paper` once B1 lands and the `dash` command once M14 lands. `workspace` fixture в†’ tmp `configs/forex.yaml` + npz cache. |
| `test_exit_price.py` | 6 | `tests/test_exit_price.py` | Adapt to lots: fill price/fee reconciliation through PaperBroker + state; needs B1/M9 (per-lot commission). |
| `test_tradeability.py` | 12 | `tests/test_sizing.py` (merged) | Replace Bybit min-notional cases with Forex min-lot/margin cases; overlaps the sizing matrix in M8. |
| `test_regression.py` | 4 | `tests/test_regression.py` (new) | Pin a Forex synthetic backtest JSON (trades, equity, metrics) byte-stable after B7-B9/M2; this is what makes B9 regressions impossible to reintroduce. |
| вЂ” (no source file) | вЂ” | `tests/test_instrument.py` (empty today) | **Author new**: `pip_size()` for 5/3 vs 4/2 digits, `round_lots` floors, `round_lots_to_meet` clears `min_lot`, `from_mt5` derives `pip_value_per_lot = tick_value*pip/tick_size`, `round_price`, `accepts(reduce_only=True)` below min lot. |
| вЂ” | вЂ” | `tests/conftest.py` | Add source-style session fixtures: `eurusd_series`/`usdjpy_series` via a Forex synthetic generator, `fx_config` with pip-based risk, `instrument` (already there), `fake_mt5_client`; keep taker_fee=0 until M9 lands. |

**Five highest-value tests to port first**

1. `tests/test_execution.py` (from `test_execution.py`) вЂ” 30 tests, **almost verbatim**;
   it is the shared bar-rule contract for backtest *and* live, and it catches B5/M6/M7.
2. `tests/test_backtest_engine.py` (from `test_engine.py`) вЂ” pins the Forex accounting
   contract: next-bar-open (B7), `$` PnL with `contract_size` (B9), realized cash (B8),
   spread on both sides (M4), swap/triple-swap (M2/M3), end-of-data fill (M7), multi-symbol
   marks (M1), truncation invariance, determinism.
3. `tests/test_sizing.py` (from `test_exits_sizing.py` + `test_tradeability.py`) вЂ” lot
   rounding, pip-value risk, min-lot floor, portfolio cap and the missing margin cap (M8).
4. `tests/test_screener.py` (from `test_screener.py`) вЂ” fake-MT5 spread gate; forces M12
   wiring and gives the live path a pre-trade spread guard.
5. `tests/test_state_schema.py` (from `test_state_schema.py`) вЂ” **almost verbatim**; locks
   the JSON contract the dashboard/live stack depends on before either is rebuilt.

Runner-up: a Forex smoke test for `python -m nornweave_forex.cli --help` + `demo` (catches
B1/B2/B3/B4/B5/B6 immediately), which should be added alongside #1.

## Recommended next steps

1. **Unbreak the import/CLI**: implement `PaperBroker` (lots + pip value + spread +
   per-lot commission) or lazy-import `live.runner` in `cli.py`; add a `--help`/`demo`
   smoke test. (B1)
2. **Unbreak the backtester API calls**: one `IndicatorSet` per symbol +
   `context_at`/`dow_codes` (B2, B3), correct `build_exit_ladder` and
   `break_even_should_arm` keywords (B4, B5), correct `compute_metrics` call (B6).
   After this, `demo` should return a `BacktestResult` (wrong numbers still вЂ” step 3).
3. **Fix the Forex money model** in one pass: `account_pnl()` with `contract_size` +
   quote conversion (B9), realized-cash credit and swap credit (B8, M2), exit-side spread
   (M4), correct triple-swap day mapping (M3), per-symbol marks (M1), end-of-data fill
   (M7), next-bar entry/exit timing (B7, M6), margin cap (M8).
4. **Convert config/conftest to Forex semantics**: pip/ATR stops, per-lot commission,
   `max_spread_points`, swap fields; ship `configs/forex.yaml`; update `conftest.py`
   fixtures (M9, M13).
5. **Port the five tests above before touching metrics**; then fix the equity curve to one
   point per timestamp and re-derive Sharpe/Sortino/annualisation and exposure (M10, M11).
6. **Restore the offline data path**: `fetch` в†’ npz cache в†’ `backtest --start/--end
   --json`, add a `screen` command wired to the spread filter (M12, M13).
7. **Rebuild the dashboard minimally but usefully**: `/api/overview` (mark-to-market
   equity, open risk, halted), `/api/health` (state age, last bar per symbol, feed
   staleness), `/api/chart/{symbol}` last N bars, plus a `dash` CLI command (M14).
8. **Synthetic data for Forex tests**: weekend gaps, bar-aligned timestamps, per-symbol
   digits/rounding, distinct seeds per symbol, occasional gap-open (P3).
9. **Regression pin**: after step 3, commit a pinned synthetic-data backtest JSON
   (`test_regression.py`) so money-math drift is caught.


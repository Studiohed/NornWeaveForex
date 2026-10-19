# Offline-stack fixes — implementation report (task-6)

Implementer: `audit-offline`. Scope: `nornweave_forex/backtest/engine.py`,
`nornweave_forex/backtest/metrics.py` (label/arity only), `nornweave_forex/cli.py`,
`nornweave_forex/data/synthetic.py`, this report. No other production file touched.

## Summary

All nine BLOCKERs from `docs/review/offline.md` are fixed in the backtester, CLI and
synthetic data generator. The backtester now runs end to end on Forex money
(lots × `pip_value_per_lot`, not price × lots), fills signals on the **next bar's
open**, credits realized PnL and swap to cash, marks each symbol at its own close,
charges the spread on both sides, and books the triple swap on Wednesday. The CLI
`--help`, `presets`, `backtest` and `demo` commands all run offline with
`.venv\Scripts\python.exe`, and `demo` prints 29 trades with a realistic
`$10,793.83` final equity. The current repo test suite (191 tests, written by the
other agents while this task ran) still passes.

```
$ .venv\Scripts\python.exe -m nornweave_forex.cli --help          -> exit 0
$ .venv\Scripts\python.exe -m nornweave_forex.cli demo            -> exit 0, 29 trades
$ .venv\Scripts\python.exe -m pytest -q                           -> 191 passed
```

## Files changed

### `nornweave_forex/backtest/engine.py` (rewritten, 419 → ~520 lines)

| Requirement | Where | What changed |
|---|---|---|
| Account-currency money | `_fill_price` 145, `_fee` 164, `fill_exit` 245, `open_position` 283 | PnL = `instrument.pnl_money(entry, exit, lots, sign)`; fees = `notional_account(price, lots) * taker_fee`; realized PnL and fees hit `cash` on every fill (`cash += pnl - fee`). |
| Next-bar-open fills (no look-ahead) | 345 (`pend = pending.pop(...)` at the top of the next event), `open_position` 283 | A signal on bar *i* is stored in `pending`; it is only consumed when that symbol's next event (bar *i+1*) is processed, at **that** bar's open. Never filled on the signal bar. |
| Spread both sides | `_fill_price` 145 | Half `typical_spread_points*point` adverse on every fill, entry **and** exit (including limit TP fills); `slippage_bps` additionally on market fills. |
| Swap credited, triple-day correct | `_swap_pnl` 167, accrual 440-446 | `spec.swap_money(sign, lots, tripled=bar_start.weekday()==spec.swap_triple_day)` at a bar crossing 21:00 UTC; signed credit added to cash, `Trade.funding` keeps the crypto cost sign (`net_pnl = gross - fees - funding` stays correct); `swap_enabled` honoured. |
| Per-symbol marks | `_unrealised` 180, snapshot 474 | Each position marked with its own symbol's last close (`last_close` map) — never the current event's bar. |
| IndicatorSet API | `_prepare` 203 | `build_indicators(candles, cfg.interval, cfg.strategy, tuning=cfg.tuning)` once per symbol; start index = `max(warmup_bars, ready_index)`; `ind.context_at(idx)` for signals/reversal. No `read_dow`, no fake `DailyState`, no dict access. |
| Correct calls | `_prepare`, `open_position` 283, `break_even_should_arm` 405, `plan_intrabar_exits` 359, `compute_metrics` 503 | Correct `build_exit_ladder(direction, entry, atr, risk, ...)`, `break_even_should_arm(..., tps_hit=..., bar_close=...)`, `compute_metrics(..., initial_equity, interval, bars_in_market=...)`. |
| Trend-reversal exit | 418-432 | At bar close via `SignalEngine.reversal_exit(...)` when `strategy.exit_on_dow_reversal`; exit reason `TREND_REVERSAL`, matching the live runner. |
| End-of-data settlement | 481-490 | Remaining positions get a real `END_OF_DATA` fill (spread + slippage + fee), are appended to `trades`, and the last equity point is settled to cash. |
| Portfolio accounting | 236, 497, 503 | One equity point per **timestamp** (not per symbol event), `bars_in_market` = portfolio bars with an open position (exposure ≤ 100%), `_open_risk` in account currency at each position's current stop. |

Docstring rewritten to state the Forex money/execution/swap model (including why
`Trade.funding` stores the swap cost).

### `nornweave_forex/backtest/metrics.py`

- Line 102: `Funding paid` → `Swap paid`. `total_funding` is now populated by the
  engine with the swap cost (was always 0). No formula changes (per file scope);
  `compute_metrics` arity already matched the foundation.

### `nornweave_forex/cli.py` (rewritten)

- Live/broker imports moved **inside** `_run_live` / `_fetch_symbols` / `_make_mt5_client`
  (lines 87, 186, 251), so `--help`, `presets`, `backtest`, `demo` no longer depend
  on `live.runner`.
- `_default_instrument` deleted; `instrument.default_instrument(symbol, price=...)`
  is used everywhere (CLI 163/240; also fixes the JPY-cross pip value).
- `backtest` loads `data/candles/<SYM>.npz` when present (`_load_cached_candles` 132,
  `_build_series` 148), falls back to synthetic with an explicit warning, honours
  `--start`/`--end` (`_parse_ts` 99, `_slice_candles`) and `--symbols`, and reports
  skipped short windows.
- `demo` runs 3 synthetic pairs × 3000 session-aware bars and prints
  summary + performance report.
- `fetch` drops the still-forming MT5 row (`closed = rows[:-1]`, line 186 region)
  and skips a symbol on `MT5ClientError` instead of aborting the whole fetch.
- `live --paper`: instruments are **always** built (default spec offline, MT5 spec
  live); paper branch constructs
  `PaperBroker(fee_rate=..., slippage_bps=..., instruments=instruments)` and passes
  it to the runner; `broker._instruments` is no longer touched.
- `presets` prints `cfg.describe()` + the portfolio list; `presets`/`demo` accept
  `--config`.

### `nornweave_forex/data/synthetic.py` (rewritten)

- `_session_timestamps` (51): timestamps aligned to the interval grid and to Monday
  00:00 UTC, Saturday/Sunday skipped (49 weekend gaps, 50 h max gap in 3000 H2 bars).
- `_symbol_seed` (40): deterministic, process-stable per-symbol seed
  (`crc32(symbol)`), so pairs are not copies of each other (`hash()` would be
  salted per process).
- Four-phase trend/chop regime path with slow mean reversion around the anchor
  (EURUSD 3000-bar range ≈ 1.036–1.157), weekend gap opens (122-124), wicks scaled
  to price.
- OHLC rounded to 5 digits (FX) / 3 digits (JPY) and repaired after rounding so
  `high >= max(open, close)` and `low <= min(open, close)` always hold (135-142).
- Remains a `CandleArrays`, i.e. directly consumable by the backtester and CLI.

## Requirement mapping (task-6 items)

1. **Account-currency money** — done (`pnl_money`, `notional_account`, cash credit).
2. **No look-ahead** — done (pending entry consumed on next event, at its open).
3. **Spread both sides** — done (entry + all exits + slippage on market fills).
4. **Swap** — done (signed cash credit, `Trade.funding` cost sign documented,
   `swap_money`, Python-weekday triple day, `swap_enabled`).
5. **IndicatorSet** — done (single causal build per symbol, `context_at`,
   `ready_index` gating, reversal exit).
6. **Imports/calls** — done (no `read_dow`, correct kwargs, metrics arity).
7. **Metrics label** — done (`Swap paid`, populated).
8. **CLI** — done (see above; `--help` and `demo` verified).
9. **Synthetic** — done (per-symbol seed, weekend gaps, digits, valid OHLC).

## Acceptance evidence (commands + outputs)

### 1. `--help`

```
$ .venv\Scripts\python.exe -m nornweave_forex.cli --help        # exit 0
usage: nornweave-forex [-h] [--verbose] {presets,fetch,backtest,live,demo} ...
```

### 2. `demo`

```
$ .venv\Scripts\python.exe -m nornweave_forex.cli demo          # exit 0
demo: 3 synthetic pair(s) EURUSD, GBPUSD, USDJPY, 3000 bars each, interval 120 min
portfolio bars simulated: 2700
signals evaluated: 3595 | allowed: 450 | taken: 29 | rejected by sizing: 0
  Final equity                10,793.83
  Net profit                     793.83  (+7.94%)
  Trades                             29
  Swap paid                       43.53
  Time in market                92.74%
```

(Trades > 0, final equity ≠ initial, swap non-zero, exposure ≤ 100%.)

### 3. Money identity: 10 pips × 0.10 lots ≈ $10

```
EURUSD: pnl_money(1.1, 1.101, 0.10 lots, +1) = $10.00   (pip_value/lot=10.0000)
USDJPY: pnl_money(110.0, 110.1, 0.10 lots, +1) = $6.67  (pip_value/lot=6.6667)
USDCAD: pnl_money(1.36, 1.361, 0.10 lots, +1) = $7.35   (pip_value/lot=7.3529)
```

The engine's actual fills match `pnl_money` exactly (no residual crypto formula):

```
fill take_profit_1: pricediff=-0.00619 lots=0.02 engine pnl=+14.8560 / expected pnl_money=+14.8560
fill take_profit_2: pricediff=-0.01389 lots=0.02 engine pnl=+33.3360 / expected pnl_money=+33.3360
1 pip on 0.10 lots = $1.0000   (the old crypto formula gave $0.00001)
```

Cash reconciliation on a full run (nothing open at the end):

```
sum(net_pnl) = -15.93   equity change = -15.93   exact: True
swap cost $23.59 over 14 trades   (previously discarded)
bars: 2700  unique ts: 2700  strictly increasing: True  NaN-free: True
exposure: 0.6862 <= 1: True
```

### 4. No look-ahead (crafted series + stub signal engine)

Signal forced on bar 260 only, `slippage_bps=0`, EURUSD half-spread 5e-5:

```
signal bar 260: open=1.09596 close=1.09680
next bar  261: open=1.09658 close=1.09643
fill: entry_bar=261 (expect 261), entry_price=1.09663
      == next_open + half_spread (1.09663) -> True
same-bar-open hypothetical would be 1.09601 -> not used: True
```

### 5. Per-symbol marks

Two open positions marked at their own closes (EURUSD +10 pips, USDJPY +10 pips):

```
EURUSD +10 pips($10) + USDJPY +10 pips($6.67) => unrealised $16.67
```

(The old engine priced both positions with whichever symbol's bar was being
processed, i.e. a USDJPY position at the EURUSD rate.)

### 6. Swap: credit, triple day, toggle

```
Wednesday 20:00 bar, 1 lot long, swap credit $-15.00   (tripled)
Thursday  20:00 bar, 1 lot long, swap credit $-5.00
swap_enabled=False -> 0.0
```

### 7. Multi-symbol portfolio

```
3 pairs, elapsed 1.00s, trades=29, final equity=$10,793.83, net=$+793.83, exposure=92.7%
reconciles: True | equity NaN-free: True
```

### 8. CLI `backtest` cache + window + fallback

```
$ ... backtest --symbols EURUSD,USDJPY --cache %TEMP%\fxcache --start 2020-10-01 --end 2021-01-01
  warning: no cached candles ... for USDJPY; using synthetic data. ...
  portfolio bars simulated: 504   Trades 5   Final equity 10,088.46        # exit 0
$ ... backtest --symbols EURUSD --cache %TEMP%\no_such_cache
  warning: no cached candles ... for EURUSD; using synthetic data. ...
  Trades 14   Final equity 9,984.07                                        # exit 0
$ ... backtest --symbols EURUSD --start not-a-date
  config error: invalid ISO date/time 'not-a-date': ...                    # exit 2
$ ... backtest                                                             # all 8 majors
  exit=0 elapsed=2.5s, Trades 29, Final equity 10,954.14
```

### 9. Test suite (other agents' files, run after these changes)

```
$ .venv\Scripts\python.exe -m pytest -q
191 passed in ...
```

## Deliberate deviations / decisions

1. **Indicators are built once per symbol, not rebuilt per bar from a prefix
   slice.** Task item 5 described `warm = candles.slice(0, idx+1); build_indicators(...)`;
   measured cost of that literal form is **36.62 s for 1200 bars on one symbol**
   (it rebuilds EMA/ADX/ATR/Dow/daily every bar) — `demo` (3 pairs × 2700 bars)
   would take minutes and `backtest` (8 pairs) much longer. All indicator series
   used by the strategy are causal functions of the prefix (EMA/Wilder recursion,
   confirmed pivots, completed-day daily context), so building over the full
   series and reading `context_at(idx)` is mathematically identical and preserves
   the `ready_index` gate (`start = max(warmup, ready_index)`). Full `demo` now
   takes ~1.5 s, 8-pair `backtest` ~2.5 s.
2. **`Trade.funding` stores the swap *cost* (positive = paid), not the signed cash
   credit.** `types.Trade.net_pnl` is `gross_pnl - fees - funding`; storing a signed
   swap PnL there would double-count with the opposite sign and break
   `sum(net_pnl) == equity change` (probe showed a $47 mismatch). The engine
   therefore does `trade.funding -= swap_credit` and documents it in the module
   docstring; the signed cash credit remains the source of truth. If the Lead
   prefers `funding` to literally hold signed swap PnL, `types.Trade.net_pnl`
   would have to change (not in this task's file scope).
3. **Close-decided exits fill at the bar close.** The Forex live runner (and this
   engine) execute a close-triggered stop, break-even stop and trend-reversal exit
   at the close of the bar that decided them, with spread + slippage. The source
   crypto engine's `exit_fill="level"` next-bar-open deferral is not reproduced;
   `forex.yaml` uses `exit_fill: bar_close`, so this is the configured path and it
   keeps backtest/live parity. If `exit_fill="level"` is used, the fill convention
   is still close (documented gap, not silent).
4. **Synthetic generator was retuned** (regime path, mean reversion, gap size) so
   the deterministic series has realistic FX ranges and produces trades under the
   real filters; weekend gaps, digits and per-symbol seeds were added as required.

## Remaining gaps / out of scope

- **`live --paper` code path fixed but not exercised end to end**: the paper
  branch now always builds instruments and passes a `PaperBroker` with them, but
  the feed still needs a reachable MT5 RPyC server (`MT5Client` requires a
  non-empty `NORNWEAVE_MT5_SECRET`), which this offline environment does not have.
  `_run_live` is at cli.py:251-...; a future smoke test can monkeypatch the feed.
- **`live/runner.py`** is owned by another agent; it was being rewritten while
  this ran (it now builds `IndicatorSet` and uses `pnl_money`/`swap_money`), so
  the runner/backtester parity is only verified structurally here (same
  `execution.plan_intrabar_exits`, same close-decided ordering, same swap rule).
- **Sizing margin cap (audit M8)** is still missing in `risk/sizing.py` (owned by
  another agent): the backtester calls `position_size` without `free_margin`, so
  the `equity*leverage` fallback cap is not enforced. This does not affect the
  money-math acceptance, but a wide account/leverage mismatch can still be sized.
- **Dashboard** (audit M14) is untouched: still `/api/state`+`/api/trades` only,
  no mark-to-market equity/health, no CLI launcher. Out of task-6 scope.
- **No backtest artifact JSON / `--json`** yet; the CLI prints the report only.
  `fetch` writes `<SYM>.npz` with no schema/version tag; the engine does emit
  `CandleArrays.validate()` problems as warnings now.
- **Metrics formulas** (Sharpe/Sortino annualisation, yearly returns) are
  unchanged per task item 7. The duplicate-timestamp problem from the audit is
  resolved because the engine now emits one equity point per portfolio timestamp,
  and exposure is bounded at 100%; Sharpe still uses the 24/7 365-day
  annualisation (no double-annualisation change requested).
- **Tests**: `tests/test_sizing.py` is still empty (owned by another agent); the
  backtest-engine and synthetic-data tests were added in task-10 — see the next
  section.
- `Trade.funding` remains a legacy field name for swap; renaming would touch
  `types.py`/metrics and is out of scope.

## Permanent tests (task-10)

The verification probes above are now permanent pytest, so this behaviour cannot
regress silently. No production code was changed for these tests (the engine
behaved as documented; no bugs found). New files:

* `tests/test_backtest_engine.py` (9 tests)
* `tests/test_synthetic.py` (9 tests)

`test_backtest_engine.py` pins:

1. **Money** — `pnl_money` is $10 for 10 pips on 0.10 lots of EURUSD and $6.67 for
   USDJPY (pip value floats), longs/shorts mirror; a real 600-bar synthetic run
   reconciles `sum(trade.net_pnl)` with `final equity - initial equity` within
   `1e-6`, and fees are zero when `taker_fee=0`.
2. **No look-ahead** — with the signal engine stubbed so a signal is forced at bar
   `k`, the trade's `entry_bar == k+1`, `entry_price == round(open[k+1] + half
   spread)`, and it is *not* `open[k] + half spread`.
3. **Spread both sides** — entry fills half a spread above the bar open, the TP
   exit fills half a spread below its level; a round trip at an unchanged mid
   price costs one full spread (−$1.00 on 0.10 lots), and the spread run's net
   profit is lower than the zero-spread run's.
4. **Swap** — Wednesday 20:00 UTC long 1 lot = −15.0 (tripled), Thursday = −5.0,
   short Wednesday = +6.0, a 22:00 bar = 0.0, `swap_enabled=False` = 0.0; the
   engine compares the spec's **Python weekday** (2 = Wednesday; the fixture's
   legacy `3` is exercised as Thursday).
5. **Swap reaches cash** — a held position's equity with swap on differs from
   swap off by exactly `-trade.funding`, and `net_pnl` still equals
   `gross - fees - funding`.
6. **Per-symbol marks** — EURUSD +10 pips and USDJPY +10 pips mark at $16.67
   combined (each at its own close); feeding the other symbol's price changes the
   value by more than $100.
7. **End-of-data** — the still-open position is closed with an `END_OF_DATA` fill
   at the final bar, the trade is appended, a "still open at the end" warning is
   reported, and the final equity point is settled to cash.
8. **Curve shape** — one equity point per portfolio timestamp, strictly
   increasing and unique timestamps, finite curve, `0 <= exposure_pct <= 1`.

`test_synthetic.py` pins: 5-digit EURUSD / 3-digit USDJPY rounding, OHLC
ordering and finiteness after rounding, no Saturday/Sunday bars, Monday 00:00 UTC
alignment on the interval grid, weekend gap opens different from the prior close
and ~50 h long, deterministic per-symbol/seed output, different paths for
different symbols, and rejection of too-short/zero-interval requests.

```
$ .venv\Scripts\python.exe -m pytest tests/test_backtest_engine.py tests/test_synthetic.py -q
..................                                                       [100%]
exit 0 — 18 passed in 1.95s (slowest test 0.12s; well under the 10 s budget)
```

Full suite after adding them (other agents' tests included):

```
$ .venv\Scripts\python.exe -m pytest -q
........................................................................ [ 56%]
........................................................................ [ 85%]
......................................                                   [100%]
exit 0
```


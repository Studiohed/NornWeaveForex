# Core strategy port fidelity — audit (`strategy/`, `indicators/`, `features.py`, `types.py`, `execution.py`, `config.py`)

Task: `task-1` · Reviewer: `audit-core` · Source of truth: `D:\PROGRAMMING\NornWeave` (read-only)
Port: `D:\PROGRAMMING\NornWeaveForex` · Consumers read end-to-end: `nornweave_forex/live/runner.py`, `nornweave_forex/backtest/engine.py`

> **Snapshot / concurrency note.** Audit window 2026-10-19 18:47–18:57. During it, other teammates edited
> `config.py`, `instrument.py`, `live/broker.py` and `live/state.py` (mtimes 18:54). All findings below were
> re-verified against the 18:56 tree; the *scope* file hashes in the table below are current. Where an issue was
> fixed concurrently, that is stated explicitly (`config.py` fees/symbol/category, `PaperBroker`, `pnl_money`/
> `swap_money` helpers). `backtest/engine.py` (mtime 18:20), `live/runner.py` (18:20) and `cli.py` (15:20) were
> **not** touched, so every consumer-side finding is against the current tree.

---

## Summary

- **The shared "unchanged" claim is true, with one nuance.** `types.py`, `features.py`, `indicators/core.py`, `indicators/swings.py` are **byte-identical** (same SHA-256). All 8 `strategy/*.py` files, `indicators/__init__.py` and `execution.py` are identical **after normalising only the package name** (`nornweave_forex` → `nornweave`); there is **no logic drift** in any of them. `config.py` is the only file with real drift, and it is still being edited concurrently (see table).
- **The port is currently non-runnable end-to-end.** Both consumers were written against APIs that do not exist in the (correctly copied) shared modules: `build_indicators(candles, strategy)` (missing `interval`), dict-style `indicators["ema"]` vs `IndicatorSet`, `read_dow` (function does not exist), `DailyState(trend=/zone=/depth=)` (real ctor is `(ts, dow, ema_slope, leg)`), `build_exit_ladder(entry_price=/risk_params=)` (real params are `entry`/`risk`), `break_even_should_arm(close=/be_arm_on=, no tps_hit)` (real: `bar_close`/`arm_on`/required `tps_hit`), `compute_metrics(trades, eq, ts)` (real needs `initial_equity` and `interval`). Each was reproduced at runtime (see Blockers 1–8). `live/runner.py` and `cli.py` still cannot be imported because of the missing `read_dow` (the missing `PaperBroker` was added by a concurrent edit at 18:54).
- **After patching only those API mismatches**, the backtester runs but its accounting is wrong: realised PnL/fees/swap never reach cash (`equity = initial_equity + unrealised` only), PnL omits `contract_size` (a 100-pip, 0.01-lot EURUSD move booked **$0.0001 instead of $10**, a 100 000× error), and the equity mark values every open position at the *current symbol's* close. The entry also fills at the **same bar's open** as the signal close (look-ahead).
- **The THURISAZ daily filter is stubbed out in both consumers** (hard-coded `DowTrend.NEUTRAL`/`Zone.UNKNOWN`). With the default `daily_filter_enabled=True` + `daily_missing_policy="block"`, `_daily_gate` returns `daily_leg_missing` for every bar: after the ctor is fixed, the default config will allow **zero entries forever** (in both backtest and live).
- **`config.py` is still mostly the crypto config with three Forex additions** (`RiskParams.portfolio_risk_pct`, `PortfolioParams`, `Config.portfolio`, plus a `symbols` alias property). A concurrent edit fixed `symbol`→EURUSD, `category`→forex, `taker_fee`/`maker_fee`→0.0 and renamed `funding_enabled`→`swap_enabled`, but the risk defaults remain FX-hostile (`stop_loss_pct=-5%`, `be_trigger_pct=9%`, `leverage=5.0`, meme/midcap presets) and ~11 fields are documented/validated but never read anywhere in the port (`size_mode`, `notional_pct`, `equity_pct`, `min_notional`, `tp_anchor`, `tuning.*`, `category`, `symbol`, `data_file`, `maker_fee`, `swap_enabled`).
- `Config.validate()` has real gaps: `category` is no longer validated at all, symbol/portfolio consistency is unchecked (`symbol="XAUUSD"` not in `portfolio.symbols` passes), duplicate symbols pass, `max_positions > len(symbols)` passes, and `"W"`/`"M"` intervals pass validation but abort later in `aggregate_daily` (`strategy/daily.py:79-84`).

---

## Verification of "unchanged" files (method + hashes)

Method: `Get-FileHash -Algorithm SHA256` on both trees, then `difflib.unified_diff` after replacing `nornweave_forex` → `nornweave` in the port file (the only legitimate difference).

| Port file | Source file | SHA-256 (first 16) | Verdict |
|---|---|---|---|
| `nornweave_forex/types.py` | `nornweave/types.py` | `732466A2F53737D7` | **identical, byte-for-byte** |
| `nornweave_forex/features.py` | `nornweave/features.py` | `A2019927C185FABF` | **identical, byte-for-byte** |
| `nornweave_forex/indicators/core.py` | `nornweave/indicators/core.py` | `432DA543E476A816` | **identical, byte-for-byte** |
| `nornweave_forex/indicators/swings.py` | `nornweave/indicators/swings.py` | `E2C53ECF7C046F81` | **identical, byte-for-byte** |
| `nornweave_forex/execution.py` | `nornweave/execution.py` | `2FF54203069AE046` / src `DF2B8A758A493478` | **identical after import rename** (diff empty) |
| `nornweave_forex/indicators/__init__.py` | `nornweave/indicators/__init__.py` | `75DFC0F8828035C4` | **identical after import rename** |
| `nornweave_forex/strategy/__init__.py` | `nornweave/strategy/__init__.py` | `C553A803BBF7A71F` | **identical after import rename** |
| `nornweave_forex/strategy/context.py` | `nornweave/strategy/context.py` | `E988E0F9C2206BE9` | **identical after import rename** |
| `nornweave_forex/strategy/daily.py` | `nornweave/strategy/daily.py` | `FA4F21EFFBB2C2C5` | **identical after import rename** |
| `nornweave_forex/strategy/dow.py` | `nornweave/strategy/dow.py` | `3CA20F542C6BED0F` | **identical after import rename** |
| `nornweave_forex/strategy/engine.py` | `nornweave/strategy/engine.py` | `1E30E85B21EA9EE9` | **identical after import rename** |
| `nornweave_forex/strategy/exits.py` | `nornweave/strategy/exits.py` | `C532F5B97CC35708` | **identical after import rename** |
| `nornweave_forex/strategy/fibonacci.py` | `nornweave/strategy/fibonacci.py` | `6D41C4C8524F1E48` | **identical after import rename** |
| `nornweave_forex/strategy/tuning.py` | `nornweave/strategy/tuning.py` | `05A1CAE769B8D726` | **identical after import rename** |
| `nornweave_forex/config.py` | `nornweave/config.py` | `81E02768BAD45C44…` (775 lines) | **DIFFERS — see below** |

`config.py` diff (the only real production drift in scope), re-verified at 18:56 (source → port):

```diff
+ RiskParams.portfolio_risk_pct: float = 0.05                  # port:321
+ class PortfolioParams(symbols=(8 majors), max_positions=2)   # port:415-445
+ Config.portfolio: PortfolioParams                            # port:592,621,641
+ Config.symbols property  -> tuple(portfolio.symbols)         # port:597-599
  Config.symbol: "DOGEUSDT" -> "EURUSD"                        # port:583
  Config.category: "linear" -> "forex"                         # port:585
- BacktestParams.taker_fee: 0.00055 -> 0.0                     # port:454 (spread charged separately)
- BacktestParams.maker_fee: 0.00036 -> 0.0, comment rewritten  # port:455
- BacktestParams.funding_enabled -> swap_enabled               # port:462
- if self.category not in ("linear","inverse","spot"):        # source Config.validate
-     raise ConfigError(...)                                 # port:633-634 (deleted, not replaced)
```

Everything else in `config.py` is still the crypto file: `_ASSET_PRESETS` (`port:680-758`, classes `meme`/`major`/`midcap`), `stop_loss_pct=-0.05`, `be_trigger_pct=0.09`, `leverage=5.0`, `size_mode`, `tp_anchor`, tuning block, etc. No spread/commission/swap knobs were added to the config (they live in `instrument.py`).

---

## Blockers

### B1. `build_indicators()` is called with the wrong arity — crashes on the first bar of every run
`nornweave_forex/backtest/engine.py:160`, `nornweave_forex/live/runner.py:202,296,450`

```python
indicators[symbol] = build_indicators(warm, cfg.strategy)          # engine.py:160
self._indicators[symbol] = build_indicators(candles, self.config.strategy)  # runner.py:202
```
Real signature (`strategy/context.py:93-99`): `build_indicators(candles, interval, strategy, delta=None, tuning=None)`.
**Evidence:** `TypeError: build_indicators() missing 1 required positional argument: 'strategy'`.
**Fix:** pass the interval as the source does (`nornweave/backtest/engine.py:186-188`): `build_indicators(candles, cfg.interval, cfg.strategy, tuning=cfg.tuning)` — once per symbol/series, not per bar.

### B2. Consumers index the indicator result as a `dict`; it is an `IndicatorSet` dataclass
`nornweave_forex/backtest/engine.py:161,400-410`, `nornweave_forex/live/runner.py:202,550-553`

```python
ind = indicators[symbol]; ... ema = indicators["ema"]; indicators["adx"][idx]
```
`build_indicators` returns `IndicatorSet` (`strategy/context.py:43-90`) exposing `.ema/.adx/.atr/.dow_codes/.daily` and, importantly, `.context_at(index)`.
**Evidence:** `TypeError: 'IndicatorSet' object is not subscriptable`.
**Fix:** use `build_indicators(...).context_at(idx)` (source pattern, `nornweave/backtest/engine.py:519`) and delete the hand-rolled `_build_context` copies; keep one shared function so backtest and live cannot drift.

### B3. `read_dow()` does not exist anywhere in the port
`nornweave_forex/live/runner.py:50,549`, `nornweave_forex/backtest/engine.py:396,399`

```python
from nornweave_forex.strategy.dow import read_dow     # runner.py:50 (module import → CLI import fails)
dow = read_dow(candles, idx, self.config.strategy)    # engine.py:399, runner.py:549
```
`strategy/dow.py` exports only `DowTracker`, `BreakoutDow`, `dow_trend_series`, `PivotPoint`. **Evidence:** `ImportError: cannot import name 'read_dow' from 'nornweave_forex.strategy.dow'`; because `runner.py:50` imports it at module scope, **both `nornweave_forex.live.runner` and `nornweave_forex.cli` currently fail to import** (this is the live path's current first failure — the previously missing `PaperBroker` was added concurrently at 18:54).
**Fix:** use `IndicatorSet.dow_codes[index]` via `DOW_BY_CODE` (`strategy/context.py:30,80`) or add `read_dow` to `dow.py` (but then it is drift from source and must be documented).

### B4. `DailyState` is constructed with keyword names that do not exist
`nornweave_forex/backtest/engine.py:402`, `nornweave_forex/live/runner.py:554-559`

```python
daily = DailyState(trend=DowTrend.NEUTRAL, zone=Zone.UNKNOWN, depth=None, leg=None)   # engine.py:402
```
Real type (`strategy/daily.py:25-37`): `DailyState(ts: int, dow: DowTrend, ema_slope: int, leg: FibLeg | None)`; there is no `zone`/`depth`/`trend` field (`trend` is a read-only property aliasing `dow`).
**Evidence:** `TypeError: DailyState.__init__() got an unexpected keyword argument 'trend'`.
**Fix:** `daily = IndicatorSet.context_at(idx).daily` (or `state_for_bar`) after building the daily context (`build_indicators` already does this).

### B5. Daily filter is silently stubbed out in both consumers → default config never trades
`nornweave_forex/backtest/engine.py:402`, `nornweave_forex/live/runner.py:554-559`, gate logic `nornweave_forex/strategy/engine.py:154-189`

Even if B4's ctor is fixed by keeping a neutral state, `daily = NEUTRAL/UNKNOWN` flows into `_daily_gate`: `resolve_daily_trend` → `NEUTRAL`, `daily_neutral_policy="block"` falls through, `leg is None` → `_Gate(ok=daily_missing_policy=="allow")` with `daily_missing_policy="block"` ⇒ **every long and short is blocked with `daily_leg_missing`**. The default config therefore yields 0 entries in both backtest and live; and if someone flips the policy to `allow`, the entire THURISAZ daily-Fibonacci overlay (a core filter, default ON) is silently bypassed.
**Fix:** build the real context — `build_indicators(..., interval ...)` already produces `DailyContext` (`strategy/daily.py:124-198`) with correct causal `bar_to_state` mapping; pass `ind.context_at(idx).daily`. Never hand-construct a placeholder.

### B6. `build_exit_ladder()` is called with parameter names that do not exist
`nornweave_forex/backtest/engine.py:269-278`, `nornweave_forex/live/runner.py:515-523`

```python
ladder = build_exit_ladder(entry_price=entry_price, atr=pend.atr, direction=pend.direction,
                           risk_params=cfg.risk, tp1_override=pend.tp1_override,
                           atr_factor=None, stop_loss_pct=None)
```
Real signature (`strategy/exits.py:60-70`): `build_exit_ladder(direction, entry, atr, risk, tp1_override=None, break_even_stop=None, tp_anchor_price=None, atr_factor=None, stop_loss_pct=None)`.
**Evidence:** `TypeError: build_exit_ladder() got an unexpected keyword argument 'entry_price'` (raised lazily from `engine.py:269`, so it only appears once an entry is attempted).
**Fix:** `build_exit_ladder(direction=..., entry=entry_price, atr=..., risk=cfg.risk, tp1_override=..., atr_factor=..., stop_loss_pct=...)`; also pass `tp_anchor_price` if `risk.tp_anchor == "signal_close"` (otherwise `tp_anchor` is dead config, see M7).

### B7. `break_even_should_arm()` is called with non-existent kwargs and without its required `tps_hit`
`nornweave_forex/backtest/engine.py:217-224`, `nornweave_forex/live/runner.py:334-341`

```python
break_even_should_arm(be_mode=..., direction=..., bar_high=..., bar_low=...,
                      be_trigger_price=..., close=bar.close, be_arm_on=...)   # engine.py:217-224
```
Real signature (`execution.py:214-224`): `break_even_should_arm(*, be_mode, direction, bar_high, bar_low, be_trigger_price, tps_hit, arm_on="extreme", bar_close=None)`.
**Evidence:** `TypeError: break_even_should_arm() got an unexpected keyword argument 'close'` (plus missing required `tps_hit`). `be_mode="after_tp1"` can never work.
**Fix:** pass `tps_hit=tuple(pos.tps_hit)`, `arm_on=cfg.risk.be_arm_on`, `bar_close=bar.close`.

### B8. `compute_metrics()` arity is wrong — every backtest run crashes at the end
`nornweave_forex/backtest/engine.py:334`

```python
metrics = compute_metrics(trades, eq, ts)
```
Real signature (`backtest/metrics.py`): `compute_metrics(trades, equity_curve, timestamps, initial_equity, interval, bars_in_market=0)`.
**Evidence:** `TypeError: compute_metrics() missing 2 required positional arguments: 'initial_equity' and 'interval'`.
**Fix:** `compute_metrics(trades, eq, ts, cfg.backtest.initial_equity, cfg.interval, bars_in_market)`.

### B9. (resolved during audit — status only)
`PaperBroker` was missing from `live/broker.py` at 18:47 (`ImportError` for `live.runner`/`cli`); a concurrent edit added it at 18:54. **The live import now fails later, at B3 (`read_dow`).** No action needed beyond B3.

### B10. Entry fills at the **same bar's open** whose close produced the signal — one full bar of look-ahead
`nornweave_forex/backtest/engine.py:233-267`, convention in `nornweave_forex/types.py:82`

```python
# signal decided on the just-closed bar ...
signal = self.engine.evaluate(ctx)           # ctx.close == candles.close[idx]
...
if symbol in pending:                        # ... filled in the SAME loop iteration
    entry_price = bar.open + spread_cost     # bar == candles[idx]: its open predates that close
```
`Candle.ts` is documented as the bar's **open** time (`types.py:82`), so `bar_ts` / `bar.open` precede the signal close by one interval. The source engine is explicit about the correct ordering: `_PendingEntry` "waiting to be filled at the next bar's open" (`nornweave/backtest/engine.py:67-68`) and fills it at the top of the *next* loop iteration (`source:457-470`). In the port the pending queue is created and drained inside one iteration, so the entry price is known before the signal.
**Fix:** evaluate the signal at bar `idx`, queue it, and fill at `idx + 1`'s open (or restructure to iterate bars once and carry `pending` across iterations, exactly as source). Add a regression test that entry_ts > signal bar close time.

### B11. Realised PnL, exit fees and swap never reach cash — the equity curve ignores every closed trade
`nornweave_forex/backtest/engine.py:351-383` (+ call sites `189,210`, equity `316-321`)

```python
def _fill_exit(self, pos, plan, bar_ts, cash: float) -> None:   # cash passed by value
    ...
    pos.trade.gross_pnl += pnl                                   # never `cash += pnl`
    pos.trade.fees += fee                                        # never `cash -= fee`
```
`cash` is a float argument (immutable), so entry fees are deducted in `run()` (`engine.py:309`) but exit PnL/fees are not, and `pos.swap_accrued` (`engine.py:230`) is never subtracted from anything (it is not even copied into `Trade`, which still has the crypto `Trade.funding` field). Equity is `cash + unrealised` (`engine.py:316-321`), i.e. `initial_equity ± open mark` — all realised results are invisible to `metrics`. Source twin: `charge_fee` does `realized -= fee` and `realized += gross` (`nornweave/backtest/engine.py:239-243,264`).
**Evidence (patched-API run):** `initial equity 10000`, curve min/max/last `9999.4 / 9999.4 / 9999.4`, `sum net = -1.14` → the closed trade's result never appears.
**Fix:** make `_fill_exit` update a running realised cash (`self._realized += pnl - fee`), book swap at each rollover via `instrument.swap_money` and deduct it, and compute equity from that running cash; add a test asserting `final_equity == initial_equity + Σ net_pnl`. Note the live side already books money through `instrument.pnl_money`/`swap_money` (`live/state.py:141,342`), so today backtest and live are numerically inconsistent by construction.

### B12. PnL is computed in lots × price, missing `contract_size` — 100 000× understatement
`nornweave_forex/backtest/engine.py:360` (and end-of-data close `329`)

```python
pnl = (price - pos.trade.entry_price) * portion * pos.trade.direction.sign   # engine.py:360
fee = price * portion * instrument.contract_size * cfg.backtest.taker_fee    # engine.py:359 (fee DOES scale)
...
pos.trade.gross_pnl += (last_close - pos.trade.entry_price) * pos.qty * sign # engine.py:329
```
For EURUSD 0.01 lots a 100-pip move is `$10`, but this books `0.0100 * 0.01 = $0.0001`; the equity mark (`engine.py:319`) has the same defect. Fees and PnL are therefore in different units, so net PnL is dominated by fees.
**Evidence (patched-API run):** entry `1.0869`, lots `0.01`, closed trade `gross_pnl = 0.0001`, `fees = 1.1417`.
**Fix:** `pnl = instrument.pnl_money(entry, price, portion, direction.sign)` — the helper was added to `instrument.py:236-250` during the audit and is already what `live/state.py:141,342` uses; the backtester must use it too (this is also a live/backtest divergence, not just a unit bug). Same for the end-of-data close (`engine.py:329`) and the unrealised mark (`engine.py:319`).

### B13. Equity mark values **every** open position at the current symbol's close
`nornweave_forex/backtest/engine.py:315-321`

```python
for sym2, pos2 in positions.items():
    inst2 = self.instruments[sym2]                     # unused
    unrealised += (bar.close - pos2.trade.entry_price) * pos2.qty * ...   # bar is the event symbol's bar
```
With up to `max_positions=2` (default) or more, EURUSD's close is used to mark a USDJPY position. `inst2` is fetched then ignored — a clear sign the per-symbol close was intended. Also `timestamps/equity_curve` get one entry **per (bar, symbol) event**, not per timestamp (`engine.py:151,320-321`; timeline built at `122-129`), so the curve/metrics interleave events and duplicate timestamps.
**Fix:** mark each position with `self.series[sym2].close[last_index_at_or_before(bar_ts)]` and build one equity point per unique timestamp.

---

## Major

### M1. Remaining defaults are crypto values and are unsafe for FX
`nornweave_forex/config.py:263,265,303,680-758`

```python
stop_loss_pct: float = -0.05        # config.py:263  -> ~550 pips on EURUSD, effectively no stop
be_trigger_pct: float = 0.09        # config.py:265  -> 9% never reached intraday
leverage: float = 5.0               # config.py:303
_ASSET_PRESETS = {"meme","major","midcap"}  # config.py:680 -> crypto asset classes, crypto ATR/stop ranges
```
A concurrent edit already fixed the worst offenders (`symbol`→EURUSD, `category`→forex, `taker_fee`/`maker_fee`→0.0, `funding_enabled`→`swap_enabled`), so this finding is now about the **risk defaults and presets**: a 5% stop with 1% risk yields ~0.01 lots on a $10k account (see M2), `be_trigger_pct=9%` never arms, and `recommended_preset`/`preset_table` still emit meme/midcap parameter ranges. `configs/` is empty, so `Config()` defaults are what the code effectively ships with.
**Fix:** change defaults to FX units — `stop_loss_pct` from ATR/pips (e.g. `-0.002` for ~20 pips), `be_trigger_pct` ~`0.001`, `be_buffer_pct=0.0005`; replace presets with FX presets (majors/crosses, M5–H4) or delete `recommended_preset`/`preset_table`; delete the now-dead `maker_fee`/`swap_enabled` or wire them.

### M2. TP ladder takes 30%·30%·30% (90%) of a **0.01-lot** minimum position → sub-`min_lot` partial closes
`nornweave_forex/config.py:249-250`, `strategy/exits.py:159-185`, `risk/sizing.py:87-124`

With defaults, `position_size` floors to `min_lot=0.01` (`instrument.py:158,110`); each TP then closes `0.01*0.30 = 0.003` lots, which is below `instrument.min_lot=0.01` — `InstrumentSpec.accepts(..., reduce_only=True)` (`instrument.py:216-229`) and an MT5 broker reject it, and the 0.001-lot residual cannot be closed either. Source crypto had no lot grid, so this is a Forex-specific incompatibility.
**Evidence (patched-API run):** `lots 0.01`, exit reasons `['take_profit_1','take_profit_2','take_profit_3']`, remaining `qty=0.001` — the position never flattened and stayed open to end-of-data.
**Fix:** compute exit quantities on the lot grid (`instrument.round_lots`) and merge/clamp rungs that fall below `min_lot`, or reject entries whose size cannot host three rungs; document the minimum viable size. Add a Forex test with `lots=0.01`.

### M3. `size_mode` / `notional_pct` / `equity_pct` / `min_notional` are validated but never read
`nornweave_forex/config.py:296-314,399-408`; `risk/sizing.py:44-136` (only `risk_per_trade_pct`/`max_risk_per_trade_pct`/`leverage` used)

`grep` across `nornweave_forex/` outside `config.py`: **0 hits** for `size_mode`, `notional_pct`, `equity_pct`, `min_notional`. A user switching `size_mode: notional` gets risk-based sizing with no warning; `min_notional` (an exchange-notional crypto concept) is meaningless for FX and never enforced (only `min_lot` is).
**Fix:** either implement the modes in `sizing.py`, or remove the fields and their validation so the config cannot lie. Same for `leverage`: it is only used to *report* `margin_required` (`sizing.py:97-98`) and is never enforced unless `free_margin` is passed — consumers never pass it (`engine.py:280-287`, `runner.py:481-488`).

### M4. `tp_anchor` is never implemented — FEHU's signal-close anchoring is silently ignored
`nornweave_forex/config.py:255` (default `"entry"`, docs at `251-254` promise `"signal_close"` behaviour); consumers never pass `tp_anchor_price` (`engine.py:269-278`, `runner.py:515-523`)

`build_exit_ladder` supports `tp_anchor_price` (`exits.py:112`) but nobody supplies it, so the documented option cannot take effect. Given the look-ahead fix (B10) the entry/close gap becomes real, making this material.
**Fix:** pass `tp_anchor_price=pend.signal_close` when `cfg.risk.tp_anchor == "signal_close"` (the field is already carried in `_PendingEntry`, `engine.py:67`).

### M5. Auto Calibration (`tuning.*`) is entirely dead — `tuning=` is never passed to `build_indicators`
`nornweave_forex/config.py:497-573`, `strategy/context.py:132-136`, consumers `engine.py:160`, `runner.py:202,296,450`

Source passes `tuning=cfg.tuning` (`nornweave/backtest/engine.py:186-188`); the port never does, so `TuningParams.enabled=True` changes nothing (no ADX threshold, no ATR factor, no dynamic stop), and `SignalContext.adx_threshold` stays `None` (`strategy/engine.py:116-119`).
**Fix:** wire `tuning=cfg.tuning` into the single `build_indicators` call and consume `ind.tuning.at(idx)` when building the ladder (source `engine.py:536-546`).

### M6. `ema_slope_mode="rising"` is silently downgraded to `"compare"`
`nornweave_forex/config.py:96`, `nornweave_forex/strategy/engine.py:132-139`; consumers never set `ema_rise_streak`/`ema_fall_streak` (`engine.py:403-413`, `runner.py:560-570`)

`build_indicators` only computes the streaks when the mode is `"rising"` (`context.py:119-124`), and `context_at` populates them; the hand-rolled contexts pass `None`, so `_slope` falls back to the plain comparison. A user selecting the Pine `ta.rising` semantics gets the weaker test with no log line.
**Fix:** use `IndicatorSet.context_at` (B2) — the streaks come for free — and change the fallback to log/warn instead of silently substituting.

### M7. `exit_order`, `reversal_rule`, `exit_on_dow_neutral` are accepted by the config but ignored/wrong
- `exit_order="exits_first"`: backtester never branches on it (grep: only `config.py` and the live guard `runner.py:169-173`, which *rejects* it). Config documents FEHU's order; the backtest silently uses `tp_first` forever → results not comparable to Pine.
- `reversal_rule` / `exit_on_dow_neutral`: `SignalEngine.reversal_exit` implements all three rules (`strategy/engine.py:276-311`), but the live runner re-implements only "Dow opposite" (`runner.py:354-360`) and never calls `reversal_exit`. Setting `reversal_rule: ema_cross` or `exit_on_dow_neutral: true` changes nothing live.
**Fix:** call `self.engine.reversal_exit(pos.direction, ctx.dow, close=bar.close, ema=ctx.ema)` in the runner, and implement `exit_order` in the backtester (or reject it in `Config.validate`, like the runner does).

### M8. Missing spread cost on every exit (docstring claims otherwise)
`nornweave_forex/backtest/engine.py:9-10,259-267,351-359`

Docstring: "Every fill pays half the typical spread per side." Only the entry adds `spread_cost` (`engine.py:259`); `_fill_exit` applies `slippage_bps` only when `plan.slippage`, and limit TP fills pay no spread at all — a systematic optimism on every trade (2×½-spread ≈ 1 pip round trip on majors). Also `spread` has no config knob: it comes from `InstrumentSpec.typical_spread_points`.
**Fix:** apply half-spread (against the position) in `_fill_exit`/`plan` pricing for all exit fills; consider bid/ask price series instead of a single mid OHLC.

### M9. `Config.validate()` gaps (invalid configs accepted)
`nornweave_forex/config.py:633-639` and the sections it delegates to

Verified by running `Config(**kw).validate()` (current tree, after the 18:54 edits):

| Input | Result | Expected |
|---|---|---|
| `category="FUTURES"` | passes | error — category validation was deleted and not replaced (`config.py:633-634`); field is still read by `describe()` and is otherwise dead |
| `symbol="XAUUSD"` (not in `portfolio.symbols`) | passes | error/warning — `symbol` is now `"EURUSD"` but is not cross-checked against `portfolio.symbols` |
| `portfolio=PortfolioParams(symbols=("EURUSD",)*3)` | passes | error — duplicates |
| `portfolio=PortfolioParams(symbols=("EURUSD",), max_positions=5)` | passes | error — cap cannot fill |
| `interval="W"` / `"M"` | passes | error — `aggregate_daily` raises later (`daily.py:79-84`); `Config.validate` must reject intervals > 1 day, or skip the daily context |
| `interval="1"` | passes | OK, but `daily.py` expects 1440 bars/day — document the data requirement |

`BacktestParams.__post_init__` also forgets `maker_fee` and `swap_enabled` (no coercion, `config.py:467-472`) and `validate()` never checks either (`481-491`); `StrategyParams.daily_ema_period` accepts 1; `RiskParams` validates `portfolio_risk_pct >= risk_per_trade_pct` even when `size_mode != "risk"`.
**Fix:** restore category validation (or remove the field), validate `portfolio.symbols` uniqueness/`max_positions`, cross-check `Config.symbol` membership, constrain intervals to the daily-aggregatable set, coerce/validate `maker_fee`/`swap_enabled`.

### M10. `portfolio_risk_pct` default and semantics make the cap inert
`nornweave_forex/config.py:316-321,358-364`; enforced in `risk/sizing.py:79-85`; `max_positions=2` (`config.py:424`)

Default cap `5%` with `risk_per_trade_pct=1%` and only 2 concurrent positions means the cap can never bind (max 2%); the docstring says "Defaults to `risk_per_trade_pct * 5` so two positions … fit comfortably" — but it is a constant, not derived, and it does not scale when `risk_per_trade_pct` is raised above 2.5% (then the validation rejects the config instead of adapting). It is also not persisted/reset anywhere.
**Fix:** derive the default in `__post_init__` (`max(risk_per_trade_pct * 2, …)`) or validate `portfolio_risk_pct >= risk_per_trade_pct * max_positions`; document that `size_mode` only affects the notional model.

---

## Minor

### m1. Swap is modelled but never accounted; rollover detection has an off-by-one at 21:00
`nornweave_forex/backtest/engine.py:230,373-383`, duplicated verbatim in `nornweave_forex/live/runner.py:572-593`

```python
rollover = bar_start.replace(hour=21, minute=0, second=0, microsecond=0)
if bar_start < rollover <= bar_end:        # a bar STARTING at 21:00 never accrues
```
Swap accumulates into `pos.swap_accrued` (`engine.py:230`) which is never read again (B11). The rollover hour is hard-coded twice; brokers differ (21:00/22:00 UTC, DST). `Trade.funding` (crypto field, `types.py:226`) is never populated.
**Fix:** use `bar_start <= rollover < bar_end`, single helper in `instrument.py`/`execution.py`, add swap to realised PnL and to `Trade` (rename `funding`→`swap` in the port or document it).

### m2. Swap units: the engine bypasses the new `swap_money` conversion
`nornweave_forex/backtest/engine.py:379-382`, `nornweave_forex/instrument.py:264-274`

```python
# engine.py:379-382
swap = instrument.swap_long if pos.trade.direction is Direction.LONG else instrument.swap_short
...
return swap * pos.qty
```
A concurrent edit added `InstrumentSpec.swap_money(sign=, lots=, tripled=)` (`instrument.py:264-274`), which converts `SYMBOL_SWAP_MODE_POINTS` quotes via `point * pip_value / pip` when `swap_in_points` is set, and `_mt5_weekday_to_python` (`instrument.py:57-71`) normalises the triple-swap day. The backtester and runner only multiply the raw number by lots, so any points-mode broker books swap in the wrong unit, and the `swap_triple_day` weekday convention is used raw at `engine.py:380` / `runner.py:590` (source `_mt5_weekday_to_python` already returns Python weekday, so that part is consistent — the unit conversion is not).
**Fix:** call `instrument.swap_money(sign=pos.trade.direction.sign, lots=pos.qty, tripled=...)`; better still, move rollover detection into `instrument.py`/`execution.py` so live and backtest share it (the two copies at `engine.py:373-383` and `runner.py:572-593` are already duplicated).

### m3. Backtest warmup ignores the indicator's `ready_index`
`nornweave_forex/backtest/engine.py:119,126`

`self._warmup = max(cfg.backtest.warmup_bars, 1)`; `build_indicators` exposes `ready_index` (`context.py:125-130`: max first finite EMA/ATR/ADX incl. `2*period-2` for ADX) but the timeline starts at `warmup_bars` regardless. With `warmup_bars` lowered, early bars silently evaluate NaN indicators (they get blocked, not crash — misleading `blocked_by` counts).
**Fix:** `self._warmup = max(cfg.backtest.warmup_bars, indicator_set.ready_index + 1)` per symbol, like source (`nornweave/backtest/engine.py:193`).

### m4. O(n²) indicator rebuild per bar in the backtest
`nornweave_forex/backtest/engine.py:159-161`

`build_indicators(candles.slice(0, idx+1), ...)` runs the full EMA/ADX/ATR/pivot/daily computation **for every bar of every symbol** — for 8 pairs × 5 000 bars that is 40 000 full recomputations of a Python-loop RMA/EMA. Correct but slow (minutes-to-hours), and it makes the eventual `context_at` rewrite (B2) mandatory rather than cosmetic.
**Fix:** build indicators once per symbol over the full series and read index `idx` (source pattern).

### m5. `_build_context` is duplicated in two consumers and already diverges from `strategy/context.py`
`nornweave_forex/backtest/engine.py:393-413`, `nornweave_forex/live/runner.py:548-570`

Both hand-roll `ema_ref_idx = max(0, idx - lookback)`, hard-code `daily`, omit streaks, `footprint_delta` and `adx_threshold`. `IndicatorSet.context_at` (`context.py:64-90`) exists precisely to prevent this and already has the right semantics (NaN `ema_ref` when not enough history).
**Fix:** delete both helpers; call `context_at` in both consumers (also fixes B2/B4/M6 for free).

### m6. Dead state/guardrails in the live runner
`nornweave_forex/live/runner.py:38,105,164`

- `feed_is_stale` imported but **never called** (grep: only import + definition in `feed.py`); `Guardrails.stale_tolerance_intervals=2` is never read ⇒ a silent MT5 feed will never halt the runner.
- `self.act_on_last_bar_only = True` is assigned and never read;
- `Guardrails.max_daily_loss_pct` is applied against `day_start_equity` computed from stale marks (`runner.py:238-239`) — acceptable, but the stale-feed guardrail is the safety valve that is missing.
**Fix:** call `feed_is_stale(now, last_seen, interval_minutes)` per symbol in `step()` and halt; remove or use the other two fields.

### m7. Gap handling in `step()` only processes the last new bar
`nornweave_forex/live/runner.py:247-254`

```python
for symbol, bars in new_bars.items():
    if state.positions.get(symbol) is not None:
        self._process_bar(state, symbol, bars[-1], now, result)   # bars[:-1] silently ignored
```
After a restart (in-memory `_last_ts` resets to 0, `mark_seen` never used) the first poll can return up to `lookback` closed bars; stops/TPs that fired in the skipped bars are never applied, yet `state.symbol_last_bar_ts` jumps to `bars[-1].ts`. Only the last bar is also used for entries — fine — but exits must be resolved bar by bar.
**Fix:** iterate `bars` oldest→newest, or at minimum detect `len(bars) > 1` and replay `plan_intrabar_exits`/`stop_breached_on_close` for each skipped bar before acting on `bars[-1]`.

### m8. Unused imports / leftovers
`nornweave_forex/backtest/engine.py:39,47,397` (`DailyState` imported top-level and again inside `_build_context`; `_DOW_BY_CODE` unused; `Sequence`/`Candle` imported but unused), `nornweave_forex/live/runner.py:53` (`Candle` used only for the history rebuild), `engine.py:318` (`inst2` unused), `nornweave_forex/config.py:25-28` (`bars_per_day`/preset helpers exported but no caller inside the port).
**Fix:** remove; wire `preset_table` into the `presets` CLI command (`cli.py:213-215` currently prints static text) or delete the presets.

### m9. Backtest result summary / `describe()` advertises crypto fields
`nornweave_forex/config.py:646-667`, printed as `BacktestResult.config_summary` (`engine.py:347`)

`describe()` prints `symbol=EURUSD interval=120 category=forex` and `fee=0.0000%` (now that `taker_fee=0.0`), plus the legacy `symbol`/`category` fields that neither engine reads; for FX the meaningful line would be spread/swap/commission. It also prints `stop=-5.00% be_trigger=9.00%`, which are the miscalibrated FX defaults from M1.
**Fix:** print `portfolio.symbols`, `max_positions`, spread points, swap, commission per lot, and give `fee=` a meaningful label when the cost model is spread-only.

---

## Port gaps

1. **Forex execution-cost model is half-wired.** `taker_fee`/`maker_fee` were just set to `0.0` (`config.py:454-455`) with a comment saying "the backtester charges the spread explicitly" — but the backtester charges half-spread **only on entry** (`engine.py:259-267`), never on exits (M8), and swap is accrued but never booked (B11). There is still no per-lot commission field, which is how many MT5 accounts actually charge (spread-only is not universal). Spread/swap themselves live in `InstrumentSpec` (`instrument.py:153-167`), fine, but the config cannot express them per account.
2. **No config file exists.** `cli.py` defaults to `configs/forex.yaml` for `fetch`/`backtest`/`live`/`demo` (`cli.py:57,61,67`; `_load_config` at `77-78`), but `configs/` is empty ⇒ every real command dies with `ConfigError: config file not found`. The shipped `Config()` is still crypto-derived (M1).
3. **`fetch` writes data that nothing reads.** `_fetch_symbols` saves `data/candles/{symbol}.npz` (`cli.py:88-101`); `_run_backtest` ignores it and always calls `synthetic_candles` (`cli.py:104-116`). `--start`/`--end` are parsed (`cli.py:62-63`) and never used. The "backtest" command therefore does not backtest real FX data (see `docs/review/offline.md` for the data path).
4. **`features.py` (19 KB, identical to source) is unreachable in the port.** Nothing imports `nornweave_forex.features` (grep: 0 hits outside itself), and `features.py`'s `FeatureLog`/`observe` path has no consumer — the crypto project's model-training pipeline was dropped without note.
5. **`strategy/tuning.py` + `TuningParams` unreachable** (M5): implemented and validated, never passed to `build_indicators`. Same for `swap_enabled`: validated-by-inheritance but read nowhere (`grep`: 0 hits outside `config.py`), while the engine accrues swap unconditionally.
6. **`cli._default_instrument` (`cli.py:118-134`) hard-codes `pip_value_per_lot=10.0` for every pair**, including `EURJPY`/`USDJPY`/`USDCHF`/`USDCAD`, where it is wrong (JPY pip value ≈ $6.7 at 150; USD-base pairs ≈ 1/rate × 10). A concurrent edit added `instrument.default_instrument()` (`instrument.py:74-116`) which derives `pip_value_per_lot` correctly (`1000/USDJPY` etc.) and swaps/spread by quote currency — the CLI should call it instead of duplicating the table. Sizing (`risk/sizing.py:88`) and open-risk aggregation (`engine.py:390`, `runner.py:479`) inherit the error for 6 of the 8 default symbols. Only the live MT5 path replaces it from `symbol_info` (`cli.py:153-157`); backtest/demo never do.
7. **Still-dead config surface:** `Config.category`, `Config.symbol`, `Config.data_file`, `Config.start`, `Config.end`, `Config.label`, `BacktestParams.maker_fee`, `BacktestParams.swap_enabled`, `RiskParams.size_mode`/`notional_pct`/`equity_pct`/`min_notional`, `RiskParams.tp_anchor`, `TuningParams.*` (0 reads outside `config.py`; `symbol` only appears in `describe()`). `Config.symbols` now exists (`config.py:597-599`), so the runner's `config.symbols` use (`runner.py:78,86,95`) resolves — that part is fixed.
8. **Auto Calibration, footprint delta and daily EMA modes are carried but unreachable** end-to-end (features.py / tuning wiring above), so the port cannot reproduce several documented FEHU behaviours.
9. **Live/backtest parity is unverified by tests.** The suite is empty (no tests directory content that exercises the strategy chain); every blocker above would have been caught by one smoke test that runs `PortfolioBacktester(...).run()` on ~300 synthetic bars and one `PortfolioRunner.step()`.

---

## Recommended next steps (ordered)

1. **Fix the consumer→shared-API contract first, in one place.** Replace both hand-rolled `_build_context` bodies with `IndicatorSet.context_at(idx)`; build indicators **once** per series with `(candles, cfg.interval, cfg.strategy, tuning=cfg.tuning)`; delete `read_dow` usage; fix `DailyState`, `build_exit_ladder`, `break_even_should_arm`, `compute_metrics` call sites. This clears B1–B8 and M5–M6 in one edit.
2. **Fix the backtester accounting and timing** (B10–B13, M8): next-bar open entry, realised cash accumulation (`cash += pnl - fee`, swap), `contract_size` in PnL and marks, per-symbol marks, one equity point per timestamp, exit spread.
3. **Restore the daily overlay** (B5): pass the real `DailyContext`; add a smoke test asserting default config can produce a non-zero number of entries on synthetic data (today it is provably zero).
4. **Add a Forex config file** (`configs/forex.yaml`) and change the remaining crypto risk defaults (M1, M2, M9): FX stop/be units, retire the meme/midcap presets, wire or delete `maker_fee`/`swap_enabled`, add a per-lot commission field if the account charges one, and validate intervals, symbol/portfolio consistency and lot-grid viability of the TP ladder.
5. **Reject or implement dead knobs** (M3/M4/M7/M10, Port gaps 4–7): `size_mode`, `tp_anchor`, `exit_order`, `tuning`, `reversal_rule`, `min_notional`, `symbol`, `category`, `data_file`, `maker_fee`, `swap_enabled`, `features.py`/`tuning.py` — either implement them or delete them so a config file cannot silently promise behaviour that does not exist.
6. **Add a minimal test suite** (Port gap 9): identity hashes for the copied modules, one synthetic multi-symbol backtest, one paper-runner step, one FX sizing/lot-grid test, one daily-filter causality test.

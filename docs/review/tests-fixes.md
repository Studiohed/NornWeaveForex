# Regression tests for the shared modules — delivery note

Task: `task-8` · Author: `audit-core` · Scope: `tests/test_execution.py`, `tests/test_state_schema.py`,
`tests/test_config.py`, `tests/test_imports.py`, this document. No production code was modified.

Environment: `.venv\Scripts\python.exe` (Python 3.13.15, pytest 9.1.1), commands run from
`D:\PROGRAMMING\NornWeaveForex`.

---

## What was delivered

| File | Tests | What it locks |
|---|---|---|
| `tests/test_execution.py` | 30 | The shared bar/exit rules, ported **verbatim** from `D:\PROGRAMMING\NornWeave\tests\test_execution.py` (import rename only — `execution.py` is byte-identical to source). Enumerates every branch: stop vs target ordering, gap fills, close-triggered stops, break-even arming, `exit_fill`/`tp_weight_basis` modes. |
| `tests/test_state_schema.py` | 35 | The state JSON contract: schema `>= 2` written and declared, multi-symbol `positions` round-trip, Forex money metadata (pip/pip_value/contract_size/swap/mt5_ticket), `ClosedTrade.notional_account` round-trip, newer schema refused and file kept, corrupt JSON raises, unknown keys raise, missing optional keys fall back, explicit quarantine, and `client_id` uniqueness/stability/31-char budget. |
| `tests/test_config.py` | 51 | The shipped `configs/forex.yaml` (8 majors, `max_positions=2`, `taker_fee==0`, `maker_fee==0`, `swap_enabled`, EURUSD, FX-scaled stops) plus every validation rule that landed during the fixes: intraday-only interval (W/M rejected, D allowed), duplicate symbols, `max_positions > len(symbols)`, symbol outside the watchlist, portfolio budget too small for `max_positions`, bad risk/strategy/backtest enums, unknown YAML keys at three nesting levels, `to_dict`/`describe` keeping the Forex fields. |
| `tests/test_imports.py` | 45 | `pkgutil.walk_packages` import of **every** module under `nornweave_forex` (39 discovered, new modules auto-picked), `cli.build_parser()` + all documented subcommands, `Config.from_yaml('configs/forex.yaml')` through the public API, and no import-time state files. Optional-extras dependencies are skipped only when genuinely unavailable; an intra-package import failure is never skipped. |

Test count is **116** for the three files the Lead asked to gate on, **45** import tests, **191** for the full suite.

---

## Exact commands and results

### 1. The requested gate (execution + state schema + config)

```powershell
cd D:\PROGRAMMING\NornWeaveForex
.venv\Scripts\python.exe -m pytest tests/test_execution.py tests/test_state_schema.py tests/test_config.py -q
```

```
........................................................................ [ 62%]
............................................                             [100%]
116 passed in 0.46s
```

Exit code `0`. Per file: `test_execution.py` 30 passed, `test_state_schema.py` 35 passed,
`test_config.py` 51 passed.

### 2. Import smoke test

```powershell
.venv\Scripts\python.exe -m pytest tests/test_imports.py -q
```

```
.............................................                            [100%]
45 passed in 0.29s
```

Exit code `0`. Modules: 39; skips: 0. (`dashboard` and `mt5_server` are importable again after the
concurrent fixes; their optional extras `fastapi`/`MetaTrader5` are *not* installed in this venv, so
they would be skipped if those modules went back to eager optional imports.)

### 3. Full suite (includes the Lead-owned tests)

```powershell
.venv\Scripts\python.exe -m pytest -q
```

```
........................................................................ [ 75%]
...............................................                          [100%]
191 passed in 0.75s
```

Exit code `0`. Breakdown: 30 Lead-owned (`test_instrument.py`, `test_state_money.py`,
`test_paper_broker.py`) + 30 execution + 35 state schema + 51 config + 45 imports.

> Note: `pyproject.toml` sets `addopts = "-q"`, so appending another `-q` (as in the requested
> command) makes it `-qq` and suppresses the summary line. The pass/fail dots and exit code are
> unaffected; add `-o addopts=""` if you want the “N passed in …” line, which is how the totals
> above were captured.

---

## Findings produced by writing the tests

### F1. `client_id` collided **across pairs** — one pair's exit would be silently dropped (found, now fixed)

This is the one defect the new tests caught in production code during the window.

* The runner keeps a single global `executed_client_ids` list (`live/state.py`) and checks
  `state.already_executed(cid)` before placing an exit. All 8 majors close on the same bar timestamps,
  so ids must be unique per `(symbol, bar, purpose)`.
* The old `client_id` shortened the **symbol to its first 4 characters** on the hashed path. `USDCAD`
  and `USDCHF` both reduce to `USDC`, and the digest covers only `purpose`, so every exit purpose on a
  normal H2 timestamp produced the same id for both pairs. Example captured at 18:59:

  ```
  client_id("USDCAD", 1792080000000, "exit-take_profit_1")
  == client_id("USDCHF", 1792080000000, "exit-take_profit_1")
  == "USDC-1792080000000-exit--f902a2"
  ```

  With 15-digit timestamps (`999999999999999`, year ~33658) the symbol shrank to 2 characters, so
  `EURUSD`/`EURJPY` and `USDJPY`/`USDCAD`/`USDCHF` collided for all purposes.
* Consequence: the second pair's take-profit/stop/reversal exit is treated as a duplicate and skipped
  — a position left open and unprotected while the runner believes it is managing it.
* `state.py` was fixed at 19:00:02 while this test file was being written: the full 6-letter symbol is
  now kept and only the purpose text is shortened. `tests/test_state_schema.py::test_client_ids_are_unique_across_the_eight_majors`
  passed on re-run; the test stays as the regression guard. Verify the same property is preserved if
  `client_id` is ever reworked again.

### F2. Recorded state of the suite while the fixes were landing

The three files were authored and run against a moving tree. Transient failures observed and what
happened to them:

| Observation | Cause | Resolution |
|---|---|---|
| 4 failures on the first run (18:59): `test_client_ids_are_unique_across_the_eight_majors` | real production defect, see F1 | fixed in `live/state.py` at 19:00:02; re-run green |
| 3 failures in `test_config.py` (`symbol` not normalised after `setattr`, stale tuning cap assumption, `asdict` keeps tuples) | bugs in the **test code**, not production | corrected; see “Test design notes” |
| `test_imports.py` was written while `live/runner.py`/`cli.py` were being repaired (they failed at 18:47 with `read_dow`/`PaperBroker`). By the time the file was executed (19:00) those fixes had landed, so it ran 45/45 green. It would have failed on the pre-fix tree exactly as intended. | — | no weakening: the test imports every module with no `pytest.importorskip`. |

**No expected failures remain at this snapshot.** If another agent re-breaks an import or an API
signature, `test_imports.py` will fail loudly rather than skip (only third-party optional extras are
eligible for a skip).

---

## Test design notes / maintenance

1. **`test_execution.py` is a verbatim port** — keep it that way. `execution.py` is byte-identical to
   the source project, so any diff between `D:\PROGRAMMING\NornWeave\tests\test_execution.py` and this
   file (beyond the package rename) is a mistake. It is the highest-value file because the ordering
   rule it enumerates (`sl_first` vs `tp_first`) moves measured return materially.
2. **Config tests pin the shipped YAML values**, not just the schema. If `configs/forex.yaml` is
   intentionally retuned, update the pinned assertions in the same commit; a silent drift from the
   Forex cost model (`taker_fee == 0`, `swap_enabled`) is what these tests are for.
3. **All validation assertions go through `Config.validate()` / `Config.from_yaml()`** (the public
   path), so moving a rule between `RiskParams`/`PortfolioParams`/`Config` will not break the tests
   unnecessarily. Error matches use substrings (`"unique"`, `"exceeds the number of watched symbols"`,
   `"cannot host"`, `"longer than a day"`, `"not in portfolio.symbols"`) rather than exact messages.
4. **`valid_config(**overrides)` applies overrides with `setattr`**, which intentionally bypasses
   `__post_init__` normalisation — only the explicit case-insensitivity test constructs
   `Config(symbol="eurusd")` directly. Keep that distinction in mind when adding cases.
5. **Optional dependencies**: `test_imports.py` skips a `ModuleNotFoundError` only when the missing
   top-level package is not installed *and* is not part of `nornweave_forex`. Today the venv lacks
   `fastapi`/`MetaTrader5` (declared under `[project.optional-dependencies]`), so installing those
   extras in CI would remove the last skip path entirely. Recommend CI runs
   `pip install -e ".[mt5server,dashboard,dev]"` (on Windows) so the smoke test covers 100% of modules
   with no conditional skips.
6. **State tests avoid arithmetic duplication**: `tests/test_state_money.py` owns `equity`/`pnl_money`/
   `book_realized` arithmetic; this file checks serialisation and identifiers only (one equality
   assertion that `pnl_money` survives a round-trip).
7. **Module-count tripwire**: `test_the_module_walk_covers_the_package` asserts `len(MODULES) >= 25`
   and that 10 known entry points are discovered. Adding modules needs no test change; accidentally
   dropping a package (or `walk_packages` being unable to recurse) fails the test.
8. **The failing-by-design test to keep** is
   `test_client_ids_are_unique_across_the_eight_majors`: it encodes a cross-cutting invariant, so if
   the id scheme is refactored, this test — not manual review — is what says whether exits can still
   collide across pairs.

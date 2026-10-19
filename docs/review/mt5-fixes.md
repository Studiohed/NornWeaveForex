# MT5 / sizing hardening — implementation report (task-7)

Owner: `audit-mt5`. Follows the audit in `docs/review/mt5.md` (task-2).
Read-only for everything except the four files below plus this report.

## Summary

The money-critical MT5 path is hardened:

* **No blind retry of orders.** `mt5_client` now retries only an explicit
  read-only method set; `order_send` / `order_modify_sltp` / `initialize` /
  `shutdown` are at-most-once and raise `MT5ConnectionLost` with an explicit
  "reply lost, reconcile before retrying" message instead of resending.
* **The request is complete and clamped.** `order_send` carries
  `price` (ask/bid, rounded to digits), `sl`/`tp` = 0 on entries, the
  un-sliced `client_id` as `comment` (<= 31 chars), and a volume clamped to the
  held lots *before* the dict is built. `LiveFill.qty` comes from the broker's
  executed volume (partial fills), and the entry position ticket is taken from
  `result["order"]` and bound to the position.
* **No more "first position wins".** Reduce-only orders target the bound ticket;
  if it is unknown, exactly one magic-matching position is required, otherwise
  the order is refused. Netting/exchange accounts are refused at `initialize`.
* **Retcode handling is real.** Correct `ENUM_TRADE_RETCODE` constants (the port
  had `PRICE_CHANGED=10021`/`PRICE_OFF=10015`, both wrong), bounded
  refresh-and-resend on `10020`/`10021` only, filling-mode fallback
  IOC -> FOK -> RETURN on `10030` cached per symbol, readable rejection text.
* **Sizing uses real account-currency margin.** `position_size` uses
  `InstrumentSpec.notional_account(price, lots)` / leverage (no `entry < 10`
  heuristic) and always applies a margin cap: broker `free_margin` when given,
  otherwise the documented `max_notional = equity * leverage` fallback. Being
  scaled below `min_lot` now rejects with `insufficient_margin` instead of
  silently bumping back over the budget.
* **Broker metadata is exposed.** `symbol_info` now returns
  `swap_rollover3days` (MT5 enum int, Sunday=0), `swap_mode`,
  `currency_profit`/`currency_margin`, `trade_stops_level`,
  `trade_freeze_level`, `filling_mode`, `trade_exemode`; `initialize`/
  `account_info` return `margin_mode`; new `exposed_order_check` preflight.
  Auth model untouched.
* **Docstrings corrected**: `live/broker.py`'s `ORDER_TYPE_CLOSE_BY` claim was
  removed from `mt5_broker`; the module now documents DEAL + position-ticket
  close semantics, the hedging requirement, price-on-market-orders and the
  filling fallback.

## Files changed (all within task-7 scope)

| file | change |
|---|---|
| `nornweave_forex/broker/mt5_client.py` | read-only retry set; at-most-once mutations; connection pre-flight enforces `connect_timeout`; `order_check()` added; docstrings |
| `nornweave_forex/broker/mt5_broker.py` | price/sl/tp in request; reduce-only clamp before request; result volume; ticket binding; margin-mode gate; retcode retries/filling fallback; error mapping; docstrings |
| `nornweave_forex/mt5_server/server.py` | swap/fill/stops/margin-mode metadata; `exposed_order_check`; `request_id` in order result; docstrings |
| `nornweave_forex/risk/sizing.py` | `notional_account`-based margin; always-on margin cap (free_margin or fallback); `insufficient_margin`; rewritten module docstring |
| `docs/review/mt5-fixes.md` | this report |

No changes to `instrument.py`, `live/state.py`, `live/broker.py`, `config.py`,
`live/runner.py`, `backtest/`, `cli.py` or the other reviewers' reports.

## Requirement -> implementation

1. **B2 client retry policy** — `mt5_client.py:48` `READ_ONLY_METHODS`;
   `_call` (`mt5_client.py:141-196`) uses `attempts = reconnect_attempts` for
   reads and `1` for mutations. On a transport error after `sent = True` for a
   mutating call it raises `MT5ConnectionLost` at
   `mt5_client.py:174` ("not retried because retrying a mutating call can
   duplicate it"). Reads still reconnect/retry. `initialize`/`shutdown` are
   mutations (lifecycle). `connect_timeout` is now enforced with a
   `socket.create_connection` pre-flight (`mt5_client.py:107`).
2. **M1 price / filling / exemode** — `mt5_broker.py:418` `"price": price`
   from `_tick_price` (`mt5_broker.py:270`, ask for buys / bid for sells,
   `spec.round_price`); `:417-419` `sl`/`tp` = 0 on entries; `:425` comment is
   the raw `client_id` (no slicing); `:427` + `_send_order`
   (`mt5_broker.py:322-373`) selects filling mode; on `10030` retries
   IOC -> FOK -> RETURN and caches the accepted mode. Price retries only on
   `10020`/`10021` (`RETRY_WITH_FRESH_PRICE`) and are bounded by
   `max_price_retries` (default 2).
3. **M2/M3 volume + ticket** — `execute` resolves/clamps reduce-only volume
   before building the request (`mt5_broker.py:378-386`), sets
   `request["volume"]` to the clamped value, targets the bound ticket
   (`_resolve_close_target`, `mt5_broker.py:294-320`), books
   `result["volume"]` (`:441-442`), binds `result["order"]`
   (`:452`) and `OpenPositionState.mt5_ticket` (`_bind_ticket`,
   `mt5_broker.py:469-484`). `initialize` refuses `margin_mode != 2`
   (`mt5_broker.py:233`).
4. **M4/M5 sizing margin** — `sizing.py:105-107` `margin_for` via
   `instrument.notional_account`; cap at `:111-128` (free margin or fallback
   `equity * leverage` notional); `insufficient_margin` when the cap drops the
   size below `min_lot` (`:132-134`, `:150-153`, `:164-181`). Signature is
   backward compatible (`free_margin` optional, `SizeDecision` fields
   unchanged).
5. **M6 swap metadata** — `server.py:206` `swap_rollover3days` (MT5 enum int,
   Sunday=0; `instrument.from_mt5` converts to Python Wednesday), `:209`
   `swap_mode` (1 == POINTS), `:211-213` `currency_profit`/`currency_margin`,
   `:214-219` stops/filling/exemode levels. No unit interpretation was moved
   into the server; `InstrumentSpec` owns it.
6. **order_check** — `server.py:310-339` `exposed_order_check`, returning
   retcode/balance/equity/profit/margin/margin_free/margin_level/comment;
   `mt5_client.py:238-241` `order_check()` client method.
7. **Docstrings** — `mt5_broker.py:1-52` (no `ORDER_TYPE_CLOSE_BY`, hedging
   requirement, price rule, filling fallback, ticket binding); `mt5_server`
   order-send docstring (`server.py:340-357`).
8. **Auth unchanged** — verified again at runtime (below).

## Evidence

Environment: `.venv` Python 3.13, `rpyc 6.0.2`, no `MetaTrader5` installed
(calls exercised through fake clients / a fake `mt5` module).

### Static checks

```
> .venv\Scripts\python.exe -c "...py_compile 4 files...; import ..."
py_compile OK: 4 files
imports OK
EXIT=0
```

### Probe (no MetaTrader5): 53/53 PASS

```
> .venv\Scripts\python.exe %TEMP%\mt5_fixes_probe.py   # C:\Users\Admin\AppData\Local\Temp\mt5_fixes_probe.py
...
[PASS] M1 order_send request has price :: price=1.10005
[PASS] M1 request has sl/tp=0 on entry
[PASS] M1 request volume floored to lot step :: volume=0.1
[PASS] M1 comment not sliced / <=31 :: comment='EURUSD-1730419200000-entry'
[PASS] M1 LiveFill qty from result volume :: qty=0.1
[PASS] M3 position.mt5_ticket bound :: ticket=555
[PASS] M3 SLTP modify used bound ticket :: modified=[(555, 1.07, 1.12)]
[PASS] M2 close request volume clamped :: volume=0.03
[PASS] M2 close request targets bound ticket :: position=555
[PASS] M2 LiveFill qty = result volume :: qty=0.03
[PASS] M3 ambiguous close target refused :: OrderRejected
[PASS] M3 netting account refused :: RuntimeError (margin_mode=0)
[PASS] B2 lost reply raises MT5ConnectionLost; order_send calls=1        <-- no duplicate send
[PASS] M1 price-changed re-sends bounded :: order_send calls=3 (10020, 10021, done)
[PASS] M1 filling fallback IOC->FOK :: filling=[1, 0]
[PASS] client: read call retried after EOF; order_send/initialize not retried (1 attempt each)
[PASS] sizing: free_margin cap scales lots :: lots=0.02 (uncapped 0.09)
[PASS] sizing: scaled margin fits budget :: margin=73.33
[PASS] sizing: unaffordable min lot -> insufficient_margin
[PASS] sizing: fallback cap equity*leverage scales lots :: lots=0.04
[PASS] sizing: JPY pair uses notional_account :: lots=1.47 margin=4998.00
[PASS] server: swap_rollover3days=3 -> swap_triple_day=2 (Python Wednesday), swap_in_points=True
[PASS] server: order_check preflight returns margin data
ALL PROBES PASSED   (53 PASS, 0 FAIL, EXIT=0)
```

### Auth model re-verified (unchanged)

```
> .venv\Scripts\python.exe - <<build_server on 127.0.0.1:18901>>
PASS pre-auth denied: PermissionError
PASS private attr denied: AttributeError
PASS auth: True
PASS ping: True
EXIT=0
```

### Existing test suite

```
> .venv\Scripts\python.exe -m pytest tests -q
...  [100%]   PYTEST_EXIT=0     # entire suite passes
> .venv\Scripts\python.exe -m pytest tests/test_instrument.py -q   # 13 passed
```

While this task was in flight another agent fixed the `live/state.py`
`client_id` collision the 31-char limit had introduced (`USDCAD`/`USDCHF` both
collapsing to `USDC-...`); the current file keeps the full symbol
(`USDCAD-1730419200000-exi-b2dbb2` vs `USDCHF-...`) and
`tests/test_state_schema.py` now passes (35/35). Independent check over the
eight majors x ten purposes x three timestamps: **0 collisions, 0 ids over 31
chars**. The broker consumes `client_id` verbatim, so that contract holds.

`tests/test_sizing.py` is still a 0-byte file (see remaining gaps).

## Remaining gaps / follow-ups (not in my write scope)

1. **Runner does not pass broker margin/equity yet.** `live/runner.py` calls
   `position_size(...)` without `free_margin` and with `equity=state.cash`;
   the fallback cap (`equity * leverage`) now applies, but the authoritative
   `account_info()["margin_free"]`/`["equity"]` cap needs the runner owner to
   pass it (and `swap_money`/`pnl_money` are available for the same
   integration).
2. **Reconciliation hooks still uncalled.** `positions_snapshot`,
   `closed_since`, `protective_stop_price`, `actual_size` are implemented and
   documented but the runner does not call them yet; startup reconciliation and
   real swap/commission booking remain an integration task.
3. **`order_check` not wired into `execute`.** Exposed/client method exists;
   calling it for the first order per symbol (or before every order at a small
   latency cost) is recommended, then map its retcode like `order_send`'s.
4. **Filling mode is learned on `10030`, not seeded from `filling_mode`.**
   The server now returns `filling_mode`/`trade_exemode`, but the broker does
   not read `symbol_info` (instruments come from `cli`). Passing the payload
   into the broker or pre-seeding `_filling[symbol]` from the bitmask would
   save the first rejected order.
5. **Ticket binding is process-local.** `_tickets` is rebuilt from
   `result["order"]` on entry and from a unique magic match on resume; after a
   restart with multiple same-symbol magic positions the broker refuses rather
   than guesses (intended). A persistent `mt5_ticket` in `OpenPositionState`
   already exists — the runner should populate it from the fill/`positions_get`
   on resume.
6. **`tests/test_sizing.py` is no longer empty** — task-9 added 18 sizing tests
   and 22 MT5 broker tests (see "Permanent pytest coverage" below). The
   `%TEMP%` probe is now superseded by the repo tests.
7. **Feed/timezone/swap-estimation items from the audit** (M9-M11 in
   `docs/review/mt5.md`) are out of this task and untouched; the runner's
   `_overnight_swap` still does not use `instrument.swap_money`.
8. **Client reconnect still does not re-run `initialize`.** After a server
   restart the process-global MT5 connection may need re-initialising; the
   client currently only re-auths a new RPyC connection. Consider a lazy
   `initialize` after reconnect (server `_ensure_mt5` currently only lazily
   imports the module).

## Verified behaviour notes

* `MT5ConnectionLost` from `order_send` propagates to the caller (it is **not**
  converted to `OrderRejected`): a lost reply means "state unknown", and the
  runner must reconcile before deciding anything. The probe asserts exactly one
  `order_send` call in that case.
* `TRADE_RETCODE_DONE_PARTIAL` is accepted and the fill quantity comes from
  `result["volume"]`; `DONE` without a volume falls back to the clamped request
  quantity.
* Retry constants corrected: `PRICE_CHANGED=10020`, `PRICE_OFF=10021`,
  `INVALID_PRICE=10015`, `INVALID_FILL=10030`, `INVALID_STOPS=10016`.
* `max_price_retries` is a new keyword-only broker arg (default 2), so the
  existing `MT5Broker(...)` call sites are unaffected.

## Permanent pytest coverage (task-9)

The `%TEMP%` probes are ported into the repo; these tests are now the source of
truth and no longer depend on ad-hoc scripts.

| file | tests | covers |
|---|---|---|
| `tests/test_sizing.py` | 18 | EURUSD 1% / 100-pip -> 0.10 lots; USDJPY pip-value effect; portfolio cap reduces/exhausts and is never exceeded; min-lot bump (+ its two caps); `free_margin` scaling and `insufficient_margin`; `equity * leverage` fallback; max-lot clamp consistency; bad-input rejections |
| `tests/test_mt5_broker.py` | 22 | request has ask/bid price, sl/tp=0, magic, 31-char un-sliced comment, lot-rounded volume; reduce-only clamp + ticket target; ambiguous/no positions rejected; fill qty from `result["volume"]`; retcode -> `OrderRejected` text; bounded price-changed retry; filling fallback IOC->FOK; **`MT5ConnectionLost` not retried (send calls == 1) and propagates**; `on_position_opened` ticket binding + disaster SL/first TP; netting/exchange refused, hedging accepted; `READ_ONLY_METHODS` policy + read-retry/mutation-at-most-once |

Both files use the shared fixtures (`eurusd_spec`, `usdjpy_spec`) and a local
`FakeClient`; they do not import `live/runner.py`, `backtest/` or `cli.py`.

Exact commands and results:

```
> .venv\Scripts\python.exe -m pytest tests/test_sizing.py tests/test_mt5_broker.py -q
........................................                                 [100%]
EXIT=0                        # 40 passed (18 + 22)

> .venv\Scripts\python.exe -m pytest tests -q
........................................................................  [ 30%]
........................................................................  [ 60%]
........................................................................  [ 90%]
........................                                                   [100%]
EXIT=0                        # 240 passed, whole suite (10 files)

# NOTE: the full suite is being edited concurrently by other agents (state.py,
# backtest engine). A later run while those edits were in flight reported 4
# failures in tests/test_backtest_engine.py (swap/funding) and
# tests/test_state_schema.py (StateStore.load exception wrapping) — both in
# production files outside this task's scope and unrelated to the sizing/broker
# files. The two files owned here stayed green throughout.
```

### Production bug found and fixed in scope: boundary lot floored one step down

Writing the acceptance test exposed a real bug in `risk/sizing.py`: pips were
computed in binary floats, so an exact 100-pip stop (`1.10 -> 1.09`) became
`100.0000000000012` pips, `risk_per_lot` became `1000.0000000000012`, and the
target `0.10` lots was `0.0999999999999988` — `round_lots` then floored it to
**0.09** (and on a smaller account a target `0.01` could floor to `0.00` and be
rejected as `zero_lots_after_rounding`). Fixed by computing the stop distance,
pips and risk-per-lot in `Decimal` from `str()` prices (`_dec`), so
`Decimal("100.0") / Decimal("1000.0")` is exactly `0.1`.
`tests/test_sizing.py::test_eurusd_one_percent_one_hundred_pip_stop_is_tenth_of_a_lot`
pins the correct result. No other production code changed for task-9.

Note on the brief's USDJPY wording ("fewer lots than EURUSD for the same pip
stop"): a JPY pip is worth ~$6.667 versus $10 for EURUSD, so equal dollar risk at
an equal 100-pip stop needs **more** lots (0.14 vs 0.10 with the fixture).
`test_usdjpy_pip_value_changes_the_lot_amount` asserts the direction the math
requires and that the USD-quoted `$10/pip` figure is not being reused.


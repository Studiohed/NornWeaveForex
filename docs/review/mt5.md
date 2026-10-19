# Audit: MT5 plumbing, instrument spec and lot sizing

Task: `task-2` (owner `audit-mt5`). Read-only review of
`nornweave_forex/mt5_server/server.py`, `broker/mt5_client.py`, `broker/mt5_broker.py`,
`instrument.py`, `risk/sizing.py`, `live/feed.py`, plus `live/broker.py` and
`live/state.py` and the call-sites in `cli.py` / `live/runner.py` needed to judge
integration. Reference intent: `D:\PROGRAMMING\NornWeave` (untouched).

Method
------
* All scope files read in full; integration checked against `cli.py`
  (`_run_live`), `live/runner.py` (`step`, `_execute_exit`, `_maybe_open`,
  `_overnight_swap`) and `config.py` (`RiskParams`).
* Environment: `.venv` (Python 3.13), `rpyc 6.0.2`, `numpy 2.5.4`. `MetaTrader5`
  is **not installed**, so MT5 calls were reviewed statically against the
  documented Python/MT5 API (MQL5 docs: `order_send` / `symbol_info` /
  `copy_rates_from_pos`), plus one runtime RPyC test (below).
* Runtime check performed with an in-process `build_server("s3cret")` on
  `127.0.0.1:18899`:
  pre-auth `ping()` -> `PermissionError`; `root._authed`, `root._secret_hash`,
  `root._require_auth`, `root.root` -> `AttributeError: cannot access ...`;
  wrong secret -> `PermissionError`; correct secret -> `True`, then `ping()`
  works. So auth + exposure restriction **do work**; client `_open` correctly
  turns a bad handshake into `MT5ClientError`.
* `tests/test_instrument.py` and `tests/test_sizing.py` are **0-byte files**
  (`pytest` collects nothing, exit 5) — the money math has no test coverage at
  all.

Severity counts: **3 BLOCKER / 14 MAJOR / 16 MINOR** (plus port gaps).

---

## Summary

The RPyC envelope is in good shape: the shared secret is genuinely enforced on
every method, only `exposed_*` names are reachable, private state cannot be read
remotely, and the payloads are plain dicts. That is the strongest part of the
port.

The money layer is not. The port kept the crypto convention that PnL is
`(exit - entry) * qty`, but `qty` is now **lots**, so every PnL/equity number in
the live path is short by `contract_size` (~100,000x): the daily-loss guardrail,
the cash balance, the portfolio open-risk cap and position sizing all read
numbers that are wrong by five orders of magnitude, and the sizing layer never
enforces a margin cap in the live path even though its docstring says it does.
On top of that, swap is booked from a field the server never reads, with the
wrong unit and the wrong weekday, and the order request is missing the `price`
field that MT5 requires on Instant-execution symbols.

Execution has one clear safety hazard (blind retry of `order_send` after a
timeout can duplicate a live order) and one functional break (`live --paper`
cannot start). The MT5 API calls themselves are mostly shaped correctly
(`copy_rates_from_pos`, `symbol_info_tick`, `positions_get`, `history_deals_get`,
`order_send` result fields, retcodes are right); the gaps are missing metadata
fields, missing pre-trade checks, hedging/netting assumptions, and an
idempotency string that exceeds MT5's 31-char comment limit.

---

## Blockers

### B1. PnL and equity are computed in price units, not account currency (off by `contract_size`, ~100,000x)

`live/state.py:276-284`

```python
def equity(self, mark_prices: dict[str, float]) -> float:
    ...
    unrealised += (mark - pos.entry_price) * pos.qty * pos.direction.sign
    return self.cash + unrealised
```

`live/runner.py:399-400, 424, 463`

```python
pos.gross_pnl += (fill.price - pos.entry_price) * fill.qty * pos.direction.sign
...
state.cash += trade.net_pnl
...
equity = state.cash
```

Why wrong: after the port `pos.qty` is **lots** (0.10 = 10,000 base units), so a
10-pip move on 0.10 lots must be `0.0010 * 0.10 * 100_000 = $10`; this code
computes `0.0010 * 0.10 = $0.0001`. Consequences, all live:

* `state.cash` barely moves on wins/losses, so `_maybe_open` sizes from a
  fictional equity (`runner.py:463` passes `equity=state.cash`, not
  `account_info()["equity"]`).
* The daily-loss halt (`runner.py:258-268`) needs a loss ~100,000x larger than
  configured to trigger — the guardrail is effectively disabled.
* `ClosedTrade.net_pct_of_notional` (`state.py:190-192`) uses
  `entry_price * qty` as notional, which is ~1/100,000 of the real notional.
* The reference did not need `contract_size`; for FX every conversion must go
  through it (and to the account currency).

Fix: `pnl = (exit - entry) * lots * instrument.contract_size * direction.sign`,
converted from the quote currency to the account currency via a documented
`quote_to_account` rate (for the 8 majors: identity when quote == USD, divide by
`USDJPY` etc.). Equity must be marked the same way and, in live mode, reconciled
with `account_info()` equity rather than paper cash. Add an FX-specific
`ClosedTrade.notional` in account currency and a unit test that a 10-pip EURUSD
move on 0.10 lots equals $10.

### B2. Client retries `order_send` after EOF/timeout -> duplicate live orders

`broker/mt5_client.py:109-132`

```python
for attempt in range(self._reconnect_attempts):
    try:
        conn = self._open()
        fn = getattr(conn.root, name)
        return fn(*args, **kwargs)
    except (EOFError, socket.error, ConnectionError, BrokenPipeError) as exc:
        ...
        self.close()
        if attempt + 1 < self._reconnect_attempts:
            time.sleep(...)
```

Why wrong: `_call` is generic and retries **every** method, including
`order_send`. If the RPyC reply is lost after MT5 executed the trade (terminal
freeze, 30 s `sync_request_timeout`, Wi-Fi drop), the retry sends the same
market order again. MT5 `order_send` has no idempotency key: the comment is not
unique-checked by the broker and a duplicate entry has real money at risk. The
docstring's "one retry on connection loss" is safe for reads (`ping`,
`symbol_info`, `account_info`, `positions_get`, `history_deals_get`,
`copy_rates_from_pos`) and unsafe for mutations (`order_send`,
`order_modify_sltp`, `initialize`, `shutdown`).

Fix: whitelist read-only methods for retry. For `order_send`/`order_modify_sltp`
either do not retry, or, after a timeout, reconcile first: fetch
`positions_get(symbol)` filtered by `magic`/comment and only re-send if the
intent is absent (and make that reconciliation durable in the state file). The
same rule must apply to any future mapping of `MT5ConnectionLost` to an
`OrderRejected` retry.

### B3. `live --paper` crashes before the first bar: `instruments` may be unbound

`cli.py:140-177`

```python
broker: Any = None
if not args.paper:
    ...
    instruments = {s: _default_instrument(s) for s in cfg.portfolio.symbols}
    broker = MT5Broker(client, instruments, credentials=creds)
    ...
feed = MT5Feed(...)
...
runner = PortfolioRunner(cfg, feed=feed, store=store, broker=broker,
                         instruments=instruments, persist_every_bar=True)
```

Why wrong: in `--paper` mode the whole `if` block is skipped, so `instruments`
is never assigned; line 175 raises `UnboundLocalError` and the documented paper
mode (`live --paper`) cannot run at all. (`PortfolioRunner` already supports
`broker=None` -> `PaperBroker` at `runner.py:151`, so the intent was clearly for
this to work.)

Fix: always build the instrument map (paper mode can call
`client.symbol_info` + `InstrumentSpec.from_mt5`, or fall back to
`_default_instrument` only when offline), then let `broker=None` select
`PaperBroker`. Also stop assigning `broker._instruments` from outside
(`cli.py:157`) — inject it once in the constructor after specs are known.

---

## Major

### M1. `order_send` request omits `price`; the computed price is dead code

`broker/mt5_broker.py:193-220`

```python
price = float(tick[price_field])
...
request: dict[str, Any] = {
    "action": 1, "symbol": order.symbol, "volume": qty, "type": mt5_type,
    "deviation": deviation_points, "magic": self._magic,
    "comment": order.client_id[:31],
    "type_time": 0, "type_filling": 1,
}
```

MQL5 `order_send` docs: *"The price is not set in case of market orders for
instruments of the 'Market Execution' type ... having the TRADE_ACTION_DEAL
type"* — i.e. `price` is **required** for `SYMBOL_TRADE_EXECUTION_INSTANT`
(common on retail FX) and exchange execution. The port never reads
`trade_exemode`, so it cannot know whether omitting `price` is legal; the
variable computed at lines 195-196 is never used, which is the smell. On an
Instant-execution account every entry/exit will come back as `10015`
(`TRADE_RETCODE_INVALID_PRICE`/price off) -> `OrderRejected` -> no trading.

Fix: always include `"price": spec.round_price(price)` (bid for sell, ask for
buy), and on `10021`/`10015`/`10004`/`10006` either refresh the tick and retry a
bounded number of times (for idempotency-safe paths) or reject with the retcode
mapped into the `OrderRejected` message. Read `filling_mode`/`trade_exemode`
once at startup and choose `type_filling` from it rather than hardcoding IOC
(`mt5_broker.py:219`; not every broker accepts IOC).

### M2. Reduce-only clamp is applied to the local variable only; `request["volume"]` keeps the pre-clamp size

`broker/mt5_broker.py:210-236`

```python
request = {..., "volume": qty, ...}
if order.reduce_only:
    ...
    target = positions[0]
    if qty > target.lots:
        qty = target.lots          # local only
    request["position"] = target.ticket
result = self._client.order_send(request)
```

Why wrong: when the requested reduce quantity is larger than the held position
(slippage on the ladder, state desync after a manual close, rounding), the
server receives the original larger volume -> MT5 rejects the close (10014
"volume invalid" / 10013), or on a hedging account closes the wrong size. The
returned `LiveFill.qty` uses the clamped value, so local and broker accounting
would diverge even if the fill succeeded partially.

Fix: clamp before building the request (`qty = min(qty, target.lots)`), refetch
the target's volume immediately before sending, and use
`result["volume"]` (not the request) for `LiveFill.qty`.

### M3. Position targeting assumes one position per symbol and a hedging account; `positions[0]` can attach the wrong ticket

`broker/mt5_broker.py:284-297`

```python
ticket = position.mt5_ticket
if not ticket:
    positions = [parse_position(p) for p in self._client.positions_get(symbol=position.symbol)
                 if int(p.get("magic", 0)) == self._magic]
    if not positions: raise OrderRejected(...)
    ticket = positions[0].ticket
```

and the close path at `mt5_broker.py:224-235` also picks `positions[0]`.

Why wrong: on a hedging account (very common for MT5 FX) two positions on the
same symbol are legal: a leftover from a failed exit, a manual trade using the
same magic (magic is a constant `20261018`, `mt5_broker.py:49`), or a
re-opened symbol after a crash. `positions[0]` is arbitrary, so the disaster
SL/TP and every partial close can be attached to the wrong ticket — the strategy
position is left naked while another is modified. Nothing checks
`account_info().margin_mode` (`ACCOUNT_MARGIN_MODE_RETAIL_HEDGING` vs
`RETAIL_NETTING`), so the port cannot even tell which model it is running in.

Fix: pass the ticket returned by the entry `order_send` (`result["order"]` is
the position ticket for market orders per MT5 docs) into `OpenPositionState`
instead of "find by symbol + magic". For reduce-only, filter by the position's
own ticket and, if unknown, require exactly one match (`len(positions) == 1`)
else reject. Verify `margin_mode` at `initialize()` and fail loudly on netting
accounts if ladder behaviour is not supported there.

### M4. Margin cap is absent in the live path; docstring claims a fallback that does not exist

`risk/sizing.py:91-105` and `live/runner.py:481-488`

```python
margin_required = notional_base * entry / leverage if entry < 10 else notional_base / leverage
if free_margin is not None and margin_required > free_margin:
    ... scale ...
```

Docstring (`sizing.py:60-63`): *"When ``None``, the cap falls back to
``equity * risk.leverage`` against the notional"* — there is no such fallback:
if `free_margin is None` the block is skipped entirely. The runner never passes
`free_margin` (`runner.py:481-488` passes only `equity`, `entry`, `stop`,
`instrument`, `risk`, `open_risk`), so the margin constraint never runs in live
trading. `risk.leverage` only appears inside the dead branch.

Fix: pass `free_margin=account_info()["margin_free"]` (live) at the call site;
implement the documented fallback (`max_notional = equity * risk.leverage`,
scale `lots` so `notional_base * quote_to_account / leverage <= free_margin`),
or delete the claim. Add a test with `free_margin` set and unset.

### M5. Margin conversion heuristic `entry < 10` is wrong for crosses and can understate margin by orders of magnitude

`risk/sizing.py:92-103, 119-120`

```python
notional_base = lots * instrument.contract_size          # in BASE currency
margin_required = notional_base * entry / leverage if entry < 10 else notional_base / leverage
```

Why wrong: `notional_base` is denominated in the **base** currency, but margin is
denominated in the **account** currency. The `entry < 10` branch assumes "price
< 10 means base->account conversion == entry". That happens to work for EURUSD
and USDJPY but fails for anything else: EURJPY (price ~160 -> branch 2) computes
`100_000 EUR / 100` as if it were USD, understating margin by `EURUSD`
(~1.15x) — in the *unsafe* direction, contradicting the docstring's
"conservative" claim. On a 2-digit symbol such as XAUUSD it is off by ~2000x
(100 oz vs $200,000). Only `quote == account currency` or `base == account
currency` makes the shortcut valid, and the code does not know which.

Fix: compute `notional_account = lots * contract_size * (quote_to_account for
quote ccy)` and `margin = notional_account / account_leverage`; get
`account_leverage`/`margin_initial` from `account_info()`/`symbol_info` instead
of the crypto `risk.leverage=5` default. Best: let MT5 answer —
`order_calc_margin(ORDER_TYPE_BUY, symbol, lots, price)` — and size against that.

### M6. Live swap is booked from a field the server does not read, in the wrong unit, on the wrong weekday, with the wrong sign convention

Three independent defects:

1. `mt5_server/server.py:188`
   ```python
   "swap_triple_day": int(getattr(info, "swap_triple_day", 3)),
   ```
   The MT5 `symbol_info` field is `swap_rollover3days` (MT5 also has
   `swap_mode`), so `getattr` **always** returns the default 3. A broker whose
   triple day is Monday/Friday is mis-booked.
2. `live/runner.py:588-592`
   ```python
   swap = spec.swap_long if pos.direction is Direction.LONG else spec.swap_short
   if bar_start.weekday() == spec.swap_triple_day:   # 3 == Wednesday? no: Python weekday 3 == Thursday
   ```
   MT5's `ENUM_DAY_OF_WEEK` is Sunday=0..Saturday=6 (so 3 = Wednesday), Python
   `weekday()` is Monday=0..Sunday=6. The comparison is off by one; even with
   the correct field the triple swap lands on Thursday. Correct mapping:
   `(mql5_day + 6) % 7`.
3. `instrument.py:72-74, 171-173` / `runner.py:588-592`: `swap_*` is documented
   and used as "per lot per night, in the account currency"
   (`return swap * pos.qty`), but `swap_mode` is never fetched. MT5's default
   `SYMBOL_SWAP_MODE_POINTS` means the number is in **points**, so the monetary
   value is `swap_points * point * contract_size * lots` (in quote currency, then
   converted). Treating it as account currency is right only by coincidence for
   5-digit USD-quoted pairs; it is wrong by ~7x for USDJPY (1 point = 100 JPY/point/lot)
   and wrong for interest/percent modes.
   Also `runner.py:418-419` computes `net_pnl = gross - fees - swap_accrued`
   while MT5's `swap_long` is normally negative for long carry — subtracting a
   negative adds the cost back (double negative).

Fix: server must expose `swap_mode` and `swap_rollover3days`; `InstrumentSpec`
must hold `swap_mode` and convert to account currency once; the triple-day
comparison must use a converted weekday; net PnL must use
`net = gross - fees + swap_signed` (or define swap as a cost consistently).
Best: stop estimating and book the real `swap`/`commission` from
`history_deals_get` (`closed_since`, see port gaps).

### M7. Deterministic client id (36 chars) is truncated to MT5's 31-char comment limit — the exact collision bug the reference fixed

`live/state.py:50, 57-77` and `broker/mt5_broker.py:217`

```python
_LINK_MAX = 36                                # Bybit orderLinkId limit
...
plain = f"{symbol}{stamp}-{purpose}"          # may be up to 36 chars
...
"comment": order.client_id[:31],              # MT5 caps the comment at 31 chars
```

The reference `state.py:105-144` documents a live defect where cutting the tail
made `exit-take_profit_1/_2/_3` identical and exits were dropped as duplicates;
the fix appends a 6-hex digest at the end. The MT5 client id is still 36 chars,
and the broker cuts it to 31 — for the intended exit ids (purpose
`exit-take_profit_1` on EURUSD: 6+14+19 = 39 -> hashed to 36) the truncation
keeps only **one digit of the 6-digit digest**:
`EURUSD-1730419200000-exit-tak-a`. Two different purposes on the same bar with
the same 8-char stub collide with probability ~1/16, and the state module's
whole "unique as well as stable" contract is silently broken at the broker.

Fix: make the comment limit an explicit parameter: `_LINK_MAX = 31` for the MT5
port (rebuild ids to fit, keeping the digest) and stop slicing in the broker —
`comment` must be exactly `client_id` (assert `len <= 31` at build time). Keep
the digest, not the tail: `symbol-bar_ts-<stub>-<digest>` with the digest always
in the final 31 chars.

### M8. Client errors are not mapped to `OrderRejected`, and the open hook's failure is swallowed -> naked position

`broker/mt5_broker.py:236, 299-305`, `live/runner.py:508-512, 542-545`

```python
result = self._client.order_send(request)      # may raise MT5ConnectionLost / MT5ClientError
...
self._client.order_modify_sltp(ticket, disaster_stop, tp)
...
except OrderRejected as exc:                   # runner: only this is caught
...
except Exception as exc:
    self._swallow(f"open_hook:{symbol}", exc)  # runner: SL/TP failure ignored
```

Why wrong: `MT5ClientError`/`MT5ConnectionLost` (RPC down, `symbol_info` None,
`RuntimeError` from the server) are not `OrderRejected`, so they escape the
runner's `except OrderRejected` and abort `step()`; the live loop in `cli.py`
then dies. Worse, if the entry succeeds but `on_position_opened`'s SLTP modify
fails (invalid stops, terminal busy), the runner logs and continues — the
position is live with **no disaster stop**, contradicting the module's safety
layer #4 (`mt5_broker.py:11-16`). Since `_round_price` may also produce an SL
inside the broker's `trade_stops_level`, this is a realistic path.

Fix: in `execute`, catch `MT5ClientError` and raise `OrderRejected` (chained)
after confirming that a lost-reply order did not fill (see B2). In the runner,
treat a failed `on_position_opened` as a hard failure: retry the SLTP modify,
and if it still fails, call `emergency_flatten()`/halt. Add
`result.retcode`-specific messages (`10016` invalid stops, `10018` market
closed, ...).

### M9. Feed unconditionally drops the rightmost bar, losing the last closed bar of every weekend/close

`live/feed.py:84-114`

```python
rows = self.client.copy_rates_from_pos(symbol, self.interval_minutes, start_pos=0, count=self.lookback)
...
# The rightmost row is the still-forming bar; drop it.
closed = rows[:-1]
```

`copy_rates_from_pos(..., 0, n)` returns bars ascending with index 0 = the
current period. During a weekend (or a symbol's session break) MT5 has no
"current" bar: index 0 is the last **closed** Friday bar, and it is dropped.
The final H2 bar of each week is therefore invisible until Monday's first poll
(and the last bar before any maintenance break is delayed similarly), so the
week-end signal/exit is processed at the open with a weekend gap — in the
wrong direction from a risk perspective.

Fix: drop the rightmost bar only when it is provably forming: compare its
`ts + interval_ms` with the current broker time, or ask for `start_pos=1,
count=n` (explicitly "one bar back") and use `symbol_info_tick().time_msc` to
decide whether index 0 is live. Also send `tick_volume`/`spread` so the
backtest can model spread.

### M10. The runner processes only `bars[-1]` per poll, discarding every intermediate closed bar

`live/feed.py:110-113` returns **all** new bars, `live/runner.py:249-254`:

```python
for symbol, bars in new_bars.items():
    if state.positions.get(symbol) is not None:
        self._process_bar(state, symbol, bars[-1], now, result)
    else:
        self._maybe_open(state, symbol, bars[-1], now, result)
```

Why wrong: after a restart, a slow step, or a weekend, `poll_new_bars()` can
return 2+ bars; only the newest is evaluated, so a stop/target that was hit on
an intermediate bar is missed and the strategy trades a bar the backtest would
have exited on. The feed's contract ("every closed bar strictly newer") is
contradicted by the caller.

Fix: loop `for bar in bars:` and process each in order (persist between bars on
crash-sensitive exits), or have the feed return only the newest bar and make the
gap explicit with a warning. The first is what the reference backtest/runner
parity requires.

### M11. Broker server time is treated as UTC everywhere (bars, deals, rollover hour, day rollover)

`mt5_server/server.py:226-236, 331-338` and `live/feed.py:36-50` /
`live/runner.py:226-239, 583-587`

```python
"ts": int(r["time"]) * 1000,                     # server.py:229 - MT5 bar time is SERVER time
since = dt.datetime.fromtimestamp(since_ms / 1000, tz=dt.timezone.utc)   # server.py:336
```

MT5 returns `rates["time"]` and deal `time` in the broker's **server** time
expressed as Unix seconds (typically UTC+2/+3), while `tick.time_msc` is true
UTC. The port compares them to `now = int(time.time()*1000)` (true UTC) in
`feed_is_stale`, `utc_day()` and the 21:00 rollover check, and passes a UTC
`datetime` into `history_deals_get`. Consequences: the daily guardrail day
boundary is 2-3h off broker midnight; `_overnight_swap` looks for 21:00 UTC but
`bar.ts` is server time (so it checks the wrong hour); the deal window edges
(`closed_since`/`history_deals_get`) are off by the broker offset near the
boundary; after a DST change the same wall-clock bar maps to shifted ids.

Fix: normalise once at the server boundary: read `terminal_info()`/
`symbol_info_tick().time` to derive `server_utc_offset`, or simply return bar
timestamps from `copy_rates` converted to true UTC (subtract the offset) and
document that all `ts` in the port are UTC. Use broker midnight, not UTC
midnight, for day rollover and rollover-hour detection. Add a startup check
comparing `rates["time"]` to `tick.time_msc` within one interval.

### M12. Position sizing uses `state.cash`, not broker equity, and never sees `margin_free`

Covered by B1/M4 but worth an explicit line because it is the sizing contract:
`runner.py:463` `equity = state.cash` (paper cash, not `account_info().equity`),
`open_risk` is computed in price units (`runner.py:472-479` multiplies
`pip_value_per_lot / pip`, which is correct, but the per-position qty is lots so
it happens to be right only because `pip_value_per_lot` is account currency per
pip — check the same code against B1's PnL fix), and `position_size` is called
without `free_margin`. Fix: pass broker equity and margin_free from
`account_info()` in live mode; keep `state.cash` for paper/reporting only.

### M13. Reconciliation safety layer is documented but not implemented

`broker/mt5_broker.py:5-16` claims layer 3 "Reconciliation before trading. On
start the broker reads the MT5 positions and refuses to trade if they disagree
with the local state." In practice `positions_snapshot()`
(`mt5_broker.py:416-432`), `actual_size()` (370-380), `protective_stop_price()`
(382-389) and `closed_since()` (391-414) are **never called** anywhere in the
live runner (`grep`: only definitions and the ABC). After a crash the local
state can hold a position the broker closed, or the broker can hold one the
state dropped; the bot will happily size a second one. Fix: implement the
documented startup reconciliation (state.positions vs `positions_snapshot()`
matched by ticket), and call `closed_since()` each bar to book real
`swap`/`commission`/exit price instead of the local crypto-unit approximations.

### M14. `InstrumentSpec.from_mt5` defaults hide missing/zero broker metadata; live ids are built before broker truth is available

`instrument.py:143-175` (`payload.get("trade_contract_size", 100_000.0)`,
`volume_step 0.01`, `volume_min 0.01`, `max_lot 100`, `swap 0`), and
`cli.py:146-157` constructs the broker (and thus its `InstrumentSpec` map) with
`_default_instrument()` first and only replaces it via the private attribute
`broker._instruments` afterwards. Any key the server forgets (see port gaps)
silently becomes a plausible-looking default, and the broker is briefly
constructed with synthetic specs. The class docstring says "this class refuses
to guess" — it does guess. Fix: require every key (`payload[...]`, no
`.get` defaults), validate positives (`point > 0`, `contract_size > 0`,
`volume_step > 0`, `min <= max`), and build the spec map before constructing
`MT5Broker`.

---

## Minor

1. **`order_check` never used** (`server.py` exposes none; `mt5_broker.py` never
   calls it). A preflight would catch invalid price/stops/volume/margin and
   return a readable retcode before the live order. Add
   `exposed_order_check(request)` and call it for the first N orders/symbol.
2. **Partial fills are booked as full** (`mt5_broker.py:236-258`): retcode
   `TRADE_RETCODE_DONE_PARTIAL (10010)` is accepted but `LiveFill.qty` is the
   requested `qty`, not `result["volume"]`; `fee` is always 0 and commission is
   never read. Use `result["volume"]`/`result["price"]`; read commission from
   `history_deals_get` after closing.
3. **`connect_timeout` is stored and never used** (`mt5_client.py:62, 73-81`);
   `rpyc.connect` has no timeout parameter in rpyc 6.0.2, so an unreachable
   host can block for the OS TCP timeout. Implement via `socket.create_connection`
   or a `ThreadPool` guard.
4. **`feed_is_stale` is dead code** (`feed.py:53-64`, imported in `runner.py:38`,
   never called). Nothing detects a silent feed; a stalled poll simply returns
   `{}` and the bot idles. Wire it into `step()` with a halt after
   `max_stale_mult` intervals, or remove it.
5. **`symbol_select` is never called** (`mt5_client.py:155-156` exists, no
   caller; `cli.py:137-157` goes straight to `symbol_info`). MT5 requires the
   symbol to be in Market Watch for `symbol_info`/`copy_rates_from_pos` to
   answer; the docs example calls `symbol_select` explicitly. Add it during
   `MT5Broker.initialize()` for every configured symbol and fail loudly if
   `False`.
6. **No stop-distance / freeze-level validation**: `InstrumentSpec.accepts`
   (`instrument.py:122-135`) checks only lot bounds; the server does not expose
   `trade_stops_level` / `trade_freeze_level` (`server.py:176-190`). A tight ATR
   stop can sit inside the broker's minimum distance -> SLTP modify rejected on
   every position (`10016` invalid stops) -> M8's naked-position path. Expose
   those fields and validate `abs(entry - stop) >= stops_level * point`.
7. **Dead retcode constants / no requote handling**: `TRADE_RETCODE_PRICE_CHANGED`
   (10021), `PRICE_OFF` (10015) are defined at `mt5_broker.py:52-56` and never
   used; `10004` (requote), `10006` (rejected) are not handled at all. Map them
   to a bounded refresh-and-retry for SL/TP modifies (idempotent) and to a
   clear `OrderRejected` message for entries.
8. **Reconnect does not re-run `initialize`**: the server's `_initialized`
   lives on the per-connection service instance (`server.py:78-80, 106-110`);
   after a client reconnect (new RPyC connection) or a server restart,
   `exposed_initialize` is never called again and MT5 calls can fail with
   "not initialized". Make the client call `initialize` after reconnect, or
   call `mt5.initialize()` lazily in `_ensure_mt5` when `terminal_info()` is
   None.
9. **`ThreadedServer` + MetaTrader5 thread-safety is unverified**
   (`server.py:375-384`): each connection is served on its own thread and all
   of them share the process-global MT5 terminal connection. The MT5 Python API
   is not documented as thread-safe. Either serialize MT5 calls behind a
   `threading.Lock` in the service or limit the server to one client.
10. **No auth throttling; default bind is `0.0.0.0`; no TLS** (`server.py:365-385,
    390-397`). The docs say "private network/VPN", but the default exposes the
    service on every interface. Add lockout/backoff after failed `auth()`, bind
    `127.0.0.1` by default, and support an SSH tunnel in docs.
11. **`positions_get`/`copy_rates` failures are silently `[]`** (`server.py:226-227,
    256-258`; `feed.py:102-103`): a dead symbol/session error is indistinguishable
    from "no positions/no new bars". Return `None` or raise and let the feed log
    "unavailable" (it already catches `MT5ClientError`).
12. **`InstrumentSpec.accepts` allows reduce-only below `min_lot`**
    (`instrument.py:129-135`): the reference kept the hard minimum quantity and
    only exempted the notional floor (`NornWeave/instrument.py:80-100`). MT5
    will reject a close below `volume_min` anyway, so this just turns a local
    validation error into a broker rejection loop. Distinguish
    "below min lot" from "below min notional" (there is no min-notional in FX).
13. **`pip_size` convention breaks for non-major symbols** (`instrument.py:42-50`):
    anything with digits not in (3,5) gets `pip = point`; XAUUSD (digits=2)
    conventionally uses a 0.1 pip. Fine for the 8 majors, wrong the moment a
    metal/index is added. Drive pip size from a symbol-class table, not digits.
14. **`expected_price` returns the stale reference price and ignores the
    spread** (`mt5_broker.py:169-174`); entries are sized on `bar.close`, not the
    ask, so risk is understated by the half-spread. Use `current_price()` (ask
    for buys) for sizing and keep the bar close only for the strategy.
15. **Dead/meaningless helpers**: `actual_size()` sums lots across *different*
    pairs (`mt5_broker.py:370-380`) — 0.1 EURUSD + 0.1 USDJPY is not 0.2 lots of
    anything; `round_lots_to_meet` (`instrument.py:94-108`) is never called and
    silently upgrades a below-min request to `min_lot` (i.e. multiplies risk);
    `MT5Position.matches`/`is_open` are unused. Either delete or document the
    single intended use.
16. **`history_deals_get` window is fixed at 30 days**
    (`mt5_broker.py:391-396`: `since_ms + 86_400_000 * 30`): after >30 days of
    downtime the catch-up booking silently misses deals between 30 days and
    now. Loop the window or clamp with logging.

Also: `server.py:83-89` docstring/comment says a bad secret "closes the
connection"; observed behaviour is that it raises and the connection stays open
and can retry forever. And `cli.py:218-226` `fetch` writes only bars (no
`spread`), so `typical_spread_points` comes from `symbol_info().spread` and can
go stale — acceptable, but note it.

---

## Port gaps (reference `D:\PROGRAMMING\NornWeave` -> port)

1. **Instrument metadata regression.** Reference `instrument.py` carried
   `tick_size`, `qty_step`, `min_qty`, `max_leverage`, `min_notional`,
   `funding_interval_minutes`. The port adds digits/point/pip/pip_value and swap
   but drops the broker-risk fields that matter for validation:
   `trade_stops_level`, `trade_freeze_level`, `trade_exemode`/`filling_mode`,
   `swap_mode`, `margin_initial`, `currency_profit`/`currency_margin`,
   `visible`, `volume_limit`. The server payload (`server.py:176-190`) must
   grow these before the broker can be correct.
2. **Live/paper instrument construction.** `cli._default_instrument`
   (`cli.py:118-134`) is the reference-era hardcoded spec with
   `pip_value_per_lot=10.0` — wrong for JPY pairs (USDJPY pip value is
   `1000/USDJPY` ≈ $6.7 at 150) and used by `backtest`/`demo` and for the
   broker's initial map. This directly contradicts `instrument.py:11-14`
   ("never hardcode $10"). Backtest PnL/position sizes for JPY pairs are
   therefore off by ~33% before any of the above.
3. **No persistent candle cache.** Reference `data/cache.py` merged Bybit REST
   pulls into a local cache so a restart had warmup. The port's feed is
   stateless: warmup relies entirely on MT5 returning `lookback` bars on the
   first poll, and `poll_new_bars` does not verify `len(rows) == lookback`
   (`feed.py:96-98`), so a short history silently truncates warmup.
4. **No exchange-booking reconciliation.** Reference runner used
   `closed_since()`/`exchange_booked_*` to book real fees/PNL per deal. The
   port defines the fields (`state.py:118-126`) and `closed_since`
   (`mt5_broker.py:391-414`) but never calls them (see M13); `swap_accrued`,
   `entry_fee`, `gross_pnl` are all local estimates with the unit bugs above.
5. **`OrderRequest` doc mismatch.** `live/broker.py:10-12` says reduce-only maps
   to `ORDER_TYPE_CLOSE_BY`; the implementation uses `TRADE_ACTION_DEAL` +
   `position` (`mt5_broker.py:222-234`). Fix the doc or implement CLOSE_BY
   (which requires an opposite position and is not generally correct here).
6. **Reference tests never ported.** `tests/test_instrument.py` and
   `tests/test_sizing.py` exist but are 0 bytes. The reference had real coverage
   for `round_qty`/`round_qty_to_meet`/`accepts` and sizing caps; the FX port has
   tests only in name, which is why B1/M4/M5 could land unnoticed.

---

## Recommended next steps (ordered)

1. **Fix the unit layer first (B1/M12).** Introduce an explicit
   `quote_to_account(symbol, price)` conversion (or `contract_size` +
   `order_calc_profit` from MT5) and route `ClosedTrade`, `LiveState.equity`,
   `runner._execute_exit`, daily-loss and sizing through it. Add the
   10-pip-EURUSD-$10 test and a USDJPY test. Do not run live until this passes.
2. **Make `order_send` non-retryable (B2)** and add post-timeout reconciliation
   (`positions_get` by magic + a durable `pending_order` record in state).
3. **Repair the order request (M1/M2/M3)**: include `price`, clamp volume before
   building the dict, take the ticket from the entry `order_send` result, check
   `margin_mode`, and choose `type_filling` from `filling_mode`.
4. **Fix the comment limit (M7)**: `_LINK_MAX = 31`, digest kept, no slice in the
   broker; add a collision test over the real exit purposes.
5. **Fix sizing caps (M4/M5)** and pass `free_margin`/`account_info` leverage
   from the runner; validate stops against `trade_stops_level`.
6. **Fix swap (M6)**: expose `swap_mode` + `swap_rollover3days`, convert
   units, correct the weekday mapping and the net-PnL sign, or book real swaps
   from history.
7. **Fix feed semantics (M9/M10/M11)**: never drop a closed bar, process every
   new bar, and normalise broker time to UTC once; wire `feed_is_stale`.
8. **Fix the paper path (B3)** and harden error mapping (M8): a failed
   `on_position_opened` must never leave a naked position.
9. Add the missing server fields (port gap 1), `symbol_select` at startup,
   `order_check` preflight, auth throttling/loopback bind, and retcode-specific
   messages.
10. Before any demo-account canary: run a 1-symbol, 1-lot manual test that
    verifies `order_send` (price included), SL/TP placement, partial close,
    swap/commission from `history_deals_get`, and restart reconciliation.

---

### Evidence / citations

* MQL5 Python `order_send` reference (price requirement for non-Market
  execution, request fields, example with `price`, `sl`, `tp`):
  [mql5.com/en/docs/python_metatrader5/mt5ordersend_py](https://www.mql5.com/en/docs/python_metatrader5/mt5ordersend_py)
* MT5 `symbol_info` fields (`swap_rollover3days`, `swap_mode`,
  `trade_stops_level`, `trade_exemode`, `filling_mode`) and
  `ENUM_SYMBOL_SWAP_MODE`/`ENUM_DAY_OF_WEEK` semantics: MQL5 reference
  (`symbol_info`, `marketinfoconstants`).
* Runtime RPyC auth exposure test (rpyc 6.0.2, in-process server on
  `127.0.0.1:18899`) as described under Method; source `server.py:83-103,
  375-385` and `mt5_client.py:69-90`.
* Reference behaviour for id-length/collision and min-quantity rules:
  `D:\PROGRAMMING\NornWeave\nornweave\live\state.py:105-144`,
  `nornweave\instrument.py:80-100`, `nornweave\risk\sizing.py:76-103`.

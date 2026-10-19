# NornWeaveForex porting plan

Source of truth: `d:\PROGRAMMING\NornWeave`. Nothing there is modified.

## Done

- [x] Scaffold project layout
- [x] `pyproject.toml` with RPyC + MetaTrader5 extras
- [x] Port core strategy modules `strategy/`, `indicators/`, `features.py`,
      `types.py`, `execution.py`, `config.py`
      — verified: identical to source after the package-import rename only
      (`types`, `features`, `indicators/core`, `indicators/swings` are byte-identical)
- [x] Rewrite `instrument.py` for Forex: digits, point, pip, lot step, contract size,
      swap, `quote_currency`, `swap_in_points`; account-currency money helpers
      `pnl_money`, `notional_account`, `swap_money`; offline `default_instrument`
- [x] Rewrite `risk/sizing.py`: lot-based sizing with per-pair pip value, portfolio
      open-risk cap, real margin cap (`notional_account / leverage`, `free_margin`
      when supplied, `equity * leverage` fallback), min-lot bump caps
- [x] Build `mt5_server/server.py`: RPyC service wrapping `MetaTrader5` with
      shared-secret auth, symbol metadata (incl. `swap_rollover3days`, `swap_mode`,
      `filling_mode`, `trade_stops_level`, `margin_mode`), order/history APIs,
      `order_check` preflight
- [x] Build `broker/mt5_client.py`: RPyC client, retry only on read-only calls,
      mutations at-most-once, timeouts/reconnect, deterministic order ids
- [x] Build `broker/mt5_broker.py`: `price` in orders, volume clamped before send,
      ticket from `order_send`, hedge/netting check, filling fallback, SL/TP hooks
- [x] Port `live/runner.py` to multi-symbol: 8 majors, up to 2 slots, portfolio risk
      cap, real `IndicatorSet.context_at` (Dow + daily + tuning), Forex money,
      ladder + sizing before the order, partial-fill booking, BE stop move
- [x] Port `backtest/engine.py` with spread model (per-pair typical spread, both
      sides), swap model (`swap_money`, Wednesday triple via Python weekday),
      next-bar-open fills, per-symbol mark-to-market, trend-reversal exit,
      end-of-data settlement
- [x] Fixed 8 majors list + spread screener (`screener.py`)
- [x] Port `cli.py`: `presets`, `fetch`, `backtest` (cached NPZ or synthetic),
      `live` (`--paper`, `--once`), `demo`; `configs/forex.yaml` added
- [x] Port `live/state.py`: schema v2 with Forex money metadata, atomic `fsync` save
      (creates parent dir, `0600`), quarantine, deterministic client ids capped at
      MT5's 31-char comment; legacy schema-less files load as v1
- [x] Live runner hardening: hook-failure escalation, fail-safe flatten + halt on a
      failed protective order, halts persisted immediately, every polled bar
      processed with persisted cursor, capped history, VWAP exit / `bars_held`
- [x] `docs/REVIEW.md` + four area audits + fix records in `docs/review/`
- [x] Tests (260 passing): `test_execution.py` (30, verbatim), `test_instrument.py`
      (13), `test_state_money.py` (15), `test_state_schema.py` (35),
      `test_config.py` (51), `test_imports.py` (45), `test_paper_broker.py` (7),
      `test_sizing.py` (18), `test_mt5_broker.py` (22),
      `test_backtest_engine.py` (9), `test_synthetic.py` (9),
      `test_live_runner.py` (5)

## In progress / next

- [ ] Live reconciliation and startup safety: call `positions_snapshot`,
      `closed_since`, `actual_size`, `protective_stop_price`; refuse to trade on a
      local/flat-vs-broker mismatch; re-attach tickets on restart; book the
      fail-safe flatten round trip from deal history
- [ ] Feed/session semantics: wire `feed_is_stale`, replay pending bars after a
      restart, broker-server-time → UTC, weekend/session gaps in the backtester
- [ ] Wire the decorative rails: `slots.py`, `notify.py`, notional guardrail,
      `order_check` preflight, `filling_mode` seeding, broker-authoritative swap
- [ ] Port `dashboard/` (104 / 948 lines): overview, marks, health, chart, strategy
- [ ] Parity extras: `exit_order: exits_first` in the backtester, remaining
      source tests (26 files → 12 ported so far), CI (`.[mt5server,dashboard,dev]`)
- [ ] Clean up the strategy files' unused imports only if the byte-identity with the
      source is deliberately dropped (currently kept identical on purpose)

## Open design questions

1. **Pip value.** Resolved: `InstrumentSpec.pip_value_per_lot` comes from MT5's
   `SYMBOL_TRADE_TICK_VALUE`/`SYMBOL_TRADE_TICK_SIZE`; offline code derives it from
   the quote currency and reference USD rates. Never hardcoded to $10.
2. **Triple swap.** Resolved representation: MT5's `SYMBOL_SWAP_ROLLOVER3DAYS`
   (Sunday = 0) is converted to a Python weekday (Wednesday = 2); the engine compares
   `bar.weekday()` against it. `swap_mode = POINTS` is converted to account currency.
3. **Session gaps.** Still open: the live feed skips missing bars, but the backtester
   does not yet model weekend gaps beyond what the data contains; needs explicit
   session handling and Monday-gap stop fills.
4. **Hedge vs netting.** The broker now reads `account_info().margin_mode` and refuses
   netting/exchange accounts for this ladder model. Confirm Alfa-Forex demo/live is
   hedging before going live.
5. **Magic number.** Implemented (`MAGIC = 20261018`); all bot orders carry it and
   reconciliation filters by it.
6. **Account currency.** Money PnL goes through the broker's tick value, so a
   non-USD account works; the offline `default_instrument` reference rates assume USD.

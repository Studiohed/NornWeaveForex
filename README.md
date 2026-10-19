# NornWeaveForex

Forex port of [NornWeave](../NornWeave) — the FEHU/URUZ/THURISAZ strategy running
on major currency pairs through MetaTrader 5 at Alfa-Forex.

## Architecture

The strategy code (EMA/Dow/ADX/Fibonacci) is broker-agnostic and is ported
unchanged. What changes is the plumbing: Forex positions live in lots, sizing is
in pips, and the broker speaks MetaTrader 5, not Bybit V5.

```
┌──────────────────────────┐         ┌─────────────────────────────┐
│ Linux VPS                │  RPyC   │ Windows machine             │
│  nornweave-forex live    │ ──────► │  MetaTrader 5 terminal      │
│  (strategy, sizing,      │  TCP    │  + nornweave-mt5-server     │
│   portfolio, state)      │         │  (MetaTrader5 Python API)   │
└──────────────────────────┘         └─────────────────────────────┘
```

## Instruments

Fixed list of 8 major pairs: EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF,
NZDUSD, EURJPY. Up to **2 positions open at once**, portfolio risk is capped the
same way as in the crypto version.

## Layout

* `nornweave_forex/strategy/`, `indicators/`, `execution.py`, `features.py`,
  `types.py` — ported from NornWeave unchanged.
* `nornweave_forex/instrument.py` — Forex symbol spec (digits, point, lot step,
  contract size, swap) and the account-currency money helpers.
* `nornweave_forex/risk/sizing.py` — lot-based sizing with pip value and margin cap.
* `nornweave_forex/mt5_server/` — Windows-side RPyC service wrapping
  `MetaTrader5`.
* `nornweave_forex/broker/` — Linux-side RPyC client implementing the broker
  interface.
* `nornweave_forex/live/` — runner, feed, state, slots, notify.
* `nornweave_forex/backtest/` — engine with spread and swap models.
* `configs/forex.yaml` — shipped live/backtest configuration.
* `deploy/` — Linux systemd installer, Windows MT5-server installer, Docker.
* `tests/` — pytest suite (260 tests).
* `docs/` — `REVIEW.md`, `PLAN.md` and the area audits under `docs/review/`.

## Install

```bash
python -m venv .venv
source .venv/bin/activate       # Linux/macOS
# .venv\Scripts\activate        # Windows
pip install -e ".[dev]"
pytest -q
```

Console scripts after install: `nornweave-forex` (bot / CLI) and
`nornweave-mt5-server` (Windows MT5 service). Everything also runs as
`python -m nornweave_forex`.

Smoke test without network or MT5 (synthetic H2 candles):

```bash
nornweave-forex demo
nornweave-forex backtest --config configs/forex.yaml
nornweave-forex --help
```

## Deployment

Production runs on two machines: the bot on Linux, the MT5 terminal + RPyC
server on Windows. Step-by-step instructions, systemd unit, Dockerfile and a
security checklist are in [`deploy/README.md`](deploy/README.md).

```bash
# Linux bot (systemd)
sudo ./deploy/linux/install.sh

# Windows MT5 server (PowerShell as Administrator)
.\deploy\windows\install-mt5-server.ps1

# Docker alternative for the Linux bot
docker compose up -d --build
```

## Status

The offline stack and the paper-mode runner are runnable and tested
(`nornweave-forex demo`). The MT5 path is hardened but still needs broker
reconciliation, fail-safe PnL booking and session-gap modelling before real
money — see `docs/REVIEW.md` for the full assessment and `docs/PLAN.md` for the
remaining checklist.


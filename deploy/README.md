# Deployment

NornWeaveForex is split across two machines because `MetaTrader5` only runs on
Windows:

```
┌──────────────────────────────┐          RPyC/TCP           ┌──────────────────────────────┐
│ Linux VPS / Docker           │  ───────────────────────►   │ Windows machine              │
│  nornweave-forex live        │  MT5_HOST:MT5_PORT          │  MetaTrader 5 terminal       │
│  strategy, sizing, state     │                             │  nornweave-mt5-server        │
└──────────────────────────────┘                             └──────────────────────────────┘
```

* **Linux side** — the strategy, risk, state and the RPyC client. This is the
  process that decides to trade. It never talks to a broker SDK directly.
* **Windows side** — a thin RPyC service wrapping the `MetaTrader5` Python API.
  It executes what the Linux process asks for and returns account/position data.

Keep the RPyC port on a private LAN, WireGuard/Tailscale or an SSH tunnel. The
shared secret is a speed bump, not transport encryption; RPyC itself is not TLS.

---

## 1. Linux bot (systemd)

Prerequisites: Linux with systemd, Python 3.11+ with the `venv` module
(`python3-venv` on Debian/Ubuntu), `tar`, `openssl`.

```bash
git clone <this repo> NornWeaveForex
cd NornWeaveForex
sudo ./deploy/linux/install.sh
```

The installer is idempotent and does:

| Step | Result |
|---|---|
| service account | system user `nornweave` |
| sources | synced to `/opt/nornweave-forex` (no `.git`, `.venv`, state) |
| application | venv + `pip install /opt/nornweave-forex` |
| config | `/etc/nornweave/forex.yaml` (`0640`, only created if absent) |
| environment | `/etc/nornweave/nornweave.env`, random RPyC secret generated |
| service | `nornweave-forex.service`, enabled (not started) |

Edit the two files, then smoke-test and start:

```bash
sudo nano /etc/nornweave/nornweave.env      # MT5_HOST + secret + credentials
sudo nano /etc/nornweave/forex.yaml         # symbols / risk / max_positions

# One poll, paper mode, no orders:
sudo -u nornweave /opt/nornweave-forex/.venv/bin/nornweave-forex \
  live --config /etc/nornweave/forex.yaml \
       --state /var/lib/nornweave/live.json --paper --once

sudo systemctl start nornweave-forex
sudo journalctl -u nornweave-forex -f
```

Notes:

* `WorkingDirectory=/var/lib/nornweave`, so the default `KILL_SWITCH` file is
  `/var/lib/nornweave/KILL_SWITCH` and state is `/var/lib/nornweave/live.json`.
* `ProtectSystem=strict` makes everything except the state directory read-only.
* `SIGTERM` (used by `systemctl stop` and Docker) is handled by the CLI: it saves
  state before exiting.
* Upgrade: `cd /opt/nornweave-forex && sudo git pull` is **not** used — pull in
  your checkout and re-run `sudo ./deploy/linux/install.sh`, then
  `sudo systemctl restart nornweave-forex`.

## 2. Windows MT5 server

Prerequisites: Windows with MetaTrader 5 installed and logged in (demo first!),
Python 3.11+ on `PATH`, PowerShell as Administrator.

```powershell
git clone <this repo> C:\src\NornWeaveForex
cd C:\src\NornWeaveForex
.\deploy\windows\install-mt5-server.ps1
```

The script:

1. copies the checkout to `C:\NornWeaveForex`;
2. creates `.venv` and installs the package with the `[mt5server]` extra
   (`MetaTrader5` + `rpyc`);
3. generates a random `NORNWEAVE_MT5_SECRET` and stores it at **machine** scope;
4. opens TCP `18813` for the **private** firewall profile only;
5. registers the scheduled task **NornWeave MT5 Server** (runs as `SYSTEM` at
   startup, restarts on failure) and starts it.

It prints the generated secret once. Put the same value in
`/etc/nornweave/nornweave.env` on the Linux box.

Verify:

```powershell
Get-ScheduledTask -TaskName "NornWeave MT5 Server" | Format-List State
Test-NetConnection -ComputerName <windows-ip> -Port 18813
```

The Windows account that runs MT5 must keep the terminal logged in. If the
terminal is opened by a different user, the service may see a different
`MetaTrader5` session or none at all — run the task under the account that owns
the terminal, or let the service log in with `MT5_DEMO_LOGIN`/`MT5_PASSWORD`/
`MT5_SERVER`.

## 3. Docker (Linux alternative)

```bash
cp .env.example .env     # set MT5_HOST, NORNWEAVE_MT5_SECRET, credentials
docker compose up -d --build
docker compose logs -f
```

State lives in the `nornweave-state` volume (`/var/lib/nornweave`); the config is
mounted read-only from `configs/forex.yaml`. The image does **not** include
MetaTrader 5.

## Configuration

| File | Purpose |
|---|---|
| `configs/forex.yaml` | strategy, risk, portfolio (8 majors, max 2 positions) |
| `.env` (repo root) | local Docker/dev MT5 connection settings |
| `/etc/nornweave/nornweave.env` | systemd environment (MT5 host/secret/credentials) |
| `state/live.json` | persisted positions, trades, executed order ids |

Never commit `.env` or `nornweave.env`; both are git-ignored / installed with
`0640`.

## Operations

```bash
# status / logs
systemctl status nornweave-forex
journalctl -u nornweave-forex -n 200 --no-pager

# stop trading immediately (idempotent; state is preserved)
sudo touch /var/lib/nornweave/KILL_SWITCH
sudo systemctl stop nornweave-forex

# resume: remove the kill switch, inspect logs, start again
sudo rm /var/lib/nornweave/KILL_SWITCH
sudo systemctl start nornweave-forex

# back up state before any risky operation
sudo cp /var/lib/nornweave/live.json /var/lib/nornweave/live.json.bak

# paper mode end-to-end (no orders, still polls MT5)
sudo -u nornweave /opt/nornweave-forex/.venv/bin/nornweave-forex \
  live --config /etc/nornweave/forex.yaml \
       --state /var/lib/nornweave/live.json --paper
```

## Security checklist

* [ ] RPyC port reachable only from the bot host (VPN/firewall/SSH tunnel).
* [ ] Distinct long `NORNWEAVE_MT5_SECRET` on both sides; no default creds.
* [ ] Demo account and `--paper` smoke test before live credentials.
* [ ] `KILL_SWITCH` procedure tested once.
* [ ] State file backed up; disk has room for logs/state.
* [ ] MT5 terminal on a UPS/reliable host — if the terminal dies, the bot cannot
      manage open positions (broker-side disaster stop is the last line).

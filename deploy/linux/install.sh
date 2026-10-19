#!/usr/bin/env bash
# Install the NornWeaveForex Linux-side bot (strategy + RPyC client) as a
# systemd service. The MetaTrader 5 terminal and the RPyC server live on a
# Windows machine — see deploy/windows/install-mt5-server.ps1.
#
# Usage (from a checkout, as root):
#   sudo ./deploy/linux/install.sh
#   sudo ./deploy/linux/install.sh --no-enable   # install files, don't enable
#
# Everything is idempotent: re-run after `git pull` to upgrade the package.
set -euo pipefail

APP_NAME="nornweave-forex"
APP_USER="${APP_USER:-nornweave}"
APP_DIR="${APP_DIR:-/opt/nornweave-forex}"
CONF_DIR="${CONF_DIR:-/etc/nornweave}"
STATE_DIR="${STATE_DIR:-/var/lib/nornweave}"
SERVICE="${SERVICE:-nornweave-forex}"
PYTHON="${PYTHON:-python3}"
ENABLE=1

for arg in "$@"; do
  case "$arg" in
    --no-enable) ENABLE=0 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

log() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[[ "${EUID}" -eq 0 ]] || die "run as root (sudo)"
[[ -f "${REPO_DIR}/pyproject.toml" ]] || die "cannot find the checkout at ${REPO_DIR}"
command -v "${PYTHON}" >/dev/null || die "${PYTHON} not found; install Python 3.11+"
"${PYTHON}" -c 'import venv' >/dev/null 2>&1 \
  || die "${PYTHON} has no venv module; install the python3-venv package"

log "checkout: ${REPO_DIR}"
log "install:  ${APP_DIR} (user ${APP_USER}), config ${CONF_DIR}, state ${STATE_DIR}"

# 1) Service account and directories.
if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  log "creating system user ${APP_USER}"
  useradd --system --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${APP_USER}"
fi
install -d -m 0755 "${APP_DIR}" "${CONF_DIR}" "${STATE_DIR}"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}" "${STATE_DIR}"

# 2) Sync the checkout into APP_DIR (never copy .git, venv, state or caches).
log "syncing sources"
tar -C "${REPO_DIR}" -cf - \
  --exclude=.git --exclude=.venv --exclude=.venv-pkgtest \
  --exclude=state --exclude=dist --exclude=build \
  --exclude=__pycache__ --exclude='*.pyc' --exclude=.pytest_cache \
  . | tar -C "${APP_DIR}" -xf -

# 3) Virtualenv + package.
if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
  log "creating virtualenv"
  "${PYTHON}" -m venv "${APP_DIR}/.venv"
fi
log "installing the package"
"${APP_DIR}/.venv/bin/python" -m pip install --quiet --upgrade pip setuptools wheel
"${APP_DIR}/.venv/bin/python" -m pip install --quiet --upgrade "${APP_DIR}"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

# 4) Config and environment (never overwrite an operator's file).
if [[ ! -f "${CONF_DIR}/forex.yaml" ]]; then
  log "installing config -> ${CONF_DIR}/forex.yaml"
  install -m 0640 -o root -g "${APP_USER}" \
    "${APP_DIR}/configs/forex.yaml" "${CONF_DIR}/forex.yaml"
else
  log "keeping existing ${CONF_DIR}/forex.yaml"
fi

if [[ ! -f "${CONF_DIR}/nornweave.env" ]]; then
  log "installing environment -> ${CONF_DIR}/nornweave.env"
  install -m 0640 -o root -g "${APP_USER}" \
    "${APP_DIR}/deploy/linux/nornweave.env.example" "${CONF_DIR}/nornweave.env"
  # Generate the shared RPyC secret instead of shipping the placeholder.
  if command -v openssl >/dev/null 2>&1; then
    secret="$(openssl rand -hex 32)"
    sed -i "s|^NORNWEAVE_MT5_SECRET=.*|NORNWEAVE_MT5_SECRET=${secret}|" \
      "${CONF_DIR}/nornweave.env"
    log "generated a random NORNWEAVE_MT5_SECRET"
  fi
else
  log "keeping existing ${CONF_DIR}/nornweave.env"
fi

# 5) systemd unit.
log "installing ${SERVICE}.service"
install -m 0644 "${APP_DIR}/deploy/linux/${SERVICE}.service" \
  "/etc/systemd/system/${SERVICE}.service"
systemctl daemon-reload
if [[ "${ENABLE}" -eq 1 ]]; then
  systemctl enable "${SERVICE}.service"
fi

cat <<EOF

Installed.

Next steps:
  1. Edit ${CONF_DIR}/nornweave.env:
       MT5_HOST=<windows-lan-ip>       # where nornweave-mt5-server runs
       NORNWEAVE_MT5_SECRET=<same value as on the Windows machine>
       MT5_DEMO_LOGIN / MT5_PASSWORD / MT5_SERVER (for live, non-paper mode)
  2. Review ${CONF_DIR}/forex.yaml (symbols, risk, max_positions).
  3. Smoke-test one poll without trading:
       sudo -u ${APP_USER} ${APP_DIR}/.venv/bin/nornweave-forex \\
         live --config ${CONF_DIR}/forex.yaml \\
              --state ${STATE_DIR}/live.json --paper --once
  4. Start and follow logs:
       sudo systemctl start ${SERVICE}
       sudo journalctl -u ${SERVICE} -f
  5. Emergency stop (also cancels all future actions, state is preserved):
       sudo touch ${STATE_DIR}/KILL_SWITCH
       sudo systemctl stop ${SERVICE}
EOF

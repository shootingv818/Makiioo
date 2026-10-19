#!/usr/bin/env bash
# =========================================================================== #
# install.sh — one-command installer for the Rubika Panel master server.
#
# What it does (idempotent — safe to re-run):
#   1. installs OS packages (python 3.11+, pip, venv, git, build tools)
#   2. clones (or fast-forwards) the repo into /opt/rubika-panel
#   3. creates a virtualenv and installs requirements.txt
#   4. collects your settings and writes .env (keeps an existing one)
#   5. generates WORKER_SECRET (Fernet key) if missing
#   6. installs and enables a systemd service "rubika-panel"
#   7. starts the service and shows status + management commands
#
# Usage on a fresh Ubuntu/Debian server:
#
#   # Option A — one line (bootstrap + install):
#   curl -fsSL https://raw.githubusercontent.com/shootingv818/Makiioo/refactor/rubika-only-structure/install.sh \
#     | sudo bash
#
#   # Option B — clone first, then run from inside the repo:
#   sudo apt-get update && sudo apt-get install -y git
#   git clone -b refactor/rubika-only-structure https://github.com/shootingv818/Makiioo.git
#   cd Makiioo && sudo bash install.sh
#
# Non-interactive install (CI / scripted):
#
#   sudo API_ID=... API_HASH=... BOT_TOKEN=... OWNER_ID=... LOG_GROUP_ID=... \
#        NON_INTERACTIVE=1 bash install.sh
# =========================================================================== #
set -euo pipefail

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
REPO_URL="${REPO_URL:-https://github.com/shootingv818/Makiioo.git}"
REPO_BRANCH="${REPO_BRANCH:-refactor/rubika-only-structure}"
INSTALL_DIR="${INSTALL_DIR:-/opt/rubika-panel}"
SERVICE_NAME="${SERVICE_NAME:-rubika-panel}"
RUN_USER="${RUN_USER:-root}"      # user to own the install and run the service
NON_INTERACTIVE="${NON_INTERACTIVE:-0}"

# --------------------------------------------------------------------------- #
# UI helpers
# --------------------------------------------------------------------------- #
c_bold()   { printf "\033[1m%s\033[0m\n" "$*"; }
c_green()  { printf "\033[32m%s\033[0m\n" "$*"; }
c_yellow() { printf "\033[33m%s\033[0m\n" "$*"; }
c_red()    { printf "\033[31m%s\033[0m\n" "$*" >&2; }
c_blue()   { printf "\033[36m%s\033[0m\n" "$*"; }
line()     { printf '%s\n' "----------------------------------------------------------"; }
step()     { echo; c_blue "▶ $*"; line; }

die() { c_red "✗ $*"; exit 1; }

# --------------------------------------------------------------------------- #
# Pre-flight
# --------------------------------------------------------------------------- #
line
c_bold "🚀 Rubika Panel — installer"
echo "   repo    : $REPO_URL"
echo "   branch  : $REPO_BRANCH"
echo "   target  : $INSTALL_DIR"
echo "   service : $SERVICE_NAME"
line

[ "$(id -u)" -eq 0 ] || die "Run as root (use: sudo bash install.sh)."

# Detect distro
if ! [ -f /etc/os-release ]; then die "Cannot detect OS (missing /etc/os-release)."; fi
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}" in
    ubuntu|debian) ;;
    *) c_yellow "⚠ untested distro '${ID:-unknown}', continuing with apt-get anyway" ;;
esac
command -v apt-get >/dev/null 2>&1 || die "apt-get not found; this installer targets Debian/Ubuntu."

# --------------------------------------------------------------------------- #
# 1) OS packages
# --------------------------------------------------------------------------- #
step "1/7  installing OS packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip python3-dev \
    git curl ca-certificates \
    build-essential libffi-dev libssl-dev \
    tzdata
c_green "✓ OS packages installed"

PY_BIN="$(command -v python3)"
PY_VERSION="$($PY_BIN -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
echo "   python  : $PY_BIN (v$PY_VERSION)"
# Require 3.9+
if ! $PY_BIN -c 'import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)'; then
    die "Python 3.9+ required (found $PY_VERSION)."
fi

# --------------------------------------------------------------------------- #
# 2) Clone or fast-forward the repo
# --------------------------------------------------------------------------- #
step "2/7  fetching source into $INSTALL_DIR"
if [ -d "$INSTALL_DIR/.git" ]; then
    c_yellow "repo already present — fetching latest from $REPO_BRANCH"
    git -C "$INSTALL_DIR" remote set-url origin "$REPO_URL"
    git -C "$INSTALL_DIR" fetch --depth 1 origin "$REPO_BRANCH"
    git -C "$INSTALL_DIR" checkout -B "$REPO_BRANCH" "origin/$REPO_BRANCH"
elif [ -f "$(dirname "$(readlink -f "$0")")/main.py" ] \
     && [ -z "${FORCE_CLONE:-}" ]; then
    # running from inside an already-cloned repo — reuse it in place
    SRC_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
    if [ "$SRC_DIR" != "$INSTALL_DIR" ]; then
        c_yellow "copying local checkout from $SRC_DIR to $INSTALL_DIR"
        mkdir -p "$INSTALL_DIR"
        rsync -a --delete \
            --exclude venv --exclude __pycache__ --exclude '*/__pycache__' \
            --exclude .git \
            "$SRC_DIR"/ "$INSTALL_DIR"/
        if [ -d "$SRC_DIR/.git" ]; then
            # also take the .git so updates can pull
            rsync -a --delete "$SRC_DIR/.git" "$INSTALL_DIR/"
        fi
    else
        c_green "already inside $INSTALL_DIR, nothing to copy"
    fi
else
    c_yellow "cloning $REPO_URL (branch $REPO_BRANCH)"
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi
c_green "✓ source ready at $INSTALL_DIR"

# --------------------------------------------------------------------------- #
# 3) Virtualenv + Python deps
# --------------------------------------------------------------------------- #
step "3/7  creating virtualenv + installing Python packages"
cd "$INSTALL_DIR"

if [ ! -x "$INSTALL_DIR/venv/bin/python" ]; then
    "$PY_BIN" -m venv "$INSTALL_DIR/venv"
fi
# shellcheck disable=SC1091
source "$INSTALL_DIR/venv/bin/activate"

pip install --upgrade --quiet pip
pip install --quiet --retries 5 --timeout 120 -r "$INSTALL_DIR/requirements.txt"
c_green "✓ Python dependencies installed into venv"

# --------------------------------------------------------------------------- #
# 4) .env — collect settings
# --------------------------------------------------------------------------- #
step "4/7  configuring .env"

ENV_FILE="$INSTALL_DIR/.env"

prompt_var() {
    # prompt_var VARNAME "Prompt text" [default]
    local __name="$1" __text="$2" __default="${3:-}" __value=""
    # already set in env (from the shell / CI)?
    if [ -n "${!__name:-}" ]; then
        printf -v "$__name" '%s' "${!__name}"
        return 0
    fi
    if [ "$NON_INTERACTIVE" = "1" ]; then
        [ -n "$__default" ] || die "Missing required value: $__name (NON_INTERACTIVE=1)"
        printf -v "$__name" '%s' "$__default"
        return 0
    fi
    local __suffix=""
    [ -n "$__default" ] && __suffix=" [$__default]"
    read -r -p "  $__text$__suffix : " __value
    [ -z "$__value" ] && __value="$__default"
    printf -v "$__name" '%s' "$__value"
}

if [ -f "$ENV_FILE" ]; then
    c_yellow "ℹ .env already exists — keeping it (delete it to reconfigure)"
else
    echo
    c_bold "Fill in your Telegram control-bot settings:"
    echo "  (API_ID / API_HASH from https://my.telegram.org,"
    echo "   BOT_TOKEN from @BotFather, OWNER_ID from @userinfobot,"
    echo "   LOG_GROUP_ID = numeric id of a group where the bot is a member)"
    echo
    prompt_var API_ID        "API_ID"
    prompt_var API_HASH      "API_HASH"
    prompt_var BOT_TOKEN     "BOT_TOKEN"
    prompt_var OWNER_ID      "OWNER_ID (numeric)"
    # In non-interactive mode a missing LOG_GROUP_ID silently falls back to
    # OWNER_ID so the bot DMs you its log cards (handy for first-boot tests).
    if [ "$NON_INTERACTIVE" = "1" ] && [ -z "${LOG_GROUP_ID:-}" ]; then
        LOG_GROUP_ID="$OWNER_ID"
        c_yellow "ℹ LOG_GROUP_ID not set → using OWNER_ID ($OWNER_ID); log cards will arrive in your bot DM"
    else
        prompt_var LOG_GROUP_ID "LOG_GROUP_ID (numeric, usually negative; press Enter to DM $OWNER_ID instead)" "$OWNER_ID"
    fi

    for req in API_ID API_HASH BOT_TOKEN OWNER_ID LOG_GROUP_ID; do
        [ -n "${!req}" ] || die "$req cannot be empty"
    done

    echo
    c_bold "Optional (press Enter to accept defaults):"
    prompt_var TIMEZONE      "TIMEZONE"              "Asia/Tehran"
    prompt_var SEND_DELAY    "SEND_DELAY (0.2..10s)" "1.0"
    prompt_var MAX_ERRORS    "MAX_ERRORS"            "5"
    prompt_var FORWARD_MARKER "FORWARD_MARKER"        "کد135"
    prompt_var PORTAL_ENABLED "PORTAL_ENABLED (0 to disable the web login portal)" "1"

    # generate WORKER_SECRET (Fernet key) — required before you can add workers
    WORKER_SECRET="$("$INSTALL_DIR/venv/bin/python" -c \
        'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

    cat > "$ENV_FILE" <<EOF
# ---- Telegram (control panel) ----
API_ID=$API_ID
API_HASH=$API_HASH
BOT_TOKEN=$BOT_TOKEN
OWNER_ID=$OWNER_ID
LOG_GROUP_ID=$LOG_GROUP_ID

# ---- Sending behaviour ----
SEND_DELAY=$SEND_DELAY
FORWARD_MARKER=$FORWARD_MARKER
MAX_ERRORS=$MAX_ERRORS
VERSION=rubika-panel

# ---- Worker subsystem ----
MODE=master
WORKER_SECRET=$WORKER_SECRET
MASTER_AS_WORKER=true

# tell newly provisioned workers which repo/branch to clone
GIT_REPO_URL=$REPO_URL
GIT_BRANCH=$REPO_BRANCH

WORKER_API_PORT=8765
WORKER_BIND_HOST=0.0.0.0
WORKER_API_TOKEN=

HEALTH_URL=https://upmessenger490.iranlms.ir/UploadFile.ashx
HEALTH_INTERVAL=1800
PING_GREEN_MS=800
PING_YELLOW_MS=2000

TIMEZONE=$TIMEZONE

# ---- Portal (web login) ----
PORTAL_ENABLED=$PORTAL_ENABLED
PORTAL_PORT=8080
PORTAL_TTL=300
MAX_PORTAL_LOGINS=5
PORTAL_MODE=quick
EOF
    chmod 600 "$ENV_FILE"
    c_green "✓ .env written (chmod 600)"
fi

mkdir -p "$INSTALL_DIR/data"
chown -R "$RUN_USER:$RUN_USER" "$INSTALL_DIR" 2>/dev/null || true

# --------------------------------------------------------------------------- #
# 5) systemd service
# --------------------------------------------------------------------------- #
step "5/7  installing systemd service: $SERVICE_NAME"

SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
cat > "$SERVICE_PATH" <<EOF
[Unit]
Description=Rubika Panel — master
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/main.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
# Reasonable soft limits for a long-running Python service
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
chmod 644 "$SERVICE_PATH"
systemctl daemon-reload
c_green "✓ service file created at $SERVICE_PATH"

# --------------------------------------------------------------------------- #
# 6) Start / restart the service
# --------------------------------------------------------------------------- #
step "6/7  starting $SERVICE_NAME"
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
if systemctl is-active --quiet "$SERVICE_NAME"; then
    systemctl restart "$SERVICE_NAME"
    c_yellow "service was running — restarted"
else
    systemctl start "$SERVICE_NAME"
fi
sleep 2

# --------------------------------------------------------------------------- #
# 7) Verify + print management commands
# --------------------------------------------------------------------------- #
step "7/7  verifying"
if systemctl is-active --quiet "$SERVICE_NAME"; then
    c_green "✓ $SERVICE_NAME is active"
    systemctl --no-pager --lines=0 status "$SERVICE_NAME" || true
else
    c_red "✗ $SERVICE_NAME failed to start"
    echo
    c_yellow "last 40 log lines:"
    journalctl -u "$SERVICE_NAME" --no-pager --lines=40 || true
    exit 1
fi

line
c_green "🎉 install complete."
echo
c_bold "Management:"
echo "   logs (live) : journalctl -u $SERVICE_NAME -f"
echo "   logs (tail) : journalctl -u $SERVICE_NAME --no-pager -n 100"
echo "   restart     : systemctl restart $SERVICE_NAME"
echo "   stop        : systemctl stop $SERVICE_NAME"
echo "   status      : systemctl status $SERVICE_NAME"
echo
c_bold "Update to latest commit:"
echo "   cd $INSTALL_DIR"
echo "   sudo git fetch origin $REPO_BRANCH && sudo git reset --hard origin/$REPO_BRANCH"
echo "   sudo $INSTALL_DIR/venv/bin/pip install -r requirements.txt"
echo "   sudo systemctl restart $SERVICE_NAME"
echo
c_bold "Reconfigure .env:"
echo "   sudo rm $INSTALL_DIR/.env && sudo bash $INSTALL_DIR/install.sh"
echo
c_bold "Portal URL (after boot, check logs):"
echo "   journalctl -u $SERVICE_NAME -g 'Portal Online' --no-pager"
line

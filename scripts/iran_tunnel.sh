#!/usr/bin/env bash
# =============================================================================
# iran_tunnel.sh — one-time, headless reverse-SSH tunnel for an Iran worker.
# =============================================================================
# Run ONCE on a fresh Iran server. It turns that box into an always-on
# "dial-out" tunnel (like a VPN that stays on): it exposes the Iran box's own
# SSH (port 22) on the MASTER's loopback at 127.0.0.1:<TUNNEL_PORT>. After this,
# you add the worker from the Telegram panel NORMALLY with:
#       IP = 127.0.0.1 , SSH port = <TUNNEL_PORT> , user = root , pass = <iran root pass>
# and the panel provisions Docker + the worker and reaches its API through the
# tunnel — no code change, no second terminal, survives reboots & drops.
#
# USAGE (paste once on the Iran box; MASTER_PASS is optional):
#   MASTER_IP=1.2.3.4 MASTER_PASS='masterRootPass' bash iran_tunnel.sh
#   # or, if you prefer to paste the key on the master yourself, omit MASTER_PASS:
#   MASTER_IP=1.2.3.4 bash iran_tunnel.sh
#
# Env knobs (all optional except MASTER_IP):
#   MASTER_IP        public IP of the master (abroad server)         [required]
#   MASTER_SSH_PORT  the master's SSH port                           [22]
#   MASTER_USER      ssh user on the master                          [root]
#   MASTER_PASS      master root password (used ONCE to install the  [unset]
#                    tunnel key, then never stored in the service)
#   TUNNEL_PORT      loopback port the worker-SSH appears on master  [19000]
#   LOCAL_SSH_PORT   this Iran box's own sshd port                   [22]
# =============================================================================
set -u

MASTER_IP="${MASTER_IP:-}"
MASTER_SSH_PORT="${MASTER_SSH_PORT:-22}"
MASTER_USER="${MASTER_USER:-root}"
TUNNEL_PORT="${TUNNEL_PORT:-19000}"
LOCAL_SSH_PORT="${LOCAL_SSH_PORT:-22}"
KEY=/root/.ssh/tnl

say(){ printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die(){ printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ -n "$MASTER_IP" ] || die "MASTER_IP is required.  e.g.  MASTER_IP=46.8.100.37 bash iran_tunnel.sh"
[ "$(id -u)" = "0" ] || die "run as root."

say "1/5 generating the tunnel SSH key (if missing)"
mkdir -p /root/.ssh && chmod 700 /root/.ssh
[ -f "$KEY" ] || ssh-keygen -t ed25519 -N "" -f "$KEY" -q
PUB="$(cat "$KEY.pub")"
echo "    pubkey: $PUB"

say "2/5 authorising this key on the master ($MASTER_USER@$MASTER_IP:$MASTER_SSH_PORT)"
AUTH_CMD="mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && grep -qF '$PUB' ~/.ssh/authorized_keys || echo '$PUB' >> ~/.ssh/authorized_keys"
if [ -n "${MASTER_PASS:-}" ]; then
  if ! command -v sshpass >/dev/null 2>&1; then
    say "    installing sshpass"
    (apt-get update -qq && apt-get install -y -qq sshpass) >/dev/null 2>&1 \
      || yum install -y sshpass >/dev/null 2>&1 \
      || dnf install -y sshpass >/dev/null 2>&1 || true
  fi
  if command -v sshpass >/dev/null 2>&1; then
    sshpass -p "$MASTER_PASS" ssh -o StrictHostKeyChecking=accept-new \
      -o ConnectTimeout=12 -p "$MASTER_SSH_PORT" "$MASTER_USER@$MASTER_IP" "$AUTH_CMD" \
      && echo "    ✅ key installed on master" \
      || die "could not log in to the master with MASTER_PASS (check IP/port/pass)."
  else
    echo "    ⚠ sshpass unavailable — paste the key on the master yourself (see below)."
  fi
else
  cat <<EOF

    MASTER_PASS not given. Run THIS once on the MASTER, then re-run me:
      mkdir -p ~/.ssh && echo '$PUB' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
EOF
fi

say "3/5 writing the systemd tunnel service"
cat > /etc/systemd/system/revtnl.service <<EOF
[Unit]
Description=reverse SSH tunnel to master (Iran worker dial-out)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/ssh -i $KEY -N -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -o ConnectTimeout=15 -p $MASTER_SSH_PORT -R 127.0.0.1:$TUNNEL_PORT:127.0.0.1:$LOCAL_SSH_PORT $MASTER_USER@$MASTER_IP
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

say "4/5 enabling + starting the service"
systemctl daemon-reload
systemctl enable --now revtnl >/dev/null 2>&1
sleep 3
STATE="$(systemctl is-active revtnl 2>/dev/null)"

say "5/5 result"
if [ "$STATE" = "active" ]; then
  cat <<EOF
  ✅ tunnel is UP and will stay up (auto-reconnect + starts on boot).

  Now add the worker from the Telegram panel  → 🛠 Workers → ➕ Add Worker:
      IP        : 127.0.0.1
      SSH port  : $TUNNEL_PORT
      user      : root
      password  : <this Iran server's root password>

  The panel will install Docker + the worker and talk to it through the tunnel,
  exactly like any normal worker.
EOF
else
  echo "  ⚠ service not active yet. Check:  journalctl -u revtnl -n 40 --no-pager"
  echo "    (most common cause: the key isn't on the master yet — see step 2)."
fi

#!/usr/bin/env bash
# sysprep_debian13.sh -- provision a fresh Debian 13 (trixie) host as a BA2 distributed GA
# trial worker (app/worker_server.py, dispatched to by testplatform/backend/app/services/
# worker_client.py). Run ONCE as root on a new box; safe to re-run (every step is guarded).
#
#   sudo ./sysprep_debian13.sh [options]
#
# What it does, in order:
#   1. apt update + full-upgrade + autoremove, enable unattended-upgrades for ongoing patches.
#   2. Disable/purge services this box has no business running (see "unneeded services" below).
#   3. sysctl network hardening baseline (SYN cookies, no source routing, no ICMP redirects).
#   4. Install + configure ufw: default deny incoming, allow outgoing, rate-limited SSH,
#      allow the worker port.
#   5. Install + configure fail2ban with the ba2-worker jail (testplatform/deploy/fail2ban/)
#      plus a standard sshd jail.
#   6. Create the least-privilege service user (system account, no login shell, no sudo).
#   7. Clone the repo over anonymous HTTPS (it's public -- no credential needed to pull; see
#      README's "Why HTTPS, no deploy key" if you point --repo-url at a private fork instead)
#      and run install.sh --test-only to build the ba2-test venv + package chain.
#   8. Generate the worker's bearer auth password (if not already set) into a root-only file,
#      consumed by the systemd service via LoadCredential= -- see ba2-worker.service's header
#      comment for why that's the secure-storage answer to "this user has no shell".
#   9. Install + enable the ba2-worker systemd service (auto-start on boot, Restart=always).
#
# What it deliberately does NOT do: touch /etc/ssh/sshd_config. Disabling SSH password auth
# without first confirming key-based access works is a self-lockout trap on a box you can only
# reach over the network. See sshd-hardening.conf.example in this folder for the recommended
# drop-in -- apply it yourself once you've confirmed you can log in with a key.
#
# Idempotent flags:
#   --worker-user NAME     service account (default: ba2worker)
#   --worker-home DIR      its home / install root (default: /opt/ba2worker)
#   --repo-url URL         git remote to clone (default: https://github.com/bmigette/BA2TradePlatform.git)
#   --branch NAME          branch to check out (default: main)
#   --ssh-port N           sshd port to allow through ufw (default: 22)
#   --worker-port N        worker server port -- ufw + fail2ban + systemd all use this (default: 8100)
#   --worker-slots N       trial pool size (default: unset -> auto, nproc-1; see _cmd_worker)
#   --skip-upgrade         skip step 1 (apt update/full-upgrade)
#   --skip-clone           skip step 7 (repo/venv already deployed by other means)
#   -h, --help
set -euo pipefail

# ---------------------------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------------------------
WORKER_USER="ba2worker"
WORKER_HOME="/opt/ba2worker"
REPO_URL="https://github.com/bmigette/BA2TradePlatform.git"
BRANCH="main"
SSH_PORT="22"
WORKER_PORT="8100"
WORKER_SLOTS=""
SKIP_UPGRADE=0
SKIP_CLONE=0

usage() { sed -n '2,40p' "$0"; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --worker-user) shift; WORKER_USER="$1" ;;
        --worker-home) shift; WORKER_HOME="$1" ;;
        --repo-url)    shift; REPO_URL="$1" ;;
        --branch)      shift; BRANCH="$1" ;;
        --ssh-port)    shift; SSH_PORT="$1" ;;
        --worker-port) shift; WORKER_PORT="$1" ;;
        --worker-slots) shift; WORKER_SLOTS="$1" ;;
        --skip-upgrade) SKIP_UPGRADE=1 ;;
        --skip-clone)   SKIP_CLONE=1 ;;
        -h|--help) usage 0 ;;
        *) echo "unknown arg: $1" >&2; usage 2 ;;
    esac
    shift
done

if [ "$(id -u)" -ne 0 ]; then
    echo "sysprep_debian13.sh must be run as root (sudo)." >&2
    exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"   # testplatform/deploy, inside the repo being provisioned FROM
REPO_DIR="$WORKER_HOME/BA2TradePlatform"

log()  { echo ">> $*"; }
step() { echo; echo "==== $* ===="; }

# ---------------------------------------------------------------------------------------------
# 1. Update / upgrade
# ---------------------------------------------------------------------------------------------
if [ "$SKIP_UPGRADE" != "1" ]; then
    step "apt update / full-upgrade"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get -y full-upgrade
    apt-get -y autoremove --purge
    apt-get clean

    log "installing base tooling (git, python3, build deps, ufw, fail2ban, unattended-upgrades)"
    apt-get -y install \
        git curl ca-certificates gnupg \
        python3 python3-venv python3-dev python3-pip \
        build-essential pkg-config libssl-dev libffi-dev \
        ufw fail2ban \
        unattended-upgrades apt-listchanges
    # TA-Lib's C library -- best-effort; if the package isn't in this Debian release's
    # archive, `pip install TA-Lib` (pulled in by install.sh below) will fail to build and
    # you'll need to build ta-lib-c from source (https://ta-lib.org) before retrying.
    apt-get -y install libta-lib0-dev || \
        log "libta-lib0-dev not available in this archive -- TA-Lib pip build may fail, see comment above"

    log "enabling unattended-upgrades (ongoing security patches; this script's apt upgrade is one-time)"
    cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
    systemctl enable --now unattended-upgrades.service
else
    step "skipping apt update/upgrade (--skip-upgrade)"
fi

# ---------------------------------------------------------------------------------------------
# 2. Disable services this worker box has no business running
# ---------------------------------------------------------------------------------------------
step "disabling unneeded services"
# Stop+disable+mask if the unit exists (fresh Debian netinst usually has none of these, but
# cloud images / non-minimal installs often do). Masking (not just disabling) stops a future
# package upgrade from silently re-enabling one of these as a dependency side effect.
UNNEEDED_UNITS=(
    bluetooth.service
    cups.service cups-browsed.service
    avahi-daemon.service avahi-daemon.socket
    ModemManager.service
    wpa_supplicant.service
    rpcbind.service rpcbind.socket
    exim4.service postfix.service
    triggerhappy.service
    rsync.service
)
for unit in "${UNNEEDED_UNITS[@]}"; do
    if systemctl list-unit-files 2>/dev/null | grep -q "^${unit}[[:space:]]"; then
        log "disabling $unit"
        systemctl stop "$unit" 2>/dev/null || true
        systemctl disable "$unit" 2>/dev/null || true
        systemctl mask "$unit" 2>/dev/null || true
    fi
done
# Purge the matching packages outright where present -- removes the attack surface, not just
# the unit. Non-fatal: `|| true` so a missing package (the common case) doesn't abort the script.
UNNEEDED_PKGS=(bluez cups cups-browsed avahi-daemon modemmanager wpasupplicant rpcbind nfs-common exim4-base exim4-daemon-light postfix triggerhappy)
for pkg in "${UNNEEDED_PKGS[@]}"; do
    if dpkg -l "$pkg" 2>/dev/null | grep -q '^ii'; then
        log "purging $pkg"
        apt-get -y purge "$pkg" || true
    fi
done
apt-get -y autoremove --purge || true

# ---------------------------------------------------------------------------------------------
# 3. sysctl network hardening baseline
# ---------------------------------------------------------------------------------------------
step "sysctl network hardening"
cat >/etc/sysctl.d/60-ba2-worker-hardening.conf <<'EOF'
# BA2 worker box baseline -- this is a leaf host (not a router), reachable from the internet
# on the worker port only. See sysprep_debian13.sh.
net.ipv4.ip_forward = 0
net.ipv6.conf.all.forwarding = 0
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.default.rp_filter = 1
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.default.accept_source_route = 0
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.default.send_redirects = 0
net.ipv4.icmp_echo_ignore_broadcasts = 1
net.ipv4.tcp_syncookies = 1
EOF
sysctl -p /etc/sysctl.d/60-ba2-worker-hardening.conf

# ---------------------------------------------------------------------------------------------
# 4. ufw
# ---------------------------------------------------------------------------------------------
step "configuring ufw (deny incoming by default; allow SSH rate-limited + worker port)"
ufw default deny incoming
ufw default allow outgoing
ufw limit "${SSH_PORT}/tcp" comment "SSH (rate-limited; fail2ban bans repeat offenders)"
ufw allow "${WORKER_PORT}/tcp" comment "ba2-worker (bearer-password gated, see worker_server.py)"
ufw logging low
ufw --force enable
ufw status verbose

# ---------------------------------------------------------------------------------------------
# 5. fail2ban
# ---------------------------------------------------------------------------------------------
step "configuring fail2ban (sshd + ba2-worker jails)"
WORKER_LOG="$WORKER_HOME/Documents/ba2/test/logs/worker_server.log"   # BA2_HOME default; see ba2_common/config.py
install -m 0644 "$HERE/fail2ban/ba2-worker.conf" /etc/fail2ban/filter.d/ba2-worker.conf
sed "s#/path/to/worker/logs/worker_server.log#${WORKER_LOG}#; s/^port     = 8100/port     = ${WORKER_PORT}/" \
    "$HERE/fail2ban/ba2-worker.jail.conf" > /etc/fail2ban/jail.d/ba2-worker.conf

cat >/etc/fail2ban/jail.d/sshd.conf <<EOF
# Debian's stock jail.conf ships sshd disabled -- enable it explicitly. maxretry/bantime match
# the ba2-worker jail's spirit (see fail2ban/ba2-worker.jail.conf) but sshd gets a longer ban:
# a scanning bot hitting SSH is far more common/persistent than one finding the worker port.
[sshd]
enabled  = true
port     = ${SSH_PORT}
maxretry = 5
findtime = 10m
bantime  = 4h
EOF
systemctl enable --now fail2ban
systemctl restart fail2ban

# ---------------------------------------------------------------------------------------------
# 6. Service user: system account, no login shell, no sudo group membership
# ---------------------------------------------------------------------------------------------
step "creating service user $WORKER_USER"
if ! id -u "$WORKER_USER" &>/dev/null; then
    useradd --system --create-home --home-dir "$WORKER_HOME" --shell /usr/sbin/nologin "$WORKER_USER"
    log "created $WORKER_USER (system account, home=$WORKER_HOME, shell=nologin, not in sudo/adm/any admin group)"
else
    log "$WORKER_USER already exists -- leaving as-is"
fi
chmod 750 "$WORKER_HOME"
chown "$WORKER_USER:$WORKER_USER" "$WORKER_HOME"

if [ "$SKIP_CLONE" = "1" ]; then
    step "skipping clone/venv build (--skip-clone)"
else
    # -----------------------------------------------------------------------------------------
    # 7. Clone + build the ba2-test venv. The repo is public, so a plain anonymous HTTPS clone
    #    needs no credential at all -- no deploy key, nothing for this no-shell service user to
    #    hold. (Point --repo-url at a private fork and you're on your own for arranging that
    #    user's git credentials; the default flow assumes the public upstream.) install.sh
    #    handles the common/providers/experts package chain + testplatform/backend/requirements.txt;
    #    see that script for details.
    # -----------------------------------------------------------------------------------------
    step "clone + build ba2-test venv"
    if [ ! -d "$REPO_DIR/.git" ]; then
        sudo -u "$WORKER_USER" git clone --branch "$BRANCH" "$REPO_URL" "$REPO_DIR"
        log "cloned $REPO_URL (@$BRANCH) -> $REPO_DIR"
    else
        log "$REPO_DIR already a git checkout -- leaving it (self-update via the running worker's"
        log "/update endpoint handles pulls from here on; see app/services/self_update.py)"
    fi

    if [ -x "$WORKER_HOME/ba2-venvs/test/bin/ba2-test" ]; then
        log "$WORKER_HOME/ba2-venvs/test already built -- skipping install.sh (re-run it directly"
        log "as $WORKER_USER with --upgrade if you need to pick up new/changed dependencies)"
    else
        log "running install.sh --test-only as $WORKER_USER (builds $WORKER_HOME/ba2-venvs/test)"
        sudo -u "$WORKER_USER" bash -c "cd '$REPO_DIR' && ./install.sh --test-only --base '$WORKER_HOME'"
    fi
fi

# ---------------------------------------------------------------------------------------------
# 8. Worker bearer password -- see ba2-worker.service's header comment for the full rationale.
#    Root-only file; systemd (as root) hands a private per-unit copy to the ba2worker-owned
#    process via LoadCredential=. The ba2worker user itself can never read this file directly.
# ---------------------------------------------------------------------------------------------
step "worker auth password"
mkdir -p -m 700 /etc/ba2-worker
PW_FILE=/etc/ba2-worker/worker_password
if [ ! -f "$PW_FILE" ]; then
    umask 077
    openssl rand -base64 32 > "$PW_FILE"
    chmod 600 "$PW_FILE"
    chown root:root "$PW_FILE"
    echo
    echo "############################################################################"
    echo "# Generated a new worker password at $PW_FILE (root-only, never printed again)."
    echo "# Register this SAME password (with this host + --worker-port $WORKER_PORT) as a"
    echo "# Worker on the master via the Test Platform's Workers page / POST /api/workers"
    echo "# (see testplatform/backend/app/api/workers.py). Read it once now if you need to"
    echo "# copy it there:"
    echo "#   sudo cat $PW_FILE"
    echo "############################################################################"
    echo
else
    log "$PW_FILE already exists -- leaving it (re-registering with the master would just be"
    log "confirming the same password already in use)"
fi

# ---------------------------------------------------------------------------------------------
# 9. systemd service -- installs + enables, but only STARTS it if the venv is actually built
#     (so a first run that stopped at the deploy-key gate above doesn't leave a crash-looping unit)
# ---------------------------------------------------------------------------------------------
step "installing ba2-worker.service"
install -m 0644 "$HERE/ba2-worker.service" /etc/systemd/system/ba2-worker.service
install -m 0755 -o "$WORKER_USER" -g "$WORKER_USER" "$HERE/run-worker.sh" "$WORKER_HOME/run-worker.sh"
install -m 0700 -o root -g root "$HERE/update-worker.sh" "$WORKER_HOME/update-worker.sh"   # root-only: it calls systemctl restart
sed -i "s/^Environment=BA2_WORKER_PORT=.*/Environment=BA2_WORKER_PORT=${WORKER_PORT}/" /etc/systemd/system/ba2-worker.service
sed -i "s#^WorkingDirectory=.*#WorkingDirectory=${REPO_DIR:-$WORKER_HOME/BA2TradePlatform}/testplatform#" /etc/systemd/system/ba2-worker.service
sed -i "s#^ReadWritePaths=.*#ReadWritePaths=${WORKER_HOME}#" /etc/systemd/system/ba2-worker.service
if [ -n "$WORKER_SLOTS" ]; then
    log "pinning trial pool to $WORKER_SLOTS slot(s) (--worker-slots)"
    sed -i "s/^#Environment=BA2_WORKER_SLOTS=.*/Environment=BA2_WORKER_SLOTS=${WORKER_SLOTS}/" /etc/systemd/system/ba2-worker.service
fi
systemctl daemon-reload
systemctl enable ba2-worker.service

if [ -x "$WORKER_HOME/ba2-venvs/test/bin/ba2-test" ]; then
    systemctl restart ba2-worker.service
    sleep 2
    systemctl --no-pager status ba2-worker.service || true
    log "worker enabled + started. Logs: journalctl -u ba2-worker -f"
else
    log "venv not built yet -- service is enabled (will auto-start on boot / next"
    log "'systemctl start ba2-worker') but NOT started now. Finish the deploy-key + clone step"
    log "above, then: systemctl start ba2-worker"
fi

step "done"
echo "Summary:"
echo "  service user     : $WORKER_USER ($WORKER_HOME)"
echo "  repo             : $REPO_URL @ $BRANCH -> $REPO_DIR"
echo "  worker port      : $WORKER_PORT (ufw + fail2ban configured)"
echo "  worker slots     : ${WORKER_SLOTS:-auto (nproc-1)}"
echo "  ssh port         : $SSH_PORT (rate-limited via ufw, fail2ban jail enabled)"
echo "  worker password  : $PW_FILE (root-only; register the same value on the master)"
echo "  systemd unit     : ba2-worker.service (enabled; journalctl -u ba2-worker -f to watch)"

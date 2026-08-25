#!/usr/bin/env bash
# Manual "pull latest + restart" for a BA2 worker box already provisioned by
# sysprep_debian13.sh. Installed to the worker's home dir by that script, so once deployed you
# can just SSH in and run:
#   sudo /opt/ba2worker/update-worker.sh
#
# This is deliberately independent of the HTTP self-update path (the master's
# POST /workers/{id}/update -> self_update.perform_update() + in-place os.execv, run from
# INSIDE the already-running worker process): that path needs the service to already be up and
# needs the bearer password. This one works from the outside via git + systemctl, so it still
# works when the service is down/crash-looping, and an operator with root on the box never
# needs to go fetch the worker's own auth secret just to update its code.
#
# Mirrors install.sh's install_chain (non-editable, no --upgrade) rather than calling
# self_update.reinstall_packages directly, to avoid importing the app (DB init side effects)
# just to update code.
set -euo pipefail
WORKER_USER="ba2worker"
WORKER_HOME="/opt/ba2worker"
RESTART_ONLY=0

while [ $# -gt 0 ]; do
    case "$1" in
        --worker-user) shift; WORKER_USER="$1" ;;
        --worker-home) shift; WORKER_HOME="$1" ;;
        --restart-only) RESTART_ONLY=1 ;;   # skip pull/reinstall, just bounce the service
        -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
    shift
done

if [ "$(id -u)" -ne 0 ]; then
    echo "update-worker.sh must be run as root (it restarts a systemd unit)." >&2
    exit 1
fi

REPO_DIR="$WORKER_HOME/BA2TradePlatform"
VENV="$WORKER_HOME/ba2-venvs/test"
[ -d "$REPO_DIR/.git" ] || { echo "no checkout at $REPO_DIR -- run sysprep_debian13.sh first." >&2; exit 1; }

if [ "$RESTART_ONLY" != "1" ]; then
    echo ">> git pull --ff-only in $REPO_DIR (as $WORKER_USER)"
    BEFORE="$(sudo -u "$WORKER_USER" git -C "$REPO_DIR" rev-parse HEAD)"
    sudo -u "$WORKER_USER" git -C "$REPO_DIR" pull --ff-only
    AFTER="$(sudo -u "$WORKER_USER" git -C "$REPO_DIR" rev-parse HEAD)"

    if [ "$BEFORE" = "$AFTER" ]; then
        echo ">> already up to date ($AFTER) -- nothing to reinstall/restart. Use --restart-only"
        echo "   if you just want to bounce the service."
        exit 0
    fi
    echo ">> $BEFORE -> $AFTER"

    echo ">> reinstalling the common/providers/experts chain (packages/ is installed non-editable,"
    echo "   so a bare git pull alone does NOT take effect until this runs -- see install.sh)"
    UV="$VENV/bin/uv"; VPY="$VENV/bin/python"
    for name in common providers experts; do
        pkg="$REPO_DIR/packages/$name"
        echo "   - $name"
        if [ -x "$UV" ]; then
            sudo -u "$WORKER_USER" "$UV" pip install --python "$VPY" --no-sources "$pkg"
        else
            sudo -u "$WORKER_USER" "$VPY" -m pip install "$pkg"
        fi
    done
else
    echo ">> --restart-only: skipping pull/reinstall"
fi

echo ">> restarting ba2-worker.service"
systemctl restart ba2-worker.service
sleep 2
systemctl --no-pager --full status ba2-worker.service || true

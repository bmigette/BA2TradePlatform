#!/usr/bin/env bash
# ExecStart target for ba2-worker.service. Not meant to be run by hand (though it's harmless
# to — it'll just fail the credential read outside of systemd).
#
# Reads the worker bearer password out of systemd's per-unit credential mount
# ($CREDENTIALS_DIRECTORY, set by the unit's LoadCredential=) and exports it as
# BA2_WORKER_PASSWORD for THIS process only, then execs the worker in place (same PID, so
# systemd's supervision/Restart= keeps working). It is never written to disk again, never
# passed as a CLI arg (which `ps`/`/proc/*/cmdline` would leak to any local user), and never
# printed.
set -euo pipefail

: "${CREDENTIALS_DIRECTORY:?run-worker.sh must be started by systemd with LoadCredential= set (see ba2-worker.service)}"
export BA2_WORKER_PASSWORD
BA2_WORKER_PASSWORD="$(cat "${CREDENTIALS_DIRECTORY}/worker_password")"

VENV="/opt/ba2worker/ba2-venvs/test"
BA2_TEST_BIN="$VENV/bin/ba2-test"
if [ ! -x "$BA2_TEST_BIN" ]; then
    echo "run-worker.sh: $BA2_TEST_BIN not found — has install.sh --test-only been run for" \
         "this user? See testplatform/deploy/README.md." >&2
    exit 1
fi

ARGS=(worker --host 0.0.0.0 --port "${BA2_WORKER_PORT:-8100}")
if [ -n "${BA2_WORKER_SLOTS:-}" ]; then
    ARGS+=(--workers "$BA2_WORKER_SLOTS")
fi

exec "$BA2_TEST_BIN" "${ARGS[@]}"

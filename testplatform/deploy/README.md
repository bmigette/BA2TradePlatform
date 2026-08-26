# BA2 worker box deployment (Debian 13)

Provisions a fresh Debian 13 (trixie) host as a distributed GA trial worker for the Test
Platform (`app/worker_server.py`, dispatched to by `backend/app/services/worker_client.py` /
`worker_fleet.py`). One host = one `ba2-worker` systemd service, bearer-password gated,
auto-started on boot.

## Files

| File | Purpose |
|---|---|
| `sysprep_debian13.sh` | The whole setup: OS hardening, service user, clone + venv build, systemd service. Run once as root; safe to re-run. |
| `ba2-worker.service` | The systemd unit installed by the script. |
| `run-worker.sh` | `ExecStart` target — pulls the auth password out of systemd's credential store and execs `ba2-test worker`. |
| `update-worker.sh` | Installed to the worker's home dir — `sudo /opt/ba2worker/update-worker.sh` does a manual git pull + package reinstall + service restart. See "Updating the code" below. |
| `sshd-hardening.conf.example` | Optional SSH hardening drop-in — **not** applied automatically, see its header. |
| `fail2ban/ba2-worker.conf`, `fail2ban/ba2-worker.jail.conf` | Filter + jail for the worker's own auth-failure log lines (installed by the script). |

## Quick start

From a checkout of this repo on the new box (or copy just `testplatform/deploy/` over first —
the script clones the full repo itself):

```bash
sudo ./sysprep_debian13.sh
```

Defaults: service user `ba2worker` at `/opt/ba2worker`, repo `https://github.com/bmigette/BA2TradePlatform.git`
branch `dev` (matches the existing worker fleet — see `docs/RUNBOOK-goal2020-grid.md` §2a: a
worker not tracking the master's branch is retry-excluded for the whole run), SSH port `22`,
worker port `8100`, trial pool size auto (`nproc-1`). Override any
of these — run `./sysprep_debian13.sh --help` for the full flag list. Pin the pool size
explicitly with `--worker-slots N` when you don't want auto-sizing (e.g. a box dedicated
entirely to this worker, or one shared with other things where you want to leave it headroom).

It runs start to finish unattended — no gate to stop for. When it finishes it prints a summary
including where the worker's auth password landed (`/etc/ba2-worker/worker_password`, root-only
— `sudo cat` it once to copy it out). **Register that same password**, this host's address, and
the worker port as a Worker entry on the master (Test Platform's Workers page, or
`POST /api/workers` — see `backend/app/api/workers.py`). Until that's done the master has
nothing to dispatch trials to, even though the service itself is up.

## Why HTTPS, no deploy key

`BA2TradePlatform` is a public repo, so a plain anonymous `git clone`/`git pull` over HTTPS needs
no credential at all — nothing for the no-shell `ba2worker` account to hold, generate, or rotate
just to read code. `self_update.perform_update()` (triggered by the master's
`POST /workers/{id}/update`, or the worker's own `/update` endpoint) uses that same anonymous
HTTPS remote to stay in lock-step with `TEST_APP_VERSION`. The **worker bearer password**
(`/etc/ba2-worker/worker_password`) is a separate, unrelated credential — what the *master*
presents to *this worker's* HTTP API (`Authorization: Bearer <password>`, checked by
`worker_server._verify`) to submit trials, push cache, or trigger an update. It has nothing to do
with git.

If you ever point `--repo-url` at a private fork instead, you're on your own for arranging that
user's git credentials (e.g. a read-only deploy key) — the default flow assumes the public
upstream.

## How the worker password is kept from the service user

The `ba2worker` account has no login shell and isn't in `sudo`/`adm` — by design it should never
need to read its own secret off disk directly. `ba2-worker.service` uses systemd's
`LoadCredential=` mechanism: `/etc/ba2-worker/worker_password` is `root:root` mode `0600` (the
`ba2worker` user cannot open it); systemd itself, running as root, reads it at service-start
time and exposes a private, unit-scoped copy at `$CREDENTIALS_DIRECTORY/worker_password` —
`0400`, owned by the running service, torn down on stop, never inherited by unrelated processes.
`run-worker.sh` reads that into an env var for its own process only (never a CLI arg — argv is
visible to any local user via `/proc/<pid>/cmdline`; env is not, once `run-worker.sh` has execed
into the venv's `ba2-test`). See `ba2-worker.service`'s header comment for the full rationale;
this is the systemd-native answer to "the service user has no shell, so how do I get it a
secret."

To rotate the password: overwrite `/etc/ba2-worker/worker_password`, `systemctl restart
ba2-worker`, then update the matching Worker entry on the master.

## Least privilege

- `ba2worker` is a `--system` account, `--shell /usr/sbin/nologin`, not in any admin group. It
  owns nothing outside `/opt/ba2worker` (the repo checkout, venv, and `BA2_HOME` cache/logs all
  live there) and `/etc/ba2-worker/worker_password` is unreadable to it directly (see above).
- The systemd unit sandboxes the process further: `ProtectSystem=strict` + `ProtectHome=true`
  make the whole filesystem read-only except `ReadWritePaths=/opt/ba2worker`; capabilities are
  stripped (`CapabilityBoundingSet=` empty); `NoNewPrivileges=true`; kernel tunables, modules,
  control groups, and namespaces are all locked down. This matters because a trial config is
  effectively an untrusted payload the master pushes to `/run-trial` — the sandbox is defense in
  depth against anything that gets past the bearer check.
- Firewall: `ufw` default-denies all incoming except SSH (rate-limited) and the worker port;
  `fail2ban` bans repeat auth failures on both SSH and the worker's own bearer-check log lines
  (`app.worker_server - WARNING - auth failed from <ip>: ...` — see `fail2ban/ba2-worker.conf`).

## Not automated: SSH password-auth lockdown

`sysprep_debian13.sh` deliberately does not touch `/etc/ssh/sshd_config` — see
`sshd-hardening.conf.example` and apply it by hand once you've confirmed key-based login works
from a second session. Getting this wrong on a box you can only reach over the network is a
self-inflicted outage, not something to run unattended.

## Updating the code

Two independent update paths exist:

- **Remote/automatic**: the master calls `POST /workers/{id}/update` (or the worker's own
  `/update` endpoint) — runs `self_update.perform_update()` (git pull + package reinstall)
  inside the already-running process, then re-execs in place (`os.execv`, same PID). Needs the
  service to already be up and needs the bearer password. This is what a GA run in progress
  relies on to keep workers in lock-step with `TEST_APP_VERSION`.
- **Local/manual**: SSH in and run `sudo /opt/ba2worker/update-worker.sh` — plain `git pull
  --ff-only` + package-chain reinstall (as `ba2worker`) + `systemctl restart ba2-worker` (as
  root). Doesn't need the worker password, and works even if the service is currently
  down/crash-looping. Use `--restart-only` to just bounce the service without pulling.

## Operational notes

- **Auto-start**: `systemctl enable ba2-worker` is run unconditionally; the service also
  `Restart=always`. A reboot or crash comes back on its own.
- **Self-update**: once running, the worker keeps itself in sync with `TEST_APP_VERSION` via
  `git pull --ff-only` + package reinstall + in-place re-exec (`os.execv`, same PID — systemd
  never sees a restart). It does **not** need any systemd/sudo privilege to do this; it only
  needs write access to `$REPO_DIR`, which `ba2worker` already owns.
- **Logs**: `journalctl -u ba2-worker -f` for the service itself; the app's own rotating log is
  at `BA2_HOME/test/logs/worker_server.log` (default `/opt/ba2worker/Documents/ba2/test/logs/`)
  — that's the path fail2ban's `ba2-worker` jail watches.
- **TA-Lib**: `libta-lib0-dev` may not exist in every Debian 13 mirror at the time you run this;
  the script warns and continues if so. If `install.sh` then fails building the `TA-Lib` pip
  package, build `ta-lib-c` from source (https://ta-lib.org) before re-running
  `--skip-upgrade --skip-clone` is *not* what you want here — just re-run `install.sh
  --test-only --base /opt/ba2worker` as the `ba2worker` user once the C library is in place.

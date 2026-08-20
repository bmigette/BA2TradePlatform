"""Distributed batch evaluator (PUSH model) — master dispatches trials to local + remote workers.

Drops in for the strategy optimizer's local ``ProcessPoolExecutor`` path. Trials for a generation
are queued on a per-optimization ``TrialBroker``; two kinds of threads drain it:
  * LOCAL consumer threads run each trial through the master's process pool (the master is also a
    worker);
  * REMOTE dispatcher threads (one per worker slot = the worker's reported capacity) PUSH each
    trial to a worker over HTTP (``worker_client.run_trial``) and post the result back.
On a worker error the dispatcher REQUEUES the trial (a local consumer or another worker picks it
up) and backs off; after repeated failures it gives up on that worker — graceful degradation to
local-only.

Pre-flight per selected worker: ``ensure_synced`` (auto-update+wait so it runs a compatible build,
matched on app version) then ``push_cache`` (stream the missing cache as one tar). Workers
that can't be reached/synced are dropped with a warning.

Re-admission: a worker that failed pre-flight (or gave up mid-run) is not excluded for the rest
of the job. Every ``n_consumers`` individuals completed, one background re-check re-runs the same
pre-flight against the down worker list; a worker that now syncs is re-admitted with fresh
dispatcher threads. The check runs off the result-consuming thread (non-blocking, one in flight
at a time) so a still-dead worker's timeout never stalls trial throughput.

Determinism: a trial config is hermetic + seeded, so its fitness is independent of WHERE it ran;
``execute_jobs`` reassembles results by the GA's input index. With no workers selected the caller
keeps the plain local-pool path (byte-identical to before).
"""

from __future__ import annotations

import logging
import os as _os
import threading
from typing import Any, Callable, Iterator, List, Optional, Tuple

from app.services import worker_client
from app.services.trial_broker import TrialBroker

logger = logging.getLogger(__name__)

# A trial job as built by the handler: (index, decoded_flat, trial_key, config).
Job = Tuple[int, dict, str, dict]

# A dispatcher gives up on a worker after this many consecutive run_trial failures (dead box).
_MAX_WORKER_FAILURES = 3
# How long a dispatcher waits out a worker's backpressure before claiming again.
_BACKPRESSURE_WAIT_S = float(_os.getenv("BT_BACKPRESSURE_WAIT_S", "15"))

# How often the master polls each remote worker's /health memory block, and the free-memory
# floor it sheds slots to stay above. Polling on a TIMER rather than per completed trial is
# the point: a trial-completion snapshot is the only reading the master used to get, and on
# the mid band a trial runs 10-30 min, so a worker could sit at 0.1% free for half an hour
# before anything noticed. One slot per poll converges without overshooting -- 12 slots can
# be walked down to 1 in 11 minutes, and shedding STOPS the moment the box is back above the
# floor, so it settles at whatever concurrency the band actually affords.
_REMOTE_MEM_POLL_S = float(_os.getenv('BT_REMOTE_MEM_POLL_S', '60'))
_REMOTE_MEM_FLOOR_PCT = float(_os.getenv('BT_REMOTE_MEM_FLOOR_PCT', '10'))


def _log_memory_diagnostics(log, context: str) -> None:
    """Best-effort process/system memory snapshot, logged when a local trial dies unexpectedly
    (e.g. BrokenProcessPool) so a recurrence leaves a real data point instead of just the bare
    exception repr. Never raises — diagnostics must not mask the original failure."""
    try:
        import os
        import psutil
        p = psutil.Process(os.getpid())
        rss_mb = p.memory_info().rss / (1024 * 1024)
        vm = psutil.virtual_memory()
        log(f"{context}: master RSS={rss_mb:.0f}MB, system available="
            f"{vm.available / (1024 * 1024):.0f}MB / {vm.total / (1024 * 1024):.0f}MB "
            f"({vm.percent:.1f}% used)")
    except Exception as e:  # noqa: BLE001 — diagnostics are best-effort, never fatal
        log(f"{context}: memory diagnostics unavailable ({e!r})")


def _is_backpressure(out: Any) -> bool:
    """True when a worker refused work because it is FULL or under its memory floor.

    This is the worker behaving correctly, not failing. Counting it toward
    _MAX_WORKER_FAILURES declared a healthy-but-busy box dead after 3 refusals, which then
    fed the re-admission storm that spawned duplicate dispatchers. Backpressure is requeued
    and waited out, never fatal.
    """
    return bool(isinstance(out, dict) and out.get("backpressure"))


class DistributedEvaluator:
    """Bridges the GA batch loop to local + remote workers via a per-optimization TrialBroker.

    *submit_pool* is the master's ``ProcessPoolExecutor`` (local consumers run trials through it).
    *pool_factory*, if given, rebuilds an equivalent fresh pool (same max_workers/mp_context/
    initializer/initargs) when the local pool dies mid-run (``BrokenProcessPool`` — one worker
    crashed, e.g. from a transient MemoryError, which permanently breaks a
    ``concurrent.futures.ProcessPoolExecutor``: EVERY subsequent ``.submit()`` on it raises
    immediately). Without a factory, local consumers degrade to remote-only for the rest of the
    run (the previous behaviour) instead of recovering.
    *workers* is a list of resolved worker dicts ``{id,name,url,password,capacity}``. *master_version*
    is the master's app version (workers are version-matched to it before use).
    """

    def __init__(self, submit_pool, fitness_metric: str, n_consumers: int,
                 optimization_id: Any, workers: Optional[List[dict]] = None,
                 master_version: Optional[str] = None, log=logger.warning,
                 trial_timeout: float = 10800.0,
                 requeue_timeout: float = 12600.0,
                 pool_factory: Optional[Callable[[], Any]] = None,
                 max_remote_slots_per_worker: Optional[int] = None,
                 governor: Optional[Any] = None):
        self.pool = submit_pool
        self._pool_factory = pool_factory
        # Dynamic worker allocation. When set, consumers above governor.current PARK
        # instead of claiming, so concurrency follows memory pressure. Parking (not
        # killing) means an in-flight individual always finishes -- nothing is discarded
        # to shed load; the governor's cache RELEASE is what actually reclaims memory.
        self.governor = governor
        self._consumer_slots = threading.local()
        self._parked_logged = set()
        self._pool_lock = threading.Lock()  # guards self.pool reads/recreation across consumers
        self.fitness_metric = fitness_metric
        self.n_consumers = max(1, n_consumers)
        self.optimization_id = optimization_id
        self.workers = workers or []
        self.master_version = master_version
        self.log = log
        # Per-run ceiling on concurrent remote slots PER WORKER, regardless of the worker's
        # reported /health capacity. Used by memory-heavy experts (e.g. FMPSenateTraderWeight,
        # see max_remote_worker_slots there) so a worker that advertises 8 slots doesn't run 8
        # concurrent trials of an expert whose per-trial footprint would OOM it.
        self.max_remote_slots_per_worker = max_remote_slots_per_worker
        # 2026-07-28: both budgets were 1800s, which is only ~2x a real trial and collapsed the
        # Senate 5min grid twice. MEASURED on an idle box (test_files/profile_senate_trial.py):
        # a 6-month S1/S3/S5 trial is 126-144s, so the full 2023-2026 window is ~15 min/trial —
        # and under 4 local + 4 remote concurrent trials it crosses 30 min. S4 lost 6 trials to
        # this and S5 lost 12 (4 slots x 3 retries), after which the worker was declared dead.
        # The strategy was NOT at fault: S5 measured within 6% of S3.
        #
        # requeue_timeout MUST stay above trial_timeout. It re-queues a trial whose worker
        # vanished; if it fired first, a slow-but-healthy trial would be duplicated onto another
        # slot while the original is still running — wasting exactly the capacity we are short of.
        self.trial_timeout = trial_timeout
        self.requeue_timeout = max(requeue_timeout, trial_timeout * 1.15)
        self.broker = TrialBroker()  # OWN broker (per-optimization isolation; queue is max_workers=4)
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._active_workers: List[dict] = []
        self._down_workers: List[dict] = []  # excluded at pre-flight or gave up mid-run; retried
        self._worker_lock = threading.Lock()  # guards _active_workers/_down_workers mutations
        # PER-WORKER memory governors. A remote worker's box is not the master's, and the master's
        # MemoryGovernor can only act on the LOCAL pool, so remote pressure previously had no
        # actuator at all -- the reading came back, throttled the wrong fleet, and the overcommitted
        # box kept taking work until a trial died. Each remote gets its own governor whose
        # `current` caps how many of ITS dispatcher slots may claim; the rest park (identical to
        # how `_consume_local` parks local slots above the local governor's ceiling).
        self._remote_govs: dict = {}
        self._remote_gov_lock = threading.Lock()
        self._recheck_lock = threading.Lock()  # at most one re-admission pre-flight in flight
        # DISPATCHER GENERATION per worker. Re-admission used to spawn a fresh full set of
        # dispatcher threads while the previous set kept running -- only the ONE thread that
        # saw the failure returned. On 2026-08-19 that compounded over 418 re-admissions
        # until remote150 reported busy:61 against a 12-slot pool. A dispatcher captures its
        # epoch at spawn and retires itself as soon as the worker's epoch moves, so exactly
        # one generation is ever alive. Guarded by _worker_lock.
        self._worker_epochs: dict = {}
        self._secrets: dict = {}

    # -- lifecycle ---------------------------------------------------------------------------
    def start(self) -> None:
        # Credential app-settings to mirror onto each (DB-less) worker so its hermetic trials
        # resolve them via get_app_setting — survives the worker's self-update restart, which drops
        # env-only keys (the recurring 'FMP API key not configured' on remote trials).
        secrets = self._resolve_master_secrets()
        self._secrets = secrets  # kept for later re-admission pre-flight retries
        # Pre-flight: version-match + cache-push each selected worker; drop the unusable ones.
        # Workers with no password are a static misconfiguration (never resolves mid-run) and are
        # excluded outright; anything else is a transient failure candidate, tracked in
        # _down_workers so _recheck_down_workers can retry it periodically.
        for w in self.workers:
            if not w.get("password"):
                self.log(f"worker {w.get('name')} has no password configured; excluding")
                continue
            if self._preflight_worker(w, secrets):
                self._active_workers.append(w)
            else:
                self._down_workers.append(w)

        # Local consumers (master-as-worker).
        for i in range(self.n_consumers):
            # Each consumer carries its SLOT INDEX so the governor can park the highest-numbered
            # ones first (deterministic, and consumer #0 always survives so a run cannot stall).
            self._spawn(lambda idx=i: self._consume_local_slot(idx),
                        f"local-trial-consumer-{i}")
        # Remote dispatchers: one thread per worker slot.
        remote_slots = 0
        for w in self._active_workers:
            remote_slots += self._spawn_remote_dispatchers(w)
        self.log(f"distributed evaluator (opt {self.optimization_id}): {self.n_consumers} local + "
                 f"{remote_slots} remote slot(s) across {len(self._active_workers)} worker(s)")
        # Surface the engaged fleet in the UI: each remote worker's active_jobs_count = its slot
        # count, the local worker's = the local consumer count. (The CLI path never updates these,
        # so without this the dashboard shows "0 jobs" + "offline" while a run is in flight.)
        self._report_fleet_state(active=True)
        # Timer-driven memory control loop for the remotes (see _remote_memory_watchdog).
        if self._active_workers:
            self._spawn(self._remote_memory_watchdog, "remote-memory-watchdog")

    def _resolve_master_secrets(self) -> dict:
        """Read the credential app-settings to mirror onto workers from the MASTER's DB. Keys absent
        on the master are simply omitted (the worker keeps whatever it had). Never raises."""
        out: dict = {}
        try:
            from ba2_common.config import get_app_setting
            for k in ("FMP_API_KEY", "finnhub_api_key"):
                v = get_app_setting(k)
                if v:
                    out[k] = v
        except Exception as e:  # noqa: BLE001 — best-effort; a missing key fails loudly at the worker
            self.log(f"master secret resolution failed (non-fatal): {e}")
        return out

    def _report_fleet_state(self, active: bool) -> None:
        """Write engaged-slot counts (and online status) for the participating workers to the DB so
        the dashboard/workers panels reflect reality. Best-effort: never raises."""
        try:
            from datetime import datetime
            from app.models.database import SessionLocal
            from app.models import Worker
            db = SessionLocal()
            try:
                for w in self._active_workers:
                    row = db.query(Worker).filter(Worker.id == w.get("id")).first()
                    if not row:
                        continue
                    row.active_jobs_count = self._engaged_slots(w) if active else 0
                    if active:
                        row.status = "online"
                        row.last_heartbeat = datetime.utcnow()
                local = db.query(Worker).filter(Worker.is_local == True).first()  # noqa: E712
                if local:
                    local.active_jobs_count = self.n_consumers if active else 0
                db.commit()
            finally:
                db.close()
        except Exception as e:  # noqa: BLE001 — fleet reporting must never affect the run
            self.log(f"fleet-state report (active={active}) skipped: {e}")

    def _spawn(self, target, name: str) -> None:
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def _preflight_worker(self, w: dict, secrets: dict) -> bool:
        """Version-match + cache/secrets-push one worker. Returns True if it's usable now."""
        try:
            if not worker_client.ensure_synced(w, self.master_version, log=self.log):
                return False
            worker_client.push_cache(w, log=self.log)
            worker_client.push_secrets(w, secrets, log=self.log)
            try:
                _health = worker_client.health(w)
                w["capacity"] = max(1, int(_health.get("capacity") or 1))
                # The daemon's ceiling, NOT the pool's current size -- a worker we shrank
                # for a narrow job must still be growable back for a wide one. Older
                # workers omit capacity_max; fall back to what they do report.
                w["capacity_max"] = max(1, int(_health.get("capacity_max")
                                               or _health.get("capacity") or 1))
                # Log the worker's RAM at pre-flight (added 2026-08-09, alongside the worker's new
                # /health memory block). Memory is what actually degrades a trial host, and until
                # now the master could not see a remote's pressure at all -- a remote stall looked
                # like an unexplained slowdown. Reported once per run, and WARNED above 90% so it
                # is visible in the grid log rather than needing a manual probe.
                _mem = _health.get("memory") or {}
                if _mem.get("available"):
                    _msg = (f"worker {w.get('name')} memory: {_mem.get('free_mb')} MB free of "
                            f"{_mem.get('total_mb')} MB ({_mem.get('used_pct')}% used), "
                            f"worker tree rss {_mem.get('rss_mb')} MB")
                    self.log(_msg + ("  <- LOW: trials will stall, not crash"
                                     if (_mem.get("used_pct") or 0) >= 90 else ""))
            except Exception:  # noqa: BLE001 — fall back to 1 slot if /health didn't report
                w["capacity"] = max(1, int(w.get("capacity") or 1))
            self._size_remote_pool(w)
            return True
        except Exception as e:  # noqa: BLE001 — a bad worker must never abort the run
            self.log(f"worker {w.get('name')} pre-flight failed: {e}; excluding")
            return False

    def _size_remote_pool(self, w: dict) -> None:
        """Size *w*'s trial pool to the slots THIS job will actually engage.

        A worker's pool children are resident whether or not the master dispatches to them,
        so leaving a 12-slot pool up to serve 6 engaged slots wastes ~half the box (measured
        on remote150: 12 children, 41 GB, for 6 slots). Sized here, at pre-flight, because a
        rebuild is only safe while the worker is idle.

        Best-effort throughout: an older worker with no /pool/resize, or one that refuses
        because another optimization is mid-trial, simply keeps its current size. This must
        never be a reason to drop a usable worker.
        """
        want = self._engaged_slots(w)
        if want == int(w.get("capacity") or 0):
            return  # the pool is already exactly this size
        try:
            out = worker_client.resize_pool(w, want)
        except Exception as e:  # noqa: BLE001 — pre-Aug-2026 workers have no such endpoint
            self.log(f"worker {w['name']} pool resize to {want} unavailable ({e}); "
                     f"keeping its {w.get('capacity')}-slot pool")
            return
        if out.get("changed"):
            w["capacity"] = max(1, int(out.get("capacity") or want))
            self.log(f"worker {w['name']} pool resized to {w['capacity']} slot(s) for this "
                     f"job -- surplus children released")
        elif not out.get("ok"):
            self.log(f"worker {w['name']} kept its {out.get('capacity')}-slot pool "
                     f"({out.get('reason')})")

    def _engaged_slots(self, w: dict) -> int:
        """Slots actually engaged for *w*: its reported capacity, capped by
        ``max_remote_slots_per_worker`` if the run set one."""
        cap = max(1, int(w.get("capacity_max") or w.get("capacity") or 1))
        if self.max_remote_slots_per_worker:
            cap = min(cap, self.max_remote_slots_per_worker)
        return cap

    def _spawn_remote_dispatchers(self, w: dict) -> int:
        from app.services.strategy_optimization_handler import MemoryGovernor
        cap = self._engaged_slots(w)
        # RETIRE the previous generation before starting a new one. Bumping the epoch is what
        # makes the old dispatchers stand down; without it they keep claiming forever and the
        # worker is driven at (generations x cap) concurrency.
        with self._worker_lock:
            epoch = self._worker_epochs.get(w["name"], 0) + 1
            self._worker_epochs[w["name"]] = epoch
        # Arm (or re-arm, on re-admission at a NEW capacity) this worker's own governor.
        # CARRY THE SHED LEVEL FORWARD: a re-admission is not evidence the box got roomier,
        # and rebuilding at full cap threw away every step the governor had walked down
        # (measured 418 resets in one run, so it logged "6 -> 5" indefinitely and never
        # converged). A JOB boundary is what restores full concurrency -- see
        # MemoryGovernor.restore.
        with self._remote_gov_lock:
            prev = self._remote_govs.get(w["name"])
            gov = MemoryGovernor(
                cap, log=lambda m, n=w["name"]: self.log(f"[{n}] {m}"),
                emergency_note="shedding one dispatch slot (the master cannot break a remote pool; "
                               "it can only stop feeding it -- see the memory watchdog)")
            if prev is not None:
                gov.current = max(1, min(gov.full, prev.current))
            self._remote_govs[w["name"]] = gov
        for i in range(cap):
            self._spawn(lambda w=w, i=i, e=epoch: self._dispatch_remote(w, i, e),
                        f"remote-{w['name']}-{i}-g{epoch}")
        return cap

    def _mark_worker_down(self, w: dict, why: str) -> bool:
        """Retire *w*: drop it from the active set, list it ONCE for re-admission, and move
        its epoch so every one of its dispatchers stands down.

        Returns True if this call is the one that took it down. Previously each dispatcher
        did this inline, so six threads giving up together appended the same worker six
        times and _recheck_down_workers then re-admitted it six times over.
        """
        with self._worker_lock:
            already_down = w in self._down_workers
            if w in self._active_workers:
                self._active_workers.remove(w)
            if not already_down:
                self._down_workers.append(w)
            self._worker_epochs[w["name"]] = self._worker_epochs.get(w["name"], 0) + 1
        return not already_down

    def _shed_remote_slot(self, worker_name: str, gov, why: str, worker: dict = None) -> bool:
        """Drop ONE dispatch slot for *worker_name*. Returns True if anything changed.

        One at a time, deliberately. Halving was tried first and is wrong: it overshoots (a box a
        little over the line loses half its throughput) and it cannot converge on the right number.
        Stepping down once per poll walks to whatever concurrency the band actually affords and
        stops there -- the parked dispatchers simply stop claiming; nothing in flight is killed.
        """
        before = gov.current
        gov.current = max(1, gov.current - 1)
        if gov.current == before:
            return False
        self.log(f"[{worker_name}] dispatch slots {before} -> {gov.current} ({why}); "
                 f"in-flight trials finish normally, full concurrency returns at the next job")
        # AND SHRINK THE POOL TO MATCH. Cutting concurrency alone does not reclaim anything:
        # ProcessPoolExecutor spreads work over every child, so all N children end up holding
        # a working set however few run at once. Measured on the small cap band (2026-08-20):
        # dispatch walked 6 -> 2 while six children stayed at ~9 GB and the box never left
        # 5-6% free. Resizing the pool is the only operation that returns a child to the OS.
        if worker is not None:
            try:
                out = worker_client.resize_pool(worker, gov.current, force=True)
                if out.get("changed"):
                    worker["capacity"] = max(1, int(out.get("capacity") or gov.current))
                    self.log(f"[{worker_name}] pool shrunk to {worker['capacity']} child(ren) "
                             f"-- resident memory released")
            except Exception as e:  # noqa: BLE001 -- an older worker has no /pool/resize;
                # the concurrency cut above still stands, which is the pre-existing behaviour.
                self.log(f"[{worker_name}] pool shrink to {gov.current} unavailable ({e})")
        return True

    def _remote_memory_watchdog(self) -> None:
        """Poll every active worker's memory on a timer and shed a slot while it is under the floor.

        Runs until the evaluator stops. Independent of trial completion, which is the whole reason
        it exists (see _REMOTE_MEM_POLL_S). Best-effort throughout: a worker that cannot be reached
        is left to the existing failure/re-admission path rather than being throttled on a guess.
        """
        while not self._stop.wait(_REMOTE_MEM_POLL_S):
            with self._worker_lock:
                active = list(self._active_workers)
            for w in active:
                name = w.get("name")
                try:
                    mem = (worker_client.health(w, timeout=10.0) or {}).get("memory") or {}
                    used = mem.get("used_pct")
                    if used is None:
                        continue
                    free_pct = 100.0 - float(used)
                    with self._remote_gov_lock:
                        gov = self._remote_govs.get(name)
                    if gov is None:
                        continue
                    if free_pct < _REMOTE_MEM_FLOOR_PCT:
                        self._shed_remote_slot(
                            name, gov,
                            f"{free_pct:.1f}% free < {_REMOTE_MEM_FLOOR_PCT:.0f}% floor "
                            f"({mem.get('free_mb')} MB of {mem.get('total_mb')} MB)",
                            worker=w)
                except Exception as e:  # noqa: BLE001 -- a poll failure must never stop the loop
                    self.log(f"[{name}] memory poll failed: {e!r}")

    def assess_remote(self, worker_name: str, mem: Any) -> None:
        """Feed a REMOTE trial's memory snapshot to that worker's own governor.

        Called by the master's result loop for every trial whose ``origin`` is a worker name.
        A 'release' verdict reduces that worker's concurrency by one slot, which its dispatcher
        threads observe before their next claim -- the remote analogue of parking a local slot.
        There is no cache-release equivalent to send (the remote's own in-process governor handles
        its pool), so shedding a slot IS the action here. Best-effort: never raises into the loop.
        """
        try:
            with self._remote_gov_lock:
                gov = self._remote_govs.get(worker_name)
            if gov is None:
                return
            verdict = gov.assess(mem)
            # ACT on the verdict. 'release' already decremented gov.current inside assess(), which
            # the dispatchers observe before their next claim. 'emergency' does NOT touch current
            # -- that branch was written for the LOCAL owner, which responds by breaking its
            # process pool. A remote owner cannot do that, so before this it shed NOTHING: on
            # 2026-08-18 remote150 fell to 85 MB free of 65 GB on the mid band (765 screened
            # symbols vs ~105 on large) and the emergency logged a pool-break that never happened.
            # Sheds ONE slot, and the watchdog steps again a minute later if the box is
            # still under the floor -- halving was tried and rejected: it overshoots and
            # cannot converge on the concurrency the band actually affords.
            if verdict == "emergency":
                self._shed_remote_slot(worker_name, gov, "emergency (trial snapshot)")
        except Exception as e:  # noqa: BLE001 -- governing must never fail a healthy trial
            self.log(f"remote governor assess failed for {worker_name}: {e!r}")

    def _recheck_down_workers(self) -> None:
        """Re-run pre-flight against previously-excluded/failed workers; re-admit any that
        recover. Best-effort and never raises — called off a background thread so a still-dead
        worker's connect timeout can't stall trial throughput."""
        with self._worker_lock:
            candidates = list(self._down_workers)
        for w in candidates:
            if self._stop.is_set():
                return
            if not self._preflight_worker(w, self._secrets):
                continue
            with self._worker_lock:
                if w in self._down_workers:
                    self._down_workers.remove(w)
                self._active_workers.append(w)
            cap = self._spawn_remote_dispatchers(w)
            self.log(f"worker {w['name']} recovered; re-admitted with {cap} slot(s)")
            self._report_fleet_state(active=True)

    def _maybe_recheck_async(self) -> None:
        """Kick off a re-admission pre-flight in the background if one isn't already running."""
        with self._worker_lock:
            if not self._down_workers:
                return
        if not self._recheck_lock.acquire(blocking=False):
            return  # a recheck is already in flight; don't pile up
        def _run():
            try:
                self._recheck_down_workers()
            finally:
                self._recheck_lock.release()
        self._spawn(_run, "worker-recheck")

    # -- workers -----------------------------------------------------------------------------
    def _consume_local_slot(self, idx: int) -> None:
        """Entry point per consumer thread: stamp the slot index, then run the claim loop."""
        self._consumer_slots.idx = idx
        self._consume_local()

    def _consume_local(self) -> None:
        from concurrent.futures.process import BrokenProcessPool
        from app.services.strategy_optimization_handler import _trial_worker
        while not self._stop.is_set():
            # THROTTLE: consumers with an index at/above the governor's current concurrency park
            # rather than claim. Checked before every claim so a release takes effect on the very
            # next individual, and a restore re-admits them just as fast.
            slot = getattr(self._consumer_slots, "idx", 0)
            gov = self.governor
            if gov is not None and slot >= gov.current:
                if slot not in self._parked_logged:
                    self._parked_logged.add(slot)
                    self.log(f"local consumer #{slot} PARKED (concurrency now {gov.current}); "
                             f"in-flight individuals finish normally, none are discarded")
                self._stop.wait(1.0)
                continue
            if gov is not None and slot in self._parked_logged:
                self._parked_logged.discard(slot)
                self.log(f"local consumer #{slot} RESUMED (concurrency now {gov.current})")
            job = self.broker.claim(worker_id="local")
            if job is None:
                self._stop.wait(0.05)
                continue
            pool = self.pool  # snapshot: may be swapped by another consumer's _recover_pool below
            try:
                out = pool.submit(_trial_worker, job["config"], job["fitness_metric"]).result()
            except BrokenProcessPool as e:
                # A crashed worker (e.g. a transient MemoryError) leaves the WHOLE pool unusable —
                # every future .submit() raises the SAME error immediately (no computation, near-
                # zero latency). Without recovery every local consumer thread spins in a tight
                # claim/fail/requeue loop for the rest of the run: local capacity is silently lost
                # AND the log floods with one line per loop iteration. Recreate once (thread-safe)
                # and requeue this trial so it isn't lost.
                self._recover_pool(pool, e)
                self.broker.requeue_one(job["trial_id"])
                continue
            except Exception as e:  # noqa: BLE001
                out = {"ok": False, "fitness": 0.0, "trades": 0, "error": repr(e), "fatal": False,
                       "origin": "local"}
                self.broker.post_result(job["trial_id"], out)
                continue
            # Ran in the MASTER's own pool, so its memory snapshot describes this box: tag it so
            # the master's governor acts on it (the remote-tagged ones go to their own governors).
            if isinstance(out, dict):
                out["origin"] = "local"
            self.broker.post_result(job["trial_id"], out)

    def swap_pool(self, fresh) -> None:
        """Install a freshly-built worker pool (master-driven RECYCLE, not a crash recovery).

        Call only at a batch boundary, when every job has resolved and the consumers are idle:
        consumers snapshot ``self.pool`` when they claim, so swapping under an in-flight claim
        would submit to a pool that is being torn down. Takes the same lock ``_recover_pool``
        uses, so a concurrent crash-recovery and a recycle cannot interleave.
        """
        with self._pool_lock:
            self.pool = fresh

    def _recover_pool(self, bad_pool, exc: Exception) -> None:
        """Replace a dead local pool with a fresh one. Thread-safe: if several consumers hit the
        SAME break around the same time, only the first to acquire the lock rebuilds it — the
        rest see ``self.pool is not bad_pool`` and no-op. No-op entirely (degrade to remote-only,
        the pre-existing behaviour) if no ``pool_factory`` was supplied at construction."""
        if self._pool_factory is None:
            self.log(f"local pool broken ({exc!r}); no pool_factory to recover, degrading to remote-only")
            return
        with self._pool_lock:
            if self.pool is not bad_pool:
                return  # another consumer already recreated it
            self.log(f"local pool broken ({exc!r}); recreating")
            _log_memory_diagnostics(self.log, "local pool crash")
            try:
                bad_pool.shutdown(wait=False, cancel_futures=True)
            except Exception:  # noqa: BLE001 — best-effort cleanup of the already-dead pool
                pass
            self.pool = self._pool_factory()

    def _dispatch_remote(self, w: dict, slot_idx: int = 0, epoch: int = None) -> None:
        failures = 0
        while not self._stop.is_set():
            # EPOCH FENCE. This dispatcher belongs to one generation; the moment the worker
            # is re-admitted (or marked down) the generation moves and this thread must
            # stand down rather than keep claiming alongside its own replacements.
            if epoch is not None:
                with self._worker_lock:
                    if self._worker_epochs.get(w["name"]) != epoch:
                        return
            # THROTTLE, mirroring _consume_local: a dispatcher whose slot index is at/above its
            # worker's current ceiling parks instead of claiming, so a memory-pressured remote
            # sheds concurrency on ITS OWN box. Checked before every claim so a release takes
            # effect on the very next trial. Slot 0 always survives, so a worker cannot be
            # throttled out of existence and stall the queue.
            gov = self._remote_govs.get(w["name"])
            if gov is not None and slot_idx >= gov.current:
                self._stop.wait(0.5)
                continue
            job = self.broker.claim(worker_id=f"remote:{w['name']}")
            if job is None:
                self._stop.wait(0.1)
                continue
            try:
                out = worker_client.run_trial(w, job["config"], job["fitness_metric"],
                                              timeout=self.trial_timeout)
                # RETRYABLE worker-side failure (e.g. the remote's own trial pool broke —
                # WinError 1450 killing a child mid-IPC): the worker returns HTTP 200 with
                # ``retryable: True`` after rebuilding its pool. The failure says nothing
                # about the GENOME, so requeue the trial (local/another slot runs it) instead
                # of accepting a poisoned failed-trial result.
                if isinstance(out, dict) and out.get("retryable"):
                    self.broker.requeue_one(job["trial_id"])
                    # BACKPRESSURE (worker full / under its memory floor) is the worker
                    # working as designed. Requeue and wait it out; do NOT count it as a
                    # failure, or a merely busy box is declared dead after three refusals.
                    if _is_backpressure(out):
                        self._stop.wait(_BACKPRESSURE_WAIT_S)
                        continue
                    failures += 1
                    self.log(f"worker {w['name']} returned retryable failure "
                             f"({failures}/{_MAX_WORKER_FAILURES}): {out.get('error')}")
                    if failures >= _MAX_WORKER_FAILURES:
                        if self._mark_worker_down(w, "repeated retryable failures"):
                            self.log(f"worker {w['name']} giving up (repeated retryable "
                                     f"failures); trials fall back to local/others")
                        return
                    self._stop.wait(2.0)
                    continue
                # WHICH BOX RAN THIS. The memory snapshot inside `out` was taken in the worker
                # process, so it describes THIS worker's machine, not the master's. Tag it so the
                # master routes it to this worker's governor instead of throttling its own pool.
                if isinstance(out, dict):
                    out["origin"] = w["name"]
                self.broker.post_result(job["trial_id"], out)
                failures = 0
            except Exception as e:  # noqa: BLE001 — push the trial back so local/another worker runs it
                self.broker.requeue_one(job["trial_id"])
                failures += 1
                self.log(f"worker {w['name']} run_trial failed ({failures}/{_MAX_WORKER_FAILURES}): {e}")
                if failures >= _MAX_WORKER_FAILURES:
                    if self._mark_worker_down(w, "dead"):
                        self.log(f"worker {w['name']} giving up (dead); trials fall back to "
                                 f"local/others")
                    return
                self._stop.wait(2.0)

    # -- coordinator -------------------------------------------------------------------------
    def execute_jobs(self, jobs: List[Job]) -> Iterator[Tuple[int, dict, str, dict]]:
        """Submit *jobs* to the broker; yield ``(index, flat, key, result)`` as each completes."""
        trial_map = {}
        for (i, flat, key, cfg) in jobs:
            tid = self.broker.submit_one(self.optimization_id, cfg, self.fitness_metric)
            trial_map[tid] = (i, flat, key)
        remaining = set(trial_map)
        completed = 0
        while remaining:
            ready = self.broker.wait_ready(remaining, timeout=2.0)
            if not ready:
                # Safety net: a trial claimed by a worker/thread that vanished without the caught
                # error path (e.g. a hard-killed dispatcher) is re-queued so it can't hang the gen.
                self.broker.requeue_stale(self.requeue_timeout)
                continue
            for tid, out in ready.items():
                i, flat, key = trial_map[tid]
                remaining.discard(tid)
                completed += 1
                # Re-admission cadence: every n_consumers individuals, give previously-excluded/
                # failed workers a chance to rejoin (see _maybe_recheck_async — no-op if none down).
                if completed % self.n_consumers == 0:
                    self._maybe_recheck_async()
                yield (i, flat, key, out)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2.0)
        self.broker.clear(self.optimization_id)
        # Release the engaged-slot counts so the panel doesn't show stale "busy" between jobs.
        self._report_fleet_state(active=False)

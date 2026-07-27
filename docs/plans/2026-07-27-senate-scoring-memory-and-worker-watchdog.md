# Senate Scoring Memory + Worker Watchdog Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Cut `FMPSenateTraderWeight`'s per-process RSS from ~5.2 GB of scoring cache to
~0.3 GB shared across all worker children, and replace the fixed 1800s remote-trial timeout
with a progress watchdog plus cancel-on-abandon.

**Architecture:** Three independent changes. (A) Replace the `dict[str, dict]` scoring cache
with an immutable memory-mapped columnar base + a small private overlay for new writes — the
base is shared by the OS page cache across all pool children instead of copied per child.
(B) Give the worker a `/cancel-job` endpoint so a trial the master abandoned stops burning a
slot. (C) Replace `_submit_and_poll`'s fixed deadline with a stall detector fed by a real
progress counter. A/B/C can ship independently; A is the root-cause fix.

**Tech Stack:** Python 3.11/3.12, numpy, `multiprocessing.shared_memory`/`np.memmap`,
FastAPI (worker), httpx (master), pytest.

---

## Background — measured, not estimated (2026-07-27)

Six remote trial timeouts during Senate S4 (opt 224). Investigation found remote150 at
**99.2% memory used, 509 MB available**, four pool children at 15433/15310/12817/12162 MB.

Root cause is not the trial count. `_load_scoring_cache`
(`packages/experts/ba2_experts/FMPSenateTraderWeight.py:1229`) does a plain `json.load()` into
`_WORKER_SCORING_CACHE`, a **module-level** dict — therefore **one full copy per pool child**:

| cache | entries | on disk | in RAM (measured) |
|---|---|---|---|
| `congress_skill_scores.json` | 3,406,775 | 452 MB | **2.22 GB** |
| `congress_confidence_scores.json` | 1,778,046 | 657 MB | **2.95 GB** |
| **total per process** | | 1.1 GB | **5.17 GB** |

**5.17 GB × 4 children ≈ 20.7 GB of the 55.7 GB is four identical copies of the same
read-only lookup table.** JSON → Python dict expands 4.7×: every entry is a ~50-char `str`
key (~104 B) plus a 4- or 12-key `dict` of floats (~547 B / ~1557 B) — for what is really 4
or 12 numbers.

Per-entry costs were measured with a `sys.getsizeof` deep-walk on representative entries;
entry counts by streaming byte-count of `"skill_score"` / `"confidence_modifier"`. Neither
required loading the files (the box had 1.0 GB free).

**Why it surfaced now:** a 2026-07-27 prewarm added ~3.4M scores, doubling the skill cache
from the 226 MB recorded in the code comment at line 49 to 452 MB. S1/S2/S3 fit; S4 did not.

**Why a swapping worker times out silently:** swapping is not an error, so the worker log is
clean. The trial just runs ~10× slower and crosses the fixed 1800s in
`worker_client.py:130`. The master polls every 2s (`worker_client.py:115-127`), raises
`TimeoutError`, and stops. The worker finishes later with nobody to collect, so from its side
it reads as "master submitted and never came back to poll", swept by its 6h
`_JOBS_MAX_ORPHAN_AGE`. **The orphan keeps computing and holds its slot for those 6h — so a
timeout removes capacity rather than freeing it.** That feedback loop is why S4 paced ~11h
against S2/S3's ~4.8h.

### Constraints that shape this plan

1. **Do not bump `ba2_trade_platform/version.py` while a distributed optimize is running.**
   The master snapshots the version once at job start; if the repo moves ahead the worker
   pulls the new version and fails the version match forever. Killed opt 218.
2. Phases B and C touch **worker-side** code (`app/worker_server.py`), so they only take
   effect after a version bump + worker pull. **Ship them between grids, never during one.**
3. Phase A touches the expert, which also runs on the worker — same rule.
4. The one-off JSON → columnar conversion needs a transient ~5 GB peak. **Run it when the box
   is idle**, not against a live grid.

---

## Phase A — Columnar, shareable scoring store

### Task A1: `ScoringStore` read path

**Files:**
- Create: `packages/common/ba2_common/core/scoring_store.py`
- Test: `packages/common/tests/test_scoring_store.py`

**Design.** The values have a **fixed schema** (always the same 4 / 12 numeric fields), which
is what makes columnar viable. Layout on disk, one directory per cache:

```
congress_skill_scores.store/
  meta.json        {"fields": ["skill_score", ...], "dtypes": [...], "n": 3406775}
  keys.blob        all key strings, UTF-8, concatenated
  offsets.npy      uint64[n+1] into keys.blob
  hashes.npy       uint64[n], SORTED (the lookup index)
  order.npy        uint64[n]  row id for hashes[i]
  col_<field>.npy  float64[n] or int64[n], one per field
```

Lookup: hash the key → `np.searchsorted(hashes, h)` → for each equal-hash candidate compare
the actual key bytes from `keys.blob` (so a 64-bit collision can never return a wrong score)
→ read row from the columns.

**Why the key blob is kept:** hashing alone is a silent-corruption risk. 3.4M keys in 2^64 is
~3e-7 collision probability — small, but a wrong skill score is a correctness bug, not a
performance one. The blob costs ~163 MB and makes it exact.

**Step 1: Write the failing test**

```python
# packages/common/tests/test_scoring_store.py
import numpy as np
from ba2_common.core.scoring_store import ScoringStore

FIELDS = ("skill_score", "scored_trades", "hit_rate", "avg_fwd_return_pct")

def test_roundtrip_get(tmp_path):
    src = {
        "Sheldon Whitehouse|854|2022-10-03|60|5|50|12":
            {"skill_score": 0.0, "scored_trades": 0, "hit_rate": None, "avg_fwd_return_pct": 0.0},
        "John Boozman|50|2022-10-03|60|5|50|12":
            {"skill_score": -0.30434782608695654, "scored_trades": 23,
             "hit_rate": 0.34782608695652173, "avg_fwd_return_pct": -1.301924681212208},
    }
    p = tmp_path / "skill.store"
    ScoringStore.build(p, src, FIELDS)
    s = ScoringStore.open(p)
    assert s.get("John Boozman|50|2022-10-03|60|5|50|12")["scored_trades"] == 23
    assert s.get("John Boozman|50|2022-10-03|60|5|50|12")["skill_score"] == \
        src["John Boozman|50|2022-10-03|60|5|50|12"]["skill_score"]
    assert s.get("nope") is None

def test_none_roundtrips_as_none(tmp_path):
    """hit_rate is legitimately None for unscored traders — it must NOT come back as nan."""
    src = {"k|1": {"skill_score": 0.0, "scored_trades": 0,
                   "hit_rate": None, "avg_fwd_return_pct": 0.0}}
    p = tmp_path / "s.store"
    ScoringStore.build(p, src, FIELDS)
    assert ScoringStore.open(p).get("k|1")["hit_rate"] is None

def test_hash_collision_cannot_return_the_wrong_entry(tmp_path, monkeypatch):
    """Force every key to the same hash; exact key comparison must still resolve correctly."""
    import ba2_common.core.scoring_store as m
    monkeypatch.setattr(m, "_hash_key", lambda k: np.uint64(42))
    src = {f"key{i}|x": {"skill_score": float(i), "scored_trades": i,
                         "hit_rate": None, "avg_fwd_return_pct": 0.0} for i in range(50)}
    p = tmp_path / "c.store"
    ScoringStore.build(p, src, FIELDS)
    s = ScoringStore.open(p)
    for i in range(50):
        assert s.get(f"key{i}|x")["scored_trades"] == i
    assert s.get("absent|x") is None
```

**Step 2: Run to verify it fails**

`.venv\Scripts\python.exe -m pytest packages/common/tests/test_scoring_store.py -v`
Expected: `ModuleNotFoundError: ba2_common.core.scoring_store`

**Step 3: Implement `build` + `open` + `get`**

Open with `np.load(..., mmap_mode="r")` for every `.npy`, and `mmap` the key blob. This is
what makes the base shared: four children mapping the same files hit the same OS page cache,
so the resident cost is paid **once for the box**, not once per child.

Represent `None` with a companion `col_<field>_isnull.npy` (`bool[n]`) only for fields that
are nullable per `meta.json` — `nan` is not a safe stand-in because a real score could be nan
and the two must stay distinguishable.

**Step 4: Verify tests pass, then commit**

```bash
git add packages/common/ba2_common/core/scoring_store.py packages/common/tests/test_scoring_store.py
git commit -m "feat(scoring): columnar mmapped ScoringStore read path"
```

---

### Task A2: Write overlay + Mapping protocol (drop-in for the existing dict)

**Files:**
- Modify: `packages/common/ba2_common/core/scoring_store.py`
- Test: `packages/common/tests/test_scoring_store.py`

The store must be a **drop-in** for the plain dict. Measured call sites in
`FMPSenateTraderWeight.py` use exactly three operations:

- `cache.get(key)` → dict or `None` (lines 1368, 1400, and the hold-days equivalent ~1336)
- `cache[key] = result` (lines 1376, 1408)
- iteration/`items()` for `json.dump(cache, f)` in `_save_scoring_cache` (line 1273)

**Design:** the mmapped base is **immutable**; new entries land in a private `dict` overlay.
`get` checks overlay first, then base. This preserves today's write semantics exactly (the
append-only delta jsonl in `_save_scoring_cache_throttled` is unchanged) and keeps the shared
base shareable — a copy-on-write scheme in one direction only.

**Step 1: Write the failing tests**

```python
def test_writes_go_to_overlay_and_are_readable(tmp_path):
    p = tmp_path / "s.store"
    ScoringStore.build(p, {"a|1": _v(1)}, FIELDS)
    s = ScoringStore.open(p)
    s["b|2"] = _v(2)
    assert s.get("b|2")["scored_trades"] == 2
    assert s.get("a|1")["scored_trades"] == 1

def test_overlay_shadows_base(tmp_path):
    p = tmp_path / "s.store"
    ScoringStore.build(p, {"a|1": _v(1)}, FIELDS)
    s = ScoringStore.open(p)
    s["a|1"] = _v(99)
    assert s.get("a|1")["scored_trades"] == 99

def test_items_merges_base_and_overlay_for_compaction(tmp_path):
    p = tmp_path / "s.store"
    ScoringStore.build(p, {"a|1": _v(1)}, FIELDS)
    s = ScoringStore.open(p)
    s["b|2"] = _v(2)
    assert dict(s.items()).keys() == {"a|1", "b|2"}
    assert len(s) == 2

def test_base_is_never_mutated_on_disk(tmp_path):
    """The shared base must stay byte-identical — another process has it mapped."""
    p = tmp_path / "s.store"
    ScoringStore.build(p, {"a|1": _v(1)}, FIELDS)
    before = (p / "col_scored_trades.npy").read_bytes()
    s = ScoringStore.open(p)
    s["a|1"] = _v(99)
    assert (p / "col_scored_trades.npy").read_bytes() == before
```

**Step 2–4:** implement `__setitem__`/`__getitem__`/`__contains__`/`__len__`/`items()` on
`collections.abc.MutableMapping`, verify, commit.

---

### Task A3: Benchmark the per-hit reconstruction cost — **decision gate**

**Files:**
- Create: `test_files/bench_scoring_store.py` (ad-hoc probe, not collected by pytest)

**This task exists because the change has a real downside and it must be measured, not
assumed.** Today `cache.get(key)` returns a *reference* to a stored dict — zero allocation.
A columnar store must **rebuild** a dict per hit (4 or 12 floats). On a path called millions
of times per trial that could trade memory for CPU.

**Step 1:** benchmark 1M `get` hits against (a) today's plain dict, (b) `ScoringStore`
returning a fresh `dict`, (c) `ScoringStore` returning a `namedtuple`.

**Step 2: Decide.**
- If (b) is within ~10% of (a) → keep `dict`, no call-site churn, done.
- If (b) is materially slower → adopt (c) and update the ~3 call sites
  (`FMPSenateTraderWeight.py:1368-1370, 1400-1402`, hold-days ~1336) to attribute access.
  A namedtuple is both faster to build and smaller.

Record the numbers in the commit message. **Do not skip this gate** — shipping a 2× CPU
regression to save memory on a CPU-bound GA would be a bad trade.

---

### Task A4: One-off converter + `_load_scoring_cache` integration

**Files:**
- Create: `tools/convert_scoring_caches.py`
- Modify: `packages/experts/ba2_experts/FMPSenateTraderWeight.py:1229-1260`

**Step 1:** converter reads the existing JSON **plus** its `.delta.jsonl` (same merge order as
`_load_scoring_cache:1248-1257`, so the delta wins) and writes a `.store/` directory.
Peak ~5 GB — run on an idle box. Keep the JSON; do not delete it in this task.

**Step 2:** `_load_scoring_cache` prefers `<name>.store/` when present, else falls back to
today's `json.load` path **unchanged**. Fallback is deliberate: a missing/corrupt store must
degrade to working-but-fat, never to broken.

**Step 3: Write the failing test** (`packages/experts/tests/test_senate_scoring_store.py`):
identical `get`/`set` results from the JSON path and the store path for a sampled 10k keys;
and store-absent → JSON fallback still works.

**Step 4:** the existing 39 tests in `packages/experts/tests/test_senate_gather_process.py`
**must stay green** — they are the regression net for this expert.

```bash
.venv\Scripts\python.exe -m pytest packages/experts/tests/ -v
```

---

### Task A5: Measure the win on the real box

**Step 1:** run one Senate trial locally, record peak RSS before/after (`worker_client.memory()`
against a local worker, or `psutil` around `_trial_worker`).

**Step 2:** expected ~5.17 GB → ~0.3 GB private per child, base shared. **Success criterion:
4 concurrent trials leave >8 GB free on a 64 GB box.** If met, raise
`max_remote_worker_slots` (line 109) from 4 and update its comment with the new measured
footprint — the current comment's "11-12GB" is stale (it is a floor; S4 measured 15.4 GB).

**Step 3:** update `docs/` and the memory note. Commit.

---

## Phase B — Cancel-on-abandon (worker-side; ship between grids)

Higher value than the watchdog and simpler: today a given-up trial runs to completion for
nobody, holding a slot up to 6h.

### Task B1: Cooperative cancel

**Files:**
- Modify: `testplatform/backend/app/worker_server.py` (`_JOBS` registry ~line 76, new endpoint)
- Test: `testplatform/backend/tests/test_worker_server.py`

**Design:** `ProcessPoolExecutor` cannot cancel a *running* future, and killing a child raises
`BrokenProcessPool` for every other in-flight trial on that worker — unacceptable when three
healthy trials are sharing the pool. So: **cooperative** cancellation via a per-job
`multiprocessing.Event` the trial polls at bar boundaries (the same hook Phase C uses for
progress). `POST /cancel-job/{job_id}` sets the event; the trial raises and returns a
`{"ok": False, "cancelled": True}` result.

**Tests:** cancel before start → future cancelled, never runs. Cancel mid-run → event set,
job leaves the registry, **pool not broken** (a subsequent submit still succeeds). Cancel an
unknown id → 404, no raise.

### Task B2: Master calls cancel when it gives up

**Files:**
- Modify: `testplatform/backend/app/services/worker_client.py:112-127`
- Test: `testplatform/backend/tests/test_worker_client_timeout.py` (new)

On `TimeoutError` — and on the caller abandoning — best-effort `POST /cancel-job/{id}`,
swallowing errors (a cancel failure must never mask the original timeout). Assert the cancel
is issued and that a failing cancel still surfaces the `TimeoutError`.

---

## Phase C — Progress watchdog replacing the fixed deadline

### Task C1: Progress counter plumbed to `/job-status`

**Files:**
- Modify: `testplatform/backend/app/worker_server.py`, `app/services/strategy_optimization_handler.py`
  (`_trial_worker`), `testplatform/backend/app/services/backtest/daily_engine.py` (bar loop)

Shared `multiprocessing.Value("q", 0)` per job, bumped every N bars from the engine's existing
bar loop; `/job-status` returns `{"status": "running", "progress": {"bars": n, "updated_at": t}}`.
The hook must be **optional** (`None` in a normal in-process backtest) so non-distributed runs
are untouched.

### Task C2: Stall detection in `_submit_and_poll`

**Files:** `testplatform/backend/app/services/worker_client.py:93-127`

Replace the single `deadline` with:
- `stall_timeout` (default **900s**) — reset on every observed progress advance
- `absolute_timeout` (default **6h**) — bounds true pathology
- unchanged 404 → `WorkerJobLost` fast path

**Tests:** progress advancing past the old 1800s does **not** time out (this is the S4 case —
the regression test for this whole incident); no progress for `stall_timeout` **does**; a
worker with no `progress` field (older version) falls back to today's fixed-deadline
behaviour, so a mixed-version fleet is safe.

---

## Phase D — Rollout

1. Land Phase A, run the converter on an idle box, verify Task A5's criterion.
2. Land B + C.
3. **Bump `APP_VERSION`** (still frozen at `2026.07.986` from the S4 grid), push, let
   remote150 self-update, confirm `/version` matches.
4. Re-run one Senate strategy and confirm zero timeouts and lower RSS.
5. Update `[[reference-distributed-optimize-traps]]` with the new footprint and slot count.

## Risks

| Risk | Mitigation |
|---|---|
| Per-hit dict rebuild is slower than a dict deref | Task A3 is a hard decision gate with a namedtuple fallback |
| Store and JSON disagree → silently wrong scores | Task A4 equivalence test over 10k sampled keys; 39 existing expert tests must stay green |
| 64-bit hash collision returns wrong entry | Exact key comparison against `keys.blob`; forced-collision test in A1 |
| Cancel breaks the pool for sibling trials | Cooperative event, never `kill`; explicit "pool not broken" test |
| Mixed worker versions during rollout | C2 falls back to the fixed deadline when `progress` is absent |
| Converter OOMs the box | Run only when idle; JSON path retained as fallback |

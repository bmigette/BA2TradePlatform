"""Ad-hoc harness: can the parquet option reader share its arrays instead of copying them?

NOT a pytest test (lives in tests_scripts/ deliberately). It answers one question:

    ``parquet_options_provider._RawUnderlying`` holds, per underlying, PRIVATE per-process
    numpy arrays. A 32-worker pool therefore holds 32 copies of the same immutable bytes.
    Can those arrays become SHARED read-only memory (a page-cache-backed ``np.load(...,
    mmap_mode='r')``, or ``multiprocessing.shared_memory``) without losing read throughput?

WHAT IT REPRODUCES. The real hot path, not a synthetic scan. ``get_chain`` filters contracts
by expiry, then PER CONTRACT bisects ``bar_ord`` inside ``[starts[ci], stops[ci])`` for the
as-of row and reads close/bid/ask/iv/volume off that row. The reader does that bisect with
``bisect_right`` over an INTERNED PYTHON LIST (``bar_ord_l``), because that measured 0.05 us
against 0.73 us for ``np.searchsorted`` on an array slice (see the comment above
``_Underlying.latest_row_on_or_before``).

That list projection is the crux of the sharing question: A PYTHON LIST OF A MEMMAP IS NOT
SHARED. Materialising ``bar_ord_l`` in a worker pulls every page of ``bar_ord`` into that
process AND allocates 8 bytes/row of private pointers, which would give back much of what the
mmap saves. So both access styles are measured under both storage modes:

    style=list  -- bisect_right over the interned python list (what the reader does today)
    style=ss    -- np.searchsorted over a memmap SLICE (no list, nothing private)

and the cost of dropping the list projections is the ``ss`` vs ``list`` gap.

Greeks are deliberately NOT in the loop: ``compute_iv_and_greeks`` is ~11 us/call of pure
arithmetic that is identical under every storage mode and would swamp the array-read signal
this harness exists to measure.

USAGE (Windows)
    C:/Users/basti/ba2-venvs/test/Scripts/python.exe \
        testplatform/backend/tests_scripts/bench_option_array_sharing.py \
        --root "C:/Users/basti/Documents/ba2/common/cache/ThetaDataOptionsProvider" \
        --tmp  "<scratch>/bench_tmp" --out results_win.json

USAGE (Linux)
    /opt/ba2worker/ba2-venvs/test/bin/python bench_option_array_sharing.py \
        --root /home/debian/ba2-grid/home/common/cache/ThetaDataOptionsProvider \
        --tmp  /home/debian/ba2-grid/bench_tmp --out results_linux.json

The build step is idempotent: it writes one .npy per (symbol, array) under --tmp and skips
symbols already built (--rebuild forces). Delete --tmp when done.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import pickle
import platform
import random
import sys
import threading
import time
from bisect import bisect_right
from datetime import date

import numpy as np
import psutil

# ---------------------------------------------------------------------------
# The array set _RawUnderlying holds. Kept COMPLETE (not just the hot columns) so the
# resident-footprint numbers are the reader's real footprint, not a flattering subset.
# ---------------------------------------------------------------------------
ROW_F64 = ("open", "high", "low", "close", "volume", "open_interest", "vendor_iv", "bid", "ask")
ROW_I32 = ("bar_ord",)
CON_I32 = ("starts", "stops", "c_expiry_ord")
CON_F64 = ("c_strike",)
CON_BOOL = ("c_is_call",)
ALL_ARRAYS = ROW_F64 + ROW_I32 + CON_I32 + CON_F64 + CON_BOOL

#: Expiry horizon of a simulated chain query, in days. The real engine asks for a window
#: around its DTE band; 60 days over this tree yields ~500-1500 contracts per chain read,
#: which is the per-op cost the reader actually pays.
CHAIN_HORIZON_DAYS = 60


def _npy(tmp: str, sym: str, name: str) -> str:
    return os.path.join(tmp, f"{sym}__{name}.npy")


# ===========================================================================
# BUILD (parent, once)
# ===========================================================================
def _iso_to_ordinal_array(series) -> np.ndarray:
    """Copy of the provider's helper (same dtype, same semantics)."""
    return np.array([date.fromisoformat(str(s)).toordinal() for s in series], dtype=np.int32)


def build_symbol(root: str, sym: str, tmp: str) -> dict:
    """Read one symbol's whole parquet history and write _RawUnderlying's arrays as .npy."""
    import pandas as pd

    paths = sorted(glob.glob(os.path.join(root, sym, "exp=*", f"{sym}_*_1d.parquet")))
    if not paths:
        raise SystemExit(f"no parquet files for {sym} under {root}")
    df = pd.concat((pd.read_parquet(p) for p in paths), ignore_index=True)
    # EXACTLY _RawUnderlying.__init__'s ordering and derivations.
    df = df.sort_values(["occ_symbol", "bar_date"], kind="mergesort").reset_index(drop=True)
    occ = df["occ_symbol"].astype(str).to_numpy(dtype=object)
    n = len(occ)
    is_new = np.empty(n, dtype=bool)
    is_new[0] = True
    if n > 1:
        is_new[1:] = occ[1:] != occ[:-1]
    starts = np.flatnonzero(is_new).astype(np.int32)
    stops = np.append(starts[1:], np.int32(n)).astype(np.int32)

    out = {
        "starts": starts,
        "stops": stops,
        "bar_ord": _iso_to_ordinal_array(df["bar_date"].to_numpy(dtype=object)),
        "c_strike": df["strike"].to_numpy(dtype="float64")[starts],
        "c_expiry_ord": _iso_to_ordinal_array(df["expiry"].to_numpy(dtype=object)[starts]),
        "c_is_call": (df["option_type"].astype(str).to_numpy(dtype=object)[starts] == "call"),
    }
    for col in ("open", "high", "low", "close"):
        out[col] = df[col].to_numpy(dtype="float64", na_value=np.nan)
    out["vendor_iv"] = df["iv"].to_numpy(dtype="float64", na_value=np.nan)
    out["volume"] = df["volume"].to_numpy(dtype="float64", na_value=np.nan)
    out["open_interest"] = df["open_interest"].to_numpy(dtype="float64", na_value=np.nan)
    has_quotes = "bid" in df.columns and "ask" in df.columns
    out["bid"] = df["bid"].to_numpy(dtype="float64", na_value=np.nan) if has_quotes else np.full(n, np.nan)
    out["ask"] = df["ask"].to_numpy(dtype="float64", na_value=np.nan) if has_quotes else np.full(n, np.nan)

    nbytes = 0
    for name in ALL_ARRAYS:
        np.save(_npy(tmp, sym, name), out[name])
        nbytes += out[name].nbytes
    dates = sorted(int(x) for x in np.unique(out["bar_ord"]))
    return {
        "symbol": sym, "n_rows": int(n), "n_contracts": int(len(starts)),
        "array_bytes": int(nbytes), "dates": dates,
        "files": len(paths), "has_quotes": bool(has_quotes),
    }


def build_all(root: str, symbols: list, tmp: str, rebuild: bool) -> dict:
    os.makedirs(tmp, exist_ok=True)
    meta_path = os.path.join(tmp, "_meta.json")
    meta = {}
    if os.path.exists(meta_path) and not rebuild:
        meta = json.load(open(meta_path))
    for sym in symbols:
        if sym in meta and not rebuild and all(
                os.path.exists(_npy(tmp, sym, n)) for n in ALL_ARRAYS):
            print(f"[build] {sym}: cached ({meta[sym]['n_rows']:,} rows)", flush=True)
            continue
        t0 = time.perf_counter()
        meta[sym] = build_symbol(root, sym, tmp)
        print(f"[build] {sym}: {meta[sym]['n_rows']:,} rows / {meta[sym]['n_contracts']:,} "
              f"contracts / {meta[sym]['array_bytes']/2**20:.1f} MB arrays "
              f"in {time.perf_counter()-t0:.1f}s", flush=True)
        json.dump(meta, open(meta_path, "w"))
    json.dump(meta, open(meta_path, "w"))
    return meta


# ===========================================================================
# WORKER SIDE
# ===========================================================================
class Arrays:
    """One underlying's arrays, loaded under one storage mode + one access style."""

    __slots__ = ALL_ARRAYS + ("bar_ord_l", "starts_l", "stops_l", "_shms")

    def __init__(self, sym: str, tmp: str, mode: str, style: str, shm_names: dict | None):
        self._shms = []
        for name in ALL_ARRAYS:
            if mode == "private":
                arr = np.load(_npy(tmp, sym, name))
            elif mode == "memmap":
                # np.asarray: np.load(mmap_mode=...) hands back an np.memmap, an ndarray
                # SUBCLASS, and scalar indexing on a subclass pays an extra type check per
                # read -- measurable here because the hot loop does 5 scalar reads per
                # contract. asarray returns a BASE ndarray VIEW over the same mapping (no
                # copy, nothing paged in; the memmap survives as the view's .base), so this
                # measures sharing rather than subclass dispatch. mode=memmap_sub keeps the
                # subclass so the difference is on the record.
                arr = np.asarray(np.load(_npy(tmp, sym, name), mmap_mode="r"))
            elif mode == "memmap_sub":
                arr = np.load(_npy(tmp, sym, name), mmap_mode="r")
            elif mode == "shm":
                from multiprocessing import shared_memory
                spec = shm_names[f"{sym}__{name}"]
                shm = shared_memory.SharedMemory(name=spec["name"])
                self._shms.append(shm)  # must outlive the view
                arr = np.ndarray(tuple(spec["shape"]), dtype=np.dtype(spec["dtype"]),
                                 buffer=shm.buf)
            else:
                raise ValueError(mode)
            setattr(self, name, arr)

        if style == "list":
            # The reader's interned projection, verbatim: ~1,500 distinct ordinals stand in
            # for millions of rows, so setdefault keeps the list to POINTERS (8 B/row) rather
            # than millions of distinct int objects (28 B each).
            seen = {}
            self.bar_ord_l = [seen.setdefault(v, v) for v in self.bar_ord.tolist()]
            self.starts_l = self.starts.tolist()
            self.stops_l = self.stops.tolist()
        else:
            self.bar_ord_l = self.starts_l = self.stops_l = None


def chain_read_list(A: Arrays, as_of_ord: int) -> tuple:
    """get_chain's loop, bisect_right over the interned python list (today's reader)."""
    keep = (A.c_expiry_ord >= as_of_ord) & (A.c_expiry_ord <= as_of_ord + CHAIN_HORIZON_DAYS)
    bl, sl, el = A.bar_ord_l, A.starts_l, A.stops_l
    close, bid, ask, iv, vol = A.close, A.bid, A.ask, A.vendor_iv, A.volume
    tot = 0.0
    cnt = 0
    for ci in np.flatnonzero(keep).tolist():
        lo = sl[ci]
        j = bisect_right(bl, as_of_ord, lo, el[ci])
        if j <= lo:
            continue  # contract had not traded yet on/before the clock
        i = j - 1
        b = bid[i]
        a = ask[i]
        c = close[i]
        m = (b + a) * 0.5 if (b == b and a == a) else c
        if m == m:
            tot += float(m)
        v = iv[i]
        if v == v:
            tot += float(v)
        q = vol[i]
        if q == q:
            tot += float(q) * 1e-6
        cnt += 1
    return tot, cnt


def chain_read_ss(A: Arrays, as_of_ord: int) -> tuple:
    """Same answers with NO python projections: np.searchsorted over the array slice."""
    keep = (A.c_expiry_ord >= as_of_ord) & (A.c_expiry_ord <= as_of_ord + CHAIN_HORIZON_DAYS)
    bo, st, sp = A.bar_ord, A.starts, A.stops
    close, bid, ask, iv, vol = A.close, A.bid, A.ask, A.vendor_iv, A.volume
    tot = 0.0
    cnt = 0
    for ci in np.flatnonzero(keep).tolist():
        lo = int(st[ci])
        j = lo + int(np.searchsorted(bo[lo:int(sp[ci])], as_of_ord, side="right"))
        if j <= lo:
            continue
        i = j - 1
        b = bid[i]
        a = ask[i]
        c = close[i]
        m = (b + a) * 0.5 if (b == b and a == a) else c
        if m == m:
            tot += float(m)
        v = iv[i]
        if v == v:
            tot += float(v)
        q = vol[i]
        if q == q:
            tot += float(q) * 1e-6
        cnt += 1
    return tot, cnt


def _mem() -> dict:
    p = psutil.Process()
    mi = p.memory_info()
    out = {"rss_mb": mi.rss / 2**20}
    try:
        out["uss_mb"] = p.memory_full_info().uss / 2**20
    except Exception:
        out["uss_mb"] = None
    out["peak_mb"] = getattr(mi, "peak_wset", 0) / 2**20 or None
    if out["peak_mb"] is None:
        try:
            import resource
            out["peak_mb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
        except Exception:
            pass
    return out


def worker_main(wid: int, cfg: dict, barrier, q) -> None:
    syms = cfg["symbols"]
    t0 = time.perf_counter()
    arrays = {s: Arrays(s, cfg["tmp"], cfg["mode"], cfg["style"], cfg.get("shm_names"))
              for s in syms}
    t_load = time.perf_counter() - t0
    mem_load = _mem()

    dates = {s: cfg["dates"][s] for s in syms}
    fn = chain_read_list if cfg["style"] == "list" else chain_read_ss
    passes = []
    for p_idx in range(cfg["passes"]):
        # SAME seed each pass: pass 1 vs pass 2 must differ ONLY in page residency, which is
        # the whole point of the cold/warm split for memmap.
        rng = random.Random(cfg["seed"] + wid)
        plan = []
        for _ in range(cfg["ops"]):
            s = rng.choice(syms)
            plan.append((s, rng.choice(dates[s])))
        barrier.wait()
        t_start = time.time()
        tot = 0.0
        rows = 0
        done = 0
        deadline = t_start + cfg["deadline_s"]
        for sym, d in plan:
            t, c = fn(arrays[sym], d)
            tot += t
            rows += c
            done += 1
            if not (done & 15) and time.time() > deadline:
                break  # wall-clock guard; ops actually done is what gets reported
        t_end = time.time()
        passes.append({"pass": p_idx, "ops": done, "rows": rows, "checksum": round(tot, 3),
                       "t_start": t_start, "t_end": t_end, "secs": t_end - t_start,
                       "ops_s": done / (t_end - t_start), "rows_s": rows / (t_end - t_start)})
    q.put({"wid": wid, "t_load": t_load, "mem_load": mem_load, "mem_end": _mem(),
           "passes": passes})
    # Hold until the parent has read every result, so its memory sampler sees the real
    # steady-state footprint of N workers rather than a staggered teardown.
    barrier.wait()


# ===========================================================================
# PARENT SIDE
# ===========================================================================
class SysMemSampler(threading.Thread):
    def __init__(self, period=0.25):
        super().__init__(daemon=True)
        self.period = period
        self.stop_flag = False
        vm = psutil.virtual_memory()
        self.base = vm.used
        self.peak = vm.used
        self.avail_base = vm.available
        self.avail_min = vm.available

    def run(self):
        while not self.stop_flag:
            vm = psutil.virtual_memory()
            self.peak = max(self.peak, vm.used)
            self.avail_min = min(self.avail_min, vm.available)
            time.sleep(self.period)

    @property
    def delta_mb(self):
        """Peak COMMITTED (anonymous) growth. Page cache counts as available on both OSes,
        so a memmap's shared pages do NOT show up here -- that is the point."""
        return (self.peak - self.base) / 2**20

    @property
    def avail_drop_mb(self):
        """Peak drop in AVAILABLE ram -- catches page cache too."""
        return (self.avail_base - self.avail_min) / 2**20


def run_case(mode: str, style: str, nproc: int, cfg: dict, ctx) -> dict:
    cfg = dict(cfg, mode=mode, style=style)
    barrier = ctx.Barrier(nproc + 1)
    q = ctx.Queue()
    sampler = SysMemSampler()
    sampler.start()
    procs = [ctx.Process(target=worker_main, args=(i, cfg, barrier, q)) for i in range(nproc)]
    t_spawn = time.perf_counter()
    for p in procs:
        p.start()
    results = []
    try:
        for _ in range(cfg["passes"]):
            barrier.wait(timeout=cfg["deadline_s"] + 600)   # release the pass
        for _ in range(nproc):
            results.append(q.get(timeout=600))
    except Exception as e:
        for p in procs:
            if p.is_alive():
                p.terminate()
        sampler.stop_flag = True
        raise RuntimeError(f"{mode}/{style}/{nproc} failed: {e!r} "
                           f"(exitcodes={[p.exitcode for p in procs]})") from e
    wall = time.perf_counter() - t_spawn
    sampler.stop_flag = True
    mem_delta = sampler.delta_mb
    avail_drop = sampler.avail_drop_mb
    try:
        barrier.wait(timeout=120)   # let workers exit
    except Exception:
        pass
    for p in procs:
        p.join(timeout=60)
    sampler.join(timeout=2)

    out = {"mode": mode, "style": style, "nproc": nproc, "wall_s": wall,
           "sys_mem_peak_delta_mb": mem_delta, "sys_avail_drop_mb": avail_drop,
           "load_s_mean": sum(r["t_load"] for r in results) / nproc,
           "rss_mb_mean": sum(r["mem_end"]["rss_mb"] for r in results) / nproc,
           "uss_mb_mean": (sum(r["mem_end"]["uss_mb"] for r in results) / nproc
                           if results[0]["mem_end"]["uss_mb"] is not None else None),
           "peak_mb_mean": (sum(r["mem_end"]["peak_mb"] for r in results) / nproc
                            if results[0]["mem_end"]["peak_mb"] else None),
           "passes": []}
    checks = set()
    for p_idx in range(cfg["passes"]):
        ps = [r["passes"][p_idx] for r in results]
        span = max(x["t_end"] for x in ps) - min(x["t_start"] for x in ps)
        ops = sum(x["ops"] for x in ps)
        out["passes"].append({
            "pass": p_idx, "ops_total": ops, "span_s": span,
            "ops_s_per_proc": sum(x["ops_s"] for x in ps) / nproc,
            "ops_s_aggregate": ops / span,
            "rows_s_aggregate": sum(x["rows"] for x in ps) / span,
        })
        checks.update(x["checksum"] for x in ps)
    out["checksums"] = sorted(checks)
    return out


def make_shm(meta: dict, symbols: list, tmp: str) -> tuple:
    """Parent-owned shared_memory blocks holding every array (mode='shm')."""
    from multiprocessing import shared_memory
    blocks, names = [], {}
    for s in symbols:
        for name in ALL_ARRAYS:
            a = np.load(_npy(tmp, s, name))
            shm = shared_memory.SharedMemory(create=True, size=max(a.nbytes, 1))
            view = np.ndarray(a.shape, dtype=a.dtype, buffer=shm.buf)
            view[:] = a[:]
            blocks.append(shm)
            names[f"{s}__{name}"] = {"name": shm.name, "shape": list(a.shape),
                                     "dtype": a.dtype.str}
            del a, view
    return blocks, names


def gotcha_probes(tmp: str, sym: str) -> dict:
    """Two things a 'just mmap it' patch would trip over. Measured, not assumed."""
    import shutil
    out = {}
    src = _npy(tmp, sym, "close")
    path = os.path.join(tmp, "_probe_copy.npy")   # never a live array file
    shutil.copyfile(src, path)
    mm = np.load(path, mmap_mode="r")
    # 1. Pickling a memmap COPIES it -- so a memmap must never be passed as a spawn arg or
    #    returned through a Queue; each worker has to open its own.
    try:
        blob = pickle.dumps(mm[:200000], protocol=pickle.HIGHEST_PROTOCOL)
        out["pickle_of_memmap_slice_bytes"] = len(blob)
        out["pickle_materialises"] = len(blob) > 200000 * 8 * 0.9
    except Exception as e:
        out["pickle_error"] = repr(e)
    # 2. Can the backing file be deleted/replaced while a worker holds the map?
    try:
        os.remove(path)
        out["unlink_while_mapped"] = "allowed (posix unlink semantics)"
    except OSError as e:
        out["unlink_while_mapped"] = (f"REFUSED {type(e).__name__} "
                                      f"winerror={getattr(e, 'winerror', None)} "
                                      f"errno={e.errno}")
    del mm
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
    return out


def drop_page_cache(tmp: str) -> int:
    """Evict this benchmark's own .npy files from the page cache (Linux, unprivileged)."""
    n = 0
    for f in sorted(glob.glob(os.path.join(tmp, "*.npy"))):
        fd = os.open(f, os.O_RDONLY)
        try:
            os.fsync(fd)
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            n += 1
        finally:
            os.close(fd)
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--tmp", required=True)
    ap.add_argument("--symbols", default="INTC,KO,ABT,CSCO,T,VZ")
    ap.add_argument("--procs", default="1,8,32")
    ap.add_argument("--modes", default="private,memmap,shm",
                    help="private | memmap (base-ndarray view of the map) | memmap_sub "
                         "(raw np.memmap subclass) | shm (multiprocessing.shared_memory)")
    ap.add_argument("--styles", default="list,ss")
    ap.add_argument("--ops", type=int, default=400, help="chain reads per worker per pass")
    ap.add_argument("--passes", type=int, default=2, help="2 = cold pass then warm pass")
    ap.add_argument("--deadline", type=float, default=90.0, help="per-pass wall guard (s)")
    ap.add_argument("--seed", type=int, default=20260914)
    ap.add_argument("--mem-frac", type=float, default=0.70,
                    help="refuse a case whose projected footprint exceeds this much of "
                         "AVAILABLE ram (protects whatever else runs on the host)")
    ap.add_argument("--drop-cache", action="store_true",
                    help="LINUX ONLY: posix_fadvise(DONTNEED) every .npy before each case, "
                         "so pass 0 is a genuinely DISK-cold read. Needs no root. Windows "
                         "has no unprivileged equivalent, so 'cold' there means first-touch "
                         "with the page cache already warm.")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--build-only", action="store_true")
    ap.add_argument("--out", default="bench_option_array_sharing.json")
    args = ap.parse_args()

    symbols = [s for s in args.symbols.split(",") if s]
    meta = build_all(args.root, symbols, args.tmp, args.rebuild)
    if args.build_only:
        return

    import multiprocessing as mp
    ctx = mp.get_context("spawn")  # what the platform's pools use, on both OSes

    vm = psutil.virtual_memory()
    arr_mb = sum(meta[s]["array_bytes"] for s in symbols) / 2**20
    env = {
        "host": platform.node(), "os": platform.platform(), "python": sys.version.split()[0],
        "numpy": np.__version__, "cpu_logical": os.cpu_count(),
        "ram_total_gb": vm.total / 2**30, "ram_avail_gb": vm.available / 2**30,
        "symbols": symbols, "arrays_mb": arr_mb,
        "rows_total": sum(meta[s]["n_rows"] for s in symbols),
        "contracts_total": sum(meta[s]["n_contracts"] for s in symbols),
        "per_symbol": {s: {"rows": meta[s]["n_rows"], "contracts": meta[s]["n_contracts"],
                           "mb": meta[s]["array_bytes"] / 2**20} for s in symbols},
        "ops_per_worker": args.ops, "chain_horizon_days": CHAIN_HORIZON_DAYS,
    }
    print(json.dumps(env, indent=2, default=str), flush=True)

    cfg = {"tmp": os.path.abspath(args.tmp), "symbols": symbols, "ops": args.ops,
           "passes": args.passes, "seed": args.seed, "deadline_s": args.deadline,
           "dates": {s: meta[s]["dates"] for s in symbols}}

    probes = gotcha_probes(args.tmp, symbols[0])
    print("[probe]", json.dumps(probes), flush=True)

    results = []
    shm_blocks = None
    for mode in args.modes.split(","):
        if mode == "shm":
            try:
                t0 = time.perf_counter()
                shm_blocks, shm_names = make_shm(meta, symbols, args.tmp)
                cfg["shm_names"] = shm_names
                print(f"[shm] {len(shm_blocks)} blocks / {arr_mb:.0f} MB in "
                      f"{time.perf_counter()-t0:.1f}s", flush=True)
            except Exception as e:
                print(f"[shm] UNAVAILABLE: {e!r}", flush=True)
                results.append({"mode": "shm", "skipped": repr(e)})
                continue
        for style in args.styles.split(","):
            for nproc in (int(x) for x in args.procs.split(",")):
                avail = psutil.virtual_memory().available / 2**20
                # private holds one full copy per worker; memmap/shm hold one system-wide
                # (plus the list projection, ~8 B/row, when style=list).
                per = arr_mb if mode == "private" else 0.0
                if style == "list":
                    per += sum(meta[s]["n_rows"] for s in symbols) * 8 / 2**20
                if per * nproc > args.mem_frac * avail:
                    msg = (f"projected {per*nproc/1024:.1f} GB > {args.mem_frac:.0%} of "
                           f"{avail/1024:.1f} GB available")
                    print(f"[skip] {mode}/{style}/{nproc}: {msg}", flush=True)
                    results.append({"mode": mode, "style": style, "nproc": nproc,
                                    "skipped": msg})
                    continue
                if args.drop_cache:
                    dropped = drop_page_cache(args.tmp)
                    print(f"[cold] fadvise DONTNEED on {dropped} files", flush=True)
                print(f"[run ] {mode}/{style}/{nproc} ...", flush=True, end=" ")
                r = run_case(mode, style, nproc, cfg, ctx)
                warm = r["passes"][-1]
                print(f"agg {warm['ops_s_aggregate']:8.1f} ops/s | "
                      f"proc {warm['ops_s_per_proc']:7.1f} | rss {r['rss_mb_mean']:7.1f} MB | "
                      f"uss {(r['uss_mb_mean'] or -1):7.1f} MB | "
                      f"sysdelta {r['sys_mem_peak_delta_mb']/1024:.2f} GB | "
                      f"load {r['load_s_mean']:.1f}s", flush=True)
                results.append(r)
        if mode == "shm" and shm_blocks:
            for b in shm_blocks:
                b.close()
                b.unlink()
            shm_blocks = None
            cfg.pop("shm_names", None)

    json.dump({"env": env, "probes": probes, "results": results},
              open(args.out, "w"), indent=2, default=str)
    print(f"\nwrote {args.out}", flush=True)


if __name__ == "__main__":
    main()

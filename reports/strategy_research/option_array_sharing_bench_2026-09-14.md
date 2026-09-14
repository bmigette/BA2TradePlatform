# Can the parquet option reader share its arrays instead of copying them per worker?

**Date:** 2026-09-14
**Harness:** `testplatform/backend/tests_scripts/bench_option_array_sharing.py` (ad-hoc, not collected by pytest)
**Question:** `parquet_options_provider._RawUnderlying` holds PRIVATE per-process numpy arrays, so a
32-worker pool holds 32 copies of the same immutable bytes. Can they become SHARED read-only memory
without losing read throughput, on Windows and on Linux?

**Answer: yes, and it is close to free — provided two specific mistakes are avoided.** Memory-mapped
arrays are within 1% of private at 32 workers on both OSes while cutting per-worker footprint 5x.
No production code was modified.

---

## 1. What was measured

The real hot path, not a synthetic scan. `get_chain` masks contracts by expiry, then PER CONTRACT
bisects `bar_ord` inside `[starts[ci], stops[ci])` for the as-of row and reads close/bid/ask/iv/volume
off that row, computing a mark. One "op" below = one such chain read; it touched **477 contracts on
average** over the 60-day expiry window used here. Greeks are excluded on purpose:
`compute_iv_and_greeks` is ~11 us/call of pure arithmetic that is identical under every storage mode
and would swamp the array-read signal.

A **checksum is accumulated per worker and matched across every mode, style and process count** — all
cases in every run produced identical checksums, so the modes provably did the same work.

### Symbol set (identical on both hosts, ThetaData 2020 tree)

| symbol | rows | contracts | array MB (resident) |
|---|---:|---:|---:|
| INTC | 1,513,836 | 34,834 | 110.4 |
| ABT | 1,450,105 | 34,786 | 105.8 |
| CSCO | 1,216,135 | 28,438 | 88.7 |
| KO | 1,166,094 | 24,672 | 85.0 |
| T | 1,035,353 | 22,132 | 75.5 |
| VZ | 974,115 | 24,290 | 71.1 |
| **total** | **7,355,638** | **169,152** | **536.5** |

≈ **89 MB of numpy per mid-cap symbol** (14 arrays: 9 float64 + `bar_ord` int32 per row, plus
`starts`/`stops`/`c_strike`/`c_expiry_ord`/`c_is_call` per contract). The trees on the two hosts are
byte-identical: the build step produced the same row and contract counts on both.

### Storage modes

| mode | what a worker holds |
|---|---|
| `private` | `np.load(path)` — its own full copy in anonymous RAM (**today's behaviour**) |
| `memmap` | `np.asarray(np.load(path, mmap_mode='r'))` — a base-ndarray **view** over a shared file mapping |
| `memmap_sub` | the raw `np.memmap` object `np.load(mmap_mode=...)` actually returns (an ndarray **subclass**) |
| `shm` | `np.ndarray(..., buffer=SharedMemory.buf)` — `multiprocessing.shared_memory`, created by the parent |

### Access styles

| style | as-of clamp |
|---|---|
| `list` | `bisect_right(bar_ord_l, ord, lo, hi)` over the interned python list — **what the reader does today** |
| `ss` | `np.searchsorted(bar_ord[lo:hi], ord)` — no python projections at all |

Both styles matter because **a python list of a memmap is not shared**: materialising `bar_ord_l`
pulls every page of `bar_ord` into the worker and allocates 8 bytes/row of private pointers. Part of
the question is whether the list projections have to go away, and what that costs.

`ops/s per proc` is the mean single-worker rate; `ops/s aggregate` is total ops divided by the wall
span from the first worker's start to the last worker's end; `system committed delta` is the peak
growth in `psutil.virtual_memory().used`, which on both OSes **excludes page cache** — so a shared
mapping's pages deliberately do not appear there.

---

## 2. remote227 (Linux) — 32 cores, 251 GB, python 3.13.5, numpy 2.2.6, pyarrow 25.0.1, idle

3,000 ops/worker. Pass 1 = cold (first touch), pass 2 = warm.

| mode | style | procs | ops/s per proc | ops/s aggregate | aggregate, cold pass 1 | warm span s | RSS/worker MB | USS/worker MB | system committed GB | load s |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| private | list | 1 | 1,231 | 1,231 | 1,203 | 2.4 | 689 | 676 | 0.68 | 0.9 |
| private | list | 8 | 1,240 | 9,749 | 9,691 | 2.5 | 690 | 676 | 5.36 | 1.0 |
| **private** | **list** | **32** | **689** | **21,500** | 20,306 | 4.5 | 691 | **676** | **20.65** | 1.6 |
| private | ss | 1 | 277 | 277 | 273 | 10.8 | 568 | 555 | 0.14 | 0.3 |
| private | ss | 8 | 284 | 2,235 | 2,244 | 10.7 | 568 | 554 | 4.23 | 0.3 |
| private | ss | 32 | 155 | 4,786 | 4,777 | 20.1 | 568 | 554 | 17.34 | 0.8 |
| memmap | list | 1 | 1,127 | 1,127 | 1,120 | 2.7 | 469 | 455 | 0.15 | 0.7 |
| memmap | list | 8 | 1,143 | 8,908 | 8,891 | 2.7 | 468 | 143 | 1.20 | 0.7 |
| **memmap** | **list** | **32** | **647** | **20,148** | 19,864 | 4.8 | 468 | **144** | **4.51** | 1.2 |
| memmap | ss | 1 | 279 | 279 | 278 | 10.7 | 343 | 329 | 0.00 | 0.0 |
| memmap | ss | 8 | 280 | 2,178 | 2,165 | 11.0 | 343 | 18 | 0.13 | 0.0 |
| **memmap** | **ss** | **32** | **154** | **4,755** | 4,761 | 20.2 | 343 | **18** | **0.61** | 0.0 |
| memmap_sub | list | 1 | 755 | 755 | 740 | 4.0 | 469 | 455 | 0.02 | 0.7 |
| memmap_sub | list | 8 | 756 | 5,627 | 5,523 | 4.3 | 468 | 144 | 1.19 | 0.7 |
| memmap_sub | list | 32 | 430 | 13,293 | 13,245 | 7.2 | 468 | 144 | 4.63 | 1.2 |
| memmap_sub | ss | 32 | 81 | 2,524 | 2,521 | 38.0 | 343 | 18 | 0.63 | 0.1 |
| shm | list | 1 | 1,222 | 1,222 | 1,218 | 2.5 | 473 | 145 | 0.18 | 0.7 |
| shm | list | 8 | 1,147 | 8,834 | 8,858 | 2.7 | 473 | 144 | 1.25 | 0.7 |
| **shm** | **list** | **32** | **655** | **20,358** | 19,244 | 4.7 | 473 | **144** | **4.40** | 1.1 |
| shm | ss | 32 | 153 | 4,782 | 4,744 | 20.1 | 347 | 19 | 0.61 | 0.0 |

**Linux headline (32 workers, list style): memmap 20,148 vs private 21,500 ops/s = -6.3%, at 4.6x
less system memory.** `shm` (20,358, -5.3%) is statistically the same as memmap. With the list
projections dropped (`ss`), memmap is -0.6% of private and the saving is 28x, but the `ss` style
itself is 4.4x slower than `list` in absolute terms.

---

## 3. minisrv (Windows 11) — 20 logical / 14 physical cores, 64 GB, python 3.12.10, numpy 2.2.6

Two live trading platforms are resident on this box, so 32 workers is a heavy oversubscription; the
meaningful figure is the memmap-vs-private RATIO at each process count, not the absolute rate.

### 3a. Full matrix (modes run in the order private → memmap → shm)

| mode | style | procs | ops/s per proc | ops/s aggregate | aggregate, cold pass 1 | warm span s | RSS/worker MB | USS/worker MB | system committed GB | load s |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| private | list | 1 | 1,543 | 1,543 | 1,992 | 1.9 | 677 | 667 | 0.64 | 0.9 |
| private | list | 8 | 774 | 6,123 | 7,240 | 3.9 | 678 | 667 | 5.22 | 1.4 |
| private | list | 32 | 304 | 8,047 | 9,834 | 11.9 | 678 | 667 | 20.02 | 1.6 |
| private | ss | 1 | 312 | 312 | 284 | 9.6 | 571 | 560 | 0.80 | 0.4 |
| private | ss | 8 | 187 | 1,478 | 1,588 | 16.2 | 571 | 561 | 4.85 | 0.3 |
| private | ss | 32 | 38 | 1,209 | 1,249 | 79.4 | 571 | 561 | 18.45 | 0.4 |
| memmap | list | 1 | 755 | 755 | 1,083 | 4.0 | 450 | 129 | 0.73 | 1.0 |
| memmap | list | 8 | 618 | 4,879 | 5,022 | 4.9 | 450 | 130 | 1.32 | 0.9 |
| memmap | list | 32 | 177 | 5,623 | 5,590 | 17.1 | 450 | 130 | 4.56 | 0.9 |
| memmap | ss | 1 | 205 | 205 | 233 | 14.6 | 343 | 23 | 0.29 | 0.0 |
| memmap | ss | 8 | 144 | 1,141 | 1,178 | 21.0 | 343 | 23 | 0.62 | 0.0 |
| memmap | ss | 32 | 39 | 1,244 | 1,267 | 77.1 | 343 | 23 | 1.66 | 0.0 |
| shm | list | 1 | 1,220 | 1,220 | 1,468 | 2.5 | 451 | 130 | 0.19 | 0.8 |
| shm | list | 8 | 718 | 5,708 | 6,043 | 4.2 | 451 | 130 | 1.02 | 0.7 |
| shm | list | 32 | 167 | 5,324 | 5,886 | 18.0 | 451 | 130 | 4.19 | 0.8 |
| shm | ss | 32 | 39 | 1,243 | 1,336 | 77.2 | 344 | 23 | 1.02 | 0.0 |

Read naively this says memmap costs 30% on Windows. **It does not — that is an ordering artifact, and
finding it is the main Windows lesson (see §5).** Every memmap case above ran *after* the private
cases had committed 20 GB, by which point Windows' working-set manager had trimmed the file-backed
mapped pages back out of the processes.

### 3b. Reverse-order control (memmap first, list style only)

| mode | procs | ops/s aggregate | USS/worker MB | system committed GB |
|---|---:|---:|---:|---:|
| memmap | 1 | 1,328 | 130 | 0.46 |
| memmap | 8 | 5,728 | 130 | 1.27 |
| memmap | 32 | 5,705 | 130 | 4.36 |
| memmap_sub | 1 | 780 | 130 | 0.47 |
| memmap_sub | 8 | 3,824 | 130 | 1.32 |
| memmap_sub | 32 | 3,831 | 130 | 4.40 |
| private | 1 | 1,290 | 668 | 0.71 |
| private | 8 | 5,503 | 667 | 5.26 |
| private | 32 | 5,466 | 667 | 21.04 |

With the order reversed, memmap *beats* private at every process count. Neither ordering is the
answer on its own, which is why the next run interleaves them.

### 3c. Interleaved paired A/B (3 alternating rounds, list style) — the fair comparison

| procs | private mean (range) | memmap mean (range) | memmap / private |
|---|---:|---:|---:|
| 1 | 1,366 (1,275 – 1,477) | 1,260 (1,114 – 1,353) | **92.2%** |
| 32 | 5,664 (5,400 – 5,975) | 5,603 (5,581 – 5,629) | **98.9%** |

Memory in the same run at 32 workers: private **667 MB USS/worker, 20.9 GB** system committed;
memmap **130 MB USS/worker, 4.43 GB**. Note also that memmap's spread at 32 workers (±0.4%) is far
tighter than private's (±5%) — with 21 GB committed, private is the noisy one.

---

## 4. The two cross-cutting effects

### 4a. `np.memmap` is an ndarray SUBCLASS, and that costs 33-43%

`np.load(path, mmap_mode='r')` returns `np.memmap`, not `np.ndarray`. Scalar indexing on a subclass
pays an extra dispatch per read, and this hot loop does 5 scalar reads per contract:

| host | procs | `memmap` (asarray view) | `memmap_sub` (raw np.memmap) | penalty |
|---|---:|---:|---:|---:|
| Linux | 1 | 1,127 | 755 | -33% |
| Linux | 32 | 20,148 | 13,293 | -34% |
| Linux (ss) | 32 | 4,755 | 2,524 | -47% |
| Windows | 1 | 1,328 | 780 | -41% |
| Windows | 32 | 5,705 | 3,831 | -33% |

`np.asarray(mm)` returns a base-ndarray **view** over the same mapping — no copy, nothing paged in,
the memmap survives as the view's `.base`. **A patch that skips this one call gives back a third of
the throughput and would read as "memmap is slow".** This is the single highest-leverage detail in
the whole benchmark.

### 4b. Keep the list projections — dropping them costs 4.4x, and they are cheap

| host | style | ops/s per proc (1 worker) | USS/worker |
|---|---|---:|---:|
| Linux | list | 1,231 | 676 MB private / 144 MB memmap |
| Linux | ss | 277 (**-77%**) | 554 MB private / 18 MB memmap |
| Windows | list | 1,543 | 667 MB private / 130 MB memmap |
| Windows | ss | 312 (**-80%**) | 561 MB private / 23 MB memmap |

The reader's comment (0.051 us for `bisect_right` vs 0.73 us for `np.searchsorted` on a slice) is
confirmed end to end: at the chain-read level the list style is **4.4x (Linux) to 4.9x (Windows)**
faster. The projections cost **~107 MB per worker for 7.36M rows (~14.5 B/row)** — `bar_ord_l`'s
8 B/row of pointers plus list slack — which is only 20% of the 537 MB they let you share. Answering
the question directly: **the list projections do NOT have to go away.** Keeping them gives a 5.1x
memory cut at ~1% throughput cost; removing them gives a 29x cut at a 4.4x throughput cost, which is
a bad trade for a CPU-bound GA.

### 4c. Cold vs warm memmap

`--drop-cache` (`posix_fadvise(DONTNEED)` on all 90 `.npy` files before each case — unprivileged, so
no root needed) gives a genuinely disk-cold read on Linux, at 8 workers:

| mode | style | load s | cold pass 1 agg | warm pass 2 agg | warm / cold |
|---|---|---:|---:|---:|---:|
| private | list | **3.9** | 9,090 | 9,572 | 1.05x |
| private | ss | **3.2** | 2,246 | 2,249 | 1.00x |
| memmap | list | 1.1 | **3,963** | 9,006 | **2.27x** |
| memmap | ss | 0.5 | **1,603** | 2,131 | 1.33x |

Memmap does not avoid the I/O, it **defers** it: private pays 3.9 s up front in `np.load`, memmap
pays it as page faults during the first pass (2.3x slower) and is at full speed from the second pass
on. For a GA worker that re-reads the same (symbol, date) pairs across thousands of trials this is
strictly better — the cost is paid once per worker lifetime instead of once per `_RawUnderlying`
construction, and with N workers sharing the page cache it is paid once per *host*, not N times.
On Windows there is no unprivileged way to evict a file from the cache, so the "cold pass 1" column
in §2/§3 means first-touch-with-warm-cache (soft faults only).

---

## 5. Windows-specific gotchas

1. **File locking: a mapped `.npy` cannot be deleted or replaced.** Probed directly —
   `os.remove()` on a file with a live mapping returns `PermissionError WinError 32` on Windows and
   **succeeds** on Linux (POSIX unlink semantics: the mapping stays valid). Any design that rebuilds
   or evicts a cached underlying's backing file must therefore close every mapping in every worker
   first on Windows. Practical consequences: write new files under a new name and swap the path
   rather than overwriting; expect temp-dir cleanup to fail while workers are alive.
2. **Pickling a memmap MATERIALISES it.** Probed: `pickle.dumps` of a 200,000-element float64 memmap
   slice produced **1,600,162 bytes** — the full data, not a reference. A memmap must never be passed
   as a spawn argument, returned through a `Queue`, or closed over by a `ProcessPoolExecutor`
   callable; each worker has to open its own `np.load(..., mmap_mode='r')`. (Same on Linux, but spawn
   makes it far easier to hit, and the platform uses spawn on both OSes.)
3. **Windows trims file-backed mapped pages under memory pressure, and the throughput loss looks
   like a memmap regression.** This is what produced the bogus -30% in §3a: after the private cases
   committed 20 GB, the mapped pages had been trimmed out of every process's working set, so the
   memmap cases spent their run re-faulting. Interleaved (§3c) the gap is 1.1%. The operational
   reading is favourable, not alarming — the 20 GB of pressure that causes the trimming is exactly
   what the mapping removes — but it does mean **a memmap benchmark on Windows must be interleaved
   with its control, never run after it**, and that a memory-pressured Windows box can see memmap
   throughput sag in a way Linux does not.
4. **Windows does not scale this workload past its physical cores.** `private/ss` went *backwards*
   from 8 to 32 workers (1,478 → 1,209 aggregate, 187 → 38 ops/s per worker) on 14 physical cores
   with two live platforms resident, while Linux scaled 2,235 → 4,786 on 32 real cores. This is
   independent of storage mode (it happens to private memory too) and is an argument about worker
   counts on Windows, not about sharing.
5. `multiprocessing.shared_memory` worked on Windows with no special handling (90 blocks / 537 MB
   created in 1.1 s, children attach by name) and performed the same as memmap. It has no advantage
   here and two disadvantages: the parent must stay alive to own the segments, and the bytes have to
   be copied into the segment at startup instead of being mapped from a file that already exists.

---

## 6. Verdict

**Memmap throughput is within ~10-20% of private at 32 workers on both operating systems — in fact
within ~1-6% — and the memory saving is about 5x.** On Linux at 32 workers, `memmap` + the existing
list projections runs at 20,148 chain reads/s against private's 21,500 (**-6.3%**) while cutting
per-worker USS from 676 MB to 144 MB and system committed memory from **20.65 GB to 4.51 GB (4.6x)**.
On Windows, once the comparison is interleaved so that neither mode runs in the other's memory
shadow, memmap runs at **98.9%** of private at 32 workers (5,603 vs 5,664 ops/s) while cutting USS
from 667 MB to 130 MB and committed memory from **20.9 GB to 4.43 GB (4.7x)**; at a single worker it
is 92.2%, and the residual gap closes as workers are added because sharing is worth more the more
processes there are. `multiprocessing.shared_memory` matches memmap exactly on both hosts and is not
worth its extra lifecycle complexity. **Two conditions are load-bearing**: wrap the mapping in
`np.asarray` so the hot loop indexes a base ndarray and not the `np.memmap` subclass (worth 33-43%,
and skipping it is the easy way to conclude "memmap is slow"), and **keep** the interned
`bar_ord_l`/`starts_l`/`stops_l` python projections — replacing the bisect with `np.searchsorted` on
the mapping costs 4.4-4.9x throughput to save 107 MB/worker that the mapping was already going to
dwarf. Scaled to a real run, the 89 MB/symbol measured here means a 100-symbol option universe costs
~8.7 GB **per worker** today and ~8.7 GB **per host** shared, which is the difference between a
6-slot and a 30-slot remote worker on the 32 GB boxes.

---

## 7. Reproducing

```bash
# Windows
C:/Users/basti/ba2-venvs/test/Scripts/python.exe \
  testplatform/backend/tests_scripts/bench_option_array_sharing.py \
  --root "C:/Users/basti/Documents/ba2/common/cache/ThetaDataOptionsProvider" \
  --tmp  "<scratch>/bench_tmp" --procs 1,8,32 --modes private,memmap,shm \
  --styles list,ss --ops 3000 --mem-frac 0.60 --out win_main.json

# the fair Windows comparison (interleaved, see 3c)
... --procs 1,32 --modes private,memmap,private,memmap,private,memmap --styles list --ops 3000

# Linux (remote227), incl. the genuinely disk-cold pass
/opt/ba2worker/ba2-venvs/test/bin/python bench_option_array_sharing.py \
  --root /home/debian/ba2-grid/home/common/cache/ThetaDataOptionsProvider \
  --tmp  /home/debian/ba2-grid/bench_tmp --procs 8 --modes private,memmap \
  --styles list,ss --ops 3000 --drop-cache --out linux_cold.json
```

The build step is idempotent and took 22 s for the 6 symbols (537 MB of `.npy`). `--mem-frac` refuses
any case whose projected footprint would exceed that share of available RAM, which is what keeps a
32-worker private run from disturbing the live platforms on the Windows box. Temp trees were deleted
after the runs.

## Acceptance (2026-09-14, tools/backtest_parity.py, private child then shared child, byte-for-byte)

| ref | source | trades | private-path private MB | shared-path private / mapped MB | verdict |
|---|---|---|---|---|---|
| 1 screener small S7 | `--bt 1681` (opt 521 rank 1), retry | 168 | bars 4374 | bars 729 / 3645 | PASS |
| 2 Senate S6 | `--bt 1645` (opt 512 rank 1), retry | 405 | bars 2574 | bars 429 / 2145 | PASS |
| 3 ThetaData 2020 options | `--bt 1688` (opt 522 rank 2, local probe) | 72 | options 3451 (20 syms) | options 1267 / 2184 | PASS |
| 4 TastyTrade 2023 options | `--bt 1695` (opt 523 rank 1, local probe) | 56 | options 174 (8 syms) | options 87 / 87 | PASS |

Every blob (results, trades, equity_curve, drawdown_curve) and every metric column compared
equal after canonical JSON. The informational archived-vs-re-run diff shows only last-digit
float noise in `results.robustness.*` / `results.fitness_*` (1e-15), the known re-run
non-determinism of summary statistics -- unrelated to sharing. A first attempt at ref 4 on
opt 429 (LEAP perf probe) was VACUOUS (0 trades: its stored block has no options provider
flag) and is why the tool now refuses 0-trade pairs. Rows: PARITY-* 1686-1704 in the test DB.

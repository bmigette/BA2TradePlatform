"""A3 DECISION GATE: is a columnar ScoringStore hit fast enough to replace a dict deref?

Today ``cache.get(key)`` returns a REFERENCE to a stored dict -- zero allocation. ScoringStore
must REBUILD a dict per hit (4 or 12 numbers). ``_get_trader_skill_cached`` /
``_get_trader_confidence_cached`` sit on the hot per-trade path, so a large regression here
would trade a memory win for a CPU loss on a CPU-bound GA -- a bad deal.

Deliberately sized at 250k entries, not the real 3.4M: the box is running the Senate grid with
~1GB free, and a full-size plain-dict baseline (3.4M x 651B = 2.2GB) would itself risk the
thing this whole change exists to prevent. Per-hit cost is what is being measured; the entry
count only needs to be large enough that lookups are not served entirely from L2.

Run:  .venv\\Scripts\\python.exe test_files\\bench_scoring_store.py
"""
import os
import random
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "common"))

from ba2_common.core.scoring_store import ScoringStore  # noqa: E402

N = 250_000
HITS = 1_000_000
FIELDS = ("skill_score", "scored_trades", "hit_rate", "avg_fwd_return_pct")


def make_src(n):
    return {
        f"Trader Name {i}|{i}|2024-{(i % 12) + 1:02d}-03|60|5|50|12": {
            "skill_score": i * 0.001,
            "scored_trades": i,
            "hit_rate": None if i % 7 == 0 else (i % 100) / 100.0,
            "avg_fwd_return_pct": -i * 0.002,
        }
        for i in range(n)
    }


def bench(label, get, keys):
    rnd = random.Random(42)
    probe = [keys[rnd.randrange(len(keys))] for _ in range(HITS)]
    t0 = time.perf_counter()
    acc = 0.0
    for k in probe:
        acc += get(k)["scored_trades"]
    dt = time.perf_counter() - t0
    print(f"  {label:28s} {dt:7.3f}s   {dt / HITS * 1e6:6.3f} us/hit   (checksum {acc:.0f})")
    return dt


def main():
    print(f"building {N:,} entries ...")
    src = make_src(N)
    keys = list(src.keys())

    tmp = tempfile.mkdtemp(prefix="scorebench_")
    try:
        path = os.path.join(tmp, "s.store")
        ScoringStore.build(path, src, FIELDS, nullable=("hit_rate",))
        store = ScoringStore.open(path)

        # correctness before speed -- a fast wrong answer is worthless
        for k in (keys[0], keys[len(keys) // 2], keys[-1]):
            assert store.get(k) == src[k], (k, store.get(k), src[k])
        print("  parity check OK\n")

        print(f"{HITS:,} random hits:")
        a = bench("plain dict (today)", src.get, keys)
        b = bench("ScoringStore (dict return)", store.get, keys)

        ratio = b / a
        print(f"\n  ScoringStore is {ratio:.2f}x the cost of a dict deref")
        print(f"  absolute overhead: {(b - a) / HITS * 1e6:.3f} us/hit")
        if ratio <= 1.10:
            print("  VERDICT: within 10% -- keep the dict return, no call-site churn (A3 pass)")
        else:
            print("  VERDICT: regression -- evaluate namedtuple return + update the 3 call sites")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()

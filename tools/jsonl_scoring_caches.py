"""Convert the congress scoring shards from one-object JSON to streamable JSON Lines.

WHY THIS IS REQUIRED, NOT OPTIONAL. The loader prefers a ``.jsonl`` shard and streams it one
entry at a time; it falls back to the legacy ``.json``, which ``json.load`` must materialise in
full before the column table can be built. That fallback sets PEAK memory to the very
representation the table replaces -- measured 480MB -> 208MB live but only 641MB -> 544MB peak,
and RSS tracks peak because the allocator keeps arenas sized to it.

A shard would normally convert itself on its next write. These never get one: BACKTESTS DO NOT
WRITE (``_save_scoring_cache_throttled`` returns immediately when ``not is_live``), so a
backtest/GA-only workflow would sit on the legacy path indefinitely. Hence a one-off pass.

    python tools/jsonl_scoring_caches.py --dry-run
    python tools/jsonl_scoring_caches.py

Conversion is atomic per shard (tmp + replace) and only removes the ``.json`` after the
``.jsonl`` is in place, so an interrupted run leaves every shard readable by one path or the
other -- never neither.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                "packages", "experts"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                "packages", "common"))

PREFIXES = ("congress_skill_scores", "congress_confidence_scores", "congress_scalper_scores")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-json", action="store_true",
                    help="leave the legacy file in place (it becomes dead weight: the loader "
                         "prefers .jsonl, so the .json is never read again)")
    a = ap.parse_args()

    from ba2_common.config import CACHE_FOLDER
    from ba2_experts.scoring_table import ScoringTable

    d = os.path.join(CACHE_FOLDER, "fmp_history")
    shards = sorted(f for f in os.listdir(d)
                    if f.startswith(PREFIXES) and f.endswith(".json"))
    if not shards:
        print(f"no legacy .json shards under {d} — nothing to do")
        return 0

    total_before = total_after = 0
    for name in shards:
        src = os.path.join(d, name)
        dst = src[:-len(".json")] + ".jsonl"
        before = os.path.getsize(src)
        total_before += before
        if os.path.exists(dst):
            print(f"  SKIP  {name}  (.jsonl already present)")
            continue
        if a.dry_run:
            print(f"  would convert {name}  ({before / 1e6:.0f} MB)")
            continue

        t0 = time.perf_counter()
        with open(src, "r", encoding="utf-8") as f:
            table = ScoringTable.from_dict(json.load(f))
        tmp = f"{dst}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            table.dump_jsonl(f)
        os.replace(tmp, dst)               # atomic; .jsonl is now readable
        if not a.keep_json:
            os.remove(src)                 # only after the replacement is durable
        after = os.path.getsize(dst)
        total_after += after
        print(f"  OK    {name}  {before / 1e6:6.0f} MB -> {after / 1e6:6.0f} MB  "
              f"{len(table):>8,} entries  overflow={table.overflow_count}  "
              f"{time.perf_counter() - t0:.0f}s")
        del table

    if a.dry_run:
        print(f"\n{len(shards)} shard(s), {total_before / 1e6:.0f} MB — re-run without --dry-run")
    else:
        print(f"\nconverted {total_before / 1e6:.0f} MB -> {total_after / 1e6:.0f} MB on disk "
              f"(the win is in PEAK RSS at load, not file size)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

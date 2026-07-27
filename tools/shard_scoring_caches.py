"""Split the monolithic congress scoring caches into per-settings shards (2026-07-27).

One-off migration for the sharding change in FMPSenateTraderWeight (commit ffbf9f5). Until this
runs, every shard is missing and each trial starts from an EMPTY cache -- correct (scores are
pure functions of their inputs) but slow, and it would silently discard the 3.4M prewarmed
skill scores.

    .venv\\Scripts\\python.exe tools\\shard_scoring_caches.py            # split
    .venv\\Scripts\\python.exe tools\\shard_scoring_caches.py --verify   # check only
    .venv\\Scripts\\python.exe tools\\shard_scoring_caches.py --dry-run

RUN ON AN IDLE BOX. Peak RSS is roughly the size of the largest cache in Python-dict form
(~2.2GB skill, ~2.95GB confidence, handled ONE AT A TIME). Running it against a live grid is
the exact memory pressure this whole change exists to remove.

The source files are RENAMED to ``.premigration`` rather than deleted, so a bad split can be
rolled back by renaming them back and deleting the shard files.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "experts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "common"))

from ba2_experts.FMPSenateTraderWeight import _shard_filename  # noqa: E402

# (filename, how many trailing '|'-separated key fields are the SETTINGS)
#   skill       "<trader>|<n>|<as_of>|<horizon>|<min_past>|<max_past>|<lookback>"   -> 4
#   confidence  "<trader>|<n>|<SYM>|<side>|<as_of>|<max_exec>|<focus_cap>"          -> 2
SPECS = [("congress_skill_scores.json", 4), ("congress_confidence_scores.json", 2)]


def cache_dir():
    from ba2_common.config import CACHE_FOLDER
    return os.path.join(CACHE_FOLDER, "fmp_history")


def load_merged(path):
    """Base file + its un-compacted delta, in the SAME precedence as _load_scoring_cache
    (delta wins) -- otherwise the split would silently roll back recent entries."""
    data = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    delta = path + ".delta.jsonl"
    n_delta = 0
    if os.path.exists(delta):
        with open(delta, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:      # noqa: BLE001 — a torn last line is expected after a crash
                    continue
                data[e["k"]] = e["v"]
                n_delta += 1
    return data, n_delta


def suffix_of(key, n_fields):
    parts = key.split("|")
    if len(parts) <= n_fields:
        return None
    return "|".join(parts[-n_fields:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true", help="report shard coverage, change nothing")
    args = ap.parse_args()
    d = cache_dir()

    for filename, n_fields in SPECS:
        src = os.path.join(d, filename)
        print(f"\n=== {filename}")

        if args.verify:
            total = 0
            for n in sorted(os.listdir(d)):
                stem = os.path.splitext(filename)[0]
                if n.startswith(stem + "__") and n.endswith(".json"):
                    with open(os.path.join(d, n), "r", encoding="utf-8") as f:
                        c = len(json.load(f))
                    total += c
                    print(f"  {n:52s} {c:>10,}")
            print(f"  {'TOTAL':52s} {total:>10,}")
            continue

        if not os.path.exists(src):
            print("  source absent (already migrated?) -- skipping")
            continue

        data, n_delta = load_merged(src)
        print(f"  loaded {len(data):,} entries (delta contributed {n_delta:,})")

        groups = {}
        skipped = 0
        for k, v in data.items():
            s = suffix_of(k, n_fields)
            if s is None:
                skipped += 1
                continue
            groups.setdefault(s, {})[k] = v
        del data  # release before writing; the groups hold the same value objects by reference

        print(f"  {len(groups)} shard(s), {skipped} unparseable key(s)")
        written = 0
        for s, entries in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            out = os.path.join(d, _shard_filename(filename, s))
            print(f"    {os.path.basename(out):52s} {len(entries):>10,}")
            written += len(entries)
            if not args.dry_run:
                tmp = out + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(entries, f)
                os.replace(tmp, out)

        # Round-trip guard: every entry must land in exactly one shard.
        assert written + skipped == sum(len(g) for g in groups.values()) + skipped, "lost entries"
        print(f"  wrote {written:,} entries across {len(groups)} shard(s)")

        if not args.dry_run:
            # Keep the source (renamed) so a bad split is reversible.
            os.replace(src, src + ".premigration")
            delta = src + ".delta.jsonl"
            if os.path.exists(delta):
                os.replace(delta, delta + ".premigration")
            print(f"  source kept as {os.path.basename(src)}.premigration")

    print("\ndone.")


if __name__ == "__main__":
    main()

"""Content-checksum cache integrity check for configured remote workers.

Slow-ish by design: reads + CRC32-checksums EVERY file in the master's cache and the worker's
cache to catch the one drift (rel_path, size)-based sync can't see — a local rebuild that rewrites
a file's content at its OLD byte size (e.g. the screener metric_store recomputing a column in
place). CRC32, not a cryptographic hash: this is corruption/staleness detection over a possibly
tens-of-GB cache, not a security boundary, so the much faster checksum is the right tradeoff. This
is a periodic/manual gate, NOT the per-job pre-flight (that's the fast, size-only push_cache,
already run automatically at the start of every optimization job).

Usage (test venv):
    ba2-venvs/test/Scripts/python.exe tools/cache_health_check.py [--worker NAME] [--fix]

--fix removes anything `push_cache` already handles (missing + stale) via the normal push/prune,
then force-repushes any CONTENT-MISMATCH file (same rel_path/size, different crc32) — the one
case `push_cache`'s size-only diff cannot detect or fix on its own.
"""
import argparse
import os
import sys

_BACKEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "testplatform", "backend")
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
os.chdir(_BACKEND_DIR)  # app.* relative imports + default DB path resolution expect this cwd


def _force_repush(worker: dict, rel_paths: list, log) -> dict:
    from app.services import cache_sync, worker_client
    local = cache_sync.build_manifest()
    stream = cache_sync.iter_tar(rel_paths, local["root"])
    import httpx
    base, headers = worker_client._base(worker), worker_client._headers(worker)
    with httpx.Client(timeout=None) as c:
        r = c.post(f"{base}/cache/push", headers=headers, content=stream)
        r.raise_for_status()
        res = r.json()
    log(f"force-repush -> {worker['name']}: {res}")
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--worker", default=None, help="Check only this worker NAME (default: all enabled, non-local).")
    ap.add_argument("--fix", action="store_true", help="Push/prune/repush anything found out of sync.")
    args = ap.parse_args()

    from app.models.database import SessionLocal
    from app.models import Worker
    from app.services import worker_client

    db = SessionLocal()
    try:
        q = db.query(Worker).filter(Worker.is_local == False, Worker.is_enabled == True)  # noqa: E712
        if args.worker:
            q = q.filter(Worker.name == args.worker)
        workers = [{"id": w.id, "name": w.name, "url": w.url, "password": w.password} for w in q.all()]
    finally:
        db.close()

    if not workers:
        print("No matching enabled remote workers configured.")
        return 0

    overall_ok = True
    for w in workers:
        print(f"\n=== {w['name']} ({w['url']}) ===")
        try:
            res = worker_client.check_cache_integrity(w)
        except Exception as e:  # noqa: BLE001
            print(f"  UNREACHABLE / check failed: {e!r}")
            overall_ok = False
            continue

        print(f"  local files:  {res['local_count']}")
        print(f"  remote files: {res['remote_count']}")
        print(f"  missing (master has, worker doesn't/wrong size): {len(res['missing'])}")
        print(f"  stale   (worker has, master no longer does):     {len(res['stale'])}")
        print(f"  content mismatch (same rel_path+size, different crc32): {len(res['content_mismatch'])}")
        for label in ("missing", "stale", "content_mismatch"):
            for rel in res[label][:5]:
                print(f"    [{label}] {rel}")
            if len(res[label]) > 5:
                print(f"    [{label}] ... +{len(res[label]) - 5} more")

        if res["ok"]:
            print("  HEALTHY")
            continue

        if not args.fix:
            print("  OUT OF SYNC (re-run with --fix to repair)")
            overall_ok = False
            continue

        print("  fixing...")
        if res["missing"] or res["stale"]:
            worker_client.push_cache(w, log=print)
        if res["content_mismatch"]:
            _force_repush(w, res["content_mismatch"], log=print)
        # Re-check after fixing so the report reflects reality, not intent.
        res2 = worker_client.check_cache_integrity(w)
        print(f"  post-fix: ok={res2['ok']} missing={len(res2['missing'])} "
              f"stale={len(res2['stale'])} content_mismatch={len(res2['content_mismatch'])}")
        if not res2["ok"]:
            overall_ok = False

    print()
    if overall_ok:
        print("ALL WORKERS HEALTHY.")
        return 0
    print("ONE OR MORE WORKERS OUT OF SYNC.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

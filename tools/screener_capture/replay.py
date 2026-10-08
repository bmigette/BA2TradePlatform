"""Offline replay of a capture through the REAL live StockScreener.screen(): the vendor seam is fed from the recorded bodies.
    python replay.py <label> [instances...]      -> prints whether the ordered picks equal the capture's own"""
import glob
import json
import os
import sys
import types
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))


class FakeResp:
    def __init__(self, status, body, headers=None):
        self.status_code, self.text, self.headers = status, body, headers or {}
        self.content = body.encode() if isinstance(body, str) else body

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def key_of(url, params):
    p = {k: v for k, v in (params or {}).items() if k.lower() not in ("apikey", "api_key")}
    return (url, json.dumps(p, sort_keys=True, default=str))


def load_store(label_dir):
    recs = []
    for f in glob.glob(os.path.join(label_dir, "inst*", "0*.json")):
        with open(f, encoding="utf-8") as fh:
            r = json.load(fh)
        r["_file"] = f
        recs.append(r)
    recs.sort(key=lambda r: r["t_utc"])
    store = defaultdict(list)
    for r in recs:
        if r["status"] == 200 and r["body"] is not None:
            p = {k: v for k, v in r["params"].items() if v != "<key>"}
            store[key_of(r["url"], p)].append(r["body"])
    return store, recs


def install(store, now_utc):
    import ba2_providers.fmp_common as fc
    import ba2_providers.StockScreener as SS
    import importlib
    FP = importlib.import_module("ba2_providers.screener.FMPScreenerProvider")
    FP = sys.modules["ba2_providers.screener.FMPScreenerProvider"]
    from ba2_providers.screener import float_filter as ff
    used = defaultdict(int)
    misses = []

    def fake(url, params=None, **kw):
        k = key_of(url, params)
        bodies = store.get(k)
        if not bodies:
            misses.append(k)
            raise fc.FMPError(f"replay: no recorded response for {url} {k[1][:120]}")
        i = min(used[k], len(bodies) - 1)             # sticky last: a live-cache hit in the capture may be a refetch here
        used[k] += 1
        return FakeResp(200, bodies[i])

    fc.fmp_http_get = fake
    for mod in (SS, FP, ff):
        if hasattr(mod, "fmp_http_get"):
            mod.fmp_http_get = fake
        mod.get_app_setting = lambda *_a, **_k: "replay-key"

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return now_utc if tz is not None else now_utc.replace(tzinfo=None)
    SS.datetime = _DT
    return misses


def replay(label, iids=None, quiet=False):
    root = os.path.join(HERE, label)
    store, recs = load_store(root)
    results = {}
    insts = sorted(int(os.path.basename(d)[4:]) for d in glob.glob(os.path.join(root, "inst*")))
    first = json.load(open(os.path.join(root, f"inst{insts[0]}", "result.json")))
    now = datetime.fromisoformat(first["started_utc"])
    misses = install(store, now)
    import ba2_providers.StockScreener as SS
    ok_all = True
    for iid in (iids or insts):
        cap = json.load(open(os.path.join(root, f"inst{iid}", "result.json")))
        if cap["error"]:
            print(f"inst {iid}: capture error {cap['error']}")
            continue
        import ba2_providers.fmp_common as fc
        sc = SS.StockScreener(cap["settings"])
        res = sc.screen()
        picks = [r["symbol"] for r in res["results"]]
        same = picks == cap["picks"]
        ok_all &= same
        results[iid] = {"capture": cap["picks"], "replay": picks, "identical": same, "stats_equal": res["stats"] == cap["stats"]}
        if not quiet:
            print(f"inst {iid}: capture {len(cap['picks'])} picks, replay {len(picks)}, IDENTICAL ordered list: {same}, stats equal: {res['stats'] == cap['stats']}")
            if not same:
                print("   capture:", cap["picks"]); print("   replay :", picks)
    print("recorded-response misses:", len(misses), misses[:2])
    return results, ok_all


if __name__ == "__main__":
    replay(sys.argv[1], [int(x) for x in sys.argv[2:]] or None)

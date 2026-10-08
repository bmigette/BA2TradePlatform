"""RECORDED live screen: runs the real (post-fix) ``StockScreener.screen()`` for prod instances' settings and records every
vendor HTTP exchange and every stage.  Standalone; repo .venv python with PYTHONPATH on the worktree (see timed.py).

    python capture.py <label> [instance ids ...]

Writes screencap/<label>/inst<N>/NNNN.json (one per HTTP attempt: scrubbed URL, params, UTC wall time, status, full raw body),
stages.json (every stage's input and output candidate lists), result.json (ordered picks, stats, settings, timing).
The prod DB is opened READ-ONLY (settings + key; the key is never printed or stored).  <= 4 attempts/s overall; aborts cleanly after
6 minutes per label (keeping what it has).
"""
import copy
import json
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone


class _ScrubStream:
    """stdout / stderr with every ``apikey=...`` value replaced by ``***``.  Installed BEFORE any ba2 module is imported, so the logger's
    handlers (which bind the stream at import) write through it: a DEBUG line that embeds a request URL never leaks the key."""
    _RE = re.compile(r"""(?i)((?:api_?key)(?:=|['"]?\s*:\s*['"]?))[^&\s'",)}]+""")

    def __init__(self, inner):
        self._inner = inner

    def write(self, s):
        return self._inner.write(self._RE.sub(r"\1***", s))

    def __getattr__(self, name):
        return getattr(self._inner, name)


sys.stdout, sys.stderr = _ScrubStream(sys.stdout), _ScrubStream(sys.stderr)

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ["BA2_PROD_DB"]            # path of the prod sqlite file (opened read-only); no default
MAX_PER_SECOND = 4.0
LABEL_BUDGET_S = float(os.environ.get("CAPTURE_BUDGET_S", "600"))   # 0940 hit the 360 s default after instance 7: 600 s from then on


class CaptureAbort(BaseException):
    """Raised (not an Exception: the screener's broad handlers must not swallow it) when the label's budget is spent."""


def read_prod():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        key = con.execute("select value_str from appsetting where key='FMP_API_KEY'").fetchone()[0]
        return con, key
    except Exception:
        con.close()
        raise


def instance_settings_raw(con, iid):
    """The instance's screener_* settings as JobManager's ``expert.settings`` carries them (values only)."""
    out = {}
    for k, vs, vj, vf in con.execute("select key, value_str, value_json, value_float from expertsetting "
                                     "where instance_id=? and key like 'screener_%'", (iid,)):
        if vf is not None:
            out[k] = vf
        elif vj not in (None, "{}", ""):
            v = json.loads(vj)
            out[k] = (1 if str(v).lower() == "true" else 0) if isinstance(v, (bool, str)) else v
        elif vs is not None:
            out[k] = vs
    return out


def scrub(params):
    return {k: ("<key>" if k.lower() in ("apikey", "api_key") else v) for k, v in (params or {}).items()}


def install(label_dir, key, deadline):
    import requests
    import ba2_providers.fmp_common as fc
    import ba2_providers.StockScreener as SS
    import importlib
    FP = importlib.import_module("ba2_providers.screener.FMPScreenerProvider")
    sys.modules["ba2_providers.screener.FMPScreenerProvider"] = sys.modules.get("ba2_providers.screener.FMPScreenerProvider", FP)
    FP = sys.modules["ba2_providers.screener.FMPScreenerProvider"]
    from ba2_providers.screener import float_filter as ff

    getkey = lambda *_a, **_k: key            # never print / store; the prod DB key, read once
    for mod in (SS, FP, ff):
        mod.get_app_setting = getkey
    state = {"inst_dir": None, "n": 0, "lock": threading.Lock(), "next": 0.0, "pace": threading.Lock()}

    def paced():
        with state["pace"]:
            now = time.time()
            wait = state["next"] - now
            state["next"] = max(now, state["next"]) + 1.0 / MAX_PER_SECOND
        if wait > 0:
            time.sleep(wait)

    def rec_getter(inner):
        def _g(url, *a, **kw):
            if time.time() > deadline:
                raise CaptureAbort("label budget spent")
            paced()
            t0 = datetime.now(timezone.utc)
            resp = inner(url, *a, **kw)
            with state["lock"]:
                state["n"] += 1
                n = state["n"]
            try:
                body = resp.text
            except Exception:  # noqa: BLE001
                body = None
            rec = {"url": url, "params": scrub(kw.get("params")), "t_utc": t0.isoformat(), "status": getattr(resp, "status_code", None),
                   "headers": {k: v for k, v in dict(getattr(resp, "headers", {}) or {}).items() if k.lower() in ("retry-after", "date", "content-type")},
                   "body": body}
            with open(os.path.join(state["inst_dir"], f"{n:04d}.json"), "w", encoding="utf-8") as f:
                json.dump(rec, f)
            return resp
        return _g

    orig = fc.fmp_http_get

    def wrapped(url, params=None, **kw):
        inner = kw.pop("getter", None) or requests.get
        return orig(url, params=params, getter=rec_getter(inner), **kw)

    fc.fmp_http_get = wrapped
    for mod in (SS, FP, ff):                  # modules that bound the name at import
        if hasattr(mod, "fmp_http_get"):
            mod.fmp_http_get = wrapped

    # ---- stage recorders (wrap, never alter) ----
    stages = []

    def snap(cands):
        out = []
        for c in cands or []:
            out.append({k: c.get(k) for k in ("symbol", "price", "market_cap", "volume", "avg_volume", "relative_volume",
                                                "float_shares", "price_drop_pct", "weinstein_stage", "weinstein_slope_pct")})
        return out

    def wrap_method(cls, name, in_arg=0, out_idx=None, keep_args=()):
        f = getattr(cls, name)

        def w(self, *a, **kw):
            t0 = datetime.now(timezone.utc).isoformat()
            before = snap(a[in_arg]) if a and isinstance(a[in_arg], list) else None
            res = f(self, *a, **kw)
            r = res[out_idx] if out_idx is not None else res
            stages.append({"stage": name, "t_utc": t0, "in": before, "out": snap(r) if isinstance(r, list) else r,
                           "args": [x for x in a[1:] if isinstance(x, (int, float, str))]})
            return res
        setattr(cls, name, w)

    wrap_method(SS.StockScreener, "_enrich_with_rvol", 0, 0)
    wrap_method(SS.StockScreener, "_filter_by_weinstein_stage2", 0, 0)
    wrap_method(SS.StockScreener, "_rank", 0, None)
    wrap_method(SS.StockScreener, "_filter_by_price_drop", 0, 0)
    f0 = SS.StockScreener._quotes_from_bars

    def qfb(self, symbols, window=20):
        res = f0(self, symbols, window)
        stages.append({"stage": "_quotes_from_bars", "t_utc": datetime.now(timezone.utc).isoformat(), "out": res})
        return res
    SS.StockScreener._quotes_from_bars = qfb
    f1 = SS.StockScreener._fetch_quotes_chunked

    def fqc(symbols, *a, **kw):
        res = f1(symbols, *a, **kw)
        stages.append({"stage": "_fetch_quotes_chunked", "t_utc": datetime.now(timezone.utc).isoformat(), "n_symbols": len(symbols),
                       "out": {s: {k: v.get(k) for k in ("price", "marketCap", "volume", "avgVolume", "open", "previousClose", "dayHigh", "dayLow", "timestamp")}
                               for s, v in res.items()}})
        return res
    SS.StockScreener._fetch_quotes_chunked = staticmethod(fqc)
    fp0 = FP.FMPScreenerProvider.screen_stocks

    def sst(self, filters, as_of=None):
        res = fp0(self, filters, as_of)
        stages.append({"stage": "provider.screen_stocks", "t_utc": datetime.now(timezone.utc).isoformat(), "filters": filters,
                       "out": snap(res) if isinstance(res, list) else res})
        return res
    FP.FMPScreenerProvider.screen_stocks = sst
    return state, stages


def main(argv):
    label = argv[1]
    iids = [int(x) for x in argv[2:]] or [7, 8, 10, 11]
    root = os.path.join(HERE, label)
    os.makedirs(root, exist_ok=True)
    con, key = read_prod()
    settings = {i: instance_settings_raw(con, i) for i in iids}
    con.close()
    deadline = time.time() + LABEL_BUDGET_S
    state, stages = install(root, key, deadline)
    import ba2_providers.StockScreener as SS
    summary = {"label": label, "started_utc": datetime.now(timezone.utc).isoformat(), "instances": {}}
    for iid in iids:
        d = os.path.join(root, f"inst{iid}")
        os.makedirs(d, exist_ok=True)
        state["inst_dir"] = d
        state["n"] = 0
        del stages[:]
        t0 = datetime.now(timezone.utc)
        res = None
        err = None
        try:
            sc = SS.StockScreener(settings[iid])          # exactly JobManager._execute_screener_analysis: StockScreener(expert.settings)
            res = sc.screen()
        except CaptureAbort as e:
            err = f"ABORTED: {e}"
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
        t1 = datetime.now(timezone.utc)
        out = {"instance": iid, "settings": settings[iid], "started_utc": t0.isoformat(), "finished_utc": t1.isoformat(),
               "http_attempts": state["n"], "error": err,
               "picks": [r.get("symbol") for r in (res or {}).get("results", [])] if res else None,
               "results": [{k: v for k, v in r.items()} for r in (res or {}).get("results", [])] if res else None,
               "stats": (res or {}).get("stats") if res else None,
               "resolved_settings": sc._settings if res is not None or err else None}
        with open(os.path.join(d, "result.json"), "w") as f:
            json.dump(out, f, default=str)
        with open(os.path.join(d, "stages.json"), "w") as f:
            json.dump(stages, f, default=str)
        summary["instances"][iid] = {"picks": len(out["picks"] or []), "attempts": state["n"], "error": err, "started": t0.isoformat(), "finished": t1.isoformat()}
        print(f"[{label}] inst {iid}: {summary['instances'][iid]}", flush=True)
        if err and err.startswith("ABORTED"):
            break
    summary["finished_utc"] = datetime.now(timezone.utc).isoformat()
    with open(os.path.join(root, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1)


if __name__ == "__main__":
    main(sys.argv)

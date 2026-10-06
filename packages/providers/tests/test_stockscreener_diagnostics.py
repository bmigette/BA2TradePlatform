"""StockScreener stage diagnostics + fail-loud live data errors (no network, fake FMP).

2026-10: four straight Monday FactorRanker screens logged only "screener returned 0 candidates".
These tests pin: per-stage counts, FMP failure/retry counters, ONE warning naming the dropped
symbols, ScreenerDataError past the threshold -- and that the as_of (backtest) path is untouched.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

import ba2_providers
import ba2_providers.fmp_common as fc
import ba2_providers.StockScreener as S

AS_OF = datetime(2024, 3, 15, tzinfo=timezone.utc)


class _Rec:
    def __init__(self):
        self.lines = []

    def _add(self, level):
        return lambda msg, *a, **k: self.lines.append((level, str(msg)))

    def __getattr__(self, name):
        if name in ("debug", "info", "warning", "error"):
            return self._add(name)
        raise AttributeError(name)

    def at(self, level):
        return [m for lv, m in self.lines if lv == level]


class _Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status
        self.headers = {}

    def json(self):
        return self._p


def _bars(anchor, *, last_vol, close, peak=100.0, n=30, today_close=None):
    """FMP shape: newest-first. Last COMPLETE session is anchor-1d; avg volume ~1000.
    ``today_close`` adds the in-progress bar dated today that FMP returns during the session."""
    out = []
    if today_close is not None:
        out.append({"date": anchor.strftime("%Y-%m-%d"), "open": peak, "high": peak,
                    "low": peak - 1, "close": today_close, "volume": 5})
    for i in range(1, n + 1):
        d = (anchor - timedelta(days=i)).strftime("%Y-%m-%d")
        out.append({"date": d, "open": peak, "high": peak, "low": peak - 1,
                    "close": close if i == 1 else peak, "volume": last_vol if i == 1 else 1000})
    return out


class FakeFMP:
    """Stand-in for fmp_http_get. ``fail`` = symbols whose chunk raises FMPError;
    ``rate_limit_first`` = chunks (by first symbol) answered 429 twice before succeeding."""

    def __init__(self, anchor, rvol_fail=(), fail=(), rate_limit_first=(), today_close=None):
        self.anchor = anchor
        self.today_close = today_close
        self.rvol_fail = set(rvol_fail)
        self.fail = set(fail)
        self.rate_limit_first = set(rate_limit_first)
        self.calls = []

    def __call__(self, url, params=None, endpoint=None, timeout=None, **kw):
        syms = url.rsplit("/", 1)[1].split(",")
        self.calls.append((tuple(syms), dict(kw)))
        if self.fail & set(syms):
            raise fc.FMPError("HTTP 429 after 4 attempts")
        getter = kw.get("getter")
        if getter is not None and syms[0] in self.rate_limit_first:
            self.rate_limit_first.discard(syms[0])    # rate-limited once, then healthy
            getter(url, params=params, timeout=timeout)   # 429
            getter(url, params=params, timeout=timeout)   # 429
            getter(url, params=params, timeout=timeout)   # 200
        hist = []
        for s in syms:
            low_vol = s in self.rvol_fail
            hist.append({"symbol": s, "historical": _bars(
                self.anchor, last_vol=100 if low_vol else 3000, close=80.0,
                today_close=self.today_close)})
        return _Resp({"historicalStockList": hist})


@pytest.fixture
def env(monkeypatch):
    rec = _Rec()
    monkeypatch.setattr(S, "logger", rec)
    monkeypatch.setattr(S, "get_app_setting", lambda k: "key", raising=False)
    # No cross-test cache bleed: the live memo is bypassed.
    monkeypatch.setattr(fc, "fmp_live_cache_get", lambda key, *a, **k: fc.FMP_LIVE_CACHE_MISS)
    monkeypatch.setattr(fc, "fmp_live_cache_put", lambda *a, **k: None)
    monkeypatch.setattr(S.StockScreener, "_fetch_quotes_chunked",
                        staticmethod(lambda symbols, *a, **k: {}))
    return rec


def _install(monkeypatch, symbols, fake, http_params=None):
    cands = [{"symbol": s, "market_cap": 5e9 - i, "price": 80.0, "volume": 1000}
             for i, s in enumerate(symbols)]

    class Prov:
        def screen_stocks(self, filters, as_of=None):
            return [dict(c) for c in cands]

        def _build_params(self, filters):
            return dict(http_params or {"apikey": "key", "priceMoreThan": 1})

    monkeypatch.setattr(ba2_providers, "get_provider", lambda *a, **k: Prov())
    monkeypatch.setattr(fc, "fmp_http_get", fake)


_SETTINGS = {"screener_relative_volume_min": 1.3, "screener_price_drop_pct": 10.0,
             "screener_price_drop_days": 5, "screener_max_stocks": 100,
             "screener_float_min": 0, "screener_volume_min": 0}


def _live(symbols, **fake_kw):
    anchor = datetime.now(timezone.utc)
    return anchor, FakeFMP(anchor, **fake_kw)


def test_normal_run_logs_stage_counts(monkeypatch, env):
    syms = [f"N{i:02d}" for i in range(10)]
    anchor, fake = _live(syms, rvol_fail={"N00", "N01"})
    _install(monkeypatch, syms, fake)
    sc = S.StockScreener(_SETTINGS)
    out = sc.screen()
    assert len(out["results"]) == 8
    st = sc.last_diagnostics["stages"]
    assert st == {"provider": 10, "relative_volume": 8, "volume_filters": 8,
                  "price_drop": 8, "final": 8}
    assert sc.last_diagnostics["fmp"]["history_chunks_failed"] == 0
    assert sc.last_diagnostics["dropped_data_fetch_failure"] == 0
    stage_lines = [m for m in env.at("info") if "LIVE STAGES" in m]
    assert len(stage_lines) == 1
    assert "provider=10 -> relative_volume=8" in stage_lines[0] and "final=8" in stage_lines[0]
    assert env.at("warning") == []
    # The result shape is unchanged: diagnostics live on the attribute only.
    assert set(out) == {"results", "stats"}


def test_stage_counts_follow_bt_formulas(monkeypatch, env):
    """float stage -> stages["float"]; relative_volume = len + dropped_float + vmin + vmax
    (of the stage-2 stats), so the floor drops are added back."""
    import ba2_providers.screener.float_filter as ff
    syms = [f"V{i:02d}" for i in range(10)]
    anchor, fake = _live(syms, rvol_fail={"V00", "V01"})     # avg volume 1005 (others 1100)
    _install(monkeypatch, syms, fake)
    monkeypatch.setattr(ff, "filter_by_float",
                        lambda cands, lo, hi: (cands[:9], {"dropped_float": 1, "float_unknown": 0}))
    settings = dict(_SETTINGS, screener_relative_volume_min=0, screener_float_min=1000,
                    screener_volume_min=1050)
    sc = S.StockScreener(settings)
    out = sc.screen()
    st = sc.last_diagnostics["stages"]
    assert st["provider"] == 10 and st["float"] == 9
    # 9 float survivors, 2 (V00, V01) under the volume floor -> 7 survive stage 2
    assert out["stats"]["dropped_volume_min"] == 2 and out["stats"]["dropped_float"] == 1
    assert st["volume_filters"] == 7
    assert st["relative_volume"] == 7 + 0 + 2 + 0      # enrich_stats["dropped_float"] is 0 live
    assert st["final"] == 7
    assert sc.last_diagnostics["stage_symbols"]["float"] == [f"V{i:02d}" for i in range(9)]


def test_every_diagnostic_field_present(monkeypatch, env):
    syms = [f"D{i:02d}" for i in range(6)]
    anchor, fake = _live(syms, rvol_fail={"D00"}, today_close=77.0)
    _install(monkeypatch, syms, fake)
    sc = S.StockScreener(dict(_SETTINGS, screener_max_stocks=3))
    out = sc.screen()
    d = sc.last_diagnostics
    assert [r["symbol"] for r in out["results"]] == ["D01", "D02", "D03"]
    # (1) vendor request + call time
    req = d["vendor_request"]
    assert req["provider"] == "fmp" and req["filters"]["limit"] == 10_000
    assert req["http_params"]["priceMoreThan"] == 1 and req["rows"] == 6
    assert req["called_at"] and req["returned_at"]
    # (2) raw vendor rows: symbol, price, marketCap, volume
    assert set(d["vendor_rows"][0]) == {"symbol", "price", "marketCap", "volume"}
    assert len(d["vendor_rows"]) == 6
    # (3) per-candidate stage-2 evidence
    c = d["candidates"]["D01"]
    for k in ("price", "price_source", "market_cap", "market_cap_source", "quote_timestamp",
              "avg_volume", "relative_volume", "volume", "history"):
        assert k in c
    h = c["history"]
    assert h["first_bar_date"] < h["last_bar_date"]
    assert h["today_bar_present"] is True and h["today_bar_close"] == 77.0
    assert "D00" not in d["candidates"]
    # (4) symbol lists per stage + the final ordered list with its rank key
    assert d["stage_symbols"]["provider"] == syms
    assert d["stage_symbols"]["volume_filters"] == syms[1:]
    assert d["stage_symbols"]["final"] == ["D01", "D02", "D03"]
    assert [(r["rank"], r["symbol"], r["rank_metric"]) for r in d["final"]] == [
        (1, "D01", "market_cap"), (2, "D02", "market_cap"), (3, "D03", "market_cap")]
    assert d["final"][0]["rank_key"] > d["final"][1]["rank_key"]
    # (5) price-drop walk: peak, current price, drop
    w = {r["symbol"]: r for r in d["price_drop"]}
    assert w["D01"]["peak"] == 100.0 and w["D01"]["current_price"] == 80.0
    assert w["D01"]["drop_pct"] == 20.0 and w["D01"]["passed"] is True
    # (6) resolved settings
    assert d["settings"]["screener_relative_volume_min"] == 1.3
    # logs: compact structured INFO lines, lists capped
    info = "\n".join(env.at("info"))
    for tag in ("DIAG vendor", "DIAG stage provider", "DIAG candidates after stage 2",
                "DIAG price-drop walk", "DIAG final order"):
        assert tag in info
    json.dumps(d)       # JSON-serialisable as stored


def test_log_lists_capped_at_50_but_diagnostics_hold_all(monkeypatch, env):
    syms = [f"L{i:03d}" for i in range(70)]
    anchor, fake = _live(syms)
    _install(monkeypatch, syms, fake)
    sc = S.StockScreener(dict(_SETTINGS, screener_max_stocks=100))
    sc.screen()
    assert len(sc.last_diagnostics["stage_symbols"]["provider"]) == 70
    line = [m for m in env.at("info") if "DIAG stage provider" in m][0]
    assert "(+20 more)" in line and "L049" in line and "L050" not in line


def test_no_api_key_leak(monkeypatch, env, tmp_path):
    secret = "sk-SUPERSECRET-123456"
    monkeypatch.setattr(S, "get_app_setting", lambda k: secret, raising=False)
    monkeypatch.setenv(S.SCREENER_DIAG_DIR_ENV, str(tmp_path))
    syms = [f"S{i:02d}" for i in range(4)]
    anchor, fake = _live(syms)
    _install(monkeypatch, syms, fake, http_params={"apikey": secret, "priceMoreThan": 1})
    # even a setting value that embeds the key is scrubbed
    sc = S.StockScreener(dict(_SETTINGS, screener_provider=f"fmp-{secret}"))
    sc.screen()
    d = sc.last_diagnostics
    assert secret not in json.dumps(d, default=str)
    assert d["vendor_request"]["http_params"]["apikey"] == S._REDACTED
    # every line THIS feature logs is clean (the pre-existing stage-1 line echoes the provider
    # setting verbatim, which this test deliberately contaminates)
    new_lines = [m for _, m in env.lines if "DIAG" in m or "LIVE STAGES" in m]
    assert new_lines and all(secret not in m for m in new_lines)
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1 and secret not in files[0].read_text(encoding="utf-8")
    # the redaction helper itself
    assert S._redact({"FMP_API_KEY": secret, "n": [f"u?{secret}"]}, [secret]) == {
        "FMP_API_KEY": S._REDACTED, "n": [f"u?{S._REDACTED}"]}


def test_json_dump_is_opt_in_and_atomic(monkeypatch, env, tmp_path):
    syms = [f"J{i:02d}" for i in range(4)]
    anchor, fake = _live(syms)
    _install(monkeypatch, syms, fake)
    monkeypatch.delenv(S.SCREENER_DIAG_DIR_ENV, raising=False)
    S.StockScreener(_SETTINGS).screen()
    assert list(tmp_path.iterdir()) == []
    target = tmp_path / "diag" / "nested"
    monkeypatch.setenv(S.SCREENER_DIAG_DIR_ENV, str(target))
    sc = S.StockScreener(_SETTINGS)
    sc.screen()
    files = list(target.iterdir())
    assert [f.suffix for f in files] == [".json"]           # no leftover .tmp
    assert json.loads(files[0].read_text(encoding="utf-8")) == json.loads(
        json.dumps(sc.last_diagnostics, default=str))


def test_diagnostics_survive_a_refusal(monkeypatch, env):
    syms = [f"F{i:02d}" for i in range(4)]
    anchor, fake = _live(syms, fail=set(syms))
    _install(monkeypatch, syms, fake)
    sc = S.StockScreener(_SETTINGS)
    with pytest.raises(S.ScreenerDataError):
        sc.screen()
    d = sc.last_diagnostics
    assert d["error"].startswith("ScreenerDataError") and d["stages"]["provider"] == 4
    assert d["dropped_data_fetch_failure"] == 4
    assert [m for m in env.at("warning") if "DATA LOSS" in m]


def test_partial_failure_under_threshold_warns_and_returns(monkeypatch, env):
    syms = [f"P{i:02d}" for i in range(60)]       # 12 chunks; one fails = 5/60 = 8.3%
    anchor, fake = _live(syms, fail={"P07"})
    _install(monkeypatch, syms, fake)
    sc = S.StockScreener(_SETTINGS)
    out = sc.screen()
    assert len(out["results"]) == 55
    data_loss = [m for m in env.at("warning") if "DATA LOSS" in m]
    assert len(data_loss) == 1
    assert "5 symbol(s) dropped" in data_loss[0] and "P05" in data_loss[0]
    assert sc.last_diagnostics["fmp"]["history_chunks_failed"] == 1
    assert sc.last_diagnostics["dropped_data_fetch_failure"] == 5


def test_rate_limit_retries_are_counted_and_logged(monkeypatch, env):
    syms = [f"R{i:02d}" for i in range(10)]
    anchor, fake = _live(syms, rate_limit_first={"R00"})
    _install(monkeypatch, syms, fake)
    statuses = iter([429, 429, 200])
    import requests
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: _Resp({}, next(statuses)))
    sc = S.StockScreener(_SETTINGS)
    out = sc.screen()
    assert len(out["results"]) == 10
    f = sc.last_diagnostics["fmp"]
    assert f["history_rate_limited_responses"] == 2
    assert f["history_retried_requests"] == 2
    line = [m for m in env.at("info") if "LIVE STAGES" in m][0]
    assert "retried=2" in line and "rate_limited_429/5xx=2" in line


def test_live_missing_api_key_raises(monkeypatch, env):
    monkeypatch.setattr(S, "get_app_setting", lambda k: "", raising=False)
    syms = ["K00", "K01"]
    anchor, fake = _live(syms)
    _install(monkeypatch, syms, fake)
    with pytest.raises(S.ScreenerDataError):
        S.StockScreener(_SETTINGS).screen()


# ---- backtest path: untouched, NO diagnostics ------------------------------------------------

def _run_backtest(monkeypatch, fail):
    syms = [f"B{i:02d}" for i in range(10)]
    fake = FakeFMP(AS_OF, rvol_fail={"B00"}, fail=fail)
    _install(monkeypatch, syms, fake)
    sc = S.StockScreener(dict(_SETTINGS), as_of=AS_OF)
    return sc, sc.screen(), fake


def test_backtest_path_unchanged_with_healthy_data(monkeypatch, env, tmp_path):
    monkeypatch.setenv(S.SCREENER_DIAG_DIR_ENV, str(tmp_path))
    sc, out, fake = _run_backtest(monkeypatch, fail=())
    # Golden value produced by the pre-change code on the same scenario.
    assert json.dumps(out, sort_keys=True) == json.dumps(_GOLDEN_HEALTHY, sort_keys=True)
    # fmp_http_get is called with exactly the pre-change arguments (no getter wrapper).
    assert all(kw == {} for _, kw in fake.calls)
    # No diagnostics of any kind: empty attribute, no new log line, no file.
    assert sc.last_diagnostics == {}
    assert [m for lv, m in env.lines if "LIVE STAGES" in m or "DATA LOSS" in m or "DIAG" in m] == []
    assert list(tmp_path.iterdir()) == []


def test_backtest_path_never_raises_on_failed_chunk(monkeypatch, env):
    # Pre-change behaviour: a failed chunk is swallowed (symbols dropped, no raise, no new log).
    sc, out, fake = _run_backtest(monkeypatch, fail={"B00", "B05"})
    assert json.dumps(out, sort_keys=True) == json.dumps(_GOLDEN_CHUNK_FAIL, sort_keys=True)
    assert sc.last_diagnostics == {}
    assert [m for lv, m in env.lines if "LIVE STAGES" in m or "DATA LOSS" in m or "DIAG" in m] == []


def test_rank_key_refactor_is_identical():
    sc = S.StockScreener(dict(_SETTINGS, screener_sort_metric="composite"))
    rows = [{"symbol": "A", "market_cap": 2, "volume": 3, "float_shares": 5},
            {"symbol": "B", "market_cap": 9, "volume": 1}, {"symbol": "C", "market_cap": 1}]
    assert [r["symbol"] for r in sc._rank(rows)] == ["A", "B", "C"]
    assert sc._sort_key_fn()(rows[0]) == 30 and sc._sort_key_fn()(rows[1]) == 9


# Captured by running these exact scenarios against origin/dev's StockScreener (pre-change).
_GOLDEN_HEALTHY = json.loads(r'''{"results": [{"avg_volume": 1100.0, "market_cap": 4999999999.0, "price": 80.0, "price_drop_pct": 20.0, "relative_volume": 2.73, "symbol": "B01", "volume": 3000}, {"avg_volume": 1100.0, "market_cap": 4999999998.0, "price": 80.0, "price_drop_pct": 20.0, "relative_volume": 2.73, "symbol": "B02", "volume": 3000}, {"avg_volume": 1100.0, "market_cap": 4999999997.0, "price": 80.0, "price_drop_pct": 20.0, "relative_volume": 2.73, "symbol": "B03", "volume": 3000}, {"avg_volume": 1100.0, "market_cap": 4999999996.0, "price": 80.0, "price_drop_pct": 20.0, "relative_volume": 2.73, "symbol": "B04", "volume": 3000}, {"avg_volume": 1100.0, "market_cap": 4999999995.0, "price": 80.0, "price_drop_pct": 20.0, "relative_volume": 2.73, "symbol": "B05", "volume": 3000}, {"avg_volume": 1100.0, "market_cap": 4999999994.0, "price": 80.0, "price_drop_pct": 20.0, "relative_volume": 2.73, "symbol": "B06", "volume": 3000}, {"avg_volume": 1100.0, "market_cap": 4999999993.0, "price": 80.0, "price_drop_pct": 20.0, "relative_volume": 2.73, "symbol": "B07", "volume": 3000}, {"avg_volume": 1100.0, "market_cap": 4999999992.0, "price": 80.0, "price_drop_pct": 20.0, "relative_volume": 2.73, "symbol": "B08", "volume": 3000}, {"avg_volume": 1100.0, "market_cap": 4999999991.0, "price": 80.0, "price_drop_pct": 20.0, "relative_volume": 2.73, "symbol": "B09", "volume": 3000}], "stats": {"dropped_float": 0, "dropped_price_drop": 0, "dropped_rvol": 1, "dropped_volume_max": 0, "final_count": 9, "price_drop_checked": 9, "screener_candidates": 10}}''')
_GOLDEN_CHUNK_FAIL = json.loads(r'''{"results": [], "stats": {"dropped_float": 0, "dropped_rvol": 10, "dropped_volume_max": 0, "screener_candidates": 10}}''')

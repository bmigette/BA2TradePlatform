"""Corporate-actions refresh of the Alpaca history cache (plan 2026-10-24-history-source-task11, Task 6)."""
import hashlib
import json
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from ba2_common.core import native_cache
from ba2_providers.ohlcv.alpaca_history import corporate_actions as ca
from ba2_providers.ohlcv.alpaca_history import live_state, split_table

FOLDER = "AlpacaHistoryOHLCVProvider"
TODAY = date(2026, 10, 23)                                    # Fri
NOW = datetime(2026, 10, 23, 12, 30, tzinfo=timezone.utc)     # 08:30 ET


def _sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


class FakeCA:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def actions(self, symbols, start, end):
        self.calls.append((list(symbols), start, end))
        return self.rows


def fwd(symbol, ex, new=2, old=1):
    return {"forward_splits": [{"symbol": symbol, "ex_date": ex, "new_rate": new, "old_rate": old}]}


def raw_series(split_day=None, f=2.0, last="2026-10-22"):
    days = pd.bdate_range("2026-09-25", last)
    px = [10.0 if split_day is None or d < pd.Timestamp(split_day) else 10.0 / f for d in days]
    return pd.DataFrame({"Date": days, "Open": px, "High": [p + 1 for p in px], "Low": [p - 1 for p in px],
                         "Close": px, "Volume": 1000.0})


class FakeRaw:
    def __init__(self, frames):
        self.frames, self.calls = frames, []

    def __call__(self, symbol, start, end):
        self.calls.append(symbol)
        return self.frames[symbol]


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(tmp_path))
    d = tmp_path / FOLDER
    meta = d / "_meta"
    meta.mkdir(parents=True)
    days = pd.bdate_range(end="2026-10-16", periods=10)
    base = pd.DataFrame({"Date": days, "Open": 10.0, "High": 11.0, "Low": 9.0, "Close": 10.0, "Volume": 1000.0,
                         "effective_date": days})
    for sym in ("AAA", "BBB"):
        base.to_parquet(d / f"{sym}_1d.parquet", index=False)
        base.to_parquet(d / f"{sym}_5min.parquet", index=False)
    (meta / "split_table.json").write_text(json.dumps(
        {"run_id": "R1", "asof": "2026-10-12", "splits": {}, "spinoffs": {}, "absorbed_spinoffs": {}, "breaks": {}}))
    manifest = {"store_asof": "2026-10-12", "split_table": {"run_id": "R1", "sha256": _sha(meta / "split_table.json")},
                "symbols": {s: {"events": {"splits": [], "spinoffs": []}, "1d": {"first": "2016-01-04"}}
                            for s in ("AAA", "BBB")}}
    (meta / "build_manifest.json").write_text(json.dumps(manifest))
    (meta / "gate_certificate.json").write_text(json.dumps(
        {"status": "PASS", "build_manifest_sha256": _sha(meta / "build_manifest.json")}))
    return d


def run(symbols, rows, frames, **kw):
    return ca.refresh(symbols, today=TODAY, now=NOW, ca_client=FakeCA(rows), raw_fetcher=FakeRaw(frames),
                      overrides=[], **kw)


def test_proven_2to1_split_rebases_both_files_and_keeps_table_manifest_certificate_consistent(cache):
    rep = run(["AAA"], fwd("AAA", "2026-10-20"), {"AAA": raw_series("2026-10-20")})
    assert [(a["symbol"], a["date"], a["factor"], a["kind"]) for a in rep["applied"]] == [("AAA", "2026-10-20", 2.0, "split")]
    for iv in ("1d", "5min"):
        df = pd.read_parquet(cache / f"AAA_{iv}.parquet")
        assert set(df["Close"]) == {5.0} and set(df["Volume"]) == {2000.0}
    meta = cache / "_meta"
    table = json.loads((meta / "split_table.json").read_text())
    manifest = json.loads((meta / "build_manifest.json").read_text())
    assert table["splits"] == {"AAA": [["2026-10-20", 2.0]]}
    assert manifest["symbols"]["AAA"]["events"]["splits"] == [["2026-10-20", 2.0]]
    assert manifest["split_table"]["sha256"] == _sha(meta / "split_table.json")      # what option_split_basis compares
    assert json.loads((meta / "gate_certificate.json").read_text())["build_manifest_sha256"] == _sha(meta / "build_manifest.json")
    assert (meta / "split_table.json.bak").exists()
    assert pd.read_parquet(cache / "BBB_1d.parquet")["Close"].eq(10.0).all()          # an unlisted symbol is untouched


def test_unproven_split_is_pending_not_applied(cache):
    rep = run(["AAA"], fwd("AAA", "2026-10-26"), {"AAA": raw_series()})             # ex-date Monday, no bar yet
    assert rep["applied"] == [] and [p["date"] for p in rep["pending"]] == ["2026-10-26"]
    assert pd.read_parquet(cache / "AAA_1d.parquet")["Close"].eq(10.0).all()
    assert json.loads((cache / "_meta" / corporate_status()).read_text())["pending"][0]["symbol"] == "AAA"


def corporate_status():
    return ca.STATUS_NAME


def test_refresh_idempotent(cache):
    args = (["AAA"], fwd("AAA", "2026-10-20"), {"AAA": raw_series("2026-10-20")})
    run(*args)
    before = pd.read_parquet(cache / "AAA_1d.parquet")
    manifest_bytes = (cache / "_meta" / "build_manifest.json").read_bytes()
    rep = run(*args)
    assert rep["applied"] == []
    pd.testing.assert_frame_equal(before, pd.read_parquet(cache / "AAA_1d.parquet"))
    assert (cache / "_meta" / "build_manifest.json").read_bytes() == manifest_bytes


def test_review_verdict_is_never_applied_and_blocks_topup(cache):
    # Alpaca lists a 2:1 split but the price never moved: the judge contradicts the listing
    rep = run(["AAA"], fwd("AAA", "2026-10-20"), {"AAA": raw_series()})
    assert rep["applied"] == [] and rep["review"], rep
    assert pd.read_parquet(cache / "AAA_1d.parquet")["Close"].eq(10.0).all()
    assert live_state.manifest_events("AAA") == []
    with open(cache / "_meta" / ca.REVIEW_CSV_NAME, encoding="utf-8") as f:
        assert "AAA" in f.read()
    from ba2_providers.ohlcv.alpaca_history.live_topup import PendingCorporateAction
    with pytest.raises(PendingCorporateAction, match="under review"):
        ca.assert_not_in_review("AAA")
    ca.assert_not_in_review("BBB")


def test_uses_decide_splits_not_a_copy(cache, monkeypatch):
    seen = []
    real = split_table.decide_splits

    def spy(*a, **k):
        seen.append(a)
        return real(*a, **k)
    monkeypatch.setattr(ca, "decide_splits", spy)
    run(["AAA"], fwd("AAA", "2026-10-20"), {"AAA": raw_series("2026-10-20")})
    assert len(seen) == 1


def test_fmp_rows_are_passed_as_second_opinion(cache, monkeypatch):
    got = []
    real = split_table.decide_splits
    monkeypatch.setattr(ca, "decide_splits", lambda raw, al, fmp, *a, **k: (got.append(list(fmp)), real(raw, al, fmp, *a, **k))[1])
    rows = lambda a, b: [{"date": "2026-10-20", "symbol": "AAA", "numerator": 2, "denominator": 1}]
    rep = run(["AAA"], fwd("AAA", "2026-10-20"), {"AAA": raw_series("2026-10-20")}, fmp_rows=rows)
    assert got and [(e.symbol, e.ratio) for e in got[0]] == [("AAA", 2.0)] and rep["fmp"] == "used"
    assert len(rep["applied"]) == 1


def test_no_fmp_key_is_said_in_the_report(cache):
    assert "no key" in run(["AAA"], {}, {})["fmp"]


def test_symbol_not_in_history_is_reported_not_fetched(cache):
    rep = run(["ZZZ", "AAA"], {}, {})
    assert rep["not_in_history"] == ["ZZZ"] and rep["symbols_checked"] == ["AAA"]


def test_stale_run_blocks_topup(cache):
    with pytest.raises(ca.CorporateActionsStale, match="no corporate-actions refresh"):
        ca.assert_fresh(datetime(2026, 10, 26, 14, tzinfo=timezone.utc))
    run(["AAA"], {}, {})                                                             # full run, run_date 2026-10-23 (Fri)
    ca.assert_fresh(datetime(2026, 10, 26, 14, tzinfo=timezone.utc))                 # Monday: previous session = Fri
    with pytest.raises(ca.CorporateActionsStale, match="last ran 2026-10-23"):
        ca.assert_fresh(datetime(2026, 10, 27, 14, tzinfo=timezone.utc))             # Tuesday needs Monday's run


def test_on_demand_refresh_does_not_move_run_date(cache):
    run(["AAA"], {}, {})
    ca.refresh_symbol("BBB", ca_client=FakeCA({}), raw_fetcher=FakeRaw({}), overrides=[],
                      today=date(2026, 10, 27), now=datetime(2026, 10, 27, 13, tzinfo=timezone.utc))
    assert json.loads((cache / "_meta" / ca.STATUS_NAME).read_text())["run_date"] == "2026-10-23"


def test_fetch_failure_is_an_error_entry_not_a_silent_skip(cache):
    def boom(symbol, start, end):
        raise ca.AlpacaLiveFetchError("HTTP 500")
    rep = ca.refresh(["AAA"], today=TODAY, now=NOW, ca_client=FakeCA(fwd("AAA", "2026-10-20")), raw_fetcher=boom, overrides=[])
    assert rep["errors"] and "HTTP 500" in rep["errors"][0]["error"] and rep["applied"] == []


def test_corporate_actions_client_pages_and_request_shape():
    from ba2_providers.ohlcv.alpaca_history.live_fetch import AlpacaLiveBars

    class T:
        def __init__(self):
            self.urls = []

        def get(self, url, headers):
            self.urls.append(url)
            tok = None if "page_token" in url else "p2"
            kind = "forward_splits" if tok else "reverse_splits"
            return 200, json.dumps({"corporate_actions": {kind: [{"symbol": "AAA"}]}, "next_page_token": tok}).encode()
    t = T()
    out = ca.AlpacaCorporateActions("k", "s", transport=t).actions(["BRK-B", "AAA"], date(2026, 10, 13), date(2026, 10, 30))
    assert set(out) == {"forward_splits", "reverse_splits"} and len(t.urls) == 2
    assert "symbols=BRK.B%2CAAA" in t.urls[0] and "/v1/corporate-actions" in t.urls[0] and "spin_off" in t.urls[0]
    assert issubclass(ca.AlpacaCorporateActions, AlpacaLiveBars)

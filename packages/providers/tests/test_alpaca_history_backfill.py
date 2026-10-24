"""Backfill-on-first-read of a symbol that is not in the Alpaca history build (plan 2026-10-24-history-source-task11,
Task 0 decision 5). Fake fetcher / corporate-actions client, tmp cache, no network."""
import hashlib
import json
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from ba2_common.core import native_cache, ohlcv_final_bars
from ba2_common.core.market_calendar import regular_session_dates
from ba2_providers.ohlcv.alpaca_history import build, live_state
from ba2_providers.ohlcv.alpaca_history.live_backfill import SymbolUnavailable
from ba2_providers.ohlcv.alpaca_history.live_topup import PendingCorporateAction, TopUpGap

FOLDER = "AlpacaHistoryOHLCVProvider"
NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)                   # Saturday: sessions through Fri 10-23 are final
START = datetime(2026, 8, 1)


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def sessions(first, last="2026-10-23"):
    return [pd.Timestamp(d) for d in regular_session_dates(date.fromisoformat(first), date.fromisoformat(last))]


def raw_daily(days, split_on=None, f=2.0, base=40.0):
    rows = []
    for i, d in enumerate(days):
        px = base * (1 + 0.004 * i) / (f if split_on is not None and d >= pd.Timestamp(split_on) else 1.0)
        rows.append({"Date": d, "Open": px, "High": px * 1.01, "Low": px * 0.99, "Close": px, "Volume": 1000.0 + i})
    return pd.DataFrame(rows)


def raw_5min(days, px=10.0):
    t = [pd.Timestamp(d) + pd.Timedelta(hours=9, minutes=30 + 5 * i) for d in days for i in range(78)]
    return pd.DataFrame({"Date": t, "Open": px, "High": px + 1, "Low": px - 1, "Close": px, "Volume": 100.0})


class FakeBars:
    def __init__(self, daily=None, intraday=None):
        self.d, self.i, self.calls = daily or {}, intraday or {}, []

    @staticmethod
    def _slice(frame, start, end):
        x = pd.to_datetime(frame["Date"]).dt.normalize()
        return frame[(x >= pd.Timestamp(start)) & (x < pd.Timestamp(end))]

    def daily(self, symbol, start, end):
        self.calls.append(("1d", symbol, start, end))
        return self._slice(self.d[symbol], start, end) if symbol in self.d else pd.DataFrame(columns=["Date"])

    def intraday_5min(self, symbol, start, end):
        self.calls.append(("5min", symbol, start, end))
        return self._slice(self.i[symbol], start, end) if symbol in self.i else pd.DataFrame(columns=["Date"])


class FakeCA:
    def __init__(self, month=None):
        self.month, self.calls = month or {}, 0

    def actions(self, symbols, start, end):
        self.calls += 1
        return self.month


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(tmp_path))
    import ba2_common.config as cfg
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path))
    monkeypatch.setattr(ohlcv_final_bars, "now_utc", lambda: NOW)
    root = tmp_path / FOLDER
    meta = root / "_meta"
    meta.mkdir(parents=True)
    days = pd.bdate_range("2026-08-03", "2026-10-16")
    base = pd.DataFrame({"Date": days, "Open": 10.0, "High": 11.0, "Low": 9.0, "Close": 10.0, "Volume": 1000,
                         "effective_date": days})
    base.to_parquet(root / "AAA_1d.parquet", index=False)
    (meta / "split_table.json").write_text(json.dumps(
        {"run_id": "R1", "asof": "2026-10-12", "splits": {}, "spinoffs": {}, "absorbed_spinoffs": {}, "breaks": {}}))
    manifest = {"store_asof": "2026-10-12", "split_table": {"run_id": "R1", "sha256": sha(meta / "split_table.json")},
                "symbols": {"AAA": {"events": {"splits": [], "spinoffs": []}, "files": {"1d": "AAA_1d.parquet"},
                                    "1d": {"first": "2026-08-03", "last": "2026-10-16", "bars": len(days)},
                                    "5min": {"first": "2026-09-01", "last": "2026-10-16", "bars": 10}}}}
    (meta / "build_manifest.json").write_text(json.dumps(manifest))
    (meta / "gate_certificate.json").write_text(json.dumps(
        {"status": "PASS", "build_manifest_sha256": sha(meta / "build_manifest.json")}))
    (meta / "corporate_actions_live.json").write_text(json.dumps(
        {"run_date": "2026-10-23", "symbols_checked": [], "applied": [], "pending": [], "review": []}))
    return root


def provider(bars, ca=None):
    from ba2_providers.ohlcv.AlpacaHistoryOHLCVProvider import AlpacaHistoryOHLCVProvider
    return AlpacaHistoryOHLCVProvider("k", "s", fetcher=bars, clock=lambda: NOW,
                                      corporate_actions_client=ca or FakeCA())


def read(root, sym, iv="1d"):
    return pd.read_parquet(root / f"{sym}_{iv}.parquet")


def meta_consistent(root):
    meta = root / "_meta"
    manifest = json.loads((meta / "build_manifest.json").read_text())
    cert = json.loads((meta / "gate_certificate.json").read_text())
    assert cert["build_manifest_sha256"] == sha(meta / "build_manifest.json")
    assert manifest["split_table"]["sha256"] == sha(meta / "split_table.json")
    table = json.loads((meta / "split_table.json").read_text())
    for sym, e in manifest["symbols"].items():          # what option_split_basis compares: table events == applied events
        assert table["splits"].get(sym, []) == e["events"]["splits"], sym
        assert table["spinoffs"].get(sym, []) == e["events"]["spinoffs"], sym
    return manifest


def test_new_ipo_after_the_build_start_is_backfilled_from_its_first_bar(cache):
    days = sessions("2026-09-15")
    bars = FakeBars(daily={"NEW": raw_daily(days)})
    df = provider(bars).get_ohlcv_data("NEW", lookback_days=100, interval="1d")
    got = read(cache, "NEW")
    assert pd.to_datetime(got["Date"]).min() == pd.Timestamp("2026-09-15") and pd.to_datetime(got["Date"]).max() == pd.Timestamp("2026-10-23")
    assert len(got) == len(days) and not got["Date"].duplicated().any() and got["Date"].is_monotonic_increasing
    assert list(got.columns) == list(read(cache, "AAA").columns) and len(df) == len(days)
    assert bars.calls[0][2] == date(2026, 8, 3)                       # asked from the build start, answered from the IPO
    m = meta_consistent(cache)["symbols"]["NEW"]
    assert m["origin"] == "live_backfill" and m["backfilled_on"] == "2026-10-24" and m["events"] == {"splits": [], "spinoffs": []}
    assert m["1d"] == {"first": "2026-09-15", "last": "2026-10-23", "bars": len(days), "covered_from": "2026-08-03", "partial": True}


def test_old_symbol_absent_from_the_build_starts_at_the_build_start(cache):
    days = sessions("2026-06-01")                                      # trading since before the build start
    bars = FakeBars(daily={"OLD": raw_daily(days)})
    provider(bars).get_ohlcv_data("OLD", lookback_days=100, interval="1d")
    got = read(cache, "OLD")
    assert pd.to_datetime(got["Date"]).min() == pd.Timestamp("2026-08-03")
    meta_consistent(cache)


def test_split_during_its_history_is_applied_like_the_build(cache):
    days = sessions("2026-09-01")
    raw = raw_daily(days, split_on="2026-09-30")
    ca = FakeCA({"forward_splits": [{"symbol": "SPL", "ex_date": "2026-09-30", "new_rate": 2, "old_rate": 1}]})
    provider(FakeBars(daily={"SPL": raw}), ca).get_ohlcv_data("SPL", lookback_days=100, interval="1d")
    expected = build.finish(build.adjust(raw, [("2026-09-30", 2.0)]), "1d")
    pd.testing.assert_frame_equal(read(cache, "SPL"), expected, check_dtype=False, rtol=1e-12)
    m = meta_consistent(cache)["symbols"]["SPL"]
    assert m["events"]["splits"] == [["2026-09-30", 2.0]] and m["live"]["effective"] == "2026-09-30"
    assert live_state.manifest_events("SPL") == [("2026-09-30", 2.0, "split")]


def test_gap_writes_nothing_and_logs_an_error(cache, monkeypatch):
    from ba2_providers.ohlcv.alpaca_history import live_backfill
    errors = []
    monkeypatch.setattr(live_backfill.logger, "error", lambda m, *a, **k: errors.append(m))
    days = sessions("2026-09-01")
    days.pop(7)
    manifest_before = (cache / "_meta" / "build_manifest.json").read_bytes()
    with pytest.raises(TopUpGap):
        provider(FakeBars(daily={"GAP": raw_daily(days)})).get_ohlcv_data("GAP", lookback_days=100, interval="1d")
    assert not (cache / "GAP_1d.parquet").exists()
    assert (cache / "_meta" / "build_manifest.json").read_bytes() == manifest_before
    assert errors and "GAP" in errors[0] and not (cache / "_meta" / "locks" / "GAP.lock").exists()


def test_second_read_uses_the_cache(cache):
    bars = FakeBars(daily={"NEW": raw_daily(sessions("2026-09-15"))})
    p = provider(bars)
    p.get_ohlcv_data("NEW", lookback_days=100, interval="1d")
    n = len(bars.calls)
    p.get_ohlcv_data("NEW", lookback_days=100, interval="1d")
    provider(bars).get_ohlcv_data("NEW", end_date=datetime(2026, 10, 20), lookback_days=100, interval="1d")
    assert len(bars.calls) == n


def test_unknown_to_alpaca_and_non_nyse_symbols_stay_refused(cache):
    bars = FakeBars()
    with pytest.raises(SymbolUnavailable):
        provider(bars).get_ohlcv_data("NOPE", lookback_days=100, interval="1d")
    assert not (cache / "NOPE_1d.parquet").exists()
    bars2 = FakeBars(daily={"BTC/USD": raw_daily(sessions("2026-09-01"))})
    with pytest.raises(SymbolUnavailable, match="NYSE"):
        provider(bars2).get_ohlcv_data("BTC/USD", lookback_days=100, interval="1d")
    assert bars2.calls == []
    with pytest.raises(SymbolUnavailable, match="NYSE"):
        provider(bars2).get_ohlcv_data("VOD.L", lookback_days=100, interval="1d")
    meta_consistent(cache)


def test_a_split_the_judge_sends_to_review_refuses_the_backfill(cache):
    raw = raw_daily(sessions("2026-09-01"))                           # Alpaca lists 2:1 but the price never moved
    ca = FakeCA({"forward_splits": [{"symbol": "REV", "ex_date": "2026-09-30", "new_rate": 2, "old_rate": 1}]})
    with pytest.raises(PendingCorporateAction, match="review"):
        provider(FakeBars(daily={"REV": raw}), ca).get_ohlcv_data("REV", lookback_days=100, interval="1d")
    assert not (cache / "REV_1d.parquet").exists() and "REV" not in json.loads(
        (cache / "_meta" / "build_manifest.json").read_text())["symbols"]


def test_unlisted_split_in_the_series_is_refused_not_mixed(cache):
    raw = raw_daily(sessions("2026-09-01"), split_on="2026-09-30")    # the price halves, no corporate action listed
    with pytest.raises(PendingCorporateAction):
        provider(FakeBars(daily={"UNL": raw})).get_ohlcv_data("UNL", lookback_days=100, interval="1d")
    assert not (cache / "UNL_1d.parquet").exists()


def test_5min_backfill_of_a_built_symbol_uses_the_manifest_basis(cache):
    live_state.rebase_symbol("AAA", [("2026-10-01", 2.0, "split")], now=NOW, sync_split_table=True)
    days = sessions("2026-09-01")
    raw = pd.concat([raw_5min([d for d in days if d < pd.Timestamp("2026-10-01")], px=10.0),
                     raw_5min([d for d in days if d >= pd.Timestamp("2026-10-01")], px=5.0)], ignore_index=True)
    bars = FakeBars(intraday={"AAA": raw})
    ca = FakeCA()
    provider(bars, ca).get_ohlcv_data("AAA", lookback_days=60, interval="5min")
    got = read(cache, "AAA", "5min")
    pre = pd.to_datetime(got["Date"]) < pd.Timestamp("2026-10-01")
    assert got.loc[pre, "Close"].eq(5.0).all() and got.loc[~pre, "Close"].eq(5.0).all()      # one basis: x1/2 before the split
    assert ca.calls == 0 and bars.calls[0][2] == date(2026, 9, 1)
    m = meta_consistent(cache)["symbols"]["AAA"]
    assert m["5min"]["first"] == "2026-09-01" and m["files"]["5min"] == "AAA_5min.parquet"


def test_5min_gap_session_refuses(cache):
    days = sessions("2026-09-01")
    raw = raw_5min(days)
    drop = pd.to_datetime(raw["Date"]).dt.normalize() == days[10]
    with pytest.raises(TopUpGap):
        provider(FakeBars(intraday={"AAA": raw[~drop]})).get_ohlcv_data("AAA", lookback_days=60, interval="5min")
    assert not (cache / "AAA_5min.parquet").exists()

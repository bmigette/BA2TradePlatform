"""BT/live parity of the Alpaca live top-up and re-base (plan 2026-10-24-history-source-task11, Task 10).

"One shared function, two callers": the BUILD adjusts a whole raw series with ``build.adjust``; the live path adjusts the
appended sessions with the same function and re-bases old rows with the same function. The three routes must give the
same frame. Pure frames and tmp caches, no network.
"""
import hashlib
import json
import random
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from ba2_common.core import native_cache, ohlcv_final_bars
from ba2_common.core.market_calendar import regular_session_dates
from ba2_providers.ohlcv.alpaca_history import build, live_state, live_topup
from ba2_providers.ohlcv.alpaca_history.live_topup import PendingCorporateAction, topup_frame

PROVIDER = live_state.PROVIDER
NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)                  # Saturday: sessions through Fri 10-23 are final


def sessions(first, last):
    return [pd.Timestamp(d) for d in regular_session_dates(date.fromisoformat(first), date.fromisoformat(last))]


def raw_series(days, split_on=None, f=2.0, base=40.0):
    """RAW daily bars: a slow drift, and the price divided by ``f`` from ``split_on`` on."""
    rows = []
    for i, d in enumerate(days):
        px = base * (1 + 0.004 * i) / (f if split_on is not None and d >= pd.Timestamp(split_on) else 1.0)
        rows.append({"Date": d, "Open": px, "High": px * 1.01, "Low": px * 0.99, "Close": px, "Volume": 1000.0 + i})
    return pd.DataFrame(rows)


def native(frame):
    return frame.assign(effective_date=frame["Date"])


def whole_build(raw, events):
    return build.finish(build.adjust(raw.copy(), events), "1d")


def fetcher(raw):
    def fetch(start, end_excl):
        d = raw["Date"].dt.normalize()
        return raw[(d >= pd.Timestamp(start)) & (d < pd.Timestamp(end_excl))]
    return fetch


def assert_same(a, b):
    cols = ["Date", "Open", "High", "Low", "Close", "Volume"]
    pd.testing.assert_frame_equal(a[cols].reset_index(drop=True), b[cols].reset_index(drop=True), check_dtype=False,
                                  rtol=1e-12, atol=0)


DAYS = sessions("2026-09-01", "2026-10-23")
CUT = pd.Timestamp("2026-10-09")                                       # the cache (the build) ends here
SPLIT = "2026-10-14"                                                   # a split INSIDE the live range, known to the table


def test_build_equals_cache_plus_live_topup_without_events():
    raw = raw_series(DAYS)
    cached = native(whole_build(raw[raw["Date"] <= CUT], []))
    merged, rep = topup_frame(cached, fetcher(raw), [], [], interval="1d", now_utc=NOW, symbol="AAA")
    assert rep["appended"] == len(raw[raw["Date"] > CUT])
    assert_same(merged, whole_build(raw, []))


def test_a_known_event_in_the_new_range_without_a_rebase_is_refused_not_mixed():
    """The invariant is file and appended bars on ONE basis: an event the table already knows must have re-based the file
    first. Appending pre-event bars divided by the split onto undivided rows is a x0.5 seam: refused."""
    raw = raw_series(DAYS, split_on=SPLIT)
    cached = native(whole_build(raw[raw["Date"] <= CUT], []))
    with pytest.raises(PendingCorporateAction):
        topup_frame(cached, fetcher(raw), [(SPLIT, 2.0)], [], interval="1d", now_utc=NOW, symbol="AAA")


def test_rebase_then_topup_equals_the_build_of_the_whole_history(tmp_path, monkeypatch):
    """cache (no event) -> the refresh accepts SPLIT and re-bases -> top-up appends: identical to building it all at once."""
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(tmp_path))
    d = tmp_path / PROVIDER
    (d / "_meta").mkdir(parents=True)
    raw = raw_series(DAYS, split_on=SPLIT)
    old = raw[raw["Date"] <= CUT]
    native(whole_build(old, [])).to_parquet(d / "AAA_1d.parquet", index=False)
    (d / "_meta" / "build_manifest.json").write_text(json.dumps(
        {"store_asof": "2026-10-09", "symbols": {"AAA": {"events": {"splits": [], "spinoffs": []}}}}))
    live_state.rebase_symbol("AAA", [(SPLIT, 2.0, "split")], now=NOW)
    cur = pd.read_parquet(d / "AAA_1d.parquet")
    merged, _ = topup_frame(cur, fetcher(raw), [(SPLIT, 2.0)], [], interval="1d", now_utc=NOW, symbol="AAA")
    assert_same(merged, whole_build(raw, [(SPLIT, 2.0)]))


def test_rebase_equals_rebuild(tmp_path, monkeypatch):
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(tmp_path))
    d = tmp_path / PROVIDER
    (d / "_meta").mkdir(parents=True)
    raw = raw_series(sessions("2026-09-01", "2026-10-09"), split_on="2026-09-21", f=4.0)
    native(whole_build(raw, [])).to_parquet(d / "AAA_1d.parquet", index=False)
    (d / "_meta" / "build_manifest.json").write_text(json.dumps(
        {"store_asof": "2026-10-09", "symbols": {"AAA": {"events": {"splits": [], "spinoffs": []}}}}))
    live_state.rebase_symbol("AAA", [("2026-09-21", 4.0, "split")], now=NOW)
    assert_same(pd.read_parquet(d / "AAA_1d.parquet"), whole_build(raw, [("2026-09-21", 4.0)]))


def test_unknown_split_in_the_new_range_is_never_appended_on_a_mixed_basis():
    raw = raw_series(DAYS, split_on=SPLIT)
    cached = native(whole_build(raw[raw["Date"] <= CUT], []))
    with pytest.raises(PendingCorporateAction):
        topup_frame(cached, fetcher(raw), [], [], interval="1d", now_utc=NOW, symbol="AAA")   # the table does not know it


# ---- property test: random interleavings keep ONE basis ---------------------------------------------------------
def _digest(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


@pytest.mark.parametrize("seed", range(12))
def test_random_interleavings_never_mix_the_basis(seed, tmp_path, monkeypatch):
    rng = random.Random(seed)
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(tmp_path))
    d = tmp_path / PROVIDER
    (d / "_meta").mkdir(parents=True)
    days = sessions("2026-09-01", "2026-10-23")
    split_day = rng.choice(days[12:-3])
    raw = raw_series(days, split_on=split_day.date().isoformat())
    cut = rng.randrange(8, 11)
    native(whole_build(raw.iloc[:cut], [])).to_parquet(d / "AAA_1d.parquet", index=False)
    (d / "_meta" / "build_manifest.json").write_text(json.dumps(
        {"store_asof": days[cut - 1].date().isoformat(), "symbols": {"AAA": {"events": {"splits": [], "spinoffs": []}}}}))
    path = d / "AAA_1d.parquet"
    accepted = []                                                       # the refresh has accepted the split
    revealed = cut
    for _ in range(14):
        revealed = min(len(days), revealed + rng.randrange(0, 4))
        now = datetime.combine(days[revealed - 1].date(), datetime.min.time(), tzinfo=timezone.utc).replace(hour=23)
        before = _digest(path)
        step = rng.choice(["topup", "refresh"])
        if step == "refresh" and not accepted and revealed - 1 >= days.index(split_day):
            live_state.rebase_symbol("AAA", [(split_day.date().isoformat(), 2.0, "split")], now=now)
            accepted = [(split_day.date().isoformat(), 2.0)]
            assert live_state.manifest_events("AAA") == [(split_day.date().isoformat(), 2.0, "split")]
        elif step == "topup":
            cur = pd.read_parquet(path)
            try:
                merged, rep = topup_frame(cur, fetcher(raw), accepted, [], interval="1d", now_utc=now, symbol="AAA")
            except PendingCorporateAction:
                assert not accepted and revealed - 1 >= days.index(split_day)      # only an UNKNOWN split blocks
                assert _digest(path) == before                                      # a refused step touches nothing
                continue
            if rep["appended"]:
                native_cache.write_timeseries(PROVIDER, "AAA", "1d", merged)
        # invariant: the file is exactly the build of the revealed history on the accepted basis
        got = pd.read_parquet(path)
        n = len(got)
        assert_same(got, whole_build(raw.iloc[:n], accepted))

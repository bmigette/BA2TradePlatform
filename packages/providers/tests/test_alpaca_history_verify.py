"""verify.py: the data-quality gate.  Synthetic violations must FAIL, clean data must PASS."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ba2_providers.ohlcv.alpaca_history import verify as ver

SESS = [d for d in ver.nyse_sessions().astype("datetime64[D]") if np.datetime64("2024-01-02") <= d <= np.datetime64("2024-03-28")]
DAYS = [pd.Timestamp(d) for d in SESS]


def daily(days, close, n=None):
    closes = close if hasattr(close, "__len__") else [close] * len(days)
    df = pd.DataFrame({"Date": list(days), "Open": closes, "High": [c + 1 for c in closes], "Low": [c - 1 for c in closes],
                       "Close": closes, "Volume": [1000] * len(days)})
    df["Volume"] = df["Volume"].astype("int64")
    df["effective_date"] = df["Date"]
    return df


def five(days, close, last_close=None):
    rows = []
    for d in days:
        starts = pd.date_range(d + pd.Timedelta(hours=9, minutes=30), d + pd.Timedelta(hours=15, minutes=55), freq="5min")
        for i, s in enumerate(starts):
            c = last_close if (last_close is not None and i == len(starts) - 1) else close
            rows.append({"Date": s, "Open": c, "High": c + 0.1, "Low": c - 0.1, "Close": c, "Volume": 100.0})
    df = pd.DataFrame(rows)
    df["effective_date"] = df["Date"]
    return df[ver.COLUMNS]


def put(root, sym, d1=None, d5=None):
    root.mkdir(parents=True, exist_ok=True)
    if d1 is not None:
        d1.to_parquet(root / f"{sym}_1d.parquet", index=False)
    if d5 is not None:
        d5.to_parquet(root / f"{sym}_5min.parquet", index=False)


def gate(cache, tmp_path, **kw):
    kw.setdefault("years", (2024,))
    kw.setdefault("workers", 1)
    return ver.run_gate(str(cache), out_dir=str(tmp_path / "verify"), **kw)


@pytest.fixture
def clean(tmp_path):
    cache = tmp_path / "cache"
    put(cache, "AAA", daily(DAYS, 10.0), five(DAYS, 10.0))
    return cache


def status(res):
    return {k: v["status"] for k, v in res.items()}


def test_clean_cache_passes_every_check(clean, tmp_path):
    other = tmp_path / "other"
    put(other, "AAA", daily(DAYS, 10.0))
    res = gate(clean, tmp_path, other=str(other), panel=["AAA"])
    assert status(res) == {"structural": "PASS", "coverage": "PASS", "selfcons": "PASS", "residual_jumps": "PASS", "vendor_ratio": "INFO",
                           "readiness": "INFO", "yahoo": "INFO", "hl_envelope": "INFO"}
    assert res["coverage"]["summary"]["panel_coverage"] == 1.0 and ver.gate_failed(res) == []
    for name in ("structural", "coverage_symbol_year", "coverage_by_year", "selfcons", "residual_jumps", "vendor_ratio_steps", "readiness"):
        assert (tmp_path / "verify" / f"{name}.csv").is_file()


# ---------------------------------------------------------------------------
# 1 structural
# ---------------------------------------------------------------------------
def _structural(tmp_path, d1, d5, breaks=None):
    cache = tmp_path / "c"
    put(cache, "AAA", d1, d5)
    return gate(cache, tmp_path, breaks=breaks)["structural"]


def issues(res):
    return set(pd.read_csv(res["csv"], keep_default_na=False)["issue"])


def mutate(df, fn):
    df = df.copy()
    fn(df)
    return df


def test_structural_violations_fail(tmp_path):
    d1, d5 = daily(DAYS, 10.0), five(DAYS, 10.0)
    cases = {
        "duplicate_timestamp": (mutate(d1, lambda d: d.__setitem__("Date", d["Date"].where(d.index != 3, d["Date"].iloc[2]))), d5),
        "low_above_open_close": (mutate(d1, lambda d: d.__setitem__("Low", d["Low"].where(d.index != 5, 10.5))), d5),
        "high_below_open_close": (mutate(d1, lambda d: d.__setitem__("High", d["High"].where(d.index != 5, 9.5))), d5),
        "price_not_positive": (mutate(d1, lambda d: d.__setitem__("Close", d["Close"].where(d.index != 5, -1.0))), d5),
        "volume_negative": (mutate(d1, lambda d: d.__setitem__("Volume", d["Volume"].where(d.index != 5, -5))), d5),
        "bar_outside_0930_1555": (d1, mutate(d5, lambda d: d.__setitem__("Date", d["Date"].where(d.index != 0, d["Date"].iloc[0] - pd.Timedelta(minutes=5))))),
        "not_an_nyse_session": (mutate(d1, lambda d: d.__setitem__("Date", d["Date"].where(d.index != 5, pd.Timestamp("2024-01-13")))), d5),
        "daily_not_midnight": (mutate(d1, lambda d: d.__setitem__("Date", d["Date"] + pd.Timedelta(hours=1))), d5),
        "nan": (mutate(d1, lambda d: d.__setitem__("Open", d["Open"].where(d.index != 5, np.nan))), d5),
        "columns": (d1.drop(columns=["effective_date"]), d5),
    }
    for name, (a, b) in cases.items():
        res = _structural(tmp_path, a, b)
        assert res["status"] == "FAIL", name
        assert name in issues(res), (name, issues(res))


def test_bar_before_the_last_break_fails(tmp_path):
    res = _structural(tmp_path, daily(DAYS, 10.0), five(DAYS, 10.0), breaks={"AAA": "2024-02-01"})
    assert res["status"] == "FAIL" and "bar_before_series_break" in issues(res)
    late = [d for d in DAYS if d >= pd.Timestamp("2024-02-01")]
    ok = _structural(tmp_path / "ok", daily(late, 10.0), five(late, 10.0), breaks={"AAA": "2024-02-01"})
    assert ok["status"] == "PASS"


def test_unreadable_symbol_fails_the_structural_check(tmp_path):
    cache = tmp_path / "c"
    cache.mkdir()
    (cache / "AAA_1d.parquet").write_bytes(b"not parquet")
    put(cache, "BBB", daily(DAYS, 10.0), five(DAYS, 10.0))
    res = gate(cache, tmp_path)
    assert res["structural"]["status"] == "FAIL" and res["structural"]["summary"]["unreadable_symbols"] == 1


# ---------------------------------------------------------------------------
# 2 coverage, 3 self-consistency
# ---------------------------------------------------------------------------
def test_low_coverage_fails_and_is_judged_on_the_panel(tmp_path):
    cache = tmp_path / "c"
    put(cache, "AAA", daily(DAYS, 10.0), five(DAYS[: len(DAYS) // 2], 10.0))          # half the sessions have no 5-minute bar
    put(cache, "BBB", daily(DAYS, 10.0), five(DAYS, 10.0))
    res = gate(cache, tmp_path, panel=["AAA", "BBB"])
    assert res["coverage"]["status"] == "FAIL" and 0.7 < res["coverage"]["summary"]["panel_coverage"] < 0.8
    res = gate(cache, tmp_path, panel=["BBB"])                                         # AAA is not a panel symbol
    assert res["coverage"]["status"] == "PASS"
    by_year = pd.read_csv(tmp_path / "verify" / "coverage_by_year.csv")
    assert by_year["year"].tolist() == [2024] and by_year["coverage"].iloc[0] == 1.0


def test_selfconsistency_fails_when_the_last_close_is_off(tmp_path):
    cache = tmp_path / "c"
    put(cache, "AAA", daily(DAYS, 10.0), five(DAYS, 10.0, last_close=10.5))            # 5 % off on every day
    res = gate(cache, tmp_path, panel=["AAA"])
    assert res["selfcons"]["status"] == "FAIL" and res["selfcons"]["summary"]["gated_bad_share_top_two_terciles"] == 1.0
    assert res["selfcons"]["summary"]["worst"][0]["symbol"] == "AAA"
    assert gate(cache, tmp_path, panel=["AAA"], max_selfcons_bad=1.0)["selfcons"]["status"] == "PASS"


def test_selfconsistency_tolerates_a_one_percent_wobble(tmp_path):
    cache = tmp_path / "c"
    put(cache, "AAA", daily(DAYS, 10.0), five(DAYS, 10.0, last_close=10.1))
    assert gate(cache, tmp_path)["selfcons"]["status"] == "PASS"


# ---------------------------------------------------------------------------
# 4 residual discontinuities
# ---------------------------------------------------------------------------
def jump_series(day_index=30, factor=0.4):
    closes = [10.0 if i < day_index else 10.0 * factor for i in range(len(DAYS))]
    return closes


def test_real_market_jump_seen_by_the_other_vendor_is_flat_and_passes(tmp_path):
    closes = jump_series()
    cache, other = tmp_path / "c", tmp_path / "o"
    put(cache, "AAA", daily(DAYS, closes), five(DAYS, 10.0))
    put(other, "AAA", daily(DAYS, closes))                                                # same move: a real gap
    res = gate(cache, tmp_path, other=str(other), panel=["AAA"])
    jp = pd.read_csv(tmp_path / "verify" / "residual_jumps.csv")
    assert len(jp) == 1 and jp["class"].iloc[0] == "flat" and res["residual_jumps"]["status"] == "PASS"


def test_unadjusted_split_the_other_vendor_adjusted_is_a_step_and_fails(tmp_path):
    cache, other = tmp_path / "c", tmp_path / "o"
    put(cache, "AAA", daily(DAYS, jump_series()), five(DAYS, 10.0))
    put(other, "AAA", daily(DAYS, [4.0] * len(DAYS)))                                     # the other series has no jump
    res = gate(cache, tmp_path, other=str(other), panel=["AAA"])
    jp = pd.read_csv(tmp_path / "verify" / "residual_jumps.csv")
    assert jp["class"].tolist() == ["step"] and res["residual_jumps"]["status"] == "FAIL"
    assert res["residual_jumps"]["summary"]["disagreements"][0]["symbol"] == "AAA"


def test_residual_jump_exceptions_do_not_fail(tmp_path):
    cache, other = tmp_path / "c", tmp_path / "o"
    put(cache, "AAA", daily(DAYS, jump_series()), five(DAYS, 10.0))
    put(other, "AAA", daily(DAYS, [4.0] * len(DAYS)))
    assert gate(cache, tmp_path / "a", other=str(other), panel=["ZZZ"])["residual_jumps"]["status"] == "PASS"      # not a panel symbol
    assert gate(cache, tmp_path / "b")["residual_jumps"]["status"] == "PASS"                                       # no other vendor: no_other
    assert pd.read_csv(tmp_path / "b" / "verify" / "residual_jumps.csv")["class"].tolist() == ["unverifiable"]
    old = [pd.Timestamp(d) for d in ver.nyse_sessions().astype("datetime64[D]") if np.datetime64("2018-03-01") <= d <= np.datetime64("2018-04-30")]
    c2, o2 = tmp_path / "c2", tmp_path / "o2"
    put(c2, "AAA", daily(old, [10.0] * 20 + [4.0] * (len(old) - 20)), five(old, 10.0))
    put(o2, "AAA", daily(old, [4.0] * len(old)))
    assert gate(c2, tmp_path / "d", other=str(o2), panel=["AAA"], years=(2018,))["residual_jumps"]["status"] == "PASS"     # before 2019


# ---------------------------------------------------------------------------
# 5 vendor ratio, 7 readiness
# ---------------------------------------------------------------------------
def test_vendor_ratio_steps_are_informational_and_counted(tmp_path):
    cache, other = tmp_path / "c", tmp_path / "o"
    put(cache, "AAA", daily(DAYS, 10.0), five(DAYS, 10.0))
    put(other, "AAA", daily(DAYS, [10.0] * 30 + [11.0] * (len(DAYS) - 30)))               # FMP steps +10 % at day 30
    put(cache, "BBB", daily(DAYS, 10.0), five(DAYS, 10.0))
    put(other, "BBB", daily(DAYS, 10.0))
    res = gate(cache, tmp_path, other=str(other), panel=["AAA"])
    r = res["vendor_ratio"]
    assert r["status"] == "INFO" and r["summary"]["symbols_with_steps"] == 1
    assert r["summary"]["top50_panel"] == [{"symbol": "AAA", "steps": 1}]
    assert r["summary"]["by_year_panel"] == {"2024/panel": 1}
    steps = pd.read_csv(tmp_path / "verify" / "vendor_ratio_steps.csv")
    assert steps["date"].tolist() == [str(DAYS[30].date())]


def test_readiness_lists_panel_symbols_without_files(tmp_path):
    cache = tmp_path / "c"
    put(cache, "AAA", daily(DAYS, 10.0), five(DAYS, 10.0))
    put(cache, "DAILYONLY", daily(DAYS, 10.0))
    res = gate(cache, tmp_path, panel=["AAA", "DAILYONLY", "MISSING"], symbols=None)
    rd = pd.read_csv(tmp_path / "verify" / "readiness.csv")
    got = {(r.symbol, bool(r.missing_daily), bool(r.missing_5min)) for r in rd.itertuples()}
    assert got == {("DAILYONLY", False, True), ("MISSING", True, True)} and res["readiness"]["status"] == "INFO"
    assert res["readiness"]["summary"][2024] == {"missing_daily": 1, "missing_5min": 2}


# ---------------------------------------------------------------------------
# 6 Yahoo
# ---------------------------------------------------------------------------
def yahoo_for(frames, calls=None, successors=None, tmp=None, refresh=False):
    def fetch(t):
        if calls is not None:
            calls.append(t)
        f = frames.get(t)
        return None if f is None else pd.DataFrame({"day": f["Date"], "Close": f["Close"]})
    return ver.YahooDaily(str(tmp / "yd"), fetch=fetch, sleep=lambda s: None, refresh=refresh,
                          successors=successors or (lambda s: []))


def test_yahoo_match_passes_and_is_cached_offline(tmp_path):
    cache = tmp_path / "c"
    put(cache, "AAA", daily(DAYS, 10.0), five(DAYS, 10.0))
    calls = []
    y = yahoo_for({"AAA": daily(DAYS, 10.0)}, calls, tmp=tmp_path)
    res = gate(cache, tmp_path, yahoo=y, symbols=["AAA"])
    assert res["yahoo"]["status"] == "PASS" and res["yahoo"]["summary"]["match_share"] == 1.0 and calls == ["AAA"]
    gate(cache, tmp_path, yahoo=yahoo_for({}, calls, tmp=tmp_path), symbols=["AAA"])        # offline: nothing fetched, cache read
    assert calls == ["AAA"]
    gate(cache, tmp_path, yahoo=yahoo_for({"AAA": daily(DAYS, 10.0)}, calls, tmp=tmp_path, refresh=True), symbols=["AAA"])
    assert calls == ["AAA", "AAA"]


def test_yahoo_sustained_split_error_fails_and_is_listed_with_its_ratio(tmp_path):
    cache = tmp_path / "c"
    put(cache, "AAA", daily(DAYS, 10.0), five(DAYS, 10.0))
    put(cache, "BBB", daily(DAYS, 10.0), five(DAYS, 10.0))
    bad = [20.0] * 30 + [10.0] * (len(DAYS) - 30)                                             # Yahoo is 2x for 30 days: a split error here
    y = yahoo_for({"AAA": daily(DAYS, 10.0), "BBB": daily(DAYS, bad)}, tmp=tmp_path)
    res = gate(cache, tmp_path, yahoo=y, symbols=["AAA", "BBB"])
    assert res["yahoo"]["status"] == "FAIL" and res["yahoo"]["summary"]["sustained_mismatch_symbols"] == ["BBB"]
    runs = pd.read_csv(tmp_path / "verify" / "yahoo_mismatch_runs.csv")
    assert runs["symbol"].tolist() == ["BBB"] and runs["days"].iloc[0] == 30 and runs["median_ratio"].iloc[0] == pytest.approx(0.5)


def test_yahoo_match_share_below_99_percent_fails_without_a_sustained_run(tmp_path):
    cache = tmp_path / "c"
    put(cache, "AAA", daily(DAYS, 10.0), five(DAYS, 10.0))
    scattered = [10.0 if i % 5 else 11.0 for i in range(len(DAYS))]                           # every 5th day 10 % off, never consecutive
    res = gate(cache, tmp_path, yahoo=yahoo_for({"AAA": daily(DAYS, scattered)}, tmp=tmp_path), symbols=["AAA"])
    assert res["yahoo"]["status"] == "FAIL" and res["yahoo"]["summary"]["sustained_mismatch_symbols"] == []


def test_yahoo_renamed_ticker_falls_back_to_the_successor(tmp_path):
    cache = tmp_path / "c"
    put(cache, "OLD", daily(DAYS, 10.0), five(DAYS, 10.0))
    y = yahoo_for({"NEW": daily(DAYS, 10.0)}, tmp=tmp_path, successors=lambda s: ["NEW"])
    res = gate(cache, tmp_path, yahoo=y, symbols=["OLD"])
    assert res["yahoo"]["status"] == "PASS"
    assert pd.read_csv(tmp_path / "verify" / "yahoo_symbols.csv")["yahoo_ticker"].tolist() == ["NEW"]


def test_yahoo_symbol_it_does_not_know_is_listed_not_hidden(tmp_path):
    cache = tmp_path / "c"
    put(cache, "AAA", daily(DAYS, 10.0), five(DAYS, 10.0))
    res = gate(cache, tmp_path, yahoo=yahoo_for({}, tmp=tmp_path), symbols=["AAA"])
    assert res["yahoo"]["summary"]["yahoo_unavailable"] == 1 and (tmp_path / "verify" / "yahoo_unavailable.csv").is_file()
    assert res["yahoo"]["status"] == "FAIL"                                                    # nothing compared: not a pass


def test_yahoo_sample_is_seeded_and_includes_the_named_symbols():
    avail = [f"S{i:03d}" for i in range(300)] + ["AAPL", "TSLA"]
    a = ver.choose_yahoo_sample(avail, set(avail), None, 50, 7)
    assert a == ver.choose_yahoo_sample(avail, set(avail), None, 50, 7) and a != ver.choose_yahoo_sample(avail, set(avail), None, 50, 8)
    assert {"AAPL", "TSLA"} <= set(a) and len(a) >= 50
    assert ver.choose_yahoo_sample(avail, None, ["S001", "NOPE"]) == ["S001"]


# ---------------------------------------------------------------------------
# owner refinements
# ---------------------------------------------------------------------------
def test_flat_bars_and_missing_intervals_are_normal(tmp_path):
    cache = tmp_path / "c"
    d5 = five(DAYS, 10.0)
    d5 = d5[d5.index % 7 != 3].reset_index(drop=True)                    # missing 5-minute intervals
    d1 = daily(DAYS, 10.0)
    for c in ("Open", "High", "Low"):
        d1[c] = 10.0                                                      # flat bars: O = H = L = C
        d5[c] = 10.0
    put(cache, "AAA", d1, d5)
    res = gate(cache, tmp_path)
    assert res["structural"]["status"] == "PASS"


def test_selfconsistency_threshold_applies_to_the_top_two_liquidity_terciles_only(tmp_path):
    cache = tmp_path / "c"
    for sym, vol_scale in (("LOW", 1), ("MID", 100), ("HIGH", 10000)):
        d1 = daily(DAYS, 10.0)
        d1["Volume"] = (d1["Volume"] * vol_scale).astype("int64")
        put(cache, sym, d1, five(DAYS, 10.0, last_close=10.5 if sym == "LOW" else None))
    res = gate(cache, tmp_path, panel=["LOW", "MID", "HIGH"])
    s = res["selfcons"]["summary"]
    assert s["by_band"]["low"]["bad_share"] == 1.0 and s["by_band"]["mid"]["bad_share"] == 0.0 and s["by_band"]["high"]["bad_share"] == 0.0
    assert res["selfcons"]["status"] == "PASS" and s["gated_bad_share_top_two_terciles"] == 0.0 and s["panel_bad_share_all"] > 0.3
    assert (tmp_path / "verify" / "selfcons_by_band.csv").is_file()
    put(cache, "HIGH", mutate_vol(daily(DAYS, 10.0), 10000), five(DAYS, 10.0, last_close=10.5))      # a liquid name off: FAIL
    assert gate(cache, tmp_path / "x", panel=["LOW", "MID", "HIGH"])["selfcons"]["status"] == "FAIL"


def mutate_vol(df, k):
    df["Volume"] = (df["Volume"] * k).astype("int64")
    return df


def test_residual_jump_with_no_other_data_is_unverifiable_and_listed_apart(tmp_path):
    cache, other = tmp_path / "c", tmp_path / "o"
    put(cache, "AAA", daily(DAYS, jump_series()), five(DAYS, 10.0))
    put(other, "AAA", daily(DAYS[:10], 4.0))                                 # the other vendor stops before the jump
    res = gate(cache, tmp_path, other=str(other), panel=["AAA"])
    r = res["residual_jumps"]
    assert r["status"] == "PASS" and r["summary"]["by_class"] == {"unverifiable": 1}
    assert r["summary"]["unverifiable_panel_since_2019"][0]["symbol"] == "AAA"


def _yahoo_gate(tmp_path, ours, theirs, spinoffs=None, breaks=None):
    cache = tmp_path / "c"
    put(cache, "AAA", daily(DAYS, ours), five(DAYS, 10.0))
    y = yahoo_for({"AAA": daily(DAYS, theirs)}, tmp=tmp_path)
    return gate(cache, tmp_path, yahoo=y, symbols=["AAA"], spinoffs=spinoffs, breaks=breaks)["yahoo"]


def test_yahoo_spinoff_convention_run_is_expected_not_a_failure(tmp_path):
    cut = 40
    ours = [10.0 / 1.25] * cut + [10.0] * (len(DAYS) - cut)                  # we divided the prices before the spin-off by 1.25
    theirs = [10.0] * len(DAYS)                                              # Yahoo did not
    y = _yahoo_gate(tmp_path, ours, theirs, spinoffs={"AAA": [(str(DAYS[cut].date()), 1.25)]})
    assert y["status"] == "PASS" and y["summary"]["runs_by_class"] == {"spinoff_convention": 1, "series_break": 0, "convention": 0, "yahoo_missing_event": 0,
                                                              "successor_later_event": 0, "unexplained": 0}
    runs = pd.read_csv(tmp_path / "verify" / "yahoo_mismatch_runs.csv")
    assert runs["classification"].tolist() == ["spinoff_convention"] and runs["days"].iloc[0] == cut
    # the same mismatch with a factor we did not apply is unexplained and fails
    y = _yahoo_gate(tmp_path / "n", ours, theirs, spinoffs={"AAA": [(str(DAYS[cut].date()), 2.0)]})
    assert y["status"] == "FAIL" and y["summary"]["runs_by_class"]["unexplained"] == 1
    y = _yahoo_gate(tmp_path / "m", ours, theirs)                              # no spin-off at all
    assert y["status"] == "FAIL"


def test_yahoo_run_at_a_series_break_is_classified_apart(tmp_path):
    cut = 40
    ours = [5.0] * cut + [10.0] * (len(DAYS) - cut)
    theirs = [10.0] * len(DAYS)
    y = _yahoo_gate(tmp_path, ours, theirs, breaks={"AAA": str(DAYS[cut].date())})
    assert y["status"] == "PASS" and y["summary"]["runs_by_class"]["series_break"] == 1


# ---------------------------------------------------------------------------
# Yahoo as the tie-breaker; convention; provenance
# ---------------------------------------------------------------------------
def _tie(tmp_path, yahoo_closes):
    """ours: a real 60 % crash at day 30; the other vendor shows NO crash (it invented a split)."""
    cache, other = tmp_path / "c", tmp_path / "o"
    put(cache, "AAA", daily(DAYS, jump_series()), five(DAYS, 10.0))
    put(other, "AAA", daily(DAYS, [4.0] * len(DAYS)))
    frames = {} if yahoo_closes is None else {"AAA": daily(DAYS, yahoo_closes)}
    y = yahoo_for(frames, tmp=tmp_path)
    res = gate(cache, tmp_path, other=str(other), panel=["AAA"], yahoo=y, symbols=["AAA"])
    jp = pd.read_csv(tmp_path / "verify" / "residual_jumps.csv", keep_default_na=False)
    return res["residual_jumps"], jp["resolution"].tolist()


def test_yahoo_agreeing_with_ours_makes_the_jump_a_vendor_error_and_passes(tmp_path):
    r, resolution = _tie(tmp_path, jump_series())
    assert resolution == ["vendor_error_fmp"] and r["status"] == "PASS" and r["summary"]["blocking"] == 0
    assert r["summary"]["vendor_error_fmp"][0]["symbol"] == "AAA"


def test_yahoo_agreeing_with_the_other_vendor_makes_ours_suspect_and_blocks(tmp_path):
    r, resolution = _tie(tmp_path / "a", [4.0] * len(DAYS))
    assert resolution == ["ours_suspect"] and r["status"] == "FAIL"


def test_yahoo_unavailable_leaves_the_jump_unresolved_and_blocks(tmp_path):
    r, resolution = _tie(tmp_path / "b", None)
    assert resolution == ["unresolved"] and r["status"] == "FAIL"
    r, resolution = _tie(tmp_path / "c", jump_series(factor=0.7))                  # Yahoo agrees with neither
    assert resolution == ["unresolved"] and r["status"] == "FAIL"


def test_spinoff_factor_within_ten_percent_of_yahoos_is_a_convention(tmp_path):
    cut = 40
    theirs = [10.0] * len(DAYS)
    ours = [10.0 / 6.43 * 1.036] * cut + [10.0] * (len(DAYS) - cut)           # we adjusted by 6.43, Yahoo by 6.66 (3.6 % apart)
    y = _yahoo_gate(tmp_path, ours, [10.0 / 6.66 * 1.0] * cut + theirs[cut:], spinoffs={"AAA": [(str(DAYS[cut].date()), 6.43)]})
    assert y["status"] == "PASS" and y["summary"]["runs_by_class"]["convention"] == 1 and y["summary"]["runs_by_class"]["unexplained"] == 0
    far = _yahoo_gate(tmp_path / "far", [10.0 / 6.43] * cut + [10.0] * (len(DAYS) - cut), [10.0 / 3.0] * cut + theirs[cut:],
                      spinoffs={"AAA": [(str(DAYS[cut].date()), 6.43)]})        # 2x apart: not a convention
    assert far["status"] == "FAIL" and far["summary"]["runs_by_class"]["unexplained"] == 1


def test_yahoo_inputs_csv_records_what_was_used(tmp_path):
    cache = tmp_path / "c"
    put(cache, "AAA", daily(DAYS, 10.0), five(DAYS, 10.0))
    gate(cache, tmp_path, yahoo=yahoo_for({"AAA": daily(DAYS, 10.0)}, tmp=tmp_path), symbols=["AAA"])
    inp = pd.read_csv(tmp_path / "verify" / "yahoo_inputs.csv", keep_default_na=False)
    row = inp.iloc[0]
    assert row["symbol"] == "AAA" and row["status"] == "ok" and row["rows"] == len(DAYS) and len(row["sha256"]) == 64
    assert row["first_day"] == str(DAYS[0].date()) and row["fetched_at"].endswith("Z")
    import hashlib
    assert row["sha256"] == hashlib.sha256((tmp_path / "yd" / "AAA.parquet").read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# yahoo_missing_event / successor_later_event
# ---------------------------------------------------------------------------
def _yahoo_frames(frames, splits=None):
    out = {}
    for t, closes in frames.items():
        f = pd.DataFrame({"day": DAYS, "Close": closes})
        f["Splits"] = 0.0
        for d, v in (splits or {}).get(t, []):
            f.loc[f["day"] == pd.Timestamp(d), "Splits"] = v
        out[t] = f
    return out


def _event_gate(tmp_path, ours, yahoo_closes, events, yahoo_splits, yahoo_ticker=None, table_splits=None, sym="AAA", proven=True):
    cache = tmp_path / "c"
    put(cache, sym, daily(DAYS, ours), five(DAYS, 10.0))
    frames = _yahoo_frames({yahoo_ticker or sym: yahoo_closes}, {yahoo_ticker or sym: yahoo_splits})
    y = ver.YahooDaily(str(tmp_path / "yd"), fetch=lambda t: frames.get(t), sleep=lambda s: None,
                       successors=lambda s: [yahoo_ticker] if yahoo_ticker else [])
    table = {sym: events}
    table.update(table_splits or {})
    pv = {sym: {d for d, _ in events}} if proven else {}
    return gate(cache, tmp_path, yahoo=y, symbols=[sym], splits=table, proven_events=pv)["yahoo"], pd.read_csv(tmp_path / "verify" / "yahoo_mismatch_runs.csv",
                                                                                             keep_default_na=False)


def test_event_our_table_has_and_yahoo_lacks_is_yahoo_missing_event(tmp_path):
    cut = 40
    d = str(DAYS[cut].date())
    ours = [10.0 / 1.05] * cut + [10.0] * (len(DAYS) - cut)                  # we applied a 5 % stock dividend; Yahoo did not
    y, runs = _event_gate(tmp_path, ours, [10.0] * len(DAYS), [(d, 1.05)], yahoo_splits=[])
    assert y["status"] == "PASS" and y["summary"]["runs_by_class"]["yahoo_missing_event"] == 1
    assert runs["classification"].tolist() == ["yahoo_missing_event"] and runs["events"].iloc[0] == f"{d} x1.05"


def test_event_yahoo_lists_is_not_missing_and_a_wrong_ratio_is_unexplained(tmp_path):
    cut = 40
    d = str(DAYS[cut].date())
    ours = [10.0 / 1.05] * cut + [10.0] * (len(DAYS) - cut)
    y, runs = _event_gate(tmp_path / "a", ours, [10.0] * len(DAYS), [(d, 1.05)], yahoo_splits=[(d, 1.05)])      # Yahoo lists it: not "missing"
    assert y["status"] == "FAIL" and runs["classification"].tolist() == ["unexplained"]
    y, runs = _event_gate(tmp_path / "b", [10.0 / 1.2] * cut + [10.0] * (len(DAYS) - cut), [10.0] * len(DAYS), [(d, 1.05)], yahoo_splits=[])
    assert y["status"] == "FAIL" and runs["classification"].tolist() == ["unexplained"]                          # ratio is not 1/1.05
    ycache = _event_gate(tmp_path / "c", ours, [10.0] * len(DAYS), [(d, 1.05)], yahoo_splits=[])[0]
    assert ycache["status"] == "PASS"


def test_old_yahoo_cache_without_a_split_list_cannot_claim_a_missing_event(tmp_path):
    cut = 40
    d = str(DAYS[cut].date())
    ours = [10.0 / 1.05] * cut + [10.0] * (len(DAYS) - cut)
    cache = tmp_path / "c"
    put(cache, "AAA", daily(DAYS, ours), five(DAYS, 10.0))
    y = yahoo_for({"AAA": daily(DAYS, 10.0)}, tmp=tmp_path)                    # frames without a Splits column
    res = gate(cache, tmp_path, yahoo=y, symbols=["AAA"], splits={"AAA": [(d, 1.05)]}, proven_events={"AAA": {d}})
    assert res["yahoo"]["status"] == "FAIL"


def test_successor_series_carrying_a_later_reverse_split_is_successor_later_event(tmp_path):
    n = len(DAYS)
    cut = 60
    last_old = DAYS[cut - 1]
    # OLD is a pre-rename duplicate that stops at cut; Yahoo answered with NEW's series, adjusted for NEW's later 0.05 reverse split
    cache = tmp_path / "c"
    put(cache, "OLD", daily(DAYS[:cut], 2.0), five(DAYS[:cut], 2.0))
    frames = _yahoo_frames({"NEW": [40.0] * n})
    y = ver.YahooDaily(str(tmp_path / "yd"), fetch=lambda t: frames.get(t), sleep=lambda s: None, successors=lambda s: ["NEW"])
    later = str((last_old + pd.Timedelta(days=30)).date())
    res = gate(cache, tmp_path, yahoo=y, symbols=["OLD"], splits={"OLD": [], "NEW": [(later, 0.05), ("2020-01-02", 2.0)]})
    runs = pd.read_csv(tmp_path / "verify" / "yahoo_mismatch_runs.csv", keep_default_na=False)
    assert res["yahoo"]["status"] == "PASS" and runs["classification"].tolist() == ["successor_later_event"]
    assert runs["events"].iloc[0] == f"{later} x0.05"
    res = gate(cache, tmp_path / "x", yahoo=y, symbols=["OLD"], splits={"OLD": [], "NEW": [(later, 0.5)]})      # factor does not match: unexplained
    assert res["yahoo"]["status"] == "FAIL"


def test_unproven_event_never_whitewashes_a_mismatch_aiv_case(tmp_path):
    # AIV 2019-02-21: both vendors list 0.9697 but the raw jump and FMP's step are flat; Yahoo lacks it; the event is not independently proven
    cut = 40
    d = str(DAYS[cut].date())
    ours = [10.0 / 0.9697] * cut + [10.0] * (len(DAYS) - cut)
    y, runs = _event_gate(tmp_path, ours, [10.0] * len(DAYS), [(d, 0.9697)], yahoo_splits=[], proven=False)
    assert y["status"] == "FAIL" and runs["classification"].tolist() == ["unexplained"]
    y, runs = _event_gate(tmp_path / "p", ours, [10.0] * len(DAYS), [(d, 0.9697)], yahoo_splits=[], proven=True)
    assert runs["classification"].tolist() == ["yahoo_missing_event"]


def test_proven_events_are_read_from_the_accepted_csv(tmp_path):
    meta = tmp_path / "m"
    meta.mkdir()
    rows = ["symbol,date,kind,factor,source,verdict,jump,fmp_step,run_id",
            "AIV,2019-02-21,split,0.9697,both,small_both,1.0,1 flat,R",
            "CBSH,2022-12-01,split,1.05,both,small_both,1.0,1.05 equals_f,R",
            "XXX,2020-01-02,split,2,alpaca,proven_by_fmp_step,1.9,2.0 equals_f,R",
            "OVR,2020-01-02,split,2,override,override_accept,1.0,none,R",
            "OTH,2020-01-02,split,2,alpaca,proven,2.0,2.0 equals_f,R",
            "SPN,2020-01-02,spinoff,1.2,alpaca_spinoff,spinoff_fmp_agrees,1.0,1.2 equals_f,R"]
    (meta / "split_accepted.csv").write_text(chr(10).join(rows) + chr(10))
    got = ver.load_proven_events(str(meta))
    assert got == {"CBSH": {"2022-12-01"}, "XXX": {"2020-01-02"}, "OVR": {"2020-01-02"}}


# ---------------------------------------------------------------------------
# hl_envelope
# ---------------------------------------------------------------------------
def _hl(tmp_path, high=None, low=None, open_close=None, five_close=10.0, bad_5min_high=None, envelope=True, panel=("AAA",)):
    cache = tmp_path / "c"
    d1 = daily(DAYS, 10.0)
    d1["Open"] = d1["Close"] = 10.0
    d1["High"], d1["Low"] = 10.1, 9.9
    i = 20
    if high is not None:
        d1.loc[i, "High"] = high
    if low is not None:
        d1.loc[i, "Low"] = low
    if open_close is not None:
        d1.loc[i, ["Open", "Close"]] = open_close
        d1.loc[i, "High"] = max(d1.loc[i, "High"], open_close)
    d5 = five(DAYS, 10.0)                                       # 5-minute envelope per day: 9.9 .. 10.1
    if bad_5min_high is not None:
        d5.loc[d5["Date"] == DAYS[i] + pd.Timedelta(hours=10), "High"] = bad_5min_high
    put(cache, "AAA", d1, d5)
    if envelope:
        meta = cache / "_meta" / "envelope"
        meta.mkdir(parents=True)
        pd.DataFrame({"Date": DAYS, "High": 10.1, "Low": 9.9}).to_parquet(meta / "AAA.parquet", index=False)
    return gate(cache, tmp_path, panel=list(panel))["hl_envelope"], pd.read_csv(tmp_path / "verify" / "hl_envelope.csv", keep_default_na=False) if envelope else None


def test_hl_envelope_clean_passes(tmp_path):
    r, df = _hl(tmp_path)
    assert r["status"] == "PASS" and r["summary"]["blocking"] == 0 and len(df) == 0


def test_hl_envelope_unrepaired_outlier_beyond_five_percent_blocks_a_panel_symbol(tmp_path):
    r, df = _hl(tmp_path, high=12.0)                             # 12 vs 10.1 (bound 10.1): 18.8 %
    assert r["status"] == "FAIL" and df["class"].tolist() == ["outlier_gt5"] and r["summary"]["blocking"] == 1
    r, _ = _hl(tmp_path / "np", high=12.0, panel=("ZZZ",))        # not a panel symbol: informational
    assert r["status"] == "PASS" and r["summary"]["by_class_all"] == {"outlier_gt5": 1}
    r, df = _hl(tmp_path / "low", low=7.0)
    assert r["status"] == "FAIL" and df["field"].tolist() == ["Low"]


def test_hl_envelope_two_to_five_percent_is_informational(tmp_path):
    r, df = _hl(tmp_path, high=10.5)                             # 10.5 / 10.1 = 3.96 %
    assert r["status"] == "PASS" and df["class"].tolist() == ["outlier_2_5"]
    assert df["excess"].iloc[0] == pytest.approx(10.5 / 10.1 - 1) and r["summary"]["by_class_panel"] == {"outlier_2_5": 1}


def test_hl_envelope_close_on_the_boundary_is_informational_not_an_outlier(tmp_path):
    r, df = _hl(tmp_path, open_close=12.0)                        # Open = Close = High = 12: late bars / closing auction
    assert r["status"] == "PASS" and set(df["class"]) == {"boundary"}


def test_bad_5min_print_outside_the_daily_range_is_reported_not_blocking(tmp_path):
    r, _ = _hl(tmp_path, bad_5min_high=14.0)
    rv = pd.read_csv(tmp_path / "verify" / "hl_envelope_5min_prints.csv")
    assert r["status"] == "PASS" and r["summary"]["bad_5min_prints"] == 1 and rv["field"].tolist() == ["High"]


def test_hl_envelope_is_informational_when_the_cache_has_no_envelope(tmp_path):
    cache = tmp_path / "c"
    put(cache, "AAA", daily(DAYS, 10.0), five(DAYS, 10.0))
    assert gate(cache, tmp_path)["hl_envelope"]["status"] == "INFO"

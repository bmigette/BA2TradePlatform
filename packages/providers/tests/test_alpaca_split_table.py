"""The verified split table: every branch of split_table.decide_splits on synthetic RAW bars."""
import json
import os
from collections import Counter
from datetime import date, timedelta

import pandas as pd
import pytest

from ba2_providers.ohlcv.alpaca_history import split_build as sb, split_table as st
from ba2_providers.ohlcv.alpaca_history.split_table import ListedSplit as L

SESSIONS = list(pd.bdate_range("2021-01-04", periods=80))
D = SESSIONS[40].date()            # the event session in most tests


def bars(jump_at=None, jump=1.0, price=100.0, sessions=SESSIONS, tz=True):
    """Flat raw bars; from sessions[jump_at] on the open/close are price / jump (close(D-1)/open(D) == jump)."""
    rows = []
    for i, d in enumerate(sessions):
        p = price / jump if (jump_at is not None and i >= jump_at) else price
        t = pd.Timestamp(d).tz_localize("America/New_York").tz_convert("UTC") if tz else pd.Timestamp(d)   # 00:00 New York
        rows.append({"t": t, "o": p, "c": p})
    return pd.DataFrame(rows, columns=["t", "o", "c"])


def run(frames, alp=(), fmp=(), ov=()):
    stats = Counter()
    acc, rev = st.decide_splits(frames, list(alp), list(fmp), list(ov), stats)
    return acc, rev, stats


def one(rows, verdict):
    got = [r for r in rows if r["verdict"] == verdict]
    assert len(got) == 1, rows
    return got[0]


def test_proven_by_both_is_accepted_without_review_row():
    acc, rev, stats = run({"X": bars(40, 4.0)}, [L("X", D, 4.0)], [L("X", D, 4.0)])
    assert [(a["symbol"], a["date"], a["factor"], a["verdict"]) for a in acc] == [("X", D.isoformat(), 4.0, "proven")]
    assert rev == [] and stats["verdict_proven"] == 1


def test_alpaca_only_proven_accepted_and_reverse_split():
    acc, rev, _ = run({"X": bars(40, 0.1)}, [L("X", D, 0.1)])
    assert [a["factor"] for a in acc] == [0.1] and rev == []


def test_proven_within_tolerance_but_not_beyond():
    acc, _, _ = run({"X": bars(40, 4.0 * 1.14)}, [L("X", D, 4.0)])
    assert len(acc) == 1
    acc, rev, _ = run({"X": bars(40, 4.0 * 1.2)}, [L("X", D, 4.0)])
    assert acc == [] and one(rev, "unresolved")["applied"] is False


def test_fmp_only_proven_accepted_with_review_row():
    acc, rev, stats = run({"X": bars(40, 3.0)}, [], [L("X", D, 3.0)])
    assert len(acc) == 1 and acc[0]["source"] == "fmp"
    r = one(rev, "fmp_only_proven")
    assert r["applied"] is True and r["fmp_ratio"] == 3.0 and r["alpaca_ratio"] is None
    assert stats["verdict_fmp_only_proven"] == 1


def test_alpaca_only_contradicted_is_rejected_with_review_row():
    acc, rev, _ = run({"X": bars(40, 1.02)}, [L("X", D, 2.0)])
    assert acc == []
    r = one(rev, "alpaca_contradicted")
    assert r["applied"] is False and r["jump"] == pytest.approx(1.02, rel=1e-4) and r["alpaca_ratio"] == 2.0


def test_fmp_only_and_both_contradicted():
    _, rev, _ = run({"X": bars()}, [], [L("X", D, 2.0)])
    one(rev, "fmp_contradicted")
    acc, rev, _ = run({"X": bars()}, [L("X", D, 2.0)], [L("X", D, 2.0)])
    assert acc == [] and one(rev, "both_contradicted")["applied"] is False


def test_big_event_between_proven_and_contradicted_is_unresolved():
    acc, rev, _ = run({"X": bars(40, 1.25)}, [L("X", D, 2.0)])
    assert acc == [] and one(rev, "unresolved")["jump"] == pytest.approx(1.25, rel=1e-4)


def test_small_event_needs_both_vendors():
    f = {"X": bars()}
    acc, rev, _ = run(f, [L("X", D, 1.05)], [L("X", D, 1.0505)])
    assert [a["verdict"] for a in acc] == ["small_both"] and rev == []
    acc, rev, _ = run(f, [L("X", D, 1.05)])
    assert acc == [] and one(rev, "small_single_vendor")["applied"] is False
    acc, rev, _ = run(f, [], [L("X", D, 1.05)])
    assert acc == [] and one(rev, "small_single_vendor")["fmp_ratio"] == 1.05
    acc, rev, _ = run(f, [L("X", D, 1.05)], [L("X", D, 1.10)])
    assert acc == [] and one(rev, "ratio_mismatch")["applied"] is False


def test_small_event_is_not_proven_by_a_matching_jump():
    acc, rev, _ = run({"X": bars(40, 1.05)}, [L("X", D, 1.05)])
    assert acc == [] and one(rev, "small_single_vendor")


def test_unexplained_overnight_jump_gets_a_review_row():
    acc, rev, _ = run({"X": bars(40, 2.5)})
    assert acc == []
    r = one(rev, "unexplained_jump")
    assert r["event_date"] == SESSIONS[40].date().isoformat() and r["applied"] is False
    assert r["jump"] == pytest.approx(2.5, rel=1e-4)
    _, rev, _ = run({"X": bars(40, 0.5)})
    one(rev, "unexplained_jump")                               # a collapse is as suspicious as a spike
    _, rev, _ = run({"X": bars(40, 1.7)})
    assert rev == []                                           # inside the bounds


def test_listed_event_within_three_sessions_explains_the_jump():
    near = SESSIONS[43].date()
    acc, rev, _ = run({"X": bars(40, 2.5)}, [L("X", near, 2.5)])
    assert [a["date"] for a in acc] == [D.isoformat()] and rev == []      # accepted at the session that jumped
    far = SESSIONS[45].date()                                  # 5 sessions away: jump stays unexplained
    _, rev, _ = run({"X": bars(40, 2.5)}, [L("X", far, 2.5)])
    assert any(r["verdict"] == "unexplained_jump" for r in rev)


def test_vendors_one_day_apart_are_one_event_and_the_proving_date_is_kept():
    after = SESSIONS[41].date()
    acc, rev, _ = run({"X": bars(40, 4.0)}, [L("X", after, 4.0)], [L("X", D, 4.0)])
    assert len(acc) == 1 and acc[0]["date"] == D.isoformat() and rev == []


def test_both_vendors_with_different_ratios():
    acc, rev, stats = run({"X": bars(40, 4.0)}, [L("X", D, 2.0)], [L("X", D, 4.0)])
    assert [(a["factor"], a["source"]) for a in acc] == [(4.0, "fmp")]
    r = one(rev, "ratio_mismatch_resolved")
    assert r["applied"] is True and stats["verdict_ratio_mismatch_resolved"] == 1
    acc, rev, _ = run({"X": bars(40, 4.0)}, [L("X", D, 2.0)], [L("X", D, 8.0)])
    assert acc == [] and one(rev, "ratio_mismatch")["applied"] is False
    acc, rev, _ = run({"X": bars(40, 4.0)}, [L("X", D, 3.8)], [L("X", D, 4.2)])     # both within 15 %: ambiguous
    assert acc == [] and "both" in one(rev, "ratio_mismatch")["reason"]


def test_duplicate_listings_collapse_to_one_event():
    acc, rev, stats = run({"X": bars(40, 4.0)}, [L("X", D, 4.0), L("X", SESSIONS[42].date(), 4.002)])
    assert len(acc) == 1 and rev == [] and stats["dup_listings_collapsed"] == 1


def test_cluster_with_several_ratios_is_decided_event_by_event():
    # AMC 2023-08: a 1.13333 distribution and the 1:10 reverse split within days of each other
    f = {"X": bars(40, 0.1)}
    acc, rev, _ = run(f, [L("X", SESSIONS[39].date(), 1.13333), L("X", D, 0.1)], [L("X", D, 0.1)])
    assert [(a["factor"], a["verdict"]) for a in acc] == [(0.1, "proven")]
    r = one(rev, "small_single_vendor")
    assert r["alpaca_ratio"] == 1.13333 and r["applied"] is False


def test_two_slightly_different_proven_ratios_are_not_applied_twice():
    # DOMH: FMP lists 0.25 and 0.235, Alpaca 0.2353, all for one jump
    f = {"X": bars(40, 0.266)}
    acc, rev, stats = run(f, [L("X", D, 0.2353)], [L("X", D, 0.25), L("X", D, 0.235)])
    assert acc == []
    assert {r["verdict"] for r in rev} == {"overlapping_accepts"} and len(rev) == 2
    assert stats["verdict_proven"] == 0


def test_no_bars_on_either_side_of_the_event():
    sess = SESSIONS[:30] + SESSIONS[70:]                       # a 3-month hole
    mid = SESSIONS[50].date()
    acc, rev, _ = run({"X": bars(sessions=sess)}, [L("X", mid, 2.0)])
    assert acc == [] and one(rev, "no_price_data")["applied"] is False
    acc, rev, _ = run({"X": bars(sessions=[])}, [L("X", D, 2.0)])
    assert acc == [] and one(rev, "no_price_data")


def test_events_outside_the_bar_range_are_counted_not_reviewed():
    f = {"X": bars()}
    acc, rev, stats = run(f, [L("X", date(2019, 1, 2), 2.0), L("X", SESSIONS[0].date(), 2.0),
                              L("X", date(2030, 1, 2), 2.0)], [L("X", date(2019, 1, 2), 2.0)])
    assert acc == [] and rev == []
    assert stats["outside_range_alpaca"] == 3 and stats["outside_range_fmp"] == 1


def test_events_for_symbols_not_in_the_store_and_ratio_one_are_counted():
    acc, rev, stats = run({"X": bars()}, [L("NOPE", D, 2.0), L("X", D, 1.0)], [L("NOPE", D, 2.0)])
    assert acc == [] and rev == []
    assert stats["events_symbol_not_in_store_alpaca"] == 1 and stats["events_symbol_not_in_store_fmp"] == 1
    assert stats["events_ratio_one_alpaca"] == 1


def test_naive_timestamps_work():
    acc, _, _ = run({"X": bars(40, 4.0, tz=False)}, [L("X", D, 4.0)])
    assert len(acc) == 1


def test_unusable_ratio_is_refused():
    with pytest.raises(ValueError):
        run({"X": bars()}, [L("X", D, 0.0)])


# ---- overrides --------------------------------------------------------------------------------------------
def ov(decision, ratio=None, d=D, **kw):
    e = {"symbol": "X", "event_date": d.isoformat(), "decision": decision, "reason": "r", "added": "2026-10-20",
         "reviewed_by": "owner"}
    if ratio is not None:
        e["ratio"] = ratio
    e.update(kw)
    return e


def test_override_decides_its_event_outright():
    acc, rev, _ = run({"X": bars()}, [L("X", D, 2.0)], [L("X", D, 2.0)], [ov("accept", 2.0)])   # price says no split
    assert [(a["factor"], a["verdict"]) for a in acc] == [(2.0, "override_accept")] and rev == []
    acc, rev, _ = run({"X": bars(40, 4.0)}, [L("X", D, 4.0)], [], [ov("reject")])               # price proves it
    assert acc == [] and rev == []
    acc, _, _ = run({"X": bars(40, 4.0)}, [L("X", D, 4.0)], [], [ov("accept", 5.0)])            # override ratio wins
    assert acc[0]["factor"] == 5.0


def test_override_without_a_listing_adds_or_silences():
    acc, rev, _ = run({"X": bars(40, 2.5)}, [], [], [ov("accept", 2.5, d=SESSIONS[40].date())])
    assert [a["factor"] for a in acc] == [2.5] and rev == []
    acc, rev, _ = run({"X": bars(40, 2.5)}, [], [], [ov("reject", d=SESSIONS[40].date())])
    assert acc == [] and rev == []                              # the jump row is silenced by the reviewed reject


def test_override_for_a_symbol_outside_the_run_is_counted():
    _, _, stats = run({"X": bars()}, [], [], [ov("accept", 2.0, symbol="Y")])
    assert stats["overrides_unused"] == 1


def write_ov(tmp_path, entries):
    p = tmp_path / "o.json"
    p.write_text(json.dumps({"_doc": "d", "entries": entries}), encoding="utf-8")
    return str(p)


def test_load_overrides_validates_and_refuses_pending(tmp_path):
    assert st.load_overrides(write_ov(tmp_path, [ov("accept", 2.0)]))[0]["ratio"] == 2.0
    with pytest.raises(st.SplitOverridesPending):
        st.load_overrides(write_ov(tmp_path, [ov("reject", reviewed_by="pending: owner")]))
    for bad in (ov("accept"), ov("maybe"), ov("reject", reason=""), ov("accept", 1.0)):
        with pytest.raises(ValueError):
            st.load_overrides(write_ov(tmp_path, [bad]))
    with pytest.raises(ValueError):
        st.load_overrides(write_ov(tmp_path, [ov("reject"), ov("reject")]))


# ---- listings -----------------------------------------------------------------------------------------------
MONTH = {
    "forward_splits": [{"symbol": "AAPL", "ex_date": "2020-08-31", "old_rate": 1, "new_rate": 4},
                       {"symbol": "OLD", "ex_date": "2020-03-02", "old_rate": 1, "new_rate": 2}],
    "reverse_splits": [{"symbol": "AMC", "ex_date": "2023-08-24", "old_rate": 10, "new_rate": 1}],
    "stock_dividends": [{"symbol": "CIG", "ex_date": "2020-08-13", "rate": 1.04113}],
    "unit_splits": [{"old_symbol": "CCH.U", "new_symbol": "CCH", "old_rate": 1, "new_rate": 1, "effective_date": "2020-09-01"},
                    {"old_symbol": "ZZ", "new_symbol": "ZZ", "old_rate": 2, "new_rate": 3, "effective_date": "2020-09-02"}],
    "name_changes": [{"old_symbol": "OLD", "new_symbol": "MID", "process_date": "2021-01-04"},
                     {"old_symbol": "MID", "new_symbol": "NEW", "process_date": "2022-05-02"},
                     {"old_symbol": "BRK.B", "new_symbol": "BRK.C", "process_date": "2019-01-01"}],
    "spin_offs": [{"symbol": "HON", "ex_date": "2020-01-01"}],
}


def test_alpaca_events_follow_rename_chains_and_skip_non_factor_actions():
    ren = st.build_rename_map([MONTH])
    assert st.map_to_current("old", date(2020, 3, 2), ren) == "NEW"
    assert st.map_to_current("MID", date(2022, 6, 1), ren) == "MID"      # rename was BEFORE the event: not applied
    assert st.map_to_current("BRK.B", date(2020, 1, 1), ren) == "BRK-B"  # rename in the past: no hop
    stats = Counter()
    evs = st.alpaca_events_from_actions([MONTH], ren, stats)
    assert sorted((e.symbol, e.date.isoformat(), round(e.ratio, 5)) for e in evs) == [
        ("AAPL", "2020-08-31", 4.0), ("AMC", "2023-08-24", 0.1), ("CIG", "2020-08-13", 1.04113),
        ("NEW", "2020-03-02", 2.0), ("ZZ", "2020-09-02", 1.5)]
    assert stats["alpaca_unit_splits_other_symbol_ignored"] == 1


def test_fmp_rows_without_a_ratio_are_counted_not_read_as_splits():
    rows = [{"date": "2022-08-25", "symbol": "TSLA", "numerator": 3, "denominator": 1},
            {"date": "2022-08-25", "symbol": "BAD", "numerator": 3, "denominator": 0},
            {"date": "2022-08-25", "symbol": "BAD2", "numerator": None, "denominator": 1}]
    stats = Counter()
    evs = st.fmp_events_from_rows(rows, {}, stats)
    assert evs == [L("TSLA", date(2022, 8, 25), 3.0)] and stats["fmp_rows_without_ratio"] == 2


def test_splits_by_symbol_layout():
    acc, _, _ = run({"X": bars(40, 4.0)}, [L("X", D, 4.0)])
    assert st.splits_by_symbol(acc) == {"X": [[D.isoformat(), 4.0]]}


# ---- the tool -----------------------------------------------------------------------------------------------
def _cli():
    import importlib.util
    import pathlib
    path = pathlib.Path(__file__).resolve().parents[3] / "tools" / "prepare_history.py"
    spec = importlib.util.spec_from_file_location("prepare_history", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fmp_calendar_windows_split_when_full_cache_and_check_dates(tmp_path):
    calls = []

    def get(a, b):
        calls.append((a, b))
        n = sb.FMP_ROW_CAP if (b - a).days >= 10 else 2
        return [{"date": a.isoformat(), "symbol": f"S{i}", "numerator": 2, "denominator": 1} for i in range(n)]

    stats = Counter()
    rows = sb.fetch_fmp_calendar(date(2021, 1, 1), date(2021, 1, 30), str(tmp_path), get, stats)
    assert stats["fmp_windows_split"] >= 1 and all(b - a < timedelta(days=10) for a, b in calls[-2:])
    n_calls = len(calls)
    again = sb.fetch_fmp_calendar(date(2021, 1, 1), date(2021, 1, 30), str(tmp_path), get, Counter())
    assert len(calls) == n_calls and again == rows            # a rerun is offline

    def outside(a, b):
        return [{"date": "2000-01-01", "symbol": "X", "numerator": 2, "denominator": 1}]
    with pytest.raises(st.SplitTableBuildError):
        sb.fetch_fmp_calendar(date(2021, 2, 1), date(2021, 2, 5), str(tmp_path / "o"), outside, Counter())


def test_pipeline_refuses_a_pending_override(tmp_path, capsys):
    cli = _cli()
    p = write_ov(tmp_path, [ov("reject", reviewed_by="pending")])
    assert cli.main(["--source", "alpaca", "--steps", "split-table", "--overrides", p, "--out", str(tmp_path / "out")]) == 2
    assert "pending" in capsys.readouterr().err
    assert cli.main(["--source", "alpaca", "--steps", "split-table", "--overrides", p, "--out", str(tmp_path / "out"), "--dry-run"]) == 2


def test_pipeline_source_and_step_validation(tmp_path, capsys):
    cli = _cli()
    base = ["--out", str(tmp_path / "out")]
    assert cli.main(base) == 2                                                    # --source is required
    assert cli.main(["--source", "yahoo"] + base) == 2
    assert cli.main(["--source", "alpaca", "--steps", "nope"] + base) == 2
    assert "not valid for --source alpaca" in capsys.readouterr().err
    assert cli.main(["--source", "fmp", "--steps", "split-table"] + base) == 2    # valid only for alpaca
    assert "not valid for --source fmp" in capsys.readouterr().err
    assert cli.main(["--source", "fmp", "--from-step", "build"] + base) == 2
    assert cli.main(["--source", "alpaca", "--steps", "split-table", "--from-step", "split-table"] + base) == 2
    assert cli.main(["--source", "alpaca", "--steps", "split-table", "--fmp-from", "yesterday"] + base) == 2
    # valid but not implemented: refuse loudly, run nothing, write nothing
    for src, step in (("alpaca", "download"), ("alpaca", "build"), ("alpaca", "verify"), ("fmp", "download"),
                      ("fmp", "verify")):
        assert cli.main(["--source", src, "--steps", step] + base) == 2
        assert "not implemented yet (plan Task 4)" in capsys.readouterr().err
    assert cli.main(["--source", "alpaca", "--steps", "split-table,build"] + base) == 2   # one unimplemented step refuses all
    assert cli.main(["--source", "alpaca"] + base) == 2                           # default = every step: refuses
    assert cli.main(["--source", "fmp", "--dry-run"] + base) == 2
    assert not (tmp_path / "out").exists()


def test_pipeline_end_to_end_on_a_tiny_store_is_offline_and_reproducible(tmp_path):
    store = tmp_path / "store"
    raw = bars(40, 4.0)
    d = store / "bars" / "1Day" / "symbol=NA"                   # the symbol "NA" must survive the CSVs
    d.mkdir(parents=True)
    raw.assign(h=raw["o"], l=raw["o"]).to_parquet(d / "all.parquet")
    (store / "STORE_INFO.json").write_text(json.dumps({"asof": "2021-03-31"}), encoding="utf-8")
    ca = store / "corporate_actions"
    ca.mkdir()
    import gzip
    with gzip.open(ca / "ym=2021-03.json.gz", "wt", encoding="utf-8") as f:
        json.dump({"forward_splits": [{"symbol": "NA", "ex_date": D.isoformat(), "old_rate": 1, "new_rate": 4}]}, f)
    fmpd = tmp_path / "fmpd"
    fmpd.mkdir()
    fmp(raw, {40: 4.0}).to_parquet(fmpd / "NA_1d.parquet")
    out = tmp_path / "out"
    cal = out / "_meta" / "fmp_split_calendar"
    cal.mkdir(parents=True)
    (cal / "2021-01-01_2021-01-30.json").write_text("[]", encoding="utf-8")
    (cal / "2021-01-31_2021-03-01.json").write_text("[]", encoding="utf-8")
    (cal / "2021-03-02_2021-03-31.json").write_text("[]", encoding="utf-8")
    argv = ["--source", "alpaca", "--steps", "split-table", "--store", str(store), "--out", str(out), "--fmp-daily-dir", str(fmpd), "--db", str(tmp_path / "no.db"),
            "--fmp-from", "2021-01-01", "--fmp-to", "2021-03-31"]
    cli = _cli()
    assert cli.main(argv + ["--dry-run"]) == 0 and not (out / "_meta" / "split_table.json").exists()
    assert cli.main(argv) == 0
    table = json.loads((out / "_meta" / "split_table.json").read_text(encoding="utf-8"))
    assert table["splits"] == {"NA": [[D.isoformat(), 4.0]]} and table["fmp_daily_files"]["count"] == 1
    acc = sb.read_csv_frame(str(out / "_meta" / "split_accepted.csv"))
    assert list(acc["symbol"]) == ["NA"] and acc["fmp_step"][0].endswith("equals_f")
    first = (out / "_meta" / "split_table.json").read_bytes()
    assert cli.main(argv) == 0 and (out / "_meta" / "split_table.json").read_bytes() == first
    report = json.loads((out / "_meta" / "prepare_history_alpaca_report.json").read_text(encoding="utf-8"))
    assert report["steps"][0]["status"] == "ok"


# ---- the FMP step arbiter -----------------------------------------------------------------------------------
def fmp(raw, applied=None, wobble=None):
    """FMP ADJUSTED daily frame over the same sessions: `applied` = {session index: factor FMP applied there}; closes
    before such a session are divided by the factor (so r = adj / raw steps by `factor` across it).  `wobble` =
    {session index: multiplier} perturbs single closes (an unstable side)."""
    close = raw["c"].to_numpy(dtype=float).copy()
    for k, f in (applied or {}).items():
        close[:k] = close[:k] / f
    for k, m in (wobble or {}).items():
        close[k] *= m
    return pd.DataFrame({"Date": [pd.Timestamp(t).tz_convert("America/New_York").tz_localize(None).normalize()
                                  for t in raw["t"]], "Close": close})


def runf(frames, fmp_frames, alp=(), fm=(), ov=()):
    stats = Counter()
    acc, rev = st.decide_splits(frames, list(alp), list(fm), list(ov), stats, fmp_daily=fmp_frames)
    return acc, rev, stats


def test_reverse_split_whose_jump_misses_by_20_percent_is_proven_by_the_fmp_step():
    raw = bars(40, 0.18)                                        # 0.18 vs 0.1: x1.8 off, so neither the jump proof nor the x1.5 rule applies
    acc, rev, _ = run({"X": raw}, [L("X", D, 0.1)], [L("X", D, 0.1)])
    assert acc == [] and one(rev, "unresolved")                 # the jump alone cannot decide it
    acc, rev, stats = runf({"X": raw}, {"X": fmp(raw, {40: 0.1})}, [L("X", D, 0.1)], [L("X", D, 0.1)])
    assert [(a["factor"], a["verdict"], a["source"]) for a in acc] == [(0.1, "proven_by_fmp_step", "both")]
    assert acc[0]["fmp_step"].endswith("equals_f") and rev == []
    assert stats["verdict_proven_by_fmp_step"] == 1


def test_fmp_only_listing_proven_by_the_step_gets_a_review_row():
    raw = bars(40, 0.12)
    acc, rev, _ = runf({"X": raw}, {"X": fmp(raw, {40: 0.1})}, [], [L("X", D, 0.1)])
    assert len(acc) == 1 and acc[0]["source"] == "fmp"
    assert one(rev, "proven_by_fmp_step")["applied"] is True


def test_jump_proof_still_comes_first():
    raw = bars(40, 4.0)
    acc, _, _ = runf({"X": raw}, {"X": fmp(raw, {40: 4.0})}, [L("X", D, 4.0)])
    assert [a["verdict"] for a in acc] == ["proven"]


def test_unexplained_jump_with_a_flat_step_is_the_market():
    raw = bars(40, 2.5)
    acc, rev, stats = runf({"X": raw}, {"X": fmp(raw)})
    assert acc == [] and rev == [] and stats["verdict_jump_explained_by_market"] == 1
    # FMP applied a 2.5 there (step "other" against f=1): the jump stays a review row, with the step shown
    _, rev, _ = runf({"X": raw}, {"X": fmp(raw, {40: 2.5})})
    assert one(rev, "unexplained_jump")["fmp_step"] == "2.5 other"
    _, rev, _ = runf({"X": raw}, {})                            # no FMP data
    assert one(rev, "unexplained_jump")["fmp_step"] == "none"
    _, rev, _ = run({"X": raw})                                 # arbiter off
    assert one(rev, "unexplained_jump")["fmp_step"] == ""


def test_contradicted_listing_with_a_flat_step_is_rejected_without_a_row():
    raw = bars()
    acc, rev, stats = runf({"X": raw}, {"X": fmp(raw)}, [L("X", D, 2.0)])
    assert acc == [] and rev == [] and stats["verdict_rejected_confirmed"] == 1
    # FMP applied something else (1.5): not flat, not equal -> the usual contradicted row, with the step
    _, rev, _ = runf({"X": raw}, {"X": fmp(raw, {40: 1.5})}, [L("X", D, 2.0)])
    assert one(rev, "alpaca_contradicted")["fmp_step"] == "1.5 other"


def test_step_without_a_matching_jump_is_not_applied():
    # guard: the step equals f but the raw jump is flat (ADEA / TJGC) -> review row, NOT applied
    raw = bars()
    acc, rev, _ = runf({"X": raw}, {"X": fmp(raw, {40: 2.0})}, [L("X", D, 2.0)])
    r = one(rev, "fmp_step_without_jump")
    assert acc == [] and r["applied"] is False and r["fmp_step"].endswith("equals_f")
    # FMP-only, the jump in the right direction but x2 off f (NEUP style): (i) fails and (ii) needs both vendors
    raw = bars(40, 2.0)
    acc, rev, _ = runf({"X": raw}, {"X": fmp(raw, {40: 4.0})}, [], [L("X", D, 4.0)])
    assert acc == [] and one(rev, "fmp_step_without_jump")["jump"] == pytest.approx(2.0, rel=1e-4)
    # (ii) both vendors list f and the jump is closer to f than to 1 (x1.9 off f=0.05, jump 0.095 vs 1): applied
    raw = bars(40, 0.095)
    acc, _, _ = runf({"X": raw}, {"X": fmp(raw, {40: 0.05})}, [L("X", D, 0.05)], [L("X", D, 0.05)])
    assert [a["verdict"] for a in acc] == ["proven_by_fmp_step"]
    # (ii) fails, JBIO style: both list f=0.02857 but the jump 0.21 is closer to 1 than to f
    raw = bars(40, 0.21)
    acc, rev, _ = runf({"X": raw}, {"X": fmp(raw, {40: 0.02857})}, [L("X", D, 0.02857)], [L("X", D, 0.02857)])
    assert acc == [] and one(rev, "fmp_step_without_jump")
    # the same on the far side, x1.4 off f (inside x1.5, outside the 15 % jump tolerance): applied
    raw = bars(40, 4.0 / 1.4)
    acc, _, _ = runf({"X": raw}, {"X": fmp(raw, {40: 4.0})}, [L("X", D, 4.0)], [L("X", D, 4.0)])
    assert [a["verdict"] for a in acc] == ["proven_by_fmp_step"] and acc[0]["jump"] == pytest.approx(4.0 / 1.4, rel=1e-4)


def test_mismatch_resolved_by_the_step():
    raw = bars(40, 1.25)                                        # proves neither 2.0 nor 4.0, contradicts neither
    acc, rev, _ = runf({"X": raw}, {"X": fmp(raw, {40: 4.0})}, [L("X", D, 2.0)], [L("X", D, 4.0)])
    assert [(a["factor"], a["verdict"], a["source"]) for a in acc] == [(4.0, "ratio_mismatch_resolved_by_fmp_step", "fmp")]
    assert one(rev, "ratio_mismatch_resolved_by_fmp_step")["applied"] is True
    # the step equals neither ratio: still a mismatch
    acc, rev, _ = runf({"X": raw}, {"X": fmp(raw, {40: 3.0})}, [L("X", D, 2.0)], [L("X", D, 4.0)])
    assert acc == [] and one(rev, "ratio_mismatch")
    # the jump CONTRADICTS the ratio the step equals: not resolved
    flat = bars()
    acc, rev, _ = runf({"X": flat}, {"X": fmp(flat, {40: 4.0})}, [L("X", D, 2.0)], [L("X", D, 4.0)])
    assert acc == [] and one(rev, "ratio_mismatch")


def test_unstable_side_gives_no_vote_unless_another_event_is_near():
    raw = bars(40, 0.18)
    f = fmp(raw, {40: 0.1}, wobble={38: 1.05})                  # r jumps 5 % two sessions before D
    acc, rev, _ = runf({"X": raw}, {"X": f}, [L("X", D, 0.1)], [L("X", D, 0.1)])
    assert acc == [] and one(rev, "unresolved")["fmp_step"].endswith("unstable")
    other = L("X", SESSIONS[38].date(), 1.05)                   # a small event in those sessions: only D-1 and D count
    acc, rev, _ = runf({"X": raw}, {"X": f}, [L("X", D, 0.1), other], [L("X", D, 0.1), other])
    assert {a["verdict"] for a in acc} == {"proven_by_fmp_step", "small_both"}


def test_no_fmp_data_leaves_the_decision_to_the_jump():
    raw = bars(40, 0.18)
    for fd in (None, {}, {"X": fmp(raw).iloc[0:0]}):
        acc, rev, _ = runf({"X": raw}, fd, [L("X", D, 0.1)], [L("X", D, 0.1)])
        assert acc == [] and one(rev, "unresolved")
    one_sided = fmp(raw).iloc[:40]                              # FMP stops before the event
    acc, rev, _ = runf({"X": raw}, {"X": one_sided}, [L("X", D, 0.1)], [L("X", D, 0.1)])
    assert acc == [] and one(rev, "unresolved")["fmp_step"] == "none"


def test_small_factors_ignore_the_step():
    raw = bars()
    acc, rev, _ = runf({"X": raw}, {"X": fmp(raw, {40: 1.05})}, [L("X", D, 1.05)])
    assert acc == [] and one(rev, "small_single_vendor")


def test_compare_rows_carry_the_step():
    raw = bars(40, 0.12)
    fd = {"X": fmp(raw, {40: 0.1})}
    acc, rev, _ = runf({"X": raw}, fd, [L("X", D, 0.1)], [L("X", D, 0.1)])
    rows = st.compare_with_fmp({"X": raw}, acc, rev, [L("X", D, 0.1)], [L("X", D, 0.1)], fd)
    assert rows[0]["class"] == "agree" and rows[0]["fmp_step"].endswith("equals_f")
    flat = bars()
    fd = {"X": fmp(flat)}
    acc, rev, _ = runf({"X": flat}, fd, [], [L("X", D, 2.0)])
    rows = st.compare_with_fmp({"X": flat}, acc, rev, [], [L("X", D, 2.0)], fd)
    assert rows[0]["class"] == "fmp_only_rejected" and rows[0]["verdict"] == "rejected_confirmed"


def test_csv_reader_keeps_the_symbol_NA(tmp_path):
    p = tmp_path / "r.csv"
    p.write_text("symbol,event_date,alpaca_ratio,fmp_ratio,jump,fmp_step,verdict,applied,reason\n"
                 "NA,2020-01-02,,2.0,1.1,2 equals_f,unresolved,False,r\nNAN,2020-01-02,0.5,,,none,x,False,r\n",
                 encoding="utf-8")
    df = sb.read_csv_frame(str(p))
    assert list(df["symbol"]) == ["NA", "NAN"] and df["alpaca_ratio"].isna().tolist() == [True, False]
    assert df["fmp_step"].tolist() == ["2 equals_f", "none"]


def test_every_verdict_code_is_documented_in_the_module_docstring():
    for v in st.VERDICTS:
        assert v in st.__doc__, v


def test_stability_waiver_counts_listed_neighbours_even_when_they_are_rejected():
    # the waiver uses LISTED events of other units, not accepted ones: a neighbour listing that is later rejected
    # (here: a contradicted 2.0 listing two sessions earlier) still waives the stability check of the main event
    raw = bars(40, 0.18)
    f = fmp(raw, {40: 0.1}, wobble={38: 1.05})                  # r jumps 5 % at session 38: an unstable side
    acc, rev, _ = runf({"X": raw}, {"X": f}, [L("X", D, 0.1)], [L("X", D, 0.1)])
    assert acc == [] and one(rev, "unresolved")["fmp_step"].endswith("unstable")            # no neighbour: no waiver
    neighbour = L("X", SESSIONS[38].date(), 2.0)                # flat raw jump there: contradicted, never accepted
    acc, rev, _ = runf({"X": raw}, {"X": f}, [L("X", D, 0.1), neighbour], [L("X", D, 0.1)])
    assert [(a["factor"], a["verdict"]) for a in acc] == [(0.1, "proven_by_fmp_step")]
    assert not any(r["applied"] and r["alpaca_ratio"] == 2.0 for r in rev)             # the neighbour stays unapplied


def test_pipeline_outputs_are_one_coherent_set(tmp_path, monkeypatch):
    store = tmp_path / "store"
    raw = bars(40, 4.0)
    d = store / "bars" / "1Day" / "symbol=AA"
    d.mkdir(parents=True)
    raw.assign(h=raw["o"], l=raw["o"]).to_parquet(d / "all.parquet")
    (store / "STORE_INFO.json").write_text(json.dumps({"asof": "2021-01-30"}), encoding="utf-8")
    (store / "corporate_actions").mkdir()
    fmpd = tmp_path / "fmpd"
    fmpd.mkdir()
    fmp(raw, {40: 4.0}).to_parquet(fmpd / "AA_1d.parquet")
    out = tmp_path / "out"
    cal = out / "_meta" / "fmp_split_calendar"
    cal.mkdir(parents=True)
    (cal / "2021-01-01_2021-01-30.json").write_text("[]", encoding="utf-8")
    import gzip
    with gzip.open(store / "corporate_actions" / "ym=2021-01.json.gz", "wt", encoding="utf-8") as f:
        json.dump({"forward_splits": [{"symbol": "AA", "ex_date": D.isoformat(), "old_rate": 1, "new_rate": 4}]}, f)
    written = []
    real = sb.atomic_write
    monkeypatch.setattr(sb, "atomic_write", lambda path, data: (written.append(path.replace("\\", "/").rsplit("/", 1)[-1]),
                                                                real(path, data)))
    res = sb.build_split_table(store=str(store), out=str(out), db="unused", fmp_daily_dir=str(fmpd), fmp_from="2021-01-01",
                               fmp_to="2021-01-30", log=lambda m: None)
    assert written[-1] == "split_table.json" and written.count("split_table.json") == 1       # the table is written LAST
    meta = out / "_meta"
    table = json.loads((meta / "split_table.json").read_text(encoding="utf-8"))
    assert table["run_id"] == res["run_id"] and len(res["run_id"].split("-")[1]) == 8
    for name in ("split_review.csv", "split_accepted.csv", "split_vs_fmp.csv", "fmp_daily_inputs.csv"):
        df = sb.read_csv_frame(str(meta / name))
        assert df.columns[-1] == "run_id" and set(df["run_id"]) <= {table["run_id"]}, name
    inp = sb.read_csv_frame(str(meta / "fmp_daily_inputs.csv"))
    import hashlib
    assert inp["file"].tolist() == ["AA_1d.parquet"]
    assert inp["sha256"][0] == hashlib.sha256((fmpd / "AA_1d.parquet").read_bytes()).hexdigest()
    assert table["fmp_daily_files"]["count"] == 1 and table["fmp_daily_files"]["content_sha256"]


# ---- review fixes: key leak, timestamps, store end, stray files, structure ------------------------------------
import gzip as _gzip


def tiny_store(tmp_path, symbol="AA", asof="2021-01-30", listed=True):
    store = tmp_path / "store"
    raw = bars(40, 4.0)
    d = store / "bars" / "1Day" / f"symbol={symbol}"
    d.mkdir(parents=True)
    raw.assign(h=raw["o"], l=raw["o"]).to_parquet(d / "all.parquet")
    (store / "STORE_INFO.json").write_text(json.dumps({"asof": asof}), encoding="utf-8")
    (store / "corporate_actions").mkdir()
    sp = [{"symbol": symbol, "ex_date": D.isoformat(), "old_rate": 1, "new_rate": 4}] if listed else []
    with _gzip.open(store / "corporate_actions" / "ym=2021-03.json.gz", "wt", encoding="utf-8") as f:
        json.dump({"forward_splits": sp}, f)
    fmpd = tmp_path / "fmpd"
    fmpd.mkdir()
    fmp(raw, {40: 4.0}).to_parquet(fmpd / f"{symbol}_1d.parquet")
    return store, fmpd, raw


def test_http_error_never_leaks_the_api_key(tmp_path, monkeypatch, capsys):
    import requests
    key = "SECRETKEY123"
    store, fmpd, _ = tiny_store(tmp_path)
    monkeypatch.setattr(sb, "read_fmp_key", lambda db: key)

    class Resp:
        status_code = 401
        headers = {}

        def raise_for_status(self):
            raise requests.HTTPError(f"401 Client Error: Unauthorized for url: https://x/api?from=a&apikey={key}")
    monkeypatch.setattr(requests, "get", lambda url, params=None, timeout=None: Resp())
    with pytest.raises(st.SplitTableBuildError) as ei:
        sb.make_fmp_getter(key)(date(2021, 1, 1), date(2021, 1, 30))
    assert key not in str(ei.value) and "***" in str(ei.value)
    out = tmp_path / "out"
    argv = ["--source", "alpaca", "--steps", "split-table", "--store", str(store), "--out", str(out),
            "--fmp-daily-dir", str(fmpd), "--db", str(tmp_path / "x.db"), "--fmp-from", "2021-01-01"]
    assert _cli().main(argv) == 1
    report = (out / "_meta" / "prepare_history_alpaca_report.json").read_text(encoding="utf-8")
    assert key not in report and key not in capsys.readouterr().err
    entry = json.loads(report)["steps"][0]
    assert entry["status"] == "failed" and "SplitTableBuildError" in entry["traceback"]


def test_daily_bar_timestamps_must_be_midnight_new_york():
    summer = pd.DataFrame({"t": [pd.Timestamp("2021-07-06 04:00", tz="UTC")], "o": [1.0], "c": [1.0]})   # 00:00 EDT
    winter = pd.DataFrame({"t": [pd.Timestamp("2021-01-06 05:00", tz="UTC")], "o": [1.0], "c": [1.0]})   # 00:00 EST
    assert len(st._bars(summer, Counter(), "X").days) == 1 and len(st._bars(winter, Counter(), "X").days) == 1
    for bad in ("2021-07-06 00:00", "2021-01-06 00:00", "2021-07-06 05:00"):        # 00:00Z, and winter time in summer
        df = pd.DataFrame({"t": [pd.Timestamp(bad, tz="UTC")], "o": [1.0], "c": [1.0]})
        with pytest.raises(st.SplitTableBuildError, match="00:00 New York"):
            st._bars(df, Counter(), "X")
    dup = pd.concat([winter, winter], ignore_index=True)
    with pytest.raises(st.SplitTableBuildError, match="more than once"):
        st._bars(dup, Counter(), "X")


def test_fmp_to_defaults_to_the_store_end_and_is_refused_before_the_asof(tmp_path):
    store, fmpd, _ = tiny_store(tmp_path, asof="2021-01-30")
    out = tmp_path / "out"
    cal = out / "_meta" / "fmp_split_calendar"
    cal.mkdir(parents=True)
    (cal / "2021-01-01_2021-01-30.json").write_text("[]", encoding="utf-8")
    (cal / "2021-01-31_2021-01-31.json").write_text("[]", encoding="utf-8")     # asof + 1 day = 2021-01-31
    res = sb.build_split_table(store=str(store), out=str(out), db="unused", fmp_daily_dir=str(fmpd), fmp_from="2021-01-01",
                               log=lambda m: None)
    assert res["fmp_to"] == "2021-01-31"
    with pytest.raises(st.SplitTableBuildError, match="before the store"):
        sb.build_split_table(store=str(store), out=str(out), db="unused", fmp_daily_dir=str(fmpd),
                             fmp_from="2021-01-01", fmp_to="2021-01-29", log=lambda m: None)


def test_a_store_without_any_split_is_refused(tmp_path):
    store, fmpd, _ = tiny_store(tmp_path, asof="2021-01-30", listed=False)
    out = tmp_path / "out"
    cal = out / "_meta" / "fmp_split_calendar"
    cal.mkdir(parents=True)
    (cal / "2021-01-01_2021-01-30.json").write_text("[]", encoding="utf-8")
    (cal / "2021-01-31_2021-01-31.json").write_text("[]", encoding="utf-8")
    with pytest.raises(st.SplitTableBuildError, match="no forward or reverse split"):
        sb.build_split_table(store=str(store), out=str(out), db="unused", fmp_daily_dir=str(fmpd),
                             fmp_from="2021-01-01", log=lambda m: None)


def test_stray_temp_files_do_not_change_the_input_hash(tmp_path):
    store, fmpd, _ = tiny_store(tmp_path, asof="2021-01-30")
    out = tmp_path / "out"
    cal = out / "_meta" / "fmp_split_calendar"
    cal.mkdir(parents=True)
    (cal / "2021-01-01_2021-01-30.json").write_text("[]", encoding="utf-8")
    (cal / "2021-01-31_2021-01-31.json").write_text("[]", encoding="utf-8")
    kw = dict(store=str(store), out=str(out), db="unused", fmp_daily_dir=str(fmpd), fmp_from="2021-01-01",
              log=lambda m: None)
    sb.build_split_table(**kw)
    first = json.loads((out / "_meta" / "split_table.json").read_text(encoding="utf-8"))["inputs_sha256"]
    (cal / "2021-01-01_2021-01-30.json.tmp4242").write_text("junk", encoding="utf-8")
    (store / "corporate_actions" / "ym=2021-03.json.gz.tmp4242").write_bytes(b"junk")
    sb.build_split_table(**kw)
    assert json.loads((out / "_meta" / "split_table.json").read_text(encoding="utf-8"))["inputs_sha256"] == first


def test_atomic_write_removes_its_temp_file_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "t.json"

    def boom(a, b):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        sb.atomic_write(str(target), b"x")
    assert list(tmp_path.iterdir()) == []


def test_default_paths_come_from_the_ba2_home_config():
    from ba2_common import config as cfg
    assert sb.DEFAULT_STORE == os.path.join(cfg.BA2_HOME, "alpaca_store_daily2016")
    assert os.path.basename(sb.DEFAULT_OUT) == "AlpacaHistoryOHLCVProvider" and sb.DEFAULT_DB == os.path.join(cfg.TRADE_DIR, "db.sqlite")


def test_unexplained_jump_scan_skips_pairs_further_apart_than_the_gap_limit():
    sess = SESSIONS[:30] + SESSIONS[70:]                       # a 3-month hole; the price is 3x after it
    raw = bars(30, 3.0, sessions=sess)
    _, rev, _ = run({"X": raw})
    assert rev == []                                            # the pair across the hole observes no overnight


def test_two_accepted_events_on_one_date_are_refused():
    with pytest.raises(st.SplitTableBuildError, match="two accepted events"):
        st.splits_by_symbol([{"symbol": "X", "date": "2021-03-01", "factor": 2.0},
                             {"symbol": "X", "date": "2021-03-01", "factor": 1.1}])


def test_override_matching_several_groups_is_refused_and_absorbed_listings_are_counted():
    raw = bars(40, 4.0)
    _, _, stats = run({"X": raw}, [L("X", D, 4.0), L("X", D, 4.0)], [], [ov("reject")])
    assert stats["listings_absorbed_by_override"] == 2          # both listings of the one group
    # the same event listed with different ratios on one date (an inverted ratio): the one override decides it once
    acc, _, stats = run({"X": raw}, [L("X", D, 4.0)], [L("X", D, 0.25)], [ov("accept", 4.0)])
    assert [(a["factor"], a["verdict"]) for a in acc] == [(4.0, "override_accept")]
    # two overrides matching the dates of one group
    with pytest.raises(st.SplitTableBuildError, match="ambiguous"):
        run({"X": raw}, [L("X", D, 4.0), L("X", SESSIONS[41].date(), 4.0)], [],
            [ov("reject"), ov("reject", d=SESSIONS[41].date())])


def test_fmp_rows_with_bool_or_negative_sides_are_refused():
    for bad in ({"numerator": True, "denominator": 1}, {"numerator": -2, "denominator": 1},
                {"numerator": 2, "denominator": -1}):
        with pytest.raises(st.SplitTableBuildError):
            st.fmp_events_from_rows([{"date": "2022-01-03", "symbol": "X", **bad}], {})


def test_symbols_option_is_normalized(tmp_path):
    store, fmpd, _ = tiny_store(tmp_path, symbol="BRK-B", asof="2021-01-30")
    out = tmp_path / "out"
    cal = out / "_meta" / "fmp_split_calendar"
    cal.mkdir(parents=True)
    (cal / "2021-01-01_2021-01-30.json").write_text("[]", encoding="utf-8")
    (cal / "2021-01-31_2021-01-31.json").write_text("[]", encoding="utf-8")
    argv = ["--source", "alpaca", "--steps", "split-table", "--store", str(store), "--out", str(out), "--fmp-daily-dir",
            str(fmpd), "--db", str(tmp_path / "x.db"), "--fmp-from", "2021-01-01", "--symbols", "brk.b"]
    assert _cli().main(argv) == 0
    table = json.loads((out / "_meta" / "split_table.json").read_text(encoding="utf-8"))
    assert list(table["splits"]) == ["BRK-B"]


# ---- A: both_listed_jump_within_1_5, B: inferred_from_fmp_step, C: the reviewed overrides, D: series breaks -----------
def test_both_vendors_listing_a_big_ratio_with_a_jump_within_x1_5_is_accepted_without_the_step():
    raw = bars(40, 3.0)                                          # f=4: outside the 15 % jump tolerance, inside x1.5
    acc, rev, stats = run({"X": raw}, [L("X", D, 4.0)], [L("X", D, 4.0)])
    assert [(a["verdict"], a["source"]) for a in acc] == [("both_listed_jump_within_1_5", "both")] and rev == []
    assert stats["verdict_both_listed_jump_within_1_5"] == 1
    acc, rev, _ = run({"X": bars(40, 2.0)}, [L("X", D, 4.0)], [L("X", D, 4.0)])         # x2 off f: not accepted
    assert acc == [] and one(rev, "unresolved")
    acc, rev, _ = run({"X": raw}, [L("X", D, 4.0)], [])                                  # one vendor only: not accepted
    assert acc == [] and one(rev, "unresolved")
    acc, rev, _ = run({"X": bars(40, 1.5)}, [L("X", D, 1.8)], [L("X", D, 1.8)])          # |ln f| < ln 2: not this rule
    assert acc == [] and rev and rev[0]["verdict"] != "both_listed_jump_within_1_5"
    acc, rev, _ = run({"X": raw}, [L("X", D, 4.0)], [L("X", D, 3.0)])                    # different ratios: not this rule
    assert [a["verdict"] for a in acc] != ["both_listed_jump_within_1_5"]


def test_clean_ratios():
    assert st._nearest_clean_ratio(0.1003) == pytest.approx(0.1) and st._nearest_clean_ratio(3.99) == 4
    assert st._nearest_clean_ratio(1.5) == 1.5 and st._nearest_clean_ratio(0.6667) == pytest.approx(2 / 3)
    assert st._nearest_clean_ratio(1000.0) == 1000 and st._nearest_clean_ratio(0.001) == pytest.approx(0.001)
    for off in (0.13, 1.0, 2.5, 6.5, 1001.0, 0.0009):
        assert st._nearest_clean_ratio(off) is None, off


def test_unlisted_split_is_inferred_from_a_clean_fmp_step_and_a_matching_jump():
    raw = bars(40, 0.081)                                        # jump 0.081, FMP applied 0.1 (OP 2022-12-22 style)
    acc, rev, stats = runf({"X": raw}, {"X": fmp(raw, {40: 0.1})})
    assert [(a["factor"], a["verdict"], a["source"]) for a in acc] == [(0.1, "inferred_from_fmp_step", "fmp_step")]
    r = one(rev, "inferred_from_fmp_step")
    assert r["applied"] is True and r["fmp_step"].endswith("equals_f") is False and "0.1" in r["fmp_step"]
    assert stats["verdict_inferred_from_fmp_step"] == 1
    # a jump x2.5 away from the clean ratio: not inferred (and 4x is not a series break)
    raw = bars(40, 0.25)
    acc, rev, _ = runf({"X": raw}, {"X": fmp(raw, {40: 0.1})})
    assert acc == [] and one(rev, "unexplained_jump")
    # the step is not a clean ratio
    raw = bars(40, 0.4)
    acc, rev, _ = runf({"X": raw}, {"X": fmp(raw, {40: 0.4})})
    assert acc == [] and one(rev, "unexplained_jump")
    # no FMP data: nothing to infer from (a x12 collapse with no explanation is then a series break)
    acc, rev, _ = run({"X": bars(40, 0.081)})
    assert acc == [] and one(rev, "series_break")


def test_series_break_for_an_unexplained_jump_beyond_x5_that_the_step_does_not_explain():
    raw = bars(40, 8.0)
    breaks = {}
    stats = Counter()
    acc, rev = st.decide_splits({"X": raw}, [], [], [], stats, fmp_daily={"X": fmp(raw, {40: 6.5})}, breaks=breaks)
    assert acc == [] and one(rev, "series_break")["applied"] is False
    assert breaks == {"X": [SESSIONS[40].date().isoformat()]} and stats["verdict_series_break"] == 1
    # FMP's step is flat there (a real market move): never a break
    breaks = {}
    acc, rev = st.decide_splits({"X": raw}, [], [], [], Counter(), fmp_daily={"X": fmp(raw)}, breaks=breaks)
    assert rev == [] and breaks == {}
    # an inferred split is not a break
    acc, rev = st.decide_splits({"X": raw}, [], [], [], Counter(), fmp_daily={"X": fmp(raw, {40: 8.0})}, breaks=breaks)
    assert [a["verdict"] for a in acc] == ["inferred_from_fmp_step"] and breaks == {}
    # between x1.8 and x5 stays an ordinary unexplained jump; without FMP data a x8 jump is a break
    breaks = {}
    _, rev = st.decide_splits({"X": bars(40, 3.0)}, [], [], [], Counter(), breaks=breaks)
    assert one(rev, "unexplained_jump") and breaks == {}
    _, rev = st.decide_splits({"X": raw}, [], [], [], Counter(), breaks=breaks)
    assert one(rev, "series_break") and list(breaks) == ["X"]
    _, rev = st.decide_splits({"X": bars(40, 0.1)}, [], [], [], Counter(), breaks=breaks)   # a collapse is a break too
    assert one(rev, "series_break")


def test_shipped_overrides_are_the_reviewed_ones():
    entries = st.load_overrides()
    by = {(e["symbol"], e["event_date"]): e for e in entries}
    assert len(entries) == 12 and all(e["reviewed_by"] == "owner (B. Migette) 2026-10-20" and e["added"] == "2026-10-20"
                                      for e in entries)
    assert by[("VTOL", "2020-06-12")]["ratio"] == pytest.approx(1 / 3) and by[("WHWK", "2021-08-27")]["ratio"] == pytest.approx(1 / 15)
    assert by[("ENOV", "2022-04-05")]["ratio"] == pytest.approx(1 / 3) and by[("IRON", "2022-12-30")]["ratio"] == 0.1
    assert by[("JBIO", "2025-04-29")]["ratio"] == pytest.approx(1 / 35)
    assert {k for k, e in by.items() if e["decision"] == "reject"} == {
        ("AMC", "2022-08-22"), ("BIP", "2020-03-31"), ("NVS", "2019-04-09"), ("HON", "2026-06-28"), ("ADEA", "2022-10-03"), ("CRESY", "2025-11-28"), ("CTVA", "2026-10-01")}


def test_breaks_are_written_to_the_table(tmp_path):
    store, fmpd, raw = tiny_store(tmp_path, asof="2021-01-30")
    sym2 = store / "bars" / "1Day" / "symbol=ZZ"
    sym2.mkdir()
    bars(30, 9.0).assign(h=1.0, l=1.0).to_parquet(sym2 / "all.parquet")      # a x9 overnight jump, no listing
    out = tmp_path / "out"
    cal = out / "_meta" / "fmp_split_calendar"
    cal.mkdir(parents=True)
    (cal / "2021-01-01_2021-01-30.json").write_text("[]", encoding="utf-8")
    (cal / "2021-01-31_2021-01-31.json").write_text("[]", encoding="utf-8")
    sb.build_split_table(store=str(store), out=str(out), db="unused", fmp_daily_dir=str(fmpd), fmp_from="2021-01-01",
                         log=lambda m: None)
    table = json.loads((out / "_meta" / "split_table.json").read_text(encoding="utf-8"))
    assert table["breaks"] == {"ZZ": [SESSIONS[30].date().isoformat()]} and "AA" in table["splits"]

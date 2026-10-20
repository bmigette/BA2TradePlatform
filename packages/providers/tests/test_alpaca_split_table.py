"""The verified split table: every branch of split_table.decide_splits on synthetic RAW bars."""
import json
from collections import Counter
from datetime import date, timedelta

import pandas as pd
import pytest

from ba2_providers.ohlcv.alpaca_history import split_table as st
from ba2_providers.ohlcv.alpaca_history.split_table import ListedSplit as L

SESSIONS = list(pd.bdate_range("2021-01-04", periods=80))
D = SESSIONS[40].date()            # the event session in most tests


def bars(jump_at=None, jump=1.0, price=100.0, sessions=SESSIONS, tz=True):
    """Flat raw bars; from sessions[jump_at] on the open/close are price / jump (close(D-1)/open(D) == jump)."""
    rows = []
    for i, d in enumerate(sessions):
        p = price / jump if (jump_at is not None and i >= jump_at) else price
        t = pd.Timestamp(d).tz_localize("UTC") + pd.Timedelta(hours=5) if tz else pd.Timestamp(d)
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


def test_shipped_overrides_file_loads_and_is_empty():
    assert st.load_overrides() == []


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
def _tool():
    import importlib.util
    import pathlib
    path = pathlib.Path(__file__).resolve().parents[3] / "tools" / "build_alpaca_split_table.py"
    spec = importlib.util.spec_from_file_location("build_alpaca_split_table", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fmp_calendar_windows_split_when_full_cache_and_check_dates(tmp_path):
    tool = _tool()
    calls = []

    def get(a, b):
        calls.append((a, b))
        n = tool.FMP_ROW_CAP if (b - a).days >= 10 else 2
        return [{"date": a.isoformat(), "symbol": f"S{i}", "numerator": 2, "denominator": 1} for i in range(n)]

    stats = Counter()
    rows = tool.fetch_fmp_calendar(date(2021, 1, 1), date(2021, 1, 30), str(tmp_path), get, stats)
    assert stats["fmp_windows_split"] >= 1 and all(b - a < timedelta(days=10) for a, b in calls[-2:])
    n_calls = len(calls)
    again = tool.fetch_fmp_calendar(date(2021, 1, 1), date(2021, 1, 30), str(tmp_path), get, Counter())
    assert len(calls) == n_calls and again == rows            # a rerun is offline

    def outside(a, b):
        return [{"date": "2000-01-01", "symbol": "X", "numerator": 2, "denominator": 1}]
    with pytest.raises(SystemExit):
        tool.fetch_fmp_calendar(date(2021, 2, 1), date(2021, 2, 5), str(tmp_path / "o"), outside, Counter())


def test_tool_refuses_a_pending_override(tmp_path, capsys):
    tool = _tool()
    p = write_ov(tmp_path, [ov("reject", reviewed_by="pending")])
    assert tool.main(["--overrides", p, "--out", str(tmp_path / "out")]) == 2
    assert "pending" in capsys.readouterr().err

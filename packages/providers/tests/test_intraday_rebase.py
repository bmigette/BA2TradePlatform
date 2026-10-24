"""The rebase rule (``intraday_rebase``), the reviewed exclusion list (``intraday_exclusions``) and the provenance /
state helpers of ``split_basis``.

The rule under test, in one place: prices of an off-basis segment are divided by the segment factor; volume is
multiplied by the factor ONLY when the factor is a product of split-calendar ratios (a real share split changes the
share count; a spin-off or an ADR-ratio change moves the price only), otherwise it is left alone and flagged
``volume_unadjusted``; anything that is not a clean level is refused with a code.

Run from ``packages/providers``:
    ...python.exe -m pytest tests/test_intraday_rebase.py -q -p no:cacheprovider
"""
from __future__ import annotations

import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from ba2_common.core import split_basis
from ba2_providers.ohlcv import cross_interval_basis as cib
from ba2_providers.ohlcv import intraday_exclusions as ix
from ba2_providers.ohlcv import intraday_rebase as rb

from .test_cross_interval_basis import FIRST, LAST, judge, make_pair, scale


@pytest.fixture(scope="module")
def pair():
    return make_pair()


def _plan(daily, intra, splits):
    t = cib.reduce_sessions(daily, intra, "5min")
    res = cib.classify(t, "SYN", cib.WHOLE_HISTORY[0], cib.WHOLE_HISTORY[1])
    plan = rb.plan_rebase(res, splits)
    plan.interval = "5min"
    return plan, res


# ------------------------------------------------------------------------------------------- the rule
def test_a_split_calendar_factor_is_snapped_and_volume_follows_the_share_split(pair):
    daily, intra = pair
    old = scale(intra, 2.003, before="2023-07-03")                    # measured 2.003: the 2-for-1 of 2024-03-01
    plan, res = _plan(daily, old, [(date(2024, 3, 1), 2.0)])
    assert res.klass == cib.KLASS_FACTOR_CHANGES
    [sg] = plan.segments
    assert sg.source == "split_calendar" and sg.factor == 2.0 and sg.measured_factor == pytest.approx(2.003, rel=2e-3)
    assert sg.volume_adjusted and not sg.volume_unadjusted and sg.calendar_splits == [("2024-03-01", 2.0)]
    out = rb.apply_plan(old, plan)
    early = out["Date"] < "2023-07-03"
    assert out.loc[early, "Close"].to_numpy() == pytest.approx((old.loc[early, "Close"] / 2.0).to_numpy())
    assert out.loc[early, "Volume"].to_numpy() == pytest.approx((old.loc[early, "Volume"] * 2.0).to_numpy())
    assert (out.loc[~early, ["Open", "Close", "Volume"]].to_numpy() == old.loc[~early, ["Open", "Close", "Volume"]].to_numpy()).all()
    assert rb.verify_rebased("SYN", "5min", out, daily).klass == cib.KLASS_OK       # the shared check agrees
    bars, sessions = rb.count_affected(old, plan)
    assert bars == int(early.sum()) and sessions > 100


def test_a_reverse_split_and_a_product_of_two_splits_match_the_calendar(pair):
    daily, intra = pair
    plan, _ = _plan(daily, scale(intra, 0.1, before="2023-07-03"), [(date(2024, 1, 5), 0.1)])
    assert plan.segments[0].source == "split_calendar" and plan.segments[0].factor == pytest.approx(0.1)
    plan, _ = _plan(daily, scale(intra, 6.01, before="2023-07-03"), [(date(2024, 1, 5), 2.0), (date(2024, 5, 6), 3.0)])
    assert plan.segments[0].source == "split_calendar" and plan.segments[0].factor == pytest.approx(6.0)
    assert [r for _, r in plan.segments[0].calendar_splits] == [2.0, 3.0]


def test_a_spin_off_factor_without_a_calendar_split_is_measured_only_and_volume_is_left_alone(pair):
    daily, intra = pair
    old = scale(intra, 1.325, before="2023-07-03")
    for splits in (None, [(date(2024, 1, 5), 2.0)]):                   # no calendar file / a calendar that does not match
        plan, _ = _plan(daily, old, splits)
        [sg] = plan.segments
        assert sg.source == "measured_only" and sg.volume_unadjusted and not sg.volume_adjusted
        out = rb.apply_plan(old, plan)
        early = old["Date"] < "2023-07-03"
        assert (out.loc[early, "Volume"].to_numpy() == old.loc[early, "Volume"].to_numpy()).all()          # untouched
        assert out.loc[early, "Close"].to_numpy() == pytest.approx((old.loc[early, "Close"] / sg.factor).to_numpy())
        assert rb.verify_rebased("SYN", "5min", out, daily).klass == cib.KLASS_OK
    assert plan.calendar_known is True and _plan(daily, old, None)[0].calendar_known is False


def test_a_calendar_split_BEFORE_the_segment_does_not_explain_it(pair):
    daily, intra = pair
    plan, _ = _plan(daily, scale(intra, 2.0, before="2023-07-03"), [(date(2022, 1, 5), 2.0)])
    assert plan.segments[0].source == "measured_only"


def test_a_constant_factor_over_the_whole_file_is_rebased_from_the_first_bar(pair):
    daily, intra = pair
    plan, res = _plan(daily, scale(intra, 0.5), [(date(2024, 1, 5), 0.5)])
    assert res.klass == cib.KLASS_CONSTANT_FACTOR and plan.segments[0].first_day == "" and plan.segments[0].last_day == ""


# ------------------------------------------------------------------------------------------- refusals
def _refused(daily, intra, splits=None):
    t = cib.reduce_sessions(daily, intra, "5min")
    res = cib.classify(t, "SYN", cib.WHOLE_HISTORY[0], cib.WHOLE_HISTORY[1])
    with pytest.raises(rb.IntradayRebaseRefused) as ei:
        rb.plan_rebase(res, splits)
    return ei.value.code


def test_refusals_carry_a_code(pair):
    daily, intra = pair
    assert _refused(daily, intra) == "ok"                                                   # nothing to do
    assert _refused(daily, scale(intra, 1.0165)) == "below_noise"                           # CRESY: 1.65% all year
    assert _refused(daily, scale(intra, 30.0)) == "implausible"                             # no calendar source, x30
    assert rb.plan_rebase(cib.classify(cib.reduce_sessions(daily, scale(intra, 30.0), "5min"), "SYN", *cib.WHOLE_HISTORY),
                          [(date(2024, 1, 5), 30.0)]).segments[0].source == "split_calendar"      # ... with one it is fine
    rng = np.random.default_rng(3)                                                          # a re-used ticker: no level
    bad = intra.copy()
    days = bad["Date"].dt.normalize()
    mult = days.map({d: float(np.exp(rng.uniform(-1.6, 1.6))) for d in days.unique()})
    for c in ("Open", "High", "Low", "Close"):
        bad[c] = bad[c] * mult
    assert _refused(daily, bad) == "noisy"
    # single bad bars (session high at the daily level) are not a basis
    out = intra.copy()
    for d in pd.bdate_range("2023-06-05", "2023-06-13"):
        m = out["Date"].dt.normalize() == d
        if m.any():
            last = out[m].index[-1]
            for c in ("Open", "Low", "Close"):
                out.loc[last, c] = out.loc[last, c] * 0.3
    assert _refused(daily, out) == "bad_prints"
    assert _refused(daily.iloc[:5], intra) == "insufficient"


def test_a_burst_with_scatter_is_refused_as_unclean(pair):
    daily, intra = pair
    rng = np.random.default_rng(5)
    out = intra.copy()
    days = [d for d in pd.bdate_range("2023-06-05", "2023-06-13")]
    for d in days:
        m = out["Date"].dt.normalize() == d
        f = 0.2 * float(np.exp(rng.normal(0, 0.06)))
        for c in ("Open", "High", "Low", "Close"):
            out.loc[m, c] = out.loc[m, c] * f
    assert _refused(daily, out) == "unclean"


# ------------------------------------------------------------------------------------------- the split calendar loader
def test_the_split_calendar_is_read_offline_from_the_warmed_file(tmp_path):
    d = tmp_path / "fmp_history"
    d.mkdir()
    (d / "mc_stock_split__ABC.json").write_text(json.dumps({"symbol": "ABC", "historical": [
        {"date": "2024-06-10", "numerator": 10, "denominator": 1}, {"date": "2021-01-04", "numerator": 3, "denominator": 2}]}))
    assert rb.load_split_calendar("abc", str(tmp_path)) == [(date(2021, 1, 4), 1.5), (date(2024, 6, 10), 10.0)]
    assert rb.load_split_calendar("ZZZ", str(tmp_path)) is None


# ------------------------------------------------------------------------------------------- provenance
def test_provenance_records_origin_and_check(pair):
    daily, intra = pair
    plan, res = _plan(daily, scale(intra, 2.0, before="2023-07-03"), [(date(2024, 3, 1), 2.0)])
    prov = rb.provenance_for(plan, daily_identity=(123, 456), backup="C:/b/SYN_5min.parquet",
                             post_check={"klass": "ok"})
    assert prov["kind"] == "rebased_by_tool" and prov["source"] == "split_calendar" and prov["tool_version"] == rb.TOOL_VERSION
    assert prov["daily_file_identity"] == [123, 456] and prov["segments"][0]["factor"] == 2.0 and prov["check_before"]["klass"]


def test_an_adhoc_note_is_adopted_verbatim_as_provenance():
    note = {"symbol": "AFCG", "applied_utc": "2026-10-08T19:14:32", "method": "OHLC / factor, Volume * factor, per segment",
            "source": "FMP stock_split calendar", "backup": "C:/b/AFCG_5min.parquet", "backup_sha256": "ab",
            "segments": [{"from": "2021-03-19", "to": "2024-07-10", "n": 831, "measured": 1.46074, "snap": 1.461,
                          "verdict": "clean calendar factor", "a": "1900-01-01", "b": "2024-07-10"},
                         {"from": "2024-07-10", "to": "2026-07-01", "n": 494, "measured": 1.0, "snap": 1.0,
                          "verdict": "already on daily basis", "a": "2024-07-10", "b": "2100-01-01"}]}
    prov = rb.provenance_from_note(note, "notes/AFCG_5min.json", post_check={"klass": "ok"}, daily_identity=(1, 2))
    assert prov["kind"] == "adopted_from_note" and prov["source"] == "split_calendar"
    assert [s["factor"] for s in prov["segments"]] == [1.461] and prov["segments"][0]["volume_adjusted"]
    assert prov["backup_sha256"] == "ab" and prov["note_file"] == "notes/AFCG_5min.json"


# ------------------------------------------------------------------------------------------- state helpers
def test_sidecars_markers_and_the_one_line_summary(tmp_path):
    folder = tmp_path / "FMPOHLCVProvider"
    folder.mkdir()
    a, b = str(folder / "AAA_5min.parquet"), str(folder / "BBB_5min.parquet")
    assert split_basis.intraday_state_summary(str(folder)).endswith("no stale-marked and no rebased intraday files")
    split_basis.write_intraday_stale(a, reason="daily replaced")
    split_basis.write_intraday_rebase(b, {"applied_utc": "2026-10-08", "source": "mixed",
                                          "segments": [{"volume_unadjusted": True}]})
    st = split_basis.list_intraday_states(str(folder))
    assert [r["file"] for r in st["stale"]] == ["AAA_5min"] and st["stale"][0]["age_days"] is not None
    assert st["rebased"] == [{"file": "BBB_5min", "applied_utc": "2026-10-08", "source": "mixed", "volume_unadjusted": True}]
    line = split_basis.intraday_state_summary(str(folder))
    assert "1 STALE-marked" in line and "AAA_5min" in line and "1 rebased" in line and "BBB_5min" in line
    assert split_basis.read_intraday_rebase(b)["source"] == "mixed" and split_basis.clear_intraday_rebase(b)
    # an unreadable marker is still a marker (never 'fresh'), and an unreadable sidecar still a rebase
    (folder / "_split_basis" / "AAA_5min.intraday-stale.json").write_text("{not json")
    assert "unreadable" in split_basis.read_intraday_stale(a)["reason"]


def test_the_reporter_logs_once_at_startup_and_stops(tmp_path):
    import threading
    got, evt = [], threading.Event()
    stop = split_basis.start_intraday_state_reporter(str(tmp_path), lambda m: (got.append(m), evt.set()), interval_s=3600)
    assert evt.wait(5) and "intraday cache state" in got[0]
    stop.set()


# ------------------------------------------------------------------------------------------- the exclusion list
def test_the_shipped_exclusion_list_is_valid_and_same_schema_as_the_panel_list():
    excl = ix.load_exclusions()
    assert isinstance(excl, dict)
    doc = json.load(open(ix.EXCLUSIONS_PATH, encoding="utf-8"))
    assert set(doc) == {"_doc", "entries"} and "pending" in doc["_doc"]


def test_exclusion_entries_need_all_four_fields_and_pending_is_not_in_force(tmp_path):
    p = tmp_path / "x.json"
    entry = {"symbol": "abc", "reason": "r", "added": "2026-10-08", "reviewed_by": "Bastien"}
    p.write_text(json.dumps({"entries": [entry, {**entry, "symbol": "DEF", "reviewed_by": "Pending owner review"}]}))
    excl = ix.load_exclusions(str(p))
    assert sorted(excl) == ["ABC", "DEF"]
    assert sorted(ix.in_force(excl)) == ["ABC"] and sorted(ix.pending(excl)) == ["DEF"]
    for missing in ("reason", "added", "reviewed_by"):
        bad = {k: v for k, v in entry.items() if k != missing}
        p.write_text(json.dumps({"entries": [bad]}))
        with pytest.raises(ix.ExclusionListError, match=missing):
            ix.load_exclusions(str(p))

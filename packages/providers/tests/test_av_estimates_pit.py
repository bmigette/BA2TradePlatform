"""Point-in-time Alpha Vantage EPS consensus: the pure rules on REAL recorded payloads (MSFT, AAPL, NVDA,
fetched 2026-11-07, key stripped) plus synthetic split cases. No network."""
import json
import pathlib
from datetime import date, datetime

import pytest

from ba2_common.core.estimates_pit import quarterly_consensus, split_factor_after

FIX = pathlib.Path(__file__).parent / "fixtures" / "alphavantage"
FETCHED = date(2026, 11, 7)
NVDA_SPLITS = [(date(2021, 7, 20), 4.0), (date(2024, 6, 10), 10.0)]
AAPL_SPLITS = [(date(2020, 8, 31), 4.0)]


def _load(sym):
    return (json.loads((FIX / f"earnings_estimates_{sym}.json").read_text()),
            json.loads((FIX / f"earnings_{sym}.json").read_text()))


def _q(sym, splits, as_of, fetched=FETCHED):
    est, earn = _load(sym)
    return {r["fiscal_period_end"]: r for r in quarterly_consensus(est, earn, splits, as_of, fetched)}


def test_fixtures_hold_no_api_key():
    for path in FIX.glob("*.json"):
        assert "apikey" not in path.read_text().lower()


def test_msft_picks_the_latest_snapshot_dated_on_or_before_as_of():
    # quarter 2022-12-31 reported 2023-01-24: snapshots 0d=01-24, 7d=01-17, 30d=12-25, 60d=11-25, 90d=10-26
    got = _q("MSFT", [], datetime(2022, 12, 1))["2022-12-31"]
    assert got["report_date"] == "2023-01-24"
    assert got["snapshot_age_days"] == 60 and got["snapshot_date"] == "2022-11-25"
    assert got["eps_avg"] == pytest.approx(2.29)               # the 60d-ago value, FMP's at-report 2.29 too
    assert got["eps_high"] is None and got["analyst_count"] is None   # AV keeps no history of those


def test_msft_before_the_oldest_snapshot_the_quarter_is_omitted_not_guessed():
    assert "2022-12-31" not in _q("MSFT", [], datetime(2022, 10, 20))     # 90d snapshot is 2022-10-26


def test_msft_a_reported_quarter_is_not_an_open_consensus():
    assert "2022-12-31" not in _q("MSFT", [], datetime(2023, 1, 24))


def test_msft_frozen_value_matches_the_reported_consensus():
    # 0-day snapshot of a quarter about to report: the avg that EARNINGS records as estimatedEPS (+-1%)
    got = _q("MSFT", [], datetime(2023, 1, 23))["2022-12-31"]
    assert got["snapshot_age_days"] == 7
    est, earn = _load("MSFT")
    rec = next(q for q in earn["quarterlyEarnings"] if q["fiscalDateEnding"] == "2022-12-31")
    assert got["eps_avg"] == pytest.approx(float(rec["estimatedEPS"]), rel=0.02)


def test_aapl_label_shifted_row_is_dropped():
    """AV row 2026-09-30 holds the NEXT quarter's number (2.90) while EARNINGS says the consensus was 1.98."""
    est, _ = _load("AAPL")
    row = next(r for r in est["estimates"] if r["date"] == "2026-09-30" and r["horizon"] == "fiscal quarter")
    assert float(row["eps_estimate_average"]) > 2.8
    assert "2026-09-30" not in _q("AAPL", AAPL_SPLITS, datetime(2026, 10, 15))


def test_aapl_pre_split_rows_are_divided_by_the_split_after_their_freeze_date():
    # quarter 2020-06-30 reported 2020-07-30, 4:1 split 2020-08-31 -> AV 2.04 on the old basis
    got = _q("AAPL", AAPL_SPLITS, datetime(2020, 7, 1))["2020-06-30"]
    assert got["split_factor"] == 4.0
    est, earn = _load("AAPL")
    rec = next(q for q in earn["quarterlyEarnings"] if q["fiscalDateEnding"] == "2020-06-30")
    assert got["eps_avg"] == pytest.approx(float(rec["estimatedEPS"]), rel=0.2)   # EARNINGS is split-adjusted


def test_nvda_two_splits_after_the_freeze_give_a_x40_factor():
    # 2021-04-30 quarter (reported 2021-05-26): 4:1 (2021-07) and 10:1 (2024-06) both follow
    got = _q("NVDA", NVDA_SPLITS, datetime(2021, 4, 30))["2021-04-30"]
    assert got["split_factor"] == 40.0
    assert got["eps_avg"] < 0.2                                     # raw AV values are ~2-3


def test_nvda_each_snapshot_is_on_the_basis_of_its_own_date():
    """Quarter 2024-07-31 reported 2024-08-28; 10:1 split 2024-06-10. Measured: the 90d value (2024-05-30) is
    pre-split (5.86), the 60d value (2024-06-29) post-split (0.63). Picking the 90d snapshot divides by 10;
    picking the 60d one by 1: both land on the same basis."""
    early = _q("NVDA", NVDA_SPLITS, datetime(2024, 6, 1))["2024-07-31"]     # only the 90d snapshot exists yet
    assert early["snapshot_age_days"] == 90 and early["snapshot_date"] == "2024-05-30"
    assert early["split_factor"] == 10.0 and early["eps_avg"] == pytest.approx(0.586, abs=0.001)
    later = _q("NVDA", NVDA_SPLITS, datetime(2024, 7, 15))["2024-07-31"]
    assert later["snapshot_age_days"] == 60 and later["split_factor"] == 1.0
    assert later["eps_avg"] == pytest.approx(0.63)
    assert abs(early["eps_avg"] / later["eps_avg"] - 1) < 0.15              # one basis, a plausible revision


def test_nvda_every_matched_quarter_agrees_with_the_split_adjusted_earnings_endpoint():
    """The whole point of the basis rule: over all of NVDA's history the adjusted frozen value is within the
    consistency band of EARNINGS' (split-adjusted) estimatedEPS -- no x4/x10/x40 leftovers."""
    est, earn = _load("NVDA")
    rep = {q["fiscalDateEnding"]: q for q in earn["quarterlyEarnings"]}
    checked = 0
    for r in est["estimates"]:
        q = rep.get(r["date"])
        if r["horizon"] != "fiscal quarter" or not q or q["estimatedEPS"] in (None, "None"):
            continue
        rd = date.fromisoformat(q["reportedDate"])
        adjusted = float(r["eps_estimate_average"]) / split_factor_after(NVDA_SPLITS, rd)
        e = float(q["estimatedEPS"])
        if abs(adjusted - e) <= max(0.25 * abs(e), 0.05):
            checked += 1
    assert checked >= 35


def _synthetic(report="2024-07-10", fetched=date(2024, 8, 1), ages=(2.0, 2.0, 2.0, 2.0, 2.0)):
    avg, a7, a30, a60, a90 = ages
    est = {"estimates": [{
        "date": "2024-06-30", "horizon": "fiscal quarter", "eps_estimate_average": str(avg),
        "eps_estimate_high": "2.2", "eps_estimate_low": "1.8", "eps_estimate_analyst_count": "10",
        "eps_estimate_average_7_days_ago": str(a7), "eps_estimate_average_30_days_ago": str(a30),
        "eps_estimate_average_60_days_ago": str(a60), "eps_estimate_average_90_days_ago": str(a90)}]}
    earn = {"quarterlyEarnings": [{"fiscalDateEnding": "2024-06-30", "reportedDate": report,
                                   "estimatedEPS": str(avg), "reportedEPS": "2.1"}]}
    return est, earn


def test_split_between_the_snapshot_dates_each_side_is_put_on_todays_basis():
    # report 2024-07-10 (0d=07-10, 7d=07-03, 30d=06-10, 60d=05-11, 90d=04-11); 2:1 split ex 2024-06-20 sits
    # between the 30d and 7d snapshots. Raw AV (as-then): 90d/60d/30d on the old basis (x2), 7d/0d new basis.
    est, earn = _synthetic(ages=(1.05, 1.0, 2.0, 1.96, 1.9))
    splits = [(date(2024, 6, 20), 2.0)]
    pre = quarterly_consensus(est, earn, splits, datetime(2024, 6, 15), date(2024, 8, 1))
    assert len(pre) == 1 and pre[0]["snapshot_age_days"] == 30 and pre[0]["split_factor"] == 2.0
    assert pre[0]["eps_avg"] == pytest.approx(1.0)                  # 2.0 old basis -> 1.0 today
    post = quarterly_consensus(est, earn, splits, datetime(2024, 7, 5), date(2024, 8, 1))
    assert post[0]["snapshot_age_days"] == 7 and post[0]["split_factor"] == 1.0
    assert post[0]["eps_avg"] == pytest.approx(1.0)                 # same basis on both sides of the split


def test_a_split_after_the_freeze_date_divides_every_snapshot():
    est, earn = _synthetic(ages=(2.1, 2.05, 2.0, 1.96, 1.9))
    earn["quarterlyEarnings"][0]["estimatedEPS"] = "1.05"          # EARNINGS is split-adjusted
    out = quarterly_consensus(est, earn, [(date(2024, 8, 15), 2.0)], datetime(2024, 7, 5), date(2024, 9, 1))
    assert out[0]["split_factor"] == 2.0 and out[0]["eps_avg"] == pytest.approx(1.025)    # 7d value 2.05 / 2


def test_a_snapshot_av_adjusted_after_all_is_omitted_not_double_divided():
    """If the ago-values were ALREADY on today's basis, dividing again would be wrong; the cross-check against the
    freeze-day value catches it and the older snapshot is dropped (the newest one is still used)."""
    est, earn = _synthetic(ages=(1.05, 1.0, 1.0, 0.98, 0.95))      # 30d value NOT scaled by the split
    splits = [(date(2024, 6, 20), 2.0)]
    pre = quarterly_consensus(est, earn, splits, datetime(2024, 6, 15), date(2024, 8, 1))
    assert pre == []                                                # 1.0/2 = 0.5 vs 1.05: > 2x off -> omitted


def test_unusable_split_ratio_refuses():
    with pytest.raises(ValueError):
        split_factor_after([(date(2025, 1, 1), None)], date(2024, 1, 1))


def test_live_and_historical_go_through_the_same_function_and_agree():
    """Live (as_of None) is the same call as as_of = the fetch day: one basis, one result."""
    est, earn = _load("AAPL")
    live = quarterly_consensus(est, earn, AAPL_SPLITS, None, FETCHED)
    hist = quarterly_consensus(est, earn, AAPL_SPLITS, datetime(2026, 11, 7), FETCHED)
    assert live == hist and live                                    # next unreported quarters, no reported one
    assert all(r["report_date"] is None and r["label_verified"] is False for r in live)
    assert all(r["snapshot_date"] == "2026-11-07" and r["split_factor"] == 1.0 for r in live)


def test_live_row_with_a_recent_split_is_on_the_post_split_basis():
    """Live: the quarter has no report date yet (frozen 'now' = fetch day). A split 20 days ago sits between its
    30d snapshot and now: the current value is used as is; an older snapshot would be divided by the split."""
    est, earn = _synthetic()
    earn["quarterlyEarnings"] = []                                 # unreported
    out = quarterly_consensus(est, earn, [(date(2024, 7, 12), 2.0)], None, date(2024, 8, 1))
    assert out[0]["snapshot_age_days"] == 0 and out[0]["eps_avg"] == pytest.approx(2.0)
    assert out[0]["eps_high"] == pytest.approx(2.2)                # current high/low/count exist for the 0d value
    assert out[0]["analyst_count"] == 10


def test_a_quarter_with_no_report_date_is_omitted_for_a_historical_as_of():
    est, earn = _synthetic()
    earn["quarterlyEarnings"] = []
    assert quarterly_consensus(est, earn, [], datetime(2024, 7, 1), date(2024, 8, 1)) == []


def test_malformed_payloads_refuse():
    with pytest.raises(ValueError):
        quarterly_consensus({"Information": "rate limit"}, {"quarterlyEarnings": []}, [], None, FETCHED)
    with pytest.raises(ValueError):
        quarterly_consensus({"estimates": []}, {"x": 1}, [], None, FETCHED)

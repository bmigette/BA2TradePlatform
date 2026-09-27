"""SYMBOL360 as an at-a-glance verdict: the pure view-model.

Operator ask: "cards with a score / trend -- (strong) buy / (strong) sell / hold -- and
overall ... I don't need the functions or weights". Overall is a TALLY of the cards (their
choice), and "Show details" carries the underlying facts, never scoring arithmetic.

The fixtures mirror the real shapes the experts export (``raw`` is the expert's
``raw_outputs`` verbatim): DeterministicScorer's technical components / fundamental
snapshot+evidence / regime, FMPRating's ``calc``, Finnhub's ``counts``, the earnings
``evaluation`` and the insider ``cluster``.
"""
from datetime import date
from types import SimpleNamespace

import pytest

from ba2_trade_platform.ui.utils import symbol360_view as v

TODAY = date(2026, 9, 27)


def _export(raw=None, *, error=None, skipped=False, settings=None):
    return SimpleNamespace(raw=raw or {}, error=error, skipped=skipped,
                           settings_used=settings or {})


def _ds(*, dist=None, mom=None, rsi=None, brk=None, adx=None, roe=None, fscore=None,
        z=None, ey=None, value_norm=None, rev=None, eps=None, regime=None):
    comps = {}
    if dist is not None:
        comps["dist_sma_trend"] = {"weight": 1, "raw": dist, "normalized": 0}
    if mom is not None:
        comps["momentum_vol_adj"] = {"weight": 1, "raw": 1, "normalized": mom}
    if rsi is not None:
        comps["rsi_meanrev"] = {"weight": 1, "raw": rsi, "normalized": 0}
    if brk is not None:
        comps["donchian_breakout"] = {"weight": 1, "raw": brk, "normalized": brk}
    snap, ev = {}, {"growth": {}}
    if roe is not None:
        ev["quality"] = {"roe": roe, "net_income": 1e9, "equity": 1e9 / roe if roe else 1}
    if fscore is not None:
        snap["fscore"] = fscore
    if z is not None:
        snap["z"] = z
    if ey is not None:
        ev["value"] = {"earnings_yield": ey, "enterprise_value": 5e10}
        snap["value_norm"] = value_norm
    if rev is not None:
        ev["growth"]["revenue"] = rev
    if eps is not None:
        ev["growth"]["eps"] = eps
    raw = {"technical": {"components": comps, "adx": adx, "trending": bool(adx and adx > 25)},
           "fundamental": {"snapshot": snap, "evidence": ev}}
    if regime is not None:
        raw["regime"] = {"score": regime}
    return _export(raw, settings={"sma_trend_period": 200})


# ---------------------------------------------------------------------------
# The tally
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("good,bad,total,expected", [
    (3, 0, 3, v.STRONG_BUY),
    (2, 0, 4, v.BUY),
    (1, 1, 2, v.HOLD),
    (0, 2, 4, v.SELL),
    (0, 3, 3, v.STRONG_SELL),
])
def test_the_balance_maps_to_a_verdict(good, bad, total, expected):
    assert v.verdict_from_balance(good, bad, total) == expected


def test_only_neutral_lines_abstain_rather_than_vote_hold():
    """'No insider activity' is not evidence the stock is a Hold. Letting it vote would drag
    every overall towards Hold for symbols that merely have less coverage."""
    assert v.verdict_from_balance(0, 0, 3) is None


def test_neutral_lines_dilute_a_verdict():
    """Two good observations among five is a weaker case than two among two."""
    assert v.verdict_from_balance(2, 0, 2) == v.STRONG_BUY
    assert v.verdict_from_balance(2, 0, 5) == v.BUY


def _card(verdict_lines, votes=True, unavailable=None):
    return v.SummaryCard("k", "t", lines=[v.Line(s, "x") for s in verdict_lines],
                         votes=votes, unavailable=unavailable)


def test_overall_is_one_vote_per_card():
    cards = [_card([v.GOOD]), _card([v.GOOD]), _card([v.GOOD, v.BAD]), _card([v.BAD])]

    overall = v.overall_from_cards(cards)

    assert (overall.bullish, overall.neutral, overall.bearish) == (2, 1, 1)
    # net (2 - 1) / 4 = 0.25: more areas bullish than bearish, so it leans Buy.
    assert overall.verdict == v.BUY


def test_an_even_split_is_hold():
    cards = [_card([v.GOOD]), _card([v.BAD]), _card([v.GOOD, v.BAD])]

    assert v.overall_from_cards(cards).verdict == v.HOLD


def test_context_and_abstaining_cards_do_not_vote():
    cards = [_card([v.GOOD]), _card([v.BAD], votes=False),
             _card([v.NEUTRAL]), _card([v.GOOD], unavailable="no data")]

    overall = v.overall_from_cards(cards)

    assert overall.voting == 1
    assert overall.abstained == 2
    assert overall.verdict == v.STRONG_BUY


def test_the_answer_is_yes_only_for_a_buy():
    """Hold is NO: no clear edge is not an opportunity."""
    assert v.overall_from_cards([_card([v.GOOD]), _card([v.GOOD])]).answer == "YES"
    assert v.overall_from_cards([_card([v.GOOD]), _card([v.BAD])]).answer == "NO"
    assert v.overall_from_cards([_card([v.NEUTRAL])]).answer is None


def test_the_summary_reads_like_a_sentence():
    overall = v.overall_from_cards([_card([v.GOOD]), _card([v.GOOD]), _card([v.BAD])])

    assert overall.summary.startswith("2 of 3 areas bullish")


# ---------------------------------------------------------------------------
# Trend & Momentum
# ---------------------------------------------------------------------------

def test_a_clean_uptrend_reads_strong_buy():
    card = v.build_trend_card(_ds(dist=0.12, mom=0.5, rsi=55, brk=0.8),
                              {"stage": 2}, {})

    assert card.verdict == v.STRONG_BUY
    assert any("12.0% above its 200-day average" in ln.text for ln in card.lines)


def test_overbought_rsi_is_a_bad_line_with_the_number_in_it():
    card = v.build_trend_card(_ds(rsi=78), None, {})

    assert card.lines[0].status == v.BAD
    assert "RSI 78" in card.lines[0].text and "overbought" in card.lines[0].text


def test_the_trend_period_comes_from_the_experts_own_setting():
    ds = _ds(dist=0.1)
    ds.settings_used["sma_trend_period"] = 150

    card = v.build_trend_card(ds, None, {})

    assert "150-day average" in card.lines[0].text


def test_an_unmeasured_leg_is_omitted_not_scored_zero():
    card = v.build_trend_card(_ds(dist=0.1), None, {})

    assert len(card.lines) == 1


def test_a_failed_export_makes_the_card_abstain_and_say_why():
    card = v.build_trend_card(_export(error="401 Client Error: Unauthorized\nmore"), None, {})

    assert card.verdict is None
    assert card.unavailable.startswith("Unavailable: 401 Client Error")


def test_details_carry_the_raw_readings_not_the_scoring_math():
    card = v.build_trend_card(_ds(dist=0.12, rsi=55, adx=31),
                              {"stage": 2}, {"priceAvg200": 150.0, "yearHigh": 199.0})
    labels = dict(card.facts)

    assert labels["RSI (14)"] == "55.0"
    assert labels["200-day average"] == "$150.00"
    assert "trending" in labels["Trend strength (ADX)"]
    assert not any("tanh" in f"{k}{val}" or "weight" in f"{k}{val}" for k, val in card.facts)


# ---------------------------------------------------------------------------
# Fundamentals and valuation
# ---------------------------------------------------------------------------

def test_healthy_fundamentals():
    card = v.build_fundamentals_card(_ds(roe=0.28, fscore=8, z=4.1))

    assert card.verdict == v.STRONG_BUY
    assert any("28.0%" in ln.text for ln in card.lines)


def test_distress_is_called_out():
    card = v.build_fundamentals_card(_ds(roe=-0.05, fscore=2, z=1.2))

    assert card.verdict == v.STRONG_SELL
    assert any("distress" in ln.text for ln in card.lines)


def test_growth_accelerating_vs_shrinking():
    card = v.build_valuation_card(
        _ds(rev={"latest_growth": 0.12, "acceleration": 0.03, "trailing_mean": 0.09},
            eps={"latest_growth": -0.08, "acceleration": -0.1, "trailing_mean": 0.02}),
        {"pe": 24.5})
    texts = {ln.status: ln.text for ln in card.lines}

    assert "Revenue growing 12.0% and accelerating" in texts[v.GOOD]
    assert "Earnings per share shrinking" in texts[v.BAD]
    assert dict(card.facts)["P/E ratio"] == "24.5"


def test_slowing_growth_is_neutral_and_says_from_what():
    card = v.build_valuation_card(
        _ds(rev={"latest_growth": 0.05, "acceleration": -0.04, "trailing_mean": 0.09}), {})

    assert card.lines[0].status == v.NEUTRAL
    assert "slowing from 9.0%" in card.lines[0].text


# ---------------------------------------------------------------------------
# Analysts
# ---------------------------------------------------------------------------

def _fmp(**calc):
    return _export({"calc": calc})


def test_analyst_consensus_and_upside():
    card = v.build_analyst_card(
        {"fmp": _fmp(analyst_count=30, strong_buy=10, buy=12, hold=6, sell=2, strong_sell=0,
                     target_consensus=220.0, target_high=260.0, target_low=170.0),
         "finnhub": None, "price_targets": []},
        current_price=190.0)

    assert card.verdict == v.STRONG_BUY
    assert any("22 of 30 analysts rate it Buy (73%)" == ln.text for ln in card.lines)
    assert any("+16% upside" in ln.text for ln in card.lines)
    facts = dict(card.facts)
    assert facts["  Strong Buy"] == "10"
    assert facts["High target"].startswith("$260.00")


def test_finnhub_is_the_fallback_when_fmp_has_no_coverage():
    card = v.build_analyst_card(
        {"fmp": _export(error="401"),
         "finnhub": _export({"counts": {"strongBuy": 1, "buy": 1, "hold": 5, "sell": 3,
                                        "strongSell": 1}})},
        current_price=None)

    assert card.lines[0].status == v.BAD
    assert "Finnhub" in dict(card.facts)["Analysts covering"]


def test_dated_price_targets_become_a_details_table_newest_first():
    card = v.build_analyst_card(
        {"fmp": _fmp(analyst_count=1, buy=1),
         "price_targets": [
             {"publishedDate": "2026-08-01T00:00", "analystCompany": "A", "analystName": "x",
              "priceTarget": 200},
             {"publishedDate": "2026-09-01T00:00", "analystCompany": "B", "analystName": "y",
              "priceTarget": 210}]},
        current_price=190.0)

    table = card.tables[0]
    assert table.columns == ["Date", "Firm", "Analyst", "Target"]
    assert table.rows[0][0] == "2026-09-01"


def test_no_coverage_abstains():
    card = v.build_analyst_card({"fmp": _fmp(analyst_count=0), "finnhub": None}, 100.0)

    assert card.verdict is None


# ---------------------------------------------------------------------------
# Earnings, insiders and congress
# ---------------------------------------------------------------------------

def test_an_earnings_beat_inside_the_drift_window():
    card = v.build_earnings_card(_export({"evaluation": {
        "surprise_pct": 8.4, "days_since_report": 12, "is_signal": True,
        "report_date": "2026-09-15", "reported_eps": 1.52, "estimated_eps": 1.40}}))

    assert card.verdict == v.STRONG_BUY
    assert "Beat estimates by 8.4% last quarter (12 days ago)" == card.lines[0].text


def test_a_miss():
    card = v.build_earnings_card(_export({"evaluation": {"surprise_pct": -6.0}}))

    assert card.verdict == v.STRONG_SELL


def test_an_insider_cluster_is_good_and_routine_selling_is_not_bad():
    card = v.build_insider_card(
        _export({"cluster": {"is_cluster": True, "buyer_count": 4, "buy_value": 2.5e6,
                             "sell_value": 9e7, "buyers": {"CEO": 2e6, "CFO": 5e5}}}),
        None, TODAY)

    assert [ln.status for ln in card.lines] == [v.GOOD]
    assert card.tables[0].rows[0][0] == "CEO"


def test_congress_counts_only_the_recent_window():
    congress = {"senate": [
        {"type": "Purchase", "transactionDate": "2026-08-01", "firstName": "A", "lastName": "B"},
        {"type": "Purchase", "transactionDate": "2026-07-01", "firstName": "C", "lastName": "D"},
        {"type": "Sale", "transactionDate": "2020-01-01", "firstName": "E", "lastName": "F"},
    ], "house": []}

    card = v.build_insider_card(None, congress, TODAY)

    assert card.lines[0].status == v.GOOD
    assert "2 buys vs 0 sales" in card.lines[0].text
    assert len(card.tables[0].rows) == 3, "the details table keeps the full history"


def test_no_activity_at_all_abstains():
    card = v.build_insider_card(
        _export({"cluster": {"is_cluster": False, "buyer_count": 0}}),
        {"senate": [], "house": []}, TODAY)

    assert card.verdict is None


# ---------------------------------------------------------------------------
# The whole page
# ---------------------------------------------------------------------------

def test_the_backdrop_never_votes():
    card = v.build_backdrop_card(_ds(regime=-0.8), {"rvol": 2.0})

    assert card.votes is False
    assert any("risk-off" in ln.text for ln in card.lines)


def test_build_summary_end_to_end():
    results = {
        "header": {"quote": {"price": 190.0, "changesPercentage": 1.25},
                   "profile": {"companyName": "Apple Inc.", "sector": "Technology"}},
        "weinstein": {"stage": 2},
        "detscorer": _ds(dist=0.1, mom=0.4, rsi=60, roe=0.3, fscore=8, z=5.0,
                         ey=0.05, value_norm=-0.3, regime=0.2),
        "analyst": {"fmp": _fmp(analyst_count=20, buy=15, hold=5, target_consensus=215.0)},
        "earnings": _export({"evaluation": {"surprise_pct": 4.0, "days_since_report": 20}}),
        "insider": _export({"cluster": {"is_cluster": False, "buyer_count": 0}}),
        "congress": {"senate": [], "house": []},
        "rvol": {"rvol": 1.1},
    }

    header, cards, backdrop, overall = v.build_summary("AAPL", results, TODAY)

    assert header["name"] == "Apple Inc."
    assert header["change"] == "+1.25%"
    assert [c.key for c in cards] == ["trend", "fundamentals", "valuation", "factors",
                                      "analysts", "earnings", "insiders"]
    assert overall.answer == "YES"
    assert overall.abstained == 2          # no factor data; no insider/congress activity
    assert backdrop.votes is False


def test_one_card_that_cannot_be_built_costs_that_card_not_the_page(monkeypatch):
    """A provider payload in an unexpected shape used to raise out of build_summary and
    blank the whole page (Piotroski components arrived as a list, the builder expected a
    dict). Now that card abstains and says why; every other card still renders."""
    def _boom(ds_export):
        raise AttributeError("'list' object has no attribute 'items'")

    monkeypatch.setattr(v, "build_fundamentals_card", _boom)

    _, cards, _, overall = v.build_summary(
        "AAPL", {"detscorer": _ds(dist=0.1, rsi=50), "weinstein": {"stage": 2}}, TODAY)

    by_key = {c.key: c for c in cards}
    assert "AttributeError" in by_key["fundamentals"].unavailable
    assert by_key["fundamentals"].verdict is None
    assert by_key["trend"].verdict == v.STRONG_BUY
    assert overall.abstained >= 1


# ---------------------------------------------------------------------------
# Piotroski components -- the shape DeterministicScorer really emits
# ---------------------------------------------------------------------------

def test_piotroski_components_are_a_list_of_tests():
    ds = _ds(fscore=7)
    ds.raw["fundamental"]["evidence"]["piotroski"] = {
        "score": 7, "computed": 8,
        "components": [
            {"name": "roa_positive", "rule": "ROA > 0", "passed": True,
             "current": 0.12, "comparator": 0},
            {"name": "leverage_down", "rule": "LTD/assets fell", "passed": False,
             "current": 0.3, "comparator": 0.25},
            {"name": "no_dilution", "rule": "shares <= prior", "passed": None,
             "current": None, "comparator": None},
        ],
    }

    facts = dict(v.build_fundamentals_card(ds).facts)

    assert facts["  roa positive"] == "pass"
    assert facts["  leverage down"] == "fail"
    assert facts["  no dilution"] == "n/a"      # not computable is never a fail


# ---------------------------------------------------------------------------
# FactorRanker's factors, judged on absolute bars
# ---------------------------------------------------------------------------

def _factors(*, mom=None, eps=None, price=None, fcf=None, ev=None, roe=None, gp=None,
             assets=None, accruals=None):
    return {
        "momentum_12_1": mom,
        "value": {"eps_ttm": eps, "price": price, "fcf_ttm": fcf, "enterprise_value": ev},
        "quality": {"roe": roe, "gross_profit": gp, "total_assets": assets,
                    "accruals_ratio": accruals},
    }


def test_a_cheap_profitable_momentum_stock_reads_strong_buy():
    card = v.build_factors_card(_factors(mom=0.25, eps=10.0, price=100.0, fcf=8e9, ev=1e11,
                                         roe=0.25, gp=4e10, assets=1e11, accruals=-0.03))

    assert card.verdict == v.STRONG_BUY
    texts = " | ".join(ln.text for ln in card.lines)
    assert "+25.0%" in texts and "P/E 10.0" in texts and "8.0%" in texts
    assert "backed by operating cash flow" in texts


def test_losses_cash_burn_and_accruals_are_bad():
    card = v.build_factors_card(_factors(mom=-0.3, eps=-2.0, price=50.0, fcf=-1e9, ev=2e10,
                                         roe=-0.1, gp=5e8, assets=2e10, accruals=0.15))

    assert card.verdict == v.STRONG_SELL
    texts = " | ".join(ln.text for ln in card.lines)
    assert "loss-making" in texts and "burning cash" in texts
    assert "ahead of operating cash flow" in texts


def test_an_unmeasured_factor_leg_is_omitted():
    """A non-positive EV or missing FCF is not a 0% yield -- FactorRanker drops that
    leg, and so does the card."""
    card = v.build_factors_card(_factors(mom=0.2, fcf=1e9, ev=-5e9))

    assert len(card.lines) == 1
    assert not any("cash-flow" in ln.text for ln in card.lines)


def test_no_factor_data_abstains():
    assert v.build_factors_card(None).verdict is None
    assert v.build_factors_card(_factors()).unavailable

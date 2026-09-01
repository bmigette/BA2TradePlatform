"""FMPEarningsEvent — the cache-fed core (options-grid2 Task 7) plus the implied-move
leg (Task 8), design §9.

The fixtures mirror the SHAPES measured against the real FMP disk cache on
2026-08-31 / verified again 2026-09-01:

  * ``past_earnings_quarterly__<SYM>.json`` is a LIST of rows
    {date, symbol, eps, epsEstimated, time, revenue, revenueEstimated,
     updatedFromDate, fiscalDateEnding} — 11-12 events per symbol over three
    years, eps+epsEstimated populated on >=90% of names in ALL cap bands, and a
    ``time`` slot distribution of bmo 4,935 / amc 2,505 / '--' 663 / missing 288
    over 8,391 rows sampled from 120 files. The SBET-class outlier (1 of 12 rows
    with usable data) is reproduced verbatim below.
  * ``earnings_estimates_quarterly__<SYM>.json`` carries ``numberAnalystsEstimatedEps``
    (plural "Analysts" — the provider used to read a singular spelling that appears
    in no payload, so the count was silently 0 everywhere).

Everything runs through the REAL ``FMPCompanyDetailsProvider`` with only
``fmp_history_disk_cached`` patched, so the provider's own end_date filtering,
row mapping and (new) announcement-slot passthrough are exercised rather than
re-implemented by a fake.
"""
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd
import pytest

from ba2_common.core.backtest_context import BacktestContext
from ba2_common.core.instance_resolver import set_instance_resolver
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight, OrderRecommendation
from ba2_experts.FMPEarningsEvent import (
    FMPEarningsEvent, composite_confidence, compute_features, earnings_day_move_pct,
    implied_move_pct, normalize_feature,
)
from ba2_providers.fundamentals.details.FMPCompanyDetailsProvider import (
    FMPCompanyDetailsProvider,
)

AS_OF = datetime(2025, 6, 10, tzinfo=timezone.utc)          # a Tuesday
UPCOMING_DAY = "2025-06-16"                                  # a Monday, 6 days out

BASE_SETTINGS = {
    "earnings_days_look": 10,
    "min_hist_events": 4,
    "min_analysts": 3,
    "allow_unconfirmed_dates": False,
    "w_hist_move": 1.0,
    "w_surprise_vol": 1.0,
    "w_vol_cheapness": 1.0,
}

#: Past quarterly print dates, newest first — all Mondays, ~3 months apart, so no
#: event's reacting session can ever be another event's reference session.
PAST_DAYS = ["2025-03-17", "2024-12-16", "2024-09-16", "2024-06-17",
             "2024-03-18", "2023-12-18", "2023-09-18", "2023-06-19",
             "2023-03-20", "2022-12-19", "2022-09-19", "2022-06-20"]


# ---------------------------------------------------------------- fixtures ---
def _row(day, *, eps=1.20, est=1.00, slot="amc", symbol="TSTX"):
    """One raw ``past_earnings_quarterly`` row in the measured cache shape."""
    return {"date": day, "symbol": symbol, "eps": eps, "epsEstimated": est,
            "time": slot, "revenue": 5_000_000_000, "revenueEstimated": 4_900_000_000,
            "updatedFromDate": "2026-08-07", "fiscalDateEnding": day}


def _prow(day, *, eps=1.20, est=1.00, slot="amc"):
    """One row in the PROVIDER's mapped shape (what ``get_past_earnings`` emits) —
    used where a pure helper is called directly, without the provider in the loop."""
    return {"fiscal_date_ending": day, "report_date": day,
            "reported_eps": eps, "estimated_eps": est, "time": slot}


def _weekdays(start: str, end: str):
    d = datetime.strptime(start, "%Y-%m-%d")
    last = datetime.strptime(end, "%Y-%m-%d")
    out = []
    while d <= last:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def _next_weekday(day: str) -> str:
    d = datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _bars_df(jumps=None, start="2022-01-03", end="2025-06-10"):
    """A flat 100.0 daily close series with single-day closes overridden.

    Because only the REACTING session of an event is moved, and events sit a
    quarter apart, each injected jump is read by exactly one event."""
    jumps = jumps or {}
    days = _weekdays(start, end)
    closes = [float(jumps.get(d, 100.0)) for d in days]
    return pd.DataFrame({"Date": pd.to_datetime(days, utc=True), "Close": closes})


def _estimates_payload(n_analysts=8):
    """One ``earnings_estimates_quarterly`` row (FMP serves these ANNUAL)."""
    return [{"symbol": "TSTX", "date": "2025-12-31", "estimatedEpsAvg": 5.0,
             "estimatedEpsHigh": 5.4, "estimatedEpsLow": 4.6,
             "numberAnalystEstimatedRevenue": 7,
             "numberAnalystsEstimatedEps": n_analysts}]


class _Bundle:
    """ProviderBundle over the real details provider + a canned OHLCV frame."""

    def __init__(self, df):
        self._df = df
        self.ohlcv_calls = []
        p = FMPCompanyDetailsProvider.__new__(FMPCompanyDetailsProvider)
        p.api_key = "fake-key"
        self._details = p

    def fundamentals_details(self):
        return self._details

    def ohlcv(self):
        outer = self

        class _O:
            def get_ohlcv_data(self, symbol, end_date=None, lookback_days=400, interval="1d"):
                outer.ohlcv_calls.append((symbol, end_date, lookback_days))
                return outer._df
        return _O()

    def price_at_date(self, symbol, as_of):
        return 100.0


def _run(earnings_rows, *, settings=None, df=None, estimates=None, as_of=AS_OF,
        account=None):
    """Drive the real _gather + _process with the provider's disk layer patched.

    ``account`` (Task 8) becomes ``context.account`` -- exactly what a real backtest
    run would pass. Omitted (None), it reproduces the pre-Task-8 environment: no
    chain capability, so vol_cheapness stays absent."""
    settings = {**BASE_SETTINGS, **(settings or {})}
    bundle = _Bundle(df if df is not None else _bars_df())

    def _fake_cache(namespace, symbol, fetch_fn, *a, **kw):
        if namespace.startswith("past_earnings"):
            return earnings_rows
        if namespace.startswith("earnings_estimates"):
            return estimates if estimates is not None else _estimates_payload()
        raise AssertionError(f"unexpected namespace {namespace!r}")

    e = FMPEarningsEvent.__new__(FMPEarningsEvent)
    e.id = 1
    with patch("ba2_providers.fundamentals.details.FMPCompanyDetailsProvider."
               "fmp_history_disk_cached", side_effect=_fake_cache):
        ctx = BacktestContext(providers=bundle, settings=settings, as_of=as_of,
                              extra={"symbol": "TSTX"}, account=account)
        rec = e.analyze_as_of(as_of, ctx)
    return rec, bundle


def _healthy_rows(*, upcoming_slot="amc", n_past=8, jump_pct=4.0):
    """An upcoming event plus ``n_past`` clean past prints, and matching bars.

    Each past print is 'amc', so its reacting session is the NEXT weekday; that is
    the only bar moved. Surprises alternate so surprise_vol is non-degenerate.
    """
    rows = [_row(UPCOMING_DAY, eps=None, est=1.30, slot=upcoming_slot)]
    jumps = {}
    for i, day in enumerate(PAST_DAYS[:n_past]):
        est = 1.00
        eps = 1.00 + (0.10 if i % 2 == 0 else 0.30)      # +10% / +30% surprises
        rows.append(_row(day, eps=eps, est=est, slot="amc"))
        jumps[_next_weekday(day)] = 100.0 * (1.0 + jump_pct / 100.0)
    return rows, _bars_df(jumps)


#: date(UPCOMING_DAY) -- the event date every Task 8 fixture below prices its
#: straddle "at or after".
EVENT_DATE = date(2025, 6, 16)


def _opt(strike, expiry, right, *, price=None, bid=None, ask=None):
    """One ``OptionContract`` chain row. ``price`` sets bid==ask (a usable two-sided
    mid); passing neither ``price`` nor ``bid``/``ask`` leaves both None, i.e. an
    unusable ('missing leg') quote -- exactly the shape a real feed produces for a
    strike with no market."""
    if price is not None:
        bid = ask = price
    return OptionContract(symbol=f"TSTX{expiry.isoformat()}{right.value}{strike:g}",
                          underlying="TSTX", option_type=right, strike=float(strike),
                          expiry=expiry, bid=bid, ask=ask)


class _StubAccount:
    """Duck-typed Task 8 chain seam test double -- publishes ONLY get_option_chain,
    exactly like the real duck-typed contract this expert consumes. Records every
    call so the PERF ("one chain read per symbol-with-upcoming-event") and
    point-in-time claims can be pinned by counting/inspecting ``calls``."""

    def __init__(self, contracts=None, raises: Exception = None):
        self._contracts = contracts if contracts is not None else []
        self._raises = raises
        self.calls = []

    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type=None,
                         strike_min=None, strike_max=None):
        self.calls.append((underlying, expiry_min, expiry_max))
        if self._raises is not None:
            raise self._raises
        return self._contracts


class _NoChainAccount:
    """An account that plainly does not support options -- no get_option_chain at
    all, the same shape a stock-only broker account has."""


#: A single ATM straddle at the event date: call 2.00 + put 1.00 = 3.00 on spot 100
#: -> implied_move_pct = 3.0. Paired with hist_move=6.0 (via _healthy_rows(jump_pct=
#: 6.0)) this is EXACTLY the plan's hand-derived example: vol_cheapness = 6/3 = 2.0,
#: n(2.0) = 2/(2+1) = 2/3 at k=1.0.
GOOD_STRADDLE_CONTRACTS = [
    _opt(100.0, EVENT_DATE, OptionRight.CALL, price=2.00),
    _opt(100.0, EVENT_DATE, OptionRight.PUT, price=1.00),
]


# ------------------------------------------------------------- happy path ---
def test_ranks_a_qualifying_upcoming_event():
    rows, df = _healthy_rows()
    rec, _ = _run(rows, df=df)
    assert not rec.skip, rec.skip_reason
    assert rec.signal == OrderRecommendation.BUY
    assert 1.0 <= rec.confidence <= 100.0
    payload = rec.raw_outputs["FMPEarningsEvent"]
    assert payload["event_date"] == UPCOMING_DAY
    assert payload["days_to_earnings"] == 6            # 2025-06-10 -> 2025-06-16
    assert payload["event_time_confirmed"] is True
    assert payload["usable_events"] == 8
    assert payload["hist_move"] == pytest.approx(4.0)
    # surprises alternate 10% / 30% -> sample stdev of four 10s and four 30s
    assert payload["surprise_vol"] == pytest.approx(10.69, abs=0.05)


def test_payload_key_path_is_the_one_task9_reads():
    """Live stamps ExpertRecommendation.data={'FMPEarningsEvent': ...}; the backtest
    engine copies raw_outputs wholesale into .data. Both must expose the SAME path,
    or Task 9's days_to_earnings condition works in one and silently never fires in
    the other."""
    rows, df = _healthy_rows()
    rec, _ = _run(rows, df=df)
    backtest_data = dict(rec.raw_outputs)              # daily_engine._persist does this
    assert backtest_data["FMPEarningsEvent"]["days_to_earnings"] == 6


def test_no_event_inside_the_look_window_is_refused():
    rows, df = _healthy_rows()
    rows[0] = _row("2025-07-21", eps=None, est=1.30)   # 41 days out
    rec, _ = _run(rows, df=df)
    assert rec.skip and "no earnings event within 10 days" in rec.skip_reason


def test_the_look_window_is_inclusive_of_the_event_day():
    """A print TODAY is 0 days away, not 'already past' — the same inclusive bound
    DaysToEarningsCondition uses."""
    rows, df = _healthy_rows()
    rows[0] = _row("2025-06-10", eps=None, est=1.30)
    rec, _ = _run(rows, df=df)
    assert not rec.skip, rec.skip_reason
    assert rec.raw_outputs["FMPEarningsEvent"]["days_to_earnings"] == 0


# ------------------------------------------------ the bmo/amc day convention ---
def test_amc_move_is_the_next_session():
    """'amc' prints after the close of D, so the reaction is Close(D+1)/Close(D)."""
    bars = [("2024-06-17", 100.0), ("2024-06-18", 107.0), ("2024-06-19", 100.0)]
    assert earnings_day_move_pct(bars, "2024-06-17", "amc") == pytest.approx(7.0)


def test_bmo_move_is_the_event_day_itself():
    """'bmo' prints before the open of D, so the reaction is Close(D)/Close(D-1)."""
    bars = [("2024-06-14", 100.0), ("2024-06-17", 107.0), ("2024-06-18", 100.0)]
    assert earnings_day_move_pct(bars, "2024-06-17", "bmo") == pytest.approx(7.0)


def test_the_two_slots_are_not_interchangeable():
    """The SAME price series read with the wrong slot yields a different number —
    which is exactly why the slot must never be defaulted."""
    bars = [("2024-06-14", 100.0), ("2024-06-17", 100.0),
            ("2024-06-18", 107.0), ("2024-06-19", 100.0)]
    assert earnings_day_move_pct(bars, "2024-06-17", "amc") == pytest.approx(7.0)
    assert earnings_day_move_pct(bars, "2024-06-17", "bmo") == pytest.approx(0.0)


def test_the_move_is_absolute():
    bars = [("2024-06-17", 100.0), ("2024-06-18", 92.0)]
    assert earnings_day_move_pct(bars, "2024-06-17", "amc") == pytest.approx(8.0)


def test_unknown_slot_yields_no_move():
    """'--' and missing are FMP's unknown slot: no guessed default, no move."""
    bars = [("2024-06-14", 100.0), ("2024-06-17", 107.0), ("2024-06-18", 112.0)]
    assert earnings_day_move_pct(bars, "2024-06-17", "--") is None
    assert earnings_day_move_pct(bars, "2024-06-17", None) is None


def test_missing_bar_on_either_side_yields_no_move():
    assert earnings_day_move_pct([("2024-06-17", 100.0)], "2024-06-17", "amc") is None
    assert earnings_day_move_pct([("2024-06-17", 100.0)], "2024-06-17", "bmo") is None


def test_unknown_slot_events_do_not_count_toward_min_hist_events():
    """A symbol whose prints are all unslotted is refused, not scored on guesses."""
    rows, df = _healthy_rows(n_past=8)
    rows = [rows[0]] + [{**r, "time": "--"} for r in rows[1:]]
    rec, _ = _run(rows, df=df)
    assert rec.skip and "0 usable past events" in rec.skip_reason


# ------------------------------------------------------ min_hist_events floor ---
def test_sbet_class_no_coverage_is_refused():
    """MEASURED outlier: SBET carried usable data on 1 of its 12 rows. One usable
    event is not a distribution — the symbol must leave the ranking."""
    rows = [_row(UPCOMING_DAY, eps=None, est=1.30, slot="amc")]
    jumps = {}
    for i, day in enumerate(PAST_DAYS):
        if i == 0:
            rows.append(_row(day, eps=1.10, est=1.00, slot="amc"))
            jumps[_next_weekday(day)] = 104.0
        else:
            rows.append(_row(day, eps=None, est=None, slot="--"))
    rec, _ = _run(rows, df=_bars_df(jumps))
    assert rec.skip and "1 usable past events < min_hist_events=4" in rec.skip_reason


def test_min_hist_events_is_a_hard_floor_not_a_padded_rank():
    """MUTATION (a): dropping/loosening the min_hist_events comparison would let a
    3-event symbol into the ranking with a composite built from three prints. It
    must SKIP, and the skip must not be reachable by any weight setting."""
    rows, df = _healthy_rows(n_past=3)
    rec, _ = _run(rows, df=df)
    assert rec.skip and "3 usable past events < min_hist_events=4" in rec.skip_reason
    assert rec.confidence == 0.0
    # ... and exactly 4 usable events is admitted, so the floor is `<`, not `<=`.
    rows4, df4 = _healthy_rows(n_past=4)
    rec4, _ = _run(rows4, df=df4)
    assert not rec4.skip and rec4.raw_outputs["FMPEarningsEvent"]["usable_events"] == 4


# ---------------------------------------------------- unconfirmed-date trap ---
def test_unconfirmed_upcoming_date_is_refused_by_default():
    """MUTATION (b): admitting an unslotted upcoming row when allow_unconfirmed_dates
    is False buys volatility against a date FMP has not pinned, which slips."""
    rows, df = _healthy_rows(upcoming_slot="--")
    rec, _ = _run(rows, df=df)
    assert rec.skip
    assert "unconfirmed" in rec.skip_reason and "2025-06-16" in rec.skip_reason


def test_missing_slot_on_the_upcoming_row_is_also_unconfirmed():
    rows, df = _healthy_rows()
    rows[0] = {**rows[0], "time": None}
    rec, _ = _run(rows, df=df)
    assert rec.skip and "unconfirmed" in rec.skip_reason


def test_unconfirmed_date_is_admitted_when_the_setting_allows_it():
    rows, df = _healthy_rows(upcoming_slot="--")
    rec, _ = _run(rows, df=df, settings={"allow_unconfirmed_dates": True})
    assert not rec.skip, rec.skip_reason
    assert rec.raw_outputs["FMPEarningsEvent"]["event_time_confirmed"] is False


# ------------------------------------------------------ point-in-time / leak ---
def test_a_future_amc_print_has_no_reacting_bar_and_so_cannot_be_priced():
    """DEFENCE IN DEPTH, not the point-in-time split (which
    test_a_bmo_print_on_the_as_of_day_cannot_leak_into_the_features owns). An 'amc'
    print dated after the as-of reacts on a session the price window does not reach,
    so even if it were mis-sorted into the history it could contribute no move -- and
    with no move it contributes no surprise either, however monstrous its cached EPS.
    Pins that second barrier explicitly, so a refactor cannot quietly remove the one
    the split-mutation test does not exercise."""
    rows, df = _healthy_rows()
    clean = _run(rows, df=df)[0].raw_outputs["FMPEarningsEvent"]

    leaky = [{**rows[0], "eps": 25.0, "epsEstimated": 1.0}] + rows[1:]
    got = _run(leaky, df=df)[0].raw_outputs["FMPEarningsEvent"]

    assert got["surprise_vol"] == pytest.approx(clean["surprise_vol"])
    assert got["hist_move"] == pytest.approx(clean["hist_move"])
    assert got["usable_events"] == clean["usable_events"]


def test_a_bmo_print_on_the_as_of_day_cannot_leak_into_the_features():
    """The GENUINELY leakable case, and the reason the split is on the DATE rather
    than on "could we price it": a 'bmo' event dated ON the as-of reacts during the
    as-of session, so BOTH bars it needs already exist. Only the strict
    ``date < as_of`` split keeps its (already-known, in a present-day cache file)
    EPS and its move out of the history. Without the split hist_move and
    surprise_vol both move violently."""
    rows, df = _healthy_rows(n_past=8)
    clean = _run(rows, df=df)[0].raw_outputs["FMPEarningsEvent"]

    leaky = list(rows)
    leaky[0] = _row("2025-06-10", eps=25.0, est=1.00, slot="bmo")   # today, pre-open
    jumps = {d: c for d, c in zip(df["Date"].dt.strftime("%Y-%m-%d"), df["Close"])
             if c != 100.0}
    jumps["2025-06-10"] = 130.0                                     # a 30% reaction
    got = _run(leaky, df=_bars_df(jumps))[0].raw_outputs["FMPEarningsEvent"]

    assert got["days_to_earnings"] == 0                # it IS the upcoming event
    assert got["usable_events"] == clean["usable_events"] == 8
    assert got["hist_move"] == pytest.approx(clean["hist_move"])
    assert got["surprise_vol"] == pytest.approx(clean["surprise_vol"])


def test_an_event_dated_on_the_as_of_is_future_not_history():
    """The boundary: 'past' is STRICTLY BEFORE the as-of. A print happening today
    has not been priced yet, so it cannot be one of the historical observations."""
    rows, df = _healthy_rows(n_past=8)
    rows[0] = _row("2025-06-10", eps=9.0, est=1.0, slot="amc")   # today
    rec, _ = _run(rows, df=df)
    assert rec.raw_outputs["FMPEarningsEvent"]["usable_events"] == 8


def test_price_history_is_never_read_past_the_as_of():
    """The OHLCV window ends at the decision date, never at the calendar horizon."""
    rows, df = _healthy_rows()
    _, bundle = _run(rows, df=df)
    assert bundle.ohlcv_calls, "no OHLCV read happened"
    for _sym, end_date, _lb in bundle.ohlcv_calls:
        assert end_date == AS_OF


# ------------------------------------------------------- min_analysts gating ---
def test_min_analysts_below_threshold_is_refused():
    rows, df = _healthy_rows()
    rec, _ = _run(rows, df=df, estimates=_estimates_payload(n_analysts=2))
    assert rec.skip and "only 2 analysts < min_analysts=3" in rec.skip_reason


def test_absent_analyst_count_fails_closed():
    """An unread count is not evidence of coverage. With the gate armed the symbol
    is refused and the reason is logged, so a coverage hole can never look like a
    qualifying name."""
    rows, df = _healthy_rows()
    rec, _ = _run(rows, df=df, estimates=[])
    assert rec.skip and "analyst count unavailable" in rec.skip_reason
    # A published-but-zero count is FMP's "no count", handled the same way.
    rec0, _ = _run(rows, df=df, estimates=_estimates_payload(n_analysts=0))
    assert rec0.skip and "analyst count unavailable" in rec0.skip_reason


def test_min_analysts_zero_disables_the_gate_and_its_fetch():
    """0 means 'no gate' — and then the estimates payload is never even read."""
    rows, df = _healthy_rows()
    seen = []

    def _fake_cache(namespace, symbol, fetch_fn, *a, **kw):
        seen.append(namespace)
        if namespace.startswith("past_earnings"):
            return rows
        raise AssertionError(f"estimates must not be fetched: {namespace!r}")

    bundle = _Bundle(df)
    e = FMPEarningsEvent.__new__(FMPEarningsEvent)
    e.id = 1
    with patch("ba2_providers.fundamentals.details.FMPCompanyDetailsProvider."
               "fmp_history_disk_cached", side_effect=_fake_cache):
        rec = e.analyze_as_of(AS_OF, BacktestContext(
            providers=bundle, settings={**BASE_SETTINGS, "min_analysts": 0},
            as_of=AS_OF, extra={"symbol": "TSTX"}))
    assert not rec.skip, rec.skip_reason
    assert not any(n.startswith("earnings_estimates") for n in seen)


def test_the_analyst_count_reads_fmps_actual_plural_key():
    """The provider used to read 'numberAnalystEstimatedEps' (singular), which
    appears in NO cached payload — the count was 0 for every symbol, which under a
    fail-closed gate would refuse the whole universe."""
    rows, df = _healthy_rows()
    rec, _ = _run(rows, df=df, estimates=_estimates_payload(n_analysts=3))
    assert not rec.skip, rec.skip_reason


# ------------------------------------------------- normalization / weighting ---
def test_normalization_is_bounded_monotone_and_scale_anchored():
    assert normalize_feature("hist_move", 0.0) == 0.0
    assert normalize_feature("hist_move", 5.0) == pytest.approx(0.5)      # k = 5%
    assert normalize_feature("surprise_vol", 25.0) == pytest.approx(0.5)  # k = 25%
    assert normalize_feature("vol_cheapness", 1.0) == pytest.approx(0.5)  # k = 1.0
    prev = -1.0
    for x in (0.0, 1.0, 5.0, 20.0, 200.0, 1e6):
        n = normalize_feature("hist_move", x)
        assert 0.0 <= n < 1.0 and n > prev
        prev = n


def test_confidence_is_scale_free_in_the_weights():
    """Scaling every weight by a constant cannot change the score: the GA searches
    weight RATIOS, which is the only thing that can reorder symbols."""
    feats = {"hist_move": 6.0, "surprise_vol": 18.0}
    base = composite_confidence(feats, {"hist_move": 1.0, "surprise_vol": 2.0,
                                        "vol_cheapness": 1.0})
    scaled = composite_confidence(feats, {"hist_move": 10.0, "surprise_vol": 20.0,
                                          "vol_cheapness": 10.0})
    assert base == pytest.approx(scaled)


def test_confidence_stays_inside_the_platform_1_to_100_scale():
    for hm, sv in ((0.0, 0.0), (0.1, 0.1), (5.0, 25.0), (1e6, 1e6)):
        c = composite_confidence({"hist_move": hm, "surprise_vol": sv},
                                 {"hist_move": 1.0, "surprise_vol": 1.0,
                                  "vol_cheapness": 1.0})
        assert 1.0 <= c <= 100.0


def test_w_hist_move_moves_the_rank():
    """DEAD-GENE GUARD: two symbols whose feature profiles cross must swap order
    when w_hist_move alone is changed. A weight that cannot do this is a gene the
    GA burns budget on for nothing."""
    a = {"hist_move": 12.0, "surprise_vol": 5.0}     # big mover, steady results
    b = {"hist_move": 2.0, "surprise_vol": 60.0}     # quiet mover, wild results
    low = {"hist_move": 0.1, "surprise_vol": 1.0, "vol_cheapness": 1.0}
    high = {"hist_move": 10.0, "surprise_vol": 1.0, "vol_cheapness": 1.0}
    assert composite_confidence(a, low) < composite_confidence(b, low)
    assert composite_confidence(a, high) > composite_confidence(b, high)


def test_w_surprise_vol_moves_the_rank():
    a = {"hist_move": 12.0, "surprise_vol": 5.0}
    b = {"hist_move": 2.0, "surprise_vol": 60.0}
    low = {"hist_move": 1.0, "surprise_vol": 0.1, "vol_cheapness": 1.0}
    high = {"hist_move": 1.0, "surprise_vol": 10.0, "vol_cheapness": 1.0}
    assert composite_confidence(a, low) > composite_confidence(b, low)
    assert composite_confidence(a, high) < composite_confidence(b, high)


def test_w_vol_cheapness_moves_the_rank_once_task8_supplies_the_feature():
    """The weight is declared TODAY and its wiring is live — only the FEATURE is
    Task 8's. Feeding a vol_cheapness value straight into the composite proves the
    seam is real, so Task 8 adds a feature and nothing else."""
    a = {"hist_move": 12.0, "vol_cheapness": 0.2}
    b = {"hist_move": 2.0, "vol_cheapness": 5.0}
    low = {"hist_move": 1.0, "surprise_vol": 1.0, "vol_cheapness": 0.1}
    high = {"hist_move": 1.0, "surprise_vol": 1.0, "vol_cheapness": 10.0}
    assert composite_confidence(a, low) > composite_confidence(b, low)
    assert composite_confidence(a, high) < composite_confidence(b, high)


def test_vol_cheapness_is_absent_and_inert_without_chain_capability():
    """MUTATION (d) anchor: with no ``context.account`` (the default -- every OTHER
    test in this file runs this way) there is no chain seam, so the implied leg can
    never be computed and the weight must be provably INERT rather than quietly
    demoting every symbol (which a 0.0-valued feature would do)."""
    rows, df = _healthy_rows()
    base = _run(rows, df=df)[0]
    assert base.raw_outputs["FMPEarningsEvent"]["vol_cheapness"] is None
    for w in (0.0, 1.0, 50.0):
        rec, _ = _run(rows, df=df, settings={"w_vol_cheapness": w})
        assert rec.confidence == base.confidence


def test_an_absent_feature_never_demotes():
    """Renormalization, not a zero: a symbol with only hist_move must score exactly
    what its hist_move alone deserves, not half of it."""
    weights = {"hist_move": 1.0, "surprise_vol": 1.0, "vol_cheapness": 1.0}
    only_move = composite_confidence({"hist_move": 5.0}, weights)
    assert only_move == pytest.approx(1.0 + 99.0 * 0.5)
    both_at_the_same_normalized_level = composite_confidence(
        {"hist_move": 5.0, "surprise_vol": 25.0}, weights)
    assert only_move == pytest.approx(both_at_the_same_normalized_level)


def test_surprise_vol_is_absent_not_zero_when_there_is_no_dispersion_sample():
    """One surprise has no standard deviation. Emitting 0.0 would assert 'this name
    never surprises' — the strongest possible claim — from a single observation."""
    jumps = {}
    rows = []
    for i, day in enumerate(PAST_DAYS[:4]):
        eps = 1.10 if i == 0 else None      # only one row has a computable surprise
        est = 1.00 if i == 0 else None
        rows.append(_prow(day, eps=eps, est=est, slot="amc"))
        jumps[_next_weekday(day)] = 103.0
    df = _bars_df(jumps)
    bars = list(zip(df["Date"].dt.strftime("%Y-%m-%d"), df["Close"].astype(float)))
    out = compute_features(rows, bars)
    assert out["usable_events"] == 4
    assert "surprise_vol" not in out["features"]
    assert out["features"]["hist_move"] == pytest.approx(3.0)


def test_all_zero_weights_refuse_rather_than_invent_a_score():
    rows, df = _healthy_rows()
    rec, _ = _run(rows, df=df, settings={"w_hist_move": 0.0, "w_surprise_vol": 0.0,
                                         "w_vol_cheapness": 0.0})
    assert rec.skip and "no weighted feature available" in rec.skip_reason


def test_negative_weights_are_clamped_not_inverted():
    """The settings are importances. A negative one would flip a feature's meaning
    behind the operator's back."""
    feats = {"hist_move": 12.0, "surprise_vol": 5.0}
    clamped = composite_confidence(feats, {"hist_move": -3.0, "surprise_vol": 1.0,
                                           "vol_cheapness": 1.0})
    only_sv = composite_confidence({"surprise_vol": 5.0},
                                   {"surprise_vol": 1.0, "vol_cheapness": 1.0})
    assert clamped == pytest.approx(only_sv)


# ----------------------------------------------------------- withheld genes ---
def test_the_withheld_features_are_not_settings():
    """design §9 withheld w_dispersion / w_revision on MEASURED coverage (~3
    in-window estimate rows per symbol, a forward-biased endpoint, 1-analyst
    degeneracy). They are unlocked only by a point-in-time replay proving the
    estimate rows predate the events they would score — until then they must not
    exist as genes for the GA to find."""
    keys = set(FMPEarningsEvent.get_settings_definitions())
    assert "w_dispersion" not in keys
    assert "w_revision" not in keys
    assert keys == {"earnings_days_look", "min_hist_events", "min_analysts",
                    "allow_unconfirmed_dates", "w_hist_move", "w_surprise_vol",
                    "w_vol_cheapness"}


def test_the_withholding_is_documented_with_its_unlock_condition():
    """A silently-dropped feature is indistinguishable from one nobody thought of."""
    import sys
    # ``import ba2_experts.FMPEarningsEvent as mod`` would resolve to the CLASS the
    # package __init__ binds under that name, not the module.
    doc = sys.modules["ba2_experts.FMPEarningsEvent"].__doc__
    assert "w_dispersion" in doc and "w_revision" in doc
    assert "WHAT UNLOCKS THEM" in doc


# ------------------------------------------------------ provider passthrough ---
def test_provider_passes_the_announcement_slot_through():
    """The expert cannot place an earnings-day move without the slot, and the
    provider used to drop it. Also pins that a future-dated row is returned when the
    caller asks to a horizon (which is how the upcoming print is found at all)."""
    p = FMPCompanyDetailsProvider.__new__(FMPCompanyDetailsProvider)
    p.api_key = "fake-key"
    raw = [_row("2025-06-16", eps=None, est=1.30, slot="--"),
           _row("2025-03-17", eps=1.10, est=1.00, slot="bmo")]
    with patch("ba2_providers.fundamentals.details.FMPCompanyDetailsProvider."
               "fmp_history_disk_cached", return_value=raw):
        out = p.get_past_earnings("TSTX", frequency="quarterly",
                                  end_date=datetime(2025, 6, 20), lookback_periods=8,
                                  format_type="dict")
    by_day = {r["report_date"]: r for r in out["earnings"]}
    assert by_day["2025-06-16"]["time"] == "--"
    assert by_day["2025-03-17"]["time"] == "bmo"


def test_provider_horizon_filter_still_excludes_rows_past_the_end_date():
    p = FMPCompanyDetailsProvider.__new__(FMPCompanyDetailsProvider)
    p.api_key = "fake-key"
    raw = [_row("2025-09-15"), _row("2025-06-16"), _row("2025-03-17")]
    with patch("ba2_providers.fundamentals.details.FMPCompanyDetailsProvider."
               "fmp_history_disk_cached", return_value=raw):
        out = p.get_past_earnings("TSTX", frequency="quarterly",
                                  end_date=datetime(2025, 6, 20), lookback_periods=8,
                                  format_type="dict")
    assert [r["report_date"] for r in out["earnings"]] == ["2025-06-16", "2025-03-17"]


# ------------------------------------------------------------------ registry ---
def test_registered_in_the_experts_registry():
    from ba2_experts import get_expert_class
    assert get_expert_class("FMPEarningsEvent") is FMPEarningsEvent


def test_every_setting_declares_a_default_and_a_description():
    for key, spec in FMPEarningsEvent.get_settings_definitions().items():
        assert "default" in spec, key
        assert spec["description"], key


# ------------------------------------------------- the coerced-zero surprise ---
def _bars_for(days, jump=104.0):
    """Ascending [(day, close)] with the reacting session of each amc print moved."""
    df = _bars_df({_next_weekday(d): jump for d in days})
    return list(zip(df["Date"].dt.strftime("%Y-%m-%d"), df["Close"].astype(float)))


def test_an_unreported_quarter_contributes_no_surprise():
    """The provider maps a MISSING eps leg to 0.0 and then refuses to state a surprise
    on that row. Recomputing from the coerced legs would invent a -100% 'total miss'
    out of a quarter nobody has reported yet -- the exact trap
    ba2_experts.earnings_surprise.surprise_history guards against."""
    days = PAST_DAYS[:3]
    # THE EXACT ROW THE PROVIDER EMITS for a scheduled-but-unreported quarter: the
    # missing actual arrives here already COERCED to 0.0, with surprise_percent left
    # None. Recomputing (0.0 - 1.30)/|1.30| gives -100.0 -- a total miss, invented.
    rows = [_prow(days[0], eps=0.0, est=1.30, slot="amc"),
            _prow(days[1], eps=1.10, est=1.00, slot="amc"),
            _prow(days[2], eps=1.30, est=1.00, slot="amc")]
    out = compute_features(rows, _bars_for(days))
    assert out["usable_events"] == 3     # it IS priceable; only the SURPRISE is absent
    assert out["surprises"] == pytest.approx([10.0, 30.0])
    assert -100.0 not in out["surprises"]
    # ACCEPTED COST, same as the shared helper's: a genuine 0.00 EPS print is
    # indistinguishable from a coerced one downstream, so it is dropped too.
    assert compute_features([_prow(days[0], eps=0.0, est=1.30, slot="amc")],
                            _bars_for(days))["surprises"] == []


def test_a_zero_coerced_leg_never_reaches_the_composite_through_the_provider():
    """End-to-end through the REAL provider mapping, which is what does the coercing:
    one scheduled-but-unreported past quarter must not distort surprise_vol."""
    import statistics
    rows, df = _healthy_rows(n_past=8)
    with_hole = list(rows)
    # _healthy_rows alternates +10%/+30% surprises; blanking the NEWEST past print's
    # actual leaves 3x10% and 4x30% behind it.
    with_hole[1] = _row(PAST_DAYS[0], eps=None, est=1.00, slot="amc")
    got = _run(with_hole, df=df)[0].raw_outputs["FMPEarningsEvent"]
    assert got["usable_events"] == 8      # still priceable, only unscoreable
    assert got["surprise_vol"] == pytest.approx(
        statistics.stdev([10.0, 30.0, 10.0, 30.0, 10.0, 30.0, 30.0]), abs=0.01)


def test_a_genuinely_present_pair_still_yields_its_surprise():
    """The guard must not throw the baby out: real legs are still scored, including a
    real MISS (which is what a fabricated -100 would otherwise be mistaken for)."""
    days = PAST_DAYS[:2]
    out = compute_features(
        [_prow(days[0], eps=1.25, est=1.00, slot="amc"),
         _prow(days[1], eps=0.80, est=1.00, slot="amc")],
        _bars_for(days))
    assert out["surprises"] == pytest.approx([25.0, -20.0])


# ------------------------------------------------------- interior-gap guard ---
def test_a_reacting_pair_across_an_interior_hole_is_not_a_move():
    """A bmo print whose previous bar is 19 days back is reading a HOLE, not a
    reaction; the whole gap's drift is not this announcement's doing."""
    bars = [("2024-05-29", 100.0), ("2024-06-17", 140.0), ("2024-06-18", 141.0)]
    assert earnings_day_move_pct(bars, "2024-06-17", "bmo") is None


def test_an_amc_pair_across_an_interior_hole_is_not_a_move():
    bars = [("2024-06-17", 100.0), ("2024-07-08", 140.0)]
    assert earnings_day_move_pct(bars, "2024-06-17", "amc") is None


def test_a_normal_holiday_weekend_pair_is_still_a_move():
    """The guard must admit every real weekend/holiday span (Fri -> Tue = 4 days),
    or it would quietly delete a chunk of ordinary history."""
    bars = [("2024-05-24", 100.0), ("2024-05-28", 106.0)]          # Memorial Day
    assert earnings_day_move_pct(bars, "2024-05-28", "bmo") == pytest.approx(6.0)
    amc = [("2024-05-24", 100.0), ("2024-05-28", 106.0)]
    assert earnings_day_move_pct(amc, "2024-05-24", "amc") == pytest.approx(6.0)


def test_a_gapped_event_does_not_count_toward_min_hist_events():
    """End-to-end: the gap guard must make the event UNUSABLE, not merely move-less,
    or min_hist_events would admit a symbol on unpriceable history."""
    rows = [_prow(d, eps=1.10, est=1.00, slot="bmo") for d in PAST_DAYS[:4]]
    bars = sorted((d, 130.0) for d in PAST_DAYS[:4])   # only the event days exist
    assert compute_features(rows, bars)["usable_events"] == 0


# ------------------------------------------ the window is filled with USABLE ---
def test_the_history_window_is_filled_with_usable_events_not_newest_rows():
    """8 newest prints unslotted + 4 older good ones: the good ones must still be
    found. Slicing to _MAX_HIST_EVENTS BEFORE attrition would see only the 8 bad rows
    and refuse the symbol -- and the generous _CALENDAR_PERIODS fetch that pays for
    those older rows would be pointless."""
    rows = [_prow(d, eps=1.10, est=1.00, slot="--") for d in PAST_DAYS[:8]]
    rows += [_prow(d, eps=(1.10 if i % 2 == 0 else 1.30), est=1.00, slot="amc")
             for i, d in enumerate(PAST_DAYS[8:12])]
    out = compute_features(rows, _bars_for(PAST_DAYS[8:12], jump=105.0))
    assert out["usable_events"] == 4
    assert out["features"]["hist_move"] == pytest.approx(5.0)


def test_the_window_still_caps_at_max_hist_events():
    """The fill is bounded: more usable events than the window does not widen it."""
    rows = [_prow(d, eps=1.10, est=1.00, slot="amc") for d in PAST_DAYS[:12]]
    assert compute_features(rows, _bars_for(PAST_DAYS[:12], jump=105.0))[
        "usable_events"] == 8


def test_surprise_vol_is_measured_over_the_same_usable_events_as_the_move():
    """The documented coupling: an event that could not be priced contributes no
    surprise either, so both features describe ONE event population and cannot report
    a move profile from 4 prints beside a surprise profile from 12."""
    rows = [_prow(PAST_DAYS[0], eps=99.0, est=1.00, slot="--")]    # wild, unpriceable
    rows += [_prow(d, eps=(1.10 if i % 2 == 0 else 1.30), est=1.00, slot="amc")
             for i, d in enumerate(PAST_DAYS[1:5])]
    out = compute_features(rows, _bars_for(PAST_DAYS[1:5]))
    assert out["usable_events"] == 4
    assert out["surprises"] == pytest.approx([10.0, 30.0, 10.0, 30.0])


# ----------------------------------------------------------- warmup contract ---
def test_backtest_warmup_bars_covers_the_price_window_it_asks_for():
    """The handler converts BARS -> calendar days and pre-warms that much history. If
    it falls short of _OHLCV_LOOKBACK_DAYS the earliest bars of a run silently see
    fewer usable events and get refused -- a crippled universe that reads as a quiet
    expert."""
    from ba2_experts.FMPEarningsEvent import _OHLCV_LOOKBACK_DAYS
    bars = FMPEarningsEvent.BACKTEST_WARMUP_BARS
    assert isinstance(bars, int)
    warmup_calendar_days = int(bars * 1.45) + 10       # daily_backtest_handler formula
    assert warmup_calendar_days >= _OHLCV_LOOKBACK_DAYS, (
        f"{bars} bars -> {warmup_calendar_days}d < the {_OHLCV_LOOKBACK_DAYS}d window")


def test_the_warmup_formula_pinned_above_is_the_handlers_own():
    """Re-derive the conversion from the handler instead of trusting the comment: if
    the handler's arithmetic changes, the test above is measuring nothing."""
    import os
    import re
    # ba2_experts ships as an independent package (BA2TradeExperts); the handler only
    # exists when the suite runs inside the monorepo, so this re-derivation SKIPS
    # rather than fails when it does not.
    root = os.path.abspath(__file__)
    for _ in range(4):
        root = os.path.dirname(root)
    src_path = os.path.join(root, "testplatform", "backend", "app", "services",
                            "backtest", "daily_backtest_handler.py")
    if not os.path.exists(src_path):
        pytest.skip("daily_backtest_handler not present (package checked out standalone)")
    src = open(src_path, encoding="utf-8").read()
    assert "_BARS_TO_CALDAYS = 1.45" in src
    assert re.search(r"int\(max_bars \* _BARS_TO_CALDAYS\) \+ 10", src)
    # And the registration this expert still needs from Task 10 is genuinely absent,
    # so the class attribute above is documented as inert rather than assumed live.
    assert '"FMPEarningsEvent"' not in src


# ------------------------------------------------------------------ settings ---
def test_setting_keys_matches_the_settings_definitions_exactly():
    """_SETTING_KEYS is what live run_analysis resolves. A setting missing from it is
    inert live while the UI field for it claims it works."""
    assert set(FMPEarningsEvent._SETTING_KEYS) == set(
        FMPEarningsEvent.get_settings_definitions())


# ============================================================ Task 8: vol_cheapness =
# The implied-move leg: a duck-typed option-chain seam, fail-to-absent throughout.
# ======================================================================================
def test_implied_move_pct_hand_derived():
    """The plan's own worked example, at the pure-arithmetic layer: straddle 3.00 on
    spot 100 -> implied_move_pct 3.0 (a PERCENT, matching hist_move's unit)."""
    leg = {"call_mid": 2.00, "put_mid": 1.00, "strike": 100.0, "expiry": EVENT_DATE}
    assert implied_move_pct(leg, 100.0) == pytest.approx(3.0)


def test_implied_move_pct_is_absent_for_every_unusable_input():
    leg = {"call_mid": 2.00, "put_mid": 1.00, "strike": 100.0, "expiry": EVENT_DATE}
    assert implied_move_pct(None, 100.0) is None                 # no leg at all
    assert implied_move_pct(leg, None) is None                   # no spot
    assert implied_move_pct(leg, 0.0) is None                    # zero spot
    assert implied_move_pct(leg, -50.0) is None                  # negative spot
    zero_straddle = {"call_mid": 0.0, "put_mid": 0.0, "strike": 100.0, "expiry": EVENT_DATE}
    assert implied_move_pct(zero_straddle, 100.0) is None        # zero straddle


def test_hand_derived_cheapness_hist6_straddle3_spot100_end_to_end():
    """The plan's own worked example, driven through the REAL _gather+_process with a
    stub chain: hist 6% / implied 3% -> vol_cheapness 2.0 -> n(2.0) = 2/3 at k=1.0."""
    rows, df = _healthy_rows(jump_pct=6.0)
    account = _StubAccount(contracts=GOOD_STRADDLE_CONTRACTS)
    rec, _ = _run(rows, df=df, account=account)
    assert not rec.skip, rec.skip_reason
    payload = rec.raw_outputs["FMPEarningsEvent"]
    assert payload["hist_move"] == pytest.approx(6.0)
    assert payload["vol_cheapness"] == pytest.approx(2.0)
    assert normalize_feature("vol_cheapness", payload["vol_cheapness"]) == pytest.approx(2.0 / 3.0)


def test_nearest_expiry_with_runway_skips_an_expiry_before_the_event():
    """MUTATION (b): the chain carries an expiry BEFORE the event (no runway -- the
    contract is already dead by the time the print happens), the correct nearest
    expiry ON/AFTER it, and a THIRD, later expiry that must lose to the nearer one.
    Only the correct (middle) expiry's straddle (call 5 + put 5 = 10, spot 100 ->
    implied 10%) may reach the composite; the too-early expiry's much cheaper
    straddle (which would read as a much HIGHER vol_cheapness) must never be picked."""
    rows, df = _healthy_rows(jump_pct=10.0)
    too_early = EVENT_DATE - timedelta(days=3)     # no runway -- must be skipped
    correct = EVENT_DATE                            # first expiry >= event date
    too_late = EVENT_DATE + timedelta(days=30)       # exists, but is not the nearest
    contracts = [
        _opt(100.0, too_early, OptionRight.CALL, price=0.50),
        _opt(100.0, too_early, OptionRight.PUT, price=0.50),
        _opt(100.0, correct, OptionRight.CALL, price=5.00),
        _opt(100.0, correct, OptionRight.PUT, price=5.00),
        _opt(100.0, too_late, OptionRight.CALL, price=20.00),
        _opt(100.0, too_late, OptionRight.PUT, price=20.00),
    ]
    account = _StubAccount(contracts=contracts)
    rec, _ = _run(rows, df=df, account=account)
    assert not rec.skip, rec.skip_reason
    payload = rec.raw_outputs["FMPEarningsEvent"]
    # implied_move_pct from the CORRECT expiry's straddle: (5+5)/100*100 = 10.0%.
    # hist_move is 10.0% too, so vol_cheapness == 1.0 -- distinct from what either
    # wrong expiry would have produced (too_early: 1.0/100*100=1.0% implied ->
    # cheapness 10.0; too_late: 40.0% implied -> cheapness 0.25).
    assert payload["vol_cheapness"] == pytest.approx(1.0)


def test_atm_strike_is_the_one_nearest_spot_among_two_sided_quotes():
    """Two strikes both have call+put quoted; the one FARTHER from spot must lose."""
    rows, df = _healthy_rows(jump_pct=6.0)
    contracts = [
        _opt(90.0, EVENT_DATE, OptionRight.CALL, price=12.00),   # far strike, cheap
        _opt(90.0, EVENT_DATE, OptionRight.PUT, price=1.00),
        _opt(100.0, EVENT_DATE, OptionRight.CALL, price=2.00),   # ATM strike
        _opt(100.0, EVENT_DATE, OptionRight.PUT, price=1.00),
    ]
    account = _StubAccount(contracts=contracts)
    rec, _ = _run(rows, df=df, account=account)
    assert not rec.skip, rec.skip_reason
    # ATM (100-strike) straddle = 3.00 -> implied 3.0% -> cheapness 6/3 = 2.0. The
    # 90-strike straddle (13.00 -> implied 13%) would have produced ~0.46 instead.
    assert rec.raw_outputs["FMPEarningsEvent"]["vol_cheapness"] == pytest.approx(2.0)


# ---------------------------------------------------------- fail-to-absent paths ---
def test_missing_leg_priced_as_zero_is_absent_not_a_fabricated_cheapness():
    """MUTATION (a): a strike with a CALL quote but no usable PUT quote (bid=ask=None,
    i.e. the leg simply never traded) must leave vol_cheapness absent. Reading the
    missing leg as 0.0 would fabricate a straddle price of just the call's mid and a
    wildly wrong (too CHEAP) cheapness instead of no feature at all."""
    rows, df = _healthy_rows(jump_pct=6.0)
    contracts = [
        _opt(100.0, EVENT_DATE, OptionRight.CALL, price=2.00),
        _opt(100.0, EVENT_DATE, OptionRight.PUT, bid=None, ask=None),   # unusable
    ]
    account = _StubAccount(contracts=contracts)
    rec, _ = _run(rows, df=df, account=account)
    assert not rec.skip, rec.skip_reason
    assert rec.raw_outputs["FMPEarningsEvent"]["vol_cheapness"] is None


def test_no_strike_quoted_on_both_legs_is_absent():
    rows, df = _healthy_rows(jump_pct=6.0)
    contracts = [
        _opt(100.0, EVENT_DATE, OptionRight.CALL, price=2.00),     # call only @100
        _opt(105.0, EVENT_DATE, OptionRight.PUT, price=1.00),      # put only @105
    ]
    account = _StubAccount(contracts=contracts)
    rec, _ = _run(rows, df=df, account=account)
    assert not rec.skip, rec.skip_reason
    assert rec.raw_outputs["FMPEarningsEvent"]["vol_cheapness"] is None


def test_no_expiry_with_runway_is_absent():
    """Every expiry the chain offers is BEFORE the event -- no usable straddle."""
    rows, df = _healthy_rows(jump_pct=6.0)
    stale = EVENT_DATE - timedelta(days=1)
    account = _StubAccount(contracts=[
        _opt(100.0, stale, OptionRight.CALL, price=2.00),
        _opt(100.0, stale, OptionRight.PUT, price=1.00),
    ])
    rec, _ = _run(rows, df=df, account=account)
    assert not rec.skip, rec.skip_reason
    assert rec.raw_outputs["FMPEarningsEvent"]["vol_cheapness"] is None


def test_empty_chain_is_absent():
    rows, df = _healthy_rows(jump_pct=6.0)
    rec, _ = _run(rows, df=df, account=_StubAccount(contracts=[]))
    assert not rec.skip, rec.skip_reason
    assert rec.raw_outputs["FMPEarningsEvent"]["vol_cheapness"] is None


def test_chain_read_failure_is_absent_not_fatal():
    rows, df = _healthy_rows(jump_pct=6.0)
    account = _StubAccount(raises=RuntimeError("broker outage"))
    rec, _ = _run(rows, df=df, account=account)
    assert not rec.skip, rec.skip_reason
    assert rec.raw_outputs["FMPEarningsEvent"]["vol_cheapness"] is None


def test_no_chain_capability_in_the_context_is_absent():
    """The account exists but publishes no get_option_chain -- a stock-only broker,
    the exact shape a real non-options AccountDefinition has."""
    rows, df = _healthy_rows(jump_pct=6.0)
    rec, _ = _run(rows, df=df, account=_NoChainAccount())
    assert not rec.skip, rec.skip_reason
    assert rec.raw_outputs["FMPEarningsEvent"]["vol_cheapness"] is None


def test_no_account_at_all_is_absent():
    """``context.account`` is None -- the golden/most-tests default -- and must be
    exactly as absent as a stock-only account, never an error."""
    rows, df = _healthy_rows(jump_pct=6.0)
    rec, _ = _run(rows, df=df, account=None)
    assert not rec.skip, rec.skip_reason
    assert rec.raw_outputs["FMPEarningsEvent"]["vol_cheapness"] is None


def test_zero_or_negative_spot_is_absent():
    """A spot of 0 (or less) makes 'straddle / spot' meaningless; the implied leg is
    never even fetched for it (spot gates the fetch itself, per _fetch_implied_leg)."""
    rows, df = _healthy_rows(jump_pct=6.0)
    account = _StubAccount(contracts=GOOD_STRADDLE_CONTRACTS)

    class _ZeroPriceBundle(_Bundle):
        def price_at_date(self, symbol, as_of):
            return 0.0

    settings = {**BASE_SETTINGS}
    e = FMPEarningsEvent.__new__(FMPEarningsEvent)
    e.id = 1
    bundle = _ZeroPriceBundle(df)

    def _fake_cache(namespace, symbol, fetch_fn, *a, **kw):
        if namespace.startswith("past_earnings"):
            return rows
        if namespace.startswith("earnings_estimates"):
            return _estimates_payload()
        raise AssertionError(namespace)

    with patch("ba2_providers.fundamentals.details.FMPCompanyDetailsProvider."
               "fmp_history_disk_cached", side_effect=_fake_cache):
        ctx = BacktestContext(providers=bundle, settings=settings, as_of=AS_OF,
                              extra={"symbol": "TSTX"}, account=account)
        rec = e.analyze_as_of(AS_OF, ctx)
    # A zero current_price also means no usable Recommendation.current_price, but
    # what THIS test pins is that the implied leg never fires for it (no chain call).
    assert account.calls == []


def test_absent_implied_never_demotes_the_composite():
    """MUTATION (d): reading an absent implied leg as 0.0 instead of leaving it out of
    `features` would DEMOTE every symbol vol_cheapness cannot be computed for. With no
    chain capability the confidence must be identical whatever w_vol_cheapness is."""
    rows, df = _healthy_rows(jump_pct=6.0)
    base = _run(rows, df=df, account=_NoChainAccount())[0]
    for w in (0.0, 1.0, 25.0, 500.0):
        rec, _ = _run(rows, df=df, account=_NoChainAccount(), settings={"w_vol_cheapness": w})
        assert rec.confidence == pytest.approx(base.confidence)


# --------------------------------------------------------- point-in-time / leak ---
def test_the_straddle_is_divided_by_the_decision_date_close_not_the_event_date():
    """MUTATION (c): the spot vol_cheapness divides the straddle by is ``current_price``
    -- the AS-OF (decision-date) close every other feature in this file uses -- never
    a price read AT the event date. A price provider that answers differently for the
    two dates catches a regression that swapped one for the other."""
    rows, df = _healthy_rows(jump_pct=6.0)
    account = _StubAccount(contracts=GOOD_STRADDLE_CONTRACTS)   # call 2 + put 1 = 3.00

    class _DatedPriceBundle(_Bundle):
        def price_at_date(self, symbol, as_of):
            # Decision date (AS_OF, 2025-06-10) -> 100.0 (implied 3.0%, cheapness 2.0).
            # Event date (2025-06-16) -> 30.0, which would read as implied 10.0% and
            # cheapness 0.6 instead -- a completely different, WRONG number.
            return 30.0 if as_of is not None and as_of.date() == EVENT_DATE else 100.0

    settings = {**BASE_SETTINGS}
    e = FMPEarningsEvent.__new__(FMPEarningsEvent)
    e.id = 1
    bundle = _DatedPriceBundle(df)

    def _fake_cache(namespace, symbol, fetch_fn, *a, **kw):
        if namespace.startswith("past_earnings"):
            return rows
        if namespace.startswith("earnings_estimates"):
            return _estimates_payload()
        raise AssertionError(namespace)

    with patch("ba2_providers.fundamentals.details.FMPCompanyDetailsProvider."
               "fmp_history_disk_cached", side_effect=_fake_cache):
        ctx = BacktestContext(providers=bundle, settings=settings, as_of=AS_OF,
                              extra={"symbol": "TSTX"}, account=account)
        rec = e.analyze_as_of(AS_OF, ctx)
    assert not rec.skip, rec.skip_reason
    assert rec.raw_outputs["FMPEarningsEvent"]["vol_cheapness"] == pytest.approx(2.0)


def test_the_chain_query_is_bounded_by_the_event_date_not_the_decision_date():
    """The chain query's ``expiry_min`` is the EVENT date (a contract expiring before
    it has no runway), which is deliberately a DIFFERENT date than the as-of decision
    date (2025-06-10 vs 2025-06-16) -- pinning that the two are not conflated."""
    rows, df = _healthy_rows(jump_pct=6.0)
    account = _StubAccount(contracts=GOOD_STRADDLE_CONTRACTS)
    _run(rows, df=df, account=account)
    assert len(account.calls) == 1
    underlying, expiry_min, expiry_max = account.calls[0]
    assert underlying == "TSTX"
    assert expiry_min == EVENT_DATE
    assert expiry_min != AS_OF.date()
    assert expiry_max > expiry_min


# --------------------------------------------------------------------- PERF ---
def test_one_chain_read_per_symbol_with_upcoming_event():
    """Perf acceptance: at most ONE get_option_chain call per symbol per analysis
    run, however many contracts/expiries it returns."""
    rows, df = _healthy_rows(jump_pct=6.0)
    account = _StubAccount(contracts=GOOD_STRADDLE_CONTRACTS)
    _run(rows, df=df, account=account)
    assert len(account.calls) == 1


def test_no_upcoming_event_never_reads_the_chain():
    """No event inside the look window -> zero chain reads, not merely zero USABLE
    ones: the analyst/hist-move gates all short-circuit before Task 8 even asks."""
    rows, df = _healthy_rows()
    rows[0] = _row("2025-07-21", eps=None, est=1.30)     # 41 days out, past the window
    account = _StubAccount(contracts=GOOD_STRADDLE_CONTRACTS)
    rec, _ = _run(rows, df=df, account=account)
    assert rec.skip
    assert account.calls == []


# -------------------------------------------------------- capability logging ---
def test_no_chain_capability_is_logged_only_once_per_instance():
    rows, df = _healthy_rows(jump_pct=6.0)
    settings = {**BASE_SETTINGS}
    bundle = _Bundle(df)
    e = FMPEarningsEvent.__new__(FMPEarningsEvent)
    e.id = 1

    class _Logger:
        def __init__(self):
            self.infos = []

        def info(self, msg):
            self.infos.append(msg)

    e.logger = _Logger()

    def _fake_cache(namespace, symbol, fetch_fn, *a, **kw):
        if namespace.startswith("past_earnings"):
            return rows
        if namespace.startswith("earnings_estimates"):
            return _estimates_payload()
        raise AssertionError(namespace)

    with patch("ba2_providers.fundamentals.details.FMPCompanyDetailsProvider."
               "fmp_history_disk_cached", side_effect=_fake_cache):
        for _ in range(3):
            ctx = BacktestContext(providers=bundle, settings=settings, as_of=AS_OF,
                                  extra={"symbol": "TSTX"}, account=_NoChainAccount())
            e.analyze_as_of(AS_OF, ctx)
    capability_msgs = [m for m in e.logger.infos if "no option-chain capability" in m]
    assert len(capability_msgs) == 1


# -------------------------------------------------------------- live seam ---
@pytest.fixture
def _restore_instance_resolver():
    """The instance resolver is a process-wide global (``ba2_common.core.
    instance_resolver``); any test that arms it MUST put the unconfigured resolver
    back, or a later, unrelated test in the same pytest run inherits a live stub."""
    from ba2_common.core.instance_resolver import _UnconfiguredResolver
    yield
    set_instance_resolver(_UnconfiguredResolver())


def test_run_analysis_resolves_the_live_account_through_the_instance_resolver(
        _restore_instance_resolver):
    """Live's duck-typed chain source: the account this expert instance's OWN
    ExpertInstance.account_id resolves to, via the SAME instance-resolver seam
    _get_current_price already uses -- not a new account-interface hook."""
    account = _StubAccount(contracts=GOOD_STRADDLE_CONTRACTS)
    resolved = {}

    class _Resolver:
        def get_expert_instance(self, expert_id):
            raise AssertionError("not needed for this seam")

        def get_account_instance(self, account_id):
            resolved["account_id"] = account_id
            return account

        def get_account_instance_from_transaction(self, transaction):
            raise AssertionError("not needed for this seam")

    e = FMPEarningsEvent.__new__(FMPEarningsEvent)
    e.id = 1
    e.instance = type("I", (), {"account_id": 42})()
    set_instance_resolver(_Resolver())
    got = e._resolve_live_account()
    assert got is account
    assert resolved["account_id"] == 42


def test_live_seam_absent_when_the_resolver_is_unwired(_restore_instance_resolver):
    """An InstanceResolverNotConfigured (or any other resolution failure) must be
    caught here and turned into 'no capability', never propagate out of run_analysis
    setup and abort the whole analysis over a missing chain feature."""
    from ba2_common.core.instance_resolver import _UnconfiguredResolver

    e = FMPEarningsEvent.__new__(FMPEarningsEvent)
    e.id = 1
    e.instance = type("I", (), {"account_id": 42})()
    set_instance_resolver(_UnconfiguredResolver())
    assert e._resolve_live_account() is None

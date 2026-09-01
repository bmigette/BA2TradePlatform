"""The ``O_ERN`` TIMING SPLIT: ``rec_days_to_earnings`` (entry) + ``days_after_event`` (exit).

Design 2026-08-31 (leaps-grid) S9 states the rule the whole earnings key rests on: the
EXPERT owns the RANKING, the STRATEGY owns the TIMING, and there is ONE timing knob. The
expert (``FMPEarningsEvent``) resolves the event date once, to score the symbol, and STAMPS
what it measured. The strategy's searched entry gene reads that stamp back
(``rec_days_to_earnings <= X``, X in 1..5) and its searched exit gene reads the event date
the entry order carried forward (``days_after_event >= Y``, Y in 0..2).

WHAT THIS FILE PINS, AND WHY EACH ONE IS A REAL FAILURE MODE
------------------------------------------------------------
1. **The stamp wins over any calendar.** A second, independently fetched earnings date is
   the second timing source the split exists to remove: the calendar moves, prints slip, and
   ``DaysToEarningsCondition``'s own annual-estimate fallback can answer a fiscal year end.
   A strategy timing off one number while acting on a rank computed from another is not
   mistimed by a little -- it is ungated in the direction nobody is watching. Pinned by
   giving the resolver a calendar that says something DIFFERENT and requiring the stamp's
   answer, with the provider never called at all.
2. **Absent stamp fires in NEITHER direction.** Every recommendation from every other expert
   has no earnings payload. If absence read as ``0``, ``rec_days_to_earnings <= 5`` would
   pass for the entire universe -- a straddle on everything, scored as if timed. Same shape
   on the exit: an absent event date read as "today" makes ``days_after_event >= 0`` fire on
   sight and flatten the book.
3. **The date convention, by hand.** Whole calendar days between two ``date`` objects, no
   timezone arithmetic. Entry: a Friday event seen from the Wednesday bar is 2. Exit: the day
   after a Monday event is 1, the event day itself is 0, and before the event it is NEGATIVE
   and not clamped.
4. **The exit is FORCED, not discretionary** (``forced_option_exit``) -- and ``days_opened``
   is still discretionary, so the difference is recorded rather than generalised.
5. **The submit-side carry-forward**: the entry order gets ``data["earnings_event_date"]``
   only when the recommendation carried one, on the same seam as ``max_loss_per_contract``.

Every date is frozen in 2024 while the wall clock is 2026: nothing here can pass by
accidentally agreeing with today.
"""
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from ba2_common.core import TradeConditions
from ba2_common.core.TradeConditions import (
    DaysAfterEventCondition,
    RecommendationDaysToEarningsCondition,
    create_condition,
)
from ba2_common.core.earnings_stamp import (
    EARNINGS_STAMP_NAMESPACE,
    ORDER_EVENT_DATE_KEY,
)
from ba2_common.core.types import ExpertEventType

# --- the hand-worked calendar ---------------------------------------------------------
#: Wednesday.
WED = date(2024, 3, 13)
#: Friday of the same week -- the print. WED -> FRI is 2 days, by hand.
FRI = date(2024, 3, 15)
#: Monday, a different week's print, used for the exit arithmetic.
MON = date(2024, 6, 3)
TUE = date(2024, 6, 4)          # one day after MON
PREV_FRI = date(2024, 5, 31)    # three days BEFORE MON


def _days_between(later: date, earlier: date) -> int:
    """The expert's own arithmetic, spelled out so the literals below are checkable."""
    return (later - earlier).days


def test_the_hand_arithmetic_this_file_asserts_against():
    """The two numbers every case below uses, derived rather than asserted from the code."""
    assert _days_between(FRI, WED) == 2, "Wed 2024-03-13 -> Fri 2024-03-15 is 2 calendar days"
    assert _days_between(TUE, MON) == 1, "the day after a Monday print is 1 day after"
    assert _days_between(MON, MON) == 0, "the event day itself is 0 days after"
    assert _days_between(PREV_FRI, MON) == -3, "the Friday before is 3 days BEFORE the print"


# --- doubles --------------------------------------------------------------------------
class _BacktestAccount:
    """Advertises a simulated clock, the way ``BacktestAccount`` does."""

    id = 1

    def __init__(self, as_of: date):
        self._bar = as_of

    def _as_of_date(self):
        return self._bar


class _BrokenClockAccount:
    id = 1

    def _as_of_date(self):
        raise RuntimeError("simulated clock unreadable")


class _CountingCalendar:
    """A fundamentals provider that would answer a DIFFERENT earnings date, and counts.

    Its whole job is to be available and to be WRONG relative to the stamp, so a condition
    that reaches for a calendar produces a number this file can name.
    """

    def __init__(self, report_dates):
        self._reports = list(report_dates)
        self.calls = 0

    def get_past_earnings(self, symbol, frequency, end_date, lookback_periods=8,
                          format_type="markdown"):
        self.calls += 1
        rows = [{"report_date": d, "fiscal_date_ending": d} for d in self._reports]
        return {"symbol": symbol, "frequency": frequency, "earnings": rows}

    def get_earnings_estimates(self, symbol, frequency, as_of_date, lookback_periods=4,
                               format_type="markdown"):
        self.calls += 1
        return {"symbol": symbol, "estimates": []}


def _rec(payload=None, *, data=..., created=datetime(2024, 3, 13, tzinfo=timezone.utc)):
    """A recommendation carrying (or deliberately NOT carrying) the expert's payload."""
    if data is ...:
        data = None if payload is None else {EARNINGS_STAMP_NAMESPACE: payload}
    return SimpleNamespace(created_at=created, instance_id=1, symbol="MSFT", data=data)


def _stamp(as_of: date, event: date):
    """The payload shape ``FMPEarningsEvent._process`` emits (the fields this task reads)."""
    return {"days_to_earnings": _days_between(event, as_of),
            "event_date": event.isoformat(),
            "event_time": "amc",
            "hist_move": 4.2}


def _entry_cond(rec, op="<=", value=3.0, account=None):
    return create_condition(ExpertEventType.N_REC_DAYS_TO_EARNINGS,
                            account if account is not None else _BacktestAccount(WED),
                            "MSFT", rec, operator_str=op, value=value)


def _order(data):
    return SimpleNamespace(id=7, symbol="MSFT", data=data, filled_qty=1, quantity=1)


def _exit_cond(order, *, as_of=TUE, op=">=", value=1.0, account=None):
    return create_condition(ExpertEventType.N_DAYS_AFTER_EVENT,
                            account if account is not None else _BacktestAccount(as_of),
                            "MSFT", _rec(), operator_str=op, value=value,
                            existing_order=order)


# ======================================================================================
# ENTRY -- rec_days_to_earnings
# ======================================================================================
def test_the_entry_gate_reads_the_stamped_distance_wed_to_fri_is_two():
    """Hand arithmetic, end to end: a Friday print seen from the Wednesday bar is 2 days."""
    cond = _entry_cond(_rec(_stamp(WED, FRI)), op="<=", value=3.0)
    assert cond.evaluate() is True
    assert cond.calculated_value == 2
    assert cond.get_actual_value_display() == "2d"


def test_the_entry_gate_does_not_fire_outside_its_window():
    """The searched gene is 1..5; 2 days out must fail a 1-day gate."""
    assert _entry_cond(_rec(_stamp(WED, FRI)), op="<=", value=1.0).evaluate() is False


def test_the_entry_gate_honours_both_operator_directions():
    rec = _rec(_stamp(WED, FRI))
    assert _entry_cond(rec, op=">=", value=2.0).evaluate() is True
    assert _entry_cond(rec, op=">", value=2.0).evaluate() is False
    assert _entry_cond(rec, op="==", value=2.0).evaluate() is True


# --- MUTATION (a): absent stamp reads as 0 --------------------------------------------
@pytest.mark.parametrize("data", [
    None,                                    # no data at all (the common case)
    {},                                      # data present, no expert namespace
    {"FMPRating": {"target_low": 1.0}},      # a DIFFERENT expert's payload
    {EARNINGS_STAMP_NAMESPACE: {}},          # our namespace, no days key
    {EARNINGS_STAMP_NAMESPACE: {"days_to_earnings": None}},
    {EARNINGS_STAMP_NAMESPACE: "not-a-dict"},
    "not-a-dict-at-all",
], ids=["no-data", "empty", "other-expert", "empty-payload", "null-days", "payload-str",
        "data-str"])
def test_an_absent_stamp_never_fires_the_entry_gate_in_either_direction(data):
    """MUTATION (a). If absence read as 0, ``rec_days_to_earnings <= 5`` would pass for
    EVERY symbol of EVERY non-earnings expert and the strategy would buy a straddle on the
    whole universe while its logs say it timed the entry. Both directions are checked
    because a ``>=`` gate is equally wrong reading absence as a number."""
    rec = _rec(data=data)
    for op, value in (("<=", 5.0), ("<", 5.0), (">=", 0.0), (">", -1.0), ("==", 0.0),
                      ("!=", 999.0)):
        cond = _entry_cond(rec, op=op, value=value)
        assert cond.evaluate() is False, f"{op} {value} fired on an ABSENT stamp"
        assert cond.calculated_value is None


@pytest.mark.parametrize("bad", [True, False, "2", b"2", float("nan"), float("inf")],
                         ids=["true", "false", "str", "bytes", "nan", "inf"])
def test_a_stringly_typed_or_nonfinite_stamp_is_refused_not_parsed(bad):
    """``True`` is an ``int`` subclass and ``float("2")`` succeeds -- either would let a
    malformed payload be PARSED into a plausible number of days. NaN is worse: every
    comparison against it is False except ``!=``, which would then fire."""
    cond = _entry_cond(_rec({"days_to_earnings": bad, "event_date": FRI.isoformat()}),
                       op="<=", value=5.0)
    assert cond.evaluate() is False
    assert cond.calculated_value is None


# --- MUTATION (c): a silent fallback to a live calendar -------------------------------
def test_the_stamp_wins_over_a_live_calendar_that_says_something_else(monkeypatch):
    """MUTATION (c) -- the one-timing-knob violation.

    The resolver is wired to a calendar whose next print is 2024-04-25 (43 days from the
    Wednesday bar), while the recommendation's stamp says 2 days. If the condition consulted
    the calendar -- as a source, or as a fallback -- ``<= 3`` would be FALSE and
    ``<= 45`` TRUE. The stamp must win, and the provider must not be touched at all: the
    strategy is timing the event the RANK was computed for.
    """
    cal = _CountingCalendar(["2024-04-25", "2024-01-30"])
    monkeypatch.setattr(TradeConditions, "_provider_resolver",
                        lambda category, name, **kw: cal, raising=False)

    rec = _rec(_stamp(WED, FRI))
    assert _entry_cond(rec, op="<=", value=3.0).evaluate() is True, (
        "the STAMP (2 days) must decide; the calendar's 43 days would fail this gate")
    assert _entry_cond(rec, op="<=", value=45.0).evaluate() is True
    assert _entry_cond(rec, op=">", value=40.0).evaluate() is False, (
        "40+ days out is the CALENDAR's answer -- the stamp says 2")
    assert cal.calls == 0, (
        f"the earnings calendar was fetched {cal.calls} time(s) for a gate that is supposed "
        f"to read the expert's stamp -- that is a second timing source")


def test_an_absent_stamp_does_not_fall_back_to_the_calendar_even_when_one_is_available(
        monkeypatch):
    """MUTATION (c), second shape. "Stamp first, calendar if missing" looks harmless and is
    the same defect: it re-introduces the second timing source, and it does so on exactly the
    recommendations that have no event at all -- every symbol of every non-earnings expert,
    for which the calendar will cheerfully return a date ~43 days out. The gate must be
    UNEVALUABLE, and the provider must not be reached."""
    cal = _CountingCalendar(["2024-04-25", "2024-01-30"])
    monkeypatch.setattr(TradeConditions, "_provider_resolver",
                        lambda category, name, **kw: cal, raising=False)

    for op, value in (("<=", 60.0), (">=", 0.0), (">", 40.0)):
        cond = _entry_cond(_rec(data=None), op=op, value=value)
        assert cond.evaluate() is False, f"{op} {value} fired off a CALENDAR fallback"
        assert cond.calculated_value is None
    assert cal.calls == 0, (
        f"the calendar was consulted {cal.calls} time(s) for a recommendation with no stamp "
        f"-- that is the fallback this field exists to not have")


def test_the_unchained_days_to_earnings_condition_is_unchanged(monkeypatch):
    """The existing calendar-fetching condition keeps its behaviour and its field. It is the
    answer for UNCHAINED uses (any expert, no stamp), and the decision NOT to make it
    stamp-first-with-a-calendar-fallback is what keeps 'absent stamp never fires' true for
    the new field. Same recommendation, same bar, DIFFERENT answers -- deliberately."""
    cal = _CountingCalendar(["2024-04-25", "2024-01-30"])
    monkeypatch.setattr(TradeConditions, "_provider_resolver",
                        lambda category, name, **kw: cal, raising=False)

    legacy = create_condition(ExpertEventType.N_DAYS_TO_EARNINGS, _BacktestAccount(WED),
                              "MSFT", _rec(_stamp(WED, FRI)), operator_str="<=", value=45.0)
    assert legacy.evaluate() is True
    assert legacy.calculated_value == _days_between(date(2024, 4, 25), WED) == 43
    assert cal.calls >= 1, "the unchained condition still reads the calendar"


# ======================================================================================
# EXIT -- days_after_event
# ======================================================================================
def test_the_exit_gate_reads_one_day_after_a_monday_event():
    """Hand arithmetic: MON print, TUE bar -> 1. ``>= 1`` fires; ``>= 2`` does not."""
    order = _order({ORDER_EVENT_DATE_KEY: MON.isoformat()})
    cond = _exit_cond(order, as_of=TUE, op=">=", value=1.0)
    assert cond.evaluate() is True
    assert cond.calculated_value == 1
    assert cond.get_actual_value_display() == "1d after event"
    assert _exit_cond(order, as_of=TUE, op=">=", value=2.0).evaluate() is False


def test_the_event_day_itself_is_zero_days_after():
    """Y=0 is a searched value (exit same day as the print), so the boundary is load-bearing."""
    order = _order({ORDER_EVENT_DATE_KEY: MON.isoformat()})
    assert _exit_cond(order, as_of=MON, op=">=", value=0.0).evaluate() is True
    assert _exit_cond(order, as_of=MON, op=">=", value=1.0).evaluate() is False
    on_the_day = _exit_cond(order, as_of=MON, op=">=", value=0.0)
    on_the_day.evaluate()
    assert on_the_day.calculated_value == 0


def test_before_the_event_the_value_is_negative_and_not_clamped():
    """The entry is taken 1-5 days BEFORE the print, so every bar between entry and event is
    negative. Clamping to 0 would make ``days_after_event >= 0`` -- a searched value -- fire
    the moment the position opened, closing the straddle before the event it was bought for."""
    order = _order({ORDER_EVENT_DATE_KEY: MON.isoformat()})
    cond = _exit_cond(order, as_of=PREV_FRI, op=">=", value=0.0)
    assert cond.evaluate() is False
    assert cond.calculated_value == -3


def test_the_exit_gate_honours_both_operator_directions():
    order = _order({ORDER_EVENT_DATE_KEY: MON.isoformat()})
    assert _exit_cond(order, as_of=TUE, op="<=", value=1.0).evaluate() is True
    assert _exit_cond(order, as_of=TUE, op="<", value=1.0).evaluate() is False


def test_the_exit_reference_is_the_order_not_whatever_recommendation_is_in_hand():
    """By exit time the recommendation is a later one -- possibly for the NEXT quarter. The
    condition must read the ENTRY order's carried date, so a recommendation stamped with a
    completely different event cannot move the exit."""
    order = _order({ORDER_EVENT_DATE_KEY: MON.isoformat()})
    cond = DaysAfterEventCondition(
        account=_BacktestAccount(TUE), instrument_name="MSFT",
        # a stamp for a print three months later
        expert_recommendation=_rec(_stamp(TUE, date(2024, 9, 3))),
        operator_str=">=", value=1.0, existing_order=order)
    assert cond.evaluate() is True
    assert cond.calculated_value == 1


# --- MUTATION: absent event date fires ------------------------------------------------
@pytest.mark.parametrize("data", [
    None,                                       # every equity order, and most option orders
    {},                                         # option order from another expert
    {"max_loss_per_contract": 350.0},           # a real neighbour stamp, no event date
    {ORDER_EVENT_DATE_KEY: None},
    {ORDER_EVENT_DATE_KEY: ""},
    {ORDER_EVENT_DATE_KEY: "not-a-date"},
    {ORDER_EVENT_DATE_KEY: 20240603},
    "not-a-dict",
], ids=["no-data", "empty", "neighbour-only", "null", "blank", "garbage", "int", "data-str"])
def test_an_absent_event_date_never_fires_the_exit_in_either_direction(data):
    """MUTATION. Absence read as "today" (0) makes ``days_after_event >= 0`` -- the Y=0 end of
    the searched range -- true for EVERY open position in the book on its first bar. That is a
    book-wide flatten dressed as an event exit, and it would hit equity positions too, since
    exit rules are evaluated against whatever order is open."""
    order = _order(data)
    for op, value in ((">=", 0.0), (">", -99.0), ("<=", 99.0), ("<", 99.0), ("==", 0.0),
                      ("!=", 12345.0)):
        cond = _exit_cond(order, as_of=TUE, op=op, value=value)
        assert cond.evaluate() is False, f"{op} {value} fired with NO event date"
        assert cond.calculated_value is None


def test_no_order_at_all_never_fires():
    for op, value in ((">=", 0.0), ("<=", 99.0)):
        cond = _exit_cond(None, as_of=TUE, op=op, value=value)
        assert cond.evaluate() is False
        assert cond.calculated_value is None


def test_an_unreadable_simulated_clock_never_fires():
    """The ``DaysToEarningsCondition`` lesson, on the exit side: substituting the wall clock
    for an unreadable simulated bar would make every 2024 position read ~2 years after its
    event and flatten the whole book on the first bar."""
    order = _order({ORDER_EVENT_DATE_KEY: MON.isoformat()})
    for op, value in ((">=", 1.0), ("<=", 1.0)):
        cond = _exit_cond(order, op=op, value=value, account=_BrokenClockAccount())
        assert cond.evaluate() is False
        assert cond.calculated_value is None


def test_a_live_account_measures_against_the_wall_clock():
    """No ``_as_of_date`` means LIVE, where ``date.today()`` IS the right answer -- the same
    tri-state ``_evaluation_date`` gives every other counting condition."""
    today = date.today()
    order = _order({ORDER_EVENT_DATE_KEY: today.isoformat()})

    class _Live:
        id = 1

    cond = DaysAfterEventCondition(account=_Live(), instrument_name="MSFT",
                                   expert_recommendation=_rec(), operator_str=">=",
                                   value=0.0, existing_order=order)
    assert cond.evaluate() is True
    assert cond.calculated_value == 0


def test_a_date_object_stamp_reads_the_same_as_its_iso_string():
    """A live path may hand the object rather than its serialisation; the two must agree."""
    iso = _exit_cond(_order({ORDER_EVENT_DATE_KEY: MON.isoformat()}), as_of=TUE)
    obj = _exit_cond(_order({ORDER_EVENT_DATE_KEY: MON}), as_of=TUE)
    assert iso.evaluate() is True and obj.evaluate() is True
    assert iso.calculated_value == obj.calculated_value == 1


# ======================================================================================
# MUTATION (b) -- the forced/discretionary classification
# ======================================================================================
def _event_action(field, op, value):
    return SimpleNamespace(triggers={"t1": {"event_type": field, "operator": op,
                                            "value": value}})


def test_the_days_after_event_exit_is_classified_FORCED_not_discretionary():
    """MUTATION (b). ``forced_option_exit`` decides how a CLOSE_OPTION is QUOTED in the
    backtest: a forced (risk) exit crosses the whole modelled spread, a discretionary one
    concedes only the entry's ``entry_cross`` fraction. Misclassifying this exit as
    discretionary books every O_ERN exit at a price the strategy could not actually get --
    the filter-flattering exit F7 exists to remove -- on the ONE grid key the design says
    deserves statistical weight (hundreds of independent events in-window)."""
    from ba2_common.core.TradeActionEvaluator import forced_option_exit

    for op in (">=", ">", "<=", "<", "==", "!="):
        assert forced_option_exit(_event_action("days_after_event", op, 1.0)) is True, (
            f"days_after_event {op} classified DISCRETIONARY -- the event exit would be "
            f"quoted as if it could wait for a better fill")


def test_days_opened_stays_discretionary_so_the_difference_is_deliberate():
    """The convention table's existing note says the ``days_opened`` time exit is DELIBERATELY
    discretionary ('recorded so it is not re-litigated'). ``days_after_event`` is classified
    the other way ON PURPOSE and the two are pinned side by side so neither drifts into the
    other: ``days_opened`` is a STALENESS exit on a live thesis and can wait for a decent
    quote; ``days_after_event`` is the terminal date of a binary event trade whose thesis is
    already over, with theta and the post-print vol crush both running against it."""
    from ba2_common.core.TradeActionEvaluator import forced_option_exit

    assert forced_option_exit(_event_action("days_opened", ">=", 21.0)) is False
    assert forced_option_exit(_event_action("days_to_expiry", "<=", 21.0)) is True


def test_the_entry_gate_is_not_an_exit_classification_at_all():
    """Entry conditions do not classify: ``rec_days_to_earnings`` never appears on a
    CLOSE_OPTION rule, and adding it to the forced set would make any rule that happens to
    carry it pay the full spread."""
    from ba2_common.core.TradeActionEvaluator import forced_option_exit

    assert forced_option_exit(_event_action("rec_days_to_earnings", "<=", 3.0)) is False


# ======================================================================================
# REGISTRY CLOSURE
# ======================================================================================
@pytest.mark.parametrize("field,event_type,cls", [
    ("rec_days_to_earnings", ExpertEventType.N_REC_DAYS_TO_EARNINGS,
     RecommendationDaysToEarningsCondition),
    ("days_after_event", ExpertEventType.N_DAYS_AFTER_EVENT, DaysAfterEventCondition),
])
def test_the_new_fields_are_closed_over_every_registry(field, event_type, cls):
    """``test_condition_registry_coverage.py`` proves the CLOSURE generically; this names the
    two fields so a reader can see which registries a numeric gate has to appear in, and so a
    deletion from any one of them fails with the field's own name."""
    from ba2_common.core.TradeConditions import CONDITION_MAP
    from ba2_common.core.rule_builders import FIELD_EVENT, triggers_from_condition_tree
    from ba2_common.core.rules_documentation import get_event_type_documentation
    from ba2_common.core.rules_export_import import _abbr_field
    from ba2_common.core.types import get_numeric_event_values, is_numeric_event

    assert event_type.value == field
    assert CONDITION_MAP[event_type] is cls
    assert FIELD_EVENT[field] is event_type
    assert field in get_numeric_event_values() and is_numeric_event(field)
    assert get_event_type_documentation()[field]["type"] == "numeric"
    assert _abbr_field(field) not in (None, "")

    triggers = triggers_from_condition_tree(
        {"type": "AND", "conditions": [{"id": "x", "field": field, "op": ">=", "value": 1}]})
    assert list(triggers.values()) == [{"event_type": field, "operator": ">=", "value": 1}], (
        f"a rule leaf naming {field!r} is dropped before the engine sees it")


@pytest.mark.parametrize("field,cls", [
    ("rec_days_to_earnings", RecommendationDaysToEarningsCondition),
    ("days_after_event", DaysAfterEventCondition),
])
def test_create_condition_builds_the_right_class_for_each_field(field, cls):
    cond = create_condition(ExpertEventType(field), _BacktestAccount(WED), "MSFT", _rec(),
                            operator_str=">=", value=1.0)
    assert isinstance(cond, cls)


@pytest.mark.parametrize("field", ["rec_days_to_earnings", "days_after_event"])
def test_each_new_numeric_field_requires_an_operator_and_a_value(field):
    """Both are ``CompareCondition``s: a value-less trigger must raise rather than default."""
    with pytest.raises(ValueError):
        create_condition(ExpertEventType(field), _BacktestAccount(WED), "MSFT", _rec())


def test_both_fields_describe_themselves_without_a_measurement():
    """``get_description`` is rendered on audit rows before evaluation, and
    ``get_actual_value_display`` must render nothing rather than a plausible number when the
    condition did not measure."""
    entry = _entry_cond(_rec())
    exit_ = _exit_cond(_order(None))
    for cond in (entry, exit_):
        assert cond.get_description()
        assert cond.get_actual_value_display() is None

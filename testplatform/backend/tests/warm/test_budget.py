"""The warm bandwidth budget (spec section 6, "Operational policy").

Three claims the spec makes that nothing could check before this existed:

* requests and bytes are attributed by PURPOSE -- live, capture (which must stay at
  zero: recording issues no request of its own) and warm;
* the shared daily allowance is reserved BEFORE dispatch, and running out pauses
  the warm with an explicit remaining-gap report rather than quietly stopping;
* a rate-limited provider backs the warm off, because live requests keep priority.
"""
from datetime import datetime, timedelta, timezone

import pytest

from ba2_providers import fmp_common
from app.services.warm import budget as warm_budget

NOW = datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc)


class _Response:
    """The part of a ``requests.Response`` ``fmp_http_get`` reads."""

    def __init__(self, body: bytes, status_code: int = 200):
        self.content = body
        self.status_code = status_code
        self.headers = {}

    def raise_for_status(self):
        return None


@pytest.fixture(autouse=True)
def clean_counters():
    fmp_common.reset_purpose_stats()
    yield
    fmp_common.reset_purpose_stats()


def _get(body=b"x" * 100, endpoint="price-target"):
    return fmp_common.fmp_http_get(
        "https://example.invalid/api", {"apikey": "secret"}, endpoint=endpoint,
        getter=lambda url, params=None, timeout=None: _Response(body),
        sleep=lambda _s: None)


# --------------------------------------------------------------------------- #
# Counters by purpose
# --------------------------------------------------------------------------- #
def test_requests_and_bytes_are_counted_by_purpose_and_endpoint():
    _get(b"a" * 10)
    with fmp_common.fmp_purpose(fmp_common.PURPOSE_WARM):
        _get(b"b" * 40, endpoint="grades")
        _get(b"c" * 60, endpoint="grades")

    stats = fmp_common.get_purpose_stats()

    assert stats["live"]["requests"] == 1 and stats["live"]["bytes"] == 10
    assert stats["warm"]["requests"] == 2 and stats["warm"]["bytes"] == 100
    assert stats["warm"]["endpoints"]["grades"]["requests"] == 2
    assert "capture" not in stats, "recording must never issue a request of its own"


def test_the_purpose_tag_does_not_leak_out_of_its_context():
    with fmp_common.fmp_purpose(fmp_common.PURPOSE_WARM):
        assert fmp_common.current_fmp_purpose() == "warm"
    assert fmp_common.current_fmp_purpose() == "live"


def test_an_unknown_purpose_is_refused():
    with pytest.raises(ValueError):
        with fmp_common.fmp_purpose("whatever"):
            pass


def test_counters_reset_per_utc_day(monkeypatch):
    day = ["2026-09-11"]
    monkeypatch.setattr(fmp_common, "_utc_day", lambda: day[0])

    with fmp_common.fmp_purpose(fmp_common.PURPOSE_WARM):
        _get(b"x" * 50)
    assert fmp_common.get_purpose_stats()["warm"]["bytes"] == 50

    day[0] = "2026-09-12"
    with fmp_common.fmp_purpose(fmp_common.PURPOSE_WARM):
        _get(b"x" * 5)

    assert fmp_common.get_purpose_stats()["warm"]["bytes"] == 5, (
        "a daily allowance is measured against a day; yesterday's bytes cannot consume it")


def test_a_response_that_cannot_report_its_size_is_still_counted_as_a_request():
    class _NoBody:
        status_code = 200
        headers = {}
        content = None

        def raise_for_status(self):
            return None

    with fmp_common.fmp_purpose(fmp_common.PURPOSE_WARM):
        fmp_common.fmp_http_get("https://example.invalid/api", endpoint="e",
                                getter=lambda url, params=None, timeout=None: _NoBody(),
                                sleep=lambda _s: None)

    stats = fmp_common.get_purpose_stats()
    assert stats["warm"]["requests"] == 1
    assert stats["warm"]["bytes"] == 0, "an unmeasurable body is not padded with a guess"


# --------------------------------------------------------------------------- #
# Reservation
# --------------------------------------------------------------------------- #
def _budget(allowance=1000, unknown=100):
    return warm_budget.WarmBudget(allowance_bytes=allowance, unknown_reserve_bytes=unknown)


def test_a_reservation_inside_the_allowance_is_granted():
    b = _budget()

    assert b.reserve("a", 400) == 400
    assert b.remaining_bytes() == 600


def test_an_unknown_estimate_reserves_the_explicit_conservative_amount():
    b = _budget(allowance=1000, unknown=250)

    assert b.reserve("a", None) == 250, (
        "an unknown-size response reserves a stated amount, never zero")


def test_exhaustion_raises_with_a_gap_report_naming_what_was_not_dispatched():
    b = _budget(allowance=500)
    b.reserve("a", 400)

    with pytest.raises(warm_budget.BudgetExhausted) as excinfo:
        b.reserve("b", 400, pending=("b", "c", "d"))

    gap = excinfo.value.gap
    assert gap.remaining_bytes == 100
    assert gap.shortfall_bytes == 300
    assert gap.pending == ("b", "c", "d")
    assert "b, c, d" in gap.to_markdown()
    assert "b" in str(excinfo.value)


def test_settling_a_reservation_frees_the_difference_for_the_next_item():
    b = _budget(allowance=1000)
    b.reserve("a", 900)
    b.settle("a", 10)

    assert b.reserve("b", 900) == 900, "an over-reservation must not strand the allowance"


def test_a_released_reservation_is_not_charged():
    b = _budget(allowance=1000)
    b.reserve("a", 900)
    b.release("a")

    assert b.remaining_bytes() == 1000


def test_reserving_the_same_key_twice_is_refused_so_nothing_is_double_charged():
    b = _budget()
    b.reserve("a", 100)

    with pytest.raises(warm_budget.WarmBudgetError):
        b.reserve("a", 100)


def test_measured_warm_bytes_count_against_the_allowance():
    """The reservation is an estimate; what the wire actually cost is what is spent."""
    b = _budget(allowance=1000)
    b.reserve("a", 100)
    with fmp_common.fmp_purpose(fmp_common.PURPOSE_WARM):
        _get(b"x" * 800)
    b.settle("a", 800)

    assert b.spent_bytes() == 800
    assert b.remaining_bytes() == 200


def test_only_warm_bytes_are_charged_to_the_warm_allowance():
    b = _budget(allowance=1000)
    _get(b"x" * 900)  # live

    assert b.spent_bytes() == 0
    assert b.remaining_bytes() == 1000


def test_a_day_roll_under_a_running_budget_restarts_the_measurement(monkeypatch):
    day = ["2026-09-11"]
    monkeypatch.setattr(fmp_common, "_utc_day", lambda: day[0])
    b = _budget(allowance=1000)
    with fmp_common.fmp_purpose(fmp_common.PURPOSE_WARM):
        _get(b"x" * 300)
    assert b.spent_bytes() == 300

    day[0] = "2026-09-12"

    assert b.spent_bytes() == 0, "a new UTC day is a new allowance, not a negative balance"


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #
def test_the_warm_waits_out_an_armed_fmp_gate():
    """A 429 armed by ANY caller pauses the warm: live requests keep priority."""
    slept = []

    def _sleep(seconds):
        slept.append(seconds)
        fmp_common._GATE_UNTIL = 0.0    # the cooldown elapses while we wait

    fmp_common._gate_arm(5.0)
    try:
        waited = warm_budget.WarmBudget(allowance_bytes=1, unknown_reserve_bytes=1).wait_for_gate(
            sleep=_sleep)
    finally:
        fmp_common._GATE_UNTIL = 0.0

    assert slept, "an armed gate must actually pause the warm"
    assert waited > 0


def test_no_wait_when_the_gate_is_clear():
    fmp_common._GATE_UNTIL = 0.0
    slept = []

    waited = warm_budget.WarmBudget(allowance_bytes=1, unknown_reserve_bytes=1).wait_for_gate(
        sleep=lambda s: slept.append(s))

    assert waited == 0.0 and slept == []


# --------------------------------------------------------------------------- #
# Sizing a budget from a plan
# --------------------------------------------------------------------------- #
def test_a_budget_can_be_sized_from_a_plan_and_refuses_when_nothing_was_measured(tmp_path):
    from ba2_common.core.replay import dependencies as dep
    from app.services.warm import planner

    req = dep.Requirement(provider="fmp", namespace="price_target", symbol="AAPL",
                          window=dep.Window(start=None, end=NOW), interval=None,
                          kind=dep.KIND_HISTORY, optional=False, reason="t")
    empty_plan = planner.plan([req], [str(tmp_path)], as_of_now=NOW)

    with pytest.raises(warm_budget.WarmBudgetError):
        warm_budget.unknown_reserve_for(empty_plan)

    history = tmp_path / "fmp_history"
    history.mkdir()
    (history / "price_target__MSFT.json").write_text("x" * 400, encoding="utf-8")
    seeded = planner.plan([req], [str(tmp_path)], as_of_now=NOW)

    assert warm_budget.unknown_reserve_for(seeded) == 400

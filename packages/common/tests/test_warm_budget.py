"""The warm bandwidth budget (spec section 6, "Operational policy").

Pure mechanism: the METER (bytes spent) and the GATE (provider cooldown) are
injected, so these tests state exactly what the budget does with them and nothing
about FMP. The provider-side half -- that the meter really counts FMP and FRED
traffic by purpose -- lives in ``packages/providers/tests/test_warm_counters.py``.

Two claims the spec makes that nothing could check before this existed: the shared
daily allowance is reserved BEFORE dispatch and running out pauses with an explicit
remaining-gap report; and a rate-limited provider backs the warm off, because live
requests keep priority.
"""
import pytest

from ba2_common.core.warm import budget as warm_budget


class _Meter:
    """A stand-in for the provider's warm byte counter."""

    def __init__(self, value: int = 0):
        self.value = value

    def __call__(self) -> int:
        return self.value


def _budget(allowance=1000, unknown=100, meter=None, gate=None):
    return warm_budget.WarmBudget(
        allowance_bytes=allowance, unknown_reserve_bytes=unknown,
        meter=meter or _Meter(), gate=gate or (lambda: 0.0))


# --------------------------------------------------------------------------- #
# Reservation
# --------------------------------------------------------------------------- #
def test_a_reservation_inside_the_allowance_is_granted():
    b = _budget()

    assert b.reserve("a", 400) == 400
    assert b.remaining_bytes() == 600


def test_an_unknown_estimate_reserves_the_explicit_stated_amount():
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


# --------------------------------------------------------------------------- #
# What the meter says is what is spent
# --------------------------------------------------------------------------- #
def test_measured_bytes_count_against_the_allowance_not_the_estimate():
    meter = _Meter()
    b = _budget(allowance=1000, meter=meter)
    b.reserve("a", 100)
    meter.value = 800            # what the wire actually cost
    b.settle("a", 800)

    assert b.spent_bytes() == 800
    assert b.remaining_bytes() == 200


def test_traffic_already_metered_before_this_budget_existed_is_not_charged_to_it():
    meter = _Meter(5000)
    b = _budget(allowance=1000, meter=meter)

    assert b.spent_bytes() == 0, (
        "an earlier warm was charged to its own budget; charging it twice would make "
        "this run's allowance vanish before it started")


def test_a_day_roll_under_a_running_budget_restarts_the_measurement():
    meter = _Meter()
    b = _budget(allowance=1000, meter=meter)
    meter.value = 300
    assert b.spent_bytes() == 300

    meter.value = 0              # the provider's counters reset at the UTC day change

    assert b.spent_bytes() == 0, "a new UTC day is a new allowance, not a negative balance"
    assert b.remaining_bytes() == 1000


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #
def test_the_warm_waits_out_an_armed_gate():
    """A cooldown armed by ANY caller pauses the warm: live requests keep priority."""
    remaining = [5.0]
    slept = []

    def _sleep(seconds):
        slept.append(seconds)
        remaining[0] = 0.0       # the cooldown elapses while we wait

    b = _budget(gate=lambda: remaining[0])

    waited = b.wait_for_gate(sleep=_sleep)

    assert slept and waited > 0


def test_no_wait_when_the_gate_is_clear():
    slept = []

    waited = _budget(gate=lambda: 0.0).wait_for_gate(sleep=lambda s: slept.append(s))

    assert waited == 0.0 and slept == []


def test_a_gate_that_never_clears_gives_up_rather_than_holding_a_worker_forever():
    slept = []

    waited = _budget(gate=lambda: 10_000.0).wait_for_gate(sleep=lambda s: slept.append(s))

    assert waited <= warm_budget._GATE_MAX_WAIT_SECONDS
    assert sum(slept) == waited


# --------------------------------------------------------------------------- #
# Sizing a budget from a plan
# --------------------------------------------------------------------------- #
class _Plan:
    def __init__(self, sizes):
        self._sizes = sizes

    def measured_sizes(self):
        return list(self._sizes)


def test_the_unknown_reserve_is_a_high_quantile_of_what_the_plan_measured():
    """Not the median: the unmeasured payloads are exactly the ones that can be large."""
    sizes = [100] * 8 + [10_000, 20_000]

    assert warm_budget.unknown_reserve_for(_Plan(sizes)) == 10_000, (
        "a median would have reserved 100 -- two orders of magnitude under the payloads "
        "whose size the plan could not measure")


def test_the_unknown_reserve_works_from_a_single_measured_file():
    assert warm_budget.unknown_reserve_for(_Plan([400])) == 400


def test_a_plan_that_measured_nothing_refuses_to_size_a_reservation():
    with pytest.raises(warm_budget.WarmBudgetError):
        warm_budget.unknown_reserve_for(_Plan([]))


@pytest.mark.parametrize("values,q,expected", [
    ([1, 2, 3, 4, 5], 0.0, 1),
    ([1, 2, 3, 4, 5], 1.0, 5),
    ([1, 2, 3, 4, 5], 0.5, 3),
])
def test_quantile_is_nearest_rank_and_works_on_short_samples(values, q, expected):
    assert warm_budget.quantile(values, q) == expected

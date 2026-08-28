
import pytest

def test_a_concession_that_would_erase_the_premium_is_DECLINED_not_clamped():
    """`max(0.0, limit - give)` is not a safe floor -- it is a zero-premium short.

    A SELL_LIMIT of 0.0 ALWAYS clears: `_option_cross` floors the sell at `max(0.0, px - half)`
    and `_arb_fill_reject_reason` does not fire on an OTM contract whose intrinsic is 0. So the
    clamp turned an order that would honestly have expired unfilled into a short option written
    for NOTHING while carrying the full assignment liability -- fabricated risk with no
    compensation. Reachable with the grid's own --option-spread-min-tick 0.02 (doubled on a thin
    contract) against anything priced at or below $0.02.
    """
    from ba2_common.core.option_entry_quote import entry_limit_with_concession
    from ba2_common.core.option_types import OptionLeg
    from ba2_common.core.types import OrderDirection

    sell = [OptionLeg(contract_symbol="X", side=OrderDirection.SELL)]
    # give (0.05) exceeds the whole premium (0.03): decline, do not clamp to zero.
    assert entry_limit_with_concession(0.03, sell, [0.05], 1.0) == 0.03
    # exactly-zero result is also declined -- 0.0 is the always-clearing value, not a limit.
    assert entry_limit_with_concession(0.05, sell, [0.05], 1.0) == 0.05
    # a concession the premium can absorb still applies.
    assert entry_limit_with_concession(1.00, sell, [0.05], 1.0) == pytest.approx(0.95)


def test_a_multileg_credit_that_would_flip_to_a_debit_is_DECLINED():
    """Same defect on the multi-leg branch: `min(limit + give, 0.0)` yields a zero-net credit
    order, which is the same always-clearing value for a structure that posts collateral."""
    from ba2_common.core.option_entry_quote import entry_limit_with_concession
    from ba2_common.core.option_types import OptionLeg
    from ba2_common.core.types import OrderDirection

    legs = [OptionLeg(contract_symbol="A", side=OrderDirection.SELL),
            OptionLeg(contract_symbol="B", side=OrderDirection.BUY)]
    assert entry_limit_with_concession(-0.04, legs, [0.05, 0.05], 1.0) == -0.04
    assert entry_limit_with_concession(-1.00, legs, [0.05, 0.05], 1.0) < 0.0

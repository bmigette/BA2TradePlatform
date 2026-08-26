"""Premium richness as PER-CONTRACT annualised return on collateral (OPT-C1).

Credit structures are admitted on ``net_credit > 0`` alone. Selling far-OTM options for
near-zero credit expires worthless ~97 % of the time, so a win-rate- or Sharpe-flavoured
fitness is ACTIVELY REWARDED for doing it. Verbatim, before this module existed::

    $ grep -rnE 'min_arc|return_on_collateral' --include='*.py' \
          packages/ testplatform/ ba2_trade_platform/
    (no matches)

Two properties carry the whole design and each is tested from both sides:

* **Per contract, the ratio is invariant to sizing.** At the book level
  ``contracts x max_loss`` IS ``option_sizing`` % of equity by construction, so a book-level
  ratio divides by a near-constant. Three contracts is three times both numerator and
  denominator, so the per-contract number is the same.
* **Unknown is neither 0 % nor infinite.** 0 % refuses everything and infinity admits
  everything; both look like a working gate.
"""
import math

import pytest

from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.option_economics import (
    DAYS_PER_YEAR,
    RESERVE_TABLE_MULTIPLIER,
    admits_credit_structure,
    annualized_return_on_collateral,
    applies_to,
    collateral_per_contract,
)
from ba2_common.core.types import OptionRight


# ---------------------------------------------------------------------------
# 1. The arithmetic, against reserves taken from option_reserve_required
# ---------------------------------------------------------------------------
def test_cash_secured_put_matches_the_hand_computation():
    """CSP: collateral is strike x 100. $2.00 credit on a $50 strike over 30 days ->
    200 / 5000 = 4.0 % held, x (365/30) = 48.67 %/yr."""
    arc = annualized_return_on_collateral(
        strategy="cash_secured_put", net_credit=2.0, days_to_expiry=30, strike=50.0)
    assert arc == pytest.approx((200.0 / 5000.0) * (365.0 / 30.0))
    assert arc == pytest.approx(0.48667, abs=1e-5)


def test_credit_vertical_matches_the_hand_computation():
    """A $5-wide bull put spread for $1.00: collateral is (5 - 1) x 100 = $400, premium $100,
    25 % held over 45 days -> 202.8 %/yr."""
    arc = annualized_return_on_collateral(
        strategy="bull_put_spread", net_credit=1.0, days_to_expiry=45,
        spread_width=5.0)
    assert arc == pytest.approx((100.0 / 400.0) * (365.0 / 45.0))


def test_the_collateral_comes_from_the_reserve_helper_not_a_second_formula():
    """Pinned against ``option_reserve_required`` itself, so the two can never drift: a
    re-derivation here would be a second thing to keep correct."""
    for kwargs in (
        {"strategy": "cash_secured_put", "strike": 50.0},
        {"strategy": "bear_call_spread", "spread_width": 5.0, "net_credit": 1.0},
        {"strategy": "short_strangle", "strike": 100.0, "spot": 100.0,
         "option_type": OptionRight.PUT},
        {"strategy": "jade_lizard", "strike": 290.0, "spread_width": 17.5, "net_credit": 3.04},
    ):
        strategy = kwargs.pop("strategy")
        assert collateral_per_contract(strategy, **kwargs) == pytest.approx(
            OptionsAccountInterface.option_reserve_required(strategy, 1, **kwargs))


def test_a_naked_short_uses_the_regt_reserve():
    reserve = OptionsAccountInterface.option_reserve_required(
        "naked_put", 1, strike=100.0, spot=100.0, option_type=OptionRight.PUT)
    arc = annualized_return_on_collateral(
        strategy="naked_put", net_credit=2.0, days_to_expiry=30,
        strike=100.0, spot=100.0, option_type=OptionRight.PUT)
    assert arc == pytest.approx((200.0 / reserve) * (365.0 / 30.0))


# ---------------------------------------------------------------------------
# 2. Invariance to size -- the property that makes it usable as a criterion
# ---------------------------------------------------------------------------
def test_the_ratio_is_invariant_to_contract_count():
    """The R4 objection to return-on-collateral was that at the BOOK level
    ``contracts x max_loss`` is option_sizing % of equity by construction. Per contract both
    sides scale together, so the number is the same for 1 and for 30."""
    one = annualized_return_on_collateral(
        strategy="cash_secured_put", net_credit=2.0, days_to_expiry=30, strike=50.0)
    for n in (3, 30, 100):
        book_premium = 2.0 * RESERVE_TABLE_MULTIPLIER * n
        book_collateral = OptionsAccountInterface.option_reserve_required(
            "cash_secured_put", n, strike=50.0)
        assert (book_premium / book_collateral) * (DAYS_PER_YEAR / 30) == pytest.approx(one)


def test_a_richer_credit_scores_higher_and_a_wider_collateral_scores_lower():
    """Guards a constant-returning mutation: the number must move with BOTH terms."""
    base = annualized_return_on_collateral(
        strategy="cash_secured_put", net_credit=2.0, days_to_expiry=30, strike=50.0)
    richer = annualized_return_on_collateral(
        strategy="cash_secured_put", net_credit=4.0, days_to_expiry=30, strike=50.0)
    dearer = annualized_return_on_collateral(
        strategy="cash_secured_put", net_credit=2.0, days_to_expiry=30, strike=100.0)
    longer = annualized_return_on_collateral(
        strategy="cash_secured_put", net_credit=2.0, days_to_expiry=90, strike=50.0)
    assert richer > base > dearer
    assert longer < base  # the same credit earned over 3x the time is a third of the rate


def test_the_pennies_case_scores_near_zero():
    """The mechanism this gate exists to stop: a 1-cent credit on a $290 cash-secured put."""
    arc = annualized_return_on_collateral(
        strategy="cash_secured_put", net_credit=0.01, days_to_expiry=30, strike=290.0)
    assert arc < 0.005                                  # under half a percent a year
    assert not admits_credit_structure(arc, 0.15)       # a 15 %/yr floor rejects it
    assert admits_credit_structure(arc, None)           # ...and today's no-floor behaviour does not


# ---------------------------------------------------------------------------
# 3. Unknown is neither 0 nor infinite
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("dte", [0, -1, -30, None, float("nan"), float("inf")])
def test_a_non_positive_dte_is_unmeasurable_not_infinite(dte):
    """365/0 is infinite, and infinity admits any credit at all -- on exactly the expiry with
    the least time left to work."""
    arc = annualized_return_on_collateral(
        strategy="cash_secured_put", net_credit=2.0, days_to_expiry=dte, strike=50.0)
    assert arc is None
    assert not admits_credit_structure(arc, 0.15)


@pytest.mark.parametrize("credit", [None, -0.5, float("nan"), float("inf")])
def test_an_absent_or_negative_credit_is_unmeasurable(credit):
    """A negative credit is a DEBIT -- not this structure at all."""
    assert annualized_return_on_collateral(
        strategy="cash_secured_put", net_credit=credit, days_to_expiry=30,
        strike=50.0) is None


def test_a_zero_credit_is_measurably_zero_not_unknown():
    """The distinction matters: a real 0 % return must be rejected ON ITS MERITS by any
    positive floor, and must still be ADMITTED by a floor of zero."""
    arc = annualized_return_on_collateral(
        strategy="cash_secured_put", net_credit=0.0, days_to_expiry=30, strike=50.0)
    assert arc == 0.0
    assert not admits_credit_structure(arc, 0.01)
    assert admits_credit_structure(arc, 0.0)


@pytest.mark.parametrize("mult", [None, 0, -100, "100", True, float("nan"), float("inf")])
def test_an_unreadable_multiplier_is_unmeasurable(mult):
    """Read it per contract, never assume 100, and treat an unreadable one as unmeasurable."""
    assert annualized_return_on_collateral(
        strategy="cash_secured_put", net_credit=2.0, days_to_expiry=30, strike=50.0,
        multiplier=mult) is None


def test_a_multiplier_the_reserve_table_does_not_price_is_unmeasurable():
    """``option_reserve_required`` bakes x100 into every branch. An adjusted 10-share contract
    would have its premium scaled by 10 against a 100-share collateral -- a 10x misstatement,
    on exactly the contracts nobody inspects."""
    assert annualized_return_on_collateral(
        strategy="cash_secured_put", net_credit=2.0, days_to_expiry=30, strike=50.0,
        multiplier=10.0) is None
    assert annualized_return_on_collateral(
        strategy="cash_secured_put", net_credit=2.0, days_to_expiry=30, strike=50.0,
        multiplier=100.0) is not None


def test_an_unknown_strategy_is_unmeasurable_not_free():
    """``option_reserve_required`` RAISES for an unrecognised strategy (correctly -- an
    unknown capital requirement must never read as zero). Here that must become None rather
    than escaping into the middle of a rule evaluation and taking the whole bar's gates with
    it."""
    assert collateral_per_contract("frobnicate", strike=50.0) is None
    assert annualized_return_on_collateral(
        strategy="frobnicate", net_credit=2.0, days_to_expiry=30, strike=50.0) is None


def test_a_missing_sizing_input_is_unmeasurable():
    """A cash-secured put with no strike: the reserve helper raises rather than pricing it."""
    assert collateral_per_contract("cash_secured_put") is None
    assert annualized_return_on_collateral(
        strategy="cash_secured_put", net_credit=2.0, days_to_expiry=30) is None


def test_a_zero_collateral_is_unmeasurable_not_infinite():
    """Long/debit structures reserve nothing, so there is no denominator. Returning 0.0 here
    would divide by zero; returning "infinite return" would admit every debit as the richest
    trade on the board."""
    assert OptionsAccountInterface.option_reserve_required("long_call", 1) == 0.0
    assert collateral_per_contract("long_call") is None
    assert annualized_return_on_collateral(
        strategy="long_call", net_credit=2.0, days_to_expiry=30) is None


def test_a_credit_at_least_as_wide_as_the_spread_is_unmeasurable():
    """A computed zero max-loss is arithmetic, not an absent field -- but it is still a zero
    denominator, so the ratio does not exist."""
    assert OptionsAccountInterface.option_reserve_required(
        "bull_put_spread", 1, spread_width=5.0, net_credit=5.0) == 0.0
    assert annualized_return_on_collateral(
        strategy="bull_put_spread", net_credit=5.0, days_to_expiry=30,
        spread_width=5.0) is None


# ---------------------------------------------------------------------------
# 4. The gate decision
# ---------------------------------------------------------------------------
def test_no_floor_configured_admits_everything_including_unknown():
    """Today's behaviour, preserved as the opt-in default."""
    assert admits_credit_structure(None, None)
    assert admits_credit_structure(0.0, None)
    assert admits_credit_structure(5.0, None)


def test_an_explicit_zero_floor_is_a_configured_gate_not_an_absent_one():
    """The distinction that stops "unknown" sneaking through the permissive end."""
    assert admits_credit_structure(0.0, 0.0)
    assert not admits_credit_structure(None, 0.0)


@pytest.mark.parametrize("floor,arc,expected", [
    (0.15, 0.16, True),
    (0.15, 0.15, True),     # the floor itself passes
    (0.15, 0.149, False),
    (0.15, 0.0, False),
    (1.0, 2.0, True),
])
def test_the_floor_actually_gates(floor, arc, expected):
    assert admits_credit_structure(arc, floor) is expected


@pytest.mark.parametrize("floor", ["0.15", float("nan"), float("inf"), object()])
def test_an_unreadable_floor_refuses(floor):
    """A misconfigured gate must not silently disable the criterion the operator asked for."""
    assert not admits_credit_structure(0.99, floor)


# ---------------------------------------------------------------------------
# 5. Which structures the gate applies to
# ---------------------------------------------------------------------------
def test_it_applies_to_every_reserving_structure_and_no_debit_one():
    for strategy in sorted(OptionsAccountInterface.RESERVING_STRATEGIES):
        assert applies_to(strategy), strategy
    for strategy in sorted(OptionsAccountInterface.ZERO_RESERVE_STRATEGIES):
        assert not applies_to(strategy), strategy


def test_both_halves_are_non_empty():
    assert OptionsAccountInterface.RESERVING_STRATEGIES
    assert OptionsAccountInterface.ZERO_RESERVE_STRATEGIES


def test_an_unknown_strategy_is_not_silently_exempted():
    """``applies_to`` says False for an unknown strategy, but the ARC is still None, so a
    configured floor REFUSES it rather than waving it through as "gate does not apply"."""
    assert not applies_to("frobnicate")
    arc = annualized_return_on_collateral(
        strategy="frobnicate", net_credit=2.0, days_to_expiry=30, strike=50.0)
    assert not admits_credit_structure(arc, 0.15)


# ---------------------------------------------------------------------------
# 6. The floor is carried to the action config (so it can become a gene)
# ---------------------------------------------------------------------------
def test_option_min_arc_reaches_the_action_config():
    """``rule_builders`` must forward the rule's ``option_min_arc`` into the ``min_arc`` key
    the option action reads, or the floor can never leave the ruleset."""
    from ba2_common.core.rule_builders import action_from_rule

    cfg = action_from_rule({"action_type": "open_bull_put_spread", "option_min_arc": 0.2})["act"]
    assert cfg["min_arc"] == 0.2


def test_an_absent_floor_is_absent_from_the_config():
    """Not 0.0 -- an unset floor must stay unset, so ``admits_credit_structure`` sees None and
    keeps today's behaviour."""
    from ba2_common.core.rule_builders import action_from_rule

    cfg = action_from_rule({"action_type": "open_bull_put_spread"})["act"]
    assert "min_arc" not in cfg


def test_every_credit_builder_consults_the_gate():
    """DRIFT GUARD, and the successor to this file's old "not yet enforced" marker.

    That marker asserted ``admits_credit_structure`` was ABSENT from ``TradeActions``, and
    was written to fail the moment enforcement landed -- the deliberate signal to emit the
    GA gene. Enforcement has landed (``_refuse_if_arc_below_floor``, called by all eight
    credit builders) and the gene is emitted
    (``ba2test_launcher._OPTION_ARC_BANDS`` / ``_apply_option_min_arc_gene``), so the
    marker is replaced by its inverse: a NEW credit builder that forgets the call is the
    thing that can still go wrong.

    Source-level on purpose. The BEHAVIOUR is pinned in
    ``tests/test_option_arc_gate_enforced.py``, which drives every builder end to end; this
    catches the ninth builder nobody added a behavioural test for.
    """
    import inspect

    from ba2_common.core import TradeActions

    src = inspect.getsource(TradeActions)
    for cls_name in ("SellCashSecuredPutAction", "OpenBearCallSpreadAction",
                     "OpenBullPutSpreadAction", "OpenShortStraddleAction",
                     "OpenShortStrangleAction", "OpenIronCondorAction",
                     "OpenJadeLizardAction", "OpenPutRatioSpreadAction"):
        body = inspect.getsource(getattr(TradeActions, cls_name))
        assert "_refuse_if_arc_below_floor" in body, (
            f"{cls_name} admits its structure on net_credit > 0 alone -- it can still learn "
            f"to sell near-worthless premium (OPT-C1)")
    assert "admits_credit_structure" in src


def test_the_helper_is_importable_from_the_package_root():
    """Sanity: the enforcement patch will import it from here."""
    import ba2_common.core.option_economics as mod

    assert callable(mod.annualized_return_on_collateral)
    assert callable(mod.admits_credit_structure)
    assert math.isclose(mod.DAYS_PER_YEAR, 365.0)

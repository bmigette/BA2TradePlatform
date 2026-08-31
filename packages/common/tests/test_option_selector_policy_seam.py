"""The production seam: ``select_single`` / ``select_vertical_spread`` route through
``SelectionPolicy`` when a NON-DEFAULT policy is supplied — and change NOTHING otherwise.

THE SEAM IS THE POINT (F17). ``pick``/``pick_with_reason`` had zero production callers: every
entry builder went straight to ``_pick_by``, so a GA-tuned weight would have been a gene the
simulation cannot see — the exact defect the whole option track keeps finding (the dead roll
gene, the whitelist-dropped knobs). These tests pin that the seam exists, that it is inert by
default, and that the applicability report actually fires where a payoff weight cannot rank.
"""
import logging
from datetime import date

import pytest

from ba2_common.core.option_payoff import PayoffLeg
from ba2_common.core.option_selection_policy import SelectionPolicy
from ba2_common.core.option_selector import (
    _pick_by, select_single, select_vertical_spread,
)
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight, OrderDirection

TODAY = date(2024, 3, 4)
NEAR = date(2024, 3, 18)     # 14 DTE
FAR = date(2024, 4, 15)      # 42 DTE


def c(strike, mid, *, expiry=FAR, delta=None, iv=0.30, vol=100):
    return OptionContract(symbol=f"X{strike:g}-{expiry:%y%m%d}", underlying="X",
                          option_type=OptionRight.CALL, strike=float(strike), expiry=expiry,
                          bid=mid, ask=mid, last=None, implied_volatility=iv, delta=delta,
                          volume=vol)


#: Realistic ladder: premium decays with strike; deltas descend. The 0.30-delta box centre
#: sits on the 105; the RICHER premium sits on the 100.
#: The 110 sits NEAR the box centre (|0.22-0.30| < |0.45-0.30|) so a cheap-premium tilt
#: can outweigh its small distance penalty; the sign test would otherwise only prove the
#: sign nudges scores, not that it can change a pick against the box weight.
CHAIN = [c(100, 2.80, delta=0.45), c(105, 1.55, delta=0.30), c(110, 0.80, delta=0.22)]

ARGS = dict(method="delta", strike_param=0.30, spot=100.0, option_type=OptionRight.CALL,
            dte_min=5, dte_max=60, today=TODAY)


# =========================================================================== #
# 1. inert by default — the no-op, three ways
# =========================================================================== #
def test_no_policy_returns_the_legacy_pick_identically():
    legacy = _pick_by("delta", CHAIN, 0.30, 100.0, None, OptionRight.CALL)
    assert select_single(CHAIN, **ARGS) is legacy


def test_a_default_policy_returns_the_legacy_pick_identically():
    legacy = _pick_by("delta", CHAIN, 0.30, 100.0, None, OptionRight.CALL)
    assert select_single(CHAIN, **ARGS, policy=SelectionPolicy()) is legacy


def test_a_default_policy_never_even_enters_the_policy_machinery(monkeypatch):
    """The zero-weight skip is load-bearing (27us legacy vs 99.5us policy default, per
    structure per bar per symbol) — a default policy must take the legacy path, not merely
    reach the same answer through the slow one."""
    from ba2_common.core import option_selection_policy as osp

    def _boom(*a, **k):
        raise AssertionError("policy machinery entered for a default policy")

    monkeypatch.setattr(osp, "pick_with_reason", _boom)
    assert select_single(CHAIN, **ARGS, policy=SelectionPolicy()) is not None


# =========================================================================== #
# 2. a non-default policy GOVERNS the pick
# =========================================================================== #
def test_w_premium_moves_the_pick_off_the_box_centre():
    """+w_premium prefers the richer 100 over the 0.30-delta 105 the legacy selector pins."""
    legacy = select_single(CHAIN, **ARGS)
    rich = select_single(CHAIN, **ARGS, policy=SelectionPolicy(w_premium=2.0))
    assert legacy.strike == 105
    assert rich.strike == 100
    assert rich is not legacy


def test_signed_w_premium_prefers_cheap_where_positive_prefers_rich():
    """THE SIGN FIX, at the seam. A debit member must be able to express 'prefer cheaper'."""
    rich = select_single(CHAIN, **ARGS, policy=SelectionPolicy(w_premium=2.0))
    cheap = select_single(CHAIN, **ARGS, policy=SelectionPolicy(w_premium=-2.0))
    assert rich.strike == 100
    assert cheap.strike == 110
    assert rich is not cheap


def test_the_dte_window_still_binds_under_a_policy():
    """The policy chooses INSIDE the candidate set; it must never resurrect a contract the
    DTE filter excluded, however rich it is."""
    juicy_but_late = c(100, 9.99, expiry=date(2024, 9, 20), delta=0.45)
    got = select_single(CHAIN + [juicy_but_late], **ARGS,
                        policy=SelectionPolicy(w_premium=2.0))
    assert got.expiry != date(2024, 9, 20)


def test_w_profit_scores_through_a_supplied_structure_fn():
    """The builder's closure flows through the seam into PolicyContext: with it, w_profit
    ranks a credit vertical's absolute credit and picks the far-dated 105 over the
    near-dated one w_premium's annualised richness prefers."""
    def _credit_vertical(cand):
        return [PayoffLeg(kind="call", side=OrderDirection.SELL, premium=cand.mid,
                          strike=cand.strike),
                PayoffLeg(kind="call", side=OrderDirection.BUY, premium=0.10,
                          strike=cand.strike + 5.0)]

    two_expiry = [c(105, 0.90, expiry=NEAR, delta=0.27), c(105, 1.55, expiry=FAR, delta=0.30)]
    prof = select_single(two_expiry, **ARGS,
                         policy=SelectionPolicy(w_box_center=0.0, w_profit=1.0),
                         structure_fn=_credit_vertical)
    prem = select_single(two_expiry, **ARGS,
                         policy=SelectionPolicy(w_box_center=0.0, w_premium=1.0),
                         structure_fn=_credit_vertical)
    assert prof.expiry == FAR
    assert prem.expiry == NEAR


# =========================================================================== #
# 3. the applicability report (F17) — wired, shared, and never a demotion
# =========================================================================== #
def test_an_inapplicable_payoff_weight_is_reported_and_inert(caplog, monkeypatch):
    """No structure_fn + a live w_profit: the column cannot rank, the report says so, and
    the pick is EXACTLY what the same policy without the dead weight selects."""
    # ba2_common's package logger sets propagate=False (ba2_common/logger.py) so its records
    # stop at its own handlers and never reach caplog's root handler; re-enable propagation
    # for this one test so the assertion can SEE the report it is pinning.
    monkeypatch.setattr(logging.getLogger("ba2_common"), "propagate", True)
    with caplog.at_level(logging.INFO, logger="ba2_common.core.option_selector"):
        got = select_single(CHAIN, **ARGS, policy=SelectionPolicy(w_profit=2.0))
    baseline = select_single(CHAIN, **ARGS)
    assert got is baseline, "an inapplicable feature demoted or moved the pick"
    assert any("inapplicable" in r.getMessage() for r in caplog.records), (
        "nothing recorded that w_profit was inert for this pick — an inert gene and a "
        "live-but-unhelpful one now look identical in the results")


def test_the_payoff_pass_is_computed_once_and_shared(monkeypatch):
    """The report and the ranking must describe the SAME numbers: one payoff_columns pass,
    handed to both (5039us each on a 200-row chain — and a stateful closure could otherwise
    make the report disagree with the behaviour it reports on)."""
    from ba2_common.core import option_selection_policy as osp

    calls = []
    real = osp.payoff_columns

    def _counting(cands, ctx):
        calls.append(1)
        return real(cands, ctx)

    monkeypatch.setattr(osp, "payoff_columns", _counting)

    def _long_call(cand):
        return [PayoffLeg(kind="call", side=OrderDirection.BUY, premium=cand.mid,
                          strike=cand.strike)]

    select_single(CHAIN, **ARGS, policy=SelectionPolicy(w_profit=1.0),
                  structure_fn=_long_call)
    assert sum(calls) == 1, f"payoff pass ran {sum(calls)}x; report and ranking may diverge"


# =========================================================================== #
# 4. the vertical seam
# =========================================================================== #
VCHAIN = [c(100, 2.80, delta=0.45), c(105, 1.55, delta=0.30), c(110, 0.80, delta=0.18),
          c(115, 0.40, delta=0.10)]
VARGS = dict(method="delta", long_param=0.45, short_param=0.18, spot=100.0,
             option_type=OptionRight.CALL, dte_min=5, dte_max=60, today=TODAY)


def test_the_vertical_defaults_to_the_legacy_pair():
    assert (select_vertical_spread(VCHAIN, **VARGS)
            == select_vertical_spread(VCHAIN, **VARGS, policy=SelectionPolicy()))


def test_a_weight_moves_the_vertical_legs_too():
    """4 of 15 members select through select_vertical_spread; a weight wired only into
    select_single would be a dead gene for every one of them."""
    base = select_vertical_spread(VCHAIN, **VARGS)
    moved = select_vertical_spread(VCHAIN, **VARGS,
                                   policy=SelectionPolicy(w_premium=-2.0))
    assert base is not None and moved is not None
    assert moved != base


def test_the_SHORT_leg_is_policy_governed_not_only_the_long():
    """Kills the mutant where only the LONG leg routes through the policy. Hand-derived so
    the long pick is INVARIANT under the weight: the 0.45-delta 100 is both nearest the
    long box centre and the richest contract, so legacy and +w_premium agree on it — any
    difference in the pair can only come from the SHORT pick, where the tilt prefers the
    richer 105 over the box-nearest 110."""
    chain = [c(100, 5.0, delta=0.45), c(105, 2.0, delta=0.25), c(110, 0.8, delta=0.18)]
    args = dict(method="delta", long_param=0.45, short_param=0.18, spot=100.0,
                option_type=OptionRight.CALL, dte_min=5, dte_max=60, today=TODAY)
    base_long, base_short = select_vertical_spread(chain, **args)
    moved_long, moved_short = select_vertical_spread(
        chain, **args, policy=SelectionPolicy(w_premium=2.0))
    assert (base_long.strike, base_short.strike) == (100, 110)
    assert moved_long.strike == 100, "the long pick was constructed to be invariant"
    assert moved_short.strike == 105, "w_premium never reached the SHORT leg pick"

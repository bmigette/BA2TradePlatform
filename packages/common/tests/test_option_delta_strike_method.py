"""``strike_method="delta"`` — the LEGACY-PATH cause-naming gap (2026-09-01).

Delta strike selection itself is NOT new: ``_pick_by``'s ``method == "delta"`` branch
(nearest ``|delta|`` to ``|target|``, ties broken by ``option_selector._tie`` — strike then
expiry) and the ``SelectionPolicy`` path's ``_no_candidate_reason`` both shipped with
``option-selection-modes``, before this file existed, and are already exercised by
``test_option_selection_pick.py``, ``test_option_selection_policy_noop.py``,
``test_option_selection_policy_features.py`` and ``test_strike_method_registry.py``. THIS
FILE DOES NOT RE-PROVE ANY OF THAT — see those modules for general nearest-pick correctness
and the shared tie-break contract every method uses.

What IS new here (Task 1, redefined 2026-09-01 after the original brief turned out to
describe already-shipped work): on the LEGACY path (``policy=None``/default — what every one
of the nine ``strike_method``-honouring builders actually runs under; the ``SelectionPolicy``
weights are opt-in and off by default), a ``None`` pick used to collapse into the SAME generic
"No liquid <structure>" message regardless of WHY it failed — an empty/DTE-filtered/illiquid
chain and a chain that carries no delta data at all were indistinguishable, so a grid
post-mortem on an O_LEAPC-style job could not tell "skip this symbol" (a data outage) from
"skip this method" (this vendor never publishes delta for this chain) apart. THE FIX:
``option_selector.describe_pick_failure`` (a pure helper, gated on ``method == "delta"``
FIRST so nothing else pays for the re-filter) plus
``_OptionEntryAction._pick_refusal_message`` (the one seam all nine builders call from inside
their existing ``if contract/pair is None`` branch, instead of nine hand-written cause checks
that could each drift from each other).

Covers, and ONLY covers:
  * cause-naming — an all-null-delta chain names the method; a merely-illiquid/DTE-filtered
    chain does not (it still gets the generic message).
  * partial-null skip — a null-delta candidate is dropped, never scored as ``delta == 0``.
  * the tie rule the legacy delta path uses (documented and pinned explicitly, since the
    other suites pin it only via the ``SelectionPolicy`` no-op equivalence).
  * the byte-identical pin — every non-delta refusal is untouched, verbatim, by this change.
"""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from ba2_common.core import TradeActions
from ba2_common.core.option_selector import _pick_by, describe_pick_failure, select_single
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight, OrderRecommendation

TODAY = date(2024, 3, 4)
EXP = TODAY + timedelta(days=30)
EXP2 = TODAY + timedelta(days=31)


def _c(strike, right=OptionRight.CALL, *, delta=0.30, bid=1.0, ask=1.2, volume=100,
      expiry=EXP):
    return OptionContract(
        symbol=f"X{int(strike * 1000):08d}{expiry:%y%m%d}", underlying="X", option_type=right,
        strike=float(strike), expiry=expiry, bid=bid, ask=ask, last=None,
        implied_volatility=0.25, delta=delta, volume=volume)


# ---------------------------------------------------------------------------------------
# describe_pick_failure — the pure cause-naming helper
# ---------------------------------------------------------------------------------------

def test_all_null_delta_chain_names_the_method():
    chain = [_c(95, delta=None), _c(100, delta=None), _c(105, delta=None)]
    reason = describe_pick_failure(chain, method="delta", option_type=OptionRight.CALL,
                                   dte_min=1, dte_max=60, today=TODAY)
    assert reason is not None
    assert "delta" in reason
    assert "strike_method" in reason


def test_merely_illiquid_chain_does_not_name_delta():
    # Real deltas throughout — the DTE window just excludes every contract (the same
    # `_candidates` filter the liquidity gates run through). The cause is "nothing survives
    # this window", not "no delta data"; the generic message must stand.
    chain = [_c(100, delta=0.30, expiry=TODAY + timedelta(days=400))]
    reason = describe_pick_failure(chain, method="delta", option_type=OptionRight.CALL,
                                   dte_min=1, dte_max=60, today=TODAY)
    assert reason is None


def test_non_delta_method_never_names_a_cause_even_on_an_all_null_chain():
    # THE PERF-ACCEPTANCE GATE: method == 'delta' is checked BEFORE the chain is re-filtered.
    # A percent_otm pick over the exact same all-null-delta chain gets no reason at all —
    # delta being absent is irrelevant to percent_otm.
    chain = [_c(95, delta=None), _c(100, delta=None), _c(105, delta=None)]
    reason = describe_pick_failure(chain, method="percent_otm", option_type=OptionRight.CALL,
                                   dte_min=1, dte_max=60, today=TODAY)
    assert reason is None


def test_a_chain_with_some_deltas_present_is_not_the_all_null_cause():
    # Reached only when _pick_by would already have found something to return — production
    # never asks this question here — but describe_pick_failure must still answer honestly
    # rather than over-claiming the all-null cause for a chain that is only partly null.
    chain = [_c(95, delta=None), _c(100, delta=0.30)]
    reason = describe_pick_failure(chain, method="delta", option_type=OptionRight.CALL,
                                   dte_min=1, dte_max=60, today=TODAY)
    assert reason is None


# ---------------------------------------------------------------------------------------
# partial-null skip: a null delta is dropped, never scored as delta == 0
# ---------------------------------------------------------------------------------------

def test_null_delta_candidate_is_skipped_not_treated_as_zero():
    # Target is deep OTM (0.05). If a null delta were coerced to 0.0, this candidate would
    # beat every real one here (distance 0.05 vs >=0.15) and win — proving the null must be
    # filtered out of the running, not scored as the closest thing to zero.
    null_leg = _c(50, delta=None)          # would "win" at delta==0.0
    real_near = _c(100, delta=0.20)        # the actual nearest — distance 0.15
    real_far = _c(110, delta=0.60)
    chosen = _pick_by("delta", [null_leg, real_near, real_far], 0.05, 100.0, None,
                      OptionRight.CALL)
    assert chosen is real_near


def test_partial_null_chain_still_selects_via_select_single():
    chain = [_c(50, delta=None), _c(100, delta=0.20), _c(110, delta=0.60)]
    chosen = select_single(chain, method="delta", strike_param=0.05, spot=100.0,
                           option_type=OptionRight.CALL, dte_min=1, dte_max=60, today=TODAY)
    assert chosen is not None and chosen.strike == 100.0


# ---------------------------------------------------------------------------------------
# THE TIE RULE (documented): among candidates tied on |delta - target| distance, the lower
# strike wins; a further tie on strike falls to the earliest expiry. This is the SAME
# convention every other method uses (``option_selector._tie``, "expiry is the FINAL
# tie-break on every pick") — the delta method does not invent a new rule, it inherits this
# one, and that inheritance is what these two tests pin.
# ---------------------------------------------------------------------------------------

def test_tied_delta_distance_breaks_to_the_lower_strike():
    a = _c(110, delta=0.40)
    b = _c(100, delta=0.20)   # same |delta - 0.30| distance as `a`
    chosen = _pick_by("delta", [a, b], 0.30, 100.0, None, OptionRight.CALL)
    assert chosen is b


def test_tied_delta_and_strike_break_to_the_earliest_expiry():
    a = _c(100, delta=0.20, expiry=EXP2)
    b = _c(100, delta=0.20, expiry=EXP)
    chosen = _pick_by("delta", [a, b], 0.30, 100.0, None, OptionRight.CALL)
    assert chosen is b


# ---------------------------------------------------------------------------------------
# end to end through a real builder: the refusal message a grid post-mortem actually reads
# ---------------------------------------------------------------------------------------

class _Acct:
    """Minimal options account double: serves a chain, a price, a balance."""

    def __init__(self, chain):
        self._chain = chain

    def get_option_chain(self, symbol, expiry_min, expiry_max, option_type):
        return [c for c in self._chain if c.option_type == option_type]

    def get_instrument_current_price(self, symbol, price_type=None):
        return 100.0

    def get_balance(self):
        return 100_000.0

    def submit_option_order(self, **kw):
        raise AssertionError("a refusal test must never reach the broker")


def _recommendation():
    return SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                           expected_profit_percent=None,
                           recommended_action=OrderRecommendation.BUY)


def _buy_call(chain, **kw):
    from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
    acct = _Acct(chain)
    acct.__class__ = type("_A", (_Acct, OptionsAccountInterface), {})
    params = dict(strike_method="delta", strike_param=0.30, dte_min=1, dte_max=60, sizing=10.0)
    params.update(kw)
    return TradeActions.BuyCallAction(
        "X", acct, OrderRecommendation.BUY, expert_recommendation=_recommendation(), **params)


@pytest.fixture(autouse=True)
def _own_db(tmp_path):
    """These tests persist TradeActionResult rows (``_result`` is not pure) — same isolation
    guard as ``test_option_resolve_split.py``, for the same reason."""
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "delta_strike_method.sqlite"))
    db.init_db()
    yield


def test_buy_call_names_delta_as_the_cause_when_the_chain_has_none():
    today = date.today()
    chain = [_c(95, OptionRight.CALL, delta=None, expiry=today + timedelta(days=30)),
             _c(105, OptionRight.CALL, delta=None, expiry=today + timedelta(days=30))]
    result = _buy_call(chain).execute()
    assert result["success"] is False
    assert "delta" in result["message"]
    assert "strike_method" in result["message"]


def test_buy_call_dte_filtered_chain_keeps_the_generic_message_under_delta():
    # A real, delta-bearing chain — but every contract sits outside the DTE window, so the
    # candidate list the picker ever sees is empty. Same shape as a thin/illiquid chain
    # (option_selector._candidates runs the DTE filter and the liquidity gates the same way);
    # must NOT be reported as a missing-delta cause.
    far = _c(100, OptionRight.CALL, delta=0.30, expiry=date.today() + timedelta(days=400))
    result = _buy_call([far], dte_min=1, dte_max=60).execute()
    assert result["success"] is False
    assert result["message"] == "No liquid call contract for X"


def test_buy_call_dte_filtered_chain_keeps_the_generic_message_under_percent_otm():
    # THE MUTATION-(c) PIN, verbatim: the exact same refused shape, but a non-delta method —
    # the message must be byte-identical to what it always was, unaffected by this change.
    far = _c(100, OptionRight.CALL, delta=0.30, expiry=date.today() + timedelta(days=400))
    result = _buy_call([far], strike_method="percent_otm", strike_param=5.0,
                       dte_min=1, dte_max=60).execute()
    assert result["success"] is False
    assert result["message"] == "No liquid call contract for X"


def test_buy_call_empty_chain_is_still_the_pre_existing_short_circuit():
    # The `_resolve`-level "Empty option chain" refusal fires before any selector is ever
    # called, so it is untouched by this change on any method — pinned for completeness.
    result = _buy_call([]).execute()
    assert result["success"] is False
    assert result["message"] == "Empty option chain for X"

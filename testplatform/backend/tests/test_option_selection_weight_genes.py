"""The SelectionPolicy weight genes: domains first (Task 7), wiring second (Task 10).

TASK 7 — THE ``w_premium`` SIGN FIX (design 2026-08-29 §8). §9.5 originally gave
``w_premium`` the domain 0.0–2.0 on the claim that premium richness has an unambiguous
good direction. It does not: a premium SELLER wants rich premium and a BUYER wants cheap —
the identical asymmetry that made ``w_iv`` the one signed weight. Unsigned, a debit member
can only ever express "prefer richer", so the gene is half dead across the entire debit
half. The domain here is the behaviour change, pinned before any gene is emitted.

TASK 10 — EMISSION, SHARING, AND THE DEAD-GENE GUARD. The three weights are emitted as
``optsel:<half>:<w>`` genes, ONE per debit/credit half (the design's sharing tier: share on
semantics, not convenience — reusing the launcher's asserted-total
``_DEBIT_OPTION_MEMBERS``/``_CREDIT_OPTION_MEMBERS`` partition), decoded onto every member
action of that half, and proven to move a REAL builder's selected contract end to end:
genome -> decode -> per-trial config -> rule builder -> evaluator kwargs -> policy -> pick.
"""
import importlib.util
import os
import sys
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LAUNCHER_PATH = os.path.join(os.path.dirname(_BACKEND), "ba2test_launcher.py")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


def _launcher():
    spec = importlib.util.spec_from_file_location("lch_selw", _LAUNCHER_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_selw"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


L = _launcher()

from app.services.strategy_param_space import (  # noqa: E402
    _collect_action_genes, collect_param_space, decode_params,
)

WEIGHTS = ("w_premium", "w_iv", "w_rvol")
MEMBERS = sorted(L._OPTION_STRATS)


def _half_of(member):
    return "debit" if member in L._DEBIT_OPTION_MEMBERS else "credit"


def _group_halves():
    """Every group and its half, DERIVED — a new group is covered without editing this file.

    Hardcoding ``[("OS1", "debit"), ...]`` made ``MEMBERS`` (derived) and the groups (not)
    disagree about what "all of them" means: adding OS5 to ``_OPTION_GROUPS`` would have left
    every group assertion below silently testing the old four, which is the dead-coverage
    failure this suite exists to catch in the launcher.

    The half comes from the group's own MEMBERS rather than from ``_DEBIT_OPTION_KINDS``,
    which would just be a second hardcoded table: a group IS its members, so its half is
    theirs. A group whose members straddle the debit/credit line has no single half to share a
    gene on, so it fails here rather than picking one — that is a real defect in the grouping,
    not a case to tolerate.
    """
    out = []
    for key, members in sorted(L._OPTION_GROUPS.items()):
        halves = {_half_of(m) for m in members}
        assert len(halves) == 1, (
            f"group {key} straddles the debit/credit line ({sorted(halves)}); its members "
            f"cannot share one selection-weight gene")
        out.append((key, halves.pop()))
    return out


GROUP_HALVES = _group_halves()


def test_the_group_halves_are_the_six_expected_ones():
    """Pins the DERIVATION against the table it replaced, so deriving it did not quietly
    change what is covered. Update this only when a group is genuinely added or removed.

    O_CONVEX (plan Task 13, added 2026-09-02) is a genuine addition: the convex-harvest
    grid's call/put group, both members debit. O_LEAP (the grid-2 LEAPS merge, same day) is
    the second: its two arms are a bought call and a bought put, both DEBIT, which is exactly
    why they can share one ``optsel:debit`` weight set. Both sort AFTER the OS* keys
    (``sorted()`` on the group-key string decides at position 1: '_' (0x5F) > 'S' (0x53)), and
    between themselves "O_CONVEX" < "O_LEAP" ('C' < 'L')."""
    assert GROUP_HALVES == [("OS1", "debit"), ("OS2", "credit"),
                            ("OS3", "credit"), ("OS4", "debit"),
                            ("O_CONVEX", "debit"), ("O_LEAP", "debit")]


def _optsel(space):
    return {k for k in space if k.startswith("optsel:")}


# ------------------------------------------------------------------------------------------ #
# Task 7 — the domains, and the sign
# ------------------------------------------------------------------------------------------ #
def test_w_premium_domain_is_signed_so_the_debit_half_can_prefer_cheap():
    """THE SIGN FIX. -2.0 .. 2.0, per the design's §9.5 correction (§8). An unsigned domain
    would leave a long-call arm unable to express 'prefer the cheaper contract' at all."""
    lo, hi, step = L._OPTION_SELECTION_WEIGHT_BANDS["w_premium"]
    assert (lo, hi) == (-2.0, 2.0)
    assert step > 0


def test_w_iv_stays_signed():
    """Sellers want rich vol, buyers cheap vol — the design's original signed weight."""
    lo, hi, _ = L._OPTION_SELECTION_WEIGHT_BANDS["w_iv"]
    assert (lo, hi) == (-2.0, 2.0)


def test_w_rvol_is_unsigned_because_nobody_wants_an_illiquid_contract():
    lo, hi, _ = L._OPTION_SELECTION_WEIGHT_BANDS["w_rvol"]
    assert (lo, hi) == (0.0, 2.0)


def test_every_band_samples_zero_exactly_so_the_GA_can_switch_a_weight_off():
    """0.0 is the no-op level (``score_all`` skips zero weights entirely); a lattice that
    cannot land on it exactly would deny the GA the control arm every other option gene
    has, and would make the un-searched run a configuration no trial can reproduce."""
    for name, (lo, hi, step) in L._OPTION_SELECTION_WEIGHT_BANDS.items():
        levels = [round(lo + i * step, 10) for i in range(int(round((hi - lo) / step)) + 1)]
        assert 0.0 in levels, f"{name}: 0.0 is not on the sampled lattice {levels}"
        assert hi in levels, f"{name}: the top of the band is unreachable"


def test_the_bands_cover_exactly_the_emitted_weights():
    """w_spread / w_profit / w_rr are deliberately NOT here — each is withheld on recorded
    evidence (see the table's comment and the withheld-weight tests below). A band with no
    gene would be dead configuration; a gene with no band would crash collection."""
    assert set(L._OPTION_SELECTION_WEIGHT_BANDS) == {"w_premium", "w_iv", "w_rvol"}


def test_the_searched_domain_is_the_domain_the_LIVE_EDITOR_ENFORCES():
    """ONE SOURCE OF TRUTH for the weight domains, across two trees.

    The launcher samples inside these bounds and the live rule editor
    (``ui/pages/settings.py``) refuses outside them, reading
    ``option_selection_policy.WIRED_WEIGHT_BANDS``. If the two drift, one of two things is
    true and neither is visible from either side alone: the GA can produce a genome the live
    editor would reject (a winner that cannot be deployed), or a human can type a weight no
    genome could contain (a live rule no backtest can reproduce). The steps stay the
    launcher's own -- a lattice is a search concern and the editor has no opinion on it.
    """
    from ba2_common.core.option_selection_policy import WIRED_WEIGHT_BANDS

    assert set(L._OPTION_SELECTION_WEIGHT_BANDS) == set(WIRED_WEIGHT_BANDS)
    for name, (lo, hi, _step) in L._OPTION_SELECTION_WEIGHT_BANDS.items():
        assert (lo, hi) == WIRED_WEIGHT_BANDS[name], (
            f"{name}: the GA searches [{lo}, {hi}] but the live editor enforces "
            f"{WIRED_WEIGHT_BANDS[name]}")


# ------------------------------------------------------------------------------------------ #
# Task 10 §1 — emission and the sharing tier
# ------------------------------------------------------------------------------------------ #
@pytest.mark.parametrize("kind", MEMBERS)
def test_every_pure_option_member_searches_the_three_weights_for_its_half(kind):
    half = _half_of(kind)
    space = collect_param_space(L._build_strategy_option(kind))
    for w, (lo, hi, step) in L._OPTION_SELECTION_WEIGHT_BANDS.items():
        spec = space.get(f"optsel:{half}:{w}")
        assert spec is not None, f"{kind}: optsel:{half}:{w} is not searched"
        assert (spec["min"], spec["max"], spec["step"]) == (lo, hi, step)
        assert spec["type"] == "float"
    other = "credit" if half == "debit" else "debit"
    assert not any(k.startswith(f"optsel:{other}:") for k in space), (
        f"{kind} emits weight genes for the half it is not in")


@pytest.mark.parametrize("group,half", GROUP_HALVES)
def test_a_group_searches_ONE_weight_gene_per_half_not_one_per_member(group, half):
    """The sharing tier. Naive per-member wiring on OS1 would be 15 weight genes; the
    sharing rule makes it 3 — and every launcher group is single-half, so 3 is the whole
    bill per group."""
    space = collect_param_space(L._build_strategy_option_group(group))
    assert _optsel(space) == {f"optsel:{half}:{w}" for w in WEIGHTS}


def test_single_and_group_jobs_use_the_SAME_weight_gene_keys():
    """The seeding requirement: a stage-1 single-structure winner is later encoded into the
    stage-2 group space, and encode_params silently drops keys the target space lacks."""
    single = _optsel(collect_param_space(L._build_strategy_option("O_LC")))
    group = _optsel(collect_param_space(L._build_strategy_option_group("OS1")))
    assert single == group


def test_the_equity_overlay_strategies_emit_no_weight_genes():
    """O_CC / O_PP option legs are overlays outside the debit/credit member partition the
    sharing tier is defined over; a gene there would have no half to share with."""
    for kind, build in (("O_CC", L._build_strategy_covered_call),
                        ("O_PP", L._build_strategy_protective_put)):
        assert not _optsel(collect_param_space(build(kind))), f"{kind} emits weight genes"


@pytest.mark.parametrize("kind", MEMBERS + [g for g, _ in GROUP_HALVES])
def test_the_withheld_weights_are_never_emitted(kind):
    """w_rr (F15: rho 0.98-1.0 with premium within a chain — operator decision), w_spread
    (both grid stores serve degenerate spreads: sqlite bid==ask on all rows, parquet no
    bid/ask at all — a uniform column cannot move any pick), and w_profit (no builder
    supplies the structure_fn it needs, so its emission set is empty today; the
    discrimination evidence below is recorded for the day one does). A gene the GA can
    never move is budget burned on a dead search dimension."""
    build = (L._build_strategy_option_group if kind in L._OPTION_GROUPS
             else L._build_strategy_option)
    space = collect_param_space(build(kind))
    offenders = [k for k in space
                 if k.endswith((":w_profit", ":w_rr", ":w_spread", ":w_box_center"))]
    assert not offenders, f"{kind} emits withheld weight genes: {offenders}"


def test_conflicting_shared_domains_are_refused_at_collection():
    """Two members of one half declaring different domains for the same shared gene would
    otherwise resolve by dict-overwrite — last member silently wins."""
    def _action(**over):
        a = {"option_selection_half": "debit", "option_w_premium_optimize": True,
             "option_w_premium_min": -2.0, "option_w_premium_max": 2.0,
             "option_w_premium_step": 0.5}
        a.update(over)
        return a

    out = {}
    _collect_action_genes("entry", "r1", 0, _action(), out)
    with pytest.raises(ValueError, match="conflicting"):
        _collect_action_genes("entry", "r2", 0, _action(option_w_premium_max=1.0), out)


def test_a_weight_flag_without_a_half_is_a_loud_config_error():
    """A weight gene with no half has nothing to key its sharing on; silently emitting a
    per-rule gene instead would fork the single/group key shapes and break seeding."""
    out = {}
    with pytest.raises(ValueError, match="half"):
        _collect_action_genes("entry", "r1", 0,
                              {"option_w_premium_optimize": True,
                               "option_w_premium_min": -2.0, "option_w_premium_max": 2.0,
                               "option_w_premium_step": 0.5}, out)


def test_an_unknown_member_is_refused_rather_than_stamped_credit():
    """THE OTHER END OF THE SAME REFUSAL. ``_collect_action_genes`` (above) refuses a weight
    flag with no half; the launcher's stamper must refuse a member with no half, instead of
    defaulting it.

    ``_DEBIT_OPTION_MEMBERS | _CREDIT_OPTION_MEMBERS`` is asserted total at import, so this
    can never fire in a real run — which is exactly why it needs a test: an
    ``else "credit"`` is invisible while the assertion holds and silently gives a new member
    the wrong half's premium thesis the moment it does not. Nothing else in the suite can
    reach that branch, because every real member is in the partition by construction.
    """
    with pytest.raises(ValueError, match="neither the debit nor the credit"):
        L._apply_option_selection_weight_genes({}, "O_NOT_A_REAL_MEMBER")


def test_the_stamper_still_gives_every_real_member_its_partition_half():
    """The refusal must not have narrowed the accepted set: every member still stamps, and
    stamps the half the partition says."""
    for m in MEMBERS:
        cfg = L._apply_option_selection_weight_genes({}, m)
        assert cfg["option_selection_half"] == _half_of(m), m


# ------------------------------------------------------------------------------------------ #
# Task 10 §2 — decode lands on every member action of the half, and only that half
# ------------------------------------------------------------------------------------------ #
def _entry_actions(decoded):
    return [a for r in (decoded["entry_rules"] or []) for a in (r.get("actions") or [])]


def test_a_decoded_weight_lands_on_every_member_action_of_its_half():
    decoded = decode_params(L._build_strategy_option_group("OS1"),
                            {"optsel:debit:w_premium": -1.5})
    actions = _entry_actions(decoded)
    assert len(actions) == len(L._OPTION_GROUPS["OS1"])
    assert all(a.get("option_w_premium") == -1.5 for a in actions), (
        "a shared gene must reach EVERY member action of its half")


def test_a_weight_for_the_other_half_touches_no_action():
    decoded = decode_params(L._build_strategy_option_group("OS1"),
                            {"optsel:credit:w_premium": -1.5})
    assert all("option_w_premium" not in a for a in _entry_actions(decoded))


def test_an_undecoded_run_leaves_the_actions_free_of_weight_values():
    """Absent means the default policy — the selector's legacy path, byte-identical picks.
    An authored 0.0 would also be a no-op, but absence keeps the evaluator forwarding
    nothing at all, which is the exact pre-Task-10 ctor input."""
    decoded = decode_params(L._build_strategy_option("O_LC"), {})
    for a in _entry_actions(decoded):
        for w in WEIGHTS:
            assert f"option_{w}" not in a


# ------------------------------------------------------------------------------------------ #
# Task 10 §3 — THE DEAD-GENE GUARD, one test per emitted gene, full chain:
# genome -> decode -> per-trial config (the whitelist hop) -> rule builder -> evaluator
# kwargs -> real builder -> a DIFFERENT selected contract.
# ------------------------------------------------------------------------------------------ #
from ba2_common.core.TradeActionEvaluator import _OPTION_ENTRY_PARAM_KEYS  # noqa: E402
from ba2_common.core.TradeActions import create_action  # noqa: E402
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface  # noqa: E402
from ba2_common.core.option_types import OptionContract  # noqa: E402
from ba2_common.core.rule_builders import action_from_rule  # noqa: E402
from ba2_common.core.types import ExpertActionType, OptionRight  # noqa: E402

TODAY = date(2024, 6, 1)
EXPIRY = TODAY + timedelta(days=30)


@pytest.fixture()
def _own_db(tmp_path):
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "weight_guard.sqlite"))
    db.init_db()
    yield


def _c(underlying, strike, px, option_type, *, iv=0.30, vol=1000):
    return OptionContract(
        symbol=f"{underlying}{strike:g}{'C' if option_type == OptionRight.CALL else 'P'}",
        underlying=underlying, option_type=option_type, strike=float(strike),
        expiry=EXPIRY, bid=px, ask=px, last=px, implied_volatility=iv,
        open_interest=1000, volume=vol)


class _ChainAccount(OptionsAccountInterface):
    """Options account serving ONE hand-built chain in the historical store's degenerate
    bid==ask shape. ``spec`` rows: (strike, px, {field overrides})."""

    def __init__(self, spec, spot=100.0, balance=50_000_000.0):
        self.id = 1
        self.spot = spot
        self._balance = balance
        self.spec = spec
        self.submitted = []

    def open_option_orders_book_wide(self):
        return []

    def get_balance(self):
        return self._balance

    def get_account_snapshot(self):
        from ba2_common.core.account_types import AccountSnapshot
        return AccountSnapshot(cash=self._balance, equity=self._balance,
                               net_liquidation=self._balance)

    def _as_of_date(self):
        return TODAY

    def get_instrument_current_price(self, symbol, price_type=None):
        return self.spot

    def get_current_price(self, symbol=None):
        return self.spot

    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type,
                         strike_min=None, strike_max=None):
        return [_c(underlying, s, px, option_type, **over) for s, px, over in self.spec]

    def submit_option_order(self, *, legs, quantity, order_type, limit_price,
                            option_strategy, expert_recommendation_id=None,
                            transaction_id=None):
        self.submitted.append(dict(quantity=quantity, limit_price=limit_price,
                                   strategy=option_strategy, legs=list(legs)))
        return SimpleNamespace(id=len(self.submitted), data={})

    def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
        return trading_order

    def get_option_quote(self, contract_symbol):
        return None

    def get_atm_implied_volatility(self, underlying):
        return 0.3

    def get_option_positions(self):
        return []

    def close_option_position(self, position, order_type="limit", limit_price=None):
        return None


def _minimal_backtest_cfg(strategy):
    return {
        "backtest_id": 7, "start_date": "2024-02-01", "end_date": "2024-02-29",
        "enabled_instruments": ["XYZ"], "warmup_days": 0, "seed": 42,
        "experts": [{"class": "FMPRating", "settings": {}}],
        "initial_capital": 20000.0, "account_settings": {"starting_cash": 20000.0},
        "options_cache_db": "/tmp/whatever.sqlite",
        "entry_action": getattr(strategy, "entry_action", None),
    }


def _picked_strikes(kind, gene, value):
    """Run the WHOLE chain for one genome {gene: value} and return the strikes the real
    builder submitted. The strike/DTE genes are pinned identically in every genome so the
    ONLY difference between two calls is the weight under test."""
    from app.services.strategy_optimization_handler import _build_daily_trial_config

    member = kind.lower()
    strat = L._build_strategy_option(kind)
    space = collect_param_space(strat)
    assert gene in space, f"{gene} is not in {kind}'s emitted space"
    lo, hi = space[gene]["min"], space[gene]["max"]
    assert lo <= value <= hi, f"{value} is outside {gene}'s emitted domain [{lo}, {hi}]"

    flat = {f"entry:{member}-entry:a0:option_strike_param": 0.0,
            f"entry:{member}-entry:a0:option_dte": 30,
            gene: value}
    decoded = decode_params(strat, flat)

    # THE WHITELIST HOP. The per-trial config is rebuilt key by key; a knob that does not
    # survive into it is inert while every upstream log claims it works.
    trial = _build_daily_trial_config(_minimal_backtest_cfg(strat), decoded)
    acts = [a for r in trial["entry_rules"] for a in (r.get("actions") or [])]
    assert len(acts) == 1
    weight_key = "option_" + gene.split(":")[-1]
    assert acts[0].get(weight_key) == value, (
        f"{weight_key}={value} did not survive into the per-trial config")

    # rule dict -> evaluator action config -> the exact ctor kwargs the evaluator forwards.
    cfg = action_from_rule(acts[0])["act"]
    kwargs = {k: cfg[k] for k in _OPTION_ENTRY_PARAM_KEYS if k in cfg}

    is_credit = _half_of(kind) == "credit"
    ladders = {
        # calls: premium decays with strike; puts: premium grows with strike. IV smiles away
        # from the money on the side being sold; volume concentrates on one strike.
        "w_premium": ([(100, 4.0, {}), (102, 0.4, {})] if not is_credit
                      else [(90, 1.8, {}), (100, 3.6, {}), (110, 9.0, {})]),
        "w_iv": ([(95, 5.0, {"iv": 0.60}), (100, 4.0, {"iv": 0.30}),
                  (105, 3.2, {"iv": 0.25})] if not is_credit
                 else [(80, 0.9, {"iv": 0.65}), (90, 1.8, {"iv": 0.45}),
                       (100, 3.6, {"iv": 0.30}), (110, 9.0, {"iv": 0.22})]),
        "w_rvol": ([(95, 5.0, {"vol": 12000}), (100, 4.0, {"vol": 40}),
                    (105, 3.2, {"vol": 30})] if not is_credit
                   else [(95, 3.0, {"vol": 12000}), (100, 3.6, {"vol": 40}),
                         (105, 4.4, {"vol": 30})]),
    }
    acct = _ChainAccount(ladders[gene.split(":")[-1]])
    rec = SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                          expected_profit_percent=None, recommended_action=None)
    action = create_action(ExpertActionType(cfg["action_type"]), "XYZ", acct,
                           SimpleNamespace(), None, rec, **kwargs)
    action.submit_to_broker = True
    res = action.execute()
    assert res["success"] is True, res["message"]
    return sorted(leg.strike for leg in acct.submitted[-1]["legs"])


# O_LC is the debit half's seam (buy_call -> select_single); O_CSP the credit half's
# (sell_cash_secured_put -> select_single). The halves' genes are DISTINCT genes.
@pytest.mark.parametrize("gene,kind,off,on", [
    ("optsel:debit:w_premium", "O_LC", 0.0, -2.0),
    ("optsel:debit:w_iv", "O_LC", 0.0, 2.0),
    ("optsel:debit:w_rvol", "O_LC", 0.0, 2.0),
    ("optsel:credit:w_premium", "O_CSP", 0.0, 2.0),
    ("optsel:credit:w_iv", "O_CSP", 0.0, 2.0),
    ("optsel:credit:w_rvol", "O_CSP", 0.0, 2.0),
], ids=["optsel_debit_w_premium", "optsel_debit_w_iv", "optsel_debit_w_rvol",
        "optsel_credit_w_premium", "optsel_credit_w_iv", "optsel_credit_w_rvol"])
def test_the_emitted_gene_moves_a_real_pick_end_to_end(_own_db, gene, kind, off, on):
    """THE DEAD-GENE GUARD (design §9). A weight that cannot change the contract selected
    on a recorded chain is a gene the GA burns budget on and can never move — the failure
    this codebase has already paid for twice (the dead roll gene; the whitelist-dropped
    knobs)."""
    assert _picked_strikes(kind, gene, off) != _picked_strikes(kind, gene, on), (
        f"{gene} decoded, survived the trial config, reached the builder — and did not "
        f"move the pick: a dead gene")


# ------------------------------------------------------------------------------------------ #
# Task 10 §4 — the F15 record: w_profit discrimination evidence
# ------------------------------------------------------------------------------------------ #
def test_w_profit_discrimination_evidence_the_F15_record():
    """The operator's F15 decision demanded evidence before w_profit could be emitted:
    within a SINGLE expiry, profit and premium are near-collinear and pick identically
    (the review's rho 0.98-1.0); across the MULTI-EXPIRY candidate sets the grid's >=14-day
    DTE windows always produce, the two genuinely diverge — premium is annualised
    (mark/strike x 365/dte) while profit is absolute dollars, so a near-dated thin credit
    outranks a far-dated fat one on premium and loses to it on profit, in both directions.

    THE GENE IS WITHHELD ANYWAY, on the other ground: its emission set is empty. w_profit
    scores only through PolicyContext.structure_fn and NO entry builder supplies one (the
    plan scopes teaching them out; the _size_by_cost premium-vs-max-loss gap must be closed
    with each closure). Emitting it now would be a gene that is provably discriminating in
    a unit test and provably inert in every real pick. When a builder is taught its
    closure, this test is the evidence that the gene is worth emitting for it."""
    from ba2_common.core.option_payoff import PayoffLeg
    from ba2_common.core.option_selection_policy import (
        PolicyContext, SelectionPolicy, pick,
    )
    from ba2_common.core.types import OrderDirection

    def call(strike, mid, expiry, delta):
        return OptionContract(symbol=f"E{strike:g}-{expiry}", underlying="E",
                              option_type=OptionRight.CALL, strike=float(strike),
                              expiry=expiry, bid=mid, ask=mid, last=None,
                              implied_volatility=0.3, delta=delta, volume=100)

    def credit_vertical(cand):
        return [PayoffLeg(kind="call", side=OrderDirection.SELL, premium=cand.mid,
                          strike=cand.strike),
                PayoffLeg(kind="call", side=OrderDirection.BUY, premium=0.10,
                          strike=cand.strike + 5.0)]

    today, near, far = date(2024, 3, 4), date(2024, 3, 18), date(2024, 4, 15)
    ctx = PolicyContext(strike_method="delta", today=today, target=0.30,
                        structure_fn=credit_vertical)
    profit_only = SelectionPolicy(w_box_center=0.0, w_profit=1.0)
    premium_only = SelectionPolicy(w_box_center=0.0, w_premium=1.0)

    # Candidate set 1 — single expiry, descending premium ladder: COLLINEAR, same pick.
    one_expiry = [call(100, 2.80, far, 0.45), call(105, 1.55, far, 0.30),
                  call(110, 0.80, far, 0.18)]
    assert pick(one_expiry, ctx, profit_only) is pick(one_expiry, ctx, premium_only)

    # Candidate set 2 — two expiries, same strike: the annualisation denominator splits
    # them. Premium prefers the near-dated (richer per day); profit the far-dated (more
    # absolute credit).
    two_expiry = [call(105, 0.90, near, 0.27), call(105, 1.55, far, 0.30)]
    assert pick(two_expiry, ctx, premium_only).expiry == near
    assert pick(two_expiry, ctx, profit_only).expiry == far

    # Both directions: holding one weight fixed, moving the OTHER changes the pick.
    held_prem = SelectionPolicy(w_box_center=0.0, w_premium=1.0)
    prem_plus_profit = SelectionPolicy(w_box_center=0.0, w_premium=1.0, w_profit=2.0)
    assert pick(two_expiry, ctx, held_prem) is not pick(two_expiry, ctx, prem_plus_profit)
    held_prof = SelectionPolicy(w_box_center=0.0, w_profit=1.0)
    prof_plus_prem = SelectionPolicy(w_box_center=0.0, w_profit=1.0, w_premium=2.0)
    assert pick(two_expiry, ctx, held_prof) is not pick(two_expiry, ctx, prof_plus_prem)

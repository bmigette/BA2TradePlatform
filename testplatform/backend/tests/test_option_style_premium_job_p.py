"""Stage-2 job P (style ``premium``) and the O_STYLE dead-gene pins.

* ``_STYLE_DEAD_GENES`` (proven dead 2026-11-04) leave the O_STYLE search and ONLY the O_STYLE search.
* ``premium`` (status approved 2026-11-04) in tools/option_styles.stage2.json: members, DTE floor 5, no price cap,
  a risk block under which a fully cash-secured CSP is admitted by the option rails.
* tools/stage2_seed_policy.jobP.json is a valid policy for it.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "testplatform"))

import ba2test_launcher as L  # noqa: E402

_ROOT = Path(__file__).resolve().parents[3]
_STAGE2 = str(_ROOT / "tools/option_styles.stage2.json")
_POLICY_P = _ROOT / "tools/stage2_seed_policy.jobP.json"

DEAD = ["model:k_stop", "model:risk_per_trade_pct", "model:atr_risk_budget_pct", "model:atr_multiplier",
        "model:min_stop_loss_pct", "model:regime_risk_scale", "model:regime_stop_scale",
        "model:regime_tp_scale", "schedule:saturday", "schedule:sunday"]
MEMBERS = ["O_CSP", "O_BULLPS", "O_JL", "O_RS", "O_WHEEL", "O_CC", "O_PP", "O_IC"]


@pytest.fixture(autouse=True)
def _no_style_leaks(monkeypatch):
    monkeypatch.setattr(L, "_STYLE_ACTIVE", None)
    yield


def _expert_cfg(kind):
    spec = L._EXPERT_OPT["DeterministicScorer"]
    return L._pin_style_dead_genes(kind, {
        **spec["expert_params"], **L._rm_opt_for(kind),
        **{f"schedule:{k}": v for k, v in L._SCHEDULE_DAY_OPT.items()}})


def _space(kind, cfg):
    from app.services.strategy_param_space import collect_param_space

    strat = L._build_strategy(kind, "t", "DeterministicScorer")
    model = {k: v for k, v in cfg.items() if not k.startswith("schedule:")}
    sched = {k[9:]: v for k, v in cfg.items() if k.startswith("schedule:")}
    return strat, collect_param_space(strat, expert_cfg=model, schedule_cfg=sched)


# ------------------------------------------------------------------ Task 1: dead genes
@pytest.mark.parametrize("style", ["mid", "premium"])
def test_style_space_has_none_of_the_dead_genes(style):
    L._STYLE_ACTIVE = L._load_style_spec(_STAGE2, style)
    _, space = _space("O_STYLE", _expert_cfg("O_STYLE"))
    assert not [g for g in DEAD if g in space]
    # the LIVE ones stay (DS target -> exp_profit gate) and so do the weekday schedule genes
    for live in ("model:k_target", "model:atr_period", "schedule:monday", "schedule:friday"):
        assert live in space, live


@pytest.mark.parametrize("kind", ["O_LC", "O_CSP", "S2"])
def test_non_style_jobs_keep_every_gene(kind):
    cfg = _expert_cfg(kind)
    assert "k_stop" in cfg and "risk_per_trade_pct" in cfg and "schedule:saturday" in cfg
    assert L._pin_style_dead_genes(kind, cfg) is cfg


def test_pinned_genes_decode_to_the_experts_declared_defaults():
    """An absent gene is simply not an override: the expert's own declared default reaches the settings."""
    from app.services.strategy_param_space import decode_params

    L._STYLE_ACTIVE = L._load_style_spec(_STAGE2, "premium")
    strat, space = _space("O_STYLE", _expert_cfg("O_STYLE"))
    genome = {g: (d["choices"][0] if d["type"] == "choice" else d["min"]) for g, d in space.items()}
    out = decode_params(strat, genome)
    overrides = out["expert_overrides"]
    for gene in DEAD:
        assert gene.split(":", 1)[1] not in overrides, gene
    assert "k_target" in overrides and "atr_period" in overrides
    assert not out["schedule_by_day"].get("saturday", False) if "schedule_by_day" in out else True


def test_dead_gene_keys_are_the_namespaced_pins():
    assert sorted(L._style_dead_gene_keys()) == sorted(DEAD)


# ------------------------------------------------------------------ Task 2: the premium style
def test_premium_is_approved_with_the_expected_members_in_order():
    style = L._load_style_spec(_STAGE2, "premium")
    assert style["status"] == "approved"
    assert [m["key"] for m in style["members"]] == MEMBERS
    assert style["price_cap_policy"] == "none"
    assert style["risk"]["fixed"]["undefined_risk_max_pct"] == 100


def test_premium_dte_window_never_starts_below_five():
    """option_dte decodes to centre +/- max(authored half-width, 7): every searched centre >= 12 keeps
    the window minimum >= 5 (strategy_param_space._apply_option_dte)."""
    L._STYLE_ACTIVE = L._load_style_spec(_STAGE2, "premium")
    strat = L._build_strategy("O_STYLE", "t", "DeterministicScorer")
    checked = 0
    for rule in strat.entry_rules:
        for a in rule["actions"]:
            if not a.get("option_dte_optimize"):
                continue
            hw = max((a["option_dte_max"] - a["option_dte_min"]) // 2, 7)
            assert a["option_dte_min_range"] - hw >= 5, (rule["id"], a)
            assert a["option_dte_max_range"] <= 30
            checked += 1
    assert checked >= 7        # CSP BULLPS JL RS WHEEL + IC's two joint arms


def test_premium_full_notional_members_can_use_all_the_cash():
    L._STYLE_ACTIVE = L._load_style_spec(_STAGE2, "premium")
    _, space = _space("O_STYLE", _expert_cfg("O_STYLE"))
    for m in ("o_csp-entry", "o_jl-entry", "o_rs-entry", "o_wheel-o_csp-entry"):
        assert space[f"entry:{m}:a0:option_sizing"]["max"] == 100.0, m
    assert space["model:max_virtual_equity_per_instrument_percent"]["max"] == 100.0
    assert space["model:max_deployment_pct"]["max"] == 100.0


def test_a_fully_cash_secured_csp_is_admitted_only_with_the_premium_risk_block():
    """A short put is undefined risk to the rails (naked sub-cap): job A's undefined_risk_max_pct 0 would
    refuse it; job P's 100 admits a CSP that reserves the whole $20k, and the assignment-cash rail still
    refuses one more dollar than the cash."""
    from ba2_common.core.option_book import (BookTotals, BreakerState, CandidateStructure, RAIL_OK,
                                             RAIL_UNDEFINED_RISK, check_rails)

    risk = L._load_style_spec(_STAGE2, "premium")["risk"]
    settings = {**risk["fixed"], "max_deployment_pct": 100, "max_notional_leverage": 1}
    empty = BookTotals(0.0, 0.0, 0.0, 0.0, 0.0, 0, frozenset())
    csp = CandidateStructure("AAA", "short_put", 19_500.0, 19_500.0, 19_500.0, False)
    ok = check_rails(csp, empty, 20_000.0, settings, BreakerState(), 20_000.0)
    assert ok.allowed and ok.reason == RAIL_OK
    refused = check_rails(csp, empty, 20_000.0, {**settings, "undefined_risk_max_pct": 0},
                          BreakerState(), 20_000.0)
    assert not refused.allowed and refused.reason == RAIL_UNDEFINED_RISK
    assert not check_rails(csp, empty, 20_000.0, settings, BreakerState(), 19_000.0).allowed


# ------------------------------------------------------------------ validator for the sizing overrides
def _write(tmp_path, members):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"schema": 1, "styles": {
        "s": {"status": "draft", "members": members, "price_cap_policy": "none"}}}))
    return str(p)


def test_draft_status_is_accepted(tmp_path):
    assert L._load_style_spec(_write(tmp_path, [{"key": "O_CSP"}]), "s")["status"] == "draft"


def test_sizing_override_accepted_and_applied(tmp_path):
    path = _write(tmp_path, [{"key": "O_CSP", "action_overrides": {
        "option_sizing_min": 10, "option_sizing_max": 100, "option_sizing_step": 10}}])
    L._STYLE_ACTIVE = L._load_style_spec(path, "s")
    strat = L._build_strategy("O_STYLE", "t", "DeterministicScorer")
    a = strat.entry_rules[0]["actions"][0]
    assert (a["option_sizing_min"], a["option_sizing_max"], a["option_sizing_step"]) == (10, 100, 10)


@pytest.mark.parametrize("ov,msg", [
    ({"option_sizing_step": 0}, "must be > 0"),
    ({"option_sizing_min": 60, "option_sizing_max": 50}, "option_sizing_min=60 > option_sizing_max=50"),
    ({"option_sizing_min": 30, "option_sizing_max": 100}, "outside the searched sizing range"),   # authored 20
])
def test_bad_sizing_overrides_are_refused_by_name(tmp_path, ov, msg):
    path = _write(tmp_path, [{"key": "O_CSP", "action_overrides": ov}])
    with pytest.raises(ValueError, match=msg):
        L._load_style_spec(path, "s")
        L._STYLE_ACTIVE = L._load_style_spec(path, "s")
        L._build_strategy("O_STYLE", "t", "DeterministicScorer")


def test_equity_entry_members_refuse_sizing_overrides(tmp_path):
    path = _write(tmp_path, [{"key": "O_CC", "action_overrides": {"option_sizing_max": 100}}])
    with pytest.raises(ValueError, match="equity buy"):
        L._load_style_spec(path, "s")


# ------------------------------------------------------------------ the seed policy
def test_job_p_seed_policy_is_valid_and_names_real_members_and_genes():
    sys.path.insert(0, str(_ROOT / "tools"))
    import build_style_seed_bundle as B

    policy = json.loads(_POLICY_P.read_text(encoding="utf-8"))
    B.validate_policy(policy)
    assert policy["style"] == "premium"
    style = L._load_style_spec(_STAGE2, "premium")
    keys = {m["key"] for m in style["members"]}
    L._STYLE_ACTIVE = style
    L._BENCHMARK_REGIME_GATES = True
    try:
        _, space = _space("O_STYLE", _expert_cfg("O_STYLE"))
    finally:
        L._BENCHMARK_REGIME_GATES = False
    sets = 0
    for ind in policy["individuals"]:
        assert {c["member"] for c in ind["contributions"]} <= keys
        for gene, value in ind.get("set", {}).items():
            assert value in space[gene]["choices"], gene
            sets += 1
    assert sets >= 4


def test_set_values_override_every_other_source():
    from app.services.style_seeding import SeedRefused, assemble_individual

    space = {"model:a": {"type": "int", "min": 0, "max": 9, "step": 1},
             "cond:m-x:mode": {"type": "choice", "choices": ["off", "pullback"], "min": 0, "max": 1, "step": 1}}
    kw = dict(space=space, authored={"model:a": 1, "cond:m-x:mode": "off"}, member_keys={"M": []},
              contributions=[{"member": "M", "genes": {"model:a": 5}}])
    genes, prov = assemble_individual(**kw, set_values={"cond:*-x:mode": "pullback", "model:a": 7})
    assert genes == {"model:a": 7, "cond:m-x:mode": "pullback"}
    assert prov["origin"]["cond:m-x:mode"] == "set"
    with pytest.raises(SeedRefused, match="matches no gene"):
        assemble_individual(**kw, set_values={"nope:*": 1})

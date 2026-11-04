"""Stage-2 job C (style ``long``, DRAFT) and the seed-policy graft.

* ``long`` in tools/option_styles.stage2.json: members/order, draft status, no price cap, the DTE-invariant members
  carry no action_overrides, O_PBS is out, the risk block does not widen job A's rails.
* tools/stage2_seed_policy.jobC.json is a valid policy for it, and assembles offline against the style's real gene
  space (synthetic source genomes: the real stage-1.5 DB is not available in CI).
* the ``donor`` / ``drop`` graft of tools/build_style_seed_bundle.py (option genes from source X, expert genes
  from source Y).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "testplatform"))

import ba2test_launcher as L  # noqa: E402

_ROOT = Path(__file__).resolve().parents[3]
_STAGE2 = str(_ROOT / "tools/option_styles.stage2.json")
_POLICY_C = _ROOT / "tools/stage2_seed_policy.jobC.json"
MEMBERS = ["O_CBS", "O_LEAP", "O_PMCC", "O_CONVEX"]

_spec = importlib.util.spec_from_file_location("build_style_seed_bundle_c",
                                               str(_ROOT / "tools" / "build_style_seed_bundle.py"))
B = importlib.util.module_from_spec(_spec)
sys.modules["build_style_seed_bundle_c"] = B
_spec.loader.exec_module(B)


@pytest.fixture(autouse=True)
def _no_style_leaks(monkeypatch):
    monkeypatch.setattr(L, "_STYLE_ACTIVE", None)
    monkeypatch.setattr(L, "_BENCHMARK_REGIME_GATES", False)
    yield


def _space():
    from app.services.strategy_param_space import collect_param_space

    spec = L._EXPERT_OPT["DeterministicScorer"]
    cfg = L._pin_style_dead_genes("O_STYLE", {
        **spec["expert_params"], **L._rm_opt_for("O_STYLE"),
        **{f"schedule:{k}": v for k, v in L._SCHEDULE_DAY_OPT.items()}})
    strat = L._build_strategy("O_STYLE", "t", "DeterministicScorer")
    model = {k: v for k, v in cfg.items() if not k.startswith("schedule:")}
    sched = {k[9:]: v for k, v in cfg.items() if k.startswith("schedule:")}
    return strat, collect_param_space(strat, expert_cfg=model, schedule_cfg=sched)


# ------------------------------------------------------------------ the style
def test_long_is_a_draft_with_the_expected_members_in_order():
    style = L._load_style_spec(_STAGE2, "long")
    assert style["status"] == "draft"
    assert [m["key"] for m in style["members"]] == MEMBERS
    assert style["price_cap_policy"] == "none"
    assert "O_PBS" not in {m["key"] for m in style["members"]}


def test_dte_invariant_members_carry_no_action_overrides_and_no_gate_is_forced():
    style = L._load_style_spec(_STAGE2, "long")
    for m in style["members"]:
        assert not m.get("action_overrides") and not m.get("exit_overrides"), m
        if m["key"] in L._STYLE_DTE_INVARIANT_MEMBERS:
            assert "action_overrides" not in m
    assert {"O_LEAP", "O_PMCC", "O_CONVEX"} <= set(L._STYLE_DTE_INVARIANT_MEMBERS)
    assert set(MEMBERS) <= L._style_admissible_members()


def test_long_risk_block_keeps_job_a_rails_and_does_not_widen_the_instrument_cap():
    long_, mid = (L._load_style_spec(_STAGE2, s)["risk"] for s in ("long", "mid"))
    assert long_ == mid
    assert long_["genes"]["max_virtual_equity_per_instrument_percent"] == {"min": 5, "max": 25, "step": 5}
    assert long_["fixed"] == {"max_concurrent_structures": 20, "undefined_risk_max_pct": 0}


def test_long_style_builds_and_every_member_keeps_its_authored_long_dte():
    L._STYLE_ACTIVE = L._load_style_spec(_STAGE2, "long")
    strat = L._build_strategy("O_STYLE", "t", "DeterministicScorer")
    dte = {}
    for rule in strat.entry_rules:
        for a in rule["actions"]:
            if a.get("option_dte_optimize"):
                dte[rule["id"]] = (a["option_dte_min"], a["option_dte_max"])
    assert dte and all(lo >= 80 for lo, _ in dte.values()), dte
    assert sorted(strat.style_id_maps) == sorted(MEMBERS)


# ------------------------------------------------------------------ the seed policy
def _policy():
    return json.loads(_POLICY_C.read_text(encoding="utf-8"))


def test_job_c_policy_is_valid_and_names_real_members():
    policy = _policy()
    B.validate_policy(policy)
    assert policy["style"] == "long"
    for ind in policy["individuals"]:
        assert {c["member"] for c in ind["contributions"]} <= set(MEMBERS)
        assert "set" not in ind          # no condition is forced on
    donors = [c["donor"]["source"]["opt_id"] for i in policy["individuals"] for c in i["contributions"]
              if "donor" in c]
    assert set(donors) == {143, 117, 119}
    # no re-run: FMPRating sources are only ever grafted, O_CONVEX comes straight from opt 143 (already DS)
    for ind in policy["individuals"]:
        for c in ind["contributions"]:
            assert ("donor" in c) == (c["member"] != "O_CONVEX"), c["member"]
            if c["member"] == "O_CONVEX":
                assert c["source"]["opt_id"] == 143


_SHARED_PREFIXES = ("model:", "optsel:", "schedule:", "cond:shared-")


def _synthetic_bundle(policy, style_space):
    """Resolve every contribution to a genome built from the style's OWN space (member keys translated back to
    the member's own ids is not needed: the launcher accepts the style key through the identity of the member
    map only for model/optsel/schedule, so build per-member genomes from the member's SINGLE build)."""
    from app.services.strategy_param_space import collect_param_space

    spec = L._EXPERT_OPT["DeterministicScorer"]
    model = {**spec["expert_params"], **L._rm_opt_for("O_LEAP")}
    cache = {}

    def genes(member, with_fmp):
        key = (member, with_fmp)
        if key not in cache:
            strat = L._build_strategy(member, "t", "DeterministicScorer")
            sp = collect_param_space(strat, expert_cfg=dict(model), schedule_cfg={})
            from app.services.strategy_param_space import authored_genome
            g = {k: v for k, v in authored_genome(strat, sp, lattice_anchor="zero").items()}
            if with_fmp:
                g["model:min_analysts"] = 3
            cache[key] = g
        return cache[key]

    bundle = json.loads(json.dumps(policy))
    for ind in bundle["individuals"]:
        for c in ind["contributions"]:
            g = dict(genes(c["member"], False))
            # the DS donor answers the expert genes (model:k_target, w_*, ...): stand in with the style's own
            g.update({k: (cfg["choices"][0] if cfg["type"] == "choice" else cfg["min"])
                      for k, cfg in style_space.items()
                      if k.startswith(_SHARED_PREFIXES)})
            for pat in c.get("drop", []):
                g.pop(pat, None)
            c["genes"] = g
            c["resolved"] = {"opt_id": c["source"]["opt_id"], "opt_name": None, "status": "completed",
                             "pick": "distinct", "rank": c["source"]["rank"], "fitness": 1.0, "trades": 1,
                             "params_key": None, "backtest_id": None}
    return bundle


def test_job_c_policy_assembles_offline_against_the_real_space():
    L._STYLE_ACTIVE = L._load_style_spec(_STAGE2, "long")
    L._BENCHMARK_REGIME_GATES = True
    strat, space = _space()
    bundle = _synthetic_bundle(_policy(), space)
    seeds, prov = L._assemble_seed_population(bundle, strat, space, "zero", 40)
    assert len(seeds) == len(bundle["individuals"]) == 19
    assert all(set(s) == set(space) for s in seeds)
    # single-member seeds switch exactly their own entry rules on and every other member's off
    first = seeds[0]
    assert first["entry:o_cbs-entry:enabled"] == 1
    assert first["entry:o_leapc-entry:enabled"] == 0 and first["entry:o_convexc-entry:enabled"] == 0
    # SPY gates are filled OFF, never seeded on
    assert all(v == "off" for s in seeds for k, v in s.items() if k.startswith("cond:") and k.endswith("-spy-trend:mode"))


# ------------------------------------------------------------------ the graft (donor / drop)
FMP_SPACE = {"cond:a:x": {"type": "int", "min": 1, "max": 9}, "model:min_analysts": {"type": "int", "min": 1, "max": 9},
             "model:k_target": {"type": "float", "min": 1, "max": 9}, "cond:shared-gate_confidence:value": {"type": "int", "min": 1, "max": 9}}
DS_SPACE = {"cond:a:x": {"type": "int", "min": 1, "max": 9}, "model:k_target": {"type": "float", "min": 1, "max": 9},
            "cond:shared-gate_confidence:value": {"type": "int", "min": 1, "max": 9}}
FMP_G = {"cond:a:x": 2, "model:min_analysts": 3, "model:k_target": 1.0, "cond:shared-gate_confidence:value": 45}
DS_G = {"cond:a:x": 8, "model:k_target": 6.0, "cond:shared-gate_confidence:value": 60}


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "t.db"
    c = sqlite3.connect(p)
    c.execute("create table strategy_optimizations (id integer primary key, name text, status text, "
              "parameter_ranges text, best_params text, best_fitness real, all_results text, optimization_config text)")
    c.execute("create table backtests (id integer primary key, name text, status text, strategy_params text, "
              "total_trades int, ga_fitness real, optimization_id int)")
    c.execute("insert into strategy_optimizations values (1,'fmp','completed',?,?,1.0,'[]','{}')",
              (json.dumps(FMP_SPACE), json.dumps(FMP_G)))
    c.execute("insert into strategy_optimizations values (2,'ds','completed',?,?,1.0,'[]','{}')",
              (json.dumps(DS_SPACE), json.dumps(DS_G)))
    c.commit(); c.close()
    return p


def _graft_policy(**contrib):
    c = {"member": "O_LEAP", "source": {"opt_id": 1, "pick": "best"}, **contrib}
    return {"schema": 1, "style": "long", "individuals": [{"contributions": [c], "other_members": "off"}]}


def test_graft_takes_option_genes_from_the_source_and_expert_genes_from_the_donor(db):
    pol = _graft_policy(drop=["model:min_analysts"],
                        donor={"source": {"opt_id": 2, "pick": "best"},
                               "keys": ["model:k_*", "cond:shared-gate_confidence:*"]})
    bundle = B.build_bundle(pol, str(db))
    c = bundle["individuals"][0]["contributions"][0]
    assert c["genes"] == {"cond:a:x": 2, "model:k_target": 6.0, "cond:shared-gate_confidence:value": 60}
    assert c["resolved"]["donor"]["opt_id"] == 2 and c["resolved"]["opt_id"] == 1


@pytest.mark.parametrize("contrib,msg", [
    ({"drop": ["model:nope"]}, "drop pattern"),
    ({"donor": {"source": {"opt_id": 2, "pick": "best"}, "keys": ["model:nope"]}}, "donor pattern"),
    ({"donor": {"source": {"opt_id": 2, "pick": "best"}}}, "donor"),
    ({"drop": []}, "non-empty list"),
    ({"extra": 1}, "unknown keys"),
])
def test_graft_mistakes_are_refused_by_name(db, contrib, msg):
    with pytest.raises(B.Refused, match=msg):
        B.build_bundle(_graft_policy(**contrib), str(db))


def test_a_donor_never_hides_a_source_that_is_not_in_the_donor_space(db):
    """The source keeps every gene the donor patterns do not match (here min_analysts stays unless dropped)."""
    pol = _graft_policy(donor={"source": {"opt_id": 2, "pick": "best"}, "keys": ["model:k_*"]})
    g = B.build_bundle(pol, str(db))["individuals"][0]["contributions"][0]["genes"]
    assert g["model:min_analysts"] == 3 and g["model:k_target"] == 6.0 and g["cond:a:x"] == 2

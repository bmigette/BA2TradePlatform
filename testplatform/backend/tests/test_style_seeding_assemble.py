"""style_seeding.assemble_individual (stage-2 seeding plan 2026-11-04 T5), on the real ``mid`` style space."""
import pytest

from app.services.genetic import GeneticOptimizer
from app.services.strategy_param_space import authored_genome, collect_param_space
from app.services.style_seeding import FILL_AUTHORED, SeedRefused, assemble_individual
from tests.test_authored_genome import _STAGE2
from tests.test_option_grid_foundations import _launcher

MEMBERS = ["O_STRG", "O_STRD", "O_BULLCS", "O_BF", "O_LC"]
_JOINT = {"O_STRG": "joint", "O_STRD": "joint"}
#: what the job-A policy fills: the SPY gates and the market gates did not exist in stage 1
FILL = {"cond:*-spy-*:mode": "off", "cond:*-market-*:mode": "off",
        "cond:*-market-*:value": FILL_AUTHORED, "cond:*-market-*:confirmation_bars": FILL_AUTHORED,
        "exit:*-mkt-*": FILL_AUTHORED, "cond:*-mkt-*": FILL_AUTHORED}


@pytest.fixture(scope="module")
def world():
    # the style, with both market profiles and the SPY regime gates on (the job-A run)
    m = _launcher()
    m._MARKET_CONDITION_PROFILES = ("ohlcv-v1", "ta-structure-v1")
    m._BENCHMARK_REGIME_GATES = True
    m._STYLE_ACTIVE = m._load_style_spec(_STAGE2, "mid")
    mid = m._clamp_confidence_genes(m._build_strategy_style("O_STYLE"), "DeterministicScorer")
    space = dict(collect_param_space(mid))
    authored = authored_genome(mid, space)
    # run-level genes the style space gets from the expert / schedule (hand made)
    space["model:risk_per_trade_pct"] = {"type": "float", "min": 0.5, "max": 3.0, "step": 0.5}
    space["model:max_deployment_pct"] = {"type": "float", "min": 10.0, "max": 100.0, "step": 10.0}
    space["schedule:time"] = {"type": "choice", "choices": ["09:45", "10:00"], "min": 0, "max": 1, "step": 1}
    authored["model:risk_per_trade_pct"] = 1.0
    authored["schedule:time"] = "10:00"
    authored["model:max_deployment_pct"] = 100.0

    # who owns what, from the style's id maps
    owner = {}
    for member in MEMBERS:
        idm = mid.style_id_maps[member]
        for key in space:
            ns, _, rest = key.partition(":")
            if ns == "cond":
                cid = rest.rpartition(":")[0]
                if cid in set(idm["entry_conds"].values()) | set(idm["exit_conds"].values()) \
                        and not cid.startswith("shared-"):
                    owner[key] = member
            elif ns in ("entry", "exit"):
                rid = rest.split(":")[0]
                if rid in set(idm[f"{ns}_rules"].values()):
                    owner[key] = member
    member_keys = {mm: {k for k, o in owner.items() if o == mm} for mm in MEMBERS}

    # stage-1 style sources: each member's own single-job authored genome (no market / SPY gates),
    # translated into the style's keys by the launcher's own translator
    m1 = _launcher()
    contrib_genes = {}
    for member in MEMBERS:
        kw = {"neutral_entry_mode": _JOINT[member]} if member in _JOINT else {}
        single = m1._build_strategy(member, "a", "DeterministicScorer", **kw)
        sg = authored_genome(single, collect_param_space(single))
        contrib_genes[member] = {m._style_translate_key(k, mid.style_id_maps[member], member, space): v
                                 for k, v in sg.items()}
    return {"mid": mid, "space": space, "authored": authored, "member_keys": member_keys,
            "owner": owner, "genes": contrib_genes}


def _contrib(w, member, label=None, **over):
    genes = dict(w["genes"][member])
    genes.update(over)
    return {"member": member, "genes": genes, "label": label or f"seed-{member}"}


def _strict(w, genes):
    opt = GeneticOptimizer(param_ranges=w["space"], population_size=4, n_generations=1)
    return opt.encode_params_strict(genes)


def _assemble(w, contributions, **kw):
    kw.setdefault("fill", FILL)
    return assemble_individual(w["space"], w["authored"], w["member_keys"], contributions, **kw)


def _toggle(k):
    p = k.split(":")
    return len(p) == 3 and p[0] == "entry" and p[2] == "enabled"


def test_the_fixture_is_the_real_mid_space(world):
    sp = world["space"]
    assert any(k.endswith("-spy-trend:mode") for k in sp) and any("-market-" in k for k in sp)
    assert all(world["member_keys"][m] for m in MEMBERS)
    shared = set(sp) - set(world["owner"])
    assert "optsel:debit:w_premium" in shared and "model:risk_per_trade_pct" in shared
    assert any(k.startswith("cond:shared-") for k in shared)


def test_a_seeded_member_is_ON_with_its_genes_and_the_others_at_AUTHORED_defaults(world):
    c = _contrib(world, "O_BULLCS")
    genes, prov = _assemble(world, [c])
    clamped = {x["key"] for x in prov["clamped"]}
    for k in world["member_keys"]["O_BULLCS"]:
        if k in c["genes"]:
            assert prov["origin"][k] == "contribution:0"
            if k not in clamped:
                assert genes[k] == c["genes"][k]
    # stage 1 -> stage 2 range differences clamp (O_BULLCS DTE 25..45 window centre 35 vs the style's 40..90)
    assert "entry:o_bullcs-entry:a0:option_dte" in clamped
    assert genes["entry:o_bullcs-entry:enabled"] == 1
    assert prov["origin"]["entry:o_bullcs-entry:enabled"] == "toggle_on"
    for mm in ("O_LC", "O_BF", "O_STRD", "O_STRG"):
        for k in world["member_keys"][mm]:
            assert genes[k] == world["authored"][k] and prov["origin"][k] == "authored", k
    assert prov["members"] == ["O_BULLCS"]
    _strict(world, genes)


def test_the_result_covers_the_target_space_exactly_and_encodes_strictly(world):
    contribs = [_contrib(world, m) for m in MEMBERS]
    genes, prov = _assemble(world, contribs)
    assert list(genes) == list(world["space"])
    assert set(prov["origin"]) == set(world["space"])
    _strict(world, genes)


def test_a_toggle_the_source_carries_is_kept_not_forced_on(world):
    k = "entry:o_strd-entry-hold:enabled"
    assert k in world["genes"]["O_STRD"]                   # a joint source carries its arm toggles
    genes, prov = _assemble(world, [_contrib(world, "O_STRD", **{k: 0})])
    assert genes[k] == 0 and prov["origin"][k] == "contribution:0"
    # the other arm is carried too (1), neither is a forced toggle_on
    assert prov["origin"]["entry:o_strd-entry-low_confidence:enabled"] == "contribution:0"


def test_other_members_off_turns_their_rule_level_entry_toggles_to_0_only(world):
    genes, prov = _assemble(world, [_contrib(world, "O_LC")], other_members="off")
    for mm in ("O_BF", "O_BULLCS", "O_STRD", "O_STRG"):
        for k in world["member_keys"][mm]:
            if _toggle(k):
                assert genes[k] == 0, k
            else:
                assert genes[k] == world["authored"][k], k
    assert genes["entry:o_lc-entry:enabled"] == 1
    # an action-level toggle of an un-seeded member is not touched
    _strict(world, genes)


def test_shared_keys_come_from_shared_from_and_conflicts_are_REPORTED(world):
    key = "cond:shared-gate_confidence:value"
    cfg = world["space"][key]
    a, b = cfg["min"], cfg["max"]
    contribs = [_contrib(world, "O_LC", **{key: a}), _contrib(world, "O_BF", **{key: b})]
    genes, prov = _assemble(world, contribs, shared_from=0)
    assert genes[key] == a and prov["origin"][key] == "shared:0"
    assert {"key": key, "values": [a, b], "used": 0} in prov["conflicts"]
    genes, prov = _assemble(world, contribs, shared_from=1)
    assert genes[key] == b and prov["origin"][key] == "shared:1"
    assert any(c["key"] == key and c["used"] == 1 for c in prov["conflicts"])
    # agreeing contributions report no conflict on that key
    same = [_contrib(world, "O_LC", **{key: a}), _contrib(world, "O_BF", **{key: a})]
    _, prov = _assemble(world, same)
    assert not [c for c in prov["conflicts"] if c["key"] == key]


def test_shared_from_falls_back_to_the_first_contribution_having_the_key(world):
    key = "model:risk_per_trade_pct"
    contribs = [_contrib(world, "O_LC"), _contrib(world, "O_BF", **{key: 2.0})]
    genes, prov = _assemble(world, contribs, shared_from=0)
    assert genes[key] == 2.0 and prov["origin"][key] == "shared:1"


def test_a_shared_key_no_contribution_has_falls_back_to_fill_then_authored_or_is_refused(world):
    genes, prov = _assemble(world, [_contrib(world, "O_LC")])
    assert prov["origin"]["model:risk_per_trade_pct"] == "authored"
    assert genes["model:risk_per_trade_pct"] == 1.0
    genes, prov = _assemble(world, [_contrib(world, "O_LC")],
                            fill={**FILL, "model:max_deployment_pct": 90.0})
    assert genes["model:max_deployment_pct"] == 90.0 and prov["origin"]["model:max_deployment_pct"] == "fill"
    nope = dict(world["authored"])
    del nope["model:risk_per_trade_pct"]
    with pytest.raises(SeedRefused, match="model:risk_per_trade_pct"):
        assemble_individual(world["space"], nope, world["member_keys"], [_contrib(world, "O_LC")], fill=FILL)


def test_a_seeded_member_gene_its_source_lacks_is_refused_unless_a_fill_pattern_matches(world):
    # no fill at all: the market / SPY gates (new in stage 2) are refused by name
    with pytest.raises(SeedRefused, match=r"seeded member .O_LC.: .cond:o_lc-(market|spy)-"):
        assemble_individual(world["space"], world["authored"], world["member_keys"],
                            [_contrib(world, "O_LC")])
    # filled
    genes, prov = _assemble(world, [_contrib(world, "O_LC")])
    assert genes["cond:o_lc-spy-trend:mode"] == "off" and prov["origin"]["cond:o_lc-spy-trend:mode"] == "fill"
    # FILL_AUTHORED takes that gene's authored value
    mk = [k for k in world["member_keys"]["O_LC"] if "-market-" in k and k.endswith(":value")][0]
    assert genes[mk] == world["authored"][mk]
    # a source gene is never overridden by fill, even when a pattern matches it
    c = _contrib(world, "O_LC")
    k = "cond:o_lc-exp_profit:value"
    g2, p2 = _assemble(world, [c], fill={**FILL, "cond:*-exp_profit:value": 0.0})
    assert g2[k] == c["genes"][k] and p2["origin"][k] == "contribution:0"


def test_an_unseeded_member_key_authored_lacks_falls_back_to_fill_then_refuses(world):
    gone = "cond:o_bf-exp_profit:value"
    authored = {k: v for k, v in world["authored"].items() if k != gone}
    with pytest.raises(SeedRefused, match=gone):
        assemble_individual(world["space"], authored, world["member_keys"], [_contrib(world, "O_LC")], fill=FILL)
    g, p = assemble_individual(world["space"], authored, world["member_keys"], [_contrib(world, "O_LC")],
                               fill={**FILL, gone: 4.0})
    assert g[gone] == 4.0 and p["origin"][gone] == "fill"


def test_two_contributions_for_one_member_are_refused(world):
    with pytest.raises(SeedRefused, match="O_LC"):
        _assemble(world, [_contrib(world, "O_LC"), _contrib(world, "O_LC")])


def test_a_foreign_or_unknown_gene_or_member_is_refused_by_name(world):
    with pytest.raises(SeedRefused, match="cond:o_bf-exp_profit:value"):
        _assemble(world, [_contrib(world, "O_LC", **{"cond:o_bf-exp_profit:value": 4.0})])
    with pytest.raises(SeedRefused, match="cond:ghost:value"):
        _assemble(world, [_contrib(world, "O_LC", **{"cond:ghost:value": 1.0})])
    with pytest.raises(SeedRefused, match="O_NOPE"):
        _assemble(world, [{"member": "O_NOPE", "genes": {}, "label": "x"}])
    with pytest.raises(SeedRefused, match="shared_from"):
        _assemble(world, [_contrib(world, "O_LC")], shared_from=3)


def test_a_choice_gene_must_be_one_of_its_choices(world):
    k = "entry:o_lc-entry:a0:option_strike_method"
    assert world["space"][k]["type"] == "choice"
    with pytest.raises(SeedRefused, match="option_strike_method"):
        _assemble(world, [_contrib(world, "O_LC", **{k: "nonsense"})])
    with pytest.raises(SeedRefused, match="option_strike_method"):
        _assemble(world, [_contrib(world, "O_LC", **{k: "nonsense"})], out_of_range="clamp")


def test_out_of_range_refuse_and_clamp(world):
    k = "entry:o_bullcs-entry:a0:option_dte"
    cfg = world["space"][k]
    way_out = cfg["max"] + 30
    c = _contrib(world, "O_BULLCS", **{k: way_out})
    genes, prov = _assemble(world, [c], out_of_range="clamp")
    assert genes[k] == cfg["max"] and isinstance(genes[k], int)
    assert {"key": k, "from": way_out, "to": cfg["max"]} in prov["clamped"]
    with pytest.raises(SeedRefused, match="option_dte"):
        _assemble(world, [c], out_of_range="refuse")
    _strict(world, genes)
    # below min as well
    low = _contrib(world, "O_BULLCS", **{k: cfg["min"] - 20})
    genes, prov = _assemble(world, [low])
    assert genes[k] == cfg["min"]


def test_an_off_lattice_value_snaps_on_clamp_and_refuses_on_refuse(world):
    k = "entry:o_lc-entry:a0:option_sizing"
    cfg = world["space"][k]
    v = cfg["min"] + cfg["step"] * 2.4
    c = _contrib(world, "O_LC", **{k: v})
    genes, prov = _assemble(world, [c])
    assert abs(genes[k] - (cfg["min"] + 2 * cfg["step"])) < 1e-9
    assert [x for x in prov["clamped"] if x["key"] == k]
    with pytest.raises(SeedRefused, match="option_sizing"):
        _assemble(world, [c], out_of_range="refuse")


def test_ints_as_floats_are_accepted_and_bools_and_junk_are_refused_for_numeric_genes(world):
    k = "entry:o_lc-entry:a0:option_dte"
    genes, prov = _assemble(world, [_contrib(world, "O_LC", **{k: 45.0})])
    assert genes[k] == 45 and isinstance(genes[k], int)
    assert not [x for x in prov["clamped"] if x["key"] == k]
    for bad in (True, "45", None, float("nan")):
        with pytest.raises(SeedRefused, match="option_dte"):
            _assemble(world, [_contrib(world, "O_LC", **{k: bad})])


def test_the_min_anchor_is_used_for_the_decode_image_check(world):
    k = "entry:o_lc-entry:a0:option_dte"
    cfg = world["space"][k]
    genes, _ = _assemble(world, [_contrib(world, "O_LC")], lattice_anchor="min")
    opt = GeneticOptimizer(param_ranges=world["space"], population_size=4, n_generations=1, lattice_anchor="min")
    opt.encode_params_strict(genes)
    assert cfg["min"] <= genes[k] <= cfg["max"]


def test_no_contributions_gives_the_pure_authored_genome(world):
    genes, prov = _assemble(world, [])
    assert genes == {k: world["authored"][k] for k in world["space"]}
    assert prov["members"] == [] and prov["conflicts"] == []
    _strict(world, genes)

"""authored_genome (stage-2 seeding plan 2026-11-04 T4): the template's own value for every strategy gene."""
from pathlib import Path

import pytest

from app.services.strategy_param_space import authored_genome, collect_param_space, decode_params
from tests.test_option_grid_foundations import _launcher

_STAGE2 = str(Path(__file__).resolve().parents[3] / "tools/option_styles.stage2.json")
_STRATEGY_NS = ("cond", "entry", "exit", "optsel")
_ANCHORS = ["zero", "min"]


def _ids(strategy_key, neutral=None):
    return strategy_key, neutral


CASES = [("O_LC", None), ("O_BULLCS", None), ("O_BF", None), ("O_STRD", None), ("O_STRG", None),
         ("O_STRD", "joint"), ("O_STRG", "joint")]


def _build(key, neutral):
    m = _launcher()
    kw = {"neutral_entry_mode": neutral} if neutral else {}
    return m._build_strategy(key, "a", "DeterministicScorer", **kw)


def _build_mid():
    m = _launcher()
    m._MARKET_CONDITION_PROFILES = ("ohlcv-v1", "ta-structure-v1")
    m._BENCHMARK_REGIME_GATES = True
    m._STYLE_ACTIVE = m._load_style_spec(_STAGE2, "mid")
    s = m._build_strategy_style("O_STYLE")
    return m._clamp_confidence_genes(s, "DeterministicScorer")


def _all_strategies():
    out = [(f"{k}-{n or 'legacy'}", _build(k, n)) for k, n in CASES]
    out.append(("mid-style", _build_mid()))
    return out


@pytest.fixture(scope="module")
def strategies():
    return dict(_all_strategies())


def _leaves(node, out=None):
    out = [] if out is None else out
    if isinstance(node, dict):
        if "field" in node:
            out.append(node)
        for c in node.get("conditions") or []:
            _leaves(c, out)
    return out


def _strategy_genes(space):
    return {k for k in space if k.split(":")[0] in _STRATEGY_NS}


@pytest.mark.parametrize("anchor", _ANCHORS)
@pytest.mark.parametrize("name", [f"{k}-{n or 'legacy'}" for k, n in CASES] + ["mid-style"])
def test_every_strategy_gene_gets_a_value_and_passes_strict_encode(strategies, name, anchor):
    from app.services.genetic import GeneticOptimizer

    s = strategies[name]
    space = collect_param_space(s)
    g = authored_genome(s, space, lattice_anchor=anchor)
    assert set(g) == _strategy_genes(space)
    # the model:/schedule:/screener: genes are not authored: add dummies at their minimum
    full = dict(g)
    for k, cfg in space.items():
        if k not in full:
            full[k] = cfg["choices"][0] if cfg["type"] == "choice" else cfg["min"]
    opt = GeneticOptimizer(param_ranges=space, population_size=4, n_generations=1, lattice_anchor=anchor)
    opt.encode_params_strict(full)       # raises on any value the decoder could not reproduce


def test_mid_style_carries_every_namespace_it_should(strategies):
    space = collect_param_space(strategies["mid-style"])
    names = list(space)
    assert any(k.endswith("-spy-trend:mode") for k in names) and any(k.endswith("-spy-vol:mode") for k in names)
    assert any("-market-" in k and k.endswith(":mode") for k in names)
    assert any(k.endswith("-signal:mode") for k in names)
    assert any(k.startswith("optsel:") for k in names)
    assert any(k.endswith(":option_dte") for k in names)


def _step(cfg):
    return cfg.get("step") or 1


def _decoded_vs_template(strategy, space, g):
    """Walk the template against the decode of its authored genome; return the list of deviations."""
    d = decode_params(strategy, g)
    dev = []
    for attr, ns in (("entry_rules", "entry"), ("exit_rules", "exit")):
        dec = {r["id"]: r for r in (d[attr] or [])}
        for rule in getattr(strategy, attr) or []:
            rid = rule["id"]
            if rule.get("enabled") is False:
                assert rid not in dec, f"authored-OFF rule {rid} must stay off"
                continue
            assert rid in dec, f"rule {rid} lost"
            if f"{ns}:{rid}:enabled" in space:
                assert g[f"{ns}:{rid}:enabled"] == 1
            dl = {x["id"]: x for x in _leaves(dec[rid]["conditions"]) if x.get("id")}
            for leaf in _leaves(rule["conditions"]):
                cid = leaf.get("id")
                mkey = f"cond:{cid}:mode"
                if mkey in space:
                    mode = g[mkey]
                    if mode == "off":
                        assert cid not in dl, f"{cid}: mode off must drop the leaf"
                        continue
                    assert cid in dl
                    if mode in ("below", "above"):
                        assert dl[cid]["op"] == leaf["op"] or {"<": "below", "<=": "below", ">": "above",
                                                              ">=": "above"}[dl[cid]["op"]] == mode
                        assert {"<": "below", "<=": "below", ">": "above", ">=": "above"}[leaf["op"]] == mode
                else:
                    assert cid in dl, f"leaf {cid} lost"
                if f"cond:{cid}:value" in space and cid in dl:
                    want, got = leaf["value"], dl[cid]["value"]
                    if abs(want - got) > 1e-9:
                        dev.append((f"cond:{cid}:value", want, got))
            for i, act in enumerate(rule["actions"]):
                da = dec[rid]["actions"][i]
                for gene, tkey, dkey in (("option_strike_param", "option_strike_param", "option_strike_param"),
                                         ("option_sizing", "option_sizing", "option_sizing"),
                                         ("option_entry_cross", "option_entry_cross", "option_entry_cross"),
                                         ("option_wing_width", "option_wing_width_pct", "option_wing_width_pct"),
                                         ("option_strike_delta", "option_strike_delta", "option_strike_delta")):
                    key = f"{ns}:{rid}:a{i}:{gene}"
                    if key in space and gene != "option_strike_delta":
                        if abs(act[tkey] - da[dkey]) > 1e-9:
                            dev.append((key, act[tkey], da[dkey]))
                key = f"{ns}:{rid}:a{i}:option_dte"
                if key in space:
                    for side in ("option_dte_min", "option_dte_max"):
                        if act[side] != da[side]:
                            dev.append((f"{key}[{side}]", act[side], da[side]))
                key = f"{ns}:{rid}:a{i}:option_structure"
                if key in space:
                    assert da["action_type"] == act["action_type"]
                key = f"{ns}:{rid}:a{i}:option_strike_method"
                if key in space:
                    assert da["option_strike_method"] == act["option_strike_method"]
    return dev


#: Template values the decode image cannot give back exactly: (gene pattern) -> (template, decoded). Both
#: anchors agree (their lattices coincide for these ranges). Python's round() is half-to-even, so a
#: template value half a step off the lattice snaps DOWN (5.0 on step 2 -> 4.0); ``td`` 28 on step 5 -> 30.
KNOWN_LATTICE = {
    "cond:*-exp_profit:value": (5.0, 4.0),
    "cond:*td:value": (28.0, 30.0),
    "entry:o_strg-entry*:a0:option_strike_param": (5.0, 4.0),
}


@pytest.mark.parametrize("name", [f"{k}-{n or 'legacy'}" for k, n in CASES] + ["mid-style"])
def test_decoding_the_authored_genome_reproduces_the_template(strategies, name):
    from fnmatch import fnmatch

    s = strategies[name]
    space = collect_param_space(s)
    g = authored_genome(s, space)
    dev = _decoded_vs_template(s, space, g)
    for key, want, got in dev:
        assert any(fnmatch(key, pat) and (want, got) == v for pat, v in KNOWN_LATTICE.items()), (key, want, got)
    assert any(k.endswith("exp_profit:value") for k, _, _ in dev)      # the list is not vacuous


def test_toggles_follow_the_authored_enabled_flag(strategies):
    for name, s in strategies.items():
        space = collect_param_space(s)
        g = authored_genome(s, space)
        for ns, attr in (("entry", "entry_rules"), ("exit", "exit_rules")):
            for r in getattr(s, attr):
                key = f"{ns}:{r['id']}:enabled"
                if key in space:
                    assert g[key] == (0 if r.get("enabled") is False else 1), (name, key)
    s = strategies["O_LC-legacy"]
    space = collect_param_space(s)
    assert authored_genome(s, space)["exit:opt_tp:enabled"] == 1
    for r in s.exit_rules:
        if r["id"] == "opt_tp":
            r["enabled"] = False                      # hand-flipped template (restored below)
    try:
        assert authored_genome(s, space)["exit:opt_tp:enabled"] == 0
    finally:
        for r in s.exit_rules:
            if r["id"] == "opt_tp":
                r.pop("enabled")


def test_mode_genes_take_the_authored_direction(strategies):
    s = strategies["O_LC-legacy"]
    g = authored_genome(s, collect_param_space(s))
    assert g["cond:o_lc-signal:mode"] == "above"                  # bullish member, authored '>'
    mid = strategies["mid-style"]
    gm = authored_genome(mid, collect_param_space(mid))
    assert all(gm[k] == "above" for k in gm if k.endswith("-signal:mode"))
    # numeric market gates take their anchor operator, SPY regime gates (categorical, no value) are off
    spy = [k for k in gm if k.endswith("-spy-trend:mode") or k.endswith("-spy-vol:mode")]
    assert spy and all(gm[k] == "off" for k in spy)
    market = [k for k in gm if "-market-" in k and k.endswith(":mode")]
    assert market and all(gm[k] in ("off", "below", "above") for k in market)


def test_option_dte_is_the_window_centre(strategies):
    s = strategies["O_LC-legacy"]
    g = authored_genome(s, collect_param_space(s))
    assert g["entry:o_lc-entry:a0:option_dte"] == 35        # window 25..45
    assert g["entry:o_lc-entry:a0:option_strike_method"] == "percent_otm"
    assert g["entry:o_lc-entry:a0:option_strike_param"] == 2.0
    assert g["optsel:debit:w_premium"] == 0.0


def test_model_schedule_screener_genes_are_not_authored_here(strategies):
    s = strategies["O_LC-legacy"]
    space = dict(collect_param_space(s))
    space["model:risk"] = {"type": "float", "min": 0.0, "max": 1.0, "step": 0.1}
    space["schedule:time"] = {"type": "choice", "choices": ["09:45", "10:00"], "min": 0, "max": 1, "step": 1}
    space["screener:price_max"] = {"type": "float", "min": 0.0, "max": 1.0, "step": 0.1}
    space["market:enabled"] = {"type": "int", "min": 0, "max": 1, "step": 1}
    g = authored_genome(s, space)
    assert not [k for k in g if k.split(":")[0] in ("model", "schedule", "screener", "market")]


def test_a_gene_the_template_cannot_answer_is_refused(strategies):
    s = strategies["O_LC-legacy"]
    space = dict(collect_param_space(s))
    space["cond:ghost:value"] = {"type": "float", "min": 0.0, "max": 1.0, "step": 0.1}
    with pytest.raises(ValueError, match="ghost"):
        authored_genome(s, space)


def test_a_template_value_more_than_one_step_outside_the_range_raises(strategies):
    s = strategies["O_LC-legacy"]
    space = dict(collect_param_space(s))
    key = "entry:o_lc-entry:a0:option_sizing"
    cfg = dict(space[key])
    space[key] = dict(cfg, max=cfg["min"] + 1.0, step=0.5)       # template 5.0 vs [1, 2] step 0.5
    with pytest.raises(ValueError, match="option_sizing"):
        authored_genome(s, space)


def test_a_value_inside_one_step_outside_the_range_is_clipped(strategies):
    s = strategies["O_LC-legacy"]
    space = dict(collect_param_space(s))
    key = "entry:o_lc-entry:a0:option_sizing"
    cfg = dict(space[key])
    space[key] = dict(cfg, max=4.5, min=1.0, step=1.0)           # template 5.0 is within one step of 4.5
    g = authored_genome(s, space)
    assert g[key] <= 4.5 + 1e-9

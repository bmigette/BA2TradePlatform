"""Stage-2 style builder (``O_STYLE``): one Strategy from a style spec.

Plan docs/plans/2026-09-25-option-stage2-build.md, Tasks 5 (entry side) and 6 (exits + id
maps), as revised:

* MEMBER NAMESPACING (user decision 2026-09-26). Inside a style, every rule and condition id a
  member emits lives in that member's namespace: an id its own builder already prefixes with
  the member key (or, for a group key, one of its arms) is KEPT; any other id is prefixed
  ``<member>-``; ``shared-*`` ids stay shared on purpose. So O_CSP + O_WHEEL (the wheel reuses
  ``o_csp-*``) and O_CC + O_PP (both enter through S2's ``buy``/``gate_*``) coexist. The rule
  depends on the member alone, never on its siblings, and ONLY a style build applies it: every
  single-structure build (the stage-1 keys) is untouched.
* Every entry action that opens a position carries its member tag, the equity ``buy`` of
  O_CC / O_PP included (E1), and ``entry_action`` does not assume the first member is an option.
* Every exit rule is ANDed with its member's ``entry_tag`` leaf, the wheel's stock-phase rules
  included: since E2 the assigned stock inherits the put's member tag.
"""
import json
from pathlib import Path

import pytest

from tests.test_option_grid_foundations import _launcher   # reuse the path loader

_CAND = str(Path(__file__).resolve().parents[3] / "tools/option_styles.candidates.json")
_STYLES = ["short", "mid", "premium", "leaps_convex"]


def _style(m, name, path=_CAND):
    m._STYLE_ACTIVE = m._load_style_spec(path, name)
    return m._build_strategy_style("O_STYLE")


def _write(tmp_path, styles):
    p = tmp_path / "styles.json"
    p.write_text(json.dumps({"schema": 1, "styles": styles}))
    return str(p)


def _custom(m, tmp_path, members, **extra):
    style = {"status": "candidate", "price_cap_policy": "none", "members": members, **extra}
    return _style(m, "x", _write(tmp_path, {"x": style}))


def _leaves(node, out=None):
    out = [] if out is None else out
    if isinstance(node, dict):
        if "field" in node:
            out.append(node)
        for c in node.get("conditions") or []:
            _leaves(c, out)
    return out


def _tag_of(action):
    from ba2_common.core.rule_builders import checked_entry_tag
    return checked_entry_tag(action)


def _pin_admissible(m, monkeypatch):
    """The admissible set is derived from builder IDENTITY; a test that swaps a member's
    builder for a wrapper pins the real set first."""
    admissible = m._style_admissible_members()
    monkeypatch.setattr(m, "_style_admissible_members", lambda: admissible)


def _own(m, key):
    return m._STRATEGY_BUILDERS[key](key)


#: Every member at once that the namespacing had to separate: the wheel beside the CSP whose
#: entry it reuses, and the two S2-entry overlays beside each other.
_CLASHING = [{"key": "O_CSP"}, {"key": "O_CC"}, {"key": "O_PP"}, {"key": "O_WHEEL"}]


# --- Task 5: entry side --------------------------------------------------------------------
@pytest.mark.parametrize("name", _STYLES)
def test_entry_rules_are_the_members_own_rules_in_member_order(name):
    m = _launcher()
    s = _style(m, name)
    expected = []
    for mem in m._STYLE_ACTIVE["members"]:
        idm = s.style_id_maps[mem["key"]]["entry_rules"]
        expected += [idm[r["id"]] for r in _own(m, mem["key"]).entry_rules]
    assert [r["id"] for r in s.entry_rules] == expected


@pytest.mark.parametrize("name", ["short", "mid", "leaps_convex"])
def test_members_whose_ids_are_already_their_own_keep_them_verbatim(name):
    """Every option single already prefixes its entry ids with its own key (a group key with
    its arms'), so the namespacing leaves them alone: a stage-1 entry gene key is the style
    key unchanged."""
    m = _launcher()
    s = _style(m, name)
    own = [r["id"] for mem in m._STYLE_ACTIVE["members"]
           for r in _own(m, mem["key"]).entry_rules]
    assert [r["id"] for r in s.entry_rules] == own
    for mem in m._STYLE_ACTIVE["members"]:
        idm = s.style_id_maps[mem["key"]]
        assert all(k == v for k, v in idm["entry_rules"].items())
        assert all(k == v for k, v in idm["entry_conds"].items())


def test_the_wheel_and_the_csp_and_both_overlays_get_distinct_entry_ids(tmp_path):
    m = _launcher()
    s = _custom(m, tmp_path, _CLASHING)
    ids = [r["id"] for r in s.entry_rules]
    assert ids == ["o_csp-entry", "o_cc-buy", "o_pp-buy", "o_wheel-o_csp-entry"]
    conds = [x["id"] for r in s.entry_rules for x in _leaves(r["conditions"])]
    non_shared = [c for c in conds if not c.startswith("shared-")]
    assert len(non_shared) == len(set(non_shared)), sorted(non_shared)
    assert "o_cc-gate_confidence" in conds and "o_pp-gate_confidence" in conds
    assert "o_wheel-o_csp-iv_rank" in conds and "o_csp-iv_rank" in conds
    # the group node ids move with them (O_CC and O_PP both authored "root")
    roots = [r["conditions"]["id"] for r in s.entry_rules]
    assert roots == ["o_csp-root", "o_cc-root", "o_pp-root", "o_wheel-o_csp-root"]
    # shared-* stays shared: ONE id across every member that carries it (the two option
    # entries; O_CC / O_PP enter through S2's gates, which carry none)
    assert conds.count("shared-rel_volume") == 2


def test_a_members_style_ids_do_not_depend_on_its_siblings(tmp_path):
    """The namespacing is a function of the member alone, so a seed translated through one
    style's id map lands on the same keys in any other style holding that member."""
    m = _launcher()
    alone = _custom(m, tmp_path, [{"key": "O_WHEEL"}])
    together = _custom(m, tmp_path, _CLASHING)
    assert alone.style_id_maps["O_WHEEL"] == together.style_id_maps["O_WHEEL"]
    assert [r for r in together.entry_rules if r["id"].startswith("o_wheel-")] == \
        alone.entry_rules


def test_the_wheels_market_leaves_keep_their_own_wheel_ids(tmp_path):
    m = _launcher()
    m._MARKET_CONDITION_PROFILES = ("ohlcv-v1",)
    s = _custom(m, tmp_path, _CLASHING)
    wheel = next(r for r in s.entry_rules if r["id"] == "o_wheel-o_csp-entry")
    market = [x["id"] for x in _leaves(wheel["conditions"]) if "-market-" in x["id"]]
    assert market and all(i.startswith("o_wheel-market-") for i in market), market
    own = [x["id"] for x in _leaves(_own(m, "O_WHEEL").entry_rules[0]["conditions"])
           if "-market-" in x["id"]]
    assert market == own


@pytest.mark.parametrize("name", _STYLES)
def test_every_entry_rule_is_toggleable_and_every_opening_action_carries_its_member_tag(name):
    m = _launcher()
    s = _style(m, name)
    tag_of_rule = {}
    for mem in m._STYLE_ACTIVE["members"]:
        idm = s.style_id_maps[mem["key"]]["entry_rules"]
        for r in _own(m, mem["key"]).entry_rules:
            tag_of_rule[idm[r["id"]]] = m._STYLE_MEMBER_TAGS[mem["key"]]
    for r in s.entry_rules:
        assert r["toggle_optimize"] is True and r["toggleOptimize"] is True
        assert {_tag_of(a) for a in r["actions"]} == {tag_of_rule[r["id"]]}, r["id"]


def test_the_equity_buy_of_an_overlay_member_is_tagged_too(tmp_path):
    m = _launcher()
    s = _custom(m, tmp_path, [{"key": "O_CC"}, {"key": "O_LC"}])
    buy = next(r for r in s.entry_rules if r["id"] == "o_cc-buy")["actions"]
    assert [a["action_type"] for a in buy] == ["buy"]
    assert _tag_of(buy[0]) == m._STYLE_MEMBER_TAGS["O_CC"]
    assert buy[0]["lot_size"] == 100                      # the round lot survives the copy


def test_entry_action_is_the_first_OPTION_entry_even_when_an_equity_member_leads(tmp_path):
    m = _launcher()
    s = _custom(m, tmp_path, [{"key": "O_CC"}, {"key": "O_LC"}, {"key": "O_LP"}])
    assert s.entry_action["action_type"] == "buy_call"
    assert _tag_of(s.entry_action) == m._STYLE_MEMBER_TAGS["O_LC"]
    # a copy, never the rule's own dict
    lc = next(r for r in s.entry_rules if r["id"] == "o_lc-entry")["actions"][0]
    assert s.entry_action == lc and s.entry_action is not lc


def test_a_style_with_no_option_entry_carries_no_entry_action_like_its_single(tmp_path):
    """O_CC alone: an equity entry, so no run-level option entry marker (its single build has
    none either); its option overlay still makes it an options run through the exits."""
    m = _launcher()
    s = _custom(m, tmp_path, [{"key": "O_CC"}])
    assert getattr(s, "entry_action", None) is None
    assert getattr(_own(m, "O_CC"), "entry_action", None) is None


def test_action_overrides_move_the_dte_band_and_nothing_else():
    m = _launcher()
    s = _style(m, "mid")
    lc = next(r for r in s.entry_rules if r["id"] == "o_lc-entry")["actions"][0]
    base = _own(m, "O_LC").entry_rules[0]["actions"][0]
    assert (base["option_dte_min"], base["option_dte_max"]) == (25, 45)
    assert (lc["option_dte_min"], lc["option_dte_max"]) == (60, 90)
    assert (lc["option_dte_min_range"], lc["option_dte_max_range"], lc["option_dte_step"]) == \
        (60, 120, 10)
    moved = {"option_dte_min", "option_dte_max", "option_dte_min_range",
             "option_dte_max_range", "option_dte_step", "option_entry_tag"}
    for k in set(base) | set(lc):
        if k not in moved:
            assert lc.get(k) == base.get(k), k


@pytest.mark.parametrize("overrides, msg", [
    # merged with the authored band, min would pass max
    ({"option_dte_min": 50}, "option_dte_min=50 > option_dte_max=45"),
    # the authored window's centre must stay inside the searched centre range
    ({"option_dte_min_range": 100, "option_dte_max_range": 200}, "centre"),
])
def test_an_override_that_leaves_the_member_undecodable_is_refused(tmp_path, overrides, msg):
    m = _launcher()
    with pytest.raises(ValueError, match=msg) as exc:
        _custom(m, tmp_path, [{"key": "O_LC", "action_overrides": overrides}])
    assert "O_LC" in str(exc.value)


@pytest.mark.parametrize("name", _STYLES + ["clashing"])
def test_single_structure_builders_are_untouched_by_building_a_style(tmp_path, name):
    """Deepcopy discipline: no shared mutable state leaks back into a member's single build,
    and the namespacing never reaches it (stage-1 keys stay byte-identical)."""
    m = _launcher()
    keys = sorted(m._style_admissible_members())

    def dump():
        return {k: json.dumps({"e": _own(m, k).entry_rules, "x": _own(m, k).exit_rules,
                               "a": getattr(_own(m, k), "entry_action", None)},
                              sort_keys=True, default=str) for k in keys}
    before = dump()
    if name == "clashing":
        _custom(m, tmp_path, _CLASHING)
    else:
        _style(m, name)
    assert dump() == before


def test_no_style_loaded_is_a_loud_refusal():
    m = _launcher()
    m._STYLE_ACTIVE = None
    with pytest.raises(SystemExit, match="--style-config"):
        m._build_strategy_style("O_STYLE")


def test_an_opening_action_that_cannot_carry_the_tag_is_refused(tmp_path, monkeypatch):
    """A short-equity ``sell`` entry would open a position no member exit could ever match
    (only option entries and ``buy`` write the tag): refused by name, never left untagged."""
    m = _launcher()
    _pin_admissible(m, monkeypatch)
    real = m._STRATEGY_BUILDERS["O_PP"]

    def with_sell(kind):
        s = real(kind)
        s.entry_rules[0]["actions"] = [dict(s.entry_rules[0]["actions"][0],
                                            action="sell", action_type="sell")]
        return s
    monkeypatch.setitem(m._STRATEGY_BUILDERS, "O_PP", with_sell)
    with pytest.raises(ValueError, match=r"O_PP.*'sell' action opens a position but cannot"):
        _custom(m, tmp_path, [{"key": "O_PP"}])


def test_a_shared_leaf_two_members_author_differently_is_refused(tmp_path, monkeypatch):
    """``shared-*`` ids are ONE gene across members; two different definitions under one id
    would resolve by dict overwrite (last member silently wins)."""
    m = _launcher()
    _pin_admissible(m, monkeypatch)
    real = m._STRATEGY_BUILDERS["O_LP"]

    def wider(kind):
        s = real(kind)
        for leaf in _leaves(s.entry_rules[0]["conditions"]):
            if leaf["id"] == "shared-rel_volume":
                leaf["value_max"] = leaf["valueMax"] = 3.0
        return s
    monkeypatch.setitem(m._STRATEGY_BUILDERS, "O_LP", wider)
    with pytest.raises(ValueError, match=r"shared-rel_volume.*O_LC.*O_LP"):
        _custom(m, tmp_path, [{"key": "O_LC"}, {"key": "O_LP"}])


# --- Task 6: member-namespaced, tag-scoped exits + id maps ------------------------------------
def _block(s, key):
    """The style's exit rules that belong to ``key`` (by its id map), in style order."""
    ids = set(s.style_id_maps[key]["exit_rules"].values())
    return [r for r in s.exit_rules if r["id"] in ids]


def _tag_leaves(rule):
    return [x for x in _leaves(rule["conditions"]) if x["field"] == "entry_tag"]


def _named(m, tmp_path, name):
    return _custom(m, tmp_path, _CLASHING) if name == "clashing" else _style(m, name)


@pytest.mark.parametrize("name", _STYLES + ["clashing"])
def test_each_members_exit_block_is_its_own_builders_exits_re_ided(tmp_path, name):
    m = _launcher()
    s = _named(m, tmp_path, name)
    ids = [r["id"] for r in s.exit_rules]
    assert len(ids) == len(set(ids))
    expected = []
    for mem in m._STYLE_ACTIVE["members"]:
        p = mem["key"].lower()
        own = [r["id"] for r in _own(m, mem["key"]).exit_rules]
        # no exit id is own-prefixed today, so every one gets the member prefix
        assert [r["id"] for r in _block(s, mem["key"])] == [f"{p}-{i}" for i in own]
        expected += [f"{p}-{i}" for i in own]
    assert ids == expected                         # member blocks concatenated in member order


@pytest.mark.parametrize("name", _STYLES + ["clashing"])
def test_every_exit_is_scoped_to_exactly_its_member_stock_phase_included(tmp_path, name):
    """Since E2 the assigned stock inherits the put's member tag and a CC/PP overlay inherits
    the covered position's, so EVERY exit rule is scoped -- the wheel's stock-phase rules and
    O_CC's / O_PP's guard+overlay pairs included (that is what keeps them off another
    member's shares)."""
    m = _launcher()
    s = _named(m, tmp_path, name)
    for mem in m._STYLE_ACTIVE["members"]:
        tag = m._STYLE_MEMBER_TAGS[mem["key"]]
        for r in _block(s, mem["key"]):
            tags = _tag_leaves(r)
            assert len(tags) == 1, r["id"]
            assert tags[0]["op"] == "==" and tags[0]["value"] == float(tag), r["id"]
            assert tags[0]["id"] == f"{r['id']}-tag"
            # appended LAST to the root AND: every authored leaf keeps its trigger index
            assert r["conditions"]["type"] == "AND"
            assert r["conditions"]["conditions"][-1] is tags[0]


def test_the_stock_phase_and_overlay_rules_carry_their_own_members_tag(tmp_path):
    m = _launcher()
    s = _custom(m, tmp_path, _CLASHING)
    rid = {r["id"]: r for r in s.exit_rules}
    expect = {"o_wheel-cc_dte": "O_WHEEL", "o_wheel-cc_guard": "O_WHEEL",
              "o_wheel-cc_sell": "O_WHEEL", "o_wheel-wheel_stock_guard": "O_WHEEL",
              "o_cc-cc_guard": "O_CC", "o_cc-cc_sell": "O_CC",
              "o_pp-pp_guard": "O_PP", "o_pp-pp_buy": "O_PP"}
    for r, key in expect.items():
        assert _tag_leaves(rid[r])[0]["value"] == float(m._STYLE_MEMBER_TAGS[key]), r
    # the stock-phase leaf is still there, namespaced, with the tag after it
    assert [x["id"] for x in _leaves(rid["o_wheel-cc_sell"]["conditions"])] == \
        ["o_wheel-cc_assigned", "o_wheel-cc_sell-tag"]


@pytest.mark.parametrize("name", _STYLES + ["clashing"])
def test_every_scoped_exit_converts_through_the_shared_rule_converters(tmp_path, name):
    """BT/live parity: the backtest seeder and the live export both turn a TradeRule into an
    EventAction through these shared functions, so a tag leaf they refuse (or drop) would break
    both runtimes alike. Each scoped rule yields exactly one entry_tag trigger."""
    from ba2_common.core.rule_builders import rule_triggers_from_tree
    from ba2_common.core.rules_convert import live_actions_from_trade_rule
    from ba2_common.core.types import ExpertEventType
    m = _launcher()
    s = _named(m, tmp_path, name)
    for r in s.exit_rules:
        if r.get("enabled") is False:
            continue
        triggers = rule_triggers_from_tree(r["conditions"], r["id"])
        # every leaf converts -- none is dropped on the way to the engine -- and exactly one of
        # the triggers is the member tag
        assert len(triggers) == len(_leaves(r["conditions"])), r["id"]
        tag_triggers = [t for t in triggers.values()
                        if t["event_type"] == ExpertEventType.N_ENTRY_TAG.value]
        assert len(tag_triggers) == 1, r["id"]
        assert live_actions_from_trade_rule(r), r["id"]


def test_debit_and_credit_members_keep_their_own_tp_bands_and_the_credit_stop():
    m = _launcher()
    s = _style(m, "premium")       # credit members
    rid = {r["id"]: r for r in s.exit_rules}
    tp = next(x for x in _leaves(rid["o_ic-opt_tp"]["conditions"]) if x["id"] == "o_ic-tp")
    own = next(x for r in _own(m, "O_IC").exit_rules if r["id"] == "opt_tp"
               for x in _leaves(r["conditions"]) if x["id"] == "tp")
    assert (tp["value"], tp["value_min"], tp["value_max"]) == \
        (own["value"], own["value_min"], own["value_max"])
    assert "o_ic-opt_sl" in rid
    s = _style(m, "short")         # debit members
    rid = {r["id"]: r for r in s.exit_rules}
    tp = next(x for x in _leaves(rid["o_lc-opt_tp"]["conditions"]) if x["id"] == "o_lc-tp")
    assert (tp["value"], tp["value_min"], tp["value_max"]) == (100, 25, 200)
    assert "o_lc-opt_sl" not in rid


def test_grid2_members_keep_their_own_bands_and_authored_off_rules():
    m = _launcher()
    s = _style(m, "leaps_convex")
    rid = {r["id"]: r for r in s.exit_rules}
    dte = next(x for x in _leaves(rid["o_leap-opt_dte"]["conditions"]) if x["id"] == "o_leap-dte")
    assert (dte["value"], dte["value_min"], dte["value_max"]) == (150, 90, 240)
    assert rid["o_pmcc-opt_sl_ml"].get("enabled") is False
    assert "o_pmcc-pmcc_roll_dte" in rid and "o_convex-opt_tp_mult" in rid
    # the PMCC roll rules still come FIRST within the PMCC block (builder order preserved)
    pmcc = [i for i in rid if i.startswith("o_pmcc-")]
    assert pmcc[:3] == ["o_pmcc-pmcc_roll_dte", "o_pmcc-pmcc_roll_buyback",
                        "o_pmcc-pmcc_delta_floor"]


def test_exit_overrides_move_only_the_named_band_and_its_camelcase_mirror():
    m = _launcher()
    s = _style(m, "mid")
    rid = {r["id"]: r for r in s.exit_rules}
    td = next(x for x in _leaves(rid["o_lc-opt_time"]["conditions"]) if x["id"] == "o_lc-td")
    assert (td["value"], td["value_min"], td["value_max"], td["value_step"]) == (45, 20, 90, 5)
    # a stale camelCase mirror would be read by any alias-first reader
    assert (td["valueMin"], td["valueMax"], td["valueStep"]) == (20, 90, 5)
    own = {x["id"]: x for r in _own(m, "O_LC").exit_rules for x in _leaves(r["conditions"])}
    tp = next(x for x in _leaves(rid["o_lc-opt_tp"]["conditions"]) if x["id"] == "o_lc-tp")
    assert {k: v for k, v in tp.items() if k != "id"} == \
        {k: v for k, v in own["tp"].items() if k != "id"}
    # O_BULLCS carries no exit_overrides in "mid": its td band is its own builder's
    own_bc = {x["id"]: x for r in _own(m, "O_BULLCS").exit_rules for x in _leaves(r["conditions"])}
    bc = next(x for x in _leaves(rid["o_bullcs-opt_time"]["conditions"])
              if x["id"] == "o_bullcs-td")
    assert {k: v for k, v in bc.items() if k != "id"} == \
        {k: v for k, v in own_bc["td"].items() if k != "id"}


@pytest.mark.parametrize("member, overrides, msg", [
    ("O_LC", {"nope": {"value": 1}}, r"nope"),
    # the merged band: value above the authored max
    ("O_LC", {"tp": {"value": 300}}, r"tp.*value=300.*value_max=200"),
    # a flag condition has no threshold at all
    ("O_WHEEL", {"wheel_assigned": {"value": 1}}, r"wheel_assigned.*flag"),
])
def test_an_exit_override_that_cannot_act_is_refused(tmp_path, member, overrides, msg):
    m = _launcher()
    with pytest.raises(ValueError, match=msg) as exc:
        _custom(m, tmp_path, [{"key": member, "exit_overrides": overrides}])
    assert member in str(exc.value)


def test_a_band_on_an_exit_condition_the_GA_does_not_search_is_refused(tmp_path, monkeypatch):
    """Inert: no gene reads value_min/value_max on a leaf without ``optimize``. No member has
    such a numeric exit leaf today, so one is simulated."""
    m = _launcher()
    _pin_admissible(m, monkeypatch)
    real = m._STRATEGY_BUILDERS["O_LP"]

    def fixed_tp(kind):
        s = real(kind)
        for leaf in _leaves(s.exit_rules[0]["conditions"]):
            leaf["optimize"] = leaf["optimizeEnabled"] = False
        return s
    monkeypatch.setitem(m._STRATEGY_BUILDERS, "O_LP", fixed_tp)
    with pytest.raises(ValueError, match=r"O_LP: exit_overrides\['tp'\].*not searched"):
        _custom(m, tmp_path, [{"key": "O_LP", "exit_overrides": {"tp": {"value_max": 300}}}])
    # moving the fixed threshold itself is not inert, and is allowed
    s = _custom(m, tmp_path, [{"key": "O_LP", "exit_overrides": {"tp": {"value": 150}}}])
    assert next(x for x in _leaves(s.exit_rules[0]["conditions"])
                if x["id"] == "o_lp-tp")["value"] == 150


def test_an_exit_rule_whose_root_is_not_an_AND_is_refused(tmp_path, monkeypatch):
    """ANDing the tag into an OR root would scope nothing (tag OR x fires for any member)."""
    m = _launcher()
    _pin_admissible(m, monkeypatch)
    real = m._STRATEGY_BUILDERS["O_LP"]

    def with_or(kind):
        s = real(kind)
        s.exit_rules[0]["conditions"]["type"] = s.exit_rules[0]["conditions"]["operator"] = "OR"
        return s
    monkeypatch.setitem(m._STRATEGY_BUILDERS, "O_LP", with_or)
    with pytest.raises(ValueError, match=r"O_LP exit rule 'opt_tp'.*OR"):
        _custom(m, tmp_path, [{"key": "O_LP"}])


@pytest.mark.parametrize("gene", ["cond:nope:value", "exit:nope:enabled", "entry:nope:enabled",
                                  "bogus:x", "cond"])
def test_a_gene_key_the_id_map_cannot_place_is_refused(tmp_path, gene):
    m = _launcher()
    s = _custom(m, tmp_path, [{"key": "O_LC"}])
    with pytest.raises(ValueError, match=r"O_LC.*cannot place"):
        m._style_translate_key(gene, s.style_id_maps["O_LC"], "O_LC")


def test_shared_and_run_level_keys_translate_by_identity(tmp_path):
    m = _launcher()
    s = _custom(m, tmp_path, _CLASHING)
    idm = s.style_id_maps["O_WHEEL"]
    for g in ("cond:shared-rel_volume:value", "optsel:credit:w_premium", "model:k_compress",
              "schedule:monday"):
        assert m._style_translate_key(g, idm, "O_WHEEL") == g
    assert m._style_translate_key("cond:o_csp-iv_rank:enabled", idm, "O_WHEEL") == \
        "cond:o_wheel-o_csp-iv_rank:enabled"
    assert m._style_translate_key("exit:opt_tp:enabled", idm, "O_WHEEL") == \
        "exit:o_wheel-opt_tp:enabled"
    assert m._style_translate_key("entry:o_csp-entry:a0:option_dte", idm, "O_WHEEL") == \
        "entry:o_wheel-o_csp-entry:a0:option_dte"


def _genome_union_check(m, s):
    """Every member's own strategy gene, translated through its id map, is in the style space
    with the SAME domain unless the style overrides that member's bands; the only new keys are
    the entry-rule toggles. Returns the style space."""
    from app.services.strategy_param_space import collect_param_space
    style_space = collect_param_space(s)
    seen = set()
    for mem in m._STYLE_ACTIVE["members"]:
        key = mem["key"]
        idm = s.style_id_maps[key]
        own = collect_param_space(_own(m, key))
        overridden = bool(mem.get("action_overrides") or mem.get("exit_overrides"))
        for g in own:
            t = m._style_translate_key(g, idm, key)      # the SAME function Task 13 wraps
            assert t in style_space, f"{key}: {g} -> {t} missing from the style space"
            assert overridden or style_space[t] == own[g], (key, g, t)
            seen.add(t)
    extra = set(style_space) - seen
    assert all(g.startswith("entry:") and g.endswith(":enabled") for g in extra), sorted(extra)
    return style_space


@pytest.mark.parametrize("name", _STYLES)
@pytest.mark.parametrize("profile", [(), ("ohlcv-v1", "ta-structure-v1")])
def test_the_style_genome_is_the_disjoint_union_of_the_members_genomes(name, profile):
    """No gene key collides and none is silently lost: every member's own strategy gene,
    translated through the id map, is in the style space; the only NEW keys are the entry
    toggles; shared keys (cond:shared-*, optsel:*) appear once."""
    m = _launcher()
    m._MARKET_CONDITION_PROFILES = profile
    s = _style(m, name)
    space = _genome_union_check(m, s)
    print(f"{name} profile={','.join(profile) or 'none'}: {len(space)} strategy genes")


def test_the_clashing_members_genomes_stay_disjoint(tmp_path):
    m = _launcher()
    s = _custom(m, tmp_path, _CLASHING)
    space = _genome_union_check(m, s)
    # the wheel's and the CSP's entry genes are separate genes now, as are O_CC's and O_PP's
    assert "cond:o_csp-iv_rank:value" in space and "cond:o_wheel-o_csp-iv_rank:value" in space
    assert "cond:o_cc-gate_confidence:value" in space
    assert "cond:o_pp-gate_confidence:value" in space
    assert "cond:gate_confidence:value" not in space


# --- review fixes (2026-09-26) -----------------------------------------------------------------
def test_I1_a_valueless_exit_leaf_is_refused_in_the_style_exactly_as_in_the_single(
        tmp_path, monkeypatch):
    """The tag leaf always converts. Counted as an ordinary trigger it would turn a rule whose
    AUTHORED leaves all dropped into ``{entry_tag == N}`` -- always true for that member --
    while the single-structure rule is refused. Both must be refused."""
    from ba2_common.core.rule_builders import rule_triggers_from_tree
    m = _launcher()
    _pin_admissible(m, monkeypatch)
    real = m._STRATEGY_BUILDERS["O_LP"]

    def valueless_tp(kind):
        s = real(kind)
        for leaf in _leaves(s.exit_rules[0]["conditions"]):
            leaf.pop("value", None)
        return s
    monkeypatch.setitem(m._STRATEGY_BUILDERS, "O_LP", valueless_tp)
    single = m._STRATEGY_BUILDERS["O_LP"]("O_LP").exit_rules[0]
    with pytest.raises(ValueError, match="ALWAYS TRUE"):
        rule_triggers_from_tree(single["conditions"], single["id"])
    styled = _custom(m, tmp_path, [{"key": "O_LP"}]).exit_rules[0]
    assert styled["id"] == "o_lp-opt_tp" and _tag_leaves(styled)
    with pytest.raises(ValueError, match="ALWAYS TRUE"):
        rule_triggers_from_tree(styled["conditions"], styled["id"])


def test_I2_screener_genes_are_refused_not_passed_through(tmp_path):
    """Screener genes are RUN-level: a stage-1 O_CSP seed's price cap would silently become the
    whole style's (pre-empting Q3). Refused by name until a policy says otherwise."""
    m = _launcher()
    s = _custom(m, tmp_path, [{"key": "O_CSP"}])
    with pytest.raises(ValueError, match=r"O_CSP.*screener:price_max.*run-level"):
        m._style_translate_key("screener:price_max", s.style_id_maps["O_CSP"], "O_CSP")


@pytest.mark.parametrize("member, overrides", [
    # O_ERN 7..14 decodes dte_min down to 0, inside its 5-day before-earnings entry ceiling:
    # an expiry that can settle BEFORE the print it bets on.
    ("O_ERN", {"option_dte_min": 7, "option_dte_max": 14}),
    # O_LEAP at 60..90 is no longer a LEAPS, and its own 90-240 DTE exit closes it at once.
    ("O_LEAP", {"option_dte_min": 60, "option_dte_max": 90}),
    ("O_PMCC", {"option_dte_min": 400, "option_dte_max": 480}),
    ("O_CONVEX", {"option_dte_step": 5}),
])
def test_I3_action_overrides_on_a_member_with_its_own_dte_invariant_are_refused(
        tmp_path, member, overrides):
    m = _launcher()
    path = _write(tmp_path, {"x": {"status": "candidate", "members": [
        {"key": member, "action_overrides": overrides}]}})
    with pytest.raises(ValueError, match=rf"{member}.*action_overrides.*DTE invariant"):
        m._load_style_spec(path, "x")


def test_M1_a_builder_refusal_names_the_style_and_the_member(tmp_path):
    m = _launcher()
    with pytest.raises(ValueError, match=r"^O_LC entry rule .*option_dte_min=50") as exc:
        _custom(m, tmp_path, [{"key": "O_LC", "action_overrides": {"option_dte_min": 50}}])
    assert exc.value.__notes__ == ["style 'x' member O_LC"]   # PEP-678 note, type kept


def test_M1_M2_a_mixed_option_and_equity_entry_is_refused_naming_member_and_rule(
        tmp_path, monkeypatch):
    from ba2_common.core.trade_cycle import MixedEntryActionsRefused
    m = _launcher()
    _pin_admissible(m, monkeypatch)
    real = m._STRATEGY_BUILDERS["O_LC"]

    def mixed(kind):
        s = real(kind)
        s.entry_rules[0]["actions"].append({"action": "buy", "action_type": "buy"})
        return s
    monkeypatch.setitem(m._STRATEGY_BUILDERS, "O_LC", mixed)
    with pytest.raises(MixedEntryActionsRefused,
                       match=r"O_LC entry rule 'o_lc-entry'.*mixes") as exc:
        _custom(m, tmp_path, [{"key": "O_LC"}])
    assert exc.value.__notes__ == ["style 'x' member O_LC"]


def test_M2_a_sell_beside_a_buy_hits_the_sell_refusal_not_the_untagged_fallback(
        tmp_path, monkeypatch):
    """The ``sell`` refusal must fire on its own: a rule whose buy IS tagged would otherwise
    pass with an untaggable short entry riding along."""
    m = _launcher()
    _pin_admissible(m, monkeypatch)
    real = m._STRATEGY_BUILDERS["O_PP"]

    def buy_and_sell(kind):
        s = real(kind)
        s.entry_rules[0]["actions"].append({"action": "sell", "action_type": "sell"})
        return s
    monkeypatch.setitem(m._STRATEGY_BUILDERS, "O_PP", buy_and_sell)
    with pytest.raises(ValueError, match=r"O_PP.*'sell' action opens a position but cannot"):
        _custom(m, tmp_path, [{"key": "O_PP"}])


@pytest.mark.parametrize("gene", ["cond:tp:bogus", "cond:tp:value:extra"])
def test_M3_a_cond_gene_with_an_unknown_field_is_refused(tmp_path, gene):
    m = _launcher()
    s = _custom(m, tmp_path, [{"key": "O_LC"}])
    with pytest.raises(ValueError, match=r"O_LC.*cannot place"):
        m._style_translate_key(gene, s.style_id_maps["O_LC"], "O_LC")


def test_M3_given_the_style_space_a_translated_key_outside_it_is_refused(tmp_path):
    from app.services.strategy_param_space import collect_param_space
    m = _launcher()
    s = _custom(m, tmp_path, [{"key": "O_LC"}])
    space = collect_param_space(s)
    idm = s.style_id_maps["O_LC"]
    assert m._style_translate_key("cond:tp:value", idm, "O_LC", space) == "cond:o_lc-tp:value"
    # a well-formed key naming a gene the style does not have (tp has no toggle gene)
    with pytest.raises(ValueError, match=r"O_LC.*cond:o_lc-tp:enabled.*not a gene"):
        m._style_translate_key("cond:tp:enabled", idm, "O_LC", space)


def test_M1_a_pydantic_ValidationError_from_a_bad_leaf_keeps_its_type_and_names_the_member(
        tmp_path, monkeypatch):
    """A ValidationError cannot be rebuilt from a message (``type(e)(msg)`` raises TypeError),
    so the style/member context rides as a PEP-678 note on the ORIGINAL exception."""
    from pydantic import ValidationError
    from ba2_common.core.rule_models import normalize_trade_rules
    m = _launcher()
    _pin_admissible(m, monkeypatch)
    real = m._STRATEGY_BUILDERS["O_LC"]

    def bad_leaf(kind):
        s = real(kind)
        s.entry_rules[0]["conditions"]["conditions"].append({"id": "nofield", "value": 1})
        s.entry_rules = normalize_trade_rules(s.entry_rules)   # a leaf without ``field``
        return s
    # load with the real builder (the loader builds every member too), then build with the bad one
    m._STYLE_ACTIVE = m._load_style_spec(_write(tmp_path, {"x": {
        "status": "candidate", "price_cap_policy": "none", "members": [{"key": "O_LC"}]}}), "x")
    monkeypatch.setitem(m._STRATEGY_BUILDERS, "O_LC", bad_leaf)
    with pytest.raises(ValidationError) as exc:
        m._build_strategy_style("O_STYLE")
    assert exc.value.__notes__ == ["style 'x' member O_LC"]

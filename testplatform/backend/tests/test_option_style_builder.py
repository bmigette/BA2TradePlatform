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
    with pytest.raises(ValueError, match=r"O_PP.*'sell'.*tag"):
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

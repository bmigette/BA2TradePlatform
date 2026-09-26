"""Stage-2 style spec: loader, validator and the append-only member -> entry_tag table.

Plan docs/plans/2026-09-25-option-stage2-build.md, Task 3 (and Task 4's candidate file), as
revised after the user's answers: O_CC / O_PP are admissible members (E1 routes each entry by
its own actions) and O_WHEEL may share a style with O_JL / O_RS (E2 holds assigned stock per
member tag). Since 2026-09-26 member ids are namespaced inside a style, so O_WHEEL + O_CSP and
O_CC + O_PP coexist too. What stays refused is what no revision answered: unbounded / phase-gated
keys, grid-1 family keys, arms of a group key, two members that own the SAME id prefix (still one
gene after namespacing), and mixed per-member price caps without an explicit policy (Q3).
"""
import json
from pathlib import Path

import pytest

from tests.test_option_grid_foundations import _launcher   # reuse the path loader


def _write(tmp_path, styles):
    p = tmp_path / "styles.json"
    p.write_text(json.dumps({"schema": 1, "styles": styles}))
    return str(p)


#: The persisted tag contract, restated ONCE here on purpose: a change to the launcher's table
#: that renumbers or drops a member must fail this test (APPEND-ONLY -- new members may only be
#: added with a new, never-used value).
_TAGS_AS_FIRST_PERSISTED = {
    "O_LC": 1, "O_LP": 2, "O_VERT": 3, "O_BULLCS": 4, "O_BF": 5, "O_BEARCS": 6, "O_BULLPS": 7,
    "O_IC": 8, "O_JL": 9, "O_RS": 10, "O_CSP": 11, "O_STRD": 12, "O_STRG": 13, "O_WHEEL": 14,
    "O_LEAP": 15, "O_PMCC": 16, "O_CONVEX": 17, "O_ERN": 18, "O_CBS": 19, "O_PBS": 20,
    "O_CC": 21, "O_PP": 22,
}


def test_member_tags_cover_every_admissible_member_and_never_collide():
    m = _launcher()
    tags = m._STYLE_MEMBER_TAGS
    assert m._style_admissible_members() <= set(tags)
    # A tagged member that is no longer admissible must be RETIRED explicitly: its tag stays in
    # the table (rows written with it still exist) and is never reused.
    assert m._STYLE_RETIRED_TAGS <= set(tags.values())
    assert {k for k in tags if k not in m._style_admissible_members()} == {
        k for k, v in tags.items() if v in m._STYLE_RETIRED_TAGS}
    assert len(set(m._STYLE_MEMBER_TAGS.values())) == len(m._STYLE_MEMBER_TAGS)
    assert all(isinstance(v, int) and not isinstance(v, bool) and v >= 1
               for v in m._STYLE_MEMBER_TAGS.values())


def test_the_member_tag_table_is_append_only():
    m = _launcher()
    for key, tag in _TAGS_AS_FIRST_PERSISTED.items():
        assert m._STYLE_MEMBER_TAGS[key] == tag, f"{key}'s persisted tag moved"


def test_the_admissible_set_is_what_the_builders_say():
    m = _launcher()
    assert m._style_admissible_members() == {
        "O_LC", "O_LP", "O_VERT", "O_BULLCS", "O_BF", "O_BEARCS", "O_BULLPS", "O_IC",
        "O_JL", "O_RS", "O_CSP", "O_STRD", "O_STRG", "O_WHEEL",
        "O_LEAP", "O_PMCC", "O_ERN", "O_CBS", "O_PBS", "O_CONVEX",
        # revision E1 (2026-09-25): stock + option overlay members route their equity entry
        # through the RM, so the premium style may hold them.
        "O_CC", "O_PP"}


def test_a_valid_spec_loads_and_resolves_one_style(tmp_path):
    m = _launcher()
    path = _write(tmp_path, {"short": {"status": "candidate",
                                        "members": [{"key": "O_LC"}, {"key": "O_LP"}]}})
    style = m._load_style_spec(path, "short")
    assert style["name"] == "short"
    assert [x["key"] for x in style["members"]] == ["O_LC", "O_LP"]
    assert len(style["spec_sha256"]) == 64


def test_the_sha_covers_this_style_only_and_follows_its_content(tmp_path):
    m = _launcher()
    a = {"status": "candidate", "members": [{"key": "O_LC"}, {"key": "O_LP"}]}
    b = {"status": "candidate", "members": [{"key": "O_BF"}]}
    alone = m._load_style_spec(_write(tmp_path, {"a": a}), "a")["spec_sha256"]
    with_sibling = m._load_style_spec(_write(tmp_path, {"a": a, "b": b}), "a")["spec_sha256"]
    assert alone == with_sibling                       # a sibling is not this style's identity
    reordered = dict(a, members=[{"key": "O_LP"}, {"key": "O_LC"}])
    assert m._load_style_spec(_write(tmp_path, {"a": reordered}), "a")["spec_sha256"] != alone


@pytest.mark.parametrize("members, msg", [
    ([{"key": "O_STK"}], "plain equity"),
    ([{"key": "O_SSTG"}], "unbounded"),
    ([{"key": "O_CAL"}], "phase-gated"),
    ([{"key": "OS1"}], "list the members"),
    ([{"key": "O_LEAPC"}], "O_LEAP"),                        # arms are not members; the group key is
    ([{"key": "O_CONVEXP"}], "O_CONVEX"),
    ([{"key": "S2"}], "not a style member"),
    ([{"key": "NOPE"}], "not a style member"),
    ([{"key": "O_LC"}, {"key": "O_LC"}], "twice"),
    ([], "no members"),
    ([{"key": "O_LC", "action_overrides": {"option_sizing": 9}}], "action_overrides"),
    ([{"key": "O_LC", "action_overrides": {"option_dte_min": 90, "option_dte_max": 60}}],
     "action_overrides"),
    ([{"key": "O_LC", "action_overrides": {"option_dte_min": "60"}}], "action_overrides"),
    ([{"key": "O_CC", "action_overrides": {"option_dte_min": 60}}], "equity"),
    ([{"key": "O_LC", "exit_overrides": {"tp": {"op": ">"}}}], "exit_overrides"),
    ([{"key": "O_LC", "exit_overrides": {"tp": {"value": 5, "value_min": 10}}}],
     "exit_overrides"),
    ([{"key": "O_LC", "exit_overrides": {"tp": {"value_step": 0}}}], "exit_overrides"),
    ([{"key": "O_LC", "exit_overrides": {"tp": 5}}], "exit_overrides"),
    ([{"key": "O_LC", "bogus": 1}], "unknown member key"),
    ([{"note": "no key"}], "no 'key'"),
])
def test_invalid_members_are_refused_by_name(tmp_path, members, msg):
    m = _launcher()
    path = _write(tmp_path, {"s": {"status": "candidate", "members": members}})
    with pytest.raises(ValueError, match=msg) as exc:
        m._load_style_spec(path, "s")
    assert "'s'" in str(exc.value)                   # every refusal names the style


@pytest.mark.parametrize("members", [
    [{"key": "O_WHEEL"}, {"key": "O_JL"}],           # E2: the hold is member-scoped now
    [{"key": "O_WHEEL"}, {"key": "O_RS"}],
    [{"key": "O_BULLPS"}, {"key": "O_CC"}],           # E1: stock + option overlay members
    [{"key": "O_IC"}, {"key": "O_PP"}],
    [{"key": "O_CC"}, {"key": "O_WHEEL"}],
    # User decision 2026-09-26: member-namespaced ids inside a style. The wheel reuses O_CSP's
    # entry ids and O_CC / O_PP both enter through S2's "buy" / "gate_*"; inside a style each
    # member's ids are its own, so the pairs coexist (tests/test_option_style_builder.py).
    [{"key": "O_WHEEL"}, {"key": "O_CSP"}],
    [{"key": "O_CC"}, {"key": "O_PP"}],
    [{"key": "O_CSP"}, {"key": "O_CC"}, {"key": "O_PP"}, {"key": "O_WHEEL"}],
])
def test_the_revisions_admit_what_they_answered(tmp_path, members):
    m = _launcher()
    style = {"status": "candidate", "price_cap_policy": "none", "members": members}
    assert m._load_style_spec(_write(tmp_path, {"p": style}), "p")["members"] == members


@pytest.mark.parametrize("style, msg", [
    ({"status": "final", "members": [{"key": "O_LC"}]}, "status"),
    ({"members": [{"key": "O_LC"}]}, "status"),
    ({"status": "candidate", "members": [{"key": "O_LC"}], "extra": 1}, "unknown style field"),
    ({"status": "candidate", "members": {"key": "O_LC"}}, "members"),
    ({"status": "candidate", "members": [{"key": "O_LC"}], "convex_fitness_waiver": ""},
     "convex_fitness_waiver"),
    ({"status": "candidate", "members": [{"key": "O_LC"}], "price_cap_policy": "max"},
     "price_cap_policy"),
])
def test_invalid_style_fields_are_refused(tmp_path, style, msg):
    m = _launcher()
    with pytest.raises(ValueError, match=msg):
        m._load_style_spec(_write(tmp_path, {"s": style}), "s")


def test_mixed_price_caps_need_an_explicit_policy(tmp_path):
    m = _launcher()
    mixed = {"status": "candidate", "members": [{"key": "O_BULLPS"}, {"key": "O_WHEEL"}]}
    with pytest.raises(ValueError, match="price_cap_policy") as exc:
        m._load_style_spec(_write(tmp_path, {"p": mixed}), "p")
    assert "Q3" in str(exc.value)
    ok = dict(mixed, price_cap_policy="none")
    assert m._load_style_spec(_write(tmp_path, {"p": ok}), "p")["price_cap_policy"] == "none"
    ok = dict(mixed, price_cap_policy="group_min")
    assert m._load_style_spec(_write(tmp_path, {"p": ok}), "p")["price_cap_policy"] == "group_min"


def test_mixed_caps_on_a_key_other_than_price_max_are_refused_even_with_a_policy(
        tmp_path, monkeypatch):
    m = _launcher()
    real = m._screener_gate_base_for_strategy
    monkeypatch.setattr(m, "_screener_gate_base_for_strategy",
                        lambda k: {"market_cap_min": 1e9} if k == "O_LP" else real(k))
    style = {"status": "candidate", "price_cap_policy": "none",
             "members": [{"key": "O_LC"}, {"key": "O_LP"}]}
    with pytest.raises(ValueError, match="market_cap_min"):
        m._load_style_spec(_write(tmp_path, {"p": style}), "p")


def test_unknown_style_name_and_schema_are_refused(tmp_path):
    m = _launcher()
    path = _write(tmp_path, {"short": {"status": "candidate", "members": [{"key": "O_LC"}]}})
    with pytest.raises(ValueError, match="no style 'mid'"):
        m._load_style_spec(path, "mid")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": 2, "styles": {}}))
    with pytest.raises(ValueError, match="schema"):
        m._load_style_spec(str(bad), "short")
    bad.write_text(json.dumps({"schema": 1, "styles": {}, "x": 1}))
    with pytest.raises(ValueError, match="unknown top-level"):
        m._load_style_spec(str(bad), "short")


def test_a_broken_sibling_style_breaks_the_whole_file(tmp_path):
    m = _launcher()
    path = _write(tmp_path, {"ok": {"status": "candidate", "members": [{"key": "O_LC"}]},
                             "broken": {"status": "candidate", "members": [{"key": "O_SSTG"}]}})
    with pytest.raises(ValueError, match="'broken'"):
        m._load_style_spec(path, "ok")


# --- Task 4: the candidate style configs, as data ----------------------------------------------
_CANDIDATES = str(Path(__file__).resolve().parents[3] / "tools/option_styles.candidates.json")
_CANDIDATE_NOTE = "CANDIDATE: membership decided after stage 1 + the LEAPS grid"


def test_the_candidate_file_has_the_four_styles_all_marked_candidate():
    m = _launcher()
    data = json.loads(Path(_CANDIDATES).read_text())
    assert set(data["styles"]) == {"short", "mid", "leaps_convex", "premium"}
    assert {s["status"] for s in data["styles"].values()} == {"candidate"}
    for name, style in data["styles"].items():
        assert _CANDIDATE_NOTE in style["note"], name
        m._load_style_spec(_CANDIDATES, name)          # every candidate validates


def test_the_premium_candidate_holds_stock_and_options_per_the_revision():
    """User decision 2026-09-25: the premium style holds stock + options. O_CC is a member (E1)
    and O_JL / O_RS sit beside O_WHEEL (E2)."""
    m = _launcher()
    style = m._load_style_spec(_CANDIDATES, "premium")
    keys = [x["key"] for x in style["members"]]
    assert {"O_CC", "O_WHEEL", "O_JL", "O_RS"} <= set(keys)
    assert keys[-1] == "O_WHEEL"                       # Q4 placeholder: the wheel last
    assert style["price_cap_policy"] == "none"         # Q3 placeholder: mixed $100 / uncapped


def test_O_CSP_sits_beside_O_WHEEL_in_premium_now_that_ids_are_member_namespaced():
    """O_CSP's absence from premium was a MECHANISM limit (the wheel reuses O_CSP's entry ids),
    never a strategy verdict: since E2 a CSP (liquidates on assignment) and a wheel (holds and
    writes calls) are different strategies. User decision 2026-09-26: namespace member ids inside
    a style and put O_CSP back, next to the wheel. Membership is still decided after stage 1."""
    m = _launcher()
    keys = [x["key"] for x in m._load_style_spec(_CANDIDATES, "premium")["members"]]
    assert keys[-2:] == ["O_CSP", "O_WHEEL"]


def test_two_members_owning_one_id_prefix_still_collide_and_are_refused(tmp_path, monkeypatch):
    """What member-prefixing CANNOT separate: two members that own the same id prefix (e.g. a
    group key beside one of its own arms, were an arm ever admissible). Simulated by letting
    O_LP own O_LC's prefix and build O_LC's rules."""
    m = _launcher()
    admissible = m._style_admissible_members()
    monkeypatch.setattr(m, "_style_admissible_members", lambda: admissible)
    real_prefixes = m._style_member_prefixes
    monkeypatch.setattr(m, "_style_member_prefixes",
                        lambda k: real_prefixes(k) + (("o_lc",) if k == "O_LP" else ()))
    monkeypatch.setitem(m._STRATEGY_BUILDERS, "O_LP", lambda k: m._build_strategy_option("O_LC"))
    path = _write(tmp_path, {"s": {"status": "candidate",
                                   "members": [{"key": "O_LC"}, {"key": "O_LP"}]}})
    with pytest.raises(ValueError, match=r"O_LC and O_LP build the SAME .*entry:o_lc-entry.*"
                                         r"common id prefix: \['o_lc'\]"):
        m._load_style_spec(path, "s")


def test_the_leaps_convex_candidate_carries_no_convex_waiver():
    """Q5 is open: the style must refuse a CAR-family fitness (Task 7), so no waiver is authored."""
    m = _launcher()
    assert "convex_fitness_waiver" not in m._load_style_spec(_CANDIDATES, "leaps_convex")


# --- review fixes: duplicate keys and non-finite numbers are refused, never absorbed ----------
def test_a_repeated_style_name_is_refused_naming_it(tmp_path):
    m = _launcher()
    p = tmp_path / "dup.json"
    p.write_text('{"schema": 1, "styles": {'
                 '"premium": {"status": "candidate", "members": [{"key": "O_LC"}]}, '
                 '"premium": {"status": "candidate", "members": [{"key": "O_LP"}]}}}')
    with pytest.raises(ValueError, match=r"duplicate key 'premium' at styles"):
        m._load_style_spec(str(p), "premium")


def test_a_member_with_two_key_fields_is_refused_naming_the_path(tmp_path):
    m = _launcher()
    p = tmp_path / "dup.json"
    p.write_text('{"schema": 1, "styles": {"s": {"status": "candidate", '
                 '"members": [{"key": "O_LC"}, {"key": "O_LP", "key": "O_BF"}]}}}')
    with pytest.raises(ValueError, match=r"duplicate key 'key' at styles\.s\.members\[1\]"):
        m._load_style_spec(str(p), "s")


def test_non_finite_json_constants_are_refused_at_load(tmp_path):
    m = _launcher()
    bad = {"value": float("nan"), "value_min": 50, "value_max": 10,
           "value_step": float("inf")}
    path = _write(tmp_path, {"s": {"status": "candidate",
                                   "members": [{"key": "O_LC", "exit_overrides": {"td": bad}}]}})
    assert "NaN" in Path(path).read_text()        # json.dumps writes the non-standard constant
    with pytest.raises(ValueError, match="non-finite"):
        m._load_style_spec(path, "s")


@pytest.mark.parametrize("field, v", [("value", float("nan")), ("value_step", float("inf")),
                                      ("value_min", float("-inf"))])
def test_non_finite_band_values_are_refused_by_the_validator_too(field, v):
    """Belt and braces: a spec reaching the validator by another route (a dict built in code)
    must not slip a NaN past the band checks -- every NaN comparison is False."""
    m = _launcher()
    band = {"value": 20, "value_min": 10, "value_max": 30, "value_step": 5, field: v}
    with pytest.raises(ValueError, match=rf"\[{field!r}\] must be (finite )?numbers"):
        m._validate_style("s", {"status": "candidate",
                                "members": [{"key": "O_LC", "exit_overrides": {"td": band}}]})

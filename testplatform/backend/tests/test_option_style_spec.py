"""Stage-2 style spec: loader, validator and the append-only member -> entry_tag table.

Plan docs/plans/2026-09-25-option-stage2-build.md, Task 3 (and Task 4's candidate file), as
revised after the user's answers: O_CC / O_PP are admissible members (E1 routes each entry by
its own actions) and O_WHEEL may share a style with O_JL / O_RS (E2 holds assigned stock per
member tag). What stays refused is what no revision answered: unbounded / phase-gated keys,
grid-1 family keys, arms of a group key, two members that build the SAME entry ids, and mixed
per-member price caps without an explicit policy (Q3).
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


def test_member_tags_cover_exactly_the_admissible_members_and_never_collide():
    m = _launcher()
    assert set(m._STYLE_MEMBER_TAGS) == m._style_admissible_members()
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
    # O_WHEEL's entry IS O_CSP's, ids included: two rules under one id collide in the genome.
    ([{"key": "O_WHEEL"}, {"key": "O_CSP"}], "SAME entry"),
    # O_CC and O_PP both enter through the S2 equity buy ("buy", "gate_confidence", ...).
    ([{"key": "O_CC"}, {"key": "O_PP"}], "SAME entry"),
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

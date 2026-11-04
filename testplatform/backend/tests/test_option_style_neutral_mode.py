"""Stage-2 style member ``neutral_entry_mode`` (plan 2026-11-04 T1).

A neutral member (O_STRD / O_STRG / O_IC) may build in the joint / hold / low_confidence mode a
stage-1 single job ran in, so its entry rule ids and genes are the single job's. Any non-legacy
member turns ``evaluate_entry_rules_on_hold`` on for the run, which lets HOLD reach EVERY entry
rule; each directional member therefore gets one fixed (gene-less) ``rec_direction != 0`` leaf.
"""
from pathlib import Path

import pytest

from tests.test_option_grid_foundations import _launcher
from tests.test_option_style_builder import _custom, _leaves, _style, _write
from tests.test_option_style_registration import _argv, _run, _no_style_leaks  # noqa: F401


def _stage2():
    return str(Path(__file__).resolve().parents[3] / "tools/option_styles.stage2.json")


def _space(s):
    from app.services.strategy_param_space import collect_param_space
    return collect_param_space(s)


def _member_genes(space, member, strategy):
    """The member's ENTRY genes: the entry rule toggles/actions and the conditions of its entry
    rules (its exit conditions share the ``cond:<member>-`` prefix and are excluded)."""
    m = member.lower()
    ids = {leaf["id"] for r in strategy.entry_rules for leaf in _leaves(r["conditions"])}
    return {k: v for k, v in space.items()
            if k.startswith(f"entry:{m}-")
            or (k.startswith("cond:") and k.split(":")[1] in ids and k.startswith(f"cond:{m}-"))}


def _clamped(m, strategy):
    """What ``_build_strategy`` does to every strategy (single or O_STYLE) for the expert."""
    return m._clamp_confidence_genes(strategy, "DeterministicScorer")


@pytest.mark.parametrize("member", ["O_STRD", "O_STRG", "O_IC"])
def test_a_joint_member_has_exactly_the_single_joint_jobs_entry_and_cond_genes(tmp_path, member):
    m = _launcher()
    s = _custom(m, tmp_path, [{"key": member, "neutral_entry_mode": "joint"}])
    single = m._build_strategy(member, "j", "DeterministicScorer", neutral_entry_mode="joint")
    s = _clamped(m, s)
    want = _member_genes(_space(single), member, single)
    got = _member_genes(_space(s), member, s)
    lo = member.lower()
    assert f"entry:{lo}-entry-hold:enabled" in got
    assert f"entry:{lo}-entry-low_confidence:enabled" in got
    assert got == want
    assert [r["id"] for r in s.entry_rules] == [r["id"] for r in single.entry_rules]


def test_an_absent_mode_is_legacy_and_changes_nothing(tmp_path):
    m = _launcher()
    plain = _custom(m, tmp_path, [{"key": "O_STRD"}])
    explicit = _custom(m, tmp_path, [{"key": "O_STRD", "neutral_entry_mode": "legacy"}])
    assert plain.entry_rules == explicit.entry_rules
    assert [r["id"] for r in plain.entry_rules] == ["o_strd-entry"]
    assert set(_space(plain)) == set(_space(explicit))


@pytest.mark.parametrize("mode", ["hold", "low_confidence"])
def test_a_fixed_arm_member_has_the_single_jobs_condition_genes(tmp_path, mode):
    m = _launcher()
    s = _custom(m, tmp_path, [{"key": "O_STRD", "neutral_entry_mode": mode}])
    single = m._build_strategy("O_STRD", "j", "DeterministicScorer", neutral_entry_mode=mode)
    s = _clamped(m, s)
    got = _member_genes(_space(s), "O_STRD", s)
    want = _member_genes(_space(single), "O_STRD", single)
    assert {k: v for k, v in got.items() if k.startswith("cond:")} ==         {k: v for k, v in want.items() if k.startswith("cond:")}


def test_directional_members_gain_only_the_fixed_leaf_and_no_gene(tmp_path):
    m = _launcher()
    flagged = _custom(m, tmp_path, [{"key": "O_STRD", "neutral_entry_mode": "joint"},
                                    {"key": "O_BULLCS"}, {"key": "O_LC"}])
    sp_f = _space(flagged)
    # the same members with the flag off (an all-legacy style)
    base = _custom(m, tmp_path, [{"key": "O_STRD"}, {"key": "O_BULLCS"}, {"key": "O_LC"}])
    sp_b = _space(base)
    for d in ("O_BULLCS", "O_LC"):
        assert _member_genes(sp_f, d, flagged) == _member_genes(sp_b, d, base)  # no gene added
        rid = f"{d.lower()}-entry"
        rf = next(r for r in flagged.entry_rules if r["id"] == rid)
        rb = next(r for r in base.entry_rules if r["id"] == rid)
        assert rf["conditions"]["conditions"][-1] == {
            "id": f"{d.lower()}-directional", "field": "rec_direction", "op": "!=", "value": 0}
        assert rf["conditions"]["conditions"][:-1] == rb["conditions"]["conditions"]
    # the leaf is the one the low_confidence arm adds (one shared builder)
    lc = m._build_strategy("O_STRD", "j", "DeterministicScorer",
                           neutral_entry_mode="low_confidence")
    assert m._directional_leaf("O_STRD") in lc.entry_rules[0]["conditions"]["conditions"]


def test_the_directional_leaf_is_accepted_by_the_canonical_leaf_model():
    from ba2_common.core.rule_models import ConditionLeaf
    ConditionLeaf(**_launcher()._directional_leaf("O_LC"))


def test_an_all_legacy_style_is_unchanged(tmp_path):
    m = _launcher()
    s = _custom(m, tmp_path, [{"key": "O_STRD"}, {"key": "O_BULLCS"}])
    ids = [x["id"] for r in s.entry_rules for x in _leaves(r["conditions"])]
    assert "o_bullcs-directional" not in ids
    assert m._style_has_non_legacy_neutral(m._STYLE_ACTIVE) is False
    assert m._style_effective_neutral_mode("O_STYLE", "legacy") == "legacy"


def test_the_setting_is_set_iff_a_member_is_non_legacy(tmp_path):
    m = _launcher()
    _custom(m, tmp_path, [{"key": "O_STRD", "neutral_entry_mode": "joint"}, {"key": "O_LC"}])
    block = {"experts": [{"settings": {}}]}
    m._apply_neutral_entry_setting(block, m._style_effective_neutral_mode("O_STYLE", "legacy"))
    assert block["experts"][0]["settings"]["evaluate_entry_rules_on_hold"] is True
    _custom(m, tmp_path, [{"key": "O_STRD"}, {"key": "O_LC"}])
    block = {"experts": [{"settings": {}}]}
    m._apply_neutral_entry_setting(block, m._style_effective_neutral_mode("O_STYLE", "legacy"))
    assert "evaluate_entry_rules_on_hold" not in block["experts"][0]["settings"]


def test_the_optimize_command_sets_the_expert_setting_for_the_mid_style(monkeypatch):
    _, cfg = _run(_argv("mid", config=_stage2()), monkeypatch)
    for ex in cfg["backtest"]["experts"]:
        assert ex["settings"]["evaluate_entry_rules_on_hold"] is True
    spec = cfg["backtest"]["option_style"]
    assert [mem["neutral_entry_mode"] for mem in spec["members"][:2]] == ["joint", "joint"]


def test_the_optimize_command_leaves_a_legacy_style_without_the_setting(monkeypatch):
    cand = str(Path(_stage2()).with_name("option_styles.candidates.json"))
    _, cfg = _run(_argv("short", config=cand), monkeypatch)
    for ex in cfg["backtest"]["experts"]:
        assert "evaluate_entry_rules_on_hold" not in ex["settings"]


def test_the_cli_flag_with_o_style_is_refused(monkeypatch):
    with pytest.raises(SystemExit, match="style spec owns it"):
        _run(_argv("mid", "--neutral-entry-mode", "joint", config=_stage2()), monkeypatch)


# --- refusals ---------------------------------------------------------------------------------
def _load(m, tmp_path, members):
    style = {"status": "candidate", "price_cap_policy": "none", "members": members}
    return m._load_style_spec(_write(tmp_path, {"x": style}), "x")


def test_a_directional_member_may_not_set_the_field(tmp_path):
    m = _launcher()
    with pytest.raises(ValueError, match=r"style 'x' member O_LC.*neutral_entry_mode.*directional"):
        _load(m, tmp_path, [{"key": "O_LC", "neutral_entry_mode": "joint"}])


def test_an_unknown_value_is_refused_by_name(tmp_path):
    m = _launcher()
    with pytest.raises(ValueError, match=r"style 'x' member O_STRD.*neutral_entry_mode.*'both'"):
        _load(m, tmp_path, [{"key": "O_STRD", "neutral_entry_mode": "both"}])


def test_a_legacy_neutral_beside_a_non_legacy_one_is_refused(tmp_path):
    m = _launcher()
    with pytest.raises(ValueError, match=r"style 'x'.*\['O_STRG'\].*legacy neutral gate"):
        _load(m, tmp_path, [{"key": "O_STRD", "neutral_entry_mode": "joint"}, {"key": "O_STRG"}])


def test_stage2_mid_is_joint_on_both_neutral_members():
    m = _launcher()
    s = _style(m, "mid", _stage2())
    ids = [r["id"] for r in s.entry_rules]
    assert "o_strd-entry-hold" in ids and "o_strg-entry-low_confidence" in ids
    assert "o_strd-entry" not in ids

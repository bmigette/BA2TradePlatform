"""The opt-in ``structure_share`` style field (plan 2026-11-06, Task 9)."""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "testplatform"))

from tests.test_option_grid_foundations import _launcher  # noqa: E402
from tests.test_option_style_builder import _custom, _leaves, _write  # noqa: E402
from tests.test_option_style_risk import _RISK  # noqa: E402

SHARE = {"min": 20, "max": 60, "step": 10}


@pytest.fixture(autouse=True)
def _no_style_leaks(monkeypatch):
    m = _launcher()
    monkeypatch.setattr(m, "_STYLE_ACTIVE", None)
    monkeypatch.setattr(m, "_MARKET_CONDITION_PROFILES", ())
    monkeypatch.setattr(m, "_BENCHMARK_REGIME_GATES", False)
    yield


def _share_leaves(rule):
    return [x for x in _leaves(rule["conditions"]) if x.get("field") == "structure_share"]


def _opted_in(m, tmp_path, members, share=SHARE, **extra):
    return _custom(m, tmp_path, members, structure_share=share, risk=_RISK, **extra)


def test_every_option_entry_rule_of_an_opted_in_style_ends_with_its_members_leaf(tmp_path):
    m = _launcher()
    s = _opted_in(m, tmp_path, [{"key": "O_CBS"}, {"key": "O_CONVEX"}, {"key": "O_PMCC"}])
    assert [r["id"] for r in s.entry_rules] == [
        "o_cbs-entry", "o_convexc-entry", "o_convexp-entry", "o_pmcc-entry"]
    expected = {"o_cbs-entry": "o_cbs-structure_share",
                "o_convexc-entry": "o_convex-structure_share",      # ONE gene for both arms
                "o_convexp-entry": "o_convex-structure_share",
                "o_pmcc-entry": "o_pmcc-structure_share"}
    for rule in s.entry_rules:
        root = rule["conditions"]["conditions"]
        (leaf,) = _share_leaves(rule)
        assert root[-1] is leaf, "the leaf must be LAST in the AND list"
        assert leaf["id"] == expected[rule["id"]]
        assert leaf["comparison"] == "<" and leaf["optimize"] is True
        assert (leaf["value_min"], leaf["value_max"], leaf["value_step"]) == (20.0, 60.0, 10.0)
        assert "toggle_optimize" not in leaf or not leaf["toggle_optimize"]


def test_the_leaf_stays_last_behind_market_and_spy_regime_gates(tmp_path, monkeypatch):
    m = _launcher()
    monkeypatch.setattr(m, "_MARKET_CONDITION_PROFILES", ("ohlcv-v1", "ta-structure-v1"))
    monkeypatch.setattr(m, "_BENCHMARK_REGIME_GATES", True)
    s = _opted_in(m, tmp_path, [{"key": "O_CBS"}])
    (rule,) = s.entry_rules
    assert rule["conditions"]["conditions"][-1]["field"] == "structure_share"
    assert len(rule["conditions"]["conditions"]) > 8       # the other gates are still there


def test_the_gene_space_has_exactly_one_value_gene_per_member_and_no_enabled_toggle(tmp_path):
    from app.services.strategy_param_space import _range_entry, collect_param_space
    m = _launcher()
    s = _opted_in(m, tmp_path, [{"key": "O_CBS"}, {"key": "O_CONVEX"}])
    space = collect_param_space(s)
    mine = {k: v for k, v in space.items() if "structure_share" in k}
    assert set(mine) == {"cond:o_cbs-structure_share:value",
                         "cond:o_convex-structure_share:value"}
    assert mine["cond:o_cbs-structure_share:value"] == _range_entry(20.0, 60.0, 10.0,
                                                                    is_int=False)


def test_the_leaf_converts_to_a_trigger_beside_the_rules_entry_tag(tmp_path):
    from ba2_common.core.rule_builders import checked_entry_tag, triggers_from_condition_tree
    m = _launcher()
    s = _opted_in(m, tmp_path, [{"key": "O_CBS"}])
    (rule,) = s.entry_rules
    triggers = triggers_from_condition_tree(rule["conditions"])
    shares = [t for t in triggers.values() if t["event_type"] == "structure_share"]
    assert shares == [{"event_type": "structure_share", "operator": "<", "value": 60.0}]
    assert checked_entry_tag(rule["actions"][0]) == m._STYLE_MEMBER_TAGS["O_CBS"]


def test_equity_entry_members_get_no_leaf(tmp_path):
    m = _launcher()
    s = _opted_in(m, tmp_path, [{"key": "O_CBS"}, {"key": "O_CC"}])
    by_id = {r["id"]: r for r in s.entry_rules}
    assert _share_leaves(by_id["o_cbs-entry"])
    cc = [r for r in s.entry_rules if r["id"] != "o_cbs-entry"]
    assert cc and all(not _share_leaves(r) for r in cc)


def test_removing_the_leaves_gives_back_the_style_without_the_field(tmp_path):
    m = _launcher()
    members = [{"key": "O_CBS"}, {"key": "O_CONVEX"}]
    plain = _custom(m, tmp_path, members, risk=_RISK)
    opted = _opted_in(m, tmp_path, members)

    def strip(rules):
        out = json.loads(json.dumps(rules, default=str))
        for r in out:
            r["conditions"]["conditions"] = [c for c in r["conditions"]["conditions"]
                                             if c.get("field") != "structure_share"]
        return out

    assert strip(opted.entry_rules) == json.loads(json.dumps(plain.entry_rules, default=str))
    assert opted.exit_rules == plain.exit_rules


@pytest.mark.parametrize("band,msg", [
    ({"min": 0, "max": 60, "step": 10}, "0 <"),
    ({"min": 20, "max": 101, "step": 10}, "<= 100"),
    ({"min": 70, "max": 60, "step": 10}, "min <= max"),
    ({"min": 20, "max": 60, "step": 0}, "step"),
    ({"min": 20, "max": 60}, "exactly"),
    ({"min": 20, "max": 60, "step": 10, "x": 1}, "exactly"),
    ({"min": 20, "max": 60, "step": 50}, "step"),                # step > max - min
    ({"min": True, "max": 60, "step": 10}, "number"),
    ({"min": "20", "max": 60, "step": 10}, "number"),
    ([20, 60, 10], "object"),
])
def test_a_bad_band_is_refused_by_name(tmp_path, band, msg):
    m = _launcher()
    with pytest.raises(ValueError, match=msg):
        m._load_style_spec(_write(tmp_path, {"x": {
            "status": "candidate", "price_cap_policy": "none", "risk": _RISK,
            "members": [{"key": "O_CBS"}], "structure_share": band}}), "x")


def test_structure_share_without_a_risk_block_is_refused(tmp_path):
    m = _launcher()
    with pytest.raises(ValueError, match="risk"):
        m._load_style_spec(_write(tmp_path, {"x": {
            "status": "candidate", "price_cap_policy": "none",
            "members": [{"key": "O_CBS"}], "structure_share": SHARE}}), "x")


def test_structure_share_on_a_style_with_no_option_entry_member_is_refused(tmp_path):
    m = _launcher()
    with pytest.raises(ValueError, match="option entry"):
        m._load_style_spec(_write(tmp_path, {"x": {
            "status": "candidate", "price_cap_policy": "none", "risk": _RISK,
            "members": [{"key": "O_CC"}], "structure_share": SHARE}}), "x")


def test_job_c_long_opts_in_with_20_to_60_and_mid_premium_do_not():
    m = _launcher()
    stage2 = str(__import__("pathlib").Path(__file__).resolve().parents[3]
                 / "tools/option_styles.stage2.json")
    assert m._load_style_spec(stage2, "long")["structure_share"] == {"min": 20, "max": 60, "step": 10}
    for name in ("mid", "premium"):
        assert "structure_share" not in m._load_style_spec(stage2, name)

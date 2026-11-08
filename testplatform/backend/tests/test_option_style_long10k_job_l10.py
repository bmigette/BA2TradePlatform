"""Stage-2 job L10 (style ``long10k``, DRAFT, owner 2026-11-08): $10k max-profit long book.

* ``long10k`` in tools/option_styles.stage2.json: members/order O_LC > O_BULLCS > O_CONVEX > O_LEAP, draft status,
  structure_share 20..80, per-instrument cap 10..40, the DTE-invariant members carry no action_overrides.
* tools/stage2_seed_policy.jobL10.json is a valid policy for it and assembles offline against the real space
  (synthetic genomes, reusing job C's offline assembler).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "testplatform"))

import ba2test_launcher as L  # noqa: E402
from tests.test_option_style_long_job_c import B, _space, _synthetic_bundle  # noqa: E402

_ROOT = Path(__file__).resolve().parents[3]
_STAGE2 = str(_ROOT / "tools/option_styles.stage2.json")
_POLICY = _ROOT / "tools/stage2_seed_policy.jobL10.json"
MEMBERS = ["O_LC", "O_BULLCS", "O_CONVEX", "O_LEAP"]


@pytest.fixture(autouse=True)
def _no_style_leaks(monkeypatch):
    monkeypatch.setattr(L, "_STYLE_ACTIVE", None)
    monkeypatch.setattr(L, "_BENCHMARK_REGIME_GATES", False)
    yield


def test_long10k_members_order_and_bands():
    style = L._load_style_spec(_STAGE2, "long10k")
    assert style["status"] == "draft"
    assert [m["key"] for m in style["members"]] == MEMBERS
    assert style["price_cap_policy"] == "none"
    assert style["structure_share"] == {"min": 20, "max": 80, "step": 10}
    g = style["risk"]["genes"]
    assert g["max_virtual_equity_per_instrument_percent"] == {"min": 10, "max": 40, "step": 5}
    assert g["max_deployment_pct"] == {"min": 30, "max": 90, "step": 10}
    assert style["risk"]["fixed"] == {"max_concurrent_structures": 20, "undefined_risk_max_pct": 0}
    for m in style["members"]:
        if m["key"] in L._STYLE_DTE_INVARIANT_MEMBERS:
            assert "action_overrides" not in m
    assert set(MEMBERS) <= L._style_admissible_members()


def test_long10k_builds_with_every_member_mapped():
    L._STYLE_ACTIVE = L._load_style_spec(_STAGE2, "long10k")
    strat = L._build_strategy("O_STYLE", "t", "DeterministicScorer")
    assert sorted(strat.style_id_maps) == sorted(MEMBERS)


def test_job_l10_policy_is_valid_and_assembles():
    policy = json.loads(_POLICY.read_text(encoding="utf-8"))
    B.validate_policy(policy)
    assert policy["style"] == "long10k"
    for ind in policy["individuals"]:
        for c in ind["contributions"]:
            assert c["member"] in MEMBERS
            # only the FMPRating LEAP source is grafted; the DS-native sources are taken as they are
            assert ("donor" in c) == (c["member"] == "O_LEAP"), c["member"]
    L._STYLE_ACTIVE = L._load_style_spec(_STAGE2, "long10k")
    L._BENCHMARK_REGIME_GATES = True
    strat, space = _space()
    bundle = _synthetic_bundle(policy, space)
    seeds, _ = L._assemble_seed_population(bundle, strat, space, "zero", 40)
    assert len(seeds) == len(policy["individuals"]) == 19
    assert all(set(s) == set(space) for s in seeds)
    share = [k for k in space if k.endswith("-structure_share:value")]
    assert len(share) == 4
    assert all(s[k] == 80.0 for s in seeds for k in share)

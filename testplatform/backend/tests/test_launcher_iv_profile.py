"""``--market-condition-profile iv-v1`` (docs/plans/2026-11-15-option-iv-conditions.md).

Pinned here, at the launcher / gene / trial-config level (the engine-level effect is pinned in
``tests/backtest/test_market_condition_iv_engine.py``):

* every permitted structure gets exactly two iv-v1 leaves -- ``<m>-market-ivrv`` and
  ``<m>-market-ivterm`` -- each with a ``mode`` (off/below/above) and a ``value`` gene over the
  registry range: FOUR genes per entry arm, on the initial-entry tree only;
* the profile COMBINES with the OHLCV profiles exactly the way they combine with each other
  (``ohlcv-v1,iv-v1`` = the two profiles' leaves in registry order, one manifest each);
* the all-off control decodes back to profile ``none``'s rules;
* THE WHITELIST TRAP: a decoded iv genome survives ``_build_daily_trial_config`` -- the resolved
  leaf (operator and threshold) is in the trial's entry rules, and the trial pins the profile and
  its manifest -- so the GA cannot score a gene the trial silently dropped;
* the launcher records the chain manifest's ``option_source`` fact (and only for it).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import date

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_LAUNCHER = os.path.normpath(os.path.join(_ROOT, "..", "ba2test_launcher.py"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ba2_common.core.market_conditions import IV_PROFILE, PROFILES  # noqa: E402
from ba2_common.core.rule_models import MODE_OFF, NUMERIC_MODE_CHOICES  # noqa: E402

from app.services.strategy_param_space import collect_param_space, decode_params  # noqa: E402

_spec = importlib.util.spec_from_file_location("ba2test_launcher_iv", _LAUNCHER)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

PERMITTED = ["O_LC", "O_LP", "O_VERT", "O_BULLCS", "O_BULLPS", "O_BEARCS", "O_BF",
             "O_IC", "O_JL", "O_RS", "O_CSP", "O_STRD", "O_STRG",
             "O_CC", "O_PP", "O_WHEEL"]
MC_START, MC_END = "2025-06-30", "2025-07-01"


def _built(kind: str):
    return mod._build_strategy(kind, f"iv-{kind}", "FMPRating")


def _market_ids(node) -> list:
    out: list = []

    def walk(n):
        if isinstance(n, list):
            for x in n:
                walk(x)
        elif isinstance(n, dict):
            cid = n.get("id")
            if isinstance(cid, str) and "-market-" in cid:
                out.append(cid)
            for v in n.values():
                walk(v)

    walk(node)
    return out


def _find(node, leaf_id):
    if isinstance(node, list):
        for x in node:
            hit = _find(x, leaf_id)
            if hit is not None:
                return hit
    elif isinstance(node, dict):
        if node.get("id") == leaf_id:
            return node
        for v in node.values():
            hit = _find(v, leaf_id)
            if hit is not None:
                return hit
    return None


@pytest.fixture
def iv_on(monkeypatch):
    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", (IV_PROFILE,))
    yield IV_PROFILE


@pytest.mark.parametrize("kind", PERMITTED)
def test_every_permitted_structure_gets_the_two_iv_leaves_and_four_genes(kind, iv_on):
    strat = _built(kind)
    member = "o_wheel" if kind == "O_WHEEL" else kind.lower()
    ids = sorted(set(_market_ids(strat.entry_rules)))
    assert ids == sorted({f"{member}-market-ivrv", f"{member}-market-ivterm"}), (kind, ids)
    assert _market_ids(strat.exit_rules) == []
    space = collect_param_space(strat)
    genes = sorted(g for g in space if "-market-" in g)
    assert genes == sorted(f"cond:{member}-market-{s}:{k}" for s in ("ivrv", "ivterm")
                           for k in ("mode", "value")), (kind, genes)
    for short, field in (("ivrv", "underlying_iv_rv_ratio_30_20"), ("ivterm", "underlying_iv_term_slope_30_60")):
        spec = next(f for f in PROFILES[IV_PROFILE].fields if f.name == field)
        mode = space[f"cond:{member}-market-{short}:mode"]
        assert mode["type"] == "choice" and list(mode["choices"]) == list(NUMERIC_MODE_CHOICES)
        value = space[f"cond:{member}-market-{short}:value"]
        assert (value["min"], value["max"]) == (pytest.approx(spec.value_min), pytest.approx(spec.value_max))
        assert value["step"] == pytest.approx(spec.value_step)
    assert sorted(mod._market_condition_gene_names(strat)) == genes


def test_the_profile_combines_with_ohlcv_in_registry_order(monkeypatch):
    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", ("ohlcv-v1", IV_PROFILE))
    leaves = mod._market_condition_gates("o_ic")
    assert [lf["id"] for lf in leaves] == ["o_ic-market-slope", "o_ic-market-adx", "o_ic-market-rv",
                                           "o_ic-market-ivrv", "o_ic-market-ivterm"]
    # the operator's comma order does not reorder the genes
    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", (IV_PROFILE, "ohlcv-v1"))
    assert [lf["id"] for lf in mod._market_condition_gates("o_ic")][:3] == \
        ["o_ic-market-slope", "o_ic-market-adx", "o_ic-market-rv"]
    assert mod._resolve_market_condition_profiles("ohlcv-v1,iv-v1", "optimize") == ("ohlcv-v1", IV_PROFILE)
    assert mod._resolve_market_condition_profiles("iv-v1", "optimize") == (IV_PROFILE,)


def test_the_authored_anchor_is_the_registry_anchor(iv_on):
    leaves = {lf["field"]: lf for lf in mod._market_condition_gates("o_bullps")}
    for f in PROFILES[IV_PROFILE].fields:
        if not f.searched:
            assert f.name not in leaves
            continue
        lf = leaves[f.name]
        assert (lf["op"], lf["value"]) == (f.anchor_op, f.anchor_value)
        assert lf["mode_optimize"] is True and not lf.get("toggle_optimize")


def test_the_all_off_control_decodes_to_profile_none(iv_on, monkeypatch):
    gated = _built("O_IC")
    space = collect_param_space(gated)
    modes = [g for g in space if g.endswith(":mode") and "-market-" in g]
    decoded = decode_params(gated, {g: MODE_OFF for g in modes})
    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", ())
    plain = decode_params(_built("O_IC"), {})
    assert _market_ids(decoded["entry_rules"]) == []
    assert decoded["entry_rules"] == plain["entry_rules"]
    assert decoded["exit_rules"] == plain["exit_rules"]


# --------------------------------------------------------------------------- the trial config
def _publish_iv_manifest(root, symbols=("AAA", "BBB"), bad=(), option_source="thetadata"):
    """``bad`` symbols: IV30/IV60 (and the ratios) ``insufficient_history`` on every session while the
    realized vol is valid -- a broken chain hiding behind a legitimate-looking status."""
    from ba2_common.core.market_condition_store import MarketConditionStore
    from ba2_common.core.market_conditions import STATUS_INSUFFICIENT_HISTORY, STATUS_VALID

    profile = PROFILES[IV_PROFILE]
    fields = [f.name for f in profile.fields]
    sessions = [date(2025, 6, 27), date(2025, 6, 30), date(2025, 7, 1)]
    store = MarketConditionStore(root)
    objects = []
    for symbol in symbols:
        if symbol in bad:
            vals, sts = [None, None, None, None, 0.2], [STATUS_INSUFFICIENT_HISTORY] * 4 + [STATUS_VALID]
        else:
            vals, sts = [1.5, 1.05, 0.3, 0.28, 0.2], [STATUS_VALID] * len(fields)
        rows = [{"session": s, "values": vals, "status": sts,
                 "reasons": ["" if st == STATUS_VALID else "no expiry" for st in sts],
                 "window_digest": "sha256:" + "1" * 64, "raw_shard_ref": "",
                 "raw_row_lo": 0, "raw_row_hi": 0} for s in sessions]
        for month in sorted({(r["session"].year, r["session"].month) for r in rows}):
            entry, _ = store.write_feature_object(
                profile, symbol, [r for r in rows if (r["session"].year, r["session"].month) == month])
            objects.append(entry)
    manifest = store.make_manifest(
        profile, source_profile="fmp-daily-split-adjusted-v1", timing_policy="prior_session_v1",
        objects=objects, raw_objects=[], coverage={s: {"rows": len(sessions)} for s in symbols},
        universe=list(symbols), sessions=sessions, window_start=date.fromisoformat(MC_START),
        window_end=date.fromisoformat(MC_END), option_source=option_source)
    return store, store.write_manifest(manifest)


@pytest.fixture
def iv_snapshot(tmp_path, monkeypatch):
    import ba2_common.config as bc

    store, digest = _publish_iv_manifest(tmp_path / "cache")
    monkeypatch.setattr(bc, "CACHE_FOLDER", str(store.cache_root))
    return digest


def test_a_decoded_iv_genome_reaches_the_trial_config_whole(iv_on, iv_snapshot, monkeypatch):
    from app.services.strategy_optimization_handler import _build_daily_trial_config

    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFESTS", {IV_PROFILE: iv_snapshot})
    strat = _built("O_IC")
    backtest_cfg = {
        "backtest_id": "iv", "start_date": MC_START, "end_date": MC_END,
        "enabled_instruments": ["AAA", "BBB"], "experts": [{"class": "FMPRating", "settings": {}}],
        "initial_capital": 20_000.0, "account_settings": {}, "warmup_days": 0, "seed": 1,
        "entry_action": getattr(strat, "entry_action", None),
    }
    recorded = mod._apply_market_conditions("optimize", backtest_cfg, strat)
    assert recorded["facts"][IV_PROFILE]["option_source"] == "thetadata"
    assert recorded["gene_count"] == 4
    backtest_cfg = json.loads(json.dumps(backtest_cfg, default=str))
    genome = {"cond:o_ic-market-ivrv:mode": "above", "cond:o_ic-market-ivrv:value": 1.4,
              "cond:o_ic-market-ivterm:mode": "off", "cond:o_ic-market-ivterm:value": 1.1}
    trial = _build_daily_trial_config(backtest_cfg, decode_params(strat, genome), None,
                                      option_trade_records=False)
    assert trial["market_condition_profiles"] == [IV_PROFILE]
    assert trial["market_condition_manifests"] == {IV_PROFILE: iv_snapshot}
    assert trial["_ga_trial"] is True
    leaf = _find(trial["entry_rules"], "o_ic-market-ivrv")
    assert leaf is not None, "the iv gene was dropped on the way into the trial"
    assert leaf["field"] == "underlying_iv_rv_ratio_30_20"
    assert leaf["op"] == ">" and leaf["value"] == pytest.approx(1.4)
    assert _find(trial["entry_rules"], "o_ic-market-ivterm") is None      # mode off removes it
    # the expert setting live and the backtest seam both read carries the profile
    assert trial["experts"][0]["settings"]["market_condition_profile"] == IV_PROFILE
    # ... and a different threshold is a different trial
    other = _build_daily_trial_config(backtest_cfg, decode_params(strat, dict(genome, **{
        "cond:o_ic-market-ivrv:mode": "below", "cond:o_ic-market-ivrv:value": 0.9})), None,
        option_trade_records=False)
    leaf2 = _find(other["entry_rules"], "o_ic-market-ivrv")
    assert leaf2["op"] == "<" and leaf2["value"] == pytest.approx(0.9)


def test_an_ohlcv_manifest_records_no_option_source_fact(monkeypatch, tmp_path):
    from ba2_common.core.market_condition_store import MarketConditionStore
    from ba2_common.core.market_conditions import STATUS_VALID
    import ba2_common.config as bc

    profile = PROFILES["ohlcv-v1"]
    store = MarketConditionStore(tmp_path / "c")
    rows = [{"session": date(2025, 6, 27), "values": [0.0, 20.0, 1.0], "status": [STATUS_VALID] * 3,
             "reasons": [""] * 3, "window_digest": "sha256:" + "2" * 64, "raw_shard_ref": "",
             "raw_row_lo": 0, "raw_row_hi": 0}]
    entry, _ = store.write_feature_object(profile, "AAA", rows)
    digest = store.write_manifest(store.make_manifest(
        profile, source_profile="fmp-daily-split-adjusted-v1", timing_policy="prior_session_v1",
        objects=[entry], raw_objects=[], coverage={"AAA": {"rows": 1}}, universe=["AAA"],
        sessions=[date(2025, 6, 27)], window_start=date(2025, 6, 27), window_end=date(2025, 6, 27)))
    monkeypatch.setattr(bc, "CACHE_FOLDER", str(store.cache_root))
    facts = mod._market_condition_manifest_facts(digest, "ohlcv-v1")
    assert "option_source" not in facts


# --------------------------------------------------------------------------- review 2026-11-15
def _cfg(strat, **over):
    cfg = {"backtest_id": "iv", "start_date": MC_START, "end_date": MC_END,
           "enabled_instruments": ["AAA", "BBB"], "experts": [{"class": "FMPRating", "settings": {}}],
           "initial_capital": 20_000.0, "account_settings": {}, "warmup_days": 0, "seed": 1,
           "entry_action": getattr(strat, "entry_action", None)}
    cfg.update(over)
    return cfg


def test_an_iv_manifest_from_another_option_store_is_refused_on_an_option_job(iv_on, iv_snapshot, monkeypatch):
    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFESTS", {IV_PROFILE: iv_snapshot})
    strat = _built("O_IC")
    with pytest.raises(SystemExit) as e:
        mod._apply_market_conditions("optimize", _cfg(strat, options_store="alphavantage"), strat, "O_IC")
    assert "thetadata" in str(e.value) and "alphavantage" in str(e.value)
    # the matching store passes, and an equity job (no chain traded) is not held to a store
    mod._apply_market_conditions("optimize", _cfg(strat, options_store="thetadata"), strat, "O_IC")
    mod._apply_market_conditions("optimize", _cfg(strat, options_store="sqlite"), strat, "S1")


def test_a_broken_chain_is_refused_at_launch_unless_accepted(iv_on, tmp_path, monkeypatch):
    import ba2_common.config as bc

    store, digest = _publish_iv_manifest(tmp_path / "cache2", bad=("AAA",))
    monkeypatch.setattr(bc, "CACHE_FOLDER", str(store.cache_root))
    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFESTS", {IV_PROFILE: digest})
    strat = _built("O_IC")
    mod._resolve_iv_quality(type("A", (), {})())
    with pytest.raises(SystemExit) as e:
        mod._apply_market_conditions("optimize", _cfg(strat, options_store="thetadata"), strat, "O_IC")
    msg = str(e.value)
    assert "AAA" in msg and "BBB" not in msg.split("silently refuse:")[1] and "--iv-quality-accept" in msg
    # accepted by name: launches, and the acceptance is recorded on the run
    args = type("A", (), {"iv_quality_accept": "aaa", "iv_quality_max_run": None, "iv_quality_max_rate": None})()
    mod._resolve_iv_quality(args)
    try:
        cfg = _cfg(strat, options_store="thetadata")
        recorded = mod._apply_market_conditions("optimize", cfg, strat, "O_IC")
        assert recorded["facts"][IV_PROFILE]["iv_quality_accepted"] == ["AAA"]
    finally:
        mod._resolve_iv_quality(type("A", (), {})())


# --------------------------------------------------------------------------- option source binding (2026-11-15)
def _ohlcv_manifest(root):
    from ba2_common.core.market_condition_store import MarketConditionStore
    from ba2_common.core.market_conditions import STATUS_VALID

    profile = PROFILES["ohlcv-v1"]
    sessions = [date(2025, 6, 27), date(2025, 6, 30), date(2025, 7, 1)]
    store = MarketConditionStore(root)
    objects = []
    for sym in ("AAA", "BBB"):
        for month in ((2025, 6), (2025, 7)):
            rows = [{"session": s, "values": [0.0, 20.0, 1.0], "status": [STATUS_VALID] * 3, "reasons": [""] * 3,
                     "window_digest": "sha256:" + "3" * 64, "raw_shard_ref": "", "raw_row_lo": 0, "raw_row_hi": 0}
                    for s in sessions if (s.year, s.month) == month]
            objects.append(store.write_feature_object(profile, sym, rows)[0])
    return store, store.write_manifest(store.make_manifest(
        profile, source_profile="fmp-daily-split-adjusted-v1", timing_policy="prior_session_v1",
        objects=objects, raw_objects=[], coverage={s: {"rows": 3} for s in ("AAA", "BBB")},
        universe=["AAA", "BBB"], sessions=sessions, window_start=date.fromisoformat(MC_START),
        window_end=date.fromisoformat(MC_END)))


def test_an_iv_job_records_its_option_source_and_an_ohlcv_job_does_not(iv_snapshot, tmp_path, monkeypatch):
    """BACKWARD COMPATIBILITY: the new expert setting appears ONLY on a job gated on a chain profile."""
    import ba2_common.config as bc
    from ba2_common.core.market_condition_rules import OPTION_SOURCE_SETTING

    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", (IV_PROFILE,))
    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFESTS", {IV_PROFILE: iv_snapshot})
    strat = _built("O_IC")
    cfg = _cfg(strat, options_store="thetadata")
    mod._apply_market_conditions("optimize", cfg, strat, "O_IC")
    assert cfg["experts"][0]["settings"][OPTION_SOURCE_SETTING] == "thetadata"

    store, digest = _ohlcv_manifest(tmp_path / "c3")
    monkeypatch.setattr(bc, "CACHE_FOLDER", str(store.cache_root))
    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", ("ohlcv-v1",))
    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFESTS", {"ohlcv-v1": digest})
    strat = _built("O_IC")
    cfg = _cfg(strat, options_store="thetadata")
    recorded = mod._apply_market_conditions("optimize", cfg, strat, "O_IC")
    assert OPTION_SOURCE_SETTING not in cfg["experts"][0]["settings"]
    assert "option_source" not in recorded["facts"]["ohlcv-v1"]


def test_the_backtest_seam_refuses_a_setting_that_contradicts_the_snapshot(iv_snapshot):
    from types import SimpleNamespace

    from app.services.backtest.seam_wiring import _check_option_source_setting

    reader = SimpleNamespace(mapped_reader=SimpleNamespace(manifest={"option_source": "thetadata"}))
    ok = {"experts": [{"settings": {"market_condition_option_source": "thetadata"}}]}
    _check_option_source_setting(ok, IV_PROFILE, reader)
    _check_option_source_setting({"experts": [{"settings": {}}]}, IV_PROFILE, reader)        # absent: fine
    with pytest.raises(ValueError, match="alphavantage"):
        _check_option_source_setting({"experts": [{"settings": {"market_condition_option_source": "alphavantage"}}]},
                                     IV_PROFILE, reader)
    # not a chain manifest: never consulted
    _check_option_source_setting(ok, "ohlcv-v1", SimpleNamespace(mapped_reader=SimpleNamespace(manifest={})))


def test_a_manifest_with_the_old_source_spelling_is_still_accepted(iv_on, tmp_path, monkeypatch):
    """Review 2026-11-15 item 9: a manifest built before the rename records
    ``thetadata-eod-parquet-v1``; it is read as ``thetadata`` (never rewritten, never refused)."""
    import ba2_common.config as bc

    store, digest = _publish_iv_manifest(tmp_path / "old", option_source="thetadata-eod-parquet-v1")
    monkeypatch.setattr(bc, "CACHE_FOLDER", str(store.cache_root))
    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFESTS", {IV_PROFILE: digest})
    strat = _built("O_IC")
    cfg = _cfg(strat, options_store="thetadata")
    recorded = mod._apply_market_conditions("optimize", cfg, strat, "O_IC")
    assert recorded["facts"][IV_PROFILE]["option_source"] == "thetadata"
    assert cfg["experts"][0]["settings"]["market_condition_option_source"] == "thetadata"

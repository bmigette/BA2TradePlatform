"""The ``iv-v1`` gates through the REAL engine (docs/plans/2026-11-15-option-iv-conditions.md).

Same frozen fixture and arms as ``test_market_condition_all_off_matches_baseline`` (the option
golden's O_LEAP chain, the launcher's own emitted rules), with an iv-v1 snapshot pinned:

  none          no profile -- the baseline
  all-off       profile iv-v1 + its manifest, every mode off (the leaf is REMOVED by the decode)
  trial-pass    the iv/rv gate decoded from a GENOME through ``_build_daily_trial_config`` (the
                whitelist path), on a measured value that is true on every session
  trial-fail    the same genome with a threshold that fails on every session
  unknown       a passing gate over a snapshot whose rows are UNKNOWN (insufficient_history):
                the gate refuses every entry, and the counters say why
  combined      ohlcv-v1 + iv-v1 together (one manifest each, one composite reader): a passing
                ADX gate and a failing term-slope gate -- the AND refuses

none == all-off == trial-pass byte for byte; trial-fail / unknown / combined trade nothing and
their counters name the reason. The first equalities are the no-impact gate, the rest prove the
genome reaches the decision path and changes entries.
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ba2_common.core import TradeConditions  # noqa: E402
from ba2_common.core.market_calendar import regular_sessions_ending_at  # noqa: E402
from ba2_common.core.market_conditions import (  # noqa: E402
    IV_PROFILE, PROFILES, STATUS_INSUFFICIENT_HISTORY, STATUS_VALID,
)

from app.services.backtest import seam_wiring  # noqa: E402
from tests.backtest.test_option_golden_run import (  # noqa: E402
    _ACCOUNT_ID, _DTE_FLOOR, _END, _START, _SYMBOL,
    _bar_rows, _chain_rows, _underlying_rows, fingerprint,
)

#: iv/rv 1.5 and term slope 0.95 on every session: ``ivrv > 1.2`` passes, ``ivrv > 1.8`` fails,
#: ``ivterm > 1.0`` (an inverted term structure) fails.
_IV_VALUES = {"underlying_iv_rv_ratio_30_20": 1.5, "underlying_iv_term_slope_30_60": 0.95,
              "underlying_atm_iv_30": 0.30, "underlying_atm_iv_60": 0.316, "underlying_realized_vol_20": 0.20}
_OHLCV_VALUES = {"underlying_trend_slope_50_atr14": 0.05, "underlying_adx_14": 10.0,
                 "underlying_realized_vol_ratio_5_20": 0.9}


def _publish(root, profile_name, symbol, values, status=STATUS_VALID):
    from ba2_common.core.market_condition_store import MarketConditionStore, month_of

    profile = PROFILES[profile_name]
    fields = [f.name for f in profile.fields]
    sessions = list(regular_sessions_ending_at(_END.date() + timedelta(days=5), 260))
    store = MarketConditionStore(root)
    by_month: dict = {}
    for session in sessions:
        by_month.setdefault(month_of(session), []).append(
            {"session": session,
             "values": [values[f] if status == STATUS_VALID else None for f in fields],
             "status": [status] * len(fields),
             "reasons": ["" if status == STATUS_VALID else "no expiry in 7..30 days"] * len(fields),
             "window_digest": "sha256:" + "0" * 64, "raw_shard_ref": "", "raw_row_lo": 0, "raw_row_hi": 0})
    objects = [store.write_feature_object(profile, symbol, rows)[0] for _, rows in sorted(by_month.items())]
    manifest = store.make_manifest(
        profile, source_profile="fmp-daily-split-adjusted-v1", timing_policy="prior_session_v1",
        objects=objects, raw_objects=[], coverage={symbol: {"rows": len(sessions)}}, universe=[symbol],
        sessions=sessions, window_start=sessions[0], window_end=sessions[-1],
        option_source="thetadata" if profile_name == IV_PROFILE else None)
    return store.write_manifest(manifest)


@pytest.fixture(autouse=True)
def _restore_seam():
    saved = TradeConditions.get_market_condition_context_resolver()
    yield
    seam_wiring.clear_backtest_market_conditions()
    TradeConditions.set_market_condition_context_resolver(saved)


@pytest.fixture(scope="module")
def snapshots(tmp_path_factory):
    import ba2_common.config as bc

    root = tmp_path_factory.mktemp("mc-iv-cache")
    digests = {
        "iv": _publish(root, IV_PROFILE, _SYMBOL, _IV_VALUES),
        "ohlcv": _publish(root, "ohlcv-v1", _SYMBOL, _OHLCV_VALUES),
    }
    # an iv snapshot over the same sessions whose every row is UNKNOWN (published to another root:
    # same profile, different content -> different digest)
    unknown_root = tmp_path_factory.mktemp("mc-iv-unknown")
    digests["iv_unknown"] = _publish(unknown_root, IV_PROFILE, _SYMBOL, _IV_VALUES,
                                     status=STATUS_INSUFFICIENT_HISTORY)
    saved = bc.CACHE_FOLDER
    bc.CACHE_FOLDER = str(root)
    yield {"root": str(root), "unknown_root": str(unknown_root), **digests}
    bc.CACHE_FOLDER = saved


def _trial_leaf(genome):
    """The iv/rv leaf as a GA trial sees it: launcher-built O_IC (profile iv-v1), the genome
    decoded by the optimizer's own ``decode_params`` and passed through
    ``_build_daily_trial_config`` -- the whitelist path every trial takes."""
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    from app.services.strategy_param_space import decode_params
    from tests.backtest.test_grid2_engine_paths import _launcher

    launcher = _launcher()
    saved = getattr(launcher, "_MARKET_CONDITION_PROFILES", ())
    launcher._MARKET_CONDITION_PROFILES = (IV_PROFILE,)
    try:
        strat = launcher._build_strategy("O_IC", "iv-engine-O_IC", "FMPRating")
    finally:
        launcher._MARKET_CONDITION_PROFILES = saved
    cfg = {"backtest_id": "iv", "start_date": "2024-03-01", "end_date": "2024-07-26",
           "enabled_instruments": [_SYMBOL], "experts": [{"class": "FMPRating", "settings": {}}],
           "initial_capital": 20_000.0, "account_settings": {}, "warmup_days": 0, "seed": 1}
    trial = _build_daily_trial_config(cfg, decode_params(strat, genome), None, option_trade_records=False)

    def find(node):
        if isinstance(node, list):
            for x in node:
                hit = find(x)
                if hit is not None:
                    return hit
        elif isinstance(node, dict):
            if node.get("id") == "o_ic-market-ivrv":
                return node
            for v in node.values():
                hit = find(v)
                if hit is not None:
                    return hit
        return None

    leaf = find(trial["entry_rules"])
    assert leaf is not None, "the iv/rv gene did not reach the trial's entry rules"
    return dict(leaf)


def _gate(launcher, profile, short, mode, threshold):
    from app.services.strategy_param_space import _apply_mode

    saved = getattr(launcher, "_MARKET_CONDITION_PROFILES", ())
    launcher._MARKET_CONDITION_PROFILES = (profile,)
    try:
        leaves = launcher._market_condition_gates("o_leap")
    finally:
        launcher._MARKET_CONDITION_PROFILES = saved
    leaf = next(dict(x) for x in leaves if x["id"].endswith(f"-market-{short}"))
    leaf["value"] = float(threshold)
    _apply_mode(leaf, leaf["id"], mode)
    return leaf


def _run(*, profiles, manifests, gates=(), cache_root=None):
    from app.services.backtest.market_condition_bt import MarketConditionRunRecord
    from tests.backtest.test_grid2_engine_paths import _PlainBuyExpert, _harness, _launcher, _leap_rules
    import ba2_common.config as bc

    launcher = _launcher()
    entry_rules, exit_rules = _leap_rules(launcher, dte_floor=_DTE_FLOOR)
    for gate in gates:
        leaf = gate(launcher) if callable(gate) else gate
        entry_rules[0]["conditions"]["conditions"].append(leaf)
    engine, account, ctx = _harness(
        symbol=_SYMBOL, underlying_rows=_underlying_rows(), chain_rows=_chain_rows(),
        bar_rows=_bar_rows(), entry_rules=entry_rules, exit_rules=exit_rules,
        entry_action=entry_rules[0]["actions"][0],
        expert_factory=lambda eid: _PlainBuyExpert(eid, _SYMBOL),
        start=_START, end=_END, account_id=_ACCOUNT_ID)
    saved_root = bc.CACHE_FOLDER
    if cache_root is not None:
        bc.CACHE_FOLDER = cache_root
    try:
        config = {"market_condition_profiles": list(profiles), "market_condition_manifests": dict(manifests),
                  "enabled_instruments": [_SYMBOL]}
        resolver = seam_wiring.install_backtest_market_conditions(config, engine.price)
        if resolver is not None:
            engine._mc = MarketConditionRunRecord(resolver, profiles=list(profiles), manifests=dict(manifests))
        engine.run()
        block = engine._mc.as_dict() if engine._mc is not None else None
        return fingerprint(account), block
    finally:
        bc.CACHE_FOLDER = saved_root
        seam_wiring.clear_backtest_market_conditions()
        ctx.__exit__(None, None, None)


@pytest.fixture(scope="module")
def arms(snapshots):
    iv = {IV_PROFILE: snapshots["iv"]}
    pass_leaf = _trial_leaf({"cond:o_ic-market-ivrv:mode": "above", "cond:o_ic-market-ivrv:value": 1.2,
                             "cond:o_ic-market-ivterm:mode": "off", "cond:o_ic-market-ivterm:value": 1.0})
    fail_leaf = _trial_leaf({"cond:o_ic-market-ivrv:mode": "above", "cond:o_ic-market-ivrv:value": 1.8,
                             "cond:o_ic-market-ivterm:mode": "off", "cond:o_ic-market-ivterm:value": 1.0})
    return {
        "none": _run(profiles=[], manifests={}),
        "all_off": _run(profiles=[IV_PROFILE], manifests=iv),
        "trial_pass": _run(profiles=[IV_PROFILE], manifests=iv, gates=[pass_leaf]),
        "trial_fail": _run(profiles=[IV_PROFILE], manifests=iv, gates=[fail_leaf]),
        "unknown": _run(profiles=[IV_PROFILE], manifests={IV_PROFILE: snapshots["iv_unknown"]},
                        gates=[pass_leaf], cache_root=snapshots["unknown_root"]),
        "combined": _run(profiles=["ohlcv-v1", IV_PROFILE],
                         manifests={"ohlcv-v1": snapshots["ohlcv"], IV_PROFILE: snapshots["iv"]},
                         gates=[lambda la: _gate(la, "ohlcv-v1", "adx", "below", 25.0),
                                lambda la: _gate(la, IV_PROFILE, "ivterm", "above", 1.0)]),
    }


def test_the_baseline_trades(arms):
    fp = arms["none"][0]
    assert fp["n_trades"] >= 1 and fp["equity_first"] != fp["equity_last"], fp


def test_all_modes_off_reproduces_the_baseline_byte_for_byte(arms):
    assert arms["all_off"][0]["sha256"] == arms["none"][0]["sha256"]
    assert arms["all_off"][0]["trades"] == arms["none"][0]["trades"]
    block = arms["all_off"][1]
    assert block["profiles"] == [IV_PROFILE]
    assert block["calc_versions"] == {IV_PROFILE: "iv-v1/calc-1"}
    assert block["stats"]["market_leaf_evaluations"] == 0


def test_a_passing_genome_gate_reproduces_the_baseline(arms):
    fp, block = arms["trial_pass"]
    assert fp["sha256"] == arms["none"][0]["sha256"]
    assert block["stats"]["market_gate_passed"] > 0 and block["stats"]["market_gate_rejected"] == 0


def test_a_failing_genome_gate_stops_every_entry(arms):
    fp, block = arms["trial_fail"]
    assert fp["n_trades"] == 0
    assert block["stats"]["market_gate_rejected"] > 0 and block["stats"]["market_gate_passed"] == 0


def test_an_unknown_value_refuses_the_entry_countably_never_passes(arms):
    fp, block = arms["unknown"]
    assert fp["n_trades"] == 0
    stats = block["stats"]
    assert stats["market_gate_passed"] == 0 and stats["market_gate_rejected"] == 0
    assert stats["market_unknown_recommendations"] > 0
    by_reason = stats["market_unknown_input_by_reason"]
    # every evaluated leaf was unknown; the rows' own status dominates. (The fixture's trading
    # clock has a few non-session dates, which every profile reports as no_context.)
    assert sum(by_reason.values()) == stats["market_leaf_evaluations"], by_reason
    assert set(by_reason) <= {STATUS_INSUFFICIENT_HISTORY, "no_context"}, by_reason
    assert by_reason[STATUS_INSUFFICIENT_HISTORY] > 10 * by_reason.get("no_context", 0), by_reason


def test_the_profile_combines_with_ohlcv_through_one_composite_reader(arms):
    fp, block = arms["combined"]
    assert fp["n_trades"] == 0
    assert block["profiles"] == ["ohlcv-v1", IV_PROFILE]
    assert set(block["manifests"]) == {"ohlcv-v1", IV_PROFILE}
    assert block["stats"]["market_gate_rejected"] > 0


def test_an_iv_profile_without_a_manifest_is_refused_not_computed(snapshots):
    """Research mode (no digest) computes OHLCV profiles on a miss; a chain profile has nothing
    to compute from, so the reader REFUSES at install -- never a run of missing_session gates."""
    from ba2_common.core.market_condition_readers import ChainProfileNeedsManifest

    class _PS:
        is_intraday = False

    with pytest.raises(ChainProfileNeedsManifest):
        seam_wiring.install_backtest_market_conditions(
            {"market_condition_profiles": [IV_PROFILE], "enabled_instruments": [_SYMBOL]}, _PS())

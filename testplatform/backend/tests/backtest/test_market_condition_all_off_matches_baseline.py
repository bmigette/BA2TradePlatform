"""Design section 8.8: "In the new profile, all modes off reproduces the corresponding frozen
baseline's orders, trades and equity curve; additional research metadata is compared separately."

THE COMPATIBILITY CLAIM THIS PINS. Turning the market-condition profile ON must not move a
single number of a run whose gates are all off. That is not self-evident: installing the
profile builds a reader, installs a process-global resolver into the ``TradeConditions`` seam,
and hangs a telemetry recorder off the engine's staging path -- three new things touching a
run whose behaviour is supposed to be unchanged. If any of them perturbed the order of a
dict, the RNG draw sequence, or the evaluator's short-circuit, the gates would look free while
quietly changing every backtest in the grid.

FOUR ARMS over ONE frozen fixture (the option golden's O_LEAP chain, reused so the arms run
the launcher's own emitted rules rather than a hand-written pair):

  none        no profile at all -- the baseline, byte-for-byte what the grid runs today
  all-off     profile ``ohlcv-v1`` + a pinned manifest, every mode off. An ``off`` leaf is
              REMOVED by the decode, so the tree is the baseline's tree (pinned by
              ``test_launcher_market_condition_profile``); what this arm varies is the
              INSTALLATION, which is exactly the thing that must cost nothing.
  always-pass a real gate on a measured value that is true on every session
  always-fail the same gate inverted

none == all-off == always-pass, byte for byte; always-fail trades NOTHING and its counters say
why. The first two equalities are the compatibility gate; the third proves the fixture can
actually be gated, so the first two are not passing because nothing was wired up at all.
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ba2_common.core import TradeConditions  # noqa: E402
from ba2_common.core.market_calendar import regular_sessions_ending_at  # noqa: E402
from ba2_common.core.market_conditions import PROFILES, STATUS_VALID  # noqa: E402

from app.services.backtest import seam_wiring  # noqa: E402
from tests.backtest.test_option_golden_run import (  # noqa: E402
    _ACCOUNT_ID, _DTE_FLOOR, _END, _START, _SYMBOL,
    _bar_rows, _chain_rows, _underlying_rows, fingerprint,
)

#: The measured state every session of the fixture window carries. ADX 10 is the lever: a
#: ``< 25`` gate passes on every session and a ``> 25`` gate fails on every session, so the
#: two gated arms differ ONLY in the comparison -- not in the data, the window or the tree.
_VALUES = {"underlying_trend_slope_50_atr14": 0.05,
           "underlying_adx_14": 10.0,
           "underlying_realized_vol_ratio_5_20": 0.9}


def _publish(root, symbol: str):
    """A published snapshot with VALID rows for every regular session around the fixture window.

    The values are written directly rather than computed: what this test pins is the engine's
    behaviour with a profile installed, and a fixture that also had to produce 128 real bars
    per session would pin the calculators a second time (``test_market_conditions_accuracy``
    already does that) while making the gate's own outcome hard to choose.
    """
    from ba2_common.core.market_condition_store import MarketConditionStore, month_of

    profile = PROFILES["ohlcv-v1"]
    fields = [f.name for f in profile.fields]
    # From well before the run's start (the gate reads the PRIOR session) to well after its end.
    sessions = [s for s in regular_sessions_ending_at(_END.date() + timedelta(days=5), 260)]
    store = MarketConditionStore(root)
    by_month: dict = {}
    for session in sessions:
        by_month.setdefault(month_of(session), []).append(
            {"session": session,
             "values": [_VALUES[f] for f in fields],
             "status": [STATUS_VALID] * len(fields),
             "reasons": [""] * len(fields),
             "window_digest": "sha256:" + "0" * 64, "raw_shard_ref": "",
             "raw_row_lo": 0, "raw_row_hi": 0})
    # One feature object per (symbol, calendar month) -- the store's own sharding rule.
    objects = [store.write_feature_object(profile, symbol, rows)[0]
               for _, rows in sorted(by_month.items())]
    manifest = store.make_manifest(
        profile, source_profile="fmp-daily-split-adjusted-v1",
        timing_policy="prior_session_v1", objects=objects, raw_objects=[],
        coverage={symbol: {"rows": len(sessions)}}, universe=[symbol], sessions=sessions,
        window_start=sessions[0], window_end=sessions[-1])
    return store, store.write_manifest(manifest)


@pytest.fixture(autouse=True)
def _restore_seam():
    """The market-condition resolver is PROCESS-global; a leak turns unrelated tests into
    tests of this one."""
    saved = TradeConditions.get_market_condition_context_resolver()
    yield
    seam_wiring.clear_backtest_market_conditions()
    TradeConditions.set_market_condition_context_resolver(saved)


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory):
    import ba2_common.config as bc

    store, digest = _publish(tmp_path_factory.mktemp("mc-cache"), _SYMBOL)
    saved = bc.CACHE_FOLDER
    bc.CACHE_FOLDER = str(store.cache_root)
    yield digest
    bc.CACHE_FOLDER = saved


def _gate(launcher, short: str, mode: str, threshold: float):
    """The launcher's OWN market leaf for ``short``, DECODED to one concrete mode.

    Built through ``_market_condition_gates`` and decoded through ``_apply_mode`` -- the two
    functions the grid itself uses -- so this test cannot pass on a leaf shape the launcher
    does not emit or a decode the optimizer does not perform.
    """
    from app.services.strategy_param_space import _apply_mode

    launcher._MARKET_CONDITION_PROFILES = ("ohlcv-v1",)
    try:
        leaves = launcher._market_condition_gates("o_leap")
    finally:
        launcher._MARKET_CONDITION_PROFILES = ()
    leaf = next(dict(x) for x in leaves if x["id"].endswith(f"-market-{short}"))
    leaf["value"] = float(threshold)
    _apply_mode(leaf, leaf["id"], mode)
    return leaf


def _run(*, profile, digest, gate=None):
    """One arm. Returns ``(fingerprint, market_condition_block_or_None)``."""
    from app.services.backtest.market_condition_bt import MarketConditionRunRecord
    from tests.backtest.test_grid2_engine_paths import (
        _PlainBuyExpert, _harness, _launcher, _leap_rules)

    launcher = _launcher()
    entry_rules, exit_rules = _leap_rules(launcher, dte_floor=_DTE_FLOOR)
    if gate is not None:
        entry_rules[0]["conditions"]["conditions"].append(_gate(launcher, *gate))
    engine, account, ctx = _harness(
        symbol=_SYMBOL, underlying_rows=_underlying_rows(), chain_rows=_chain_rows(),
        bar_rows=_bar_rows(), entry_rules=entry_rules, exit_rules=exit_rules,
        entry_action=entry_rules[0]["actions"][0],
        expert_factory=lambda eid: _PlainBuyExpert(eid, _SYMBOL),
        start=_START, end=_END, account_id=_ACCOUNT_ID)
    try:
        config = {"market_condition_profile": profile, "enabled_instruments": [_SYMBOL]}
        if digest:
            config["market_condition_manifest"] = digest
        resolver = seam_wiring.install_backtest_market_conditions(config, engine.price)
        if resolver is not None:
            # What ``run_daily_backtest`` does between installing the seam and running the
            # engine; done here because ``_harness`` predates the profile and builds its own
            # config (extending it would move the two goldens that depend on that fixture).
            engine._mc = MarketConditionRunRecord(resolver, profile=profile,
                                                  manifest_digest=digest)
        engine.run()
        block = engine._mc.as_dict() if engine._mc is not None else None
        if block is not None:
            block["entry_states"] = engine._mc.entry_states()
        return fingerprint(account), block
    finally:
        seam_wiring.clear_backtest_market_conditions()
        ctx.__exit__(None, None, None)


@pytest.fixture(scope="module")
def arms(snapshot):
    """Every arm, run ONCE (each is a full engine run over the fixture)."""
    return {
        "none": _run(profile="none", digest=None),
        "all_off": _run(profile="ohlcv-v1", digest=snapshot),
        "always_pass": _run(profile="ohlcv-v1", digest=snapshot, gate=("adx", "below", 25.0)),
        "always_fail": _run(profile="ohlcv-v1", digest=snapshot, gate=("adx", "above", 25.0)),
    }


def test_the_baseline_arm_actually_trades(arms):
    """ANTI-VACUITY. Two runs that both did nothing are byte-identical for free."""
    fp = arms["none"][0]
    assert fp["n_trades"] >= 1, fp
    assert any(t["pnl"] != 0.0 for t in fp["trades"]), fp["trades"]
    assert fp["equity_first"] != fp["equity_last"], fp


def test_all_modes_off_reproduces_the_baseline_byte_for_byte(arms):
    baseline, _ = arms["none"]
    all_off, _ = arms["all_off"]
    assert all_off["sha256"] == baseline["sha256"]
    assert all_off["trades"] == baseline["trades"]
    assert all_off["equity_curve"] == baseline["equity_curve"]


def test_a_gate_that_passes_on_every_session_also_reproduces_the_baseline(arms):
    """A real, evaluated gate whose answer is always yes changes nothing either -- which is
    what separates "the gates are inert" from "the gates were never wired in"."""
    baseline, _ = arms["none"]
    passing, block = arms["always_pass"]
    assert passing["sha256"] == baseline["sha256"]
    assert block["stats"]["market_gate_passed"] > 0
    assert block["stats"]["market_gate_rejected"] == 0


def test_a_gate_that_fails_on_every_session_stops_every_entry(arms):
    """The fixture CAN be gated, so the equalities above are claims about the gates rather
    than about a wiring that never reached the decision path."""
    failing, block = arms["always_fail"]
    assert failing["n_trades"] == 0
    assert block["stats"]["market_gate_rejected"] > 0
    assert block["stats"]["market_gate_passed"] == 0
    assert block["stats"]["entries_staged"] == 0


def test_the_research_metadata_is_the_only_difference(arms):
    """Design 8.8: "additional research metadata is compared separately"."""
    assert arms["none"][1] is None
    block = arms["all_off"][1]
    assert block["profile"] == "ohlcv-v1"
    assert block["manifest"]
    assert block["calc_version"] == PROFILES["ohlcv-v1"].calc_version
    assert block["timing_policy"] == "prior_session_v1"
    stats = block["stats"]
    # Eligible is counted even with every gate off -- it is the denominator the gate
    # rejections are a fraction OF, not a count of gate evaluations.
    assert stats["eligible_recommendations"] > 0
    assert stats["market_evaluated"] == 0
    assert stats["market_leaf_evaluations"] == 0
    assert stats["market_unknown_input_by_reason"] == {}
    assert stats["entries_staged"] >= 1


def test_the_entry_state_is_recorded_even_with_every_gate_off(arms):
    """Attribution must not require the gate to be ON: a run's entry states are what makes
    "which regime did this genome actually trade in" answerable for the control arm too."""
    states = arms["all_off"][1]["entry_states"]
    assert states, "an entry fired but no entry state was recorded"
    first = states[0]
    assert first["symbol"] == _SYMBOL
    assert date.fromisoformat(first["prior_session"]) < date.fromisoformat(first["session"])
    values = first["values"]
    assert set(values) == set(_VALUES)
    for name, expected in _VALUES.items():
        assert values[name]["status"] == STATUS_VALID
        assert values[name]["value"] == pytest.approx(expected)


def test_the_recorded_states_attach_to_the_executed_trades(arms):
    from app.services.backtest.market_condition_bt import attach_entry_states

    trades = [dict(t) for t in arms["all_off"][0]["trades"]]
    attached = attach_entry_states(trades, arms["all_off"][1]["entry_states"])
    assert attached == len(trades)
    for trade in trades:
        assert trade["entry_state"]["values"]["underlying_adx_14"]["value"] == pytest.approx(10.0)

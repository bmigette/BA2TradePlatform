"""B7 live half: the market exit fires in the REAL live open-positions pass.

The backtest half is ``testplatform/backend/tests/backtest/test_market_exit_parity_engine.py``:
the same template rule, the same kind of pinned manifest (structure bull, then bear from
2024-01-08), closes the position on bar 2024-01-08 -- the bar that reads session 2024-01-08, i.e.
the live decision labelled 2024-01-09 (``backtest_decision_label``). That file also pins that
the backtest reader and a live resolver over one manifest return the identical row for the same
``(symbol, prior_session)``.

Here the LIVE path runs for real:

* the rule is the template (``market_exit_rules``) decoded ON, converted by
  ``trade_rules_to_live_export`` and imported with ``RulesImporter.import_multiple_rulesets``
  -- exactly what ``tools/import_deploy_payload.py`` does;
* the resolver is the one ``wire_all_seams`` installs, ``PerInstanceMarketConditionResolver``,
  reading the expert's ``market_condition_profile`` setting and a manifest pinned through
  ``BA2_MARKET_CONDITION_MANIFEST``;
* ``TradeManager.process_open_positions_recommendations`` runs with the REAL
  ``TradeActionEvaluator``, ``TradeConditions`` and ``CloseAction`` against the in-memory DB.
  Only the edges are faked: the account class (it records ``close_transaction``), the expert
  object (its ``allow_automated_trade_modification``), the instance-resolver seam the profile
  setting is read through, and the decision clock.

A decision at 10:00 New York on 2024-01-09 reads prior session 2024-01-08 (bear) and closes the
open transaction; the same pass one session earlier reads 2024-01-05 (bull) and does not.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import ba2_common.core.TradeConditions as TC
from ba2_common.core import market_condition_live as live
from ba2_common.core.market_calendar import (
    decision_data_session, live_decision_label, next_regular_session, regular_sessions_ending_at,
)
from ba2_common.core.market_condition_store import MarketConditionStore
from ba2_common.core.market_condition_templates import market_exit_rules
from ba2_common.core.market_conditions import (
    FIELD_STRUCTURE_STATE, PROFILES, STATUS_VALID, STRUCTURE_STATE_CODES,
)
from ba2_common.core.rules_convert import trade_rules_to_live_export
from ba2_common.core.rules_export_import import RulesImporter

from ba2_trade_platform.core.types import (
    AnalysisUseCase, OrderDirection, OrderRecommendation, OrderStatus, TransactionStatus,
)
from tests.factories import (
    create_account_definition, create_expert_instance, create_recommendation,
    create_trading_order, create_transaction,
)
# The instance-resolver stub the per-instance resolver reads the profile setting through.
from tests.test_market_condition_live_context import _write_certifiable, instances  # noqa: F401

SYMBOL = "AAPL"
STRUCT = "ta-structure-v1"
BULL, BEAR = float(STRUCTURE_STATE_CODES["bull"]), float(STRUCTURE_STATE_CODES["bear"])
FLIP = date(2024, 1, 8)            # first bear session (the backtest test's DAYS[4])
BEFORE = date(2024, 1, 5)          # the session before it (bull)
LAST = date(2024, 1, 16)
NY = ZoneInfo("America/New_York")
RULE_ID = "b7-mkt-exit-structure"


def publish(root, profile, values, symbols=(SYMBOL,), last=LAST, n=40):
    """The backtest test's ``publish``: ``profile``'s snapshot over the ``n`` sessions ending at
    ``last``, every field 0.5 unless ``values(session)`` says otherwise, every row VALID."""
    store = MarketConditionStore(root)
    spec = PROFILES[profile]
    sessions = regular_sessions_ending_at(last, n)
    rows = []
    for day in sessions:
        chosen = values(day)
        rows.append({"session": day,
                     "values": [float(chosen.get(f.name, 0.5)) for f in spec.fields],
                     "status": [STATUS_VALID] * len(spec.fields),
                     "reasons": ["fixture"] * len(spec.fields),
                     "window_digest": "sha256:" + "0" * 64, "raw_shard_ref": "",
                     "raw_row_lo": 0, "raw_row_hi": 0})
    objects = []
    for symbol in symbols:
        for month in sorted({r["session"].strftime("%Y-%m") for r in rows}):
            obj, _ = store.write_feature_object(
                spec, symbol, [r for r in rows if r["session"].strftime("%Y-%m") == month])
            objects.append(obj)
    manifest = store.make_manifest(
        spec, source_profile="fmp-daily-split-adjusted-v1", timing_policy="prior_session_v1",
        objects=objects, raw_objects=[], coverage={s: {"rows": len(rows)} for s in symbols},
        universe=symbols, sessions=sessions, window_start=min(sessions), window_end=max(sessions),
        created_at="2026-09-24T00:00:00+00:00")
    return store.write_manifest(manifest)


def decoded_rule_on():
    """The structure-close template with its toggle gene decoded ON (``_decode_rule_list``
    keeps a toggled rule only when the gene is exactly 1, and drops ``enabled``)."""
    (rule,) = market_exit_rules("b7", [STRUCT], "long", ("exit",))
    assert rule["id"] == RULE_ID and rule["enabled"] is False
    on = {k: v for k, v in rule.items() if k not in ("enabled", "toggle_optimize")}
    return on


class _Account:
    """The account class the pass instantiates: records ``close_transaction``."""

    closed: list = []

    def __init__(self, account_id):
        self.id = account_id

    def has_pending_closing_order(self, transaction_id):
        return False

    def close_transaction(self, transaction_id):
        type(self).closed.append(transaction_id)
        return {"success": True, "message": "closed", "close_order_id": None}


class _Expert:
    def get_setting_with_interface_default(self, key, log_warning=True):
        return {"allow_automated_trade_modification": True}[key]


@pytest.fixture
def world(tmp_path, monkeypatch, instances):  # noqa: F811 -- the imported fixture
    import ba2_trade_platform.core.utils as core_utils
    import ba2_trade_platform.modules.accounts as accounts_mod

    root = str(tmp_path)
    _write_certifiable(root)                       # the split-certification caches
    digest = publish(root, STRUCT, lambda day: {FIELD_STRUCTURE_STATE: BEAR if day >= FLIP else BULL})

    export = trade_rules_to_live_export(exit_rules=[decoded_rule_on()], name="b7-deploy")
    ruleset_ids, _warnings = RulesImporter.import_multiple_rulesets(export)
    (ruleset_id,) = ruleset_ids

    account_def = create_account_definition()
    expert_instance = create_expert_instance(account_id=account_def.id,
                                             open_positions_ruleset_id=ruleset_id)
    instances[expert_instance.id] = STRUCT         # the market_condition_profile setting
    create_recommendation(instance_id=expert_instance.id, symbol=SYMBOL,
                          recommended_action=OrderRecommendation.HOLD,
                          subtype=AnalysisUseCase.OPEN_POSITIONS)
    txn = create_transaction(symbol=SYMBOL, status=TransactionStatus.OPENED, open_price=100.0,
                             expert_id=expert_instance.id)
    create_trading_order(account_id=account_def.id, symbol=SYMBOL, side=OrderDirection.BUY,
                         status=OrderStatus.FILLED, transaction_id=txn.id, open_price=100.0,
                         filled_qty=10.0)

    _Account.closed = []
    monkeypatch.setattr(accounts_mod, "get_account_class", lambda provider: _Account)
    monkeypatch.setattr(core_utils, "get_expert_instance_from_id",
                        lambda expert_id, use_cache=True: _Expert())

    saved = TC.get_market_condition_context_resolver()
    live.clear_certification_cache()
    dispatcher = live.PerInstanceMarketConditionResolver(
        cache_root=root, environ={live.MANIFEST_ENV: f"{STRUCT}={digest}"})
    TC.set_market_condition_context_resolver(dispatcher)
    try:
        yield SimpleNamespace(expert_id=expert_instance.id, transaction_id=txn.id,
                              dispatcher=dispatcher, digest=digest, export=export)
    finally:
        TC.set_market_condition_context_resolver(saved)
        live.clear_certification_cache()


def _decide_during(monkeypatch, session):
    """Pin the live clock to 10:00 New York during ``session``."""
    moment = datetime.combine(session, time(10, 0), tzinfo=NY).astimezone(timezone.utc)
    monkeypatch.setattr(live, "_replay_now", lambda: moment)
    return moment


def _evaluations():
    """``(action_type, the one condition evaluation)`` per pass, from the ``TradeActionResult``
    rows the pass stores (in id order)."""
    from sqlmodel import select

    from ba2_trade_platform.core.db import get_db
    from ba2_trade_platform.core.models import TradeActionResult

    out = []
    with get_db() as session:
        for row in session.exec(select(TradeActionResult).order_by(TradeActionResult.id)).all():
            if not row.action_type.startswith("evaluation"):
                continue
            (condition,) = row.data["evaluation_details"]["condition_evaluations"]
            assert condition["event_type"] == FIELD_STRUCTURE_STATE
            out.append((row.action_type, condition))
    return out


def _pass(expert_id):
    from ba2_trade_platform.core.TradeManager import TradeManager

    return TradeManager().process_open_positions_recommendations(expert_id)


def test_the_live_rule_is_the_exported_template(world):
    (ruleset,) = world.export["rulesets"]
    (rule,) = ruleset["rules"]
    assert rule["name"] == RULE_ID and rule["subtype"] == AnalysisUseCase.OPEN_POSITIONS.value
    assert list(rule["triggers"].values()) == [
        {"event_type": FIELD_STRUCTURE_STATE, "operator": "==", "value": BEAR}]
    assert [a["action_type"] for a in rule["actions"].values()] == ["close"]


def test_live_pass_closes_on_the_session_after_the_flip_and_not_before(world, monkeypatch):
    # The decision labelled FLIP reads the session before it, BEFORE: bull, no close.
    moment = _decide_during(monkeypatch, FLIP)
    assert decision_data_session(live_decision_label(moment)) == BEFORE
    _pass(world.expert_id)
    assert _Account.closed == [], "a bull prior session must not close the position"
    (first,) = _evaluations()
    assert first[0] == "evaluation_only"
    assert first[1]["market_condition_status"] == STATUS_VALID, first
    assert first[1]["calculated_value"] == BULL and first[1]["condition_result"] is False

    # The decision labelled N(FLIP) reads FLIP itself: bear -> the close fires.
    label = next_regular_session(FLIP)
    moment = _decide_during(monkeypatch, label)
    assert decision_data_session(live_decision_label(moment)) == FLIP
    results = _pass(world.expert_id)
    assert _Account.closed == [world.transaction_id]
    assert any(r.get("success") for r in results), results
    # The observation the live pass actually evaluated: the valid bear row of session FLIP.
    (_, second) = _evaluations()
    assert second[0] == "evaluation_with_actions"
    assert second[1]["market_condition_status"] == STATUS_VALID, second
    assert second[1]["calculated_value"] == BEAR and second[1]["condition_result"] is True

    # The row the live pass read for (SYMBOL, FLIP) is the bear row the backtest reads for bar
    # FLIP (the backtest test pins that both readers return the identical row).
    resolver = live.resolver_for_expert_instance(world.expert_id)
    assert resolver is not None and resolver.manifest_digest == world.digest
    row = resolver.reader.observe(SYMBOL, FLIP)
    assert row.values[FIELD_STRUCTURE_STATE].status == STATUS_VALID
    assert row.values[FIELD_STRUCTURE_STATE].value == BEAR
    assert resolver.reader.observe(SYMBOL, BEFORE).values[FIELD_STRUCTURE_STATE].value == BULL

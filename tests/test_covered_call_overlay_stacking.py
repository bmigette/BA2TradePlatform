"""Regression tests for bug B2 (options audit 2026-07-22), now FIXED: the O_CC/O_PP overlay rules were gated only on has_position and re-fired every manage cycle, stacking short calls. These tests pin the fix.

Bug summary (historical)
------------------------
The backtest launcher builds the covered-call / protective-put overlay strategies
(O_CC / O_PP) in ``testplatform/ba2test_launcher.py`` —
``_build_strategy_covered_call`` (line 2135) and ``_build_strategy_protective_put``
(line 2166). Each appended an OPEN_POSITIONS overlay rule whose ONLY condition was
``has_position`` and whose action is ``sell_covered_call`` (resp.
``buy_protective_put``), with NO guard against an already-existing overlay. The
actions (``SellCoveredCallAction`` / ``BuyProtectivePutAction`` in
``packages/common/ba2_common/core/TradeActions.py``) size off ``held_shares / 100``
and never check for an existing overlay; the evaluator's action dedup
(``TradeActionEvaluator.py``) is per-evaluation only. So every manage cycle sold
another full batch of covered calls against the same shares — runtime proof showed
2 FILLED sell_to_open covered calls against 100 shares after 2 cycles (1 naked).

The fix
-------
Each overlay rule is now preceded by a ``stop_processing`` GUARD rule — the
codebase's negation idiom (``test_files/setup_option_rulesets.py``;
``rules_documentation.py:193``: "require NOT has_covered_call before
sell_covered_call"). Rules evaluate in order: the guard fires when
``has_covered_call`` (resp. ``has_protective_put`` — the dedicated
``HasCoveredCallCondition`` / ``HasProtectivePutCondition``,
``packages/common/ba2_common/core/TradeConditions.py:2137`` / ``:2184``) is true
and halts the ruleset, so the overlay only fires while the position has NO
existing overlay. The option-position flags were also wired into the shared
``ba2_common.core.rule_builders.FLAG_FIELD_EVENT`` map — without that, a
condition-tree leaf naming ``has_covered_call`` was silently SKIPPED by
``triggers_from_condition_tree`` and the guard could never reach the engine.

The RUNTIME tests below seed a filled 100-share equity position, seed the overlay
rule PAIR exactly as the engine does (``triggers_from_condition_tree`` +
``live_actions_from_trade_rule`` per rule, one EventAction per rule in order —
the same conversion
``testplatform/backend/app/services/backtest/default_rulesets.py:seed_ruleset_from_rules``
uses), and run TWO consecutive manage cycles through the real packaged
``TradeActionEvaluator``. Correct behavior — asserted here: the first cycle opens
the overlay, the second cycle creates NO new overlay order.

The launcher is NOT imported (see ``test_overlay_rules_have_overlay_guard_static``
for why); the rule dicts are replicated verbatim with source citations, and the
static test re-checks the real launcher source via ``ast`` so a future regression
that removes the guard (or drifts the replica) is detected here.
"""
import ast
from pathlib import Path

import pytest
from sqlmodel import select

from ba2_trade_platform.core.TradeActionEvaluator import TradeActionEvaluator
from ba2_trade_platform.core.db import add_instance, get_db, get_instance, update_instance
from ba2_trade_platform.core.models import EventAction, Ruleset, TradingOrder, Transaction
# NOTE: rule_builders / rules_convert have no in-tree ba2_trade_platform shim —
# import them straight from the installed shared package (the source of truth).
from ba2_common.core.rule_builders import triggers_from_condition_tree
from ba2_common.core.rules_convert import live_actions_from_trade_rule
from ba2_trade_platform.core.types import (
    AnalysisUseCase,
    AssetClass,
    ExpertEventRuleType,
    ExpertEventType,
    OrderDirection,
    OrderRecommendation,
    OrderStatus,
    OrderType,
    TransactionStatus,
)
from tests.conftest import MockAccount
from tests.factories import (
    create_account_definition,
    create_expert_instance,
    create_recommendation,
    create_trading_order,
    create_transaction,
    link_rule_to_ruleset,
)

SYMBOL = "AAPL"          # MockAccount spot price: 150.0
HELD_SHARES = 100.0      # exactly one option contract lot

# ---------------------------------------------------------------------------
# Overlay rule dicts, replicated VERBATIM from the backtest launcher (the rules
# are NOT imported — see the static test below for the rationale). Each strategy
# appends a GUARD rule + the OVERLAY rule, in this order (order matters: the
# guard must evaluate first):
#   _O_CC_RULES  <- testplatform/ba2test_launcher.py:2147-2157
#                   (_build_strategy_covered_call, strategy "O_CC")
#   _O_PP_RULES  <- testplatform/ba2test_launcher.py:2178-2188
#                   (_build_strategy_protective_put, strategy "O_PP")
# If the launcher rules change, update these replicas (the static test scans the
# real launcher source and fails if the guard disappears or the ids drift,
# flagging the drift).
# ---------------------------------------------------------------------------
_O_CC_GUARD_RULE = {
    "id": "cc_guard",
    "conditions": {"type": "AND", "conditions": [
        {"id": "cc_guard_has_cc", "field": "has_covered_call"}]},
    "actions": [{"action_type": "stop_processing"}],
    "continue_processing": False,
}

_O_CC_OVERLAY_RULE = {
    "id": "cc_sell",
    "conditions": {"type": "AND", "conditions": [{"id": "cc_hold", "field": "has_position"}]},
    "actions": [{"action_type": "sell_covered_call",
                 "option_strike_method": "percent_otm", "option_strike_param": 5.0,
                 "option_dte_min": 25, "option_dte_max": 45}],
    "continue_processing": False,
}

_O_PP_GUARD_RULE = {
    "id": "pp_guard",
    "conditions": {"type": "AND", "conditions": [
        {"id": "pp_guard_has_pp", "field": "has_protective_put"}]},
    "actions": [{"action_type": "stop_processing"}],
    "continue_processing": False,
}

_O_PP_OVERLAY_RULE = {
    "id": "pp_buy",
    "conditions": {"type": "AND", "conditions": [{"id": "pp_hold", "field": "has_position"}]},
    "actions": [{"action_type": "buy_protective_put",
                 "option_strike_method": "percent_otm", "option_strike_param": 8.0,
                 "option_dte_min": 25, "option_dte_max": 45}],
    "continue_processing": False,
}

_O_CC_RULES = [_O_CC_GUARD_RULE, _O_CC_OVERLAY_RULE]
_O_PP_RULES = [_O_PP_GUARD_RULE, _O_PP_OVERLAY_RULE]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _seed_overlay_ruleset(rules: list, name: str) -> int:
    """Seed an OPEN_POSITIONS ruleset from the launcher's overlay rule pair (guard
    + overlay), mirroring the backtest engine path exactly:
    ``default_rulesets.seed_ruleset_from_rules`` (testplatform/backend/app/services/
    backtest/default_rulesets.py:93-139) converts EACH rule's condition tree via the
    shared ``triggers_from_condition_tree`` and its ``actions`` list via the shared
    ``live_actions_from_trade_rule`` (which routes each action through
    ``action_from_rule`` / the stop_processing passthrough), then links ONE
    EventAction per rule, in order.
    """
    ruleset = Ruleset(
        name=name,
        description="Replicated O_CC/O_PP overlay ruleset (bug B2 regression)",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        subtype=AnalysisUseCase.OPEN_POSITIONS,
    )
    ruleset_id = add_instance(ruleset)

    for order_index, rule in enumerate(rules):
        actions = live_actions_from_trade_rule(rule)
        assert actions, f"engine conversion produced no actions for rule {rule['id']}"
        triggers = triggers_from_condition_tree(rule["conditions"])

        event_action = EventAction(
            name=f"{name}-{rule['id']}",
            type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
            subtype=AnalysisUseCase.OPEN_POSITIONS,
            triggers=triggers,
            actions=actions,
            extra_parameters={},
            continue_processing=bool(rule.get("continue_processing") or False),
        )
        ea_id = add_instance(event_action)
        link_rule_to_ruleset(ruleset_id, ea_id, order_index=order_index)
    return ruleset_id


def _seed_equity_position():
    """Seed an expert with a filled 100-share AAPL long (one contract lot).

    Returns (account, recommendation, transaction). The Transaction is OPENED and
    its entry order FILLED — exactly what ``_OptionEntryAction._held_equity_shares``
    sums (TradeActions.py) and what ``HasPositionCondition`` checks.

    The same 100 shares are ALSO published on the account's position feed, because
    the platform's view and the broker's view are two different readings and the
    covered-call cover guard in ``submit_option_order`` (OPT-L1) deliberately uses
    the ACCOUNT-WIDE one: shares bought by another expert still cover the call, and
    an expert-scoped reading would report a covered call as naked. A double that
    seeded only the order rows made the broker hold nothing, so the write was — quite
    correctly — refused as uncovered.
    """
    acct_def = create_account_definition()
    account = MockAccount(acct_def.id)
    expert_instance = create_expert_instance(account_id=acct_def.id)
    recommendation = create_recommendation(
        instance_id=expert_instance.id,
        symbol=SYMBOL,
        recommended_action=OrderRecommendation.BUY,
    )
    txn = create_transaction(
        symbol=SYMBOL,
        quantity=HELD_SHARES,
        side=OrderDirection.BUY,
        status=TransactionStatus.OPENED,
        open_price=150.0,
        expert_id=expert_instance.id,
    )
    create_trading_order(
        account_id=acct_def.id,
        symbol=SYMBOL,
        quantity=HELD_SHARES,
        side=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        status=OrderStatus.FILLED,
        transaction_id=txn.id,
        filled_qty=HELD_SHARES,
        open_price=150.0,
    )
    account._positions = [{"symbol": SYMBOL, "qty": HELD_SHARES,
                           "asset_class": "us_equity"}]
    return account, recommendation, txn


def _run_manage_cycle(account, recommendation, open_transactions, ruleset_id):
    """One OPEN_POSITIONS manage cycle through the REAL packaged evaluator:
    evaluate the ruleset, then execute (submit_to_broker=True) — the same two-phase
    drive the live ``process_open_positions_recommendations`` and the backtest
    engine use. A FRESH evaluator per cycle (dedup state is per-evaluation, so a
    fresh instance is the faithful two-cycle simulation).
    """
    evaluator = TradeActionEvaluator(
        account=account,
        instrument_name=SYMBOL,
        existing_transactions=open_transactions,
    )
    summaries = evaluator.evaluate(SYMBOL, recommendation, ruleset_id)
    results = evaluator.execute(submit_to_broker=True)
    return evaluator, summaries, results


def _overlay_orders(option_strategy: str):
    """All filled/pending option orders of one overlay strategy (e.g. 'covered_call')."""
    with get_db() as session:
        return session.exec(
            select(TradingOrder).where(
                TradingOrder.asset_class == AssetClass.OPTION,
                TradingOrder.option_strategy == option_strategy,
            )
        ).all()


def _mark_overlay_transactions_opened(option_strategy: str):
    """Simulate the account status-sync the engine runs between manage cycles.

    MockAccount fills the submitted option ORDER but does not run the transaction
    sync that live and the backtest engine run every cycle
    (``ReadOnlyAccountInterface.refresh_transactions``: WAITING -> OPENED once the
    entry order is FILLED). The overlay guard conditions
    (``HasCoveredCallCondition`` / ``HasProtectivePutCondition``) — like every
    position condition — only count OPENED transactions, so the overlay the first
    cycle opened must be OPENED before the second cycle evaluates, exactly as the
    engine would have it.
    """
    for order in _overlay_orders(option_strategy):
        txn = get_instance(Transaction, order.transaction_id)
        if txn is not None and txn.status == TransactionStatus.WAITING:
            txn.status = TransactionStatus.OPENED
            update_instance(txn)


# ---------------------------------------------------------------------------
# Runtime test 1 — covered call (O_CC): the overlay must NOT re-fire
# ---------------------------------------------------------------------------
def test_second_manage_cycle_does_not_sell_another_covered_call():
    """Two consecutive manage cycles over a 100-share long with the O_CC overlay
    ruleset must produce AT MOST one covered-call order (100 shares cover exactly
    one short call).

    Regression test for fixed bug B2: the overlay rule pair now carries the
    ``cc_guard`` stop_processing rule (has_covered_call), so the second cycle is
    halted by the guard and sells no second contract. Before the fix this test
    FAILED with 2 short calls against coverage for 1 (1 naked).
    """
    account, recommendation, txn = _seed_equity_position()
    ruleset_id = _seed_overlay_ruleset(_O_CC_RULES, "O_CC-overlay")

    # --- Manage cycle 1: no covered call exists yet -> the overlay sells one. ---
    ev1, summaries1, results1 = _run_manage_cycle(account, recommendation, [txn], ruleset_id)
    assert len(ev1.trade_actions) == 1, (
        f"precondition: cycle 1 should generate exactly one sell_covered_call action, "
        f"got {len(ev1.trade_actions)} (summaries={summaries1})"
    )
    assert any(r.get("success") for r in results1), (
        f"precondition: cycle 1 covered-call order must fill, results={results1}"
    )
    orders_after_1 = _overlay_orders("covered_call")
    assert len(orders_after_1) == 1, (
        f"precondition: exactly one covered_call order after cycle 1, got {len(orders_after_1)}"
    )
    # Engine fill-sync between cycles: the covered call is now an OPEN position.
    _mark_overlay_transactions_opened("covered_call")

    # --- Manage cycle 2: a covered call ALREADY exists -> must NOT sell another. ---
    ev2, summaries2, results2 = _run_manage_cycle(account, recommendation, [txn], ruleset_id)
    orders_after_2 = _overlay_orders("covered_call")
    short_contracts = sum(int(o.filled_qty or o.quantity or 0) for o in orders_after_2)
    coverage_contracts = int(HELD_SHARES // 100)

    assert len(ev2.trade_actions) == 0, (
        f"cycle 2 should create NO actions (the cc_guard stop_processing rule must halt the "
        f"ruleset), got {[a.get_description() for a in ev2.trade_actions]} "
        f"(summaries={summaries2})"
    )
    assert len(orders_after_2) == 1, (
        f"BUG B2 REGRESSION: the O_CC overlay rule re-fired on the second manage cycle - "
        f"{len(orders_after_2)} covered_call orders now exist against {int(HELD_SHARES)} held "
        f"shares ({short_contracts} short call contracts vs coverage for {coverage_contracts}; "
        f"{short_contracts - coverage_contracts} NAKED). The launcher rule pair "
        f"(testplatform/ba2test_launcher.py:2147-2157) must keep the 'cc_guard' "
        f"stop_processing rule (has_covered_call) ahead of 'cc_sell'. "
        f"cycle2 actions={[a.get_description() for a in ev2.trade_actions]}, "
        f"cycle2 results={results2}"
    )


# ---------------------------------------------------------------------------
# Runtime test 2 — protective put (O_PP): same fix shape, mirrored action
# ---------------------------------------------------------------------------
def test_second_manage_cycle_does_not_buy_another_protective_put():
    """Two consecutive manage cycles over a 100-share long with the O_PP overlay
    ruleset must produce AT MOST one protective-put order.

    Regression test for fixed bug B2: the ``pp_guard`` stop_processing rule
    (has_protective_put) halts the second cycle, so no second put is bought
    against the same shares.
    """
    account, recommendation, txn = _seed_equity_position()
    ruleset_id = _seed_overlay_ruleset(_O_PP_RULES, "O_PP-overlay")

    # --- Manage cycle 1: no protective put exists yet -> the overlay buys one. ---
    ev1, summaries1, results1 = _run_manage_cycle(account, recommendation, [txn], ruleset_id)
    assert len(ev1.trade_actions) == 1, (
        f"precondition: cycle 1 should generate exactly one buy_protective_put action, "
        f"got {len(ev1.trade_actions)} (summaries={summaries1})"
    )
    assert any(r.get("success") for r in results1), (
        f"precondition: cycle 1 protective-put order must fill, results={results1}"
    )
    orders_after_1 = _overlay_orders("protective_put")
    assert len(orders_after_1) == 1, (
        f"precondition: exactly one protective_put order after cycle 1, got {len(orders_after_1)}"
    )
    # Engine fill-sync between cycles: the protective put is now an OPEN position.
    _mark_overlay_transactions_opened("protective_put")

    # --- Manage cycle 2: a protective put ALREADY exists -> must NOT buy another. ---
    ev2, summaries2, results2 = _run_manage_cycle(account, recommendation, [txn], ruleset_id)
    orders_after_2 = _overlay_orders("protective_put")
    long_contracts = sum(int(o.filled_qty or o.quantity or 0) for o in orders_after_2)
    needed_contracts = int(HELD_SHARES // 100)

    assert len(ev2.trade_actions) == 0, (
        f"cycle 2 should create NO actions (the pp_guard stop_processing rule must halt the "
        f"ruleset), got {[a.get_description() for a in ev2.trade_actions]} "
        f"(summaries={summaries2})"
    )
    assert len(orders_after_2) == 1, (
        f"BUG B2 REGRESSION: the O_PP overlay rule re-fired on the second manage cycle - "
        f"{len(orders_after_2)} protective_put orders now exist against {int(HELD_SHARES)} held "
        f"shares ({long_contracts} long put contracts vs {needed_contracts} needed). The "
        f"launcher rule pair (testplatform/ba2test_launcher.py:2178-2188) must keep the "
        f"'pp_guard' stop_processing rule (has_protective_put) ahead of 'pp_buy'. "
        f"cycle2 actions={[a.get_description() for a in ev2.trade_actions]}, "
        f"cycle2 results={results2}"
    )


# ---------------------------------------------------------------------------
# Static regression test (PASSES): the launcher overlay rules carry the
# existing-overlay guard. Scans the REAL launcher source via ast so a future
# regression that removes the guard (has_covered_call / has_protective_put +
# stop_processing), reorders it after the overlay, or renames the rule ids
# without updating the replicas above fails here.
#
# The launcher is not IMPORTED for this: calling _build_strategy_covered_call
# needs testplatform/backend on sys.path and pulls app.models.strategy ->
# app.models.database (import-time engine/DB side effects) plus the whole S2
# equity ruleset via _build_strategy_S2 — far too heavy and fragile for a unit
# test. Reading + ast-parsing the source has no side effects.
# ---------------------------------------------------------------------------
_LAUNCHER_PATH = Path(__file__).resolve().parents[1] / "testplatform" / "ba2test_launcher.py"


def _launcher_function_source(function_name: str) -> str:
    source = _LAUNCHER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            segment = ast.get_source_segment(source, node)
            assert segment, f"could not extract source of {function_name} from {_LAUNCHER_PATH}"
            return segment
    raise AssertionError(f"{function_name} not found in {_LAUNCHER_PATH}")


@pytest.mark.parametrize(
    "function_name,guard_rule,overlay_rule,guard_field",
    [
        ("_build_strategy_covered_call", _O_CC_GUARD_RULE, _O_CC_OVERLAY_RULE,
         "has_covered_call"),
        ("_build_strategy_protective_put", _O_PP_GUARD_RULE, _O_PP_OVERLAY_RULE,
         "has_protective_put"),
    ],
)
def test_overlay_rules_have_overlay_guard_static(function_name, guard_rule, overlay_rule,
                                                 guard_field):
    """The fix for B2: each launcher overlay rule is preceded by a stop_processing
    guard rule on the existing-overlay condition.

    Asserts (a) the replicated rule pair — translated via the SAME shared
    converters the engine uses — yields an overlay rule gated only on
    ``has_position`` plus a guard rule gated exactly on the guard flag
    (``has_covered_call`` / ``has_protective_put``) with a single
    ``stop_processing`` action; and (b) the real launcher function source
    contains that guard, ordered BEFORE the overlay rule.
    """
    # (a) The replicated rules convert as the engine expects.
    overlay_triggers = triggers_from_condition_tree(overlay_rule["conditions"])
    overlay_event_types = {t["event_type"] for t in overlay_triggers.values()}
    assert overlay_event_types == {ExpertEventType.F_HAS_POSITION.value}, (
        f"replicated {overlay_rule['id']} rule should be gated only on has_position, "
        f"got {overlay_event_types}"
    )
    guard_triggers = triggers_from_condition_tree(guard_rule["conditions"])
    guard_event_types = {t["event_type"] for t in guard_triggers.values()}
    assert guard_event_types == {guard_field}, (
        f"replicated {guard_rule['id']} rule should be gated exactly on {guard_field!r}, "
        f"got {guard_event_types} — if empty, the field is missing from "
        f"rule_builders.FLAG_FIELD_EVENT and the guard never reaches the engine"
    )
    guard_actions = live_actions_from_trade_rule(guard_rule)
    assert guard_actions and [c["action_type"] for c in guard_actions.values()] == [
        "stop_processing"], (
        f"replicated {guard_rule['id']} rule should carry exactly one stop_processing "
        f"action, got {guard_actions}"
    )

    # (b) The REAL launcher function contains the guard, ahead of the overlay rule.
    fn_source = _launcher_function_source(function_name)
    assert guard_field in fn_source, (
        f"{function_name} no longer references {guard_field!r} — the overlay rule lost "
        f"its existing-overlay guard (bug B2 regression); restore the guard rule or "
        f"update the replicated rule dicts in this test file"
    )
    assert "stop_processing" in fn_source, (
        f"{function_name} has a {guard_field!r} reference but no stop_processing guard "
        f"rule — the negation idiom changed; update the replicated rule dicts"
    )
    # Match the '"id": "<rule>"' dict literals so docstring mentions can't fool the check.
    guard_literal = f'"id": "{guard_rule["id"]}"'
    overlay_literal = f'"id": "{overlay_rule["id"]}"'
    assert guard_literal in fn_source and overlay_literal in fn_source, (
        f"replicated rule ids {guard_rule['id']!r}/{overlay_rule['id']!r} not found in "
        f"{function_name} — launcher changed, update the replicas in this test file"
    )
    assert fn_source.index(guard_literal) < fn_source.index(overlay_literal), (
        f"{function_name}: the guard rule {guard_rule['id']!r} must precede the overlay "
        f"rule {overlay_rule['id']!r} in exit_rules (rules evaluate in order; the guard "
        f"only protects the overlay if it evaluates first)"
    )

"""THE CONTRARIAN ARM ACTUALLY FIRES -- end to end, on the real machinery.

The point of making the option entry gate a ``rec_direction`` mode gene is that the optimizer
can pick the direction, INCLUDING the opposite of the one the structure was authored with: a
LONG CALL entered on the expert's SELL signal. Before this change the field was hard-coded per
structure (``bullish``/``bearish``), so the only thing the optimizer could do was switch the
gate off; the contrarian arm did not exist in the search space at all.

WHY THIS TEST GOES ALL THE WAY DOWN. Asserting the condition class in isolation proves
nothing about the grid: the gate is authored by the launcher, rewritten by the genome decode,
flattened into EventAction triggers, persisted, and only then evaluated. The step that fails
SILENTLY is the flattening -- ``triggers_from_condition_tree`` DROPS a leaf whose field has no
``rule_builders.FIELD_EVENT`` mapping and logs a WARNING, nothing more. A dropped direction
gate makes every option strategy enter in BOTH directions while every log looks healthy and
the optimizer keeps scoring the mode gene. So this file runs:

    _option_entry_rule  ->  collect_param_space  ->  decode_params (the mode gene)
        ->  seed_entry_ruleset_from_rules (the REAL backtest seeding, which is what calls
            triggers_from_condition_tree)  ->  the persisted EventAction
        ->  TradeActionEvaluator's OWN condition construction  ->  evaluate

and asserts the rule's verdict flips with the recommendation's direction.

THE GENOME IS DELIBERATELY MINIMAL. Every optional gate is toggled OFF, which is a genome the
optimizer can and does produce, so the only strategy leaf left is the direction one and the
rule's verdict IS the direction verdict. Leaving the market-data gates in would make the rule
False for both recommendations (no option chain in this fixture) and the test would pass
while proving nothing.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest
from sqlmodel import Session

_BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LAUNCHER = os.path.normpath(os.path.join(_BACKEND, "..", "ba2test_launcher.py"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
_spec = importlib.util.spec_from_file_location("ba2test_launcher_contrarian", _LAUNCHER)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

import ba2_common.core.db as cdb  # noqa: E402
from app.services.backtest import default_rulesets as dr  # noqa: E402
from app.services.backtest.backtest_db import backtest_trading_db  # noqa: E402
from app.services.backtest.seam_wiring import wire_backtest_seams  # noqa: E402
from app.services.strategy_param_space import (  # noqa: E402
    collect_param_space,
    decode_params,
)
from ba2_common.core.models import Ruleset  # noqa: E402
from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator  # noqa: E402
from ba2_common.core.types import ExpertEventType, OrderRecommendation  # noqa: E402

#: The structure under test: a LONG CALL. Authored bullish, so "fires on SELL" is
#: unambiguously the contrarian reading and not the default one sneaking through.
KIND = "O_LC"
SIGNAL_ID = "o_lc-signal"


@pytest.fixture()
def _trading_db():
    wire_backtest_seams()
    with backtest_trading_db("option-entry-direction-test"):
        yield


class _StubAccount:
    """Enough account for the evaluator's condition construction. ``has_no_position`` reads
    the transaction store (empty in this fixture), not the account."""

    id = 1


def _rec(action):
    """A recommendation with every NON-direction gate satisfied, so the only thing that
    differs between the BUY case and the SELL case is the direction."""
    return SimpleNamespace(
        id=1, instance_id=1, symbol="MSFT", recommended_action=action,
        confidence=90.0, current_price=100.0, target_price=200.0, data=None,
    )


def _genome(strategy, *, mode):
    """A decoded genome with every optional gate OFF and the direction gene set to ``mode``.

    Built from the REAL collected space, so a gene that stops existing (or a mode gene that
    never appears) fails here rather than being silently skipped.
    """
    space = collect_param_space(strategy)
    assert f"cond:{SIGNAL_ID}:mode" in space, (
        f"{KIND} emits no direction mode gene; the whole feature is missing")
    flat = {key: 0 for key in space if key.endswith(":enabled")}
    flat[f"cond:{SIGNAL_ID}:mode"] = mode
    return flat


def _seed(strategy, *, mode):
    decoded = decode_params(strategy, _genome(strategy, mode=mode))
    rid = dr.seed_entry_ruleset_from_rules(decoded["entry_rules"], name=f"contrarian-{mode}")
    with Session(cdb.get_engine()) as s:
        ruleset = s.get(Ruleset, rid)
        eas = list(ruleset.event_actions)
    assert len(eas) == 1, f"expected one entry rule, got {len(eas)}"
    return eas[0]


def _direction_triggers(event_action):
    return [cfg for cfg in (event_action.triggers or {}).values()
            if cfg.get("event_type") == ExpertEventType.N_REC_DIRECTION.value]


def _rule_fires(event_action, action) -> bool:
    """Every persisted trigger, ANDed -- the evaluator's own contract -- built through the
    evaluator's OWN ``_create_condition_from_trigger`` so this exercises the live
    construction path and not a re-implementation of it."""
    evaluator = TradeActionEvaluator(_StubAccount(), instrument_name="MSFT")
    rec = _rec(action)
    results = []
    for key, cfg in (event_action.triggers or {}).items():
        condition = evaluator._create_condition_from_trigger(
            ExpertEventType(cfg["event_type"]), cfg, "MSFT", rec, None)
        assert condition is not None, f"trigger {key} built no condition"
        results.append(condition.evaluate())
    assert results, "the rule has no triggers at all; the verdict would be vacuously True"
    return all(results)


# --- the gate reaches the engine at all ------------------------------------------------
@pytest.mark.parametrize("mode,operator", [("above", ">"), ("below", "<")])
def test_the_decoded_direction_gate_survives_into_the_persisted_ruleset(_trading_db, mode,
                                                                       operator):
    """The silent-drop fence. One ``rec_direction`` trigger, carrying the operator the mode
    decoded to -- not zero triggers with a WARNING in the log."""
    strategy = mod._build_strategy(KIND, f"g-{KIND}", "DeterministicScorer")
    triggers = _direction_triggers(_seed(strategy, mode=mode))
    assert len(triggers) == 1, f"mode {mode!r} persisted {len(triggers)} direction triggers"
    assert triggers[0]["operator"] == operator
    assert triggers[0]["value"] == 0.0


# --- the contrarian arm ----------------------------------------------------------------
def test_a_long_call_on_mode_below_enters_on_SELL_and_not_on_BUY(_trading_db):
    """THE WHOLE POINT. O_LC is a LONG CALL authored bullish; with the direction gene at
    ``below`` it must enter on the expert's SELL call and refuse the BUY."""
    strategy = mod._build_strategy(KIND, f"g-{KIND}", "DeterministicScorer")
    ea = _seed(strategy, mode="below")
    assert _rule_fires(ea, OrderRecommendation.SELL) is True, (
        "the contrarian arm does not fire on SELL -- the direction gate is inverted or inert")
    assert _rule_fires(ea, OrderRecommendation.BUY) is False, (
        "the contrarian arm also fires on BUY -- the gate was DROPPED, not flipped")
    assert _rule_fires(ea, OrderRecommendation.HOLD) is False


def test_the_default_direction_still_enters_on_BUY_and_not_on_SELL(_trading_db):
    """The mirror, on the SAME machinery: ``above`` is the authored default and must behave
    exactly as the old ``bullish`` flag did. Without this, an inverted operator table would
    make the test above pass."""
    strategy = mod._build_strategy(KIND, f"g-{KIND}", "DeterministicScorer")
    ea = _seed(strategy, mode="above")
    assert _rule_fires(ea, OrderRecommendation.BUY) is True
    assert _rule_fires(ea, OrderRecommendation.SELL) is False
    assert _rule_fires(ea, OrderRecommendation.HOLD) is False


def test_mode_off_removes_the_direction_gate_entirely(_trading_db):
    """The third choice, and the one that must NOT be confused with a dropped gate: ``off``
    is a deliberate removal, so the structure enters on any reading -- including HOLD."""
    strategy = mod._build_strategy(KIND, f"g-{KIND}", "DeterministicScorer")
    ea = _seed(strategy, mode="off")
    assert _direction_triggers(ea) == [], "mode 'off' must remove the leaf, not weaken it"
    for action in (OrderRecommendation.BUY, OrderRecommendation.HOLD, OrderRecommendation.SELL):
        assert _rule_fires(ea, action) is True, action


def test_an_expert_that_ERRORED_enters_in_neither_direction(_trading_db):
    """Unevaluable is not 0. A failed analysis must not read as HOLD and must not satisfy
    either direction -- otherwise the ``off``/``below``/``above`` choice would quietly become
    a three-way tie on broken data."""
    strategy = mod._build_strategy(KIND, f"g-{KIND}", "DeterministicScorer")
    for mode in ("above", "below"):
        ea = _seed(strategy, mode=mode)
        assert _rule_fires(ea, OrderRecommendation.ERROR) is False, mode

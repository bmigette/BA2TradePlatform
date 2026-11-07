"""tools/backfill_assigned_stock_policy.py: the plan (dry-run logic) on a throwaway SQLite DB."""
import importlib.util
import json
import sqlite3
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "backfill_assigned_stock_policy",
    Path(__file__).resolve().parents[1] / "tools" / "backfill_assigned_stock_policy.py")
tool = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tool)


def _live():
    c = sqlite3.connect(":memory:")
    c.executescript("""
    create table expertinstance(id integer, alias text, expert text, user_description text,
        enter_market_ruleset_id integer, open_positions_ruleset_id integer);
    create table expertsetting(instance_id integer, key text);
    create table eventaction(id integer, actions text);
    create table ruleset_eventaction_link(ruleset_id integer, eventaction_id integer);
    """)
    return c


def _inst(c, i, alias, actions, desc="", stored=()):
    c.execute("insert into expertinstance values(?,?,?,?,?,?)", (i, alias, "DeterministicScorer", desc, i, None))
    c.execute("insert into eventaction values(?,?)", (i, json.dumps(actions)))
    c.execute("insert into ruleset_eventaction_link values(?,?)", (i, i))
    for k in stored:
        c.execute("insert into expertsetting values(?,?)", (i, k))


def _test_db(acct):
    t = sqlite3.connect(":memory:")
    t.executescript("create table backtests(id integer, optimization_id integer);"
                    "create table strategy_optimizations(id integer, optimization_config text);")
    t.execute("insert into backtests values(164, 1)")
    t.execute("insert into strategy_optimizations values(1, ?)",
              (json.dumps({"backtest": {"account_settings": acct}}),))
    return t


def test_structure_decides_without_a_backtest():
    c = _live()
    _inst(c, 1, "wheel", {"a": "sell_cash_secured_put", "b": "sell_covered_call"})
    _inst(c, 2, "csp", {"a": "sell_cash_secured_put"})
    _inst(c, 3, "equity", {"a": "buy"})
    rows = {r[0]: r for r in tool.plan(c, None)}
    assert rows[1][4] == {"hold_assigned_stock": True}
    assert rows[2][4] == {"hold_assigned_stock": False}
    assert rows[3][3] == "SKIP" and rows[3][4] is None


def test_the_source_backtest_wins_and_a_disagreement_is_printed():
    c = _live()
    _inst(c, 1, "x", {"a": "sell_cash_secured_put"}, desc="Deployed from L (backtest 164).")
    rows = tool.plan(c, _test_db({"hold_assigned_stock": False, "hold_assigned_stock_tags": [7]}))
    assert rows[0][4] == {"hold_assigned_stock_tags": [7]} and "backtest" in rows[0][3]
    rows = tool.plan(c, _test_db({"hold_assigned_stock": True}))
    assert rows[0][4] == {"hold_assigned_stock": True} and "disagrees" in rows[0][5]


def test_an_instance_that_already_stores_a_key_is_never_touched():
    c = _live()
    _inst(c, 1, "csp", {"a": "sell_cash_secured_put"}, stored=("hold_assigned_stock",))
    (row,) = tool.plan(c, None)
    assert row[3].startswith("already set") and row[4] is None


def test_an_unknown_backtest_falls_back_to_structure():
    c = _live()
    _inst(c, 1, "csp", {"a": "sell_cash_secured_put"}, desc="(backtest 999)")
    (row,) = tool.plan(c, _test_db({}))
    assert row[4] == {"hold_assigned_stock": False} and "structure" in row[3]


def test_the_settings_are_declared_on_the_expert_interface():
    from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
    MarketExpertInterface._ensure_builtin_settings()
    defs = MarketExpertInterface._builtin_settings
    assert defs["hold_assigned_stock"]["type"] == "bool"
    assert defs["hold_assigned_stock_tags"]["type"] == "json"

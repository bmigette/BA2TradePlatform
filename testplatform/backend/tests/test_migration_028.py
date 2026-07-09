"""
Test for migration 028: unify Strategy on entry_rules/exit_rules (EventAction-shaped
TradeRule lists), converting the legacy trio and dropping the 5 legacy columns.

Builds a legacy strategies table (post-026 shape: buy/sell trees + entry_actions +
exit_conditions), runs db_migrate/028_unified_trade_rules.py via importlib against a real
sqlite3 connection (the runner contract: upgrade(cursor, conn)), and asserts:
  * legacy columns are gone, entry_rules/exit_rules present, PK/AUTOINCREMENT preserved;
  * a 2-OR-branch buy tree + flat bracket converts to 2 entry rules EACH carrying
    buy + bracket actions;
  * legacy single-action exit rows lift to one-action rules (order preserved);
  * idempotent re-run is a no-op.
"""

import importlib.util
import json
import sqlite3
from pathlib import Path

MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "db_migrate" / "028_unified_trade_rules.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_028", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _table_columns(cursor, table):
    cursor.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cursor.fetchall()]


BUY_TREE = {"operator": "OR", "conditions": [
    {"operator": "AND", "conditions": [{"field": "bullish", "fieldType": "flag"}]},
    {"operator": "AND", "conditions": [
        {"field": "confidence", "comparison": ">=", "value": 90}]},
]}
ENTRY_ACTIONS = [
    {"id": "tp", "action_type": "adjust_take_profit",
     "reference_value": "expert_target_price", "action_value": -2},
    {"id": "sl", "action_type": "adjust_stop_loss",
     "reference_value": "order_open_price", "action_value": -10},
]
EXIT_CONDITIONS = [
    {"id": "x_bear", "action_type": "close",
     "conditions": {"type": "AND", "conditions": [{"field": "bearish"}]}},
    {"id": "x_lock", "action_type": "adjust_stop_loss", "reference_value": "order_open_price",
     "action_value": 4.0, "toggle_optimize": True,
     "conditions": {"type": "AND", "conditions": [
         {"field": "profit_loss_percent", "op": ">", "value": 16}]}},
]


def _build_legacy_strategies(conn):
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE strategies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(255) NOT NULL,
            description TEXT,
            required_fields JSON,
            entry_conditions JSON,
            buy_entry_conditions JSON,
            sell_entry_conditions JSON,
            exit_conditions JSON,
            entry_actions JSON,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME
        )
    """)
    cursor.execute(
        "INSERT INTO strategies (name, buy_entry_conditions, exit_conditions, entry_actions)"
        " VALUES (?, ?, ?, ?)",
        ("legacy-strat", json.dumps(BUY_TREE), json.dumps(EXIT_CONDITIONS),
         json.dumps(ENTRY_ACTIONS)),
    )
    cursor.execute("INSERT INTO strategies (name) VALUES (?)", ("empty-strat",))
    conn.commit()


def test_migration_028_converts_and_drops_legacy_columns():
    conn = sqlite3.connect(":memory:")
    _build_legacy_strategies(conn)
    migration = _load_migration()
    cursor = conn.cursor()

    assert migration.upgrade(cursor, conn) is True

    cols = _table_columns(cursor, "strategies")
    for legacy in ("entry_conditions", "buy_entry_conditions", "sell_entry_conditions",
                   "exit_conditions", "entry_actions"):
        assert legacy not in cols
    assert "entry_rules" in cols and "exit_rules" in cols

    cursor.execute("SELECT id, name, entry_rules, exit_rules FROM strategies ORDER BY id")
    rows = cursor.fetchall()
    assert [(r[0], r[1]) for r in rows] == [(1, "legacy-strat"), (2, "empty-strat")]

    entry_rules = json.loads(rows[0][2])
    assert len(entry_rules) == 2  # one per OR branch
    for rule in entry_rules:
        kinds = [a["action_type"] for a in rule["actions"]]
        assert kinds == ["buy", "adjust_take_profit", "adjust_stop_loss"]
        assert rule["continue_processing"] is False

    def fields(rule):
        return [c["field"] for c in rule["conditions"]["conditions"]]

    # The old seeder's implicit base gates become EXPLICIT leaves: branch 1 already had
    # bullish (only flat added); branch 2 (confidence-only) gains both.
    assert set(fields(entry_rules[0])) == {"has_no_position", "bullish"}
    assert set(fields(entry_rules[1])) == {"bullish", "has_no_position", "confidence"}

    exit_rules = json.loads(rows[0][3])
    assert [r["id"] for r in exit_rules] == ["x_bear", "x_lock"]
    assert exit_rules[0]["actions"][0]["action_type"] == "close"
    lock = exit_rules[1]
    assert lock["actions"][0]["action_type"] == "adjust_stop_loss"
    assert lock["actions"][0]["action_value"] == 4.0
    assert lock["toggle_optimize"] is True  # rule-level key stays on the rule
    assert lock["conditions"]["conditions"][0]["field"] == "profit_loss_percent"

    # empty strategy converts to empty lists, not NULL surprises
    assert json.loads(rows[1][2]) == [] and json.loads(rows[1][3]) == []

    # AUTOINCREMENT preserved: next insert continues the sequence
    cursor.execute("INSERT INTO strategies (name) VALUES ('post')")
    cursor.execute("SELECT id FROM strategies WHERE name='post'")
    assert cursor.fetchone()[0] == 3


def test_migration_028_is_idempotent():
    conn = sqlite3.connect(":memory:")
    _build_legacy_strategies(conn)
    migration = _load_migration()
    cursor = conn.cursor()
    assert migration.upgrade(cursor, conn) is True
    assert migration.upgrade(cursor, conn) is False  # second run: nothing to do

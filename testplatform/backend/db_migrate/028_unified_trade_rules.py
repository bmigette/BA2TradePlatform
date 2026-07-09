"""
Migration 028: unify Strategy on the EventAction-shaped rule model.

Replaces the split representation (`buy_entry_conditions`/`sell_entry_conditions` condition
trees + one flat `entry_actions` list + single-action `exit_conditions` rows + the long-dead
`entry_conditions`) with TWO ordered TradeRule lists that mirror the live `EventAction`
contract 1:1 (see docs/plans/2026-07-08-unified-rule-model.md):

    entry_rules JSON  -- [{id, name?, conditions: tree|None, actions: [{action_type, ...}, ...],
                           continue_processing: bool, toggle_optimize?}, ...]
    exit_rules  JSON  -- same shape

Data conversion (in-place, per row):
  * entry: one rule per top-level OR branch of each side's tree (AND/leaf tree = one rule);
    each rule's actions = the side's open action + the flat `entry_actions` bracket replicated
    per rule — exactly the semantics the old seeder implemented implicitly, now explicit data.
  * exit: each legacy row lifts to a one-action rule (action fields move into `actions[0]`,
    rule-level keys stay on the rule); order preserved.

Follows migration 022/026's SQLite-safe schema-preserving table-rebuild pattern (explicit DDL
mirroring the ORM model). Idempotent: no-op when `entry_rules` exists and no legacy columns
remain. `upgrade(cursor, conn)` returns truthy when changes were applied.
"""
import json

STRATEGIES_DDL = """
CREATE TABLE strategies_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    required_fields JSON,
    entry_rules JSON,
    exit_rules JSON,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME
)
"""

STRATEGIES_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_strategies_id ON strategies (id)",
]

LEGACY_COLUMNS = (
    "entry_conditions", "buy_entry_conditions", "sell_entry_conditions",
    "exit_conditions", "entry_actions",
)

# Rule-level keys when lifting a legacy single-action exit row (everything else belongs to
# that row's single action) — mirrors ba2_common.core.rule_models._RULE_LEVEL_KEYS, inlined
# so the migration stays self-contained like 022/026.
_RULE_LEVEL_KEYS = {
    "id", "name", "conditions", "enabled",
    "continue_processing", "continueProcessing",
    "toggle_optimize", "toggleOptimize",
    "actions",
}


def _loads(val):
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    try:
        return json.loads(val)
    except (TypeError, ValueError):
        return None


def _or_branches(tree):
    if not isinstance(tree, dict):
        return []
    op = str(tree.get("operator") or tree.get("type") or "AND").upper()
    if op == "OR":
        return [b for b in (tree.get("conditions") or []) if isinstance(b, dict)]
    return [tree]


def _lift_exit_row(rule):
    if "actions" in rule and isinstance(rule["actions"], list):
        return rule
    action = {k: v for k, v in rule.items() if k not in _RULE_LEVEL_KEYS}
    lifted = {k: v for k, v in rule.items() if k in _RULE_LEVEL_KEYS}
    lifted["actions"] = [action] if action else []
    lifted.setdefault("continue_processing", False)
    return lifted


def _leaf_fields(node):
    if not isinstance(node, dict):
        return set()
    fields = set()
    for child in (node.get("conditions") or []):
        fields |= _leaf_fields(child)
    if node.get("field"):
        fields.add(node["field"])
    return fields


def _with_base_gates(branch, side, rid):
    """The OLD seeder implicitly ANDed bullish/bearish + has_no_position onto every entry
    rule; the unified seeder is 1:1 with no magic, so prepend those base leaves explicitly
    whenever the branch doesn't already carry them (mirrors
    rule_models.trade_rules_from_legacy, inlined so the migration stays self-contained)."""
    present = _leaf_fields(branch)
    signal = "bullish" if side == "buy" else "bearish"
    base = []
    if signal not in present:
        base.append({"id": f"{rid}-{signal}", "field": signal, "field_type": "flag"})
    if "has_no_position" not in present:
        base.append({"id": f"{rid}-flat", "field": "has_no_position", "field_type": "flag"})
    if not base:
        return branch
    kids = branch.get("conditions") if isinstance(branch, dict) else None
    if isinstance(kids, list):
        merged = dict(branch)
        merged["conditions"] = base + kids
        return merged
    return {"id": f"{rid}-grp", "operator": "AND", "conditions": base + [branch]}


def convert_row(buy_tree, sell_tree, entry_actions, exit_conditions):
    """Legacy trio -> (entry_rules, exit_rules). Pure; exercised directly by the tests."""
    bracket = [a for a in (entry_actions or []) if isinstance(a, dict)]
    entry_rules = []
    for tree, open_action, side in ((buy_tree, "buy", "buy"), (sell_tree, "sell", "sell")):
        branches = _or_branches(tree)
        for j, branch in enumerate(branches):
            suffix = f"-{j + 1}" if len(branches) > 1 else ""
            rid = f"{side}{suffix}"
            entry_rules.append({
                "id": rid,
                "name": f"enter-{side}{suffix}",
                "conditions": _with_base_gates(branch, side, rid),
                "actions": [{"action_type": open_action}] + [dict(a) for a in bracket],
                "continue_processing": False,
            })
    exit_rules = [_lift_exit_row(r) for r in (exit_conditions or []) if isinstance(r, dict)]
    return entry_rules, exit_rules


def get_table_columns(cursor, table_name):
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [col[1] for col in cursor.fetchall()]


def _table_exists(cursor, name):
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cursor.fetchone() is not None


def upgrade(cursor, conn):
    """Rebuild `strategies` on entry_rules/exit_rules, converting each row's legacy data."""
    if not _table_exists(cursor, "strategies"):
        print("  - strategies table does not exist yet; nothing to migrate")
        return False

    columns = get_table_columns(cursor, "strategies")
    legacy_present = [c for c in columns if c in LEGACY_COLUMNS]
    if "entry_rules" in columns and not legacy_present:
        print("  - entry_rules present and no legacy columns; nothing to migrate")
        return False

    print(f"  - unifying strategies on entry_rules/exit_rules "
          f"(converting + dropping {len(legacy_present)} legacy column(s))")

    def col(row, name):
        return row[columns.index(name)] if name in columns else None

    cursor.execute(f"SELECT {', '.join(columns)} FROM strategies")
    rows = cursor.fetchall()
    converted = []
    for row in rows:
        entry_rules, exit_rules = convert_row(
            _loads(col(row, "buy_entry_conditions")),
            _loads(col(row, "sell_entry_conditions")),
            _loads(col(row, "entry_actions")),
            _loads(col(row, "exit_conditions")),
        )
        # A row already on the new shape keeps its data verbatim.
        if "entry_rules" in columns and _loads(col(row, "entry_rules")) is not None:
            entry_rules = _loads(col(row, "entry_rules"))
        if "exit_rules" in columns and _loads(col(row, "exit_rules")) is not None:
            exit_rules = _loads(col(row, "exit_rules"))
        converted.append((
            col(row, "id"), col(row, "name"), col(row, "description"),
            col(row, "required_fields"),
            json.dumps(entry_rules), json.dumps(exit_rules),
            col(row, "created_at"), col(row, "updated_at"),
        ))

    cursor.execute("DROP TABLE IF EXISTS strategies_new")
    cursor.execute(STRATEGIES_DDL)
    cursor.executemany(
        "INSERT INTO strategies_new (id, name, description, required_fields, entry_rules, "
        "exit_rules, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        converted,
    )
    cursor.execute("DROP TABLE strategies")
    cursor.execute("ALTER TABLE strategies_new RENAME TO strategies")
    for index_sql in STRATEGIES_INDEXES:
        cursor.execute(index_sql)

    conn.commit()
    print(f"  - rebuilt strategies: {len(converted)} row(s) converted to rule lists "
          f"(PK/AUTOINCREMENT preserved)")
    return True


def downgrade(cursor, conn):
    """SQLite has no simple DROP COLUMN; downgrade is a no-op (data is gone)."""
    print("  - Downgrade not supported for this migration")
    return False

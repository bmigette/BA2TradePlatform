#!/usr/bin/env python
"""Canonicalise every stored boolean setting to the value it CURRENTLY reads as.

    python tools/migrate_bool_settings.py                 # dry run over every known DB
    python tools/migrate_bool_settings.py --apply
    python tools/migrate_bool_settings.py --db path.sqlite --apply

WHY. ``save_settings`` used to store a bool as ``json.dumps(value)`` with no coercion, so the
GA's integer ``1`` was written as the JSON string ``"1"`` -- and the reader tested
``value.lower() == 'true'``, which ``"1"`` is not. A gene the optimizer turned ON therefore came
back OFF: thirteen live rows across instances 6-12 had ``use_atr_stop``,
``regime_overlay_enabled`` and ``screener_weinstein_stage2_only`` silently disabled on
strategies that had been selected with them enabled.

``coerce_bool`` now fixes both ends, which means a stored ``"1"`` STARTS READING AS TRUE. That
is the right long-term behaviour and the wrong thing to happen by surprise: those instances were
measured -- in their backtests too, which share the defect -- with the features off. Flipping
thirteen live strategies into a configuration nothing has ever tested, silently, at the next
market open, is not an upgrade.

So this migration writes each row back as the canonical JSON of the value it reads TODAY, under
the OLD semantics (replicated here as ``_legacy_effective`` rather than imported, so the result
does not depend on whether the code fix has landed yet). Every ``"1"`` becomes ``false``:
behaviour is pinned exactly where it is, and the reader fix stops being able to change it.

Turning those features back ON is then a deliberate act -- a fresh GA run whose winners are
selected with the genes actually working -- not a side effect of a bug fix.
"""
import argparse
import json
import os
import sqlite3
import sys

REPO = os.environ.get("BA2_REPO", r"C:\Users\basti\Documents\dev\BA2TradePlatform")
if REPO not in sys.path:
    sys.path.insert(0, REPO)

#: Every database that holds live-shaped settings tables.
DEFAULT_DBS = [
    os.path.expanduser(r"~\Documents\ba2_trade_platform-prod\db.sqlite"),
    os.path.expanduser(r"~\Documents\ba2_trade_platform\db.sqlite"),
    os.path.expanduser(r"~\Documents\ba2\test\dl_forecasting.db"),
]
TABLES = ("expertsetting", "accountsetting", "appsetting")


def _legacy_effective(raw):
    """What the OLD reader made of a stored value. Deliberately duplicated, not imported: the
    migration must pin today's behaviour whether or not the fix is already deployed."""
    value = raw
    while isinstance(value, str) and value.startswith('"') and value.endswith('"'):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            break
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() == "true"
    return bool(value)


def _bool_keys():
    """Keys declared ``type: bool`` by any live expert or account class, plus the builtins."""
    import ba2_trade_platform.modules.accounts as accounts_mod
    from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
    from ba2_trade_platform.modules.experts import experts as live_experts

    # The accounts package exports the classes by name, not as a registry list.
    live_accounts = [v for v in vars(accounts_mod).values()
                     if isinstance(v, type) and hasattr(v, "get_settings_definitions")]

    MarketExpertInterface._ensure_builtin_settings()
    keys = set()
    sources = [MarketExpertInterface._builtin_settings or {}]
    for cls in list(live_experts) + live_accounts:
        try:
            sources.append(cls.get_settings_definitions() or {})
        except Exception:  # noqa: BLE001 - a class that cannot describe itself blocks nothing
            continue
    for defs in sources:
        for key, spec in defs.items():
            if isinstance(spec, dict) and spec.get("type") == "bool":
                keys.add(key)
    return keys


def migrate(db_path, bool_keys, apply_changes):
    if not os.path.exists(db_path):
        print(f"  (missing, skipped) {db_path}")
        return 0
    con = sqlite3.connect(db_path)
    changed = 0
    try:
        have = {r[0] for r in con.execute(
            "select name from sqlite_master where type='table'")}
        for table in TABLES:
            if table not in have:
                continue
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
            if "value_json" not in cols or "key" not in cols:
                continue
            for rowid, key, raw in con.execute(
                    f"select rowid, key, value_json from {table}").fetchall():
                if key not in bool_keys or raw is None:
                    continue
                canonical = json.dumps(_legacy_effective(raw))
                if raw == canonical:
                    continue
                print(f"    {table}#{rowid} {key}: {raw!r} -> {canonical}")
                changed += 1
                if apply_changes:
                    con.execute(f"update {table} set value_json=? where rowid=?",
                                (canonical, rowid))
        if apply_changes:
            con.commit()
    finally:
        con.close()
    return changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--db", action="append", help="a specific DB (repeatable)")
    ns = ap.parse_args()

    bool_keys = _bool_keys()
    print(f"{len(bool_keys)} boolean-declared setting key(s) known\n"
          f"mode = {'APPLY' if ns.apply else 'DRY RUN'}\n")
    total = 0
    for db in (ns.db or DEFAULT_DBS):
        print(f"  {db}")
        total += migrate(db, bool_keys, ns.apply)
    print(f"\n{total} row(s) {'rewritten' if ns.apply else 'would change'}.")
    if total and ns.apply:
        print("POST /api/reload on any running instance so the settings caches are dropped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

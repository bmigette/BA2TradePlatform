"""
Recovery script — populates the 16 new experts with their full template
settings (read from the pre-wipe backup DB), preserving the overrides
already in place. Also unifies execution_schedule_* across all FMP variants.
"""

import json
import os
import sqlite3
import sys

DB_PATH  = os.path.expanduser("~/Documents/ba2_trade_platform/db.sqlite")
BAK_PATH = os.path.expanduser("~/Documents/ba2_trade_platform/db.sqlite.bak.20260531-225640")

# Template instance ids in the BACKUP DB
TEMPLATE_IDS = {
    "FH_short_med":   3,
    "FH_risky_long":  5,
    "senate_weight":  7,
    "penny":          17,
    "fmp_consensus":  6,
    "fmp_screener":   18,
}

# New aliases mapped to which template to use
ALIAS_TO_TEMPLATE = {
    "FH Short/Med":         "FH_short_med",
    "RiskyLongFH":          "FH_risky_long",
    "FMPSenateWeight":      "senate_weight",
    "TestPenny":            "penny",
    "FMP-nas30-cons-hc":    "fmp_consensus",
    "FMP-nas30-low-hc":     "fmp_consensus",
    "FMP-nas30-stat":       "fmp_consensus",
    "FMP-ark26-cons-hc":    "fmp_consensus",
    "FMP-ark26-low-hc":     "fmp_consensus",
    "FMP-ark26-stat":       "fmp_consensus",
    "FMP-scr-cons-hc":      "fmp_screener",
    "FMP-scr-low-hc":       "fmp_screener",
    "FMP-scr-stat":         "fmp_screener",
}

# Unified FMP schedules (applied to all FMP-* aliases)
FMP_ENTER_SCHEDULE = {
    "days": {"monday": True, "tuesday": True, "wednesday": True,
              "thursday": True, "friday": True,
              "saturday": False, "sunday": False},
    "times": ["09:30"],
}
FMP_OPEN_SCHEDULE = {
    "days": {"monday": True, "tuesday": True, "wednesday": True,
              "thursday": True, "friday": True,
              "saturday": False, "sunday": False},
    "times": ["15:30"],
}


def load_templates() -> dict[str, list[dict]]:
    c = sqlite3.connect(BAK_PATH)
    c.row_factory = sqlite3.Row
    out: dict[str, list[dict]] = {}
    for tname, tid in TEMPLATE_IDS.items():
        rows = c.execute(
            "SELECT key, value_str, value_float, value_json "
            "FROM expertsetting WHERE instance_id=?",
            (tid,),
        ).fetchall()
        out[tname] = [dict(r) for r in rows]
        print(f"  loaded {len(out[tname])} settings for template '{tname}' (backup id={tid})")
    c.close()
    return out


def restore() -> None:
    print("Loading templates from backup DB...")
    templates = load_templates()

    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")

    experts = list(c.execute(
        "SELECT id, alias, expert FROM expertinstance ORDER BY id"
    ).fetchall())

    print(f"\nApplying templates to {len(experts)} experts...")
    for exp in experts:
        eid = exp["id"]
        alias = exp["alias"]
        tname = ALIAS_TO_TEMPLATE.get(alias)
        if not tname:
            print(f"  SKIP id={eid} alias={alias} (no template mapping)")
            continue

        existing_keys = {
            r["key"]
            for r in c.execute(
                "SELECT key FROM expertsetting WHERE instance_id=?", (eid,)
            ).fetchall()
        }

        added = 0
        for s in templates[tname]:
            if s["key"] in existing_keys:
                continue  # Don't overwrite our overrides
            c.execute(
                """
                INSERT INTO expertsetting(instance_id, key, value_str, value_float, value_json)
                VALUES (?,?,?,?,?)
                """,
                (
                    eid,
                    s["key"],
                    s.get("value_str"),
                    s.get("value_float"),
                    s.get("value_json") if s.get("value_json") is not None else "{}",
                ),
            )
            added += 1
        print(f"  id={eid:2d} {alias:24s}: +{added} settings (was {len(existing_keys)})")

    # Unified FMP schedule for all FMP-* experts
    print("\nUnifying FMP schedules...")
    enter_json = json.dumps(FMP_ENTER_SCHEDULE)
    open_json  = json.dumps(FMP_OPEN_SCHEDULE)
    fmp_experts = list(c.execute(
        "SELECT id, alias FROM expertinstance WHERE alias LIKE 'FMP-%'"
    ).fetchall())
    for fexp in fmp_experts:
        for key, payload in (
            ("execution_schedule_enter_market", enter_json),
            ("execution_schedule_open_positions", open_json),
        ):
            # Upsert
            existing = c.execute(
                "SELECT id FROM expertsetting WHERE instance_id=? AND key=?",
                (fexp["id"], key),
            ).fetchone()
            if existing:
                c.execute(
                    "UPDATE expertsetting SET value_str=NULL, value_float=NULL, value_json=? WHERE id=?",
                    (payload, existing["id"]),
                )
            else:
                c.execute(
                    "INSERT INTO expertsetting(instance_id, key, value_str, value_float, value_json) VALUES (?,?,?,?,?)",
                    (fexp["id"], key, None, None, payload),
                )
        print(f"  id={fexp['id']:2d} {fexp['alias']:24s}: schedule unified")

    c.commit()

    # Report final settings count
    print("\n=== Final settings count per expert ===")
    for r in c.execute("SELECT id, alias FROM expertinstance ORDER BY id"):
        n = c.execute(
            "SELECT COUNT(*) FROM expertsetting WHERE instance_id=?", (r["id"],)
        ).fetchone()[0]
        print(f"  id={r['id']:2d} {r['alias']:24s}: {n} settings")

    c.close()


if __name__ == "__main__":
    if not os.path.exists(BAK_PATH):
        print(f"ERROR: backup not found at {BAK_PATH}")
        sys.exit(1)
    restore()

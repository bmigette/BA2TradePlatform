"""Back up and migrate one live-app database, optionally setting one expert priority.

Example: python tools/migrations/apply_expert_priority.py --db C:/path/db.sqlite
    --expert-id 11 --expected-alias goal2020-mid_ED_S1top1 --priority 100
"""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "packages/common")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--expert-id", type=int)
    parser.add_argument("--expected-alias")
    parser.add_argument("--priority", type=int)
    args = parser.parse_args()
    if any(value is not None for value in (args.expert_id, args.expected_alias, args.priority)):
        if args.expert_id is None or args.expected_alias is None or args.priority is None or args.priority < 1:
            parser.error("Setting a priority requires --expert-id, --expected-alias and --priority >= 1")
    path = args.db.resolve(strict=True)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30) as source:
        revisions = source.execute("SELECT version_num FROM alembic_version").fetchall()
        if revisions not in ([("c8f2a41d67be",)], [("d9e3b72a10fc",)]):
            raise RuntimeError(f"Unexpected migration revision: {revisions}")
        if args.expert_id is not None:
            identity = source.execute("SELECT alias FROM expertinstance WHERE id=?", (args.expert_id,)).fetchone()
            if identity != (args.expected_alias,):
                raise RuntimeError(f"Expert identity mismatch: {identity}")
        # sqlite backup includes committed WAL pages; a file copy would not.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_name(path.name + ".bak-expert-priority-" + stamp)
        with sqlite3.connect(backup) as destination:
            source.backup(destination)
    print(f"Backup: {backup}")

    from alembic import command
    from alembic.config import Config
    os.environ["BA2_DB_FILE"] = str(path)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "d9e3b72a10fc")

    with sqlite3.connect(path, timeout=30) as connection:
        if args.expert_id is not None:
            changed = connection.execute(
                "UPDATE expertinstance SET priority=? WHERE id=? AND alias=?",
                (args.priority, args.expert_id, args.expected_alias))
            if changed.rowcount != 1:
                raise RuntimeError("Expert identity changed during migration; priority update rolled back")
        invalid = connection.execute(
            "SELECT count(*) FROM expertinstance WHERE priority IS NULL OR priority < 1").fetchone()[0]
        if invalid:
            raise RuntimeError(f"Invalid priority in {invalid} expert rows")
        print("Revision:", connection.execute("SELECT version_num FROM alembic_version").fetchone()[0])
        print("Priority counts:", connection.execute(
            "SELECT priority, count(*) FROM expertinstance GROUP BY priority ORDER BY priority").fetchall())
        if args.expert_id is not None:
            print("Expert:", connection.execute(
                "SELECT id, alias, priority FROM expertinstance WHERE id=?", (args.expert_id,)).fetchone())


if __name__ == "__main__":
    main()

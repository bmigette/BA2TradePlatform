#!/usr/bin/env python3
"""
Database Migration Script for BA2 Trade Platform

This script provides convenient commands for managing database migrations using Alembic.
"""

import os
import sys
import subprocess
from pathlib import Path


def _alembic_argv(*args):
    """Build the argv list for an alembic invocation.

    Alembic is invoked as ``<this interpreter> -m alembic ...`` rather than as a
    bare ``alembic`` binary: the console script only exists on PATH when the venv
    is activated, so ``venv/bin/python migrate.py upgrade`` used to die with
    ``/bin/sh: alembic: command not found``. Going through sys.executable also
    guarantees alembic runs against the same interpreter (and therefore the same
    site-packages, and the same alembic/env.py sys.path fixups) as this script.
    """
    return [sys.executable, "-m", "alembic", *args]


def run_command(cmd):
    """Run an alembic argv list and return True on success.

    ``cmd`` is a list of arguments and is executed WITHOUT a shell, so migration
    messages containing quotes, ``$``, ``;`` or any other shell metacharacter are
    passed through verbatim instead of being re-parsed by /bin/sh.
    """
    print(f"Running: {subprocess.list2cmdline(cmd)}")
    result = subprocess.run(cmd, shell=False, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.returncode == 0


def create_migration(message):
    """Create a new migration with the given message."""
    if not message:
        message = input("Enter migration message: ")

    cmd = _alembic_argv("revision", "--autogenerate", "-m", message)
    if run_command(cmd):
        print(f"[OK] Migration created successfully: {message}")
        return True
    else:
        print(f"[FAIL] Failed to create migration: {message}")
        return False


def upgrade_database(revision="head"):
    """Upgrade database to the specified revision (default: head)."""
    cmd = _alembic_argv("upgrade", revision)
    if run_command(cmd):
        print(f"[OK] Database upgraded to {revision}")
        return True
    else:
        print(f"[FAIL] Failed to upgrade database to {revision}")
        return False


def downgrade_database(revision):
    """Downgrade database to the specified revision."""
    if not revision:
        revision = input("Enter revision to downgrade to: ")

    cmd = _alembic_argv("downgrade", revision)
    if run_command(cmd):
        print(f"[OK] Database downgraded to {revision}")
        return True
    else:
        print(f"[FAIL] Failed to downgrade database to {revision}")
        return False


def show_history():
    """Show migration history."""
    cmd = _alembic_argv("history", "--verbose")
    run_command(cmd)


def show_current():
    """Show current database revision."""
    cmd = _alembic_argv("current")
    run_command(cmd)


def show_heads():
    """Show current head revisions."""
    cmd = _alembic_argv("heads")
    run_command(cmd)


def stamp_database(revision="head"):
    """Stamp database with the specified revision without running migrations."""
    cmd = _alembic_argv("stamp", revision)
    if run_command(cmd):
        print(f"[OK] Database stamped with {revision}")
        return True
    else:
        print(f"[FAIL] Failed to stamp database with {revision}")
        return False

def main():
    """Main function to handle command line arguments."""
    if len(sys.argv) < 2:
        print("Usage: python migrate.py <command> [args]")
        print("\nAvailable commands:")
        print("  create <message>     - Create a new migration")
        print("  upgrade [revision]   - Upgrade database (default: head)")
        print("  downgrade <revision> - Downgrade database to revision")
        print("  history              - Show migration history")
        print("  current              - Show current database revision")
        print("  heads                - Show current head revisions")
        print("  stamp [revision]     - Stamp database without running migrations")
        print("\nExamples:")
        print("  python migrate.py create 'Add risk_level and time_horizon to ExpertRecommendation'")
        print("  python migrate.py upgrade")
        print("  python migrate.py downgrade -1")
        print("  python migrate.py history")
        sys.exit(1)

    command = sys.argv[1].lower()

    # Change to script directory to ensure alembic commands work
    script_dir = Path(__file__).parent
    os.chdir(script_dir)

    if command == "create":
        message = sys.argv[2] if len(sys.argv) > 2 else None
        create_migration(message)
    elif command == "upgrade":
        revision = sys.argv[2] if len(sys.argv) > 2 else "head"
        upgrade_database(revision)
    elif command == "downgrade":
        revision = sys.argv[2] if len(sys.argv) > 2 else None
        downgrade_database(revision)
    elif command == "history":
        show_history()
    elif command == "current":
        show_current()
    elif command == "heads":
        show_heads()
    elif command == "stamp":
        revision = sys.argv[2] if len(sys.argv) > 2 else "head"
        stamp_database(revision)
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)

if __name__ == "__main__":
    main()

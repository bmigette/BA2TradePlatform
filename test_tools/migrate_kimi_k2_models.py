"""
Migration script to update Kimi K2 model references to Kimi K2.5.

This script updates expert settings in the database:
- kimi_k2_thinking -> kimi_k2.5 (thinking version)
- kimi_k2 -> kimi_k2.5-nonthinking (instant/non-thinking version)

The mappings handle various formats:
- moonshot/kimi_k2_thinking -> moonshot/kimi_k2.5
- native/kimi_k2_thinking -> native/kimi_k2.5
- moonshot/kimi_k2 -> moonshot/kimi_k2.5-nonthinking
- native/kimi_k2 -> native/kimi_k2.5-nonthinking

Usage:
    python test_tools/migrate_kimi_k2_models.py           # Dry run (show what would be changed)
    python test_tools/migrate_kimi_k2_models.py --apply   # Apply changes
"""

import sys
import os
import re
import argparse

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import select
from ba2_trade_platform.core.db import get_db, update_instance
from ba2_trade_platform.core.models import ExpertSetting, ExpertInstance


# Migration mapping: old model patterns -> new model names
# Order matters! More specific patterns first
MIGRATIONS = [
    # Thinking variant: kimi_k2_thinking -> kimi_k2.5
    (r"^(.*/)kimi_k2_thinking$", r"\1kimi_k2.5"),
    (r"^kimi_k2_thinking$", "kimi_k2.5"),

    # Thinking turbo variant: kimi_k2_thinking_turbo -> kimi_k2.5 (same target)
    (r"^(.*/)kimi_k2_thinking_turbo$", r"\1kimi_k2.5"),
    (r"^kimi_k2_thinking_turbo$", "kimi_k2.5"),

    # Non-thinking variant: kimi_k2 -> kimi_k2.5-nonthinking
    # Match kimi_k2 but NOT kimi_k2.5 or kimi_k2_thinking
    (r"^(.*/)kimi_k2$", r"\1kimi_k2.5-nonthinking"),
    (r"^kimi_k2$", "kimi_k2.5-nonthinking"),
]


def find_settings_to_migrate(session):
    """Find all expert settings that need migration."""
    # Get all settings with string values containing 'kimi_k2'
    stmt = select(ExpertSetting).where(ExpertSetting.value_str.contains("kimi_k2"))
    settings = session.exec(stmt).all()

    migrations = []
    for setting in settings:
        if setting.value_str is None:
            continue

        old_value = setting.value_str
        new_value = None

        # Try each migration pattern
        for pattern, replacement in MIGRATIONS:
            if re.match(pattern, old_value):
                new_value = re.sub(pattern, replacement, old_value)
                break

        # Skip if no migration needed or already migrated
        if new_value is None:
            continue
        if old_value == new_value:
            continue
        if "kimi_k2.5" in old_value:  # Already migrated
            continue

        migrations.append((setting, old_value, new_value))

    return migrations


def print_migration_plan(migrations, session):
    """Print the migration plan with expert instance details."""
    if not migrations:
        print("\nNo settings need migration.")
        return

    print(f"\n{'='*80}")
    print(f"Found {len(migrations)} settings to migrate:")
    print(f"{'='*80}\n")

    for setting, old_value, new_value in migrations:
        # Get expert instance info
        expert_stmt = select(ExpertInstance).where(ExpertInstance.id == setting.instance_id)
        expert = session.exec(expert_stmt).first()
        expert_info = f"{expert.expert} (ID: {expert.id})" if expert else f"ID: {setting.instance_id}"

        print(f"Expert: {expert_info}")
        print(f"  Setting: {setting.key}")
        print(f"  Old: {old_value}")
        print(f"  New: {new_value}")
        print()


def apply_migrations(migrations, session):
    """Apply the migrations."""
    if not migrations:
        print("\nNo settings to migrate.")
        return

    success_count = 0
    error_count = 0

    for setting, old_value, new_value in migrations:
        try:
            setting.value_str = new_value
            update_instance(setting, session)
            success_count += 1
            print(f"✓ Updated setting {setting.id}: {old_value} -> {new_value}")
        except Exception as e:
            error_count += 1
            print(f"✗ Error updating setting {setting.id}: {e}")

    session.commit()

    print(f"\n{'='*80}")
    print(f"Migration complete: {success_count} success, {error_count} errors")
    print(f"{'='*80}")


def main():
    parser = argparse.ArgumentParser(
        description="Migrate Kimi K2 model references to Kimi K2.5"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the migrations (default is dry run)"
    )
    args = parser.parse_args()

    print("\n" + "="*80)
    print("Kimi K2 -> K2.5 Migration Script")
    print("="*80)
    print("\nMigration mappings:")
    print("  - kimi_k2_thinking -> kimi_k2.5 (thinking enabled)")
    print("  - kimi_k2 -> kimi_k2.5-nonthinking (instant mode)")
    print()

    session = get_db()

    try:
        # Find settings to migrate
        migrations = find_settings_to_migrate(session)

        # Print plan
        print_migration_plan(migrations, session)

        if args.apply:
            print("\n" + "-"*80)
            print("APPLYING MIGRATIONS...")
            print("-"*80 + "\n")
            apply_migrations(migrations, session)
        else:
            print("-"*80)
            print("DRY RUN - No changes made.")
            print("Run with --apply to apply the migrations.")
            print("-"*80)

    except Exception as e:
        print(f"\nError: {e}")
        session.rollback()
        return 1
    finally:
        session.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())

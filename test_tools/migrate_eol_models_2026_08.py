"""Migrate ExpertSetting model strings off EOL models (2026-08 registry refresh).

Dry-run by default; pass --apply to write. Mirrors migrate_kimi_k2_models.py.
Remaps BOTH friendly-name forms (moonshot/kimi_k2_thinking) and provider-name
forms (NagaAI/kimi-k2-thinking), preserving the provider prefix.

Usage:
    python test_tools/migrate_eol_models_2026_08.py           # Dry run (show what would be changed)
    python test_tools/migrate_eol_models_2026_08.py --apply   # Apply changes
"""

import sys
import os
import re
import argparse

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import select
from ba2_trade_platform import config
from ba2_trade_platform.core.db import configure_db, get_db, update_instance
from ba2_trade_platform.core.models import ExpertSetting, ExpertInstance


# Ordered: longer/more-specific patterns first (thinking before base, nonthinking explicit).
REMAP = [
    # moonshot friendly names
    (r"kimi_k2_thinking(_turbo)?$", "kimi_k2.6"),
    (r"kimi_k2\.5$", "kimi_k2.6"),
    (r"kimi_k2\.5-nonthinking$", "kimi_k2.6-nonthinking"),
    (r"kimi_k2$", "kimi_k2.6-nonthinking"),
    (r"kimi_k1\.5$", "kimi_k2.6-nonthinking"),
    # moonshot provider names
    (r"kimi-k2-thinking(-turbo)?$", "kimi-k2.6"),
    (r"kimi-k2\.5$", "kimi-k2.6"),
    (r"kimi-k2(-0905-preview|-0711-preview|-turbo-preview)?$", "kimi-k2.6-nonthinking"),
    (r"moonshot-v1-(8k|32k|128k)(-vision-preview)?$", "kimi-k2.6-nonthinking"),
    # deepseek friendly names
    (r"deepseek_(v3\.2|chat|reasoner|coder)$", "deepseek_v4_flash"),
    # deepseek provider names (legacy DB strings carry :free / -speciale suffixes)
    (r"deepseek-(v3\.2|v3\.2-speciale|chat|chat-v3\.1|reasoner|reasoner-0528|coder)(:free)?$",
     "deepseek-v4-flash"),
    # xai friendly + provider names (legacy DB strings use the DOT form grok-4.1-...)
    (r"grok4\.1_fast(_reasoning)?$", "grok4.5"),
    (r"grok-4-1-fast(-non)?-reasoning$", "grok-4.5"),
    (r"grok-4\.1-fast(-non)?-reasoning$", "grok-4.5"),
    # openai o1 family — dash = provider-name form, underscore = friendly form;
    # a bare trailing "/o1" defaults to the friendly successor (ModelSelector stores friendly names)
    (r"o1-mini(-\d{4}-\d{2}-\d{2})?$", "gpt-5.6-terra"),
    (r"o1_mini$", "gpt5.6_terra"),
    (r"(^|/)o1-\d{4}-\d{2}-\d{2}$", r"\g<1>gpt-5.6-terra"),
    (r"(^|/)o1$", r"\g<1>gpt5.6_terra"),
]


def remap_value(value: str) -> "str | None":
    """Return the remapped model string, or None when the value is untouched."""
    for pattern, replacement in REMAP:
        new = re.sub(pattern, replacement, value)
        if new != value:
            return new
    return None


def find_settings_to_migrate(session):
    """Find all expert settings that need migration."""
    stmt = select(ExpertSetting).where(ExpertSetting.value_str.is_not(None))
    settings = session.exec(stmt).all()

    migrations = []
    for setting in settings:
        old_value = setting.value_str
        new_value = remap_value(old_value)

        # Skip if no migration needed
        if new_value is None:
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
        description="Migrate ExpertSetting model strings off EOL models (2026-08 registry refresh)"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the migrations (default is dry run)"
    )
    args = parser.parse_args()

    print("\n" + "="*80)
    print("EOL Model Migration Script (2026-08 registry refresh)")
    print("="*80)
    print("\nRemap rules (first match wins):")
    for pattern, replacement in REMAP:
        print(f"  - /{pattern}/ -> {replacement}")
    print()

    # Point the (lazy) package engine at the live trade DB — same wiring
    # wire_all_seams() does at app startup. DB_FILE env var still overrides.
    configure_db(config.DB_FILE)
    print(f"Database: {config.DB_FILE}")

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

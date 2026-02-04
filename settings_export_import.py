"""
Settings Export/Import Script for BA2 Trade Platform

Exports and imports AppSettings, AccountDefinitions (with AccountSettings),
and ExpertInstances (with ExpertSettings) to/from JSON files.

On import, existing settings are merged/overwritten by key.

Usage:
    # Export all settings
    python settings_export_import.py export settings_backup.json

    # Export only app settings
    python settings_export_import.py export settings_backup.json --only app

    # Export only accounts (with their settings)
    python settings_export_import.py export settings_backup.json --only accounts

    # Export only experts (with their settings)
    python settings_export_import.py export settings_backup.json --only experts

    # Import all settings (merge/overwrite)
    python settings_export_import.py import settings_backup.json

    # Import with --dry-run to preview changes
    python settings_export_import.py import settings_backup.json --dry-run
"""

import argparse
import json
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime
from typing import Dict, Any, List, Optional

from sqlmodel import select, Session
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import (
    AppSetting,
    AccountDefinition,
    AccountSetting,
    ExpertInstance,
    ExpertSetting,
)


def _setting_to_dict(setting) -> Dict[str, Any]:
    """Convert a setting record (AppSetting/AccountSetting/ExpertSetting) to a dict."""
    return {
        "key": setting.key,
        "value_str": setting.value_str,
        "value_json": setting.value_json,
        "value_float": setting.value_float,
    }


# ─── EXPORT ─────────────────────────────────────────────────────────

def export_app_settings(session: Session) -> List[Dict[str, Any]]:
    """Export all AppSetting records."""
    settings = session.exec(select(AppSetting)).all()
    return [_setting_to_dict(s) for s in settings]


def export_accounts(session: Session) -> List[Dict[str, Any]]:
    """Export all AccountDefinitions with their AccountSettings."""
    accounts = session.exec(select(AccountDefinition)).all()
    result = []
    for account in accounts:
        # Get settings for this account
        stmt = select(AccountSetting).where(AccountSetting.account_id == account.id)
        account_settings = session.exec(stmt).all()

        result.append({
            "name": account.name,
            "provider": account.provider,
            "description": account.description,
            "settings": [_setting_to_dict(s) for s in account_settings],
        })
    return result


def export_experts(session: Session) -> List[Dict[str, Any]]:
    """Export all ExpertInstances with their ExpertSettings."""
    experts = session.exec(select(ExpertInstance)).all()

    # Pre-fetch account names for display
    accounts = session.exec(select(AccountDefinition)).all()
    account_map = {a.id: a.name for a in accounts}

    result = []
    for expert in experts:
        # Get settings for this expert
        stmt = select(ExpertSetting).where(ExpertSetting.instance_id == expert.id)
        expert_settings = session.exec(stmt).all()

        result.append({
            "expert": expert.expert,
            "account_name": account_map.get(expert.account_id, f"unknown-{expert.account_id}"),
            "enabled": expert.enabled,
            "alias": expert.alias,
            "user_description": expert.user_description,
            "virtual_equity_pct": expert.virtual_equity_pct,
            "settings": [_setting_to_dict(s) for s in expert_settings],
        })
    return result


def do_export(filepath: str, only: Optional[str] = None):
    """Export settings to a JSON file."""
    with get_db() as session:
        data = {
            "export_version": "1.0",
            "export_type": "settings",
            "export_timestamp": datetime.now().isoformat(),
        }

        if only is None or only == "app":
            data["app_settings"] = export_app_settings(session)
        if only is None or only == "accounts":
            data["accounts"] = export_accounts(session)
        if only is None or only == "experts":
            data["experts"] = export_experts(session)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)

    # Print summary
    print(f"Exported to {filepath}:")
    if "app_settings" in data:
        print(f"  App settings:  {len(data['app_settings'])}")
    if "accounts" in data:
        total_acc_settings = sum(len(a["settings"]) for a in data["accounts"])
        print(f"  Accounts:      {len(data['accounts'])} ({total_acc_settings} settings)")
    if "experts" in data:
        total_exp_settings = sum(len(e["settings"]) for e in data["experts"])
        print(f"  Experts:       {len(data['experts'])} ({total_exp_settings} settings)")


# ─── IMPORT ─────────────────────────────────────────────────────────

def _upsert_setting(session: Session, model_class, lookup_filters: dict, setting_data: Dict[str, Any]):
    """Insert or update a setting record by key + parent ID.

    Args:
        session: DB session
        model_class: AppSetting, AccountSetting, or ExpertSetting
        lookup_filters: dict of filter columns (e.g. {"key": "x", "account_id": 1})
        setting_data: dict with value_str, value_json, value_float
    """
    stmt = select(model_class)
    for col, val in lookup_filters.items():
        stmt = stmt.where(getattr(model_class, col) == val)
    existing = session.exec(stmt).first()

    if existing:
        existing.value_str = setting_data["value_str"]
        existing.value_json = setting_data["value_json"]
        existing.value_float = setting_data["value_float"]
        session.add(existing)
        return "updated"
    else:
        new_record = model_class(**lookup_filters, **{
            "value_str": setting_data["value_str"],
            "value_json": setting_data["value_json"],
            "value_float": setting_data["value_float"],
        })
        session.add(new_record)
        return "created"


def import_app_settings(session: Session, settings_list: List[Dict], dry_run: bool) -> Dict[str, int]:
    """Import AppSettings. Merges/overwrites by key."""
    stats = {"created": 0, "updated": 0, "skipped": 0}
    for s in settings_list:
        if dry_run:
            existing = session.exec(
                select(AppSetting).where(AppSetting.key == s["key"])
            ).first()
            action = "update" if existing else "create"
            print(f"  [DRY RUN] Would {action} app setting: {s['key']}")
            stats["updated" if existing else "created"] += 1
        else:
            result = _upsert_setting(session, AppSetting, {"key": s["key"]}, s)
            stats[result] += 1
    return stats


def import_accounts(session: Session, accounts_list: List[Dict], dry_run: bool) -> Dict[str, int]:
    """Import AccountDefinitions + AccountSettings. Matches accounts by name+provider."""
    stats = {"accounts_created": 0, "accounts_existing": 0, "settings_created": 0, "settings_updated": 0}

    for acc_data in accounts_list:
        # Find existing account by name + provider
        existing_account = session.exec(
            select(AccountDefinition).where(
                AccountDefinition.name == acc_data["name"],
                AccountDefinition.provider == acc_data["provider"],
            )
        ).first()

        if existing_account:
            account_id = existing_account.id
            # Update description if provided
            if acc_data.get("description") is not None:
                existing_account.description = acc_data["description"]
                session.add(existing_account)
            stats["accounts_existing"] += 1
            if dry_run:
                print(f"  [DRY RUN] Account exists: {acc_data['name']} ({acc_data['provider']})")
        else:
            if dry_run:
                print(f"  [DRY RUN] Would create account: {acc_data['name']} ({acc_data['provider']})")
                stats["accounts_created"] += 1
                # Skip settings for new accounts in dry-run (no ID)
                for s in acc_data.get("settings", []):
                    print(f"    [DRY RUN] Would create setting: {s['key']}")
                    stats["settings_created"] += 1
                continue
            else:
                new_account = AccountDefinition(
                    name=acc_data["name"],
                    provider=acc_data["provider"],
                    description=acc_data.get("description"),
                )
                session.add(new_account)
                session.flush()  # Get the ID
                account_id = new_account.id
                stats["accounts_created"] += 1

        # Import settings for this account
        for s in acc_data.get("settings", []):
            if dry_run:
                existing_setting = session.exec(
                    select(AccountSetting).where(
                        AccountSetting.account_id == account_id,
                        AccountSetting.key == s["key"],
                    )
                ).first()
                action = "update" if existing_setting else "create"
                print(f"    [DRY RUN] Would {action} setting: {s['key']}")
                stats[f"settings_{action}d"] += 1
            else:
                result = _upsert_setting(
                    session, AccountSetting,
                    {"account_id": account_id, "key": s["key"]},
                    s,
                )
                stats[f"settings_{result}"] += 1

    return stats


def import_experts(session: Session, experts_list: List[Dict], dry_run: bool) -> Dict[str, int]:
    """Import ExpertInstances + ExpertSettings. Matches by expert type + account name + alias."""
    stats = {"experts_created": 0, "experts_existing": 0, "settings_created": 0, "settings_updated": 0}

    # Build account name -> ID map
    accounts = session.exec(select(AccountDefinition)).all()
    account_name_map = {a.name: a.id for a in accounts}

    for exp_data in experts_list:
        account_name = exp_data.get("account_name", "")
        account_id = account_name_map.get(account_name)

        if account_id is None:
            print(f"  WARNING: Account '{account_name}' not found, skipping expert '{exp_data['expert']}' (alias: {exp_data.get('alias')})")
            continue

        # Find existing expert by expert type + account_id + alias
        stmt = select(ExpertInstance).where(
            ExpertInstance.expert == exp_data["expert"],
            ExpertInstance.account_id == account_id,
        )
        if exp_data.get("alias"):
            stmt = stmt.where(ExpertInstance.alias == exp_data["alias"])

        existing_expert = session.exec(stmt).first()

        if existing_expert:
            expert_id = existing_expert.id
            # Update fields
            existing_expert.enabled = exp_data.get("enabled", existing_expert.enabled)
            existing_expert.alias = exp_data.get("alias", existing_expert.alias)
            existing_expert.user_description = exp_data.get("user_description", existing_expert.user_description)
            existing_expert.virtual_equity_pct = exp_data.get("virtual_equity_pct", existing_expert.virtual_equity_pct)
            session.add(existing_expert)
            stats["experts_existing"] += 1
            if dry_run:
                print(f"  [DRY RUN] Expert exists: {exp_data['expert']} (alias: {exp_data.get('alias')}, account: {account_name})")
        else:
            if dry_run:
                print(f"  [DRY RUN] Would create expert: {exp_data['expert']} (alias: {exp_data.get('alias')}, account: {account_name})")
                stats["experts_created"] += 1
                for s in exp_data.get("settings", []):
                    print(f"    [DRY RUN] Would create setting: {s['key']}")
                    stats["settings_created"] += 1
                continue
            else:
                new_expert = ExpertInstance(
                    expert=exp_data["expert"],
                    account_id=account_id,
                    enabled=exp_data.get("enabled", True),
                    alias=exp_data.get("alias"),
                    user_description=exp_data.get("user_description"),
                    virtual_equity_pct=exp_data.get("virtual_equity_pct", 100.0),
                )
                session.add(new_expert)
                session.flush()
                expert_id = new_expert.id
                stats["experts_created"] += 1

        # Import settings for this expert
        for s in exp_data.get("settings", []):
            if dry_run:
                existing_setting = session.exec(
                    select(ExpertSetting).where(
                        ExpertSetting.instance_id == expert_id,
                        ExpertSetting.key == s["key"],
                    )
                ).first()
                action = "update" if existing_setting else "create"
                print(f"    [DRY RUN] Would {action} setting: {s['key']}")
                stats[f"settings_{action}d"] += 1
            else:
                result = _upsert_setting(
                    session, ExpertSetting,
                    {"instance_id": expert_id, "key": s["key"]},
                    s,
                )
                stats[f"settings_{result}"] += 1

    return stats


def do_import(filepath: str, dry_run: bool = False):
    """Import settings from a JSON file. Merges/overwrites existing settings."""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    version = data.get("export_version", "unknown")
    timestamp = data.get("export_timestamp", "unknown")
    print(f"Importing from {filepath} (version: {version}, exported: {timestamp})")
    if dry_run:
        print("=== DRY RUN MODE - no changes will be made ===\n")

    with get_db() as session:
        # Import app settings
        if "app_settings" in data:
            print(f"\n--- App Settings ({len(data['app_settings'])} entries) ---")
            stats = import_app_settings(session, data["app_settings"], dry_run)
            print(f"  Result: {stats}")

        # Import accounts
        if "accounts" in data:
            print(f"\n--- Accounts ({len(data['accounts'])} entries) ---")
            stats = import_accounts(session, data["accounts"], dry_run)
            print(f"  Result: {stats}")

        # Import experts
        if "experts" in data:
            print(f"\n--- Experts ({len(data['experts'])} entries) ---")
            stats = import_experts(session, data["experts"], dry_run)
            print(f"  Result: {stats}")

        if not dry_run:
            session.commit()
            print("\nAll changes committed.")
        else:
            print("\n=== DRY RUN COMPLETE - no changes were made ===")


# ─── CLI ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Export/Import BA2 Trade Platform settings"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Export command
    export_parser = subparsers.add_parser("export", help="Export settings to JSON")
    export_parser.add_argument("file", help="Output JSON file path")
    export_parser.add_argument(
        "--only",
        choices=["app", "accounts", "experts"],
        help="Export only a specific section",
    )

    # Import command
    import_parser = subparsers.add_parser("import", help="Import settings from JSON")
    import_parser.add_argument("file", help="Input JSON file path")
    import_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without applying them",
    )

    args = parser.parse_args()

    if args.command == "export":
        do_export(args.file, only=args.only)
    elif args.command == "import":
        if not os.path.exists(args.file):
            print(f"Error: File not found: {args.file}")
            sys.exit(1)
        do_import(args.file, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

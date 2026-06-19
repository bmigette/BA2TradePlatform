"""Expert settings export/import — shared by both platforms (Amendment A3).

Extracted from BA2TradePlatform/settings_export_import.py (the expert portion:
``export_experts`` @87, ``import_experts`` @258, plus the shared
``_setting_to_dict`` @51 and ``_upsert_setting`` @146 helpers). The app-settings
and accounts portions stay in BA2TradePlatform.

Public API (parameterized by a SQLModel ``session``; nothing here opens a DB):
    export_expert_settings(session, expert_ids=None) -> list[dict]
    import_expert_settings(session, experts_list, dry_run=False) -> dict

Expert classes are resolved via ``ba2_experts.get_expert_class`` (the package
registry — NOT the live BA2TradePlatform registry), so an exported config whose
expert type this package does not know is skipped rather than silently created.

Both BA2TradePlatform (Phase 6 wires its UI/CLI to this) and BA2TestPlatform
(load an exported expert config into a backtest; export optimizer-found params
back) import from ``ba2_experts.settings_io``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from ba2_common.core.models import (
    AccountDefinition,
    ExpertInstance,
    ExpertSetting,
)


# ─── helpers (shared with the live settings_export_import.py) ─────────────────

def _setting_to_dict(setting) -> Dict[str, Any]:
    """Convert a setting record (ExpertSetting) to a plain dict."""
    return {
        "key": setting.key,
        "value_str": setting.value_str,
        "value_json": setting.value_json,
        "value_float": setting.value_float,
    }


def _upsert_setting(session: Session, model_class, lookup_filters: dict,
                    setting_data: Dict[str, Any]) -> str:
    """Insert or update a setting record by key + parent ID.

    Args:
        session: DB session.
        model_class: ExpertSetting (the only setting model this module handles).
        lookup_filters: filter columns, e.g. {"instance_id": 1, "key": "x"}.
        setting_data: dict with value_str, value_json, value_float.

    Returns "updated" or "created".
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


# ─── EXPORT ──────────────────────────────────────────────────────────────────

def export_expert_settings(session: Session,
                           expert_ids: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """Export ExpertInstances (with their ExpertSettings) as a list of dicts.

    Args:
        session: SQLModel session.
        expert_ids: when given, export only these ExpertInstance ids; otherwise
            export all expert instances.

    Returns a list of expert dicts, each:
        {expert, account_name, enabled, alias, user_description,
         virtual_equity_pct, settings: [{key, value_str, value_json, value_float}]}
    The shape is identical to the live ``export_experts`` output so files are
    interchangeable between platforms.
    """
    stmt = select(ExpertInstance)
    if expert_ids is not None:
        stmt = stmt.where(ExpertInstance.id.in_(list(expert_ids)))
    experts = session.exec(stmt).all()

    # Pre-fetch account names for display / matching on import.
    accounts = session.exec(select(AccountDefinition)).all()
    account_map = {a.id: a.name for a in accounts}

    result: List[Dict[str, Any]] = []
    for expert in experts:
        settings_stmt = select(ExpertSetting).where(
            ExpertSetting.instance_id == expert.id)
        expert_settings = session.exec(settings_stmt).all()

        result.append({
            "expert": expert.expert,
            "account_name": account_map.get(
                expert.account_id, f"unknown-{expert.account_id}"),
            "enabled": expert.enabled,
            "alias": expert.alias,
            "user_description": expert.user_description,
            "virtual_equity_pct": expert.virtual_equity_pct,
            "settings": [_setting_to_dict(s) for s in expert_settings],
        })
    return result


# ─── IMPORT ──────────────────────────────────────────────────────────────────

def import_expert_settings(session: Session,
                           experts_list: List[Dict[str, Any]],
                           dry_run: bool = False) -> Dict[str, int]:
    """Import ExpertInstances + ExpertSettings. Matches by expert type + account
    name + alias; existing settings are merged/overwritten by key.

    Expert types are validated against this package's registry
    (``ba2_experts.get_expert_class``): an unknown type is skipped (counted in
    ``experts_skipped``) so a config exported on a platform with extra experts
    does not create bogus rows here.

    Args:
        session: SQLModel session. The caller commits (this function does not),
            so a dry_run leaves the session untouched.
        experts_list: the list produced by ``export_expert_settings``.
        dry_run: when True, no records are written; the stats reflect what
            *would* happen.

    Returns a stats dict:
        {experts_created, experts_existing, experts_skipped,
         settings_created, settings_updated}
    """
    # Resolve via the package registry (not the live BA2TradePlatform registry).
    from ba2_experts import get_expert_class

    stats = {
        "experts_created": 0,
        "experts_existing": 0,
        "experts_skipped": 0,
        "settings_created": 0,
        "settings_updated": 0,
    }

    # Build account name -> ID map.
    accounts = session.exec(select(AccountDefinition)).all()
    account_name_map = {a.name: a.id for a in accounts}

    for exp_data in experts_list:
        expert_type = exp_data["expert"]

        # Validate the expert type against this package's registry.
        if get_expert_class(expert_type) is None:
            stats["experts_skipped"] += 1
            continue

        account_name = exp_data.get("account_name", "")
        account_id = account_name_map.get(account_name)
        if account_id is None:
            stats["experts_skipped"] += 1
            continue

        # Find existing expert by expert type + account_id (+ alias if present).
        stmt = select(ExpertInstance).where(
            ExpertInstance.expert == expert_type,
            ExpertInstance.account_id == account_id,
        )
        if exp_data.get("alias"):
            stmt = stmt.where(ExpertInstance.alias == exp_data["alias"])
        existing_expert = session.exec(stmt).first()

        if existing_expert:
            expert_id = existing_expert.id
            stats["experts_existing"] += 1
            if not dry_run:
                existing_expert.enabled = exp_data.get(
                    "enabled", existing_expert.enabled)
                existing_expert.alias = exp_data.get(
                    "alias", existing_expert.alias)
                existing_expert.user_description = exp_data.get(
                    "user_description", existing_expert.user_description)
                existing_expert.virtual_equity_pct = exp_data.get(
                    "virtual_equity_pct", existing_expert.virtual_equity_pct)
                session.add(existing_expert)
        else:
            if dry_run:
                stats["experts_created"] += 1
                # No ID available in dry-run; count the settings that would be made.
                stats["settings_created"] += len(exp_data.get("settings", []))
                continue
            new_expert = ExpertInstance(
                expert=expert_type,
                account_id=account_id,
                enabled=exp_data.get("enabled", True),
                alias=exp_data.get("alias"),
                user_description=exp_data.get("user_description"),
                virtual_equity_pct=exp_data.get("virtual_equity_pct", 100.0),
            )
            session.add(new_expert)
            session.flush()  # assign the primary key
            expert_id = new_expert.id
            stats["experts_created"] += 1

        # Import settings for this expert.
        for s in exp_data.get("settings", []):
            if dry_run:
                existing_setting = session.exec(
                    select(ExpertSetting).where(
                        ExpertSetting.instance_id == expert_id,
                        ExpertSetting.key == s["key"],
                    )
                ).first()
                stats["settings_updated" if existing_setting else "settings_created"] += 1
            else:
                result = _upsert_setting(
                    session, ExpertSetting,
                    {"instance_id": expert_id, "key": s["key"]},
                    s,
                )
                stats[f"settings_{result}"] += 1

    return stats

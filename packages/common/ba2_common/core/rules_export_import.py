"""
Export/Import functionality for trading rules and rulesets.

This module provides functionality to export rules and rulesets to JSON files
and import them back into the system.
"""

import json
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
from sqlmodel import select, Session

from ba2_common.core.models import Ruleset, EventAction, RulesetEventActionLink
from ba2_common.core.db import get_db, get_all_instances, add_instance, get_instance
from ba2_common.logger import logger


import re as _re

# Short, readable tags for the auto-generated rule name (fallback for UNNAMED rules only — a real
# hand-crafted name like "BUY_Longterm_70pctConfidence_10pctProfit" is always kept). A trigger's
# ``event_type`` value IS the field name (e.g. "days_opened", "confidence"), so we tag off it.
_FIELD_ABBR: Dict[str, str] = {
    "days_opened": "openD",
    "confidence": "conf",
    "expected_profit_target_percent": "profit",
    "profit_loss_percent": "pl",
    "profit_loss_amount": "plAmt",
    "percent_to_current_target": "toTgt",
    "new_target_percent": "newTgt",
    "days_since_last_close": "sinceClose",
    "days_since_last_profitable_close": "sinceWin",
    "days_since_last_losing_close": "sinceLoss",
    "iv_rank": "ivRank",
    "days_to_earnings": "toEarn",
    "percent_below_recent_high": "belowHigh",
    "percent_above_recent_low": "aboveLow",
    "instrument_account_share": "acctShare",
    "bullish": "bull", "bearish": "bear",
    "has_position": "hasPos", "has_no_position": "noPos",
    "has_buy_position": "buyPos", "has_sell_position": "sellPos",
    "short_term": "short", "medium_term": "med", "long_term": "long",
    "highrisk": "hiRisk", "mediumrisk": "medRisk", "lowrisk": "lowRisk",
    "current_rating_positive": "ratingPos", "current_rating_negative": "ratingNeg",
    "current_rating_neutral": "ratingNeu",
    "new_target_higher": "tgtUp", "new_target_lower": "tgtDown",
}
_OP_TOKEN: Dict[str, str] = {
    ">": "gt", ">=": "gte", "<": "lt", "<=": "lte", "==": "eq", "!=": "ne",
    "gt": "gt", "gte": "gte", "lt": "lt", "lte": "lte", "eq": "eq", "ne": "ne", "neq": "ne",
}


def _abbr_field(field: str) -> str:
    """Short tag for a field. Curated where we have one, else a compact camelCase of the words."""
    if field in _FIELD_ABBR:
        return _FIELD_ABBR[field]
    parts = [p for p in str(field).split("_") if p]
    if not parts:
        return "x"
    camel = parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])
    return camel[:12]


def _fmt_value(v: Any) -> str:
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else f"{f:g}"
    except (TypeError, ValueError):
        return str(v)


def generate_rule_name(triggers: Dict[str, Any], actions: Optional[Dict[str, Any]] = None,
                       max_len: int = 40) -> str:
    """Build a readable, content-derived name from a rule's triggers (conditions are ANDed).

    Numeric condition -> ``<abbr>_<op>_<value>`` (e.g. ``openD_gt_10``, ``conf_gte_70``); flag
    condition -> just its tag (e.g. ``bull``, ``ratingNeg``). Tokens are joined with ``_`` and the
    whole thing is capped at ``max_len`` so it fits the UI — overflow keeps the leading tokens and
    appends ``_+Nmore``. Deterministic (triggers iterated by key). Used as a FALLBACK only, for
    rules with no/generic name."""
    toks: List[str] = []
    for key in sorted((triggers or {}).keys()):
        cond = triggers[key]
        if not isinstance(cond, dict):
            continue
        et = cond.get("event_type")
        if not et:
            continue
        tag = _abbr_field(str(et))
        op, val = cond.get("operator"), cond.get("value")
        if op is not None and val is not None:
            toks.append(f"{tag}_{_OP_TOKEN.get(str(op).strip().lower(), str(op))}_{_fmt_value(val)}")
        else:
            toks.append(tag)
    if not toks:
        return "rule"
    full = "_".join(toks)
    if len(full) <= max_len:
        return full
    kept: List[str] = []
    for t in toks:
        if kept and len("_".join(kept + [t])) + len("_+99more") > max_len:
            break
        kept.append(t)
    more = len(toks) - len(kept)
    return "_".join(kept) + (f"_+{more}more" if more else "")


def _is_generic_rule_name(name: Any) -> bool:
    """True when a rule has no meaningful name (so we substitute a generated one on export).
    Hand-crafted names (e.g. ``BUY_Longterm_70pctConfidence_10pctProfit``) are NOT generic."""
    if not name or not str(name).strip():
        return True
    s = str(name).strip().lower()
    return bool(_re.match(r"^(cond_\d+|rule([\s_-]?\d+)?|new rule|untitled|unnamed|eventaction.*|\d+)$", s))


def _display_rule_name(rule: "EventAction") -> str:
    """The rule's name for export: its own name, or a generated readable one if that's missing/generic."""
    if _is_generic_rule_name(rule.name):
        return generate_rule_name(rule.triggers, rule.actions)
    return rule.name


class RulesExporter:
    """Handles exporting rules and rulesets to JSON format."""

    @staticmethod
    def export_ruleset(ruleset_id: int) -> Dict[str, Any]:
        """Export a single ruleset with all its rules."""
        try:
            with get_db() as session:
                # Get ruleset with rules
                statement = select(Ruleset).where(Ruleset.id == ruleset_id)
                ruleset = session.exec(statement).first()

                if not ruleset:
                    raise ValueError(f"Ruleset with ID {ruleset_id} not found")

                # Get all event actions for this ruleset with their order
                ruleset_data = {
                    "export_version": "1.0",
                    "export_type": "ruleset",
                    "export_timestamp": datetime.now().isoformat(),
                    "ruleset": {
                        "name": ruleset.name,
                        "description": ruleset.description,
                        "type": ruleset.type.value if ruleset.type else None,
                        "subtype": ruleset.subtype.value if ruleset.subtype else None,
                        "rules": []
                    }
                }

                # Get rules in order by querying the link table
                from ba2_common.core.models import RulesetEventActionLink, EventAction
                
                with get_db() as session:
                    # Query link table to get ordered associations
                    statement = (
                        select(RulesetEventActionLink, EventAction)
                        .join(EventAction, RulesetEventActionLink.eventaction_id == EventAction.id)
                        .where(RulesetEventActionLink.ruleset_id == ruleset_id)
                        .order_by(RulesetEventActionLink.order_index)
                    )
                    
                    results = session.exec(statement).all()
                    
                    for link, rule in results:
                        rule_data = {
                            "name": _display_rule_name(rule),
                            "type": rule.type.value if rule.type else None,
                            "subtype": rule.subtype.value if rule.subtype else None,
                            "triggers": rule.triggers,
                            "actions": rule.actions,
                            "extra_parameters": rule.extra_parameters,
                            "continue_processing": rule.continue_processing,
                            "order_index": link.order_index
                        }
                        ruleset_data["ruleset"]["rules"].append(rule_data)

                return ruleset_data

        except Exception as e:
            logger.error(f"Error exporting ruleset {ruleset_id}: {e}", exc_info=True)
            raise

    @staticmethod
    def export_rule(rule_id: int) -> Dict[str, Any]:
        """Export a single rule."""
        try:
            rule = get_instance(EventAction, rule_id)
            if not rule:
                raise ValueError(f"Rule with ID {rule_id} not found")

            rule_data = {
                "export_version": "1.0",
                "export_type": "rule",
                "export_timestamp": datetime.now().isoformat(),
                "rule": {
                    "name": _display_rule_name(rule),
                    "type": rule.type.value if rule.type else None,
                    "subtype": rule.subtype.value if rule.subtype else None,
                    "triggers": rule.triggers,
                    "actions": rule.actions,
                    "extra_parameters": rule.extra_parameters,
                    "continue_processing": rule.continue_processing
                }
            }

            return rule_data

        except Exception as e:
            logger.error(f"Error exporting rule {rule_id}: {e}", exc_info=True)
            raise

    @staticmethod
    def export_multiple_rulesets(ruleset_ids: List[int]) -> Dict[str, Any]:
        """Export multiple rulesets."""
        try:
            rulesets_data = {
                "export_version": "1.0",
                "export_type": "rulesets",
                "export_timestamp": datetime.now().isoformat(),
                "rulesets": []
            }

            for ruleset_id in ruleset_ids:
                ruleset_data = RulesExporter.export_ruleset(ruleset_id)
                rulesets_data["rulesets"].append(ruleset_data["ruleset"])

            return rulesets_data

        except Exception as e:
            logger.error(f"Error exporting multiple rulesets: {e}", exc_info=True)
            raise

    @staticmethod
    def export_multiple_rules(rule_ids: List[int]) -> Dict[str, Any]:
        """Export multiple rules."""
        try:
            rules_data = {
                "export_version": "1.0",
                "export_type": "rules",
                "export_timestamp": datetime.now().isoformat(),
                "rules": []
            }

            for rule_id in rule_ids:
                rule_data = RulesExporter.export_rule(rule_id)
                rules_data["rules"].append(rule_data["rule"])

            return rules_data

        except Exception as e:
            logger.error(f"Error exporting multiple rules: {e}", exc_info=True)
            raise


def _rule_content_key(type_, subtype, triggers, actions, extra_parameters, continue_processing) -> str:
    """Canonical, comparable signature of a rule's CONTENT (everything but its name/id).

    Used to decide, on import, whether a same-named rule is truly identical (reuse it) or a
    different rule that merely collides on name (import under a fresh name instead of silently
    reusing the wrong one). Enums are reduced to their ``.value`` so a stored EventAction (enum
    type/subtype) compares equal to an imported JSON rule (string type/subtype)."""
    def _v(x):
        return getattr(x, "value", x)
    return json.dumps(
        {
            "type": _v(type_),
            "subtype": _v(subtype),
            "triggers": triggers or {},
            "actions": actions or {},
            "extra_parameters": extra_parameters or {},
            "continue_processing": bool(continue_processing),
        },
        sort_keys=True,
        default=str,
    )


def _eventaction_content_key(rule: "EventAction") -> str:
    return _rule_content_key(
        rule.type, rule.subtype, rule.triggers, rule.actions,
        rule.extra_parameters, rule.continue_processing,
    )


class RulesImporter:
    """Handles importing rules and rulesets from JSON format."""

    @staticmethod
    def import_ruleset(ruleset_data: Dict[str, Any], name_suffix: str = "") -> Tuple[int, List[str]]:
        """Import a ruleset from JSON data. Returns (ruleset_id, warnings)."""
        warnings = []

        try:
            with get_db() as session:
                ruleset_info = ruleset_data["ruleset"]
                
                # Preserve original name, but handle duplicates
                base_name = ruleset_info['name']
                final_name = base_name
                
                # Check if name already exists
                counter = 1
                while True:
                    existing = session.exec(
                        select(Ruleset).where(Ruleset.name == final_name)
                    ).first()
                    
                    if not existing:
                        break
                    
                    # Name exists, try with suffix
                    final_name = f"{base_name}-{counter}"
                    counter += 1
                    warnings.append(f"Ruleset name '{base_name}' already exists, renamed to '{final_name}'")

                # Create ruleset with unique name
                ruleset = Ruleset(
                    name=final_name,
                    description=ruleset_info.get('description'),
                    type=ruleset_info.get('type'),
                    subtype=ruleset_info.get('subtype')
                )

                session.add(ruleset)
                session.flush()  # Get the ID

                # Track rules already processed in this import to avoid duplicate warnings
                processed_rule_ids = set()

                # Import rules and create links
                for rule_data in ruleset_info["rules"]:
                    rule_id, rule_warnings = RulesImporter._import_rule_to_session(
                        session, rule_data, ""  # Don't add suffix to individual rules
                    )
                    
                    # Only add warnings for rules we haven't seen yet in this import
                    if rule_id not in processed_rule_ids:
                        warnings.extend(rule_warnings)
                        processed_rule_ids.add(rule_id)

                    # Create link
                    link = RulesetEventActionLink(
                        ruleset_id=ruleset.id,
                        eventaction_id=rule_id,
                        order_index=rule_data.get('order_index', 0)
                    )
                    session.add(link)

                session.commit()
                return ruleset.id, warnings

        except Exception as e:
            logger.error(f"Error importing ruleset: {e}", exc_info=True)
            raise

    @staticmethod
    def import_rule(rule_data: Dict[str, Any], name_suffix: str = "") -> Tuple[int, List[str]]:
        """Import a single rule from JSON data. Returns (rule_id, warnings)."""
        try:
            with get_db() as session:
                rule_id, warnings = RulesImporter._import_rule_to_session(
                    session, rule_data["rule"], ""  # Don't add suffix
                )
                session.commit()
                return rule_id, warnings

        except Exception as e:
            logger.error(f"Error importing rule: {e}", exc_info=True)
            raise

    @staticmethod
    def import_multiple_rulesets(rulesets_data: Dict[str, Any], name_suffix: str = "") -> Tuple[List[int], List[str]]:
        """Import multiple rulesets, reusing rules across rulesets. Returns (ruleset_ids, warnings)."""
        ruleset_ids = []
        all_warnings = []
        processed_rule_names = {}  # Map rule names to IDs to avoid duplicates across rulesets

        try:
            with get_db() as session:
                for ruleset_data in rulesets_data["rulesets"]:
                    ruleset_info = ruleset_data
                    warnings = []
                    
                    # Preserve original name, but handle duplicates
                    base_name = ruleset_info['name']
                    final_name = base_name
                    
                    # Check if name already exists
                    counter = 1
                    while True:
                        existing = session.exec(
                            select(Ruleset).where(Ruleset.name == final_name)
                        ).first()
                        
                        if not existing:
                            break
                        
                        # Name exists, try with suffix
                        final_name = f"{base_name}-{counter}"
                        counter += 1
                        warnings.append(f"Ruleset name '{base_name}' already exists, renamed to '{final_name}'")

                    # Create ruleset with unique name
                    ruleset = Ruleset(
                        name=final_name,
                        description=ruleset_info.get('description'),
                        type=ruleset_info.get('type'),
                        subtype=ruleset_info.get('subtype')
                    )

                    session.add(ruleset)
                    session.flush()  # Get the ID
                    ruleset_ids.append(ruleset.id)

                    # Import rules and create links, reusing rules across rulesets
                    for rule_data in ruleset_info["rules"]:
                        rule_name = f"{rule_data['name']}{name_suffix}"
                        
                        # Check if we already processed this rule in current import batch
                        if rule_name in processed_rule_names:
                            rule_id = processed_rule_names[rule_name]
                        else:
                            # Import/reuse rule
                            rule_id, rule_warnings = RulesImporter._import_rule_to_session(
                                session, rule_data, name_suffix
                            )
                            processed_rule_names[rule_name] = rule_id
                            warnings.extend(rule_warnings)

                        # Create link
                        link = RulesetEventActionLink(
                            ruleset_id=ruleset.id,
                            eventaction_id=rule_id,
                            order_index=rule_data.get('order_index', 0)
                        )
                        session.add(link)
                    
                    all_warnings.extend(warnings)

                session.commit()
                return ruleset_ids, all_warnings

        except Exception as e:
            logger.error(f"Error importing multiple rulesets: {e}", exc_info=True)
            raise

    @staticmethod
    def import_multiple_rules(rules_data: Dict[str, Any], name_suffix: str = "") -> Tuple[List[int], List[str]]:
        """Import multiple rules. Returns (rule_ids, warnings)."""
        rule_ids = []
        all_warnings = []

        try:
            for rule_data in rules_data["rules"]:
                rule_id, warnings = RulesImporter.import_rule(
                    {"rule": rule_data}, name_suffix
                )
                rule_ids.append(rule_id)
                all_warnings.extend(warnings)

            return rule_ids, all_warnings

        except Exception as e:
            logger.error(f"Error importing multiple rules: {e}", exc_info=True)
            raise

    @staticmethod
    def _import_rule_to_session(session: Session, rule_data: Dict[str, Any], name_suffix: str = "") -> Tuple[int, List[str]]:
        """Import a rule within an existing session. Returns (rule_id, warnings)."""
        warnings = []

        try:
            # Check if rule with same name already exists
            rule_name = f"{rule_data['name']}{name_suffix}"
            incoming_key = _rule_content_key(
                rule_data.get("type"), rule_data.get("subtype"),
                rule_data.get("triggers", {}), rule_data.get("actions", {}),
                rule_data.get("extra_parameters", {}), rule_data.get("continue_processing", False),
            )
            try:
                existing_rule = session.exec(
                    select(EventAction).where(EventAction.name == rule_name)
                ).first()
            except Exception as e:
                # Handle corrupted data in database (e.g., invalid enum values from old tests)
                logger.debug(f"Error checking for existing rule '{rule_name}': {e}")
                existing_rule = None

            if existing_rule:
                # CONTENT-AWARE dedup: a same-named rule whose content is IDENTICAL is reused
                # (a true duplicate — skip). A same-named rule with DIFFERENT content is a
                # genuinely different rule that merely collides on name; reusing it would wire the
                # ruleset to the wrong logic and silently drop the imported one. So import the
                # incoming rule under a fresh, unique name (`<name>-1`, `-2`, ...) and return THAT
                # id, so the caller links the ruleset to the imported variant. If an earlier
                # import already created an identical-content variant, reuse it (idempotent
                # re-import — no -1,-2,-3 pile-up).
                if _eventaction_content_key(existing_rule) == incoming_key:
                    warnings.append(
                        f"Rule '{rule_name}' already exists with identical content, reusing (ID: {existing_rule.id})"
                    )
                    return existing_rule.id, warnings
                base_name = rule_name
                counter = 1
                while True:
                    candidate = f"{base_name}-{counter}"
                    try:
                        clash = session.exec(
                            select(EventAction).where(EventAction.name == candidate)
                        ).first()
                    except Exception:  # noqa: BLE001 — treat an unreadable row as a free slot
                        clash = None
                    if clash is None:
                        rule_name = candidate
                        warnings.append(
                            f"Rule '{base_name}' exists with DIFFERENT content; imported as '{candidate}'"
                        )
                        break
                    if _eventaction_content_key(clash) == incoming_key:
                        warnings.append(
                            f"Rule '{base_name}' matches existing variant '{candidate}', reusing (ID: {clash.id})"
                        )
                        return clash.id, warnings
                    counter += 1

            # Create new rule
            rule = EventAction(
                name=rule_name,
                type=rule_data.get('type'),
                subtype=rule_data.get('subtype'),
                triggers=rule_data.get('triggers', {}),
                actions=rule_data.get('actions', {}),
                extra_parameters=rule_data.get('extra_parameters', {}),
                continue_processing=rule_data.get('continue_processing', False)
            )

            session.add(rule)
            session.flush()  # Get the ID

            return rule.id, warnings

        except Exception as e:
            logger.error(f"Error importing rule to session: {e}", exc_info=True)
            raise

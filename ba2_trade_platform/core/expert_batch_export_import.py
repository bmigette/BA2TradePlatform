"""Batch export/import of several experts at once, rules included.

The per-expert Import/Export tab in the edit dialog handles ONE expert and records its rulesets
by NAME ONLY -- which silently assumes the destination already has a ruleset of that name. That
is fine for moving a config between two instances of the same machine and useless for moving a
set of strategies anywhere else.

This module carries the rules themselves, so an import can rebuild them, and does a whole
selection in one file.

Two halves, deliberately split so the UI can show what an import WILL do before it does any of
it (a batch import writes to the live trading database; there is no undo):

    build_batch_export(instance_ids)  -> payload dict          (reads only)
    plan_batch_import(payload)        -> BatchImportPlan        (reads only)
    apply_batch_import(plan)          -> list[str] of messages  (writes)

``plan_batch_import`` resolves every entry against the database and reports what each one would
create or update; nothing is written until ``apply_batch_import`` is called with that plan.

Import semantics, chosen 2026-09-17:
  * An entry is matched to an existing expert by ALIAS. Match -> update in place; no match ->
    create. (An alias is what the deploy tooling already treats as an expert's identity.)
  * A ruleset is matched by NAME: match -> reuse that row and replace its rules; no match ->
    create it. Reuse is the point -- it keeps a re-import from leaving ``foo-1``, ``foo-2``
    copies behind, and every other expert pointing at that ruleset follows the update.
  * ``enabled`` is NEVER turned on by an import. A created expert starts disabled and an
    updated one keeps whatever it currently is, so restoring a config can never start trading
    on its own. The file's own ``enabled`` is recorded for the reader and otherwise ignored.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from ba2_common.core.rules_export_import import RulesExporter, RulesImporter

from .db import (InstanceNotFound, add_instance, get_all_instances, get_instance,
                 update_instance)
from .ExpertPriority import priority_from_settings
from .models import ExpertInstance, Ruleset
from .utils import get_expert_instance_from_id
from ..logger import logger

EXPORT_TYPE = "expert_batch"
EXPORT_VERSION = "1.0"

# The two slots an expert can point a ruleset at, and the payload key naming each one.
_RULESET_SLOTS = (
    ("enter_market_ruleset_id", "enter_market_ruleset_name"),
    ("open_positions_ruleset_id", "open_positions_ruleset_name"),
)


# ─── export ─────────────────────────────────────────────────────────────────

def build_batch_export(instance_ids: List[int]) -> Dict[str, Any]:
    """The export payload for the selected experts. Reads only; raises if an id is unknown."""
    experts: List[Dict[str, Any]] = []
    for instance_id in instance_ids:
        try:
            instance = get_instance(ExpertInstance, instance_id)
        except InstanceNotFound as e:
            # get_instance RAISES on a missing row; say which export failed and why.
            raise ValueError(f"Expert instance {instance_id} not found") from e
        experts.append(_export_one(instance))

    return {
        "export_version": EXPORT_VERSION,
        "export_type": EXPORT_TYPE,
        "export_timestamp": datetime.now().isoformat(),
        "experts": experts,
    }


def _export_one(instance: ExpertInstance) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "expert_type": instance.expert,
        "general": {
            "alias": instance.alias or "",
            "user_description": instance.user_description,
            "enabled": instance.enabled,
            "virtual_equity_pct": instance.virtual_equity_pct,
            "priority": getattr(instance, "priority", 1),
            "account_id": instance.account_id,
        },
    }

    # Ruleset NAMES name the slots; the rule CONTENT travels in one `rulesets` block, in the
    # RulesExporter shape so RulesImporter can consume it without a second serialiser.
    ruleset_ids: List[int] = []
    for id_attr, name_key in _RULESET_SLOTS:
        ruleset_id = getattr(instance, id_attr, None)
        name = None
        if ruleset_id:
            try:
                ruleset = get_instance(Ruleset, ruleset_id)
            except InstanceNotFound:
                # A dangling id (its ruleset was deleted) must not fail the whole export --
                # the table itself already tolerates one and shows '(Not found)'.
                logger.warning(f"Expert {instance.id} references missing ruleset {ruleset_id}; "
                               f"exporting the slot as empty")
            else:
                name = ruleset.name
                if ruleset_id not in ruleset_ids:
                    ruleset_ids.append(ruleset_id)
        entry[name_key] = name
    entry["rulesets"] = (RulesExporter.export_multiple_rulesets(ruleset_ids)
                         if ruleset_ids else None)

    expert = get_expert_instance_from_id(instance.id)
    if expert is None:
        raise ValueError(f"Expert instance {instance.id} ({instance.alias}) could not be built; "
                         f"its settings cannot be exported")
    entry["expert_settings"] = dict(expert.settings)
    entry["symbol_settings"] = (expert._get_enabled_instruments_config()
                                if hasattr(expert, "_get_enabled_instruments_config") else {})
    return entry


# ─── import plan ────────────────────────────────────────────────────────────

@dataclass
class PlannedExpert:
    """One entry's resolved intent. ``existing_id`` is None for a create."""
    alias: str
    expert_type: str
    action: str                       # 'create' | 'update' | 'skip'
    existing_id: Optional[int] = None
    settings_changed: int = 0
    ruleset_names: List[str] = field(default_factory=list)
    problem: Optional[str] = None     # set iff action == 'skip'
    entry: Dict[str, Any] = field(default_factory=dict)

    @property
    def describes_a_write(self) -> bool:
        return self.action in ("create", "update")


@dataclass
class BatchImportPlan:
    experts: List[PlannedExpert]

    @property
    def creates(self) -> List[PlannedExpert]:
        return [e for e in self.experts if e.action == "create"]

    @property
    def updates(self) -> List[PlannedExpert]:
        return [e for e in self.experts if e.action == "update"]

    @property
    def skips(self) -> List[PlannedExpert]:
        return [e for e in self.experts if e.action == "skip"]

    @property
    def write_count(self) -> int:
        return len(self.creates) + len(self.updates)


def parse_batch_payload(raw: bytes | str) -> Dict[str, Any]:
    """Decode a batch file, refusing anything that is not one.

    A single-expert export is accepted and wrapped, so the existing per-expert files still
    import here -- but it will carry no rule content (that format only records ruleset names),
    which ``plan_batch_import`` reports rather than discovering halfway through a write.
    """
    payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    if not isinstance(payload, dict):
        raise ValueError("not a settings export: expected a JSON object")

    if payload.get("export_type") == EXPORT_TYPE or isinstance(payload.get("experts"), list):
        if not isinstance(payload.get("experts"), list):
            raise ValueError("batch export has no 'experts' list")
        return payload

    if "expert_type" in payload or "expert_settings" in payload:
        return {"export_version": payload.get("export_version", "1.0"),
                "export_type": EXPORT_TYPE,
                "experts": [payload]}

    raise ValueError("unrecognised file: not a batch export and not a single-expert export")


def plan_batch_import(payload: Dict[str, Any]) -> BatchImportPlan:
    """Resolve every entry against the database WITHOUT writing anything."""
    by_alias = {}
    for instance in get_all_instances(ExpertInstance):
        if instance.alias:
            by_alias[instance.alias] = instance

    planned: List[PlannedExpert] = []
    for entry in payload["experts"]:
        general = entry.get("general") or {}
        alias = (general.get("alias") or "").strip()
        expert_type = entry.get("expert_type")

        if not alias:
            planned.append(PlannedExpert(alias="(no alias)", expert_type=expert_type or "?",
                                         action="skip", problem="entry has no alias to match on",
                                         entry=entry))
            continue
        if not expert_type:
            planned.append(PlannedExpert(alias=alias, expert_type="?", action="skip",
                                         problem="entry has no expert_type", entry=entry))
            continue

        existing = by_alias.get(alias)
        if existing is not None and existing.expert != expert_type:
            planned.append(PlannedExpert(
                alias=alias, expert_type=expert_type, action="skip", existing_id=existing.id,
                problem=f"expert {existing.id} has this alias but is a {existing.expert}, "
                        f"not a {expert_type}",
                entry=entry))
            continue

        ruleset_names = [entry.get(key) for _, key in _RULESET_SLOTS if entry.get(key)]
        settings_changed = _count_setting_changes(existing, entry.get("expert_settings") or {})
        planned.append(PlannedExpert(
            alias=alias, expert_type=expert_type,
            action="update" if existing is not None else "create",
            existing_id=existing.id if existing is not None else None,
            settings_changed=settings_changed,
            ruleset_names=ruleset_names,
            entry=entry))

    return BatchImportPlan(experts=planned)


def _count_setting_changes(existing: Optional[ExpertInstance], incoming: Dict[str, Any]) -> int:
    """How many settings this entry would change. Best effort: a preview, never a gate."""
    if existing is None:
        return len(incoming)
    try:
        expert = get_expert_instance_from_id(existing.id)
        current = dict(expert.settings) if expert else {}
    except Exception as e:  # noqa: BLE001 -- a preview must not fail the dialog
        logger.warning(f"Could not read settings of expert {existing.id} for the diff: {e}")
        return len(incoming)
    return sum(1 for k, v in incoming.items() if current.get(k) != v)


# ─── import apply ───────────────────────────────────────────────────────────

def apply_batch_import(plan: BatchImportPlan) -> List[str]:
    """Write the plan. Returns one message per expert; raises only on a total failure.

    Each expert is applied independently: one bad entry is reported and the rest still land,
    because a half-applied batch the operator cannot see is worse than a reported failure.
    """
    messages: List[str] = []
    for planned in plan.experts:
        if not planned.describes_a_write:
            messages.append(f"SKIP {planned.alias}: {planned.problem}")
            continue
        try:
            messages.extend(_apply_one(planned))
        except Exception as e:  # noqa: BLE001 -- see docstring
            logger.error(f"Batch import failed for {planned.alias}: {e}", exc_info=True)
            messages.append(f"FAILED {planned.alias}: {e}")
    return messages


def _apply_one(planned: PlannedExpert) -> List[str]:
    entry = planned.entry
    general = entry.get("general") or {}
    messages: List[str] = []

    ruleset_ids_by_name = _import_rulesets(entry, messages)

    def _slot(name_key: str) -> Optional[int]:
        name = entry.get(name_key)
        if not name:
            return None
        resolved = ruleset_ids_by_name.get(name)
        if resolved is None:
            # No rule content in the file (old single-expert format): fall back to a name
            # lookup, and say so if there is nothing to point at.
            existing = _find_ruleset_by_name(name)
            if existing is None:
                messages.append(f"  ruleset '{name}' not in the file and not in this database; "
                                f"left unset")
                return None
            resolved = existing
            messages.append(f"  ruleset '{name}' taken from this database (id {resolved}); "
                            f"the file carried no rules for it")
        return resolved

    enter_id = _slot("enter_market_ruleset_name")
    open_id = _slot("open_positions_ruleset_name")

    if planned.action == "create":
        instance = ExpertInstance(
            expert=planned.expert_type,
            account_id=int(general.get("account_id") or 1),
            alias=planned.alias,
            user_description=general.get("user_description") or "",
            # NEVER enabled by an import -- see the module docstring.
            enabled=False,
            virtual_equity_pct=float(general.get("virtual_equity_pct") or 100.0),
            priority=priority_from_settings(general),
            enter_market_ruleset_id=enter_id,
            open_positions_ruleset_id=open_id,
        )
        instance_id = add_instance(instance)
        messages.append(f"CREATED expert {instance_id} '{planned.alias}' (disabled)")
    else:
        try:
            instance = get_instance(ExpertInstance, planned.existing_id)
        except InstanceNotFound as e:
            raise ValueError(f"expert {planned.existing_id} disappeared between "
                             f"plan and apply") from e
        instance.expert = planned.expert_type
        if general.get("account_id") is not None:
            instance.account_id = int(general["account_id"])
        instance.user_description = general.get("user_description") or ""
        if general.get("virtual_equity_pct") is not None:
            instance.virtual_equity_pct = float(general["virtual_equity_pct"])
        instance.priority = priority_from_settings(general, current=instance.priority)
        instance.enter_market_ruleset_id = enter_id
        instance.open_positions_ruleset_id = open_id
        # instance.enabled is deliberately NOT touched: an import never starts trading.
        update_instance(instance)
        instance_id = instance.id
        messages.append(f"UPDATED expert {instance_id} '{planned.alias}'")

    _apply_settings(instance_id, entry, messages)
    return messages


def _import_rulesets(entry: Dict[str, Any], messages: List[str]) -> Dict[str, int]:
    """Rebuild the entry's rulesets, returning {ruleset name: id}."""
    rulesets = entry.get("rulesets")
    if not rulesets or not rulesets.get("rulesets"):
        return {}
    names = [r["name"] for r in rulesets["rulesets"]]
    ids, warnings = RulesImporter.import_rulesets_reusing_by_name(rulesets)
    for w in warnings:
        messages.append(f"  {w}")
    return dict(zip(names, ids))


def _find_ruleset_by_name(name: str) -> Optional[int]:
    for ruleset in get_all_instances(Ruleset):
        if ruleset.name == name:
            return ruleset.id
    return None


def _apply_settings(instance_id: int, entry: Dict[str, Any], messages: List[str]) -> None:
    expert = get_expert_instance_from_id(instance_id)
    if expert is None:
        messages.append(f"  settings NOT applied: expert {instance_id} could not be built")
        return

    settings = entry.get("expert_settings")
    if settings:
        # Reset first: an import only writes the keys it contains, so without this a key the
        # file dropped would keep its stale value instead of returning to the class default.
        expert.reset_settings()

        # A None is "not set", and after the reset above an ABSENT key already reads as the
        # class default -- which is the same thing. Writing it instead would go through the
        # declared type's coercion, and a bool setting exported as None dies there
        # ("cannot read None as a boolean setting value"), failing the whole expert over a
        # value that carries no information.
        writable = {k: (v, None) for k, v in settings.items() if v is not None}
        expert.save_settings(writable)
        skipped = len(settings) - len(writable)
        messages.append(f"  {len(writable)} setting(s) applied"
                        + (f", {skipped} unset left at default" if skipped else ""))

    # ``symbol_settings`` is a VIEW of expert_settings['enabled_instruments'], not a second
    # store, so applying both would write the key twice -- and the second write would go
    # through set_enabled_instruments, which filters to enabled==True and would quietly drop
    # a disabled entry the settings payload had kept. Only used when the file has no settings
    # block to carry it (the old single-expert export with 'Expert Settings' unticked).
    symbols = entry.get("symbol_settings")
    if symbols and not (settings and "enabled_instruments" in settings) \
            and hasattr(expert, "set_enabled_instruments"):
        expert.set_enabled_instruments(symbols)
        messages.append(f"  {len(symbols)} instrument config(s) applied")

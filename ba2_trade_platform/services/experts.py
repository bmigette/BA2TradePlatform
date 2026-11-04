"""Expert instance reads for the management API."""
import math
from typing import Any, Callable, Dict, List, Optional

from sqlmodel import select

from ba2_common.core.market_condition_rules import (
    PROFILE_SETTING as MARKET_CONDITION_PROFILE_SETTING,
    PROFILE_SETTING_OFF as MARKET_CONDITION_PROFILE_OFF,
    assert_fields_served,
    assert_market_rule_actions_live,
    parse_profile_setting,
)

from ..core.db import get_db, update_instance
from ..core.models import ExpertInstance, MarketAnalysis
from ..core.utils import get_expert_instance_from_id
from ..logger import logger
from ._common import page, require_row, row_dict
from ._locks import expert_write_lock
from ._scrub import mask_setting
from ._readonly import readonly_balance_reads
from .accounts import is_secret_setting
from .errors import Conflict, Invalid, NotFound, Unmeasurable

_SCHEDULE_PREFIX = "execution_schedule"
_RESERVE_KEY = "overalloc_min_res_pct"


def _last_analysis(session, expert_id: int) -> Optional[Dict[str, Any]]:
    row = session.exec(select(MarketAnalysis).where(MarketAnalysis.expert_instance_id == expert_id)
                       .order_by(MarketAnalysis.id.desc()).limit(1)).first()
    if row is None:
        return None
    return {"id": row.id, "symbol": row.symbol, "status": row.status.value,
            "subtype": row.subtype.value, "created_at": row.created_at.isoformat()}


def list_experts(*, account: Optional[int], enabled: Optional[bool], limit: int, offset: int) -> Dict[str, Any]:
    """Instance rows + last analysis. No settings and no broker figures here (those need a live expert and a
    broker round trip per row): use the detail endpoint."""
    stmt = select(ExpertInstance)
    if account is not None:
        stmt = stmt.where(ExpertInstance.account_id == account)
    if enabled is not None:
        stmt = stmt.where(ExpertInstance.enabled == enabled)
    with get_db() as session:
        def convert(e: ExpertInstance) -> Dict[str, Any]:
            out = row_dict(e)
            out["last_analysis"] = _last_analysis(session, e.id)
            return out
        return page(session, stmt.order_by(ExpertInstance.id), limit=limit, offset=offset, convert=convert)


def expert_detail(expert_id: int) -> Dict[str, Any]:
    with get_db() as session:
        out = row_dict(require_row(session, ExpertInstance, expert_id, "expert"))
        out["last_analysis"] = _last_analysis(session, expert_id)
    try:
        expert = get_expert_instance_from_id(expert_id)
    except ValueError as e:        # e.g. the stored expert type is not registered
        raise Invalid(f"Expert {expert_id} cannot be instantiated: {type(e).__name__}: {e}", {"id": expert_id}) from e
    if expert is None:
        raise NotFound(f"No expert instance with id {expert_id}", {"id": expert_id})
    definitions = type(expert).get_merged_settings_definitions()
    settings: Dict[str, Any] = {}
    for key, value in expert.settings.items():
        definition = definitions[key] if key in definitions else None
        settings[key] = mask_setting(key, value, definition)
    out["settings"] = settings
    out["schedule"] = {k: v for k, v in settings.items() if k.startswith(_SCHEDULE_PREFIX)}
    # Explicit access: the key is a built-in MarketExpertInterface setting, so a KeyError is a real bug.
    out["overalloc_min_res_pct"] = expert.settings[_RESERVE_KEY]
    # A GET must not touch the live buying-power failure memory nor sleep on retries (see _readonly).
    with readonly_balance_reads():
        capital = expert.describe_capital_mapping()
    if capital is None or "error" in capital:
        reason = "nothing to describe" if capital is None else capital["error"]
        raise Unmeasurable(f"Expert {expert_id}: capital cannot be measured: {reason}", {"id": expert_id})
    out["capital"] = capital
    return out


# ======================================================================================================
# Writes (phase 3): shared with the Settings page where the page has the same step
# ======================================================================================================

def check_market_gates(enter_market_ruleset_id: Optional[int], open_positions_ruleset_id: Optional[int],
                       profile_setting: Callable[[], str]) -> None:
    """Refuse a (ruleset, profile setting) combination the live platform cannot run (the Settings page's
    ``_refuse_unserved_market_gates``, moved here so enabling an expert or changing its profile through the API is
    refused exactly as the dialog refuses it).

    The gates and the data that feeds them are two halves of one strategy held in two places: the enter-market
    RULESET carries the leaves, the expert SETTING says which profile is served. Saved apart the instance comes up
    enabled and correct-looking while every gated entry is refused for ever. The open-positions slot may carry a
    market leaf only on a rule that CLOSES, REDUCES or ADJUSTS TP/SL (checked first); an exit leaf must also be
    served by the profile setting.

    ``profile_setting`` is called only when a market leaf exists, so an unreadable setting does not block an
    ungated expert. Raises ValueError naming the leaf, the field and the setting."""
    from ba2_common.core.market_condition_live import (     # looked up per call: tests patch the module
        market_condition_fields_in_ruleset,
        ruleset_rule_contents,
    )

    exit_where = f"open-positions ruleset {open_positions_ruleset_id}"
    assert_market_rule_actions_live(ruleset_rule_contents(open_positions_ruleset_id), exit_where)

    used = market_condition_fields_in_ruleset(enter_market_ruleset_id)
    on_exit = market_condition_fields_in_ruleset(open_positions_ruleset_id)
    if not used and not on_exit:
        return          # no market leaf: any profile setting is fine, including empty
    profiles = parse_profile_setting(profile_setting())
    if used:
        assert_fields_served(used, profiles, where=f"enter-market ruleset {enter_market_ruleset_id}")
    if on_exit:
        assert_fields_served(on_exit, profiles, where=exit_where)


def refresh_schedules(expert_id: int) -> None:
    """Queue a schedule refresh for one expert (the Settings page's save tail). Logged, never raised: the save
    already happened and the next restart reloads the schedules anyway."""
    try:
        from ..core.JobManager import get_job_manager
        get_job_manager().refresh_expert_schedules(expert_id)
        logger.info(f"Refreshed scheduled analysis jobs for expert {expert_id}")
    except Exception as e:
        logger.error(f"Error refreshing scheduled jobs for expert {expert_id}: {e}", exc_info=True)


def _drop_caches(expert_id: int) -> None:
    """Drop the cached expert instance (and so its settings) and the market-condition resolver built from them,
    as ``/api/reload`` does for one expert."""
    from ..core.ExpertInstanceCache import ExpertInstanceCache
    from ..core.instance_registry import drop_market_condition_resolver
    ExpertInstanceCache.invalidate_instance(expert_id)
    drop_market_condition_resolver(expert_id)


MAX_VIRTUAL_EQUITY_PCT = 1000.0


def _require_expert_row(expert_id: int) -> ExpertInstance:
    with get_db() as session:
        row = require_row(session, ExpertInstance, expert_id, "expert")
        session.expunge(row)
    return row


def _stored_profile(expert_id: int) -> str:
    expert = _live_expert(expert_id)
    return str(expert.settings[MARKET_CONDITION_PROFILE_SETTING]
               if MARKET_CONDITION_PROFILE_SETTING in expert.settings and expert.settings[MARKET_CONDITION_PROFILE_SETTING]
               else MARKET_CONDITION_PROFILE_OFF)


def _live_expert(expert_id: int):
    try:
        expert = get_expert_instance_from_id(expert_id)
    except ValueError as e:
        raise Invalid(f"Expert {expert_id} cannot be instantiated: {type(e).__name__}: {e}", {"id": expert_id}) from e
    if expert is None:
        raise NotFound(f"No expert instance with id {expert_id}", {"id": expert_id})
    return expert


def update_expert(expert_id: int, *, enabled: Optional[bool], virtual_equity_pct: Optional[float],
                  dry_run: bool) -> Dict[str, Any]:
    """PATCH /experts/{id}: the two instance fields the Settings dialog edits as ``enabled`` and ``virtual_equity_pct``,
    then the schedule refresh the dialog's save ends with. Enabling runs the dialog's market-gate refusal first."""
    if enabled is None and virtual_equity_pct is None:
        raise Invalid("Give enabled and/or virtual_equity_pct", {"fields": ["enabled", "virtual_equity_pct"]})
    if enabled is not None and not isinstance(enabled, bool):
        raise Invalid("enabled must be true or false", {"field": "enabled"})
    if virtual_equity_pct is not None:
        if isinstance(virtual_equity_pct, bool) or not isinstance(virtual_equity_pct, (int, float)) \
                or not math.isfinite(virtual_equity_pct) or not 0 <= virtual_equity_pct <= MAX_VIRTUAL_EQUITY_PCT:
            raise Invalid(f"virtual_equity_pct must be a number from 0 to {MAX_VIRTUAL_EQUITY_PCT:g}",
                          {"field": "virtual_equity_pct"})
        virtual_equity_pct = float(virtual_equity_pct)
    def view(row: ExpertInstance):
        previous = {"enabled": row.enabled, "virtual_equity_pct": row.virtual_equity_pct}
        wanted = {"enabled": previous["enabled"] if enabled is None else enabled,
                  "virtual_equity_pct": previous["virtual_equity_pct"] if virtual_equity_pct is None
                  else virtual_equity_pct}
        return previous, wanted, sorted(k for k, v in wanted.items() if v != previous[k])

    row = _require_expert_row(expert_id)
    previous, wanted, changed = view(row)
    if enabled and not previous["enabled"]:
        try:
            check_market_gates(row.enter_market_ruleset_id, row.open_positions_ruleset_id,
                               lambda: _stored_profile(expert_id))
        except ValueError as e:
            raise Conflict(f"Expert {expert_id} cannot be enabled: {e}", {"id": expert_id}) from e
    if dry_run or not changed:
        return {"id": expert_id, "dry_run": dry_run, "previous": previous, "current": wanted, "changed": changed}
    with expert_write_lock(expert_id):
        row = _require_expert_row(expert_id)           # re-read under the lock: judge against what is stored NOW
        previous, wanted, changed = view(row)
        if changed:
            row.enabled, row.virtual_equity_pct = wanted["enabled"], wanted["virtual_equity_pct"]
            update_instance(row)
            logger.info(f"Updated expert instance: {expert_id} ({', '.join(changed)})")
    if changed:
        _drop_caches(expert_id)
        refresh_schedules(expert_id)
    return {"id": expert_id, "dry_run": False, "previous": previous, "current": wanted, "changed": changed}


def _check_setting_value(key: str, definition: Dict[str, Any], value: Any) -> None:
    kind = definition["type"] if "type" in definition else "str"
    bad = None
    if kind == "bool":
        bad = None if isinstance(value, bool) else "a boolean"
    elif kind in ("int", "float"):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            bad = "a finite number"
        elif kind == "int" and float(value) != int(value):
            bad = "a whole number"
    elif kind == "list":
        bad = None if isinstance(value, list) else "a list"
    elif kind == "json":
        bad = None if isinstance(value, (dict, list)) else "an object or a list"
    elif kind in ("str", "password"):
        bad = None if isinstance(value, str) else "a string"
    if bad is not None:
        raise Invalid(f"Setting {key!r} must be {bad}", {"field": key, "type": kind})
    if "valid_values" in definition and key != MARKET_CONDITION_PROFILE_SETTING:
        allowed = definition["valid_values"]
        if isinstance(allowed, (list, tuple)) and allowed and value not in allowed:
            raise Invalid(f"Setting {key!r} must be one of {list(allowed)}", {"field": key})


def secret_setting_keys(expert_id: int) -> List[str]:
    """Names of the expert's settings that are credentials (by definition flag or by name), so the audit trail can
    redact their values whatever the request body looked like. An unknown expert has none (the write will 404
    before anything is stored). If the definitions CANNOT be determined the PATCH is refused (503): auditing with
    weaker redaction is not an option."""
    try:
        expert = _live_expert(expert_id)
    except NotFound:
        return []
    except Invalid as e:
        raise Unmeasurable(f"Expert {expert_id}: its settings definitions cannot be read, so the write is refused "
                           f"({e.message})", {"id": expert_id}) from e
    definitions = type(expert).get_merged_settings_definitions()
    return [k for k, d in definitions.items() if is_secret_setting(k, d)]


def update_expert_settings(expert_id: int, values: Dict[str, Any], *, dry_run: bool) -> Dict[str, Any]:
    """PATCH /experts/{id}/settings: validate EVERY key against ``get_merged_settings_definitions()`` (unknown key,
    wrong type, value outside ``valid_values`` -> 422, nothing written), then save each changed key with
    ``expert.save_setting`` (the Settings dialog's own write), then drop the caches and queue the schedule refresh.
    A value equal to the stored one is not written (the dialog's no-edit-is-a-no-op rule). Values in the answer are
    masked like GET /experts/{id}: a credential shows as ``{"set": true}``."""
    if not isinstance(values, dict) or not values:
        raise Invalid("Give a non-empty object of {setting: value}")
    row = _require_expert_row(expert_id)
    expert = _live_expert(expert_id)
    definitions = type(expert).get_merged_settings_definitions()
    unknown = sorted(k for k in values if k not in definitions)
    if unknown:
        raise Invalid(f"Unknown setting(s) {unknown} for this expert type", {"unknown": unknown})
    for key, value in values.items():
        _check_setting_value(key, definitions[key], value)
    stored = expert.settings
    changes = {k: v for k, v in values.items() if not (k in stored and stored[k] == v)}
    if MARKET_CONDITION_PROFILE_SETTING in changes:
        try:
            check_market_gates(row.enter_market_ruleset_id, row.open_positions_ruleset_id,
                               lambda: str(changes[MARKET_CONDITION_PROFILE_SETTING]))
        except ValueError as e:
            raise Conflict(f"Setting refused: {e}", {"id": expert_id}) from e
    shown = lambda k, v: mask_setting(k, v, definitions[k])
    out = {"id": expert_id, "dry_run": dry_run, "changed": sorted(changes),
           "unchanged": sorted(k for k in values if k not in changes),
           "previous": {k: shown(k, stored[k] if k in stored else None) for k in changes},
           "current": {k: shown(k, v) for k, v in changes.items()}}
    if dry_run or not changes:
        return out
    with expert_write_lock(expert_id):
        writer = type(expert)(expert_id)
        written = []
        try:
            for key, value in changes.items():
                writer.save_setting(key, value, setting_type=definitions[key]["type"] if "type" in definitions[key] else None)
                written.append(key)
        except Exception as e:
            logger.error(f"Expert {expert_id}: saving settings stopped after {written}: {type(e).__name__}", exc_info=True)
            _drop_caches(expert_id)
            raise Invalid(f"Saving stopped at {key!r} ({type(e).__name__}); already saved: {written}",
                          {"id": expert_id, "saved": written}) from e
    _drop_caches(expert_id)
    refresh_schedules(expert_id)
    return out

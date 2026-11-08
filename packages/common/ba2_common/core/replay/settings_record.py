"""What an analysis record's ``settings`` object holds, and how a reader judges it.

Before schema 2 the recorded settings were whatever the expert handed ``_process`` -- its own
keys only. The platform/risk-manager settings the DECISION reads (``use_atr_stop``,
``sizing_mode``, ``atr_period``, ...) live on the same instance but were not in it, so no
warm plan or historical comparison could resolve an expert from a recording.

Writer side: :func:`full_recorded_settings` composes the one resolved dict the expert itself
produced (``MarketExpertInterface.resolve_all_settings``) with the instance columns that drive
decisions, and drops credentials with the SAME key rule the provider taps use
(``observe.is_secret_key``). Reader side: :func:`explain_missing_setting` turns the opaque
"setting X is required" into a statement about WHICH recording is incomplete and why.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

from ba2_common.core.replay.dependencies import RULE_REQUIRED_SETTINGS, MissingDependencySetting
from ba2_common.core.replay.observe import is_secret_key

__all__ = [
    "FULL_SETTINGS_SCHEMA_VERSION",
    "INSTANCE_KEY_PREFIX",
    "INSTANCE_COLUMNS",
    "instance_decision_columns",
    "redact_settings",
    "full_recorded_settings",
    "missing_rule_settings",
    "explain_missing_setting",
]

#: First schema version whose recorded settings are the expert's full resolved settings.
FULL_SETTINGS_SCHEMA_VERSION = 2

#: Instance columns are namespaced so they can never collide with a setting key.
INSTANCE_KEY_PREFIX = "instance."

#: The ExpertInstance columns a live decision reads.
INSTANCE_COLUMNS = (
    "account_id", "enabled", "priority", "virtual_equity_pct",
    "enter_market_ruleset_id", "open_positions_ruleset_id",
)


def instance_decision_columns(instance: Any) -> Dict[str, Any]:
    """``{"instance.<column>": value}`` for every decision-driving column (all must exist)."""
    if instance is None:
        raise ValueError("cannot record instance columns: the expert has no instance row")
    return {f"{INSTANCE_KEY_PREFIX}{name}": getattr(instance, name) for name in INSTANCE_COLUMNS}


def redact_settings(settings: Mapping[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """``(kept, dropped_key_names)``: credential-shaped keys are dropped, never masked."""
    kept: Dict[str, Any] = {}
    dropped: List[str] = []
    for key, value in settings.items():
        if is_secret_key(key):
            dropped.append(str(key))
        else:
            kept[key] = value
    return kept, sorted(dropped)


def full_recorded_settings(resolved: Mapping[str, Any], instance: Any,
                           passed: Optional[Mapping[str, Any]] = None
                           ) -> Tuple[Dict[str, Any], List[str]]:
    """The settings object to record, plus the names of the keys redacted out of it.

    ``resolved`` is the expert's own full resolution; ``passed`` is the dict the live body
    actually handed ``_process`` and wins on any overlap (it is by definition what was used).
    """
    merged: Dict[str, Any] = dict(resolved)
    if passed:
        merged.update(passed)
    merged.update(instance_decision_columns(instance))
    return redact_settings(merged)


def missing_rule_settings(settings: Mapping[str, Any]) -> List[str]:
    """The decision-time settings :data:`RULE_REQUIRED_SETTINGS` this recording does not hold."""
    return [key for key in RULE_REQUIRED_SETTINGS if key not in settings]


def explain_missing_setting(exc: MissingDependencySetting, settings: Mapping[str, Any], *,
                            schema_version: int) -> str:
    """A reader-facing sentence for a resolver that could not find ``exc.key``.

    A pre-schema-2 recording is reported as INCOMPLETE (naming every decision key it lacks),
    not as an opaque missing setting; a schema-2 recording that still lacks the key is a
    different, louder problem (the capture-time resolution failed or the key was redacted).
    """
    held = len(settings) if settings is not None else 0
    missing = sorted(set(missing_rule_settings(settings or {})) | {exc.key})
    if schema_version < FULL_SETTINGS_SCHEMA_VERSION:
        return (
            f"recorded settings incomplete (pre-v{FULL_SETTINGS_SCHEMA_VERSION} recording, "
            f"schema_version {schema_version}): it holds only the expert's own {held} keys and "
            f"lacks {missing}; {exc}. Re-record with a current build (filling from a DB's "
            f"CURRENT instance settings would not be as-recorded and is never done implicitly)")
    return (
        f"recorded settings incomplete although the recording is schema_version {schema_version}: "
        f"lacks {missing} ({held} keys held); check the record's 'settings_resolution_failed' / "
        f"'redacted_setting_keys' branch flags; {exc}")

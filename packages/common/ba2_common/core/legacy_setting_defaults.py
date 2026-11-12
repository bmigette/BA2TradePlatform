"""Expert setting DEFAULTS that changed after results were produced on the old value.

A stored backtest config (an optimization's ``backtest`` block, a row's ``expertSettings`` /
``expertFixedSettings``, a deploy payload) records only the settings it SET; everything else was
resolved from the expert's definition default AT THE TIME. When a definition default changes, a
re-run or a deploy of such a config would silently resolve the NEW default and diverge from what
was measured. This table keeps the old value for those configs.

The rule is one line: a config of a listed expert that does not state a listed key ran with the
legacy value. So:

* every reader that turns a stored/run config into the settings an expert decides with applies
  :func:`pin_legacy_setting_defaults` -- the backtest engine
  (``daily_backtest_handler._expert_decision_settings``, i.e. every GA trial, re-run, TOP-N and
  standalone run) and the deploy export (``ba2_common.export.backtest_export``);
* every place that creates a NEW job states the current default with
  :func:`materialise_current_defaults`, so the new value is recorded on the job and is never
  mistaken for a legacy config later.

A config that states the key always wins. The pin applies the same way whether a config is run
for the first time or re-run years later, so the same config always means the same settings.

Live expert instances are pinned separately by an alembic data migration (live resolves a missing
row through the definition, not through this table).
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Mapping

#: expert class name -> {setting key: the definition default every config before the change ran}.
LEGACY_SETTING_DEFAULTS: Dict[str, Dict[str, Any]] = {
    # 2026-11-09: DeterministicScorer yc_scale 0.005 -> 0.5 (macro.DEF_YC_SCALE). FRED serves the
    # 10y-3m spread in PERCENT; 0.005 saturated the tanh on ~96% of days. The constant was fixed
    # on 2026-08-07 but the definition kept 0.005 until 2026-11-09, so every DS backtest, GA run
    # and live instance before then used 0.005. yc_scale is not a GA gene.
    "DeterministicScorer": {"yc_scale": 0.005},
}


def pin_legacy_setting_defaults(expert_class: str, settings: Mapping[str, Any]) -> Dict[str, Any]:
    """A copy of ``settings`` in which each changed-default key of ``expert_class`` that the config
    does NOT state carries its legacy value. Stated keys win; other experts are copied unchanged."""
    out = dict(settings)
    for key, legacy in LEGACY_SETTING_DEFAULTS.get(expert_class, {}).items():
        if key not in out:
            out[key] = legacy
    return out


def materialise_current_defaults(expert_class: str, settings: Mapping[str, Any],
                                 definitions_for: Callable[[str], Mapping[str, Any]]) -> Dict[str, Any]:
    """A copy of ``settings`` for a NEW job: each changed-default key of ``expert_class`` that is
    not stated gets the CURRENT definition default, so the job records the value it runs with.

    ``definitions_for(expert_class)`` returns the class's ``get_settings_definitions()``; it is only
    called for an expert listed in :data:`LEGACY_SETTING_DEFAULTS`. A definition without the key or
    its default raises ``KeyError`` (no guessed value)."""
    out = dict(settings)
    changed = LEGACY_SETTING_DEFAULTS.get(expert_class)
    if not changed:
        return out
    definitions = definitions_for(expert_class)
    for key in changed:
        if key not in out:
            out[key] = definitions[key]["default"]
    return out

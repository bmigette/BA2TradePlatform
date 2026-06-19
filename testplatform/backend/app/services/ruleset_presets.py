"""Default exit-rule presets — the single source of truth for the UI's preset picker.

Each preset is ``{"key": str, "label": str, "rule": <ExitCondition-shaped dict>}``.
Every ``rule`` validates against ``app.api.strategies.ExitCondition``: the top level
is an exit-rule (id/conditions/action/...), ``conditions`` is a single AND-group node,
and its children are flag leaves ``{id, field}`` or numeric leaves
``{id, field, comparison, value, optimize_enabled, value_min, value_max, value_step}``.

These are read-only data; the API exposes them via ``GET /api/ruleset/exit-presets``.
"""
from __future__ import annotations

EXIT_PRESETS: list[dict] = [
    {
        "key": "bearish-close",
        "label": "Close on bearish recommendation",
        "rule": {
            "id": "preset_bearish",
            "name": "Close on bearish recommendation",
            "action": "close",
            "toggle_optimize": True,
            "conditions": {
                "id": "bearish_grp",
                "operator": "AND",
                "conditions": [
                    {"id": "bearish_leaf", "field": "bearish"},
                ],
            },
        },
    },
    {
        "key": "downgrade-close",
        "label": "Close on rating downgrade to negative",
        "rule": {
            "id": "preset_downgrade",
            "name": "Close on rating downgrade to negative",
            "action": "close",
            "toggle_optimize": True,
            "conditions": {
                "id": "downgrade_grp",
                "operator": "AND",
                "conditions": [
                    {"id": "downgrade_leaf", "field": "current_rating_negative"},
                ],
            },
        },
    },
    {
        "key": "break-even-profit-lock",
        "label": "Break-even profit lock",
        "rule": {
            "id": "preset_belock",
            "name": "Break-even profit lock",
            "action": "adjust_stop_loss",
            "reference_value": "order_open_price",
            "action_value": 0.0,
            "action_value_optimize": True,
            "action_value_min": -2.0,
            "action_value_max": 8.0,
            "action_value_step": 2.0,
            "toggle_optimize": True,
            "conditions": {
                "id": "belock_grp",
                "operator": "AND",
                "conditions": [
                    {
                        "id": "belock_cond",
                        "field": "profit_loss_percent",
                        "comparison": ">",
                        "value": 5,
                        "optimize_enabled": True,
                        "value_min": 3,
                        "value_max": 20,
                        "value_step": 2,
                    },
                ],
            },
        },
    },
    {
        "key": "time-exit",
        "label": "Time-based exit",
        "rule": {
            "id": "preset_time",
            "name": "Time-based exit",
            "action": "close",
            "toggle_optimize": True,
            "conditions": {
                "id": "time_grp",
                "operator": "AND",
                "conditions": [
                    {
                        "id": "time_cond",
                        "field": "days_opened",
                        "comparison": ">",
                        "value": 60,
                        "optimize_enabled": True,
                        "value_min": 20,
                        "value_max": 120,
                        "value_step": 20,
                    },
                ],
            },
        },
    },
]

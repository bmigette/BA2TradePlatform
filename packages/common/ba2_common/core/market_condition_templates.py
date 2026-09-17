"""Registry-derived entry genes shared by option and equity research drivers."""
from __future__ import annotations


def market_condition_leaves(prefix: str, profiles) -> list:
    """One optional leaf per searched field, in registry order, with stable gene IDs."""
    if not profiles:
        return []
    from ba2_common.core.TradeConditions import market_condition_condition_class
    from ba2_common.core.market_conditions import PROFILES, field_codes
    from ba2_common.core.market_condition_rules import parse_profile_setting
    from ba2_common.core.rule_models import MODE_OFF, NUMERIC_MODE_CHOICES

    selected = set(parse_profile_setting(",".join(profiles)))
    leaves = []
    for name, profile in PROFILES.items():
        if name not in selected:
            continue
        for spec in profile.fields:
            if not spec.searched:
                continue
            allowed = market_condition_condition_class(spec.name).ALLOWED_OPERATORS
            leaf = {"id": f"{prefix}-market-{spec.short}", "field": spec.name,
                    "field_type": "numeric", "mode_optimize": True}
            if spec.kind == "numeric":
                if spec.anchor_op not in allowed:
                    raise ValueError(f"market-condition field {spec.name!r}: unsupported anchor operator")
                leaf.update(op=spec.anchor_op, value=float(spec.anchor_value), optimize=True,
                            value_min=float(spec.value_min), value_max=float(spec.value_max),
                            value_step=float(spec.value_step), mode_choices=list(NUMERIC_MODE_CHOICES))
            else:
                if "==" not in allowed:
                    raise ValueError(f"market-condition field {spec.name!r}: equality is unsupported")
                leaf.update(op="==", mode_choices=[MODE_OFF, *field_codes(spec.name)])
            leaves.append(leaf)
    return leaves

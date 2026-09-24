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


#: The kinds :func:`market_exit_rules` can build, in emission order.
MARKET_EXIT_KINDS = ("exit", "stop", "tp")
_DIRECTIONS = ("long", "short")

#: ``adjust_stop_loss`` percent band from ``order_open_price``. 0 = breakeven.
_STOP_PCT = {"value": 0.0, "min": -2.0, "max": 0.0, "step": 1.0}
#: ``adjust_take_profit`` percent band from ``order_open_price``.
_TP_PCT = {"value": 20.0, "min": 10.0, "max": 30.0, "step": 10.0}

_THRESHOLD_OPS = {"below": "<", "above": ">"}


def _fixed_numeric_leaf(leaf_id: str, spec, mode: str, lo: float, hi: float, value: float) -> dict:
    """A numeric market leaf whose MODE is fixed (no mode gene) and whose threshold is searched.

    Shaped like a leaf the GA decode resolved (``strategy_param_space._apply_mode``): ``op`` and
    ``comparison`` carry the operator, ``mode`` keeps the token as provenance, and there is no
    ``mode_optimize`` / ``mode_choices``. The collector emits only its ``cond:<id>:value`` gene."""
    op = _THRESHOLD_OPS[mode]
    return {"id": leaf_id, "field": spec.name, "field_type": "numeric",
            "op": op, "comparison": op, "mode": mode, "value": float(value), "optimize": True,
            "value_min": float(lo), "value_max": float(hi), "value_step": float(spec.value_step)}


def _fixed_categorical_leaf(leaf_id: str, field: str, token: str) -> dict:
    """A categorical market leaf resolved to ONE value, ``field == code(token)``, with no gene.

    A categorical mode gene cannot be narrowed to a single value: ``ConditionLeaf`` requires
    ``mode_choices`` to start with ``"off"``, and the gene collector requires exactly
    ``["off", *codes]``. So the leaf is emitted already resolved, as the decode would write it."""
    from ba2_common.core.market_conditions import field_codes
    code = field_codes(field)[token]
    return {"id": leaf_id, "field": field, "field_type": "numeric",
            "op": "==", "comparison": "==", "mode": token, "value": float(code)}


def _exit_rule(rule_id: str, leaves: list, actions: list, continue_processing: bool) -> dict:
    """One exit TradeRule, authored OFF behind a rule-level toggle gene.

    ``toggle_optimize`` makes the collector emit ``exit:<id>:enabled``. ``enabled: False`` keeps
    the rule absent unless that gene decodes to exactly 1 (``strategy_param_space
    ._decode_rule_list``), and ``rules_convert.live_actions_from_trade_rule`` drops a rule that
    still carries ``enabled: False``, on the backtest seeder and the live export alike."""
    return {"id": rule_id,
            "conditions": {"type": "AND", "conditions": leaves},
            "actions": actions,
            "continue_processing": continue_processing,
            "toggle_optimize": True,
            "enabled": False}


def _assert_no_off_leaf(rules: list, where: str) -> None:
    """Refuse a market exit leaf that could resolve to ``off`` or is not concrete.

    The decode REMOVES an off leaf. In a one-leaf exit rule that leaves an empty AND, which is
    always true: the rule would close (or re-stop) EVERY position. So each leaf here must carry
    no removal gene (``mode_optimize``, ``mode_choices``, ``toggle_optimize``), a non-off
    ``mode``, an operator and a value, and every rule must keep at least one leaf."""
    from ba2_common.core.rule_models import MODE_OFF
    removal_keys = ("mode_optimize", "modeOptimize", "mode_choices", "modeChoices",
                    "toggle_optimize", "toggleOptimize")
    for rule in rules:
        leaves = rule["conditions"]["conditions"]
        if not leaves:
            raise ValueError(f"{where}: rule {rule['id']!r} has no condition leaf (always true)")
        for leaf in leaves:
            bad = [k for k in removal_keys if leaf.get(k)]
            if (bad or leaf.get("mode") in (None, MODE_OFF) or not leaf.get("op")
                    or leaf.get("value") is None):
                raise ValueError(
                    f"{where}: rule {rule['id']!r} leaf {leaf.get('id')!r} can be switched off or "
                    f"is not concrete (removal keys {bad!r}, mode {leaf.get('mode')!r}); a market "
                    f"exit leaf must be a fixed, resolved condition")


def market_exit_rules(prefix: str, profiles, direction: str, kinds=MARKET_EXIT_KINDS) -> list:
    """Market-condition exit / stop / take-profit rules, each OFF by default behind a rule toggle.

    Emitted in this order, each only when the selected profiles serve every field it reads:

    * ``<prefix>-mkt-exit-structure`` (ta-structure-v1): ``structure_state == against`` -> close.
    * ``<prefix>-mkt-exit-slope`` (ohlcv-v1): trend slope against the position, threshold
      searched on the against side of the anchor -> close. The rule format has no rule-level
      choice gene, so the two exit variants are two independently toggled rules.
    * ``<prefix>-mkt-stop`` (ta-structure-v1): ``structure_state == against`` ->
      ``adjust_stop_loss`` from ``order_open_price``, percent searched -2..0 (0 = breakeven).
    * ``<prefix>-mkt-tp`` (ohlcv-v1): trend slope WITH the position AND ADX above a threshold
      (both searched) -> ``adjust_take_profit`` from ``order_open_price``, percent +10..+30.

    The close rules stop processing. The adjustment rules continue, so they never shadow a later
    close. ``direction`` "long": against = ``bear``, slope below the anchor (0); "short" mirrors
    it. The percent is DIRECTION-RELATIVE in ``TradeActions._AdjustPriceLevelAction`` (long
    ``ref * (1 + p/100)``, short ``ref * (1 - p/100)``), so the SAME numbers serve both sides: a
    negative stop percent is on the adverse side and a positive TP percent on the profit side,
    for a long and a short alike.

    No leaf carries a mode gene: mode ``off`` would empty a rule's AND and make it always true
    (close every position). Leaves are fixed and resolved; only thresholds are searched.
    """
    from ba2_common.core.market_condition_rules import (
        assert_market_rule_actions, parse_profile_setting, served_fields)
    from ba2_common.core.market_conditions import (
        FIELD_ADX, FIELD_STRUCTURE_STATE, FIELD_TREND_SLOPE, STATE_BEAR, STATE_BULL, field_spec)

    if direction not in _DIRECTIONS:
        raise ValueError(f"market_exit_rules: direction must be one of {_DIRECTIONS!r}, got {direction!r}")
    kinds = tuple(kinds)
    if any(k not in MARKET_EXIT_KINDS for k in kinds) or len(set(kinds)) != len(kinds):
        raise ValueError(f"market_exit_rules: kinds must be distinct values of {MARKET_EXIT_KINDS!r}, "
                         f"got {kinds!r}")
    if not profiles:
        return []
    served = served_fields(parse_profile_setting(",".join(profiles)))
    long = direction == "long"
    against_state = STATE_BEAR if long else STATE_BULL

    slope = field_spec(FIELD_TREND_SLOPE) if FIELD_TREND_SLOPE in served else None
    if slope is not None and slope.anchor_op != ">":
        # The half-ranges below read the anchor as the neutral point and ">" as "uptrend".
        raise ValueError(f"market_exit_rules: the {FIELD_TREND_SLOPE!r} anchor is now "
                         f"{slope.anchor_op!r} {slope.anchor_value!r}; revisit the slope halves")

    def slope_leaf(leaf_id: str, with_position: bool) -> dict:
        a = slope.anchor_value
        if with_position == long:  # an uptrend: with a long, against a short
            return _fixed_numeric_leaf(leaf_id, slope, "above", a, slope.value_max, a)
        return _fixed_numeric_leaf(leaf_id, slope, "below", slope.value_min, a, a)

    def state_leaf(rid: str) -> dict:
        return _fixed_categorical_leaf(f"{rid}-state", FIELD_STRUCTURE_STATE, against_state)

    def pct_action(action_type: str, band: dict) -> dict:
        return {"action_type": action_type, "reference_value": "order_open_price",
                "action_value": band["value"], "action_value_optimize": True,
                "action_value_min": band["min"], "action_value_max": band["max"],
                "action_value_step": band["step"]}

    rules = []
    if "exit" in kinds:
        close = [{"action_type": "close"}]
        if FIELD_STRUCTURE_STATE in served:
            rid = f"{prefix}-mkt-exit-structure"
            rules.append(_exit_rule(rid, [state_leaf(rid)], close, False))
        if slope is not None:
            rid = f"{prefix}-mkt-exit-slope"
            rules.append(_exit_rule(rid, [slope_leaf(f"{rid}-slope", False)], close, False))
    if "stop" in kinds and FIELD_STRUCTURE_STATE in served:
        rid = f"{prefix}-mkt-stop"
        rules.append(_exit_rule(rid, [state_leaf(rid)],
                                [pct_action("adjust_stop_loss", _STOP_PCT)], True))
    if "tp" in kinds and slope is not None and FIELD_ADX in served:
        rid = f"{prefix}-mkt-tp"
        adx = field_spec(FIELD_ADX)
        leaves = [slope_leaf(f"{rid}-slope", True),
                  _fixed_numeric_leaf(f"{rid}-adx", adx, "above", adx.value_min, adx.value_max,
                                      adx.anchor_value)]
        rules.append(_exit_rule(rid, leaves, [pct_action("adjust_take_profit", _TP_PCT)], True))

    where = f"market_exit_rules({prefix!r}, {direction!r})"
    _assert_no_off_leaf(rules, where)
    assert_market_rule_actions(rules, where)
    return rules

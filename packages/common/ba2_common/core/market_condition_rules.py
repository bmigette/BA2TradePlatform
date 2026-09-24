"""Rule-level validation of market-condition leaves (design 2026-09-15 sections 5 and 6).

Three refusals live here, each closing a failure that is SILENT without it:

* :func:`assert_market_rule_actions` (tree form) / :func:`assert_market_rule_actions_live`
  (``EventAction`` form) -- what an OPEN-POSITIONS / exit rule carrying a market-condition leaf
  may do. Since plan 2026-09-24 Task B1 the live open-positions pass opens a decision scope, so
  an exit leaf evaluates live exactly as in the backtest, and a failed read is UNKNOWN (the rule
  does not fire, exits are never aborted). That is safe only for a rule that CLOSES, REDUCES or
  ADJUSTS TP/SL (:data:`MARKET_RULE_ACTIONS`), and only when every market leaf sits in a
  top-level AND. Before B2 the leaf was refused on any exit rule
  (:func:`assert_no_market_conditions`, still defined for importers, no longer called by the
  exit doors).
* :func:`assert_market_conditions_resolved` -- an UNRESOLVED mode gene leaving for live. Live
  receives concrete conditions only: ``mode_optimize`` is a TEMPLATE marker (the optimizer picks
  the mode), and a leaf still carrying it -- or a ``mode`` token with no concrete operator/value
  behind it -- describes a search space, not a rule.
* :func:`assert_market_fields_served` -- a market leaf whose FIELD no profile in the expert's
  ``market_condition_profile`` setting serves. Since Task 12 the profile is an expert setting
  (:data:`PROFILE_SETTING`), so a ruleset and a setting are two halves of one strategy that can
  be saved apart. A gated leaf with no profile behind it builds no reader: live the gate reads
  ``no_context`` (and a leaf whose profile is merely MISSING from a multi-profile reader raises
  ``LookupError`` in ``MarketConditionCompare.evaluate``), so the sleeve never enters and looks
  exactly like a strategy that found no setup. Refused at deploy-import and at settings save.
* :data:`STRICT_FIELD_NAMES` / :func:`market_condition_fields` -- the names that must never be
  DROPPED by a converter that does not recognise them (see
  ``rule_builders.assert_market_fields_mappable``). Importing a gated ruleset onto a server whose
  event vocabulary predates these fields would otherwise deploy the strategy UNGATED, which is
  not a degraded version of it: it is a different strategy with the same name.

The literal names exist so the refusals survive on a server whose REGISTRY does not know the
field (an older ba2_common, or a future one that retired a profile): a name is permanent once a
payload can carry it, whereas a registry entry is code that travels.
"""
from __future__ import annotations

from typing import Any, Iterable, Iterator, List, Mapping, Sequence, Tuple

from ba2_common.core.rule_models import MODE_OFF

#: The expert setting that names the market-condition profile(s) an expert's entry rules may gate
#: on (plan Task 12, operator decision 2026-09-16). Defined HERE, next to the refusals that quote
#: it, and imported by the interface's settings definition, the live resolver, the backtest seam,
#: the launcher and the deploy importer -- one spelling, one parser.
PROFILE_SETTING = "market_condition_profile"

#: How the setting spells "no market-condition data at all". NOT ``"none"``: the launcher's CLI
#: uses that token, the setting uses the empty string, and accepting both here would let
#: ``market_condition_profile=none`` read as configured on a settings page while serving nothing.
PROFILE_SETTING_OFF = ''

#: Every market-condition field name that has ever been deployable, independent of what THIS
#: process's registry happens to hold: ohlcv-v1 (design 3) and ALL TWELVE ta-structure-v1 fields
#: (design 3.2), not only the five the first launcher profile searches -- a name becomes
#: deployable the moment a payload can carry it, and searching one more field later must not
#: need a server upgrade to be REFUSED correctly. A new profile adds its names HERE as well as to
#: the registry (``test_every_registered_field_is_a_strict_name`` enforces that direction).
STRICT_FIELD_NAMES = frozenset({
    "underlying_trend_slope_50_atr14",
    "underlying_adx_14",
    "underlying_realized_vol_ratio_5_20",
    "structure_dist_support_atr",
    "structure_dist_resistance_atr",
    "structure_support_touches",
    "structure_resistance_touches",
    "channel_slope_20_atr",
    "channel_width_20_atr",
    "channel_pos_20",
    "close_vs_prior_high_20_atr",
    "close_vs_prior_low_20_atr",
    "structure_state",
    "structure_bars_since_bos",
    "structure_bars_since_choch",
})


def market_condition_fields() -> frozenset:
    """The strict names UNION whatever this process's registry declares."""
    from ba2_common.core.market_conditions import PROFILES

    return STRICT_FIELD_NAMES | {f.name for prof in PROFILES.values() for f in prof.fields}


def iter_market_condition_leaves(node: Any, path: str = "rules") -> Iterator[Tuple[str, Mapping]]:
    """Yield ``(label, leaf)`` for every condition leaf under ``node`` naming a market field.

    ``label`` is the leaf's id when it has one, else its path inside ``node`` -- so a message can
    always point at something the reader can find. Walks dicts and lists; does not decode
    JSON-encoded trees (that is ``seam_wiring.market_condition_leaves_in``'s job, for configs).
    """
    fields = market_condition_fields()

    def walk(n: Any, p: str) -> Iterator[Tuple[str, Mapping]]:
        if isinstance(n, dict):
            field = n.get("field")
            if isinstance(field, str) and field in fields:
                yield (str(n["id"]) if n.get("id") else p), n
            for key, value in n.items():
                yield from walk(value, f"{p}.{key}")
        elif isinstance(n, (list, tuple)):
            for i, value in enumerate(n):
                yield from walk(value, f"{p}[{i}]")

    yield from walk(node, path)


def assert_no_market_fields(used: Iterable[Tuple[str, str]], where: str) -> None:
    """Refuse ``(label, field)`` pairs on an exit / open-positions ruleset.

    The pairs form, for the same reason :func:`assert_fields_served` has one: a payload carries
    condition TREES keyed on ``field`` while a live ruleset is persisted as ``EventAction``
    triggers keyed on ``event_type``. One message for both doors.
    """
    hits = [label for label, _ in used]
    if hits:
        raise ValueError(
            f"{where}: market-condition leaves {hits!r} are not allowed in an open-positions / "
            f"exit ruleset. These gates decide whether to ENTER; on an exit rule the live "
            f"resolver has no decision context, so the condition reads 'no_context', the rule "
            f"never fires, and the position's exit or protective-order adjustment silently stops "
            f"happening. Put the gate on the entry ruleset instead.")


def assert_no_market_conditions(rules: Any, where: str) -> None:
    """Refuse a market-condition leaf anywhere in ``rules`` (tree form).

    The pre-B2 blanket exit refusal. Kept, unchanged, for any caller that imports it; the exit
    doors now call :func:`assert_market_rule_actions` instead."""
    assert_no_market_fields(((label, str(leaf.get("field")))
                             for label, leaf in iter_market_condition_leaves(rules, where)), where)


#: What a rule carrying a market-condition leaf may DO (operator decision 2026-09-24, plan
#: 2026-09-24-pullback-and-market-exits Task B2): CLOSE, REDUCE (``decrease_instrument_share``;
#: there is no "reduce" action string) or ADJUST the protective TP/SL orders. Every one of these
#: is safe when the leaf reads UNKNOWN -- the rule simply does not fire and the position keeps
#: its standing protection -- which is what an exit-pass market failure reads since Task B1.
#: Everything else is refused: an open (``buy``/``sell``, every option entry) is an entry
#: decision on the exit pass; ``stop_processing`` would let an unknown read silence the rules
#: BELOW it; a roll / lifecycle / overlay action skipped on an unknown read can leave a short leg
#: to be assigned. ``close_option`` (``ExpertActionType.CLOSE_OPTION``) is a close too: it closes
#: the option position the rule runs on.
MARKET_RULE_ACTIONS = frozenset({
    "close",
    "close_option",
    "decrease_instrument_share",
    "adjust_stop_loss",
    "adjust_take_profit",
})

#: The only boolean group a market leaf may sit under. A nested OR is flattened to AND on the
#: live export (``rule_builders.tree_leaves``), and a NOT -- or any other operator -- would turn
#: "unknown -> does not fire" into "unknown -> fires".
_MARKET_LEAF_GROUP = "AND"


def _group_operator(node: Mapping) -> str:
    """A condition group's boolean, read the way ``rule_models.ConditionGroup`` reads it
    (``operator`` first, then ``type``; absent means AND) but WITHOUT its coercion of an unknown
    operator to AND -- that coercion is exactly what this check must not inherit."""
    return str(node.get("operator") or node.get("type") or _MARKET_LEAF_GROUP).upper()


def _market_leaves_with_ancestry(node: Any, path: str) -> Iterator[Tuple[str, Mapping, Tuple[str, ...]]]:
    """Yield ``(label, leaf, group_operators)`` for every market leaf under ``node``.

    ``group_operators`` lists the boolean of every GROUP between ``node`` and the leaf (a group
    is a dict carrying ``conditions``, as ``rule_models._is_group`` defines it). A bare list is
    transparent: the export flattens it like an AND. Labels match
    :func:`iter_market_condition_leaves` (the leaf id, else its path)."""
    fields = market_condition_fields()

    def walk(n: Any, p: str, ops: Tuple[str, ...]) -> Iterator[Tuple[str, Mapping, Tuple[str, ...]]]:
        if isinstance(n, dict):
            if n.get("conditions") is not None:
                kids = n.get("conditions")
                inner = ops + (_group_operator(n),)
                if isinstance(kids, (list, tuple)):
                    for i, child in enumerate(kids):
                        yield from walk(child, f"{p}.conditions[{i}]", inner)
                else:
                    yield from walk(kids, f"{p}.conditions", inner)
                return
            # ``event_type`` too: ``rule_models.ConditionLeaf`` accepts it as the field's alias,
            # and a raw (un-normalized) list is checked here as well as the normalized one.
            field = n.get("field") or n.get("event_type")
            if isinstance(field, str) and field in fields:
                yield (str(n["id"]) if n.get("id") else p), n, ops
        elif isinstance(n, (list, tuple)):
            for i, child in enumerate(n):
                yield from walk(child, f"{p}[{i}]", ops)

    yield from walk(node, path, ())


def _tree_rule_action_types(rule: Mapping) -> List[str]:
    """The action types of ONE tree-form rule, in every spelling the converters accept.

    New shape: ``actions`` is a list of ActionCfg dicts (``action_type`` / ``action`` /
    ``actionType``, the ``rule_models.ActionCfg`` aliases, in its order). Legacy single-action
    rows carry the same keys at the rule's top level (``rule_models._lift_legacy_rule``).
    An action whose type cannot be read is reported as ``'<missing>'`` -- refused, never skipped.
    """
    def one(cfg: Mapping) -> str:
        at = cfg.get("action_type") or cfg.get("action") or cfg.get("actionType")
        return str(at) if at else "<missing>"

    actions = rule.get("actions")
    if isinstance(actions, list):
        return [one(a) if isinstance(a, Mapping) else "<missing>" for a in actions]
    if any(k in rule for k in ("action_type", "action", "actionType")):
        return [one(rule)]
    return []


def _live_action_types(actions: Any) -> List[str]:
    """The action types of ONE live ``EventAction.actions`` value -- ``{key: {"action_type": ..}}``
    (``type`` accepted as the alias ``rules_convert._live_action_to_action_cfg`` accepts)."""
    values = actions.values() if isinstance(actions, Mapping) else (actions or [])
    out: List[str] = []
    for cfg in values:
        at = (cfg.get("action_type") or cfg.get("type")) if isinstance(cfg, Mapping) else None
        out.append(str(getattr(at, "value", at)) if at else "<missing>")
    return out


def _refuse_market_rule_actions(where: str, rule_label: str, leaves: Sequence[str],
                                action_types: Sequence[str]) -> None:
    offending = [at for at in action_types if at not in MARKET_RULE_ACTIONS]
    if not action_types:
        offending = ["<no action>"]
    if offending:
        raise ValueError(
            f"{where}: rule {rule_label!r} carries market-condition leaves {list(leaves)!r} and "
            f"action(s) {offending!r}, which a market-gated rule may not use. A rule gated on "
            f"market conditions may only CLOSE, REDUCE or ADJUST TP/SL -- allowed actions: "
            f"{sorted(MARKET_RULE_ACTIONS)!r}. When a market condition cannot be read on the "
            f"exit pass it reads unknown and the rule does not fire, which is safe only for those "
            f"actions: an open, a stop_processing, or a roll/lifecycle/overlay action skipped on "
            f"an unknown read changes the position (a skipped roll can leave a short leg to be "
            f"assigned). Split the market condition into its own close/reduce/adjust rule.")


def assert_market_rule_actions(rules: Any, where: str) -> None:
    """Every rule that carries a market-condition leaf may only CLOSE, REDUCE, or ADJUST TP/SL.

    Refuses (ValueError) a market leaf in a rule with any other action (open, roll, lifecycle,
    overlay, stop_processing...), and a market leaf nested under OR/NOT (only a top-level AND tree
    of leaves is allowed: a nested OR silently flattens to AND, and NOT would turn 'unknown ->
    does not fire' into 'unknown -> fires').

    Tree form: ``rules`` is a list of TradeRule dicts (``conditions`` + ``actions``), or legacy
    single-action exit rows. A market leaf may share its AND group with ordinary leaves. A rule
    with no market leaf is not looked at, so an ordinary ruleset is untouched. The message names
    the rule id, the offending actions (or the offending group) and the allowed set.
    """
    for i, rule in enumerate(rules or []):
        if not isinstance(rule, Mapping):
            continue
        rule_label = str(rule.get("id") or rule.get("name") or f"#{i}")
        found = list(_market_leaves_with_ancestry(rule.get("conditions"),
                                                  f"{where}[{rule_label}]"))
        if not found:
            continue
        nested = [(label, [op for op in ops if op != _MARKET_LEAF_GROUP])
                  for label, _leaf, ops in found
                  if any(op != _MARKET_LEAF_GROUP for op in ops)]
        if nested:
            shown = "; ".join(f"{label} under {ops!r}" for label, ops in nested)
            raise ValueError(
                f"{where}: rule {rule_label!r} nests market-condition leaf/leaves under a "
                f"non-AND group ({shown}). A market leaf may only sit in a top-level AND of "
                f"leaves: a nested OR is flattened to AND on the live export, and a NOT (or any "
                f"other operator) would turn 'unknown -> does not fire' into 'unknown -> fires'. "
                f"Write each OR branch as its own rule.")
        _refuse_market_rule_actions(where, rule_label, [label for label, _l, _o in found],
                                    _tree_rule_action_types(rule))


def assert_market_rule_actions_live(rules: Iterable[Tuple[str, Any, Any]], where: str) -> None:
    """:func:`assert_market_rule_actions` for the LIVE format: ``(label, triggers, actions)``.

    A persisted rule is an ``EventAction`` -- ``triggers`` ``{"cond_0": {"event_type": ...}}``
    (always ANDed: the format has no groups, so the OR/NOT half of the tree check has nothing to
    look at) and ``actions`` ``{"a0": {"action_type": ...}}``. Market leaves are the triggers
    whose ``event_type`` is a market field (the ``EventType`` value IS the field name). Same
    allow-list, same message, as the tree form.
    """
    fields = market_condition_fields()
    for rule_label, triggers, actions in rules:
        leaves = [f"{key} ({cfg.get('event_type')})"
                  for key, cfg in (triggers or {}).items()
                  if isinstance(cfg, Mapping) and cfg.get("event_type") in fields]
        if leaves:
            _refuse_market_rule_actions(where, str(rule_label), leaves,
                                        _live_action_types(actions))


def _unresolved_reason(leaf: Mapping) -> str:
    """Why this leaf is not a concrete live condition ("" when it is one)."""
    if leaf.get("mode_optimize") or leaf.get("modeOptimize"):
        return "it still carries mode_optimize (an optimizer template, not a resolved rule)"
    mode = leaf.get("mode")
    if mode is None:
        return ""
    if mode == MODE_OFF:
        return (f"its mode is {MODE_OFF!r}: an off leaf is REMOVED by the decode, so a payload "
                f"that still contains it was not decoded")
    op = leaf.get("comparison") or leaf.get("op") or leaf.get("operator")
    if not op:
        return f"its mode {mode!r} resolved to no operator"
    if op != "==" and leaf.get("value") is None:
        return f"its mode {mode!r} resolved to operator {op!r} but no threshold value"
    return ""


def assert_market_conditions_resolved(rules: Any, where: str) -> None:
    """Refuse an UNRESOLVED market-condition leaf (design section 5: live gets concrete rules)."""
    bad: List[str] = []
    for label, leaf in iter_market_condition_leaves(rules, where):
        reason = _unresolved_reason(leaf)
        if reason:
            bad.append(f"{label} ({leaf.get('field')}): {reason}")
    if bad:
        raise ValueError(
            f"{where}: unresolved market-condition gene(s) -- " + "; ".join(bad) +
            ". Live deployment receives resolved conditions only: export a DECODED genome (the "
            "optimizer's chosen mode written onto the leaf as an ordinary operator/threshold), "
            "never the search template.")


def parse_profile_setting(value: Any, *, setting: str = PROFILE_SETTING) -> Tuple[str, ...]:
    """The profiles named by a ``market_condition_profile`` setting value, in the order given.

    ONE reader for the whole platform: the live per-instance resolver, the backtest seam, the
    launcher's gate builder and the deploy importer all call this, so an unregistered name, a
    repeat or a ``none`` is the same loud ``ValueError`` on every side rather than an empty tuple
    on one of them (which is a strategy running UNGATED under the name of a gated one).

    ``None`` is accepted as empty because that is what ``ExtendableSettingsInterface.settings``
    returns for a defined-but-unset key, not because a missing value is a default: an empty
    setting means "no market-condition data is served", which the refusals below then enforce
    against the expert's rules.

    Raises:
        ValueError: a non-string value, the token ``none``, a repeat, or a name this process's
            registry does not know (the message names ``setting`` and lists what IS registered).
    """
    from ba2_common.core.market_conditions import PROFILES

    if value is None:
        return ()
    if not isinstance(value, str):
        raise ValueError(f"{setting} must be a string (comma-separated profile names), got "
                         f"{type(value).__name__} {value!r}")
    names = [t for t in (part.strip() for part in value.split(",")) if t]
    if not names:
        return ()
    seen: List[str] = []
    for name in names:
        if name.lower() == "none":
            raise ValueError(
                f"{setting}={value!r}: 'none' is not a profile name. Leave the setting empty to "
                f"serve no market-condition data (registered profiles: {sorted(PROFILES)!r}).")
        if name in seen:
            raise ValueError(f"{setting}={value!r} repeats profile {name!r}")
        if name not in PROFILES:
            raise ValueError(
                f"{setting}={value!r} names {name!r}, which is not a registered market-condition "
                f"profile on this installation (registered: {sorted(PROFILES)!r}). A payload "
                f"built against a newer ba2_common must not be deployed onto an older one: the "
                f"gate would have no data and the strategy would never enter.")
        seen.append(name)
    return tuple(seen)


def served_fields(profiles: Sequence[str]) -> frozenset:
    """Every field name the listed profiles serve (empty for an empty list)."""
    from ba2_common.core.market_conditions import PROFILES

    return frozenset(f.name for p in profiles for f in PROFILES[p].fields)


def assert_fields_served(used: Iterable[Tuple[str, str]], profiles: Sequence[str], *,
                         where: str = "rules", setting: str = PROFILE_SETTING) -> None:
    """Refuse ``(label, field)`` pairs the listed profiles do not serve.

    The pairs form exists because the two callers read two different vocabularies for the same
    thing: a deploy payload carries condition TREES keyed on ``field``, while a live ruleset is
    persisted as ``EventAction.triggers`` keyed on ``event_type`` (equal to the field name by
    ``rule_builders.register_market_condition_field_events``). Both end here so the refusal is
    one message.
    """
    profiles = tuple(profiles)
    served = served_fields(profiles)
    bad = [(label, field) for label, field in used if field not in served]
    if not bad:
        return
    shown = "; ".join(f"{label} ({field})" for label, field in bad)
    if profiles:
        tail = (f"{setting} lists {list(profiles)!r}, which serves {sorted(served)!r}. Add the "
                f"profile that owns the field to the setting, or take the leaf off the ruleset.")
    else:
        tail = (f"{setting} is empty, so NO market-condition data is served for this expert. Set "
                f"it to the profile(s) these leaves were built for, or take them off the ruleset.")
    raise ValueError(
        f"{where}: market-condition leaf/leaves {shown} name a field no configured profile "
        f"serves. {tail} A gate with no data behind it never passes, so the strategy would be "
        f"deployed unable to enter -- which looks exactly like one that found no setup.")


def assert_market_fields_served(rules: Any, profiles: Sequence[str], *, where: str = "rules",
                                setting: str = PROFILE_SETTING) -> None:
    """Refuse a condition TREE whose market leaves the listed profiles do not serve."""
    assert_fields_served(((label, str(leaf.get("field")))
                          for label, leaf in iter_market_condition_leaves(rules, where)),
                         profiles, where=where, setting=setting)

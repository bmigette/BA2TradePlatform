"""Joint optimization parameter space for strategy/expert optimization.

Collects ONE flat param_ranges dict (the GeneticOptimizer shape
{name: {'type','min','max','step'}}) from a Strategy row + expert numeric
settings, and decodes a flat decoded-params dict back into concrete
``entry_rules``/``exit_rules`` TradeRule lists by deep-copying the rules and
substituting condition values / action values by id. The Strategy row is never
mutated.

The Strategy is the unified EventAction-shaped model (migration 028, see
docs/plans/2026-07-08-unified-rule-model.md): ``Strategy.entry_rules`` and
``Strategy.exit_rules`` are ordered TradeRule lists — each rule = conditions
tree + one-or-more actions + continue_processing. Genes are derived per rule
and per action, so a per-rule bracket (live's per-tier TP/SL) optimizes
independently per rule.

RM sizing is optimized through the expert ``model:*`` path keyed by the REAL
ba2 setting names (e.g. ``risk_per_trade_pct``); there is no separate rm
namespace.

Namespacing:
  model:<p>                        expert numeric decision settings (incl. RM sizing)
  cond:<id>:value                  a condition node's threshold (any rule's tree)
  cond:<id>:confirmation_bars      that node's confirmation bars
  cond:<id>:enabled                that node's ON/OFF toggle
  entry:<rid>:enabled              entry rule ON/OFF toggle (rule.toggle_optimize)
  entry:<rid>:a<i>:action_value    entry rule action i's value
  entry:<rid>:a<i>:enabled         entry rule action i's ON/OFF toggle
  exit:<rid>:enabled               exit rule ON/OFF toggle
  exit:<rid>:a<i>:action_value     exit rule action i's value
  exit:<rid>:a<i>:enabled          exit rule action i's ON/OFF toggle
  exit:<rid>:a<i>:option_strike_param  option strike PERCENT-OTM (option actions; was
                                       misnamed option_delta, still accepted on decode)
  exit:<rid>:a<i>:option_strike_method strike selection method (choice: percent_otm | delta)
  exit:<rid>:a<i>:option_strike_delta  option strike DELTA (used when the method is delta;
                                   the SHORT leg when a _long companion is declared)
  exit:<rid>:a<i>:option_strike_delta_long  the LONG leg's DELTA for a two-leg builder that
                                   targets its legs independently (backspreads)
  exit:<rid>:a<i>:option_structure which option ACTION TYPE the entry submits (choice)
  exit:<rid>:a<i>:option_dte       option DTE window center
  exit:<rid>:a<i>:option_wing_width  option wing width %
  exit:<rid>:a<i>:option_sizing    option position size (% of equity per structure)
  exit:<rid>:a<i>:option_min_arc   minimum annualised return on collateral a CREDIT
                                   structure must offer (fraction; credit actions only)
  exit:<rid>:a<i>:option_entry_cross  fraction of the contract's own MODELLED bid-ask
                                   spread the entry gives up when it quotes (0 = mid,
                                   1 = the far touch the fill engine models)
  optsel:<half>:<w>                a SelectionPolicy weight (w_premium | w_iv | w_rvol),
                                   SHARED across every option entry action of one
                                   debit/credit half (keyed on the action's stamped
                                   option_selection_half, NOT on the rule id) -- one gene
                                   per half per weight, identical keys in single-member and
                                   group jobs so stage-1 winners seed the stage-2 space
  schedule:<day>                   ON/OFF toggle for that weekday's entry scan
  screener:<setting>               screener settings

The pre-028 namespaces (``exit:<id>:action_value`` with the action fields on
the rule itself, ``entry:<id>:*`` for the flat entry_actions list) are decoded
nowhere here anymore — saved rows carrying them are reconstructed by the
quick-load path, not by this module.
"""
import copy
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Fixed order so the gene list (and therefore reproducibility) is stable across runs.
SCHEDULE_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

#: The SelectionPolicy weights emitted as shared per-half genes (``optsel:<half>:<w>``).
#: Deliberately NOT w_spread (the parquet store this grid reads synthesises bid == ask, so
#: spread_pct is a constant 0.0 that scores uniformly BEST -- it fails OPEN, not closed),
#: NOT w_rr (F15: collinear with premium within a chain), NOT w_profit (needs a structure_fn
#: no builder supplies yet) -- see the launcher's _OPTION_SELECTION_WEIGHT_BANDS for the
#: full evidence trail, including why an option job cannot run on the sqlite store at all.
OPTION_SELECTION_WEIGHTS = ("w_premium", "w_iv", "w_rvol")

# Actions that must NEVER be dropped by a per-action toggle: removing the open action would
# turn an entry rule into a no-op bracket; guards must stay. (Rule-level toggles can still
# drop the whole rule.)
_UNDROPPABLE_ACTIONS = {"buy", "sell"}


def _range_entry(min_v, max_v, step_v, is_int: bool) -> Dict[str, Any]:
    """Build one GeneticOptimizer range entry; fail-early on missing bounds."""
    if min_v is None or max_v is None or step_v is None:
        raise ValueError(f"range requires min/max/step, got {min_v}/{max_v}/{step_v}")
    return {
        "type": "int" if is_int else "float",
        "min": int(min_v) if is_int else float(min_v),
        "max": int(max_v) if is_int else float(max_v),
        "step": int(step_v) if is_int else float(step_v),
    }


def _collect_expert(expert_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """model:<p> ranges from per-expert numeric settings marked optimize=True.

    expert_cfg shape: {param_name: {'optimize': bool,'min','max','step','type'}}.
    """
    out: Dict[str, Any] = {}
    if not expert_cfg:
        return out
    for name, spec in expert_cfg.items():
        if spec and spec.get("optimize"):
            if spec.get("type") == "choice":
                # Categorical expert setting (e.g. FMPRating target_price_type). Encoded as an
                # int index into 'choices'; the GA evolves the index and decode_individual maps
                # it back to the choice VALUE, which flows through model:<name> -> expert_overrides.
                choices = list(spec["choices"])
                out[f"model:{name}"] = {
                    "type": "choice", "choices": choices,
                    "min": 0, "max": len(choices) - 1, "step": 1,
                }
                continue
            is_int = spec.get("type") == "int"
            out[f"model:{name}"] = _range_entry(spec.get("min"), spec.get("max"),
                                                spec.get("step"), is_int=is_int)
    return out


def _collect_screener(screener_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """screener:<setting> ranges from a screener_cfg ({setting: {min,max,step,type,optimize}})."""
    out: Dict[str, Any] = {}
    for name, spec in (screener_cfg or {}).items():
        if not spec or not spec.get("optimize"):
            continue
        is_int = spec.get("type") == "int"
        out[f"screener:{name}"] = _range_entry(spec.get("min"), spec.get("max"),
                                               spec.get("step"), is_int=is_int)
    return out


def _collect_schedule_days(schedule_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """schedule:<day> ON/OFF toggle genes from a schedule_cfg ({day: {optimize: bool}}).

    One boolean gene per weekday the GA can independently flip. ``decode_params`` enforces at
    least one day stays ON (an all-OFF individual would never scan for entries at all — a dead
    config, not a legitimately weak one)."""
    out: Dict[str, Any] = {}
    for day, spec in (schedule_cfg or {}).items():
        if day in SCHEDULE_DAYS and spec and spec.get("optimize"):
            out[f"schedule:{day}"] = _range_entry(0, 1, 1, is_int=True)
    return out


def _walk_condition_nodes(cond: Optional[Dict[str, Any]], out: Dict[str, Any]) -> None:
    """Emit cond:<id>:value / :confirmation_bars / :enabled for optimizable nodes.

    AND/OR nodes recurse via 'conditions'; leaf nodes carry id + value + optimize flags.
    """
    if not isinstance(cond, dict):
        return
    for child in (cond.get("conditions") or []):
        _walk_condition_nodes(child, out)
    cid = cond.get("id")
    if not cid:
        return
    if cond.get("optimize") or cond.get("optimize_enabled"):
        out[f"cond:{cid}:value"] = _range_entry(
            cond.get("value_min"), cond.get("value_max"), cond.get("value_step"),
            is_int=False,
        )
    if cond.get("confirmation_bars_min") is not None:
        out[f"cond:{cid}:confirmation_bars"] = _range_entry(
            cond.get("confirmation_bars_min"), cond.get("confirmation_bars_max"),
            cond.get("confirmation_bars_step"), is_int=True,
        )
    if cond.get("toggle_optimize"):
        out[f"cond:{cid}:enabled"] = _range_entry(0, 1, 1, is_int=True)


def _validate_value_offsets(tree: Optional[Dict[str, Any]], where: str) -> None:
    """Fail EARLY (once per job, at gene collection) on a dangling ``value_offset_from``.

    Without this the dangling reference only surfaces per-trial inside ``_apply_to_tree``,
    i.e. as N identical crashed trials rather than one actionable message.
    """
    known: Dict[str, Any] = {}
    _collect_authored_values(tree, known)
    refs: Dict[str, str] = {}

    def _walk(node):
        if not isinstance(node, dict):
            return
        for child in (node.get("conditions") or []):
            _walk(child)
        base = node.get("value_offset_from")
        if base is not None:
            refs[str(node.get("id"))] = str(base)

    _walk(tree)
    bad = {cid: base for cid, base in refs.items() if base not in known}
    if bad:
        raise ValueError(
            f"{where}: value_offset_from references no threshold-carrying leaf in the same "
            f"condition tree: {bad}. A relative threshold with an unresolvable base is "
            f"unmeasurable, not zero."
        )


def _collect_action_genes(ns: str, rid: str, idx: int, action: Dict[str, Any],
                          out: Dict[str, Any]) -> None:
    """Genes for ONE action of a rule: value, per-action toggle, and option selection params."""
    prefix = f"{ns}:{rid}:a{idx}"
    if action.get("action_value_optimize"):
        out[f"{prefix}:action_value"] = _range_entry(
            action.get("action_value_min"), action.get("action_value_max"),
            action.get("action_value_step"), is_int=False,
        )
    at = str(action.get("action_type") or action.get("action") or "")
    if action.get("toggle_optimize") and at not in _UNDROPPABLE_ACTIONS:
        out[f"{prefix}:enabled"] = _range_entry(0, 1, 1, is_int=True)
    # OPTION action selection params: strike param / strike method / delta / DTE / wing width.
    #
    # NAMING (OPT-C3). This gene used to be emitted as ``option_delta`` while carrying
    # PERCENT-OTM values in every range the grid declared -- two quantities, one name. It is
    # now ``option_strike_param``, which is the field it actually writes; the real delta is
    # ``option_strike_delta``. ``option_delta`` is still ACCEPTED on decode (see
    # _decode_rule_list) as the legacy spelling of the percent param, so a persisted
    # best-params blob or a warm start from an older optimization still applies -- silently
    # re-reading it as a delta would turn a "6" into a 6-delta lookup, i.e. deep ITM.
    if action.get("option_strike_param_optimize"):
        out[f"{prefix}:option_strike_param"] = _range_entry(
            action.get("option_strike_param_min"),
            action.get("option_strike_param_max"),
            action.get("option_strike_param_step"), is_int=False,
        )
    # STRIKE METHOD as a categorical gene (percent_otm | delta | ...). percent_otm is
    # volatility-BLIND -- 5 % OTM on a 15-vol utility and on a 90-vol biotech are not the same
    # proposition -- while delta is normalised across symbols and is the live default. Emitted
    # only where the producer asked for it; the producer is responsible for asking only on
    # actions whose builder actually reads strike_method (types.honours_strike_method), since
    # eight of the nineteen builders hard-code percent_otm and would make this gene inert.
    if action.get("option_strike_method_optimize"):
        choices = list(action["option_strike_method_choices"])
        if len(choices) < 2:
            raise ValueError(
                f"{prefix}: option_strike_method_optimize needs >= 2 choices, got {choices}")
        # A delta choice is meaningless without a delta-scaled parameter to go with it: the
        # percent range (e.g. 0..8) read as a delta target picks the deepest-ITM contract on
        # the chain. Fail here rather than silently mis-select for a whole campaign.
        if "delta" in choices and action.get("option_strike_delta_min") is None:
            raise ValueError(
                f"{prefix}: option_strike_method_choices offers 'delta' but the action "
                f"declares no option_strike_delta_min/_max/_step, so the percent-OTM range "
                f"would be used as a delta target")
        out[f"{prefix}:option_strike_method"] = {
            "type": "choice", "choices": choices,
            "min": 0, "max": len(choices) - 1, "step": 1,
        }
    if action.get("option_strike_delta_optimize"):
        out[f"{prefix}:option_strike_delta"] = _range_entry(
            action.get("option_strike_delta_min"),
            action.get("option_strike_delta_max"),
            action.get("option_strike_delta_step"), is_int=False,
        )
    # THE SECOND LEG's delta, for the two-leg builders that target the legs INDEPENDENTLY.
    #
    # ``option_strike_delta`` alone cannot express a backspread (grid-2 O_CBS/O_PBS, design
    # 2026-08-31 §2: short leg 0.35-0.50, long leg 0.15-0.30). ``_spread_params`` on the
    # builder side already accepts a per-leg pair -- ``[long, short]`` or
    # ``{"long":..,"short":..}`` -- and every vertical/backspread builder reads it; what was
    # missing was a GENE for the second target, so a single value had to serve both legs and
    # the selector merely picked the two nearest strikes to ONE delta. That is not the
    # structure the design searches.
    #
    # ``option_strike_delta`` is the SHORT leg and this is the LONG leg whenever both are
    # declared (see ``_apply_option_strike``); a builder declaring only the first is
    # unchanged, so no existing action's genome moves.
    if action.get("option_strike_delta_long_optimize"):
        out[f"{prefix}:option_strike_delta_long"] = _range_entry(
            action.get("option_strike_delta_long_min"),
            action.get("option_strike_delta_long_max"),
            action.get("option_strike_delta_long_step"), is_int=False,
        )
    # STRUCTURE as a categorical gene: which option ACTION TYPE the entry submits.
    #
    # Grid-2's ``O_ERN`` searches "straddle or strangle" (design §2), which is a choice
    # between two BUILDERS rather than between two parameter values. Expressed as one choice
    # gene on the action rather than as two toggleable entry rules because the alternative
    # duplicates the entry rule's whole gate set (signal/iv_rank/iv_rv/expected_profit) for a
    # single either/or, and both rules' ``has_no_position`` guard means only one could ever
    # fire anyway -- so the second copy would be pure gene budget at a population of 40.
    #
    # The producer is responsible for offering only OPTION action types (the decode below
    # writes ``action_type`` verbatim); ``_UNDROPPABLE_ACTIONS`` and the rest of the action
    # machinery are keyed off that field, so an equity value here would be a category error.
    if action.get("option_structure_optimize"):
        choices = list(action["option_structure_choices"])
        if len(choices) < 2:
            raise ValueError(
                f"{prefix}: option_structure_optimize needs >= 2 choices, got {choices}")
        from ba2_common.core.types import is_option_action
        non_option = [c for c in choices if not is_option_action(str(c))]
        if non_option:
            raise ValueError(
                f"{prefix}: option_structure_choices must all be OPTION action types; "
                f"{non_option} are not")
        out[f"{prefix}:option_structure"] = {
            "type": "choice", "choices": choices,
            "min": 0, "max": len(choices) - 1, "step": 1,
        }
    if action.get("option_dte_optimize"):
        out[f"{prefix}:option_dte"] = _range_entry(
            action.get("option_dte_min_range"),
            action.get("option_dte_max_range"),
            action.get("option_dte_step"), is_int=True,
        )
    if action.get("option_wing_width_optimize"):
        out[f"{prefix}:option_wing_width"] = _range_entry(
            action.get("option_wing_width_min"),
            action.get("option_wing_width_max"),
            action.get("option_wing_width_step"), is_int=False,
        )
    # POSITION SIZE (% of equity per structure). Bounded and symbol-comparable -- the same
    # category as the other option genes -- but it was a per-structure CONSTANT the GA could
    # not touch. It also gates any return-on-collateral fitness: contracts x max_loss IS
    # option_sizing % of equity by construction, so with sizing frozen that ratio divides by a
    # constant and degenerates back into plain return.
    if action.get("option_sizing_optimize"):
        out[f"{prefix}:option_sizing"] = _range_entry(
            action.get("option_sizing_min"),
            action.get("option_sizing_max"),
            action.get("option_sizing_step"), is_int=False,
        )
    # PREMIUM RICHNESS (OPT-C1): the minimum per-contract annualised return on collateral a
    # CREDIT structure must offer, as a FRACTION (0.15 == 15 %/yr). The producer emits this
    # ONLY for structures that post collateral -- a debit structure has no denominator, so a
    # configured floor would turn its unmeasurable ARC into a blanket refusal.
    if action.get("option_min_arc_optimize"):
        out[f"{prefix}:option_min_arc"] = _range_entry(
            action.get("option_min_arc_min"),
            action.get("option_min_arc_max"),
            action.get("option_min_arc_step"), is_int=False,
        )
    # ENTRY-QUOTE CONCESSION (F3): what fraction of the contract's own MODELLED spread the
    # entry gives up when it quotes. The default fill model (next_bar_open) makes the NEXT
    # bar cross a quote struck at the ANALYSIS bar, and the historical store's bid==ask puts
    # that quote at the MID -- so an entry has to earn the whole modelled spread back
    # overnight before anything fills, and premium sellers structurally almost never do.
    # 0.0 is the pre-F3 quote exactly; 1.0 is the touch the fill engine already models.
    if action.get("option_entry_cross_optimize"):
        out[f"{prefix}:option_entry_cross"] = _range_entry(
            action.get("option_entry_cross_min"),
            action.get("option_entry_cross_max"),
            action.get("option_entry_cross_step"), is_int=False,
        )
    # SELECTION-POLICY WEIGHTS, SHARED PER HALF. The key is ``optsel:<half>:<w>`` -- built
    # from the action's stamped ``option_selection_half``, NOT from ``prefix`` -- so every
    # member action of one half collapses onto ONE gene per weight (dict identity), and a
    # single-member job emits exactly the keys the group job searches (the seeding
    # requirement; encode_params silently drops keys the target space lacks). Two guards:
    # a flag with no half has nothing to share on and must not silently fall back to a
    # per-rule key shape, and two members declaring different domains for one shared key
    # would otherwise resolve by dict-overwrite, last member silently winning.
    for w in OPTION_SELECTION_WEIGHTS:
        if not action.get(f"option_{w}_optimize"):
            continue
        half = action.get("option_selection_half")
        if half not in ("debit", "credit"):
            raise ValueError(
                f"{prefix}: option_{w}_optimize is set but option_selection_half is "
                f"{half!r}; a selection-weight gene is shared per debit/credit half and "
                f"cannot be emitted without one")
        key = f"optsel:{half}:{w}"
        spec = _range_entry(action.get(f"option_{w}_min"), action.get(f"option_{w}_max"),
                            action.get(f"option_{w}_step"), is_int=False)
        if key in out and out[key] != spec:
            raise ValueError(
                f"{prefix}: conflicting domains for shared gene {key}: {out[key]} vs "
                f"{spec}; members of one half must declare identical bands")
        out[key] = spec


def _collect_rule_list(rules, ns: str, out: Dict[str, Any]) -> None:
    """cond:* + rule/action genes across ONE TradeRule list (ns = 'entry' or 'exit')."""
    for rule in (rules or []):
        if not isinstance(rule, dict):
            continue
        rid = rule.get("id")
        if rid and rule.get("toggle_optimize"):
            out[f"{ns}:{rid}:enabled"] = _range_entry(0, 1, 1, is_int=True)
        if rid:
            for idx, action in enumerate(a for a in (rule.get("actions") or [])
                                         if isinstance(a, dict)):
                _collect_action_genes(ns, rid, idx, action, out)
        _validate_value_offsets(rule.get("conditions"), f"{ns} rule {rid!r}")
        _walk_condition_nodes(rule.get("conditions"), out)


def _collect_conditions(strategy) -> Dict[str, Any]:
    """All cond:*/entry:*/exit:* genes across the strategy's two rule lists."""
    out: Dict[str, Any] = {}
    _collect_rule_list(getattr(strategy, "entry_rules", None), "entry", out)
    _collect_rule_list(getattr(strategy, "exit_rules", None), "exit", out)
    return out


def collect_param_space(
    strategy,
    expert_cfg: Optional[Dict[str, Any]] = None,
    bypass: bool = False,
    screener_cfg: Optional[Dict[str, Any]] = None,
    schedule_cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return the flat joint param_ranges dict for GeneticOptimizer.

    Merges expert (model:*, including RM sizing settings) + rule/action/condition
    (cond:*/entry:*/exit:*) ranges. Key order is deterministic (model, rules) so the gene
    list is stable across runs — required for reproducibility.

    BYPASS experts: when ``bypass`` is True the strategy/expert does NOT use the classic RM
    or the enter/exit ruleset (e.g. FactorRanker rebalances to target weights via its own
    portfolio manager). The search space is restricted to the expert's OWN params (model:*)
    ONLY — cond:*/entry:*/exit:* and schedule:* are EXCLUDED (no effect on the rebalance
    path). ``screener:*`` genes apply on BOTH paths.
    """
    space: Dict[str, Any] = {}
    space.update(_collect_expert(expert_cfg))
    if not bypass:
        space.update(_collect_conditions(strategy))
        space.update(_collect_schedule_days(schedule_cfg))
    space.update(_collect_screener(screener_cfg))
    if not space:
        raise ValueError(
            "No optimizable parameters found: "
            + (
                "a bypass expert searches only its own params — mark at least one expert "
                "param optimize=True."
                if bypass
                else "mark at least one of expert settings, entry/exit rule conditions "
                "or actions optimize=True."
            )
        )
    logger.info(
        f"Collected {'bypass ' if bypass else ''}joint param space: "
        f"{len(space)} params: {list(space.keys())}"
    )
    return space


def _collect_authored_values(tree: Optional[Dict[str, Any]], out: Dict[str, Any]) -> None:
    """Map node id -> its AUTHORED (template) ``value``, for ``value_offset_from`` resolution."""
    if not isinstance(tree, dict):
        return
    for child in (tree.get("conditions") or []):
        _collect_authored_values(child, out)
    cid = tree.get("id")
    if cid is not None and "value" in tree:
        out[cid] = tree["value"]


def _apply_to_tree(tree: Optional[Dict[str, Any]], by_id: Dict[str, Dict[str, Any]]
                   ) -> Optional[Dict[str, Any]]:
    """Deep-copy a condition tree, substituting value/confirmation_bars by node id and
    dropping toggle-disabled nodes. The input tree is never mutated.

    ``value_offset_from`` — RELATIVE thresholds (see OPT-C5)
    -------------------------------------------------------
    A leaf may declare ``"value_offset_from": "<other leaf id>"``, in which case its
    ``cond:<id>:value`` gene is a **width above that leaf's threshold**, not an absolute
    threshold: the decoded value is ``resolved(other) + gene``.

    This exists because two leaves testing the SAME field with opposing operators
    (``x > a`` AND ``x < b``) are an interval, and as two INDEPENDENT absolute genes roughly
    half their joint grid is ``b <= a`` — an empty conjunction that guarantees zero trades for
    every symbol on every bar, scoring the identical zero-trade sentinel. Measured on the
    built ``O_LC`` entry rule before this change: **25.8 % of the four price-vs-target gates'
    joint gene space was guaranteed-empty, including the authored all-zero default** that
    warm-start and every hand-seeded individual begins from. Re-parameterising the upper bound
    as (lower bound + width >= step) makes every point in the grid a live interval, with no
    loss of expressiveness — each bound still has its own independent ON/OFF gene, so
    lower-only, upper-only and band patterns all remain reachable.

    The AUTHORED ``value`` on an offset leaf stays ABSOLUTE (so an un-decoded template still
    seeds a correct ruleset); only ``value_min/max/step`` — which nothing but the gene
    collector reads — describe the width.

    A dangling reference RAISES rather than defaulting the base to 0: silently treating an
    unresolvable base as zero would turn a width gene back into an absolute threshold and
    quietly reintroduce exactly the empty conjunctions this mechanism removes.
    """
    if tree is None:
        return None
    new = copy.deepcopy(tree)
    authored: Dict[str, Any] = {}
    _collect_authored_values(new, authored)

    def _resolved(cid: str) -> Any:
        """The other leaf's EFFECTIVE threshold: its decoded gene when it has one, else its
        authored value. Read from the gene map (not from the tree) so resolution does not
        depend on recursion order, and so a base leaf dropped by its own ON/OFF gene still
        anchors the offset."""
        sub = by_id.get(cid)
        if sub is not None and "value" in sub:
            return sub["value"]
        return authored.get(cid)

    def _recurse(node):
        if not isinstance(node, dict):
            return
        kids = node.get("conditions")
        if kids:
            kept = []
            for child in kids:
                ccid = child.get("id") if isinstance(child, dict) else None
                # ON/OFF toggle: a child whose 'enabled' gene decoded to 0 is dropped.
                if ccid and by_id.get(ccid, {}).get("enabled") == 0:
                    continue
                _recurse(child)
                kept.append(child)
            node["conditions"] = kept
        cid = node.get("id")
        if cid and cid in by_id:
            sub = by_id[cid]
            if "value" in sub:
                base_id = node.get("value_offset_from")
                if base_id is None:
                    node["value"] = sub["value"]
                else:
                    base = _resolved(base_id)
                    if base is None:
                        raise ValueError(
                            f"condition {cid!r} declares value_offset_from="
                            f"{base_id!r}, which resolves to no value (no such leaf, or "
                            f"that leaf carries no threshold). A relative threshold with "
                            f"an unresolvable base is unmeasurable, not zero."
                        )
                    node["value"] = float(base) + float(sub["value"])
            if "confirmation_bars" in sub:
                node["confirmation_bars"] = sub["confirmation_bars"]

    _recurse(new)
    return new


def _apply_option_dte(action: Dict[str, Any], center_val: Any) -> None:
    """option_dte gene tunes the DTE WINDOW CENTER; keep a half-width so the [min, max] span
    covers real (weekly) expiries instead of a single impossible day."""
    center = int(round(center_val))
    base_hw = 0
    try:
        bmin = action.get("option_dte_min")
        bmax = action.get("option_dte_max")
        if bmin is not None and bmax is not None and bmax > bmin:
            base_hw = int((bmax - bmin) // 2)
    except Exception:  # noqa: BLE001 - defensive: malformed base window -> default hw
        base_hw = 0
    hw = max(base_hw, 7)  # at least +/-7 days so a weekly expiry falls in-window
    action["option_dte_min"] = max(0, center - hw)
    action["option_dte_max"] = center + hw


def _apply_option_strike(action: Dict[str, Any], agenes: Dict[str, Any]) -> None:
    """Write the decoded strike METHOD and the matching strike PARAM onto an option action.

    ``option_strike_param`` (percent OTM) and ``option_strike_delta`` are two different
    quantities on two different scales, and the action carries exactly one
    ``option_strike_param`` field that the selector interprets ACCORDING TO the method. So the
    param that lands on the action must be the one belonging to the EFFECTIVE method -- the
    decoded ``option_strike_method`` gene when there is one, else the action's authored method.
    Writing the percent value under a ``delta`` method targets a 6-delta contract when 6 % OTM
    was meant (deep ITM); writing the delta under ``percent_otm`` targets 0.3 % OTM when a
    0.30 delta was meant (at the money). Both mis-selections are silent.

    ``option_delta`` is the LEGACY gene name for the percent param (it never carried a delta,
    despite the name -- OPT-C3). Accepted so persisted best-params blobs and warm starts from
    older optimizations still decode to what they meant.

    PER-LEG TARGETS. A two-leg builder that aims its legs INDEPENDENTLY (the grid-2
    backspreads: short 0.35-0.50, long 0.15-0.30) declares BOTH ``option_strike_delta`` (the
    SHORT leg) and ``option_strike_delta_long``; the decoded pair is written as the
    ``[long, short]`` sequence ``TradeActions._spread_params`` already destructures -- the
    ordering it has used since it was promoted to the base class, and the ordering
    ``test_backspread_builders``'s ``DELTAS = [0.20, 0.40]`` pins. A single value stays a
    single value, so no existing action's decode moves.
    """
    method = agenes.get("option_strike_method")
    if method is not None:
        action["option_strike_method"] = method
    effective = str(method if method is not None
                    else (action.get("option_strike_method") or "percent_otm"))
    if effective == "delta":
        long_delta = agenes.get("option_strike_delta_long",
                                action.get("option_strike_delta_long"))
        if long_delta is not None:
            short_delta = agenes.get("option_strike_delta",
                                     action.get("option_strike_delta"))
            if short_delta is None:
                raise ValueError(
                    f"option action {action.get('action_type')!r} declares a LONG-leg delta "
                    f"but no short-leg delta; a per-leg pair needs both targets")
            action["option_strike_param"] = [long_delta, short_delta]
            return
        if "option_strike_delta" in agenes:
            action["option_strike_param"] = agenes["option_strike_delta"]
        elif action.get("option_strike_delta") is not None:
            # The method gene chose delta but the delta itself is not searched: use the
            # action's authored delta rather than leaving the percent value in place.
            action["option_strike_param"] = action["option_strike_delta"]
        elif "option_strike_param" in agenes or "option_delta" in agenes:
            raise ValueError(
                f"option action {action.get('action_type')!r} decoded to strike_method="
                f"'delta' but carries no delta to select with; the percent-OTM gene would be "
                f"read as a delta target"
            )
        return
    if "option_strike_param" in agenes:
        action["option_strike_param"] = agenes["option_strike_param"]
    elif "option_delta" in agenes:  # legacy spelling of the percent param
        action["option_strike_param"] = agenes["option_delta"]


def _decode_rule_list(rules, ns: str,
                      rule_genes: Dict[str, Dict[str, Any]],
                      cond_by_id: Dict[str, Dict[str, Any]],
                      optsel_by_half: Optional[Dict[str, Dict[str, Any]]] = None):
    """Deep-copy ONE TradeRule list applying decoded genes: drop toggle-disabled rules,
    substitute per-action values / option params, drop toggle-disabled (non-open) actions,
    apply the shared per-half selection-weight genes, and substitute condition values."""
    out = []
    for rule in copy.deepcopy(rules or []):
        if not isinstance(rule, dict):
            out.append(rule)
            continue
        rid = rule.get("id")
        genes = rule_genes.get(rid or "", {})
        if genes.get("enabled") == 0:
            continue  # whole rule dropped by the GA
        actions = []
        for idx, action in enumerate(a for a in (rule.get("actions") or [])
                                     if isinstance(a, dict)):
            agenes = genes.get(f"a{idx}", {})
            at = str(action.get("action_type") or action.get("action") or "")
            if agenes.get("enabled") == 0 and at not in _UNDROPPABLE_ACTIONS:
                continue  # this action dropped by the GA
            if "action_value" in agenes:
                action["action_value"] = agenes["action_value"]
                action["value"] = agenes["action_value"]
            # STRUCTURE first: the action TYPE decides which builder the rest of the
            # option params are read by, so it must be settled before they are written.
            if "option_structure" in agenes:
                action["action_type"] = agenes["option_structure"]
            _apply_option_strike(action, agenes)
            if "option_dte" in agenes:
                _apply_option_dte(action, agenes["option_dte"])
            if "option_wing_width" in agenes:
                action["option_wing_width_pct"] = agenes["option_wing_width"]
            if "option_sizing" in agenes:
                action["option_sizing"] = agenes["option_sizing"]
            if "option_min_arc" in agenes:
                action["option_min_arc"] = agenes["option_min_arc"]
            if "option_entry_cross" in agenes:
                action["option_entry_cross"] = agenes["option_entry_cross"]
            # Shared per-half selection weights: applied to EVERY action stamped with the
            # matching half (that is what "shared" means mechanically); an action with no
            # stamp -- every equity action, and the O_CC/O_PP overlays -- is untouched.
            half = action.get("option_selection_half")
            if optsel_by_half and half in optsel_by_half:
                for w, wval in optsel_by_half[half].items():
                    action[f"option_{w}"] = wval
            actions.append(action)
        rule["actions"] = actions
        if rule.get("conditions"):
            rule["conditions"] = _apply_to_tree(rule["conditions"], cond_by_id)
        out.append(rule)
    return out


def decode_params(strategy, flat_params: Dict[str, Any]) -> Dict[str, Any]:
    """Reconstruct a concrete trial config from a decoded flat params dict.

    The flat dict comes from GeneticOptimizer.decode_individual (namespaced keys — see the
    module docstring). Returns::

      {
        'expert_overrides': {param: value},       # model:* stripped of prefix (incl. RM sizing)
        'screener_overrides': {setting: value},
        'schedule_days': {day: bool}|None,        # None when no schedule:* genes were collected
        'entry_rules': list,                      # concrete TradeRule lists (genes applied,
        'exit_rules': list,                       #  disabled rules/actions pruned)
      }

    The source Strategy is NEVER mutated (rules are deep-copied).
    """
    cond_by_id: Dict[str, Dict[str, Any]] = {}
    entry_genes: Dict[str, Dict[str, Any]] = {}
    exit_genes: Dict[str, Dict[str, Any]] = {}
    expert_overrides: Dict[str, Any] = {}
    screener_overrides: Dict[str, Any] = {}
    schedule_by_day: Dict[str, Any] = {}
    optsel_by_half: Dict[str, Dict[str, Any]] = {}

    def _rule_gene(store: Dict[str, Dict[str, Any]], rid: str, rest: str, val: Any) -> None:
        genes = store.setdefault(rid, {})
        if rest == "enabled":
            genes["enabled"] = val
        elif rest.startswith("a") and ":" in rest:
            aid, field = rest.split(":", 1)
            genes.setdefault(aid, {})[field] = val
        else:
            raise ValueError(f"Unknown rule gene field {rest!r} for rule {rid!r}")

    for key, val in flat_params.items():
        if key.startswith("model:"):
            expert_overrides[key[len("model:"):]] = val
        elif key.startswith("screener:"):
            screener_overrides[key[len("screener:"):]] = val
        elif key.startswith("schedule:"):
            schedule_by_day[key[len("schedule:"):]] = bool(val)
        elif key.startswith("cond:"):
            _, cid, field = key.split(":", 2)
            cond_by_id.setdefault(cid, {})[field] = val
        elif key.startswith("entry:"):
            _, rid, rest = key.split(":", 2)
            _rule_gene(entry_genes, rid, rest, val)
        elif key.startswith("exit:"):
            _, rid, rest = key.split(":", 2)
            _rule_gene(exit_genes, rid, rest, val)
        elif key.startswith("optsel:"):
            _, half, w = key.split(":", 2)
            optsel_by_half.setdefault(half, {})[w] = val
        else:
            raise ValueError(f"Unknown decoded param namespace: {key!r}")

    # None (no unified-model template on this Strategy -- legacy buy_tree/exit_conditions
    # path) is preserved as None, NOT coerced to []: downstream (daily_backtest_handler's
    # _seed_enter/_seed_exit) treats "entry_rules is not None" as "the unified model applies,
    # even if every rule got pruned" vs "not configured, fall back to buy_tree/default". If a
    # real template got pruned down to zero rules by the GA (every branch disabled), that is a
    # genuine decision -- an empty list, not None -- and must NOT collapse into the same
    # signal as "no template" the way `[] or []` truthiness checks would.
    _template_entry = getattr(strategy, "entry_rules", None)
    _template_exit = getattr(strategy, "exit_rules", None)
    entry_rules = (_decode_rule_list(_template_entry, "entry", entry_genes, cond_by_id,
                                     optsel_by_half)
                   if _template_entry else None)
    exit_rules = (_decode_rule_list(_template_exit, "exit", exit_genes, cond_by_id,
                                    optsel_by_half)
                  if _template_exit else None)

    # Repair, don't reject: an all-days-OFF individual would never scan for entries at all (a
    # dead config the fitness function can't even distinguish from "just unlucky"), so force the
    # first weekday (fixed SCHEDULE_DAYS order) back ON rather than wasting a trial evaluating it.
    schedule_days: Optional[Dict[str, bool]] = None
    if schedule_by_day:
        schedule_days = {day: schedule_by_day.get(day, False) for day in SCHEDULE_DAYS}
        if not any(schedule_days.values()):
            schedule_days[SCHEDULE_DAYS[0]] = True

    return {
        "expert_overrides": expert_overrides,
        "screener_overrides": screener_overrides,
        "schedule_days": schedule_days,
        "entry_rules": entry_rules,
        "exit_rules": exit_rules,
    }

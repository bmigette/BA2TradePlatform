"""Joint optimization parameter space for strategy/expert optimization.

Collects ONE flat param_ranges dict (the GeneticOptimizer shape
{name: {'type','min','max','step'}}) from a Strategy row + expert numeric
settings, and decodes a flat decoded-params dict back into
(tp, sl, expert_overrides, buy_tree, sell_tree, exit_rules) by
deep-copying the condition trees and substituting node value/confirmation_bars/
action_value by id. The Strategy row is never mutated.

RM sizing is optimized through the expert ``model:*`` path keyed by the REAL ba2
setting names (e.g. ``risk_per_trade_pct``); there is no separate rm namespace.

Namespacing (design §5):
  model:<p>                       expert numeric decision settings (incl. RM sizing)
  tp | sl                         initial TP/SL percent
  cond:<id>:value                 a buy/sell condition node's threshold
  cond:<id>:confirmation_bars     that node's confirmation bars
  exit:<id>:action_value          an exit rule's action value
"""
import copy
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


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


def _collect_tp_sl(strategy) -> Dict[str, Any]:
    """tp/sl ranges from Strategy.initial_{tp,sl}_{optimize,min,max,step}."""
    out: Dict[str, Any] = {}
    if getattr(strategy, "initial_tp_optimize", False):
        out["tp"] = _range_entry(strategy.initial_tp_min, strategy.initial_tp_max,
                                 strategy.initial_tp_step, is_int=False)
    if getattr(strategy, "initial_sl_optimize", False):
        out["sl"] = _range_entry(strategy.initial_sl_min, strategy.initial_sl_max,
                                 strategy.initial_sl_step, is_int=False)
    return out


def _collect_expert(expert_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """model:<p> ranges from per-expert numeric settings marked optimize=True.

    expert_cfg shape: {param_name: {'optimize': bool,'min','max','step','type'}}.
    For the ML expert this is typically empty (model frozen, decision thresholds
    live in the condition tree). For ba2 experts: EarningsDrift surprise_min_pct/
    max_days_since_report/expected_profit_percent; Insider lookback_days/
    min_insiders/min_total_value; Rating profit_ratio/min_analysts; FactorRanker
    factor weights/top_n/winsorize_pct.
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


def _walk_condition_nodes(cond: Optional[Dict[str, Any]], out: Dict[str, Any]) -> None:
    """Emit cond:<id>:value and cond:<id>:confirmation_bars for optimizable nodes.

    Mirrors api/strategies.py traverse_conditions: AND/OR nodes recurse via
    'conditions'; leaf nodes carry id + value + optimize flags.
    """
    if not isinstance(cond, dict):
        return
    # Recurse into AND/OR sub-trees
    for child in (cond.get("conditions") or []):
        _walk_condition_nodes(child, out)
    cid = cond.get("id")
    if not cid:
        return
    # value optimization
    if cond.get("optimize") or cond.get("optimize_enabled"):
        out[f"cond:{cid}:value"] = _range_entry(
            cond.get("value_min"), cond.get("value_max"), cond.get("value_step"),
            is_int=False,
        )
    # confirmation-bars optimization
    if cond.get("confirmation_bars_min") is not None:
        out[f"cond:{cid}:confirmation_bars"] = _range_entry(
            cond.get("confirmation_bars_min"), cond.get("confirmation_bars_max"),
            cond.get("confirmation_bars_step"), is_int=True,
        )
    # ON/OFF toggle: a 0/1 gene the optimizer flips to enable/disable this condition
    # (a "step" the optimizer can turn on or off). Marked via toggle_optimize=True.
    if cond.get("toggle_optimize"):
        out[f"cond:{cid}:enabled"] = _range_entry(0, 1, 1, is_int=True)


def _collect_conditions(strategy) -> Dict[str, Any]:
    """cond:<id>:* across buy + sell trees and exit:<id>:action_value across exits."""
    out: Dict[str, Any] = {}
    _walk_condition_nodes(getattr(strategy, "buy_entry_conditions", None), out)
    _walk_condition_nodes(getattr(strategy, "sell_entry_conditions", None), out)
    # legacy single entry tree (backwards compat)
    _walk_condition_nodes(getattr(strategy, "entry_conditions", None), out)
    for exit_rule in (getattr(strategy, "exit_conditions", None) or []):
        if not isinstance(exit_rule, dict):
            continue
        eid = exit_rule.get("id")
        if eid and exit_rule.get("action_value_optimize"):
            out[f"exit:{eid}:action_value"] = _range_entry(
                exit_rule.get("action_value_min"), exit_rule.get("action_value_max"),
                exit_rule.get("action_value_step"), is_int=False,
            )
        # OPTION action selection params (Plan 2 T4): the strike delta and DTE the
        # optimizer can tune for an exit rule that opens an option position.
        if eid and exit_rule.get("option_strike_param_optimize"):
            out[f"exit:{eid}:option_delta"] = _range_entry(
                exit_rule.get("option_strike_param_min"),
                exit_rule.get("option_strike_param_max"),
                exit_rule.get("option_strike_param_step"), is_int=False,
            )
        if eid and exit_rule.get("option_dte_optimize"):
            out[f"exit:{eid}:option_dte"] = _range_entry(
                exit_rule.get("option_dte_min_range"),
                exit_rule.get("option_dte_max_range"),
                exit_rule.get("option_dte_step"), is_int=True,
            )
        # WING WIDTH for multi-leg option strategies (iron condor / jade lizard /
        # butterfly / ratio): a float % the optimizer can tune for the spread width.
        if eid and exit_rule.get("option_wing_width_optimize"):
            out[f"exit:{eid}:option_wing_width"] = _range_entry(
                exit_rule.get("option_wing_width_min"),
                exit_rule.get("option_wing_width_max"),
                exit_rule.get("option_wing_width_step"), is_int=False,
            )
        # ON/OFF toggle for the whole exit rule (optimizer can drop it entirely).
        if eid and exit_rule.get("toggle_optimize"):
            out[f"exit:{eid}:enabled"] = _range_entry(0, 1, 1, is_int=True)
        # exit rules may also carry an optimizable condition sub-tree
        _walk_condition_nodes(exit_rule.get("conditions"), out)
    return out


def collect_param_space(
    strategy,
    expert_cfg: Optional[Dict[str, Any]] = None,
    bypass: bool = False,
    screener_cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return the flat joint param_ranges dict for GeneticOptimizer.

    Merges expert (model:*, including RM sizing settings) + tp/sl + condition
    (cond:*/exit:*) ranges. Key order is deterministic (model, tp/sl, conditions)
    so the gene list is stable across runs — required for reproducibility.

    BYPASS experts (piece 1c): when ``bypass`` is True the strategy/expert does NOT use
    the classic RM or the enter/exit ruleset (e.g. FactorRanker rebalances to target weights
    via its own portfolio manager). For such an expert the search space is restricted to the
    expert's OWN params (model:*) ONLY — the tp, sl, cond:* and exit:* namespaces are
    EXCLUDED (they have no effect on the rebalance path, so optimizing them would be noise).
    """
    space: Dict[str, Any] = {}
    space.update(_collect_expert(expert_cfg))
    if not bypass:
        space.update(_collect_tp_sl(strategy))
        space.update(_collect_conditions(strategy))
    space.update(_collect_screener(screener_cfg))  # screener genes apply on BOTH paths
    if not space:
        raise ValueError(
            "No optimizable parameters found: "
            + (
                "a bypass expert searches only its own params — mark at least one expert "
                "param optimize=True."
                if bypass
                else "mark at least one of expert/TP/SL/condition fields optimize=True."
            )
        )
    logger.info(
        f"Collected {'bypass ' if bypass else ''}joint param space: "
        f"{len(space)} params: {list(space.keys())}"
    )
    return space


def _apply_to_tree(tree: Optional[Dict[str, Any]], by_id: Dict[str, Dict[str, Any]]
                   ) -> Optional[Dict[str, Any]]:
    """Deep-copy a condition tree, substituting value/confirmation_bars by node id.

    The input tree (and therefore the source Strategy) is never mutated.
    """
    if tree is None:
        return None
    new = copy.deepcopy(tree)

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
                node["value"] = sub["value"]
            if "confirmation_bars" in sub:
                node["confirmation_bars"] = sub["confirmation_bars"]

    _recurse(new)
    return new


def decode_params(strategy, flat_params: Dict[str, Any]) -> Dict[str, Any]:
    """Reconstruct a concrete trial config from a decoded flat params dict.

    The flat dict comes from GeneticOptimizer.decode_individual (namespaced keys:
    tp | sl | model:<p> | cond:<id>:value | cond:<id>:confirmation_bars |
    exit:<id>:action_value). Returns::

      {
        'tp': float, 'sl': float,                 # falls back to strategy defaults
        'expert_overrides': {param: value},       # model:* stripped of prefix (incl. RM sizing)
        'buy_tree': dict|None, 'sell_tree': dict|None, 'exit_rules': list,
      }

    The source Strategy is NEVER mutated (trees are deep-copied).
    """
    # Partition flat keys by namespace
    cond_by_id: Dict[str, Dict[str, Any]] = {}
    exit_action_by_id: Dict[str, Any] = {}
    exit_enabled_by_id: Dict[str, Any] = {}
    exit_option_delta_by_id: Dict[str, Any] = {}
    exit_option_dte_by_id: Dict[str, Any] = {}
    exit_option_wing_by_id: Dict[str, Any] = {}
    expert_overrides: Dict[str, Any] = {}
    screener_overrides: Dict[str, Any] = {}
    tp = getattr(strategy, "initial_tp_percent", None)
    sl = getattr(strategy, "initial_sl_percent", None)

    for key, val in flat_params.items():
        if key == "tp":
            tp = val
        elif key == "sl":
            sl = val
        elif key.startswith("model:"):
            expert_overrides[key[len("model:"):]] = val
        elif key.startswith("screener:"):
            screener_overrides[key[len("screener:"):]] = val
        elif key.startswith("cond:"):
            _, cid, field = key.split(":", 2)
            cond_by_id.setdefault(cid, {})[field] = val
        elif key.startswith("exit:"):
            _, eid, field = key.split(":", 2)  # 'action_value'|'enabled'|'option_delta'|'option_dte'|'option_wing_width'
            if field == "enabled":
                exit_enabled_by_id[eid] = val
            elif field == "option_delta":
                exit_option_delta_by_id[eid] = val
            elif field == "option_dte":
                exit_option_dte_by_id[eid] = val
            elif field == "option_wing_width":
                exit_option_wing_by_id[eid] = val
            else:
                exit_action_by_id[eid] = val
        else:
            raise ValueError(f"Unknown decoded param namespace: {key!r}")

    buy_tree = _apply_to_tree(getattr(strategy, "buy_entry_conditions", None), cond_by_id)
    sell_tree = _apply_to_tree(getattr(strategy, "sell_entry_conditions", None), cond_by_id)

    exit_rules = []
    for rule in copy.deepcopy(getattr(strategy, "exit_conditions", None) or []):
        if not isinstance(rule, dict):
            exit_rules.append(rule)
            continue
        eid = rule.get("id")
        # ON/OFF toggle: an exit rule whose 'enabled' gene decoded to 0 is dropped entirely.
        if eid in exit_enabled_by_id and exit_enabled_by_id[eid] == 0:
            continue
        if eid in exit_action_by_id:
            rule["action_value"] = exit_action_by_id[eid]
        # OPTION action selection params (Plan 2 T4).
        if eid in exit_option_delta_by_id:
            rule["option_strike_param"] = exit_option_delta_by_id[eid]
        if eid in exit_option_dte_by_id:
            # option_dte gene tunes the DTE WINDOW CENTER; keep a half-width so the
            # [min, max] span covers real (weekly) expiries instead of a single
            # impossible day. A single-day window (min == max) almost never matches an
            # actual discrete expiry, so the option entry selects nothing -> 0 fills.
            center = int(round(exit_option_dte_by_id[eid]))
            base_hw = 0
            try:
                bmin = rule.get("option_dte_min")
                bmax = rule.get("option_dte_max")
                if bmin is not None and bmax is not None and bmax > bmin:
                    base_hw = int((bmax - bmin) // 2)
            except Exception:  # noqa: BLE001 - defensive: malformed base window -> default hw
                base_hw = 0
            hw = max(base_hw, 7)  # at least +/-7 days so a weekly expiry falls in-window
            rule["option_dte_min"] = max(0, center - hw)
            rule["option_dte_max"] = center + hw
        # WING WIDTH: applied directly as the rule_builders key option_wing_width_pct
        # (mirrors option_strike_param; no window logic — it's a plain float %).
        if eid in exit_option_wing_by_id:
            rule["option_wing_width_pct"] = exit_option_wing_by_id[eid]
        if rule.get("conditions"):
            rule["conditions"] = _apply_to_tree(rule["conditions"], cond_by_id)
        exit_rules.append(rule)

    return {
        "tp": tp, "sl": sl,
        "expert_overrides": expert_overrides,
        "screener_overrides": screener_overrides,
        "buy_tree": buy_tree, "sell_tree": sell_tree, "exit_rules": exit_rules,
    }

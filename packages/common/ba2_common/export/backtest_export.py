"""Backtest export payloads (``expert_settings`` / ``ruleset``) and the deploy-payload entry.

Moved from testplatform/backend/app/api/backtests.py::_derive_export_payload (2026-09, site plan
P0a) so the public site produces byte-identical payloads without importing the test app. Pure:
the caller resolves the optimization's run-level ``backtest`` block, the bypass-expert check and
(optionally) the legacy ruleset reconstruction, and passes them in. Pinned by
testplatform/backend/tests/test_export_payload_golden.py.
"""
from typing import Any, Callable, Dict, Optional, Tuple

from ba2_common.core.deploy_parity import (
    BacktestRunFacts, backtest_only_settings, forced_expert_settings,
)
from ba2_common.core.interfaces.ExtendableSettingsInterface import coerce_bool
from ba2_common.core.market_condition_rules import (
    assert_market_conditions_resolved, assert_market_rule_actions,
)
from ba2_common.core.rule_models import normalize_trade_rules, trade_rules_from_legacy
from ba2_common.core.schedule_genes import schedule_override_from_genes
from ba2_common.logger import logger

EXPORT_KINDS = ("expert_settings", "ruleset")

LegacyReconstructor = Callable[[], Tuple[Any, Any, Any, Any]]

_LEGACY_TREE_KEYS = ("buyEntryConditions", "buy_entry_conditions", "sellEntryConditions",
                     "sell_entry_conditions", "exitConditions", "exit_conditions",
                     "entryActions", "entry_actions")


class ExportRefused(ValueError):
    """The stored run cannot be exported as a deployable payload (the reason is the message)."""


class UnsupportedExportKind(ValueError):
    """``kind`` is not one of EXPORT_KINDS."""


def _is_rule_gene(k: Any) -> bool:
    return isinstance(k, str) and (k.startswith("cond:") or k.startswith("exit:")
                                   or k.startswith("entry:"))


def needs_legacy_reconstruction(strategy_params: Any) -> bool:
    """True when the ``ruleset`` export of this row can only be rebuilt by decoding its flat
    genes against the optimization's base strategy (the test app's ``decode_params``): no
    unified rule lists, no legacy trees, but rule genes present. Mirrors the condition inside
    ``derive_export_payload``; callers without a reconstructor must treat such rows as not
    exportable."""
    sp = strategy_params if isinstance(strategy_params, dict) else {}
    for k in ("entryRules", "entry_rules", "exitRules", "exit_rules"):
        if sp.get(k) is not None:
            return False
    buy = sp.get("buyEntryConditions") if sp.get("buyEntryConditions") is not None else sp.get("buy_entry_conditions")
    sell = sp.get("sellEntryConditions") if sp.get("sellEntryConditions") is not None else sp.get("sell_entry_conditions")
    exits = sp.get("exitConditions") if sp.get("exitConditions") is not None else sp.get("exit_conditions")
    entries = sp.get("entryActions") if sp.get("entryActions") is not None else sp.get("entry_actions")
    return (buy is None and sell is None and not exits and not entries
            and any(_is_rule_gene(k) for k in sp))


def derive_export_payload(
    backtest: Any,
    kind: str,
    *,
    opt_backtest_block: Optional[Dict[str, Any]] = None,
    bypass_check: Optional[Callable[[Optional[str]], bool]] = None,
    reconstruct_legacy_ruleset: Optional[LegacyReconstructor] = None,
) -> Dict[str, Any]:
    """Build the chosen read-only export payload from a backtest's strategy_params.

    Two ``kind`` values are supported:

      * ``expert_settings`` — the expert this run used + its decision/RM settings. We surface
        the expert class name (``Backtest.expert_name``) and the flat optimized ``model:*`` genes
        recoverable from ``strategy_params`` (the expert/RM decision settings an optimization
        tunes). The entry-time TP/SL bracket is NOT part of this kind anymore — it rides on
        ``entryActions``/``entry_rules`` rule lists (same shape as exit conditions), surfaced
        under the ``ruleset`` kind below instead of the deleted ``initialTpPercent``/
        ``initialSlPercent`` scalar fields.
      * ``ruleset`` — the conditions ruleset (buy/sell entry trees + exit conditions + the
        entry-time TP/SL bracket). Structured runs carry ``buyEntryConditions``/
        ``sellEntryConditions``/``exitConditions``/``entryActions``; optimization TOP-N runs
        instead carry the flat ``cond:*``/``exit:*``/``entry:*`` genes, which we pass through so
        the export is still self-describing.

    Pure derivation from the persisted ``strategy_params`` — NO server filesystem writes.

    ``backtest`` needs attributes id, name, expert_name, engine_type, strategy_params,
    start_date, end_date, initial_capital. ``opt_backtest_block`` is the source optimization's
    ``optimization_config['backtest']`` dict, or None. ``bypass_check(expert_name)`` is only
    consulted on the screener + apply_to_expert_settings branch (lazy, as before).
    ``reconstruct_legacy_ruleset()`` is only consulted for gene-only legacy rows.

    Raises ExportRefused (undeployable ruleset) or UnsupportedExportKind.
    """
    sp = backtest.strategy_params or {}

    def _pick(*keys):
        for k in keys:
            if isinstance(sp, dict) and k in sp and sp[k] is not None:
                return sp[k]
        return None

    if kind == "expert_settings":
        # GA flat model:* genes stripped of the prefix = the optimized expert decision settings.
        model_overrides = (
            {k[len("model:"):]: v for k, v in sp.items()
             if isinstance(k, str) and k.startswith("model:")}
            if isinstance(sp, dict) else {}
        )
        # Fixed (non-GA-tunable) settings _persist_top_backtests stashed directly onto this
        # Backtest's strategy_params at persist time (e.g. sizing_mode=risk_atr) -- the durable
        # floor layer that survives even if the source StrategyOptimization row is later pruned
        # by `server db-cleanup` (see _opt_backtest_block's docstring). Lowest priority: the
        # live optimization row's base_settings (when it still resolves) and model_overrides
        # both take precedence, this only fills the gap when they can't.
        persisted_fixed = (sp.get("expertFixedSettings") or {}) if isinstance(sp, dict) else {}
        bt_block = opt_backtest_block if isinstance(opt_backtest_block, dict) else None
        # FULL expert settings = the optimization's base expert spec settings overlaid with the
        # optimized overrides (faithful reproduction); falls back to a standalone run's stored
        # expertSettings, then to the bare overrides.
        if bt_block is not None:
            base_specs = bt_block.get("experts") or []
            base_settings = {}
            for spec in base_specs:
                if isinstance(spec, dict) and spec.get("class") == backtest.expert_name:
                    base_settings = dict(spec.get("settings") or {})
                    break
            expert_params = {**persisted_fixed, **base_settings, **model_overrides}
            acct = bt_block.get("account_settings") or {}
            # Universe: a SCREENER-settings run (``backtest.screener_opt`` present) must export the
            # screener block — store + the EFFECTIVE settings (run-level base overlaid with this
            # individual's optimized ``screener:*`` genes, mirroring _build_daily_trial_config's
            # ``eff``) — NOT the static candidate list (``enabled_instruments``, the whole metric-
            # store union). Otherwise Load drops the screener config and pins a static universe.
            screener_opt = bt_block.get("screener_opt")
            if isinstance(screener_opt, dict) and screener_opt.get("store"):
                screener_overrides = {
                    k[len("screener:"):]: v for k, v in sp.items()
                    if isinstance(k, str) and k.startswith("screener:")
                } if isinstance(sp, dict) else {}
                eff_screener = {**(screener_opt.get("base_settings") or {}), **screener_overrides}
                universe = {
                    "mode": "screener",
                    "screener_store": screener_opt["store"],
                    "screener_settings": eff_screener,
                    "screener_cadence_days": int(screener_opt.get("cadence_days", 7)),
                }
                # BYPASS-expert screener wiring (piece 1c mirror): for an expert that declares
                # bypasses_classic_rm (e.g. FactorRanker), _build_daily_trial_config pushes
                # universe_source=screener + screener_store + the effective screener_* settings
                # directly onto the expert's OWN settings at trial-build time -- that override is
                # never written back to Backtest.strategy_params, so base_settings here still shows
                # whatever static default the Strategy template had (e.g. universe_source="static").
                # Re-derive the same overlay here so the export/deploy reproduces what the backtest
                # actually ran with, not the un-overridden template. Explicit model:* overrides
                # still win last, matching _build_daily_trial_config's merge order.
                if (screener_opt.get("apply_to_expert_settings") and bypass_check is not None
                        and bypass_check(backtest.expert_name)):
                    expert_params = {
                        **persisted_fixed,
                        **base_settings,
                        "universe_source": "screener",
                        "screener_store": screener_opt["store"],
                        **eff_screener,
                        **model_overrides,
                    }
            else:
                universe = {"mode": "static", "symbols": list(bt_block.get("enabled_instruments") or [])}
            execution = {
                "seed": bt_block.get("seed"),
                "fill_model": acct.get("fill_model"),
                "warmup_days": bt_block.get("warmup_days"),
                "commission": acct.get("commission_per_trade"),
                "slippage": acct.get("slippage_bps"),
                "enable_short": bool(bt_block.get("enable_short")),
                # THE ENTRY CADENCE THIS INDIVIDUAL WAS SCORED ON. The GA searches the entry
                # weekday per individual (schedule:<day> genes) and _build_daily_trial_config
                # lets the decoded days REPLACE the run-level override -- so exporting
                # bt_block's run-level value deploys a cadence the genome never used. Found
                # live 2026-09-07: five instances firing Mondays for genomes that had chosen
                # Thursday, Wed/Thu/Fri, Tue/Thu/Fri, Mon/Tue and Mon/Wed/Thu/Fri. Same
                # overlay shape as the screener genes above.
                "run_schedule_override": (
                    schedule_override_from_genes(sp, bt_block.get("run_schedule_override"),
                                                weekdays_only=True)
                    or bt_block.get("run_schedule_override")
                ),
            }
            interval = bt_block.get("execution_interval")
        else:
            expert_params = (
                {**persisted_fixed, **model_overrides} if (persisted_fixed or model_overrides)
                else _pick("expertSettings", "expert_settings") or {}
            )
            universe = _pick("universe")
            execution = {
                "seed": _pick("seed"),
                "fill_model": _pick("fillModel", "fill_model"),
                "warmup_days": _pick("warmupDays", "warmup_days"),
                "commission": _pick("commission"),
                "slippage": _pick("slippage"),
                "enable_short": _pick("enableShort", "enable_short"),
                # Genome days win here too (see the opt-derived branch): a standalone row
                # cloned from an optimized one still carries the schedule:* genes.
                "run_schedule_override": (
                    schedule_override_from_genes(
                        sp, _pick("runScheduleOverride", "run_schedule_override"),
                        weekdays_only=True)
                    or _pick("runScheduleOverride", "run_schedule_override")
                ),
            }
            interval = _pick("executionInterval", "execution_interval")
        # THE ONE TABLE (ba2_common.core.deploy_parity), read here and in
        # daily_backtest_handler._build_experts. Every setting the backtest FORCES onto a
        # trial's expert is merged LAST -- exactly as the handler applies its gates last -- so
        # a deployed genome cannot end up with a permission the scored run did not have. The
        # highest-severity one is allow_automated_trade_modification: it defaults False live and
        # gates every exit, so without this a deployed sleeve evaluates its exits and never
        # submits them (2026-09-02 review, V3).
        # The two RM toggles come from the ROW'S OWN GENES, not a constant. They were hardcoded
        # False in the table, which is true of every run on record (INERT_RM_TOGGLES pins them)
        # and becomes a lie the first time a run is scored with the ATR stop genuinely on -- the
        # deploy would then declare use_atr_stop=False for a run that used it, and size its live
        # stops differently from the backtest it came from. Reading the gene makes the export
        # follow the day that pin is lifted, with nothing here to remember to change.
        #
        # ABSENT means False: 244 of the 692 rows carry no such gene (the expert never had it),
        # and those ran with it off exactly as the constant said. coerce_bool because the gene
        # arrives as an int and a legacy row can hold the JSON string "1" -- which is the very
        # defect these two were pinned for, and which bool() reads backwards.
        def _executed_toggle(name: str) -> bool:
            raw = sp.get(f"model:{name}")
            if raw is None:
                return False
            try:
                return coerce_bool(raw)
            except ValueError:
                logger.warning(f"backtest {backtest.id}: model:{name}={raw!r} is not a boolean "
                               f"spelling; exporting it OFF, as every run on record was")
                return False

        facts = BacktestRunFacts(
            enable_short=bool(execution.get("enable_short")),
            hold_assigned_stock=bool((acct if bt_block is not None else {}).get(
                "hold_assigned_stock")),
            entry_action=(bt_block.get("entry_action") if bt_block is not None else None),
            use_atr_stop=_executed_toggle("use_atr_stop"),
            regime_overlay_enabled=_executed_toggle("regime_overlay_enabled"),
        )
        expert_params = {**expert_params, **forced_expert_settings(facts)}
        return {
            "backtest_id": backtest.id,
            "name": backtest.name,
            "expert": backtest.expert_name,
            "engine_type": backtest.engine_type or "ml",
            "settings": {
                "expert_params": expert_params,
            },
            # Behaviours the backtest DERIVED that have no live analogue (review V4/V5).
            # Carried, never applied: a deploy that differs from its backtest must say so
            # rather than differ silently. Each row's reason is in the table.
            "backtest_only": backtest_only_settings(facts),
            # Execution config (seed/fill_model/warmup/commission/slippage/enable_short/
            # run_schedule) + universe + interval so a saved run can be reproduced
            # faithfully from the exports alone (the reproducibility goal).
            "execution": execution,
            "universe": universe,
            "execution_interval": interval,
            # Date range + capital: without these a file-based import silently falls back to
            # whatever the form happens to have typed in, diverging from the original run.
            "start_date": backtest.start_date.isoformat() if backtest.start_date else None,
            "end_date": backtest.end_date.isoformat() if backtest.end_date else None,
            "initial_capital": backtest.initial_capital,
        }

    if kind == "ruleset":
        cond_genes = (
            {k: v for k, v in sp.items() if _is_rule_gene(k)}
            if isinstance(sp, dict) else {}
        )

        # UNIFIED RULE MODEL rows (post-migration-028 saves): the concrete TradeRule lists.
        entry_rules = _pick("entryRules", "entry_rules")
        exit_rules = _pick("exitRules", "exit_rules")

        if entry_rules is None and exit_rules is None:
            # LEGACY rows: buy/sell trees + single-action exit rows + flat entry bracket —
            # lift them through the shared converter so every saved backtest exports the
            # same canonical shape.
            buy = _pick("buyEntryConditions", "buy_entry_conditions")
            sell = _pick("sellEntryConditions", "sell_entry_conditions")
            exits = _pick("exitConditions", "exit_conditions")
            entries = _pick("entryActions", "entry_actions")
            # Older optimization runs stored only the flat genes — reconstruct the concrete
            # trees from the optimization's base strategy + those genes so Load still
            # restores conditions.
            if buy is None and sell is None and not exits and not entries and cond_genes:
                r_buy, r_sell, r_exits, r_entries = (
                    reconstruct_legacy_ruleset() if reconstruct_legacy_ruleset is not None
                    else (None, None, None, None))
                buy = buy if buy is not None else r_buy
                sell = sell if sell is not None else r_sell
                exits = exits if exits else r_exits
                entries = entries if entries else r_entries
            converted = trade_rules_from_legacy(
                buy_tree=buy, sell_tree=sell, entry_actions=entries, exit_conditions=exits,
            )
            entry_rules = converted["entry_rules"]
            exit_rules = converted["exit_rules"]

        # The exit list as STORED is checked too: normalizing coerces an unknown group operator
        # (a NOT) to AND, which would hide exactly the nesting the action check refuses.
        raw_exit_rules = exit_rules if isinstance(exit_rules, list) else []
        entry_rules = normalize_trade_rules(entry_rules or [])
        exit_rules = normalize_trade_rules(exit_rules or [])
        # MARKET-CONDITION GATES (design 2026-09-15 section 5). An export is what a deploy reads,
        # so it is the first place an undeployable ruleset can be caught: an UNRESOLVED mode gene
        # (entry OR exit) is the optimizer's search template, not a rule (live would have no
        # operator to apply), and a market gate on an open-positions rule may only close, reduce
        # or adjust TP/SL, from a top-level AND (plan 2026-09-24 Task B2). All are 400s, not
        # 500s: the payload is wrong, the server is fine.
        try:
            assert_market_conditions_resolved(entry_rules, f"backtest {backtest.id} entry_rules")
            assert_market_conditions_resolved(exit_rules, f"backtest {backtest.id} exit_rules")
            assert_market_rule_actions(raw_exit_rules, f"backtest {backtest.id} exit_rules")
            assert_market_rule_actions(exit_rules, f"backtest {backtest.id} exit_rules")
        except ValueError as e:
            raise ExportRefused(str(e)) from e
        return {
            "backtest_id": backtest.id,
            "name": backtest.name,
            # Canonical TradeRule lists (conditions + actions[] + continue_processing per
            # rule) — what the live import dialog and the builder consume.
            "entry_rules": entry_rules,
            "exit_rules": exit_rules,
            "optimized_genes": cond_genes,
        }

    raise UnsupportedExportKind(
        f"Unsupported export kind: {kind!r}. Use 'expert_settings' or 'ruleset'.")


def build_deploy_entry(*, backtest_id: Any, target_instance_id: Any, account_id: Any,
                       virtual_equity_pct: Any, expert_name: Any, label: Any,
                       ruleset: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
    """One entry of the deploy-payload file that tools/import_deploy_payload.py consumes
    (a JSON list of these). ``target_instance_id=None`` makes the import create the instance."""
    return {
        "backtest_id": backtest_id,
        "target_instance_id": target_instance_id,
        "account_id": account_id,
        "virtual_equity_pct": virtual_equity_pct,
        "expert_name": expert_name,
        "label": label,
        "ruleset": ruleset,
        "settings": settings,
    }

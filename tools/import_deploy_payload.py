"""Step 2 of a backtest -> live-instance deploy: read the payload dumped by
export_deploy_payload.py and write it into the LIVE trade DB.

Generalised 2026-08-09 from the Senate-only version: works for ANY expert (the class is resolved
from the payload's expert_name via the live registry instead of being hardcoded), and CREATES the
ExpertInstance when ``target_instance_id`` is null so a first-of-its-kind deploy needs no
hand-made row.

allow_automated_trade_opening is forced ON for a freshly created instance: it defaults to False,
and an instance that silently never places an order looks identical to one whose strategy simply
found no setup -- a trap this project has already hit once.

instrument_selection_method is forced to the expert class's own
``required_instrument_selection_method`` whenever it declares one (basket experts: Senate
Weight/Copy). It defaults to "static", and a static instance with no instrument rows makes
JobManager skip enter_market job creation entirely -- the same silent "enabled but never trades"
shape as the flag above. Cost the first Senate deploy (instance 13, 2026-09-13) its entry job
until it was set by hand.

OPERATOR NOTE (2026-09-03): every live O_CC / O_WHEEL ExpertInstance deployed BEFORE 2026-09-03
must be re-exported and re-imported through this pair of tools (then POST /api/reload): the live
option lifecycle pass no longer closes the written call at the roll window -- the ruleset's
`cc_dte` rule (repository-resolved, identical in backtest and live) owns that exit, and an old
payload does not carry it. The forced/derived settings the backtest handler applies now travel in
the payload through the shared table in ba2_common.core.deploy_parity (pinned by
testplatform/backend/tests/backtest/test_deploy_round_trip_parity.py).

For each entry: converts entry/exit TradeRule lists to a live ruleset export via
``trade_rules_to_live_export``, imports it as NEW Ruleset+EventAction rows via
``RulesImporter.import_multiple_rulesets`` (never touches the existing rulesets -- old ones are
left orphaned, not deleted, so this is reversible), repoints the target ExpertInstance's
enter_market_ruleset_id/open_positions_ruleset_id at the new rulesets, and writes the expert_params
via the expert's own ``save_settings`` (so value typing follows get_settings_definitions exactly,
same as any other settings save through the app).

The pinned risk-manager toggles (use_atr_stop, regime_overlay_enabled) are written EXPLICITLY on
every deploy and reported either way. Off by default, which is parity with every backtest on
record; ``--use-atr`` / ``--use-regime-overlay`` turn them on and print a loud block saying the
instance will no longer reproduce its backtest. The OFF notice exists so that a live settings page
showing ``atr_multiplier = 5.0`` is never mistaken for a tuned value -- see _PINNED_RM_TOGGLES.

Usage: python tools/import_deploy_payload.py <payload.json> [--use-atr] [--use-regime-overlay]
"""
import json
import os
import sys

REPO = os.environ.get("BA2_REPO", r"C:\Users\basti\Documents\dev\BA2TradePlatform")
for p in (REPO, os.path.join(REPO, "packages", "experts")):
    if p not in sys.path:
        sys.path.insert(0, p)

LIVE_DB = os.environ.get("BA2_LIVE_DB", os.path.expanduser(r"~\Documents\ba2\trade\db.sqlite"))

from ba2_common.core import db as _ba2_db  # noqa: E402
_ba2_db.configure_db(LIVE_DB)

from ba2_common.core.db import add_instance, get_instance, update_instance  # noqa: E402
from ba2_common.core.deploy_parity import (  # noqa: E402
    SCREENER_UNIVERSE_SETTING, live_settings_from_universe, unmapped_screener_keys,
)
from ba2_common.core.models import ExpertInstance  # noqa: E402
from ba2_common.core.rules_convert import trade_rules_to_live_export  # noqa: E402
from ba2_common.core.rules_export_import import RulesImporter  # noqa: E402


def _expert_class(name: str):
    """Resolve an expert class by name from the LIVE registry (no hardcoded import)."""
    from ba2_trade_platform.modules.experts import experts as _live_experts
    for cls in _live_experts:
        if cls.__name__ == name:
            return cls
    raise SystemExit(f"expert {name!r} not found in the live registry")


#: The risk-manager toggles that are currently pinned OFF everywhere -- search space, trial
#: config and deploy -- mapped to the flag that turns each one ON for a deployed instance.
#:
#: They are NOT dead code and NOT a permanent state. ``use_atr_stop`` and
#: ``regime_overlay_enabled`` never took effect in any run on record (a bool was stored as the
#: JSON string "1" and read as False); ``coerce_bool`` fixed the encoding, and
#: ``strategy_param_space.INERT_RM_TOGGLES`` then pinned them OFF deliberately so that enabling
#: them could not silently make new results incomparable with the whole archive. Turning them on
#: is planned, as its own re-optimization and its own baseline.
#:
#: Until that happens the genome's ``atr_multiplier`` / ``atr_period`` / ``regime_*_scale`` values
#: are INERT -- carried, never read. A deploy therefore has to say so out loud, because a settings
#: page showing ``atr_multiplier = 5.0`` on a live instance reads exactly like a tuned parameter.
_PINNED_RM_TOGGLES = {
    "use_atr_stop": "--use-atr",
    "regime_overlay_enabled": "--use-regime-overlay",
}


def _apply_rm_toggles(expert_params: dict, enabled: dict) -> None:
    """Set the currently-pinned RM toggles and SAY what was done, either way.

    OFF (the default, and what every backtest on record ran): a one-line notice per toggle naming
    the genes it renders inert, so nobody reads those numbers as live tuning.

    ON: a loud block, because it is the dangerous direction -- the backtest that justified this
    deploy did NOT exercise the feature, so live stops matching it the moment the flag is passed.
    That is a legitimate thing to do on purpose (it is how the planned ATR baseline starts) and an
    expensive thing to do by accident.
    """
    for setting, flag in _PINNED_RM_TOGGLES.items():
        on = bool(enabled.get(setting))
        expert_params[setting] = on
        if not on:
            genes = ("atr_multiplier / atr_period" if setting == "use_atr_stop"
                     else "regime_risk_scale / regime_stop_scale / regime_tp_scale")
            print(f"  {setting}=False (pinned; pass {flag} to enable) "
                  f"-- {genes} in this genome are INERT, not tuned values")
        else:
            print("  " + "!" * 74)
            print(f"  !! {setting}=True -- ENABLED BY {flag}")
            print("  !! Every backtest on record, INCLUDING the one this deploy is derived from,")
            print("  !! ran with this OFF (strategy_param_space.INERT_RM_TOGGLES). This live")
            print("  !! instance will NOT reproduce its backtest. Only do this deliberately, as")
            print("  !! part of a re-optimized ATR/regime baseline.")
            print("  " + "!" * 74)


def main() -> int:
    argv = [a for a in sys.argv[1:]]
    enabled = {s: False for s in _PINNED_RM_TOGGLES}
    for setting, flag in _PINNED_RM_TOGGLES.items():
        if flag in argv:
            enabled[setting] = True
            argv.remove(flag)
    if not argv:
        raise SystemExit(
            "usage: import_deploy_payload.py <payload.json> "
            + " ".join(f"[{f}]" for f in _PINNED_RM_TOGGLES.values()))
    payload_path = argv[0]
    with open(payload_path) as f:
        payloads = json.load(f)

    print(f"LIVE_DB = {LIVE_DB}")
    for entry in payloads:
        inst_id = entry["target_instance_id"]
        label = entry["label"]
        bt_id = entry["backtest_id"]
        print(f"\n=== {label}: backtest {bt_id} -> instance {inst_id} ===")

        expert_name = entry["expert_name"]   # payload is authoritative; no silent default
        created = False
        if inst_id is None:
            acct = entry.get("account_id")
            if acct is None:
                print("FATAL: target_instance_id is null but no account_id in payload")
                return 1
            inst = ExpertInstance(
                account_id=int(acct), expert=expert_name, alias=label,
                virtual_equity_pct=float(entry.get("virtual_equity_pct") or 10.0),
                enabled=True,
            )
            inst_id = add_instance(inst)
            inst = get_instance(ExpertInstance, inst_id)
            created = True
            print(f"CREATED ExpertInstance {inst_id} ({expert_name}, account {acct}, "
                  f"{inst.virtual_equity_pct:g}% virtual equity)")
        else:
            inst = get_instance(ExpertInstance, inst_id)
            if inst is None:
                print(f"FATAL: ExpertInstance {inst_id} not found in {LIVE_DB}")
                return 1
            if inst.expert != expert_name:
                print(f"FATAL: instance {inst_id} is expert={inst.expert!r}, expected {expert_name!r}")
                return 1
        old_enter, old_open = inst.enter_market_ruleset_id, inst.open_positions_ruleset_id
        print(f"current rulesets: enter={old_enter} open={old_open}")

        entry_rules = entry["ruleset"]["entry_rules"]
        exit_rules = entry["ruleset"]["exit_rules"]
        live_export = trade_rules_to_live_export(entry_rules, exit_rules, name=label)
        n_rulesets = len(live_export["rulesets"])
        print(f"live_export: {n_rulesets} ruleset(s) "
              f"({[r['subtype'] for r in live_export['rulesets']]})")

        ruleset_ids, warnings = RulesImporter.import_multiple_rulesets(live_export)
        for w in warnings:
            print(f"  warning: {w}")
        by_subtype = dict(zip((r["subtype"] for r in live_export["rulesets"]), ruleset_ids))
        new_enter = by_subtype.get("enter_market")
        new_open = by_subtype.get("open_positions")
        print(f"created rulesets: enter={new_enter} open={new_open}")

        inst.enter_market_ruleset_id = new_enter
        inst.open_positions_ruleset_id = new_open
        inst.user_description = (
            f"Deployed from {label} (backtest {bt_id}). "
            + ("Created by import_deploy_payload." if created
               else f"Old rulesets {old_enter}/{old_open} left orphaned (not deleted).")
        )
        inst.alias = label
        update_instance(inst)
        print(f"expertinstance {inst_id}: rulesets repointed, alias/description updated")

        expert = _expert_class(expert_name)(inst_id)
        expert_params = dict(entry["settings"]["settings"]["expert_params"])
        # THE UNIVERSE BLOCK, which this tool used to DROP -- the common root of review
        # findings V1 (the six screener:* genes) and V2 (the $100 underlying-price cap every
        # option grid screened on). The exporter has always built it; consuming only
        # ``settings.expert_params`` meant a genome selected on cheap names was deployed onto
        # whatever universe the live instance happened to have. The live settings mostly exist
        # under the same names -- but NOT all of them: the run-level base settings use the metric
        # store's unprefixed vocabulary, so live_settings_from_universe canonicalises them onto
        # the screener_* names StockScreener reads (2026-09-07; see its docstring).
        universe_params = live_settings_from_universe(entry["settings"].get("universe"))
        # A screener key that reaches live under a name nothing reads is inert, and silence is
        # how `market_cap_max` survived six deploys with the whole upper bound of the cap band
        # missing (parity review 2026-09-07). live_settings_from_universe now canonicalises the
        # known ones; anything left over gets said out loud rather than written and forgotten.
        stray = unmapped_screener_keys(entry["settings"].get("universe"))
        if stray:
            print(f"  WARNING: screener key(s) with no live setting of that name: {stray} -- "
                  f"they will be stored but NOTHING READS THEM")
        if universe_params:
            # The universe is part of WHAT WAS SCORED, so it wins over the run's base settings
            # for the same reason the forced gates do.
            expert_params = {**expert_params, **universe_params}
            print(f"universe: {len(universe_params)} screener setting(s) carried into "
                  f"expert_params ({SCREENER_UNIVERSE_SETTING}="
                  f"{universe_params[SCREENER_UNIVERSE_SETTING]!r})")
        # INSTRUMENT SELECTION, forced when the expert class declares one. Same failure mode as
        # the two settings below, found live 2026-09-13 on the first Senate deploy (instance 13).
        #
        # A basket expert -- FMPSenateTraderWeight, FMPSenateTraderCopy -- picks its own symbols
        # and declares required_instrument_selection_method="expert". JobManager only creates the
        # single placeholder EXPERT job when the INSTANCE's instrument_selection_method says
        # "expert"; the setting defaults to "static", and a static instance with no instrument
        # rows yields an empty list, so `_get_enabled_instruments` returns [] and NO enter_market
        # job is ever created. The instance comes up enabled, correctly configured, schedule
        # attached, visible in the UI -- and never trades. Indistinguishable from a strategy that
        # found no setup, which is exactly the trap the two lines below already exist to close.
        #
        # ASSIGNED, not setdefault: this is a hard requirement of the expert class, not a
        # default. A payload (or an operator) disagreeing with it is wrong by construction, and
        # silently honouring that disagreement is what cost instance 13 its first trading day.
        # Applied for an EXISTING instance too -- a re-deploy onto a wrongly-configured row must
        # repair it, not inherit the fault.
        # The pinned RM toggles, applied EXPLICITLY rather than inherited from the payload, so a
        # deploy always states its ATR/regime posture instead of silently carrying whatever the
        # exporting run happened to embed. Off by default = parity with the backtest.
        _apply_rm_toggles(expert_params, enabled)

        required_ism = (expert.__class__.get_expert_properties() or {}).get(
            "required_instrument_selection_method")
        if required_ism:
            prior = expert.get_setting_with_interface_default(
                "instrument_selection_method", log_warning=False)
            expert_params["instrument_selection_method"] = required_ism
            if prior != required_ism:
                print(f"instrument_selection_method: {prior!r} -> {required_ism!r} "
                      f"(REQUIRED by {expert.__class__.__name__}; without it JobManager creates "
                      f"no enter_market job and the instance never trades)")

        if created:
            # Defaults to False; without it the instance analyses and never trades. The export
            # now carries it explicitly (deploy_parity), so this is a floor for an OLD payload.
            expert_params.setdefault("allow_automated_trade_opening", True)

        # THE SCHEDULE -- applied on EVERY deploy, not only on creation.
        #
        # The payload carries the backtest's own cadence as execution.run_schedule_override.
        # Originally nothing wrote it at all, so a freshly deployed instance came up enabled,
        # correctly configured, and with NO schedule -- it loaded, showed in the UI and never
        # fired (found 2026-09-06 on five instances with an empty Scheduled Jobs table). That was
        # fixed for NEW instances only, which left the mirror-image bug:
        #
        # RE-DEPLOYING a DIFFERENT strategy onto an EXISTING instance kept the OLD cadence. The
        # GA optimizes the entry WEEKDAY (see reference-ga-schedule-genes), so the instance then
        # runs the new genome on the previous strategy's calendar -- a silent parity break, with
        # every other setting correct. Hit live 2026-09-14 swapping prod instance 13 from
        # sen-S5 (mon/wed/fri) to sen-S6 (mon/tue/fri): the rules and settings changed, the
        # schedule did not, and only a post-deploy read caught it.
        #
        # ASSIGNED, not setdefault, for the same reason: the payload's cadence IS the strategy's,
        # and silently keeping a stale one is the failure being closed here.
        sched = ((entry["settings"].get("execution") or {}).get("run_schedule_override") or {})
        if sched.get("days"):
            days = {d: bool(v) for d, v in sched["days"].items()}
            times = sched.get("times") or ["09:30"]
            prior = (expert.get_setting_with_interface_default(
                "execution_schedule_enter_market", log_warning=False) or {}).get("days") or {}
            expert_params["execution_schedule_enter_market"] = {
                "days": days, "times": times, "time_basis": "market"}
            # EXITS RUN EVERY WEEKDAY, not on the entry cadence. Entry is typically one or two
            # days a week (that is when the screener re-ranks), but an open position's stop and
            # target have to be evaluated daily -- inheriting the entry schedule here would
            # leave live positions unmanaged for the rest of the week.
            expert_params["execution_schedule_open_positions"] = {
                "days": {d: d not in ("saturday", "sunday") for d in days},
                "times": times, "time_basis": "market"}
            on = sorted(d for d, v in days.items() if v)
            was = sorted(d for d, v in prior.items() if v)
            if was and was != on:
                print(f"  entry schedule: {was} -> {on}  (the new strategy's OWN cadence; the GA "
                      f"optimizes the entry weekday, so keeping the old one would run this "
                      f"genome on the previous strategy's calendar)")
            else:
                print(f"  entry schedule: {on} (exits every weekday)")
        else:
            print("  WARNING: payload carries no run_schedule_override -- the instance will "
                  "have NO schedule and will never fire. Set one before enabling it.")
        expert.save_settings({k: (v, None) for k, v in expert_params.items()})
        print(f"expertsetting: saved {len(expert_params)} keys for instance {inst_id}"
              + ("  (incl. allow_automated_trade_opening)" if created else ""))
        # Behaviours the backtest derived that live has no analogue for. NOT applied -- a
        # deploy that differs from its backtest must SAY so (review V4/V5).
        for k, v in (entry["settings"].get("backtest_only") or {}).items():
            print(f"  BACKTEST-ONLY, not applied: {k}={v!r}")

    print("\n=== deploy complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())

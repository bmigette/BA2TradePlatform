"""The option LIFECYCLE thresholds a ``classic_options`` deploy must carry, in the form that
reproduces its backtest. Shared by ``export_deploy_payload.py`` (writes them),
``import_deploy_payload.py`` (refuses a payload without them) and
``tests/test_deploy_option_lifecycle_thresholds.py`` (pins both against the live constant).

WHY THIS EXISTS (found live 2026-11-13). Four instances deployed from option "style" backtests
with ``risk_manager_mode=classic_options`` carried the sleeve rails but none of the thresholds
``option_lifecycle_service.REQUIRED_SETTINGS`` lists. The first time such an instance held an
option, ``run_option_lifecycle_pass`` aborted every cycle ("does not declare ... These positions
are NOT being managed"), which ALSO skipped the drawdown-breaker update and the covered-call cover
check: the two things the pass shares with the backtest.

WHY THE EXPORT HAD NOTHING TO COPY. A backtest never calls ``option_lifecycle.decide``. Its option
exits are the ruleset's ``close_option`` rules (both runtimes walk them), and of the live pass it
runs only the sleeve breaker (``daily_engine`` step 4c, ``update_sleeve_breaker``). The premium
rules of ``decide`` (profit capture, credit-multiple stop, tested short) are LIVE-ONLY, so no trial
config carries their keys, and any value a config does carry was never read by the run.

THE FORM THAT REPRODUCES THE BACKTEST: every premium rule off.

* ``tested_delta_enabled`` / ``dr_stop_enabled`` / ``ur_stop_enabled`` = False: the switches the
  rules already have. With them off, ``tested_delta`` and the two ``*_credit_mult`` are never read.
* ``profit_capture_pct`` / ``strangle_capture_pct`` = ``UNREACHABLE_PROFIT_CAPTURE_PCT``. Profit
  capture has NO switch: the key is required and numeric, so "off" is a value it cannot reach.
  ``strangle_capture_pct`` is optional live, but ``decide`` requires it for any ``short_strangle``
  and a missing one raises out of ``decide`` -- which aborts the dispatch, breaker flatten and
  cover-lost close included. Written for the same reason as the required five.
* ``roll_dte`` = the backtest's OWN roll window when the ruleset can open a two-expiry structure
  (see ``backtest_roll_dte``), else ``NO_TWO_EXPIRY_ROLL_DTE`` (``decide`` reads it only for a
  ``MULTI_EXPIRY_OPTION_STRATEGIES`` structure, which such a ruleset cannot open).

WHY A SENTINEL AND NOT A SWITCH. An honest ``profit_capture_enabled`` needs a new setting on
``MarketExpertInterface`` and a read in ``option_lifecycle.decide``, both in ``packages/common``.
That is a package bump whose minimum must be raised (neither file can be GA-neutral), which cannot
be pushed while a shared master is mid-job. The sentinel changes no shared code and matches the
manual repair already applied to the four live instances. Replace it with a switch when a minimum
raise is possible; this module is then the one place to change.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from ba2_common.core.OptionRiskManagement import (
    RISK_MANAGER_MODE_CLASSIC_OPTIONS, normalise_risk_manager_mode,
)
from ba2_common.core.rule_builders import action_type_of, rule_carries_action
from ba2_common.core.rules_convert import trade_rules_to_live_export
from ba2_common.core.types import ExpertActionType

#: ``profit_capture_pct`` / ``strangle_capture_pct`` for a deploy whose backtest ran no premium
#: profit capture. ``decide`` closes when ``pnl_pct >= capture``, where ``pnl_pct`` is the percent of
#: the entry net premium. A CREDIT structure's ``pnl_pct`` cannot exceed 100 (the whole credit kept),
#: so it can never fire there; a DEBIT structure would need its value to grow ~10,000x. One million is
#: also the value the manual repair of 2026-11-13 wrote, so new and repaired instances agree.
UNREACHABLE_PROFIT_CAPTURE_PCT: float = 1_000_000.0

#: ``roll_dte`` for a ruleset that cannot open a two-expiry structure. ``decide`` reads it only for a
#: ``MULTI_EXPIRY_OPTION_STRATEGIES`` structure (``pmcc``), so any integer is inert; 0 is the manual
#: repair's value.
NO_TWO_EXPIRY_ROLL_DTE: int = 0

#: The rule-level reader of the roll window: ``TradeConditions.ShortLegDaysToExpiryCondition``, which
#: calls ``option_lifecycle.roll_window_dte`` -- the same function ``decide``'s roll branch calls.
#: So ``roll_dte = N`` live and a ``short_leg_days_to_expiry <= N`` roll rule measure the same day.
ROLL_WINDOW_FIELD = "short_leg_days_to_expiry"

#: Every key this module writes. ``decide`` can ``_require`` each of them with the three switches off.
LIFECYCLE_THRESHOLD_SETTINGS = (
    "profit_capture_pct", "strangle_capture_pct", "roll_dte",
    "tested_delta_enabled", "dr_stop_enabled", "ur_stop_enabled",
)

_ROLL_ACTION = ExpertActionType.ROLL_PMCC_SHORT.value
_TWO_EXPIRY_OPENERS = (ExpertActionType.OPEN_PMCC.value,)


def is_classic_options_payload(expert_params: Mapping[str, Any], *, where: str) -> bool:
    """Does this payload deploy a ``classic_options`` sleeve, i.e. one the live lifecycle pass runs for?

    Strict where the live gate (``option_risk_manager_enabled``) is lenient: an unadmitted mode string
    is REFUSED here. The live gate fails open on garbage so a typo cannot stop OTHER experts trading;
    a deploy can afford to stop and say so before anything is written.
    """
    try:
        mode = normalise_risk_manager_mode(expert_params)
    except ValueError as e:
        raise ValueError(f"{where}: {e}") from e
    return mode == RISK_MANAGER_MODE_CLASSIC_OPTIONS


def backtest_roll_dte(entry_rules: Sequence[dict], exit_rules: Sequence[dict], *, where: str) -> int:
    """The roll window the BACKTEST rolled a two-expiry overlay at, as the live ``roll_dte``.

    Read off the rules exactly as live will run them: the payload's rules go through the same
    converter the importer writes (``trade_rules_to_live_export``), and the roll rule is recognised by
    the helper the live pass itself uses to ask "does a rule own this roll?" (``rule_carries_action``).

    * no enter_market rule opens a two-expiry structure: ``NO_TWO_EXPIRY_ROLL_DTE``;
    * else exactly ONE window ``short_leg_days_to_expiry <= N`` on the roll rules: ``N``.

    Refused: a two-expiry opener with no roll window (the backtest let the overlay run to expiry, and
    live would raise ``UnownedRollError`` on it), a window spelled other than ``<=``, a non-integer
    window, or two different windows (no single ``roll_dte`` reproduces both).
    """
    live = trade_rules_to_live_export(list(entry_rules), list(exit_rules), name=where)
    by_subtype: Dict[str, List[dict]] = {}
    for ruleset in live["rulesets"]:
        by_subtype.setdefault(ruleset["subtype"], []).extend(ruleset["rules"])

    opens_two_expiry = any(
        action_type_of(action) in _TWO_EXPIRY_OPENERS
        for rule in by_subtype.get("enter_market", [])
        for action in rule["actions"].values())

    windows = set()
    for rule in by_subtype.get("open_positions", []):
        if not rule_carries_action(rule["actions"], _ROLL_ACTION):
            continue
        for trigger in rule["triggers"].values():
            if trigger["event_type"] != ROLL_WINDOW_FIELD:
                continue
            if trigger["operator"] != "<=":
                raise ValueError(
                    f"{where}: roll rule {rule['name']!r} reads {ROLL_WINDOW_FIELD} with "
                    f"{trigger['operator']!r}; the live roll_dte means '<=', so this window has no "
                    f"exact live equivalent")
            value = float(trigger["value"])
            if not value.is_integer():
                raise ValueError(f"{where}: roll rule {rule['name']!r} has a non-integer window "
                                 f"{trigger['value']!r} (roll_dte is a whole number of days)")
            windows.add(int(value))

    if not opens_two_expiry:
        return NO_TWO_EXPIRY_ROLL_DTE
    if not windows:
        raise ValueError(
            f"{where}: the entry rules open a two-expiry structure ({', '.join(_TWO_EXPIRY_OPENERS)}) "
            f"but no {_ROLL_ACTION!r} rule carries a {ROLL_WINDOW_FIELD} <= N window. There is no "
            f"backtest roll window to deploy as roll_dte, and live would raise UnownedRollError")
    if len(windows) > 1:
        raise ValueError(
            f"{where}: the roll rules carry {len(windows)} different windows {sorted(windows)}; one "
            f"live roll_dte cannot reproduce them all")
    return windows.pop()


def deploy_lifecycle_thresholds(expert_params: Mapping[str, Any], entry_rules: Sequence[dict],
                                exit_rules: Sequence[dict], *, where: str) -> Dict[str, Any]:
    """``{setting: value}`` the deploy must write so the live pass runs what the backtest ran.

    Empty for a payload that is not ``classic_options`` (the live pass does nothing for it).
    """
    if not is_classic_options_payload(expert_params, where=where):
        return {}
    return {
        "profit_capture_pct": UNREACHABLE_PROFIT_CAPTURE_PCT,
        "strangle_capture_pct": UNREACHABLE_PROFIT_CAPTURE_PCT,
        "roll_dte": backtest_roll_dte(entry_rules, exit_rules, where=where),
        "tested_delta_enabled": False,
        "dr_stop_enabled": False,
        "ur_stop_enabled": False,
    }


def missing_settings(expert_params: Mapping[str, Any], required: Sequence[str]) -> List[str]:
    """The names in ``required`` the payload does not state (absent, or None)."""
    return [k for k in required if k not in expert_params or expert_params[k] is None]


def assert_deployable_lifecycle(expert_params: Mapping[str, Any], entry_rules: Sequence[dict],
                                exit_rules: Sequence[dict], required: Sequence[str], *,
                                where: str) -> Optional[Dict[str, Any]]:
    """Refuse a ``classic_options`` payload a live instance could not run as its backtest did.

    ``required``: what the live platform requires of a classic_options sleeve. The importer passes
    ``option_lifecycle_service.REQUIRED_SETTINGS`` + ``OptionRiskManagement.REQUIRED_RAIL_SETTINGS``,
    the live code's own constants, never a copy.

    Raises ValueError naming every missing setting (``required`` plus the lifecycle keys this module
    writes), then every lifecycle threshold whose value is not
    the one ``deploy_lifecycle_thresholds`` derives from the payload's own rules (a stale or
    hand-edited payload would add an exit the backtest never ran). Returns the verified thresholds,
    or None for a payload that is not ``classic_options``.
    """
    expected = deploy_lifecycle_thresholds(expert_params, entry_rules, exit_rules, where=where)
    if not expected:
        return None
    # The required names first, then the lifecycle keys live reads conditionally
    # (``strangle_capture_pct``): absent, ``decide`` raises on a short_strangle and the dispatch aborts.
    missing = missing_settings(expert_params,
                               list(required) + [k for k in expected if k not in required])
    if missing:
        raise ValueError(
            f"{where}: risk_manager_mode={RISK_MANAGER_MODE_CLASSIC_OPTIONS!r} but the payload does "
            f"not state {', '.join(missing)}. The live option pass refuses to manage a sleeve "
            f"without them (its breaker and cover checks stop with it). Re-export it with "
            f"tools/export_deploy_payload.py, which writes the lifecycle thresholds; a missing rail "
            f"means the backtest config itself lacked it")
    wrong = {k: (expert_params[k], v) for k, v in expected.items()
             if k in expert_params and expert_params[k] != v}
    if wrong:
        raise ValueError(
            f"{where}: lifecycle threshold(s) differ from the form that reproduces the backtest: "
            + "; ".join(f"{k}={got!r} (backtest form {want!r})" for k, (got, want) in wrong.items())
            + ". A backtest runs no premium exit, so any other value adds an exit it never scored")
    return expected

"""A deployed ``classic_options`` instance must carry every setting the live option pass requires.

Found live 2026-11-13: four instances deployed by ``tools/export_deploy_payload.py`` +
``tools/import_deploy_payload.py`` from option style backtests carried the sleeve rails but not
``profit_capture_pct`` / ``roll_dte`` / ``tested_delta_enabled`` / ``dr_stop_enabled`` /
``ur_stop_enabled``. Once one held an option, ``run_option_lifecycle_pass`` aborted every cycle,
skipping the breaker update and the cover check with it. A backtest never runs those premium exits,
so its config had nothing for the exporter to copy.

What is pinned here:

* PARITY: every name the live pass requires (``option_lifecycle_service.REQUIRED_SETTINGS``, read
  from the live module, not copied) is present after the export step on a payload that carries only
  what a classic_options backtest guarantees (its rails). A requirement added upstream fails this
  until the exporter handles it.
* BEHAVIOUR: the written thresholds, fed through the live accessor into the real ``decide``, close
  nothing a backtest would not (no profit capture, credit stop or tested-short close) while the
  breaker and the cover check still act.
* The PMCC roll window is the backtest's own (``short_leg_days_to_expiry <= N`` on its roll rule).
* The importer REFUSES a payload without them, by name, before anything is written.

The tools cannot be imported here (each reconfigures the process-global DB at import time, see
``testplatform/backend/tests/backtest/test_deploy_round_trip_parity.py``), so the shared helper
module is tested directly and the tools' source is checked for the calls.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOLS = os.path.join(_REPO, "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

from deploy_option_lifecycle import (  # noqa: E402
    LIFECYCLE_THRESHOLD_SETTINGS, NO_TWO_EXPIRY_ROLL_DTE, ROLL_WINDOW_FIELD,
    UNREACHABLE_PROFIT_CAPTURE_PCT, assert_deployable_lifecycle, backtest_roll_dte,
    deploy_lifecycle_thresholds,
)

from ba2_common.core.OptionRiskManagement import REQUIRED_RAIL_SETTINGS  # noqa: E402
from ba2_common.core.option_lifecycle import (  # noqa: E402
    LIFECYCLE_BREAKER, LIFECYCLE_COVER_LOST, LIFECYCLE_HOLD, LIFECYCLE_ROLL_SHORT,
    SETTING_BREAKER_TRIPPED, LifecycleLeg, OptionStructure, decide,
)
from ba2_common.core.option_types import OptionContract  # noqa: E402
from ba2_common.core.types import OptionRight  # noqa: E402
from ba2_trade_platform.core.option_lifecycle_service import (  # noqa: E402
    REQUIRED_SETTINGS, _lifecycle_settings,
)

#: What the importer enforces, assembled the way it assembles it (the source guard below pins that).
LIVE_REQUIRED = tuple(dict.fromkeys(REQUIRED_SETTINGS + REQUIRED_RAIL_SETTINGS))

AS_OF = datetime(2026, 3, 2, 14, 30, tzinfo=timezone.utc)
FAR = date(2026, 4, 17)

#: The rails a classic_options STYLE backtest always carries (the launcher refuses a style risk
#: block without them: ``_STYLE_RISK_REQUIRED``, pinned equal to REQUIRED_RAIL_SETTINGS). Exactly
#: the shape of the four instances found on 2026-11-13: rails, and nothing for the lifecycle pass.
BACKTEST_PARAMS = {
    "risk_manager_mode": "classic_options",
    "max_concurrent_structures": 6, "max_deployment_pct": 40.0, "max_notional_leverage": 3.0,
    "circuit_breaker_pct": 25.0, "undefined_risk_max_pct": 10.0,
}

ENTRY_CSP = [{"name": "csp", "conditions": None,
              "actions": [{"action_type": "sell_cash_secured_put", "option_dte_min": 20,
                           "option_dte_max": 45}]}]
EXIT_DTE = [{"name": "opt_dte", "actions": [{"action_type": "close_option"}],
             "conditions": {"type": "AND", "conditions": [
                 {"field": "days_to_expiry", "op": "<=", "value": 21}]}}]


def _pmcc_rules(*windows, op="<="):
    entry = [{"name": "pmcc", "conditions": None,
              "actions": [{"action_type": "open_pmcc", "option_dte_min": 365}]}]
    exits = list(EXIT_DTE) + [
        {"name": f"pmcc_roll_dte_{i}", "continue_processing": True,
         "actions": [{"action_type": "roll_pmcc_short"}],
         "conditions": {"type": "AND", "conditions": [
             {"field": ROLL_WINDOW_FIELD, "op": op, "value": w}]}}
        for i, w in enumerate(windows)]
    return entry, exits


def _exported(params=None, entry=ENTRY_CSP, exits=EXIT_DTE):
    """The expert_params the exporter writes: the backtest's, plus the lifecycle thresholds."""
    params = dict(BACKTEST_PARAMS if params is None else params)
    params.update(deploy_lifecycle_thresholds(params, entry, exits, where="t"))
    return params


class _Expert:
    """The accessor the live pass reads thresholds through, over a deployed instance's settings."""

    def __init__(self, settings):
        self._s = settings

    def get_setting_with_interface_default(self, key, log_warning=True):
        return self._s[key] if key in self._s else None


# ==================================================================================================
# THE PARITY PIN
# ==================================================================================================
def test_every_setting_the_live_pass_requires_is_carried_by_a_classic_options_deploy():
    """THE pin. Adding a name to ``option_lifecycle_service.REQUIRED_SETTINGS`` fails this until the
    exporter writes it (or the backtest carries it as a rail)."""
    exported = _exported()
    missing = [k for k in REQUIRED_SETTINGS if k not in exported or exported[k] is None]
    assert not missing, (
        f"a classic_options deploy does not carry {missing}, which the live option pass requires; "
        f"teach tools/deploy_option_lifecycle.py to write them in the backtest's form")
    # Stated, so the split is visible: the rails come from the backtest, the rest from the exporter.
    assert set(REQUIRED_SETTINGS) - set(REQUIRED_RAIL_SETTINGS) <= set(LIFECYCLE_THRESHOLD_SETTINGS)
    # ... and the live accessor agrees: nothing missing, the pass will not abort.
    _settings, live_missing = _lifecycle_settings(_Expert(exported))
    assert live_missing == []


def test_the_exported_payload_passes_the_importer_check_after_a_json_round_trip():
    exported = json.loads(json.dumps(_exported()))
    verified = assert_deployable_lifecycle(exported, ENTRY_CSP, EXIT_DTE, LIVE_REQUIRED, where="t")
    assert verified == {
        "profit_capture_pct": UNREACHABLE_PROFIT_CAPTURE_PCT,
        "strangle_capture_pct": UNREACHABLE_PROFIT_CAPTURE_PCT,
        "roll_dte": NO_TWO_EXPIRY_ROLL_DTE,
        "tested_delta_enabled": False, "dr_stop_enabled": False, "ur_stop_enabled": False,
    }


def test_a_payload_that_is_not_classic_options_gets_nothing_and_is_not_checked():
    params = {"risk_manager_mode": "classic", "enable_buy": True}
    assert deploy_lifecycle_thresholds(params, ENTRY_CSP, EXIT_DTE, where="t") == {}
    assert assert_deployable_lifecycle(params, ENTRY_CSP, EXIT_DTE, LIVE_REQUIRED, where="t") is None
    assert deploy_lifecycle_thresholds({}, ENTRY_CSP, EXIT_DTE, where="t") == {}


def test_an_unadmitted_mode_is_refused_rather_than_read_as_not_classic_options():
    with pytest.raises(ValueError, match="classic_optoins"):
        deploy_lifecycle_thresholds({"risk_manager_mode": "classic_optoins"}, ENTRY_CSP, EXIT_DTE,
                                    where="t")


# ==================================================================================================
# THE IMPORTER'S REFUSAL
# ==================================================================================================
def test_the_payload_shape_found_live_is_refused_naming_each_missing_setting():
    """Rails only, as the four instances of 2026-11-13 were deployed."""
    with pytest.raises(ValueError) as e:
        assert_deployable_lifecycle(dict(BACKTEST_PARAMS), ENTRY_CSP, EXIT_DTE, LIVE_REQUIRED,
                                    where="inst")
    for name in ("profit_capture_pct", "roll_dte", "tested_delta_enabled", "dr_stop_enabled",
                 "ur_stop_enabled", "strangle_capture_pct"):
        assert name in str(e.value)
    assert "circuit_breaker_pct" not in str(e.value)


def test_a_missing_rail_is_refused_by_name():
    params = _exported()
    del params["max_notional_leverage"]
    with pytest.raises(ValueError, match="max_notional_leverage"):
        assert_deployable_lifecycle(params, ENTRY_CSP, EXIT_DTE, LIVE_REQUIRED, where="t")


def test_a_requirement_added_upstream_is_refused_without_an_edit_here():
    """The importer passes the live constants, so a new required name is enforced at once."""
    with pytest.raises(ValueError, match="a_threshold_added_upstream"):
        assert_deployable_lifecycle(_exported(), ENTRY_CSP, EXIT_DTE,
                                    LIVE_REQUIRED + ("a_threshold_added_upstream",), where="t")


@pytest.mark.parametrize("key,value", [("profit_capture_pct", 50.0), ("dr_stop_enabled", True),
                                       ("roll_dte", 21), ("strangle_capture_pct", 25.0)])
def test_a_threshold_that_would_add_an_exit_the_backtest_never_ran_is_refused(key, value):
    params = {**_exported(), key: value}
    with pytest.raises(ValueError, match=key):
        assert_deployable_lifecycle(params, ENTRY_CSP, EXIT_DTE, LIVE_REQUIRED, where="t")


# ==================================================================================================
# BEHAVIOUR: the written thresholds close nothing a backtest would not
# ==================================================================================================
def _c(sym, *, bid, ask, right=OptionRight.PUT, strike=100.0, expiry=FAR, delta=None):
    return OptionContract(symbol=sym, underlying="XYZ", option_type=right, strike=strike,
                          expiry=expiry, bid=bid, ask=ask, delta=delta)


def _leg(sym, qty, *, right=OptionRight.PUT, strike=100.0, expiry=FAR):
    return LifecycleLeg(sym, net_qty=qty, strike=strike, option_type=right, expiry=expiry,
                        underlying="XYZ")


def _spread(txn, credit=2.00):
    return OptionStructure(transaction_id=txn, underlying="XYZ", strategy="put_credit_spread",
                           legs=[_leg("P100", -1.0), _leg("P95", 1.0, strike=95.0)], quantity=1,
                           multiplier=100, entry_net_premium=-credit, expiry=FAR)


def _live_settings():
    """The thresholds exactly as the live pass hands them to ``decide``."""
    settings, missing = _lifecycle_settings(_Expert(_exported()))
    assert missing == []
    return settings


@pytest.mark.parametrize("label,short_ask,long_bid,short_delta", [
    ("99% of the credit captured", 0.12, 0.10, -0.05),
    ("losing 5x the credit", 12.10, 0.10, -0.60),
    ("short leg deep in the money (tested)", 6.10, 0.10, -0.95),
])
def test_no_premium_rule_closes_a_credit_spread(label, short_ask, long_bid, short_delta):
    chain = {"P100": _c("P100", bid=short_ask - 0.05, ask=short_ask, delta=short_delta),
             "P95": _c("P95", bid=long_bid, ask=long_bid + 0.05, strike=95.0, delta=-0.02)}
    (d,) = decide([_spread(1)], chain, _live_settings(), AS_OF)
    assert d.reason == LIFECYCLE_HOLD, f"{label}: {d.reason} ({d.detail})"


def test_no_profit_capture_closes_a_short_strangle_and_nothing_raises_for_one():
    st = OptionStructure(
        transaction_id=2, underlying="XYZ", strategy="short_strangle",
        legs=[_leg("P90", -1.0, strike=90.0), _leg("C110", -1.0, right=OptionRight.CALL,
                                                   strike=110.0)],
        quantity=1, multiplier=100, entry_net_premium=-3.00, expiry=FAR)
    chain = {"P90": _c("P90", bid=0.01, ask=0.02, strike=90.0),
             "C110": _c("C110", bid=0.0, ask=0.01, right=OptionRight.CALL, strike=110.0)}
    (d,) = decide([st], chain, _live_settings(), AS_OF)
    assert d.reason == LIFECYCLE_HOLD, d.detail


def test_a_long_option_up_five_thousand_fold_is_still_not_profit_captured():
    """The sentinel's only reach: a DEBIT structure must gain ~10,000x to touch it."""
    st = OptionStructure(transaction_id=3, underlying="XYZ", strategy="long_call",
                         legs=[_leg("C100", 1.0, right=OptionRight.CALL)], quantity=1,
                         multiplier=100, entry_net_premium=0.01, expiry=FAR)
    chain = {"C100": _c("C100", bid=50.0, ask=50.1, right=OptionRight.CALL)}
    (d,) = decide([st], chain, _live_settings(), AS_OF)
    assert d.reason == LIFECYCLE_HOLD, d.detail


def test_the_breaker_and_the_cover_check_still_act():
    """What the pass shares with the backtest (the breaker) and its naked-call guard keep working."""
    chain = {"P100": _c("P100", bid=1.0, ask=1.1), "P95": _c("P95", bid=0.1, ask=0.15, strike=95.0)}
    tripped = {**_live_settings(), SETTING_BREAKER_TRIPPED: True}
    (d,) = decide([_spread(1)], chain, tripped, AS_OF)
    assert d.reason == LIFECYCLE_BREAKER

    cc = OptionStructure(transaction_id=4, underlying="XYZ", strategy="covered_call",
                         legs=[_leg("C105", -1.0, right=OptionRight.CALL, strike=105.0)],
                         quantity=1, multiplier=100, entry_net_premium=-1.00, expiry=FAR)
    cc_chain = {"C105": _c("C105", bid=0.5, ask=0.6, right=OptionRight.CALL, strike=105.0)}
    (d,) = decide([cc], cc_chain, _live_settings(), AS_OF,
                  cover_shares_required=100, cover_shares_held=0)
    assert d.reason == LIFECYCLE_COVER_LOST


# ==================================================================================================
# roll_dte: the backtest's own PMCC roll window
# ==================================================================================================
def test_a_ruleset_that_cannot_open_a_two_expiry_structure_gets_the_inert_roll_dte():
    assert backtest_roll_dte(ENTRY_CSP, EXIT_DTE, where="t") == NO_TWO_EXPIRY_ROLL_DTE


def test_a_pmcc_ruleset_deploys_its_own_roll_window():
    entry, exits = _pmcc_rules(4)
    assert backtest_roll_dte(entry, exits, where="t") == 4
    assert deploy_lifecycle_thresholds(BACKTEST_PARAMS, entry, exits, where="t")["roll_dte"] == 4


def test_the_live_roll_observation_fires_on_the_day_the_backtest_rule_rolls():
    """``roll_dte = N`` and the rule's ``short_leg_days_to_expiry <= N`` read the same function
    (``roll_window_dte``), so the live pass reports the roll due on the rule's day, not before."""
    entry, exits = _pmcc_rules(4)
    settings, missing = _lifecycle_settings(_Expert(_exported(entry=entry, exits=exits)))
    assert missing == []

    def pmcc(short_dte):
        short_exp = AS_OF.date() + timedelta(days=short_dte)
        leaps = AS_OF.date() + timedelta(days=400)
        st = OptionStructure(
            transaction_id=5, underlying="XYZ", strategy="pmcc",
            legs=[_leg("LEAP", 1.0, right=OptionRight.CALL, strike=80.0, expiry=leaps),
                  _leg("SC", -1.0, right=OptionRight.CALL, strike=110.0, expiry=short_exp)],
            quantity=1, multiplier=100, entry_net_premium=20.0, expiry=None)
        chain = {"LEAP": _c("LEAP", bid=25.0, ask=25.5, right=OptionRight.CALL, strike=80.0,
                            expiry=leaps),
                 "SC": _c("SC", bid=0.2, ask=0.25, right=OptionRight.CALL, strike=110.0,
                          expiry=short_exp)}
        (d,) = decide([st], chain, settings, AS_OF)
        return d.reason

    assert pmcc(4) == LIFECYCLE_ROLL_SHORT
    assert pmcc(5) == LIFECYCLE_HOLD


def test_two_different_roll_windows_are_refused():
    entry, exits = _pmcc_rules(3, 5)
    with pytest.raises(ValueError, match=r"\[3, 5\]"):
        backtest_roll_dte(entry, exits, where="t")


def test_a_pmcc_opener_without_a_roll_window_is_refused():
    entry, _ = _pmcc_rules()
    with pytest.raises(ValueError, match="no 'roll_pmcc_short' rule"):
        backtest_roll_dte(entry, EXIT_DTE, where="t")


def test_a_roll_window_that_is_not_spelled_le_is_refused():
    entry, exits = _pmcc_rules(4, op="<")
    with pytest.raises(ValueError, match="'<'"):
        backtest_roll_dte(entry, exits, where="t")


def test_the_launcher_still_builds_the_roll_rule_this_module_reads():
    """The roll window is recognised by field + operator + action; the launcher is the author."""
    src = open(os.path.join(_REPO, "testplatform", "ba2test_launcher.py"), encoding="utf-8").read()
    assert f'"field": "{ROLL_WINDOW_FIELD}", "op": "<="' in src
    assert '"action_type": "roll_pmcc_short"' in src


# ==================================================================================================
# the tools route through this module (source-level: they cannot be imported in-process)
# ==================================================================================================
def _src(name):
    return open(os.path.join(_TOOLS, name), encoding="utf-8").read()


def test_the_exporter_writes_the_thresholds_before_the_payload_is_built():
    exp = _src("export_deploy_payload.py")
    assert "from deploy_option_lifecycle import deploy_lifecycle_thresholds" in exp
    assert exp.index("_write_lifecycle_thresholds(settings, ruleset") < exp.index(
        "payloads.append(build_deploy_entry(")


def test_the_importer_refuses_from_the_live_constants_before_its_first_write():
    imp = _src("import_deploy_payload.py")
    assert "from ba2_trade_platform.core.option_lifecycle_service import" in imp
    assert "REQUIRED_SETTINGS as LIFECYCLE_REQUIRED_SETTINGS" in imp
    assert ("LIVE_CLASSIC_OPTIONS_REQUIRED = tuple(dict.fromkeys(LIFECYCLE_REQUIRED_SETTINGS\n"
            "                                                    + REQUIRED_RAIL_SETTINGS))") in imp
    check = imp.index("assert_deployable_lifecycle(")
    assert check < imp.index("add_instance(inst)")
    assert check < imp.index("import_multiple_rulesets(live_export)")
    assert check < imp.index("save_settings(")

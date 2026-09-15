"""The dependency-resolver contract (spec step 4, section 5).

The per-expert knowledge is tested in ``packages/experts``. What is tested here is
the contract every adapter is dispatched through, and the three things that turn a
warm plan into a lie when they are wrong:

* an expert nobody wrote an adapter for must come back ``unsupported`` -- one
  requirement, not an empty list a coverage report renders green;
* a setting an adapter needs must be READ, never defaulted;
* the rules and the risk manager contribute reads the expert class cannot
  describe (ATR, the earnings calendar, the cooldown ledger).
"""
from datetime import datetime, timedelta, timezone

import pytest

from ba2_common.core.replay import dependencies as dep

NOW = datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc)
WINDOW = dep.Window(start=NOW - timedelta(days=365), end=NOW)

#: A settings dict carrying every key ``rule_requirements`` reads, so a test that
#: is not about a missing key does not accidentally test one.
RM_SETTINGS = {"use_atr_stop": False, "sizing_mode": "notional", "atr_period": 14}


def _event_action(*event_types):
    """An EventAction-shaped object: what the live host actually holds."""
    class _EA:
        triggers = {f"t{i}": {"event_type": e, "operator": ">", "value": 1}
                    for i, e in enumerate(event_types)}
    return _EA()


@pytest.fixture
def registered():
    """Register a throwaway adapter and always remove it again."""
    calls = []

    def adapter(settings, universe, window):
        calls.append((dict(settings), list(universe), window))
        return [dep.Requirement(
            provider=dep.FMP_PROVIDER, namespace="price_target", symbol=universe[0],
            window=window, interval=None, kind=dep.KIND_HISTORY, optional=False,
            reason="test adapter")]

    dep.register_adapter("TestExpert", adapter)
    try:
        yield calls
    finally:
        dep.unregister_adapter("TestExpert")


# --------------------------------------------------------------------------- #
# Unsupported is never an empty list
# --------------------------------------------------------------------------- #
def test_expert_without_an_adapter_resolves_to_exactly_one_unsupported_requirement():
    out = dep.required_replay_inputs("NoSuchExpert", RM_SETTINGS, None, ["AAPL"], WINDOW)

    assert len(out) == 1, "an undeclared expert must not produce a list that reads as coverage"
    assert out[0].kind == dep.KIND_UNSUPPORTED
    assert "NoSuchExpert" in out[0].reason


def test_unsupported_does_not_pick_up_rule_extras():
    """A partial list presented as a complete one is worse than an honest refusal."""
    settings = {"use_atr_stop": True, "sizing_mode": "risk_atr", "atr_period": 14}
    out = dep.required_replay_inputs("NoSuchExpert", settings,
                                     [_event_action("days_to_earnings")], ["AAPL"], WINDOW)

    assert [r.kind for r in out] == [dep.KIND_UNSUPPORTED]


def test_registered_expert_dispatches_to_its_adapter(registered):
    out = dep.required_replay_inputs("TestExpert", RM_SETTINGS, None, ["aapl", "AAPL", " msft "],
                                     WINDOW)

    assert registered[0][1] == ["AAPL", "MSFT"], "symbols are cleaned once, for every adapter"
    assert [r.namespace for r in out] == ["price_target"]
    assert "TestExpert" in dep.registered_experts()


# --------------------------------------------------------------------------- #
# Settings are read, never defaulted
# --------------------------------------------------------------------------- #
def test_a_missing_setting_is_a_loud_error_naming_the_key_and_the_requirement():
    with pytest.raises(dep.MissingDependencySetting) as excinfo:
        dep.rule_requirements({}, None, ["AAPL"], WINDOW)

    assert excinfo.value.key == "use_atr_stop"
    assert "ATR" in str(excinfo.value)


def test_require_setting_returns_a_falsy_value_rather_than_treating_it_as_absent():
    assert dep.require_setting({"w_analyst": 0.0}, "w_analyst", needed_by="x") == 0.0


@pytest.mark.parametrize("value,expected", [
    (True, True), (False, False), ("true", True), ("1", True), ("0", False),
    ("False", False), (1, True), (0, False),
])
def test_as_bool_reads_every_spelling_a_setting_row_can_hold(value, expected):
    assert dep.as_bool(value) is expected


@pytest.mark.parametrize("value", ["maybe", None, ""])
def test_as_bool_refuses_anything_that_does_not_mean_a_boolean(value):
    """``None`` included: a present-but-null row is broken, not "off".

    Reading it as False silently drops the ATR declaration from the plan, which is the
    same damage a missing key does -- so it fails the same way.
    """
    with pytest.raises(ValueError):
        dep.as_bool(value)


# --------------------------------------------------------------------------- #
# Rule and risk-manager extras
# --------------------------------------------------------------------------- #
def test_atr_stop_adds_one_daily_indicator_requirement_per_symbol():
    settings = {"use_atr_stop": True, "sizing_mode": "risk_atr", "atr_period": 20}

    out = dep.rule_requirements(settings, None, ["AAPL", "MSFT"], WINDOW)

    atr = [r for r in out if r.kind == dep.KIND_INDICATOR]
    assert [r.symbol for r in atr] == ["AAPL", "MSFT"]
    assert {r.interval for r in atr} == {"1d"}, "indicator lookbacks use daily bars"
    assert {r.namespace for r in atr} == {"atr_20"}
    # max(period*4, 60) -- the window get_latest_atr actually asks for.
    assert (atr[0].window.end - atr[0].window.start).days == 80
    assert "risk_atr" in atr[0].reason


def test_atr_is_declared_in_notional_mode_too_because_the_safeguard_stop_still_reads_it():
    settings = {"use_atr_stop": True, "sizing_mode": "notional", "atr_period": 14}

    out = dep.rule_requirements(settings, None, ["AAPL"], WINDOW)

    assert [r.kind for r in out] == [dep.KIND_INDICATOR]
    assert (out[0].window.end - out[0].window.start).days == 60


def test_use_atr_stop_off_removes_the_indicator_requirement():
    settings = {"use_atr_stop": False, "sizing_mode": "risk_atr", "atr_period": 14}

    assert dep.rule_requirements(settings, None, ["AAPL"], WINDOW) == []


def test_an_earnings_condition_adds_the_calendar_and_its_optional_annual_fallback():
    out = dep.rule_requirements(RM_SETTINGS, [_event_action("days_to_earnings")],
                                ["AAPL"], WINDOW)

    by_ns = {r.namespace: r for r in out}
    assert by_ns[dep.EARNINGS_CALENDAR_NAMESPACE].optional is False
    assert by_ns[dep.EARNINGS_ESTIMATES_NAMESPACE].optional is True, (
        "the annual estimate period is a documented FALLBACK, not a proven live input")


def test_a_cooldown_condition_adds_a_state_marker_that_nothing_can_warm():
    out = dep.rule_requirements(RM_SETTINGS, [_event_action("days_since_last_profitable_close")],
                                ["AAPL", "MSFT"], WINDOW)

    assert [r.kind for r in out] == [dep.KIND_STATE]
    assert out[0].symbol is None, "the cooldown ledger is per expert, not per symbol"
    assert out[0].provider == dep.PLATFORM_PROVIDER


def test_rules_with_no_data_conditions_add_nothing():
    assert dep.rule_requirements(RM_SETTINGS, [_event_action("profit_loss_percent")],
                                 ["AAPL"], WINDOW) == []


# --------------------------------------------------------------------------- #
# Reading rules out of the shapes callers actually hold
# --------------------------------------------------------------------------- #
def test_event_types_are_read_from_event_action_rows_dicts_and_plain_strings():
    rows = [_event_action("days_to_earnings", "confidence")]
    dicts = [{"triggers": {"a": {"event_type": "days_to_earnings"}}}]
    ruleset = {"event_actions": dicts}

    assert dep.iter_rule_event_types(rows) == ["days_to_earnings", "confidence"]
    assert dep.iter_rule_event_types(dicts) == ["days_to_earnings"]
    assert dep.iter_rule_event_types(ruleset) == ["days_to_earnings"]
    assert dep.iter_rule_event_types(["days_to_earnings"]) == ["days_to_earnings"]
    assert dep.iter_rule_event_types(None) == []


def test_a_rule_shape_this_does_not_understand_raises_rather_than_reading_zero_conditions():
    with pytest.raises(TypeError):
        dep.iter_rule_event_types([object()])


# --------------------------------------------------------------------------- #
# Requirement identity and round-trip
# --------------------------------------------------------------------------- #
def test_the_key_ignores_the_window_so_two_spans_of_one_payload_are_one_fetch():
    a = dep.Requirement(provider="fmp", namespace="price_target", symbol="AAPL", window=WINDOW,
                        interval=None, kind=dep.KIND_HISTORY, optional=False, reason="a")
    b = dep.Requirement(provider="fmp", namespace="price_target", symbol="aapl",
                        window=WINDOW.trailing(30), interval=None, kind=dep.KIND_HISTORY,
                        optional=True, reason="b")

    assert a.key == b.key
    assert [r.reason for r in dep.dedupe([a, b])] == ["a"]


def test_dedupe_keeps_the_required_copy_even_when_the_optional_one_came_first():
    optional = dep.Requirement(provider="fmp", namespace="past_earnings_quarterly", symbol="AAPL",
                               window=WINDOW, interval=None, kind=dep.KIND_HISTORY, optional=True,
                               reason="optional")
    required = dep.Requirement(provider="fmp", namespace="past_earnings_quarterly", symbol="AAPL",
                               window=WINDOW, interval=None, kind=dep.KIND_HISTORY, optional=False,
                               reason="required")

    assert [r.reason for r in dep.dedupe([optional, required])] == ["required"]


def test_requirement_and_window_round_trip_through_their_mappings():
    req = dep.Requirement(provider="fmp", namespace="atr_14", symbol="AAPL",
                          window=WINDOW.trailing(60), interval="1d", kind=dep.KIND_INDICATOR,
                          optional=False, reason="r")

    assert dep.Requirement.from_mapping(req.to_mapping()) == req


def test_a_timeseries_requirement_must_name_its_interval():
    with pytest.raises(ValueError):
        dep.Requirement(provider="fmp", namespace="ohlcv", symbol="AAPL", window=WINDOW,
                        interval=None, kind=dep.KIND_TIMESERIES, optional=False, reason="r")


def test_a_naive_window_is_refused_because_a_cache_mtime_comparison_needs_utc():
    with pytest.raises(ValueError):
        dep.Window(start=None, end=datetime(2026, 9, 11, 20, 0))

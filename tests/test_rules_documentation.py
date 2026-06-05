from ba2_trade_platform.core.rules_documentation import (
    get_event_type_documentation, get_action_type_documentation, get_rules_overview_html,
)
from ba2_trade_platform.core.types import ExpertEventType, ExpertActionType


NEW_EVENTS = [
    ExpertEventType.N_PERCENT_BELOW_RECENT_HIGH, ExpertEventType.N_PERCENT_ABOVE_RECENT_LOW,
    ExpertEventType.N_IV_RANK, ExpertEventType.F_HAS_OPTION_POSITION,
    ExpertEventType.F_HAS_COVERED_CALL,
]
NEW_ACTIONS = [
    ExpertActionType.BUY_CALL, ExpertActionType.OPEN_BULL_CALL_SPREAD,
    ExpertActionType.SELL_COVERED_CALL, ExpertActionType.CLOSE_OPTION,
]


def test_new_events_documented():
    docs = get_event_type_documentation()
    for ev in NEW_EVENTS:
        assert ev.value in docs, ev.value
        entry = docs[ev.value]
        assert entry["name"] and entry["description"] and entry["example"]
        assert entry["type"] in ("boolean", "numeric")
    # correct numeric/boolean classification
    assert docs[ExpertEventType.N_IV_RANK.value]["type"] == "numeric"
    assert docs[ExpertEventType.F_HAS_OPTION_POSITION.value]["type"] == "boolean"


def test_new_actions_documented():
    docs = get_action_type_documentation()
    for ac in NEW_ACTIONS:
        assert ac.value in docs, ac.value
        entry = docs[ac.value]
        assert entry["name"] and entry["description"]
        assert isinstance(entry["use_cases"], list) and len(entry["use_cases"]) >= 1


def test_overview_html_renders_without_error():
    html = get_rules_overview_html()   # must not KeyError on use_cases
    assert "buy_call" in html or "Buy Call" in html or "Call" in html

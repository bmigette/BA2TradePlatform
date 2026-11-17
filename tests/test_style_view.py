"""Pure style-summary builder (ba2_trade_platform.core.style_view); shapes copied from a deployed O_STYLE expert."""
from ba2_trade_platform.core.style_view import build_style_summary


def _leaf(field, op=None, value=None):
    d = {"event_type": field}
    if op is not None:
        d["operator"] = op
    if value is not None:
        d["value"] = value
    return d


def _trig(*leaves):
    return {f"cond_{i}": leaf for i, leaf in enumerate(leaves)}


STRD = {"action_type": "open_straddle", "strike_method": "percent_otm", "strike_param": 0.0,
        "dte_min": 40, "dte_max": 60, "sizing": 8.0, "min_volume": 25, "entry_cross": 0.9,
        "entry_tag": 12}
ENTER = [
    {"name": "O_STRD-entry-hold", "continue_processing": False,
     "triggers": _trig(_leaf("current_rating_neutral"), _leaf("has_no_position"),
                       _leaf("iv_rank", "<", 40.0),
                       _leaf("underlying_trend_slope_50_atr14", ">", -0.15000000000000002),
                       _leaf("channel_pos_20", "<", 0.7000000000000001)),
     "actions": {"a0": STRD}},
    {"name": "O_STRD-entry-low_confidence", "continue_processing": False,
     "triggers": _trig(_leaf("confidence", "<=", 15.0), _leaf("iv_rank", "<", 40.0)),
     "actions": {"a0": dict(STRD, dte_min=45, dte_max=65, entry_cross=0.8)}},
    {"name": "O_LC-entry", "continue_processing": False,
     "triggers": _trig(_leaf("rec_direction", ">", 0.0)),
     "actions": {"a0": {"action_type": "buy_call", "option_entry_tag": 13, "dte_min": 90,
                        "dte_max": 120, "strike_method": "delta", "strike_param": 0.6}}},
]
OPEN = [
    {"name": "x0", "continue_processing": False, "actions": {"a0": {"action_type": "close_option"}},
     "triggers": _trig(_leaf("profit_loss_percent", ">", 100.0), _leaf("entry_tag", "==", 13.0))},
    {"name": "x1", "continue_processing": False, "actions": {"a0": {"action_type": "close_option"}},
     "triggers": _trig(_leaf("days_opened", ">", 30.0), _leaf("entry_tag", "==", 12.0))},
    {"name": "x2", "continue_processing": False, "actions": {"a0": {"action_type": "close_option"}},
     "triggers": _trig(_leaf("days_to_expiry", "<=", 7.0))},
    {"name": "x3", "continue_processing": False, "actions": {"a0": {"action_type": "close_option"}},
     "triggers": _trig(_leaf("days_opened", ">", 30.0), _leaf("entry_tag", "==", 99.0))},
]


def test_members_in_priority_order_with_grouped_gates():
    s = build_style_summary(ENTER, OPEN, {"id": 12})
    assert s["header"] == {"id": 12}
    assert [(m["rank"], m["structure"], m["tag"]) for m in s["members"]] == [
        (1, "O_STRD", 12), (2, "O_LC", 13)]
    strd = s["members"][0]
    assert [e["precedence"] for e in strd["entries"]] == [1, 2]
    gates = strd["entries"][0]["gates"]
    assert gates["signal"] == ["current_rating_neutral", "has_no_position"]
    assert gates["iv"] == ["iv_rank < 40"]
    assert gates["market"] == ["underlying_trend_slope_50_atr14 > -0.15", "channel_pos_20 < 0.7"]
    assert s["members"][1]["entries"][0]["precedence"] == 3
    assert strd["entries"][1]["gates"]["signal"] == ["confidence <= 15"]


def test_action_params_readable():
    params = {p["label"]: p["value"] for p in build_style_summary(ENTER, OPEN)["members"][0]["entries"][1]["params"]}
    assert params["structure"] == "open_straddle"
    assert params["DTE"] == "45-65"
    assert params["strike"] == "percent_otm 0"
    assert params["sizing %"] == "8"
    assert params["entry cross"] == "0.8"
    assert params["min volume"] == "25"


def test_option_prefixed_param_keys_and_bare_tag():
    rule = {"name": "O_BF-entry", "continue_processing": False, "triggers": {},
            "actions": {"a0": {"action_type": "open_butterfly", "option_entry_tag": 4,
                               "option_dte_min": 30, "option_dte_max": 45,
                               "option_strike_method": "delta", "option_strike_param": 0.3}}}
    m = build_style_summary([rule], [])["members"][0]
    labels = {p["label"]: p["value"] for p in m["entries"][0]["params"]}
    assert (m["tag"], m["structure"]) == (4, "O_BF")
    assert labels["DTE"] == "30-45" and labels["strike"] == "delta 0.3"


def test_exits_scoped_unscoped_and_orphan():
    s = build_style_summary(ENTER, OPEN)
    by_tag = {m["tag"]: [e["name"] for e in m["exits"]] for m in s["members"]}
    assert by_tag == {12: ["x1"], 13: ["x0"]}
    assert [e["name"] for e in s["all_member_exits"]] == ["x2"]
    assert [e["name"] for e in s["orphan_exits"]] == ["x3"]
    x0 = s["members"][1]["exits"][0]
    assert x0["gates"] == ["profit_loss_percent > 100"]  # entry_tag leaf scopes, not shown as a gate
    assert x0["actions"] == ["close_option"]


def test_non_style_instance_is_none():
    plain = [{"name": "buy", "continue_processing": False, "triggers": {},
              "actions": {"a0": {"action_type": "buy"}}}]
    assert build_style_summary(plain, OPEN) is None
    assert build_style_summary([], []) is None


def test_unknown_leaf_shape_is_shown_not_dropped():
    rule = dict(ENTER[0], triggers={"cond_0": {"weird": 1}})
    s = build_style_summary([rule], [])
    assert s["members"][0]["entries"][0]["unreadable"] == ["cond_0: {'weird': 1}"]


def test_has_style_members_tells_a_style_expert_from_a_plain_one(monkeypatch):
    """The settings table offers the Style view button only when this is True: a stock expert's
    button used to open a page with nothing on it."""
    from ba2_trade_platform.core import style_view
    plain = [{"name": "buy", "continue_processing": False, "triggers": {},
              "actions": {"a0": {"action_type": "buy"}}}]
    rulesets = {1: ENTER, 2: plain, None: []}
    monkeypatch.setattr(style_view, "_rule_dicts", lambda rid: rulesets[rid])
    assert style_view.has_style_members(1) is True
    assert style_view.has_style_members(2) is False
    assert style_view.has_style_members(None) is False


def _store_ruleset(name, rules):
    from ba2_trade_platform.core.db import add_instance, get_db
    from ba2_trade_platform.core.models import EventAction, Ruleset, RulesetEventActionLink
    from ba2_trade_platform.core.types import ExpertEventRuleType
    rid = add_instance(Ruleset(name=name))
    for i, rule in enumerate(rules):
        eid = add_instance(EventAction(type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
                                       name=rule["name"], triggers=rule["triggers"],
                                       actions=rule["actions"],
                                       continue_processing=rule["continue_processing"]))
        with get_db() as session:      # the link table has no id column, add_instance needs one
            session.add(RulesetEventActionLink(ruleset_id=rid, eventaction_id=eid, order_index=i))
            session.commit()
    return rid


def test_ruleset_view_pairs_the_entry_ruleset_with_each_experts_exit_ruleset():
    """The Rulesets tab opens the view by ENTRY ruleset; the exits come from the open-positions
    ruleset of every expert that uses it, one view per distinct exit ruleset."""
    from ba2_trade_platform.core.style_view import load_ruleset_style_views
    from tests.factories import create_account_definition, create_expert_instance
    enter = _store_ruleset("style-enter", ENTER)
    exits = _store_ruleset("style-open", OPEN)
    acct = create_account_definition()
    a = create_expert_instance(acct.id, expert="DeterministicScorer", alias="opt-a",
                               enter_market_ruleset_id=enter, open_positions_ruleset_id=exits)
    b = create_expert_instance(acct.id, expert="DeterministicScorer", alias="opt-b",
                               enter_market_ruleset_id=enter, open_positions_ruleset_id=exits)
    data = load_ruleset_style_views(enter)
    assert data["ruleset"] == {"id": enter, "name": "style-enter"}
    assert len(data["views"]) == 1
    view = data["views"][0]
    assert view["exit_ruleset"] == {"id": exits, "name": "style-open"}
    assert [e["id"] for e in view["experts"]] == [a.id, b.id]
    assert [m["structure"] for m in view["summary"]["members"]] == ["O_STRD", "O_LC"]
    assert any(m["exits"] for m in view["summary"]["members"])


def test_ruleset_view_without_an_expert_shows_entries_only_and_plain_rulesets_show_nothing():
    from ba2_trade_platform.core.style_view import load_ruleset_style_views
    enter = _store_ruleset("style-enter-unused", ENTER)
    data = load_ruleset_style_views(enter)
    assert [v["exit_ruleset"] for v in data["views"]] == [None]
    assert data["views"][0]["experts"] == []
    assert len(data["views"][0]["summary"]["members"]) == 2
    plain = _store_ruleset("plain", [{"name": "buy", "continue_processing": False, "triggers": {},
                                      "actions": {"a0": {"action_type": "buy"}}}])
    assert load_ruleset_style_views(plain) is None
    assert load_ruleset_style_views(999999) is None


def test_ruleset_page_draws_members_exits_and_the_experts_using_it():
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    from ba2_trade_platform.ui.pages import style_view as page
    from tests.factories import create_account_definition, create_expert_instance
    enter = _store_ruleset("style-enter", ENTER)
    exits = _store_ruleset("style-open", OPEN)
    acct = create_account_definition()
    inst = create_expert_instance(acct.id, expert="DeterministicScorer", alias="opt-a",
                                  enter_market_ruleset_id=enter, open_positions_ruleset_id=exits)
    plain = _store_ruleset("plain", [{"name": "buy", "continue_processing": False, "triggers": {},
                                      "actions": {"a0": {"action_type": "buy"}}}])
    client = Client(nicegui_page('/test-style-ruleset'), request=None)
    try:
        with client:
            page.ruleset_content(enter)
            texts = [d.text for d in client.layout.descendants() if getattr(d, 'text', None)]
        joined = " | ".join(texts)
        assert f"Entry ruleset #{enter}: style-enter" in joined
        assert "O_STRD" in joined and "O_LC" in joined
        assert f"Exits from open-positions ruleset #{exits}: style-open" in joined
        assert f"#{inst.id} opt-a" in joined
    finally:
        Client.instances.pop(client.id, None)
    client = Client(nicegui_page('/test-style-ruleset-plain'), request=None)
    try:
        with client:
            page.ruleset_content(plain)
            texts = [d.text for d in client.layout.descendants() if getattr(d, 'text', None)]
        assert "Nothing to show" in texts
    finally:
        Client.instances.pop(client.id, None)

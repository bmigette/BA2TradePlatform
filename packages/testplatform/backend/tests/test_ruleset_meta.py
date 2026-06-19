from fastapi.testclient import TestClient
from app.main import app
client = TestClient(app)


def test_vocabulary_lists_flags_numerics_actions_refs():
    v = client.get("/api/ruleset/vocabulary").json()
    assert "bearish" in {f["value"] for f in v["flags"]}
    assert "profit_loss_percent" in {n["value"] for n in v["numerics"]}
    assert {"close", "sell", "adjust_take_profit", "adjust_stop_loss"}.issubset({a["value"] for a in v["actions"]})
    assert any(a["value"] == "buy_call" and a["is_option"] for a in v["actions"])
    assert any(a["value"] == "adjust_stop_loss" and a["needs_reference"] for a in v["actions"])
    assert "order_open_price" in v["reference_values"] and ">" in v["operators"]


def test_exit_presets_validate_against_model():
    from app.api.strategies import ExitCondition
    presets = client.get("/api/ruleset/exit-presets").json()["presets"]
    assert len(presets) >= 4
    for p in presets:
        ExitCondition(**p["rule"])   # must not raise


def test_open_positions_ruleset_503_without_live_db(monkeypatch):
    monkeypatch.delenv("BA2_LIVE_DB", raising=False)
    r = client.get("/api/experts/123/open-positions-ruleset")
    assert r.status_code == 503


def _seed_live_db(path):
    """Build a tiny stand-in 'live' sqlite with one expert + open_positions ruleset.

    Mirrors the ba2_common schema the endpoint reads (expertinstance / ruleset /
    eventaction / ruleset_eventaction_link), with EventAction.triggers/actions JSON in the
    exact live shape so the converter exercises a real numeric trigger + an adjust action.
    """
    import json
    import sqlite3

    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE expertinstance (
            id INTEGER PRIMARY KEY, account_id INTEGER, expert TEXT, enabled INTEGER,
            alias TEXT, user_description TEXT, virtual_equity_pct REAL,
            enter_market_ruleset_id INTEGER, open_positions_ruleset_id INTEGER
        );
        CREATE TABLE ruleset (
            id INTEGER PRIMARY KEY, name TEXT, description TEXT, type TEXT, subtype TEXT
        );
        CREATE TABLE eventaction (
            id INTEGER PRIMARY KEY, type TEXT, subtype TEXT, name TEXT,
            triggers JSON, actions JSON, extra_parameters JSON, continue_processing INTEGER
        );
        CREATE TABLE ruleset_eventaction_link (
            ruleset_id INTEGER, eventaction_id INTEGER, order_index INTEGER,
            PRIMARY KEY (ruleset_id, eventaction_id)
        );
        """
    )
    conn.execute(
        "INSERT INTO expertinstance (id, account_id, expert, enabled, virtual_equity_pct, "
        "enter_market_ruleset_id, open_positions_ruleset_id) VALUES (?,?,?,?,?,?,?)",
        (7, 1, "FMPEarningsDrift", 1, 100.0, 21, 42),
    )
    conn.execute(
        "INSERT INTO ruleset (id, name, description, type, subtype) VALUES (?,?,?,?,?)",
        (42, "live-open-positions", "live", "trading_recommendation_rule", "open_positions"),
    )
    conn.execute(
        "INSERT INTO ruleset (id, name, description, type, subtype) VALUES (?,?,?,?,?)",
        (21, "live-enter-market", "live", "trading_recommendation_rule", "enter_market"),
    )
    # enter_market rule: BUY when bullish (flag) AND confidence > 0.6 (numeric)
    conn.execute(
        "INSERT INTO eventaction (id, type, subtype, name, triggers, actions, "
        "extra_parameters, continue_processing) VALUES (?,?,?,?,?,?,?,?)",
        (
            200, "trading_recommendation_rule", "enter_market", "enter-long",
            json.dumps({
                "bullish": {"event_type": "bullish"},
                "c0": {"event_type": "confidence", "operator": ">", "value": 0.6},
            }),
            json.dumps({"buy": {"action_type": "buy"}}),
            "{}", 0,
        ),
    )
    conn.execute(
        "INSERT INTO ruleset_eventaction_link (ruleset_id, eventaction_id, order_index) "
        "VALUES (?,?,?)",
        (21, 200, 0),
    )
    # Rule 1: take profit at +10% -> close
    conn.execute(
        "INSERT INTO eventaction (id, type, subtype, name, triggers, actions, "
        "extra_parameters, continue_processing) VALUES (?,?,?,?,?,?,?,?)",
        (
            100, "trading_recommendation_rule", "open_positions", "tp",
            json.dumps({"c0": {"event_type": "profit_loss_percent", "operator": ">", "value": 10}}),
            json.dumps({"a": {"action_type": "close"}}),
            "{}", 0,
        ),
    )
    # Rule 2: trail stop -> adjust_stop_loss with reference + offset value
    conn.execute(
        "INSERT INTO eventaction (id, type, subtype, name, triggers, actions, "
        "extra_parameters, continue_processing) VALUES (?,?,?,?,?,?,?,?)",
        (
            101, "trading_recommendation_rule", "open_positions", "trail",
            json.dumps({"c0": {"event_type": "days_opened", "operator": ">=", "value": 5}}),
            json.dumps({"a": {"action_type": "adjust_stop_loss",
                              "reference_value": "current_price", "value": -3.0}}),
            "{}", 0,
        ),
    )
    conn.executemany(
        "INSERT INTO ruleset_eventaction_link (ruleset_id, eventaction_id, order_index) "
        "VALUES (?,?,?)",
        [(42, 100, 0), (42, 101, 1)],
    )
    conn.commit()
    conn.close()


def test_open_positions_ruleset_imports_from_live(monkeypatch, tmp_path):
    from app.api.strategies import ExitCondition

    db = tmp_path / "live.sqlite"
    _seed_live_db(db)
    monkeypatch.setenv("BA2_LIVE_DB", str(db))

    r = client.get("/api/experts/7/open-positions-ruleset")
    assert r.status_code == 200, r.text
    rules = r.json()["rules"]
    assert len(rules) == 2

    # Every returned rule must validate against the API model the UI loads.
    parsed = [ExitCondition(**rule) for rule in rules]

    tp = parsed[0]
    assert tp.action == "close"
    # numeric leaf preserved + marked optimizable with sensible default range
    leaf = tp.conditions.conditions[0]
    assert leaf.field == "profit_loss_percent" and leaf.value == 10
    assert leaf.optimize_enabled and leaf.value_min is not None and leaf.value_max is not None

    trail = parsed[1]
    assert trail.action == "adjust_stop_loss"
    assert trail.reference_value == "current_price"
    assert trail.action_value == -3.0
    assert trail.action_value_optimize  # adjust action_value is optimizable

    # whole-rule toggle is on so the optimizer can drop a rule
    assert tp.toggle_optimize and trail.toggle_optimize


def test_open_positions_ruleset_404_for_missing_expert(monkeypatch, tmp_path):
    db = tmp_path / "live.sqlite"
    _seed_live_db(db)
    monkeypatch.setenv("BA2_LIVE_DB", str(db))
    r = client.get("/api/experts/999/open-positions-ruleset")
    assert r.status_code == 404


def test_open_positions_ruleset_503_on_unreadable_db(monkeypatch, tmp_path):
    # Points at a path that does not exist -> graceful 503, never a 500.
    monkeypatch.setenv("BA2_LIVE_DB", str(tmp_path / "nope.sqlite"))
    r = client.get("/api/experts/7/open-positions-ruleset")
    assert r.status_code == 503


# --- enter_market importer (inverse of triggers_from_condition_tree) -----------------------

def test_enter_market_ruleset_503_without_live_db(monkeypatch):
    monkeypatch.delenv("BA2_LIVE_DB", raising=False)
    r = client.get("/api/experts/123/enter-market-ruleset")
    assert r.status_code == 503


def test_enter_market_ruleset_imports_buy_tree_from_live(monkeypatch, tmp_path):
    from app.api.strategies import ConditionBase

    db = tmp_path / "live.sqlite"
    _seed_live_db(db)
    monkeypatch.setenv("BA2_LIVE_DB", str(db))

    r = client.get("/api/experts/7/enter-market-ruleset")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sell_entry_conditions"] is None  # no SELL rule seeded

    buy = body["buy_entry_conditions"]
    assert buy is not None
    # Single BUY rule -> a single AND group (tree validates as a ConditionBase).
    tree = ConditionBase(**buy)
    assert tree.operator == "AND"
    leaves = {leaf.field: leaf for leaf in tree.conditions}

    # numeric leaf: confidence > 0.6, optimizable with a sensible range, comparison preserved
    num = leaves["confidence"]
    assert num.comparison == ">"
    assert num.value == 0.6
    assert num.optimize_enabled
    assert num.value_min is not None and num.value_max is not None and num.value_step is not None

    # flag leaf: bullish (no operator/value)
    flag = leaves["bullish"]
    assert flag.field_type == "flag"
    assert flag.comparison is None and flag.value is None


def test_enter_market_ruleset_404_for_missing_expert(monkeypatch, tmp_path):
    db = tmp_path / "live.sqlite"
    _seed_live_db(db)
    monkeypatch.setenv("BA2_LIVE_DB", str(db))
    r = client.get("/api/experts/999/enter-market-ruleset")
    assert r.status_code == 404


def test_enter_market_ruleset_503_on_unreadable_db(monkeypatch, tmp_path):
    monkeypatch.setenv("BA2_LIVE_DB", str(tmp_path / "nope.sqlite"))
    r = client.get("/api/experts/7/enter-market-ruleset")
    assert r.status_code == 503

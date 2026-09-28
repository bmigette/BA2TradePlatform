from ba2_common.export.expert_batch import (
    EXPORT_TYPE, EXPORT_VERSION, RULESET_SLOTS, build_batch_envelope, build_expert_batch_entry,
)


def test_entry_shape_and_key_order():
    e = build_expert_batch_entry(
        expert_type="FMPRating", alias=None, user_description="secret note", enabled=True,
        virtual_equity_pct=10.0, priority=1, account_id=3,
        ruleset_names={"enter_market_ruleset_name": "EM", "open_positions_ruleset_name": None},
        rulesets_export={"rulesets": []}, expert_settings={"a": 1}, symbol_settings={})
    assert list(e) == ["expert_type", "general", "enter_market_ruleset_name",
                       "open_positions_ruleset_name", "rulesets", "expert_settings",
                       "symbol_settings"]
    assert e["general"] == {"alias": "", "user_description": "secret note", "enabled": True,
                            "virtual_equity_pct": 10.0, "priority": 1, "account_id": 3}
    assert e["enter_market_ruleset_name"] == "EM" and e["open_positions_ruleset_name"] is None


def test_envelope_and_constants():
    env = build_batch_envelope([{"x": 1}], exported_at="2026-01-01T00:00:00")
    assert env == {"export_version": EXPORT_VERSION, "export_type": EXPORT_TYPE,
                   "export_timestamp": "2026-01-01T00:00:00", "experts": [{"x": 1}]}
    assert RULESET_SLOTS[0] == ("enter_market_ruleset_id", "enter_market_ruleset_name")

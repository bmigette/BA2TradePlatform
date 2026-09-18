"""Batch export/import of several experts, rules included.

This writes to the LIVE trading database and has no undo, so the properties pinned here are the
ones that decide whether an operator can trust it: the plan is honest about what will happen,
nothing is written before the plan is applied, a re-import updates in place rather than
duplicating, and an import can never start trading.
"""
import json

import pytest

from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.expert_batch_export_import import (
    EXPORT_TYPE, apply_batch_import, build_batch_export, parse_batch_payload, plan_batch_import,
)
from ba2_trade_platform.core.models import ExpertInstance, Ruleset
from ba2_trade_platform.core.types import (
    AnalysisUseCase, ExpertActionType, ExpertEventRuleType, ExpertEventType,
)
from ba2_trade_platform.core.utils import get_expert_instance_from_id
from tests.factories import (
    create_account_definition, create_event_action, create_expert_instance, create_ruleset,
    link_rule_to_ruleset,
)


def _ruleset_with_a_rule(name, subtype=AnalysisUseCase.ENTER_MARKET, event=None):
    rs = create_ruleset(name=name, type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
                        subtype=subtype)
    ea = create_event_action(
        name=f"{name} rule",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        triggers={"t0": {"event_type": (event or ExpertEventType.F_BULLISH).value}},
        actions={"a0": {"action_type": ExpertActionType.BUY.value}},
    )
    link_rule_to_ruleset(rs.id, ea.id, order_index=0)
    return rs


@pytest.fixture
def seeded():
    """One account, one expert with both ruleset slots filled and a couple of settings."""
    account = create_account_definition()
    enter = _ruleset_with_a_rule("batch enter_market")
    hold = _ruleset_with_a_rule("batch open_positions", subtype=AnalysisUseCase.OPEN_POSITIONS)
    inst = create_expert_instance(
        account_id=account.id, expert="FMPRating", enabled=True, virtual_equity_pct=42.0,
        alias="batch-alpha", user_description="the original", priority=7,
        enter_market_ruleset_id=enter.id, open_positions_ruleset_id=hold.id,
    )
    expert = get_expert_instance_from_id(inst.id)
    expert.save_settings({"some_setting": ("original", None)})
    return {"account": account, "instance": inst, "enter": enter, "hold": hold}


class TestExport:
    def test_the_export_carries_the_rules_not_just_their_names(self, seeded):
        """THE REASON THIS EXISTS: the per-expert export records ruleset NAMES only, so it can
        only be imported where those rulesets already exist."""
        payload = build_batch_export([seeded["instance"].id])
        entry = payload["experts"][0]

        assert payload["export_type"] == EXPORT_TYPE
        assert entry["enter_market_ruleset_name"] == "batch enter_market"
        assert entry["open_positions_ruleset_name"] == "batch open_positions"

        names = [r["name"] for r in entry["rulesets"]["rulesets"]]
        assert names == ["batch enter_market", "batch open_positions"]
        rules = entry["rulesets"]["rulesets"][0]["rules"]
        assert len(rules) == 1 and rules[0]["triggers"], "rule CONTENT must travel, not just a name"

    def test_general_and_settings_travel(self, seeded):
        entry = build_batch_export([seeded["instance"].id])["experts"][0]
        assert entry["general"]["alias"] == "batch-alpha"
        assert entry["general"]["virtual_equity_pct"] == 42.0
        assert entry["general"]["priority"] == 7
        assert entry["general"]["account_id"] == seeded["account"].id
        assert entry["expert_settings"]["some_setting"] == "original"

    def test_several_experts_in_one_file(self, seeded):
        other = create_expert_instance(account_id=seeded["account"].id, expert="FMPRating",
                                       alias="batch-beta")
        payload = build_batch_export([seeded["instance"].id, other.id])
        assert [e["general"]["alias"] for e in payload["experts"]] == ["batch-alpha", "batch-beta"]

    def test_an_expert_with_no_rulesets_exports_cleanly(self, seeded):
        bare = create_expert_instance(account_id=seeded["account"].id, expert="FMPRating",
                                      alias="batch-bare")
        entry = build_batch_export([bare.id])["experts"][0]
        assert entry["rulesets"] is None
        assert entry["enter_market_ruleset_name"] is None

    def test_an_unknown_id_is_refused(self):
        with pytest.raises(ValueError, match="not found"):
            build_batch_export([999999])


class TestPlanIsReadOnly:
    def test_planning_writes_nothing(self, seeded):
        """The whole point of the two-step: the operator sees the plan before anything moves."""
        payload = build_batch_export([seeded["instance"].id])
        payload["experts"][0]["general"]["alias"] = "batch-brand-new"
        payload["experts"][0]["rulesets"]["rulesets"][0]["name"] = "brand new ruleset"

        before_experts = len(get_all(ExpertInstance))
        before_rulesets = len(get_all(Ruleset))
        plan = plan_batch_import(payload)

        assert plan.write_count == 1 and len(plan.creates) == 1
        assert len(get_all(ExpertInstance)) == before_experts, "plan created an expert"
        assert len(get_all(Ruleset)) == before_rulesets, "plan created a ruleset"

    def test_a_known_alias_plans_an_update_an_unknown_one_a_create(self, seeded):
        payload = build_batch_export([seeded["instance"].id])
        payload["experts"].append(json.loads(json.dumps(payload["experts"][0])))
        payload["experts"][1]["general"]["alias"] = "batch-unseen"

        plan = plan_batch_import(payload)
        assert [(p.alias, p.action) for p in plan.experts] == [
            ("batch-alpha", "update"), ("batch-unseen", "create")]
        assert plan.updates[0].existing_id == seeded["instance"].id

    def test_an_alias_held_by_a_different_expert_type_is_skipped_not_overwritten(self, seeded):
        payload = build_batch_export([seeded["instance"].id])
        payload["experts"][0]["expert_type"] = "FMPEarningsDrift"
        plan = plan_batch_import(payload)
        assert plan.skips and "not a FMPEarningsDrift" in plan.skips[0].problem
        assert plan.write_count == 0

    def test_an_entry_without_an_alias_is_skipped(self, seeded):
        payload = build_batch_export([seeded["instance"].id])
        payload["experts"][0]["general"]["alias"] = ""
        plan = plan_batch_import(payload)
        assert plan.skips[0].problem == "entry has no alias to match on"

    def test_the_diff_counts_only_what_changes(self, seeded):
        payload = build_batch_export([seeded["instance"].id])
        assert plan_batch_import(payload).updates[0].settings_changed == 0
        payload["experts"][0]["expert_settings"]["some_setting"] = "different"
        assert plan_batch_import(payload).updates[0].settings_changed == 1


class TestApply:
    def test_round_trip_updates_in_place(self, seeded):
        """Re-importing the same file must not clone the expert or its rulesets."""
        payload = build_batch_export([seeded["instance"].id])
        before_experts = len(get_all(ExpertInstance))
        before_rulesets = len(get_all(Ruleset))

        apply_batch_import(plan_batch_import(payload))

        assert len(get_all(ExpertInstance)) == before_experts
        assert len(get_all(Ruleset)) == before_rulesets
        again = get_instance(ExpertInstance, seeded["instance"].id)
        assert again.enter_market_ruleset_id == seeded["enter"].id, "ruleset id must not move"
        assert again.open_positions_ruleset_id == seeded["hold"].id

    def test_settings_and_general_are_applied(self, seeded):
        payload = build_batch_export([seeded["instance"].id])
        payload["experts"][0]["expert_settings"]["some_setting"] = "changed"
        payload["experts"][0]["general"]["virtual_equity_pct"] = 11.0
        payload["experts"][0]["general"]["priority"] = 55

        apply_batch_import(plan_batch_import(payload))

        inst = get_instance(ExpertInstance, seeded["instance"].id)
        assert inst.virtual_equity_pct == 11.0
        assert inst.priority == 55
        assert get_expert_instance_from_id(inst.id).settings["some_setting"] == "changed"

    def test_an_import_never_enables_anything(self, seeded):
        """A restored config must not start trading on its own."""
        # A created expert starts disabled even though the file says enabled=True...
        payload = build_batch_export([seeded["instance"].id])
        assert payload["experts"][0]["general"]["enabled"] is True
        payload["experts"][0]["general"]["alias"] = "batch-created"
        apply_batch_import(plan_batch_import(payload))
        created = [i for i in get_all(ExpertInstance) if i.alias == "batch-created"][0]
        assert created.enabled is False

        # ...and an update leaves the live flag exactly as it was, either way.
        for state in (False, True):
            inst = get_instance(ExpertInstance, seeded["instance"].id)
            inst.enabled = state
            from ba2_trade_platform.core.db import update_instance
            update_instance(inst)
            payload2 = build_batch_export([seeded["instance"].id])
            payload2["experts"][0]["general"]["enabled"] = not state
            apply_batch_import(plan_batch_import(payload2))
            assert get_instance(ExpertInstance, seeded["instance"].id).enabled is state

    def test_the_rules_are_rebuilt_on_a_database_that_never_had_them(self, seeded):
        """The move this format exists for: carry a strategy to a machine with no such ruleset."""
        payload = build_batch_export([seeded["instance"].id])
        payload["experts"][0]["general"]["alias"] = "batch-elsewhere"
        for r in payload["experts"][0]["rulesets"]["rulesets"]:
            r["name"] = r["name"].replace("batch ", "carried ")
        payload["experts"][0]["enter_market_ruleset_name"] = "carried enter_market"
        payload["experts"][0]["open_positions_ruleset_name"] = "carried open_positions"

        apply_batch_import(plan_batch_import(payload))

        created = [i for i in get_all(ExpertInstance) if i.alias == "batch-elsewhere"][0]
        enter = get_instance(Ruleset, created.enter_market_ruleset_id)
        assert enter.name == "carried enter_market"
        assert created.open_positions_ruleset_id != created.enter_market_ruleset_id

    def test_a_missing_ruleset_with_no_rules_in_the_file_is_reported_not_invented(self, seeded):
        """An old single-expert export names a ruleset it does not carry."""
        payload = {"experts": [{
            "expert_type": "FMPRating",
            "general": {"alias": "batch-nameonly", "account_id": seeded["account"].id},
            "expert_settings": {},
            "enter_market_ruleset_name": "a ruleset this database never had",
        }]}
        messages = apply_batch_import(plan_batch_import(payload))
        created = [i for i in get_all(ExpertInstance) if i.alias == "batch-nameonly"][0]
        assert created.enter_market_ruleset_id is None
        assert any("left unset" in m for m in messages)

    def test_one_bad_entry_does_not_lose_the_others(self, seeded):
        payload = build_batch_export([seeded["instance"].id])
        good = json.loads(json.dumps(payload["experts"][0]))
        good["general"]["alias"] = "batch-good"
        payload["experts"] = [{"expert_type": "FMPRating",
                               "general": {"alias": "batch-bad", "account_id": 999999},
                               "expert_settings": {}}, good]
        apply_batch_import(plan_batch_import(payload))
        assert [i for i in get_all(ExpertInstance) if i.alias == "batch-good"]


class TestParse:
    def test_a_single_expert_export_is_accepted_and_wrapped(self):
        raw = json.dumps({"expert_type": "FMPRating", "general": {"alias": "x"},
                          "expert_settings": {}})
        payload = parse_batch_payload(raw)
        assert payload["export_type"] == EXPORT_TYPE
        assert len(payload["experts"]) == 1

    def test_bytes_are_accepted(self, seeded):
        raw = json.dumps(build_batch_export([seeded["instance"].id])).encode("utf-8")
        assert len(parse_batch_payload(raw)["experts"]) == 1

    @pytest.mark.parametrize("raw", ['{"rulesets": []}', '[]', '{"nothing": 1}'])
    def test_an_unrelated_file_is_refused_loudly(self, raw):
        with pytest.raises(ValueError):
            parse_batch_payload(raw)


def get_all(model):
    from ba2_trade_platform.core.db import get_all_instances
    return get_all_instances(model)


class TestSettingsSurviveTheRoundTripUnchanged:
    """An import REPLACES an expert's settings (reset_settings, then write the file's keys), so
    anything the export loses or re-types is silently corrupted rather than reported.

    These compare the expert's OWN resolved settings before and after, which is what every
    reader sees -- not the raw rows, which legitimately differ (an unset key has no row).
    """

    @staticmethod
    def _settings(instance_id):
        from ba2_trade_platform.core.utils import get_expert_instance_from_id
        expert = get_expert_instance_from_id(instance_id)
        expert._invalidate_settings_cache()
        return dict(expert.settings)

    def test_every_value_type_comes_back_identical(self, seeded):
        """str / float / int / bool / json, plus the untyped extras a deploy writes."""
        inst = seeded["instance"]
        expert = get_expert_instance_from_id(inst.id)
        expert.save_settings({
            "target_price_type": ("consensus", None),      # str
            "profit_ratio": (0.6180339887, None),          # float, non-round
            "min_analysts": (7, None),                     # int
            "enable_sell": (True, None),                   # bool True
            "enable_buy": (False, None),                   # bool False
            "price_target_window_days": (0, None),         # int zero -- not "unset"
            "enabled_instruments": ({"AAPL": {"enabled": True, "weight": 60.0}}, None),  # json
        })
        before = self._settings(inst.id)

        payload = build_batch_export([inst.id])
        apply_batch_import(plan_batch_import(payload))
        after = self._settings(inst.id)

        for key in ("target_price_type", "profit_ratio", "min_analysts", "enable_sell",
                    "enable_buy", "price_target_window_days", "enabled_instruments"):
            assert after[key] == before[key], (
                f"{key}: {before[key]!r} ({type(before[key]).__name__}) -> "
                f"{after[key]!r} ({type(after[key]).__name__})")
            assert type(after[key]) is type(before[key]), f"{key} changed type"

    def test_a_false_bool_is_not_lost_as_if_unset(self, seeded):
        """The classic: `if value:` drops False, and an expert silently starts BUYING again."""
        inst = seeded["instance"]
        get_expert_instance_from_id(inst.id).save_settings({"enable_buy": (False, None)})
        apply_batch_import(plan_batch_import(build_batch_export([inst.id])))
        assert self._settings(inst.id)["enable_buy"] is False

    def test_a_zero_is_not_lost_as_if_unset(self, seeded):
        inst = seeded["instance"]
        get_expert_instance_from_id(inst.id).save_settings({"min_analysts": (0, None)})
        apply_batch_import(plan_batch_import(build_batch_export([inst.id])))
        assert self._settings(inst.id)["min_analysts"] == 0

    def test_the_whole_resolved_settings_dict_is_unchanged(self, seeded):
        """The strongest form: nothing at all moves, across all ~100 declared settings."""
        inst = seeded["instance"]
        before = self._settings(inst.id)
        apply_batch_import(plan_batch_import(build_batch_export([inst.id])))
        after = self._settings(inst.id)
        moved = {k: (before.get(k), after.get(k))
                 for k in set(before) | set(after) if before.get(k) != after.get(k)}
        assert not moved, f"settings changed across an export/import round trip: {moved}"

    def test_a_second_round_trip_is_also_a_no_op(self, seeded):
        """Idempotent: re-importing the same file must not drift further each time."""
        inst = seeded["instance"]
        apply_batch_import(plan_batch_import(build_batch_export([inst.id])))
        once = self._settings(inst.id)
        apply_batch_import(plan_batch_import(build_batch_export([inst.id])))
        assert self._settings(inst.id) == once

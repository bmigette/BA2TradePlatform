"""structure_share is registered everywhere a condition must be (plan 2026-11-06, Task 7)."""
from ba2_common.core.TradeConditions import CONDITION_MAP, StructureShareCondition
from ba2_common.core.rule_builders import FIELD_EVENT, triggers_from_condition_tree
from ba2_common.core.rules_export_import import _abbr_field
from ba2_common.core.rules_documentation import get_event_type_documentation
from ba2_common.core.trigger_catalog import trigger_catalog, triggers_in_category
from ba2_common.core.types import ExpertEventType, is_numeric_event


def test_the_event_type_is_numeric_and_maps_to_its_class():
    et = ExpertEventType("structure_share")
    assert et is ExpertEventType.N_STRUCTURE_SHARE
    assert is_numeric_event(et.value)
    assert CONDITION_MAP[et] is StructureShareCondition
    assert FIELD_EVENT["structure_share"] is et


def test_a_leaf_becomes_a_trigger_and_is_not_dropped():
    tree = {"type": "AND", "conditions": [
        {"id": "o_cbs-structure_share", "field": "structure_share", "op": "<", "value": 40.0}]}
    (trigger,) = triggers_from_condition_tree(tree).values()
    assert trigger == {"event_type": "structure_share", "operator": "<", "value": 40.0}


def test_it_has_a_curated_abbreviation_a_catalog_row_and_documentation():
    assert _abbr_field("structure_share") == "structShare"
    assert "structure_share" in {e.value for e in trigger_catalog()}
    assert "structure_share" in {e.value for e in triggers_in_category("options")}
    assert "structure_share" in get_event_type_documentation()

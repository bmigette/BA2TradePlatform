# packages/common/tests/test_new_option_action_enums.py
from ba2_common.core.types import ExpertActionType, get_option_action_values, is_option_action

NEW = ["open_short_straddle", "open_short_strangle", "open_iron_condor",
       "open_jade_lizard", "open_call_butterfly", "open_put_ratio_spread"]


def test_new_enum_members_exist_and_detected():
    for v in NEW:
        assert ExpertActionType(v).value == v
        assert v in get_option_action_values()
        assert is_option_action(v)

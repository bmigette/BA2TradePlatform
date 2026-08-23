"""The live rule editor must be able to express every option param the action consumes.

WHY (2026-08-23 structure audit): ``min_volume`` and ``wing_width_pct`` were plumbed the
whole way -- ``rule_builders._OPTION_ACTION_PARAM_KEYS``,
``TradeActionEvaluator._OPTION_ENTRY_PARAM_KEYS``, the ``_OptionEntryAction`` ctor -- and had
NO widget in the rule editor, so no live rule could ever set them. Confirmed read-only
against the production DB: all 14 live option entry actions carry min_open_interest and
max_spread_pct, and NOT ONE carries either of those two. A live iron condor therefore ran on
``OpenIronCondorAction.DEFAULT_WING_PCT`` with nothing on screen saying so.

Layer-by-layer tests could not catch that: every layer was correct in isolation and the
PRODUCER was missing. So this test compares the producer's key set against the consumer's.
It reads source rather than driving NiceGUI (which needs a UI slot context); brittle to
reformatting, but the alternative is no coverage of the exact defect that occurred.
"""
import inspect
import re

import pytest

from ba2_common.core.TradeActionEvaluator import _OPTION_ENTRY_PARAM_KEYS
from ba2_common.core.types import ExpertActionType, get_wing_width_action_values


@pytest.fixture(scope="module")
def save_path_src():
    from ba2_trade_platform.ui.pages import settings
    src = inspect.getsource(settings)
    start = src.index("elif is_option_action(action_type):")
    end = src.index("actions_data[action_id] = action_config", start)
    return src[start:end]


def _persisted_keys(src):
    return set(re.findall(r"action_config\['([a-z_]+)'\]\s*=", src))


def test_the_editor_persists_wing_width_pct(save_path_src):
    """The four multi-leg structures read it; without a widget they silently used a constant."""
    assert "wing_width_pct" in _persisted_keys(save_path_src)


def test_the_editor_persists_every_param_it_is_expected_to(save_path_src):
    persisted = _persisted_keys(save_path_src)
    consumed = set(_OPTION_ENTRY_PARAM_KEYS)
    # min_volume is the ONE deliberate omission: AlpacaAccount builds its chain from the
    # option SNAPSHOT endpoint, whose payload has no bar, so OptionContract.volume is always
    # None on the live path and the gate could only ever raise. See the note at the save site.
    expected = consumed - {"min_volume"}
    assert expected <= persisted, f"live rules cannot set: {sorted(expected - persisted)}"
    assert "min_volume" not in persisted


def test_the_deliberate_omission_is_documented_where_someone_would_add_it(save_path_src):
    assert "min_volume" in save_path_src, (
        "the reason min_volume has no widget must live next to the other params, or the "
        "next person will 'fix' the gap and ship a field that can only error")


def test_wing_width_is_offered_for_exactly_the_actions_that_read_it():
    """Guard the decoy risk in the other direction: the set must track the builders."""
    from ba2_common.core import TradeActions as TA

    wing_types = set(get_wing_width_action_values())
    # every declared wing action really has a DEFAULT_WING_PCT-driven builder
    by_value = {
        ExpertActionType.OPEN_IRON_CONDOR.value: TA.OpenIronCondorAction,
        ExpertActionType.OPEN_JADE_LIZARD.value: TA.OpenJadeLizardAction,
        ExpertActionType.OPEN_CALL_BUTTERFLY.value: TA.OpenCallButterflyAction,
        ExpertActionType.OPEN_PUT_RATIO_SPREAD.value: TA.OpenPutRatioSpreadAction,
    }
    assert wing_types == set(by_value)
    for value, cls in by_value.items():
        assert hasattr(cls, "DEFAULT_WING_PCT")

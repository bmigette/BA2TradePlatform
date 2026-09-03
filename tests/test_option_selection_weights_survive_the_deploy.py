"""A GA-tuned selection weight must still be tuned once the genome is LIVE.

WHY THIS FILE EXISTS. The selection weights (``w_premium``/``w_iv``/``w_rvol``) are searched in
stage 2 and the winning genome is then DEPLOYED to a live ``ExpertInstance`` by
``tools/export_deploy_payload.py`` + ``tools/import_deploy_payload.py``. Between the genome and
the live pick sit five layers that each rename or rebuild the config:

    genome action (option_w_premium)
      -> rule_builders._option_action_config           (option_w_premium -> w_premium)
      -> rules_convert.trade_rules_to_live_export      (what import_deploy_payload calls)
      -> RulesImporter.import_multiple_rulesets        (writes EventAction.actions)
      -> TradeActionEvaluator._create_trade_action     (forwards to the ctor)
      -> _OptionEntryAction.selection_policy           (what the pick actually reads)

Every one of them is a place a weight can be dropped, and dropping one is SILENT: the rule still
saves, the action still builds, the entry still fires, and it simply picks a different contract
from the one the backtest picked. This project has already shipped exactly that defect once --
FactorRanker's GA-tuned factor weights were inert on the live path, so instances 26/27 collapsed
to a single portfolio at double size while every backtest said otherwise. A per-layer test could
not see it then and cannot see it now: each layer was correct in isolation.

So this file asserts the WHOLE chain on ONE genome, ending at the object the pick consults --
``action.selection_policy`` -- and not at any intermediate dict.

THE EDITOR IS PART OF THE DEPLOY PATH, which is the half that was actually broken. The live rule
editor rebuilds ``action_config`` KEY BY KEY from its widgets. Until the three weight widgets
existed (this commit), opening a freshly deployed rule and pressing Save rebuilt the config
without them -- so the deploy was correct and the first human edit silently reverted the genome
to the legacy selector, leaving a live rule that no longer matched the backtest that justified
deploying it. ``test_a_human_edit_of_a_deployed_rule_does_not_wipe_the_weights`` is that case.
"""
from types import SimpleNamespace

import pytest

from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator
from ba2_common.core.option_selection_policy import (
    SelectionPolicy,
    SelectionWeightOutOfBand,
    WIRED_WEIGHT_BANDS,
)
from ba2_common.core.rules_convert import trade_rules_to_live_export
from ba2_common.core.types import ExpertActionType
from ba2_trade_platform.core.rules_export_import import RulesExporter, RulesImporter

from tests.test_option_ui_param_reachability import _Editor

#: One tuned genome. Every value is INSIDE the launcher's sampled band and OFF its default, and
#: w_premium is NEGATIVE on purpose -- the sign fix is the whole point of a signed domain, and a
#: layer that coerced weights through ``abs`` or dropped falsy values would pass with +1.5.
TUNED = {"option_w_premium": -1.5, "option_w_iv": 0.5, "option_w_rvol": 1.0}
TUNED_POLICY = SelectionPolicy(w_premium=-1.5, w_iv=0.5, w_rvol=1.0)

ACTION = ExpertActionType.SELL_CASH_SECURED_PUT.value


def _genome_rules(**weights):
    """The entry_rules list a Backtest row carries, as ``_derive_export_payload`` hands it to
    ``import_deploy_payload``: TradeRule dicts whose actions carry the ``option_*`` gene names."""
    return [{
        "name": "stage-2 winner",
        "conditions": None,
        "actions": [{
            "action_type": ACTION,
            "option_strike_method": "delta",
            "option_strike_param": 0.30,
            "option_dte_min": 20,
            "option_dte_max": 45,
            **weights,
        }],
    }]


def _deploy(entry_rules):
    """Run the real deploy conversion + import, and return the action config as it was READ BACK
    OUT OF THE DATABASE -- not as it was handed in. A converter that builds the right dict and
    an importer that persists a different one is precisely the seam a shape assertion misses."""
    live_export = trade_rules_to_live_export(entry_rules, [], name="deploy-probe")
    ruleset_ids, _warnings = RulesImporter.import_multiple_rulesets(live_export)
    # Read back through the EXPORTER rather than by hand: it is the code the operator's own
    # "export this ruleset" button runs, so anything it cannot see is invisible to them too.
    (rule,) = RulesExporter.export_ruleset(ruleset_ids[0])["ruleset"]["rules"]
    (cfg,) = list(rule["actions"].values())
    return cfg


def _live_action(action_cfg):
    """The action the LIVE evaluator builds from that config.

    ``_create_trade_action`` is called for real (unbound instance -- it reads nothing off self
    but ``account``) rather than re-implementing its forwarding loop here: that loop is one of
    the layers under test, so a test that copied it would be asserting against itself."""
    evaluator = object.__new__(TradeActionEvaluator)
    evaluator.account = SimpleNamespace()
    action = evaluator._create_trade_action(
        action_type=ExpertActionType(action_cfg["action_type"]),
        action_config=action_cfg,
        instrument_name="AAPL",
        order_recommendation=SimpleNamespace(),
        existing_order=None,
        expert_recommendation=SimpleNamespace(),
    )
    assert action is not None, (
        "the evaluator refused to build the action at all; it swallows ctor exceptions and "
        "returns None, so this is a config the live path would silently skip")
    return action


# --------------------------------------------------------------------------- #
# the chain, end to end
# --------------------------------------------------------------------------- #
def test_a_tuned_genome_reaches_the_live_pick_with_its_weights_intact():
    """THE ONE THAT MATTERS. Not "the key is present somewhere" -- the object the selector
    actually ranks with, equal to the policy the genome describes."""
    action = _live_action(_deploy(_genome_rules(**TUNED)))
    assert action.selection_policy == TUNED_POLICY, (
        f"the deployed genome's weights did not survive to the pick: "
        f"{action.selection_policy}")
    assert not action.selection_policy.is_default, (
        "a policy that reports itself default reproduces the legacy selector exactly, so the "
        "tuned genome would pick the same contract as an untuned one")


def test_the_deployed_rule_row_really_carries_the_weights():
    """The intermediate the operator can inspect. If this passes and the test above fails, the
    loss is in the evaluator; if this fails, it is in the converter or the importer."""
    cfg = _deploy(_genome_rules(**TUNED))
    assert cfg["w_premium"] == -1.5
    assert cfg["w_iv"] == 0.5
    assert cfg["w_rvol"] == 1.0


def test_the_negative_weight_survives_as_a_negative_weight():
    """w_premium is SIGNED and the sign carries the meaning: +1.5 prefers the richest contract
    on the chain and -1.5 prefers the cheapest. A layer that dropped the sign would still show
    a tuned-looking policy while inverting the strategy."""
    action = _live_action(_deploy(_genome_rules(**TUNED)))
    assert action.selection_policy.w_premium == -1.5


def test_an_untuned_genome_deploys_onto_the_legacy_path_unchanged():
    """THE NO-OP PIN, through the whole deploy chain rather than at the ctor alone. All-zero
    weights must build NO policy at all -- ``None``, not a default-valued SelectionPolicy --
    because that is what keeps the selector on its legacy path and every existing backtest
    reproducible."""
    action = _live_action(_deploy(_genome_rules(
        option_w_premium=0.0, option_w_iv=0.0, option_w_rvol=0.0)))
    assert action.selection_policy is None


def test_a_genome_with_no_weights_at_all_deploys_onto_the_legacy_path():
    """Every option rule that predates the weights takes this branch."""
    cfg = _deploy(_genome_rules())
    assert "w_premium" not in cfg
    assert _live_action(cfg).selection_policy is None


# --------------------------------------------------------------------------- #
# the editor half: a human opening the deployed rule
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def settings_module():
    from ba2_trade_platform.ui.pages import settings

    return settings


@pytest.fixture
def nicegui_client():
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page

    client = Client(nicegui_page('/test-option-weight-deploy'), request=None)
    yield client
    client.remove_elements(client.elements.values())


def test_a_human_edit_of_a_deployed_rule_does_not_wipe_the_weights(
        settings_module, nicegui_client, monkeypatch):
    """DEPLOY, THEN EDIT. The editor rebuilds action_config key by key from its widgets, so a
    weight with no widget is not merely unsettable -- it is DELETED by the first save of a rule
    that already had it. The deployed config is loaded into the real editor, saved through the
    real ``_save_rule``, and the result is taken all the way back to a policy.

    Kills the missing-widget defect at the level that costs money: delete the three
    ``ui.number`` calls and the reloaded policy comes back ``None`` -- the legacy selector,
    under a rule the operator believes is the stage-2 winner."""
    deployed = _deploy(_genome_rules(**TUNED))

    with nicegui_client:
        editor = _Editor(settings_module, monkeypatch).add_row(
            deployed["action_type"],
            **{k: v for k, v in deployed.items() if k != "action_type"})
        resaved = editor.save()

    assert resaved["w_premium"] == -1.5, (
        f"a human opening the deployed rule and saving it dropped the tuned weights: "
        f"{sorted(resaved)}")
    assert resaved["w_iv"] == 0.5
    assert resaved["w_rvol"] == 1.0
    assert _live_action(resaved).selection_policy == TUNED_POLICY


def test_the_deployed_weights_are_shown_to_the_operator_not_just_stored(
        settings_module, nicegui_client, monkeypatch):
    """A value that round-trips through hidden state would pass the test above while the screen
    said 0.00 -- an operator reading the rule would be told the opposite of what it does."""
    deployed = _deploy(_genome_rules(**TUNED))
    with nicegui_client:
        editor = _Editor(settings_module, monkeypatch).add_row(
            deployed["action_type"],
            **{k: v for k, v in deployed.items() if k != "action_type"})
        assert editor.widget('w_premium_input').value == -1.5
        assert editor.widget('w_iv_input').value == 0.5
        assert editor.widget('w_rvol_input').value == 1.0


def test_the_editor_default_is_the_legacy_path_not_a_tuned_one(
        settings_module, nicegui_client, monkeypatch):
    """A fresh rule must deploy no preference at all. If the widgets defaulted to anything but
    0, every new live option rule would silently start ranking on weights nobody chose."""
    with nicegui_client:
        saved = _Editor(settings_module, monkeypatch).add_row(ACTION).save()
    assert saved["w_premium"] == 0.0
    assert saved["w_iv"] == 0.0
    assert saved["w_rvol"] == 0.0
    assert _live_action(saved).selection_policy is None


# --------------------------------------------------------------------------- #
# fail closed: a weight the search could never have produced
# --------------------------------------------------------------------------- #
def _try_save(editor):
    """Run ``_save_rule`` without the success assertions ``_Editor.save`` makes."""
    rule = SimpleNamespace(id=1, name=None, type=None, subtype=None, triggers=None,
                           actions=None, continue_processing=None)
    editor.tab._save_rule(rule)


def test_the_editor_refuses_a_weight_outside_the_searched_band(
        settings_module, nicegui_client, monkeypatch):
    """REFUSED, NOT CLAMPED. A live rule carrying w_premium=9 is a rule no backtest can
    reproduce, and clamping it to 2 would show the operator 9 while the picker ranked on 2.

    Kills the band check in ``_save_rule``: without it the rule saves and the divergence is
    invisible until someone compares live fills against the backtest."""
    with nicegui_client:
        editor = _Editor(settings_module, monkeypatch).add_row(ACTION)
        editor.widget('w_premium_input').value = 9.0
        _try_save(editor)
    assert editor.saved == [], "an out-of-band weight was written to the rule"
    assert editor.errors == [], "the refusal came out as a swallowed exception, not a message"


def test_the_editor_refuses_a_negative_relative_volume_weight(
        settings_module, nicegui_client, monkeypatch):
    """w_rvol is UNSIGNED by design: a negative value asks the picker to prefer the THINNEST
    contract on the chain, which is not a strategy, it is a fill failure. The GA cannot emit
    one (its band starts at 0.0), so the editor must not be the one producer that can."""
    with nicegui_client:
        editor = _Editor(settings_module, monkeypatch).add_row(ACTION)
        editor.widget('w_rvol_input').value = -1.0
        _try_save(editor)
    assert editor.saved == [], "a negative w_rvol was written to the rule"


def test_the_action_itself_refuses_an_out_of_band_weight():
    """THE SECOND RAIL, and it is not redundant: the editor is not the only producer. A rule
    hand-edited in the database, or a live export file assembled by anything but the editor,
    reaches this ctor without passing the widget check.

    Kills ``validate_wired_weights`` in ``_OptionEntryAction.__init__``: without it the action
    builds happily and ranks on a weight the search never explored."""
    from ba2_common.core.TradeActions import create_action

    with pytest.raises(SelectionWeightOutOfBand):
        create_action(ExpertActionType(ACTION), 'AAPL', SimpleNamespace(), SimpleNamespace(),
                      None, None, w_premium=9.0)


def test_the_action_refuses_a_nan_weight():
    """A NaN weight makes every comparison False, so the pick collapses to list order with
    nothing anywhere reporting a problem -- worse than an error, because it looks like a
    working policy."""
    from ba2_common.core.TradeActions import create_action

    with pytest.raises(SelectionWeightOutOfBand):
        create_action(ExpertActionType(ACTION), 'AAPL', SimpleNamespace(), SimpleNamespace(),
                      None, None, w_iv=float('nan'))


def test_the_bands_cover_exactly_the_weights_the_ga_emits():
    """The band table is the contract between the launcher's gene table and the live editor.
    A weight added to one and not the other is either an unsettable gene or an unsearchable
    field, and both look fine from their own side."""
    from ba2_common.core.TradeActionEvaluator import _OPTION_ENTRY_PARAM_KEYS

    assert set(WIRED_WEIGHT_BANDS) == {"w_premium", "w_iv", "w_rvol"}
    for name in WIRED_WEIGHT_BANDS:
        assert name in _OPTION_ENTRY_PARAM_KEYS, (
            f"{name} has a band but the evaluator will not forward it to the ctor")

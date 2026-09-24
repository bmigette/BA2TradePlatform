"""Trial-memo canonicalisation of an INACTIVE threshold (design 2026-09-15 section 5, Task 8).

A leaf whose mode decoded to ``off`` is removed from the tree, so its ``cond:<id>:value`` gene
describes nothing. Two genomes differing only there are ONE phenotype: they produce the same
orders, the same trades and the same fitness, and hashing them apart buys the GA a full trial
every time it wanders along that axis.

The two halves of the contract, and what each protects:

* WITH mode leaves: the memo key collapses the inactive dimension onto the template's authored
  anchor. Only the KEY -- the persisted genome and the config the trial runs are the raw decode,
  so nothing about a result changes, only how often an identical one is recomputed.
* WITHOUT mode leaves (every job that existed before the profiles): the key is bit-identical to
  what the pre-change code produced. Pinned against a literal digest, not against a re-derivation,
  so a refactor of the identity dict cannot quietly invalidate a running job's memo.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.services.strategy_optimization_handler import (  # noqa: E402
    canonical_trial_params,
    mode_anchor_index,
)
from app.services.trial_memo import trial_key  # noqa: E402
from ba2_common.core.rule_models import NUMERIC_MODE_CHOICES  # noqa: E402

_LAUNCHER = os.path.normpath(os.path.join(_ROOT, "..", "ba2test_launcher.py"))
_spec = importlib.util.spec_from_file_location("ba2test_launcher_anchor", _LAUNCHER)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

#: The identity dict shape the handler hashes, with a params dict that carries NO mode gene.
LEGACY_IDENTITY = {
    "engine": "daily", "model_id": None, "pred_dataset_id": None, "exec_dataset_id": None,
    "start": "2020-01-01", "end": "2025-12-31", "seed": 42,
    "params": {"cond:o_lc-iv_rank:value": 30.0, "model:min_confidence": 55.0},
}
#: ``trial_key(LEGACY_IDENTITY)`` -- the value the pre-change code produced for it. A PIN: a
#: changed digest here means every running job's memo (and every checkpoint that outlived a
#: restart) just stopped recognising its own trials.
LEGACY_KEY = "eef28a8d7e9c15633ade420a532bf81633d9c362bd25657273949f84c3816d10"


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", ("ohlcv-v1",))
    return mod._build_strategy("O_LC", "anchor-O_LC", "FMPRating")


@pytest.fixture
def plain():
    """A strategy with NO mode leaf at all -- which since 2026-09-19 means a NON-DIRECTIONAL
    structure, not just "no profile".

    O_LC used to qualify; it no longer does. Its direction gate became a ``rec_direction``
    numeric leaf with a mode gene (off/below/above), so every DIRECTIONAL option strategy now
    carries one mode leaf even under profile ``none``. O_STRD is the straddle: it bets on the
    SIZE of the move, keeps the ``current_rating_neutral`` FLAG leaf with its ON/OFF toggle
    (off/below/above cannot say ``== HOLD``), and so is genuinely mode-free. The pre-profiles
    key contract still has a strategy to be pinned against.
    """
    return mod._build_strategy("O_STRD", "anchor-O_STRD", "FMPRating")


@pytest.fixture
def directional():
    """The DIRECTIONAL option strategy under profile ``none``: one mode leaf, no threshold gene."""
    return mod._build_strategy("O_LC", "anchor-O_LC", "FMPRating")


def _key(anchors, params):
    return trial_key({**LEGACY_IDENTITY, "params": canonical_trial_params(anchors, params)})


# ----------------------------------------------------------------- runs without any mode gene
def test_a_run_without_mode_leaves_produces_exactly_todays_key(plain):
    anchors = mode_anchor_index(plain)
    assert anchors == {}
    params = LEGACY_IDENTITY["params"]
    assert canonical_trial_params(anchors, params) is params, "no copy, no rewrite, no cost"
    assert _key(anchors, params) == LEGACY_KEY


def test_the_direction_mode_leaf_does_not_move_the_key_either(directional):
    """THE 2026-09-19 QUESTION, answered in the one place it is decidable.

    Every directional option strategy now carries a mode leaf even with no profile, so
    ``mode_anchor_index`` is no longer empty for them and the "no anchors, no rewrite" guard
    above does not cover them. They are nonetheless UNAFFECTED, and for a structural reason
    rather than by luck: the direction leaf's threshold is PINNED (``optimize: False``), so the
    space emits no ``cond:o_lc-signal:value`` gene, and ``canonical_trial_params`` rewrites a
    threshold only when both the mode gene AND the value gene are in the genome. With nothing
    to canonicalise it returns the SAME dict object, so the digest is the raw one -- byte for
    byte what the pre-change code hashed for the same params.

    If the threshold ever became searchable, this test fails and the memo key for every option
    run moves with it.
    """
    anchors = mode_anchor_index(directional)
    assert anchors == {"o_lc-signal": (list(NUMERIC_MODE_CHOICES), 0.0)}
    # the mode gene is in the genome; the value gene does not exist
    params = {**LEGACY_IDENTITY["params"], "cond:o_lc-signal:mode": "above"}
    assert canonical_trial_params(anchors, params) is params, "no copy, no rewrite, no cost"
    # ...and the pinned legacy params (no signal gene at all) still hash to the pinned digest
    assert _key(anchors, LEGACY_IDENTITY["params"]) == LEGACY_KEY


def test_the_pinned_legacy_key_is_what_the_raw_identity_hashes_to():
    assert trial_key(LEGACY_IDENTITY) == LEGACY_KEY


# ----------------------------------------------------------------- runs with mode genes
def test_the_index_reads_the_authored_anchor_from_the_template(gated):
    anchors = mode_anchor_index(gated)
    # o_lc-signal joined the index on 2026-09-19 (the direction gate became a mode leaf). It is
    # listed here rather than filtered out because the index is meant to name EVERY mode leaf:
    # a leaf missing from it would silently stop being canonicalised if it ever gained a
    # threshold gene.
    assert set(anchors) == {"o_lc-signal", "o_lc-market-slope", "o_lc-market-adx",
                            "o_lc-market-rv"}
    assert anchors["o_lc-market-adx"] == (["off", "below", "above"], 25.0)
    # the direction leaf's anchor is the fixed point of the signed grade scale, not a threshold
    assert anchors["o_lc-signal"] == (list(NUMERIC_MODE_CHOICES), 0.0)


def test_two_genomes_differing_only_in_an_inactive_threshold_share_one_key(gated):
    anchors = mode_anchor_index(gated)
    base = {"cond:o_lc-market-adx:mode": "off", "cond:o_lc-market-adx:value": 10.0,
            "cond:o_lc-market-slope:mode": "above", "cond:o_lc-market-slope:value": 0.1}
    other = {**base, "cond:o_lc-market-adx:value": 40.0}
    assert _key(anchors, base) == _key(anchors, other)
    # ...and both equal the genome that already carries the anchor.
    assert _key(anchors, {**base, "cond:o_lc-market-adx:value": 25.0}) == _key(anchors, base)


def test_an_active_threshold_is_never_canonicalised(gated):
    anchors = mode_anchor_index(gated)
    below_10 = {"cond:o_lc-market-adx:mode": "below", "cond:o_lc-market-adx:value": 10.0}
    below_40 = {"cond:o_lc-market-adx:mode": "below", "cond:o_lc-market-adx:value": 40.0}
    assert _key(anchors, below_10) != _key(anchors, below_40)
    # An ACTIVE leaf at the anchor value is not the same genome as an INACTIVE one.
    assert _key(anchors, {"cond:o_lc-market-adx:mode": "below",
                          "cond:o_lc-market-adx:value": 25.0}) != _key(
        anchors, {"cond:o_lc-market-adx:mode": "off", "cond:o_lc-market-adx:value": 25.0})


def test_the_mode_gene_may_arrive_as_a_choice_index(gated):
    """A checkpoint, a persisted genome or a hand-built dict carries the INDEX; the handler and
    the decoder must read it the same way (both go through ``strategy_param_space.mode_token``)."""
    anchors = mode_anchor_index(gated)
    by_token = {"cond:o_lc-market-adx:mode": "off", "cond:o_lc-market-adx:value": 40.0}
    by_index = {"cond:o_lc-market-adx:mode": 0, "cond:o_lc-market-adx:value": 40.0}
    assert canonical_trial_params(anchors, by_token)["cond:o_lc-market-adx:value"] == 25.0
    assert canonical_trial_params(anchors, by_index)["cond:o_lc-market-adx:value"] == 25.0


def test_the_raw_genome_is_never_mutated(gated):
    anchors = mode_anchor_index(gated)
    raw = {"cond:o_lc-market-adx:mode": "off", "cond:o_lc-market-adx:value": 40.0}
    canonical = canonical_trial_params(anchors, raw)
    assert raw["cond:o_lc-market-adx:value"] == 40.0, "the persisted provenance must survive"
    assert canonical is not raw and canonical["cond:o_lc-market-adx:value"] == 25.0


def test_an_uninterpretable_mode_gene_raises_rather_than_being_ignored(gated):
    anchors = mode_anchor_index(gated)
    with pytest.raises(ValueError, match="neither a choice token nor an index"):
        canonical_trial_params(anchors, {"cond:o_lc-market-adx:mode": 7.5,
                                         "cond:o_lc-market-adx:value": 40.0})


def test_a_mode_leaf_used_as_an_offset_base_is_refused_at_index_build():
    """A leaf the optimizer can switch OFF must not be another leaf's ruler.

    ``_apply_to_tree`` resolves a ``value_offset_from`` base from the GENE MAP on purpose, so a
    dropped base still anchors its dependant -- which means the base's threshold is still live
    when its own mode decoded to ``off``. Canonicalising it to the anchor would fold two
    genuinely different phenotypes onto one key and hand the second one the first one's fitness,
    so the template is refused once, at index build, naming both leaves.
    """
    from types import SimpleNamespace

    base = {"id": "o_lc-market-adx", "field": "underlying_adx_14", "op": "<", "value": 25.0,
            "optimize": True, "value_min": 10.0, "value_max": 40.0, "value_step": 5.0,
            "mode_optimize": True, "mode_choices": ["off", "below", "above"]}
    dependant = {"id": "o_lc-adx-band", "field": "underlying_adx_14", "op": "<", "value": 30.0,
                 "optimize": True, "value_offset_from": "o_lc-market-adx",
                 "value_min": 1.0, "value_max": 10.0, "value_step": 1.0}
    strategy = SimpleNamespace(
        entry_rules=[{"id": "e", "conditions": {"id": "root", "operator": "AND",
                                                "conditions": [base, dependant]},
                      "actions": [{"action_type": "buy"}]}],
        exit_rules=[])
    with pytest.raises(ValueError, match="value_offset_from base"):
        mode_anchor_index(strategy)
    # The message has to name BOTH leaves: the base is where the mode gene is, the dependant is
    # what has to change.
    try:
        mode_anchor_index(strategy)
    except ValueError as e:
        assert "o_lc-market-adx" in str(e) and "o_lc-adx-band" in str(e)


def test_an_offset_base_without_a_mode_gene_is_still_fine(gated):
    """The refusal is about MODE leaves only: ordinary offset chains are untouched."""
    from types import SimpleNamespace

    base = {"id": "plain-base", "field": "iv_rank", "op": "<", "value": 30.0, "optimize": True,
            "value_min": 10.0, "value_max": 60.0, "value_step": 5.0}
    dependant = {"id": "plain-band", "field": "iv_rank", "op": "<", "value": 35.0,
                 "optimize": True, "value_offset_from": "plain-base",
                 "value_min": 1.0, "value_max": 10.0, "value_step": 1.0}
    strategy = SimpleNamespace(
        entry_rules=[{"id": "e", "conditions": {"id": "root", "operator": "AND",
                                                "conditions": [base, dependant]},
                      "actions": [{"action_type": "buy"}]}],
        exit_rules=[])
    assert mode_anchor_index(strategy) == {}


def test_a_leaf_with_no_value_gene_is_left_alone(gated):
    """A categorical leaf has a mode gene and NO threshold: there is nothing to canonicalise."""
    anchors = dict(mode_anchor_index(gated))
    anchors["o_x-market-structure-state"] = (["off", "bull", "bear"], None)
    params = {"cond:o_x-market-structure-state:mode": "off"}
    assert canonical_trial_params(anchors, params) is params

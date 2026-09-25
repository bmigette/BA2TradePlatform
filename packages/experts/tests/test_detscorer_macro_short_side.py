"""macro_short_side: direction-aware regime multiplier (plan 2026-09-24 Task 10).

In macro_mode 'multiply' the final score is scaled by exposure_multiplier(regime).
That multiplier was designed to cut LONG exposure in a bad regime, but applied to
both signs it also shrinks NEGATIVE scores -- so SELL conviction is muted exactly in
bear regimes (measured 2026-09-24: bars with final < -0.4 in 2022 fell from 7.7% to
0.6% at default weights). "mirror" scales a negative score by m(-regime) instead.

The DEFAULT ("same") must reproduce every existing result bit for bit: this expert
runs LIVE and every stored backtest was scored under it. _GOLDEN below was generated
from combine.final_score BEFORE the setting existed (float.hex, so -0.0 vs 0.0 and the
last ulp are both pinned). Columns: technical, regime, regime_n_inputs, final, multiplier.
"""
import math

import pytest

from ba2_experts.DeterministicScorer import DeterministicScorer
from ba2_experts.DeterministicScorer import combine as C
from ba2_experts.DeterministicScorer.macro import (DEF_HARD_RISKOFF, DEF_M_FLOOR,
                                                   exposure_multiplier)

_GOLDEN = [
    (-0.9, None, None, '-0x1.cf6f9786df577p-1', '0x1.0000000000000p+0'),
    (-0.9, None, 1, '-0x1.cf6f9786df577p-1', '0x1.0000000000000p+0'),
    (-0.9, None, 3, '-0x1.cf6f9786df577p-1', '0x1.0000000000000p+0'),
    (-0.9, -1.0, None, '-0x0.0p+0', '0x0.0p+0'),
    (-0.9, -1.0, 1, '-0x1.cf6f9786df577p-3', '0x1.0000000000000p-2'),
    (-0.9, -1.0, 3, '-0x0.0p+0', '0x0.0p+0'),
    (-0.9, -0.8, None, '-0x0.0p+0', '0x0.0p+0'),
    (-0.9, -0.8, 1, '-0x1.2d3bbc17aac59p-2', '0x1.4ccccccccccccp-2'),
    (-0.9, -0.8, 3, '-0x0.0p+0', '0x0.0p+0'),
    (-0.9, -0.75, None, '-0x1.3e9cb82cb98c2p-2', '0x1.6000000000000p-2'),
    (-0.9, -0.75, 1, '-0x1.3e9cb82cb98c2p-2', '0x1.6000000000000p-2'),
    (-0.9, -0.75, 3, '-0x1.3e9cb82cb98c2p-2', '0x1.6000000000000p-2'),
    (-0.9, -0.4, None, '-0x1.b8439cc020f97p-2', '0x1.e666666666666p-2'),
    (-0.9, -0.4, 1, '-0x1.b8439cc020f97p-2', '0x1.e666666666666p-2'),
    (-0.9, -0.4, 3, '-0x1.b8439cc020f97p-2', '0x1.e666666666666p-2'),
    (-0.9, 0.0, None, '-0x1.21a5beb44b96ap-1', '0x1.4000000000000p-1'),
    (-0.9, 0.0, 1, '-0x1.21a5beb44b96ap-1', '0x1.4000000000000p-1'),
    (-0.9, 0.0, 3, '-0x1.21a5beb44b96ap-1', '0x1.4000000000000p-1'),
    (-0.9, 0.3, None, '-0x1.55c8b2f377ea2p-1', '0x1.799999999999ap-1'),
    (-0.9, 0.3, 1, '-0x1.55c8b2f377ea2p-1', '0x1.799999999999ap-1'),
    (-0.9, 0.3, 3, '-0x1.55c8b2f377ea2p-1', '0x1.799999999999ap-1'),
    (-0.9, 0.75, None, '-0x1.a3fd21523a674p-1', '0x1.d000000000000p-1'),
    (-0.9, 0.75, 1, '-0x1.a3fd21523a674p-1', '0x1.d000000000000p-1'),
    (-0.9, 0.75, 3, '-0x1.a3fd21523a674p-1', '0x1.d000000000000p-1'),
    (-0.9, 0.8, None, '-0x1.acad9f5cc1ca8p-1', '0x1.d99999999999ap-1'),
    (-0.9, 0.8, 1, '-0x1.acad9f5cc1ca8p-1', '0x1.d99999999999ap-1'),
    (-0.9, 0.8, 3, '-0x1.acad9f5cc1ca8p-1', '0x1.d99999999999ap-1'),
    (-0.9, 1.0, None, '-0x1.cf6f9786df577p-1', '0x1.0000000000000p+0'),
    (-0.9, 1.0, 1, '-0x1.cf6f9786df577p-1', '0x1.0000000000000p+0'),
    (-0.9, 1.0, 3, '-0x1.cf6f9786df577p-1', '0x1.0000000000000p+0'),
    (-0.35, None, None, '-0x1.0cd7cd785c770p-1', '0x1.0000000000000p+0'),
    (-0.35, None, 1, '-0x1.0cd7cd785c770p-1', '0x1.0000000000000p+0'),
    (-0.35, None, 3, '-0x1.0cd7cd785c770p-1', '0x1.0000000000000p+0'),
    (-0.35, -1.0, None, '-0x0.0p+0', '0x0.0p+0'),
    (-0.35, -1.0, 1, '-0x1.0cd7cd785c770p-3', '0x1.0000000000000p-2'),
    (-0.35, -1.0, 3, '-0x0.0p+0', '0x0.0p+0'),
    (-0.35, -0.8, None, '-0x0.0p+0', '0x0.0p+0'),
    (-0.35, -0.8, 1, '-0x1.5d7ef182de9aap-3', '0x1.4ccccccccccccp-2'),
    (-0.35, -0.8, 3, '-0x0.0p+0', '0x0.0p+0'),
    (-0.35, -0.75, None, '-0x1.71a8ba857f23ap-3', '0x1.6000000000000p-2'),
    (-0.35, -0.75, 1, '-0x1.71a8ba857f23ap-3', '0x1.6000000000000p-2'),
    (-0.35, -0.75, 3, '-0x1.71a8ba857f23ap-3', '0x1.6000000000000p-2'),
    (-0.35, -0.4, None, '-0x1.fecd3997e2e21p-3', '0x1.e666666666666p-2'),
    (-0.35, -0.4, 1, '-0x1.fecd3997e2e21p-3', '0x1.e666666666666p-2'),
    (-0.35, -0.4, 3, '-0x1.fecd3997e2e21p-3', '0x1.e666666666666p-2'),
    (-0.35, 0.0, None, '-0x1.500dc0d67394cp-2', '0x1.4000000000000p-1'),
    (-0.35, 0.0, 1, '-0x1.500dc0d67394cp-2', '0x1.4000000000000p-1'),
    (-0.35, 0.0, 3, '-0x1.500dc0d67394cp-2', '0x1.4000000000000p-1'),
    (-0.35, 0.3, None, '-0x1.8c8b1bde552f9p-2', '0x1.799999999999ap-1'),
    (-0.35, 0.3, 1, '-0x1.8c8b1bde552f9p-2', '0x1.799999999999ap-1'),
    (-0.35, 0.3, 3, '-0x1.8c8b1bde552f9p-2', '0x1.799999999999ap-1'),
    (-0.35, 0.75, None, '-0x1.e747246a2797bp-2', '0x1.d000000000000p-1'),
    (-0.35, 0.75, 1, '-0x1.e747246a2797bp-2', '0x1.d000000000000p-1'),
    (-0.35, 0.75, 3, '-0x1.e747246a2797bp-2', '0x1.d000000000000p-1'),
    (-0.35, 0.8, None, '-0x1.f15c08eb77dc3p-2', '0x1.d99999999999ap-1'),
    (-0.35, 0.8, 1, '-0x1.f15c08eb77dc3p-2', '0x1.d99999999999ap-1'),
    (-0.35, 0.8, 3, '-0x1.f15c08eb77dc3p-2', '0x1.d99999999999ap-1'),
    (-0.35, 1.0, None, '-0x1.0cd7cd785c770p-1', '0x1.0000000000000p+0'),
    (-0.35, 1.0, 1, '-0x1.0cd7cd785c770p-1', '0x1.0000000000000p+0'),
    (-0.35, 1.0, 3, '-0x1.0cd7cd785c770p-1', '0x1.0000000000000p+0'),
    (0.0, None, None, '0x0.0p+0', '0x1.0000000000000p+0'),
    (0.0, None, 1, '0x0.0p+0', '0x1.0000000000000p+0'),
    (0.0, None, 3, '0x0.0p+0', '0x1.0000000000000p+0'),
    (0.0, -1.0, None, '0x0.0p+0', '0x0.0p+0'),
    (0.0, -1.0, 1, '0x0.0p+0', '0x1.0000000000000p-2'),
    (0.0, -1.0, 3, '0x0.0p+0', '0x0.0p+0'),
    (0.0, -0.8, None, '0x0.0p+0', '0x0.0p+0'),
    (0.0, -0.8, 1, '0x0.0p+0', '0x1.4ccccccccccccp-2'),
    (0.0, -0.8, 3, '0x0.0p+0', '0x0.0p+0'),
    (0.0, -0.75, None, '0x0.0p+0', '0x1.6000000000000p-2'),
    (0.0, -0.75, 1, '0x0.0p+0', '0x1.6000000000000p-2'),
    (0.0, -0.75, 3, '0x0.0p+0', '0x1.6000000000000p-2'),
    (0.0, -0.4, None, '0x0.0p+0', '0x1.e666666666666p-2'),
    (0.0, -0.4, 1, '0x0.0p+0', '0x1.e666666666666p-2'),
    (0.0, -0.4, 3, '0x0.0p+0', '0x1.e666666666666p-2'),
    (0.0, 0.0, None, '0x0.0p+0', '0x1.4000000000000p-1'),
    (0.0, 0.0, 1, '0x0.0p+0', '0x1.4000000000000p-1'),
    (0.0, 0.0, 3, '0x0.0p+0', '0x1.4000000000000p-1'),
    (0.0, 0.3, None, '0x0.0p+0', '0x1.799999999999ap-1'),
    (0.0, 0.3, 1, '0x0.0p+0', '0x1.799999999999ap-1'),
    (0.0, 0.3, 3, '0x0.0p+0', '0x1.799999999999ap-1'),
    (0.0, 0.75, None, '0x0.0p+0', '0x1.d000000000000p-1'),
    (0.0, 0.75, 1, '0x0.0p+0', '0x1.d000000000000p-1'),
    (0.0, 0.75, 3, '0x0.0p+0', '0x1.d000000000000p-1'),
    (0.0, 0.8, None, '0x0.0p+0', '0x1.d99999999999ap-1'),
    (0.0, 0.8, 1, '0x0.0p+0', '0x1.d99999999999ap-1'),
    (0.0, 0.8, 3, '0x0.0p+0', '0x1.d99999999999ap-1'),
    (0.0, 1.0, None, '0x0.0p+0', '0x1.0000000000000p+0'),
    (0.0, 1.0, 1, '0x0.0p+0', '0x1.0000000000000p+0'),
    (0.0, 1.0, 3, '0x0.0p+0', '0x1.0000000000000p+0'),
    (0.25, None, None, '0x1.9393d160096c8p-2', '0x1.0000000000000p+0'),
    (0.25, None, 1, '0x1.9393d160096c8p-2', '0x1.0000000000000p+0'),
    (0.25, None, 3, '0x1.9393d160096c8p-2', '0x1.0000000000000p+0'),
    (0.25, -1.0, None, '0x0.0p+0', '0x0.0p+0'),
    (0.25, -1.0, 1, '0x1.9393d160096c8p-4', '0x1.0000000000000p-2'),
    (0.25, -1.0, 3, '0x0.0p+0', '0x0.0p+0'),
    (0.25, -0.8, None, '0x0.0p+0', '0x0.0p+0'),
    (0.25, -0.8, 1, '0x1.0653481806201p-3', '0x1.4ccccccccccccp-2'),
    (0.25, -0.8, 3, '0x0.0p+0', '0x0.0p+0'),
    (0.25, -0.75, None, '0x1.15759ff2067aap-3', '0x1.6000000000000p-2'),
    (0.25, -0.75, 1, '0x1.15759ff2067aap-3', '0x1.6000000000000p-2'),
    (0.25, -0.75, 3, '0x1.15759ff2067aap-3', '0x1.6000000000000p-2'),
    (0.25, -0.4, None, '0x1.7f6606e808f3ep-3', '0x1.e666666666666p-2'),
    (0.25, -0.4, 1, '0x1.7f6606e808f3ep-3', '0x1.e666666666666p-2'),
    (0.25, -0.4, 3, '0x1.7f6606e808f3ep-3', '0x1.e666666666666p-2'),
    (0.25, 0.0, None, '0x1.f878c5b80bc7ap-3', '0x1.4000000000000p-1'),
    (0.25, 0.0, 1, '0x1.f878c5b80bc7ap-3', '0x1.4000000000000p-1'),
    (0.25, 0.0, 3, '0x1.f878c5b80bc7ap-3', '0x1.4000000000000p-1'),
    (0.25, 0.3, None, '0x1.29a36a6a06f34p-2', '0x1.799999999999ap-1'),
    (0.25, 0.3, 1, '0x1.29a36a6a06f34p-2', '0x1.799999999999ap-1'),
    (0.25, 0.3, 3, '0x1.29a36a6a06f34p-2', '0x1.799999999999ap-1'),
    (0.25, 0.75, None, '0x1.6dbdf5bf088a5p-2', '0x1.d000000000000p-1'),
    (0.25, 0.75, 1, '0x1.6dbdf5bf088a5p-2', '0x1.d000000000000p-1'),
    (0.25, 0.75, 3, '0x1.6dbdf5bf088a5p-2', '0x1.d000000000000p-1'),
    (0.25, 0.8, None, '0x1.754f21ac08b79p-2', '0x1.d99999999999ap-1'),
    (0.25, 0.8, 1, '0x1.754f21ac08b79p-2', '0x1.d99999999999ap-1'),
    (0.25, 0.8, 3, '0x1.754f21ac08b79p-2', '0x1.d99999999999ap-1'),
    (0.25, 1.0, None, '0x1.9393d160096c8p-2', '0x1.0000000000000p+0'),
    (0.25, 1.0, 1, '0x1.9393d160096c8p-2', '0x1.0000000000000p+0'),
    (0.25, 1.0, 3, '0x1.9393d160096c8p-2', '0x1.0000000000000p+0'),
    (0.8, None, None, '0x1.bd78b8dd605a8p-1', '0x1.0000000000000p+0'),
    (0.8, None, 1, '0x1.bd78b8dd605a8p-1', '0x1.0000000000000p+0'),
    (0.8, None, 3, '0x1.bd78b8dd605a8p-1', '0x1.0000000000000p+0'),
    (0.8, -1.0, None, '0x0.0p+0', '0x0.0p+0'),
    (0.8, -1.0, 1, '0x1.bd78b8dd605a8p-3', '0x1.0000000000000p-2'),
    (0.8, -1.0, 3, '0x0.0p+0', '0x0.0p+0'),
    (0.8, -0.8, None, '0x0.0p+0', '0x0.0p+0'),
    (0.8, -0.8, 1, '0x1.218e78297ea13p-2', '0x1.4ccccccccccccp-2'),
    (0.8, -0.8, 3, '0x0.0p+0', '0x0.0p+0'),
    (0.8, -0.75, None, '0x1.3242ff18323e4p-2', '0x1.6000000000000p-2'),
    (0.8, -0.75, 1, '0x1.3242ff18323e4p-2', '0x1.6000000000000p-2'),
    (0.8, -0.75, 3, '0x1.3242ff18323e4p-2', '0x1.6000000000000p-2'),
    (0.8, -0.4, None, '0x1.a732af9f1b892p-2', '0x1.e666666666666p-2'),
    (0.8, -0.4, 1, '0x1.a732af9f1b892p-2', '0x1.e666666666666p-2'),
    (0.8, -0.4, 3, '0x1.a732af9f1b892p-2', '0x1.e666666666666p-2'),
    (0.8, 0.0, None, '0x1.166b738a5c389p-1', '0x1.4000000000000p-1'),
    (0.8, 0.0, 1, '0x1.166b738a5c389p-1', '0x1.4000000000000p-1'),
    (0.8, 0.0, 3, '0x1.166b738a5c389p-1', '0x1.4000000000000p-1'),
    (0.8, 0.3, None, '0x1.48890856770f9p-1', '0x1.799999999999ap-1'),
    (0.8, 0.3, 1, '0x1.48890856770f9p-1', '0x1.799999999999ap-1'),
    (0.8, 0.3, 3, '0x1.48890856770f9p-1', '0x1.799999999999ap-1'),
    (0.8, 0.75, None, '0x1.93b567889f520p-1', '0x1.d000000000000p-1'),
    (0.8, 0.75, 1, '0x1.93b567889f520p-1', '0x1.d000000000000p-1'),
    (0.8, 0.75, 3, '0x1.93b567889f520p-1', '0x1.d000000000000p-1'),
    (0.8, 0.8, None, '0x1.9c0faafff9209p-1', '0x1.d99999999999ap-1'),
    (0.8, 0.8, 1, '0x1.9c0faafff9209p-1', '0x1.d99999999999ap-1'),
    (0.8, 0.8, 3, '0x1.9c0faafff9209p-1', '0x1.d99999999999ap-1'),
    (0.8, 1.0, None, '0x1.bd78b8dd605a8p-1', '0x1.0000000000000p+0'),
    (0.8, 1.0, 1, '0x1.bd78b8dd605a8p-1', '0x1.0000000000000p+0'),
    (0.8, 1.0, 3, '0x1.bd78b8dd605a8p-1', '0x1.0000000000000p+0'),
]

_TECH_ONLY = {"w_technical": 1.0, "w_fundamental": 0.0, "macro_mode": "multiply"}


def _fs(t, r, n, **extra):
    return C.final_score(technical=t, fundamental=None, analyst=None, regime=r,
                         s={**_TECH_ONLY, **extra}, regime_n_inputs=n)


# ---------------------------------------------------------------- backward compatibility
@pytest.mark.parametrize("extra", [{}, {"macro_short_side": "same"}],
                         ids=["absent", "same"])
def test_default_is_bit_identical_to_pre_change_output(extra):
    for t, r, n, final_hex, mult_hex in _GOLDEN:
        res = _fs(t, r, n, **extra)
        assert res["final"].hex() == final_hex, (t, r, n)
        assert float(res["exposure_multiplier"]).hex() == mult_hex, (t, r, n)


def test_result_keys_unchanged():
    """The combination dict is persisted in raw_outputs: the setting must not add keys."""
    assert set(_fs(-0.3, -0.5, 3, macro_short_side="mirror")) == set(_fs(-0.3, -0.5, 3))


def test_setting_default_and_values_declared():
    d = DeterministicScorer.get_settings_definitions()["macro_short_side"]
    assert d["default"] == "same"
    assert d["valid_values"] == ["same", "mirror"]
    assert d["description"]


def test_setting_is_resolved_on_every_path():
    """_SETTING_KEYS feeds the live _resolve_settings AND the backtest host's
    _expert_decision_settings; a key missing there is silently dropped live."""
    assert "macro_short_side" in DeterministicScorer._SETTING_KEYS
    # It changes the final score, i.e. the SYMBOL360 card's header badge/confidence.
    assert "macro_short_side" in DeterministicScorer.EXPORT_RELEVANT_SETTINGS


def test_unknown_value_is_refused_loudly():
    with pytest.raises(ValueError, match="macro_short_side"):
        _fs(-0.3, -0.5, 3, macro_short_side="mirorr")


# ---------------------------------------------------------------- mirror behaviour
def test_mirror_positive_side_is_identical_to_same():
    for t, r, n, _f, _m in _GOLDEN:
        if t < 0:
            continue
        a, b = _fs(t, r, n), _fs(t, r, n, macro_short_side="mirror")
        assert a["final"].hex() == b["final"].hex(), (t, r, n)
        assert a["exposure_multiplier"] == b["exposure_multiplier"], (t, r, n)


@pytest.mark.parametrize("regime", [-0.7, -0.5, -0.2])
def test_mirror_bearish_regime_keeps_more_short_magnitude(regime):
    same = _fs(-0.35, regime, 3)
    mirror = _fs(-0.35, regime, 3, macro_short_side="mirror")
    assert mirror["final"] < same["final"] < 0
    pre = math.tanh(-0.35 / C.DEF_K_COMPRESS)
    assert mirror["final"] == pre * exposure_multiplier(-regime, n_inputs=3)
    assert mirror["exposure_multiplier"] == exposure_multiplier(-regime, n_inputs=3)


def test_mirror_bullish_regime_damps_shorts():
    same = _fs(-0.35, 0.6, 3)
    mirror = _fs(-0.35, 0.6, 3, macro_short_side="mirror")
    assert same["final"] < mirror["final"] < 0


def test_mirror_neutral_regime_equals_same():
    assert _fs(-0.35, 0.0, 3, macro_short_side="mirror")["final"] == _fs(-0.35, 0.0, 3)["final"]


def test_mirror_hard_riskoff_zeroes_longs_not_shorts():
    r = DEF_HARD_RISKOFF - 0.1  # corroborated hard risk-off
    assert _fs(0.5, r, 3, macro_short_side="mirror")["final"] == 0.0
    short = _fs(-0.35, r, 3, macro_short_side="mirror")
    assert short["final"] == math.tanh(-0.35 / C.DEF_K_COMPRESS) * exposure_multiplier(-r)
    assert short["final"] < -0.4  # the most bearish regime is where SELL conviction survives
    # "same" still flattens both sides, as it always did.
    assert _fs(-0.35, r, 3)["final"] == 0.0


def test_mirror_hard_riskon_zeroes_shorts_symmetrically():
    """The mirror of hard risk-off: regime > -hard_riskoff (corroborated) zeroes shorts."""
    r = -DEF_HARD_RISKOFF + 0.1
    assert _fs(-0.35, r, 3, macro_short_side="mirror")["final"] == 0.0
    assert _fs(0.5, r, 3, macro_short_side="mirror")["final"] > 0  # longs untouched


def test_mirror_hard_riskon_needs_corroboration_like_riskoff():
    """A lone binary index-trend reading of +1 must not flatten every short -- the same
    n_inputs guard the long side has against a lone -1."""
    one = _fs(-0.35, 1.0, 1, macro_short_side="mirror")
    assert one["final"] == math.tanh(-0.35 / C.DEF_K_COMPRESS) * DEF_M_FLOOR
    assert _fs(-0.35, 1.0, None, macro_short_side="mirror")["final"] == 0.0  # unknown = armed


def test_mirror_only_affects_multiply_mode():
    for mode in ("gate", "input", "off"):
        for r in (-0.9, -0.5, 0.5, 0.9):
            a = _fs(-0.35, r, 3, macro_mode=mode)
            b = _fs(-0.35, r, 3, macro_mode=mode, macro_short_side="mirror")
            assert a == b, (mode, r)


def test_mirror_stays_clipped():
    res = C.final_score(technical=-5.0, fundamental=None, analyst=None, regime=-1.0,
                        s={**_TECH_ONLY, "k_compress": 0.0, "macro_short_side": "mirror"},
                        regime_n_inputs=3)
    assert res["final"] == -1.0


# ---------------------------------------------------------------- wiring into _process
class _Stubbed(DeterministicScorer):
    """_process with the non-technical section builders pinned, so only the combine
    step can move between the two settings."""

    def __init__(self):  # no DB row
        pass

    def _build_fundamental(self, data_bundle, settings, as_of=None):
        return {"score": None, "veto": False}

    def _build_regime(self, data_bundle, settings):
        return {"score": -0.6, "n_inputs": 3, "components": {}}


def _defaults():
    defs = DeterministicScorer.get_settings_definitions()
    return {k: defs[k]["default"] for k in DeterministicScorer._SETTING_KEYS}


def _bundle():
    import numpy as np
    import pandas as pd
    n = 400
    close = np.linspace(200.0, 100.0, n)  # a steady decline -> negative technical score
    df = pd.DataFrame({"Date": pd.date_range("2021-01-01", periods=n, freq="B"),
                       "Open": close, "High": close * 1.01, "Low": close * 0.99,
                       "Close": close, "Volume": 1e6})
    return {"symbol": "ZZZT10", "ohlcv": df, "current_price": float(close[-1])}


def test_process_honours_the_setting():
    exp = _Stubbed()
    same = exp._process(_bundle(), _defaults())
    mirror = exp._process(_bundle(), {**_defaults(), "macro_short_side": "mirror"})
    f_same = same.raw_outputs["calc"]["final_score"]
    f_mirror = mirror.raw_outputs["calc"]["final_score"]
    assert f_same < 0
    assert f_mirror < f_same  # a bearish regime no longer mutes the SELL


def test_analyze_as_of_carries_the_setting(monkeypatch):
    """Backtest path: context.settings -> _process."""
    from datetime import datetime, timezone
    from ba2_common.core.backtest_context import BacktestContext

    exp = _Stubbed()
    monkeypatch.setattr(_Stubbed, "_gather", lambda self, providers, as_of: _bundle())
    as_of = datetime(2022, 6, 1, tzinfo=timezone.utc)
    got = {}
    for side in ("same", "mirror"):
        ctx = BacktestContext(providers=None, settings={**_defaults(), "macro_short_side": side},
                              as_of=as_of, extra={"symbol": "ZZZT10"})
        got[side] = exp.analyze_as_of(as_of, ctx).raw_outputs["calc"]["final_score"]
    assert got["mirror"] < got["same"] < 0


def test_live_resolved_settings_carry_the_setting(monkeypatch):
    """Live path: run_analysis builds settings with _resolve_settings(_SETTING_KEYS)."""
    exp = _Stubbed()
    stored = {"macro_short_side": "mirror"}
    defaults = _defaults()
    monkeypatch.setattr(_Stubbed, "get_setting_with_interface_default",
                        lambda self, k, *a, **kw: stored.get(k, defaults[k]), raising=False)
    resolved = exp._resolve_settings(DeterministicScorer._SETTING_KEYS)
    assert resolved["macro_short_side"] == "mirror"
    f = exp._process(_bundle(), resolved).raw_outputs["calc"]["final_score"]
    assert f < exp._process(_bundle(), defaults).raw_outputs["calc"]["final_score"] < 0

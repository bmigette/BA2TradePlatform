"""SPY benchmark regime leaf conditions + the registry fallback (plan 2026-10-24-spy-regime-gate, Task 3)."""
import pytest

from ba2_common.core import benchmark_regime as br
from ba2_common.core import market_conditions as mc
from ba2_common.core import rule_builders as rb
from ba2_common.core import trigger_catalog as tc
from ba2_common.core.market_condition_rules import market_condition_fields, served_fields, STRICT_FIELD_NAMES
from ba2_common.core.market_conditions import field_codes, field_spec
from ba2_common.core.TradeConditions import CONDITION_MAP, SpyTrendRegimeCondition, SpyVolRegimeCondition
from ba2_common.core.types import ExpertEventType


def _cond(cls, mode_code, op="=="):
    return cls(account=None, instrument_name="AAPL", expert_recommendation=None,
               operator_str=op, value=float(mode_code))


@pytest.fixture(autouse=True)
def _clean():
    br.reset_benchmark_regime()
    yield
    br.reset_benchmark_regime()


def test_registered_everywhere():
    assert CONDITION_MAP[ExpertEventType.N_SPY_TREND_REGIME] is SpyTrendRegimeCondition
    assert CONDITION_MAP[ExpertEventType.N_SPY_VOL_REGIME] is SpyVolRegimeCondition
    assert rb.FIELD_EVENT["spy_trend_regime"] is ExpertEventType.N_SPY_TREND_REGIME
    assert rb.FIELD_EVENT["spy_vol_regime"] is ExpertEventType.N_SPY_VOL_REGIME
    names = {"spy_trend_regime", "spy_vol_regime"}
    assert names <= STRICT_FIELD_NAMES <= market_condition_fields()
    assert names <= served_fields([])        # needs no profile
    assert dict(field_codes("spy_trend_regime")) == dict(br.TREND_MODE_CODES)
    assert dict(field_codes("spy_vol_regime")) == dict(br.VOL_MODE_CODES)
    assert field_spec("spy_vol_regime").kind == "categorical"


def test_benchmark_fields_are_not_a_profile():
    assert not (set(mc.BENCHMARK_FIELDS) & {f.name for p in mc.PROFILES.values() for f in p.fields})
    assert mc.BENCHMARK_FIELDS["spy_trend_regime"].short == "spy-trend"
    assert mc.BENCHMARK_FIELDS["spy_vol_regime"].short == "spy-vol"
    assert all(s.searched for s in mc.BENCHMARK_FIELDS.values())


def test_trigger_catalog_lists_them_as_market_categoricals_without_a_profile():
    by_value = {e.value: e for e in tc.trigger_catalog()}
    for name, table in (("spy_trend_regime", br.TREND_MODE_CODES), ("spy_vol_regime", br.VOL_MODE_CODES)):
        e = by_value[name]
        assert e.kind == "categorical" and "market" in e.categories and e.requires_profile == ""
        assert "SPY" in e.description
        assert tc.categorical_codes_for(name) == tuple(table.items())
        assert tc.operator_options_for(name) == ["=="]


@pytest.mark.parametrize("op", ["<", ">", "!=", ">="])
def test_only_equality_is_accepted(op):
    with pytest.raises(ValueError):
        _cond(SpyTrendRegimeCondition, 1, op)


def test_unknown_mode_code_refused_at_construction():
    with pytest.raises(ValueError):
        _cond(SpyVolRegimeCondition, 99)


def test_nothing_published_never_passes_and_says_why():
    c = _cond(SpyTrendRegimeCondition, br.TREND_MODE_CODES["above_200"])
    assert c.evaluate() is False
    assert c.last_status == "no_regime" and "regime" in c.last_reason
    assert c.get_actual_value_display().startswith("unknown")


def test_published_failure_reason_is_carried():
    br.set_benchmark_regime_failure("SPY fetch failed: boom")
    c = _cond(SpyVolRegimeCondition, br.VOL_MODE_CODES["calm"])
    assert c.evaluate() is False and "boom" in c.last_reason


def test_unknown_axis_never_passes_and_carries_the_axis_reason():
    br.set_benchmark_regime(br.BenchmarkRegime(None, "calm", trend_reason="insufficient history (150 bars, need 200)"))
    c = _cond(SpyTrendRegimeCondition, br.TREND_MODE_CODES["below_200"])
    assert c.evaluate() is False and c.last_status == "insufficient_history" and "150 bars" in c.last_reason
    assert _cond(SpyVolRegimeCondition, br.VOL_MODE_CODES["calm"]).evaluate() is True    # other axis unaffected


@pytest.mark.parametrize("state,mode,expected", [
    ("up", "up", True), ("up", "pullback", False), ("pullback", "above_200", True),
    ("bear_rally", "below_200", True), ("down", "above_200", False)])
def test_trend_leaf_truth_table(state, mode, expected):
    br.set_benchmark_regime(br.BenchmarkRegime(state, "normal"))
    c = _cond(SpyTrendRegimeCondition, br.TREND_MODE_CODES[mode])
    assert c.evaluate() is expected and c.get_actual_value_display() == state
    assert c.last_status == "valid"


def test_vol_leaf_truth_table():
    br.set_benchmark_regime(br.BenchmarkRegime("up", "stressed"))
    assert _cond(SpyVolRegimeCondition, br.VOL_MODE_CODES["not_stressed"]).evaluate() is False
    assert _cond(SpyVolRegimeCondition, br.VOL_MODE_CODES["not_calm"]).evaluate() is True


def test_description_names_axis_and_mode():
    d = _cond(SpyTrendRegimeCondition, br.TREND_MODE_CODES["pullback"]).get_description()
    assert "trend" in d and "pullback" in d
    assert "volatility" in _cond(SpyVolRegimeCondition, br.VOL_MODE_CODES["calm"]).get_description()

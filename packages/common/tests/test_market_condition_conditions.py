"""Market-condition gates in the ENGINE: generated condition classes, the lazily-resolved
``MarketConditionContext`` seam, and the enum/registry agreement.

Design ``docs/plans/2026-09-15-option-market-condition-genes-design.md`` §4.1, §5, §7:

* unknown NEVER passes -- no context, no feature row, a non-valid observation: False for every
  operator, ``calculated_value`` None, and the status/reason recorded;
* equality passes neither strict comparison;
* with no market leaf in a ruleset the seam is never consulted (gates off cost nothing);
* classes, CONDITION_MAP entries and FIELD_EVENT entries are GENERATED from the registry.

CATEGORICAL PATH. No categorical field is registered yet and enum members cannot be added at
runtime, so the categorical tests register a throwaway categorical profile, check that
``register_market_condition_conditions()`` skips it LOUDLY (returns its name; no CONDITION_MAP
entry), then build its generated class directly with ``market_condition_condition_class`` and
exercise the ``==``-on-float-code comparison through the real ``evaluate()``.
"""
import dataclasses
import logging
import pickle
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from ba2_common.core import TradeConditions as T
from ba2_common.core import rule_builders as rb
from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator
from ba2_common.core.market_condition_context import (
    TIMING_POLICY_PRIOR_SESSION_V1,
    DictMarketConditionReader,
    MarketConditionContext,
    FeatureRowLike,
    MarketConditionReader,
)
from ba2_common.core.market_conditions import (
    CALC_VERSION,
    FIELD_ADX,
    FIELD_RV_RATIO,
    FIELD_TREND_SLOPE,
    PROFILES,
    STATUS_INSUFFICIENT_HISTORY,
    STATUS_INVALID_PRICES,
    STATUS_MISSING_SESSION,
    STATUS_NO_CONTEXT,
    STATUS_VALID,
    FieldSpec,
    MarketConditionValues,
    Observation,
    ProfileSpec,
    registered_profile,
)
from ba2_common.core.types import (
    ExpertEventType,
    OrderRecommendation,
    get_numeric_event_values,
    is_numeric_event,
)

SYMBOL = "AAPL"
SESSION = date(2024, 3, 15)
PRIOR = date(2024, 3, 14)
DECISION = datetime(2024, 3, 15, 14, 30, tzinfo=timezone.utc)


def _values(adx=30.0, slope=0.1, rv=0.9, adx_obs=None):
    return MarketConditionValues(
        trend_slope=Observation(slope, STATUS_VALID),
        adx=adx_obs if adx_obs is not None else Observation(adx, STATUS_VALID),
        rv_ratio=Observation(rv, STATUS_VALID),
    )


def _ctx(rows=None, recorder=None, reader=None):
    return MarketConditionContext(
        decision_time=DECISION, session_label=SESSION, prior_session=PRIOR,
        source_profile="ohlcv-v1", timing_policy=TIMING_POLICY_PRIOR_SESSION_V1,
        calc_version=CALC_VERSION,
        reader=reader if reader is not None else DictMarketConditionReader(
            rows if rows is not None else {(SYMBOL, PRIOR): _values()}),
        recorder=recorder,
    )


def _rec():
    return SimpleNamespace(created_at=DECISION, instance_id=1, symbol=SYMBOL, data={},
                           confidence=80.0, recommended_action=OrderRecommendation.BUY)


class _Account:
    id = 1


@pytest.fixture
def resolver():
    """Install a resolver for the test; ALWAYS restore the previous one."""
    previous = T.get_market_condition_context_resolver()
    calls = []

    def install(ctx_or_fn):
        def fn(account, instrument_name, expert_recommendation):
            calls.append((account, instrument_name, expert_recommendation))
            return ctx_or_fn(account, instrument_name, expert_recommendation) if callable(ctx_or_fn) else ctx_or_fn
        T.set_market_condition_context_resolver(fn)
        return calls

    yield install
    T.set_market_condition_context_resolver(previous)


@pytest.fixture
def no_resolver():
    previous = T.get_market_condition_context_resolver()
    T.set_market_condition_context_resolver(None)
    yield
    T.set_market_condition_context_resolver(previous)


def _cond(event=ExpertEventType.N_UNDERLYING_ADX, op="<", value=35.0):
    return T.create_condition(event, _Account(), SYMBOL, _rec(), operator_str=op, value=value)


# --- enum / registry agreement ------------------------------------------------------------

def test_every_registered_field_has_an_expert_event_type_with_the_same_value():
    """Every field of every profile in ``PROFILES`` needs an ``ExpertEventType`` member whose
    VALUE is the field name. Enum members cannot be generated at runtime, so a newly registered
    profile (Task 10's categorical ``ta-structure-v1``) FAILS HERE until it adds its members to
    ``types.ExpertEventType`` (and to ``get_numeric_event_values``); the condition classes and
    the CONDITION_MAP / FIELD_EVENT entries then follow from the registry automatically."""
    values = {et.value for et in ExpertEventType}
    missing = [f.name for prof in PROFILES.values() for f in prof.fields if f.name not in values]
    assert not missing, f"registered market-condition fields with no ExpertEventType member: {missing}"
    for prof in PROFILES.values():
        for f in prof.fields:
            et = ExpertEventType(f.name)
            assert et.name.startswith("N_")
            assert et.value in get_numeric_event_values() and is_numeric_event(et.value)


def test_every_registered_field_is_mapped_to_its_generated_class_and_rule_field():
    for prof in PROFILES.values():
        for f in prof.fields:
            et = ExpertEventType(f.name)
            cls = T.CONDITION_MAP[et]
            assert issubclass(cls, T.MarketConditionCompare) and cls.FIELD == f.name
            assert cls is T.market_condition_condition_class(f.name)
            assert rb.FIELD_EVENT[f.name] is et


def test_explicit_v1_names_are_the_generated_classes():
    assert T.UnderlyingTrendSlopeCondition is T.market_condition_condition_class(FIELD_TREND_SLOPE)
    assert T.UnderlyingAdxCondition is T.market_condition_condition_class(FIELD_ADX)
    assert T.UnderlyingRealizedVolRatioCondition is T.market_condition_condition_class(FIELD_RV_RATIO)
    assert T.UnderlyingAdxCondition.__name__ == "UnderlyingAdx14Condition"


def test_registration_is_idempotent():
    before = dict(T.CONDITION_MAP)
    fe_before = dict(rb.FIELD_EVENT)
    assert T.register_market_condition_conditions() == []
    assert rb.register_market_condition_field_events() == []
    assert T.CONDITION_MAP == before and rb.FIELD_EVENT == fe_before


def test_unknown_field_has_no_class():
    with pytest.raises(KeyError):
        T.market_condition_condition_class("frobnicate")


def test_create_condition_returns_the_generated_class():
    c = _cond(ExpertEventType.N_UNDERLYING_ADX)
    assert type(c) is T.UnderlyingAdxCondition
    assert c.last_status is None and c.last_reason == "" and c.calculated_value is None


# --- unknown never passes -----------------------------------------------------------------

@pytest.mark.parametrize("op", ["<", ">"])
def test_no_resolver_is_no_context(no_resolver, op):
    c = _cond(op=op, value=30.0)
    assert c.evaluate() is False
    assert c.calculated_value is None
    assert c.last_status == STATUS_NO_CONTEXT
    assert c.last_reason == "market-condition profile not wired for this process"


def test_resolver_returning_none_is_no_context(resolver):
    calls = resolver(None)
    c = _cond()
    assert c.evaluate() is False
    assert c.calculated_value is None and c.last_status == STATUS_NO_CONTEXT
    assert calls and calls[0][1] == SYMBOL


def test_resolver_returning_a_non_context_is_a_wiring_defect(resolver):
    resolver(object())
    with pytest.raises(TypeError):
        _cond().evaluate()


@pytest.mark.parametrize("op", ["<", ">"])
def test_missing_row_is_missing_session(resolver, op):
    resolver(_ctx(rows={}))
    c = _cond(op=op)
    assert c.evaluate() is False
    assert c.calculated_value is None
    assert c.last_status == STATUS_MISSING_SESSION
    assert SYMBOL in c.last_reason and str(PRIOR) in c.last_reason


def test_the_row_is_read_at_the_prior_session_not_the_decision_session(resolver):
    resolver(_ctx(rows={(SYMBOL, SESSION): _values(adx=30.0)}))
    c = _cond()
    assert c.evaluate() is False and c.last_status == STATUS_MISSING_SESSION


@pytest.mark.parametrize("status,reason", [
    (STATUS_INSUFFICIENT_HISTORY, "insufficient history: 90 of 128 bars"),
    (STATUS_INVALID_PRICES, "index 50 (close non-finite)"),
])
@pytest.mark.parametrize("op,value", [("<", 1e9), (">", -1e9)])
def test_non_valid_observation_never_passes(resolver, status, reason, op, value):
    recorded = []
    bad = Observation(None, status, reason)
    resolver(_ctx(rows={(SYMBOL, PRIOR): _values(adx_obs=bad)},
                  recorder=lambda *a: recorded.append(a)))
    c = _cond(op=op, value=value)
    assert c.evaluate() is False
    assert c.calculated_value is None
    assert (c.last_status, c.last_reason) == (status, reason)
    assert recorded == [], "the recorder must never be called on an unknown"


def test_row_without_the_field_raises(resolver):
    """A row that does not carry the leaf's field is a launcher/reader WIRING defect (leaves are
    placed by profile) -- never an unknown the GA could learn "mode=off wins" from."""
    class _OtherProfileValues:
        def by_field(self):
            return {"some_other_field": Observation(1.0, STATUS_VALID)}

    resolver(_ctx(reader=DictMarketConditionReader({(SYMBOL, PRIOR): _OtherProfileValues()})))
    with pytest.raises(LookupError, match=FIELD_ADX) as ei:
        _cond().evaluate()
    assert "some_other_field" in str(ei.value)


def test_reader_exception_escapes_evaluate(resolver):
    class _Boom:
        def observe(self, symbol, session):
            raise RuntimeError("reader down")

    resolver(_ctx(reader=_Boom()))
    with pytest.raises(RuntimeError, match="reader down"):
        _cond().evaluate()


def test_recorder_exception_escapes_evaluate(resolver):
    def _boom(*a):
        raise RuntimeError("recorder down")

    resolver(_ctx(recorder=_boom))
    with pytest.raises(RuntimeError, match="recorder down"):
        _cond().evaluate()


def test_no_resolver_warns_once_per_process(no_resolver, monkeypatch):
    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            if record.levelno == logging.WARNING:
                records.append(record.getMessage())

    from ba2_common import logger as _logger_mod
    monkeypatch.setattr(T, "_warned_no_market_condition_resolver", False)
    handler = _Capture()
    _logger_mod.logger.addHandler(handler)
    try:
        for _ in range(3):
            assert _cond().evaluate() is False
    finally:
        _logger_mod.logger.removeHandler(handler)
    assert len([m for m in records if "NO context resolver" in m]) == 1


# --- comparisons --------------------------------------------------------------------------

@pytest.mark.parametrize("op,value,expected", [
    ("<", 25.0, False),
    ("<", 35.0, True),
    (">", 30.0, False),   # equality passes neither strict comparison
    ("<", 30.0, False),
    (">", 25.0, True),
])
def test_valid_numeric_comparison(resolver, op, value, expected):
    resolver(_ctx(rows={(SYMBOL, PRIOR): _values(adx=30.0)}))
    c = _cond(op=op, value=value)
    assert c.evaluate() is expected
    assert c.calculated_value == 30.0
    assert c.last_status == STATUS_VALID


def test_each_v1_field_reads_its_own_observation(resolver):
    resolver(_ctx(rows={(SYMBOL, PRIOR): _values(adx=30.0, slope=-0.2, rv=1.5)}))
    got = {}
    for et in (ExpertEventType.N_UNDERLYING_TREND_SLOPE, ExpertEventType.N_UNDERLYING_ADX,
               ExpertEventType.N_UNDERLYING_RV_RATIO):
        c = _cond(et, op=">", value=-100.0)
        assert c.evaluate() is True
        got[et.value] = c.calculated_value
    assert got == {FIELD_TREND_SLOPE: -0.2, FIELD_ADX: 30.0, FIELD_RV_RATIO: 1.5}


def test_recorder_called_exactly_once_per_successful_evaluation(resolver):
    recorded = []
    vals = _values(adx=30.0)
    resolver(_ctx(rows={(SYMBOL, PRIOR): vals}, recorder=lambda *a: recorded.append(a)))
    passing = _cond(op="<", value=35.0)
    failing = _cond(op=">", value=35.0)
    assert passing.evaluate() is True
    assert recorded == [(SYMBOL, PRIOR, vals)]
    # A valid read whose comparison fails is still a successful evaluation: recorded once.
    assert failing.evaluate() is False
    assert len(recorded) == 2


def test_reader_protocol_is_satisfied_by_the_dict_reader():
    assert isinstance(DictMarketConditionReader({}), MarketConditionReader)
    assert isinstance(_values(), FeatureRowLike)


@pytest.mark.parametrize("op", [">=", "<=", "!=", "=="])
def test_numeric_field_rejects_non_strict_operators_at_construction(op):
    with pytest.raises(ValueError, match="accepts only"):
        _cond(ExpertEventType.N_UNDERLYING_ADX, op=op, value=25.0)


@pytest.mark.parametrize("cls", [T.UnderlyingTrendSlopeCondition, T.UnderlyingAdxCondition,
                                 T.UnderlyingRealizedVolRatioCondition])
def test_generated_classes_pickle_by_reference(cls):
    assert pickle.loads(pickle.dumps(cls)) is cls
    assert cls.KIND == "numeric" and cls.ALLOWED_OPERATORS == frozenset({"<", ">"})


def test_condition_map_conflict_guard(monkeypatch):
    monkeypatch.setitem(T.CONDITION_MAP, ExpertEventType.N_UNDERLYING_ADX, T.ConfidenceCondition)
    with pytest.raises(ValueError, match="refusing to replace"):
        T.register_market_condition_conditions()


def test_field_event_conflict_guard(monkeypatch):
    monkeypatch.setitem(rb.FIELD_EVENT, FIELD_ADX, ExpertEventType.N_CONFIDENCE)
    with pytest.raises(ValueError, match="refusing to replace"):
        rb.register_market_condition_field_events()


def test_valid_numeric_read_does_not_touch_the_registry(resolver, monkeypatch):
    """The display is called on every evaluation: a numeric field must not scan the registry."""
    resolver(_ctx(rows={(SYMBOL, PRIOR): _values(adx=20.0)}))
    monkeypatch.setattr(T, "_mc_field_codes", lambda *a: pytest.fail("registry read"))
    monkeypatch.setattr(T, "_mc_field_spec", lambda *a: pytest.fail("registry read"))
    c = _cond(op="<", value=25.0)
    assert c.evaluate() is True
    assert c.get_actual_value_display() == "20"
    c.get_description()


# --- categorical ------------------------------------------------------------------------

_CAT = FieldSpec(name="test_cat_regime_v0", kind="categorical", short="tcat", searched=True,
                 codes={"bull": 1, "bear": 2}, ui_name="Test regime")


class _CatValues:
    """Duck-typed feature row carrying the categorical field (MarketConditionValues holds only
    the ohlcv-v1 trio today; a categorical observation is a float code)."""

    def __init__(self, obs):
        self._obs = obs

    def by_field(self):
        return {_CAT.name: self._obs}


@pytest.fixture
def cat_classes(monkeypatch):
    """Isolate the factory's memo and the module global it binds for the throwaway field."""
    monkeypatch.setattr(T, "_MARKET_CONDITION_CLASSES", dict(T._MARKET_CONDITION_CLASSES))
    yield
    T.__dict__.pop("TestCatRegimeV0Condition", None)


def test_categorical_field_without_event_type_is_skipped_loudly():
    with registered_profile(ProfileSpec(name="test-cat-v0", calc_version="t/1", fields=(_CAT,))):
        before = dict(T.CONDITION_MAP)
        fe_before = dict(rb.FIELD_EVENT)
        assert T.register_market_condition_conditions() == [_CAT.name]
        assert rb.register_market_condition_field_events() == [_CAT.name]
        assert T.CONDITION_MAP == before and rb.FIELD_EVENT == fe_before


@pytest.mark.parametrize("value,expected", [(2.0, True), (1.0, False), (2, True)])
def test_categorical_equality_on_float_code(resolver, cat_classes, value, expected):
    with registered_profile(ProfileSpec(name="test-cat-v0", calc_version="t/1", fields=(_CAT,))):
        cls = T.market_condition_condition_class(_CAT.name)
        assert cls.__name__ == "TestCatRegimeV0Condition"
        recorded = []
        resolver(_ctx(reader=DictMarketConditionReader(
            {(SYMBOL, PRIOR): _CatValues(Observation(2.0, STATUS_VALID))}),
            recorder=lambda *a: recorded.append(a)))
        c = cls(_Account(), SYMBOL, _rec(), "==", value)
        assert c.evaluate() is expected
        assert c.calculated_value == 2.0
        assert c.get_actual_value_display() == "bear"
        assert ("bear" if float(value) == 2.0 else "bull") in c.get_description()
        assert len(recorded) == 1
        assert pickle.loads(pickle.dumps(cls)) is cls


@pytest.mark.parametrize("op", ["<", ">", "!="])
def test_categorical_field_rejects_non_equality_operators_at_construction(cat_classes, op):
    with registered_profile(ProfileSpec(name="test-cat-v0", calc_version="t/1", fields=(_CAT,))):
        cls = T.market_condition_condition_class(_CAT.name)
        assert cls.KIND == "categorical" and cls.ALLOWED_OPERATORS == frozenset({"=="})
        with pytest.raises(ValueError, match="accepts only"):
            cls(_Account(), SYMBOL, _rec(), op, 2.0)


def test_memo_is_keyed_by_spec_so_a_rekinded_field_gets_a_fresh_class(cat_classes):
    numeric = FieldSpec(name=_CAT.name, kind="numeric", short="tcat", searched=True,
                        value_min=0.0, value_max=1.0, value_step=0.5, anchor_op="<",
                        anchor_value=0.5)
    with registered_profile(ProfileSpec(name="test-cat-v0", calc_version="t/1", fields=(_CAT,))):
        cat_cls = T.market_condition_condition_class(_CAT.name)
    with registered_profile(ProfileSpec(name="test-num-v0", calc_version="t/1", fields=(numeric,))):
        num_cls = T.market_condition_condition_class(_CAT.name)
    assert cat_cls is not num_cls
    assert (cat_cls.KIND, num_cls.KIND) == ("categorical", "numeric")


def test_categorical_unknown_never_passes_equality(resolver, cat_classes):
    with registered_profile(ProfileSpec(name="test-cat-v0", calc_version="t/1", fields=(_CAT,))):
        cls = T.market_condition_condition_class(_CAT.name)
        resolver(_ctx(reader=DictMarketConditionReader(
            {(SYMBOL, PRIOR): _CatValues(Observation(None, STATUS_INSUFFICIENT_HISTORY, "short"))})))
        c = cls(_Account(), SYMBOL, _rec(), "==", 2.0)
        assert c.evaluate() is False and c.calculated_value is None
        assert c.last_status == STATUS_INSUFFICIENT_HISTORY


# --- rule building -----------------------------------------------------------------------

def test_tree_with_the_three_leaves_yields_three_triggers_and_no_unmapped_warning():
    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    from ba2_common import logger as _logger_mod
    handler = _Capture()
    _logger_mod.logger.addHandler(handler)
    try:
        tree = {"type": "AND", "conditions": [
            {"id": "s", "field": FIELD_TREND_SLOPE, "op": ">", "value": 0.0},
            {"id": "a", "field": FIELD_ADX, "op": "<", "value": 25.0},
            {"id": "r", "field": FIELD_RV_RATIO, "op": "<", "value": 1.0},
        ]}
        triggers = rb.triggers_from_condition_tree(tree)
    finally:
        _logger_mod.logger.removeHandler(handler)
    assert sorted(t["event_type"] for t in triggers.values()) == sorted(
        [FIELD_TREND_SLOPE, FIELD_ADX, FIELD_RV_RATIO])
    assert not [m for m in records if "DROPPED" in m or "mapping" in m]


# --- gates off cost nothing ---------------------------------------------------------------

def _evaluate_triggers(triggers):
    ev = TradeActionEvaluator(account=_Account(), evaluate_all_conditions=True)
    action = SimpleNamespace(name="r", id=1, triggers=triggers)
    return ev, ev._evaluate_conditions(action, SYMBOL, _rec(), None)


def test_legacy_tree_never_consults_the_market_condition_seam(no_resolver, monkeypatch):
    spy = []
    real = T.resolve_market_condition_context
    monkeypatch.setattr(T, "resolve_market_condition_context",
                        lambda *a, **k: spy.append(a) or real(*a, **k))
    triggers = rb.triggers_from_condition_tree({"type": "AND", "conditions": [
        {"id": "c", "field": "confidence", "op": ">", "value": 50},
        {"id": "b", "field": "bullish"},
    ]})
    _, ok = _evaluate_triggers(triggers)
    assert ok is True, "control: the legacy leaves were really evaluated"
    assert spy == [], "a ruleset with no market leaf must not resolve a market-condition context"

    # Control: the spy DOES see a market leaf, so the empty result above is meaningful.
    _evaluate_triggers(rb.triggers_from_condition_tree({"type": "AND", "conditions": [
        {"id": "a", "field": FIELD_ADX, "op": "<", "value": 25.0}]}))
    assert len(spy) == 1


def test_evaluator_reports_market_leaf_value_and_unknown(resolver):
    resolver(_ctx(rows={(SYMBOL, PRIOR): _values(adx=20.0)}))
    ev, ok = _evaluate_triggers({"a": {"event_type": FIELD_ADX, "operator": "<", "value": 25.0}})
    assert ok is True
    assert ev.condition_evaluations[0]["calculated_value"] == 20.0
    resolver(_ctx(rows={}))
    ev, ok = _evaluate_triggers({"a": {"event_type": FIELD_ADX, "operator": "<", "value": 25.0}})
    assert ok is False
    assert "calculated_value" not in ev.condition_evaluations[0]


# --- context validation -------------------------------------------------------------------

def test_context_rejects_a_naive_decision_time():
    with pytest.raises(ValueError, match="timezone-aware"):
        MarketConditionContext(
            decision_time=datetime(2024, 3, 15, 14, 30), session_label=SESSION,
            prior_session=PRIOR, source_profile="ohlcv-v1",
            timing_policy=TIMING_POLICY_PRIOR_SESSION_V1, calc_version=CALC_VERSION,
            reader=DictMarketConditionReader({}))


@pytest.mark.parametrize("kw", [
    {"session_label": datetime(2024, 3, 15, tzinfo=timezone.utc)},
    {"prior_session": SESSION},
    {"timing_policy": "same_session"},
    {"source_profile": ""},
    {"calc_version": ""},
    {"reader": object()},
])
def test_context_rejects_inconsistent_fields(kw):
    base = dict(decision_time=DECISION, session_label=SESSION, prior_session=PRIOR,
                source_profile="ohlcv-v1", timing_policy=TIMING_POLICY_PRIOR_SESSION_V1,
                calc_version=CALC_VERSION, reader=DictMarketConditionReader({}))
    base.update(kw)
    with pytest.raises(ValueError):
        MarketConditionContext(**base)


def test_context_is_immutable():
    ctx = _ctx()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.prior_session = SESSION  # type: ignore[misc]

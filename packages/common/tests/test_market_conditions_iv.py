"""The ``iv-v1`` market-condition profile: registry, calculator, missing values, readers, live.

Pinned here:

* the five fields are registered with their ranges/anchors, two of them searched, every one an
  ``ExpertEventType`` member and a strict deployable name; the profile is a CHAIN profile (not in
  ``COMPUTE_BY_PROFILE``) and is flagged as having no live source;
* ``compute_iv_v1`` against an independent recomputation; every missing case has its status
  (``missing_session`` for no chain rows, ``insufficient_history`` for an undefined measurement,
  the OHLCV window's own status for the realized leg) and never a value;
* a reader refuses to serve the profile without a pinned manifest (there is no compute-on-miss);
* the live resolver serves it with a reader that refuses every observation loudly, while an
  ohlcv-v1 profile of the same expert keeps its own reader.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pytest

from ba2_common.core import implied_vol as ivm
from ba2_common.core import market_conditions as mc
from ba2_common.core.market_conditions import (
    FIELD_ATM_IV_30, FIELD_ATM_IV_60, FIELD_IV_RV_RATIO, FIELD_IV_TERM_SLOPE, FIELD_REALIZED_VOL_20,
    IV_PROFILE, PROFILES, STATUS_INSUFFICIENT_HISTORY, STATUS_INVALID_PRICES, STATUS_MISSING_SESSION,
    STATUS_NO_CONTEXT, STATUS_VALID, WINDOW,
)

SESSION = date(2024, 3, 1)


def _b76(F, K, T, s, is_call):
    return ivm.black76_price(F, K, T, s, is_call)


def _chain(F=100.0, vol=lambda d: 0.25, dtes=(9, 16, 23, 37, 44, 58, 72, 100)):
    rows = []
    for d in dtes:
        for K in (90.0, 95.0, 100.0, 105.0, 110.0):
            for c in (True, False):
                p = _b76(F, K, d / 365.0, vol(d), c)
                rows.append((SESSION + timedelta(days=d), c, K, max(p - 0.0001, 0.0), p + 0.0001))
    e, c, k, b, a = zip(*rows)
    return ivm.ChainQuotes.from_columns(list(e), list(c), list(k), list(b), list(a))


def _bars(seed=5, n=WINDOW):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.015, n)))
    return c.copy(), c * 1.01, c * 0.99, c.copy(), np.full(n, 1e6)


# --------------------------------------------------------------------------- registry
def test_profile_is_registered_with_five_fields_two_searched():
    spec = PROFILES[IV_PROFILE]
    assert spec.calc_version == "iv-v1/calc-1"
    assert [f.name for f in spec.fields] == [FIELD_IV_RV_RATIO, FIELD_IV_TERM_SLOPE, FIELD_ATM_IV_30,
                                             FIELD_ATM_IV_60, FIELD_REALIZED_VOL_20]
    searched = {f.name: f for f in spec.fields if f.searched}
    assert set(searched) == {FIELD_IV_RV_RATIO, FIELD_IV_TERM_SLOPE}
    ivrv, term = searched[FIELD_IV_RV_RATIO], searched[FIELD_IV_TERM_SLOPE]
    assert (ivrv.short, ivrv.value_min, ivrv.value_max, ivrv.value_step) == ("ivrv", 0.6, 2.0, 0.1)
    assert (term.short, term.value_min, term.value_max, term.value_step) == ("ivterm", 0.85, 1.20, 0.05)
    assert all(f.kind == "numeric" and f.unit for f in spec.fields)


def test_chain_profile_is_not_computable_from_bars_and_has_no_live_source():
    assert mc.is_chain_profile(IV_PROFILE)
    assert IV_PROFILE not in mc.COMPUTE_BY_PROFILE
    assert mc.CHAIN_COMPUTE_BY_PROFILE[IV_PROFILE] is mc.compute_iv_v1
    assert mc.LIVE_OPTION_SOURCES[IV_PROFILE] == ("alphavantage",)
    assert mc.live_unavailable_reason(IV_PROFILE, "alphavantage") is None
    assert "thetadata" in mc.live_unavailable_reason(IV_PROFILE, "thetadata")
    assert "NO option source" in mc.live_unavailable_reason(IV_PROFILE, None)
    # the two OHLCV profiles are untouched by the addition
    for p in ("ohlcv-v1", "ta-structure-v1"):
        assert p in mc.COMPUTE_BY_PROFILE and not mc.is_chain_profile(p)
        assert mc.live_unavailable_reason(p, None) is None


def test_every_iv_field_is_an_event_type_and_a_strict_deployable_name():
    from ba2_common.core.market_condition_rules import STRICT_FIELD_NAMES
    from ba2_common.core.types import ExpertEventType

    for f in PROFILES[IV_PROFILE].fields:
        assert ExpertEventType(f.name).value == f.name
        assert f.name in STRICT_FIELD_NAMES


def test_the_generated_conditions_compare_strictly():
    from ba2_common.core.TradeConditions import market_condition_condition_class

    cls = market_condition_condition_class(FIELD_IV_RV_RATIO)
    assert cls.FIELD == FIELD_IV_RV_RATIO


# --------------------------------------------------------------------------- calculator
def test_compute_matches_an_independent_recomputation():
    o, h, l, c, v = _bars()
    vol = lambda d: 0.22 + 0.001 * d   # noqa: E731
    row = mc.compute_iv_v1(SESSION, _chain(vol=vol), (o, h, l, c, v)).by_field()
    term = ivm.atm_iv_term(_chain(vol=vol), SESSION)
    r = np.diff(np.log(c[-21:]))
    rv = float(np.std(r, ddof=1)) * math.sqrt(252)
    assert row[FIELD_ATM_IV_30].value == term.iv30
    assert row[FIELD_ATM_IV_60].value == term.iv60
    assert row[FIELD_REALIZED_VOL_20].value == pytest.approx(rv, rel=1e-12)
    assert row[FIELD_IV_RV_RATIO].value == pytest.approx(term.iv30 / rv, rel=1e-12)
    assert row[FIELD_IV_TERM_SLOPE].value == pytest.approx(term.iv30 / term.iv60, rel=1e-15)
    assert all(obs.status == STATUS_VALID for obs in row.values())


def test_an_inverted_term_structure_reads_above_one():
    o, h, l, c, v = _bars()
    row = mc.compute_iv_v1(SESSION, _chain(vol=lambda d: 0.5 - 0.002 * d), (o, h, l, c, v)).by_field()
    assert row[FIELD_IV_TERM_SLOPE].value > 1.0
    row2 = mc.compute_iv_v1(SESSION, _chain(vol=lambda d: 0.2 + 0.002 * d), (o, h, l, c, v)).by_field()
    assert row2[FIELD_IV_TERM_SLOPE].value < 1.0


def test_no_chain_rows_is_missing_session_for_the_implied_fields_only():
    row = mc.compute_iv_v1(SESSION, None, _bars()).by_field()
    for f in (FIELD_ATM_IV_30, FIELD_ATM_IV_60, FIELD_IV_RV_RATIO, FIELD_IV_TERM_SLOPE):
        assert row[f].status == STATUS_MISSING_SESSION and row[f].value is None
    assert row[FIELD_REALIZED_VOL_20].status == STATUS_VALID
    empty = mc.compute_iv_v1(SESSION, ivm.ChainQuotes.empty(), _bars()).by_field()
    assert empty[FIELD_ATM_IV_30].status == STATUS_MISSING_SESSION


def test_an_undefined_measurement_is_insufficient_history_never_zero():
    # monthly-only chain with no expiry in 7..30 days: IV30 undefined, IV60 fine
    row = mc.compute_iv_v1(SESSION, _chain(dtes=(5, 40, 70, 100)), _bars()).by_field()
    assert row[FIELD_ATM_IV_30].status == STATUS_INSUFFICIENT_HISTORY
    assert "7..30" in row[FIELD_ATM_IV_30].reason
    assert row[FIELD_ATM_IV_60].status == STATUS_VALID
    # both ratios need IV30: they inherit its status and say so
    for f in (FIELD_IV_RV_RATIO, FIELD_IV_TERM_SLOPE):
        assert row[f].status == STATUS_INSUFFICIENT_HISTORY and row[f].value is None
        assert "7..30" in row[f].reason


def test_the_realized_leg_inherits_the_window_status():
    row = mc.compute_iv_v1(SESSION, _chain(), None, window_status=STATUS_MISSING_SESSION,
                           window_reason="expected session 2024-02-29 missing").by_field()
    assert row[FIELD_REALIZED_VOL_20].status == STATUS_MISSING_SESSION
    assert row[FIELD_IV_RV_RATIO].status == STATUS_MISSING_SESSION
    assert "2024-02-29" in row[FIELD_IV_RV_RATIO].reason
    assert row[FIELD_IV_TERM_SLOPE].status == STATUS_VALID     # needs no bars
    o, h, l, c, v = _bars()
    c[50] = float("nan")
    bad = mc.compute_iv_v1(SESSION, _chain(), (o, h, l, c, v)).by_field()
    assert bad[FIELD_REALIZED_VOL_20].status == STATUS_INVALID_PRICES


def test_zero_realized_vol_makes_the_ratio_undefined_not_infinite():
    flat = np.full(WINDOW, 50.0)
    row = mc.compute_iv_v1(SESSION, _chain(), (flat, flat + 1, flat - 1, flat, np.full(WINDOW, 1e6))).by_field()
    assert row[FIELD_REALIZED_VOL_20].status == STATUS_VALID and row[FIELD_REALIZED_VOL_20].value == 0.0
    assert row[FIELD_IV_RV_RATIO].status == STATUS_INSUFFICIENT_HISTORY
    assert row[FIELD_IV_RV_RATIO].value is None


def test_a_valid_status_without_bars_is_a_caller_error():
    with pytest.raises(ValueError):
        mc.compute_iv_v1(SESSION, _chain(), None)
    with pytest.raises(ValueError):
        mc.compute_iv_v1(SESSION, _chain(), tuple(x[-50:] for x in _bars()))


def test_no_look_ahead_a_later_sessions_chain_cannot_change_the_row():
    """The calculator reads the chain it is HANDED for the session; the store build hands it only
    rows whose bar_date is the session. A later session's quotes on the same contracts are a
    different ChainQuotes and never reach this row -- pinned on the digest the build keys on."""
    a = _chain(vol=lambda d: 0.25)
    later = _chain(vol=lambda d: 0.60)
    row_a = mc.compute_iv_v1(SESSION, a, _bars()).by_field()
    row_b = mc.compute_iv_v1(SESSION, a, _bars()).by_field()
    assert row_a == row_b
    assert a.digest() != later.digest()


# --------------------------------------------------------------------------- readers
def test_a_reader_refuses_the_chain_profile_without_a_manifest():
    from ba2_common.core.market_condition_readers import (
        ChainProfileNeedsManifest, WindowMarketConditionReader,
    )

    with pytest.raises(ChainProfileNeedsManifest, match="ONLY from a published manifest"):
        WindowMarketConditionReader(IV_PROFILE)


def test_the_live_resolver_refuses_iv_and_keeps_the_ohlcv_reader(monkeypatch):
    from ba2_common.core import market_condition_live as live
    from ba2_common.core.market_condition_readers import LiveUnavailableMarketConditionReader

    class _Stub:
        def __init__(self, profile):
            self.profile = profile
            self.calc_version = PROFILES[profile].calc_version
            self.mapped_reader = None

        def observe(self, symbol, session):
            return None

    monkeypatch.setattr(live, "_fmp_cache_reader", lambda name, root, **kw: _Stub(name))
    resolver = live.resolver_for_profiles(("ohlcv-v1", IV_PROFILE))
    readers = resolver.reader.readers
    assert isinstance(readers[0], _Stub)
    assert isinstance(readers[1], LiveUnavailableMarketConditionReader)
    assert resolver.live_unavailable_profiles() == (IV_PROFILE,)
    # not reported as "unpinned research mode" -- it is a different, louder fault
    assert resolver.research_mode_profiles() == ("ohlcv-v1",)
    row = readers[1].observe("AAPL", SESSION).by_field()
    assert {o.status for o in row.values()} == {STATUS_NO_CONTEXT}
    assert all("NO option source" in o.reason for o in row.values())
    merged = resolver.reader.observe("AAPL", SESSION)
    assert merged.by_field()[FIELD_IV_RV_RATIO].status == STATUS_NO_CONTEXT
    assert merged.by_field()["underlying_adx_14"].status == STATUS_MISSING_SESSION


def test_an_iv_only_live_resolver_refuses_and_records_nothing():
    from ba2_common.core import market_condition_live as live
    from ba2_common.core.market_condition_readers import capturing_reader_for

    resolver = live.resolver_for_profiles((IV_PROFILE,))
    assert resolver.live_unavailable_profiles() == (IV_PROFILE,)
    assert resolver.research_mode_profiles() == ()

    class _Capture:
        def record_observation(self, **kw):
            raise AssertionError("nothing may be recorded for a refusing reader")

    reader, record = capturing_reader_for(resolver.reader, _Capture(), source_profile="x", timing_policy="y")
    assert reader is resolver.reader
    record("AAPL", SESSION)
    assert reader.observe("AAPL", SESSION).by_field()[FIELD_IV_TERM_SLOPE].status == STATUS_NO_CONTEXT


def test_a_live_gate_on_an_iv_field_never_passes():
    """Through the real condition class: a refused observation is unknown, and unknown never
    passes either comparison."""
    from datetime import datetime, timezone

    from ba2_common.core.market_condition_context import MarketConditionContext
    from ba2_common.core.market_condition_readers import LiveUnavailableMarketConditionReader
    from ba2_common.core.TradeConditions import market_condition_condition_class

    reader = LiveUnavailableMarketConditionReader(IV_PROFILE, mc.live_unavailable_reason(IV_PROFILE, "thetadata"))
    ctx = MarketConditionContext(
        decision_time=datetime(2024, 3, 4, 15, 0, tzinfo=timezone.utc), session_label=date(2024, 3, 4),
        prior_session=SESSION, source_profile="fmp-daily-split-adjusted-v1",
        timing_policy="prior_session_v1", calc_version=reader.calc_version, reader=reader, recorder=None)
    cls = market_condition_condition_class(FIELD_IV_RV_RATIO)
    from ba2_common.core import TradeConditions

    saved = TradeConditions.get_market_condition_context_resolver()
    TradeConditions.set_market_condition_context_resolver(lambda account, sym, rec: ctx)
    try:
        for op in ("<", ">"):
            cond = cls(None, "AAPL", None, operator_str=op, value=1.0)
            assert cond.evaluate() is False
            assert cond.last_status == STATUS_NO_CONTEXT
    finally:
        TradeConditions.set_market_condition_context_resolver(saved)


# --------------------------------------------------------------------------- live source binding (2026-11-15)
class _Expert:
    def __init__(self, settings):
        self.settings = settings


class _Resolver:
    def __init__(self, experts):
        self.experts = experts
        self.reads = []

    def get_expert_instance(self, iid):
        self.reads.append(iid)
        return self.experts[iid]


@pytest.fixture
def instances(monkeypatch):
    from ba2_common.core import instance_resolver
    from ba2_common.core import market_condition_live as live

    experts = {1: _Expert({"market_condition_profile": "ohlcv-v1"}),
               2: _Expert({"market_condition_profile": "iv-v1", "market_condition_option_source": "alphavantage"}),
               3: _Expert({"market_condition_profile": "iv-v1", "market_condition_option_source": "thetadata"}),
               4: _Expert({})}
    res = _Resolver(experts)
    monkeypatch.setattr(instance_resolver, "get_instance_resolver", lambda: res)
    monkeypatch.setattr(live, "certify_cache_root", lambda root=None: ("/x", type("R", (), {"consistent": True})()))
    built = []

    def fake_profiles(profiles, **kw):
        built.append((profiles, kw))
        return ("resolver", profiles, kw.get("option_source"))
    monkeypatch.setattr(live, "resolver_for_profiles", fake_profiles)
    return live.PerInstanceMarketConditionResolver(), built, res


def test_an_ohlcv_expert_never_reads_or_keys_on_an_option_source(instances, monkeypatch):
    """BACKWARD COMPATIBILITY: an expert without a chain profile is built exactly as before -- no
    option-source read, a (id, profiles) cache key, and no option_source argument."""
    pir, built, _res = instances
    calls = []
    monkeypatch.setattr(type(pir), "option_source_for", lambda self, iid: calls.append(iid))
    monkeypatch.setattr(type(pir), "refresh_coverage", lambda *a, **k: None, raising=False)
    from ba2_common.core import market_condition_live as live
    monkeypatch.setattr(live, "resolver_for_profiles",
                        lambda profiles, **kw: built.append((profiles, kw)) or type("L", (), {
                            "refresh_coverage": lambda self: None, "source_profile": "s"})())
    pir.resolver_for(1)
    assert calls == []
    assert list(pir._resolvers) == [(1, ("ohlcv-v1",))]
    assert "option_source" not in built[-1][1]
    assert pir.resolver_for(4) is None


def test_an_iv_expert_is_built_with_its_recorded_option_source(instances, monkeypatch):
    pir, built, _res = instances
    from ba2_common.core import market_condition_live as live
    monkeypatch.setattr(live, "resolver_for_profiles",
                        lambda profiles, **kw: built.append((profiles, kw)) or type("L", (), {
                            "refresh_coverage": lambda self: None, "source_profile": "s"})())
    pir.resolver_for(2)
    pir.resolver_for(3)
    assert [kw.get("option_source") for _p, kw in built] == ["alphavantage", "thetadata"]
    # the key also carries the live IV chain source (app setting) and the broker universe bound (None here)
    assert (2, ("iv-v1",), "alphavantage", None, None) in pir._resolvers
    assert (3, ("iv-v1",), "thetadata", None, None) in pir._resolvers


def test_a_thetadata_trained_iv_gate_is_refused_live_by_name():
    from ba2_common.core import market_condition_live as live
    from ba2_common.core.market_condition_readers import LiveUnavailableMarketConditionReader

    r = live.resolver_for_profiles((IV_PROFILE,), option_source="thetadata")
    assert isinstance(r.reader, LiveUnavailableMarketConditionReader)
    assert "trained on option source 'thetadata'" in r.reader.reason
    assert r.live_unavailable_profiles() == (IV_PROFILE,)


# --------------------------------------------------------------------------- review 2026-11-15
def test_the_decision_scope_logs_one_summary_per_refusal_kind(monkeypatch, caplog):
    import logging
    from datetime import datetime, timezone

    import ba2_common.core.TradeConditions as TC
    from ba2_common.core import market_condition_live as live
    from ba2_common.core.market_condition_iv_live import ChainLiveReader
    from ba2_common.core.types import ExpertEventType

    class _NoKey(ChainLiveReader):
        option_source = "alphavantage"

        def _evidence(self, symbol, session):
            return self._refusal(symbol, session, "alpha_vantage_api_key is not set", "no_key")

    reader = _NoKey(IV_PROFILE)
    saved = TC.get_market_condition_context_resolver()
    TC.set_market_condition_context_resolver(live.LiveMarketConditionResolver(IV_PROFILE, reader=reader))
    monkeypatch.setattr(live, "_replay_now", lambda: datetime(2025, 11, 17, 15, 0, tzinfo=timezone.utc))
    try:
        with caplog.at_level(logging.WARNING):
            with live.market_condition_decision_scope():
                for sym in ("AAA", "BBB", "CCC"):
                    for _ in range(2):           # a second leaf on the same symbol: no second read
                        TC.create_condition(ExpertEventType.N_UNDERLYING_IV_RV_RATIO, object(), sym, None,
                                            operator_str=">", value=1.0).evaluate()
    finally:
        TC.set_market_condition_context_resolver(saved)
    assert reader.refused == 3
    lines = [r.getMessage() for r in caplog.records if "refused this pass" in r.getMessage()]
    assert len(lines) == 1 and "no_key: 3 symbol(s)" in lines[0] and "AAA" in lines[0]


def test_an_old_option_source_spelling_is_read_under_its_current_name():
    from ba2_common.core.market_condition_store import manifest_option_source

    assert manifest_option_source({"option_source": "thetadata-eod-parquet-v1"}) == "thetadata"
    assert manifest_option_source({"option_source": "alphavantage"}) == "alphavantage"
    assert manifest_option_source({}) is None


def test_a_programming_error_in_a_live_read_propagates_in_enforce_mode(monkeypatch):
    """Enforce-mode convention: expected I/O / data faults become an unknown, a programming error
    propagates (the entry pass isolates it per recommendation)."""
    from ba2_common.core.market_condition_iv_live import ChainLiveReader

    class _Bug(ChainLiveReader):
        option_source = "alphavantage"

        def _evidence(self, symbol, session):
            raise TypeError("a bug")

    class _Io(ChainLiveReader):
        option_source = "alphavantage"

        def _evidence(self, symbol, session):
            raise OSError("disk")

    monkeypatch.setenv("BA2_ERROR_MODE", "enforce")
    with pytest.raises(TypeError):
        _Bug(IV_PROFILE).observe("AAA", SESSION)
    assert _Io(IV_PROFILE).observe("AAA", SESSION).by_field()[FIELD_IV_RV_RATIO].status == STATUS_NO_CONTEXT

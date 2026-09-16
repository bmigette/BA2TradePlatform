"""Backtest adapter for the market-condition gates (design 2026-09-15 sections 4, 4.1).

``AsOfPriceSource.window_before`` slices the columnar store; ``BacktestMarketConditionReader``
assembles/computes/memoises; ``BacktestMarketConditionResolver`` builds one frozen context per
simulated session; ``seam_wiring.install_backtest_market_conditions`` installs nothing for
profile ``none``.

Task 7 added the pinned snapshot: with ``market_condition_manifest`` in the config the reader
serves the published rows and calls no calculator at all; without it an OPTIMIZER config
(``_ga_trial``) is refused outright, and a research config computes on a miss after one warning.
"""
from __future__ import annotations

import sys
import threading
from datetime import date, datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from ba2_common.core import TradeConditions
from ba2_common.core import market_conditions as mc
from ba2_common.core.market_calendar import regular_sessions_ending_at
from ba2_common.core.market_condition_readers import MarketConditionVersionMismatch
from ba2_common.core.market_conditions import (
    OHLCV_V1,
    STATUS_INSUFFICIENT_HISTORY,
    STATUS_MISSING_SESSION,
    STATUS_VALID,
    WINDOW,
    FeatureRow,
    ProfileSpec,
)

from app.services.backtest import seam_wiring
from app.services.backtest.price_source import AsOfPriceSource

SESSION = date(2025, 6, 30)


def _rows(days, start=100.0):
    rng = np.random.default_rng(11)
    c = start * np.exp(np.cumsum(rng.normal(0, 0.012, len(days))))
    return [{"Date": d, "Open": float(x) * 1.001, "High": float(x) * 1.012, "Low": float(x) * 0.988,
             "Close": float(x), "Volume": 1e6} for d, x in zip(days, c)]


def _source(days_by_symbol):
    ps = AsOfPriceSource(ohlcv_provider=None, interval="1d")
    for sym, days in days_by_symbol.items():
        ps.load_bars(sym, _rows(days))
    return ps


@pytest.fixture
def ps():
    return _source({"AAA": regular_sessions_ending_at(date(2025, 7, 31), 200)})


@pytest.fixture(autouse=True)
def _restore_seam():
    saved = TradeConditions.get_market_condition_context_resolver()
    yield
    seam_wiring.clear_backtest_market_conditions()
    TradeConditions.set_market_condition_context_resolver(saved)


# --------------------------------------------------------------------------- window_before
def test_window_before_returns_exactly_n_bars_ending_at_the_session(ps):
    dates, o, h, l, c, v = ps.window_before("AAA", SESSION, WINDOW)
    assert len(dates) == WINDOW and all(len(a) == WINDOW for a in (o, h, l, c, v))
    assert dates.dtype == np.dtype("datetime64[D]")
    assert list(dates.astype(object)) == regular_sessions_ending_at(SESSION, WINDOW)
    assert c.dtype == np.float64 and not c.flags.writeable
    assert c[-1] == ps.close_at("AAA", datetime(2025, 6, 30))


def test_window_before_none_when_short_or_unknown(ps):
    first = regular_sessions_ending_at(date(2025, 7, 31), 200)[0]
    early = regular_sessions_ending_at(date(2025, 7, 31), 200)[WINDOW - 2]  # only 127 bars through it
    assert ps.window_before("AAA", early, WINDOW) is None
    assert ps.count_through("AAA", early) == WINDOW - 1
    assert ps.window_before("AAA", first, 1)[0][0] == np.datetime64(first)
    assert ps.window_before("ZZZ", SESSION, WINDOW) is None
    assert ps.count_through("ZZZ", SESSION) == 0


def test_window_before_on_a_non_session_date_ends_at_the_prior_bar(ps):
    dates, *_ = ps.window_before("AAA", date(2025, 6, 29), 3)  # a Sunday
    assert dates[-1] == np.datetime64("2025-06-27")


def test_window_before_refuses_an_intraday_store():
    with pytest.raises(ValueError, match="daily"):
        AsOfPriceSource(ohlcv_provider=None, interval="5m").window_before("AAA", SESSION, 5)


# --------------------------------------------------------------------------- reader
def _reader(ps):
    from app.services.backtest.market_condition_bt import BacktestMarketConditionReader
    return BacktestMarketConditionReader(ps, "ohlcv-v1")


def test_observe_memoises_one_compute_for_three_reads(ps, monkeypatch):
    calls = []
    real = mc.COMPUTE_BY_PROFILE["ohlcv-v1"]

    def spy(*arrays):
        calls.append(1)
        return real(*arrays)

    monkeypatch.setitem(mc.COMPUTE_BY_PROFILE, "ohlcv-v1", spy)
    reader = _reader(ps)
    rows = [reader.observe("AAA", SESSION) for _ in range(3)]
    assert len(calls) == 1 and reader.computed == 1
    assert rows[0] is rows[1] is rows[2]
    assert rows[0].by_field() is rows[1].by_field()
    assert all(o.status == STATUS_VALID for o in rows[0].by_field().values())


def test_observe_matches_the_pure_calculator(ps):
    dates, o, h, l, c, v = ps.window_before("AAA", SESSION, WINDOW)
    expected = mc.compute_market_conditions(o, h, l, c, v).to_feature_row()
    assert _reader(ps).observe("AAA", SESSION) == expected


def test_observe_statuses_for_short_missing_and_unknown():
    days = regular_sessions_ending_at(date(2025, 7, 31), 200)
    hole = regular_sessions_ending_at(SESSION, 40)[0]
    ps = _source({"YOUNG": days[-60:], "HOLE": [d for d in days if d != hole], "LATE": days[-5:]})
    reader = _reader(ps)
    status = lambda sym: {o.status for o in reader.observe(sym, SESSION).by_field().values()}  # noqa: E731
    assert status("YOUNG") == {STATUS_INSUFFICIENT_HISTORY}
    assert status("HOLE") == {STATUS_MISSING_SESSION}
    assert status("LATE") == {STATUS_INSUFFICIENT_HISTORY}
    assert reader.observe("NOPE", SESSION) is None


def test_calc_version_mismatch_raises(ps, monkeypatch):
    def wrong(*arrays):
        row = mc.compute_market_conditions(*arrays).to_feature_row()
        return FeatureRow(values=row.by_field(), calc_versions={f: "ohlcv-v1/calc-0" for f in row.by_field()})

    monkeypatch.setitem(mc.COMPUTE_BY_PROFILE, "ohlcv-v1", wrong)
    with pytest.raises(MarketConditionVersionMismatch):
        _reader(ps).observe("AAA", SESSION)


def test_registry_version_change_under_a_live_reader_raises(ps, monkeypatch):
    reader = _reader(ps)
    reader.observe("AAA", SESSION)
    monkeypatch.setitem(mc.PROFILES, "ohlcv-v1",
                        ProfileSpec(name="ohlcv-v1", calc_version="ohlcv-v1/calc-2", fields=OHLCV_V1.fields))
    with pytest.raises(MarketConditionVersionMismatch):
        reader.observe("AAA", SESSION)


# --------------------------------------------------------------------------- resolver
class _Account:
    def __init__(self, day):
        self.day = day

    def _as_of_date(self):
        return self.day


def test_resolver_builds_one_context_per_session(ps):
    from app.services.backtest.market_condition_bt import BacktestMarketConditionResolver

    resolver = BacktestMarketConditionResolver(_reader(ps))
    account = _Account(date(2025, 7, 1))
    a, b, c = (resolver(account, "AAA", None) for _ in range(3))
    assert a is b is c
    assert a.session_label == date(2025, 7, 1)
    assert a.prior_session == SESSION
    assert a.decision_time == datetime(2025, 7, 1, 20, 0, tzinfo=timezone.utc)
    assert a.calc_version == OHLCV_V1.calc_version
    assert a.source_profile == "fmp-daily-split-adjusted-v1"
    assert a.timing_policy == "prior_session_v1"

    account.day = date(2025, 7, 2)
    d = resolver(account, "AAA", None)
    assert d is not a and d.prior_session == date(2025, 7, 1)
    assert resolver(account, "BBB", None) is d


def test_resolver_non_session_date_is_no_context(ps):
    from app.services.backtest.market_condition_bt import BacktestMarketConditionResolver

    resolver = BacktestMarketConditionResolver(_reader(ps))
    assert resolver(_Account(date(2025, 7, 4)), "AAA", None) is None


def test_a_market_leaf_evaluates_through_the_installed_resolver(ps):
    seam_wiring.install_backtest_market_conditions({"market_condition_profile": "ohlcv-v1"}, ps)
    from ba2_common.core.types import ExpertEventType

    account = SimpleNamespace(_as_of_date=lambda: date(2025, 7, 1))
    row = _reader(ps).observe("AAA", SESSION)
    adx = row.by_field()["underlying_adx_14"].value
    leaf = TradeConditions.create_condition(ExpertEventType.N_UNDERLYING_ADX, account, "AAA", None,
                                            operator_str=">", value=adx - 1.0)
    assert leaf.evaluate() is True and leaf.calculated_value == adx


# --------------------------------------------------------------------------- wiring
def test_profile_none_installs_nothing_and_imports_nothing(ps, monkeypatch):
    TradeConditions.set_market_condition_context_resolver(None)
    monkeypatch.delitem(sys.modules, "app.services.backtest.market_condition_bt", raising=False)
    assert seam_wiring.install_backtest_market_conditions({"market_condition_profile": "none"}, ps) is None
    assert TradeConditions.get_market_condition_context_resolver() is None
    assert "app.services.backtest.market_condition_bt" not in sys.modules


def test_profile_key_is_required(ps):
    with pytest.raises(KeyError):
        seam_wiring.install_backtest_market_conditions({}, ps)


def test_unknown_profile_raises(ps):
    with pytest.raises(ValueError, match="not registered"):
        seam_wiring.install_backtest_market_conditions({"market_condition_profile": "ohlcv-v9"}, ps)


def test_profile_installs_a_thread_local_run_resolver(ps):
    resolver = seam_wiring.install_backtest_market_conditions({"market_condition_profile": "ohlcv-v1"}, ps)
    assert resolver is not None
    dispatch = TradeConditions.get_market_condition_context_resolver()
    assert dispatch is seam_wiring._dispatch_market_condition_context
    account = _Account(date(2025, 7, 1))
    assert dispatch(account, "AAA", None) is resolver(account, "AAA", None)

    seen = []
    t = threading.Thread(target=lambda: seen.append(dispatch(account, "AAA", None)))
    t.start()
    t.join()
    assert seen == [None], "another thread's run must not see this run's resolver"

    seam_wiring.clear_backtest_market_conditions()
    assert dispatch(account, "AAA", None) is None


def test_profile_none_with_market_leaves_in_the_rules_raises(ps):
    import json

    tree = {"op": "AND", "children": [
        {"id": "o_lc-entry-iv", "field": "iv_rank", "op": "<", "value": 30},
        {"id": "o_lc-market-adx", "field": "underlying_adx_14", "op": "<", "value": 25},
    ]}
    with pytest.raises(ValueError, match="o_lc-market-adx"):
        seam_wiring.install_backtest_market_conditions(
            {"market_condition_profile": "none", "entry_rules": [{"conditions": tree}]}, ps)
    # A tree held as a JSON string inside expert settings is found too.
    cfg = {"market_condition_profile": "none",
           "experts": [{"class": "X", "settings": {"entry_condition": json.dumps(tree)}}]}
    with pytest.raises(ValueError, match="o_lc-market-adx"):
        seam_wiring.install_backtest_market_conditions(cfg, ps)
    # A leaf without an id is named by its config path.
    no_id = {"market_condition_profile": "none",
             "exit_rules": [{"field": "underlying_realized_vol_ratio_5_20", "op": ">", "value": 1}]}
    with pytest.raises(ValueError, match=r"config\.exit_rules\[0\]"):
        seam_wiring.install_backtest_market_conditions(no_id, ps)


def test_profile_none_without_market_leaves_is_fine(ps):
    cfg = {"market_condition_profile": "none", "enabled_instruments": ["AAA"],
           "entry_rules": [{"conditions": {"id": "x", "field": "iv_rank", "op": "<", "value": 30}}],
           "experts": [{"class": "X", "settings": {"note": "[not json", "tree": "{\"field\": \"iv_rank\"}"}}]}
    assert seam_wiring.install_backtest_market_conditions(cfg, ps) is None
    assert seam_wiring.market_condition_leaves_in(cfg) == []


# --------------------------------------------------------------------- pinned manifest (Task 7)
def _publish_manifest(root, symbol="AAA", sessions=(date(2025, 6, 27), SESSION)):
    """A tiny published snapshot: negative rows only (their VALUES do not matter here -- what is
    being pinned is that they come from the store and not from a calculator)."""
    from ba2_common.core.market_condition_store import MarketConditionStore
    from ba2_common.core.market_conditions import PROFILES

    profile = PROFILES["ohlcv-v1"]
    fields = [f.name for f in profile.fields]
    store = MarketConditionStore(root)
    rows = [{"session": s, "values": [None] * len(fields),
             "status": [STATUS_INSUFFICIENT_HISTORY] * len(fields),
             "reasons": ["published row"] * len(fields),
             "window_digest": "sha256:" + "0" * 64, "raw_shard_ref": "",
             "raw_row_lo": 0, "raw_row_hi": 0} for s in sessions]
    entry, _ = store.write_feature_object(profile, symbol, rows)
    manifest = store.make_manifest(
        profile, source_profile="fmp-daily-split-adjusted-v1", timing_policy="prior_session_v1",
        objects=[entry], raw_objects=[], coverage={symbol: {"rows": len(rows)}},
        universe=[symbol], sessions=list(sessions), window_start=sessions[0],
        window_end=sessions[-1])
    return store, store.write_manifest(manifest)


def test_a_pinned_manifest_serves_published_rows_and_never_computes(ps, tmp_path, monkeypatch):
    import ba2_common.config as bc

    store, digest = _publish_manifest(tmp_path / "cache")
    monkeypatch.setattr(bc, "CACHE_FOLDER", str(store.cache_root))

    def boom(*a, **kw):
        raise AssertionError("a pinned run must not call the calculator")

    monkeypatch.setattr(mc, "compute_market_conditions", boom)
    monkeypatch.setitem(mc.COMPUTE_BY_PROFILE, "ohlcv-v1", boom)

    resolver = seam_wiring.install_backtest_market_conditions(
        {"market_condition_profile": "ohlcv-v1", "market_condition_manifest": digest,
         "_ga_trial": True}, ps)
    row = resolver.reader.observe("AAA", SESSION)
    assert row is not None
    assert {o.status for o in row.by_field().values()} == {STATUS_INSUFFICIENT_HISTORY}
    assert {o.reason for o in row.by_field().values()} == {"published row"}
    assert resolver.reader.computed == 0 and resolver.reader.mapped_rows == 1
    # A symbol the snapshot does not carry is a miss, never a fallback computation.
    assert resolver.reader.observe("BBB", SESSION) is None
    assert resolver.reader.computed == 0


def test_a_ga_trial_without_a_pinned_manifest_is_refused(ps):
    with pytest.raises(ValueError, match="pins no manifest for it"):
        seam_wiring.install_backtest_market_conditions(
            {"market_condition_profile": "ohlcv-v1", "_ga_trial": True}, ps)
    # ... and PER PROFILE: one profile pinned, the other not, is still a refusal NAMING the
    # profile whose snapshot is missing -- half a gated genome is a zero-trade fitness that
    # looks like a verdict.
    with pytest.raises(ValueError, match="ta-structure-v1"):
        seam_wiring.install_backtest_market_conditions(
            {"market_condition_profiles": ["ta-structure-v1", "ohlcv-v1"],
             "market_condition_manifests": {"ohlcv-v1": "sha256:" + "a" * 64},
             "_ga_trial": True}, ps)


def test_research_mode_computes_but_says_so_once(ps, monkeypatch):
    from ba2_common.core import market_condition_readers as readers

    monkeypatch.setattr(readers, "_RESEARCH_WARNED", set())
    warned = []
    # seam_wiring imports the helper INSIDE the function, so patching it on the module it lives
    # in is what the call actually resolves.
    monkeypatch.setattr(readers, "warn_research_mode",
                        lambda profile, where: warned.append((profile, where)) or True)
    resolver = seam_wiring.install_backtest_market_conditions(
        {"market_condition_profile": "ohlcv-v1"}, ps)
    assert resolver.reader.observe("AAA", SESSION) is not None
    assert resolver.reader.computed == 1
    assert warned == [("ohlcv-v1", "backtest reader")]


def test_the_once_per_process_warning_is_actually_once(monkeypatch):
    from ba2_common.core import market_condition_readers as readers

    monkeypatch.setattr(readers, "_RESEARCH_WARNED", set())
    assert readers.warn_research_mode("ohlcv-v1", "backtest reader") is True
    assert readers.warn_research_mode("ohlcv-v1", "backtest reader") is False


def test_a_ga_trial_whose_universe_the_snapshot_does_not_cover_is_refused(ps, tmp_path,
                                                                         monkeypatch):
    """The real snapshot covers 85 of the 98-symbol option universe (13 need a provider re-fetch
    first). For an uncovered symbol every gate reads missing_session for the whole run: it never
    enters, and the genome scores as though its strategy simply did not fire there. A
    feature-cache miss must not become a property of the fitness landscape."""
    import ba2_common.config as bc

    store, digest = _publish_manifest(tmp_path / "cache", symbol="AAA")
    monkeypatch.setattr(bc, "CACHE_FOLDER", str(store.cache_root))
    cfg = {"market_condition_profile": "ohlcv-v1", "market_condition_manifest": digest,
           "_ga_trial": True, "enabled_instruments": ["AAA", "BBB", "CCC"]}

    with pytest.raises(ValueError) as exc:
        seam_wiring.install_backtest_market_conditions(cfg, ps)
    message = str(exc.value)
    assert "does not cover" in message and "BBB" in message and "CCC" in message
    assert digest in message


def test_research_mode_reports_the_gap_and_proceeds(ps, tmp_path, monkeypatch, caplog):
    import logging

    import ba2_common.config as bc

    store, digest = _publish_manifest(tmp_path / "cache", symbol="AAA")
    monkeypatch.setattr(bc, "CACHE_FOLDER", str(store.cache_root))
    cfg = {"market_condition_profile": "ohlcv-v1", "market_condition_manifest": digest,
           "enabled_instruments": ["AAA", "BBB"]}

    with caplog.at_level(logging.ERROR, logger="app.services.backtest.seam_wiring"):
        resolver = seam_wiring.install_backtest_market_conditions(cfg, ps)
    assert resolver is not None
    assert any("does not cover" in r.message and "BBB" in r.message for r in caplog.records)
    # ...and the covered symbol still serves its published row.
    assert resolver.reader.observe("AAA", SESSION) is not None


def test_a_fully_covered_universe_passes_and_an_empty_one_is_not_checked(ps, tmp_path,
                                                                        monkeypatch):
    import ba2_common.config as bc

    store, digest = _publish_manifest(tmp_path / "cache", symbol="AAA")
    monkeypatch.setattr(bc, "CACHE_FOLDER", str(store.cache_root))
    base = {"market_condition_profile": "ohlcv-v1", "market_condition_manifest": digest,
            "_ga_trial": True}

    assert seam_wiring.install_backtest_market_conditions(
        {**base, "enabled_instruments": ["AAA"]}, ps) is not None
    # Case-insensitively, the way instrument names reach a config.
    assert seam_wiring.install_backtest_market_conditions(
        {**base, "enabled_instruments": ["aaa"]}, ps) is not None
    # A config with no universe recorded has nothing to compare against -- not a silent pass for
    # a real gap, just the absence of the question.
    assert seam_wiring.install_backtest_market_conditions(base, ps) is not None
    assert seam_wiring.market_condition_universe({"enabled_instruments": ["b", "a"]}) == ["A", "B"]


# --------------------------------------------------------------------------- plural pins (Task 10)
def test_the_pins_are_read_from_either_shape():
    """ONE decoder for the plural keys and the legacy singular pair. Every optimization_config
    persisted before Task 10 carries the pair, and re-running one of those genomes has to work."""
    pins = seam_wiring.market_condition_pins
    assert pins({"market_condition_profile": "none"}) == ([], {})
    assert pins({"market_condition_profile": "ohlcv-v1",
                 "market_condition_manifest": "d1"}) == (["ohlcv-v1"], {"ohlcv-v1": "d1"})
    assert pins({"market_condition_profiles": ["ohlcv-v1", "ta-structure-v1"],
                 "market_condition_manifests": {"ohlcv-v1": "d1", "ta-structure-v1": "d2"}}) == (
        ["ohlcv-v1", "ta-structure-v1"], {"ohlcv-v1": "d1", "ta-structure-v1": "d2"})
    # a comma string is the CLI spelling, accepted so a config written from argv round-trips
    assert pins({"market_condition_profiles": "ohlcv-v1,ta-structure-v1"})[0] == [
        "ohlcv-v1", "ta-structure-v1"]
    # no key at all: required by default (the seam is handed a NORMALISED config) ...
    with pytest.raises(KeyError):
        pins({})
    # ... and optional for the callers that read a RAW stored config
    assert pins({}, required=False) == ([], {})


@pytest.mark.parametrize("config,message", [
    ({"market_condition_profiles": ["ohlcv-v1"], "market_condition_profile": "ta-structure-v1"},
     "they disagree"),
    # "none" is a contradiction too: one key says the gates are off and the other names a
    # profile, and the quiet reading of that is a run that gates without a word.
    ({"market_condition_profiles": ["ohlcv-v1"], "market_condition_profile": "none"},
     "they disagree"),
    ({"market_condition_profiles": ["ohlcv-v1", "ohlcv-v1"]}, "repeats a profile"),
    ({"market_condition_profiles": ["none", "ohlcv-v1"]}, "mixes"),
    ({"market_condition_profiles": ["nope-v1"]}, "not registered"),
    ({"market_condition_profiles": ["ohlcv-v1", "ta-structure-v1"],
      "market_condition_manifest": "d1"}, "A manifest names the ONE profile"),
    ({"market_condition_profiles": ["ohlcv-v1"],
      "market_condition_manifests": {"ta-structure-v1": "d2"}}, "the run does not use"),
])
def test_a_contradictory_pin_is_refused_rather_than_half_applied(config, message):
    with pytest.raises(ValueError, match=message):
        seam_wiring.market_condition_pins(config)


def test_two_profiles_install_one_reader_each_behind_one_composite(ps):
    from ba2_common.core.market_condition_readers import CompositeMarketConditionReader

    resolver = seam_wiring.install_backtest_market_conditions(
        {"market_condition_profiles": ["ohlcv-v1", "ta-structure-v1"]}, ps)
    reader = resolver.reader
    assert isinstance(reader, CompositeMarketConditionReader)
    assert [r.profile for r in reader.readers] == ["ohlcv-v1", "ta-structure-v1"]
    assert all(r._ps is ps for r in reader.readers)
    seam_wiring.clear_backtest_market_conditions()


def test_one_profile_installs_the_reader_itself_unwrapped(ps):
    """A single-profile run is byte-identical to what it was before the widening."""
    from app.services.backtest.market_condition_bt import BacktestMarketConditionReader

    resolver = seam_wiring.install_backtest_market_conditions(
        {"market_condition_profiles": ["ohlcv-v1"]}, ps)
    assert type(resolver.reader) is BacktestMarketConditionReader
    assert resolver.reader.profile == "ohlcv-v1"
    seam_wiring.clear_backtest_market_conditions()


# ------------------------------------------------------- the profile is an expert setting (T12)
def _spec(profile, cls="FMPRating"):
    return {"class": cls, "settings": {"market_condition_profile": profile}}


def test_the_profiles_come_from_the_expert_setting():
    """THE canonical shape since Task 12: no config key at all, just the setting live also reads.

    ``required=True`` is satisfied by the setting -- it is a shape, not an absence -- so the seam
    does not mistake a setting-only config for one that skipped normalisation.
    """
    pins = seam_wiring.market_condition_pins
    assert pins({"experts": [_spec("ohlcv-v1")]}) == (["ohlcv-v1"], {"ohlcv-v1": None})
    assert pins({"experts": [_spec("ohlcv-v1,ta-structure-v1")],
                 "market_condition_manifests": {"ohlcv-v1": "d1", "ta-structure-v1": "d2"}}) == (
        ["ohlcv-v1", "ta-structure-v1"], {"ohlcv-v1": "d1", "ta-structure-v1": "d2"})
    # An EXPLICIT empty setting is a statement ("this expert is not gated"), not an absence.
    assert pins({"experts": [_spec("")]}) == ([], {})


def test_two_experts_settings_are_unioned_in_first_appearance_order():
    """One run, two experts, one reader set: the union serves each expert exactly its own fields
    (every field belongs to exactly one profile, so nothing else changes hands)."""
    pins = seam_wiring.market_condition_pins
    assert pins({"experts": [_spec("ta-structure-v1"), _spec("ohlcv-v1", "FMPRatingB"),
                             _spec("ta-structure-v1", "FMPRatingC")]})[0] == [
        "ta-structure-v1", "ohlcv-v1"]


def test_an_unregistered_or_repeated_setting_is_refused_by_the_shared_parser():
    with pytest.raises(ValueError, match="not a registered"):
        seam_wiring.market_condition_pins({"experts": [_spec("nope-v1")]})
    with pytest.raises(ValueError, match="repeats"):
        seam_wiring.market_condition_pins({"experts": [_spec("ohlcv-v1,ohlcv-v1")]})


def test_a_setting_that_contradicts_the_config_key_is_refused():
    """The failure this task exists to prevent, in its backtest half: one key says the run is
    gated and the other says it is not, and the setting is the half that also reaches LIVE."""
    pins = seam_wiring.market_condition_pins
    with pytest.raises(ValueError, match="setting names"):
        pins({"experts": [_spec("")], "market_condition_profiles": ["ohlcv-v1"]})
    with pytest.raises(ValueError, match="setting names"):
        pins({"experts": [_spec("ohlcv-v1")], "market_condition_profiles": []})
    with pytest.raises(ValueError, match="setting names"):
        pins({"experts": [_spec("ohlcv-v1")], "market_condition_profile": "ta-structure-v1"})
    # Agreement, in each of the two config shapes, is fine.
    assert pins({"experts": [_spec("ohlcv-v1")],
                 "market_condition_profiles": ["ohlcv-v1"]})[0] == ["ohlcv-v1"]
    assert pins({"experts": [_spec("ohlcv-v1")],
                 "market_condition_profile": "ohlcv-v1"})[0] == ["ohlcv-v1"]


def test_a_legacy_only_persisted_config_still_resolves():
    """No setting anywhere -- every config persisted before Task 12. Read, never refused."""
    pins = seam_wiring.market_condition_pins
    assert pins({"experts": [{"class": "FMPRating", "settings": {}}],
                 "market_condition_profile": "ohlcv-v1",
                 "market_condition_manifest": "d1"}) == (["ohlcv-v1"], {"ohlcv-v1": "d1"})
    assert pins({"experts": ["FMPRating"], "market_condition_profiles": ["ohlcv-v1"]})[0] == [
        "ohlcv-v1"]
    with pytest.raises(KeyError):
        pins({"experts": [{"class": "FMPRating", "settings": {}}]})


def test_the_setting_alone_installs_the_readers(ps):
    """End to end: a config carrying ONLY the setting installs the same resolver the config key
    would have -- that is what makes the setting safe to be the authority."""
    resolver = seam_wiring.install_backtest_market_conditions(
        {"experts": [_spec("ohlcv-v1,ta-structure-v1")]}, ps)
    assert [r.profile for r in resolver.reader.readers] == ["ohlcv-v1", "ta-structure-v1"]
    seam_wiring.clear_backtest_market_conditions()


def test_the_setting_survives_the_trial_config_whitelist():
    """``_build_daily_trial_config`` rebuilds the config KEY BY KEY, so a run-level knob missing
    from it is inert. The setting escapes that trap by riding inside the expert SETTINGS dict,
    which the builder copies wholesale -- pinned here because it is the reason the profile was
    made a setting rather than another run-level key."""
    from app.services.strategy_optimization_handler import _build_daily_trial_config

    cfg = _build_daily_trial_config(
        {"backtest_id": "mc", "start_date": "2024-02-01", "end_date": "2024-06-01",
         "enabled_instruments": ["AAA"], "experts": [_spec("ohlcv-v1")],
         "initial_capital": 20_000.0, "account_settings": {}, "warmup_days": 0, "seed": 1,
         "market_condition_manifests": {"ohlcv-v1": "d1"}}, {})
    assert cfg["experts"][0]["settings"]["market_condition_profile"] == "ohlcv-v1"
    # ... and the derived keys are written out too, so every stored-config consumer still reads
    # a resolved list (and the two are checked against each other on the way back in).
    assert cfg["market_condition_profiles"] == ["ohlcv-v1"]
    assert cfg["market_condition_manifests"] == {"ohlcv-v1": "d1"}
    assert seam_wiring.market_condition_pins(cfg) == (["ohlcv-v1"], {"ohlcv-v1": "d1"})


def test_a_manifestless_setting_still_fails_an_optimizer_trial(ps):
    """Task 10 behaviour, reached through the setting: a search may not compute on a miss."""
    with pytest.raises(ValueError, match="pins no manifest"):
        seam_wiring.install_backtest_market_conditions(
            {"experts": [_spec("ohlcv-v1")], "_ga_trial": True}, ps)

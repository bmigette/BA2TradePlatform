"""MLForecast expert: gene validation, store-alias resolution, entry mapping and the held-position flip, against a
temporary forecast store and fake providers (no DB, no torch)."""
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from ba2_common.core.ml_forecast import mapping, ranking, store
from ba2_common.core.ml_forecast.forecast_row import ForecastRow
from ba2_common.core.ml_forecast.model_card import ModelCard, model_key
from ba2_common.core.types import AnalysisUseCase, OrderRecommendation
import importlib

from ba2_experts.MLForecast import MLForecast, mapping_genes, resolve_model_key

mlf_mod = importlib.import_module("ba2_experts.MLForecast")   # the module (the package re-exports the class)

SETTINGS = {"model_key": "timesfm3-ctx512", "horizon": 10, "min_edge_pct": 1.0, "min_prob": 60.0,
            "max_uncertainty": 6.0, "tp_quantile": 70.0, "sl_quantile": 30.0, "exit_flip_prob": 60.0, "rank_top_n": 0,
            "enable_sell": False}
CARD = ModelCard(model_id="google/timesfm-3.0-pytorch", family="timesfm", version="3.0.2", input_fields=("Close",),
                 timeframe="1d", context_length=512, past_only_covariates=(), past_future_covariates=(),
                 horizons=(5, 10, 20), scaler=None, output_type="deciles", pretraining_cutoff=None,
                 licence="timesfm-non-commercial-license-v1.0", licence_commercial_ok=False)
BULL = (-0.01, 0.0, 0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05)     # q50 +2%, P(up) ~0.8
BEAR = tuple(-x for x in reversed(BULL))


@pytest.fixture
def cache(tmp_path, monkeypatch):
    from ba2_common import config
    monkeypatch.setattr(config, "CACHE_FOLDER", str(tmp_path))
    mlf_mod._alias_memo.clear()
    store.reset_cache()
    key = model_key(CARD, ())
    rows = [ForecastRow("AAPL", date(2025, 3, 3), {h: BULL for h in (5, 10, 20)}),
            ForecastRow("AAPL", date(2025, 3, 4), {h: BEAR for h in (5, 10, 20)})]
    store.write_rows(key, CARD, {"card_id": "timesfm3-ctx512"}, rows, (), tmp_path)
    yield key
    store.reset_cache()


class _Ohlcv:
    def get_ohlcv_data(self, symbol, start_date, end_date, lookback_days, interval):
        d = pd.bdate_range("2025-01-02", "2025-03-04")
        c = np.linspace(100, 110, len(d))
        return pd.DataFrame({"Date": d.tz_localize("UTC"), "Open": c, "High": c + 1.0, "Low": c - 1.0, "Close": c,
                             "Volume": 1e6})


class _Providers:
    def ohlcv(self):
        return _Ohlcv()

    def price_at_date(self, symbol, as_of):
        return 110.0


def _expert():
    e = MLForecast.__new__(MLForecast)
    e._skips_logged = None
    return e


def _at(d):
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def test_alias_resolves_from_store_meta(cache):
    assert resolve_model_key("timesfm3-ctx512") == cache
    assert resolve_model_key(cache) == cache
    with pytest.raises(ValueError, match="matches 0 stores"):
        resolve_model_key("timesfm3-ctx2048")


def test_genes_validate_without_defaults():
    assert mapping_genes(SETTINGS).horizon == 10
    with pytest.raises(ValueError, match="horizon"):
        mapping_genes({**SETTINGS, "horizon": 7})
    bad = dict(SETTINGS)
    del bad["min_prob"]
    with pytest.raises(ValueError, match="Missing setting 'min_prob'"):
        mapping_genes(bad)


def test_entry_buy_carries_tp_distance_as_expected_profit(cache):
    rec = _expert()._analyze("AAPL", _Providers(), SETTINGS, _at(date(2025, 3, 3)), "daily_expert")
    assert rec.signal == OrderRecommendation.BUY
    assert rec.raw_outputs["forecast_session"] == "2025-03-03"
    # tp_quantile 70 -> +3% of the decision price 110
    assert rec.target_price == pytest.approx(110.0 * 1.03, rel=1e-6)
    assert rec.expected_profit_percent == pytest.approx(3.0, rel=1e-5)
    assert 1 <= rec.confidence <= 95


def test_row_of_the_decision_day_is_visible_on_the_daily_clock_only_from_that_day(cache):
    # 2025-03-04 (bearish row) decides on the bearish row, not on the 03-03 bullish one
    rec = _expert()._analyze("AAPL", _Providers(), SETTINGS, _at(date(2025, 3, 4)), "daily_expert")
    assert rec.signal == OrderRecommendation.HOLD and "sell" in rec.details.lower()


def test_held_position_flips_on_the_bearish_row(cache):
    rec = _expert()._analyze("AAPL", _Providers(), SETTINGS, _at(date(2025, 3, 4)), AnalysisUseCase.OPEN_POSITIONS)
    assert rec.signal == OrderRecommendation.SELL
    rec = _expert()._analyze("AAPL", _Providers(), SETTINGS, _at(date(2025, 3, 3)), AnalysisUseCase.OPEN_POSITIONS)
    assert rec.signal == OrderRecommendation.BUY      # bullish p >= flip: a held long just stays (no exit)


def test_missing_row_is_a_skip_never_a_default(cache):
    rec = _expert()._analyze("AAPL", _Providers(), SETTINGS, _at(date(2025, 2, 1)), "daily_expert")
    assert rec.skip and rec.skip_reason == "no_forecast"


def test_class_store_with_horizon_zero_uses_the_store_horizon(tmp_path, monkeypatch):
    from ba2_common import config
    monkeypatch.setattr(config, "CACHE_FOLDER", str(tmp_path))
    mlf_mod._alias_memo.clear()
    store.reset_cache()
    card = ModelCard(model_id="ba2/custom", family="tsai", version="1", input_fields=("panel",), timeframe="1h",
                     context_length=64, past_only_covariates=(), past_future_covariates=(), horizons=(3,), scaler=None,
                     output_type="class_probs", pretraining_cutoff=None, licence="internal", licence_commercial_ok=True)
    key = model_key(card, ())
    cf = ((0.2, -0.03), (0.3, 0.002), (0.5, 0.03))
    store.write_rows(key, card, {"card_id": "custom-x"}, [ForecastRow("AAPL", date(2025, 3, 3), {}, class_probs={3: cf})],
                     (), tmp_path)
    s = {**SETTINGS, "model_key": "custom-x", "horizon": 0}
    with pytest.raises(ValueError, match="needs the resolved model key"):
        mapping_genes(s)
    assert mapping_genes(s, key).horizon == 3
    rec = _expert()._analyze("AAPL", _Providers(), {**s, "min_edge_pct": 0.5}, _at(date(2025, 3, 3)), "daily_expert")
    assert rec.signal == OrderRecommendation.BUY and rec.raw_outputs["horizon"] == 3 and "classes" in rec.raw_outputs
    assert rec.target_price == pytest.approx(110.0 * 1.03, rel=1e-5)
    store.reset_cache()


def test_horizon_zero_refuses_a_multi_horizon_store(cache):
    with pytest.raises(ValueError, match="single-horizon"):
        mapping_genes({**SETTINGS, "horizon": 0}, cache)


# ---------------------------------------------------------------- ranking mode (rank_top_n)
UNIV = "AAA,BBB,CCC,DDD,EEE,ZZZ"          # ZZZ's only row is stale at the cutoff


def _dec_c(c):
    """Deciles c + 0.01 * (i - 4): q50 = c, P(up) = 0.5 + 10 * c (0.5 .. 0.9) until the 0.95 clamp (c >= 0.05)."""
    return tuple(c + 0.01 * (i - 4) for i in range(9))


def _write_rank_store(tmp_path, with_future=True, extra=()):
    key = model_key(CARD, ())
    d = date(2025, 3, 3)
    rows = []
    for sym, c in (("EEE", 0.0), ("DDD", 0.01), ("CCC", 0.02), ("BBB", 0.03), ("AAA", 0.04)):
        rows.append(ForecastRow(sym, d, {h: _dec_c(c) for h in (5, 10, 20)}))
    if with_future:
        # after the 03-03 cutoff: the most bullish row ever for EEE, a bearish one for AAA
        rows.append(ForecastRow("EEE", date(2025, 3, 10), {h: _dec_c(0.06) for h in (5, 10, 20)}))
        rows.append(ForecastRow("AAA", date(2025, 3, 10), {h: _dec_c(-0.06) for h in (5, 10, 20)}))
    # stale at the cutoff: its only row is 10 days old (> MAX_STALE_CALENDAR_DAYS)
    rows.append(ForecastRow("ZZZ", date(2025, 2, 21), {h: _dec_c(0.05) for h in (5, 10, 20)}))
    for sym, c in extra:
        rows.append(ForecastRow(sym, d, {h: _dec_c(c) for h in (5, 10, 20)}))
    store.write_rows(key, CARD, {"card_id": "timesfm3-ctx512"}, rows, (), tmp_path)
    return key


@pytest.fixture
def rank_cache(tmp_path, monkeypatch):
    from ba2_common import config
    monkeypatch.setattr(config, "CACHE_FOLDER", str(tmp_path))
    mlf_mod._alias_memo.clear()
    ranking.reset_panels()
    store.reset_cache()
    yield _write_rank_store(tmp_path)
    store.reset_cache()
    ranking.reset_panels()


def _rs(**over):
    return {**SETTINGS, "rank_top_n": 2, "rank_universe": UNIV, "min_prob": 55.0, "min_edge_pct": 0.5, **over}


def _signals(settings, when=date(2025, 3, 3), use_case="daily_expert", syms=("AAA", "BBB", "CCC", "DDD", "EEE")):
    return {s: _expert()._analyze(s, _Providers(), settings, _at(when), use_case) for s in syms}


def test_rank_top_n_is_validated():
    assert mlf_mod.rank_top_n(SETTINGS) == 0
    assert mlf_mod.rank_top_n({**SETTINGS, "rank_top_n": 3}) == 3
    assert mlf_mod.rank_top_n({**SETTINGS, "rank_top_n": 3.0}) == 3
    for bad in (-1, 1.5, True, "3", None, float("nan")):
        with pytest.raises(ValueError, match="rank_top_n"):
            mlf_mod.rank_top_n({**SETTINGS, "rank_top_n": bad})
    d = MLForecast.get_settings_definitions()
    assert d["rank_top_n"]["default"] == 0 and d["rank_universe"]["default"] == ""
    assert "rank_top_n" in MLForecast._SETTING_KEYS and "rank_universe" in MLForecast._SETTING_KEYS


def test_absent_ranking_settings_mean_ranking_off(rank_cache):
    """A persisted instance that predates the ranking settings reproduces the pre-ranking behaviour (the only default)."""
    old = {k: v for k, v in SETTINGS.items() if k != "rank_top_n"}
    assert mlf_mod.rank_top_n(old) == 0 and mlf_mod.rank_universe(old) == ()
    rec = _expert()._analyze("AAA", _Providers(), {**old, "min_prob": 55.0}, _at(date(2025, 3, 3)), "daily_expert")
    assert rec.signal == OrderRecommendation.BUY and "rank" not in rec.raw_outputs


def test_rank_universe_is_parsed_sorted_and_deduplicated():
    s = {**SETTINGS, "rank_universe": "ccc, AAA,bbb,AAA"}
    assert mlf_mod.rank_universe(s) == ("AAA", "BBB", "CCC")
    assert mlf_mod.rank_universe({**SETTINGS, "rank_universe": ["BBB", "aaa"]}) == ("AAA", "BBB")
    assert mlf_mod.rank_universe({**SETTINGS, "rank_universe": ""}) == ()
    for bad in (5, ["AAA", 1], None, "../x"):
        with pytest.raises(ValueError):
            mlf_mod.rank_universe({**SETTINGS, "rank_universe": bad})
    a = ranking.universe_id(["AAA", "BBB"])
    assert a == ranking.universe_id(["bbb", "AAA", "BBB"]) and len(a) == 16 and a != ranking.universe_id(["AAA"])
    import hashlib
    assert a == hashlib.sha256(b"AAA,BBB").hexdigest()[:16]


def test_ranking_mode_needs_an_explicit_universe_containing_the_symbol(rank_cache):
    for u in ("", None):
        s = {**SETTINGS, "rank_top_n": 2}
        if u is not None:
            s["rank_universe"] = u
        with pytest.raises(ValueError, match="rank_universe"):
            _expert()._analyze("AAA", _Providers(), s, _at(date(2025, 3, 3)), "daily_expert")
    with pytest.raises(ValueError, match="not in the 'rank_universe'"):
        _expert()._analyze("AAA", _Providers(), _rs(rank_universe="BBB,CCC"), _at(date(2025, 3, 3)), "daily_expert")


def test_rank_mode_is_long_only(rank_cache):
    with pytest.raises(ValueError, match="long-only"):
        _expert()._analyze("AAA", _Providers(), _rs(enable_sell=True), _at(date(2025, 3, 3)), "daily_expert")


def test_ranking_off_ignores_the_rank(rank_cache):
    recs = _signals({**SETTINGS, "rank_top_n": 0, "min_prob": 55.0, "min_edge_pct": 0.5})
    assert [recs[s].signal for s in "AAA BBB CCC DDD EEE".split()] == [OrderRecommendation.BUY] * 4 + [OrderRecommendation.HOLD]
    assert "rank" not in recs["AAA"].raw_outputs


def test_ranking_buys_exactly_the_top_n_of_the_fresh_rows(rank_cache):
    recs = _signals(_rs())
    assert recs["AAA"].signal == OrderRecommendation.BUY and recs["BBB"].signal == OrderRecommendation.BUY
    for sym, k in (("CCC", 3), ("DDD", 4), ("EEE", 5)):
        assert recs[sym].signal == OrderRecommendation.HOLD
        assert f"rank {k} of 5" in recs[sym].details          # ZZZ is stale: not counted; the future rows are unseen
        assert recs[sym].raw_outputs["rank"] == k and recs[sym].raw_outputs["rank_n"] == 5
    assert recs["AAA"].raw_outputs["rank"] == 1
    assert recs["AAA"].raw_outputs["rank_universe_id"] == ranking.universe_id(UNIV.split(","))
    # a stale symbol is a skip, as without ranking
    z = _expert()._analyze("ZZZ", _Providers(), _rs(), _at(date(2025, 3, 3)), "daily_expert")
    assert z.skip and z.skip_reason == "no_forecast"


def test_extra_store_files_do_not_change_ranks(tmp_path, monkeypatch, rank_cache):
    """A locked-holdout / non-GA symbol in the store but not in rank_universe has no effect on any rank."""
    before = _signals(_rs())
    # HOLDOUT is the most bullish symbol of the store (clamped P(up) 0.95, largest q50), another is the least
    _write_rank_store(tmp_path, extra=(("HOLDOUT", 0.09), ("OTHER", -0.09)))
    ranking.reset_panels()
    assert (tmp_path / "ml_forecast" / rank_cache / "HOLDOUT.parquet").is_file()
    after = _signals(_rs())
    for s in before:
        assert after[s].signal == before[s].signal
        assert after[s].raw_outputs["rank"] == before[s].raw_outputs["rank"]
        assert after[s].raw_outputs["rank_n"] == 5
    # naming it in the universe DOES change them (the universe, not the directory, decides)
    inc = _signals(_rs(rank_universe=UNIV + ",HOLDOUT"))
    assert inc["AAA"].raw_outputs["rank"] == 2 and inc["AAA"].raw_outputs["rank_n"] == 6


def test_a_universe_symbol_without_a_store_file_aborts(rank_cache):
    with pytest.raises(store.ForecastStoreError, match="NOFILE"):
        _expert()._analyze("AAA", _Providers(), _rs(rank_universe=UNIV + ",NOFILE"), _at(date(2025, 3, 3)),
                           "daily_expert")
    # also when the analysed symbol itself has no file: an error, not a quiet no_forecast skip
    with pytest.raises(store.ForecastStoreError, match="NOFILE"):
        _expert()._analyze("NOFILE", _Providers(), _rs(rank_universe=UNIV + ",NOFILE"), _at(date(2025, 3, 3)),
                           "daily_expert")
    # the same through the run entry point: it aborts the run
    from ba2_providers.fmp_common import FMPHistoryCacheMiss

    class _Ctx:
        extra = {"symbol": "AAA"}
        providers = _Providers()
        settings = _rs(rank_universe=UNIV + ",NOFILE")
        subtype = "daily_expert"

    with pytest.raises(FMPHistoryCacheMiss):
        _expert().analyze_as_of(_at(date(2025, 3, 3)), _Ctx())


def test_top_n_still_goes_through_the_gates(rank_cache):
    # EEE (q50 = 0, P(up) 0.5) is inside the top 5 but fails the direction gate: HOLD with the gate's reason
    recs = _signals(_rs(rank_top_n=5))
    assert recs["EEE"].signal == OrderRecommendation.HOLD and "gate=" in recs["EEE"].details
    # min_prob 75 blocks everyone whose P(up) < 0.75 even inside the top N
    recs = _signals(_rs(rank_top_n=5, min_prob=75.0))
    assert [recs[x].signal for x in "AAA BBB CCC".split()] == [OrderRecommendation.BUY, OrderRecommendation.BUY,
                                                               OrderRecommendation.HOLD]
    assert "gate=min_prob" in recs["CCC"].details


def test_held_long_dropped_from_the_top_n_is_sold(rank_cache):
    recs = _signals(_rs(), use_case=AnalysisUseCase.OPEN_POSITIONS)
    assert recs["CCC"].signal == OrderRecommendation.SELL and "dropped out of top 2" in recs["CCC"].details
    assert recs["CCC"].confidence == mlf_mod.ROTATION_EXIT_CONFIDENCE == 50.0
    assert recs["EEE"].signal == OrderRecommendation.SELL
    # inside the top N the existing flip logic applies: a bullish row leaves a held long alone
    assert recs["AAA"].signal == OrderRecommendation.BUY and recs["BBB"].signal == OrderRecommendation.BUY
    # ranking off: CCC (P(up) 0.7) is no flip
    off = _signals({**SETTINGS, "rank_top_n": 0}, use_case=AnalysisUseCase.OPEN_POSITIONS)
    assert off["CCC"].signal == OrderRecommendation.BUY


def test_a_held_symbol_with_a_stale_row_is_skipped_not_rotated(rank_cache):
    rec = _expert()._analyze("ZZZ", _Providers(), _rs(), _at(date(2025, 3, 3)), AnalysisUseCase.OPEN_POSITIONS)
    assert rec.skip and rec.skip_reason == "no_forecast"


def test_ties_break_by_symbol_name_and_clamped_probabilities_by_the_median(tmp_path, monkeypatch):
    from ba2_common import config
    monkeypatch.setattr(config, "CACHE_FOLDER", str(tmp_path))
    mlf_mod._alias_memo.clear()
    ranking.reset_panels()
    store.reset_cache()
    key = model_key(CARD, ())
    d = date(2025, 3, 3)
    # TIEB is written before TIEA on purpose. LOW, TIEA, TIEB: identical rows. HIGH also clamps P(up) at 0.95 but has
    # the larger q50.
    rows = [ForecastRow(sym, d, {h: _dec_c(c) for h in (5, 10, 20)})
            for sym, c in (("TIEB", 0.05), ("TIEA", 0.05), ("LOW", 0.05), ("HIGH", 0.08), ("WEAK", 0.01))]
    store.write_rows(key, CARD, {"card_id": "timesfm3-ctx512"}, rows, (), tmp_path)
    univ = ["WEAK", "TIEB", "HIGH", "LOW", "TIEA"]
    ranks, n = mlf_mod.session_ranks(key, d, 10, univ)
    assert n == 5
    assert dict(ranks) == {"HIGH": 1, "LOW": 2, "TIEA": 3, "TIEB": 4, "WEAK": 5}
    assert mlf_mod.session_ranks(key, d, 10, sorted(univ)) == (ranks, n)
    store.reset_cache()
    ranking.reset_panels()


def test_ranking_uses_no_row_after_the_cutoff(rank_cache, tmp_path_factory, monkeypatch):
    """Ranks at 03-03 equal the ranks of a store that never had the later rows; the later rows do count on 03-10."""
    univ = UNIV.split(",")
    now, n = mlf_mod.session_ranks(rank_cache, date(2025, 3, 3), 10, univ)
    other = tmp_path_factory.mktemp("nofuture")
    _write_rank_store(other, with_future=False)
    ref, n2 = ranking.session_ranks(rank_cache, date(2025, 3, 3), 10, univ, cache_root=other)
    assert (dict(now), n) == (dict(ref), n2)
    later, _ = mlf_mod.session_ranks(rank_cache, date(2025, 3, 10), 10, univ)
    assert later["EEE"] == 1 and later["AAA"] == max(later.values())


def test_the_ranking_is_built_once_per_store_and_rebuilt_when_the_store_changes(rank_cache, monkeypatch, tmp_path):
    builds = []
    real = ranking.RankingPanel.build.__func__

    def spy(cls, *a, **k):
        builds.append(a)
        return real(cls, *a, **k)

    monkeypatch.setattr(ranking.RankingPanel, "build", classmethod(spy))
    univ = UNIV.split(",")
    mlf_mod.session_ranks(rank_cache, date(2025, 3, 3), 10, univ)
    mlf_mod.session_ranks(rank_cache, date(2025, 3, 3), 10, univ)
    mlf_mod.session_ranks(rank_cache, date(2025, 3, 4), 10, univ)       # another date: same panel
    assert len(builds) == 1
    mlf_mod.session_ranks(rank_cache, date(2025, 3, 3), 10, univ[:3])   # another universe: another panel
    assert len(builds) == 2
    # a rewrite of the store (new meta.json stamp) rebuilds
    import os
    meta = tmp_path / "ml_forecast" / rank_cache / "meta.json"
    st = meta.stat()
    os.utime(meta, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    mlf_mod.session_ranks(rank_cache, date(2025, 3, 3), 10, univ)
    assert len(builds) == 3


def test_session_ranks_are_read_only(rank_cache):
    ranks, _ = mlf_mod.session_ranks(rank_cache, date(2025, 3, 3), 10, UNIV.split(","))
    with pytest.raises(TypeError):
        ranks["AAA"] = 99

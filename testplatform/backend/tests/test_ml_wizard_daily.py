"""Dataset wizard, DAILY source of the ML exploration v2 (docs/plans, Phase 7 / Task 7.1): the alpaca_hist 1d
point-in-time feature store as a wizard data source (groups t / mtf / m / x / mac / f / ag / at / i / s, coverage
threshold) and the daily targets (rel_return, quintile, beats, up, volatility, multistep)."""
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from fastapi import HTTPException

from app.services import ml_dataset_source as S
from app.services import ml_features as F
from app.services import ml_panel as P
from app.services import ml_wizard_targets as W

FIRST, LAST = date(2023, 1, 3), date(2024, 12, 31)
UNIVERSE5 = ["AAA", "BBB", "CCC", "DDD", "EEE"]


def _sessions(first=FIRST, last=LAST):
    return pd.DatetimeIndex(pd.to_datetime(P.regular_sessions(first, last)["session"]))


def _bars_of(sessions, seed, drift=0.0005, noise=0.012, filled_at=()):
    """One symbol's load_daily-style frame (open/high/low/close/volume + listed / filled / ended)."""
    rng = np.random.default_rng(seed)
    n = len(sessions)
    close = 100.0 * np.exp(np.cumsum(drift + rng.normal(0, noise, n)))
    open_ = np.r_[100.0, close[:-1]] * np.exp(rng.normal(0, 0.003, n))
    hi = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.004, n)))
    lo = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.004, n)))
    d = pd.DataFrame({"open": open_, "high": hi, "low": lo, "close": close, "volume": 1e6,
                      "listed": True, "filled": False, "ended": False}, index=sessions)
    d.index.name = "session"
    for i in filled_at:
        d.iloc[i, d.columns.get_loc("filled")] = True
    return d


def _fake_features(symbols, first, last, groups, cache_root, **kw):
    """A build_features stand-in: [symbol, session] frame with the flags and two columns per group; group ``f`` is
    half covered in 2023 and fully in 2024."""
    sessions = _sessions(first, last)
    idx = pd.MultiIndex.from_product([list(symbols), sessions], names=["symbol", "session"])
    df = pd.DataFrame(index=idx)
    df["listed"] = True
    df["ended"] = False
    df["filled"] = False
    df["label_bar_filled"] = pd.array([False] * len(df), dtype="boolean")
    rng = np.random.default_rng(1)
    for g in groups:
        prefix = F.GROUPS[g].prefix
        for name in (F.GROUPS[g].coverage_columns or (f"{prefix}a", f"{prefix}b")):
            df[name] = rng.normal(size=len(df))
        if g in ("ag", "mac"):                     # groups whose coverage columns are named: keep an a/b pair too
            for k in ("a", "b"):
                df[f"{prefix}{k}"] = rng.normal(size=len(df))
    if "m" in groups:
        df["m_breadth_sma50"] = rng.uniform(size=len(df))
    if "f" in groups:
        yr = idx.get_level_values("session").year
        half = (np.arange(len(df)) % 2 == 0)
        for c in ("f_a", "f_b"):
            df.loc[(yr == 2023) & half, c] = np.nan
    df.attrs["meta"] = {"sectorless": [], "groups": {g: [] for g in groups}}
    return df


@pytest.fixture(autouse=True)
def _daily_env(monkeypatch):
    """No real cache: build_features / load_daily / last_cached_bar are stand-ins that record their arguments."""
    S.clear_daily_memo()
    calls = {"build": [], "load": []}

    def build(*a, **k):
        calls["build"].append((a, k))
        return _fake_features(*a, **k)

    def load(symbols, first, last, cache_root, cache_end=None):
        calls["load"].append((list(symbols), first, last, cache_end))
        sess = _sessions(first, last)
        return {s: _bars_of(sess, 7 + i) for i, s in enumerate(symbols)}, {"filled": {}, "first_bar": {}, "ended": {}}

    monkeypatch.setattr(F, "build_features", build)
    monkeypatch.setattr(F, "load_daily", load)
    monkeypatch.setattr(F, "last_cached_bar", lambda sym, root: date(2026, 10, 22))
    yield calls
    S.clear_daily_memo()


class _FakeDb:
    def __init__(self):
        self.added = []
        self.commit = MagicMock()
        self.refresh = MagicMock()
        self.rollback = MagicMock()

    def add(self, obj):
        obj.id = len(self.added) + 1
        self.added.append(obj)


# ---------------------------------------------------------------------------------------------- the group catalogue
class TestCatalogue:
    def test_the_filled_flag_is_not_a_model_feature(self):
        from app.services.job_handler import select_feature_columns
        cols = ["Date", "Open", "High", "Low", "Close", "Volume", "filled", "t_rsi", "f_missing", "up_h5"]
        assert select_feature_columns(cols, ["up_h5"], "up_h5") == ["t_rsi", "f_missing"]

    def test_every_store_group_is_offered_with_a_description_in_store_order(self):
        assert list(S.DAILY_GROUP_DESCRIPTIONS) == ["t", "mtf", "m", "x", "mac", "f", "ag", "at", "i", "s"]
        assert set(S.DAILY_GROUP_DESCRIPTIONS) == set(F.GROUPS)           # a group added to the store must be offered here
        assert all(len(d) > 10 and "\n" not in d for d in S.DAILY_GROUP_DESCRIPTIONS.values())

    def test_default_coverage_threshold_is_the_stores(self):
        assert S.DAILY_DEFAULT_MIN_SHARE == 0.7


# ---------------------------------------------------------------------------------------------- validation
class TestValidateDailyRequest:
    def v(self, **kw):
        args = dict(provider_name="alpaca_hist", timeframe="1d", daily_groups=["t", "m"], daily_min_share=None,
                    ticker="AAA", universe=None)
        args.update(kw)
        return S.validate_daily_request(**args)

    def test_normalised_request(self):
        r = self.v()
        assert r == {"daily_groups": ["t", "m"], "daily_min_share": 0.7, "daily_universe": ["AAA"]}

    def test_other_sources_carry_no_daily_keys(self):
        assert self.v(provider_name="yfinance", daily_groups=None) == {}
        assert self.v(timeframe="1h", daily_groups=None) == {}

    @pytest.mark.parametrize("kw, text", [
        (dict(provider_name="yfinance"), "need the alpaca_hist data provider with timeframe '1d'"),
        (dict(timeframe="1h"), "need the alpaca_hist data provider with timeframe '1d'"),
        (dict(provider_name="yfinance", daily_groups=None, daily_min_share=0.5), "daily_min_share"),
        (dict(daily_groups=None), "needs daily_groups"),
        (dict(daily_groups=[]), "needs daily_groups"),
        (dict(daily_groups=["t", "zz"]), "unknown daily_groups"),
        (dict(daily_groups=["t", "t"]), "duplicate"),
        (dict(daily_min_share=0.0), "daily_min_share"),
        (dict(daily_min_share=1.5), "daily_min_share"),
        (dict(daily_groups=["x"]), "at least 5 tickers"),
    ])
    def test_refusals(self, kw, text):
        with pytest.raises(ValueError, match=text):
            self.v(**kw)

    def test_x_with_a_big_enough_universe(self):
        r = self.v(daily_groups=["x"], universe=UNIVERSE5, ticker="CCC", daily_min_share=0.5)
        assert r["daily_universe"] == UNIVERSE5 and r["daily_min_share"] == 0.5

    def test_ticker_is_added_to_the_universe(self):
        assert self.v(universe=["BBB", "CCC"], ticker="AAA")["daily_universe"] == ["BBB", "CCC", "AAA"]

    def test_panel_features_are_intraday_only(self):
        with pytest.raises(ValueError, match="'1h', '15m'"):
            S.validate_panel_request("alpaca_hist", "1d", ["daily"])
        assert S.validate_panel_request("alpaca_hist", "1d", []) == []


# ---------------------------------------------------------------------------------------------- the build helper
class TestDailyBuild:
    def fetch(self, daily=None, ticker="AAA", **kw):
        daily = daily or {"daily_groups": ["t", "f"], "daily_min_share": 0.7, "daily_universe": ["AAA", "BBB"]}
        return S.fetch_ohlcv_frame("alpaca_hist", ticker, "1d", datetime(2023, 1, 3), datetime(2024, 12, 31), [], None,
                                   daily, **kw)

    def test_calls_build_features_with_the_chosen_groups_and_universe(self, _daily_env):
        df = self.fetch()
        (args, kwargs), = _daily_env["build"]
        assert list(args[0]) == ["AAA", "BBB"] and args[1] == FIRST and args[2] == LAST
        assert list(args[3]) == ["t", "f"]
        assert kwargs["sector_of"] == "store"
        assert {"Date", "Open", "High", "Low", "Close", "Volume", "filled", "t_a", "f_a"} <= set(df.columns)
        assert df["Date"].is_monotonic_increasing and len(df) == len(_sessions())

    def test_the_coverage_threshold_masks_the_thin_group_year(self):
        df = self.fetch({"daily_groups": ["t", "f"], "daily_min_share": 0.7, "daily_universe": ["AAA", "BBB"]})
        year = pd.to_datetime(df["Date"]).dt.year
        assert df.loc[year == 2023, ["f_a", "f_b"]].isna().all().all()            # 50% covered < 0.7: the year is absent
        assert (df.loc[year == 2023, "f_missing"] == 1.0).all() and (df.loc[year == 2024, "f_missing"] == 0.0).all()
        assert df.loc[year == 2024, "f_a"].notna().all()
        assert df["t_a"].notna().all() and (df["t_missing"] == 0.0).all()        # the t group is whole: not masked

    def test_a_lower_threshold_keeps_the_year(self):
        df = self.fetch({"daily_groups": ["t", "f"], "daily_min_share": 0.4, "daily_universe": ["AAA", "BBB"]})
        year = pd.to_datetime(df["Date"]).dt.year
        assert df.loc[year == 2023, "f_a"].notna().mean() == pytest.approx(0.5, abs=0.02)
        assert (df["f_missing"] == 0.0).all()

    def test_flags_and_bars_come_from_the_daily_cache(self, _daily_env):
        df = self.fetch()
        assert df["filled"].dtype == bool and not df["filled"].any()
        assert "listed" not in df.columns and "ended" not in df.columns and "label_bar_filled" not in df.columns
        (symbols, first, last, cache_end), = _daily_env["load"]
        assert symbols == ["AAA"] and cache_end == date(2026, 10, 22)

    def test_the_universe_is_built_once_for_all_of_a_batch(self, _daily_env):
        daily = {"daily_groups": ["t"], "daily_min_share": 0.7, "daily_universe": ["AAA", "BBB", "CCC"]}
        for t in ("AAA", "BBB", "CCC"):
            self.fetch(daily, ticker=t)
        assert len(_daily_env["build"]) == 1
        self.fetch({**daily, "daily_min_share": 0.5}, ticker="AAA")
        assert len(_daily_env["build"]) == 2                                      # another request: another build

    def test_unlisted_rows_are_dropped_and_the_listing_flag_is_not_a_feature(self, monkeypatch):
        def late(symbols, first, last, groups, cache_root, **kw):
            df = _fake_features(symbols, first, last, ["t"], cache_root)
            idx = df.index.get_level_values("session")
            early = (idx < pd.Timestamp("2023-03-01")) & (df.index.get_level_values("symbol") == "AAA")
            df.loc[early, "listed"] = False
            df.loc[early, ["t_a", "t_b"]] = np.nan
            return df
        monkeypatch.setattr(F, "build_features", late)
        df = self.fetch({"daily_groups": ["t"], "daily_min_share": 0.7, "daily_universe": ["AAA", "BBB"]})
        assert pd.to_datetime(df["Date"]).min() >= pd.Timestamp("2023-03-01") and df["t_a"].notna().all()

    def test_a_missing_daily_request_is_refused_loudly(self):
        with pytest.raises(ValueError, match="'1h', '15m'"):
            S.fetch_ohlcv_frame("alpaca_hist", "AAPL", "1d", datetime(2025, 1, 1), datetime(2025, 2, 1))

    def test_intraday_and_other_sources_are_unchanged(self, monkeypatch):
        monkeypatch.setattr(P, "ticker_frame", lambda *a, **k: _tiny_intraday().set_index("Date"))
        out = S.fetch_ohlcv_frame("alpaca_hist", "AAA", "1h", datetime(2024, 1, 2), datetime(2024, 1, 5), ["daily"])
        assert "Date" in out.columns
        with pytest.raises(ValueError, match="need the alpaca_hist data provider with timeframe '1d'"):
            S.fetch_ohlcv_frame("alpaca_hist", "AAA", "1h", datetime(2024, 1, 2), datetime(2024, 1, 5), [], None,
                                {"daily_groups": ["t"]})


def _tiny_intraday():
    ends = pd.date_range("2024-01-02 10:30", periods=5, freq="h")
    return pd.DataFrame({"Date": ends, "Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1.0})


# ---------------------------------------------------------------------------------------------- payload -> generation_config
class TestDailyPayload:
    def _create(self, **overrides):
        from app.api.datasets import create_dataset
        from app.schemas.dataset import DatasetCreate
        body = dict(ticker="AAPL", timeframe="1d", start_date="2023-01-03", end_date="2024-12-31",
                    data_provider="alpaca_hist", daily_groups=["t", "m", "mac"], daily_min_share=0.6)
        body.update(overrides)
        db, executor = _FakeDb(), MagicMock()
        with patch("app.api.datasets._dataset_executor", executor):
            create_dataset(DatasetCreate(**body), db)
        return db, executor

    def test_create_persists_the_daily_options_in_generation_config_and_build_config(self):
        db, executor = self._create()
        gc = db.added[0].generation_config
        assert gc["daily_groups"] == ["t", "m", "mac"] and gc["daily_min_share"] == 0.6
        assert gc["daily_universe"] == ["AAPL"] and gc["panel_features"] == [] and gc["data_provider"] == "alpaca_hist"
        build = executor.submit.call_args[0][2]
        assert build["daily_groups"] == ["t", "m", "mac"] and build["daily_min_share"] == 0.6
        assert build["daily_universe"] == ["AAPL"]

    def test_the_threshold_defaults_to_the_stores(self):
        db, _ = self._create(daily_min_share=None)
        assert db.added[0].generation_config["daily_min_share"] == 0.7

    def test_batch_create_shares_the_universe(self):
        from app.api.datasets import create_batch_datasets
        db, executor = _FakeDb(), MagicMock()
        with patch("app.api.datasets._dataset_executor", executor):
            create_batch_datasets({"symbols": UNIVERSE5, "timeframe": "1d", "data_provider": "alpaca_hist",
                                   "daily_groups": ["t", "x"], "daily_min_share": 0.8}, db)
        assert len(db.added) == 5
        for d in db.added:
            assert d.generation_config["daily_universe"] == UNIVERSE5
            assert d.generation_config["daily_groups"] == ["t", "x"] and d.generation_config["daily_min_share"] == 0.8
        assert executor.submit.call_args[0][2]["daily_universe"] == UNIVERSE5

    @pytest.mark.parametrize("overrides, text", [
        (dict(data_provider="yfinance"), "need the alpaca_hist data provider with timeframe '1d'"),
        (dict(daily_groups=["nope"]), "unknown daily_groups"),
        (dict(daily_groups=None), "needs daily_groups"),
        (dict(daily_groups=["x"]), "at least 5 tickers"),
        (dict(panel_features=["daily"]), "'1h', '15m'"),
        (dict(daily_min_share=2), "daily_min_share"),
    ])
    def test_invalid_requests_are_refused(self, overrides, text):
        with pytest.raises(HTTPException) as exc:
            self._create(**overrides)
        assert exc.value.status_code == 400 and text in exc.value.detail

    def test_schemas_carry_the_fields(self):
        from app.schemas.dataset import DatasetCreate, DatasetUpdate
        assert DatasetUpdate(daily_groups=["t"], daily_min_share=0.5).daily_groups == ["t"]
        assert DatasetUpdate().daily_groups is None and DatasetUpdate().daily_min_share is None
        assert DatasetCreate(ticker="A", timeframe="1d").daily_groups is None


# ---------------------------------------------------------------------------------------------- build paths
class TestBreadthNeedsAUniverse:
    def fetch(self, universe, ticker="AAA"):
        return S.fetch_ohlcv_frame("alpaca_hist", ticker, "1d", datetime(2023, 1, 3), datetime(2024, 12, 31), [], None,
                                   {"daily_groups": ["t", "m"], "daily_min_share": 0.7, "daily_universe": universe})

    def test_the_breadth_of_a_universe_below_five_tickers_is_dropped(self):
        for universe in (["AAA"], ["AAA", "BBB"], ["AAA", "BBB", "CCC", "DDD"]):
            df = self.fetch(universe)
            assert "m_breadth_sma50" not in df.columns
            assert "m_a" in df.columns and "t_a" in df.columns          # the rest of group m stays

    def test_the_breadth_of_five_or_more_tickers_is_kept(self):
        assert "m_breadth_sma50" in self.fetch(UNIVERSE5).columns

    def test_a_group_other_than_m_is_not_touched(self):
        df = S.fetch_ohlcv_frame("alpaca_hist", "AAA", "1d", datetime(2023, 1, 3), datetime(2024, 12, 31), [], None,
                                 {"daily_groups": ["t"], "daily_min_share": 0.7, "daily_universe": ["AAA"]})
        assert "t_a" in df.columns

    def test_the_group_description_says_so(self):
        assert "5" in S.DAILY_GROUP_DESCRIPTIONS["m"] and "breadth" in S.DAILY_GROUP_DESCRIPTIONS["m"]


class TestMemoKeyCoversTheSourceFiles:
    """The memoised universe build is reused only while every source file the build reads is unchanged."""

    @pytest.fixture
    def root(self, tmp_path, monkeypatch):
        from ba2_providers.macro import fred_series as fs
        monkeypatch.setattr("ba2_common.config.CACHE_FOLDER", str(tmp_path))
        monkeypatch.setattr(fs, "CACHE_FOLDER", str(tmp_path))
        d = tmp_path / "AlpacaHistoryOHLCVProvider"
        d.mkdir()
        for sym in ("AAA", "BBB", "SPY", "QQQ", "IWM", "TLT", "GLD"):
            (d / f"{sym}_1d.parquet").write_bytes(b"x" * 10)
        (tmp_path / "fred").mkdir()
        (tmp_path / "fred" / "VIXCLS.json").write_text("{}")
        fmp = F.fmp_history_path("analyst_grades", "AAA", tmp_path)
        fmp.parent.mkdir(parents=True, exist_ok=True)
        fmp.write_text("[]")
        return tmp_path

    def fetch(self, groups):
        return S.fetch_ohlcv_frame("alpaca_hist", "AAA", "1d", datetime(2023, 1, 3), datetime(2024, 12, 31), [], None,
                                   {"daily_groups": groups, "daily_min_share": 0.7, "daily_universe": ["AAA", "BBB"]})

    def test_unchanged_files_reuse_the_build(self, root, _daily_env):
        self.fetch(["t"])
        self.fetch(["t"])
        assert len(_daily_env["build"]) == 1

    def test_a_changed_daily_file_of_a_member_rebuilds(self, root, _daily_env):
        self.fetch(["t"])
        (root / "AlpacaHistoryOHLCVProvider" / "BBB_1d.parquet").write_bytes(b"y" * 25)
        self.fetch(["t"])
        assert len(_daily_env["build"]) == 2

    def test_a_changed_fmp_history_file_rebuilds_when_its_group_is_on(self, root, _daily_env):
        self.fetch(["t", "ag"])
        F.fmp_history_path("analyst_grades", "AAA", root).write_text("[1, 2, 3]")
        self.fetch(["t", "ag"])
        assert len(_daily_env["build"]) == 2

    def test_a_changed_fred_file_rebuilds_when_mac_is_on(self, root, _daily_env):
        self.fetch(["t", "mac"])
        (root / "fred" / "VIXCLS.json").write_text('{"observations": []}')
        self.fetch(["t", "mac"])
        assert len(_daily_env["build"]) == 2

    def test_a_changed_market_etf_file_rebuilds_when_m_is_on(self, root, _daily_env):
        self.fetch(["t", "m"])
        (root / "AlpacaHistoryOHLCVProvider" / "QQQ_1d.parquet").write_bytes(b"z" * 40)
        self.fetch(["t", "m"])
        assert len(_daily_env["build"]) == 2


class TestDailyBuildPaths:
    DAILY_GC = {"data_provider": "alpaca_hist", "panel_features": [], "daily_groups": ["t", "mtf"],
                "daily_min_share": 0.75, "daily_universe": ["AAA", "BBB"], "original_start_date": "2023-01-03",
                "original_end_date": "2024-12-31"}

    def _frame(self):
        return _fake_features(["AAA"], FIRST, LAST, ["t"], None).xs("AAA", level="symbol").reset_index().rename(
            columns={"session": "Date"}).assign(Open=1.0, High=1.0, Low=1.0, Close=1.0, Volume=1.0)

    def test_background_build(self, tmp_path):
        from app.api import datasets as D
        calls = []
        dataset = MagicMock(file_path=str(tmp_path / "ds.csv"))
        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = dataset
        with patch.object(D, "SessionLocal", return_value=session), \
                patch.object(D, "fetch_ohlcv_frame", side_effect=lambda *a: calls.append(a) or self._frame()):
            D._build_dataset_in_background(1, {
                "ticker": "AAA", "timeframe": "1d", "start_date": "2023-01-03", "end_date": "2024-12-31",
                "data_provider": "alpaca_hist", "panel_features": [], "technical_indicators": [],
                "sentiment_config": {}, "fundamentals_config": {}, "daily_groups": ["t", "mtf"],
                "daily_min_share": 0.75, "daily_universe": ["AAA", "BBB"]})
        assert calls[0][:3] == ("alpaca_hist", "AAA", "1d")
        assert calls[0][7] == {"daily_groups": ["t", "mtf"], "daily_min_share": 0.75, "daily_universe": ["AAA", "BBB"]}
        assert "t_a" in pd.read_csv(tmp_path / "ds.csv").columns

    def test_regeneration_reads_the_daily_options_from_generation_config(self, tmp_path):
        from app.api import datasets as D
        from app.schemas.dataset import DatasetRegenerate
        calls = []
        with patch.object(D, "SessionLocal", return_value=MagicMock()), \
                patch.object(D, "fetch_ohlcv_frame", side_effect=lambda *a: calls.append(a) or self._frame()):
            D._regenerate_dataset_in_background(1, {
                "regen_options": DatasetRegenerate(), "ticker": "AAA", "timeframe": "1d",
                "file_path": str(tmp_path / "ds.csv"), "technical_indicators": None, "fundamentals_config": None,
                "sentiment_config": None, "start_date": datetime(2023, 1, 3), "end_date": datetime(2024, 12, 31),
                "gen_config": dict(self.DAILY_GC)})
        assert calls[0][7]["daily_groups"] == ["t", "mtf"] and calls[0][7]["daily_min_share"] == 0.75

    def test_partial_regeneration_keeps_the_daily_columns(self, tmp_path):
        from app.api import datasets as D
        from app.schemas.dataset import DatasetRegenerate
        path = tmp_path / "ds.csv"
        df = self._frame()
        for c in ("mtf_a", "x_a", "mac_a", "ag_a", "at_a", "i_a", "s_a", "f_a", "m_a", "filled"):
            df[c] = 1.0
        df.to_csv(path, index=False)
        with patch.object(D, "SessionLocal", return_value=MagicMock()), \
                patch.object(D, "fetch_ohlcv_frame", side_effect=AssertionError("OHLCV not regenerated")):
            D._regenerate_dataset_in_background(1, {
                "regen_options": DatasetRegenerate(regenerate_ohlcv=False, regenerate_technical=True),
                "ticker": "AAA", "timeframe": "1d", "file_path": str(path), "technical_indicators": None,
                "fundamentals_config": None, "sentiment_config": None, "start_date": datetime(2023, 1, 3),
                "end_date": datetime(2024, 12, 31), "gen_config": dict(self.DAILY_GC)})
        kept = set(pd.read_csv(path).columns)
        assert {"t_a", "mtf_a", "x_a", "mac_a", "ag_a", "at_a", "i_a", "s_a", "f_a", "m_a", "filled"} <= kept

    def test_duplicate(self, tmp_path):
        from app.api import datasets as D
        from app.models.dataset import Dataset
        from app.schemas.dataset import DatasetDuplicate
        calls = []
        original = Dataset(id=7, name="o", ticker="AAA", timeframe="1d", start_date=datetime(2023, 1, 3),
                           end_date=datetime(2024, 12, 31), rows_count=1, file_path="x.csv", technical_indicators=None,
                           generation_config=dict(self.DAILY_GC))
        db = _FakeDb()
        db.query = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = original
        with patch.object(D, "DATASETS_DIR", tmp_path), \
                patch.object(D, "fetch_ohlcv_frame", side_effect=lambda *a: calls.append(a) or self._frame()):
            new = D.duplicate_dataset(7, DatasetDuplicate(new_ticker="CCC"), db)
        assert calls[0][:3] == ("alpaca_hist", "CCC", "1d")
        assert calls[0][7] == {"daily_groups": ["t", "mtf"], "daily_min_share": 0.75,
                               "daily_universe": ["AAA", "BBB", "CCC"]}
        gc = new.generation_config
        assert gc["daily_groups"] == ["t", "mtf"] and gc["daily_min_share"] == 0.75
        assert gc["daily_universe"] == ["AAA", "BBB", "CCC"]

    def _duplicate(self, tmp_path, groups, new_ticker):
        from app.api import datasets as D
        from app.models.dataset import Dataset
        from app.schemas.dataset import DatasetDuplicate
        original = Dataset(id=7, name="o", ticker="AAA", timeframe="1d", start_date=datetime(2023, 1, 3),
                           end_date=datetime(2024, 12, 31), rows_count=1, file_path="x.csv", technical_indicators=None,
                           generation_config={**self.DAILY_GC, "daily_groups": groups})
        db = _FakeDb()
        db.query = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = original
        with patch.object(D, "DATASETS_DIR", tmp_path), \
                patch.object(D, "fetch_ohlcv_frame", side_effect=lambda *a: self._frame()):
            return D.duplicate_dataset(7, DatasetDuplicate(new_ticker=new_ticker), db)

    def test_a_duplicate_that_widens_the_universe_warns_about_x_and_breadth(self, tmp_path):
        new = self._duplicate(tmp_path, ["t", "m"], "CCC")
        assert len(new.warnings) == 1
        w = new.warnings[0]
        assert "CCC" in w and "2 -> 3" in w and "breadth" in w and "differ" in w
        from app.schemas.dataset import DatasetResponse
        assert "warnings" in DatasetResponse.model_fields

    def test_no_warning_when_the_universe_is_unchanged_or_nothing_depends_on_it(self, tmp_path):
        assert not self._duplicate(tmp_path, ["t", "m"], "BBB").warnings      # already a member
        assert not self._duplicate(tmp_path, ["t", "mtf"], "CCC").warnings    # neither x nor breadth in use


class TestDailyUpdatePath:
    def _update(self, generation_config, update, fetched, ticker="AAA", timeframe="1d"):
        from app.api import datasets as D
        from app.models.dataset import Dataset
        from app.schemas.dataset import DatasetUpdate
        ds = Dataset(id=3, name="o", ticker=ticker, timeframe=timeframe, start_date=datetime(2023, 1, 3),
                     end_date=datetime(2024, 12, 31), rows_count=1, file_path="x.csv", technical_indicators=None,
                     fundamentals_config=None, generation_config=generation_config)
        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = ds
        queue = MagicMock()
        frame = _fake_features(["AAA"], FIRST, LAST, ["t"], None).xs("AAA", level="symbol").reset_index().rename(
            columns={"session": "Date"})
        with patch("app.models.database.SessionLocal", return_value=session), \
                patch("app.services.task_queue.get_task_queue", return_value=queue), \
                patch.object(D, "SessionLocal", return_value=session), \
                patch.object(D, "fetch_ohlcv_frame", side_effect=lambda *a: fetched.append(a) or frame.copy()):
            D.update_dataset(3, DatasetUpdate(**update))
        return ds, queue

    GC = {"data_provider": "alpaca_hist", "panel_features": [], "daily_groups": ["t"], "daily_min_share": 0.7,
          "daily_universe": ["AAA", "BBB"], "original_start_date": "2023-01-03", "original_end_date": "2024-12-31"}

    def test_edit_keeps_and_updates_the_daily_options(self):
        fetched = []
        ds, _ = self._update(dict(self.GC), {}, fetched)
        assert fetched[0][7] == {"daily_groups": ["t"], "daily_min_share": 0.7, "daily_universe": ["AAA", "BBB"]}
        ds, _ = self._update(dict(self.GC), {"daily_groups": ["t", "mac"], "daily_min_share": 0.5}, fetched)
        assert ds.generation_config["daily_groups"] == ["t", "mac"] and ds.generation_config["daily_min_share"] == 0.5
        assert ds.generation_config["daily_universe"] == ["AAA", "BBB"]

    def test_leaving_the_daily_source_drops_the_daily_options(self):
        fetched = []
        with patch("app.api.datasets.get_ohlcv_provider"):
            ds, _ = self._update(dict(self.GC), {"data_provider": "yfinance"}, fetched)
        gc = ds.generation_config
        assert "daily_groups" not in gc and "daily_min_share" not in gc and "daily_universe" not in gc

    def test_changing_the_timeframe_away_from_1d_is_refused_for_daily_groups(self):
        with pytest.raises(HTTPException) as exc:
            self._update(dict(self.GC), {"timeframe": "1h", "daily_groups": ["t"]}, [])
        assert exc.value.status_code == 400

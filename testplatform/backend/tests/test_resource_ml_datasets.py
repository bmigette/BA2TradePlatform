"""OHLCV provider factory + dataset-build coverage against the shared providers.

History: this file used to test ``BA2ProvidersOHLCVAdapter`` and the old
``OHLCV_SOURCE``-flagged ``get_ohlcv_provider`` factory (legacy direct providers
vs an adapter that bridged a ba2_providers DataFrame into the
List[MarketDataPoint] contract). After the provider-consolidation migration the
local ``dataproviders/`` tree (incl. the adapter and the legacy direct providers)
was deleted: the shared ``ba2_providers`` OHLCV providers now expose
``get_data(...) -> List[MarketDataPoint]`` NATIVELY, so the adapter no longer
exists and the factory simply returns the shared provider (augmented with the
backend OHLCV disk-cache layer).

These tests are rewritten to cover what survived the seam:
  * ``get_ohlcv_provider`` returns the shared provider for known names (incl. the
    yf->yfinance alias) and that provider satisfies the get_data ->
    List[MarketDataPoint] contract the dataset builder consumes.
  * The REAL ``_build_dataset_in_background`` runs end-to-end against the shared
    get_data() seam (torch-free) and produces the expected OHLCV matrix.
  * A torch-free MLStrategy smoke runs on a dataset built through that seam.
"""
from datetime import datetime

import pandas as pd
import pytest

from ba2_common.core.types import MarketDataPoint


def _canonical_ohlcv(start, end, volume_as_float=True):
    """A deterministic daily OHLCV series in [start, end], business days only."""
    dates = pd.bdate_range(start=start, end=end)
    rows = []
    for i, d in enumerate(dates):
        base = 100.0 + i
        vol = float(1000 + i) if volume_as_float else (1000 + i)
        rows.append(dict(Date=d.to_pydatetime(), Open=base, High=base + 2.0,
                         Low=base - 1.0, Close=base + 0.5, Volume=vol))
    return pd.DataFrame(rows)


def _df_to_points(df, symbol="AAPL", interval="1d"):
    """Build shared MarketDataPoint list from a canonical OHLCV frame.

    Mirrors what the shared provider's get_data() returns: the six attributes the
    dataset builder reads (.timestamp/.open/.high/.low/.close/.volume).
    """
    pts = []
    for _, r in df.iterrows():
        pts.append(MarketDataPoint(
            symbol=symbol,
            timestamp=pd.Timestamp(r["Date"]).to_pydatetime(),
            open=float(r["Open"]), high=float(r["High"]), low=float(r["Low"]),
            close=float(r["Close"]), volume=float(r["Volume"]), interval=interval,
        ))
    return pts


class _FakeSharedProvider:
    """Mimics a shared ba2_providers OHLCV provider's public contract:
    get_data -> List[MarketDataPoint], windowed to [start_date, end_date]."""

    def __init__(self, df):
        self._df = df
        self.calls = []

    def get_data(self, symbol, start_date=None, end_date=None, interval="1d", **kw):
        self.calls.append(dict(symbol=symbol, start_date=start_date,
                               end_date=end_date, interval=interval))
        win = self._df[(self._df["Date"] >= start_date)
                       & (self._df["Date"] <= end_date)].copy()
        return _df_to_points(win, symbol=symbol, interval=interval)


# --------------------------------------------------------------------------- #
# get_ohlcv_provider: returns the shared provider (no flag, no adapter).       #
# --------------------------------------------------------------------------- #

def test_get_ohlcv_provider_returns_shared_provider():
    from app.api.datasets import get_ohlcv_provider
    from ba2_common.core.interfaces.MarketDataProviderInterface import (
        MarketDataProviderInterface,
    )

    prov = get_ohlcv_provider("yfinance")
    # The shared providers subclass the shared MarketDataProviderInterface.
    assert isinstance(prov, MarketDataProviderInterface)
    # ...and expose the native get_data contract the dataset builder consumes.
    assert hasattr(prov, "get_data")
    # ...plus the backend OHLCV disk-cache layer (wrap_with_cache) the cache
    # fetch task relies on.
    assert hasattr(prov, "extend_ohlcv_cache")
    assert hasattr(prov, "_get_cache_file")


def test_get_ohlcv_provider_yf_alias():
    """'yf' is an alias for 'yfinance' and resolves to the same provider type."""
    from app.api.datasets import get_ohlcv_provider

    assert type(get_ohlcv_provider("yf")) is type(get_ohlcv_provider("yfinance"))


def test_get_ohlcv_provider_default_is_yfinance():
    from app.api.datasets import get_ohlcv_provider

    assert type(get_ohlcv_provider()) is type(get_ohlcv_provider("yfinance"))


def test_shared_provider_get_data_returns_marketdatapoints():
    """The shared provider's get_data() yields objects exposing the six attributes
    the dataset builder reads (the contract the old adapter used to synthesize)."""
    fake = _FakeSharedProvider(_canonical_ohlcv(datetime(2020, 1, 1),
                                                datetime(2020, 1, 10)))
    pts = fake.get_data("AAPL", datetime(2020, 1, 1), datetime(2020, 1, 10), "1d")
    assert len(pts) > 0
    p0 = pts[0]
    for attr in ("timestamp", "open", "high", "low", "close", "volume"):
        assert hasattr(p0, attr)
    assert p0.open == 100.0 and p0.high == 102.0 and p0.low == 99.0
    assert p0.close == 100.5 and p0.volume == 1000.0


# --------------------------------------------------------------------------- #
# REAL _build_dataset_in_background through the shared get_data() seam.        #
# --------------------------------------------------------------------------- #

FIXED = dict(ticker="AAPL", timeframe="1d",
             start_date="2023-01-03", end_date="2023-03-31")


def _gate_db_session(tmp_path):
    """Throwaway SQLite + a Session factory with the full schema created."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import app.models  # noqa: F401  registers every model on Base.metadata
    from app.models.database import Base

    db_file = tmp_path / "gate.sqlite"
    eng = create_engine(f"sqlite:///{db_file}",
                        connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)


def _build_csv(tmp_path, Session, src_df, technical_indicators=None, tag="ds"):
    """Build the FIXED dataset through the REAL _build_dataset_in_background, with
    the shared get_ohlcv_provider seam faked to return canonical MarketDataPoints.
    Returns (csv_path, dataset_id)."""
    import unittest.mock as mock

    import app.api.datasets as dsmod
    from app.models.dataset import Dataset, DatasetStatus

    technical_indicators = technical_indicators or []
    dsmod.SessionLocal = Session  # the builder uses this module-level factory

    out = tmp_path / f"{tag}.csv"
    start = datetime.strptime(FIXED["start_date"], "%Y-%m-%d")
    end = datetime.strptime(FIXED["end_date"], "%Y-%m-%d")

    s = Session()
    ds = Dataset(name=f"gate_{tag}", ticker=FIXED["ticker"],
                 timeframe=FIXED["timeframe"], start_date=start, end_date=end,
                 file_path=str(out), technical_indicators=technical_indicators,
                 status=DatasetStatus.BUILDING.value)
    s.add(ds)
    s.commit()
    ds_id = ds.id
    s.close()

    cfg = dict(ticker=FIXED["ticker"], timeframe=FIXED["timeframe"],
               start_date=FIXED["start_date"], end_date=FIXED["end_date"],
               data_provider="fmp", technical_indicators=technical_indicators,
               sentiment_config={}, fundamentals_config={})

    fake = _FakeSharedProvider(src_df)
    with mock.patch.object(dsmod, "get_ohlcv_provider", lambda name="fmp": fake):
        dsmod._build_dataset_in_background(ds_id, cfg)

    return out, ds_id


def test_build_dataset_through_shared_seam(tmp_path):
    """The REAL builder produces a correct OHLCV CSV when fed the shared
    get_data() seam (no network, no torch)."""
    Session = _gate_db_session(tmp_path)
    start = datetime.strptime(FIXED["start_date"], "%Y-%m-%d")
    end = datetime.strptime(FIXED["end_date"], "%Y-%m-%d")
    src = _canonical_ohlcv(start, end, volume_as_float=True)

    csv, ds_id = _build_csv(tmp_path, Session, src)

    from app.models.dataset import Dataset, DatasetStatus
    s = Session()
    assert s.get(Dataset, ds_id).status == DatasetStatus.READY.value
    s.close()

    df = pd.read_csv(csv)
    cols = ["Date", "Open", "High", "Low", "Close", "Volume"]
    for c in cols:
        assert c in df.columns
    assert len(df) == len(src)
    # The matrix reflects the canonical source fed at the seam.
    assert df["Open"].iloc[0] == 100.0
    assert df["Close"].iloc[0] == 100.5
    assert df["Volume"].iloc[0] == 1000.0


def test_build_dataset_with_indicators_through_shared_seam(tmp_path):
    """A richer matrix (technical indicators + warmup widening) also builds
    end-to-end through the shared seam, exercising the warmup fetch/refilter path."""
    from datetime import timedelta
    Session = _gate_db_session(tmp_path)
    start = datetime.strptime(FIXED["start_date"], "%Y-%m-%d")
    end = datetime.strptime(FIXED["end_date"], "%Y-%m-%d")
    # Indicators trigger warmup_days > 0, so the builder fetches before `start`.
    src = _canonical_ohlcv(start - timedelta(days=120), end, volume_as_float=True)
    indicators = [{"type": "sma", "period": 10, "timeframe": "1d"}]

    csv, ds_id = _build_csv(tmp_path, Session, src,
                            technical_indicators=indicators, tag="ds_ind")

    from app.models.dataset import Dataset, DatasetStatus
    s = Session()
    assert s.get(Dataset, ds_id).status == DatasetStatus.READY.value
    s.close()

    df = pd.read_csv(csv)
    assert len(df) > 0
    # An SMA column materialized (the warmup path produced indicator values).
    sma_cols = [c for c in df.columns if "sma" in c.lower() or "SMA" in c]
    assert sma_cols, f"expected an SMA indicator column, got {list(df.columns)}"


# --------------------------------------------------------------------------- #
# Torch-free MLStrategy smoke on a dataset built through the shared seam.      #
# --------------------------------------------------------------------------- #

def test_ml_training_smoke_through_provider_path(tmp_path):
    """A dataset built through the shared get_data() seam feeds a backtesting.py
    MLStrategy run end-to-end (torch-free; the MLStrategy engine is unchanged)."""
    import unittest.mock as mock
    import numpy as np
    from backtesting import Backtest

    import app.api.datasets as dsmod
    from app.models.dataset import Dataset, DatasetStatus
    from app.services.backtest_handler import MLStrategy
    from app.services.strategy_executor import reset_evaluation_stats

    Session = _gate_db_session(tmp_path)
    dsmod.SessionLocal = Session

    smoke_start = datetime(2023, 1, 2)
    smoke_end = datetime(2023, 4, 30)
    dates = pd.bdate_range(start=smoke_start, end=smoke_end)
    rng = np.random.RandomState(42)
    price = 100.0
    rows = []
    for i, d in enumerate(dates):
        price += rng.uniform(-1.0, 1.2)
        rows.append(dict(Date=d.to_pydatetime(), Open=price, High=price + 1.5,
                         Low=price - 1.5, Close=price + rng.uniform(-0.5, 0.5),
                         Volume=float(1000 + i)))
    src = pd.DataFrame(rows)

    # --- 1) Build the dataset through the shared get_data() seam ----------- #
    out = tmp_path / "smoke.csv"
    s = Session()
    ds = Dataset(name="smoke", ticker="AAPL", timeframe="1d",
                 start_date=smoke_start, end_date=smoke_end, file_path=str(out),
                 technical_indicators=[], status=DatasetStatus.BUILDING.value)
    s.add(ds)
    s.commit()
    ds_id = ds.id
    s.close()

    fake = _FakeSharedProvider(src)
    with mock.patch.object(dsmod, "get_ohlcv_provider", lambda name="fmp": fake):
        dsmod._build_dataset_in_background(ds_id, dict(
            ticker="AAPL", timeframe="1d",
            start_date=smoke_start.strftime("%Y-%m-%d"),
            end_date=smoke_end.strftime("%Y-%m-%d"), data_provider="fmp",
            technical_indicators=[], sentiment_config={},
            fundamentals_config={}))

    s = Session()
    built = s.get(Dataset, ds_id)
    assert built.status == DatasetStatus.READY.value
    assert built.rows_count > 0
    s.close()

    df = pd.read_csv(out, parse_dates=["Date"])
    assert len(df) > 0

    # --- 2) Run the UNCHANGED MLStrategy engine on the built dataset ------ #
    bt_data = df[["Date", "Open", "High", "Low", "Close", "Volume"]].copy()
    bt_data["Date"] = pd.to_datetime(bt_data["Date"])
    bt_data.set_index("Date", inplace=True)
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        bt_data[c] = pd.to_numeric(bt_data[c])

    ts = sorted(pd.Timestamp(t) for t in bt_data.index)
    preds = {t: (np.array([0.1, 0.9]) if i % 5 == 0 else np.array([0.8, 0.2]))
             for i, t in enumerate(ts)}

    reset_evaluation_stats()
    MLStrategy.predictions = preds
    MLStrategy.prediction_timestamps = ts
    MLStrategy.buy_entry_conditions = {
        "operator": "AND",
        "conditions": [
            {"field": "model:predicted_class", "comparison": "==", "value": 1}
        ],
    }
    MLStrategy.sell_entry_conditions = None
    MLStrategy.exit_conditions = [{
        "label": "take_profit",
        "conditions": {
            "operator": "AND",
            "conditions": [
                {"field": "position_pnl_pct", "comparison": ">=", "value": 2.0}
            ],
        },
    }]
    MLStrategy.tp_percent = 3.0
    MLStrategy.sl_percent = 2.0
    MLStrategy.n_classes = 2
    MLStrategy.position_sizing_pct = 20.0

    bt = Backtest(bt_data, MLStrategy, cash=10000.0, commission=0.0,
                  exclusive_orders=True, trade_on_close=True, hedging=False)
    stats = bt.run()

    assert int(stats["# Trades"]) > 0, "MLStrategy run opened no trades"
    assert stats._strategy.buy_trades_opened > 0, (
        "entry path was not exercised by the smoke")
    assert stats["Equity Final [$]"] > 0

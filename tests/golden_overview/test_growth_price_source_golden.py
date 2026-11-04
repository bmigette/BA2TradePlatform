"""The growth golden, re-derived through the NEW price source path (services.price_history, FMP-shaped frames).

The seed prices are turned into the frame an OHLCV provider returns (a UTC ``Date`` column and a ``Close`` column),
read back with ``price_history.closes_from_frame`` -- the only step the new source adds between the provider and the
growth maths -- and the growth series computed from THAT must equal ``growth_golden.json``. The source of prices was
changed on purpose (yfinance -> the account's FMP history provider); this pins that the change moves the numbers only
through the prices themselves, never through the extraction."""
import json
from pathlib import Path

import pandas as pd

from ba2_trade_platform.core.utils import get_labels_by_symbol
from ba2_trade_platform.services import growth, price_history
from tests.golden_overview import growth_seed as seed

GOLDEN = json.loads(Path(__file__).with_name("growth_golden.json").read_text())


def _through_the_new_source(prices):
    out = {}
    for sym, closes in prices.items():
        frame = pd.DataFrame({"Date": pd.to_datetime(sorted(closes), utc=True),
                              "Close": [closes[d] for d in sorted(closes)]})
        out[sym] = price_history.closes_from_frame(frame)
    return out


def test_the_frame_round_trip_reproduces_the_seed_prices_exactly():
    prices = seed.historical_prices()
    assert _through_the_new_source(prices) == prices


def test_growth_series_from_the_new_source_equal_the_golden():
    seed.seed_instruments()
    pos, trades, divs = seed.positions(), seed.trades(), seed.dividends()
    prices = _through_the_new_source(seed.historical_prices())
    info = growth.build_symbol_info(pos, get_labels_by_symbol([p.symbol for p in pos]) or {})
    dates = growth.chart_dates(prices, None)
    got = growth.position_growth(info, dates, prices, trades, divs)
    want = GOLDEN["position"]["max"]
    assert json.loads(json.dumps({k: got[k] for k in want if k != "symbol_info"}, default=str)) == \
        {k: v for k, v in want.items() if k != "symbol_info"}


def test_closes_from_frame_handles_a_date_index_nan_and_empty_frames():
    idx = pd.DatetimeIndex(["2026-09-01", "2026-09-02"])
    assert price_history.closes_from_frame(pd.DataFrame({"Close": [1.0, float("nan")]}, index=idx)) == {"2026-09-01": 1.0}
    assert price_history.closes_from_frame(pd.DataFrame(columns=["Date", "Close"])) == {}
    assert price_history.closes_from_frame(None) == {}

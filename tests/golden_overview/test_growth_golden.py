"""The growth services hold to what the INLINE Account Growth tab computed (``growth_golden.json``, captured by
``capture_growth.py`` before the extraction). JSON turns tuples into lists and dict keys into strings; the comparison
normalises the service output the same way."""
import json
from pathlib import Path

import pytest

from ba2_trade_platform.core.utils import get_labels_by_symbol
from ba2_trade_platform.services import growth
from tests.golden_overview import growth_seed as seed

GOLDEN = json.loads(Path(__file__).with_name("growth_golden.json").read_text())
SCOPES = {"max": None, "from_0915": seed.RANGE_START}


def norm(value):
    return json.loads(json.dumps(value, default=str))


@pytest.fixture
def world():
    seed.seed_instruments()
    return seed.positions(), seed.historical_prices(), seed.trades(), seed.dividends(), seed.balance_history()


@pytest.mark.parametrize("scope", SCOPES)
def test_total_growth(world, scope):
    _, _, trades, divs, bal = world
    got = growth.total_growth(bal, divs, trades, SCOPES[scope])
    assert norm(got) == GOLDEN["total"][scope]


@pytest.mark.parametrize("scope", SCOPES)
def test_label_growth(world, scope):
    pos, prices, trades, divs, _ = world
    labelled = [p for p in pos if p.symbol != "BND"]
    info = growth.build_symbol_info(labelled, get_labels_by_symbol([p.symbol for p in labelled]) or {})
    dates = growth.chart_dates(prices, SCOPES[scope])
    got = growth.label_growth(info, dates, prices, trades, divs)
    want = GOLDEN["label"][scope]
    assert norm({k: got[k] for k in want}) == want


@pytest.mark.parametrize("scope", SCOPES)
def test_position_growth(world, scope):
    pos, prices, trades, divs, _ = world
    info = growth.build_symbol_info(pos, get_labels_by_symbol([p.symbol for p in pos]) or {})
    dates = growth.chart_dates(prices, SCOPES[scope])
    got = growth.position_growth(info, dates, prices, trades, divs)
    want = GOLDEN["position"][scope]
    assert norm({**{k: got[k] for k in want if k not in ("symbol_info",)}, "symbol_info": info}) == want


@pytest.mark.parametrize("scope", SCOPES)
def test_single_position_growth(world, scope):
    _, prices, trades, divs, _ = world
    for sym, qty in (("AAPL", 12.0), ("MSFT", 5.0), ("BND", 8.0)):
        got = growth.single_position_growth([d for d in divs if d["symbol"] == sym], prices[sym], qty,
                                            [t for t in trades if t["symbol"] == sym], SCOPES[scope])
        assert norm(got) == GOLDEN["single"][scope][sym], (scope, sym)


def test_monthly_realized_and_denominators(world):
    pos, prices, trades, divs, bal = world
    months, income, by_label, labels, detail = growth.monthly_realized(trades, divs)
    want = GOLDEN["monthly"]
    assert norm({"months": months, "income": income, "by_label": by_label, "labels": labels,
                 "symbol_detail": detail}) == want
    mg, ml = growth.value_denominators(bal, pos, prices, trades, divs, months)
    assert norm({"global": mg, "label": ml}) == GOLDEN["denominators"]


def test_quantity_timeline(world):
    _, prices, trades, divs, _ = world
    dates = sorted({d for sp in prices.values() for d in sp})
    for sym, qty in (("AAPL", 12.0), ("MSFT", 5.0), ("BND", 8.0), ("XLE", 0.0)):
        qbd, first = growth.build_qty_timeline(qty, [t for t in trades if t["symbol"] == sym], dates,
                                               dividends=[d for d in divs if d["symbol"] == sym])
        assert norm({"qty_by_date": qbd, "first_purchase": first}) == GOLDEN["qty_timeline"][sym]


def test_empty_range_is_none_not_an_empty_series(world):
    assert growth.total_growth([], [], [], None) is None
    assert growth.single_position_growth([], {}, 1.0, [], None) is None

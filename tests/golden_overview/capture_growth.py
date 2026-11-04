"""ONE-OFF golden capture of the INLINE Account Growth computations (run on the pre-extraction commit):

    BA2_CAPTURE_GOLDEN=1 pytest tests/golden_overview/capture_growth.py -q -p no:cacheprovider

Runs the real AccountGrowthTab methods on ``growth_seed`` data and records the locals they draw their series from
(read at function return through ``sys.setprofile``); writes ``growth_golden.json``."""
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.golden_overview import growth_seed as seed

pytestmark = pytest.mark.skipif(not os.environ.get("BA2_CAPTURE_GOLDEN"), reason="one-off capture")
OUT = Path(__file__).with_name("growth_golden.json")


def _locals_at_return(func_name, call):
    grabbed = {}

    def prof(frame, event, arg):
        if event == "return" and frame.f_code.co_name == func_name:
            grabbed.clear()
            grabbed.update(frame.f_locals)
    sys.setprofile(prof)
    try:
        result = call()
    finally:
        sys.setprofile(None)
    return grabbed, result


def test_capture(monkeypatch):
    import ba2_trade_platform.ui.pages.overview as ov
    seed.seed_instruments()
    for name in ("ui", "responsive_echart", "fullscreen_button", "mode_toggle"):
        monkeypatch.setattr(ov, name, MagicMock())
    monkeypatch.setattr(ov, "_stored_label_colors", lambda: {})
    tab = object.__new__(ov.AccountGrowthTab)
    tab._scope = None
    tab._hidden_labels = 0
    tab._range = "Max"
    tab._balance_first = None
    tab._activity_first = None
    tab._raw_range_start = None
    tab._single_account = None
    tab._sess = lambda: {}
    tab._account_ids = []
    monkeypatch.setattr(ov.AccountGrowthTab, "_stored_selection",
                        lambda self, chart, legacy, options: list(options))
    monkeypatch.setattr(ov.AccountGrowthTab, "_stored_singles", lambda self, chart: [])
    monkeypatch.setattr(ov.AccountGrowthTab, "_flag", lambda self, name, default: True)

    out = {"inline_code_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
           "total": {}, "label": {}, "position": {}, "single": {}, "monthly": {}, "denominators": {},
           "qty_timeline": {}}
    pos, prices = seed.positions(), seed.historical_prices()
    trades, divs, bal = seed.trades(), seed.dividends(), seed.balance_history()
    labelled = [p for p in pos if p.symbol != "BND"]      # the label chart auto-adds unknown symbols: keep it quiet

    for name, start in (("max", None), ("from_0915", seed.RANGE_START)):
        tab._range_start = start
        got, _ = _locals_at_return("_render_total_growth_chart",
                                   lambda: tab._render_total_growth_chart(bal, divs, trades))
        out["total"][name] = {k: got[k] for k in ("all_dates", "aligned_balance", "aligned_cash", "aligned_div",
                                                  "aligned_invested", "pnl_pct_data")}
        got, _ = _locals_at_return("_render_growth_by_label_charts",
                                   lambda: tab._render_growth_by_label_charts(labelled, prices, divs, trades))
        out["label"][name] = {k: got[k] for k in ("all_dates", "all_labels", "label_daily_values", "label_cum_divs",
                                                  "label_cum_cash_divs", "label_cum_invested")}
        got, _ = _locals_at_return("_render_growth_by_position_in_label_charts",
                                   lambda: tab._render_growth_by_position_in_label_charts(pos, prices, divs, trades))
        out["position"][name] = {k: got[k] for k in ("all_dates", "sym_daily_values", "sym_cum_divs",
                                                     "sym_cum_cash_divs", "sym_cum_invested", "symbol_info")}
        single = {}
        for sym, qty in (("AAPL", 12.0), ("MSFT", 5.0), ("BND", 8.0)):
            sym_trades = [t for t in trades if t["symbol"] == sym]
            sym_divs = [d for d in divs if d["symbol"] == sym]
            got, _ = _locals_at_return(
                "_render_position_growth_chart_from_data",
                lambda: tab._render_position_growth_chart_from_data(
                    sym, sym_divs, hist_prices=prices[sym], current_qty=qty, filled_trades=sym_trades,
                    avg_entry_price=100.0))
            single[sym] = ({k: got[k] for k in ("all_dates", "values", "invested_data", "pnl_pct_data",
                                                "cumulative_divs", "drip_quantities", "drip_invested")}
                           if "values" in got else None)
        out["single"][name] = single

    tab._range_start = None
    months, income, by_label, labels = tab._compute_monthly_realized(trades, divs)
    out["monthly"] = {"months": months, "income": income, "by_label": by_label, "labels": labels,
                      "symbol_detail": tab._monthly_symbol_detail}
    mg, ml = tab._compute_value_denominators(bal, pos, prices, trades, divs, months)
    out["denominators"] = {"global": mg, "label": ml}
    dates = sorted({d for sp in prices.values() for d in sp})
    for sym, qty in (("AAPL", 12.0), ("MSFT", 5.0), ("BND", 8.0), ("XLE", 0.0)):
        sym_trades = [t for t in trades if t["symbol"] == sym]
        sym_divs = [d for d in divs if d["symbol"] == sym]
        qbd, first = ov._build_qty_timeline(qty, sym_trades, dates, dividends=sym_divs)
        out["qty_timeline"][sym] = {"qty_by_date": qbd, "first_purchase": first}
    OUT.write_text(json.dumps(out, indent=1, sort_keys=True, default=str))

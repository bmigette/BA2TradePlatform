"""Probe screener combinations + Weinstein Stage 2 filter to find a config that
yields >= 30 symbols, for a new FMPRating expert.

Run: .venv\\Scripts\\python.exe test_files\\probe_weinstein_screener.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from ba2_trade_platform.core.StockScreener import StockScreener

BASE = {
    "screener_provider": "fmp",
    "screener_market_cap_max": 0,
    "screener_volume_max": 0,
    "screener_float_min": 0,
    "screener_float_max": 0,
    "screener_price_max": 0,
    "screener_relative_volume_min": 0,
    "screener_price_drop_pct": 0,
    "screener_price_drop_days": 1,
    "screener_sort_metric": "market_cap",
}

SCENARIOS = [
    ("Large-cap >=10B, vol>=2M, price>=20, Stage2", {
        "screener_market_cap_min": 10_000_000_000, "screener_volume_min": 2_000_000,
        "screener_price_min": 20.0, "screener_max_stocks": 60,
        "screener_weinstein_stage2_only": 1}),
    ("Mid+ >=2B, vol>=1M, price>=10, Stage2", {
        "screener_market_cap_min": 2_000_000_000, "screener_volume_min": 1_000_000,
        "screener_price_min": 10.0, "screener_max_stocks": 60,
        "screener_weinstein_stage2_only": 1}),
    ("Broad >=1B, vol>=500k, price>=5, Stage2", {
        "screener_market_cap_min": 1_000_000_000, "screener_volume_min": 500_000,
        "screener_price_min": 5.0, "screener_max_stocks": 80,
        "screener_weinstein_stage2_only": 1}),
]


def run():
    for name, overrides in SCENARIOS:
        settings = {**BASE, **overrides}
        screener = StockScreener(settings)
        out = screener.screen()
        results = out["results"]
        stats = out["stats"]
        print(f"\n{'='*70}\n{name}")
        print(f"  candidates={stats.get('screener_candidates')} "
              f"weinstein_checked={stats.get('weinstein_checked')} "
              f"stage2={stats.get('weinstein_stage2')} -> FINAL {len(results)}")
        syms = [r["symbol"] for r in results]
        print("  symbols:", ", ".join(syms[:40]))
        if len(results) >= 30:
            print(f"  >>> MEETS >=30 target ({len(results)})")


if __name__ == "__main__":
    run()

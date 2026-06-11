"""Probe FMP screener settings for the option experts (dev DB).

Goal 1 (bearish experts: OPT-LongPut/BearPutSpread/BearCallSpread):
    find screener filters that actually surface symbols whose FMP analyst
    consensus is SELL-leaning, since the current megacap list is ~100% BUY.

Goal 2 (overlay experts: OPT-CoveredCall/ProtectivePut):
    find liquid LOW-PRICED (<= $10) underlyings so a ~$1,000 per-instrument cap
    can fund the 100-share lots one option contract requires — and check the
    candidates still carry BUY-leaning consensus (entry rule needs rating
    positive + confidence >= 70).

Run:  .venv\\Scripts\\python.exe test_files\\probe_option_expert_universes.py
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

from ba2_trade_platform.modules.dataproviders.screener.FMPScreenerProvider import FMPScreenerProvider
from ba2_trade_platform.modules.dataproviders.fmp_common import fmp_http_get
from ba2_trade_platform.config import get_app_setting

PROBE_LIMIT = 35  # grade-consensus calls per scenario (rate-limit friendly)
API_KEY = get_app_setting("FMP_API_KEY")


def consensus_signal(symbol: str):
    """FMPRating's signal math (strong ratings x2, dominant side wins)."""
    url = "https://financialmodelingprep.com/api/v4/upgrades-downgrades-consensus"
    try:
        resp = fmp_http_get(url, {"symbol": symbol, "apikey": API_KEY}, symbol=symbol,
                            endpoint="upgrades-downgrades-consensus", timeout=30)
        data = resp.json()
    except Exception as e:
        return None, f"error: {e}"
    if not data:
        return None, "no data"
    g = data[0]
    sb, b, h = g.get("strongBuy", 0), g.get("buy", 0), g.get("hold", 0)
    s, ss = g.get("sell", 0), g.get("strongSell", 0)
    total = sb + b + h + s + ss
    if total < 3:  # min_analysts default
        return None, f"only {total} analysts"
    buy_score, sell_score, hold_score = sb * 2 + b, ss * 2 + s, h
    if buy_score > sell_score and buy_score > hold_score:
        sig = "BUY"
    elif sell_score > buy_score and sell_score > hold_score:
        sig = "SELL"
    else:
        sig = "HOLD"
    conf = round(max(buy_score, sell_score, hold_score) /
                 (buy_score + sell_score + hold_score) * 100, 1)
    return sig, f"conf={conf} (SB{sb}/B{b}/H{h}/S{s}/SS{ss})"


def run_scenario(name: str, filters: dict):
    print(f"\n{'=' * 70}\nSCENARIO: {name}\n  filters: {filters}")
    provider = FMPScreenerProvider()
    rows = provider.screen_stocks(filters)
    print(f"  screener returned {len(rows)} symbols")
    counts = {"BUY": 0, "SELL": 0, "HOLD": 0, "skipped": 0}
    sells, buys = [], []
    for row in rows[:PROBE_LIMIT]:
        sym = row.get("symbol")
        sig, detail = consensus_signal(sym)
        if sig is None:
            counts["skipped"] += 1
            continue
        counts[sig] += 1
        if sig == "SELL":
            sells.append((sym, row.get("price"), detail))
        elif sig == "BUY":
            buys.append((sym, row.get("price"), detail))
    probed = PROBE_LIMIT if len(rows) >= PROBE_LIMIT else len(rows)
    print(f"  probed {probed}: {counts}")
    if sells:
        print("  SELL-rated candidates:")
        for sym, px, d in sells[:12]:
            print(f"    {sym:<6} ${px:<8} {d}")
    if name.startswith("OVERLAY") and buys:
        print("  BUY-rated cheap candidates (overlay universe):")
        for sym, px, d in buys[:12]:
            print(f"    {sym:<6} ${px:<8} {d}")
    return counts, sells, buys


if __name__ == "__main__":
    # Baseline: what the bearish experts see today (megacap-style filters)
    run_scenario("BASELINE megacap (current universe profile)", {
        "market_cap_min": 200_000_000_000, "volume_min": 1_000_000,
        "price_min": 50, "limit": 40, "exchanges": ["NYSE", "NASDAQ"],
    })

    # Bearish candidate A: mid/large caps that dropped hard recently
    run_scenario("BEARISH A: mcap>=2B, price 10-200, 52w-low-ish via price drop", {
        "market_cap_min": 2_000_000_000, "volume_min": 1_000_000,
        "price_min": 10, "price_max": 200, "limit": 120,
        "exchanges": ["NYSE", "NASDAQ"],
        "price_drop_pct": 15.0, "price_drop_days": 30,
    })

    # Bearish candidate B: same but milder drop, larger pool
    run_scenario("BEARISH B: mcap>=2B, drop>=8% over 20d", {
        "market_cap_min": 2_000_000_000, "volume_min": 1_000_000,
        "price_min": 10, "price_max": 200, "limit": 120,
        "exchanges": ["NYSE", "NASDAQ"],
        "price_drop_pct": 8.0, "price_drop_days": 20,
    })

    # Overlay universe: cheap, liquid, still BUY-rated
    run_scenario("OVERLAY: price 3-10, mcap>=300M, vol>=1M (100-share lots ~ <=$1k)", {
        "market_cap_min": 300_000_000, "volume_min": 1_000_000,
        "price_min": 3.0, "price_max": 10.0, "limit": 120,
        "exchanges": ["NYSE", "NASDAQ"],
    })

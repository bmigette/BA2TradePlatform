"""READ-ONLY: how much historical option data can TastyTrade/dxfeed actually serve?

Decides whether ThetaData is needed at all. Three questions, in order of importance:

  1. Do candles exist for a contract that has ALREADY EXPIRED?  <-- make or break.
     Live chains list only current contracts. If dxfeed refuses expired symbols you can
     build history going forward but cannot backtest the past two years.
  2. How far back does a LISTED contract go?
  3. Are `imp_volatility` and `open_interest` actually populated? Those are NULL across
     all 6,757,055 rows of the Alpaca-built cache and are why delta selection and
     min_open_interest are dead in backtest.

Market data only. No orders: every order-mutating SDK method is tripwired.

Run:
  TT_REFRESH_TOKEN=... PYTHONPATH=packages/common:packages/providers:packages/experts \
  venv/bin/python test_files/probe_tastytrade_option_history.py
"""
import asyncio
import os
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_files.probe_tastytrade_live import load_settings, _sandbox_from  # noqa: E402

UNDERLYING = os.environ.get("TT_SYMBOL", "AAPL")
WINDOW_YEARS = float(os.environ.get("TT_YEARS", "3"))
COLLECT_SECONDS = float(os.environ.get("TT_WAIT", "25"))


def tripwire():
    from tastytrade.account import Account

    async def blocked(*a, **k):
        raise RuntimeError("TRIPWIRE: this probe is market-data only")

    for name in ("place_order", "place_complex_order", "delete_order",
                 "replace_order", "edit_order"):
        if hasattr(Account, name):
            setattr(Account, name, blocked)
    print("tripwire armed: no order method can be called\n")


def occ_to_streamer(underlying: str, expiry: date, right: str, strike: float) -> str:
    """dxfeed option streamer symbol: .AAPL250117C150 — strike without trailing zeros."""
    s = f"{strike:.8f}".rstrip("0").rstrip(".")
    return f".{underlying}{expiry:%y%m%d}{right}{s}"


async def collect(session, symbols, start, label, interval="1d"):
    """Subscribe and drain candles for COLLECT_SECONDS. Returns {symbol: [Candle]}."""
    import certifi, ssl as _ssl
    from tastytrade import DXLinkStreamer
    from tastytrade.dxfeed import Candle

    # The websocket picks up a self-signed corporate root from the system store while
    # httpx uses certifi, so REST works and the stream does not. Pin certifi for both.
    ctx = _ssl.create_default_context(cafile=certifi.where())

    got = {s: [] for s in symbols}
    print(f"\n--- {label} ---")
    print(f"  symbols   : {symbols}")
    print(f"  from      : {start:%Y-%m-%d}   interval={interval}")
    try:
        async with DXLinkStreamer(session, ssl_context=ctx) as streamer:
            await streamer.subscribe_candle(symbols, interval, start)

            async def drain():
                async for c in streamer.listen(Candle):
                    base = c.event_symbol.split("{")[0]
                    if base in got:
                        got[base].append(c)

            try:
                await asyncio.wait_for(drain(), timeout=COLLECT_SECONDS)
            except asyncio.TimeoutError:
                pass
    except Exception as e:
        print(f"  !! {type(e).__name__}: {str(e)[:300]}")
        return got

    for sym in symbols:
        rows = [c for c in got[sym] if c.time]
        if not rows:
            print(f"  {sym:<24} NO DATA")
            continue
        rows.sort(key=lambda c: c.time)
        first = datetime.fromtimestamp(rows[0].time / 1000, timezone.utc)
        last = datetime.fromtimestamp(rows[-1].time / 1000, timezone.utc)
        span = (last - first).days
        iv = sum(1 for c in rows if c.imp_volatility is not None)
        oi = sum(1 for c in rows if c.open_interest is not None)
        vol = sum(1 for c in rows if c.volume is not None)
        print(f"  {sym:<24} {len(rows):>5} bars  {first:%Y-%m-%d} -> {last:%Y-%m-%d} "
              f"({span}d, ~{span/365:.1f}y)")
        print(f"  {'':<24}       iv={iv}/{len(rows)}  oi={oi}/{len(rows)}  vol={vol}/{len(rows)}")
        s = rows[0]
        print(f"  {'':<24}       oldest: close={s.close} iv={s.imp_volatility} "
              f"oi={s.open_interest} vol={s.volume}")
    return got


async def main():
    settings = load_settings()
    if not settings.get("refresh_token"):
        sys.exit("no refresh_token — set TT_REFRESH_TOKEN")
    tripwire()

    from tastytrade.session import Session
    from tastytrade.instruments import NestedOptionChain

    session = Session(provider_secret=settings["client_secret"],
                      refresh_token=settings["refresh_token"],
                      is_test=_sandbox_from(settings))
    print(f"connected (sandbox={_sandbox_from(settings)})")

    start = datetime.now(timezone.utc) - timedelta(days=int(365 * WINDOW_YEARS))

    # Q3 baseline: does the underlying itself go back that far?
    await collect(session, [UNDERLYING], start, "UNDERLYING equity (baseline)")

    # Q2: a currently-LISTED contract, taken from the real chain.
    listed = []
    try:
        chains = await NestedOptionChain.get(session, UNDERLYING)
        chain = chains[0] if isinstance(chains, list) else chains
        exp = sorted(chain.expirations, key=lambda e: e.days_to_expiration)
        far = next((e for e in exp if e.days_to_expiration > 200), exp[-1])
        mid = far.strikes[len(far.strikes) // 2]
        listed = [mid.call_streamer_symbol, mid.put_streamer_symbol]
        print(f"\nlisted contract: {far.expiration_date} strike {mid.strike_price} "
              f"({far.days_to_expiration} DTE)")
    except Exception as e:
        print(f"\n!! chain fetch failed: {type(e).__name__}: {str(e)[:200]}")
    if listed:
        await collect(session, listed, start, "LISTED contract (how far back?)")

    # Q1 — THE decisive one: contracts that have ALREADY EXPIRED.
    # Third Friday of a few past months, ATM-ish strikes around the current price.
    expired = []
    for months_ago, strike in ((6, 200.0), (12, 180.0), (24, 150.0)):
        d = date.today() - timedelta(days=30 * months_ago)
        third_friday = next(day for day in
                            (date(d.year, d.month, x) for x in range(15, 22))
                            if day.weekday() == 4)
        expired.append(occ_to_streamer(UNDERLYING, third_friday, "C", strike))
    await collect(session, expired, start, "EXPIRED contracts (THE decisive question)")

    print("\n" + "=" * 72)
    print("READ THE 'EXPIRED contracts' BLOCK FIRST.")
    print("  bars returned      -> dxfeed serves dead contracts; a 2-year backtest cache")
    print("                        can be built from TastyTrade and ThetaData is unnecessary")
    print("  NO DATA everywhere -> listed symbols only; history can only accrue FORWARD,")
    print("                        so ThetaData (or the existing Alpaca cache) is still")
    print("                        required for the past")
    print("Then check iv/oi counts: those two fields are the whole reason for the exercise.")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())

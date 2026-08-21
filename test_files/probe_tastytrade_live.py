"""READ-ONLY live probe of the TastyTrade adapter.

Section E was written entirely against mocks -- there was no TastyTrade account in the
live DB -- and its adversarial review flagged several findings as "unverifiable without
a live account". This answers those.

SAFETY
  * Credentials are read from a BACKUP db with sqlite `mode=ro`. Nothing is written.
  * The platform's DB layer is never initialised; the account object is built the way
    the test suite builds it, so no create_all / no migration / no writes.
  * A TRIPWIRE monkeypatches every order-mutating SDK method to raise. Even a bug in
    this script cannot open, modify or cancel a position. `place_order` is tripwired
    too -- including dry runs -- because "never open or close trades" is the rule.
  * No secret is ever printed.

Run:  venv/bin/python test_files/probe_tastytrade_live.py
"""
import asyncio
import json
import os
import sqlite3
import sys
import threading
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BACKUP = os.path.expanduser(os.environ.get(
    "TT_BACKUP", "~/Downloads/db.sqlite.bak-20260821-073413-pre-spcx-label"))
ACCOUNT_ROW_ID = int(os.environ.get("TT_ACCOUNT_ROW_ID", "0")) or None


def _sandbox_from(settings):
    """Mirror TastyTradeAccount._is_sandbox, NOT naive bool().

    A legacy row holding the literal string "None" (the str(None) write bug) coerces
    to True under bool(), which silently points a PRODUCTION account at the sandbox --
    exactly the bug Task 37 fixed, and exactly the bug an earlier version of this
    probe reproduced. Treat "None"/""/None as unset, and default to production.

    TT_SANDBOX=1 forces sandbox, for a certification token.
    """
    if os.environ.get("TT_SANDBOX", "").strip().lower() in ("1", "true", "yes"):
        return True
    raw = settings.get("is_test")
    if raw is None or (isinstance(raw, str) and raw.strip().lower() in ("", "none")):
        return False
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(raw)


def load_settings():
    """Credentials, from env if given, else read-only out of the backup. None printed.

    Env override (nothing touches the DB, nothing is persisted):
        TT_CLIENT_SECRET  TT_REFRESH_TOKEN  TT_ACCOUNT_NUMBER  [TT_SANDBOX=1]
    """
    env = {k: v for k, v in (
        ("client_secret", os.environ.get("TT_CLIENT_SECRET")),
        ("refresh_token", os.environ.get("TT_REFRESH_TOKEN")),
        ("account_id", os.environ.get("TT_ACCOUNT_NUMBER"))) if v}
    if len(env) == 3:
        print("credentials: entirely from environment (DB not read)")
        globals()["ACCOUNT_ROW_ID"] = ACCOUNT_ROW_ID or 0
        return env

    con = sqlite3.connect(f"file:{BACKUP}?mode=ro", uri=True)
    try:
        acct_id = ACCOUNT_ROW_ID
        if acct_id is None:
            found = con.execute(
                "SELECT id FROM accountdefinition WHERE lower(provider) LIKE '%tasty%' "
                "ORDER BY id").fetchall()
            if not found:
                sys.exit("no TastyTrade account in that backup")
            acct_id = found[0][0]
        globals()["ACCOUNT_ROW_ID"] = acct_id
        rows = con.execute(
            "SELECT key, value_str, value_json, value_float FROM accountsetting "
            "WHERE account_id = ?", (acct_id,)).fetchall()
    finally:
        con.close()
    out = {}
    for k, s, j, f in rows:
        if s is not None:
            out[k] = s
        elif j is not None:
            try:
                out[k] = json.loads(j)
            except Exception:
                out[k] = j
        elif f is not None:
            out[k] = f
    if env:
        out.update(env)
        print(f"credentials: {sorted(env)} overridden from environment, "
              f"rest from the backup")
    return out


def install_tripwire():
    """Make it physically impossible for this process to touch an order."""
    from tastytrade.account import Account

    def deny(name):
        async def _blocked(*a, **k):
            raise RuntimeError(f"TRIPWIRE: {name} blocked - this probe is read-only")
        return _blocked

    for name in ("place_order", "place_complex_order", "delete_order",
                 "replace_order", "edit_order"):
        if hasattr(Account, name):
            setattr(Account, name, deny(name))
    print("tripwire armed: place_order / place_complex_order / delete_order / "
          "replace_order / edit_order all raise\n")


def build_account(settings):
    """A real, connected TastyTradeAccount with NO database involvement."""
    from ba2_trade_platform.modules.accounts.TastyTradeAccount import TastyTradeAccount

    acct = object.__new__(TastyTradeAccount)
    acct.id = ACCOUNT_ROW_ID
    acct._authentication_error = None
    acct._session = None
    acct._account = None
    acct._settings_cache = dict(settings)
    acct._loop = asyncio.new_event_loop()
    acct._loop_thread = threading.Thread(target=acct._loop.run_forever, daemon=True)
    acct._loop_thread.start()
    with TastyTradeAccount._CACHE_LOCK:
        TastyTradeAccount._GLOBAL_PRICE_CACHE[acct.id] = {}

    # is_test resolved the way the production code does -- NOT naive bool().
    is_test = _sandbox_from(settings)
    acct._is_sandbox = lambda: is_test
    print(f"sandbox mode: {is_test}   (env TT_SANDBOX=1 forces sandbox)")

    acct._connect()
    return acct


def check(label, fn):
    print(f"\n{'=' * 72}\n{label}\n{'=' * 72}")
    try:
        fn()
    except Exception as e:
        print(f"  !! {type(e).__name__}: {e}")
        traceback.print_exc(limit=3)


def main():
    settings = load_settings()
    missing = [k for k in ("client_secret", "refresh_token", "account_id")
               if not settings.get(k)]
    if missing:
        sys.exit(f"backup is missing: {missing}")

    install_tripwire()
    acct = build_account(settings)
    print(f"connected: account_number={acct._account.account_number} "
          f"type={acct._account.margin_or_cash}")

    # ---- D1: does initial_requirement really arrive NEGATIVE for a Debit? -------
    def d1():
        from tastytrade.account import Account
        report = acct._run_async(acct._account.get_margin_requirements(acct._session))
        print(f"  margin report type: {type(report).__name__}")
        groups = getattr(report, "groups", None) or []
        print(f"  groups: {len(groups)}")
        for g in groups[:6]:
            sym = getattr(g, "description", None) or getattr(g, "code", "?")
            ir = getattr(g, "initial_requirement", None)
            mr = getattr(g, "maintenance_requirement", None)
            print(f"    {str(sym)[:28]:<28} initial={ir!r:>14}  maint={mr!r}")
        signs = [getattr(g, "initial_requirement", None) for g in groups]
        neg = [s for s in signs if s is not None and s < 0]
        print(f"  -> negative initial_requirement values: {len(neg)}/{len(signs)}")
        print("     (if ANY are negative, the abs() in get_symbol_margin_info is "
              "load-bearing, confirming review finding D1)")

    # ---- D6: is is_fractional_quantity_eligible ever None in the wild? ---------
    def d6():
        from tastytrade.instruments import Equity
        syms = ["AAPL", "MSFT", "BRK/A", "SPY", "GME", "F", "T", "KO"]
        eqs = acct._run_async(Equity.get(acct._session, syms, page_offset=None))
        got = {e.symbol: e for e in eqs}
        print(f"  requested {len(syms)}, returned {len(got)}")
        counts = {"True": 0, "False": 0, "None": 0}
        for s in syms:
            e = got.get(s)
            if e is None:
                print(f"    {s:<8} ABSENT from response")
                continue
            v = e.is_fractional_quantity_eligible
            counts[str(v)] += 1
            print(f"    {s:<8} is_fractional_quantity_eligible={v!r}")
        print(f"  -> {counts}")
        print("     (a None here proves the tri-state matters and that folding it "
              "into False loses information)")

    # ---- D7: is there a GENERIC (symbol is None) EQUITY precision row? ---------
    def d7():
        from tastytrade.instruments import get_quantity_decimal_precisions
        from tastytrade.order import InstrumentType
        rows = acct._run_async(get_quantity_decimal_precisions(acct._session))
        print(f"  rows: {len(rows)}")
        by_type = {}
        for r in rows:
            by_type.setdefault(r.instrument_type, []).append(r)
        for t, rs in by_type.items():
            gen = [r for r in rs if r.symbol is None]
            print(f"    {str(t):<34} {len(rs):>3} rows, {len(gen)} generic")
        eq = by_type.get(InstrumentType.EQUITY, [])
        generic = [r for r in eq if r.symbol is None]
        per_sym = [r for r in eq if r.symbol is not None]
        print(f"\n  EQUITY: {len(eq)} rows, generic={len(generic)}, per-symbol={len(per_sym)}")
        for r in generic:
            inc = float(10 ** -int(r.minimum_increment_precision))
            print(f"    GENERIC value={r.value} precision={r.minimum_increment_precision}"
                  f"  -> increment={inc}")
        for r in per_sym[:8]:
            print(f"    {r.symbol:<8} value={r.value} "
                  f"precision={r.minimum_increment_precision}")
        print("\n  -> the adapter takes ONLY the generic row (symbol is None) and does"
              "\n     min_trade_increment = increment if fractionable else 1.0")
        held = [x.symbol for x in (acct.get_positions() or [])]
        fracheld = [(x.symbol, x.qty) for x in (acct.get_positions() or [])
                    if x.qty != int(x.qty)]
        print(f"  -> positions actually held at a FRACTIONAL quantity: {len(fracheld)}/{len(held)}")
        for s, q in fracheld[:6]:
            print(f"       {s:<8} qty={q}")

    # ---- market sessions: the real MarketStatus values -------------------------
    def sessions():
        from tastytrade.market_sessions import (ExchangeType, get_market_sessions,
                                                get_market_holidays)
        # NB: the member is NYSE (value "Equity"), there is no ExchangeType.EQUITY
        ms = acct._run_async(get_market_sessions(acct._session, [ExchangeType.NYSE]))
        for s in ms or []:
            print(f"  status={s.status!r}  open_at={s.open_at}  close_at={s.close_at}")
            print(f"  start_at={s.start_at}  close_at_ext={s.close_at_ext}")
            nxt = s.next_session
            if nxt:
                print(f"  next_session: open_at={nxt.open_at} close_at={nxt.close_at}")
        cal = acct._run_async(get_market_holidays(acct._session))
        print(f"  holidays: {len(cal.holidays)}  half_days: {len(cal.half_days)}")

    # ---- the adapter's own seams, end to end -----------------------------------
    def snapshot():
        s = acct.get_account_snapshot()
        for f in ("equity", "net_liquidation", "cash", "buying_power",
                  "long_market_value", "short_market_value", "margin_multiplier",
                  "supports_fractional"):
            print(f"    {f:<22} {getattr(s, f, '<missing>')!r}")

    def positions():
        p = acct.get_positions()
        print(f"  get_positions() -> {type(p).__name__}, "
              f"{'None (FETCH FAILED)' if p is None else f'{len(p)} rows'}")
        for x in (p or [])[:8]:
            print(f"    {x.symbol:<8} qty={x.qty} side={x.side} "
                  f"avg={getattr(x, 'avg_entry_price', None)}")

    def margin_info():
        held = [x.symbol for x in (acct.get_positions() or [])][:5]
        syms = held or ["AAPL", "MSFT"]
        mi = acct.get_symbol_margin_info(syms)
        print(f"  requested {syms} -> {len(mi)} described")
        for s, m in mi.items():
            print(f"    {s:<8} rate={m.initial_margin_rate!r} bp={m.bp_factor!r} "
                  f"frac={m.fractionable!r} incr={m.min_trade_increment!r} "
                  f"src={m.source!r} marginable={m.marginable!r}")
        for s in syms:
            if s not in mi:
                print(f"    {s:<8} OMITTED (broker could not describe it)")

    def transfers():
        from datetime import date, timedelta
        end = date(2026, 8, 21)
        t = acct.get_cash_transfers(start_date=end - timedelta(days=365), end_date=end)
        print(f"  {len(t)} transfers in 365d")
        for x in t[:10]:
            print(f"    {x.event_date} {x.event_type:<12} {x.amount:>10.2f} "
                  f"id={x.external_id} income={x.is_income}")
        neg = [x for x in t if x.event_type == "DIVIDEND" and x.amount < 0]
        print(f"  -> negative DIVIDEND rows: {len(neg)} (must be 0)")

    check("D1  MarginReportEntry.initial_requirement sign (is abs() load-bearing?)", d1)
    check("D6  is_fractional_quantity_eligible: True / False / None in the wild", d6)
    check("D7  generic vs per-symbol quantity precision rows", d7)
    check("MARKET SESSIONS  real MarketStatus values and session bounds", sessions)
    check("SEAM  get_account_snapshot()", snapshot)
    check("SEAM  get_positions()  (tri-state: None = failed, [] = flat)", positions)
    check("SEAM  get_symbol_margin_info()", margin_info)
    check("SEAM  get_cash_transfers()  (I5 dividend-tax netting)", transfers)

    print("\ndone - no order was placed, modified or cancelled (tripwire armed).")


if __name__ == "__main__":
    main()

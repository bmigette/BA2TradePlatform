#!/usr/bin/env python
"""Warm the historical options parquet cache from TastyTrade/dxfeed. Resumable.

WHAT IT DOES. For each underlying it enumerates the option contracts expiring in the window,
groups them by expiry, and downloads each expiry's daily candles — including
``imp_volatility`` and ``open_interest``, the two fields that are NULL across all 6,757,055
rows of the incumbent cache and that therefore make ``method="delta"`` select nothing and
``min_open_interest`` reject entire chains.

Each (underlying, expiry) is one UNIT OF WORK and one parquet partition. Interrupting the run
— Ctrl-C, a dropped socket, a sleeping laptop — loses at most the unit in flight, never
anything already written: the parquet lands via temp+rename, and its ``_manifest.json``
(the completion marker) is written LAST, also via temp+rename. A unit with no manifest is
simply redone; a unit with one is skipped, whether it holds rows or is recorded as genuinely
EMPTY.

    # See the plan without downloading anything or writing a file:
    venv/bin/python tools/warm_options_history.py --symbols AAPL --dry-run

    # Prove it on one name, one expiry:
    venv/bin/python tools/warm_options_history.py --symbols AAPL --limit 1

    # The real thing (2023-01-01 -> today). Ctrl-C whenever; re-run the SAME command to
    # pick up exactly where it stopped.
    venv/bin/python tools/warm_options_history.py \
        --symbols-file tools/options_universe_top100.txt

CREDENTIALS are read from the environment (TT_CLIENT_SECRET / TT_REFRESH_TOKEN, optionally
TT_SANDBOX=1) or, failing that, READ-ONLY from a platform sqlite DB via ``--db``. Nothing is
ever written to any database and no token is ever printed.

This writes to a NEW tree and never reads, migrates or touches the incumbent ~10 GB
``options_history.sqlite``.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ba2_common.core.interfaces.OptionsDataProviderInterface import (  # noqa: E402
    OptionContractMeta,
)
from ba2_providers.options.tastytrade import StreamInterrupted  # noqa: E402
from ba2_providers.options.parquet_store import (  # noqa: E402
    OptionHistoryParquetStore, PartitionState,
)

DEFAULT_START = "2023-01-01"
#: Rough per-unit cost used only for the dry-run estimate; the real figure is measured and
#: reported live once the run starts.
ESTIMATE_SECONDS_PER_UNIT = 8.0

_LAST_PLAN: Optional["Plan"] = None


# --------------------------------------------------------------------------- #
# plan / stats
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class WorkUnit:
    """One (underlying, expiry) chain — one download, one parquet partition."""
    underlying: str
    expiry: date
    contracts: List[OptionContractMeta]


@dataclass
class Plan:
    units: List[WorkUnit] = field(default_factory=list)
    units_done: int = 0
    units_empty: int = 0
    per_symbol: Dict[str, Dict[str, int]] = field(default_factory=dict)
    #: symbols whose CONTRACT DISCOVERY itself raised (not a fetch failure -- a fetch failure
    #: is a WorkUnit that fails at run_units time and is reported in RunStats.units_failed).
    #: 2026-08-25: 'BF-B'.upper() failed to round-trip through occ_symbol/parse_occ (the OCC
    #: root pattern is letters+digits only), an unhandled ValueError that used to propagate
    #: straight through this loop and kill the ENTIRE worker process -- abandoning every other
    #: symbol in that worker's chunk, not just the one bad ticker. See discovery_failed below.
    discovery_failed: Dict[str, str] = field(default_factory=dict)

    @property
    def units_pending(self) -> int:
        return len(self.units)

    @property
    def contracts_pending(self) -> int:
        return sum(len(u.contracts) for u in self.units)


@dataclass
class RunStats:
    units_written: int = 0
    units_empty: int = 0
    units_failed: int = 0
    rows: int = 0
    empty_contracts: int = 0


def last_plan() -> Optional[Plan]:
    """The plan built by the most recent :func:`main` call (for tests / introspection)."""
    return _LAST_PLAN


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="warm_options_history",
        description="Resumably warm the historical options parquet cache from dxfeed.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Interrupt with Ctrl-C at any time; re-run the same command to resume.")
    p.add_argument("--symbols", help="Comma-separated underlyings, e.g. AAPL,MSFT.")
    p.add_argument("--symbols-file",
                   help="File with one underlying per line ('#' comments allowed), "
                        "e.g. tools/options_universe_top100.txt.")
    p.add_argument("--start", default=DEFAULT_START, help=f"Window start (default {DEFAULT_START}).")
    p.add_argument("--end", help="Window end (default: today).")
    p.add_argument("--out", help="Store root (default: CACHE_FOLDER/TastyTradeOptionsProvider).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print exactly what would be fetched and write NOTHING. Contract "
                        "discovery still runs so the counts are real (free under the default "
                        "--discovery synthetic, one read-only listing call under 'rest'); no "
                        "bars are downloaded and no file is created.")
    p.add_argument("--limit", type=int,
                   help="Stop after this many units of work (counts only units that would "
                        "actually be fetched, never skipped ones).")
    p.add_argument("--strike-band-pct", type=float, default=40.0,
                   help="Keep strikes within this %% of the underlying's price range "
                        "(default 40). Only used by --discovery synthetic.")
    p.add_argument("--max-contracts", type=int,
                   help="Cap contracts per expiry, keeping the strikes nearest the money.")
    p.add_argument("--discovery", choices=("rest", "synthetic"), default="synthetic",
                   help="'synthetic' (default) generates a Friday x strike-ladder grid and "
                        "needs NO listing endpoint -- this is the mode that works with a "
                        "personal OAuth app. 'rest' lists the real chain via "
                        "/instruments/equity-options, which returns 403 'Token has "
                        "insufficient scopes' for personal OAuth apps regardless of "
                        "parameters; the three offered scopes (read/trade/openid) cannot "
                        "grant it, so 'rest' needs a different credential type.")
    p.add_argument("--rate-limit", type=float, default=1.0,
                   help="Seconds to pause between units (default 1.0). Be polite.")
    p.add_argument("--max-retries", type=int, default=4,
                   help="Attempts per unit before giving up on it (default 4). A unit that "
                        "never completes leaves NO manifest, so the next run redoes it.")
    p.add_argument("--backoff", type=float, default=5.0,
                   help="First backoff in seconds; doubles each retry (default 5).")
    p.add_argument("--progress-every", type=int, default=10,
                   help="Emit a progress+ETA line every N units (default 10).")
    p.add_argument("--batch-size", type=int, default=50,
                   help="dxfeed symbols per subscription (default 50).")
    p.add_argument("--strict-snapshot", action="store_true",
                   help="Require an explicit end-of-snapshot per contract; treat silence as "
                        "unknown rather than as empty. Slower and safer.")
    p.add_argument("--db", help="Read-only sqlite path to load TastyTrade credentials from "
                                "when the environment does not supply them.")
    p.add_argument("--account-id", type=int, help="Account row id inside --db.")
    return p.parse_args(list(argv) if argv is not None else None)


def resolve_symbols(ns: argparse.Namespace) -> List[str]:
    raw: List[str] = []
    if ns.symbols:
        raw.extend(ns.symbols.split(","))
    if ns.symbols_file:
        with open(ns.symbols_file) as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    raw.append(line)
    out: List[str] = []
    for s in raw:
        s = s.strip().upper()
        if s and s not in out:
            out.append(s)
    return out


# --------------------------------------------------------------------------- #
# credentials (read-only; never printed, never written)
# --------------------------------------------------------------------------- #
def load_credentials(db_path: Optional[str] = None,
                     account_id: Optional[int] = None) -> Dict[str, object]:
    """TastyTrade credentials from the environment, else READ-ONLY from a platform sqlite.

    Mirrors ``test_files/probe_tastytrade_live.py``. Nothing is written and no value is
    returned to any log — callers must not print this dict.
    """
    env = {k: v for k, v in (
        ("client_secret", os.environ.get("TT_CLIENT_SECRET")),
        ("refresh_token", os.environ.get("TT_REFRESH_TOKEN"))) if v}
    if len(env) == 2:
        env["is_test"] = os.environ.get("TT_SANDBOX", "").strip().lower() in ("1", "true", "yes")
        return env
    if not db_path:
        raise SystemExit(
            "No TastyTrade credentials. Set TT_CLIENT_SECRET and TT_REFRESH_TOKEN "
            "(and TT_SANDBOX=1 for the certification environment), or pass --db "
            "<platform sqlite> to read them read-only from an account's settings.")
    # mode=ro is belt; immutable=1 is braces — neither this process nor sqlite may write.
    con = sqlite3.connect(f"file:{os.path.expanduser(db_path)}?mode=ro&immutable=1", uri=True)
    try:
        acct = account_id
        if acct is None:
            found = con.execute(
                "SELECT id FROM accountdefinition WHERE lower(provider) LIKE '%tasty%' "
                "ORDER BY id").fetchall()
            if not found:
                raise SystemExit(f"no TastyTrade account in {db_path}")
            acct = found[0][0]
        rows = con.execute(
            "SELECT key, value_str, value_json, value_float FROM accountsetting "
            "WHERE account_id = ?", (acct,)).fetchall()
    finally:
        con.close()
    out: Dict[str, object] = {}
    for k, s, j, f in rows:
        if s is not None:
            out[k] = s
        elif j is not None:
            try:
                out[k] = json.loads(j)
            except ValueError:
                out[k] = j
        elif f is not None:
            out[k] = f
    out.update(env)
    return out


def _sandbox_from(settings: Dict[str, object]) -> bool:
    """Mirror TastyTradeAccount._is_sandbox, NOT bare bool().

    A legacy row holding the literal string "None" coerces to True under bool(), which
    silently points a PRODUCTION account at the sandbox.
    """
    if os.environ.get("TT_SANDBOX", "").strip().lower() in ("1", "true", "yes"):
        return True
    raw = settings.get("is_test")
    if raw is None or (isinstance(raw, str) and raw.strip().lower() in ("", "none")):
        return False
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(raw)


def build_provider(ns: argparse.Namespace):  # pragma: no cover - network
    """A real provider with a lazily-created TastyTrade session."""
    from ba2_providers.options.tastytrade import TastyTradeOptionsProvider

    def session_factory():
        from tastytrade.session import Session
        settings = load_credentials(ns.db, ns.account_id)
        missing = [k for k in ("client_secret", "refresh_token") if not settings.get(k)]
        if missing:
            raise SystemExit(f"TastyTrade credentials incomplete: missing {missing}")
        return Session(provider_secret=settings["client_secret"],
                       refresh_token=settings["refresh_token"],
                       is_test=_sandbox_from(settings))

    return TastyTradeOptionsProvider(session_factory=session_factory,
                                     batch_size=ns.batch_size,
                                     strict_snapshot=ns.strict_snapshot)


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
def discover(provider, store: OptionHistoryParquetStore, underlying: str,
             start: date, end: date, ns: argparse.Namespace,
             persist: bool) -> List[OptionContractMeta]:
    """The contract universe for ``underlying``, from disk when the cache covers the window.

    ``persist=False`` (a dry run) still discovers — the counts must be real — but writes
    nothing, not even this cache.
    """
    cached = store.read_contracts(underlying, start, end)
    if cached is not None:
        return cached
    if ns.discovery == "synthetic":
        contracts = _synthetic_contracts(underlying, start, end, ns)
    else:
        try:
            contracts = provider.discover_contracts(
                underlying, expiry_gte=start, expiry_lte=end,
                max_contracts=ns.max_contracts)
        except Exception as e:
            # /instruments/equity-options returns 403 "Token has insufficient scopes" for a
            # personal OAuth app, with or without with-expired, and none of the three offered
            # scopes (read/trade/openid) grants it -- openid is OpenID Connect identity only.
            # Do not let that read as "this symbol has no contracts": say which mode works.
            if "403" in str(e) or "insufficient scope" in str(e).lower():
                raise SystemExit(
                    f"--discovery rest cannot list contracts for {underlying}: "
                    f"/instruments/equity-options returned 403 (insufficient scopes). A "
                    f"personal OAuth app cannot reach that endpoint. Re-run with "
                    f"--discovery synthetic, which needs no listing endpoint.") from e
            raise
    if persist:
        store.write_contracts(underlying, contracts, start, end)
    return contracts


def _synthetic_contracts(underlying: str, start: date, end: date,
                         ns: argparse.Namespace) -> List[OptionContractMeta]:
    """A Friday x strike-ladder grid, sized from the LOCAL daily OHLCV parquet cache.

    Needs no listing endpoint. Strikes that never existed simply come back as empty
    snapshots and are recorded as empty — a wasted request, never fabricated data.
    """
    from ba2_providers.options.tastytrade import (
        expiry_calendar, occ_symbol, parse_occ, strike_ladder,
    )
    low, high = _price_range(underlying, start, end)
    strikes = strike_ladder(low, high, band_pct=ns.strike_band_pct)
    out: List[OptionContractMeta] = []
    for expiry in expiry_calendar(start, end):
        for strike in strikes:
            for right in ("C", "P"):
                out.append(parse_occ(occ_symbol(underlying, expiry, right, strike)))
    return out


def _price_range(underlying: str, start: date, end: date):
    """(min, max) daily close over the window, from the local OHLCV parquet cache."""
    import pandas as pd
    from ba2_common.core.native_cache import find_timeseries_path

    for provider_dir in ("FMPOHLCVProvider", "AlpacaOHLCVProvider", "YFinanceDataProvider",
                         "AlphaVantageOHLCVProvider"):
        path = find_timeseries_path(provider_dir, underlying, "1d")
        if not path:
            continue
        df = pd.read_parquet(path)
        col = "Close" if "Close" in df.columns else "close"
        dcol = "Date" if "Date" in df.columns else "date"
        d = pd.to_datetime(df[dcol]).dt.tz_localize(None)
        sel = df[(d >= pd.Timestamp(start)) & (d <= pd.Timestamp(end))][col].dropna()
        if len(sel):
            return float(sel.min()), float(sel.max())
    raise SystemExit(
        f"--discovery synthetic needs a local daily OHLCV parquet for {underlying} to size "
        f"the strike ladder, and none was found. Build it first, or use --discovery rest.")


def build_plan(provider, store: OptionHistoryParquetStore, symbols: Sequence[str],
               start: date, end: date, ns: argparse.Namespace, *, persist: bool,
               log: Optional[Callable[[str], None]] = None) -> Plan:
    log = log or print
    plan = Plan()
    budget = ns.limit if ns.limit and ns.limit > 0 else None
    for symbol in symbols:
        try:
            contracts = discover(provider, store, symbol, start, end, ns, persist)
        except Exception as e:
            # A per-symbol discovery bug (e.g. an underlying occ_symbol/parse_occ can't
            # round-trip) must cost exactly that symbol, never the rest of the batch --
            # SystemExit (discover()'s deliberate 403/scope-refusal signal, a systemic
            # problem every symbol would hit identically) is a BaseException, not an
            # Exception, so it is NOT caught here and still halts the whole run as intended.
            plan.discovery_failed[symbol] = str(e)
            log(f"  [{symbol}] discovery failed, skipping this symbol: {e}")
            continue
        by_expiry: Dict[date, List[OptionContractMeta]] = {}
        for c in contracts:
            by_expiry.setdefault(c.expiry, []).append(c)

        done = empty = pending = 0
        for expiry in sorted(by_expiry):
            state = store.partition_state(symbol, expiry, start, end)
            if state is PartitionState.COMPLETE:
                done += 1
                continue
            if state is PartitionState.EMPTY:
                empty += 1
                continue
            pending += 1
            # The limit is spent only on work that would actually be FETCHED, so
            # `--limit 1` against a mostly-warm store still makes progress.
            if budget is None or len(plan.units) < budget:
                plan.units.append(WorkUnit(symbol, expiry,
                                           sorted(by_expiry[expiry],
                                                  key=lambda c: c.occ_symbol)))
        plan.units_done += done
        plan.units_empty += empty
        plan.per_symbol[symbol] = {
            "expiries": len(by_expiry), "done": done, "empty": empty, "pending": pending,
            "contracts": len(contracts),
        }
        if budget is not None and len(plan.units) >= budget:
            break
    return plan


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #
def _fmt_secs(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _fmt_bytes(n: int) -> str:
    x = float(n)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if x < 1024 or unit == "TB":
            return f"{x:.1f} {unit}" if unit != "bytes" else f"{int(x)} bytes"
        x /= 1024
    return f"{x:.1f} TB"  # pragma: no cover


def _is_transient(e: Exception) -> bool:
    """A retryable network/rate-limit condition vs a permanent one (auth, scope, bad request)
    that will fail identically on every attempt.

    Mirrors ``fetch_options.py``'s ``_is_transient`` (the FMP-style retry contract already
    proven elsewhere in this repo): a fixed, evidence-backed signature list rather than
    "anything is retryable". Without this distinction, a dead credential burned through every
    unit's full retry budget with backoff before giving up -- on the 2026-08-25 TastyTrade
    403 ("Token has insufficient scopes for this request"), that would have meant
    ``max_retries`` attempts x doubling backoff repeated on EVERY one of 825 symbols' worth of
    units before the run finished failing, instead of once.
    """
    if isinstance(e, StreamInterrupted):
        # The tool's own "the candle stream died mid-fetch" signal -- always transient
        # regardless of wording; fetch_bars_detailed already tried to absorb it internally
        # (see tastytrade.py), so one reaching here means the socket genuinely could not
        # recover and a fresh subscription attempt is exactly the right response.
        return True
    s = repr(e)
    return any(m in s for m in (
        "RemoteDisconnected", "Connection aborted", "ConnectionError", "ConnectionResetError",
        "timed out", "Timeout", "Max retries", "TooManyRequests", "429",
        "502", "503", "504", "Temporarily", "rate limit", "Rate limit"))


def run_units(plan: Plan, provider, store: OptionHistoryParquetStore,
              start: date, end: date, ns: argparse.Namespace, *,
              clock: Callable[[], datetime], sleep: Callable[[float], None],
              log: Callable[[str], None]) -> RunStats:
    stats = RunStats()
    total = len(plan.units)
    t0 = clock()
    for i, unit in enumerate(plan.units, 1):
        bars, empties = [], set()
        pending = list(unit.contracts)
        completed = False
        backoff = ns.backoff

        for attempt in range(1, max(1, ns.max_retries) + 1):
            try:
                batch = provider.fetch_bars_detailed(pending, start=start, end=end)
            except KeyboardInterrupt:
                # Deliberate: propagate immediately. Everything already written is whole,
                # and the unit in flight simply has no manifest, so it is redone.
                raise
            except Exception as e:  # noqa: BLE001 — classified below; never silently swallowed
                if not _is_transient(e):
                    # Fail THIS unit at once -- retrying a permanent error (auth, scope,
                    # malformed request) just burns the whole backoff schedule for an
                    # identical result every time. The run itself keeps going (matches
                    # test_a_failing_unit_does_not_abort_the_rest_of_the_run): a symbol-
                    # specific permanent error must not be conflated with an account-wide
                    # one, and if it IS account-wide (a dead token), every subsequent unit
                    # fails just as fast rather than each paying the full retry cost too.
                    log(f"  [{unit.underlying} {unit.expiry}] attempt {attempt} failed "
                        f"(permanent, not retrying): {type(e).__name__}: {e}")
                    break
                log(f"  [{unit.underlying} {unit.expiry}] attempt {attempt} failed: "
                    f"{type(e).__name__}: {e}")
            else:
                bars.extend(batch.bars)
                empties |= set(batch.empty)
                if not batch.unresolved:
                    completed = True
                    break
                # Re-request ONLY what did not answer: the bars already received are good
                # work and re-subscribing the whole chain would throw them away on every
                # socket blip.
                unresolved = set(batch.unresolved)
                pending = [c for c in pending if c.occ_symbol in unresolved]
                log(f"  [{unit.underlying} {unit.expiry}] attempt {attempt}: "
                    f"{len(unresolved)} contract(s) unresolved, retrying those")
            if attempt < max(1, ns.max_retries):
                sleep(backoff)
                backoff *= 2

        if not completed:
            # No manifest is written, so the whole unit is redone on the next run. That is
            # strictly better than persisting a partial chain that no re-run revisits.
            stats.units_failed += 1
            log(f"  [{unit.underlying} {unit.expiry}] GIVING UP after {ns.max_retries} "
                f"attempts — left unfetched, re-run to retry")
        else:
            manifest = store.write_partition(unit.underlying, unit.expiry, bars, start, end,
                                             empty_contracts=sorted(empties))
            stats.rows += manifest["rows"]
            stats.empty_contracts += len(empties)
            if manifest["status"] == "empty":
                stats.units_empty += 1
            else:
                stats.units_written += 1

        if ns.rate_limit:
            sleep(ns.rate_limit)

        if ns.progress_every and (i % ns.progress_every == 0 or i == total):
            elapsed = (clock() - t0).total_seconds()
            per_unit = elapsed / i if i else 0.0
            eta = per_unit * (total - i)
            log(f"progress {i}/{total} units  ({stats.rows} rows, "
                f"{stats.units_empty} empty, {stats.units_failed} failed)  "
                f"elapsed {_fmt_secs(elapsed)}  ETA {_fmt_secs(eta)}")
    return stats


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def print_plan(plan: Plan, store: OptionHistoryParquetStore, symbols: Sequence[str],
               start: date, end: date, ns: argparse.Namespace,
               log: Callable[[str], None]) -> None:
    log("=" * 78)
    log("DRY RUN — no bars downloaded, no file written (discovery is read-only)")
    log("=" * 78)
    log(f"store root : {store.root}")
    log(f"window     : {start.isoformat()} .. {end.isoformat()}")
    log(f"symbols    : {len(symbols)}  ({', '.join(symbols[:12])}"
        f"{' ...' if len(symbols) > 12 else ''})")
    log(f"discovery  : {ns.discovery}")
    log("")
    log(f"{'symbol':<8}{'expiries':>10}{'done':>8}{'empty':>8}{'to fetch':>10}"
        f"{'contracts':>12}")
    for symbol in symbols:
        s = plan.per_symbol.get(symbol)
        if not s:
            continue
        log(f"{symbol:<8}{s['expiries']:>10}{s['done']:>8}{s['empty']:>8}"
            f"{s['pending']:>10}{s['contracts']:>12}")
    log("")
    log(f"TOTAL      : {plan.units_pending} units to fetch "
        f"({plan.units_done} already complete, {plan.units_empty} already known empty), "
        f"{plan.contracts_pending} contracts")
    est = plan.units_pending * (ESTIMATE_SECONDS_PER_UNIT + ns.rate_limit)
    log(f"estimate   : ~{_fmt_secs(est)} wall clock at ~{ESTIMATE_SECONDS_PER_UNIT:.0f}s/unit "
        f"+ {ns.rate_limit}s rate limit")
    if plan.units:
        u = plan.units[0]
        log(f"would write: {store.bars_path(u.underlying, u.expiry)}")
        log(f"        and: {store.manifest_path(u.underlying, u.expiry)}")
    log("Re-run without --dry-run to start. Ctrl-C is safe at any point.")


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def main(argv: Optional[Sequence[str]] = None, *, provider=None, store=None,
         clock: Optional[Callable[[], datetime]] = None,
         sleep: Optional[Callable[[float], None]] = None,
         log: Optional[Callable[[str], None]] = None) -> int:
    global _LAST_PLAN
    ns = parse_args(argv)
    clock = clock or (lambda: datetime.now(timezone.utc))
    sleep = sleep or _time.sleep
    log = log or print

    symbols = resolve_symbols(ns)
    if not symbols:
        raise SystemExit("No underlyings. Pass --symbols AAPL,MSFT or --symbols-file <path>.")

    start = date.fromisoformat(ns.start)
    end = date.fromisoformat(ns.end) if ns.end else clock().date()
    if end < start:
        raise SystemExit(f"--end {end} is before --start {start}.")

    if store is None:
        store = OptionHistoryParquetStore(root=ns.out)
    if provider is None:
        provider = build_provider(ns)  # pragma: no cover - network

    floor = provider.history_floor()
    if start < floor:
        raise SystemExit(
            f"--start {start} is before this vendor's history floor {floor}. Implied "
            f"volatility — the whole point of this cache — is not available earlier, and "
            f"accepting the window would build a cache with silently unusable leading "
            f"months. Use --start {floor} or later.")

    plan = build_plan(provider, store, symbols, start, end, ns, persist=not ns.dry_run, log=log)
    _LAST_PLAN = plan
    if plan.discovery_failed:
        log(f"discovery  : {len(plan.discovery_failed)} symbol(s) failed and were skipped: "
            f"{', '.join(sorted(plan.discovery_failed))}")

    if ns.dry_run:
        print_plan(plan, store, symbols, start, end, ns, log)
        return 0

    log(f"store root : {store.root}")
    log(f"window     : {start.isoformat()} .. {end.isoformat()}")
    log(f"units      : {plan.units_pending} to fetch "
        f"({plan.units_done} complete, {plan.units_empty} known empty already on disk)")
    stats = run_units(plan, provider, store, start, end, ns,
                      clock=clock, sleep=sleep, log=log)
    log(f"done: {stats.units_written} partitions written, {stats.units_empty} empty, "
        f"{stats.units_failed} failed, {stats.rows} rows, "
        f"{stats.empty_contracts} empty contracts recorded, "
        f"{_fmt_bytes(store.disk_bytes())} on disk")
    if stats.units_failed:
        log(f"{stats.units_failed} unit(s) did not complete and were NOT written. "
            f"Re-run the same command to retry only those.")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted — everything already written is intact; "
              "re-run the same command to resume.")
        sys.exit(130)

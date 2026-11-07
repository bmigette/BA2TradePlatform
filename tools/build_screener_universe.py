"""Build a survivorship-free, DATED stock backtest universe.

The committed universe (tools/screener_store_universe.txt) is today's listing plus 144 delisted names that
happened to be in a scratch file: it misses SIVB, FRC, TWTR, ATVI, XLNX, CTXS, SPLK, PXD, HES, ANSS, CERN and
hundreds more. Owner decision 2026-11-07: a symbol is eligible from its listing until its delisting. The
backtest already honours the second half (the Alpaca history build records every last bar in
``_meta/suspensions.json`` and the engine closes a held position at it and blocks re-entry; the screener panel
drops the symbol after its last bar, see docs/REPRODUCE-BACKTESTS.md); what was missing is the NAMES.

This tool adds them. Output = base universe + every US-listed common stock whose Alpaca daily history ends
inside the backtest span (``--start`` .. ``--end``, default 2019-03-01 .. today), in the base file's order with the
additions appended sorted. Nothing is written without ``--write`` (dry run is the default: it still reads
Alpaca, read-only, and prints/saves the report).

Candidate sources (Alpaca only; FMP's delisted list is capped to its newest 100 rows on our plan)
  assets        GET /v2/assets?status=inactive&asset_class=us_equity (name + exchange of dead tickers)
  corp-actions  GET /v1/corporate-actions: the ACQUIREE symbol of cash_mergers / stock_mergers /
                stock_and_cash_mergers and the symbol of worthless_removals inside the span. Alpaca drops
                many dead tickers from the assets list (SIVB, TWTR, ATVI, ... are only reachable here).

Rules (each rejection is counted by reason in the report)
  already_in_universe        skipped, not a candidate
  exchange_not_equity_venue  assets row on OTC / ARCA / BATS / other (ARCA, BATS are ETF venues; the current
                             file holds ETFs on neither); only NYSE, NASDAQ, AMEX are kept
  name_not_common            name says Warrant / Right(s) / Unit(s) / Preferred / Notes / Debenture / ETF / ETN
                             / Fund / Index / % coupon / Depositary share of a preferred
  symbol_shape               a five-letter symbol ending O/P/N/M/L/Z/V/T/K whose first four letters are a known ticker
                             (HBANO, ZIONN, FTAIP: preferred / warrant series); digits, '.', '-' or a CUSIP-like string (class shares such as BRK.B are listed
                             for review, not added); a 5-letter symbol ending W/U/R/WS with no name on record
                             is a derivative
  ticker_reused_active       the ticker is held today by an active asset that is NOT in the base universe: the
                             bars would join two companies
  no_profile                 no Alpaca asset row AND no FMP profile: the instrument class is unknown (an ETF or
                             fund cannot be ruled out); never guessed, listed for review
  etf_or_fund                FMP profile isEtf / isFund
  fmp_active_but_bars_ended  FMP says the ticker trades today but Alpaca's bars stopped: reused ticker or OTC move
  ticker_reused_inactive     two inactive assets carry the ticker (different Alpaca asset ids)
  renamed                    the symbol is the OLD side of a name_change action whose NEW symbol is listed on
                             NYSE/NASDAQ/AMEX (or in the base universe): the history continues under the new
                             symbol (rename machinery: rename_successors.csv), not a delisting. A new symbol on
                             OTC (RAD -> RADCQ after a bankruptcy) is a delisting and the old symbol is kept
  exchange_not_equity_venue  (also) an OTC asset row whose FMP profile is absent or not on those venues
  no_bars                    no Alpaca daily bar in the span
  too_short                  fewer than --min-months monthly bars in the span
  history_gap                a calendar month without any bar between first and last bar (>= 20 sessions
                             dark: a halt, or a reused ticker); listed for review, never guessed
  still_trading              last bar is within --stale-days of --end (not delisted)
  ipo_after_last_bar         FMP profile ipoDate (the _meta/ipo_dates.json the cache build uses) is after the
                             last bar: the ticker now belongs to a newer company
The delisting date is the LAST DAILY BAR (what the build's suspensions.json records as ``delisted``). Monthly
bars are fetched for every candidate (about 90 bars each), then daily bars of only the last month: a few
hundred requests in all. Raw responses are cached in --work-dir, so re-runs cost nothing.

Exit price of a delisting (the backtest applies it): ``--write`` also saves ``<out>.terminal_events.json`` (worthless
removals, bankruptcy-ticker renames such as RAD -> RADCQ, merger cash prices, extras tagged ``worthless``). After the cache
rebuild, ``--install-terminals <cache>/_meta/suspensions.json`` resolves them against each ``delisted`` event and writes
``_meta/delisting_terminals.json``: 0 for worthless / bankruptcy, the per-share cash of a cash merger, else the last close
(counted and logged by the backtest). Stock-for-stock mergers keep the last close.

Credentials are never printed: --keys-db / --account-id (accountsetting, opened read-only, same mechanism as
tools/alpaca_history_download.py) or APCA_API_KEY_ID / APCA_API_SECRET_KEY. Paper-account keys read the
public asset list and market data fine.

  python tools/build_screener_universe.py --keys-db ~/Documents/ba2/trade/db.sqlite --account-id 1          # dry run
  python tools/build_screener_universe.py --keys-db ... --account-id 1 --write --out tools/screener_store_universe_dated.txt
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import alpaca_history_download as dl  # noqa: E402  (shared Transport, throttling, credential loading)

REPO = Path(__file__).resolve().parents[1]
DEFAULT_BASE = REPO / "tools" / "screener_store_universe.txt"
DEFAULT_OUT = REPO / "tools" / "screener_store_universe_dated.txt"
DEFAULT_SPAN_START = dt.date(2019, 3, 1)
EQUITY_VENUES = {"NYSE", "NASDAQ", "AMEX"}
MERGER_KEYS = ("cash_mergers", "stock_mergers", "stock_and_cash_mergers")
CA_TYPES = "cash_merger,stock_merger,stock_and_cash_merger,worthless_removal,name_change"
BATCH = 100
TRADING_HOST_PAPER = "https://paper-api.alpaca.markets"

_NAME_REJECT = re.compile(
    r"\bWarrants?\b|\bRights?\s*$|\bRights?\s*\(|\bUnits?\s*$|\bUnits?\b[,(]|\bPreferred\b|\bPreference\b|"
    r"\bNotes?\b|\bDebentures?\b|\bETF\b|\bETN\b|\bFunds?\b|\bIndex\b|\d%|\bSubordinated\b|"
    r"\bTrust Preferred\b|\bDepositary Shares?\b.*\bPreferred\b|\bCapital Securities\b|\bContingent Value\b|"
    r"\bLiquidating Trust\b|\bDep(ositary)? Shs?\b|\bPfd\b|\bPerp(etual)?\b|\bWhen[- ]Issued\b|\bEx-Distribution\b|"
    r"\s-\s\d+\.\d+\s*$|\bDepositary Shares? Representing\b", re.I)
#: Fifth letters that mark a preferred / warrant / subordinated series of the four-letter ticker in front of them
#: (HBANO, ZIONN, FTAIP, PAVMZ): rejected when that four-letter ticker is itself a known symbol. A, B (classes) are kept.
_DERIVATIVE_SUFFIX = "OPNMLZVTK"
_SPAC = re.compile(r"\bAcquisition\b|\bBlank Check\b", re.I)
_SYMBOL_OK = re.compile(r"^[A-Z]{1,5}$")


def read_symbol_file(path: Path) -> list:
    out, seen = [], set()
    for line in path.read_text(encoding="utf-8").splitlines():
        symbol = line.split("#", 1)[0].strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            out.append(symbol)
    return out


def classify_symbol_and_name(symbol: str, name: Optional[str], exchange: Optional[str]) -> Optional[str]:
    """Rejection reason for a candidate that is not a plain US-listed common stock, else None."""
    if not _SYMBOL_OK.match(symbol):
        return "symbol_shape"
    if exchange is not None and exchange not in EQUITY_VENUES and exchange != "OTC":
        return "exchange_not_equity_venue"           # ARCA / BATS: ETF venues. OTC is judged on the FMP profile
    if name is not None:
        if _NAME_REJECT.search(name):
            return "name_not_common"
    elif len(symbol) == 5 and symbol[-1] in "WUR":
        return "symbol_shape"
    return None


def month_starts(first: dt.date, last: dt.date) -> list:
    out, cursor = [], dt.date(first.year, first.month, 1)
    while cursor <= last:
        out.append(cursor)
        cursor = dt.date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    return out


def has_month_gap(months_with_bars: Iterable[dt.date]) -> bool:
    """True when a calendar month lies between the first and last month that have a bar but has none."""
    months = sorted(set(months_with_bars))
    if len(months) < 2:
        return False
    return len(month_starts(months[0], months[-1])) != len(months)


class CachedTransport:
    """Wraps dl.Transport.get with an on-disk response cache keyed by path+params (resumable, no credentials)."""

    def __init__(self, inner, folder: Path):
        self.inner, self.folder = inner, folder
        folder.mkdir(parents=True, exist_ok=True)
        self.hits = self.calls = 0

    def get(self, path: str, params: dict) -> dict:
        key = hashlib.sha256(json.dumps([path, params], sort_keys=True).encode()).hexdigest()[:24]
        cached = self.folder / f"{key}.json"
        if cached.exists():
            self.hits += 1
            return json.loads(cached.read_text(encoding="utf-8"))
        payload = self.inner.get(path, params)
        self.calls += 1
        tmp = cached.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(cached)
        return payload


def paged(transport, path: str, params: dict, collect: Callable[[dict, dict], None]) -> int:
    pages, token, seen = 0, None, set()
    while True:
        query = dict(params, **({"page_token": token} if token else {}))
        payload = transport.get(path, query)
        pages += 1
        collect(payload, {})
        token = payload.get("next_page_token")
        if not token:
            return pages
        if token in seen:
            raise dl.FatalError(f"pagination loop on {path}")
        seen.add(token)


def fetch_assets(headers: dict, host: str, work: Path) -> tuple:
    """(inactive, active) us_equity assets; one request each, cached."""
    out = []
    for status in ("inactive", "active"):
        cache = work / f"assets_{status}.json"
        if not cache.exists():
            url = f"{host}/v2/assets?status={status}&asset_class=us_equity"
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as response:
                    rows = json.loads(response.read())
            except urllib.error.HTTPError as error:
                raise dl.FatalError(f"assets {status}: HTTP {error.code} (credentials or --trading-host)") from error
            cache.write_text(json.dumps(rows), encoding="utf-8")
        out.append(json.loads(cache.read_text(encoding="utf-8")))
    return out[0], out[1]


def fetch_corporate_actions(transport, start: dt.date, end: dt.date) -> dict:
    """Merger acquirees, worthless removals and name changes inside the span, month by month."""
    merged: dict = collections.defaultdict(list)

    def collect(payload, _):
        for kind, rows in (payload.get("corporate_actions") or {}).items():
            merged[kind].extend(rows)

    for _label, first, last in dl.month_chunks(start, end + dt.timedelta(days=1)):
        paged(transport, "/v1/corporate-actions",
              {"types": CA_TYPES, "start": first.isoformat(), "end": last.isoformat(), "limit": 1000, "sort": "asc"},
              collect)
    return dict(merged)


def _is_equity_listed(symbol: str, active_by: dict, universe: set) -> bool:
    """The successor of a name change trades on NYSE / NASDAQ / AMEX today (or is a base-universe name): the
    history continues, it is a rename. A successor on OTC (bankruptcy tickers such as RADCQ, TUPBQ) means the
    exchange listing ENDED: that is a delisting and the old symbol is a candidate."""
    row = active_by.get(symbol)
    return symbol in universe or (row is not None and row["exchange"] in EQUITY_VENUES)


def profile_row(row: dict) -> dict:
    return {"name": row.get("companyName"), "exchange": row.get("exchangeShortName"),
            "etf": bool(row.get("isEtf")), "fund": bool(row.get("isFund")),
            "active": bool(row.get("isActivelyTrading")), "ipo": row.get("ipoDate") or None}


def load_fmp_key(keys_db: Optional[str]) -> Optional[str]:
    """FMP_API_KEY from the keys db's appsetting table (read-only) or the environment; never printed."""
    import os
    import sqlite3
    if keys_db:
        connection = sqlite3.connect("file:" + Path(keys_db).expanduser().as_posix() + "?mode=ro", uri=True)
        try:
            row = connection.execute("select value_str, value_json from appsetting where key = 'FMP_API_KEY'").fetchone()
        finally:
            connection.close()
        if row:
            value = row[0] or (json.loads(row[1]) if row[1] else None)
            if value:
                return str(value).strip('"')
    return os.environ.get("FMP_API_KEY") or None


def fetch_fmp_delisted(api_key: str, work: Path, span_start: dt.date, opener=urllib.request.urlopen) -> list:
    """Symbols on page 0 of FMP delisted-companies (our plan returns only the newest 100 rows) delisted in the span."""
    cache = work / "fmp_delisted_page0.json"
    if not cache.exists():
        url = "https://financialmodelingprep.com/api/v3/delisted-companies?page=0&apikey=" + api_key
        try:
            with opener(url, timeout=60) as response:
                rows = json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise dl.FatalError(f"FMP delisted-companies: HTTP {error.code}") from None
        cache.write_text(json.dumps(rows), encoding="utf-8")
    rows = json.loads(cache.read_text(encoding="utf-8"))
    return sorted({str(r["symbol"]).upper() for r in rows
                   if isinstance(r, dict) and r.get("symbol") and str(r.get("delistedDate") or "")[:10] >= span_start.isoformat()})


def make_profile_fetcher(api_key: Optional[str], work: Path, opener=urllib.request.urlopen) -> Callable[[list], dict]:
    """``fetch(symbols) -> {symbol: profile}`` from FMP /api/v3/profile (100 per request, cached per batch). The key
    is never put in an exception message or a log line."""
    folder = work / "profiles"
    folder.mkdir(parents=True, exist_ok=True)

    def fetch(symbols: list) -> dict:
        out: dict = {}
        for index in range(0, len(symbols), 100):
            batch = symbols[index:index + 100]
            cache = folder / (hashlib.sha256(",".join(batch).encode()).hexdigest()[:24] + ".json")
            if not cache.exists():
                if not api_key:
                    raise dl.FatalError("an FMP key is needed to classify candidates (--keys-db with FMP_API_KEY, "
                                        "env FMP_API_KEY, or --no-fmp to run on Alpaca names only)")
                url = ("https://financialmodelingprep.com/api/v3/profile/" + ",".join(batch)
                       + "?apikey=" + api_key)
                try:
                    with opener(url, timeout=60) as response:
                        rows = json.loads(response.read())
                except urllib.error.HTTPError as error:
                    raise dl.FatalError(f"FMP profile: HTTP {error.code}") from None
                except (urllib.error.URLError, OSError, ValueError) as error:
                    raise dl.FatalError(f"FMP profile: {type(error).__name__}") from None
                if not isinstance(rows, list):
                    raise dl.FatalError("FMP profile: unexpected response shape")
                cache.write_text(json.dumps(rows), encoding="utf-8")
            for row in json.loads(cache.read_text(encoding="utf-8")):
                if row.get("symbol"):
                    out[str(row["symbol"]).upper()] = profile_row(row)
        return out

    return fetch


def fmp_rejection(profile: Optional[dict]) -> Optional[str]:
    if profile is None:
        return "no_profile"
    if profile["etf"] or profile["fund"]:
        return "etf_or_fund"
    if profile["exchange"] not in EQUITY_VENUES | {"NYSE American", "NYSE MKT"}:
        return "exchange_not_equity_venue"
    if profile["name"] and _NAME_REJECT.search(profile["name"]):
        return "name_not_common"
    return None


def gather_candidates(inactive: list, active: list, actions: dict, universe: set,
                      profile_fetch: Optional[Callable[[list], dict]] = None, extras: Iterable[str] = ()) -> tuple:
    """-> (candidates {symbol: {...}}, rejected [(symbol, reason, detail)]).

    Phase 1 judges what Alpaca says (shape, venue, name); the survivors go to FMP (``profile_fetch``) for
    ETF / fund / venue / name / ipoDate, because a merger acquiree often has no Alpaca asset row at all."""
    rejected: list = []
    inactive_by: dict = collections.defaultdict(list)
    for row in inactive:
        inactive_by[row["symbol"]].append(row)
    active_by = {row["symbol"]: row for row in active}
    renamed_old = {row["old_symbol"]: row["new_symbol"] for row in actions.get("name_changes", [])
                   if row["old_symbol"] != row["new_symbol"]}
    raw: dict = {}

    def add(symbol, source, date=None):
        entry = raw.setdefault(symbol, {"sources": set(), "dates": []})
        entry["sources"].add(source)
        if date:
            entry["dates"].append(date)

    for symbol in inactive_by:
        add(symbol, "assets_inactive")
    for kind in MERGER_KEYS:
        for row in actions.get(kind, []):
            if row["acquiree_symbol"] != row.get("acquirer_symbol"):
                add(row["acquiree_symbol"], kind, row["process_date"])
    for row in actions.get("worthless_removals", []):
        add(row["symbol"], "worthless_removals", row["process_date"])
    for symbol in extras:
        add(symbol, "extra_list")

    known = set(universe) | set(active_by) | set(inactive_by) | set(raw)
    phase1 = {}
    for symbol, entry in sorted(raw.items()):
        if symbol in universe:
            continue
        rows = inactive_by.get(symbol, [])
        if len({row["id"] for row in rows}) > 1:
            rejected.append((symbol, "ticker_reused_inactive", "; ".join(sorted({row["name"] for row in rows}))))
            continue
        if symbol in renamed_old and _is_equity_listed(renamed_old[symbol], active_by, universe):
            rejected.append((symbol, "renamed", f"-> {renamed_old[symbol]}"))
            continue
        name = rows[0]["name"] if rows else None
        exchange = rows[0]["exchange"] if rows else None
        if len(symbol) == 5 and symbol[-1] in _DERIVATIVE_SUFFIX and symbol[:-1] in known:
            rejected.append((symbol, "symbol_shape", f"{symbol[-1]} series of {symbol[:-1]}: " + (name or "")))
            continue
        reason = classify_symbol_and_name(symbol, name, exchange)
        if reason:
            rejected.append((symbol, reason, name or ""))
            continue
        phase1[symbol] = {"name": name, "exchange": exchange, "sources": sorted(entry["sources"]),
                          "process_date": max(entry["dates"]) if entry["dates"] else None,
                          "alpaca_active_row": symbol in active_by}
    profiles = profile_fetch(sorted(phase1)) if profile_fetch else {}
    candidates = {}
    for symbol, entry in phase1.items():
        if profile_fetch:
            profile = profiles.get(symbol)
            if profile is None and entry["name"] is not None and entry["exchange"] != "OTC":
                candidates[symbol] = entry           # Alpaca already named and vetted it; FMP does not carry old names
                continue
            reason = fmp_rejection(profile)
            if reason:
                rejected.append((symbol, reason, (profile or {}).get("name") or entry["name"] or ""))
                continue
            entry = dict(entry, name=profile["name"] or entry["name"], exchange=profile["exchange"], ipo=profile["ipo"],
                         fmp_active=profile["active"])
        candidates[symbol] = entry
    return candidates, rejected


def _merge_bars(payload: dict, into: dict) -> None:
    for symbol, bars in (payload.get("bars") or {}).items():
        into.setdefault(symbol, []).extend(bars)


def fetch_bars_batch(transport, symbols: list, timeframe: str, start: str, end: str) -> dict:
    out: dict = {}
    paged(transport, "/v2/stocks/bars",
          {"symbols": ",".join(symbols), "timeframe": timeframe, "start": start, "end": end,
           "adjustment": "raw", "feed": "sip", "limit": 10000, "sort": "asc"},
          lambda payload, _: _merge_bars(payload, out))
    return out


def bar_date(bar: dict) -> dt.date:
    return dt.date.fromisoformat(bar["t"][:10])


def verify_candidates(transport, candidates: dict, span_start: dt.date, end: dt.date, stale_days: int,
                      min_months: int, ipo_dates: dict) -> tuple:
    """Monthly bars for all, daily bars of the last month for the survivors. -> (accepted, rejected)."""
    symbols = sorted(candidates)
    monthly: dict = {}
    for index in range(0, len(symbols), BATCH):
        batch = symbols[index:index + BATCH]
        monthly.update(fetch_bars_batch(transport, batch, "1Month", f"{span_start.isoformat()}T00:00:00Z",
                                        f"{end.isoformat()}T00:00:00Z"))  # exclusive of --end day: the plan refuses SIP data < 15 min old
    rejected, passing = [], {}
    cutoff = end - dt.timedelta(days=stale_days)
    for symbol in symbols:
        bars = monthly.get(symbol, [])
        if not bars:
            rejected.append((symbol, "no_bars", ""))
            continue
        months = [bar_date(b).replace(day=1) for b in bars]
        if len(months) < min_months:
            rejected.append((symbol, "too_short", f"{len(months)} monthly bars"))
        elif has_month_gap(months):
            rejected.append((symbol, "history_gap", f"{min(months)}..{max(months)}, {len(months)} months"))
        else:
            passing[symbol] = (min(months), max(months))
    by_last_month: dict = collections.defaultdict(list)
    for symbol, (_, last_month) in passing.items():
        by_last_month[last_month].append(symbol)
    accepted = {}
    for last_month, group in sorted(by_last_month.items()):
        month_end = min(month_starts(last_month, last_month + dt.timedelta(days=40))[1], end)
        for index in range(0, len(group), BATCH):
            batch = group[index:index + BATCH]
            daily = fetch_bars_batch(transport, batch, "1Day", f"{last_month.isoformat()}T00:00:00Z",
                                     f"{month_end.isoformat()}T00:00:00Z")
            for symbol in batch:
                days = [bar_date(b) for b in daily.get(symbol, [])]
                if not days:
                    rejected.append((symbol, "no_bars", "monthly bar but no daily bar in its last month"))
                    continue
                last = max(days)
                first_month = passing[symbol][0]
                if last > cutoff:
                    rejected.append((symbol, "still_trading", f"last bar {last}"))
                    continue
                if candidates[symbol].get("fmp_active"):
                    rejected.append((symbol, "fmp_active_but_bars_ended",
                                     f"FMP profile is actively trading, Alpaca bars end {last}: reused ticker or OTC move"))
                    continue
                if last < span_start:
                    rejected.append((symbol, "no_bars", f"last bar {last} is before the span"))
                    continue
                ipo = candidates[symbol].get("ipo") or ipo_dates.get(symbol)
                if ipo and dt.date.fromisoformat(ipo) > last:
                    rejected.append((symbol, "ipo_after_last_bar", f"ipoDate {ipo}, last bar {last}"))
                    continue
                accepted[symbol] = dict(candidates[symbol], first_month=first_month.isoformat(),
                                        last_bar=last.isoformat(),
                                        spac=bool(candidates[symbol]["name"] and _SPAC.search(candidates[symbol]["name"])))
    return accepted, rejected


def check_against_suspensions(dates_tsv: Path, suspensions_json: Path) -> list:
    """After the January cache rebuild: every added name must be a ``delisted`` event of the cache build whose last
    session equals the last bar this tool saw. Returns the problems (empty = consistent)."""
    events = json.loads(suspensions_json.read_text(encoding="utf-8"))["symbols"]
    problems = []
    for line in dates_tsv.read_text(encoding="utf-8").splitlines()[1:]:
        symbol, _first, last_bar = line.split("\t")[:3]
        rows = [r for r in events.get(symbol, []) if r[2] == "delisted"]
        if not rows:
            problems.append((symbol, "no delisted event in the cache build (no bars, or last bar within the slack)"))
        elif rows[-1][0] != last_bar:
            problems.append((symbol, f"delisted event last session {rows[-1][0]} != expected last bar {last_bar}"))
    return problems


def read_extra_tags(path: Path) -> dict:
    """``{SYMBOL: tag or None}`` of the extras file: ``SYMBOL [worthless]  # evidence``. ``worthless`` = equity cancelled
    (bankruptcy, seizure, liquidation): a holder received nothing."""
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("#", 1)[0].split()
        if parts:
            out[parts[0].upper()] = parts[1].lower() if len(parts) > 1 else None
    return out


def terminal_events(actions: dict, extra_tags: dict) -> dict:
    """What happened to each symbol's shares, from Alpaca corporate actions (+ tagged extras): ``{symbol: [{kind, date,
    price}]}``. Consumed AT/after the delisting only (point-in-time safe)."""
    events: dict = collections.defaultdict(list)
    for row in actions.get("worthless_removals", []):
        events[row["symbol"]].append({"kind": "worthless", "date": row["process_date"], "price": 0.0})
    for row in actions.get("cash_mergers", []):
        if row["acquiree_symbol"] != row.get("acquirer_symbol") and row.get("rate") is not None:
            events[row["acquiree_symbol"]].append({"kind": "cash_merger", "date": row["process_date"],
                                                   "price": float(row["rate"])})
    for kind in ("stock_mergers", "stock_and_cash_mergers"):
        for row in actions.get(kind, []):
            if row["acquiree_symbol"] != row.get("acquirer_symbol"):
                events[row["acquiree_symbol"]].append({"kind": "stock_merger", "date": row["process_date"], "price": None})
    for row in actions.get("name_changes", []):
        new = row["new_symbol"]
        if len(new) >= 5 and new.endswith("Q") and new != row["old_symbol"] and new[:-1].startswith(row["old_symbol"][:3]):
            events[row["old_symbol"]].append({"kind": "bankruptcy_otc", "date": row["process_date"], "price": 0.0})
    for symbol, tag in extra_tags.items():
        if tag == "worthless":
            events[symbol].append({"kind": "worthless", "date": None, "price": 0.0})
    return dict(events)


_PRIORITY = ("worthless", "bankruptcy_otc", "cash_merger", "stock_merger")
_WINDOW_DAYS = {"worthless": (-5, 730), "bankruptcy_otc": (-5, 730), "cash_merger": (-3, 10), "stock_merger": (-3, 10)}


def resolve_terminals(events: dict, suspension_symbols: dict) -> tuple:
    """-> (terminals {symbol: {last_session, kind, price, source}}, counts by kind incl. 'unknown').

    One entry per ``delisted`` event of the cache build whose corporate action falls in the kind's window around the last
    session (a cash merger within -3..+10 days, a worthless removal / bankruptcy ticker -5..+730 days, an extras tag any
    date); anything else is 'unknown' (not written: the account prices it at the last close and counts it)."""
    terminals, counts = {}, collections.Counter()
    for symbol, rows in sorted(suspension_symbols.items()):
        delisted = [r for r in rows if r[2] == "delisted"]
        if not delisted:
            continue
        last = dt.date.fromisoformat(delisted[-1][0])
        chosen = None
        for kind in _PRIORITY:
            for ev in events.get(symbol, []):
                if ev["kind"] != kind:
                    continue
                if ev["date"] is not None:
                    lo, hi = _WINDOW_DAYS[kind]
                    if not lo <= (dt.date.fromisoformat(ev["date"]) - last).days <= hi:
                        continue
                chosen = ev
                break
            if chosen:
                break
        if chosen is None:
            counts["unknown"] += 1
            continue
        counts[chosen["kind"]] += 1
        terminals[symbol] = {"last_session": last.isoformat(), "kind": chosen["kind"], "price": chosen["price"],
                             "source": "tag" if chosen["date"] is None else f"corporate action {chosen['date']}"}
    return terminals, dict(counts)


def install_terminals(events_json: Path, suspensions_json: Path) -> tuple:
    """Write ``delisting_terminals.json`` beside ``suspensions_json`` (the cache's _meta), stamped with its build."""
    suspensions = json.loads(suspensions_json.read_text(encoding="utf-8"))
    events = json.loads(events_json.read_text(encoding="utf-8"))
    terminals, counts = resolve_terminals(events, suspensions["symbols"])
    target = suspensions_json.with_name("delisting_terminals.json")
    target.write_text(json.dumps({"build": suspensions["build"], "symbols": terminals}, indent=1), encoding="utf-8")
    return target, counts


def make_report(base: list, accepted: dict, rejected: list, params: dict, stats: dict) -> dict:
    reasons = collections.Counter(reason for _, reason, _ in rejected)
    by_year = collections.Counter(entry["last_bar"][:4] for entry in accepted.values())
    by_source = collections.Counter("+".join(entry["sources"]) for entry in accepted.values())
    return {
        "params": params, "base_symbols": len(base), "added": len(accepted),
        "added_spac_shares": sum(1 for e in accepted.values() if e["spac"]),
        "added_by_delisting_year": dict(sorted(by_year.items())),
        "added_by_source": dict(by_source.most_common()),
        "rejected_by_reason": dict(reasons.most_common()),
        "rejected": [{"symbol": s, "reason": r, "detail": d} for s, r, d in sorted(rejected)],
        "added_symbols": {s: {"name": e["name"], "exchange": e["exchange"], "first_month": e["first_month"],
                              "last_bar": e["last_bar"], "sources": e["sources"], "spac": e["spac"]}
                          for s, e in sorted(accepted.items())},
        "requests": stats,
    }


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", type=Path, default=DEFAULT_BASE, help="current universe file")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="universe file written with --write")
    ap.add_argument("--start", type=dt.date.fromisoformat, default=DEFAULT_SPAN_START)
    ap.add_argument("--end", type=dt.date.fromisoformat, default=dt.date.today())
    ap.add_argument("--stale-days", type=int, default=10,
                    help="last bar this many calendar days (or fewer) before --end = still trading")
    ap.add_argument("--min-months", type=int, default=2)
    ap.add_argument("--exclude-spacs", action="store_true", help="drop blank-check company shares (default keeps "
                    "them, as the base file does)")
    ap.add_argument("--keys-db")
    ap.add_argument("--account-id", type=int)
    ap.add_argument("--extra-file", type=Path, default=REPO / "tools" / "screener_universe_extra_delisted.txt",
                    help="reviewed symbols that no automatic source lists (bankruptcies / liquidations without a "
                    "merger record: SIVB, FRC, ...); verified like every other candidate")
    ap.add_argument("--no-fmp", action="store_true", help="classify on Alpaca names only (no ETF/fund/ipoDate check)")
    ap.add_argument("--trading-host", default=TRADING_HOST_PAPER)
    ap.add_argument("--rpm", type=float, default=120.0)
    ap.add_argument("--work-dir", type=Path, default=Path("~/Documents/ba2/common/cache/_universe_build").expanduser())
    ap.add_argument("--ipo-dates", type=Path, default=Path(
        "~/Documents/ba2/common/cache/AlpacaHistoryOHLCVProvider/_meta/ipo_dates.json").expanduser())
    ap.add_argument("--report", type=Path, help="write the JSON report here (default <work-dir>/report.json)")
    ap.add_argument("--write", action="store_true", help="write --out (and --out.dates.tsv); default is a dry run")
    ap.add_argument("--install-terminals", type=Path, metavar="SUSPENSIONS_JSON",
                    help="after the rebuild: write the cache's _meta/delisting_terminals.json (exit price of each delisting: "
                    "0 worthless / bankruptcy, the merger cash price, else the last close) from --out's .terminal_events.json")
    ap.add_argument("--check-cache", type=Path, metavar="SUSPENSIONS_JSON",
                    help="after the rebuild: compare --out's .dates.tsv with the cache build's _meta/suspensions.json "
                    "and exit 1 on any difference (no network)")
    args = ap.parse_args(argv)
    if args.install_terminals:
        target, counts = install_terminals(args.out.with_suffix(".terminal_events.json"), args.install_terminals)
        print(f"wrote {target}; delisting exits by kind: {counts} (unknown = last close, counted and logged by the backtest)")
        return 0
    if args.check_cache:
        problems = check_against_suspensions(args.out.with_suffix(".dates.tsv"), args.check_cache)
        for symbol, why in problems:
            print(f"{symbol}: {why}")
        print(f"{len(problems)} problems")
        return 1 if problems else 0

    key, secret = dl.load_credentials(args.keys_db, args.account_id)
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}
    base = read_symbol_file(args.base)
    universe = set(base)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    transport = CachedTransport(dl.Transport(key, secret, rpm=args.rpm, max_attempts=5, min_remaining=20),
                                args.work_dir / "responses")
    inactive, active = fetch_assets(headers, args.trading_host, args.work_dir)
    actions = fetch_corporate_actions(transport, args.start, args.end)
    fmp_key = None if args.no_fmp else load_fmp_key(args.keys_db)
    extra_tags = read_extra_tags(args.extra_file) if args.extra_file.exists() else {}
    extras = list(extra_tags)
    if fmp_key:
        extras += [s for s in fetch_fmp_delisted(fmp_key, args.work_dir, args.start) if s not in extras]
    candidates, rejected = gather_candidates(inactive, active, actions, universe,
                                             None if args.no_fmp else make_profile_fetcher(fmp_key, args.work_dir),
                                             extras)
    ipo_dates = (json.loads(args.ipo_dates.read_text(encoding="utf-8")).get("symbols", {})
                 if args.ipo_dates.exists() else {})
    accepted, more = verify_candidates(transport, candidates, args.start, args.end, args.stale_days,
                                       args.min_months, ipo_dates)
    rejected += more
    if args.exclude_spacs:
        rejected += [(s, "spac_excluded", e["name"] or "") for s, e in accepted.items() if e["spac"]]
        accepted = {s: e for s, e in accepted.items() if not e["spac"]}
    params = {"start": args.start.isoformat(), "end": args.end.isoformat(), "stale_days": args.stale_days,
              "min_months": args.min_months, "exclude_spacs": args.exclude_spacs, "base": str(args.base),
              "ipo_dates_file": bool(ipo_dates)}
    report = make_report(base, accepted, rejected, params,
                         {"api_calls": transport.calls, "cache_hits": transport.hits,
                          "candidates_checked": len(candidates)})
    report_path = args.report or args.work_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"base {len(base)} | candidates checked {len(candidates)} | ADDED {len(accepted)} "
          f"(SPAC shares {report['added_spac_shares']}) | api calls {transport.calls}, cached {transport.hits}")
    print("added by delisting year:", report["added_by_delisting_year"])
    print("rejected by reason:", report["rejected_by_reason"])
    print("report:", report_path)
    if args.write:
        out = base + sorted(accepted)
        args.out.write_text("\n".join(out) + "\n", encoding="utf-8")
        args.out.with_suffix(".dates.tsv").write_text(
            "symbol\tfirst_month\tlast_bar\tsources\tname\n" + "".join(
                f"{s}\t{e['first_month']}\t{e['last_bar']}\t{'+'.join(e['sources'])}\t{e['name'] or ''}\n"
                for s, e in sorted(accepted.items())), encoding="utf-8")
        args.out.with_suffix(".terminal_events.json").write_text(
            json.dumps(terminal_events(actions, extra_tags), sort_keys=True), encoding="utf-8")
        print(f"wrote {args.out} ({len(out)} symbols)")
    else:
        print("dry run: nothing written (--write writes the universe file)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

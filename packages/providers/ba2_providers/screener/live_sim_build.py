"""Building (and refreshing) the daily criteria panel of ``live_sim`` from the caches.

Inputs, all produced by the platform's cache tooling (``ba2-test prewarm --screener-panel`` /
``ba2-test build-screener-metrics --daily-panel``):
  * ``<cache>/FMPOHLCVProvider/<SYM>_1d.parquet``                daily bars (``ba2-test fetch-cache``)
  * ``<cache>/screener_fundamentals/shares/<SYM>.parquet``       the VENDOR's own outstanding-share history
                                                                 (``api/v4/historical/shares_float``, one call per symbol)
  * ``<cache>/screener_fundamentals/float/<SYM>.parquet``        free float, effective-dated (metric-store tooling)
  * ``<cache>/screener_fundamentals/market_cap/<SYM>.parquet``   FMP historical market cap (fallback + pre-2021 history)
  * ``<cache>/screener/vendor_shares/<YYYY-MM-DD>.json``         the vendor's bulk share table of one day
                                                                 (``api/v4/shares_float/all``, ONE call)

The panel is written to ``<cache>/screener/daily_panel`` (``live_sim.panel_dir_for``).  A build whose source
fingerprint matches the manifest is a no-op ("up to date"); an interrupted build leaves the previous panel
untouched (the manifest is the last file swapped in).

SHARES (the vendor's market cap on day D = previous close x the vendor's share count).  Measured 2026-10-08: the
vendor's current ``marketCap / price`` equals the ``outstandingShares`` of the bulk table ``shares_float/all``
(median |diff| 6e-11, 95.4 % within 1 %), and ``api/v4/historical/shares_float`` returns the same field as a dated
series (daily-ish rows, dated by the vendor, from 2021-05-18: the endpoint keeps its ~1,800 newest rows).  So:
  1. ``shares(D)`` = the vendor's own series as of D (a row is usable from its own date: no look-ahead);
  2. before the series starts (2020-01 .. 2021-05), and for a symbol with no vendor series: FMP's implied share
     series (cached historical market cap / close) delayed by ``SHARES_LAG_DAYS`` and scaled per symbol to the
     vendor's count at the first vendor row (or at the bulk table when there is no series).  This is the weak part:
     FMP's series has basis artifacts (ADR ratios, one-off jumps); every symbol using it is COUNTED in the manifest.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import numpy as np

from ba2_providers.screener import live_sim as ls

#: Bump when the BUILD logic changes what the panel contains for the same inputs (it is part of the panel fingerprint).
#: 2: shares in RAW (as-traded) basis everywhere, split factors, coverage lists.
BUILD_REV = 2
SHARES_ALL_URL = "https://financialmodelingprep.com/api/v4/shares_float/all"
SHARES_HIST_URL = "https://financialmodelingprep.com/api/v4/historical/shares_float"


# ----------------------------------------------------------------------------------- vendor table (1 call)
def vendor_dir(cache_folder: str) -> str:
    return os.path.join(cache_folder, "screener", "vendor_shares")


def fetch_vendor_snapshot(cache_folder: str, api_key: str, *, now_utc: Optional[datetime] = None) -> str:
    """TWO FMP calls: (1) the vendor's bulk shares table (every instrument, ~88k rows, ~12 MB) -> ``rows``
    ``{symbol: outstandingShares}``; (2) the live screener's own listing (no cap band) -> ``caps``
    ``{symbol: [marketCap, price]}``.  The vendor's market cap uses ``outstandingShares`` for ~96 % of the names and
    another count (class shares, ADR ratio) for the rest (measured 2026-10-08: 4.2 % differ by more than 5 %), so the
    cap basis ``marketCap / price`` is kept next to it: ``k = (cap/price) / outstanding`` per symbol.
    Stored in ``vendor_shares/<NY date>.json``."""
    from zoneinfo import ZoneInfo
    from ba2_providers.fmp_common import fmp_http_get
    from ba2_providers.screener import metric_store as ms
    caps_rows = ms._fetch_screener_rows(api_key)
    resp = fmp_http_get(SHARES_ALL_URL, params={"apikey": api_key, "page": 0}, endpoint="shares-float-all", timeout=180)
    rows = resp.json()
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"shares_float/all returned {type(rows).__name__}, expected a non-empty list")
    now_utc = now_utc or datetime.now(timezone.utc)
    ny = now_utc.astimezone(ZoneInfo("America/New_York"))
    table = {}
    for r in rows:
        if isinstance(r, dict) and r.get("symbol") and r.get("outstandingShares"):
            try:
                v = float(r["outstandingShares"])
            except (TypeError, ValueError):
                continue
            if v > 0:
                table[str(r["symbol"]).upper()] = v
    caps = {str(r["symbol"]).upper(): [r.get("marketCap"), r.get("price")] for r in caps_rows
            if r.get("symbol") and (r.get("marketCap") or 0) > 0 and (r.get("price") or 0) > 0}
    out = {"source": "shares_float/all + stock-screener", "fetched_at_utc": now_utc.isoformat(timespec="seconds"),
           "fetched_at_ny": ny.replace(tzinfo=None).isoformat(timespec="seconds"), "n_rows": len(rows),
           "rows": table, "caps": caps}
    d = vendor_dir(cache_folder)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{ny.date().isoformat()}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f)
    os.replace(tmp, path)
    return path


def latest_vendor_snapshot(cache_folder: str) -> Optional[str]:
    """Newest snapshot; ``cache_folder`` may also be the ``vendor_shares`` directory itself."""
    d = cache_folder if os.path.basename(cache_folder.rstrip("/\\")) == "vendor_shares" else vendor_dir(cache_folder)
    if not os.path.isdir(d):
        return None
    names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
    return os.path.join(d, names[-1]) if names else None


def vendor_snapshot_is_fresh(path: Optional[str], max_age_days: int, today: Optional[date] = None) -> bool:
    if not path:
        return False
    try:
        d = date.fromisoformat(os.path.basename(path)[:10])
    except ValueError:
        return False
    return ((today or date.today()) - d).days <= max_age_days


# ----------------------------------------------------------------------- vendor share history (1 call/symbol)
def shares_cache_path(cache_folder: str, symbol: str) -> str:
    return os.path.join(cache_folder, "screener_fundamentals", "shares", f"{symbol.upper()}.parquet")


def _write_atomic(df, path: str) -> None:
    import threading
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def fetch_historical_shares(cache_folder: str, symbol: str, api_key: str, *, max_age_days: int = 7,
                            today: Optional[date] = None) -> str:
    """The vendor's dated outstanding-share series of ``symbol`` -> ``screener_fundamentals/shares``.  Returns
    'cached' (fresh), 'fetched' or 'empty'.  A fetch failure RAISES (the caller counts it): a missing series is
    never recorded as an empty one."""
    import pandas as pd
    from ba2_providers.fmp_common import fmp_http_get
    path = shares_cache_path(cache_folder, symbol)
    meta = path + ".meta.json"
    today = today or date.today()
    try:
        with open(meta) as f:
            fetched_on = date.fromisoformat(json.load(f)["fetched_on"])
        if os.path.exists(path) and (today - fetched_on).days <= max_age_days:
            return "cached"
    except Exception:  # noqa: BLE001 - no / unreadable meta = fetch
        pass
    resp = fmp_http_get(SHARES_HIST_URL, params={"symbol": symbol, "apikey": api_key},
                        endpoint="historical-shares-float", timeout=60)
    rows = resp.json()
    if not isinstance(rows, list):
        raise RuntimeError(f"historical/shares_float for {symbol}: unexpected {type(rows).__name__}")
    recs = []
    for x in rows:
        if not isinstance(x, dict) or not x.get("date") or not x.get("outstandingShares"):
            continue
        try:
            v = float(x["outstandingShares"])
        except (TypeError, ValueError):
            continue
        if v > 0:
            recs.append({"date": str(x["date"])[:10], "outstanding": v})
    df = pd.DataFrame(recs, columns=["date", "outstanding"])
    if not df.empty:
        df = df.drop_duplicates("date", keep="last").sort_values("date")
    _write_atomic(df, path)
    with open(meta, "w") as f:
        json.dump({"fetched_on": today.isoformat(), "n": int(len(df))}, f)
    return "fetched" if len(df) else "empty"


def prefetch_shares(cache_folder: str, symbols: List[str], api_key: str, *, max_age_days: int = 7, workers: int = 4,
                    log: Callable[[str], None] = print, max_per_second: Optional[float] = None,
                    deadline_utc: Optional[datetime] = None) -> Dict[str, Any]:
    """Fetch / refresh the vendor share history of every symbol (resumable: a fresh cache file is skipped, so an
    interrupted run continues where it stopped).  Returns the counts; failures are listed, not hidden."""
    t0 = time.time()
    counts = {"cached": 0, "fetched": 0, "empty": 0, "failed": 0, "skipped": 0}
    failed: List[str] = []

    import threading
    gate = threading.Lock()
    nxt = [0.0]
    stopped = [False]

    def _one(sym: str):
        if deadline_utc is not None and datetime.now(timezone.utc) >= deadline_utc:
            stopped[0] = True
            return sym, "skipped", "deadline reached (resume later)"
        if max_per_second:
            # a shared pacing clock: at most ``max_per_second`` vendor calls per second over all threads (the key is
            # shared with the live instances); a cache hit costs nothing and is checked inside fetch_historical_shares
            p = shares_cache_path(cache_folder, sym)
            if not _is_fresh(p, max_age_days):
                with gate:
                    now = time.time()
                    wait = nxt[0] - now
                    nxt[0] = max(now, nxt[0]) + 1.0 / max_per_second
                if wait > 0:
                    time.sleep(wait)
        try:
            return sym, fetch_historical_shares(cache_folder, sym, api_key, max_age_days=max_age_days), None
        except Exception as e:  # noqa: BLE001 - counted and reported below
            return sym, "failed", f"{type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for i, (sym, status, err) in enumerate(ex.map(_one, symbols), 1):
            counts[status] += 1
            if status == "failed":
                failed.append(f"{sym} ({err})")
            if i % 500 == 0:
                log(f"vendor share history: {i}/{len(symbols)} {counts} ({time.time() - t0:.0f}s)")
    counts["failed_symbols"] = failed[:20]
    log(f"vendor share history: {counts} in {time.time() - t0:.0f}s")
    return counts


def _is_fresh(path: str, max_age_days: int) -> bool:
    try:
        with open(path + ".meta.json") as f:
            fetched_on = date.fromisoformat(json.load(f)["fetched_on"])
        return os.path.exists(path) and (date.today() - fetched_on).days <= max_age_days
    except Exception:  # noqa: BLE001
        return False


def _vendor_history(cache_folder: str, sym: str) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    import pandas as pd
    p = shares_cache_path(cache_folder, sym)
    if not os.path.exists(p):
        return None
    try:
        df = pd.read_parquet(p)
    except Exception:  # noqa: BLE001
        return None
    if df.empty:
        return None
    d = pd.to_datetime(df["date"], errors="coerce")
    ok = d.notna()
    if not ok.any():
        return None
    o = (d[ok].to_numpy().astype("datetime64[D]").astype(np.int64) + 719163).astype(np.int64)
    v = df["outstanding"][ok].to_numpy(dtype=np.float64)
    order = np.argsort(o, kind="stable")
    return o[order], v[order]


# ----------------------------------------------------------------------- split calendars (as-traded basis)
def splits_cache_path(cache_folder: str, symbol: str) -> str:
    return os.path.join(cache_folder, "screener_fundamentals", "splits", f"{symbol.upper()}.json")


def load_split_calendar(cache_folder: str, symbol: str) -> Optional[List[Tuple[date, float]]]:
    """``[(split date, ratio)]`` (ratio = numerator / denominator: 4-for-1 -> 4.0) from the own splits cache, else from
    the market-condition warm-up's ``fmp_history/mc_stock_split__<SYM>.json`` (same FMP payload).  ``[]`` is a real
    answer (never split); ``None`` = no calendar known for the symbol."""
    for p in (splits_cache_path(cache_folder, symbol),
              os.path.join(cache_folder, "fmp_history", f"mc_stock_split__{symbol.upper()}.json")):
        if not os.path.exists(p):
            continue
        try:
            with open(p) as f:
                payload = json.load(f)
        except Exception:  # noqa: BLE001 - unreadable = unknown
            continue
        from ba2_providers import symbol_info
        return [(e.date, float(e.ratio)) for e in symbol_info.parse_splits(payload) if e.ratio]
    return None


def prefetch_splits(cache_folder: str, symbols: List[str], api_key: str, *, workers: int = 4,
                    max_per_second: float = 3.0, deadline_utc: Optional[datetime] = None,
                    log: Callable[[str], None] = print) -> Dict[str, Any]:
    """Fetch the split calendar (``symbol_info.fetch_splits``) of every symbol that has none cached.  Resumable (a
    cached calendar is never refetched; splits are historical facts), paced, failures counted and listed."""
    import threading
    from ba2_providers import symbol_info
    todo = [s for s in symbols if load_split_calendar(cache_folder, s) is None]
    counts = {"cached": len(symbols) - len(todo), "fetched": 0, "failed": 0, "skipped": 0}
    failed: List[str] = []
    gate, nxt = threading.Lock(), [0.0]
    t0 = time.time()

    def _one(sym: str):
        if deadline_utc is not None and datetime.now(timezone.utc) >= deadline_utc:
            return sym, "skipped", "deadline"
        with gate:
            now = time.time()
            wait = nxt[0] - now
            nxt[0] = max(now, nxt[0]) + 1.0 / max_per_second
        if wait > 0:
            time.sleep(wait)
        try:
            payload = symbol_info.fetch_splits(api_key, sym)
            if not isinstance(payload, (dict, list)):
                raise RuntimeError(f"unexpected split payload {type(payload).__name__}")
            hist = payload.get("historical", []) if isinstance(payload, dict) else payload
            p = splits_cache_path(cache_folder, sym)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "w") as f:
                json.dump({"symbol": sym, "fetched_on": date.today().isoformat(), "historical": hist or []}, f)
            os.replace(tmp, p)
            return sym, "fetched", None
        except Exception as e:  # noqa: BLE001
            return sym, "failed", f"{type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for i, (sym, status, err) in enumerate(ex.map(_one, todo), 1):
            counts[status] += 1
            if status == "failed":
                failed.append(f"{sym} ({err})")
            if i % 500 == 0:
                log(f"split calendars: {i}/{len(todo)} {counts} ({time.time() - t0:.0f}s)")
    counts["failed_symbols"] = failed[:20]
    log(f"split calendars: {counts}")
    return counts


def build_factor_matrix(cache_folder: str, symbols: List[str], sessions: List[str], basis_ord: int
                        ) -> Tuple[np.ndarray, Dict[str, Any]]:
    """``(S, T)`` as-traded multiplier of the adjusted cache for the morning of every session: the product of the ratios
    of the splits dated AFTER the session and up to ``basis_ord`` (the last day the adjusted cache covers) -- exactly
    ``split_basis.as_traded_factor``.  NaN for a symbol with no known split calendar (not a candidate; listed)."""
    sess_ord = np.array([date.fromisoformat(s).toordinal() for s in sessions], dtype=np.int64)
    out = np.ones((len(symbols), len(sessions)), dtype=np.float32)
    unknown: List[str] = []
    with_split: List[str] = []
    for s, sym in enumerate(symbols):
        cal = load_split_calendar(cache_folder, sym)
        if cal is None:
            out[s] = np.nan
            unknown.append(sym)
            continue
        inwin = [(d, r) for d, r in cal if date.fromisoformat(sessions[0]) <= d <= date.fromordinal(basis_ord)]
        if inwin:
            with_split.append(sym)
        for d, r in inwin:
            if not (np.isfinite(r) and r > 0):
                raise ls.SimulationRefusal(f"{sym}: split on {d} has an unusable ratio {r!r}")
            out[s] *= np.where(sess_ord < d.toordinal(), np.float32(r), np.float32(1.0))
    return out, {"splits_unknown": unknown, "symbols_with_split_in_window": len(with_split),
                 "split_symbols_examples": with_split[:10]}


# ------------------------------------------------------------------------------------------- fingerprints
def _stat_token(path: str) -> str:
    try:
        st = os.stat(path)
        return f"{st.st_size}:{st.st_mtime_ns}"
    except OSError:
        return "-"


def _ohlcv_path(cache_folder: str, sym: str) -> Optional[str]:
    d = os.path.join(cache_folder, "FMPOHLCVProvider")
    for cand in (sym, sym.replace("-", "_"), sym.replace("-", ".")):
        p = os.path.join(d, f"{cand}_1d.parquet")
        if os.path.exists(p):
            return p
    return None


def source_fingerprint(cache_folder: str, symbols: List[str], first_day: str, end_day: str,
                       lag: int, snapshot: Optional[str]) -> str:
    h = hashlib.sha1()
    h.update(f"{ls.CRITERIA_VERSION}|{ls.PANEL_FORMAT}|rev{BUILD_REV}|{first_day}|{end_day}|{lag}|"
             f"{os.path.basename(snapshot or '')}".encode())
    fund = os.path.join(cache_folder, "screener_fundamentals")
    for s in symbols:
        p = _ohlcv_path(cache_folder, s)
        h.update(f"{s}|{_stat_token(p) if p else '-'}|"
                 f"{_stat_token(os.path.join(fund, 'market_cap', s.upper() + '.parquet'))}|"
                 f"{_stat_token(shares_cache_path(cache_folder, s))}|"
                 f"{_stat_token(os.path.join(fund, 'float', s.upper() + '.parquet'))}|"
                 f"{_stat_token(splits_cache_path(cache_folder, s))}\n".encode())
    return h.hexdigest()


def symbols_without_daily_bars(cache_folder: str, symbols: List[str]) -> List[str]:
    """Universe symbols with no cached daily-bar file (the panel cannot screen them; ``fetch-cache`` fixes it)."""
    return [s for s in symbols if _ohlcv_path(cache_folder, s) is None]


def store_symbols(store_dir: str) -> List[str]:
    """The symbols of a metric store, read from the ``symbol`` column of its partitions only."""
    import pyarrow.parquet as pq
    out: set = set()
    for ym in sorted(os.listdir(store_dir)):
        d = os.path.join(store_dir, ym)
        if not (ym.startswith("ym=") and os.path.isdir(d)):
            continue
        for fn in os.listdir(d):
            if fn.endswith(".parquet"):
                out.update(pq.read_table(os.path.join(d, fn), columns=["symbol"]).column("symbol").to_pylist())
    return sorted(out)


# ----------------------------------------------------------------------------------------------- shares
def _factor_at(cal: List[Tuple[date, float]], ords: np.ndarray) -> np.ndarray:
    """``split_basis.as_traded_factor`` for many dates: the product of the ratios of the splits dated AFTER each date."""
    f = np.ones(len(ords), dtype=np.float64)
    for d, r in cal:
        f = f * np.where(ords < d.toordinal(), float(r), 1.0)
    return f


def _implied_shares(cache_folder: str, sym: str, bar_ord: np.ndarray, closes: np.ndarray,
                    cal: Optional[List[Tuple[date, float]]] = None
                    ) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    """(date ordinals, implied ADJUSTED share count) from the cached market-cap series and the bars' closes.  The market
    cap is as-traded and the cached closes are split-ADJUSTED, so ``cap / adjusted close`` = raw shares(t) x F(t) (F = the
    as-traded factor): continuous ACROSS a split (the raw count steps, F steps back).  It is lagged by the filing delay
    in this adjusted basis and divided by F(D) only afterwards (a split is known on its ex-date and must not be lagged)."""
    import pandas as pd
    p = os.path.join(cache_folder, "screener_fundamentals", "market_cap", f"{sym.upper()}.parquet")
    if not os.path.exists(p):
        return None
    try:
        mc = pd.read_parquet(p)
    except Exception:  # noqa: BLE001 - unreadable cache = no series (symbol reported as missing)
        return None
    if mc.empty or "market_cap" not in mc.columns:
        return None
    mc = mc.assign(d=pd.to_datetime(mc["date"], errors="coerce")).dropna(subset=["d"])
    mc["market_cap"] = pd.to_numeric(mc["market_cap"], errors="coerce")
    mc = mc.dropna(subset=["market_cap"]).drop_duplicates("d", keep="last").sort_values("d")
    if mc.empty:
        return None
    mo = (mc["d"].to_numpy().astype("datetime64[D]").astype(np.int64) + 719163).astype(np.int64)
    pos = np.searchsorted(bar_ord, mo, side="left")
    ok = (pos < bar_ord.size)
    ok &= bar_ord[np.minimum(pos, bar_ord.size - 1)] == mo
    if not ok.any():
        return None
    cl = closes[pos[ok]]
    cap = mc["market_cap"].to_numpy(dtype=np.float64)[ok]
    with np.errstate(invalid="ignore", divide="ignore"):
        sh = np.where(cl > 0, cap / cl, np.nan)
    good = np.isfinite(sh) & (sh > 0)
    if not good.any():
        return None
    return mo[ok][good], sh[good]


def build_shares_matrix(cache_folder: str, symbols: List[str], sessions: List[str],
                        bars: Dict[str, Tuple[np.ndarray, ...]], snapshot_path: str, lag_days: Any,
                        bar_ord: Dict[str, np.ndarray]) -> Tuple[Any, Dict[str, Any]]:
    """``(S, T)`` shares on the vendor's basis, NaN where unknown, and a report of the sources used.
    ``lag_days`` may be a list (experiments): a dict ``lag -> matrix`` is returned."""
    with open(snapshot_path) as f:
        snap = json.load(f)
    snap_rows: Dict[str, float] = snap["rows"]
    snap_caps: Dict[str, list] = snap.get("caps") or {}
    fetched = datetime.fromisoformat(snap["fetched_at_ny"])
    snap_day = fetched.date().toordinal()
    in_session = (fetched.weekday() < 5 and datetime.strptime("09:30", "%H:%M").time() <= fetched.time()
                  < datetime.strptime("16:05", "%H:%M").time())

    def cap_basis_now(sym: str, c_arr: np.ndarray, b_ord: np.ndarray) -> Optional[float]:
        """The vendor's share count AS ITS MARKET CAP USES IT (cap / price).  Outside regular hours cap and price
        are struck on the same close; in session the cap is on the previous close, so the price is read from the
        bars (a stale cache -> unknown)."""
        v = snap_caps.get(sym)
        if not v:
            return None
        cap, px = float(v[0]), float(v[1])
        if not in_session:
            return cap / px
        j = np.searchsorted(b_ord, snap_day, side="left") - 1
        if j >= 0 and snap_day - int(b_ord[j]) <= 4:
            return cap / float(c_arr[j])
        return None

    sess_ord = np.array([date.fromisoformat(s).toordinal() for s in sessions], dtype=np.int64)
    S, T = len(symbols), len(sessions)
    multi = isinstance(lag_days, (list, tuple))
    lags = list(lag_days) if multi else [int(lag_days)]
    outs = {lg: np.full((S, T), np.nan) for lg in lags}
    src = {"vendor_history": 0, "vendor_history_plus_fmp_pre_history": 0, "fmp_implied_calibrated": 0,
           "fmp_implied_uncalibrated": 0, "none": 0}
    none_syms: List[str] = []
    uncal: List[str] = []
    ks: List[float] = []
    for s, sym in enumerate(symbols):
        b = bars.get(sym)
        if b is None:
            continue
        c = b[4]
        vh = _vendor_history(cache_folder, sym)
        cal = load_split_calendar(cache_folder, sym) or []
        f_sess = _factor_at(cal, sess_ord)                                 # F(D) for every session of the panel
        imp = _implied_shares(cache_folder, sym, bar_ord[sym], c, cal)
        if vh is None and imp is None:
            src["none"] += 1
            none_syms.append(sym)
            continue
        now_out = snap_rows.get(sym)
        basis = cap_basis_now(sym, c, bar_ord[sym])
        k_cap = (basis / now_out) if (basis and now_out) else 1.0
        for lg in lags:
            row = np.full(T, np.nan)
            if vh is not None:
                vo, vv = vh
                i = np.searchsorted(vo, sess_ord, side="right") - 1
                # the vendor's value as of D, on the basis its market cap uses (k_cap fixes the ~4 % of names whose
                # cap is struck on another share count than outstandingShares)
                row = np.where(i >= 0, vv[np.maximum(i, 0)] * k_cap, np.nan)
                pre = sess_ord < vo[0]
                if pre.any() and imp is not None:                             # before the vendor series starts
                    mo, sh = imp
                    k_i = np.searchsorted(mo, vo[0], side="right") - 1
                    f0 = float(_factor_at(cal, np.array([vo[0]]))[0])
                    k0 = vv[0] * k_cap * f0 / sh[max(k_i, 0)] if k_i >= 0 else vv[0] * k_cap * f0 / sh[0]
                    j = np.searchsorted(mo, sess_ord - int(lg), side="right") - 1
                    row = np.where(pre & (j >= 0), sh[np.maximum(j, 0)] * k0 / f_sess, row)
            else:
                mo, sh = imp
                k = 1.0
                now_v = basis or snap_rows.get(sym)
                if now_v:
                    k_i = np.searchsorted(mo, snap_day, side="right") - 1
                    if k_i >= 0:
                        k = now_v / sh[k_i]
                j = np.searchsorted(mo, sess_ord - int(lg), side="right") - 1
                row = np.where(j >= 0, sh[np.maximum(j, 0)] * k / f_sess, np.nan)
            outs[lg][s] = row
        if vh is not None:
            src["vendor_history_plus_fmp_pre_history" if (imp is not None and vh[0][0] > sess_ord[0]) else "vendor_history"] += 1
        else:
            if basis or snap_rows.get(sym):
                src["fmp_implied_calibrated"] += 1
                ks.append((basis or snap_rows[sym]) / imp[1][max(np.searchsorted(imp[0], snap_day, side="right") - 1, 0)])
            else:
                src["fmp_implied_uncalibrated"] += 1
                uncal.append(sym)
    ka = np.array(ks) if ks else np.array([1.0])
    rep = {"snapshot": os.path.basename(snapshot_path), "sources": src, "no_share_data_examples": none_syms[:10],
           "no_share_data_symbols": none_syms,
           "uncalibrated_examples": uncal[:10], "fmp_implied_k_median": float(np.median(ka)),
           "fmp_implied_k_share_off_by_over_5pct": float(np.mean(np.abs(ka - 1) > 0.05))}
    return (outs if multi else outs[lags[0]]), rep


def build_float_matrix(cache_folder: str, symbols: List[str], sessions: List[str]) -> Tuple[np.ndarray, Dict[str, Any]]:
    """``(S, T)`` free float, effective-dated (``fetch_historical_float`` stores each row on its filing /
    accepted date, so an as-of read cannot see a float before it was public); NaN = unknown (passes the
    float stage, as in live)."""
    import pandas as pd
    sess_ord = np.array([date.fromisoformat(s).toordinal() for s in sessions], dtype=np.int64)
    out = np.full((len(symbols), len(sessions)), np.nan)
    n = 0
    for s, sym in enumerate(symbols):
        p = os.path.join(cache_folder, "screener_fundamentals", "float", f"{sym.upper()}.parquet")
        if not os.path.exists(p):
            continue
        try:
            df = pd.read_parquet(p)
        except Exception:  # noqa: BLE001
            continue
        if df.empty or "float_shares" not in df.columns:
            continue
        d = pd.to_datetime(df["date"], errors="coerce")
        v = pd.to_numeric(df["float_shares"], errors="coerce")
        ok = d.notna() & v.notna()
        if not ok.any():
            continue
        fo = (d[ok].to_numpy().astype("datetime64[D]").astype(np.int64) + 719163).astype(np.int64)
        fv = v[ok].to_numpy(dtype=np.float64)
        order = np.argsort(fo, kind="stable")
        fo, fv = fo[order], fv[order]
        i = np.searchsorted(fo, sess_ord, side="right") - 1
        out[s] = np.where(i >= 0, fv[np.maximum(i, 0)], np.nan)
        n += 1
    return out, {"symbols_with_float_series": n}


def load_bars(cache_folder: str, symbols: List[str], sessions: List[str], workers: int = 8
              ) -> Tuple[Dict[str, Tuple[np.ndarray, ...]], Dict[str, np.ndarray], List[str]]:
    """``bars[sym] = (session_idx, o, h, l, c, v)`` restricted to the panel's sessions, and the bars'
    date ordinals."""
    sess_ord = np.array([date.fromisoformat(s).toordinal() for s in sessions], dtype=np.int64)
    cdir = os.path.join(cache_folder, "FMPOHLCVProvider")

    def _one(sym: str):
        r = ls._read_daily(cdir, sym)
        if r is None:
            return sym, None
        d, o, h, l, c, v = r
        pos = np.searchsorted(sess_ord, d, side="left")
        keep = (pos < sess_ord.size) & (sess_ord[np.minimum(pos, sess_ord.size - 1)] == d)
        if not keep.any():
            return sym, None
        return sym, (pos[keep], o[keep], h[keep], l[keep], c[keep], v[keep], d[keep])

    bars: Dict[str, Tuple[np.ndarray, ...]] = {}
    bar_ord: Dict[str, np.ndarray] = {}
    no_data: List[str] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for sym, res in ex.map(_one, symbols):
            if res is None:
                no_data.append(sym)
            else:
                bars[sym] = res[:6]
                bar_ord[sym] = res[6]
    return bars, bar_ord, no_data


def build_daily_panel(cache_folder: str, symbols: List[str], first_day: str, end_day: str, *,
                      lag_days: int = ls.SHARES_LAG_DAYS, workers: int = 8, force: bool = False,
                      log: Callable[[str], None] = print, out_root: Optional[str] = None,
                      snapshot_dir: Optional[str] = None, acknowledged_stale: Iterable[str] = ()) -> Dict[str, Any]:
    """Build / refresh the panel for ``symbols``.  Returns the manifest (``status`` 'up_to_date' | 'built').
    ``first_day`` / ``end_day``: the session range (end extended by ten calendar days of FUTURE sessions so
    the daily clock's next-session lookup and the last decisions have a column)."""
    import time
    from ba2_common.core.market_calendar import regular_session_dates
    t0 = time.time()
    snap = latest_vendor_snapshot(snapshot_dir or cache_folder)
    if snap is None:
        raise ls.SimulationRefusal(
            f"no vendor share table under {snapshot_dir or vendor_dir(cache_folder)}: take one (ONE FMP call) with "
            f"`ba2-test prewarm --screener-panel` / `ba2-test build-screener-metrics --daily-panel`")
    symbols = sorted(set(symbols))
    fp = source_fingerprint(cache_folder, symbols, first_day, end_day, lag_days, snap)
    root = out_root or ls.panel_root(cache_folder)
    path = os.path.join(root, fp[:16])
    man = ls.read_manifest(path)
    if man and not force and man.get("source_fingerprint") == fp and man.get("criteria_version") == ls.CRITERIA_VERSION:
        log(f"daily panel: up to date at {path} ({man['n_symbols']} symbols, {man['first_session']}..{man['last_session']})")
        return dict(man, status="up_to_date", path=path)
    last = (date.fromisoformat(end_day) + timedelta(days=10))
    sessions = [d.isoformat() for d in regular_session_dates(date.fromisoformat(first_day), last)]
    log(f"daily panel: {len(symbols)} symbols x {len(sessions)} sessions ({sessions[0]}..{sessions[-1]})")
    bars, bar_ord, no_data = load_bars(cache_folder, symbols, sessions, workers=workers)
    log(f"daily panel: bars read for {len(bars)} symbols ({len(no_data)} without cached daily bars) "
        f"in {time.time() - t0:.0f}s")
    shares, srep = build_shares_matrix(cache_folder, symbols, sessions, bars, snap, lag_days, bar_ord)
    flt, frep = build_float_matrix(cache_folder, symbols, sessions)
    basis = max((int(bd[-1]) for bd in bar_ord.values()), default=date.fromisoformat(first_day).toordinal())
    fac, split_rep = build_factor_matrix(cache_folder, symbols, sessions, basis)
    arrays = ls.build_panel_arrays(bars, sessions, shares, symbols, progress=lambda m: log(f"daily panel: {m}"),
                                   fl=flt, fac=fac)
    last_bar = date.fromordinal(int(max((int(bd[-1]) for bd in bar_ord.values()),
                                        default=date.fromisoformat(first_day).toordinal()))).isoformat()
    # cache completeness: the universe is today's actively traded names, so nearly all must have a recent bar
    lb = max((int(bd[-1]) for bd in bar_ord.values()), default=0)
    fresh = sum(1 for bd in bar_ord.values() if lb - int(bd[-1]) <= 6)
    # COVERAGE, as explicit lists (no tolerance): a symbol in the vendor's CURRENT listing whose bars are stale is a
    # defect of the cache (refuse); a stale symbol that is not listed any more is treated as delisted (counted/listed)
    with open(snap) as f:
        listing = set((json.load(f).get("caps") or {}).keys())
    ack = {str(x).upper() for x in acknowledged_stale}
    lb_ord = max((int(bd[-1]) for bd in bar_ord.values()), default=0)
    stale = sorted(sy for sy, bd in bar_ord.items() if lb_ord - int(bd[-1]) > 6)
    stale_listed = [sy for sy in stale if sy in listing and sy not in ack]
    delisted = [sy for sy in stale if sy not in listing]
    shares_missing_listed = [sy for sy in listing & set(symbols) if sy in set(srep.get("no_share_data_symbols", []))]
    manifest = {"source_fingerprint": fp, "panel_fingerprint": fp[:16], "stale_listed_symbols": stale_listed,
                "delisted_symbols": delisted, "delisted_symbols_count": len(delisted),
                "acknowledged_stale": sorted(ack), "split_report": split_rep,
                "splits_unknown": split_rep["splits_unknown"], "shares_missing_listed": shares_missing_listed,
                "shares_lag_days": lag_days, "shares_vendor_snapshot": srep["snapshot"],
                "shares_report": srep, "float_report": frep, "symbols_without_daily_bars": no_data[:50],
                "n_without_daily_bars": len(no_data), "last_bar_date": last_bar,
                "fresh_fraction": round(fresh / max(1, len(symbols)), 4), "build_seconds": round(time.time() - t0, 1)}
    ls.save_panel(path, symbols, sessions, arrays, manifest)
    size = sum(os.path.getsize(os.path.join(path, f)) for f in os.listdir(path)) / 1e6
    log(f"daily panel: built at {path} in {time.time() - t0:.0f}s, {size:.0f} MB, shares: {srep['sources']}; "
        f"{len(stale_listed)} stale listed, {len(delisted)} treated as delisted, {len(shares_missing_listed)} listed without shares")
    for d, m, mb in ls.list_panels_in(root):
        log(f"  panel {os.path.basename(d)}  built {m.get('built_at')}  {mb:.0f} MB" + ("  <- current" if d == path else
            "  (older: remove by hand when no job uses it)"))
    return dict(ls.read_manifest(path) or {}, status="built", path=path)

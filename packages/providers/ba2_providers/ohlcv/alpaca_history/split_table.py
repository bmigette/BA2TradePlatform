"""Our own split table over the RAW Alpaca daily store: every candidate split is judged by the raw price.

Runbook: docs/REPRODUCE-BACKTESTS.md section 4 (data preparation).  Run it through the pipeline CLI, step ``split-table``
(offline once the FMP calendar windows are cached):

    python tools/prepare_history.py --source alpaca --steps split-table

This module is the RULES (pure: no network, no writes) plus this spec; the loaders, the FMP calendar fetch, the writers and
``build_split_table`` are in ``split_build.py``.

WHY.  Neither vendor's split list is trusted: Alpaca lists spurious events (HON, NVS, STRR, SNEX, BIP, DRS, IR) and misses
real ones (KOD, UHAL, SNX, IAC, RTX); FMP has mixed-basis series (CRWD, META before 2022-06).  The bars here are RAW, so a
real split shows as an overnight jump ``close(D-1) / open(D)`` about equal to the factor ``f`` (new shares / old shares; a
reverse split has f < 1).  Task 4 (the cache build) reads only ``_meta/split_table.json``.

INPUTS (all read-only)
  * ``--store`` (default ~/Documents/ba2/alpaca_store_daily2016): ``bars/1Day/symbol=<S>/all.parquet`` (t UTC bar start, o,
    h, l, c, v, n, vw; folder names use '-' as in BRK-B), ``corporate_actions/ym=YYYY-MM.json.gz``, ``STORE_INFO.json``.  Every daily ``t`` must
    be exactly 00:00 New York time (04:00Z in summer, 05:00Z in winter; the session is its date) and each session may appear
    once, else the build STOPS (SplitTableBuildError); the corporate actions must hold at least one forward or reverse split.
    The calendar is fetched from ``--fmp-from`` to ``--fmp-to`` (default: the store's asof + 1 day; earlier than the asof is
    refused).
  * FMP split calendar ``/api/v3/stock_split_calendar`` (date, symbol, numerator, denominator), fetched in 30-day windows
    (a window of 1000+ rows is halved down to one day; a row more than a day outside its window is refused) and cached raw
    in ``<out>/_meta/fmp_split_calendar/<from>_<to>.json``.  The key is the AppSetting FMP_API_KEY of ``--db`` (read-only),
    read only when a window is not cached, never printed.
  * FMP ADJUSTED daily files ``<FMP daily dir>/<SYM>_1d.parquet`` (Date, Close; '.' written '-') for the FMP step.
  * ``split_overrides.json`` (below).

LISTINGS -> EVENTS.  Alpaca: forward_splits and reverse_splits (ex_date, f = new_rate/old_rate), stock_dividends (ex_date,
f = rate), unit_splits ONLY when old_symbol == new_symbol (effective_date, f = new_rate/old_rate; a unit separated into a
different symbol is ignored and counted).  IGNORED, because FMP's close does not adjust for them either: spin_offs,
cash_dividends, mergers (cash, stock, stock_and_cash), rights_distributions, redemptions, worthless_removals.  FMP:
numerator/denominator (a row with a missing or zero side is counted, never read as a split; a boolean or negative side stops the build).  An absent kind in a monthly
file means none that month.  f == 1 events are counted and dropped.
NAME CHANGES.  The bars were fetched as-of 2026-10-12, so a renamed ticker's whole history sits under its CURRENT symbol,
while a split before the rename is listed under the OLD one: an event is mapped to the current symbol by following the
``name_changes`` chain old -> new in process_date order, using only renames whose process_date is AFTER the event date (a
same-day rename is ambiguous -- tickers swap on a date -- and is not followed); a rename to the same ticker (CUSIP-only) is
skipped.  Symbols are upper-cased with '.' written '-'.  Events of symbols not in the store are counted, not judged.

THE RULES (tolerances are module constants)
  * Event placement: listings of one symbol whose first sessions on/after their dates are at most NEAR_SESSIONS = 3
    sessions apart (chained) form a cluster.  A cluster where each vendor lists at most one ratio is ONE event; where a
    vendor lists several ratios it is one event per ratio (AMC 2023-08: a 1.13333 distribution and the 1:10 reverse split).
    Duplicate listings (ratio within RATIO_TOL = 1 %) are one (``dup_listings_collapsed``).
  * An event dated on/before the symbol's first bar or after its last cannot affect its prices: counted
    (``outside_range_<source>``), not reviewed.
  * ``jump`` = close(D-1) / open(D), D = the first session on/after the event date.  The jump needs a bar before D and at
    most MAX_GAP_DAYS = 30 days between the two bars; else the price cannot observe the event (``no_price_data``).
  * BIG event: ``|ln f| >= ln 1.25`` (BIG_MIN).  PROVEN BY JUMP when ``|ln jump - ln f| <= ln 1.15`` (PROVEN_TOL), looked
    for at the listed dates first, then up to 3 sessions either side (a vendor's ex-date can be a day or two off); the
    accepted date is the session that jumped.  CONTRADICTED when every jump is within ``ln 1.10`` of 1 (CONTRADICT_TOL).
  * SMALL event (``|ln f| < ln 1.25``: stock dividends, small unit splits): the price cannot prove it.  Accepted only when
    BOTH vendors list it (ratios within 1 %); otherwise review, not applied.  The FMP step is NOT used for these (it also
    reflects spin-off adjustments such as HON's, which our convention does not apply).
  * THE FMP STEP (second arbiter).  r(t) = FMP ADJUSTED close / Alpaca RAW close on the sessions both have.  FMP divides
    its history by the factors it applied, so across an event the market move cancels in r and
    ``step = r(D) / r(D-1)`` equals the factor FMP applied (1.0 if none); D-1 = the last common session before D, D the first
    on/after it.  STABILITY: also take the 2 common sessions before D-1 and the 2 after D; if r varies by more than 2 %
    (STEP_STABLE) within either 3-session side the vote is ``unstable`` -- unless another LISTED event (of another unit, an
    override) lies in those sessions, then only D-1 and D count.  (Not "accepted": the verdicts of the neighbours are not
    known yet.)  VOTE: ``equals_f`` when ``|ln step - ln f| <= ln 1.02`` (STEP_TOL), ``flat`` when ``|ln step| <= ln 1.02``,
    ``other``, ``none`` (no FMP data on both sides), ``unstable``.  Column ``fmp_step`` = "<step> <vote>" (or "none"; empty
    when the arbiter is off).  Candidate sessions for the step are searched like the jump's.  CAVEAT: step == f shows FMP
    APPLIED the factor, not that the market had the split; that is why a step-proven accept is GUARDED (below) and the step is
    recorded per event (``fmp_step``).  GUARD: ``proven_by_fmp_step`` needs the step equal to f within 2 % AND the raw jump
    EITHER (i) within x1.5 of f (STEP_JUMP_MAX) and not within ln 1.10 of 1, OR (ii) BOTH vendors list f (within 1 %) and the
    jump is closer to f than to 1 in log terms (|ln jump - ln f| < |ln jump|: a reverse split on a day the stock also moved
    hard); otherwise the event is NOT applied and gets the review verdict
    ``fmp_step_without_jump`` (FMP adjusted for a separation / spin-off the raw bars barely show: ADEA, JBIO, NEUP, RGC, TJGC).
  * both vendors list DIFFERENT ratios (not within 1 %): the ratio the jump proves wins if exactly one is proven
    (``ratio_mismatch_resolved``); else the ratio FMP's step equals wins if exactly one does and the jump does not contradict
    it (``ratio_mismatch_resolved_by_fmp_step``); else ``ratio_mismatch``.
  * UNEXPLAINED JUMPS: an overnight close(t-1)/open(t) > 1.8 or < 0.55 with no listed event (or override) within 3 sessions
    is a review row, unless the FMP step there is ``flat`` (``jump_explained_by_market``: a real price move, counted).
    A pair of bars further apart than MAX_GAP_DAYS observes no overnight and is skipped.
  * Two accepted BIG events in the same direction within 3 sessions would adjust one split twice (the vendors list slightly
    different ratios, both "proven"): both are retracted (``overlapping_accepts``).
  * OVERRIDES (``split_overrides.json``, reviewed, same rules as screener/panel_exclusions.json): ``_doc`` + ``entries``
    with symbol, event_date (ISO), decision accept|reject, reason, added, reviewed_by, and ``ratio`` for accept (> 0, != 1).
    An entry decides the event of its symbol that lists event_date (any date of the cluster) outright; one matching no listing
    ADDS the split (accept) or silences the unexplained-jump row at its date (reject).  An override that matches more than one
    listing group (or two overrides one group) is ambiguous and stops the build; the listings an override absorbs are counted
    (``listings_absorbed_by_override``).  A reviewed_by starting with
    'pending' REFUSES the build (``SplitOverridesPending``; the pipeline exits 2).  The file's sha256 is in the table.

VERDICT CODES (``VERDICTS``; counted in the printed summary as ``verdict_<code>``)
  accepted:  proven                  big, jump proves it (any source; FMP-only gets fmp_only_proven)
             fmp_only_proven         FMP-only and proven by the jump: accepted + review row "Alpaca missed"
             proven_by_fmp_step      big, jump does not prove it but FMP's step equals f and the guard holds: accepted (FMP-only: + review row)
             ratio_mismatch_resolved           vendors disagree; the jump proves one ratio
             ratio_mismatch_resolved_by_fmp_step  vendors disagree; the step equals one, the jump does not contradict it
             small_both              small, both vendors list it
             override_accept         decided by an override entry
  rejected without a review row:
             override_reject         decided by an override entry
             rejected_confirmed      listing contradicted by the raw jump AND flat in FMP's step: no split anywhere
             jump_explained_by_market (an unexplained jump, not an event) flat FMP step: a real price move
  review rows (``split_review.csv``; applied False unless noted):
             alpaca_contradicted / fmp_contradicted / both_contradicted   listed by that vendor(s), the raw price does not
                                     jump and FMP's step is not flat (or is unstable/none)
             unresolved              listed, the jump neither proves nor contradicts it, the step does not equal f
             fmp_step_without_jump   the step equals f but neither guard condition (i) nor (ii) holds
             small_single_vendor     small, only one vendor lists it
             ratio_mismatch          vendors disagree and neither jump nor step settles it (or both are proven)
             no_price_data           no usable bars, or none within 30 days on both sides of the event
             unexplained_jump        raw overnight jump > x1.8 / < x0.55 with no event near and the step not flat
             overlapping_accepts     see above (both retracted)
  (applied True review rows are the informational ones of fmp_only_proven, proven_by_fmp_step and the two resolved verdicts)

OUTPUTS (``<out>/_meta``, atomic temp + os.replace; read the CSVs with ``read_csv_frame`` -- pandas' default NA parsing
turns the symbol "NA" into NaN)
  split_table.json     {run_id, asof, inputs_sha256, overrides_sha256, fmp_daily_files {dir, count, sha256 over (name,
                       size, mtime_ns), content_sha256 over (name, size, sha256 of the bytes), inputs_csv},
                       splits {symbol: [[date, factor], ...]}} events sorted; factor = new/old shares, prices BEFORE the
                       date are divided by it.  Written LAST.  Two accepted events of one symbol on one date stop the build (decide them with an
                       override).
  RUN COHERENCE        every run has a run_id (UTC timestamp + 8 hex of a hash of the inputs); it is in split_table.json and
                       the last column ("run_id") of every CSV below.  The CSVs are written first, the table last, so a
                       crash never leaves a table newer than its CSVs; the cache build checks that the run_ids match.
  fmp_daily_inputs.csv file, size, mtime_ns, mtime_utc, sha256 (of the bytes) of every FMP daily file read: the record of
                       exactly which bytes were used (that cache is archived after the build)
  split_review.csv     REVIEW_COLUMNS (+ run_id): symbol, event_date, alpaca_ratio, fmp_ratio, jump, fmp_step, verdict, applied, reason
  split_accepted.csv   ACCEPTED_COLUMNS: symbol, date, factor, source (alpaca|fmp|both|override), verdict, jump, fmp_step
  split_vs_fmp.csv     COMPARE_COLUMNS: symbol, date, our_factor, fmp_ratio, alpaca_ratio, jump, fmp_step, verdict, class;
                       class agree | ratio_differs | ours_only | fmp_only_accepted (ours and FMP, Alpaca does not list it)
                       | fmp_only_rejected (verdict = our review verdict, outside_range, rejected_confirmed or no_row)
  fmp_split_calendar/  the raw calendar windows (reruns are offline)
"""

from __future__ import annotations

import gzip
import json
import math
import os
from collections import Counter
from datetime import date
from typing import Any, Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Tuple

import numpy as np
import pandas as pd


OVERRIDES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "split_overrides.json")

PROVEN_TOL = math.log(1.15)        # |ln jump - ln f| within this: the price shows the split
BIG_MIN = math.log(1.25)           # |ln f| below this is "small": price cannot prove it
CONTRADICT_TOL = math.log(1.10)    # |ln jump| within this (for a big f): the price shows no split
RATIO_TOL = 0.01                   # two listings of one event agree when their ratios are within 1 %
NEAR_SESSIONS = 3                  # listings / unexplained jumps this close are the same place
JUMP_HI = 1.8                      # unexplained overnight jump bounds
JUMP_LO = 0.55
MAX_GAP_DAYS = 30                  # bars further apart than this do not observe an event between them
STEP_TOL = math.log(1.02)          # FMP step: |ln step - ln f| within this -> "equals_f"; |ln step| within this -> "flat"
STEP_JUMP_MAX = math.log(1.5)      # a step-proven accept also needs the raw jump within this of ln f (and not flat)
STEP_STABLE = 0.02                 # FMP step: r may vary by at most 2 % within each 3-session side of the event

VERDICTS = ("proven", "fmp_only_proven", "proven_by_fmp_step", "ratio_mismatch_resolved",
            "ratio_mismatch_resolved_by_fmp_step", "small_both", "override_accept", "override_reject",
            "rejected_confirmed", "jump_explained_by_market", "alpaca_contradicted", "fmp_contradicted",
            "both_contradicted", "unresolved", "fmp_step_without_jump", "small_single_vendor", "ratio_mismatch", "no_price_data",
            "unexplained_jump", "overlapping_accepts")
REVIEW_COLUMNS = ["symbol", "event_date", "alpaca_ratio", "fmp_ratio", "jump", "fmp_step", "verdict", "applied", "reason"]
ACCEPTED_COLUMNS = ["symbol", "date", "factor", "source", "verdict", "jump", "fmp_step"]


class ListedSplit(NamedTuple):
    symbol: str
    date: date
    ratio: float            # new shares / old shares: 4-for-1 -> 4.0, 1-for-10 -> 0.1


class SplitTableBuildError(RuntimeError):
    """An input is missing or has changed shape: the build stops (the pipeline step fails)."""


class SplitOverridesPending(ValueError):
    """An override entry is still pending review: the build must not run."""


def _norm_symbol(s: str) -> str:
    return s.strip().upper().replace(".", "-")        # the store's folder names use '-' (BRK-B)


# ---------------------------------------------------------------------------
# overrides
# ---------------------------------------------------------------------------
def load_overrides(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every entry of the reviewed overrides file, validated.  Raises ``SplitOverridesPending`` while any entry's
    ``reviewed_by`` starts with 'pending', ``ValueError`` for a malformed entry."""
    with open(path or OVERRIDES_PATH, encoding="utf-8") as f:
        entries = json.load(f)["entries"]
    seen = set()
    for e in entries:
        miss = [k for k in ("symbol", "event_date", "decision", "reason", "added", "reviewed_by") if not e.get(k)]
        if miss:
            raise ValueError(f"split override entry {e!r} lacks {miss}: needs symbol, event_date, decision, reason, "
                             f"a date and a reviewer")
        if e["decision"] not in ("accept", "reject"):
            raise ValueError(f"split override {e['symbol']} {e['event_date']}: decision must be accept|reject, "
                             f"got {e['decision']!r}")
        date.fromisoformat(e["event_date"])
        if e["decision"] == "accept":
            r = e.get("ratio")
            if not isinstance(r, (int, float)) or not math.isfinite(r) or r <= 0 or r == 1:
                raise ValueError(f"split override {e['symbol']} {e['event_date']}: 'accept' needs a ratio > 0 and != 1, "
                                 f"got {r!r}")
        key = (_norm_symbol(e["symbol"]), e["event_date"])
        if key in seen:
            raise ValueError(f"split override {key} is listed twice")
        seen.add(key)
    pending = [e for e in entries if str(e["reviewed_by"]).lower().startswith("pending")]
    if pending:
        raise SplitOverridesPending(
            f"{len(pending)} split override(s) pending review ({', '.join(e['symbol'] + ' ' + e['event_date'] for e in pending)}): "
            f"set reviewed_by in {path or OVERRIDES_PATH} before building")
    return entries


# ---------------------------------------------------------------------------
# vendor listings -> ListedSplit
# ---------------------------------------------------------------------------
def _kind(month: Mapping[str, Any], kind: str) -> List[Mapping[str, Any]]:
    """The actions of one kind in a monthly file.  Alpaca OMITS a kind with no action that month (measured: 'forward_splits'
    is absent from 4 of 130 files, 'name_changes' from 42), so an absent key is an empty list -- for the kinds we read only."""
    return month[kind] if kind in month else []


def build_rename_map(corporate_actions: Iterable[Mapping[str, Any]]) -> Dict[str, List[Tuple[date, str]]]:
    """old symbol -> [(process_date, new symbol)] sorted by date, from the ``name_changes`` of the monthly files."""
    out: Dict[str, List[Tuple[date, str]]] = {}
    for month in corporate_actions:
        for nc in _kind(month, "name_changes"):
            old, new = _norm_symbol(nc["old_symbol"]), _norm_symbol(nc["new_symbol"])
            if old == new:                       # a CUSIP-only change: the ticker stays
                continue
            out.setdefault(old, []).append((date.fromisoformat(nc["process_date"]), new))
    for lst in out.values():
        lst.sort()
    return out


def map_to_current(symbol: str, on: date, renames: Mapping[str, Sequence[Tuple[date, str]]]) -> str:
    """The symbol the bars of ``symbol`` as of ``on`` sit under: follow every rename effective AFTER ``on`` (a same-day one is ambiguous: tickers swap on a date),
    in date order (the store was fetched as-of a later date, so a renamed ticker's whole history is under its
    CURRENT symbol)."""
    cur, at = _norm_symbol(symbol), on
    for _ in range(25):
        nxt = next(((d, n) for d, n in renames.get(cur, ()) if d > at), None)
        if nxt is None:
            return cur
        at, cur = nxt
    raise ValueError(f"rename chain of {symbol} from {on} does not end (cycle?)")


def alpaca_events_from_actions(corporate_actions: Iterable[Mapping[str, Any]],
                               renames: Mapping[str, Sequence[Tuple[date, str]]],
                               stats: Optional[Counter] = None) -> List[ListedSplit]:
    """Factor events of the Alpaca corporate-action files, mapped to current symbols.  Spin-offs, cash dividends and
    mergers are not price adjustments here (same convention as FMP close) and are ignored; a unit split into a
    DIFFERENT symbol (SPAC unit separation) is not a factor event of either symbol."""
    stats = stats if stats is not None else Counter()
    out: List[ListedSplit] = []

    def add(sym: str, d: str, ratio: float, kind: str) -> None:
        if not (isinstance(ratio, (int, float)) and math.isfinite(ratio) and ratio > 0):
            raise ValueError(f"alpaca {kind} {sym} {d}: unusable ratio {ratio!r}")
        day = date.fromisoformat(d)
        stats[f"alpaca_{kind}"] += 1
        out.append(ListedSplit(map_to_current(sym, day, renames), day, float(ratio)))

    for month in corporate_actions:
        for e in _kind(month, "forward_splits"):
            add(e["symbol"], e["ex_date"], e["new_rate"] / e["old_rate"], "forward_splits")
        for e in _kind(month, "reverse_splits"):
            add(e["symbol"], e["ex_date"], e["new_rate"] / e["old_rate"], "reverse_splits")
        for e in _kind(month, "stock_dividends"):
            add(e["symbol"], e["ex_date"], e["rate"], "stock_dividends")
        for e in _kind(month, "unit_splits"):
            if _norm_symbol(e["old_symbol"]) != _norm_symbol(e["new_symbol"]):
                stats["alpaca_unit_splits_other_symbol_ignored"] += 1
                continue
            add(e["old_symbol"], e["effective_date"], e["new_rate"] / e["old_rate"], "unit_splits")
    return out


def fmp_events_from_rows(rows: Iterable[Mapping[str, Any]],
                         renames: Mapping[str, Sequence[Tuple[date, str]]],
                         stats: Optional[Counter] = None) -> List[ListedSplit]:
    """Events of FMP split-calendar rows (``date, symbol, numerator, denominator``).  A row with a missing or zero
    side has no ratio and is counted, never read as a 0-for-1 split."""
    stats = stats if stats is not None else Counter()
    out: List[ListedSplit] = []
    for r in rows:
        num, den = r.get("numerator"), r.get("denominator")
        if isinstance(num, bool) or isinstance(den, bool) or (isinstance(num, (int, float)) and num < 0) \
                or (isinstance(den, (int, float)) and den < 0):
            raise SplitTableBuildError(f"FMP split row {dict(r)!r}: a boolean or negative numerator/denominator")
        if not isinstance(num, (int, float)) or not isinstance(den, (int, float)) or not den or num <= 0:
            stats["fmp_rows_without_ratio"] += 1
            continue
        day = date.fromisoformat(r["date"][:10])
        stats["fmp_rows"] += 1
        out.append(ListedSplit(map_to_current(r["symbol"], day, renames), day, float(num) / float(den)))
    return out


def corporate_action_paths(store: str) -> List[str]:
    """The monthly corporate-action files of a store (``ym=*.json.gz``, a stray temp file is not one), oldest first --
    the ONE listing both the reader and the input hash use."""
    d = os.path.join(store, "corporate_actions")
    paths = [os.path.join(d, n) for n in sorted(os.listdir(d)) if n.startswith("ym=") and n.endswith(".json.gz")]
    if not paths:
        raise FileNotFoundError(f"no corporate_actions/ym=*.json.gz under {store}")
    return paths


def read_corporate_actions(store: str) -> List[Dict[str, Any]]:
    """The monthly corporate-action files of a store, oldest first."""
    out = []
    for path in corporate_action_paths(store):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            out.append(json.load(f))
    return out


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------
class _Bars(NamedTuple):
    days: np.ndarray        # datetime64[D], ascending, one per session with a usable bar
    o: np.ndarray
    c: np.ndarray


def _bars(df: pd.DataFrame, stats: Counter, symbol: str = "?") -> _Bars:
    """Usable daily bars as sessions.  Alpaca's daily ``t`` is the bar start; converted to New York time EVERY daily bar must
    be exactly 00:00 there (04:00Z in summer, 05:00Z in winter) -- the session is then its date -- and a session may appear
    once: anything else means the store's timestamps are not what this module assumes, and the build stops."""
    t = pd.to_datetime(df["t"])
    if t.dt.tz is not None:
        t = t.dt.tz_convert("America/New_York").dt.tz_localize(None)
    off = (t - t.dt.normalize()) != pd.Timedelta(0)
    if off.any():
        raise SplitTableBuildError(f"{symbol}: {int(off.sum())} daily bar(s) are not at 00:00 New York time "
                                   f"(first: {t[off].iloc[0]}): the store's timestamps are not the assumed bar starts")
    days = t.dt.normalize().to_numpy().astype("datetime64[D]")
    if len(np.unique(days)) != len(days):
        raise SplitTableBuildError(f"{symbol}: the daily bars hold the same session more than once")
    o = df["o"].to_numpy(dtype=float)
    c = df["c"].to_numpy(dtype=float)
    ok = np.isfinite(o) & np.isfinite(c) & (o > 0) & (c > 0)
    if not ok.all():
        stats["bars_unusable_skipped"] += int((~ok).sum())
    order = np.argsort(days[ok], kind="stable")
    return _Bars(days[ok][order], o[ok][order], c[ok][order])


class _RatioSeries:
    """r(t) = FMP ADJUSTED daily close / Alpaca RAW daily close on the sessions both have.  FMP divides its history
    by the factors it applied, so across an event the market move cancels in r: ``step = r(D) / r(D-1)`` equals the
    factor FMP applied (1.0 when it applied none)."""

    def __init__(self, days: np.ndarray, vals: np.ndarray):
        self.days, self.vals = days, vals

    @classmethod
    def build(cls, bars: _Bars, fmp_df: pd.DataFrame) -> Optional["_RatioSeries"]:
        cols = {c.lower(): c for c in fmp_df.columns}
        if "date" not in cols or "close" not in cols:
            raise ValueError(f"FMP daily frame needs Date and Close columns, has {list(fmp_df.columns)}")
        idx = pd.DatetimeIndex(pd.to_datetime(fmp_df[cols["date"]])).tz_localize(None).normalize()
        f = pd.Series(fmp_df[cols["close"]].to_numpy(dtype=float), index=idx)
        a = pd.Series(bars.c, index=pd.DatetimeIndex(bars.days))
        r = (f[~f.index.duplicated()] / a[~a.index.duplicated()]).dropna()
        r = r[np.isfinite(r) & (r > 0)].sort_index()
        if not len(r):
            return None
        return cls(r.index.to_numpy().astype("datetime64[D]"), r.to_numpy(dtype=float))

    def step(self, on: date, others: Sequence[date]) -> Tuple[Optional[float], bool]:
        """``(r(D) / r(D-1), stable)`` with D-1 = the last common session before ``on`` and D the first on/after it;
        ``(None, True)`` when there is none on a side.  ``stable`` is False when r varies by more than STEP_STABLE
        within the 3 common sessions before D or the 3 from D on -- unless another event lies in those sessions (then
        only D-1 and D count)."""
        pos = int(np.searchsorted(self.days, np.datetime64(on, "D"), side="left"))
        if pos < 1 or pos >= len(self.days):
            return None, True
        if int((self.days[pos] - self.days[pos - 1]).astype(int)) > MAX_GAP_DAYS:
            return None, True
        lo, hi = max(0, pos - 3), min(len(self.days), pos + 3)
        waived = any(self.days[lo] <= np.datetime64(o, "D") <= self.days[hi - 1] for o in others)
        stable = waived or all(side.max() / side.min() - 1.0 <= STEP_STABLE
                               for side in (self.vals[lo:pos], self.vals[pos:hi]))
        return float(self.vals[pos] / self.vals[pos - 1]), stable


def _step_vote(step: Optional[float], stable: bool, f: Optional[float]) -> str:
    """none (no FMP data on both sides) | unstable | equals_f (|ln step - ln f| <= ln 1.02) | flat | other"""
    if step is None:
        return "none"
    if not stable:
        return "unstable"
    ls = math.log(step)
    if f is not None and abs(ls - math.log(f)) <= STEP_TOL:
        return "equals_f"
    if abs(ls) <= STEP_TOL:
        return "flat"
    return "other"


def _fmt_step(step: Optional[float], vote: str) -> str:
    return "none" if step is None else f"{step:.6g} {vote}"


def _same_ratio(a: float, b: float) -> bool:
    return abs(a / b - 1.0) <= RATIO_TOL


def _jump_idx(bars: _Bars, i: int) -> Optional[float]:
    """close(D-1) / open(D) for the session at index ``i``; None when the two bars do not observe an overnight
    (no bar before it, or the two are more than MAX_GAP_DAYS apart)."""
    if i <= 0 or i >= len(bars.days):
        return None
    if int((bars.days[i] - bars.days[i - 1]).astype(int)) > MAX_GAP_DAYS:
        return None
    return float(bars.c[i - 1] / bars.o[i])


def _first_session_on_or_after(bars: _Bars, on: date) -> int:
    return int(np.searchsorted(bars.days, np.datetime64(on, "D"), side="left"))


def _row(sym: str, d: date, a: Optional[float], f: Optional[float], jump: Optional[float], verdict: str,
         applied: bool, reason: str, step: str = "") -> Dict[str, Any]:
    return {"symbol": sym, "event_date": d.isoformat(), "alpaca_ratio": a, "fmp_ratio": f,
            "jump": None if jump is None else round(jump, 6), "fmp_step": step, "verdict": verdict,
            "applied": applied, "reason": reason}


def _cluster(listings: List[Tuple[str, ListedSplit, int]]) -> List[List[Tuple[str, ListedSplit, int]]]:
    """Group (source, listing, position) by position: members at most NEAR_SESSIONS apart (chained) are one event."""
    listings = sorted(listings, key=lambda m: (m[2], m[1].date))
    groups: List[List[Tuple[str, ListedSplit, int]]] = []
    for m in listings:
        if groups and m[2] - groups[-1][-1][2] <= NEAR_SESSIONS:
            groups[-1].append(m)
        else:
            groups.append([m])
    return groups


def _units(groups: List[List[Tuple[str, ListedSplit, int]]]) -> List[List[Tuple[str, ListedSplit, int]]]:
    """Events to decide.  A cluster where each vendor lists at most one ratio is ONE event (the vendors may disagree on
    its ratio: that is a mismatch to review).  A cluster where a vendor lists several ratios holds several distinct
    events (AMC 2023-08: a 1.13333 distribution and the 1:10 reverse split), so it splits into one event per ratio."""
    out: List[List[Tuple[str, ListedSplit, int]]] = []
    for grp in groups:
        per_vendor = [_distinct([m[1].ratio for m in grp if m[0] == v]) for v in ("alpaca", "fmp")]
        if all(len(r) <= 1 for r in per_vendor):
            out.append(grp)
            continue
        classes = _distinct([m[1].ratio for m in grp])
        for rep in classes:
            out.append([m for m in grp if _same_ratio(m[1].ratio, rep)])
    return out


def _distinct(ratios: List[float]) -> List[float]:
    out: List[float] = []
    for r in ratios:
        if not any(_same_ratio(r, o) for o in out):
            out.append(r)
    return out


class _EventContext:
    """What the decision of ONE event unit reads: the symbol's bars and FMP ratio series, the unit's listed dates, the first
    session on/after each, the raw jump there, and the other listed events (they waive the step's stability check)."""

    def __init__(self, bars: _Bars, rser: Optional[_RatioSeries], step_on: bool, dates: Sequence[date],
                 others: Sequence[date]):
        self.bars, self.rser, self.step_on, self.dates, self.others = bars, rser, step_on, list(dates), list(others)
        self.n = len(bars.days)
        self.idxs = {d: _first_session_on_or_after(bars, d) for d in self.dates}
        self.jumps = {d: _jump_idx(bars, i) for d, i in self.idxs.items()}

    def observable(self) -> bool:
        """some listed date has a bar before it within MAX_GAP_DAYS"""
        return any(j is not None for j in self.jumps.values())

    @property
    def jump_contra(self) -> bool:
        """every observable jump is within CONTRADICT_TOL of 1"""
        return all(j is None or abs(math.log(j)) <= CONTRADICT_TOL for j in self.jumps.values())

    def proving(self, ratio: float) -> Optional[date]:
        """the SESSION whose overnight jump proves ``ratio`` (big events only): a listed date first, then the sessions up
        to NEAR_SESSIONS either side of it (a vendor's ex-date can sit a day or two off the jump)"""
        if abs(math.log(ratio)) < BIG_MIN:
            return None
        for off in (0, 1, -1, 2, -2, 3, -3):
            for d in self.dates:
                i = self.idxs[d] + off
                j = _jump_idx(self.bars, i)
                if j is not None and abs(math.log(j) - math.log(ratio)) <= PROVEN_TOL:
                    return self.bars.days[i].astype(object)
        return None

    def shown(self, d: Optional[date] = None) -> float:
        if d is not None:                                      # a proving session: its own jump
            return _jump_idx(self.bars, _first_session_on_or_after(self.bars, d))
        return next(j for j in self.jumps.values() if j is not None)

    def step_str(self, ratio: Optional[float], d: date) -> str:
        if self.rser is None:
            return "none" if self.step_on else ""
        v, ok = self.rser.step(d, self.others)
        return _fmt_step(v, _step_vote(v, ok, ratio))

    def step_proving(self, ratio: float, guard: bool = False, both: bool = False) -> Optional[date]:
        """the session whose FMP step equals ``ratio`` (big events only), searched like ``proving``; with ``guard`` also the
        raw jump there must (i) be within STEP_JUMP_MAX of ``ratio`` and not within CONTRADICT_TOL of 1, or (ii) ``both``
        vendors list it and the jump is closer to ``ratio`` than to 1 in log terms"""
        if self.rser is None or abs(math.log(ratio)) < BIG_MIN:
            return None
        for off in (0, 1, -1, 2, -2, 3, -3):
            for d in self.dates:
                i = self.idxs[d] + off
                if 0 <= i < self.n:
                    day = self.bars.days[i].astype(object)
                    v, ok = self.rser.step(day, self.others)
                    if _step_vote(v, ok, ratio) == "equals_f":
                        j = _jump_idx(self.bars, i)
                        if guard and not (j is not None and (
                                (abs(math.log(j) - math.log(ratio)) <= STEP_JUMP_MAX and abs(math.log(j)) > CONTRADICT_TOL)
                                or (both and abs(math.log(j) - math.log(ratio)) < abs(math.log(j))))):
                            continue
                        return day
        return None

    def step_flat(self) -> bool:
        """the FMP step is flat at every listed date that has FMP data on both sides (and at one at least)"""
        if self.rser is None:
            return False
        votes = []
        for d in self.dates:
            v, ok = self.rser.step(self.bars.days[self.idxs[d]].astype(object), self.others)
            votes.append(_step_vote(v, ok, None))
        return "flat" in votes and all(x in ("flat", "none") for x in votes)


def decide_splits(raw_daily: Mapping[str, pd.DataFrame],
                  alpaca_events: Iterable[ListedSplit],
                  fmp_events: Iterable[ListedSplit],
                  overrides: Sequence[Mapping[str, Any]],
                  stats: Optional[Counter] = None,
                  fmp_daily: Optional[Mapping[str, pd.DataFrame]] = None
                  ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Judge every candidate event by the raw price jump across it, and by the FMP step when ``fmp_daily`` is given.

    ``raw_daily``: symbol -> frame with ``t`` (bar start), ``o``, ``c`` of RAW bars.  Events: current symbols.
    Returns ``(accepted, review_rows)``: accepted = ``[{symbol, date, factor, source, verdict}]`` sorted;
    review rows have REVIEW_COLUMNS.  Everything set aside or decided is counted in ``stats`` (verdict_<code>,
    outside_range, dup_listings_collapsed, events_symbol_not_in_store, overrides_unused, ...).

    ``fmp_daily``: symbol -> FMP daily frame (``Date``, ``Close`` ADJUSTED).  None = the FMP-step arbiter is off (every
    vote is "none"); a symbol missing from it is counted in ``fmp_daily_missing_symbol`` and votes "none"."""
    stats = stats if stats is not None else Counter()
    by_sym: Dict[str, List[Tuple[str, ListedSplit]]] = {}
    for src, evs in (("alpaca", alpaca_events), ("fmp", fmp_events)):
        for ev in evs:
            if not (math.isfinite(ev.ratio) and ev.ratio > 0):
                raise ValueError(f"unusable ratio in {src} listing {ev!r}")
            if ev.symbol not in raw_daily:
                stats[f"events_symbol_not_in_store_{src}"] += 1
                continue
            if abs(math.log(ev.ratio)) < 1e-9:
                stats[f"events_ratio_one_{src}"] += 1
                continue
            by_sym.setdefault(ev.symbol, []).append((src, ev))
    ov_by_sym: Dict[str, List[Mapping[str, Any]]] = {}
    for o in overrides:
        sym = _norm_symbol(o["symbol"])
        if sym not in raw_daily:
            stats["overrides_unused"] += 1
            continue
        ov_by_sym.setdefault(sym, []).append(o)

    accepted: List[Dict[str, Any]] = []
    rows: List[Dict[str, Any]] = []
    for sym in sorted(raw_daily):
        bars = _bars(raw_daily[sym], stats, sym)
        rser = None
        if fmp_daily is not None:
            if sym in fmp_daily:
                rser = _RatioSeries.build(bars, fmp_daily[sym])
            if rser is None:
                stats["fmp_daily_missing_symbol"] += 1
        a, r = _decide_symbol(sym, bars, by_sym.get(sym, []), ov_by_sym.get(sym, []), stats, rser,
                           fmp_daily is not None)
        accepted += a
        rows += r
    accepted.sort(key=lambda x: (x["symbol"], x["date"]))
    rows.sort(key=lambda x: (x["symbol"], x["event_date"], x["verdict"]))
    return accepted, rows


def _decide_symbol(sym: str, bars: _Bars, listed: List[Tuple[str, ListedSplit]], overrides: List[Mapping[str, Any]],
                   stats: Counter, rser: Optional[_RatioSeries] = None, step_on: bool = False) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    acc: List[Dict[str, Any]] = []
    rows: List[Dict[str, Any]] = []
    n = len(bars.days)

    def accept(d: date, f: float, source: str, verdict: str, step: str = "") -> None:
        stats[f"verdict_{verdict}"] += 1
        j = _jump_idx(bars, _first_session_on_or_after(bars, d)) if n else None
        acc.append({"symbol": sym, "date": d.isoformat(), "factor": f, "source": source, "verdict": verdict,
                    "jump": None if j is None else round(j, 6), "fmp_step": step})

    def review(d: date, a: Optional[float], fm: Optional[float], jump: Optional[float], verdict: str,
               applied: bool, reason: str, count: bool = True, step: str = "") -> None:
        if count:
            stats[f"verdict_{verdict}"] += 1
        rows.append(_row(sym, d, a, fm, jump, verdict, applied, reason, step))

    # every listed date of the symbol: an event of ANOTHER unit near a step's sessions waives the stability check
    listed_dates = sorted({ev.date for _, ev in listed})
    ov_dates = [date.fromisoformat(o["event_date"]) for o in overrides]

    # position of each listing; a listing the bars cannot be affected by is counted, not judged
    placed: List[Tuple[str, ListedSplit, int]] = []
    for src, ev in listed:
        if n == 0:
            placed.append((src, ev, ev.date.toordinal()))
            continue
        first, last = bars.days[0].astype(object), bars.days[-1].astype(object)
        if ev.date <= first or ev.date > last:
            stats[f"outside_range_{src}"] += 1
            continue
        placed.append((src, ev, _first_session_on_or_after(bars, ev.date)))

    ov_used = set()
    ov_positions: List[int] = []
    event_positions: List[int] = []
    for grp in _units(_cluster(placed)):
        event_positions += [m[2] for m in grp]
        dates = sorted({m[1].date for m in grp})
        # ---- override: decides the event outright
        matches = [o for o in overrides if date.fromisoformat(o["event_date"]) in dates]
        if len(matches) > 1 or (matches and id(matches[0]) in ov_used):
            raise SplitTableBuildError(f"{sym}: override(s) {[o['event_date'] for o in overrides if o in matches]} match more "
                                       f"than one event / listing group around {dates[0]}: ambiguous, split the entries")
        if matches:
            ov = matches[0]
            ov_used.add(id(ov))
            stats["listings_absorbed_by_override"] += len(grp)
            if ov["decision"] == "accept":
                accept(date.fromisoformat(ov["event_date"]), float(ov["ratio"]), "override", "override_accept")
            else:
                stats["verdict_override_reject"] += 1
            continue
        a_all = _distinct([m[1].ratio for m in grp if m[0] == "alpaca"])
        f_all = _distinct([m[1].ratio for m in grp if m[0] == "fmp"])
        stats["dup_listings_collapsed"] += len(grp) - len(a_all) - len(f_all)
        d0 = dates[0]
        a_r = a_all[0] if a_all else None
        f_r = f_all[0] if f_all else None
        if n == 0:
            review(d0, a_r, f_r, None, "no_price_data", False, "the symbol has no usable bars")
            continue
        ctx = _EventContext(bars, rser, step_on, dates, [d for d in listed_dates if d not in dates] + ov_dates)
        if not ctx.observable():
            review(d0, a_r, f_r, None, "no_price_data", False,
                   f"no bars on both sides within {MAX_GAP_DAYS} days of the event")
            continue

        if a_r is not None and f_r is not None and not _same_ratio(a_r, f_r):
            pa, pf = ctx.proving(a_r), ctx.proving(f_r)
            if (pa is None) != (pf is None):
                d, r, src = (pa, a_r, "alpaca") if pa is not None else (pf, f_r, "fmp")
                accept(d, r, src, "ratio_mismatch_resolved", ctx.step_str(r, d))
                review(d, a_r, f_r, ctx.shown(d), "ratio_mismatch_resolved", True,
                       f"vendors disagree ({src} ratio {r:.6g} is the one the jump proves)", count=False,
                       step=ctx.step_str(r, d))
                continue
            sa, sf = ctx.step_proving(a_r), ctx.step_proving(f_r)
            if (sa is None) != (sf is None) and not ctx.jump_contra:
                d, r, src = (sa, a_r, "alpaca") if sa is not None else (sf, f_r, "fmp")
                accept(d, r, src, "ratio_mismatch_resolved_by_fmp_step", ctx.step_str(r, d))
                review(d, a_r, f_r, ctx.shown(d), "ratio_mismatch_resolved_by_fmp_step", True,
                       f"vendors disagree ({src} ratio {r:.6g} is the one FMP's step equals; the jump does not contradict it)",
                       count=False, step=ctx.step_str(r, d))
            else:
                review(d0, a_r, f_r, ctx.shown(), "ratio_mismatch", False,
                       "vendors list different ratios and the jump proves " + ("both" if pa is not None else "neither"),
                       step=ctx.step_str(a_r, d0))
            continue

        ratio = a_r if a_r is not None else f_r
        src = "alpaca" if a_r is not None else "fmp"
        both = a_r is not None and f_r is not None
        if abs(math.log(ratio)) < BIG_MIN:
            if both:
                accept(d0, ratio, "both", "small_both", ctx.step_str(ratio, d0))
            else:
                review(d0, a_r, f_r, ctx.shown(), "small_single_vendor", False,
                       f"small ratio {ratio:.6g}: the price cannot prove it and only {src} lists it",
                       step=ctx.step_str(ratio, d0))
            continue
        pd_ = ctx.proving(ratio)
        if pd_ is not None:
            if both or src == "alpaca":
                accept(pd_, ratio, "both" if both else "alpaca", "proven", ctx.step_str(ratio, pd_))
            else:
                accept(pd_, ratio, "fmp", "fmp_only_proven", ctx.step_str(ratio, pd_))
                review(pd_, a_r, f_r, ctx.shown(pd_), "fmp_only_proven", True,
                       "Alpaca missed this split; the jump proves it", count=False, step=ctx.step_str(ratio, pd_))
            continue
        ps = ctx.step_proving(ratio, guard=True, both=both)
        if ps is None:
            pe = ctx.step_proving(ratio)
            if pe is not None:                       # FMP applied it, but the raw bars barely show it: not applied
                review(pe, a_r, f_r, ctx.shown(pe), "fmp_step_without_jump", False,
                       f"FMP's step equals the listed ratio {ratio:.6g} but the raw jump {ctx.shown(pe):.4g} does not support it "
                       f"(not within x{math.exp(STEP_JUMP_MAX):.2g} of it, nor, for a both-vendor listing, closer to it than to 1): "
                       f"a separation or spin-off adjustment, not a split",
                       step=ctx.step_str(ratio, pe))
                continue
        if ps is not None:
            accept(ps, ratio, "both" if both else src, "proven_by_fmp_step", ctx.step_str(ratio, ps))
            if not both and src == "fmp":
                review(ps, a_r, f_r, ctx.shown(ps), "proven_by_fmp_step", True,
                       "Alpaca missed this split; FMP's step equals it (the jump alone does not prove it)",
                       count=False, step=ctx.step_str(ratio, ps))
            continue
        if ctx.jump_contra:
            if ctx.step_flat():
                stats["verdict_rejected_confirmed"] += 1       # price flat in the raw bars AND in FMP's: no split
                continue
            v = "both_contradicted" if both else f"{src}_contradicted"
            review(d0, a_r, f_r, ctx.shown(), v, False,
                   f"listed ratio {ratio:.6g} but the raw price does not jump (jump {ctx.shown():.4g})",
                   step=ctx.step_str(ratio, d0))
        else:
            review(d0, a_r, f_r, ctx.shown(), "unresolved", False,
                   f"listed ratio {ratio:.6g}, jump {ctx.shown():.4g}: neither proves nor contradicts it",
                   step=ctx.step_str(ratio, d0))

    # ---- overrides no listing led to: an 'accept' ADDS the split, a 'reject' silences the jump row at its date
    for o in overrides:
        if id(o) in ov_used:
            continue
        d = date.fromisoformat(o["event_date"])
        if n:
            ov_positions.append(_first_session_on_or_after(bars, d))
        if o["decision"] == "accept":
            accept(d, float(o["ratio"]), "override", "override_accept")
        else:
            stats["verdict_override_reject"] += 1
    positions = event_positions + ov_positions

    # ---- two big accepts in the same direction within NEAR_SESSIONS sessions would adjust one split twice (the
    # vendors list slightly different ratios for one event and each is "proven"): retract both, for a human
    if n:
        big = [a for a in acc if a["source"] != "override" and abs(math.log(a["factor"])) >= BIG_MIN]
        big_idx = {id(a): _first_session_on_or_after(bars, date.fromisoformat(a["date"])) for a in big}
        retract = set()
        for i, x in enumerate(big):
            for y in big[i + 1:]:
                if (abs(big_idx[id(x)] - big_idx[id(y)]) <= NEAR_SESSIONS
                        and (x["factor"] > 1) == (y["factor"] > 1)):
                    retract.update((id(x), id(y)))
        for a in [a for a in big if id(a) in retract]:
            acc.remove(a)
            stats[f"verdict_{a['verdict']}"] -= 1
            rows[:] = [r for r in rows if not (r["applied"] and r["event_date"] == a["date"]
                                               and r["verdict"] == a["verdict"])]       # its "applied" row
            review(date.fromisoformat(a["date"]), a["factor"], None, None, "overlapping_accepts", False,
                   f"accepted ratio {a['factor']:.6g} lies within {NEAR_SESSIONS} sessions of another accepted big "
                   f"split in the same direction: one split would be applied twice")

    # ---- unexplained raw overnight jumps with no event within NEAR_SESSIONS sessions
    if n > 1:
        ratio_ = bars.c[:-1] / bars.o[1:]
        observed = np.diff(bars.days).astype(int) <= MAX_GAP_DAYS      # a pair further apart than that observes no overnight
        for k in np.flatnonzero(((ratio_ > JUMP_HI) | (ratio_ < JUMP_LO)) & observed):
            t = int(k) + 1
            if any(abs(t - p) <= NEAR_SESSIONS for p in positions):
                continue
            day = bars.days[t].astype(object)
            step = "none" if step_on else ""
            if rser is not None:
                v, ok = rser.step(day, listed_dates + ov_dates)
                vote = _step_vote(v, ok, None)
                if vote == "flat":                            # FMP's adjusted/raw ratio did not move: a real price move
                    stats["verdict_jump_explained_by_market"] += 1
                    continue
                step = _fmt_step(v, vote)
            review(day, None, None, float(ratio_[k]), "unexplained_jump", False,
                   f"overnight close/open {ratio_[k]:.4g} with no listed split within {NEAR_SESSIONS} sessions",
                   step=step)
    return acc, rows


def splits_by_symbol(accepted: Sequence[Mapping[str, Any]]) -> Dict[str, List[List[Any]]]:
    """``{symbol: [[date, factor], ...]}`` events sorted by date (the split_table.json layout)."""
    out: Dict[str, List[List[Any]]] = {}
    for a in sorted(accepted, key=lambda x: (x["symbol"], x["date"])):
        rows = out.setdefault(a["symbol"], [])
        if rows and rows[-1][0] == a["date"]:
            raise SplitTableBuildError(f"{a['symbol']}: two accepted events on {a['date']} ({rows[-1][1]} and {a['factor']}): "
                                       f"decide them with an override")
        rows.append([a["date"], a["factor"]])
    return out


# ---------------------------------------------------------------------------
# comparison with FMP's split list
# ---------------------------------------------------------------------------
COMPARE_COLUMNS = ["symbol", "date", "our_factor", "fmp_ratio", "alpaca_ratio", "jump", "fmp_step", "verdict", "class"]
COMPARE_DAYS = 5          # an FMP listing and one of our events this many calendar days apart are the same event


def near(d1: date, d2: date) -> bool:
    return abs((d1 - d2).days) <= COMPARE_DAYS


def compare_with_fmp(raw_daily: Mapping[str, pd.DataFrame], accepted: Sequence[Mapping[str, Any]],
                     review_rows: Sequence[Mapping[str, Any]], alpaca_events: Iterable[ListedSplit],
                     fmp_events: Iterable[ListedSplit],
                     fmp_daily: Optional[Mapping[str, pd.DataFrame]] = None) -> List[Dict[str, Any]]:
    """One row per (symbol, event) across our table and FMP's list (symbols of ``raw_daily`` only).

    class: ``agree`` (ours and FMP, ratios within 1 %), ``ratio_differs``, ``ours_only`` (applied, FMP does not list
    it), ``fmp_only_accepted`` (ours and FMP, and Alpaca does not list it: Alpaca missed it), ``fmp_only_rejected``
    (FMP lists it, we did not apply it; ``verdict`` = our review verdict, ``outside_range`` for an event no bar
    can show, ``rejected_confirmed`` when no row exists and FMP's step is flat there, else ``no_row``).  ``jump`` =
    close(D-1)/open(D) at the event, when observable; ``fmp_step`` = FMP step value and vote (see _RatioSeries)."""
    stats: Counter = Counter()
    by_sym: Dict[str, Dict[str, list]] = {}
    for key, evs in (("fmp", fmp_events), ("alp", alpaca_events)):
        for e in evs:
            if e.symbol in raw_daily:
                dup = any(near(x.date, e.date) and _same_ratio(x.ratio, e.ratio)
                          for x in by_sym.get(e.symbol, {"fmp": [], "alp": []})[key])
                if dup:                                  # a vendor's duplicate listing is one event
                    continue
                by_sym.setdefault(e.symbol, {"fmp": [], "alp": [], "ours": [], "rev": []})[key].append(e)
    for a in accepted:
        by_sym.setdefault(a["symbol"], {"fmp": [], "alp": [], "ours": [], "rev": []})["ours"].append(a)
    for r in review_rows:
        by_sym.setdefault(r["symbol"], {"fmp": [], "alp": [], "ours": [], "rev": []})["rev"].append(r)

    out: List[Dict[str, Any]] = []
    for sym, g in sorted(by_sym.items()):
        bars = _bars(raw_daily[sym], stats, sym)
        rser = (_RatioSeries.build(bars, fmp_daily[sym]) if fmp_daily is not None and sym in fmp_daily else None)
        all_dates = ([e.date for e in g["fmp"]] + [e.date for e in g["alp"]]
                     + [date.fromisoformat(o["date"]) for o in g["ours"]])

        def step(d: date, f: Optional[float]) -> Tuple[str, str]:
            if rser is None:
                return "none", "none"
            v, ok = rser.step(d, [x for x in all_dates if x != d])
            vote = _step_vote(v, ok, f)
            return _fmt_step(v, vote), vote

        def jump(d: date) -> Optional[float]:
            if not len(bars.days) or d <= bars.days[0].astype(object) or d > bars.days[-1].astype(object):
                return None
            j = _jump_idx(bars, _first_session_on_or_after(bars, d))
            return None if j is None else round(j, 6)

        def alp_near(d: date) -> Optional[float]:
            return next((e.ratio for e in g["alp"] if near(e.date, d)), None)

        free_fmp = list(g["fmp"])
        for o in g["ours"]:
            od = date.fromisoformat(o["date"])
            m = next((e for e in free_fmp if near(e.date, od)), None)
            if m is not None:
                free_fmp.remove(m)
            a_r = alp_near(od)
            if m is None:
                cls = "ours_only"
            elif not _same_ratio(o["factor"], m.ratio):
                cls = "ratio_differs"
            elif a_r is None:
                cls = "fmp_only_accepted"
            else:
                cls = "agree"
            out.append({"symbol": sym, "date": o["date"], "our_factor": o["factor"],
                        "fmp_ratio": None if m is None else m.ratio, "alpaca_ratio": a_r, "jump": jump(od),
                        "fmp_step": step(od, o["factor"])[0], "verdict": o["verdict"], "class": cls})
        for m in free_fmp:
            row = next((r for r in g["rev"] if near(date.fromisoformat(r["event_date"]), m.date)), None)
            if row is not None:
                verdict = row["verdict"]
            elif len(bars.days) and (m.date <= bars.days[0].astype(object) or m.date > bars.days[-1].astype(object)):
                verdict = "outside_range"
            else:
                verdict = "rejected_confirmed" if step(m.date, m.ratio)[1] == "flat" else "no_row"
            out.append({"symbol": sym, "date": m.date.isoformat(), "our_factor": None, "fmp_ratio": m.ratio,
                        "alpaca_ratio": alp_near(m.date), "jump": jump(m.date),
                        "fmp_step": step(m.date, m.ratio)[0], "verdict": verdict,
                        "class": "fmp_only_rejected"})
    out.sort(key=lambda r: (r["symbol"], r["date"]))
    return out

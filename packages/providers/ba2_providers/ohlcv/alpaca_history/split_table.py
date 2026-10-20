"""Our own split table over the RAW Alpaca daily store: every candidate event is judged by the raw price jump.

Neither vendor's split list is trusted (Alpaca lists spurious events -- HON, NVS, STRR, SNEX, BIP, DRS, IR -- and
misses real ones -- KOD, UHAL, SNX, IAC, RTX; FMP has mixed-basis series).  The bars are RAW, so a real split shows
as an overnight jump ``close(D-1) / open(D)`` about equal to the factor ``f`` (new shares / old shares; a reverse
split has f < 1).  ``decide_splits`` accepts an event the price proves, rejects (with a review row) one the price
contradicts, and sends everything it cannot settle to a review row that is NOT applied.  Nothing is dropped
quietly: the counts of what was set aside (outside the bar range, not in the store, ...) go into ``stats``.

The rules (``PROVEN_TOL`` etc. below):
  * big event (``|ln f| >= ln 1.25``): PROVEN when ``|ln(jump) - ln(f)| <= ln 1.15`` -> accepted, whatever the source
    (FMP-only and proven gets a review row, "Alpaca missed").  CONTRADICTED when the jump is within ``ln 1.10`` of 1
    -> rejected + review row.  In between -> review row, not applied.
  * small event (stock dividends, small unit splits): price cannot prove it.  Accepted when BOTH vendors list it
    (ratios within 1 %); otherwise review row, not applied.
  * both vendors list one event with different ratios: the ratio the jump proves wins (review row); if the jump
    proves neither or both, review row, not applied.
  * a cluster where a vendor lists several ratios is several events, one per ratio, each decided on its own;
    no usable bars on the two sides of it: review row, not applied.
  * duplicate listings (same event within ``NEAR_SESSIONS`` sessions, ratio within 1 %) are one event.
  * an unexplained overnight jump (> x1.8 or < x0.55) with no listed event within 3 sessions: review row.
  * an override (``split_overrides.json``) decides its event outright.
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

REVIEW_COLUMNS = ["symbol", "event_date", "alpaca_ratio", "fmp_ratio", "jump", "verdict", "applied", "reason"]


class ListedSplit(NamedTuple):
    symbol: str
    date: date
    ratio: float            # new shares / old shares: 4-for-1 -> 4.0, 1-for-10 -> 0.1


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
        if not isinstance(num, (int, float)) or not isinstance(den, (int, float)) or not den or num <= 0:
            stats["fmp_rows_without_ratio"] += 1
            continue
        day = date.fromisoformat(r["date"][:10])
        stats["fmp_rows"] += 1
        out.append(ListedSplit(map_to_current(r["symbol"], day, renames), day, float(num) / float(den)))
    return out


def read_corporate_actions(store: str) -> List[Dict[str, Any]]:
    """The monthly corporate-action files of a store, oldest first."""
    d = os.path.join(store, "corporate_actions")
    out = []
    for name in sorted(os.listdir(d)):
        if name.startswith("ym=") and name.endswith(".json.gz"):
            with gzip.open(os.path.join(d, name), "rt", encoding="utf-8") as f:
                out.append(json.load(f))
    if not out:
        raise FileNotFoundError(f"no corporate_actions/ym=*.json.gz under {store}")
    return out


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------
class _Bars(NamedTuple):
    days: np.ndarray        # datetime64[D], ascending, one per session with a usable bar
    o: np.ndarray
    c: np.ndarray


def _bars(df: pd.DataFrame, stats: Counter) -> _Bars:
    t = pd.to_datetime(df["t"])
    if t.dt.tz is not None:
        t = t.dt.tz_convert("America/New_York").dt.tz_localize(None)
    days = t.dt.normalize().to_numpy().astype("datetime64[D]")
    o = df["o"].to_numpy(dtype=float)
    c = df["c"].to_numpy(dtype=float)
    ok = np.isfinite(o) & np.isfinite(c) & (o > 0) & (c > 0)
    if not ok.all():
        stats["bars_unusable_skipped"] += int((~ok).sum())
    order = np.argsort(days[ok], kind="stable")
    return _Bars(days[ok][order], o[ok][order], c[ok][order])


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
         applied: bool, reason: str) -> Dict[str, Any]:
    return {"symbol": sym, "event_date": d.isoformat(), "alpaca_ratio": a, "fmp_ratio": f,
            "jump": None if jump is None else round(jump, 6), "verdict": verdict, "applied": applied, "reason": reason}


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


def decide_splits(raw_daily: Mapping[str, pd.DataFrame],
                  alpaca_events: Iterable[ListedSplit],
                  fmp_events: Iterable[ListedSplit],
                  overrides: Sequence[Mapping[str, Any]],
                  stats: Optional[Counter] = None) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Judge every candidate event by the raw price jump across it.

    ``raw_daily``: symbol -> frame with ``t`` (bar start), ``o``, ``c`` of RAW bars.  Events: current symbols.
    Returns ``(accepted, review_rows)``: accepted = ``[{symbol, date, factor, source, verdict}]`` sorted;
    review rows have REVIEW_COLUMNS.  Everything set aside or decided is counted in ``stats`` (verdict_<code>,
    outside_range, dup_listings_collapsed, events_symbol_not_in_store, overrides_unused, ...)."""
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
        a, r = _decide_symbol(sym, _bars(raw_daily[sym], stats), by_sym.get(sym, []), ov_by_sym.get(sym, []), stats)
        accepted += a
        rows += r
    accepted.sort(key=lambda x: (x["symbol"], x["date"]))
    rows.sort(key=lambda x: (x["symbol"], x["event_date"], x["verdict"]))
    return accepted, rows


def _decide_symbol(sym: str, bars: _Bars, listed: List[Tuple[str, ListedSplit]], overrides: List[Mapping[str, Any]],
                   stats: Counter) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    acc: List[Dict[str, Any]] = []
    rows: List[Dict[str, Any]] = []
    n = len(bars.days)

    def accept(d: date, f: float, source: str, verdict: str) -> None:
        stats[f"verdict_{verdict}"] += 1
        acc.append({"symbol": sym, "date": d.isoformat(), "factor": f, "source": source, "verdict": verdict})

    def review(d: date, a: Optional[float], fm: Optional[float], jump: Optional[float], verdict: str,
               applied: bool, reason: str, count: bool = True) -> None:
        if count:
            stats[f"verdict_{verdict}"] += 1
        rows.append(_row(sym, d, a, fm, jump, verdict, applied, reason))

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
        ov = next((o for o in overrides if date.fromisoformat(o["event_date"]) in dates), None)
        if ov is not None:
            ov_used.add(id(ov))
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
        idxs = {d: _first_session_on_or_after(bars, d) for d in dates}
        jumps = {d: _jump_idx(bars, i) for d, i in idxs.items()}
        if all(j is None for j in jumps.values()):
            review(d0, a_r, f_r, None, "no_price_data", False,
                   f"no bars on both sides within {MAX_GAP_DAYS} days of the event")
            continue

        def proving(ratio: float) -> Optional[date]:
            """the SESSION whose overnight jump proves ``ratio`` (big events only): a listed date first, then the
            sessions up to NEAR_SESSIONS either side of it (a vendor's ex-date can sit a day or two off the jump)"""
            if abs(math.log(ratio)) < BIG_MIN:
                return None
            for off in (0, 1, -1, 2, -2, 3, -3):
                for d in dates:
                    i = idxs[d] + off
                    j = _jump_idx(bars, i)
                    if j is not None and abs(math.log(j) - math.log(ratio)) <= PROVEN_TOL:
                        return bars.days[i].astype(object)
            return None

        def shown(d: Optional[date] = None) -> float:
            if d is not None:                                  # a proving session: its own jump
                return _jump_idx(bars, _first_session_on_or_after(bars, d))
            return next(j for j in jumps.values() if j is not None)

        if a_r is not None and f_r is not None and not _same_ratio(a_r, f_r):
            pa, pf = proving(a_r), proving(f_r)
            if (pa is None) != (pf is None):
                d, r, src = (pa, a_r, "alpaca") if pa is not None else (pf, f_r, "fmp")
                accept(d, r, src, "ratio_mismatch_resolved")
                review(d, a_r, f_r, shown(d), "ratio_mismatch_resolved", True,
                       f"vendors disagree ({src} ratio {r:.6g} is the one the jump proves)", count=False)
            else:
                review(d0, a_r, f_r, shown(), "ratio_mismatch", False,
                       "vendors list different ratios and the jump proves " + ("both" if pa is not None else "neither"))
            continue

        ratio = a_r if a_r is not None else f_r
        src = "alpaca" if a_r is not None else "fmp"
        both = a_r is not None and f_r is not None
        if abs(math.log(ratio)) < BIG_MIN:
            if both:
                accept(d0, ratio, "both", "small_both")
            else:
                review(d0, a_r, f_r, shown(), "small_single_vendor", False,
                       f"small ratio {ratio:.6g}: the price cannot prove it and only {src} lists it")
            continue
        pd_ = proving(ratio)
        if pd_ is not None:
            if both or src == "alpaca":
                accept(pd_, ratio, "both" if both else "alpaca", "proven")
            else:
                accept(pd_, ratio, "fmp", "fmp_only_proven")
                review(pd_, a_r, f_r, shown(pd_), "fmp_only_proven", True,
                       "Alpaca missed this split; the jump proves it", count=False)
            continue
        if all(j is None or abs(math.log(j)) <= CONTRADICT_TOL for j in jumps.values()):
            v = "both_contradicted" if both else f"{src}_contradicted"
            review(d0, a_r, f_r, shown(), v, False,
                   f"listed ratio {ratio:.6g} but the raw price does not jump (jump {shown():.4g})")
        else:
            review(d0, a_r, f_r, shown(), "unresolved", False,
                   f"listed ratio {ratio:.6g}, jump {shown():.4g}: neither proves nor contradicts it")

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
        for k in np.flatnonzero((ratio_ > JUMP_HI) | (ratio_ < JUMP_LO)):
            t = int(k) + 1
            if any(abs(t - p) <= NEAR_SESSIONS for p in positions):
                continue
            review(bars.days[t].astype(object), None, None, float(ratio_[k]), "unexplained_jump", False,
                   f"overnight close/open {ratio_[k]:.4g} with no listed split within {NEAR_SESSIONS} sessions")
    return acc, rows


def splits_by_symbol(accepted: Sequence[Mapping[str, Any]]) -> Dict[str, List[List[Any]]]:
    """``{symbol: [[date, factor], ...]}`` events sorted by date (the split_table.json layout)."""
    out: Dict[str, List[List[Any]]] = {}
    for a in sorted(accepted, key=lambda x: (x["symbol"], x["date"])):
        out.setdefault(a["symbol"], []).append([a["date"], a["factor"]])
    return out


# ---------------------------------------------------------------------------
# comparison with FMP's split list
# ---------------------------------------------------------------------------
COMPARE_COLUMNS = ["symbol", "date", "our_factor", "fmp_ratio", "alpaca_ratio", "jump", "verdict", "class"]
COMPARE_DAYS = 5          # an FMP listing and one of our events this many calendar days apart are the same event


def near(d1: date, d2: date) -> bool:
    return abs((d1 - d2).days) <= COMPARE_DAYS


def compare_with_fmp(raw_daily: Mapping[str, pd.DataFrame], accepted: Sequence[Mapping[str, Any]],
                     review_rows: Sequence[Mapping[str, Any]], alpaca_events: Iterable[ListedSplit],
                     fmp_events: Iterable[ListedSplit]) -> List[Dict[str, Any]]:
    """One row per (symbol, event) across our table and FMP's list (symbols of ``raw_daily`` only).

    class: ``agree`` (ours and FMP, ratios within 1 %), ``ratio_differs``, ``ours_only`` (applied, FMP does not list
    it), ``fmp_only_accepted`` (ours and FMP, and Alpaca does not list it: Alpaca missed it), ``fmp_only_rejected``
    (FMP lists it, we did not apply it; ``verdict`` = our review verdict, ``outside_range`` for an event no bar
    can show, ``no_row`` if nothing else explains it).  ``jump`` = close(D-1)/open(D) at the event, when observable."""
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
        bars = _bars(raw_daily[sym], stats)

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
                        "verdict": o["verdict"], "class": cls})
        for m in free_fmp:
            row = next((r for r in g["rev"] if near(date.fromisoformat(r["event_date"]), m.date)), None)
            if row is not None:
                verdict = row["verdict"]
            elif len(bars.days) and (m.date <= bars.days[0].astype(object) or m.date > bars.days[-1].astype(object)):
                verdict = "outside_range"
            else:
                verdict = "no_row"
            out.append({"symbol": sym, "date": m.date.isoformat(), "our_factor": None, "fmp_ratio": m.ratio,
                        "alpaca_ratio": alp_near(m.date), "jump": jump(m.date), "verdict": verdict,
                        "class": "fmp_only_rejected"})
    out.sort(key=lambda r: (r["symbol"], r["date"]))
    return out

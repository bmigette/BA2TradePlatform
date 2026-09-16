#!/usr/bin/env python
"""Per-job market-condition report: what the gates chose, what they refused, and what the
trades they let through actually earned.

Design: ``docs/plans/2026-09-15-option-market-condition-genes-design.md`` section 7
("Add a compact result summary per job"), plan Task 9.

    python tools/report_market_conditions.py --opt 1743
    python tools/report_market_conditions.py --like %ohlcv% --top 3 --out report.md
    python tools/report_market_conditions.py --opt 1743 --coverage   # feature-off diagnostics

WHAT IT PRINTS, per optimization row:

1. the VERSIONS, read from the ``market_condition`` block the launcher persisted into
   ``optimization_config["backtest"]`` -- profiles, manifest digest, source profile, timing
   policy, calendar version, calculator versions, the FieldSpec list and the gene count. Never
   re-derived from this process's registry: a report that silently re-computed the versions
   would agree with itself on a machine whose registry had moved on, which is precisely when
   the numbers stop meaning what they say.
2. the WINNING MODES AND THRESHOLDS per structure, from the top-N distinct-fitness genomes.
3. the per-run COUNTERS (eligible recommendations, market-gate evaluated / rejected, unknown
   input BY REASON, entries staged) from each persisted ``Backtest.results["market_condition"]``.
   Design section 6: eligible is reported SEPARATELY from condition rejections, and an unknown
   measurement is never folded into "the gate said no".
4. SUBMITTED vs FILLED structures.
5. per-year PROFIT / RETURN / DRAWDOWN, from ``results.yearly_breakdown`` -- the account
   engine's own boundary rules, not a second implementation of them.
6. the ATTRIBUTION of executed trades' net P&L, and of top-1/top-5 concentration, to the
   ENTRY-STATE measurement bins recorded on each trade, with explicit bin edges and the issuer
   and date counts per bin.

WHAT IT REFUSES TO DO. It never annualises a filtered subset of trades. A bin holds the
overlapping trades that happened to be entered in one measured regime; it is not a funded
account, it has no capital of its own, and a CAR computed for it would be a number with no
referent. The per-year block is the whole account and says so; the bin block reports dollars,
counts and shares, and says that too.

THE FEATURE-OFF COVERAGE REPORT (``--coverage``) is deliberately a separate pass over the
published snapshot: for the run's universe and window it counts, per symbol, how many sessions
would have been UNKNOWN and why. It reads the manifest and nothing else -- no provider, no
decision path, no re-run -- so it can be pointed at a job whose winners had every gate off
without changing a single trial.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter, OrderedDict
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BOOTSTRAPPED = False


def _bootstrap() -> None:
    """Put ``testplatform/backend`` on the path (for ``yearly_breakdown`` and the registry).

    Mirrors ``tools/backtest_parity.py._bootstrap``: idempotent, a no-op when the backend is
    already importable (so pytest importing this module does not disable the session's logging),
    and it silences INFO first because the backend import chain is chatty.
    """
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    _BOOTSTRAPPED = True
    import importlib.util

    try:
        if importlib.util.find_spec("app.services.backtest.results") is not None:
            return
    except (ImportError, ValueError):
        pass
    import logging

    logging.disable(logging.INFO)
    for path in (os.path.join(REPO, "testplatform", "backend"),
                 os.path.join(REPO, "testplatform"),
                 os.path.join(REPO, "packages", "common")):
        if path not in sys.path:
            sys.path.insert(0, path)


def default_db() -> str:
    """The test DB this repo's tools read (``BA2_HOME/test/dl_forecasting.db``)."""
    home = os.environ.get("BA2_HOME") or os.path.join(os.path.expanduser("~"), "Documents", "ba2")
    return os.path.join(home, "test", "dl_forecasting.db")


# --------------------------------------------------------------------------- bins
#: EXPLICIT bin edges per field (design section 7: "explicit bin boundaries"). A bin scheme
#: derived from the data -- quantiles, say -- would move between jobs, and two reports of the
#: same strategy would not be comparable, which is the one thing an attribution table is for.
#: Edges are LOWER-inclusive, upper-exclusive, except the final bin which includes its top.
BIN_EDGES: Dict[str, Tuple[float, ...]] = {
    "underlying_trend_slope_50_atr14": (
        float("-inf"), -0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3, float("inf")),
    "underlying_adx_14": (0.0, 15.0, 25.0, 40.0, 100.0),
    "underlying_realized_vol_ratio_5_20": (0.0, 0.75, 1.0, 1.5, 2.0, float("inf")),
}

#: How many equal-width bins a field with no explicit scheme gets, from its FieldSpec range.
_FALLBACK_BINS = 8


def _fmt_edge(x: float) -> str:
    if x == float("-inf"):
        return "-inf"
    if x == float("inf"):
        return "+inf"
    return f"{x:g}"


def bin_labels(edges: Sequence[float]) -> List[str]:
    out = [f"[{_fmt_edge(edges[i])}, {_fmt_edge(edges[i + 1])})" for i in range(len(edges) - 2)]
    out.append(f"[{_fmt_edge(edges[-2])}, {_fmt_edge(edges[-1])}]")
    return out


def field_bins(field: str, spec: Optional[Dict[str, Any]] = None) -> Tuple[Tuple[float, ...], List[str]]:
    """``(edges, labels)`` for a field: the explicit scheme, else one derived from its
    FieldSpec range, else a single "unbinned" bucket. ``spec`` is a ``FieldSpec.to_dict()``
    from the persisted block -- the report never asks THIS process's registry for a range a
    run may have been searched under a different version of."""
    edges = BIN_EDGES.get(field)
    if edges is not None:
        return edges, bin_labels(edges)
    lo = (spec or {}).get("value_min")
    hi = (spec or {}).get("value_max")
    if lo is None or hi is None or not (float(hi) > float(lo)):
        return (), ["all values"]
    lo, hi = float(lo), float(hi)
    step = (hi - lo) / _FALLBACK_BINS
    derived = tuple([float("-inf")] + [lo + step * i for i in range(1, _FALLBACK_BINS)] + [float("inf")])
    return derived, bin_labels(derived)


def bin_of(value: float, edges: Sequence[float]) -> Optional[int]:
    """The index of ``value``'s bin, or None when it falls outside the declared edges.

    OUTSIDE IS NOT A BIN. A value below the first edge or above the last is reported as
    ``outside`` in its own row rather than folded into the nearest bucket -- an ADX of 140 is a
    data problem, and hiding it inside "[40, 100]" is how a data problem becomes a finding.
    """
    if not edges:
        return 0
    # NaN FIRST. Every comparison with NaN is False, so without this test it falls through the
    # range check AND every bin test and lands in the final bin via the ``i == len - 2``
    # fallback -- the top bucket, silently, which is the one thing this function's own
    # docstring says must not happen. A valid Observation cannot be NaN, but this reads a
    # persisted JSON blob and ``json.loads`` accepts the literal ``NaN``.
    if value != value:
        return None
    if value < edges[0] or value > edges[-1]:
        return None
    for i in range(len(edges) - 1):
        if value < edges[i + 1] or i == len(edges) - 2:
            return i
    return None


# --------------------------------------------------------------------------- db
def open_db(path: str):
    import sqlite3

    if not os.path.exists(path):
        raise SystemExit(f"no database at {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _jloads(raw: Any) -> Any:
    """SQLAlchemy JSON columns read through raw sqlite3 come back as text; through the ORM as
    objects. Accept both rather than depending on which reader the caller used."""
    if raw is None or isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def optimizations(con, *, opt_id: Optional[int] = None, like: Optional[str] = None) -> List[Dict[str, Any]]:
    if opt_id is not None:
        rows = con.execute(
            "SELECT id, name, status, optimization_config, all_results, best_params, best_fitness "
            "FROM strategy_optimizations WHERE id = ?", (opt_id,)).fetchall()
    else:
        rows = con.execute(
            "SELECT id, name, status, optimization_config, all_results, best_params, best_fitness "
            "FROM strategy_optimizations WHERE name LIKE ? ORDER BY id", (like,)).fetchall()
    return [{"id": r[0], "name": r[1], "status": r[2], "config": _jloads(r[3]) or {},
             "all_results": _jloads(r[4]) or [], "best_params": _jloads(r[5]) or {},
             "best_fitness": r[6]} for r in rows]


def persisted_runs(con, opt_id: int) -> List[Dict[str, Any]]:
    """The BEST-/TOP-n ``Backtest`` rows this optimization persisted, with their blobs."""
    rows = con.execute(
        "SELECT id, name, initial_capital, final_equity, total_return, annualized_return, "
        "       max_drawdown, calmar_ratio, total_trades, win_rate, ga_fitness, "
        "       results, trades, equity_curve, drawdown_curve, start_date, end_date "
        "FROM backtests WHERE optimization_id = ? ORDER BY id", (opt_id,)).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r[0], "name": r[1], "initial_capital": r[2], "final_equity": r[3],
            "total_return": r[4], "annualized_return": r[5], "max_drawdown": r[6],
            "calmar_ratio": r[7], "total_trades": r[8], "win_rate": r[9], "ga_fitness": r[10],
            "results": _jloads(r[11]) or {}, "trades": _jloads(r[12]) or [],
            "equity_curve": _jloads(r[13]) or [], "drawdown_curve": _jloads(r[14]) or [],
            "start_date": r[15], "end_date": r[16],
        })
    return out


def ranked_genomes(all_results: Sequence[Dict[str, Any]], top: int) -> List[Dict[str, Any]]:
    """The top-N DISTINCT-FITNESS genomes -- the same ranking every other tool in this repo
    uses (``run_genome_once``, ``recover_missing_topn``, ``backtest_parity``), so "TOP3" means
    the same individual in the report as in the persisted row name."""
    seen, ranked = set(), []
    for r in sorted(all_results,
                    key=lambda r: (r.get("fitness") if r.get("fitness") is not None else -1e9),
                    reverse=True):
        key = round(float(r.get("fitness") or 0.0), 6)
        if key in seen:
            continue
        seen.add(key)
        ranked.append(r)
        if len(ranked) >= top:
            break
    return ranked


# --------------------------------------------------------------------------- genes
def market_gene_rows(params: Dict[str, Any], block: Dict[str, Any]) -> List[Tuple[str, str, str, str]]:
    """``(structure, field-short, mode, threshold)`` for every market gene in one genome.

    Leaf ids are ``<structure>-market-<short>`` (plan Task 8), so the structure and the field
    fall straight out of the gene name and the report stays registry-generic: a profile that
    adds fields adds rows here with no code change.
    """
    shorts = {str(f.get("short")): f for f in (block.get("fields") or [])}
    rows: List[Tuple[str, str, str, str]] = []
    for key, value in sorted(params.items()):
        if not key.startswith("cond:") or not key.endswith(":mode") or "-market-" not in key:
            continue
        leaf = key[len("cond:"):-len(":mode")]
        structure, short = leaf.split("-market-", 1)
        mode = _mode_token(value, short, shorts)
        threshold = params.get(f"cond:{leaf}:value")
        shown = "-" if mode == "off" or threshold is None else f"{float(threshold):g}"
        rows.append((structure, short, str(mode), shown))
    return rows


def _mode_token(value: Any, short: str, shorts: Dict[str, Dict[str, Any]]) -> str:
    """A persisted mode gene as its TOKEN, VALIDATED against the choices the PERSISTED FieldSpec
    declares -- not against this process's registry, which may have moved on since the run.

    NOT ``strategy_param_space.mode_token``, which is the one reader for the DECODE path and
    raises on anything it cannot interpret. A report walks many jobs, some of them old; one
    unreadable gene must render as an obvious ``INVALID`` cell rather than abort the report on
    the other twenty. It applies the same rule and the same choice order, and it refuses the
    same things -- it just says so in a column instead of an exception.
    """
    spec = shorts.get(short) or {}
    if spec.get("kind") == "categorical":
        choices = ["off", *list((spec.get("codes") or {}).keys())]
    else:
        choices = ["off", "below", "above"]
    if isinstance(value, str):
        return value if value in choices else f"INVALID({value!r} not in {choices!r})"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"INVALID({value!r})"
    if float(value) != int(value):
        return f"INVALID({value!r})"
    index = int(value)
    return choices[index] if 0 <= index < len(choices) else f"INVALID(index {index})"


# --------------------------------------------------------------------------- trades
def _pnl(trade: Dict[str, Any]) -> float:
    try:
        return float(trade.get("pnl") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def structures(trades: Sequence[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Group trade rows into the units a reader means by "a structure": option legs sharing a
    ``transaction_id`` are ONE bet (a vertical's two legs are meaningless apart), everything
    else is its own group. Same rule as ``results._cap_groups``, stated here because this tool
    reads a persisted blob rather than a live account."""
    groups: "OrderedDict[Any, List[Dict[str, Any]]]" = OrderedDict()
    loose: List[List[Dict[str, Any]]] = []
    for trade in trades:
        txn = trade.get("transaction_id")
        if txn is not None and trade.get("contract_symbol"):
            groups.setdefault(txn, []).append(trade)
        else:
            loose.append([trade])
    return list(groups.values()) + loose


def concentration(values: Sequence[float]) -> Tuple[float, float, float]:
    """``(net, top1_pct, top5_pct)`` -- the share of NET P&L the single best and best five
    units carry.

    ``nan`` shares when net is zero OR NEGATIVE. A percentage of nothing is not zero, and a
    share of a LOSS is worse than meaningless: dividing a positive best trade by a negative net
    prints "-42% of net P&L", which reads as a loss concentration when it is the opposite, and
    a large loser in a losing book prints a reassuring small positive. Concentration is a
    statement about how a PROFIT was earned; a book that lost money has no profit to
    concentrate, and the honest answer is that the question does not apply.
    """
    ordered = sorted(values, reverse=True)
    net = math.fsum(ordered)
    if not ordered or net <= 0.0:
        return net, float("nan"), float("nan")
    return net, ordered[0] / net * 100.0, math.fsum(ordered[:5]) / net * 100.0


def attribution(trades: Sequence[Dict[str, Any]], block: Dict[str, Any]) -> Dict[str, Any]:
    """Per FIELD, per BIN: units, issuers, entry dates, net P&L and the bin's own top-1/top-5.

    The unit is a STRUCTURE (see :func:`structures`), not a leg, so a four-leg condor counts
    once and its P&L is the net of its legs. A structure whose legs somehow disagree about
    their entry state is skipped into ``inconsistent`` rather than attributed to one of them.
    """
    specs = {str(f.get("name")): f for f in (block.get("fields") or [])}
    fields: List[str] = list(specs) or sorted({
        name for t in trades for name in ((t.get("entry_state") or {}).get("values") or {})})
    result: Dict[str, Any] = {"fields": OrderedDict(), "units": 0, "unattributed": 0,
                              "inconsistent": 0, "ambiguous": 0, "gap_days": []}
    units = []
    for group in structures(trades):
        # THE STATE IS WRITTEN ONCE PER STRUCTURE, on whichever leg came first -- a four-leg
        # condor is one decision and one measurement (see
        # ``market_condition_bt.attach_entry_states``). So take the first leg that HAS one, and
        # count as inconsistent only legs that disagree on a state they both carry.
        present = [t.get("entry_state") for t in group if t.get("entry_state")]
        distinct = {json.dumps(st.get("values") or {}, sort_keys=True) for st in present}
        if len(distinct) > 1:
            result["inconsistent"] += 1
            continue
        state = present[0] if present else {}
        if state.get("ambiguous"):
            result["ambiguous"] += 1
        if state:
            result["gap_days"].append(int(state.get("gap_days") or 0))
        units.append({
            "pnl": math.fsum(_pnl(t) for t in group),
            "symbol": (group[0].get("underlying_symbol") or group[0].get("symbol") or "?"),
            "session": state.get("session"),
            "values": (state.get("values") or {}),
        })
    result["units"] = len(units)
    result["unattributed"] = sum(1 for u in units if not u["values"])
    for field in fields:
        edges, labels = field_bins(field, specs.get(field))
        buckets: List[Dict[str, Any]] = [
            {"label": label, "pnls": [], "symbols": set(), "sessions": set()} for label in labels]
        outside = {"label": "outside the declared edges", "pnls": [], "symbols": set(),
                   "sessions": set()}
        unknown: Counter = Counter()
        for unit in units:
            obs = unit["values"].get(field)
            if obs is None:
                unknown["not recorded"] += 1
                continue
            if obs.get("status") != "valid" or obs.get("value") is None:
                unknown[str(obs.get("status") or "unknown")] += 1
                continue
            index = bin_of(float(obs["value"]), edges)
            bucket = outside if index is None else buckets[index]
            bucket["pnls"].append(unit["pnl"])
            bucket["symbols"].add(unit["symbol"])
            if unit["session"]:
                bucket["sessions"].add(unit["session"])
        rows = []
        for bucket in buckets + ([outside] if outside["pnls"] else []):
            net, top1, top5 = concentration(bucket["pnls"])
            rows.append({"bin": bucket["label"], "units": len(bucket["pnls"]),
                         "issuers": len(bucket["symbols"]), "dates": len(bucket["sessions"]),
                         "net_pnl": net, "top1_pct": top1, "top5_pct": top5})
        result["fields"][field] = {"edges": list(edges), "rows": rows,
                                   "unknown": dict(sorted(unknown.items()))}
    return result


# --------------------------------------------------------------------------- per-year
def per_year(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Per-year return / drawdown from the ACCOUNT ENGINE's own ``yearly_breakdown``, plus the
    dollar profit of each of its segments read off the same equity curve.

    ``yearly_breakdown`` owns the boundary rules (a short first/last year merges into its
    neighbour), so re-deriving them here would be a second implementation of the thing the
    report is supposed to be quoting.
    """
    _bootstrap()
    from app.services.backtest.results import yearly_breakdown

    years = yearly_breakdown(run["equity_curve"] or [], run["drawdown_curve"] or [],
                             run["trades"] or [])
    equity = {str(p.get("date"))[:10]: float(p.get("equity") or 0.0)
              for p in (run["equity_curve"] or [])}
    for row in years:
        start = equity.get(str(row.get("startDate"))[:10])
        end = equity.get(str(row.get("endDate"))[:10])
        row["profit"] = (end - start) if (start is not None and end is not None) else None
    return years


# --------------------------------------------------------------------------- coverage
def coverage_report(manifest: str, profile: str, universe: Sequence[str],
                    cache_root: Optional[str] = None) -> Dict[str, Any]:
    """Per symbol, how many sessions of a published snapshot are UNKNOWN and why.

    THE FEATURE-OFF DIAGNOSTIC (design section 7: "Store coverage diagnostics for feature-off
    winners too in a separate offline report, without introducing decision-path fetches or
    changing those trials"). It reads the manifest's own rows -- nothing else -- so pointing it
    at a job whose winners had every gate off costs those trials nothing and changes nothing.
    """
    _bootstrap()
    from ba2_common.core.market_condition_reader import MappedMarketConditionReader

    if cache_root is None:
        from ba2_common.config import CACHE_FOLDER
        cache_root = CACHE_FOLDER
    reader = MappedMarketConditionReader(cache_root, manifest, profile)
    covered = set(reader.symbols())
    coverage = reader.coverage() or {}
    out: Dict[str, Any] = {"manifest": reader.manifest_digest, "profile": profile,
                           "symbols": OrderedDict(), "uncovered": []}
    for symbol in sorted({str(s).upper() for s in universe}):
        if symbol not in covered:
            out["uncovered"].append(symbol)
            continue
        record = coverage.get(symbol) or {}
        out["symbols"][symbol] = {
            "rows": record.get("rows"),
            "first_session": record.get("first_session"),
            "last_session": record.get("last_session"),
            "exceptions": record.get("exceptions") or [],
        }
    return out


# --------------------------------------------------------------------------- rendering
def _line(out: List[str], text: str = "") -> None:
    out.append(text)


def _table(out: List[str], headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    if not rows:
        _line(out, "  (none)")
        return
    cells = [[str(c) for c in row] for row in rows]
    widths = [max(len(str(headers[i])), max(len(r[i]) for r in cells)) for i in range(len(headers))]
    _line(out, "  " + "  ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers)))
    _line(out, "  " + "  ".join("-" * w for w in widths))
    for row in cells:
        _line(out, "  " + "  ".join(row[i].ljust(widths[i]) for i in range(len(headers))))


def _pct(x: Any) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "n/a"
    return "n/a" if math.isnan(v) else f"{v:.1f}%"


def _share(pct: Any, net: Any) -> str:
    """A concentration share, or WHY there isn't one. ``concentration`` returns ``nan`` for a
    net that is zero or negative; printing that as "n/a" alone would read as a missing
    measurement rather than a question that does not apply to a losing book."""
    try:
        value = float(pct)
    except (TypeError, ValueError):
        return "n/a"
    if not math.isnan(value):
        return f"{value:.1f}%"
    try:
        n = float(net)
    except (TypeError, ValueError):
        return "n/a"
    return "n/a (net<0)" if n < 0 else "n/a (net=0)"


def _money(x: Any) -> str:
    try:
        return f"{float(x):,.0f}"
    except (TypeError, ValueError):
        return "n/a"


def render(opt: Dict[str, Any], runs: Sequence[Dict[str, Any]], top: int,
           want_coverage: bool = False, cache_root: Optional[str] = None,
           manifest_override: Optional[str] = None,
           profile_override: Optional[str] = None) -> str:
    out: List[str] = []
    block = ((opt["config"].get("backtest") or {}).get("market_condition")) or {}
    # ``enabled_instruments`` can hold a live SENTINEL (EXPERT / DYNAMIC / SCREENER) instead of
    # symbols -- the instance picks its universe at analysis time. Coverage-checking "SCREENER"
    # would report a missing symbol that does not exist, so they are dropped here and named.
    raw_universe = [str(x).upper()
                    for x in ((opt["config"].get("backtest") or {}).get("enabled_instruments") or [])]
    sentinels = sorted(set(raw_universe) & _universe_sentinels())
    universe = [x for x in raw_universe if x not in sentinels]
    _line(out, f"# market conditions -- optimization {opt['id']}: {opt['name']}")
    _line(out, f"status {opt['status']}, best fitness "
               f"{opt['best_fitness'] if opt['best_fitness'] is not None else 'n/a'}, "
               f"{len(universe)} instruments")
    if sentinels:
        _line(out, f"  {len(sentinels)} entr(y/ies) of enabled_instruments are universe "
                   f"SENTINELS ({', '.join(sentinels)}): the instance picks its symbols at "
                   f"analysis time, so they are not coverage-checkable and are excluded below.")
    _line(out)

    _line(out, "## versions (as PERSISTED with the run, never re-derived)")
    if not block:
        _line(out, "  This job carries NO market_condition block: it ran with the gates OFF.")
        _line(out, "  Nothing below describes gate behaviour; the coverage section (--coverage)")
        _line(out, "  is the diagnostic that applies to a feature-off run.")
    else:
        for key in ("profiles", "manifest", "source_profile", "timing_policy", "calendar_version",
                    "calc_version", "calc_versions", "window_start", "window_end", "gene_count"):
            if key in block:
                _line(out, f"  {key:<17} {block[key]}")
        names = [f.get("name") for f in (block.get("fields") or [])]
        _line(out, f"  {'fields':<17} {names}")
        _line(out, f"  {'genes':<17} {block.get('genes')}")
    _line(out)

    _line(out, f"## winning modes and thresholds (top {top} distinct-fitness genomes)")
    rows: List[Sequence[Any]] = []
    for rank, genome in enumerate(ranked_genomes(opt["all_results"], top), start=1):
        fitness = genome.get("fitness")
        gene_rows = market_gene_rows(genome.get("params") or {}, block)
        if not gene_rows:
            rows.append([f"TOP{rank}", f"{fitness:.4f}" if fitness is not None else "n/a",
                         "(no market genes in this genome)", "", "", ""])
            continue
        for structure, short, mode, threshold in gene_rows:
            rows.append([f"TOP{rank}", f"{fitness:.4f}" if fitness is not None else "n/a",
                         structure, short, mode, threshold])
    _table(out, ("rank", "fitness", "structure", "field", "mode", "threshold"), rows)
    _line(out)

    for run in runs:
        _render_run(out, run, block)

    if want_coverage:
        _line(out, "## feature-off coverage diagnostic")
        # An OVERRIDE, not a default: ``--manifest``/``--profile`` are what make this useful on
        # a feature-off job, which by definition pins neither -- "check THIS job's universe
        # against the snapshot the gated jobs use". With neither given and no persisted block
        # there is nothing to check against, and the report says so rather than guessing a
        # profile name that happens to be the only one registered today.
        digest = manifest_override or block.get("manifest")
        profiles = block.get("profiles") or []
        profile = profile_override or (profiles[0] if profiles else None)
        # ONE branch, so a refusal cannot be followed by advice that contradicts it. Setting
        # ``digest = None`` and falling through printed "REFUSED: a manifest without a profile"
        # and then "No manifest is pinned ... pass --manifest" -- to a reader who had just
        # passed one.
        if digest and not profile:
            _line(out, "  REFUSED: a manifest without a profile. The snapshot's rows are keyed")
            _line(out, "  by profile; pass --profile to say which one to read.")
        elif not digest:
            _line(out, "  No manifest is pinned on this job, so there is no published snapshot")
            _line(out, "  to diagnose. Pass --manifest/--profile to check a snapshot anyway.")
        elif not universe:
            # REFUSE rather than print an all-clear. "Every instrument has rows" about a list of
            # zero instruments is a reassuring sentence describing a check that examined nothing.
            _line(out, "  REFUSED: this job's stored config records no enabled_instruments, so")
            _line(out, "  there is no universe to check the snapshot against. Nothing here says")
            _line(out, "  the coverage is good; it says the question was not asked.")
        else:
            _render_coverage(out, coverage_report(digest, profile, universe, cache_root))
        _line(out)
    return "\n".join(out) + "\n"


def _render_run(out: List[str], run: Dict[str, Any], block: Dict[str, Any]) -> None:
    mc = (run["results"] or {}).get("market_condition") or {}
    stats = mc.get("stats") or {}
    _line(out, f"## run {run['id']}: {run['name']}")
    _line(out, f"  window {run['start_date']}..{run['end_date']}, "
               f"profit {_money((run['final_equity'] or 0) - (run['initial_capital'] or 0))}, "
               f"CAR {_pct(run['annualized_return'])}, maxDD {_pct(run['max_drawdown'])}, "
               f"{run['total_trades']} trades")
    _line(out)
    _line(out, "### gate counters (eligible is reported SEPARATELY from condition rejections)")
    if not stats:
        _line(out, "  (no market_condition block on this run's results: the gates were off, or")
        _line(out, "   the row predates the per-run counters)")
    else:
        for key in ("eligible_recommendations", "market_evaluated", "market_gate_passed",
                    "market_gate_rejected", "market_unknown_recommendations",
                    "market_leaf_evaluations", "entries_staged",
                    "structures_with_entry_state", "entry_read_failures"):
            if key in stats:
                _line(out, f"  {key:<30} {stats[key]}")
        unknown = stats.get("market_unknown_input_by_reason") or {}
        _line(out, f"  {'unknown input by reason':<30} {unknown if unknown else '(none)'}")
        _line(out, "  (passed + rejected + unknown does NOT sum to evaluated: one recommendation")
        _line(out, "   whose gates both rejected on a value AND read an unknown counts in both.)")
    _line(out)

    groups = structures(run["trades"])
    _line(out, "### structures")
    # NEITHER NUMBER IS WHAT ITS ONE-WORD NAME SUGGESTS, so both are labelled with what they
    # actually count. entries_staged is incremented when the entry RULE fires -- before the
    # dup-position and equity gates -- so it is an upper bound on submissions; the second is
    # CLOSED round trips, so a position still open at the end of the run is in neither.
    _line(out, f"  entry rules that fired (before the dup/equity gates) "
               f"{stats.get('entries_staged', 'n/a')}")
    _line(out, f"  closed round-trip units in the trade blob                {len(groups)}")
    net, top1, top5 = concentration([math.fsum(_pnl(t) for t in g) for g in groups])
    _line(out, f"  net P&L {_money(net)}, top-1 {_share(top1, net)} of it, "
               f"top-5 {_share(top5, net)}")
    _line(out)

    _line(out, "### per year (the WHOLE account, as the engine computed it)")
    try:
        years = per_year(run)
    except Exception as e:  # noqa: BLE001 -- a report must not die on one unreadable curve
        years = []
        _line(out, f"  per-year breakdown unavailable: {e}")
    _table(out, ("year", "start", "end", "profit", "return", "maxDD", "trades", "win rate"),
           [[y.get("year"), y.get("startDate"), y.get("endDate"), _money(y.get("profit")),
             _pct(y.get("returnPct")), _pct(y.get("maxDrawdownPct")), y.get("totalTrades"),
             _pct(y.get("winRate"))] for y in years])
    _line(out)

    _line(out, "### entry-state attribution")
    _line(out, "  A BIN IS NOT AN ACCOUNT. These rows hold overlapping trades that happened to")
    _line(out, "  be entered in one measured regime; there is no capital behind a bin and no")
    _line(out, "  annualised return is computed for one. Dollars, counts and shares only.")
    # THE APPROXIMATION, STATED WHERE IT IS CONSUMED. Anyone reading a bin table is about to
    # draw a conclusion from it, and this is the assumption that conclusion rests on.
    _line(out, "  Each structure is matched to a decision by DATE PROXIMITY, not identity (the")
    _line(out, "  blob carries no recommendation id): the latest recorded decision for its")
    _line(out, "  underlying within 7 days of the fill, counted as AMBIGUOUS below and flagged")
    _line(out, "  in the blob when more than one decision fell in that window.")
    data = attribution(run["trades"], block)
    gaps = data["gap_days"]
    _line(out, f"  units {data['units']}, without a recorded entry state {data['unattributed']}, "
               f"legs disagreeing on their state {data['inconsistent']}")
    _line(out, f"  bound on the decision's own session {sum(1 for g in gaps if g == 0)}, "
               f"across a gap {sum(1 for g in gaps if g > 0)} "
               f"(max {max(gaps) if gaps else 0} days), AMBIGUOUS {data['ambiguous']}")
    for key, label in (("bound_same_session", "same session"), ("bound_with_gap", "with a gap"),
                       ("ambiguous", "ambiguous")):
        if key in stats:
            _line(out, f"  run-recorded binding: {label:<14} {stats[key]}")
    if data["units"] and data["unattributed"] == data["units"]:
        _line(out, "  NOTHING TO ATTRIBUTE: no trade of this run carries an entry state. Either")
        _line(out, "  the run predates the entry-state capture, or it ran with the profile off.")
        _line(out, "  Its P&L is not evidence about any regime -- use --coverage instead.")
    for field, info in data["fields"].items():
        _line(out)
        _line(out, f"  {field}  edges {[_fmt_edge(e) for e in info['edges']] or 'none declared'}")
        _table(out, ("bin", "units", "issuers", "dates", "net P&L", "top-1", "top-5"),
               [[r["bin"], r["units"], r["issuers"], r["dates"], _money(r["net_pnl"]),
                 _share(r["top1_pct"], r["net_pnl"]), _share(r["top5_pct"], r["net_pnl"])]
                for r in info["rows"]])
        if info["unknown"]:
            _line(out, f"    not binned: {info['unknown']}")
    _line(out)


def _universe_sentinels() -> set:
    """The live universe sentinels, from their one definition -- never a literal list here."""
    try:
        from ba2_common.core.market_condition_live import UNIVERSE_SENTINELS

        return set(UNIVERSE_SENTINELS)
    except ImportError:                     # a stdlib-only invocation: nothing to filter against
        return set()


def _render_coverage(out: List[str], report: Dict[str, Any]) -> None:
    _line(out, f"  manifest {report['manifest']} profile {report['profile']}")
    if report["uncovered"]:
        _line(out, f"  NOT COVERED AT ALL ({len(report['uncovered'])}): "
                   f"{', '.join(report['uncovered'])}")
        _line(out, "  Every gate on those symbols would have been unknown for the whole run.")
    else:
        _line(out, "  every instrument of this run's universe has rows in the snapshot")
    rows = [[symbol, info["rows"], info["first_session"], info["last_session"],
             len(info["exceptions"])]
            for symbol, info in report["symbols"].items() if info["exceptions"]]
    if rows:
        _line(out, "  symbols warmed WITH exceptions (sessions that would read unknown):")
        _table(out, ("symbol", "rows", "first", "last", "exceptions"), rows)


# --------------------------------------------------------------------------- cli
def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="report_market_conditions.py",
        description="Per-job market-condition coverage, gate and attribution report.")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--opt", type=int, help="StrategyOptimization id.")
    source.add_argument("--like", help="SQL LIKE over the optimization NAME, e.g. %%ohlcv%%.")
    p.add_argument("--top", type=int, default=5, help="How many distinct-fitness genomes (5).")
    p.add_argument("--db", default=default_db(), help="Test database (read-only).")
    p.add_argument("--out", help="Also write the report to this file.")
    p.add_argument("--coverage", action="store_true",
                   help="Append the offline snapshot-coverage diagnostic (feature-off runs).")
    p.add_argument("--cache-root", help="Cache root for --coverage (default: ba2_common CACHE_FOLDER).")
    p.add_argument("--manifest", help="Check --coverage against THIS snapshot instead of the "
                                      "one the job pinned (the point of the flag: a feature-off "
                                      "job pins none).")
    p.add_argument("--profile", help="Profile to read the --manifest snapshot as; required with "
                                     "--manifest when the job records no profile of its own.")
    args = p.parse_args(argv)

    con = open_db(args.db)
    rows = optimizations(con, opt_id=args.opt, like=args.like)
    if not rows:
        raise SystemExit(f"no optimization matched {args.opt or args.like!r} in {args.db}")
    if (args.manifest or args.profile) and not args.coverage:
        raise SystemExit("--manifest/--profile only affect the --coverage diagnostic; pass "
                         "--coverage too, or drop them")
    text = "\n".join(render(opt, persisted_runs(con, opt["id"]), args.top,
                            want_coverage=args.coverage, cache_root=args.cache_root,
                            manifest_override=args.manifest, profile_override=args.profile)
                     for opt in rows)
    sys.stdout.write(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        sys.stderr.write(f"written to {args.out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

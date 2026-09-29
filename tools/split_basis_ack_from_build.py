"""Generate ``acknowledge_jump`` ``BasisOverride`` entries from a ``warm_market_conditions.py
build`` log's EXCLUDED report (split-basis ack sweep, 2026-09-29).

WHY. The market-condition build refuses a symbol whose FMP daily history reads ``mixed_basis``
(``ba2_common.core.split_basis.check_split_basis``): a one-day close move beyond x1.4 sits within
30 days of a calendar split but not on its ex-date bar. For a volatile small/micro-cap this is
often a REAL price gap (earnings, trial data, a resignation...), not a basis defect, and the
sanctioned repair is an ``acknowledge_jump`` entry in
``ba2_common.core.split_basis_overrides.BASIS_OVERRIDES`` (read that module's docstring first).

WHAT THIS TOOL DOES. ``build`` writes one line per refused symbol to its log:

    [11:05:45] build ABTC: EXCLUDED [{'kind': 'split_basis_unverified', 'checks': [...]}]

-- a Python literal. Each ``mixed_basis`` check's ``reason`` lists every offending move as
``"<date> x<ratio>"``. This tool parses those lines, and for every named symbol and offending
move:

  1. reads the symbol's CURRENT cached FMP daily file (the same loader the split-basis check
     itself uses -- ``ba2_common.core.market_condition_source.read_fmp_daily_cache`` via
     ``fmp_daily_cache_path``, from ``cache_root`` or the live ``ba2_common.config.CACHE_FOLDER``);
  2. MEASURES the jump bar's close / previous-bar close from those bytes -- never copies the
     ratio out of the log text;
  3. asserts the measured ratio matches the log's ratio within ``--tolerance`` (the log prints
     ``:.4f``, so the default 0.0005 comfortably covers rounding) -- a mismatch is reported, not
     guessed past: the entry is left out and the symbol/date is listed as a problem;
  4. emits an ``acknowledge_jump`` ``BasisOverride`` with ``anchors`` = the (date, close) pair
     of the previous bar and the jump bar, both read from the same file.

A ``mixed_basis`` verdict is the only kind this tool can acknowledge. A symbol whose EXCLUDED
report carries a verdict ``acknowledge_jump`` cannot express (``drift``, ``undetectable``, a
calendar-row defect) is reported as a problem instead of forced into a jump entry.

USAGE

    python tools/split_basis_ack_from_build.py build.out --symbols ABTC,ACB,... > entries.py
    python tools/split_basis_ack_from_build.py build.out --symbols ABTC,ACB --out entries.py

Exits 0 when every requested symbol produced entries with no problems, 1 otherwise (the Python
source printed/written is still whatever DID measure cleanly -- read the problem list before
trusting it complete).
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

#: The 44 symbols of the 2026-09-29 sweep (docs: operator decision "they are legit gaps, leave
#: them through"). IAC is deliberately excluded -- its verdicts are drift/undetectable
#: (spin-off related), a different problem acknowledge_jump cannot express.
ALL_SWEEP_SYMBOLS: Tuple[str, ...] = (
    "ABTC", "ACB", "AMC", "AMWL", "ARWR", "BLNK", "BMNR", "BNED", "CBUS", "CLDX", "DRIO", "ENLT",
    "FCEL", "FIZZ", "FRMM", "GEVO", "GIBO", "GOAI", "GPUS", "GREE", "GYRE", "IOVA", "LAC", "LYEL",
    "MAAS", "MARA", "NKTR", "NRXP", "OCGN", "ONDS", "OTLY", "PTN", "QUBT", "QXO", "RGC", "SBET",
    "SEZL", "SLGL", "SMCI", "SRPT", "TLRY", "TONX", "UP", "VRDN",
)

EXCLUDED_LINE = re.compile(r"^\[\d\d:\d\d:\d\d\] build (?P<symbol>\S+): EXCLUDED (?P<payload>\[.*\])\s*$")
MOVE_RE = re.compile(r"(\d{4}-\d{2}-\d{2}) x(-?[0-9]*\.?[0-9]+)")

DEFAULT_TOLERANCE = 0.0005
EVIDENCE = ("operator 2026-09-29: accepted as a real price gap (volatile name; flagged "
           "mixed_basis by the goal2027atr equity market-condition build); not verified "
           "against a second source")


class RatioMismatch(RuntimeError):
    """A move's measured ratio does not match the log within tolerance, or the bar is missing."""


@dataclass(frozen=True)
class LoggedMove:
    symbol: str
    split_date: date
    jump_date: date
    logged_ratio: float


@dataclass(frozen=True)
class MeasuredAck:
    symbol: str
    event_date: date
    ratio: float
    #: ((prev_bar_date, prev_close), (jump_date, jump_close)), both read from the cache.
    anchors: Tuple[Tuple[date, float], Tuple[date, float]]


def parse_build_log(path) -> Dict[str, List[dict]]:
    """``{symbol: [check dict, ...]}`` for every ``build SYM: EXCLUDED [...]`` line of a
    ``warm_market_conditions.py build`` log. Only ``split_basis_unverified`` checks are kept (a
    ``split_calendar_unavailable``/``fetch_failed`` exclusion carries no ``checks`` to parse). A
    log concatenated from more than one run keeps the LAST line per symbol."""
    out: Dict[str, List[dict]] = {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = EXCLUDED_LINE.match(line.rstrip("\n"))
            if not m:
                continue
            payload = ast.literal_eval(m.group("payload"))
            checks: List[dict] = []
            for item in payload:
                if isinstance(item, dict) and item.get("kind") == "split_basis_unverified":
                    checks.extend(item.get("checks", []))
            out[m.group("symbol")] = checks
    return out


def mixed_basis_moves(symbol: str, checks: Sequence[dict]) -> List[LoggedMove]:
    """Every offending ``(jump_date, logged ratio)`` named in ``symbol``'s ``mixed_basis``
    checks' ``reason`` text."""
    moves: List[LoggedMove] = []
    for ch in checks:
        if ch.get("verdict") != "mixed_basis":
            continue
        split_date = date.fromisoformat(ch["split_date"])
        for d, r in MOVE_RE.findall(ch.get("reason", "")):
            moves.append(LoggedMove(symbol, split_date, date.fromisoformat(d), float(r)))
    return moves


#: Verdicts that are not a refusal ``acknowledge_jump`` would need to lift: ``consistent`` and
#: ``future`` never excluded the symbol, ``refetched`` is informational, and ``mixed_basis`` is
#: handled by ``mixed_basis_moves`` instead (it IS what acknowledge_jump fixes).
_NOT_A_PROBLEM = ("consistent", "future", "refetched", "mixed_basis")


def unacknowledgeable_verdicts(checks: Sequence[dict]) -> List[dict]:
    """Checks whose verdict is a refusal (``split_basis.REFETCH_VERDICTS``) that
    ``acknowledge_jump`` cannot express -- ``drift`` and ``undetectable``, never ``mixed_basis``
    (that is the one it fixes)."""
    return [ch for ch in checks if ch.get("verdict") not in _NOT_A_PROBLEM]


def _load_daily(symbol: str, cache_root: Optional[str] = None) -> Tuple[np.ndarray, np.ndarray]:
    """``(dates, close)`` of ``symbol``'s cached FMP daily file, sorted like
    ``check_split_basis`` sorts it. Raises ``FileNotFoundError`` when there is no cache file."""
    from ba2_common.core.market_condition_source import fmp_daily_cache_path, read_fmp_daily_cache

    path = fmp_daily_cache_path(symbol, cache_root)
    if path is None:
        raise FileNotFoundError(f"{symbol}: no FMP daily cache file under {cache_root!r}")
    dates, _o, _h, _l, c, _v = read_fmp_daily_cache(path)
    order = np.argsort(dates, kind="stable")
    return dates[order], c[order]


def measure_ack(move: LoggedMove, dates: np.ndarray, close: np.ndarray, *,
               tolerance: float = DEFAULT_TOLERANCE) -> MeasuredAck:
    """Measure ``move`` against the cached bytes; raises ``RatioMismatch`` rather than guess."""
    hit = np.flatnonzero(dates == np.datetime64(move.jump_date, "D"))
    if not len(hit):
        raise RatioMismatch(f"{move.symbol} {move.jump_date}: no bar in the current cache "
                            f"(log split {move.split_date}, logged x{move.logged_ratio:.4f})")
    i = int(hit[0])
    if i == 0:
        raise RatioMismatch(f"{move.symbol} {move.jump_date}: is the file's first bar, no "
                            "previous close to measure a ratio against")
    prev_date = dates[i - 1].astype(object)
    prev_close, cur_close = float(close[i - 1]), float(close[i])
    if not (prev_close > 0 and cur_close > 0):
        raise RatioMismatch(f"{move.symbol} {move.jump_date}: non-positive close "
                            f"({prev_close!r} -> {cur_close!r})")
    measured = cur_close / prev_close
    if abs(measured - move.logged_ratio) > tolerance:
        raise RatioMismatch(
            f"{move.symbol} {move.jump_date}: measured ratio {measured:.6f} does not match the "
            f"log's x{move.logged_ratio:.4f} (split {move.split_date}) within tolerance "
            f"{tolerance}; the cache changed since the build ran -- re-run the build, don't guess")
    return MeasuredAck(move.symbol, move.jump_date, measured,
                       ((prev_date, prev_close), (move.jump_date, cur_close)))


def generate(build_log, symbols: Sequence[str], *, cache_root: Optional[str] = None,
            tolerance: float = DEFAULT_TOLERANCE) -> Tuple[Dict[str, List[MeasuredAck]], List[str]]:
    """``({symbol: [MeasuredAck, ...]}, [problem, ...])`` for ``symbols`` against ``build_log``.

    A symbol with any problem still contributes the moves that DID measure cleanly; the caller
    must not commit those without resolving the listed problems for that symbol."""
    parsed = parse_build_log(build_log)
    acks: Dict[str, List[MeasuredAck]] = {}
    problems: List[str] = []
    for sym in symbols:
        checks = parsed.get(sym)
        if checks is None:
            problems.append(f"{sym}: no 'build {sym}: EXCLUDED' line in the log")
            continue
        bad_verdicts = unacknowledgeable_verdicts(checks)
        if bad_verdicts:
            problems.append(
                f"{sym}: verdict(s) acknowledge_jump cannot express: "
                + ", ".join(f"{c.get('split_date')}:{c.get('verdict')}" for c in bad_verdicts))
        moves = mixed_basis_moves(sym, checks)
        if not moves:
            if not bad_verdicts:
                problems.append(f"{sym}: no mixed_basis offending moves in the log; nothing to acknowledge")
            continue
        try:
            dates, close = _load_daily(sym, cache_root)
        except FileNotFoundError as e:
            problems.append(str(e))
            continue
        measured: Dict[date, MeasuredAck] = {}
        for mv in moves:
            if mv.jump_date in measured:
                continue  # the same bar can be near two splits; one acknowledgement covers both
            try:
                measured[mv.jump_date] = measure_ack(mv, dates, close, tolerance=tolerance)
            except RatioMismatch as e:
                problems.append(str(e))
        if measured:
            acks[sym] = sorted(measured.values(), key=lambda e: e.event_date)
    return acks, problems


def render_entries(acks: Dict[str, List[MeasuredAck]]) -> str:
    """The Python source of the ``BasisOverride(...)`` tuple entries, one per line group,
    symbols in alphabetical order. Meant to be pasted into ``BASIS_OVERRIDES``."""
    lines: List[str] = []
    for sym in sorted(acks):
        for e in acks[sym]:
            anchors_src = ", ".join(
                f"(date({d.year}, {d.month}, {d.day}), {c!r})" for d, c in e.anchors)
            lines.append(
                f'    BasisOverride(\n'
                f'        "{sym}", date({e.event_date.year}, {e.event_date.month}, {e.event_date.day}), '
                f'{e.ratio!r}, KIND_ACKNOWLEDGE_JUMP,\n'
                f'        ({anchors_src}),\n'
                f'        {EVIDENCE!r}),')
    return "\n".join(lines)


BLOCK_BEGIN = "# --- split_basis_ack_from_build: generated block begin ---"
BLOCK_END = "# --- split_basis_ack_from_build: generated block end ---"


def render_block(acks: Dict[str, List[MeasuredAck]]) -> str:
    total = sum(len(v) for v in acks.values())
    header = f"{BLOCK_BEGIN} ({total} entries, {len(acks)} symbols)"
    return "\n".join([header, render_entries(acks), BLOCK_END])


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("build_log", help="warm_market_conditions.py build log (text)")
    p.add_argument("--symbols", default=",".join(ALL_SWEEP_SYMBOLS),
                   help="comma-separated symbols to acknowledge (default: the 44-symbol sweep)")
    p.add_argument("--exclude", default="", help="comma-separated symbols to skip")
    p.add_argument("--cache-root", default=None, help="defaults to the live ba2_common CACHE_FOLDER")
    p.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE)
    p.add_argument("--out", default=None, help="write the block here instead of stdout")
    args = p.parse_args(argv)

    excluded = {s.strip().upper() for s in args.exclude.split(",") if s.strip()}
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip() and s.strip().upper() not in excluded]

    acks, problems = generate(args.build_log, symbols, cache_root=args.cache_root, tolerance=args.tolerance)
    block = render_block(acks)
    if args.out:
        Path(args.out).write_text(block + "\n", encoding="utf-8")
    else:
        print(block)

    if problems:
        print(f"\n{len(problems)} problem(s):", file=sys.stderr)
        for pr in problems:
            print(f"  - {pr}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

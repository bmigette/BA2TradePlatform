"""ONE parser/normaliser of the screener's symbol-universe restriction, shared by the backtest launcher
(``--screener-universe-file``, the stamped ``screener_opt.universe_restriction``) and live (``StockScreener``, setting
``screener_symbol_universe``), so the two sides can never spell or split the list differently.

Spelling: the daily panel, the metric store and the vendor write a share class with a DASH (``BRK-B``, ``BF-B``); a
dotted spelling (``BRK.B``) is the same instrument and is normalised to the dash. Parsing: entries are separated by
commas or newlines; blank entries, whole-line ``#`` comments and trailing ``#`` comments are ignored.
"""
from __future__ import annotations

from typing import Any, Iterable, List, Optional


def canonical_symbol(raw: Any) -> str:
    """``BRK.b`` -> ``BRK-B``; non-strings and blanks -> ``""`` (callers drop empties)."""
    if not isinstance(raw, str):
        return ""
    return raw.strip().upper().replace(".", "-")


def split_entries(text: str) -> List[str]:
    """Raw entries of a list text: newline/comma separated, ``#`` comments (whole-line or trailing) removed."""
    out: List[str] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0]
        out.extend(part.strip() for part in line.split(","))
    return [x for x in out if x]


def parse_symbols(raw: Any, *, what: str = "symbol universe") -> List[str]:
    """Sorted, de-duplicated canonical symbols of a restriction given as text or as an iterable of strings. Anything
    else raises (a malformed restriction must not silently become "no restriction")."""
    if isinstance(raw, str):
        entries: Iterable[str] = split_entries(raw)
    elif isinstance(raw, (list, tuple, set, frozenset)) and all(isinstance(x, str) for x in raw):
        entries = [e for x in raw for e in split_entries(x)]
    else:
        raise ValueError(f"{what} must be a string or a list of strings, got {type(raw).__name__}: {raw!r}")
    return sorted({canonical_symbol(e) for e in entries if canonical_symbol(e)})


def parse_symbol_universe(raw: Any, *, what: str = "screener_symbol_universe") -> Optional[frozenset]:
    """The live setting as a set; None (or an empty value) = no restriction."""
    if raw is None:
        return None
    syms = parse_symbols(raw, what=what)
    return frozenset(syms) if syms else None


def read_universe_file(path: str) -> List[str]:
    """The canonical symbols of a universe file (see the module docstring for the format)."""
    with open(path, encoding="utf-8") as f:
        return parse_symbols(f.read(), what=f"universe file {path!r}")

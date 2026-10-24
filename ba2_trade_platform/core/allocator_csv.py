"""CSV export / import of one account's portfolio-allocator setup.

One row per (label, symbol): the label's target %, comment and colour are repeated on each
of its rows; ``weight_pct`` is the symbol's share INSIDE the label. A label with no symbol is
one row with an empty ``symbol``. An empty ``weight_pct`` means "no stored weight" (the symbol
takes the even-split default), exactly as the page treats a symbol nobody has edited. The
"shares" the page shows are computed from these percentages and the account value, so they
are not stored and not exported.

Import REPLACES: the account's managed labels become exactly the file's, each label's symbols
become exactly the file's (instrument labels are global, as on the page's own "remove from
label"), and a symbol's weight row is written only when the file carries a weight. The whole
file is validated before anything is written; any problem is reported by line and nothing
changes.
"""
import csv
import io
from typing import Dict, List, Optional

from ..logger import logger
from ba2_common.core import portfolio_allocation_store as store
from ba2_common.core.utils import get_symbols_by_label

COLUMNS = ["label", "target_pct", "label_comment", "label_color",
           "symbol", "weight_pct", "symbol_comment"]


def export_allocation_csv(account_id: int) -> str:
    """The account's managed labels, their symbols and percentages, as CSV text."""
    labels = store.get_managed_labels(account_id)
    members = get_symbols_by_label([lab.label for lab in labels])
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)
    for lab in labels:
        rows = store.get_symbol_rows(account_id, lab.label)
        symbols = sorted(set(members[lab.label]) | set(rows))
        head = [lab.label, _num(lab.target_pct), lab.comment or "", lab.color or ""]
        if not symbols:
            writer.writerow(head + ["", "", ""])
        for symbol in symbols:
            row = rows.get(symbol)
            writer.writerow(head + [symbol,
                                    _num(row.weight_pct) if row is not None else "",
                                    (row.comment or "") if row is not None else ""])
    return out.getvalue()


def _num(value: float) -> str:
    return f"{float(value):.10g}"


def parse_allocation_csv(text: str) -> List[Dict]:
    """Validate ``text`` and return ``[{label, target_pct, comment, color, symbols:
    [{symbol, weight_pct|None, comment}]}]`` in file order. Raises ``ValueError`` listing
    every problem found."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(f"missing column(s): {', '.join(missing)}")
    errors: List[str] = []
    labels: Dict[str, Dict] = {}
    for line, rec in enumerate(reader, start=2):
        label = (rec["label"] or "").strip()
        if not label:
            errors.append(f"line {line}: empty label")
            continue
        target = _pct(rec["target_pct"], line, "target_pct", errors, required=True)
        entry = labels.get(label)
        if entry is None:
            entry = labels[label] = {"label": label, "target_pct": target,
                                     "comment": rec["label_comment"] or "",
                                     "color": (rec["label_color"] or "").strip(),
                                     "symbols": []}
        elif target is not None and entry["target_pct"] is not None \
                and abs(entry["target_pct"] - target) > 1e-9:
            errors.append(f"line {line}: label {label!r} has two different target_pct values")
        symbol = (rec["symbol"] or "").strip().upper()
        if not symbol:
            continue
        if any(s["symbol"] == symbol for s in entry["symbols"]):
            errors.append(f"line {line}: {symbol} appears twice in label {label!r}")
            continue
        weight = _pct(rec["weight_pct"], line, "weight_pct", errors, required=False)
        entry["symbols"].append({"symbol": symbol, "weight_pct": weight,
                                 "comment": rec["symbol_comment"] or ""})
    if not labels and not errors:
        errors.append("the file has no labels")
    if errors:
        raise ValueError("; ".join(errors))
    return list(labels.values())


def _pct(raw, line: int, column: str, errors: List[str], *, required: bool) -> Optional[float]:
    text = (raw or "").strip()
    if not text:
        if required:
            errors.append(f"line {line}: {column} is empty")
        return None
    try:
        value = float(text)
    except ValueError:
        errors.append(f"line {line}: {column} {text!r} is not a number")
        return None
    if not 0.0 <= value <= 100.0:
        errors.append(f"line {line}: {column} {value:g} is outside 0-100")
        return None
    return value


def import_allocation_csv(account_id: int, text: str) -> Dict[str, int]:
    """Replace the account's allocator setup with the CSV ``text``. Returns counts."""
    parsed = parse_allocation_csv(text)
    wanted = [entry["label"] for entry in parsed]
    result = store.replace_managed_labels(account_id, wanted)
    members = get_symbols_by_label(wanted)
    symbols_added = symbols_removed = 0
    for order, entry in enumerate(parsed):
        label = entry["label"]
        store.set_managed_label(account_id, label, target_pct=entry["target_pct"],
                                sort_order=order, comment=entry["comment"],
                                color=entry["color"])
        file_symbols = [s["symbol"] for s in entry["symbols"]]
        stale = [s for s in members[label] if s not in set(file_symbols)]
        symbols_removed += store.remove_symbols_from_label(account_id, label, stale)
        for symbol in set(store.get_symbol_rows(account_id, label)) - set(file_symbols):
            store.remove_symbol_weight(account_id, label, symbol)
        symbols_added += store.add_symbols_to_label(account_id, label, file_symbols)
        for sym in entry["symbols"]:
            if sym["weight_pct"] is not None:
                store.set_symbol_weight(account_id, label, sym["symbol"],
                                        weight_pct=sym["weight_pct"], comment=sym["comment"])
            else:
                store.remove_symbol_weight(account_id, label, sym["symbol"])
    summary = {"labels": len(parsed), "labels_added": result["added"],
               "labels_removed": result["removed"], "symbols_added": symbols_added,
               "symbols_removed": symbols_removed}
    logger.info(f"Allocator CSV import for account {account_id}: {summary}")
    return summary

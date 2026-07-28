"""Column-oriented, dict-compatible store for the congress scoring caches (2026-07-28).

WHY. The scoring caches are big and their memory is almost entirely Python boxing, not data.
Measured on the real shards:

    congress_confidence_scores__60_10_0   399,000 entries   146 MB JSON -> 311 MB RSS as dicts
    congress_skill_scores__60_5_50_12     567,796 entries

A confidence VALUE is a 12-key dict of numbers: ~378 B for the dict plus ~24 B for every boxed
float inside it, so ~660 B to carry 12 numbers that need 96 B. The keys are strings and
unavoidable-ish; the values are pure overhead.

Every value in these caches has a FIXED, ALL-NUMERIC schema, which is exactly the case where
struct-of-arrays wins: one ``array.array`` per field (8 B/value, unboxed), plus the same
``dict`` mapping key -> row index that a plain dict would have needed anyway. Lookups stay
O(1) + O(1).

WHAT THIS IS NOT. An earlier attempt moved these caches to disk (a columnar store, ecfe14c, and
an indexed SQLite KV benchmarked 2026-07-28) and BOTH lost: the workload is high-volume random
point lookups against a long-lived worker, where any per-lookup I/O cost is multiplied by
millions while the load cost amortises to nothing. This keeps everything in memory and changes
only the LAYOUT, so throughput is preserved and only the footprint moves.

TRADE-OFF, STATED PLAINLY. ``get`` MATERIALISES a dict on every read, because callers index the
result (``trader_stats['confidence_modifier']``). So reads are somewhat slower than a plain dict
and allocate transiently. That is the intended bargain: the 399k STORED values shrink, while the
~13k values read per trial are short-lived and collected. Do not "optimise" this by returning a
live view -- callers would then share mutable state with the cache.
"""
from __future__ import annotations

from array import array
from typing import Any, Dict, Iterator, List, Optional

# int64 for integral fields, float64 for the rest. A field that ever sees a float is promoted to
# 'd' permanently: mixing would otherwise silently truncate (0.7 -> 0).
_INT_CODE = "q"
_FLOAT_CODE = "d"


def use_scoring_table() -> bool:
    """False when ``BA2_SCORING_TABLE=0`` — escape hatch for the column storage, and the control
    arm when measuring it. Read at import by the caller, so a run's layout can't change midway."""
    import os
    return (os.environ.get("BA2_SCORING_TABLE") or "1").strip() not in ("0", "false", "no")


class ScoringTable:
    """Dict-compatible mapping of ``str`` key -> ``dict`` of numbers, stored column-wise.

    Supports the subset of the mapping protocol the scoring caches actually use: ``get``,
    ``[]``, ``[]=``, ``in``, ``len``, ``keys``, ``values``, ``items``. Not a full MutableMapping
    on purpose -- an unsupported operation should fail loudly rather than silently work on a
    partial copy.
    """

    __slots__ = ("_index", "_fields", "_codes", "_cols", "_nulls", "_overflow",
                 "_shapes", "_shape_ids", "_shape_of")

    def __init__(self) -> None:
        self._index: Dict[str, int] = {}
        self._fields: List[str] = []               # UNION of every shape's fields, first-seen order
        self._codes: Dict[str, str] = {}
        self._cols: Dict[str, array] = {}
        # Per-field null mask, allocated ONLY for fields that have actually seen None (e.g.
        # skill's ``hit_rate``, which is None when no trades were scored). A NaN sentinel was
        # rejected: it cannot distinguish "absent" from a genuine NaN, and silently turning one
        # into the other is the class of bug this codebase keeps paying for.
        self._nulls: Dict[str, bytearray] = {}
        # MULTI-SHAPE. These caches legitimately hold more than one key set: a confidence entry
        # carries the two ``yearly_symbol_*`` subtotals only when the trader has actually traded
        # the symbol, so the real shard splits 234,867 x 12-field / 164,133 x 10-field. A
        # single-schema table sent 41% of entries to the overflow dict and gave back most of the
        # memory win. Storing an ordered key-tuple per row instead keeps the EXACT key set (and
        # its order) each entry was written with -- so a caller doing ``'yearly_symbol_buy_amount'
        # in stats`` still sees the truth rather than a None the table invented.
        self._shapes: List[tuple] = []
        self._shape_ids: Dict[tuple, int] = {}
        self._shape_of = bytearray()               # row -> shape id (max 256 distinct shapes)
        # Entries that cannot be stored column-wise at all (non-numeric value, or a 257th shape).
        # Kept as plain dicts so an unexpected payload degrades to the old memory profile rather
        # than corrupting or dropping data.
        self._overflow: Dict[str, Any] = {}

    # --- construction ----------------------------------------------------------------- #
    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ScoringTable":
        t = cls()
        for k, v in d.items():
            t[k] = v
        return t

    def to_dict(self) -> Dict[str, Any]:
        """Plain-dict copy. Materialises EVERY entry at once, so it undoes the memory win for as
        long as the result is held -- fine for tests and small tables; use :meth:`dump_json` for
        the persist path."""
        return {k: self[k] for k in self.keys()}

    def dump_json(self, fh) -> None:
        """Stream this table to *fh* as a JSON object, one entry at a time.

        ``json.dump(self.to_dict(), fh)`` would rebuild all ~400k value dicts simultaneously --
        a spike that defeats the point of storing them column-wise, and one that lands during
        PREWARM, which holds several shards open at once. Writing incrementally keeps peak
        memory at one entry. Output is byte-identical to dumping the equivalent plain dict.
        """
        import json as _json
        fh.write("{")
        first = True
        for k in self.keys():
            if not first:
                fh.write(",")
            first = False
            fh.write(_json.dumps(k))
            fh.write(":")
            fh.write(_json.dumps(self[k]))
        fh.write("}")

    # --- mapping protocol ------------------------------------------------------------- #
    def __len__(self) -> int:
        return len(self._index) + len(self._overflow)

    def __contains__(self, key: object) -> bool:
        return key in self._index or key in self._overflow

    def keys(self) -> Iterator[str]:
        yield from self._index.keys()
        yield from self._overflow.keys()

    def values(self) -> Iterator[Any]:
        for k in self.keys():
            yield self[k]

    def items(self) -> Iterator[tuple]:
        for k in self.keys():
            yield k, self[k]

    def __iter__(self) -> Iterator[str]:
        return self.keys()

    def get(self, key: str, default: Any = None) -> Any:
        row = self._index.get(key)
        if row is None:
            return self._overflow.get(key, default)
        return self._materialise(row)

    def __getitem__(self, key: str) -> Any:
        row = self._index.get(key)
        if row is None:
            return self._overflow[key]
        return self._materialise(row)

    def __setitem__(self, key: str, value: Any) -> None:
        if not self._storable(value):
            self._overflow[key] = value
            self._index.pop(key, None)
            return
        shape = tuple(value.keys())
        shape_id = self._shape_ids.get(shape)
        if shape_id is None:
            if len(self._shapes) >= 256:            # bytearray row->shape is 1 byte
                self._overflow[key] = value
                self._index.pop(key, None)
                return
            shape_id = len(self._shapes)
            self._shapes.append(shape)
            self._shape_ids[shape] = shape_id
            for f in shape:                         # widen the union with any new field
                if f not in self._cols:
                    self._add_field(f, value[f])

        row = self._index.get(key)
        if row is None:
            row = len(self._index)
            self._index[key] = row
            self._overflow.pop(key, None)
            self._shape_of.append(shape_id)
            for f in self._fields:
                # Fields outside THIS row's shape still occupy a slot so every column stays
                # row-aligned; they are never read back, because materialise walks the shape.
                self._append(f, value.get(f))
        else:
            self._shape_of[row] = shape_id
            for f in self._fields:
                self._write(f, row, value.get(f))

    # --- internals -------------------------------------------------------------------- #
    @staticmethod
    def _storable(value: Any) -> bool:
        """A dict whose every value is a number or None. bool is excluded deliberately: it is an
        int subclass, and round-tripping True as 1 would change a caller's ``is True`` check."""
        if type(value) is not dict or not value:
            return False
        for v in value.values():
            if v is None:
                continue
            if type(v) is bool or not isinstance(v, (int, float)):
                return False
        return True

    def _add_field(self, field: str, sample: Any) -> None:
        """Extend the union with a new field, back-filling the rows that predate it."""
        code = _INT_CODE if (sample is not None and isinstance(sample, int)) else _FLOAT_CODE
        self._fields.append(field)
        self._codes[field] = code
        existing = len(self._shape_of)
        self._cols[field] = array(code, [0] * existing)
        if existing:
            self._nulls[field] = bytearray(b"\x01" * existing)  # absent in every earlier row

    def _promote(self, field: str) -> None:
        """int64 column -> float64, in place, when a float turns up in an integral field."""
        self._codes[field] = _FLOAT_CODE
        self._cols[field] = array(_FLOAT_CODE, self._cols[field])

    def _append(self, field: str, v: Any) -> None:
        col = self._cols[field]
        if v is None:
            self._nulls.setdefault(field, bytearray(len(col)))
            self._nulls[field].append(1)
            col.append(0)
            return
        if field in self._nulls:
            self._nulls[field].append(0)
        if self._codes[field] == _INT_CODE and not isinstance(v, int):
            self._promote(field)
            col = self._cols[field]
        col.append(v)

    def _write(self, field: str, row: int, v: Any) -> None:
        col = self._cols[field]
        if v is None:
            mask = self._nulls.setdefault(field, bytearray(len(col)))
            mask[row] = 1
            col[row] = 0
            return
        if field in self._nulls:
            self._nulls[field][row] = 0
        if self._codes[field] == _INT_CODE and not isinstance(v, int):
            self._promote(field)
            col = self._cols[field]
        col[row] = v

    def _materialise(self, row: int) -> Dict[str, Any]:
        """Rebuild the entry with the EXACT key set and order it was stored with -- driven by the
        row's own shape, not the union, so a field this entry never had stays absent."""
        out = {}
        for f in self._shapes[self._shape_of[row]]:
            mask = self._nulls.get(f)
            out[f] = None if (mask is not None and mask[row]) else self._cols[f][row]
        return out

    # --- diagnostics ------------------------------------------------------------------ #
    @property
    def overflow_count(self) -> int:
        """Entries stored as plain dicts because their schema did not match. A non-zero value
        means the memory win is partial -- worth surfacing rather than hiding."""
        return len(self._overflow)

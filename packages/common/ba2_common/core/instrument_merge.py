"""Merge duplicate ``instrument`` rows so ``instrument.name`` can become unique.

Shared on purpose by BOTH the Alembic revision that runs it against the
production database and the tests that prove it correct: a migration that inlines
its own merge SQL is a migration nobody can test.

Raw SQL over a SQLAlchemy ``Connection``, not the ORM. This runs INSIDE a
migration, where the ORM ``Instrument`` model already declares ``unique=True``
while the database does not yet have the index, and where importing the app
engine would open a second connection to the wrong database.

Rows are grouped by the NORMALISED name (``.strip().upper()``), not the stored
one. A unique index alone does not stop ``aapl`` and ``AAPL`` from coexisting, and
once the label helpers normalise their lookups a leftover lower-case row is
unreachable -- orphaned data carrying labels nobody can read. Every live name is
already upper-case, so on production this grouping is a no-op.

Idempotent by construction: the plan is recomputed from the current table state on
every call, so a second run finds nothing and writes nothing.
"""
import json
from typing import Any, Dict, List

from sqlalchemy import text

from ba2_common.core.utils import normalize_symbol
from ba2_common.logger import logger

_SELECT_ROWS = text(
    "SELECT id, name, instrument_type, company_name, categories, labels "
    "FROM instrument ORDER BY id"
)
_UPDATE_ROW = text(
    "UPDATE instrument SET name = :name, instrument_type = :instrument_type, "
    "company_name = :company_name, categories = :categories, labels = :labels "
    "WHERE id = :id"
)
_DELETE_ROW = text("DELETE FROM instrument WHERE id = :id")


def _as_list(raw) -> List[str]:
    """Decode a JSON list column read through a RAW connection.

    SQLAlchemy's JSON type only decodes when its Core type is attached; a textual
    SELECT hands back the stored TEXT. Anything that is not a JSON list (NULL, an
    empty string, a stray scalar) decodes to ``[]``.
    """
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return [str(v) for v in raw]
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning(f"instrument merge: undecodable JSON value {raw!r} treated as empty")
        return []
    if not isinstance(decoded, list):
        return []
    return [str(v) for v in decoded]


def _union_preserving_order(lists) -> List[str]:
    """Concatenate lists, dropping repeats, keeping first-seen order."""
    out: List[str] = []
    for values in lists:
        for value in values:
            if value not in out:
                out.append(value)
    return out


def _first_non_null(values):
    """First value that is neither ``None`` nor an empty string, else ``None``."""
    for value in values:
        if value is not None and value != "":
            return value
    return None


def report_duplicate_instruments(connection) -> List[Dict[str, Any]]:
    """Describe every rewrite the table needs, WITHOUT writing anything.

    Args:
        connection: an open SQLAlchemy ``Connection`` (``op.get_bind()`` inside a
            migration).

    Returns:
        List[Dict[str, Any]]: one entry per group needing work, sorted by name,
        each with ``name`` (normalised), ``original_name`` (as stored on the
        keeper), ``keep_id`` (lowest id in the group), ``delete_ids``,
        ``instrument_type``, ``company_name``, ``categories`` and ``labels``
        (the merged values). Groups that are already a single correctly-named row
        are omitted, which is what makes a second run a no-op.
    """
    rows = connection.execute(_SELECT_ROWS).fetchall()
    groups: Dict[str, List[Any]] = {}
    for row in rows:
        groups.setdefault(normalize_symbol(row[1]), []).append(row)

    plan: List[Dict[str, Any]] = []
    for name in sorted(groups):
        members = groups[name]          # ordered by id, so members[0] is the keeper
        keeper = members[0]
        if len(members) == 1 and keeper[1] == name:
            continue
        plan.append({
            "name": name,
            "original_name": keeper[1],
            "keep_id": keeper[0],
            "delete_ids": [m[0] for m in members[1:]],
            "instrument_type": _first_non_null([m[2] for m in members]),
            "company_name": _first_non_null([m[3] for m in members]),
            "categories": _union_preserving_order([_as_list(m[4]) for m in members]),
            "labels": _union_preserving_order([_as_list(m[5]) for m in members]),
        })
    return plan


def merge_duplicate_instruments(connection, *, dry_run: bool = False) -> Dict[str, int]:
    """Collapse every duplicate ``instrument`` name onto its lowest id.

    For each group: keep the lowest id, coalesce ``instrument_type`` and
    ``company_name`` to the first non-null value, union ``labels`` and
    ``categories`` preserving order, delete the other rows.

    Args:
        connection: an open SQLAlchemy ``Connection``.
        dry_run: when True, compute and log the plan and write NOTHING.

    Returns:
        Dict[str, int]: ``groups`` (rows rewritten), ``duplicate_groups`` (groups
        that had more than one row), ``rows_deleted`` and ``rows_renamed`` (names
        that were not already normalised).
    """
    plan = report_duplicate_instruments(connection)
    stats = {
        "groups": len(plan),
        "duplicate_groups": sum(1 for g in plan if g["delete_ids"]),
        "rows_deleted": sum(len(g["delete_ids"]) for g in plan),
        "rows_renamed": sum(1 for g in plan if g["original_name"] != g["name"]),
    }

    if dry_run:
        logger.info(
            f"instrument merge DRY RUN: {stats['duplicate_groups']} duplicate group(s), "
            f"{stats['rows_deleted']} row(s) would be deleted, "
            f"{stats['rows_renamed']} name(s) would be normalised"
        )
        for group in plan:
            logger.info(
                f"instrument merge DRY RUN: {group['name']} keep id={group['keep_id']} "
                f"delete ids={group['delete_ids']} labels={group['labels']}"
            )
        return stats

    for group in plan:
        connection.execute(_UPDATE_ROW, {
            "id": group["keep_id"],
            "name": group["name"],
            "instrument_type": group["instrument_type"],
            "company_name": group["company_name"],
            "categories": json.dumps(group["categories"]),
            "labels": json.dumps(group["labels"]),
        })
        for dead_id in group["delete_ids"]:
            connection.execute(_DELETE_ROW, {"id": dead_id})

    logger.info(
        f"instrument merge: {stats['duplicate_groups']} duplicate group(s) merged, "
        f"{stats['rows_deleted']} row(s) deleted, {stats['rows_renamed']} name(s) normalised"
    )
    return stats

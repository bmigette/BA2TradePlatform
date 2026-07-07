"""Worker-side receiver for replicated Backtest/StrategyOptimization/Strategy rows.

Matches incoming rows by (name, created_at) — NEVER by the master's raw id, since ids aren't
stable across separate databases (see docs/plans/2026-07-07-remote-result-sync-design.md).
Cross-references (e.g. Backtest.strategy_id) arrive alongside the referenced parent's natural
key, so this module can resolve them to the WORKER's own local id instead of trusting the
master's id verbatim.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Type

from sqlalchemy import DateTime


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)


def upsert_by_natural_key(
    session: Any,
    model_cls: Type,
    payload: dict,
    parent_fk: Optional[dict[str, tuple]] = None,
) -> Optional[Any]:
    """Insert or update ``model_cls`` matched by ``(name, created_at)``.

    ``parent_fk`` maps ``{fk_column_name: (parent_model_cls, name_key, created_at_key)}``.
    For each entry, the parent's natural key is popped out of ``payload`` (it isn't a real
    column on ``model_cls``), and the parent is looked up locally by that natural key. If found,
    ``fk_column_name`` is set to the parent's LOCAL id. If not found (parent hasn't synced yet):
    a NULLABLE fk column resolves to None and the row is still written (e.g. a standalone
    Backtest legitimately has no optimization); a NOT NULL fk column (e.g.
    StrategyOptimization.strategy_id) can't take None without violating the schema, so the
    entire write is skipped instead — the caller gets None back, and the next full-state push
    (e.g. the next generation) retries the parent first and self-heals.

    Returns the local row (existing, updated in place, or newly inserted), or None if the write
    was skipped because a required parent hasn't synced yet. Does not raise for an unresolved
    parent — that's the expected, self-healing case. A genuinely malformed payload (wrong type,
    missing a required non-FK column, etc.) still raises, since that indicates a real bug on the
    sender's side that should surface rather than be silently swallowed.
    """
    data = dict(payload)
    data.pop("id", None)  # never trust the master's id

    # Every DateTime-typed column (created_at, start_date, end_date, ...) arrives over the wire
    # as an ISO string — coerce all of them up front, not just the natural-key's created_at.
    for col in model_cls.__table__.columns:
        if isinstance(col.type, DateTime) and isinstance(data.get(col.name), str):
            data[col.name] = _parse_iso(data[col.name])

    name = data.get("name")
    created_at = data.get("created_at")

    if parent_fk:
        for fk_col, (parent_cls, name_key, created_key) in parent_fk.items():
            parent_name = data.pop(name_key, None)
            parent_created = _parse_iso(data.pop(created_key, None))
            resolved_id = None
            if parent_name is not None and parent_created is not None:
                parent = (
                    session.query(parent_cls)
                    .filter(parent_cls.name == parent_name, parent_cls.created_at == parent_created)
                    .first()
                )
                resolved_id = parent.id if parent else None
            if resolved_id is None and not model_cls.__table__.columns[fk_col].nullable:
                # Required parent not yet synced to this worker — skip rather than violate
                # the NOT NULL constraint. Nothing is lost: the next full-state push retries.
                return None
            data[fk_col] = resolved_id

    # Drop any key that isn't a real column on model_cls. Without this, the insert path
    # (model_cls(**data)) raises TypeError on an unknown key while the update path
    # (setattr(existing, key, value)) would silently set an inert, never-persisted attribute for
    # the exact same malformed payload — filtering uniformly here keeps both paths behaving the
    # same way for the same input.
    valid_columns = set(model_cls.__table__.columns.keys())
    data = {key: value for key, value in data.items() if key in valid_columns}

    existing = (
        session.query(model_cls)
        .filter(model_cls.name == name, model_cls.created_at == created_at)
        .first()
    )
    if existing is not None:
        for key, value in data.items():
            if key in ("name", "created_at"):
                continue
            setattr(existing, key, value)
        session.commit()
        return existing

    row = model_cls(**data)
    session.add(row)
    session.commit()
    return row

"""Generic event/record cache index — generalizes BA2TestPlatform NewsCache.

Time-series data (ohlcv, indicators) lives in parquet (see ba2_providers cache);
event/record data (insider, fundamentals, news, estimates) is indexed here with
large payloads spilled to disk JSON. EVERY row carries effective_date (when the
datum became PUBLIC) distinct from value_date (what the datum is ABOUT).

The DB is host-owned (Amendment A4): Phase 1 ships only this model + the existing
``db.init_db()`` ``create_all`` registration. The BA2TestPlatform migrate_db
revision that lets the host consume this table lands in Phase 2 — there is NO
migrator inside the packages.
"""
from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field, Index


def _utcnow() -> datetime:
    """tz-aware UTC now (datetime.utcnow is deprecated and naive)."""
    return datetime.now(timezone.utc)


class ProviderCache(SQLModel, table=True):
    __tablename__ = "provider_cache"

    id: Optional[int] = Field(default=None, primary_key=True)
    provider: str = Field(index=True)            # e.g. FMPInsiderProvider
    data_type: str = Field(index=True)           # insider_txn|balance_sheet|income_stmt|...
    symbol: str = Field(index=True)
    frequency: Optional[str] = Field(default=None)   # quarterly/annual/None
    value_date: datetime                          # fiscalDateEnding / transactionDate / event time
    effective_date: datetime                      # fillingDate / filingDate / publishedDate / report date
    payload_hash: str = Field(index=True)         # sha256 of canonical payload (dedupe key)
    content_file_path: Optional[str] = Field(default=None)   # spill for large payloads
    raw_json: Optional[str] = Field(default=None)            # inline for small payloads
    fetched_at: datetime = Field(default_factory=_utcnow)

    __table_args__ = (
        Index("ix_provcache_lookup", "provider", "data_type", "symbol", "effective_date"),
        Index("ix_provcache_value", "data_type", "symbol", "value_date"),
        Index("ix_provcache_dedupe", "provider", "data_type", "symbol", "payload_hash", unique=True),
    )


def create_all(engine=None) -> None:
    """Host-callable create_all helper (Amendment A4: host owns the migration).

    Phase 1 ships the model + this helper; ``db.init_db()`` already calls
    ``SQLModel.metadata.create_all`` after importing models, so importing this
    module before ``init_db()`` is enough to register the table. This helper exists
    so a host (or a test) can create just the provider_cache table against a given
    engine without importing the whole models module.
    """
    if engine is None:
        from ba2_common.core.db import get_engine
        engine = get_engine()
    SQLModel.metadata.create_all(engine, tables=[ProviderCache.__table__])

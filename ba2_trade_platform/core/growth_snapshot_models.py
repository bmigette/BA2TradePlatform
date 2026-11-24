"""Stored copies of slow reads, so the Account Growth tab and the Portfolio Allocation page paint at once.

LIVE-ONLY and deliberately IN-TREE (not in ``ba2_common``), for the same reason as
``allocator_protection_models``: they hold what the LIVE pages last read from a broker and from FMP,
nothing the backtest engine or the GA reads, so the feature needs no ``PACKAGE_VERSION`` bump.

Registration with the shared ``SQLModel.metadata`` is the job of whoever imports this module first:
``main.initialize_system`` does it BEFORE ``init_db()`` (so ``create_all`` builds the tables on any
database), ``alembic/env.py`` does it for autogenerate, and ``services.growth_snapshot`` imports it.

TWO tables:

* ``account_growth_snapshot``: per ``(account_id, dataset)``. ``dataset`` is ``"growth"`` (the four
  broker datasets the growth tab draws from, written together after a refresh in which all four were
  good) or ``"dividends"`` (the latest dividend rows, read by the allocator page). ``payload`` is the
  codec's output (``services.growth_snapshot``); a row whose ``schema_version`` is not
  ``GROWTH_SNAPSHOT_SCHEMA_VERSION`` is treated as absent.
* ``symbol_dividend_history``: per symbol, the RAW FMP ``stock_dividend`` payload (parsed at read time,
  so a parser change needs no refetch). An empty payload (a non-payer) is stored too, so a cold start
  does not ask FMP again for every symbol that pays nothing.

Every ``fetched_at`` is UTC: when the broker / FMP answered, not when the row was written.
"""
from datetime import datetime as DateTime, timezone
from typing import Any

from sqlalchemy import JSON
from sqlmodel import Column, Field, SQLModel


def _utcnow() -> DateTime:
    return DateTime.now(timezone.utc)


#: Bumped whenever the payload layout changes; a stored row with another version is ignored (refetched).
GROWTH_SNAPSHOT_SCHEMA_VERSION = 1

DATASET_GROWTH = "growth"
DATASET_DIVIDENDS = "dividends"


class AccountGrowthSnapshot(SQLModel, table=True):
    """The last good broker read of one account, for one dataset (``DATASET_GROWTH`` | ``DATASET_DIVIDENDS``)."""
    __tablename__ = "account_growth_snapshot"

    account_id: int = Field(foreign_key="accountdefinition.id", ondelete="CASCADE", primary_key=True)
    dataset: str = Field(primary_key=True, description="'growth' | 'dividends' (plain str)")
    fetched_at: DateTime = Field(description="UTC: when the broker rows were fetched")
    payload: Any = Field(sa_column=Column(JSON, nullable=False), description="The codec's encoded rows")
    schema_version: int = Field(description="GROWTH_SNAPSHOT_SCHEMA_VERSION at write time")
    updated_at: DateTime = Field(default_factory=_utcnow)


class SymbolDividendHistory(SQLModel, table=True):
    """One symbol's declared dividend history as FMP returned it (raw; empty for a non-payer)."""
    __tablename__ = "symbol_dividend_history"

    symbol: str = Field(primary_key=True, description="Upper-cased symbol")
    fetched_at: DateTime = Field(description="UTC: when FMP answered")
    payload: Any = Field(sa_column=Column(JSON, nullable=True), description="Raw FMP stock_dividend payload")

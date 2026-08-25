"""Scoping a ``Transaction`` query to ONE ACCOUNT.

WHY THIS EXISTS. The header's account dropdown used to reach the dashboard's
transaction queries through ``get_expert_ids_for_account`` -- the widget asked
"which experts belong to the selected account?" and then filtered
``Transaction.expert_id.in_(those_ids)``. That is the right question for a
per-expert figure and the WRONG one for anything account-level, because it
silently conflates two different statements:

    "this account has no experts"   !=   "this account has no data"

A manual account (TastyTrade, or a hand-traded Alpaca account) has zero
``ExpertInstance`` rows, so the expert-id list is ``[]`` and the query matched
nothing -- the widgets rendered a row of zeros over real money. The same filter
also dropped hand-placed trades on EXPERT-driven accounts: the production
database has open transactions with ``expert_id IS NULL`` sitting on accounts
that do have experts, and those were invisible too.

A ``Transaction`` carries no ``account_id`` of its own; its account is the
account of its ``TradingOrder`` rows. So the scope is a subquery, and it lives
here -- in one place, used by ``ui/pages/overview.py`` and by
``FloatingPLPerAccountWidget`` -- rather than being re-derived per widget.

THE TWO RULES THAT MUST NOT DRIFT:

1. ``account_id is None`` means the header says "All". That -- and ONLY that --
   widens the query to every account.
2. An account with no matching transactions yields NO ROWS. Never fall back to
   "match everything" on an empty result: the moment an account legitimately has
   nothing, that would show every other account's money under its name.
"""
from typing import Optional

from sqlmodel import select

from ...core.models import Transaction, TradingOrder


def account_transaction_ids(account_id: int):
    """The ids of every transaction that has at least one order on *account_id*.

    Returned as a SELECT for use inside ``IN (...)``, not as a materialised list:
    the caller's query stays a single round trip, and an account with thousands of
    transactions does not turn into a thousand-element bind-parameter list.

    ``transaction_id IS NOT NULL`` is deliberately BELT-AND-BRACES and is the one
    line here that no test can kill: under ``IN``, a NULL in the candidate set makes
    the comparison UNKNOWN, which a WHERE clause discards exactly as it discards
    FALSE -- so removing it is a provably equivalent mutant TODAY. It stays because
    the equivalence is a property of ``IN``, not of this query: the day someone
    writes ``NOT IN`` (to ask "transactions NOT on this account") a single NULL
    would silently return the empty set.
    """
    return (
        select(TradingOrder.transaction_id)
        .where(TradingOrder.account_id == account_id)
        .where(TradingOrder.transaction_id.isnot(None))
    )


def scope_transactions_to_account(query, account_id: Optional[int]):
    """Restrict a ``Transaction`` query to the transactions of one account.

    Args:
        query: any select whose FROM includes ``Transaction`` (a ``select(Transaction)``
            or an aggregate such as ``select(func.count(Transaction.id))``).
        account_id: the selected account, or ``None`` for the header's "All".

    Returns:
        The query, narrowed. ``None`` returns it untouched -- that is "All", and it
        is the only input that widens the result set.
    """
    if account_id is None:
        return query
    return query.where(Transaction.id.in_(account_transaction_ids(account_id)))

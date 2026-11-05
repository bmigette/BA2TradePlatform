"""Scoping a ``Transaction`` query to ONE ACCOUNT. The implementation (and the two rules that must not drift) now
lives in ``ba2_trade_platform.services.account_scope`` so the management API and the Overview widgets share it;
this module re-exports it for the UI."""
from ...services.account_scope import account_transaction_ids, scope_transactions_to_account

__all__ = ["account_transaction_ids", "scope_transactions_to_account"]

"""End-to-end FactorRanker verification on the BA2NewStrat account — NO ORDERS.

This script runs every FactorRanker ``ExpertInstance`` on the BA2NewStrat account
against **real FMP data** (verifying the data adapters + StockScreener end-to-end),
but it **places no orders and creates no Transactions**. It does this by
monkeypatching ``FactorPortfolioManager.rebalance`` with a dry-run that computes the
would-be share deltas (via ``get_holdings`` + ``rebalance_deltas``) and returns them
without ever touching the broker or the DB.

For each instance it creates a PENDING ``MarketAnalysis`` (symbol="EXPERT",
subtype=ENTER_MARKET), calls ``run_analysis``, then prints the resolved universe
size, held count, the top-10 ranking rows, and the captured (un-placed) trades.
It exits non-zero if any instance ended FAILED or produced an empty universe.

Run (read-only against the broker, real FMP):
    .venv/bin/python test_files/e2e_factorranker.py
"""

import sys
from typing import Dict, List, Optional

from sqlmodel import select

from ba2_trade_platform.core.db import add_instance, get_db
from ba2_trade_platform.core.models import (
    AccountDefinition, ExpertInstance, MarketAnalysis,
)
from ba2_trade_platform.core.types import AnalysisUseCase, MarketAnalysisStatus
from ba2_trade_platform.core.utils import get_expert_instance_from_id
from ba2_trade_platform.logger import logger
from ba2_trade_platform.modules.experts.FactorRanker import portfolio as fr_portfolio
from ba2_trade_platform.modules.experts.FactorRanker.portfolio import (
    FactorPortfolioManager, rebalance_deltas,
)

ACCOUNT_NAME = "BA2NewStrat"

# Captured would-be deltas from the dry-run rebalance, keyed by expert_instance_id.
# The patched rebalance writes here so this script can read the intended (un-placed)
# trades after run_analysis completes.
CAPTURED_DELTAS: Dict[int, Dict[str, float]] = {}


# ---------------------------------------------------------------------------
# Order-free dry-run rebalance (monkeypatched onto FactorPortfolioManager)
# ---------------------------------------------------------------------------

def _dry_run_rebalance(self, target_weights: Dict[str, float],
                       equity: Optional[float] = None) -> Dict[str, float]:
    """Drop-in replacement for ``FactorPortfolioManager.rebalance`` that computes the
    rebalance deltas but submits NO orders and creates NO Transactions.

    Mirrors the real ``rebalance`` math (get_holdings -> price targets ->
    rebalance_deltas) but returns the deltas instead of submitting them, and stashes
    them in ``CAPTURED_DELTAS`` keyed by this manager's expert_instance_id.
    """
    held, _ = self.get_holdings()
    symbols = set(target_weights) | set(held)
    prices = {s: self.account.get_instrument_current_price(s) for s in symbols}

    if equity is None:
        equity = self.expert.get_virtual_balance()
    equity = equity or 0.0

    deltas = rebalance_deltas(target_weights, held, prices, equity)

    CAPTURED_DELTAS[self.expert_instance_id] = deltas
    logger.info(
        f"FactorRanker[{self.expert_instance_id}]: DRY-RUN rebalance — NO orders "
        f"submitted (equity={equity:.2f}, would-be deltas={deltas})"
    )
    return deltas


# Patch the class attribute so every FactorPortfolioManager.rebalance() is a dry-run.
FactorPortfolioManager.rebalance = _dry_run_rebalance


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_account_id(name: str) -> int:
    with get_db() as session:
        account = session.exec(
            select(AccountDefinition).where(AccountDefinition.name == name)
        ).first()
        if account is None:
            raise SystemExit(f"Account '{name}' not found")
        return account.id


def _list_factorranker_instances(account_id: int) -> List[tuple]:
    """Return [(id, alias)] for FactorRanker instances on the account, ordered by id."""
    with get_db() as session:
        instances = session.exec(
            select(ExpertInstance)
            .where(ExpertInstance.account_id == account_id)
            .where(ExpertInstance.expert == "FactorRanker")
            .order_by(ExpertInstance.id)
        ).all()
        return [(inst.id, inst.alias or inst.expert) for inst in instances]


def _book_status(ma: MarketAnalysis, book: dict) -> str:
    """Derive a short status label for the per-instance report."""
    if not book:
        return ma.status.value if ma.status else "UNKNOWN"
    if book.get("failed"):
        return "FAILED"
    if book.get("skipped"):
        return "SKIPPED"
    return ma.status.value if ma.status else "COMPLETED"


def _print_instance_report(iid: int, alias: str, ma: MarketAnalysis) -> dict:
    """Print the per-instance report and return a summary row dict."""
    book = (ma.state or {}).get("factor_ranker") or {}
    status = _book_status(ma, book)
    universe_size = book.get("universe_size", 0)
    held_count = book.get("held_count", 0)

    print("=" * 78)
    print(f"Instance {iid}: {alias}")
    print(f"  MarketAnalysis id={ma.id}  status={ma.status.value if ma.status else 'N/A'}")
    print(f"  state status        : {status}")

    if book.get("skipped"):
        print(f"  SKIPPED             : {book.get('reason', 'no reason given')}")
    elif book.get("failed"):
        print(f"  FAILED              : {book.get('error', 'no error given')}")
    else:
        print(f"  universe_size       : {universe_size}")
        print(f"  held_count          : {held_count}")
        print(f"  gross_exposure      : {book.get('gross_exposure')}")
        print(f"  factor weights      : {book.get('weights')}")

        ranking = book.get("ranking") or []
        print(f"  top-10 ranking (symbol / composite / target_weight):")
        if not ranking:
            print("    (empty)")
        for row in ranking[:10]:
            print(
                f"    {row.get('rank'):>3}. {row.get('symbol'):<8} "
                f"composite={row.get('composite'):>9}  "
                f"target_weight={row.get('target_weight'):>7}  "
                f"{row.get('action', '')}"
            )

    deltas = CAPTURED_DELTAS.get(iid)
    print(f"  would-be trades (dry-run deltas — NO orders placed):")
    if deltas is None:
        print("    (rebalance not reached)")
    elif not deltas:
        print("    (none — book already matches targets)")
    else:
        for sym, delta in sorted(deltas.items()):
            verb = "BUY " if delta > 0 else "SELL"
            print(f"    {verb} {abs(int(delta)):>6} {sym}")

    return {
        "id": iid,
        "alias": alias,
        "status": status,
        "universe_size": universe_size,
        "held_count": held_count,
        "would_be_trades": 0 if deltas is None else len(deltas),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    account_id = _resolve_account_id(ACCOUNT_NAME)
    print(f"Account '{ACCOUNT_NAME}' resolved to id={account_id}")
    print("FactorPortfolioManager.rebalance is patched to DRY-RUN — NO orders will be placed.\n")

    instances = _list_factorranker_instances(account_id)
    if not instances:
        print(f"No FactorRanker instances found on account '{ACCOUNT_NAME}' (id={account_id}).")
        return 1
    print(f"Found {len(instances)} FactorRanker instance(s) on '{ACCOUNT_NAME}'.\n")

    summary: List[dict] = []
    failures: List[str] = []

    for iid, alias in instances:
        ma = MarketAnalysis(
            symbol="EXPERT",
            expert_instance_id=iid,
            subtype=AnalysisUseCase.ENTER_MARKET,
            status=MarketAnalysisStatus.PENDING,
        )
        add_instance(ma)

        try:
            expert = get_expert_instance_from_id(iid, use_cache=False)
            expert.run_analysis("EXPERT", ma)
        except Exception as e:
            # run_analysis re-raises on failure after marking the MA FAILED; the
            # report below reads ma.state, so just log and continue.
            logger.error(f"FactorRanker[{iid}] run_analysis raised: {e}", exc_info=True)

        row = _print_instance_report(iid, alias, ma)
        summary.append(row)

        if row["status"] == "FAILED" or ma.status == MarketAnalysisStatus.FAILED:
            failures.append(f"{iid} ({alias}): FAILED")
        elif not (ma.state or {}).get("factor_ranker", {}).get("skipped") \
                and row["universe_size"] == 0:
            failures.append(f"{iid} ({alias}): empty universe")
        print()

    # Summary table
    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"{'id':>4}  {'alias':<26} {'status':<10} {'univ':>5} {'held':>5} {'trades':>7}")
    print("-" * 78)
    for row in summary:
        print(
            f"{row['id']:>4}  {row['alias'][:26]:<26} {row['status']:<10} "
            f"{row['universe_size']:>5} {row['held_count']:>5} {row['would_be_trades']:>7}"
        )
    print("-" * 78)

    if failures:
        print(f"\n{len(failures)} instance(s) failed verification:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nAll instances ran (zero orders placed).")
    return 0


# Optional speed-up (Part E): if FMP rate limits bite with many instances, the
# per-universe data.py fetchers (fetch_close_prices / fetch_value_inputs /
# fetch_quality_inputs / fetch_pead_inputs) could be memoized per-universe so each
# symbol is fetched once and reused across instances sharing a universe. Not
# implemented here as it is non-trivial (keying on the exact universe set) — add it
# only if rate limits actually become a problem.


if __name__ == "__main__":
    sys.exit(main())

"""``GET /stats/overview``: the Overview page's figures as one payload.

Every figure comes from the SAME function the Overview widget calls (``stats``, ``floating_pl``, ``balance_usage``,
``accounts``); this module only composes them. Sections that need the broker report their own ``status`` so one
unreadable account does not blank the others and nothing reads as 0 when it could not be measured:

* ``status: "ok"``           -- every account in scope was read;
* ``status: "partial"``      -- some were not; ``unmeasured`` names them and the totals exclude them;
* ``status: "unmeasurable"`` -- nothing could be read (``error`` / ``message`` say why).

A GET: no broker write, no sync, no buying-power failure memory touched (``readonly_balance_reads``)."""
from typing import Any, Callable, Dict, List, Optional

from sqlmodel import select

from ..core.db import get_db
from ..core.models import AccountDefinition, ExpertInstance
from ..logger import logger
from . import accounts as accounts_svc
from . import balance_usage, floating_pl, stats
from ._common import jsonable, require_row, utcnow_iso
from ._positions_cache import share_positions
from ._readonly import readonly_balance_reads
from .errors import ServiceError, Unmeasurable


def _accounts_in_scope(account_id: Optional[int]) -> List[Dict[str, Any]]:
    with get_db() as session:
        if account_id is not None:
            require_row(session, AccountDefinition, account_id, "account")
        rows = session.exec(select(AccountDefinition).order_by(AccountDefinition.id)).all()
        return [{"id": a.id, "name": a.name} for a in rows if account_id is None or a.id == account_id]


def _failed(error: ServiceError) -> Dict[str, Any]:
    return {"status": "unmeasurable", "error": error.code, "message": error.message}


def _section(name: str, fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    """Run one broker-backed section in isolation: whatever it raises (a typed service error, an unbuildable account,
    an odd broker payload) becomes THAT section's ``{"status": "unmeasurable", "error", "message"}`` while the others
    still render. ``message`` is the error CLASS name for anything that is not a typed service error (its text can echo
    broker data); the full error goes to the log."""
    try:
        return {"status": "ok", **fn()}
    except ServiceError as e:
        logger.warning(f"API: overview section {name} failed: {e.code}: {e.message}")
        return _failed(e)
    except Exception as e:   # noqa: BLE001 -- deliberate: section isolation, see the docstring
        logger.error(f"API: overview section {name} failed: {type(e).__name__}: {e}", exc_info=True)
        return {"status": "unmeasurable", "error": "internal", "message": type(e).__name__}


def _pl_rows(rows: List[floating_pl.PLRow]) -> Dict[str, Any]:
    total, unreadable = floating_pl.combine_measurements([(r.name, r.pl) for r in rows])
    understated = [r.name for r in rows if r.unpriced]
    out_rows = [{"name": r.name, "pl": r.pl, "pl_status": "unmeasured" if r.pl is None else "measured",
                 "unpriced_symbols": list(r.unpriced), "balance": r.balance,
                 "tradable_balance": r.tradable, "broker_buying_power": r.broker_bp} for r in rows]
    out_rows.sort(key=lambda r: (r["pl"] is None, -(r["pl"] if r["pl"] is not None else 0.0), r["name"]))
    status = "ok" if not unreadable else ("partial" if total is not None else "unmeasurable")
    return {"status": status, "rows": out_rows, "total": total, "unmeasured": unreadable,
            "understated": understated}


def _floating_pl(account_id: Optional[int]) -> Dict[str, Any]:
    with readonly_balance_reads():
        return {"per_account": _pl_rows(floating_pl.floating_pl_per_account(account_id)),
                "per_expert": _pl_rows(floating_pl.floating_pl_per_expert(account_id))}


def _balance_usage(account_id: Optional[int]) -> Dict[str, Any]:
    with readonly_balance_reads():
        rows = balance_usage.balance_usage_per_expert(account_id)
    with get_db() as session:
        query = select(ExpertInstance).where(ExpertInstance.enabled == True)  # noqa: E712 -- SQL
        ids = stats.expert_ids_for_account(account_id)
        if ids is not None:
            query = query.where(ExpertInstance.id.in_(ids)) if ids else query.where(ExpertInstance.id == -1)
        wanted = {stats.expert_display_name(e) for e in session.exec(query).all()}
    missing = sorted(wanted - set(rows))
    unpriced = [{"expert": name, "symbols": row["unpriced_symbols"]} for name, row in rows.items()
                if row["unpriced_symbols"]]
    out: Dict[str, Any] = {
        "experts": [{"expert": name, **{k: v for k, v in row.items() if k != "unpriced_symbols"},
                     "unpriced_symbols": row["unpriced_symbols"]} for name, row in rows.items()],
        "summary": balance_usage.summarize_capital(rows) if rows else None,
        "unmeasured_experts": missing, "understated": unpriced,
    }
    if missing:
        out["status"] = "partial" if rows else "unmeasurable"
    return out


def _allocation(accounts: List[Dict[str, Any]]) -> Dict[str, Any]:
    positions: List[Dict[str, Any]] = []
    unreachable: List[str] = []
    for acc in accounts:
        try:
            for pos in accounts_svc.broker_positions(acc["id"])["items"]:
                positions.append(dict(pos))
        except ServiceError as e:
            logger.warning(f"API: overview allocation: account {acc['id']}: {e.code}")
            unreachable.append(acc["name"])
    skipped: List[Dict[str, Any]] = []

    def shaped(grouping: str) -> List[Dict[str, Any]]:
        dist = stats.position_distribution(positions, grouping, skipped if grouping == "labels" else None)
        total = sum(dist.values())
        return [{"name": n, "value": v, "percent": (v / total * 100.0) if total else None} for n, v in dist.items()]
    if unreachable and not positions and accounts and len(unreachable) == len(accounts):
        raise Unmeasurable("No account's positions could be read", {"unreachable_accounts": unreachable})
    out = {"by_label": shaped("labels"), "by_category": shaped("categories"), "unmeasured": unreachable,
           "positions_included": len(positions) - len(skipped),
           "skipped": {"count": len(skipped), "positions": skipped},
           "percent_basis": "market value of the included positions only (skipped and unreachable ones are not in the denominator)"}
    if unreachable or skipped:
        out["status"] = "partial"
    return out


def _exposure(accounts: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows, failed = [], []
    with readonly_balance_reads():
        for acc in accounts:
            try:
                snap = accounts_svc.account_snapshot(acc["id"])
            except ServiceError as e:
                rows.append({"account_id": acc["id"], "account": acc["name"], "status": "unmeasurable",
                             "error": e.code, "message": e.message})
                failed.append(acc["name"])
                continue
            rows.append({"account_id": acc["id"], "account": acc["name"], "status": "ok",
                         **{k: snap[k] for k in ("as_of", "equity", "gross_exposure", "long_market_value",
                                                 "short_market_value", "pending_entries", "headroom",
                                                 "margin_enabled", "effective_factor", "buying_power")}})
    out: Dict[str, Any] = {"accounts": rows, "unmeasured": failed}
    if failed:
        out["status"] = "partial" if len(failed) < len(rows) else "unmeasurable"
    return out


def stats_overview(account_id: Optional[int]) -> Dict[str, Any]:
    accounts = _accounts_in_scope(account_id)
    perf = stats.trade_performance(account_id, strict=True)
    skipped_profit: List[Dict[str, Any]] = []
    profits = stats.profit_per_expert(account_id, skipped_profit)
    with share_positions():      # ONE get_positions per account for every section of this request (15 s)
        sections = {
            "balance_usage": _section("balance_usage", lambda: _balance_usage(account_id)),
            "floating_pl": _section("floating_pl", lambda: _floating_pl(account_id)),
            "allocation": _section("allocation", lambda: _allocation(accounts)),
            "exposure": _section("exposure", lambda: _exposure(accounts)),
        }
    return jsonable({
        "as_of": utcnow_iso(),
        "account": account_id,
        "trade_performance": perf,
        "orders": stats.order_counts(account_id),
        "profit_per_expert": {"experts": [{"expert": n, "profit": p} for n, p in profits.items()],
                              "skipped": {"count": len(skipped_profit), "transactions": skipped_profit}},
        **sections,
    })

"""Read-only TastyTrade probe for the allocator TP/SL supervised test (step 3).

Answers three questions about the REAL broker without placing, changing or cancelling anything:

1. Does the tag search find a given tag (and without an HTTP 400 for the ``start-at`` filter)?
2. In which order do the PLAIN order history (asked ``sort=Desc, start-at=...``) and the COMPLEX order
   history come back (newest first or oldest first), and what is the ``received_at`` of page 0?
3. Do the legs of an OCO show up in the plain history carrying a ``complex_order_id``?

Usage (from the repo root, with the app's venv, on the machine that holds the app database)::

    .venv\\Scripts\\python.exe tools/tt_protection_probe.py --account-id 2 --tag ba2prot:7:0:1a2b3c4d
    .venv\\Scripts\\python.exe tools/tt_protection_probe.py --account-id 2 --since-minutes 600 --db-file D:\\path\\db.sqlite

It only calls read endpoints (``get_*``). Every other method of the broker object is blocked by a guard that
raises, so a typo cannot place or cancel an order. Nothing is written to the database.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

READ_PREFIX = "get_"


class ReadOnly:
    """Wraps the SDK account object: only ``get_*`` calls pass, anything else raises."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        if name.startswith("_") or name == "account_number":
            return getattr(self._inner, name)
        if not name.startswith(READ_PREFIX):
            raise PermissionError(f"tt_protection_probe is read-only: '{name}' is blocked")
        return getattr(self._inner, name)


def stamp(item):
    value = getattr(item, "received_at", None) or getattr(item, "updated_at", None)
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def ordering(stamps):
    stamps = [s for s in stamps if s is not None]
    if len(stamps) < 2:
        return "unknown (fewer than 2 rows)"
    if stamps[0] > stamps[-1]:
        return "NEWEST FIRST"
    if stamps[0] < stamps[-1]:
        return "OLDEST FIRST"
    return "unknown (all equal)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--tag", help="an external id (ba2prot:<id>:<n>:<8hex>) to search for")
    parser.add_argument("--since-minutes", type=int, default=120, help="how far back the plain history is asked")
    parser.add_argument("--kind", choices=["OCO", "STOP"], help="restrict the tag search to one kind")
    parser.add_argument("--db-file", help="the app database (default: the app's own)")
    args = parser.parse_args()
    if args.db_file:
        os.environ["DB_FILE"] = args.db_file

    from ba2_trade_platform.core.utils import get_account_instance_from_id
    account = get_account_instance_from_id(args.account_id)
    if account is None or getattr(account, "supports_allocator_protection", False) is not True:
        print(f"Account {args.account_id} is not a TastyTrade account with protection support.")
        return 2
    if not account._check_authentication():
        print("Not authenticated with TastyTrade.")
        return 2
    broker = ReadOnly(account._account)
    session = account._session
    since = datetime.now(timezone.utc) - timedelta(minutes=args.since_minutes)
    print(f"Probe at {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC, history back to {since:%Y-%m-%d %H:%M} UTC")

    print("\n[2a] PLAIN order history, sort=Desc, start-at=" + since.isoformat())
    plain = list(account._run_async(broker.get_order_history(
        session, per_page=50, page_offset=0, sort="Desc", start_at=since.isoformat())))
    stamps = [stamp(o) for o in plain]
    print(f"  rows on page 0: {len(plain)}; received_at first={stamps[0] if stamps else None} "
          f"last={stamps[-1] if stamps else None}")
    print(f"  ordering: {ordering(stamps)}")
    older = [s for s in stamps if s is not None and s < since]
    print(f"  rows older than start-at: {len(older)} (should be 0)")

    print("\n[2b] COMPLEX order history, page 0 (no sort/filter exists)")
    complex_rows = list(account._run_async(broker.get_complex_order_history(session, per_page=50, page_offset=0)))
    cstamps = [min([s for s in (stamp(m) for m in (getattr(c, "orders", None) or [])) if s is not None] or [None])
               if getattr(c, "orders", None) else None for c in complex_rows]
    print(f"  rows on page 0: {len(complex_rows)}; first={cstamps[0] if cstamps else None} "
          f"last={cstamps[-1] if cstamps else None}")
    print(f"  ordering: {ordering(cstamps)}")

    print("\n[3] OCO legs in the PLAIN history")
    legs = [o for o in plain if getattr(o, "complex_order_id", None) not in (None, 0)]
    print(f"  plain rows carrying a complex_order_id: {len(legs)} of {len(plain)}")
    for o in legs[:5]:
        print(f"    order {o.id} complex {o.complex_order_id} {getattr(o, 'underlying_symbol', '')} "
              f"tag={getattr(o, 'external_identifier', None)}")

    if args.tag:
        print(f"\n[1] tag search for {args.tag}")
        try:
            found = account.find_protective_orders_by_tag(args.tag, since=since, kind=args.kind)
        except Exception as e:  # noqa: BLE001 -- the point is to SEE the failure (a 400 here is a finding)
            print(f"  SEARCH FAILED: {type(e).__name__}: {e}")
            return 1
        if not found:
            print("  nothing found")
        for kind, broker_id, wrapper in found:
            members = getattr(wrapper, "orders", None) or []
            print(f"  FOUND {kind} {broker_id}: " + "; ".join(
                f"{getattr(m, 'underlying_symbol', '?')} size={getattr(m, 'size', '?')} "
                f"status={getattr(getattr(m, 'status', None), 'value', getattr(m, 'status', '?'))} "
                f"received_at={stamp(m)} external_id={getattr(m, 'external_identifier', None)}" for m in members))
    print("\nDone. Nothing was placed, changed or cancelled.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

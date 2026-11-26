"""Read every account in scope in parallel, each under a deadline: the scope rule and the thread fan-out shared by
the Overview heatmap (``services.position_heatmap``) and the Live Trades "Live View" (``services.live_positions``).

Each account runs in its own daemon thread; the caller waits at most ``deadline_s`` for ALL of them together (they
run at the same time, so a dead account bounds the wait, it does not add to it). An account that has not answered by
then is TIMED_OUT and its thread is returned still running (it finishes on its own; its answer is dropped)."""
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from sqlmodel import select

from ..core.db import get_db

#: One account's broker read(s).
BROKER_DEADLINE_S = 15.0

OK = "ok"
FAILED = "failed"
TIMED_OUT = "timed out"


@dataclass
class Outcome:
    status: str
    value: Any = None
    error: Optional[BaseException] = None


def accounts_in_scope(account_id: Optional[int]) -> List[int]:
    """The header's account, or (``None`` = All) every ``AccountDefinition`` -- the rule the app already uses for
    "which accounts are live": the scheduled account refresh (``TradeManager.refresh_accounts``, every 5 minutes)
    instantiates and refreshes every account definition, and the Overview's other "All" tabs read them all. There is
    no enabled / active flag on an account. The accounts are read in parallel, so a dead one bounds the load at
    ``BROKER_DEADLINE_S``; it does not add to it."""
    if account_id is not None:
        return [account_id]
    from ..core.models import AccountDefinition
    with get_db() as session:
        return sorted(session.exec(select(AccountDefinition.id)).all())


def account_names(account_ids: Sequence[int]) -> Dict[int, str]:
    """``{id: AccountDefinition.name}`` for the ids that exist."""
    if not account_ids:
        return {}
    from ..core.models import AccountDefinition
    with get_db() as session:
        rows = session.exec(select(AccountDefinition.id, AccountDefinition.name)
                            .where(AccountDefinition.id.in_(list(account_ids)))).all()
    return {aid: name for aid, name in rows}


def fan_out(account_ids: Sequence[int], work: Callable[[int], Any], deadline_s: float,
            thread_name: str) -> Tuple[Dict[int, Outcome], Dict[int, threading.Thread]]:
    """``work(account_id)`` for every account at once, ``deadline_s`` for all of them. Returns ``({account: Outcome},
    {account: thread still running})``: OK with ``work``'s value, FAILED with what it raised, TIMED_OUT."""
    lock = threading.Lock()
    done: Dict[int, Outcome] = {}

    def run(aid):
        try:
            value = work(aid)
            with lock:
                done[aid] = Outcome(OK, value=value)
        except BaseException as e:  # noqa: BLE001 -- reported per account to the caller
            with lock:
                done[aid] = Outcome(FAILED, error=e)

    threads = {aid: threading.Thread(target=run, args=(aid,), daemon=True, name=f"{thread_name}{aid}")
               for aid in account_ids}
    for t in threads.values():
        t.start()
    ends = time.monotonic() + deadline_s
    for t in threads.values():
        t.join(max(0.0, ends - time.monotonic()))
    with lock:
        outcomes = {aid: done[aid] if aid in done else Outcome(TIMED_OUT) for aid in threads}
    alive = {aid: t for aid, t in threads.items() if outcomes[aid].status == TIMED_OUT}
    return outcomes, alive

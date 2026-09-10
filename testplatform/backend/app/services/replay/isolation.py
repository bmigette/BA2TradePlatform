"""Replay runs offline. This module is what makes that true (spec section 8).

"Replay runs in an isolated process with network disabled at the transport layer
and a broker adapter that cannot submit/cancel/replace real orders. It cannot
fall through to ``_get_current_price``, a provider fetch, or production DB
lookup. Every unexpected dependency is a typed replay miss with analysis/request
ID."

Four locks, entered together by :func:`replay_isolation` before any replay
command touches a bundle:

1. ``hermetic_fmp_history()`` -- the FMP layer's own "never fetch" mode, so a
   provider that would have gone to FMP raises there rather than here.
2. ``socket.socket.connect`` -- the transport itself. Anything that gets past
   the layers above (a library with its own HTTP client, a stray `requests`
   call) dies at the socket with a :class:`ReplayMiss`, not with a timeout.
3. The instance resolver -- a loud stub. Resolving an expert or an account is
   how replay would reach a live broker and the trading database; the pattern is
   ``packages/experts/tests/test_golden_live_vs_asof.py::_host_seams``.
4. The provider resolver -- the same, one layer down: ``_live_providers()``
   must not be able to hand a replay a real FMP provider.

Nothing here opens a trading database, and nothing here has a fallback: a miss
is a coverage fact the report prints, never something to paper over with a live
read.
"""
from __future__ import annotations

import socket
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Any, List, Optional

from ba2_common.core.replay import ReplayMiss

__all__ = ["IsolationProbe", "replay_isolation", "ReplayIsolationBreach"]


class ReplayIsolationBreach(ReplayMiss):
    """Replay tried to leave the machine (or reach a live host object).

    A :class:`ReplayMiss` subclass on purpose: to the report this is the same
    kind of event as a missing tape entry -- the run needed something the bundle
    does not contain -- and it is counted as a coverage gap, never retried live.
    """


@dataclass
class IsolationProbe:
    """What replay ATTEMPTED to reach. All counters must stay 0 in a clean run."""

    network_attempts: List[str] = field(default_factory=list)
    instance_resolutions: List[str] = field(default_factory=list)
    provider_resolutions: List[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def note(self, bucket: List[str], what: str) -> None:
        with self._lock:
            bucket.append(what)

    @property
    def clean(self) -> bool:
        return not (self.network_attempts or self.instance_resolutions
                    or self.provider_resolutions)

    def summary(self) -> str:
        return (f"network={len(self.network_attempts)} "
                f"instances={len(self.instance_resolutions)} "
                f"providers={len(self.provider_resolutions)}")


class _StubInstanceResolver:
    """Every method is a breach: replay resolves no live expert, account or broker."""

    def __init__(self, probe: IsolationProbe, analysis_id: Optional[str] = None):
        self._probe = probe
        self._analysis_id = analysis_id

    def _refuse(self, what: str, identity: Any):
        self._probe.note(self._probe.instance_resolutions, f"{what}:{identity}")
        raise ReplayIsolationBreach(
            "instance_resolver",
            analysis_id=self._analysis_id,
            request_identity={"resolve": what, "id": identity},
            detail="replay must not resolve a live instance",
        )

    def get_expert_instance(self, expert_id):
        self._refuse("expert_instance", expert_id)

    def get_account_instance(self, account_id):
        self._refuse("account_instance", account_id)

    def get_account_instance_from_transaction(self, transaction):
        self._refuse("account_from_transaction", getattr(transaction, "id", transaction))


def _stub_provider_resolver(probe: IsolationProbe):
    """A ``get_provider(category, name, **kw)`` that refuses instead of resolving."""

    def get_provider(category, name=None, **kwargs):
        probe.note(probe.provider_resolutions, f"{category}/{name}")
        raise ReplayIsolationBreach(
            "provider_resolver",
            request_identity={"category": category, "name": name},
            detail="replay must read providers from the tape, never resolve a live one",
        )

    return get_provider


def _blocked_connect(probe: IsolationProbe):
    def connect(self, address, *args, **kwargs):
        probe.note(probe.network_attempts, str(address))
        raise ReplayIsolationBreach(
            "network",
            request_identity={"address": _address_text(address)},
            detail="replay is offline; the transport is closed",
        )

    return connect


def _address_text(address: Any) -> str:
    if isinstance(address, (tuple, list)):
        return ":".join(str(part) for part in address)
    return str(address)


@contextmanager
def replay_isolation():
    """Enter every isolation lock; yield the :class:`IsolationProbe` recording breaches.

    Restores each seam on exit, including when the body raises -- a replay run
    must not leave a process with its instance resolver stubbed out.

    PROCESS-WIDE while it is entered (``socket.socket.connect`` and the two host
    seams are module state), so a replay command owns the process for its
    duration. That is what "replay runs in an isolated process" means here; it is
    not something to nest inside a live application.
    """
    from ba2_common.core import TradeConditions, instance_resolver
    from ba2_providers.fmp_common import hermetic_fmp_history

    probe = IsolationProbe()
    previous_connect = socket.socket.connect
    previous_instance = instance_resolver.get_instance_resolver()
    previous_provider = TradeConditions.get_provider_resolver()

    with ExitStack() as stack:
        stack.enter_context(hermetic_fmp_history())
        socket.socket.connect = _blocked_connect(probe)
        instance_resolver.set_instance_resolver(_StubInstanceResolver(probe))
        TradeConditions.set_provider_resolver(_stub_provider_resolver(probe))
        stack.callback(TradeConditions.set_provider_resolver, previous_provider)
        stack.callback(instance_resolver.set_instance_resolver, previous_instance)
        stack.callback(_restore_connect, previous_connect)
        yield probe


def _restore_connect(previous) -> None:
    socket.socket.connect = previous


def refuse(kind: str, analysis_id: Optional[str] = None,
           request_identity: Optional[Any] = None, detail: str = "") -> ReplayMiss:
    """Build the typed miss a replay adapter raises instead of reaching out.

    Returned rather than raised so the call site reads ``raise refuse(...)`` and
    a static reader can see the control flow leaves there.
    """
    return ReplayMiss(kind, analysis_id=analysis_id,
                      request_identity=request_identity, detail=detail)

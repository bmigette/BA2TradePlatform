"""The IBKR connection runtime: one private asyncio loop thread + one ``ib_async.IB`` per account.

WHY THIS EXISTS (design doc s3). ``ib_async`` is asyncio-based and its blocking wrappers need a loop
on the calling thread. The platform is NiceGUI (one loop on the main thread) plus worker threads.
So every ``IBKRAccount`` owns a daemon thread running its own loop; the ``IB`` object is created
ON that loop; all ib_async calls are made as coroutines on it; every other thread only ever
``run_coroutine_threadsafe(...).result(timeout)``.

Properties pinned by ``tests/test_ibkr_runtime.py``:

* NEVER BLOCKS THE UI LOOP with I/O: sockets and callbacks live on the private thread. A facade call
  entered from a thread that has a running asyncio loop (NiceGUI) blocks that thread for at most the
  call's bounded timeout and is logged at WARNING with the call site, so it can be moved to
  ``run.io_bound``.
* CANNOT DEADLOCK: a facade call made from the IB loop thread itself raises (it would wait on the
  loop it runs on); the connect lock is an ``asyncio.Lock`` never held across a facade call; every wait
  is bounded and cancels the pending work on timeout.
* NO CONNECT STORM: a failed connect arms a cooldown; calls inside it fail at once with the last error.
* NEVER AUTO-CHANGES THE CLIENT ID, and refuses a paper/live mismatch (design doc 3.3).
"""
from __future__ import annotations

import asyncio
import inspect
import threading
import time
import traceback
from collections import deque
from concurrent.futures import CancelledError, TimeoutError as FutureTimeoutError
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from .ibkr_mapping import (
    CLIENT_ID_IN_USE_CODE, CONNECTION_RESTORED_CODES, IB_PORTS,
    PAPER_ACCOUNT_PREFIX, error_severity)

from ib_async import StartupFetch

from ...logger import logger


class IBKRError(Exception):
    """Base of every IBKR adapter error."""


class IBKRConnectionError(IBKRError):
    """The Gateway/TWS cannot be reached, refused the session, or addresses the wrong account."""


class IBKRReadOnlyError(IBKRError):
    """A write was requested on an account configured read-only."""


class IBKROrderRejected(IBKRError):
    """IB refused or cancelled an order during submission. ``code`` is IB's error code."""

    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


class IBKROrphanStop(IBKROrderRejected):
    """An OCO's take-profit failed and the stop leg already placed could not be CONFIRMED cancelled
    (or it filled meanwhile): a live/filled order exists that the caller must record, never lose."""

    def __init__(self, message: str, code: Optional[int] = None, *, view: Any = None,
                 sl_stop: Optional[float] = None, sl_limit: Optional[float] = None,
                 outcome: str = "unconfirmed"):
        super().__init__(message, code)
        self.view, self.sl_stop, self.sl_limit, self.outcome = view, sl_stop, sl_limit, outcome


class IBKRContractError(IBKRError):
    """A symbol/contract could not be resolved to exactly one IB contract."""


#: How many recent errors are kept per request id (order errors are read right after a submit).
_MAX_TRACKED_REQ_IDS = 2000


class RuntimeState:
    """Per-connection caches. They live on the SHARED runtime (not the account object) because the
    platform builds short-lived account objects (``TradeManager`` constructs one per call); a cache
    on the object would be empty every time and a connection per object would collide on the
    client id."""

    def __init__(self) -> None:
        self.contracts: Dict[str, Any] = {}
        self.rules: Dict[str, Any] = {}
        self.prev_close: Dict[str, Tuple[str, float]] = {}
        self.warned: set = set()
        #: Serialises every market-data request group (price snapshots, option streaming batches,
        #: the shortable tick) so their lines can never add up past IBKR's ~100-line budget.
        #: Created lazily ON the loop thread.
        self.data_lock: Optional[asyncio.Lock] = None
        self.flex: Any = None
        #: One asyncio lock per ib_async request TYPE (see ``IBKRRuntime.request_lock``).
        self.request_locks: Dict[str, asyncio.Lock] = {}
        #: the buying-power component that last bound (logged when it changes)
        self.bp_binding: Optional[str] = None


class IBKRRuntime:
    """Loop thread + IB object + connection policy for one account."""

    def __init__(self, *, label: str, ib_factory: Callable[[], Any], host: str, port: int,
                 client_id: int, account_id: str, paper: bool, read_only: bool,
                 connect_timeout: float = 15.0, cooldown: float = 15.0):
        self.label = label
        self._ib_factory = ib_factory
        self.host, self.port, self.client_id = host, int(port), int(client_id)
        self.account_id, self.paper, self.read_only = account_id, bool(paper), bool(read_only)
        self.connect_timeout = float(connect_timeout)
        self.cooldown = float(cooldown)

        self.state = RuntimeState()
        self._ib: Any = None
        self._events_wired = False
        self._degraded = False
        self._last_failure_at: Optional[float] = None
        self._last_failure: Optional[str] = None
        self._connect_lock: Optional[asyncio.Lock] = None
        #: Errors with a request id (orders use their orderId), in ARRIVAL order with a monotonic
        #: sequence number: [(seq, reqId, code, message)]. ib_async reuses request/order ids after a
        #: reconnect, so an error is only ever read together with the sequence mark taken just before
        #: the request it belongs to (``mark()`` / ``order_errors(..., after_seq=)``), and the list is
        #: cleared on every (re)connect.
        self._errors: Deque[Tuple[int, int, int, str]] = deque(maxlen=5000)
        self._err_seq = 0
        self.closed = False
        self._pending: set = set()
        #: Connection-level errors (reqId -1) seen recently; read to explain a failed connect.
        self.recent_global_errors: Deque[Tuple[int, str]] = deque(maxlen=50)

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name=f"ibkr-loop-{label}", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ loop
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    @property
    def loop_thread(self) -> threading.Thread:
        return self._thread

    def is_connected(self) -> bool:
        ib = self._ib
        return bool(ib is not None and ib.isConnected() and not self._degraded)

    # ------------------------------------------------------------------ facade
    def call(self, fn: Callable[[Any], Any], *, timeout: float, op: str) -> Any:
        """Run ``fn(ib)`` (returning a value or an awaitable) on the IB loop and return its result.

        Raises:
            RuntimeError: when called from the IB loop thread itself (deadlock guard).
            TimeoutError: the call exceeded ``timeout`` seconds (the pending work is cancelled).
            IBKRConnectionError: the connection could not be (re)established.
            Anything ``fn`` raises.
        """
        if threading.current_thread() is self._thread:
            raise RuntimeError(
                f"[{self.label}] IBKR facade call '{op}' made from the IB loop thread; it would "
                f"wait on the loop it runs on and deadlock")
        self._warn_if_on_a_running_loop(op)
        if self.closed:
            raise IBKRConnectionError(f"[{self.label}] the IBKR runtime was closed (settings changed or "
                                      f"shutdown); retry on the current one")
        budget = float(timeout) + (0.0 if self.is_connected() else self.connect_timeout + 5.0)
        future = asyncio.run_coroutine_threadsafe(self._run(fn, op), self._loop)
        self._pending.add(future)
        try:
            return future.result(timeout=budget)
        except FutureTimeoutError as e:
            if future.done() and not future.cancelled():
                # concurrent.futures.TimeoutError IS the builtin TimeoutError (3.11): an inner
                # ``asyncio.wait_for`` that expired inside the coroutine surfaces here too. Say which
                # one fired, so a read that timed out is never reported as the whole call's budget.
                raise TimeoutError(f"[{self.label}] IBKR call '{op}': an inner wait timed out "
                                   f"({e or 'no detail'}); the call's own budget of {budget:.0f}s "
                                   f"had not run out") from e
            future.cancel()
            raise TimeoutError(f"[{self.label}] IBKR call '{op}' exceeded its total budget of "
                               f"{budget:.0f}s (the call's own deadline, not an inner read)") from None
        except CancelledError:
            raise IBKRConnectionError(
                f"[{self.label}] IBKR call '{op}' was cancelled: the runtime was closed (settings "
                f"changed or shutdown)") from None
        finally:
            self._pending.discard(future)

    def _warn_if_on_a_running_loop(self, op: str) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        site = "".join(traceback.format_stack(limit=6)[:-2])
        logger.warning(
            f"[{self.label}] IBKR call '{op}' is blocking a thread that is running an asyncio loop "
            f"(the NiceGUI UI thread?). It is bounded by its timeout, but move it to "
            f"run.io_bound. Call site:\n{site}")

    def request_lock(self, name: str) -> asyncio.Lock:
        """The lock serialising every caller of one ib_async request TYPE. ib_async keeps ONE pending
        future per type (``openOrders``, ``completedOrders``, ``positions``, ...): a second concurrent
        request overwrites the first's future, so the first caller never gets its answer and times
        out. Every such request is therefore made under this lock. Loop thread only."""
        lock = self.state.request_locks.get(name)
        if lock is None:
            lock = self.state.request_locks[name] = asyncio.Lock()
        return lock

    async def _run(self, fn: Callable[[Any], Any], op: str) -> Any:
        ib = await self._ensure_connected()
        result = fn(ib)
        if inspect.isawaitable(result):
            result = await result
        return result

    # ------------------------------------------------------------------ connection
    async def _ensure_connected(self) -> Any:
        if self._ib is not None and self._ib.isConnected():
            if not self._degraded:
                return self._ib
            await self._probe_degraded()
            return self._ib
        if self._connect_lock is None:
            self._connect_lock = asyncio.Lock()
        async with self._connect_lock:
            if self._ib is not None and self._ib.isConnected() and not self._degraded:
                return self._ib
            now = time.monotonic()
            if self._last_failure_at is not None and now - self._last_failure_at < self.cooldown:
                raise IBKRConnectionError(
                    f"[{self.label}] IBKR connection is down (last attempt "
                    f"{now - self._last_failure_at:.0f}s ago failed: {self._last_failure}); "
                    f"next attempt after the {self.cooldown:.0f}s cooldown")
            try:
                await self._connect()
            except Exception as e:  # noqa: BLE001 -- re-raised as a typed error below
                self._last_failure_at = time.monotonic()
                self._last_failure = str(e) or type(e).__name__
                if isinstance(e, IBKRConnectionError):
                    raise
                raise IBKRConnectionError(self._describe_connect_failure(e)) from e
            self._last_failure_at = None
            self._last_failure = None
            return self._ib

    def _describe_connect_failure(self, exc: Exception) -> str:
        seen = [f"{code}: {msg}" for code, msg in self.recent_global_errors]
        hint = ""
        if any(str(CLIENT_ID_IN_USE_CODE) in s.split(":")[0] for s in seen):
            hint = (f" Client id {self.client_id} is already in use by another API session "
                    f"(IB error {CLIENT_ID_IN_USE_CODE}); choose a different client_id. The adapter "
                    f"never changes it on its own.")
        kind = IB_PORTS.get(self.port, "unknown port")
        return (f"[{self.label}] cannot connect to IBKR at {self.host}:{self.port} ({kind}, "
                f"clientId={self.client_id}): {type(exc).__name__}: {exc}.{hint} "
                f"Recent IB errors: {seen[-3:] or 'none'}. Is IB Gateway/TWS running, logged in, "
                f"with the API enabled and this host trusted?")

    async def _connect(self) -> None:
        if self._ib is None:
            self._ib = self._ib_factory()
        ib = self._ib
        if not self._events_wired:
            ib.errorEvent += self._on_error
            ib.disconnectedEvent += self._on_disconnected
            self._events_wired = True
        self.recent_global_errors.clear()
        self._errors.clear()                  # ids repeat across sessions: nothing from before counts
        logger.info(f"[{self.label}] connecting to IBKR {self.host}:{self.port} "
                    f"clientId={self.client_id} readonly={self.read_only}")
        # Startup is trimmed to what the adapter streams: the account-updates feed (values + portfolio).
        # Completed orders, open orders and executions are NOT preloaded (the order book is read
        # explicitly, under a request lock, when needed) and a slow optional sync must not fail the
        # connect: ``raiseSyncErrors`` stays False. Positions are always requested by ib_async at
        # connect, and the adapter re-confirms them with ``reqPositions`` on EVERY positions read, so an
        # unsynced cache can never be mistaken for a flat book.
        await asyncio.wait_for(
            ib.connectAsync(self.host, self.port, clientId=self.client_id,
                            timeout=self.connect_timeout, readonly=self.read_only,
                            account=self.account_id, raiseSyncErrors=False,
                            fetchFields=StartupFetch.ACCOUNT_UPDATES),
            self.connect_timeout + 5.0)
        managed = list(ib.managedAccounts())
        if self.account_id not in managed:
            ib.disconnect()
            raise IBKRConnectionError(
                f"[{self.label}] account {self.account_id!r} is not among the accounts this IBKR "
                f"login manages ({managed}); refusing to trade on another account")
        is_paper_id = self.account_id.startswith(PAPER_ACCOUNT_PREFIX)
        if self.paper != is_paper_id:
            ib.disconnect()
            raise IBKRConnectionError(
                f"[{self.label}] paper_account={self.paper} but account {self.account_id!r} is a "
                f"{'PAPER' if is_paper_id else 'LIVE'} account (paper ids start "
                f"{PAPER_ACCOUNT_PREFIX!r}). Refusing to connect: fix the setting or the port.")
        # 2 = frozen: live ticks while the market is open, the last values when closed. Delayed
        # (3/4) is never requested, and a delayed tick is refused by the price readers.
        ib.reqMarketDataType(2)
        self._degraded = False
        logger.info(f"[{self.label}] IBKR connected; account {self.account_id} "
                    f"({'paper' if self.paper else 'LIVE'})")

    async def _probe_degraded(self) -> None:
        """After error 1100 (IB <-> TWS link lost) the socket can stay up; probe before trusting it."""
        try:
            await asyncio.wait_for(self._ib.reqCurrentTimeAsync(), 5.0)
        except Exception as e:  # noqa: BLE001
            raise IBKRConnectionError(
                f"[{self.label}] IBKR reports lost connectivity to the IB servers (error 1100) "
                f"and the probe failed: {e}") from e
        self._degraded = False

    # ------------------------------------------------------------------ events
    def _on_error(self, reqId: int, errorCode: int, errorString: str, contract: Any = None) -> None:
        severity = error_severity(errorCode)
        if severity == "info":
            logger.debug(f"[{self.label}] IB info {errorCode}: {errorString}")
        else:
            logger.warning(f"[{self.label}] IB {severity} {errorCode} (reqId {reqId}): {errorString}")
        if errorCode == 1100:
            self._degraded = True
        elif errorCode in CONNECTION_RESTORED_CODES:
            self._degraded = False
        if reqId is None or reqId < 0:
            self.recent_global_errors.append((errorCode, errorString))
            return
        self._err_seq += 1
        self._errors.append((self._err_seq, reqId, errorCode, errorString))

    def _on_disconnected(self) -> None:
        logger.warning(f"[{self.label}] IBKR disconnected")

    def mark(self) -> int:
        """The current error sequence number: take it immediately BEFORE sending a request."""
        return self._err_seq

    def order_errors(self, order_id: int, after_seq: int = 0,
                     kinds: Optional[Tuple[str, ...]] = ("order", "cancelled")) -> List[Tuple[int, str]]:
        """Messages for ``order_id`` that arrived AFTER ``after_seq``. By default only the kinds that can
        fail an order (warnings, info, connection and market-data chatter excluded); ``kinds=None``
        returns every message, used to EXPLAIN a rejection that ib_async already decided."""
        return [(c, m) for seq, rid, c, m in self._errors
                if rid == order_id and seq > after_seq
                and (kinds is None or error_severity(c) in kinds)]

    def order_warnings(self, order_id: int, after_seq: int = 0) -> List[Tuple[int, str]]:
        return self.order_errors(order_id, after_seq, kinds=("order_warning",))

    # ------------------------------------------------------------------ shutdown
    def close(self) -> None:
        self.closed = True
        on_own_thread = threading.current_thread() is self._thread
        for future in list(self._pending):          # waiting callers fail NOW, not after their timeout
            future.cancel()
        loop = self._loop
        if loop.is_closed():
            return
        ib = self._ib

        def _stop() -> None:
            try:
                if ib is not None and ib.isConnected():
                    ib.disconnect()
            finally:
                loop.stop()

        try:
            loop.call_soon_threadsafe(_stop)
            if not on_own_thread:                 # a thread cannot join itself
                self._thread.join(timeout=5.0)
        except Exception as e:  # noqa: BLE001 -- shutdown must never raise
            logger.error(f"[{self.label}] error closing IBKR runtime: {e}")


# ---------------------------------------------------------------------------
# One runtime per account definition, shared by every account OBJECT built for it
# ---------------------------------------------------------------------------
_REGISTRY: Dict[int, Tuple[Tuple, IBKRRuntime]] = {}
_REGISTRY_LOCK = threading.Lock()


def get_runtime(account_definition_id: int, signature: Tuple,
                factory: Callable[[], IBKRRuntime]) -> IBKRRuntime:
    """The one runtime (= one TWS session, one client id) for this account definition.

    Several ``IBKRAccount`` objects exist for one account over a process's life (the instance cache
    is dropped by ``/api/reload``; ``TradeManager`` builds a fresh object per call). A connection
    per object would put two sessions on one ``clientId``; instead they all share this runtime. A
    changed ``signature`` (host, port, client id, account, flags) replaces it: the old session is
    closed first, so a settings edit takes effect without a restart.
    """
    with _REGISTRY_LOCK:
        entry = _REGISTRY.get(account_definition_id)
        if entry is not None and entry[0] == signature and not entry[1].closed:
            return entry[1]
        if entry is not None:
            logger.info(f"IBKR account {account_definition_id}: settings changed, replacing its "
                        f"connection runtime")
            entry[1].close()
        runtime = factory()
        _REGISTRY[account_definition_id] = (signature, runtime)
        return runtime


def registry_signature(account_definition_id: int) -> Optional[Tuple]:
    """The signature the current runtime for this account definition was built with, if any."""
    with _REGISTRY_LOCK:
        entry = _REGISTRY.get(account_definition_id)
        return entry[0] if entry is not None and not entry[1].closed else None


def shutdown_runtime(account_definition_id: int) -> None:
    """Disconnect and forget this account's runtime (app shutdown, tests)."""
    with _REGISTRY_LOCK:
        entry = _REGISTRY.pop(account_definition_id, None)
    if entry is not None:
        entry[1].close()


def shutdown_all_runtimes() -> None:
    with _REGISTRY_LOCK:
        entries = list(_REGISTRY.values())
        _REGISTRY.clear()
    for _, runtime in entries:
        runtime.close()

"""Shared helpers for Financial Modeling Prep (FMP) data providers.

FMP returns rate-limit / API errors as **HTTP 200 with a JSON dict body** such
as ``{"Error Message": "Limit Reach."}`` instead of a proper error status code.
Providers that assume a list then crash when they slice/index the dict
(``unhashable type: 'slice'`` or ``KeyError: 0``).

``fmp_list_call`` wraps an FMP call and guarantees a list result:

* list           -> returned as-is
* ``None`` / ``[]`` -> ``[]`` (legitimate "no data")
* error dict     -> retried with backoff, then ``FMPError`` (raw payload logged)
* unexpected dict -> ``FMPError`` immediately (raw payload logged, no retry)
"""

import threading
import time
from typing import Any, Callable, List, Optional

import requests

from ...logger import logger


class TTLCache:
    """Tiny thread-safe time-to-live cache to dedupe identical fetches across callers.

    ``get_or_call(key, fn)`` returns the cached value if present and unexpired,
    otherwise calls ``fn()`` (outside the lock), caches the result — including
    ``None`` — and returns it. Intended to collapse the many redundant FMP calls
    that multiple experts make for the same symbol within a short window.
    """

    def __init__(self, ttl_seconds: float, clock: Callable[[], float] = time.time):
        self._ttl = ttl_seconds
        self._clock = clock
        self._store: dict = {}
        self._lock = threading.Lock()

    def get_or_call(self, key, fn: Callable[[], Any]) -> Any:
        with self._lock:
            item = self._store.get(key)
            if item is not None and self._clock() < item[1]:
                return item[0]
        value = fn()  # network call outside the lock
        with self._lock:
            self._store[key] = (value, self._clock() + self._ttl)
        return value

# FMP error responses use one of these keys in a 200-status JSON dict.
_FMP_ERROR_KEYS = ("Error Message", "error", "message")


class FMPError(RuntimeError):
    """Raised when an FMP call returns an error/unexpected payload (after retries)."""


def _fmp_error_message(payload: dict) -> Any:
    """Return the FMP error string from a dict payload, or None if not an error."""
    for key in _FMP_ERROR_KEYS:
        if key in payload and payload[key]:
            return payload[key]
    return None


def _parse_retry_after(value) -> Optional[float]:
    """Parse a Retry-After header value expressed in seconds; ignore HTTP-date form."""
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fmp_http_get(
    url: str,
    params: Optional[dict] = None,
    *,
    symbol: str = "",
    endpoint: str = "",
    timeout: int = 60,
    delays: tuple = (5, 15, 30),
    sleep: Callable[[float], None] = time.sleep,
    getter: Optional[Callable] = None,
    retry_statuses: tuple = (429, 500, 502, 503, 504),
):
    """GET an FMP endpoint with backoff retry on real HTTP rate-limit/server errors.

    Unlike ``fmp_list_call`` (which handles FMP's 200-status error-dict form), the
    direct v3/v4 endpoints return a genuine HTTP **429 Too Many Requests** (or 5xx).
    This retries those — and transient connection/timeout errors — with backoff,
    honouring a ``Retry-After`` header when present. Non-retryable HTTP errors
    (e.g. 401/404) raise immediately via ``raise_for_status``.

    Returns the successful ``requests.Response``. Raises ``FMPError`` after the
    retries are exhausted on a retryable condition.
    """
    getter = getter or requests.get
    total_attempts = len(delays) + 1
    last_reason: Any = None
    retry_after: Optional[float] = None

    for attempt in range(total_attempts):
        if attempt > 0:
            delay = delays[attempt - 1]
            if retry_after is not None:
                delay = max(delay, retry_after)
            sleep(delay)
        retry_after = None

        try:
            resp = getter(url, params=params, timeout=timeout)
        except requests.exceptions.RequestException as e:
            last_reason = e
            logger.warning(
                f"FMP {endpoint or 'call'} request error for {symbol or '?'} "
                f"(attempt {attempt + 1}/{total_attempts}): {e}"
            )
            continue

        status = getattr(resp, "status_code", None)
        if status in retry_statuses:
            retry_after = _parse_retry_after(getattr(resp, "headers", {}).get("Retry-After"))
            last_reason = f"HTTP {status}"
            logger.warning(
                f"FMP {endpoint or 'call'} {status} for {symbol or '?'} "
                f"(attempt {attempt + 1}/{total_attempts}); backing off"
            )
            continue

        # Any other 4xx (401/404/...) is a non-retryable client error -> raise.
        resp.raise_for_status()
        return resp

    logger.error(
        f"FMP {endpoint or 'call'} failed for {symbol or '?'} after "
        f"{total_attempts} attempts (last: {last_reason})"
    )
    raise FMPError(
        f"FMP {endpoint or 'call'} failed for {symbol or '?'} after "
        f"{total_attempts} attempts (last: {last_reason})"
    )


def fmp_list_call(
    fn: Callable[[], Any],
    *,
    symbol: str = "",
    endpoint: str = "",
    delays: tuple = (15, 30, 60),
    sleep: Callable[[float], None] = time.sleep,
) -> List[Any]:
    """Call ``fn`` (a 0-arg wrapper around an FMP request) and normalize to a list.

    Args:
        fn: Zero-arg callable that performs the FMP call (e.g. an fmpsdk call).
        symbol: Ticker symbol, for logging context.
        endpoint: FMP endpoint name, for logging context.
        delays: Backoff delays (seconds) between retries on FMP error dicts.
            The call is retried ``len(delays)`` times.
        sleep: Sleep function (injectable for tests).

    Returns:
        A list (possibly empty) of FMP records.

    Raises:
        FMPError: On a persistent FMP error dict (after retries) or on an
            unexpected (non-list, non-error-dict) payload.
    """
    total_attempts = len(delays) + 1
    last_payload: Any = None

    for attempt in range(total_attempts):
        if attempt > 0:
            sleep(delays[attempt - 1])

        result = fn()
        last_payload = result

        # Legitimate results.
        if isinstance(result, list):
            return result
        if result is None:
            return []

        if isinstance(result, dict):
            err = _fmp_error_message(result)
            if err is not None:
                # FMP error dict (e.g. rate limit) -> warn and retry with backoff.
                logger.warning(
                    f"FMP {endpoint or 'call'} error for {symbol or '?'} "
                    f"(attempt {attempt + 1}/{total_attempts}): {err}"
                )
                continue
            # Dict without a known error key -> unexpected shape, no retry.
            logger.error(
                f"FMP {endpoint or 'call'} unexpected payload for {symbol or '?'}. "
                f"Payload: {result!r}"
            )
            raise FMPError(
                f"FMP {endpoint or 'call'} returned unexpected payload for {symbol or '?'}"
            )

        # Any other type (str, int, ...) is unexpected -> no retry.
        logger.error(
            f"FMP {endpoint or 'call'} unexpected payload type "
            f"({type(result).__name__}) for {symbol or '?'}. Payload: {result!r}"
        )
        raise FMPError(
            f"FMP {endpoint or 'call'} returned unexpected payload type "
            f"{type(result).__name__} for {symbol or '?'}"
        )

    # All attempts exhausted on FMP error dicts -> log raw payload and raise.
    logger.error(
        f"FMP {endpoint or 'call'} error for {symbol or '?'} after "
        f"{total_attempts} attempts. Payload: {last_payload!r}"
    )
    raise FMPError(
        f"FMP {endpoint or 'call'} error for {symbol or '?'} after {total_attempts} attempts"
    )

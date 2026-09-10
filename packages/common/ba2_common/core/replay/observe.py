"""Provider taps: record a return value WITHOUT changing it (spec step 2, section 4).

"Record provider responses at the return boundary, including memory/disk cache
hits. [...] Never issue a duplicate fetch just to fill metadata."

So a tap does exactly three things: call the wrapped function ONCE, hand the
caller back the very object it returned, and -- only when a capture context is
active -- freeze a copy of that object into the record. With no context every
wrapper here is a plain passthrough: one ``is None`` check and the original call.

Nothing in this module may raise into the caller. A tap that cannot build its
identity, classify its provenance or freeze its payload counts the failure on the
analysis's :class:`~ba2_common.core.replay.context.CaptureHealth` and returns the
provider's value unchanged: capture is observational, and a broken recorder must
never turn a working fetch into a failed analysis.
"""
from __future__ import annotations

import functools
import inspect
import re
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Mapping, Optional

from ba2_common.core.replay.context import current_capture
from ba2_common.core.replay.schemas import ReplayStatus

__all__ = [
    "observe_provider",
    "record_observation",
    "sanitize_identity",
    "SECRET_KEY_TOKENS",
]

#: Any identity key containing one of these (case-insensitive) is DROPPED, never
#: masked: the guarantee the tests assert is that no credential reaches the store
#: at all. Sanitizing at the tap (not at the call site) means a new tap cannot
#: leak a key by forgetting to exclude it.
SECRET_KEY_TOKENS = (
    "api_key", "apikey", "api-key", "token", "secret", "password", "passwd",
    "credential", "bearer", "authorization",
)

#: The same guarantee for a credential embedded in a string VALUE (a URL query
#: string, typically): the value survives, the credential does not.
_SECRET_IN_VALUE = re.compile(
    r"(?i)\b(api[_-]?key|apikey|token|secret|password|passwd|access[_-]?key)"
    r"\s*[=:]\s*[^&\s,;)\]}\"']+"
)
_REDACTED = r"\1=REDACTED"


def _is_secret_key(name: Any) -> bool:
    text = str(name).lower()
    return any(token in text for token in SECRET_KEY_TOKENS)


def _json_safe(value: Any, depth: int = 0) -> Any:
    """A JSON-encodable rendering of an identity value.

    ``request_identity`` is stored as JSON in the index, so a datetime or an Enum
    that reached it verbatim would fail the writer's ``json.dumps`` and drop the
    whole analysis record. Times become ISO strings (tz preserved), enums their
    value, and anything else its ``repr`` -- lossy on purpose, and only for the
    IDENTITY: the payload itself goes through the exact codec.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # NaN/inf are not JSON: keep them visible as text rather than dropped.
        return value if value == value and value not in (float("inf"), float("-inf")) else repr(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return _json_safe(value.value, depth + 1)
    if depth >= 6:
        return repr(value)
    if isinstance(value, Mapping):
        return {
            str(k): _json_safe(v, depth + 1)
            for k, v in value.items()
            if not _is_secret_key(k)
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=repr) if isinstance(value, (set, frozenset)) else value
        return [_json_safe(item, depth + 1) for item in items]
    return repr(value)


def sanitize_identity(identity: Optional[Mapping[str, Any]]) -> dict:
    """Drop every credential-shaped key and make the rest JSON-encodable."""
    if not identity:
        return {}
    out = {}
    for key, value in identity.items():
        if _is_secret_key(key):
            continue
        safe = _json_safe(value)
        if isinstance(safe, str):
            safe = _SECRET_IN_VALUE.sub(_REDACTED, safe)
        out[str(key)] = safe
    return out


def record_observation(
    *,
    provider: str,
    method: str,
    identity: Optional[Mapping[str, Any]] = None,
    payload: Any = None,
    provenance: str = ReplayStatus.PROVENANCE_UNKNOWN,
    response_class: Optional[str] = None,
    fetched_at: Optional[datetime] = None,
    published_at: Optional[datetime] = None,
    first_observed_at: Optional[datetime] = None,
) -> Optional[str]:
    """Record one provider return against the active analysis.

    Returns the observation id, or ``None`` when capture is off (or the process
    is replaying, where recording would corrupt the tape it is reading).
    """
    context = current_capture()
    if context is None or context.is_replay:
        return None
    return context.record_observation(
        provider=provider,
        method=method,
        request_identity=sanitize_identity(identity),
        payload=payload,
        provenance=provenance,
        response_class=response_class,
        fetched_at=fetched_at,
        published_at=published_at,
        first_observed_at=first_observed_at,
    )


def observe_provider(
    provider: str,
    method: str,
    *,
    identity: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    provenance: Any = ReplayStatus.PROVENANCE_UNKNOWN,
    before: Optional[Callable[[Mapping[str, Any]], Any]] = None,
    payload: Optional[Callable[[Mapping[str, Any], Any], Any]] = None,
):
    """Decorate a provider boundary so its RETURN is recorded when capture is on.

    ``identity``/``payload``/``provenance``/``before`` all receive the wrapped
    call's arguments as one mapping of parameter name -> value (defaults applied,
    ``self`` included for a method), so a tap never has to guess whether the
    caller passed something positionally.

    * ``provenance`` -- a constant string, or ``fn(args, result, before_value)``.
    * ``before`` -- ``fn(args)`` run BEFORE the wrapped call (capture on only),
      for a cheap "was this served from cache?" probe. It must not fetch.
    * ``payload`` -- ``fn(args, result)`` when what is worth recording is not the
      return value itself. Defaults to the return value.

    The wrapped function is called EXACTLY once, and its return value is passed
    back by identity: capture never changes a result or a call count.
    """
    def decorator(fn):
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            context = current_capture()
            if context is None or context.is_replay:
                return fn(*args, **kwargs)

            bound = _bind(context, signature, args, kwargs, provider, method)
            before_value = None
            if before is not None and bound is not None:
                try:
                    before_value = before(bound)
                except Exception as exc:
                    context.note_failure(f"{provider}.{method} pre-probe failed", exc)

            result = fn(*args, **kwargs)

            try:
                request_identity = identity(bound) if bound is not None else {
                    "identity_unavailable": True
                }
                recorded = result if payload is None else payload(bound, result)
                where = provenance
                if callable(where):
                    where = where(bound, result, before_value)
            except Exception as exc:
                context.note_failure(f"{provider}.{method} observation not built", exc)
                return result

            record_observation(
                provider=provider,
                method=method,
                identity=request_identity,
                payload=recorded,
                provenance=where,
            )
            return result

        return wrapper

    return decorator


def _bind(context, signature, args, kwargs, provider, method) -> Optional[dict]:
    """The call's arguments as ``name -> value`` (defaults applied).

    ``self`` is kept: a tap on an interface method often needs the concrete
    provider class in its identity. It is never recorded unless an identity
    function asks for it.
    """
    try:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)
    except Exception as exc:
        context.note_failure(f"{provider}.{method} arguments not bindable", exc)
        return None

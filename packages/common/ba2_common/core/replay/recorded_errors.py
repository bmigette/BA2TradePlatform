"""A provider read that RAISED, as a recorded observation, and its replay as a raise.

``observe_provider(record_errors=True)`` (the option taps) records ``error_payload(exc)`` with
``response_class="error"`` and re-raises the original unchanged. A tape serves such an observation
by RAISING :func:`rebuild_error` of it, in recorded order, so an action that handled the failure
(skip this candidate) takes the same branch offline.

**Honest limit.** Only exception classes the replay can construct faithfully are rebuilt: builtins,
``ba2_common`` and ``ba2_providers`` classes that take ONE message argument and print the same
message back. Anything else -- a third-party SDK class (``alpaca...APIError``), a class with a
different constructor, a ``KeyError`` whose ``str`` adds quotes -- becomes a
:class:`RecordedProviderError` carrying the recorded type name and message. An action that catches a
specific third-party type therefore REPLAYS as a difference (its handler does not see the type it
expects): never a false match.
"""
from __future__ import annotations

import builtins
import importlib
from typing import Any, Dict, Mapping

__all__ = ["RecordedProviderError", "error_payload", "rebuild_error"]

#: Module roots a recorded class may be re-imported from (all ship with the replay).
_IMPORTABLE_ROOTS = ("ba2_common", "ba2_providers")


class RecordedProviderError(Exception):
    """A recorded provider failure whose original class cannot be rebuilt offline."""

    def __init__(self, type_name: str, message: str):
        super().__init__(f"{type_name}: {message}")
        self.type_name = type_name
        self.message = message


def error_payload(exc: BaseException) -> Dict[str, str]:
    """What is recorded about a raised exception: qualified class name, module and message."""
    cls = type(exc)
    return {"exc_type": cls.__qualname__, "module": cls.__module__, "message": str(exc)}


def _resolve(module_name: str, qualname: str):
    if module_name == "builtins":
        target: Any = builtins
    elif module_name.split(".")[0] in _IMPORTABLE_ROOTS:
        target = importlib.import_module(module_name)
    else:
        return None
    for part in qualname.split("."):
        target = getattr(target, part, None)
        if target is None:
            return None
    if isinstance(target, type) and issubclass(target, Exception):
        return target
    return None


def rebuild_error(payload: Mapping[str, Any]) -> Exception:
    """The exception to raise for a recorded error (see the module docstring for the limit)."""
    type_name, module, message = payload["exc_type"], payload["module"], payload["message"]
    try:
        cls = _resolve(module, type_name)
        if cls is not None:
            rebuilt = cls(message)
            if str(rebuilt) == message:
                return rebuilt
    except Exception:  # noqa: BLE001 -- any failure to rebuild degrades to the typed stand-in
        pass
    return RecordedProviderError(f"{module}.{type_name}", message)

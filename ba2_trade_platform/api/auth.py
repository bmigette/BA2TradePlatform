"""Token auth of the management API.

Rules (design section 1):
* A request from a NON-LOCAL address (anything but 127.0.0.1 / ::1) is refused with 403 ``remote_disabled``
  unless the AppSetting ``api_allow_remote`` is on (default OFF). This runs BEFORE any token check and covers
  reads, writes, /docs and /openapi.json. The setting is read per request, so a flip needs no restart.
* Every write needs ``Authorization: Bearer <api_token>``; a read needs no token from the loopback address and
  the token from anywhere else (once remote access is allowed).
* With NO token set, writes and non-local requests are refused with 403 -- nothing is open by default.

The caller address is the SOCKET PEER (``request.client.host``). ``X-Forwarded-For`` is deliberately not read:
it is client-controlled, and trusting it would let any LAN host claim to be local. The platform is not run
behind a reverse proxy; if that ever changes, a local read through the proxy would present the proxy's
address and be refused, which fails closed.
"""
import hmac
import secrets
from typing import Optional

from fastapi import HTTPException, Request
from sqlmodel import select

from ..core.db import add_instance, get_db, get_setting, update_instance
from ..core.models import AppSetting
from ..logger import logger

API_TOKEN_KEY = "api_token"
ALLOW_REMOTE_KEY = "api_allow_remote"
SAFE_METHODS = frozenset({"GET", "HEAD"})
LOCAL_HOSTS = frozenset({"127.0.0.1", "::1"})


def get_api_token() -> Optional[str]:
    """The stored token, or None when none is set (missing row or blank value)."""
    value = get_setting(API_TOKEN_KEY)
    if value is None or not value.strip():
        return None
    return value.strip()


def _store(key: str, value: str) -> None:
    with get_db() as session:
        row = session.exec(select(AppSetting).where(AppSetting.key == key)).first()
        if row is None:
            add_instance(AppSetting(key=key, value_str=value), session)
        else:
            row.value_str = value
            update_instance(row, session)


def rotate_api_token() -> str:
    """Generate a fresh token, store it (replacing any previous one) and return it. The caller shows it to
    the operator ONCE; it is never logged."""
    token = secrets.token_urlsafe(32)
    _store(API_TOKEN_KEY, token)
    logger.info("Management API token generated (stored in AppSetting 'api_token')")
    return token


def get_allow_remote() -> bool:
    """Whether non-local callers may use the API. A missing row means False."""
    value = get_setting(ALLOW_REMOTE_KEY)
    return value is not None and value.strip().lower() in ("true", "1", "yes", "on")


def set_allow_remote(allow: bool) -> None:
    _store(ALLOW_REMOTE_KEY, "true" if allow else "false")
    logger.info(f"Management API remote access {'ENABLED' if allow else 'disabled'}")


def _presented_token(request: Request) -> Optional[str]:
    header = request.headers.get("authorization")
    if header is None:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise HTTPException(status_code=401, detail="Authorization must be 'Bearer <token>'")
    return value.strip()


def require_access(request: Request) -> None:
    """App-wide dependency: see the module docstring. Stores the presented token's last four characters on
    ``request.state.token_tail`` for the audit log."""
    request.state.token_tail = None
    is_write = request.method not in SAFE_METHODS
    host = request.client.host if request.client is not None else None
    is_local = host in LOCAL_HOSTS
    if not is_local and not get_allow_remote():
        raise HTTPException(status_code=403, detail={
            "error": "remote_disabled",
            "message": "API access from other machines is disabled on this instance "
                       "(Settings -> Management API -> Allow API access from other machines)."})
    if not is_write and is_local:
        return
    stored = get_api_token()
    if stored is None:
        raise HTTPException(
            status_code=403,
            detail="No api_token is set on this instance: writes and non-local requests are refused. "
                   "Generate one on the Settings page.")
    presented = _presented_token(request)
    if presented is None or not hmac.compare_digest(presented.encode(), stored.encode()):
        raise HTTPException(status_code=401, detail="Missing or wrong API token")
    request.state.token_tail = presented[-4:]

"""Token auth of the management API.

Rules (design section 1):
* A request is REMOTE unless its socket peer is a loopback address (``ipaddress``: 127.0.0.0/8, ::1, and the
  IPv4-mapped forms of those; an unparseable peer is remote) AND it carries none of the proxy headers
  ``X-Forwarded-For``, ``Forwarded``, ``X-Real-IP``, ``X-Forwarded-Host``. Those headers are client-controlled
  and are NEVER trusted to prove locality; their mere presence can only make the check stricter (a tunnel or
  reverse proxy in front of the platform shows a loopback peer, and the header betrays it).
* A remote request is refused with 403 ``remote_disabled`` unless the AppSetting ``api_allow_remote`` is on
  (default OFF). The check is applied by ``RemoteGateMiddleware`` before routing (so 404/405/307 paths cannot
  leak) and again by ``require_access``; it covers reads, writes, /docs and /openapi.json and runs BEFORE any
  token check. The setting is read per request, so a flip needs no restart.
* Every write needs ``Authorization: Bearer <api_token>``. A read needs no token only from a local request whose
  Host header names localhost / 127.0.0.1 / ::1 (DNS-rebinding guard: a browser tricked into resolving an
  attacker's name to 127.0.0.1 sends the attacker's Host); otherwise it needs the token.
* With NO token set, writes and token-requiring reads are refused with 403 -- nothing is open by default.
"""
import hmac
import ipaddress
import secrets
from typing import Optional

from fastapi import HTTPException, Request
from sqlmodel import select

from ..core.db import add_instance, get_db, update_instance
from ..core.models import AppSetting
from ..logger import logger

API_TOKEN_KEY = "api_token"
ALLOW_REMOTE_KEY = "api_allow_remote"
SAFE_METHODS = frozenset({"GET", "HEAD"})
LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})
FORWARD_HEADERS = frozenset({"x-forwarded-for", "forwarded", "x-real-ip", "x-forwarded-host"})
REMOTE_DISABLED_MESSAGE = ("API access from other machines is disabled on this instance "
                           "(Settings -> Management API -> Allow API access from other machines).")


def _read_setting(key: str) -> Optional[str]:
    """The AppSetting's value_str, or None for a missing row. A direct query, not ``get_setting``, which logs a
    'Setting not found' WARNING for every missing row (this is read on every request)."""
    with get_db() as session:
        row = session.exec(select(AppSetting).where(AppSetting.key == key)).first()
        return None if row is None else row.value_str


def get_api_token() -> Optional[str]:
    """The stored token, or None when none is set (missing row or blank value)."""
    value = _read_setting(API_TOKEN_KEY)
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
    value = _read_setting(ALLOW_REMOTE_KEY)
    return value is not None and value.strip().lower() in ("true", "1", "yes", "on")


def set_allow_remote(allow: bool) -> None:
    _store(ALLOW_REMOTE_KEY, "true" if allow else "false")
    logger.info(f"Management API remote access {'ENABLED' if allow else 'disabled'}")


def is_loopback_peer(host: Optional[str]) -> bool:
    """True iff ``host`` parses as a loopback IP address (IPv4-mapped IPv6 unwrapped). Anything else, including
    a non-IP such as 'testclient' or a missing peer, is not loopback."""
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def is_remote_scope(scope) -> bool:
    """Whether the ASGI request counts as REMOTE: a non-loopback peer, or any proxy/forwarding header."""
    client = scope["client"] if "client" in scope else None
    if client is None or not is_loopback_peer(client[0]):
        return True
    names = {name.decode("latin-1").lower() for name, _ in scope["headers"]}
    return not FORWARD_HEADERS.isdisjoint(names)


def _presented_token(request: Request) -> Optional[str]:
    header = request.headers.get("authorization")
    if header is None:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise HTTPException(status_code=401, detail="Authorization must be 'Bearer <token>'",
                            headers={"WWW-Authenticate": "Bearer"})
    return value.strip()


def require_access(request: Request) -> None:
    """App-wide dependency: see the module docstring. Stores the presented token's last four characters on
    ``request.state.token_tail`` for the audit log."""
    request.state.token_tail = None
    is_write = request.method not in SAFE_METHODS
    remote = is_remote_scope(request.scope)
    if remote and not get_allow_remote():
        raise HTTPException(status_code=403, detail={"error": "remote_disabled",
                                                     "message": REMOTE_DISABLED_MESSAGE})
    hostname = request.url.hostname
    if not is_write and not remote and hostname is not None and hostname.lower() in LOCAL_HOSTNAMES:
        return
    stored = get_api_token()
    if stored is None:
        raise HTTPException(
            status_code=403,
            detail="No api_token is set on this instance: writes and token-requiring reads are refused. "
                   "Generate one on the Settings page.")
    presented = _presented_token(request)
    if presented is None or not hmac.compare_digest(presented.encode(), stored.encode()):
        raise HTTPException(status_code=401, detail="Missing or wrong API token",
                            headers={"WWW-Authenticate": "Bearer"})
    request.state.token_tail = presented[-4:]

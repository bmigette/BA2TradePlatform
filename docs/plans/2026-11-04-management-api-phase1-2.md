# Management HTTP API, Phases 1 and 2 -- Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship the auth + skeleton of the `/api/v1` management API (phase 1) and the read endpoints that come straight from the DB and account methods (phase 2), as specified in `docs/plans/2026-11-04-management-api-design.md`.

**Architecture:** UI-free service functions in `ba2_trade_platform/services/` return plain dicts and raise typed errors; one FastAPI router per domain in `ba2_trade_platform/api/` turns those into HTTP. The routers live on a dedicated FastAPI sub-app (`create_v1_app()`), mounted at `/api/v1` from `ui/main.py`. The sub-app carries one app-wide auth dependency (`require_access`: GET/HEAD = read rule, every other method = write rule), its own JSON error envelope, and its own `/docs` + `/openapi.json` guarded by the same dependency. The three existing endpoints keep their implementation in `ui/api_routes.py` (several tests inspect that module's source); `/api/v1` re-registers the same function objects and the old `/api/*` paths stay as aliases behind the same auth.

**Tech Stack:** Python 3.11 (repo `.venv`), FastAPI 0.136 / Starlette 1.1 (`TestClient`), SQLModel, NiceGUI (Settings page card only), pytest.

---

## Ground rules for the implementer (read first)

1. **Run everything with the repo venv**: `.venv\Scripts\python.exe -m pytest ...` from `C:\Users\basti\Documents\dev\BA2TradePlatform`. One pytest process at a time. **Never run the full suite**; run only the files named in each task. **Never start, stop or call the running apps** (8080 dev, 8081 PROD real money, 8082 options). The only HTTP in this plan is `TestClient` against in-process apps. The `tools/api_smoke.py` of Task 21 is written but NOT run by this plan.
2. **Dirty working tree (not yours).** These files carry someone else's UNCOMMITTED changes: `ba2_trade_platform/core/app_settings.py`, `ba2_trade_platform/ui/pages/settings.py`, `tests/test_settings_dialog_missing_defaults.py` (plus AGENTS.md, CLAUDE.md, reports, etc.). Never revert, stage or commit their hunks. This plan does NOT touch `app_settings.py` or the test file (the `api_token` setting is deliberately NOT added to `APP_SETTINGS_DEFINITIONS`; it is not part of the "Save All Settings" flow). It touches `settings.py` in exactly one place (Task 6) and commits only that hunk. Always `git add <explicit paths>`; never `git add -A` / `git add .`.
3. **Commit messages** end with the two trailer lines your session was given (`Co-Authored-By: ...` and `Claude-Session: ...`). Do not push.
4. **Project rules that bite here** (CLAUDE.md): no `.get()` with defaults on config (use `d[key]`; `in` checks for optional keys); no fallback values for prices/balances/quantities (a figure that cannot be measured raises `Unmeasurable`, never reads as 0/null); `logger.error(..., exc_info=True)` only inside `except`; confidences are 1-100; settings access via `get_setting` / settings dicts.
5. **Where code goes.** New live-only code goes in-tree (`ba2_trade_platform/services`, `ba2_trade_platform/api`). Nothing in `packages/` changes, so there is no package version bump and no `testplatform/required_package_versions.py` change. Version bumps: `ba2_trade_platform/version.py` `APP_VERSION` +1 at the end of phase 1 (Task 10) and +1 at the end of phase 2 (Task 21).
6. **Test conventions.** Tests live in `tests/` (pytest collects only there), use the in-memory DB fixture from `tests/conftest.py` (autouse reset per test) and the factories in `tests/factories.py`. The DB-backed `get_setting` / `add_instance` helpers hit the patched test engine, as the existing `tests/test_api_routes_reload.py` relies on.

---

## PHASE 1 -- auth and skeleton

### Task 1: Typed service errors + shared JSON helpers

**Files:**
- Create: `ba2_trade_platform/services/__init__.py`
- Create: `ba2_trade_platform/services/errors.py`
- Create: `ba2_trade_platform/services/_common.py`
- Test: `tests/test_api_v1_services_common.py`

**Step 1: Write the failing test**

```python
"""Typed service errors and the shared serialisation / paging helpers of the management API."""
from datetime import date, datetime, timezone
from enum import Enum

import pytest
from sqlmodel import select

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import AccountDefinition
from ba2_trade_platform.core.types import TransactionStatus
from ba2_trade_platform.services import errors
from ba2_trade_platform.services._common import (
    jsonable, page, parse_enum, require_row, row_dict,
)
from tests.factories import create_account_definition


def test_error_classes_carry_code_and_status():
    cases = {
        errors.NotFound: (404, "not_found"),
        errors.Conflict: (409, "conflict"),
        errors.Invalid: (422, "invalid"),
        errors.BrokerRefused: (502, "broker_refused"),
        errors.BrokerUnreachable: (503, "broker_unreachable"),
        errors.Unmeasurable: (503, "unmeasurable"),
    }
    for cls, (status, code) in cases.items():
        e = cls("boom", {"k": 1})
        assert (e.status_code, e.code, e.message, e.detail) == (status, code, "boom", {"k": 1})
    assert errors.NotFound("x").detail == {}


def test_jsonable_converts_enums_dates_and_nested():
    class Color(str, Enum):
        RED = "red"
    out = jsonable({"c": Color.RED, "d": date(2026, 1, 2),
                    "t": datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
                    "l": [Color.RED, (1, 2)]})
    assert out == {"c": "red", "d": "2026-01-02", "t": "2026-01-02T03:04:00+00:00",
                   "l": ["red", [1, 2]]}


def test_jsonable_rejects_non_finite_floats_as_unmeasurable():
    with pytest.raises(errors.Unmeasurable):
        jsonable({"x": float("nan")})


def test_parse_enum_accepts_value_or_name_and_rejects_garbage():
    assert parse_enum(TransactionStatus, "OPENED", "status") is TransactionStatus.OPENED
    assert parse_enum(TransactionStatus, "opened", "status") is TransactionStatus.OPENED
    with pytest.raises(errors.Invalid) as ei:
        parse_enum(TransactionStatus, "nope", "status")
    assert ei.value.detail["field"] == "status"


def test_page_and_require_row():
    for i in range(5):
        create_account_definition(name=f"a{i}")
    with get_db() as s:
        out = page(s, select(AccountDefinition).order_by(AccountDefinition.id),
                   limit=2, offset=1, convert=row_dict)
        assert out["total"] == 5 and out["limit"] == 2 and out["offset"] == 1
        assert [r["name"] for r in out["items"]] == ["a1", "a2"]
        assert require_row(s, AccountDefinition, out["items"][0]["id"], "account")["id"]
        with pytest.raises(errors.NotFound):
            require_row(s, AccountDefinition, 99999, "account")
```

**Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api_v1_services_common.py -v`
Expected: FAIL / ERROR with `ModuleNotFoundError: No module named 'ba2_trade_platform.services'`.

**Step 3: Implement**

`ba2_trade_platform/services/__init__.py`:

```python
"""UI-free service functions behind the management HTTP API (see docs/plans/2026-11-04-management-api-design.md).

One function, two callers: the API router and (from phase 3) the NiceGUI page both call these, so
the API can never disagree with the UI. Functions return plain dicts and raise ``errors.ServiceError``
subclasses; nothing here imports NiceGUI or FastAPI.
"""
```

`ba2_trade_platform/services/errors.py`:

```python
"""Typed errors of the service layer. The API maps ``status_code`` / ``code`` onto the JSON envelope
``{"error": code, "message": ..., "detail": {...}}``."""
from typing import Any, Dict, Optional


class ServiceError(Exception):
    status_code: int = 500
    code: str = "internal"

    def __init__(self, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.detail: Dict[str, Any] = {} if detail is None else detail


class NotFound(ServiceError):
    status_code, code = 404, "not_found"


class Conflict(ServiceError):
    status_code, code = 409, "conflict"


class Invalid(ServiceError):
    status_code, code = 422, "invalid"


class BrokerRefused(ServiceError):
    """The broker answered and said no. ``message`` is the broker's own text."""
    status_code, code = 502, "broker_refused"


class BrokerUnreachable(ServiceError):
    status_code, code = 503, "broker_unreachable"


class Unmeasurable(ServiceError):
    """A figure the request asks for cannot be measured. The request fails; it never reads as 0 or null."""
    status_code, code = 503, "unmeasurable"
```

`ba2_trade_platform/services/_common.py`:

```python
"""Serialisation, paging and lookup helpers shared by every service module."""
import dataclasses
import math
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, Type

from sqlalchemy import func
from sqlmodel import select

from .errors import Invalid, NotFound, Unmeasurable


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def jsonable(value: Any) -> Any:
    """Recursively convert ``value`` to JSON-serialisable plain data.

    Enums -> their value; datetime/date -> ISO text; SQLModel/pydantic -> ``model_dump``; dataclass,
    NamedTuple, dict, list, tuple, set recurse. A NaN/inf float RAISES ``Unmeasurable`` -- JSON cannot carry
    it and silently turning it into null/0 would hide an unmeasured figure."""
    if value is None or isinstance(value, (bool, int, str)):
        return value.value if isinstance(value, Enum) else value
    if isinstance(value, Enum):
        return jsonable(value.value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise Unmeasurable(f"A non-finite figure ({value!r}) cannot be returned")
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return jsonable(value.model_dump())
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return jsonable(dataclasses.asdict(value))
    if hasattr(value, "_asdict"):
        return jsonable(value._asdict())
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in value]
    return str(value)


def row_dict(row: Any) -> Dict[str, Any]:
    """One DB row (columns only, no relationships) as a JSON-safe dict."""
    return jsonable(row.model_dump())


def parse_enum(enum_cls: Type[Enum], text: str, field: str) -> Enum:
    """Accept an enum VALUE or NAME (case-insensitive name); anything else is ``Invalid`` (422)."""
    for member in enum_cls:
        if text == member.value or text.upper() == member.name:
            return member
    raise Invalid(f"{field}={text!r} is not one of {[m.value for m in enum_cls]}", {"field": field})


def require_row(session, model, row_id: int, label: str):
    row = session.get(model, row_id)
    if row is None:
        raise NotFound(f"No {label} with id {row_id}", {"id": row_id})
    return row


def page(session, stmt, *, limit: int, offset: int, convert: Callable[[Any], Dict[str, Any]]) -> Dict[str, Any]:
    """Run ``stmt`` (an ordered ``select``) as one page: ``{items, limit, offset, total}``."""
    total = session.exec(select(func.count()).select_from(stmt.order_by(None).subquery())).one()
    rows = session.exec(stmt.limit(limit).offset(offset)).all()
    return {"items": [convert(r) for r in rows], "limit": limit, "offset": offset, "total": total}
```

**Step 4: Run to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api_v1_services_common.py -v`
Expected: 5 passed.

**Step 5: Commit**

```bash
git add ba2_trade_platform/services/__init__.py ba2_trade_platform/services/errors.py ba2_trade_platform/services/_common.py tests/test_api_v1_services_common.py
git commit -m "feat(api): typed service errors and shared JSON/paging helpers"
```

---

### Task 2: Token storage + auth dependency (the auth matrix)

**Files:**
- Create: `ba2_trade_platform/api/__init__.py`
- Create: `ba2_trade_platform/api/auth.py`
- Create: `tests/api_helpers.py`
- Test: `tests/test_api_v1_auth.py`

**Step 1: Write the failing test**

`tests/api_helpers.py` (shared by every API test):

```python
"""Helpers for the management-API tests: token setup and TestClients with a chosen peer address."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import AppSetting

TOKEN = "t" * 40 + "abcd"


def set_api_token(token: str = TOKEN) -> None:
    add_instance(AppSetting(key="api_token", value_str=token), expunge_after_flush=True)


def auth_headers(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def client_for(app: FastAPI, *, host: str = "127.0.0.1", token: str | None = None) -> TestClient:
    """TestClient whose peer address is ``host`` (the auth rule reads the socket peer) and which sends
    ``Authorization: Bearer <token>`` when ``token`` is given."""
    return TestClient(app, client=(host, 50000), headers=auth_headers(token) if token else {})
```

`tests/test_api_v1_auth.py`:

```python
"""Auth matrix of the management API: {local, remote} x {read, write} x {token unset, set, wrong}."""
import pytest
from fastapi import Depends, FastAPI

from ba2_trade_platform.api.auth import (
    API_TOKEN_KEY, get_api_token, require_access, rotate_api_token,
)
from tests.api_helpers import TOKEN, client_for, set_api_token


def _app() -> FastAPI:
    app = FastAPI(dependencies=[Depends(require_access)])

    @app.get("/r")
    def r():
        return {"ok": True}

    @app.post("/w")
    def w():
        return {"ok": True}

    return app


def test_no_token_set_local_read_open_everything_else_403():
    assert client_for(_app()).get("/r").status_code == 200
    assert client_for(_app()).post("/w").status_code == 403
    assert client_for(_app(), host="192.168.1.9").get("/r").status_code == 403
    # Even a presented token cannot open anything when none is set.
    assert client_for(_app(), token="whatever").post("/w").status_code == 403


def test_token_set_write_requires_bearer_everywhere():
    set_api_token()
    for host in ("127.0.0.1", "192.168.1.9"):
        assert client_for(_app(), host=host).post("/w").status_code == 401
        assert client_for(_app(), host=host, token="wrong" * 8).post("/w").status_code == 401
        assert client_for(_app(), host=host, token=TOKEN).post("/w").status_code == 200


def test_token_set_remote_read_requires_bearer_local_read_does_not():
    set_api_token()
    assert client_for(_app()).get("/r").status_code == 200
    assert client_for(_app(), host="10.0.0.5").get("/r").status_code == 401
    assert client_for(_app(), host="10.0.0.5", token="x" * 44).get("/r").status_code == 401
    assert client_for(_app(), host="10.0.0.5", token=TOKEN).get("/r").status_code == 200


def test_ipv6_loopback_counts_as_local():
    assert client_for(_app(), host="::1").get("/r").status_code == 200


def test_malformed_authorization_header_is_401():
    set_api_token()
    c = client_for(_app(), host="10.0.0.5")
    assert c.get("/r", headers={"Authorization": "Basic abc"}).status_code == 401
    assert c.get("/r", headers={"Authorization": "Bearer"}).status_code == 401


def test_blank_stored_token_counts_as_unset():
    set_api_token("   ")
    assert get_api_token() is None
    assert client_for(_app()).post("/w", headers={"Authorization": "Bearer    "}).status_code == 403


def test_rotate_api_token_stores_and_replaces():
    first = rotate_api_token()
    assert len(first) >= 40 and get_api_token() == first
    second = rotate_api_token()
    assert second != first and get_api_token() == second
    assert API_TOKEN_KEY == "api_token"


def test_write_success_records_token_last_four_only():
    set_api_token()
    app = FastAPI(dependencies=[Depends(require_access)])
    seen = {}

    @app.post("/w")
    def w(request: __import__("fastapi").Request):
        seen["tail"] = request.state.token_tail
        return {}

    client_for(app, token=TOKEN).post("/w")
    assert seen["tail"] == TOKEN[-4:] == "abcd"
```

**Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api_v1_auth.py -v`
Expected: ERROR `ModuleNotFoundError: No module named 'ba2_trade_platform.api'`.

**Step 3: Implement**

`ba2_trade_platform/api/__init__.py`:

```python
"""Management HTTP API (FastAPI routers over ``ba2_trade_platform.services``). Mounted at ``/api/v1`` by
``ui/main.py`` via ``api.app.create_v1_app``."""
```

`ba2_trade_platform/api/auth.py`:

```python
"""Token auth of the management API.

Rules (design section 1): every write needs ``Authorization: Bearer <api_token>``; a read needs no token from
the loopback address and the token from anywhere else; with NO token set, writes and non-local requests are
refused with 403 -- nothing is open by default.

The caller address is the SOCKET PEER (``request.client.host``). ``X-Forwarded-For`` is deliberately not read:
it is client-controlled, and trusting it would let any LAN host claim to be local. The platform is not run
behind a reverse proxy; if that ever changes, a local read through the proxy would present the proxy's
address and need the token, which fails closed.
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
SAFE_METHODS = frozenset({"GET", "HEAD"})
LOCAL_HOSTS = frozenset({"127.0.0.1", "::1"})


def get_api_token() -> Optional[str]:
    """The stored token, or None when none is set (missing row or blank value)."""
    value = get_setting(API_TOKEN_KEY)
    if value is None or not value.strip():
        return None
    return value.strip()


def rotate_api_token() -> str:
    """Generate a fresh token, store it (replacing any previous one) and return it. The caller shows it to
    the operator ONCE; it is never logged."""
    token = secrets.token_urlsafe(32)
    with get_db() as session:
        row = session.exec(select(AppSetting).where(AppSetting.key == API_TOKEN_KEY)).first()
        if row is None:
            add_instance(AppSetting(key=API_TOKEN_KEY, value_str=token), session)
        else:
            row.value_str = token
            update_instance(row, session)
    logger.info("Management API token generated (stored in AppSetting 'api_token')")
    return token


def _presented_token(request: Request) -> Optional[str]:
    header = request.headers.get("authorization")
    if header is None:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise HTTPException(status_code=401, detail="Authorization must be 'Bearer <token>'")
    return value.strip()


def require_access(request: Request) -> None:
    """App-wide dependency: GET/HEAD follow the read rule, every other method the write rule. Stores the
    presented token's last four characters on ``request.state.token_tail`` for the audit log."""
    request.state.token_tail = None
    is_write = request.method not in SAFE_METHODS
    host = request.client.host if request.client is not None else None
    if not is_write and host in LOCAL_HOSTS:
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
```

**Step 4: Run to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api_v1_auth.py -v`
Expected: 8 passed. If `get_setting` returns `None` in tests because it does not see the patched engine, STOP and report it (the existing `test_api_routes_reload.py` relies on the same patched engine, so it should work).

**Step 5: Commit**

```bash
git add ba2_trade_platform/api/__init__.py ba2_trade_platform/api/auth.py tests/api_helpers.py tests/test_api_v1_auth.py
git commit -m "feat(api): api_token storage and read/write auth dependency"
```

---

### Task 3: JSON error envelope handlers

**Files:**
- Create: `ba2_trade_platform/api/errors.py`
- Test: `tests/test_api_v1_errors.py`

**Step 1: Write the failing test**

```python
"""The /api/v1 error envelope: {"error", "message", "detail"} for service, HTTP, validation and crash errors."""
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from ba2_trade_platform.api.errors import install_error_handlers
from ba2_trade_platform.services import errors as se


class Body(BaseModel):
    n: int


def _client() -> TestClient:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/nf")
    def nf():
        raise se.NotFound("no such thing", {"id": 7})

    @app.get("/broker")
    def broker():
        raise se.BrokerRefused("40310000 wash trade", {"code": 40310000})

    @app.get("/http")
    def http():
        raise HTTPException(status_code=403, detail="nope")

    @app.get("/crash")
    def crash():
        raise RuntimeError("secret internals")

    @app.post("/v")
    def v(body: Body):
        return body

    return TestClient(app, raise_server_exceptions=False)


def test_service_errors_use_the_envelope():
    r = _client().get("/nf")
    assert r.status_code == 404
    assert r.json() == {"error": "not_found", "message": "no such thing", "detail": {"id": 7}}
    r = _client().get("/broker")
    assert r.status_code == 502 and r.json()["error"] == "broker_refused"
    assert r.json()["message"] == "40310000 wash trade"


def test_http_exceptions_are_enveloped_with_a_code_per_status():
    r = _client().get("/http")
    assert r.status_code == 403
    assert r.json() == {"error": "forbidden", "message": "nope", "detail": {}}


def test_validation_errors_are_422_invalid_with_field_errors():
    r = _client().post("/v", json={"n": "x"})
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "invalid" and body["detail"]["errors"][0]["loc"][-1] == "n"


def test_unhandled_exception_is_500_internal_without_leaking_the_message():
    r = _client().get("/crash")
    assert r.status_code == 500
    assert r.json() == {"error": "internal", "message": "Internal error (see the platform log)", "detail": {}}
```

**Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api_v1_errors.py -v`
Expected: ERROR `ModuleNotFoundError: ba2_trade_platform.api.errors`.

**Step 3: Implement** `ba2_trade_platform/api/errors.py`:

```python
"""JSON error envelope of /api/v1: ``{"error": code, "message": ..., "detail": {...}}``.

Installed on the v1 sub-app only; the NiceGUI app and the legacy ``/api/*`` aliases keep FastAPI's default
``{"detail": ...}`` bodies."""
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..logger import logger
from ..services.errors import ServiceError

_HTTP_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
               405: "method_not_allowed", 409: "conflict", 422: "invalid"}


def envelope(error: str, message: str, detail: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return {"error": error, "message": message, "detail": {} if detail is None else detail}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def _service_error(request: Request, exc: ServiceError):
        return JSONResponse(status_code=exc.status_code,
                            content=envelope(exc.code, exc.message, exc.detail))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        code = _HTTP_CODES[exc.status_code] if exc.status_code in _HTTP_CODES else "http_error"
        return JSONResponse(status_code=exc.status_code, content=envelope(code, str(exc.detail)),
                            headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content=envelope(
            "invalid", "Request validation failed", {"errors": jsonable_encoder(exc.errors())}))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logger.error(f"API {request.method} {request.url.path} crashed: {exc}", exc_info=True)
        return JSONResponse(status_code=500,
                            content=envelope("internal", "Internal error (see the platform log)"))
```

**Step 4:** Run `.venv\Scripts\python.exe -m pytest tests/test_api_v1_errors.py -v`. Expected: 4 passed.

**Step 5: Commit**

```bash
git add ba2_trade_platform/api/errors.py tests/test_api_v1_errors.py
git commit -m "feat(api): JSON error envelope for /api/v1"
```

---

### Task 4: Audit logging for writes

**Files:**
- Create: `ba2_trade_platform/api/audit.py`
- Test: `tests/test_api_v1_audit.py`

**Design note.** `AuditedRoute` is a custom `APIRoute` class used by every router that has writes. For any non-GET/HEAD request it logs one INFO line (caller IP, method + path, truncated arguments, outcome, token last four) and writes one `ActivityLog` row with `data["source"] == "api"`. A write refused by auth (no `token_tail`) logs a WARNING and writes NO ActivityLog row. There is no `ActivityLogType` for API writes and adding one means editing `packages/common` (version bump); this plan reuses `ActivityLogType.APPLICATION_STATUS_CHANGE` and marks the row with `data["source"] = "api"` (see Open Questions). Phase 3 must redact secret keys from `arguments` before logging PATCH-settings bodies.

**Step 1: Write the failing test**

```python
"""Audit trail of API writes: one INFO log line and one ActivityLog row (source 'api'), token last 4 only."""
import json
import logging

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from sqlmodel import select

from ba2_trade_platform.api.audit import AuditedRoute
from ba2_trade_platform.api.auth import require_access
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import ActivityLog
from tests.api_helpers import TOKEN, client_for, set_api_token


def _app() -> FastAPI:
    app = FastAPI(dependencies=[Depends(require_access)])
    router = APIRouter(route_class=AuditedRoute)

    @router.post("/act")
    def act(payload: dict):
        return {"done": True}

    @router.post("/boom")
    def boom():
        raise HTTPException(status_code=409, detail="busy")

    @router.get("/read")
    def read():
        return {}

    app.include_router(router)
    return app


def _flush_activity_log():
    """log_activity is queued to a worker thread; write synchronously for the test instead."""
    from ba2_trade_platform.core import db
    while not db._activity_log_queue.empty():
        sev, typ, desc, data, ex, ac = db._activity_log_queue.get()
        from ba2_trade_platform.core.db import add_instance
        add_instance(ActivityLog(severity=sev, type=typ, description=desc, data=data or {},
                                 source_expert_id=ex, source_account_id=ac))


def test_write_is_audited_with_token_tail_only(caplog, monkeypatch):
    set_api_token()
    from ba2_trade_platform.api import audit
    captured = []
    monkeypatch.setattr(audit, "log_activity", lambda *a, **k: captured.append((a, k)))
    caplog.set_level(logging.INFO)
    r = client_for(_app(), token=TOKEN).post("/act", json={"x": 1})
    assert r.status_code == 200
    (args, kwargs), = captured
    data = kwargs["data"]
    assert data["source"] == "api" and data["path"] == "/act" and data["status"] == 200
    assert data["token_tail"] == "abcd" and data["client"] == "127.0.0.1"
    assert json.loads(data["arguments"]) == {"x": 1}
    assert TOKEN not in json.dumps(data)
    lines = [m for m in caplog.messages if "API write" in m]
    assert lines and "abcd" in lines[0] and TOKEN not in lines[0]


def test_failed_write_is_audited_with_its_status(monkeypatch):
    set_api_token()
    from ba2_trade_platform.api import audit
    captured = []
    monkeypatch.setattr(audit, "log_activity", lambda *a, **k: captured.append(k["data"]))
    assert client_for(_app(), token=TOKEN).post("/boom").status_code == 409
    assert captured[0]["status"] == 409


def test_reads_and_refused_writes_write_no_activity_row(monkeypatch):
    set_api_token()
    from ba2_trade_platform.api import audit
    captured = []
    monkeypatch.setattr(audit, "log_activity", lambda *a, **k: captured.append(k["data"]))
    client_for(_app()).get("/read")
    assert client_for(_app()).post("/act", json={}).status_code == 401   # no bearer
    assert captured == []
```

(`_flush_activity_log` is kept for a manual end-to-end check; delete it if unused when you finish the task -- do not leave dead code.)

**Step 2:** Run `.venv\Scripts\python.exe -m pytest tests/test_api_v1_audit.py -v`. Expected: ERROR `ModuleNotFoundError: ba2_trade_platform.api.audit`.

**Step 3: Implement** `ba2_trade_platform/api/audit.py`:

```python
"""Audit trail for management-API writes (design section 1, "Audit")."""
import json
from typing import Callable

from fastapi import Request, Response
from fastapi.routing import APIRoute

from ..core.db import log_activity
from ..core.types import ActivityLogSeverity, ActivityLogType
from ..logger import logger
from .auth import SAFE_METHODS

_MAX_ARGUMENT_CHARS = 500


def _arguments(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace")
    try:
        text = json.dumps(json.loads(text), sort_keys=True) if text.strip() else ""
    except ValueError:
        pass
    return text[:_MAX_ARGUMENT_CHARS]


def audit_write(request: Request, body: bytes, status: int) -> None:
    client = request.client.host if request.client is not None else "unknown"
    tail = request.state.token_tail if hasattr(request.state, "token_tail") else None
    where = f"{request.method} {request.url.path}"
    if tail is None:     # refused by auth (or the dependency never ran): never an authenticated write
        logger.warning(f"API write refused: {where} from {client} -> {status}")
        return
    arguments = _arguments(body)
    logger.info(f"API write: {where} from {client} token=...{tail} args={arguments} -> {status}")
    log_activity(
        ActivityLogSeverity.SUCCESS if status < 400 else ActivityLogSeverity.WARNING,
        ActivityLogType.APPLICATION_STATUS_CHANGE,
        f"API {where} -> {status}",
        data={"source": "api", "method": request.method, "path": request.url.path, "status": status,
              "client": client, "token_tail": tail, "arguments": arguments},
    )


class AuditedRoute(APIRoute):
    """Route class for routers that hold writes: every non-GET/HEAD request is audited, whatever its outcome."""

    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            if request.method in SAFE_METHODS:
                return await original(request)
            body = await request.body()
            try:
                response = await original(request)
            except Exception as exc:
                status = exc.status_code if hasattr(exc, "status_code") else 500
                audit_write(request, body, status)
                raise
            audit_write(request, body, response.status_code)
            return response

        return handler
```

**Step 4:** Run `.venv\Scripts\python.exe -m pytest tests/test_api_v1_audit.py -v`. Expected: 3 passed.

**Step 5: Commit**

```bash
git add ba2_trade_platform/api/audit.py tests/test_api_v1_audit.py
git commit -m "feat(api): audit log (INFO line + ActivityLog source api) for API writes"
```

---

### Task 5: The v1 app: `create_v1_app()`, `/ping`, guarded `/docs` + `/openapi.json`, three endpoints under `/api/v1`, legacy aliases behind auth

**Files:**
- Create: `ba2_trade_platform/api/app.py`
- Create: `ba2_trade_platform/api/maintenance.py`
- Modify: `ba2_trade_platform/ui/api_routes.py` (router declaration only, lines 16-21; no function body changes)
- Modify: `tests/test_api_routes_reload.py`, `tests/test_api_routes_run_schedule.py`, `tests/test_api_routes_process_recommendations.py` (their `_client()` helpers only)
- Test: `tests/test_api_v1_app.py`

**Why the bodies stay in `ui/api_routes.py`:** `tests/test_atm_iv_review2.py` and `tests/test_live_review_l2_l3_l4.py` assert on `inspect.getsource(api_routes)`, and `tests/test_market_condition_live_context.py` calls `api_routes.reload_from_db` directly. Moving the functions would break them for no gain. `/api/v1` re-registers the same function objects.

**Step 1: Write the failing test** `tests/test_api_v1_app.py`:

```python
"""The /api/v1 sub-app: auth on everything incl. docs, envelope errors, the three maintenance endpoints,
and the legacy /api/* aliases staying alive behind the same auth."""
from fastapi import FastAPI

from ba2_trade_platform.api.app import create_v1_app
from ba2_trade_platform.core.ExpertInstanceCache import ExpertInstanceCache
from ba2_trade_platform.ui import api_routes
from ba2_trade_platform.version import APP_VERSION
from tests.api_helpers import TOKEN, client_for, set_api_token


def test_ping_reports_version_to_local_reader_without_token():
    r = client_for(create_v1_app()).get("/ping")
    assert r.status_code == 200 and r.json() == {"status": "ok", "app_version": APP_VERSION}


def test_docs_and_openapi_follow_the_read_rule():
    set_api_token()
    local = client_for(create_v1_app())
    assert local.get("/openapi.json").status_code == 200
    assert "text/html" in local.get("/docs").headers["content-type"]
    remote = client_for(create_v1_app(), host="10.1.1.1")
    assert remote.get("/openapi.json").status_code == 401
    assert remote.get("/docs").status_code == 401
    assert client_for(create_v1_app(), host="10.1.1.1", token=TOKEN).get("/openapi.json").status_code == 200


def test_openapi_lists_the_three_maintenance_posts_and_ping():
    spec = client_for(create_v1_app()).get("/openapi.json").json()
    for path in ("/reload", "/run-schedule", "/process-recommendations"):
        assert "post" in spec["paths"][path]
    assert "get" in spec["paths"]["/ping"]


def test_unknown_route_is_an_enveloped_404():
    r = client_for(create_v1_app()).get("/nope")
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_v1_reload_needs_the_write_token_and_works_with_it():
    ExpertInstanceCache._cache[991] = object()
    assert client_for(create_v1_app()).post("/reload", json={}).status_code == 403   # no token set
    set_api_token()
    assert client_for(create_v1_app()).post("/reload", json={}).status_code == 401
    r = client_for(create_v1_app(), token=TOKEN).post("/reload", json={})
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_mounted_under_api_v1_next_to_the_legacy_aliases():
    set_api_token()
    host = FastAPI()
    host.include_router(api_routes.router)
    host.mount("/api/v1", create_v1_app())
    c = client_for(host, token=TOKEN)
    assert c.get("/api/v1/ping").status_code == 200
    assert c.post("/api/v1/reload", json={}).status_code == 200
    assert c.post("/api/reload", json={}).status_code == 200            # legacy alias, same handler
    # The alias is behind the same auth (design: it also applies to the three existing endpoints).
    assert client_for(host).post("/api/reload", json={}).status_code == 401


def test_legacy_alias_with_no_token_set_is_403():
    host = FastAPI()
    host.include_router(api_routes.router)
    assert client_for(host).post("/api/reload", json={}).status_code == 403
```

**Step 2:** Run `.venv\Scripts\python.exe -m pytest tests/test_api_v1_app.py -v`. Expected: ERROR `ModuleNotFoundError: ba2_trade_platform.api.app`.

**Step 3: Implement.**

`ba2_trade_platform/api/maintenance.py`:

```python
"""The three pre-existing operations endpoints, under /api/v1. Their implementation stays in
``ui/api_routes.py`` (also served at the legacy /api/* paths); this router registers the SAME function
objects, so the two paths cannot drift."""
from fastapi import APIRouter

from ..ui import api_routes
from .audit import AuditedRoute

router = APIRouter(tags=["maintenance"], route_class=AuditedRoute)

router.add_api_route("/reload", api_routes.reload_from_db, methods=["POST"])
router.add_api_route("/run-schedule", api_routes.run_schedule_now, methods=["POST"])
router.add_api_route("/process-recommendations", api_routes.process_recommendations, methods=["POST"])
```

`ba2_trade_platform/api/system.py`:

```python
"""Liveness / version probe: the smallest read, used by tools/api_smoke.py and the MCP client."""
from fastapi import APIRouter

from ..version import APP_VERSION

router = APIRouter(tags=["system"])


@router.get("/ping")
def ping():
    return {"status": "ok", "app_version": APP_VERSION}
```

`ba2_trade_platform/api/app.py`:

```python
"""Factory of the /api/v1 sub-app. Phase 2 routers are registered in ``_ROUTER_MODULES`` as they land."""
from importlib import import_module

from fastapi import Depends, FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html

from ..version import APP_VERSION
from .auth import require_access
from .errors import install_error_handlers

#: Router modules (relative to this package), each exposing ``router``.
_ROUTER_MODULES = ["system", "maintenance"]


def create_v1_app() -> FastAPI:
    # One app-wide dependency: GET/HEAD = read rule, other methods = write rule. A router that forgets to
    # declare auth is still guarded. /docs and /openapi.json are declared by hand (FastAPI's built-in
    # doc routes cannot carry dependencies) so they sit under the same read rule.
    app = FastAPI(title="BA2 Trade Platform management API", version=APP_VERSION,
                  docs_url=None, redoc_url=None, openapi_url=None,
                  dependencies=[Depends(require_access)])
    install_error_handlers(app)

    @app.get("/openapi.json", include_in_schema=False)
    def openapi_json():
        return app.openapi()

    @app.get("/docs", include_in_schema=False)
    def docs(request: Request):
        return get_swagger_ui_html(openapi_url=request.scope["root_path"] + "/openapi.json",
                                   title=f"{app.title} - docs")

    for name in _ROUTER_MODULES:
        app.include_router(import_module(f"{__package__}.{name}").router)
    return app
```

Edit `ba2_trade_platform/ui/api_routes.py` lines 16-21 -- replace

```python
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..logger import logger

router = APIRouter(prefix="/api", tags=["reload"])
```

with

```python
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..api.audit import AuditedRoute
from ..api.auth import require_access
from ..logger import logger

# Legacy /api/* paths: aliases of the /api/v1 maintenance endpoints (api/maintenance.py registers the same
# functions). Behind the same token rule as /api/v1 -- with no api_token set they answer 403.
router = APIRouter(prefix="/api", tags=["reload"], route_class=AuditedRoute,
                   dependencies=[Depends(require_access)])
```

and update the module docstring's first sentence block by adding the line `Also served under /api/v1 (see ba2_trade_platform/api/maintenance.py); these paths are the legacy aliases.` after the first paragraph.

Circular-import check: `api/maintenance.py` imports `ui.api_routes`, which imports `api.audit` / `api.auth` (no import of `api.app`/`maintenance`) -- no cycle. `import ba2_trade_platform.ui` runs `ui/__init__.py`; the existing tests already import `ba2_trade_platform.ui.api_routes`, so that is already safe.

Update the three existing test files' `_client()`; in each, add `from tests.api_helpers import TOKEN, client_for, set_api_token` and replace the body:

```python
def _client():
    set_api_token()
    app = FastAPI()
    app.include_router(api_routes.router)
    return client_for(app, token=TOKEN)
```

(Remove the now-unused `TestClient` import only if nothing else in that file uses it.)

**Step 4: Run to verify**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api_v1_app.py tests/test_api_routes_reload.py tests/test_api_routes_run_schedule.py tests/test_api_routes_process_recommendations.py tests/test_atm_iv_review2.py::test_reload_invalidates_gate_cache -v` (if that last node id does not exist, run `tests/test_atm_iv_review2.py -k api_routes` instead).
Expected: all pass. Then run `.venv\Scripts\python.exe -m pytest tests/test_live_review_l2_l3_l4.py tests/test_market_condition_live_context.py -k "api_routes or reload or source" -v` -- expected pass (they inspect source / call the function directly).

**Step 5: Commit**

```bash
git add ba2_trade_platform/api/app.py ba2_trade_platform/api/maintenance.py ba2_trade_platform/api/system.py ba2_trade_platform/ui/api_routes.py tests/test_api_v1_app.py tests/test_api_routes_reload.py tests/test_api_routes_run_schedule.py tests/test_api_routes_process_recommendations.py
git commit -m "feat(api): /api/v1 sub-app with guarded docs, maintenance endpoints and legacy aliases behind auth"
```

---

### Task 6: Settings page "Generate" card for the API token

**Files:**
- Create: `ba2_trade_platform/ui/pages/api_token_card.py`
- Modify: `ba2_trade_platform/ui/pages/settings.py` (ONE insertion in `AppSettingsTab.render`, between the Broker API Keys card and the "System Settings Section" banner, around line 1026-1030 -- locate by the `# System Settings Section` banner comment, NOT by line number: the file has someone else's uncommitted edits)
- Test: `tests/test_api_token_card.py`

**Step 1: Write the failing test** (logic only; NiceGUI rendering is not unit-tested in this repo for new cards):

```python
"""Logic behind the Settings-page API token card."""
from ba2_trade_platform.api.auth import get_api_token
from ba2_trade_platform.ui.pages.api_token_card import generate_token_for_ui, token_status_text


def test_status_text_reflects_stored_token_without_revealing_it():
    assert "No API token" in token_status_text()
    token = generate_token_for_ui()
    text = token_status_text()
    assert "set" in text and token not in text and token[-4:] in text


def test_generate_replaces_the_stored_token():
    first = generate_token_for_ui()
    second = generate_token_for_ui()
    assert first != second and get_api_token() == second
```

**Step 2:** Run `.venv\Scripts\python.exe -m pytest tests/test_api_token_card.py -v`. Expected: ERROR (module missing).

**Step 3: Implement** `ba2_trade_platform/ui/pages/api_token_card.py`:

```python
"""Settings-page card that generates the management-API token (AppSetting 'api_token').

The token is written the moment you press Generate (it is NOT part of 'Save All Settings') and shown ONCE in
a dialog; afterwards only its last four characters are displayed. Regenerating invalidates every client that
holds the old token (a confirm dialog says so)."""
from nicegui import ui

from ...api.auth import get_api_token, rotate_api_token


def token_status_text() -> str:
    token = get_api_token()
    if token is None:
        return "No API token set: API writes and non-local API reads are refused (403)."
    return f"API token is set (ends in ...{token[-4:]})."


def generate_token_for_ui() -> str:
    return rotate_api_token()


def render_api_token_card() -> None:
    with ui.card().classes('w-full mb-4'):
        ui.label('Management API').classes('text-xl font-bold mb-2')
        ui.label('HTTP API under /api/v1 (docs at /api/v1/docs). Writes always need '
                 '"Authorization: Bearer <token>"; reads need it from any address except 127.0.0.1.'
                 ).classes('text-sm text-gray-500 mb-2')
        status = ui.label(token_status_text()).classes('text-sm')

        def _show_new_token():
            token = generate_token_for_ui()
            status.set_text(token_status_text())
            with ui.dialog() as dialog, ui.card():
                ui.label('New API token (shown once -- copy it now)').classes('font-bold')
                ui.input(value=token).props('readonly outlined').classes('w-96')
                ui.button('Copy', icon='content_copy',
                          on_click=lambda: ui.run_javascript(f'navigator.clipboard.writeText({token!r})'))
                ui.button('Close', on_click=dialog.close)
            dialog.open()

        def _on_click():
            if get_api_token() is None:
                _show_new_token()
                return
            with ui.dialog() as confirm, ui.card():
                ui.label('Generate a new token? The current token stops working immediately.')
                with ui.row():
                    ui.button('Cancel', on_click=confirm.close)
                    ui.button('Generate', color='negative',
                              on_click=lambda: (confirm.close(), _show_new_token()))
            confirm.open()

        ui.button('Generate token', icon='key', on_click=_on_click)
```

Then in `settings.py`, insert (immediately before the `# =====` banner line that precedes `# System Settings Section`; keep the existing blank line style):

```python
        from .api_token_card import render_api_token_card
        render_api_token_card()

```

**Step 4: Run** `.venv\Scripts\python.exe -m pytest tests/test_api_token_card.py -v` (expected 2 passed), then a no-render sanity import: `.venv\Scripts\python.exe -c "import ba2_trade_platform.ui.pages.api_token_card"` (expected no output). Do not start the app. Then `.venv\Scripts\python.exe -m pytest tests/test_settings_dialog_missing_defaults.py -k "app_settings or AppSettingsTab" -v` to confirm the tab still builds (it has someone's uncommitted edits; if it fails for a reason unrelated to your insertion, note it, do not fix it).

**Step 5: Commit ONLY your hunk of `settings.py`.** The file also holds the other person's two `replay_capture` hunks (near `_APP_CONTROLS` and near the System Settings switches); yours is a third, separate hunk:

```bash
git diff ba2_trade_platform/ui/pages/settings.py          # expect 3 hunks; yours is the api_token_card one
printf 'n\ny\nn\n' | git add -p ba2_trade_platform/ui/pages/settings.py    # answer for hunks 1,2,3 in diff order: skip, take, skip
git diff --cached ba2_trade_platform/ui/pages/settings.py   # MUST show only the render_api_token_card lines
git diff --cached --stat                                    # MUST list only settings.py
git add ba2_trade_platform/ui/pages/api_token_card.py tests/test_api_token_card.py
git commit -m "feat(settings): Generate button for the management API token"
```

If the hunk order differs from the 3-hunk assumption, adjust the y/n sequence; if `git add -p` cannot be scripted, write your hunk as a patch (`git diff -U0 ... > scratch.patch`, delete the foreign hunks) and `git apply --cached --unidiff-zero scratch.patch`. If you cannot isolate the hunk cleanly, STOP and ask the user rather than committing their changes. After the commit, `git diff ba2_trade_platform/ui/pages/settings.py` must still show the two foreign `replay_capture` hunks.

---

### Task 7: Mount the v1 app from `ui/main.py`

**Files:**
- Modify: `ba2_trade_platform/ui/main.py` (lines 1-10)
- Test: `tests/test_api_v1_mount.py`

**Step 1: Write the failing test** (importing `ui/main.py` calls `ui.run`, so test the wiring helper instead):

```python
"""ui/main.py wires the API through register_management_api(app); test the wiring on a bare FastAPI."""
from fastapi import FastAPI

from ba2_trade_platform.api.app import register_management_api
from tests.api_helpers import client_for


def test_register_management_api_mounts_v1_and_legacy_aliases():
    app = FastAPI()
    register_management_api(app)
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/reload" in paths and "/api/v1" in paths
    assert client_for(app).get("/api/v1/ping").status_code == 200
```

**Step 2:** Run `.venv\Scripts\python.exe -m pytest tests/test_api_v1_mount.py -v`. Expected: FAIL `ImportError: cannot import name 'register_management_api'`.

**Step 3: Implement.** Append to `ba2_trade_platform/api/app.py`:

```python
def register_management_api(host_app: FastAPI) -> None:
    """Attach the management API to the NiceGUI/FastAPI host app: the legacy ``/api/*`` aliases (routes) and
    the ``/api/v1`` sub-app (mount). Called once from ``ui/main.py``."""
    from ..ui import api_routes
    host_app.include_router(api_routes.router)
    host_app.mount("/api/v1", create_v1_app())
```

In `ba2_trade_platform/ui/main.py` replace line 4 `from . import api_routes` and lines 8-10:

```python
from . import api_routes
```
->
```python
from ..api.app import register_management_api
```
and
```python
# Plain HTTP API routes (non-UI): DB reload callback etc. `app` here is the FastAPI instance
# NiceGUI wraps, same object `app.on_shutdown` below hooks into.
app.include_router(api_routes.router)
```
->
```python
# Management HTTP API (non-UI): /api/v1 sub-app + the legacy /api/* aliases. `app` here is the FastAPI
# instance NiceGUI wraps, same object `app.on_shutdown` below hooks into.
register_management_api(app)
```

Check nothing else in `ui/main.py` uses the name `api_routes`: `Grep "api_routes" ba2_trade_platform/ui/main.py` must return nothing after the edit.

**Step 4:** Run `.venv\Scripts\python.exe -m pytest tests/test_api_v1_mount.py tests/test_api_v1_app.py -v`. Expected: all pass. Then `.venv\Scripts\python.exe -c "import ast,sys; ast.parse(open('ba2_trade_platform/ui/main.py',encoding='utf-8').read())"` (syntax only -- do NOT import or run `ui/main.py`).

**Step 5: Commit**

```bash
git add ba2_trade_platform/api/app.py ba2_trade_platform/ui/main.py tests/test_api_v1_mount.py
git commit -m "feat(api): mount /api/v1 and the legacy aliases from ui/main.py"
```

---

### Task 8: Pagination dependency + test client fixtures for phase 2

**Files:**
- Create: `ba2_trade_platform/api/common.py`
- Modify: `tests/api_helpers.py` (add `FakeAccount`, `v1_client`)
- Test: `tests/test_api_v1_common.py`

**Step 1: Write the failing test**

```python
"""Paging query parameters shared by every list endpoint."""
from fastapi import Depends, FastAPI

from ba2_trade_platform.api.common import Paging, paging
from ba2_trade_platform.api.errors import install_error_handlers
from tests.api_helpers import client_for


def _app():
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/l")
    def l(p: Paging = Depends(paging)):
        return {"limit": p.limit, "offset": p.offset}

    return app


def test_paging_defaults_and_bounds():
    c = client_for(_app())
    assert c.get("/l").json() == {"limit": 50, "offset": 0}
    assert c.get("/l?limit=500&offset=10").json() == {"limit": 500, "offset": 10}
    assert c.get("/l?limit=501").status_code == 422
    assert c.get("/l?limit=0").status_code == 422
    assert c.get("/l?offset=-1").status_code == 422
```

**Step 2:** Run `.venv\Scripts\python.exe -m pytest tests/test_api_v1_common.py -v`. Expected: ERROR (module missing).

**Step 3: Implement** `ba2_trade_platform/api/common.py`:

```python
"""Query-parameter dependencies shared by the routers."""
from dataclasses import dataclass

from fastapi import Query

MAX_LIMIT = 500
DEFAULT_LIMIT = 50


@dataclass
class Paging:
    limit: int
    offset: int


def paging(limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT), offset: int = Query(0, ge=0)) -> Paging:
    return Paging(limit=limit, offset=offset)
```

Append to `tests/api_helpers.py`:

```python
from ba2_trade_platform.core.account_types import AccountSnapshot


class FakeAccount:
    """Stands in for a broker account in service/route tests: canned snapshot, capital, positions, orders.
    Every figure is a plain attribute so a test can blank one (None) to exercise the Unmeasurable path."""

    def __init__(self, account_id: int = 1):
        self.id = account_id
        self.snapshot = AccountSnapshot(cash=2500.0, equity=10000.0, net_liquidation=10000.0,
                                        buying_power=20000.0, option_buying_power=5000.0,
                                        margin_multiplier=2.0, is_margin_account=True,
                                        long_market_value=7500.0, short_market_value=0.0)
        self.capital = {"account_id": account_id, "balance": 10000.0, "margin_enabled": True,
                        "margin_factor": 1.5, "broker_multiplier": 2.0, "effective_factor": 1.5,
                        "tradable_balance": 15000.0, "broker_buying_power": 20000.0,
                        "gross_exposure": 7500.0, "pending_entries": 500.0, "headroom": 7000.0}
        self.positions = [{"symbol": "AAPL", "qty": 10.0}]
        self.orders = [{"id": "b-1", "symbol": "AAPL", "status": "new"}]
        self.capital_error = None

    def get_account_snapshot(self):
        return self.snapshot

    def describe_capital(self):
        if self.capital_error is not None:
            raise ValueError(self.capital_error)
        return self.capital

    def get_positions(self):
        return self.positions

    def get_orders(self, status=None):
        return self.orders

    def _pending_stock_entry_notional(self, exclude_order_id=None):
        return 500.0


def v1_client(**kwargs) -> TestClient:
    from ba2_trade_platform.api.app import create_v1_app
    return client_for(create_v1_app(), **kwargs)
```

**Step 4:** Run `.venv\Scripts\python.exe -m pytest tests/test_api_v1_common.py -v`. Expected: 1 passed.

**Step 5: Commit**

```bash
git add ba2_trade_platform/api/common.py tests/api_helpers.py tests/test_api_v1_common.py
git commit -m "feat(api): paging dependency and phase-2 test fixtures"
```

---

### Task 9: Phase 1 regression sweep (no new code)

**Step 1:** Run, one pytest process at a time:

```
.venv\Scripts\python.exe -m pytest tests/test_api_v1_services_common.py tests/test_api_v1_auth.py tests/test_api_v1_errors.py tests/test_api_v1_audit.py tests/test_api_v1_app.py tests/test_api_v1_mount.py tests/test_api_v1_common.py tests/test_api_token_card.py tests/test_api_routes_reload.py tests/test_api_routes_run_schedule.py tests/test_api_routes_process_recommendations.py -v
```
Expected: all pass.

**Step 2:** `.venv\Scripts\python.exe tools/check_package_versions.py --include-worktree` -- expected: passes (no `packages/` change). If it errors for reasons unrelated to this work, report it.

No commit.

---

### Task 10: Phase 1 version bump

**Files:** Modify `ba2_trade_platform/version.py`.

**Step 1:** Read the current `APP_VERSION` (`2026.09.NNNNN` -- last seen `2026.09.1261`) and increment NNNNN by 1 (e.g. `"2026.09.1262"`). Keep the month part as is unless the repo's convention at that time has moved on (look at `git log -3 -- ba2_trade_platform/version.py` and follow it).

**Step 2:** Verify: `.venv\Scripts\python.exe -c "from ba2_trade_platform.version import APP_VERSION; print(APP_VERSION)"` prints the new value; `.venv\Scripts\python.exe -m pytest tests/test_api_v1_app.py -k ping -v` passes (it compares against `APP_VERSION`).

**Step 3: Commit**

```bash
git add ba2_trade_platform/version.py
git commit -m "chore(version): APP bump for management API phase 1"
```

(Phase 1 is one branch/merge in the design; merging and the 8080/8082 test rollout are the user's call and are not part of this plan.)

---

## PHASE 2 -- read endpoints from the DB and account methods

Conventions for every Phase 2 task: service function in `services/<domain>.py` (sync, plain dicts, typed errors); router in `api/<domain>.py` with plain `def` handlers (they block on DB/broker, so FastAPI runs them in its threadpool); the router module is added to `_ROUTER_MODULES` in `api/app.py`; tests in `tests/test_api_v1_<domain>.py` combine service-level and route-level checks. List endpoints return `{items, limit, offset, total}` ordered newest/highest id first. Query param `from` is a Python keyword: declare it as `from_: Optional[datetime] = Query(None, alias="from")` and `to` likewise.

### Task 11: Accounts service: definition, masked settings, live snapshot

**Files:**
- Create: `ba2_trade_platform/services/accounts.py`
- Test: `tests/test_api_v1_accounts_service.py`

**Real sources used (verified in code):** `AccountDefinition` / `AccountSetting` rows (`packages/common/ba2_common/core/models.py`); account class definitions via `get_account_class(provider).get_merged_settings_definitions()` (credential settings carry `"secret": True`, e.g. `AlpacaAccount.get_settings_definitions`); live figures via `get_account_instance_from_id(id)` (cached singleton, `ba2_trade_platform/core/utils.py`) then `get_account_snapshot()` (`AccountSnapshot`: cash, equity, net_liquidation, buying_power, option_buying_power, margin_multiplier, long/short_market_value) and `describe_capital()` (balance, margin_enabled, margin_factor, broker_multiplier, effective_factor, tradable_balance, broker_buying_power, gross_exposure, pending_entries, headroom -- `ReadOnlyAccountInterface.py:1112`). `pending_entries`/`headroom`/`gross_exposure`/`margin_factor`/`broker_multiplier` are `None` when margin is OFF by that method's own contract; the service reports them as `null` AND adds `margin_enabled` so a reader can tell "not applicable" from "unknown".

**Step 1: Write the failing test**

```python
"""Accounts service: masked credentials, live snapshot, Unmeasurable instead of zeros."""
import pytest

from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import AccountSetting
from ba2_trade_platform.services import accounts as svc
from ba2_trade_platform.services.errors import BrokerUnreachable, NotFound, Unmeasurable
from tests.api_helpers import FakeAccount
from tests.factories import create_account_definition


def _setting(account_id, key, value):
    add_instance(AccountSetting(account_id=account_id, key=key, value_str=value, value_json={},
                                value_float=None), expunge_after_flush=True)


@pytest.fixture
def fake(monkeypatch):
    acct = FakeAccount()
    monkeypatch.setattr(svc, "get_account_instance_from_id", lambda account_id: acct)
    return acct


def test_masked_settings_never_return_credentials():
    d = create_account_definition(name="Paper")
    _setting(d.id, "api_key", "AKSECRET123")
    _setting(d.id, "api_secret", "SHHH")
    _setting(d.id, "paper_account", "true")
    out = svc.account_definition(d.id)
    assert out["name"] == "Paper" and out["provider"] == "MockAccount"
    assert out["settings"]["paper_account"] == "true"
    assert out["settings"]["api_key"] == {"set": True}
    assert out["settings"]["api_secret"] == {"set": True}
    flat = str(out)
    assert "AKSECRET123" not in flat and "SHHH" not in flat


def test_unset_secret_reports_set_false():
    d = create_account_definition()
    _setting(d.id, "api_key", "")
    assert svc.account_definition(d.id)["settings"]["api_key"] == {"set": False}


def test_account_detail_has_live_snapshot_and_capital(fake):
    d = create_account_definition()
    out = svc.account_detail(d.id)
    snap = out["snapshot"]
    assert snap["equity"] == 10000.0 and snap["cash"] == 2500.0 and snap["buying_power"] == 20000.0
    assert snap["broker_multiplier"] == 2.0 and snap["margin_factor"] == 1.5
    assert snap["tradable_balance"] == 15000.0 and snap["gross_exposure"] == 7500.0
    assert snap["headroom"] == 7000.0 and snap["pending_entries"] == 500.0
    assert snap["margin_enabled"] is True and "as_of" in snap


def test_missing_broker_figure_is_unmeasurable_not_zero(fake):
    fake.snapshot.cash = None
    d = create_account_definition()
    with pytest.raises(Unmeasurable) as ei:
        svc.account_detail(d.id)
    assert "cash" in ei.value.message


def test_describe_capital_refusal_is_unmeasurable(fake):
    fake.capital_error = "broker published no multiplier"
    with pytest.raises(Unmeasurable):
        svc.account_detail(create_account_definition().id)


def test_unknown_account_is_not_found():
    with pytest.raises(NotFound):
        svc.account_definition(424242)


def test_list_isolates_a_failing_snapshot(monkeypatch):
    a = create_account_definition(name="ok")
    b = create_account_definition(name="bad")
    good = FakeAccount(a.id)

    def resolver(account_id):
        if account_id == b.id:
            raise ValueError("no credentials")
        return good
    monkeypatch.setattr(svc, "get_account_instance_from_id", resolver)
    out = svc.list_accounts(limit=10, offset=0)
    by_name = {i["name"]: i for i in out["items"]}
    assert by_name["ok"]["snapshot"]["equity"] == 10000.0
    assert by_name["bad"]["snapshot"] is None
    assert by_name["bad"]["snapshot_error"]["error"] == "unmeasurable"


def test_margin_off_reports_null_margin_terms_with_flag(fake):
    fake.capital.update(margin_enabled=False, margin_factor=None, broker_multiplier=None,
                        effective_factor=1.0, tradable_balance=10000.0, broker_buying_power=None,
                        gross_exposure=None, pending_entries=None, headroom=None)
    snap = svc.account_detail(create_account_definition().id)["snapshot"]
    assert snap["margin_enabled"] is False and snap["headroom"] is None
    assert snap["pending_entries"] == 500.0   # still measured from the order store (see service comment)
```

**Step 2:** Run `.venv\Scripts\python.exe -m pytest tests/test_api_v1_accounts_service.py -v`. Expected: ERROR (module missing).

**Step 3: Implement** `ba2_trade_platform/services/accounts.py`:

```python
"""Account reads for the management API: definition + masked settings, live snapshot, broker positions/orders.

Credentials are NEVER returned: a setting is masked when its definition says ``"secret": True`` OR its key
looks like a credential (defence in depth for providers that forgot the flag)."""
from typing import Any, Dict, Optional

from sqlmodel import select

from ..core.db import get_db
from ..core.models import AccountDefinition, AccountSetting
from ..core.utils import get_account_instance_from_id
from ..modules.accounts import get_account_class
from ._common import page, require_row, row_dict, utcnow_iso
from .errors import BrokerUnreachable, NotFound, ServiceError, Unmeasurable

_SECRET_KEY_PARTS = ("secret", "password", "token", "api_key", "apikey", "private_key")


def is_secret_setting(key: str, definition: Optional[Dict[str, Any]]) -> bool:
    if definition is not None and "secret" in definition and definition["secret"]:
        return True
    lowered = key.lower()
    return any(part in lowered for part in _SECRET_KEY_PARTS)


def _stored_value(row: AccountSetting) -> Any:
    if row.value_str is not None:
        return row.value_str
    if row.value_float is not None:
        return row.value_float
    return row.value_json if row.value_json else None


def masked_settings(provider: str, rows) -> Dict[str, Any]:
    cls = get_account_class(provider)
    definitions = cls.get_merged_settings_definitions() if cls is not None else {}
    out: Dict[str, Any] = {}
    for row in rows:
        value = _stored_value(row)
        definition = definitions[row.key] if row.key in definitions else None
        if is_secret_setting(row.key, definition):
            out[row.key] = {"set": value is not None and value != ""}
        else:
            out[row.key] = value
    return out


def _definition_dict(session, account: AccountDefinition) -> Dict[str, Any]:
    rows = session.exec(select(AccountSetting).where(AccountSetting.account_id == account.id)).all()
    out = row_dict(account)
    out["settings"] = masked_settings(account.provider, rows)
    return out


def account_definition(account_id: int) -> Dict[str, Any]:
    with get_db() as session:
        return _definition_dict(session, require_row(session, AccountDefinition, account_id, "account"))


def _live_account(account_id: int):
    account = get_account_instance_from_id(account_id)
    if account is None:
        raise NotFound(f"No account with id {account_id}", {"id": account_id})
    return account


def account_snapshot(account_id: int) -> Dict[str, Any]:
    """Live balance / equity / cash / buying power / margin / exposure of one account.

    Built from ONE ``get_account_snapshot()`` and ONE ``describe_capital()``. A figure the broker did not publish
    raises ``Unmeasurable`` naming it; it is never reported as 0 or null. The margin-only terms of
    ``describe_capital`` are legitimately ``None`` with margin OFF (that method's contract); ``margin_enabled``
    says so. Option buying power and the long/short market values may stay null when the broker publishes none."""
    account = _live_account(account_id)
    try:
        snap = account.get_account_snapshot()
        capital = account.describe_capital()
    except ValueError as e:
        raise Unmeasurable(f"Account {account_id}: {e}", {"account_id": account_id}) from e
    required = {"equity": snap.equity, "cash": snap.cash, "buying_power": snap.buying_power,
                "balance": capital["balance"], "tradable_balance": capital["tradable_balance"]}
    missing = sorted(name for name, value in required.items() if value is None)
    if missing:
        raise Unmeasurable(f"Account {account_id}: the broker published no value for {missing}",
                           {"account_id": account_id, "missing": missing})
    # pending_entries is also available with margin OFF straight from the order store; describe_capital
    # skips it there to keep backtests cheap. Private method on purpose -- no public equivalent exists.
    pending = capital["pending_entries"]
    if pending is None:
        pending = account._pending_stock_entry_notional()
    return {
        "as_of": utcnow_iso(),
        "balance": capital["balance"],
        "equity": snap.equity,
        "net_liquidation": snap.net_liquidation,
        "cash": snap.cash,
        "buying_power": snap.buying_power,
        "option_buying_power": snap.option_buying_power,
        "long_market_value": snap.long_market_value,
        "short_market_value": snap.short_market_value,
        "margin_enabled": capital["margin_enabled"],
        "margin_factor": capital["margin_factor"],
        "broker_multiplier": capital["broker_multiplier"],
        "effective_factor": capital["effective_factor"],
        "tradable_balance": capital["tradable_balance"],
        "gross_exposure": capital["gross_exposure"],
        "pending_entries": pending,
        "headroom": capital["headroom"],
    }


def account_detail(account_id: int) -> Dict[str, Any]:
    out = account_definition(account_id)
    out["snapshot"] = account_snapshot(account_id)
    return out


def list_accounts(*, limit: int, offset: int) -> Dict[str, Any]:
    """Definitions plus a snapshot SUMMARY each. One account's broker failure does not fail the list: that row
    carries ``snapshot: null`` and a ``snapshot_error`` envelope (explicit, never a silent zero)."""
    with get_db() as session:
        result = page(session, select(AccountDefinition).order_by(AccountDefinition.id),
                      limit=limit, offset=offset, convert=lambda a: _definition_dict(session, a))
    for item in result["items"]:
        try:
            snap = account_snapshot(item["id"])
            item["snapshot"] = {k: snap[k] for k in ("as_of", "balance", "equity", "cash",
                                                     "buying_power", "tradable_balance", "headroom")}
            item["snapshot_error"] = None
        except (ServiceError, ValueError) as e:
            item["snapshot"] = None
            code = e.code if isinstance(e, ServiceError) else "unmeasurable"
            item["snapshot_error"] = {"error": code, "message": str(e)}
    return result
```

Notes for the implementer: `get_account_class` / `get_account_instance_from_id` are imported at module level so tests can monkeypatch `svc.get_account_instance_from_id`. In the list test the resolver raising `ValueError` must land in `snapshot_error` as `unmeasurable` (covered by the `except (ServiceError, ValueError)`); `AccountInstanceCache.get_instance` can raise `ValueError` for an unknown provider -- let any OTHER exception type propagate (enforce mode: no broad swallow).

**Step 4:** Run `.venv\Scripts\python.exe -m pytest tests/test_api_v1_accounts_service.py -v`. Expected: 8 passed.

**Step 5: Commit**

```bash
git add ba2_trade_platform/services/accounts.py tests/test_api_v1_accounts_service.py
git commit -m "feat(api): accounts service with masked credentials and live snapshot"
```

---

### Task 12: Accounts router + broker positions / orders

**Files:**
- Modify: `ba2_trade_platform/services/accounts.py` (add `broker_positions`, `broker_orders`)
- Create: `ba2_trade_platform/api/accounts.py`
- Modify: `ba2_trade_platform/api/app.py` (`_ROUTER_MODULES`)
- Test: `tests/test_api_v1_accounts_routes.py`

**Step 1: Write the failing test**

```python
"""GET /accounts, /accounts/{id}, /accounts/{id}/positions, /accounts/{id}/orders."""
import pytest

from ba2_trade_platform.services import accounts as svc
from tests.api_helpers import FakeAccount, v1_client
from tests.factories import create_account_definition


@pytest.fixture
def fake(monkeypatch):
    acct = FakeAccount()
    monkeypatch.setattr(svc, "get_account_instance_from_id", lambda account_id: acct)
    return acct


def test_list_and_detail(fake):
    d = create_account_definition(name="Paper")
    c = v1_client()
    body = c.get("/accounts").json()
    assert body["total"] == 1 and body["items"][0]["snapshot"]["equity"] == 10000.0
    detail = c.get(f"/accounts/{d.id}").json()
    assert detail["snapshot"]["headroom"] == 7000.0
    assert c.get("/accounts/999").json()["error"] == "not_found"


def test_unmeasurable_detail_is_503_envelope(fake):
    fake.snapshot.equity = None
    d = create_account_definition()
    r = v1_client().get(f"/accounts/{d.id}")
    assert r.status_code == 503 and r.json()["error"] == "unmeasurable"


def test_positions_and_orders_carry_as_of(fake):
    d = create_account_definition()
    c = v1_client()
    pos = c.get(f"/accounts/{d.id}/positions").json()
    assert pos["items"] == [{"symbol": "AAPL", "qty": 10.0}] and "as_of" in pos
    ords = c.get(f"/accounts/{d.id}/orders?status=open").json()
    assert ords["items"][0]["id"] == "b-1" and "as_of" in ords


def test_positions_none_means_broker_unreachable_not_flat(fake):
    fake.positions = None          # the get_positions() tri-state: None = the FETCH failed, [] = flat
    d = create_account_definition()
    r = v1_client().get(f"/accounts/{d.id}/positions")
    assert r.status_code == 503 and r.json()["error"] == "broker_unreachable"
    fake.positions = []
    assert v1_client().get(f"/accounts/{d.id}/positions").json()["items"] == []
```

**Step 2:** Run `.venv\Scripts\python.exe -m pytest tests/test_api_v1_accounts_routes.py -v`. Expected: FAIL (404 on `/accounts`).

**Step 3: Implement.** Append to `services/accounts.py`:

```python
def broker_positions(account_id: int) -> Dict[str, Any]:
    """Broker-side positions. ``get_positions()`` is tri-state (``None`` = the fetch failed, ``[]`` = flat):
    a failed fetch is ``BrokerUnreachable``, never an empty list."""
    account = _live_account(account_id)
    positions = account.get_positions()
    if positions is None:
        raise BrokerUnreachable(f"Account {account_id}: the broker positions fetch failed",
                                {"account_id": account_id})
    return {"as_of": utcnow_iso(), "items": jsonable(list(positions))}


def broker_orders(account_id: int, status: Optional[str]) -> Dict[str, Any]:
    account = _live_account(account_id)
    orders = account.get_orders(status)
    if orders is None:
        raise BrokerUnreachable(f"Account {account_id}: the broker orders fetch failed",
                                {"account_id": account_id})
    return {"as_of": utcnow_iso(), "items": jsonable(list(orders))}
```

and change the import line to `from ._common import jsonable, page, require_row, row_dict, utcnow_iso`.

`ba2_trade_platform/api/accounts.py`:

```python
from typing import Optional

from fastapi import APIRouter, Depends

from ..services import accounts as svc
from .common import Paging, paging

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get("")
def list_accounts(p: Paging = Depends(paging)):
    return svc.list_accounts(limit=p.limit, offset=p.offset)


@router.get("/{account_id}")
def account_detail(account_id: int):
    return svc.account_detail(account_id)


@router.get("/{account_id}/positions")
def account_positions(account_id: int):
    return svc.broker_positions(account_id)


@router.get("/{account_id}/orders")
def account_orders(account_id: int, status: Optional[str] = None):
    return svc.broker_orders(account_id, status)
```

In `api/app.py` set `_ROUTER_MODULES = ["system", "maintenance", "accounts"]`.

**Step 4:** Run `.venv\Scripts\python.exe -m pytest tests/test_api_v1_accounts_routes.py tests/test_api_v1_accounts_service.py tests/test_api_v1_app.py -v`. Expected: all pass.

**Step 5: Commit**

```bash
git add ba2_trade_platform/services/accounts.py ba2_trade_platform/api/accounts.py ba2_trade_platform/api/app.py tests/test_api_v1_accounts_routes.py
git commit -m "feat(api): GET /accounts, /accounts/{id}, broker positions and orders"
```

---

### Task 13: Transactions: list and detail

**Files:**
- Create: `ba2_trade_platform/services/trades.py`
- Create: `ba2_trade_platform/api/transactions.py`
- Modify: `ba2_trade_platform/api/app.py`
- Test: `tests/test_api_v1_transactions.py`

**Real sources:** `Transaction` (symbol, side, quantity, open/close price and dates, `take_profit`, `stop_loss`, `tp_manual_override`/`sl_manual_override`, status `TransactionStatus`, `expert_id`, `close_reason`); `TradingOrder.transaction_id` / `account_id` (a transaction has no account column: filter through its orders); realised P&L via `calculate_transaction_pnl` (`ba2_common.core.utils`, handles side and the option multiplier, `None` when not closed); orders via `orders_where(transaction_id=...)` (`ba2_common.core.trade_store`); activity rows: `ActivityLog.data["transaction_id"]` is the key the TP/SL and lifecycle writers use (`TradeManager.py:2196`, `AlpacaAccount.py:4737`).

**Open P&L is NOT in this task** (needs a live price per row; the design's "open and closed P&L" arrives with the stats service in phase 4). `realized_pnl` is `null` for a transaction that has not closed; the list response documents it. See Open Questions.

**Step 1: Write the failing test**

```python
"""GET /transactions and /transactions/{id}."""
from datetime import datetime, timedelta, timezone

from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import ActivityLog
from ba2_trade_platform.core.types import (ActivityLogSeverity, ActivityLogType, OrderDirection,
                                           TransactionStatus)
from tests.api_helpers import v1_client
from tests.factories import (create_account_definition, create_expert_instance,
                             create_trading_order, create_transaction)


def _setup():
    acct = create_account_definition()
    expert = create_expert_instance(account_id=acct.id)
    open_tx = create_transaction(symbol="AAPL", status=TransactionStatus.OPENED, expert_id=expert.id)
    closed = create_transaction(symbol="MSFT", status=TransactionStatus.CLOSED, expert_id=expert.id,
                                open_price=100.0, close_price=110.0, quantity=5.0)
    short = create_transaction(symbol="TSLA", status=TransactionStatus.CLOSED, side=OrderDirection.SELL,
                               open_price=100.0, close_price=90.0, quantity=2.0)
    create_trading_order(acct.id, symbol="AAPL", transaction_id=open_tx.id)
    create_trading_order(acct.id, symbol="MSFT", transaction_id=closed.id)
    return acct, expert, open_tx, closed, short


def test_list_filters_and_realized_pnl():
    acct, expert, open_tx, closed, short = _setup()
    c = v1_client()
    body = c.get("/transactions").json()
    assert body["total"] == 3
    by_sym = {i["symbol"]: i for i in body["items"]}
    assert by_sym["MSFT"]["realized_pnl"] == 50.0 and by_sym["TSLA"]["realized_pnl"] == 20.0
    assert by_sym["AAPL"]["realized_pnl"] is None and by_sym["AAPL"]["status"] == "OPENED"
    assert {i["symbol"] for i in c.get(f"/transactions?account={acct.id}").json()["items"]} == {"AAPL", "MSFT"}
    assert [i["symbol"] for i in c.get("/transactions?status=OPENED").json()["items"]] == ["AAPL"]
    assert c.get(f"/transactions?expert={expert.id}&symbol=MSFT").json()["total"] == 1
    assert c.get("/transactions?status=bogus").status_code == 422


def test_list_date_filters_on_open_date():
    _setup()
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert v1_client().get("/transactions", params={"from": future}).json()["total"] == 0


def test_detail_has_orders_locks_and_activity():
    acct, expert, open_tx, *_ = _setup()
    add_instance(ActivityLog(severity=ActivityLogSeverity.INFO, type=ActivityLogType.TP_SL_ADJUSTED,
                             description="tp moved", data={"transaction_id": open_tx.id}),
                 expunge_after_flush=True)
    add_instance(ActivityLog(severity=ActivityLogSeverity.INFO, type=ActivityLogType.TP_SL_ADJUSTED,
                             description="other", data={"transaction_id": 999}),
                 expunge_after_flush=True)
    d = v1_client().get(f"/transactions/{open_tx.id}").json()
    assert d["symbol"] == "AAPL" and d["tp_manual_override"] is False and d["sl_manual_override"] is False
    assert [o["symbol"] for o in d["orders"]] == ["AAPL"]
    assert [a["description"] for a in d["activity"]] == ["tp moved"]
    assert v1_client().get("/transactions/99999").status_code == 404
```

**Step 2:** Run `.venv\Scripts\python.exe -m pytest tests/test_api_v1_transactions.py -v`. Expected: FAIL (404s on the routes).

**Step 3: Implement** `ba2_trade_platform/services/trades.py` (platform transactions + orders reads; phase 3 adds the write functions to this same module):

```python
"""Platform trades (Transaction / TradingOrder) for the management API. Reads only in phase 2; the TP/SL,
close and cancel write functions join this module in phase 3."""
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import func
from sqlmodel import select

from ba2_common.core.trade_store import orders_where
from ba2_common.core.utils import calculate_transaction_pnl

from ..core.db import get_db
from ..core.models import ActivityLog, TradingOrder, Transaction
from ..core.types import OrderStatus, TransactionStatus
from ._common import page, parse_enum, require_row, row_dict


def _transaction_dict(tx: Transaction) -> Dict[str, Any]:
    out = row_dict(tx)
    # None = the transaction has not closed (or lacks a price); open P&L is a phase-4 stats figure.
    out["realized_pnl"] = calculate_transaction_pnl(tx)
    return out


def list_transactions(*, account: Optional[int], expert: Optional[int], status: Optional[str],
                      symbol: Optional[str], from_: Optional[datetime], to: Optional[datetime],
                      limit: int, offset: int) -> Dict[str, Any]:
    stmt = select(Transaction)
    if account is not None:      # a transaction has no account column: it belongs to its orders' account
        stmt = stmt.where(Transaction.id.in_(
            select(TradingOrder.transaction_id).where(TradingOrder.account_id == account)))
    if expert is not None:
        stmt = stmt.where(Transaction.expert_id == expert)
    if status is not None:
        stmt = stmt.where(Transaction.status == parse_enum(TransactionStatus, status, "status"))
    if symbol is not None:
        stmt = stmt.where(Transaction.symbol == symbol.upper())
    if from_ is not None:
        stmt = stmt.where(Transaction.open_date >= from_)
    if to is not None:
        stmt = stmt.where(Transaction.open_date <= to)
    with get_db() as session:
        return page(session, stmt.order_by(Transaction.id.desc()), limit=limit, offset=offset,
                    convert=_transaction_dict)


def transaction_detail(transaction_id: int) -> Dict[str, Any]:
    with get_db() as session:
        tx = require_row(session, Transaction, transaction_id, "transaction")
        out = _transaction_dict(tx)
        activity = session.exec(
            select(ActivityLog)
            .where(func.json_extract(ActivityLog.data, "$.transaction_id") == transaction_id)
            .order_by(ActivityLog.id.desc()).limit(200)).all()
    out["orders"] = [row_dict(o) for o in sorted(orders_where(transaction_id=transaction_id),
                                                 key=lambda o: o.id)]
    out["activity"] = [row_dict(a) for a in activity]
    return out


def list_orders(*, account: Optional[int], status: Optional[str], transaction: Optional[int],
                limit: int, offset: int) -> Dict[str, Any]:
    """Platform orders (TradingOrder rows), including PENDING and WAITING_TRIGGER ones."""
    stmt = select(TradingOrder)
    if account is not None:
        stmt = stmt.where(TradingOrder.account_id == account)
    if status is not None:
        stmt = stmt.where(TradingOrder.status == parse_enum(OrderStatus, status, "status"))
    if transaction is not None:
        stmt = stmt.where(TradingOrder.transaction_id == transaction)
    with get_db() as session:
        return page(session, stmt.order_by(TradingOrder.id.desc()), limit=limit, offset=offset,
                    convert=row_dict)
```

`ba2_trade_platform/api/transactions.py`:

```python
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query

from ..services import trades as svc
from .common import Paging, paging

router = APIRouter(prefix="/transactions", tags=["transactions"])


@router.get("")
def list_transactions(account: Optional[int] = None, expert: Optional[int] = None,
                      status: Optional[str] = None, symbol: Optional[str] = None,
                      from_: Optional[datetime] = Query(None, alias="from"), to: Optional[datetime] = None,
                      p: Paging = Depends(paging)):
    return svc.list_transactions(account=account, expert=expert, status=status, symbol=symbol,
                                 from_=from_, to=to, limit=p.limit, offset=p.offset)


@router.get("/{transaction_id}")
def transaction_detail(transaction_id: int):
    return svc.transaction_detail(transaction_id)
```

Add `"transactions"` to `_ROUTER_MODULES`.

If the `calculate_transaction_pnl` import path `ba2_common.core.utils` fails in the venv, use `from ..core.utils import calculate_transaction_pnl` (the shim re-exports it). If `func.json_extract` raises on the test engine, the SQLite build lacks JSON1; fall back to filtering `ActivityLog` rows with `type in (TP_SL_ADJUSTED, TRANSACTION_*, ORDER_*)` in Python over the last 2000 rows and report it.

**Step 4:** Run `.venv\Scripts\python.exe -m pytest tests/test_api_v1_transactions.py -v`. Expected: 3 passed.

**Step 5: Commit**

```bash
git add ba2_trade_platform/services/trades.py ba2_trade_platform/api/transactions.py ba2_trade_platform/api/app.py tests/test_api_v1_transactions.py
git commit -m "feat(api): GET /transactions and /transactions/{id}"
```

---

### Task 14: Platform orders endpoint

**Files:**
- Create: `ba2_trade_platform/api/orders.py`
- Modify: `ba2_trade_platform/api/app.py`
- Test: `tests/test_api_v1_orders.py`

(`list_orders` already exists in `services/trades.py` from Task 13.)

**Step 1: Write the failing test**

```python
"""GET /orders (platform TradingOrder rows)."""
from ba2_trade_platform.core.types import OrderStatus
from tests.api_helpers import v1_client
from tests.factories import create_account_definition, create_trading_order, create_transaction


def test_orders_filters_and_includes_pending_and_waiting_trigger():
    a1, a2 = create_account_definition(), create_account_definition()
    tx = create_transaction()
    create_trading_order(a1.id, status=OrderStatus.PENDING, transaction_id=tx.id)
    create_trading_order(a1.id, status=OrderStatus.WAITING_TRIGGER, transaction_id=tx.id)
    create_trading_order(a2.id, status=OrderStatus.FILLED)
    c = v1_client()
    assert c.get("/orders").json()["total"] == 3
    assert c.get(f"/orders?account={a1.id}").json()["total"] == 2
    assert c.get(f"/orders?transaction={tx.id}&status=WAITING_TRIGGER").json()["total"] == 1
    assert c.get("/orders?status=nonsense").status_code == 422
    item = c.get("/orders?status=FILLED").json()["items"][0]
    assert item["status"] and item["account_id"] == a2.id and "limit_price" in item


def test_orders_paging():
    a = create_account_definition()
    for _ in range(3):
        create_trading_order(a.id)
    body = v1_client().get("/orders?limit=2&offset=2").json()
    assert body["total"] == 3 and len(body["items"]) == 1
```

(If `OrderStatus.WAITING_TRIGGER` is not a member in this repo, `Grep "WAITING_TRIGGER" packages/common/ba2_common/core/types.py` and use the real member name; the design and `TradingOrder.data` docs say it exists.)

**Step 2:** Run it; expected FAIL (404 on `/orders`).

**Step 3: Implement** `ba2_trade_platform/api/orders.py`:

```python
from typing import Optional

from fastapi import APIRouter, Depends

from ..services import trades as svc
from .common import Paging, paging

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("")
def list_orders(account: Optional[int] = None, status: Optional[str] = None,
                transaction: Optional[int] = None, p: Paging = Depends(paging)):
    return svc.list_orders(account=account, status=status, transaction=transaction,
                           limit=p.limit, offset=p.offset)
```

Add `"orders"` to `_ROUTER_MODULES`.

**Step 4:** Run `.venv\Scripts\python.exe -m pytest tests/test_api_v1_orders.py -v`. Expected: 2 passed.

**Step 5: Commit**

```bash
git add ba2_trade_platform/api/orders.py ba2_trade_platform/api/app.py tests/test_api_v1_orders.py
git commit -m "feat(api): GET /orders"
```

---

### Task 15: Experts: list and detail (settings, schedule, balance, reserve)

**Files:**
- Create: `ba2_trade_platform/services/experts.py`
- Create: `ba2_trade_platform/api/experts.py`
- Modify: `ba2_trade_platform/api/app.py`
- Test: `tests/test_api_v1_experts.py`

**Real sources:** `ExpertInstance` row (account_id, expert, enabled, priority, alias, virtual_equity_pct, ruleset ids); live instance via `get_expert_instance_from_id(id)` (`core/utils.py`, cached; `.settings` dict, `type(expert).get_merged_settings_definitions()`); the reserve setting `overalloc_min_res_pct` (`MarketExpertInterface.OVERALLOC_MIN_RES_PCT`, read as `expert.settings["overalloc_min_res_pct"]`, `None` = no reserve); balances via `expert.describe_capital_mapping()` (`MarketExpertInterface.py:2028`: `virtual_equity_pct`, `virtual_balance`, `used_balance`, `available_balance`, `equivalent_unlevered_balance`; NEVER raises, puts an `"error"` key on a refusal; returns None when nothing to describe); schedule = the settings whose key starts with `execution_schedule` (grep `execution_schedule_enter_market` in `MarketExpertInterface.py` to confirm the exact key prefix before coding); last run = newest `MarketAnalysis` for the expert.

**Step 1: Write the failing test**

```python
"""GET /experts and /experts/{id}."""
import pytest

from ba2_trade_platform.core.types import MarketAnalysisStatus
from ba2_trade_platform.services import experts as svc
from tests.api_helpers import v1_client
from tests.factories import create_account_definition, create_expert_instance, create_market_analysis


class FakeExpert:
    settings = {"overalloc_min_res_pct": 10.0, "execution_schedule_enter_market": "{\"days\": [\"monday\"]}",
                "openai_api_key": "sk-secret", "min_confidence": 70}

    @classmethod
    def get_merged_settings_definitions(cls):
        return {"openai_api_key": {"type": "str", "secret": True}, "min_confidence": {"type": "int"}}

    def describe_capital_mapping(self):
        return {"expert_id": 1, "virtual_equity_pct": 50.0, "virtual_balance": 5000.0,
                "used_balance": 1200.0, "available_balance": 3800.0,
                "equivalent_unlevered_balance": 15000.0}


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(svc, "get_expert_instance_from_id", lambda expert_id: FakeExpert())


def test_list_has_rows_and_last_analysis_without_touching_the_broker():
    acct = create_account_definition()
    e = create_expert_instance(account_id=acct.id, alias="Alpha")
    create_market_analysis(symbol="AAPL", expert_instance_id=e.id, status=MarketAnalysisStatus.COMPLETED)
    body = v1_client().get(f"/experts?account={acct.id}").json()
    item = body["items"][0]
    assert item["alias"] == "Alpha" and item["last_analysis"]["symbol"] == "AAPL"
    assert item["last_analysis"]["status"] == "completed"


def test_detail_masks_secrets_and_reports_balance_and_reserve(fake):
    acct = create_account_definition()
    e = create_expert_instance(account_id=acct.id, virtual_equity_pct=50.0)
    d = v1_client().get(f"/experts/{e.id}").json()
    assert d["settings"]["openai_api_key"] == {"set": True}
    assert "sk-secret" not in str(d)
    assert d["settings"]["min_confidence"] == 70
    assert d["overalloc_min_res_pct"] == 10.0
    assert d["capital"]["available_balance"] == 3800.0 and d["capital"]["virtual_balance"] == 5000.0
    assert d["schedule"] == {"execution_schedule_enter_market": "{\"days\": [\"monday\"]}"}
    assert v1_client().get("/experts/99999").status_code == 404


def test_capital_refusal_is_unmeasurable_not_zero(monkeypatch):
    class Refusing(FakeExpert):
        def describe_capital_mapping(self):
            return {"expert_id": 1, "error": "broker published no multiplier"}
    monkeypatch.setattr(svc, "get_expert_instance_from_id", lambda expert_id: Refusing())
    e = create_expert_instance(account_id=create_account_definition().id)
    r = v1_client().get(f"/experts/{e.id}")
    assert r.status_code == 503 and r.json()["error"] == "unmeasurable"
```

**Step 2:** Run `.venv\Scripts\python.exe -m pytest tests/test_api_v1_experts.py -v`. Expected: ERROR (module missing).

**Step 3: Implement** `ba2_trade_platform/services/experts.py`:

```python
"""Expert instance reads for the management API."""
from typing import Any, Dict, Optional

from sqlmodel import select

from ..core.db import get_db
from ..core.models import ExpertInstance, MarketAnalysis
from ..core.utils import get_expert_instance_from_id
from ._common import page, require_row, row_dict
from .accounts import is_secret_setting
from .errors import NotFound, Unmeasurable

_SCHEDULE_PREFIX = "execution_schedule"
_RESERVE_KEY = "overalloc_min_res_pct"


def _last_analysis(session, expert_id: int) -> Optional[Dict[str, Any]]:
    row = session.exec(select(MarketAnalysis).where(MarketAnalysis.expert_instance_id == expert_id)
                       .order_by(MarketAnalysis.id.desc()).limit(1)).first()
    if row is None:
        return None
    return {"id": row.id, "symbol": row.symbol, "status": row.status.value,
            "subtype": row.subtype.value, "created_at": row.created_at.isoformat()}


def list_experts(*, account: Optional[int], enabled: Optional[bool], limit: int, offset: int) -> Dict[str, Any]:
    """Instance rows + last analysis. No settings and no broker figures here (those need a live expert and a
    broker round trip per row): use the detail endpoint."""
    stmt = select(ExpertInstance)
    if account is not None:
        stmt = stmt.where(ExpertInstance.account_id == account)
    if enabled is not None:
        stmt = stmt.where(ExpertInstance.enabled == enabled)
    with get_db() as session:
        def convert(e: ExpertInstance) -> Dict[str, Any]:
            out = row_dict(e)
            out["last_analysis"] = _last_analysis(session, e.id)
            return out
        return page(session, stmt.order_by(ExpertInstance.id), limit=limit, offset=offset, convert=convert)


def expert_detail(expert_id: int) -> Dict[str, Any]:
    with get_db() as session:
        out = row_dict(require_row(session, ExpertInstance, expert_id, "expert"))
        out["last_analysis"] = _last_analysis(session, expert_id)
    expert = get_expert_instance_from_id(expert_id)
    if expert is None:
        raise NotFound(f"No expert instance with id {expert_id}", {"id": expert_id})
    definitions = type(expert).get_merged_settings_definitions()
    settings: Dict[str, Any] = {}
    for key, value in expert.settings.items():
        definition = definitions[key] if key in definitions else None
        settings[key] = {"set": value is not None and value != ""} if is_secret_setting(key, definition) else value
    out["settings"] = settings
    out["schedule"] = {k: v for k, v in settings.items() if k.startswith(_SCHEDULE_PREFIX)}
    out["overalloc_min_res_pct"] = expert.settings[_RESERVE_KEY]
    capital = expert.describe_capital_mapping()
    if capital is None or "error" in capital:
        reason = "nothing to describe" if capital is None else capital["error"]
        raise Unmeasurable(f"Expert {expert_id}: capital cannot be measured: {reason}", {"id": expert_id})
    out["capital"] = capital
    return out
```

(`expert.settings["overalloc_min_res_pct"]` is an explicit access: the key is declared in `MarketExpertInterface`'s built-in settings, so every expert has it. A `KeyError` there is a real bug -- let it surface as a 500, do not default it.)

`ba2_trade_platform/api/experts.py`:

```python
from typing import Optional

from fastapi import APIRouter, Depends

from ..services import experts as svc
from .common import Paging, paging

router = APIRouter(prefix="/experts", tags=["experts"])


@router.get("")
def list_experts(account: Optional[int] = None, enabled: Optional[bool] = None, p: Paging = Depends(paging)):
    return svc.list_experts(account=account, enabled=enabled, limit=p.limit, offset=p.offset)


@router.get("/{expert_id}")
def expert_detail(expert_id: int):
    return svc.expert_detail(expert_id)
```

Add `"experts"` to `_ROUTER_MODULES`. Phase 3 adds the PATCH/run routes to this same router and switches it to `route_class=AuditedRoute`.

**Step 4:** Run `.venv\Scripts\python.exe -m pytest tests/test_api_v1_experts.py -v`. Expected: 3 passed.

**Step 5: Commit**

```bash
git add ba2_trade_platform/services/experts.py ba2_trade_platform/api/experts.py ba2_trade_platform/api/app.py tests/test_api_v1_experts.py
git commit -m "feat(api): GET /experts and /experts/{id} with balance and reserve"
```

---

### Task 16: Market analyses + recommendations

**Files:**
- Create: `ba2_trade_platform/services/analysis.py`
- Create: `ba2_trade_platform/api/analysis.py`
- Modify: `ba2_trade_platform/api/app.py`
- Test: `tests/test_api_v1_analysis.py`

**Real sources:** `MarketAnalysis` (status `MarketAnalysisStatus`, subtype `AnalysisUseCase`, `state` JSON whose `skipped` / `skip_reason` keys are written by `WorkerQueue.py:791-792`), `AnalysisOutput` (text may be huge: list returns metadata + `text_length`, text only with `include_text=true`; `blob` is never returned), `ExpertRecommendation` (columns incl. `confidence` 1-100 scale).

**Step 1: Write the failing test**

```python
"""GET /analyses, /analyses/{id}, /recommendations."""
from datetime import datetime, timedelta, timezone

from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import AnalysisOutput
from ba2_trade_platform.core.types import AnalysisUseCase, MarketAnalysisStatus
from tests.api_helpers import v1_client
from tests.factories import (create_account_definition, create_expert_instance, create_market_analysis,
                             create_recommendation)


def _setup():
    acct = create_account_definition()
    e = create_expert_instance(account_id=acct.id)
    done = create_market_analysis(symbol="AAPL", expert_instance_id=e.id,
                                  status=MarketAnalysisStatus.COMPLETED, state={"k": "v"})
    skipped = create_market_analysis(symbol="MSFT", expert_instance_id=e.id,
                                     status=MarketAnalysisStatus.SKIPPED,
                                     subtype=AnalysisUseCase.OPEN_POSITIONS,
                                     state={"skipped": True, "skip_reason": "insufficient_balance"})
    return e, done, skipped


def test_analyses_list_filters_and_skip_reason():
    e, done, skipped = _setup()
    c = v1_client()
    body = c.get("/analyses").json()
    by_sym = {i["symbol"]: i for i in body["items"]}
    assert by_sym["MSFT"]["skip_reason"] == "insufficient_balance"
    assert by_sym["AAPL"]["skip_reason"] is None
    assert "state" not in by_sym["AAPL"]                      # list rows stay small
    assert c.get(f"/analyses?expert={e.id}&symbol=AAPL").json()["total"] == 1
    assert c.get("/analyses?status=skipped").json()["total"] == 1
    assert c.get("/analyses?subtype=open_positions").json()["total"] == 1
    assert c.get("/analyses?status=bogus").status_code == 422
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert c.get("/analyses", params={"from": future}).json()["total"] == 0


def test_analysis_detail_state_outputs_and_recommendations():
    e, done, _ = _setup()
    add_instance(AnalysisOutput(market_analysis_id=done.id, name="AAPL_news", type="news",
                                text="x" * 50, blob=b"\x00\x01", provider_metadata={}),
                 expunge_after_flush=True)
    create_recommendation(e.id, symbol="AAPL", market_analysis_id=done.id)
    c = v1_client()
    d = c.get(f"/analyses/{done.id}").json()
    assert d["state"] == {"k": "v"}
    out = d["outputs"][0]
    assert out["name"] == "AAPL_news" and out["text_length"] == 50
    assert "text" not in out and "blob" not in out
    assert d["recommendations"][0]["symbol"] == "AAPL"
    assert c.get(f"/analyses/{done.id}?include_text=true").json()["outputs"][0]["text"] == "x" * 50
    assert c.get("/analyses/99999").status_code == 404


def test_recommendations_list():
    e, done, _ = _setup()
    create_recommendation(e.id, symbol="AAPL", confidence=78.1)
    create_recommendation(e.id, symbol="NVDA")
    c = v1_client()
    assert c.get("/recommendations").json()["total"] == 2
    item = c.get(f"/recommendations?expert={e.id}&symbol=AAPL").json()["items"][0]
    assert item["confidence"] == 78.1 and item["recommended_action"]
    assert c.get("/recommendations?expert=999").json()["total"] == 0
```

**Step 2:** Run `.venv\Scripts\python.exe -m pytest tests/test_api_v1_analysis.py -v`. Expected: FAIL (404s).

**Step 3: Implement** `ba2_trade_platform/services/analysis.py`:

```python
"""Market analyses and expert recommendations for the management API (reads)."""
from datetime import datetime
from typing import Any, Dict, Optional

from sqlmodel import select

from ..core.db import get_db
from ..core.models import AnalysisOutput, ExpertRecommendation, MarketAnalysis
from ..core.types import AnalysisUseCase, MarketAnalysisStatus
from ._common import page, parse_enum, require_row, row_dict


def _skip_reason(state: Dict[str, Any]) -> Optional[str]:
    return state["skip_reason"] if "skip_reason" in state else None


def _analysis_summary(a: MarketAnalysis) -> Dict[str, Any]:
    out = row_dict(a)
    state = out.pop("state")                 # list rows stay small; the full state is on the detail
    out["skip_reason"] = _skip_reason(state)
    return out


def list_analyses(*, expert: Optional[int], symbol: Optional[str], status: Optional[str],
                  subtype: Optional[str], from_: Optional[datetime], limit: int, offset: int) -> Dict[str, Any]:
    stmt = select(MarketAnalysis)
    if expert is not None:
        stmt = stmt.where(MarketAnalysis.expert_instance_id == expert)
    if symbol is not None:
        stmt = stmt.where(MarketAnalysis.symbol == symbol.upper())
    if status is not None:
        stmt = stmt.where(MarketAnalysis.status == parse_enum(MarketAnalysisStatus, status, "status"))
    if subtype is not None:
        stmt = stmt.where(MarketAnalysis.subtype == parse_enum(AnalysisUseCase, subtype, "subtype"))
    if from_ is not None:
        stmt = stmt.where(MarketAnalysis.created_at >= from_)
    with get_db() as session:
        return page(session, stmt.order_by(MarketAnalysis.id.desc()), limit=limit, offset=offset,
                    convert=_analysis_summary)


def analysis_detail(analysis_id: int, include_text: bool) -> Dict[str, Any]:
    with get_db() as session:
        a = require_row(session, MarketAnalysis, analysis_id, "analysis")
        out = row_dict(a)
        out["skip_reason"] = _skip_reason(out["state"])
        outputs = session.exec(select(AnalysisOutput).where(AnalysisOutput.market_analysis_id == analysis_id)
                               .order_by(AnalysisOutput.id)).all()
        recs = session.exec(select(ExpertRecommendation)
                            .where(ExpertRecommendation.market_analysis_id == analysis_id)
                            .order_by(ExpertRecommendation.id)).all()
        out["outputs"] = [_output_dict(o, include_text) for o in outputs]
        out["recommendations"] = [row_dict(r) for r in recs]
    return out


def _output_dict(o: AnalysisOutput, include_text: bool) -> Dict[str, Any]:
    d = row_dict(o.model_copy(update={"blob": None, "text": None}))   # never ship the binary blob
    d.pop("blob")
    d.pop("text")
    d["text_length"] = 0 if o.text is None else len(o.text)
    if include_text:
        d["text"] = o.text
    return d


def list_recommendations(*, expert: Optional[int], symbol: Optional[str], from_: Optional[datetime],
                         limit: int, offset: int) -> Dict[str, Any]:
    stmt = select(ExpertRecommendation)
    if expert is not None:
        stmt = stmt.where(ExpertRecommendation.instance_id == expert)
    if symbol is not None:
        stmt = stmt.where(ExpertRecommendation.symbol == symbol.upper())
    if from_ is not None:
        stmt = stmt.where(ExpertRecommendation.created_at >= from_)
    with get_db() as session:
        return page(session, stmt.order_by(ExpertRecommendation.id.desc()), limit=limit, offset=offset,
                    convert=row_dict)
```

`ba2_trade_platform/api/analysis.py`:

```python
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query

from ..services import analysis as svc
from .common import Paging, paging

router = APIRouter(tags=["analysis"])


@router.get("/analyses")
def list_analyses(expert: Optional[int] = None, symbol: Optional[str] = None, status: Optional[str] = None,
                  subtype: Optional[str] = None, from_: Optional[datetime] = Query(None, alias="from"),
                  p: Paging = Depends(paging)):
    return svc.list_analyses(expert=expert, symbol=symbol, status=status, subtype=subtype, from_=from_,
                             limit=p.limit, offset=p.offset)


@router.get("/analyses/{analysis_id}")
def analysis_detail(analysis_id: int, include_text: bool = False):
    return svc.analysis_detail(analysis_id, include_text)


@router.get("/recommendations")
def list_recommendations(expert: Optional[int] = None, symbol: Optional[str] = None,
                         from_: Optional[datetime] = Query(None, alias="from"), p: Paging = Depends(paging)):
    return svc.list_recommendations(expert=expert, symbol=symbol, from_=from_, limit=p.limit, offset=p.offset)
```

Add `"analysis"` to `_ROUTER_MODULES`. If `model_copy(update=...)` on a table model misbehaves, build the output dict with `{k: v for k, v in row_dict(o).items() if k not in ("blob", "text")}` instead (note `row_dict` would have to tolerate `bytes`: make `jsonable` return `None`-safe by dropping the blob first via `o.model_dump(exclude={"blob", "text"})`; prefer that: `d = jsonable(o.model_dump(exclude={"blob", "text"}))`).

**Step 4:** Run `.venv\Scripts\python.exe -m pytest tests/test_api_v1_analysis.py -v`. Expected: 3 passed.

**Step 5: Commit**

```bash
git add ba2_trade_platform/services/analysis.py ba2_trade_platform/api/analysis.py ba2_trade_platform/api/app.py tests/test_api_v1_analysis.py
git commit -m "feat(api): GET /analyses, /analyses/{id} and /recommendations"
```

---

### Task 17: Risk-manager runs (classic + smart)

**Files:**
- Create: `ba2_trade_platform/services/risk_manager.py`
- Create: `ba2_trade_platform/api/risk_manager.py`
- Modify: `ba2_trade_platform/api/app.py`
- Test: `tests/test_api_v1_risk_manager.py`

**Real sources:** `RiskManagerRun` (mode `classic|options`, `decisions` JSON, counts, `context` JSON) and `SmartRiskManagerJob` (`graph_state` JSON can be large; `actions_summary`, `status`, balances). Ids of the two tables overlap, so the run id is namespaced: `kind` is `rm` or `smart`, and the detail route is `/risk-manager/runs/{kind}-{id}` (e.g. `rm-12`, `smart-3`); the list merges both tables ordered by `run_date`. (The design says `GET /risk-manager/runs/{id}` for "RiskManagerRun and SmartRiskManagerJob"; the namespacing is the minimal way to make `{id}` unambiguous -- see Open Questions.)

**Step 1: Write the failing test**

```python
"""GET /risk-manager/runs and /risk-manager/runs/{kind}-{id}."""
from datetime import datetime, timedelta, timezone

from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import RiskManagerRun, SmartRiskManagerJob
from tests.api_helpers import v1_client
from tests.factories import create_account_definition, create_expert_instance


def _setup():
    acct = create_account_definition()
    e = create_expert_instance(account_id=acct.id)
    now = datetime.now(timezone.utc)
    add_instance(RiskManagerRun(expert_instance_id=e.id, account_id=acct.id, mode="classic",
                                run_date=now - timedelta(hours=2), symbols_received=3, symbols_funded=1,
                                decisions=[{"symbol": "AAPL", "outcome": "funded"}],
                                context={"available_balance": 1000.0}), expunge_after_flush=True)
    add_instance(SmartRiskManagerJob(expert_instance_id=e.id, account_id=acct.id, run_date=now,
                                     model_used="m", user_instructions="u", status="COMPLETED",
                                     graph_state={"actions_log": [1]}, actions_summary="did things"),
                 expunge_after_flush=True)
    return e


def test_list_merges_both_kinds_newest_first_without_big_json():
    e = _setup()
    body = v1_client().get(f"/risk-manager/runs?expert={e.id}").json()
    assert [i["kind"] for i in body["items"]] == ["smart", "rm"]
    assert body["total"] == 2
    assert all("graph_state" not in i and "decisions" not in i for i in body["items"])
    assert body["items"][0]["ref"].startswith("smart-") and body["items"][1]["ref"].startswith("rm-")
    assert v1_client().get("/risk-manager/runs?kind=rm").json()["total"] == 1


def test_detail_by_ref():
    _setup()
    c = v1_client()
    ref_rm = next(i["ref"] for i in c.get("/risk-manager/runs").json()["items"] if i["kind"] == "rm")
    d = c.get(f"/risk-manager/runs/{ref_rm}").json()
    assert d["decisions"] == [{"symbol": "AAPL", "outcome": "funded"}] and d["context"]["available_balance"] == 1000.0
    ref_s = next(i["ref"] for i in c.get("/risk-manager/runs").json()["items"] if i["kind"] == "smart")
    assert c.get(f"/risk-manager/runs/{ref_s}").json()["graph_state"] == {"actions_log": [1]}
    assert c.get("/risk-manager/runs/rm-99999").status_code == 404
    assert c.get("/risk-manager/runs/garbage").status_code == 422
```

**Step 2:** Run it; expected FAIL (404).

**Step 3: Implement** `ba2_trade_platform/services/risk_manager.py`:

```python
"""Risk-manager run history for the management API: classic/options runs and smart-RM jobs in one feed."""
from typing import Any, Dict, Optional

from sqlmodel import select

from ..core.db import get_db
from ..core.models import RiskManagerRun, SmartRiskManagerJob
from ._common import require_row, row_dict
from .errors import Invalid

_KINDS = {"rm": RiskManagerRun, "smart": SmartRiskManagerJob}
_BIG = {"rm": ("decisions", "context"), "smart": ("graph_state",)}


def _summary(kind: str, row) -> Dict[str, Any]:
    d = row_dict(row)
    for key in _BIG[kind]:
        d.pop(key)
    d["kind"] = kind
    d["ref"] = f"{kind}-{row.id}"
    return d


def list_runs(*, expert: Optional[int], kind: Optional[str], limit: int, offset: int) -> Dict[str, Any]:
    if kind is not None and kind not in _KINDS:
        raise Invalid(f"kind={kind!r} is not one of {sorted(_KINDS)}", {"field": "kind"})
    items = []
    with get_db() as session:
        for k, model in _KINDS.items():
            if kind is not None and kind != k:
                continue
            stmt = select(model)
            if expert is not None:
                stmt = stmt.where(model.expert_instance_id == expert)
            items.extend((row.run_date, _summary(k, row)) for row in session.exec(stmt).all())
    items.sort(key=lambda pair: pair[0], reverse=True)
    window = [summary for _, summary in items[offset:offset + limit]]
    return {"items": window, "limit": limit, "offset": offset, "total": len(items)}


def run_detail(ref: str) -> Dict[str, Any]:
    kind, sep, raw_id = ref.partition("-")
    if not sep or kind not in _KINDS or not raw_id.isdigit():
        raise Invalid(f"run ref {ref!r} must look like 'rm-<id>' or 'smart-<id>'", {"field": "ref"})
    with get_db() as session:
        row = require_row(session, _KINDS[kind], int(raw_id), "risk-manager run")
        d = row_dict(row)
    d["kind"] = kind
    d["ref"] = ref
    return d
```

(Merging in Python is acceptable: both tables hold one row per expert pass -- hundreds, not millions. If the executor finds a table large in prod, switch to a SQL `UNION` in phase 4; do not pre-optimise.)

`ba2_trade_platform/api/risk_manager.py`:

```python
from typing import Optional

from fastapi import APIRouter, Depends

from ..services import risk_manager as svc
from .common import Paging, paging

router = APIRouter(prefix="/risk-manager", tags=["risk-manager"])


@router.get("/runs")
def list_runs(expert: Optional[int] = None, kind: Optional[str] = None, p: Paging = Depends(paging)):
    return svc.list_runs(expert=expert, kind=kind, limit=p.limit, offset=p.offset)


@router.get("/runs/{ref}")
def run_detail(ref: str):
    return svc.run_detail(ref)
```

Add `"risk_manager"` to `_ROUTER_MODULES`.

**Step 4:** Run `.venv\Scripts\python.exe -m pytest tests/test_api_v1_risk_manager.py -v`. Expected: 2 passed.

**Step 5: Commit**

```bash
git add ba2_trade_platform/services/risk_manager.py ba2_trade_platform/api/risk_manager.py ba2_trade_platform/api/app.py tests/test_api_v1_risk_manager.py
git commit -m "feat(api): GET /risk-manager/runs (classic and smart) and run detail"
```

---

### Task 18: Activity log

**Files:**
- Create: `ba2_trade_platform/services/activity.py`
- Create: `ba2_trade_platform/api/activity.py`
- Modify: `ba2_trade_platform/api/app.py`
- Test: `tests/test_api_v1_activity.py`

**Step 1: Write the failing test**

```python
"""GET /activity."""
from datetime import datetime, timedelta, timezone

from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import ActivityLog
from ba2_trade_platform.core.types import ActivityLogSeverity, ActivityLogType
from tests.api_helpers import v1_client
from tests.factories import create_account_definition, create_expert_instance


def _log(type_, severity=ActivityLogSeverity.INFO, **kw):
    add_instance(ActivityLog(severity=severity, type=type_, description=f"{type_.value}", data=kw.pop("data", {}),
                             **kw), expunge_after_flush=True)


def test_activity_filters():
    acct = create_account_definition()
    e = create_expert_instance(account_id=acct.id)
    _log(ActivityLogType.ORDER_FILLED, source_expert_id=e.id, source_account_id=acct.id)
    _log(ActivityLogType.TP_SL_ADJUSTED, severity=ActivityLogSeverity.WARNING, data={"source": "api"})
    c = v1_client()
    assert c.get("/activity").json()["total"] == 2
    assert c.get("/activity?type=order_filled").json()["total"] == 1
    assert c.get("/activity?type=ORDER_FILLED").json()["total"] == 1
    assert c.get("/activity?severity=warning").json()["total"] == 1
    assert c.get(f"/activity?expert={e.id}").json()["total"] == 1
    assert c.get(f"/activity?account={acct.id}").json()["total"] == 1
    assert c.get("/activity?type=bogus").status_code == 422
    item = c.get("/activity?type=tp_sl_adjusted").json()["items"][0]
    assert item["data"] == {"source": "api"} and item["severity"] == "warning"
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert c.get("/activity", params={"from": future}).json()["total"] == 0
```

**Step 2:** Run it; expected FAIL (404).

**Step 3: Implement** `ba2_trade_platform/services/activity.py`:

```python
"""Activity log reads for the management API."""
from datetime import datetime
from typing import Any, Dict, Optional

from sqlmodel import select

from ..core.db import get_db
from ..core.models import ActivityLog
from ..core.types import ActivityLogSeverity, ActivityLogType
from ._common import page, parse_enum, row_dict


def list_activity(*, type_: Optional[str], severity: Optional[str], expert: Optional[int],
                  account: Optional[int], from_: Optional[datetime], limit: int, offset: int) -> Dict[str, Any]:
    stmt = select(ActivityLog)
    if type_ is not None:
        stmt = stmt.where(ActivityLog.type == parse_enum(ActivityLogType, type_, "type"))
    if severity is not None:
        stmt = stmt.where(ActivityLog.severity == parse_enum(ActivityLogSeverity, severity, "severity"))
    if expert is not None:
        stmt = stmt.where(ActivityLog.source_expert_id == expert)
    if account is not None:
        stmt = stmt.where(ActivityLog.source_account_id == account)
    if from_ is not None:
        stmt = stmt.where(ActivityLog.created_at >= from_)
    with get_db() as session:
        return page(session, stmt.order_by(ActivityLog.id.desc()), limit=limit, offset=offset, convert=row_dict)
```

`ba2_trade_platform/api/activity.py`:

```python
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query

from ..services import activity as svc
from .common import Paging, paging

router = APIRouter(prefix="/activity", tags=["activity"])


@router.get("")
def list_activity(type: Optional[str] = None, severity: Optional[str] = None, expert: Optional[int] = None,
                  account: Optional[int] = None, from_: Optional[datetime] = Query(None, alias="from"),
                  p: Paging = Depends(paging)):
    return svc.list_activity(type_=type, severity=severity, expert=expert, account=account, from_=from_,
                             limit=p.limit, offset=p.offset)
```

Add `"activity"` to `_ROUTER_MODULES`.

**Step 4:** Run `.venv\Scripts\python.exe -m pytest tests/test_api_v1_activity.py -v`. Expected: 1 passed.

**Step 5: Commit**

```bash
git add ba2_trade_platform/services/activity.py ba2_trade_platform/api/activity.py ba2_trade_platform/api/app.py tests/test_api_v1_activity.py
git commit -m "feat(api): GET /activity"
```

---

### Task 19: Guard test: every read is token-protected for remote callers and no GET writes

**Files:**
- Test: `tests/test_api_v1_guard.py`

A structural test so a future router cannot be added unguarded or mutate on GET.

**Step 1: Write the test** (it should pass immediately; it exists to pin the invariant)

```python
"""Invariants over the whole /api/v1 route table."""
from ba2_trade_platform.api.app import create_v1_app
from tests.api_helpers import TOKEN, client_for, set_api_token


def _routes():
    app = create_v1_app()
    return [(m, r.path) for r in app.routes if hasattr(r, "methods") for m in r.methods
            if r.path not in ("/openapi.json", "/docs", "/docs/oauth2-redirect")]


def test_every_route_is_refused_to_a_remote_caller_without_token():
    set_api_token()
    c = client_for(create_v1_app(), host="10.9.9.9")
    for method, path in _routes():
        concrete = path.replace("{account_id}", "1").replace("{transaction_id}", "1") \
                       .replace("{expert_id}", "1").replace("{analysis_id}", "1").replace("{ref}", "rm-1")
        r = c.request(method, concrete)
        assert r.status_code == 401, (method, path, r.status_code)


def test_every_route_is_refused_when_no_token_is_set_and_caller_is_remote():
    c = client_for(create_v1_app(), host="10.9.9.9")
    for method, path in _routes():
        assert c.request(method, path.replace("{", "").replace("}", "")).status_code == 403


def test_read_routes_are_get_only_in_phase_2_except_maintenance_posts():
    posts = {path for method, path in _routes() if method == "POST"}
    assert posts == {"/reload", "/run-schedule", "/process-recommendations"}
    assert not any(m in ("PUT", "PATCH", "DELETE") for m, _ in _routes())


def test_token_opens_every_get_for_a_remote_caller_to_at_least_not_401(monkeypatch):
    set_api_token()
    c = client_for(create_v1_app(), host="10.9.9.9", token=TOKEN)
    assert c.get("/ping").status_code == 200
    assert c.get("/transactions").status_code == 200
    assert c.get("/activity").status_code == 200
```

**Step 2:** Run `.venv\Scripts\python.exe -m pytest tests/test_api_v1_guard.py -v`. Expected: 4 passed. (Phase 3 will intentionally relax the third test; it is the tripwire that forces that decision to be explicit.)

**Step 3: Commit**

```bash
git add tests/test_api_v1_guard.py
git commit -m "test(api): pin the auth guard over the whole /api/v1 route table"
```

---

### Task 20: Read-only smoke tool

**Files:**
- Create: `tools/api_smoke.py`

Not run by this plan (no live calls). It is for the user's manual runs against 8080/8082 after merge.

**Step 1: Write** `tools/api_smoke.py`:

```python
"""Read-only smoke test of the management API against a RUNNING instance.

    python tools/api_smoke.py --base http://127.0.0.1:8080 [--token TOKEN]

Does GETs only. From 127.0.0.1 no token is needed; from another host pass --token (or set BA2_API_TOKEN).
Exits non-zero if any endpoint does not answer 2xx. Never run against prod (8081) without the owner's say-so.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

GETS = ["/ping", "/accounts", "/experts", "/transactions?limit=5", "/orders?limit=5",
        "/analyses?limit=5", "/recommendations?limit=5", "/risk-manager/runs?limit=5", "/activity?limit=5"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--token", default=os.environ["BA2_API_TOKEN"] if "BA2_API_TOKEN" in os.environ else None)
    args = parser.parse_args()
    failures = 0
    for path in GETS:
        request = urllib.request.Request(args.base.rstrip("/") + "/api/v1" + path)
        if args.token:
            request.add_header("Authorization", f"Bearer {args.token}")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = json.load(response)
                total = body["total"] if isinstance(body, dict) and "total" in body else "-"
                print(f"OK   {response.status} {path} total={total}")
        except urllib.error.HTTPError as e:
            failures += 1
            print(f"FAIL {e.code} {path} {e.read().decode()[:200]}")
        except urllib.error.URLError as e:
            failures += 1
            print(f"FAIL --- {path} {e.reason}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
```

**Step 2:** Syntax check only: `.venv\Scripts\python.exe -c "import ast; ast.parse(open('tools/api_smoke.py', encoding='utf-8').read())"`. Expected: no output. (Do NOT run the tool.)

**Step 3: Commit**

```bash
git add tools/api_smoke.py
git commit -m "feat(tools): read-only API smoke script"
```

---

### Task 21: Phase 2 regression sweep + version bump

**Step 1:** Run, one process at a time:

```
.venv\Scripts\python.exe -m pytest tests/test_api_v1_services_common.py tests/test_api_v1_auth.py tests/test_api_v1_errors.py tests/test_api_v1_audit.py tests/test_api_v1_app.py tests/test_api_v1_mount.py tests/test_api_v1_common.py tests/test_api_token_card.py tests/test_api_v1_accounts_service.py tests/test_api_v1_accounts_routes.py tests/test_api_v1_transactions.py tests/test_api_v1_orders.py tests/test_api_v1_experts.py tests/test_api_v1_analysis.py tests/test_api_v1_risk_manager.py tests/test_api_v1_activity.py tests/test_api_v1_guard.py tests/test_api_routes_reload.py tests/test_api_routes_run_schedule.py tests/test_api_routes_process_recommendations.py -v
```
Expected: all pass.

**Step 2:** Bump `APP_VERSION` in `ba2_trade_platform/version.py` by 1 NNNNN (phase 2 build; follows the Task 10 bump). Verify: `.venv\Scripts\python.exe -c "from ba2_trade_platform.version import APP_VERSION; print(APP_VERSION)"`.

**Step 3:** `.venv\Scripts\python.exe tools/check_package_versions.py --include-worktree` -- expected pass (no `packages/` change).

**Step 4: Commit**

```bash
git add ba2_trade_platform/version.py
git commit -m "chore(version): APP bump for management API phase 2"
```

Do NOT push. Rollout (test on 8080, then 8082; prod 8081 on its next restart, after generating its token on the Settings page) is the user's call. Reminder for that rollout: the three legacy `/api/*` endpoints now answer 403 on an instance with no `api_token`, so scripts that call `/api/run-schedule` or `/api/reload` need the token header after upgrade.

---

## Open questions and design gaps found while planning

1. **No `ActivityLogType` for API writes, and no `source` column on `ActivityLog`.** The design says "ActivityLog entry with source `api`". The table has `severity`, `type`, `source_expert_id`, `source_account_id`, `data`. This plan reuses `ActivityLogType.APPLICATION_STATUS_CHANGE` and puts `"source": "api"` in `data`. A dedicated `API_WRITE` member needs a change in `packages/common/ba2_common/core/types.py` (PACKAGE_VERSION bump, GA-neutral glob review). Decide before phase 3, since `adjust_tp_sl(source="api")` entries may want their own type.
2. **"Pending close" in the account snapshot has no account-level source.** Only `has_pending_closing_order(transaction_id)` exists (per transaction). Task 11 returns `pending_entries` (stock entry notional) and omits pending close; adding it means looping open transactions or a new account method.
3. **`pending_entries` with margin OFF.** `describe_capital()` returns `None` for it when margin is off; the service falls back to the PRIVATE `account._pending_stock_entry_notional()`. A public accessor on `ReadOnlyAccountInterface` (in `packages/common`, version bump) would be cleaner.
4. **Open P&L on `GET /transactions`.** The design says "open and closed P&L"; open P&L needs a live price per row (broker cache traffic on a list). Phase 2 returns `realized_pnl` (null until closed). Open P&L belongs with phase 4's stats service unless you want a `?with_open_pnl=true` flag.
5. **Risk-manager run ids.** `RiskManagerRun` and `SmartRiskManagerJob` ids overlap, so the plan uses refs `rm-<id>` / `smart-<id>` for the detail route (design wrote `{id}`).
6. **`Unmeasurable` HTTP status.** The design lists no status for it; the plan uses 503 (like `broker_unreachable`). Change in `services/errors.py` if you prefer 502/500.
7. **Auth status codes.** Design says 403 for "no token set". The plan uses 403 for that and 401 for a missing/wrong token when one is set (`unauthorized`).
8. **IPv6 loopback.** The design says 127.0.0.1; the plan also treats `::1` as local. Drop it from `LOCAL_HOSTS` if undesired. `X-Forwarded-For` is ignored (fail closed behind any proxy).
9. **`/docs` from a remote browser.** A remote browser cannot attach a Bearer header, so Swagger UI is only usable locally (remote tools fetch `/openapi.json` with the token). Swagger UI also loads its JS from a CDN.
10. **Settings page file is dirty.** Task 6 must commit only its own hunk of `settings.py` (scripted `git add -p`). If the owner of the uncommitted `replay_capture` edits commits first, the hunk isolation step simplifies. `app_settings.py` is deliberately untouched: `api_token` lives outside `APP_SETTINGS_DEFINITIONS` and outside "Save All Settings".
11. **Phase 3 reminders baked into the skeleton.** `AuditedRoute` logs request bodies truncated to 500 chars: PATCH-settings bodies must be redacted for secret keys before phase 3 ships. Writes should take the `TradeManager` processing lock (not exercised in phases 1-2).
12. **Broker order/position payload shape.** `get_positions()` / `get_orders()` return provider-specific objects (Alpaca pydantic models, IBKR/TastyTrade dicts); `jsonable()` converts them generically, so the field names differ per broker. A normalised schema is not in the design; flagged for the MCP typing work.
13. **Account caches / `as_of`.** The design says broker reads "use the account caches and return `as_of`". `get_positions`/`get_orders` do not expose their cache timestamps, so `as_of` is the time the service read them (honest only if the call is not served from a stale cache). Exposing true cache ages needs an account-interface change.

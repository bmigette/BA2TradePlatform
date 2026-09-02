"""An `async def` route that never awaits does blocking work ON the event loop: one slow DB call
in it stalls every other request (observed 2026-09-02: /api/dashboard/stats froze /docs for two
minutes). FastAPI runs plain `def` routes in a thread pool, so the rule is: a route is `async def`
only if it actually awaits.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from dataclasses import dataclass
from typing import Iterable

import pytest

# Polled by the frontend every 2-5 s; the routes that took the backend down.
HOT_PATHS = {
    ("GET", "/api/dashboard/stats"),
    ("GET", "/api/tasks"),
    ("GET", "/api/backtests"),
}


@dataclass(frozen=True)
class _ResolvedRoute:
    """An `APIRoute` with its `include_router(prefix=...)` chain already merged into `.path`."""
    path: str
    methods: frozenset
    endpoint: object


def _flatten_routes(routes, prefix: str = "") -> Iterable[_ResolvedRoute]:
    """Yield every real endpoint under `routes`, resolving lazy/nested includes to full paths.

    This FastAPI (0.137.0 here — requirements.txt only pins `fastapi>=0.115.0`) made
    `app.include_router(...)` lazy: `app.routes` holds opaque `fastapi.routing._IncludedRouter`
    wrapper objects, not flattened `APIRoute`s with the prefix already merged into `.path`.
    Checked for a public replacement first — `dir(fastapi.routing)` and `dir(APIRouter)` expose
    nothing that iterates included routers or resolves an effective path — so this recurses
    through the wrapper's `original_router.routes`, accumulating each `include_router
    (prefix=...)` via its `include_context.prefix`. All attribute access is `getattr`-tolerant so
    an older FastAPI, where `app.routes` already holds flattened `APIRoute`s, still works: those
    routes satisfy `isinstance(r, APIRoute)` on the first check and are yielded as-is (prefix=""
    at the top level leaves their already-merged `.path` untouched).
    """
    from fastapi.routing import APIRoute

    for r in routes:
        if isinstance(r, APIRoute):
            yield _ResolvedRoute(path=prefix + r.path, methods=frozenset(r.methods), endpoint=r.endpoint)
            continue
        sub_router = getattr(r, "original_router", None)
        if sub_router is None:
            continue  # not an APIRoute and not an included router (e.g. a Mount) - skip it
        include_context = getattr(r, "include_context", None)
        sub_prefix = getattr(include_context, "prefix", "") or ""
        yield from _flatten_routes(getattr(sub_router, "routes", []), prefix + sub_prefix)


def _api_routes():
    from app.main import app
    return list(_flatten_routes(app.routes))


_NESTED_SCOPE = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _own_body_nodes(nodes) -> Iterable[ast.AST]:
    """Yield `nodes` and everything under them, WITHOUT descending into nested scopes.

    Like `ast.walk`, but it prunes nested `def`/`async def`/`lambda`. An `await` inside a nested
    coroutine that the route never awaits is not work the route does on the event loop, so
    counting it would let a genuinely blocking route pass as "awaits something".
    """
    for node in nodes:
        if isinstance(node, _NESTED_SCOPE):
            continue
        yield node
        yield from _own_body_nodes(ast.iter_child_nodes(node))


def _awaits_something(fn) -> bool:
    src = textwrap.dedent(inspect.getsource(inspect.unwrap(fn)))
    func = ast.parse(src).body[0]
    assert isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)), f"{fn!r} is not a function"
    # `func.body` only: not its decorators, not its argument defaults, not nested definitions.
    return any(
        isinstance(n, (ast.Await, ast.AsyncWith, ast.AsyncFor))
        for n in _own_body_nodes(func.body)
    )


def _pure_blocking_async_routes():
    bad = []
    for r in _api_routes():
        if inspect.iscoroutinefunction(r.endpoint) and not _awaits_something(r.endpoint):
            for m in sorted(r.methods):
                bad.append(f"{m} {r.path} -> {r.endpoint.__module__}.{r.endpoint.__name__}")
    return bad


async def _awaits_only_inside_a_nested_def():
    """An `async def` whose sole `await` sits in a nested coroutine it never awaits: every
    statement it actually runs is blocking, so the oracle must NOT call this one awaiting."""
    async def _inner():
        await asyncio.sleep(0)

    return _inner


async def _awaits_for_real():
    await asyncio.sleep(0)
    return None


def test_awaits_something_ignores_nested_scopes():
    """The oracle judges the route body alone — an `await` parked in a nested `def`/`lambda`
    never runs on the loop unless the route awaits it, and the route here does not."""
    assert _awaits_something(_awaits_for_real) is True
    assert _awaits_something(_awaits_only_inside_a_nested_def) is False


def test_route_discovery_sees_the_included_routers():
    """Guards `_flatten_routes` against silently degrading back to a 2-route view (just `/` and
    `/health`, as it did before this fix): every other test here trusts `_api_routes()` to see
    every endpoint reachable through `app.include_router(...)`, so pin it against the independent
    ground truth FastAPI itself builds — `app.openapi()["paths"]`."""
    from app.main import app

    discovered = {(m, r.path) for r in _api_routes() for m in r.methods}
    spec = app.openapi()
    documented = {
        (method.upper(), path)
        for path, methods in spec.get("paths", {}).items()
        for method in methods
    }
    assert discovered == documented
    assert len(discovered) > 100
    assert HOT_PATHS <= discovered


def test_hot_path_routes_are_plain_def():
    by_key = {(m, r.path): r for r in _api_routes() for m in r.methods}
    for key in HOT_PATHS:
        assert key in by_key, f"route {key} not found — did its path change?"
        assert not inspect.iscoroutinefunction(by_key[key].endpoint), \
            f"{key} is async def but does only blocking work; make it `def`"


# Task 6 of docs/plans/2026-09-02-testplatform-backend-event-loop-stall.md converts the rest of
# the routes and REMOVES this xfail marker. strict=True so the marker cannot outlive the sweep.
@pytest.mark.xfail(strict=True, reason="remaining pure-blocking async routes are converted in Task 6")
def test_no_async_route_does_only_blocking_work():
    """Every remaining `async def` route must genuinely await. Fix = delete `async` on the
    listed endpoints."""
    assert _pure_blocking_async_routes() == []

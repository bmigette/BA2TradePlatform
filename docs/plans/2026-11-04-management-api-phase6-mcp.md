# Management API phase 6: MCP server Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A standalone MCP server (`mcp_server/`, package `ba2_mcp`, script `ba2-mcp`) that exposes every `/api/v1` endpoint as one explicitly named tool, with a dry-run-first model for every write.

**Architecture:** `ba2_mcp` is a thin synchronous HTTP client (httpx) of `/api/v1`; it imports nothing from `ba2_trade_platform` or any `ba2_*` package. `client.py` holds config, request, and error mapping. `tools.py` declares each tool through one decorator, `@api_tool(method, path_template, write=...)`, which registers the tool with FastMCP and records `(method, path)` in a registry. `server.py` builds the FastMCP app (stdio by default). A parity test compares the registry with the OpenAPI schema of `create_v1_app()` (test-only import).

**Tech Stack:** `mcp` (FastMCP), `httpx`, pytest with the repo's `api_db` fixtures; tool tests run against the in-process FastAPI app (starlette `TestClient` is an `httpx.Client`, so the client accepts it as its transport).

**Decisions**
- Config by env only: `BA2_API_URL` (required, e.g. `http://localhost:8081/api/v1`), `BA2_API_TOKEN` (optional: local reads need none; writes and remote access do), `BA2_API_TIMEOUT` (default 60 s). The token is never logged or echoed (error text, repr, tool results).
- Every write tool has `confirm: bool = False`. False: the endpoint is called with `?dry_run=true` and the tool returns `{"dry_run": true, "preview": ..., "note": "Nothing was sent. Call again with confirm=true to execute."}`. True: execute with a per-call `Idempotency-Key` (uuid4). Writes are never retried automatically; a timeout is reported with "check before retrying".
- Endpoints WITHOUT server-side dry run (`/reload`, `/process-recommendations`): FastAPI ignores `?dry_run`, so sending it would EXECUTE. Those tools, when `confirm` is false, send nothing and return a local description (flagged `endpoint_has_dry_run=false`). A test pins which write tools are local-preview.
- `POST /run-schedule` is a legacy alias of `POST /experts/{id}/run`; it is listed in `INTENTIONALLY_UNEXPOSED` with that reason (the parity test accepts only listed exceptions).
- Errors: API envelope `{"error","message","detail"}` becomes a `ToolError` with the code; 401 mentions `BA2_API_TOKEN`, 403 `remote_disabled` mentions `api_allow_remote`, other 403 mentions the missing token, connection refused mentions `BA2_API_URL`.

### Task 1: client + error mapping
Files: create `mcp_server/pyproject.toml`, `mcp_server/ba2_mcp/{__init__,version,client}.py`; test `tests/test_mcp_server_client.py`.
Steps: write tests (query building drops None and lowercases bools; envelope mapping; 401/403/connect error texts; token absent from every error text; write timeout text), see them fail, implement, pass, commit.

### Task 2: tools + server
Files: create `mcp_server/ba2_mcp/{tools,server}.py`; test `tests/test_mcp_server_tools.py`.
Steps: tests per group (reads return the API body; write dry-run sends `dry_run=true` and no `Idempotency-Key`; confirm sends a key and executes against a recording fake broker; same key replays; no-dry-run endpoints send nothing when unconfirmed; secrets in settings never echoed), implement, pass, commit.

### Task 3: parity test
File: `tests/test_mcp_server_parity.py`. Every OpenAPI `(method, path)` has exactly one tool or is in `INTENTIONALLY_UNEXPOSED`; every tool maps to an existing `(method, path)`; path placeholders equal function parameters; every write tool has `confirm`.

### Task 4: README + commit
`mcp_server/README.md`: install, env vars, Claude Code snippet, token setup, confirm/dry-run model. Run each test file once (one pytest at a time). Commit; do not push.

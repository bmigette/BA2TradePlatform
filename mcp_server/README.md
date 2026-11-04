# ba2-mcp: MCP server for the BA2 management API

A thin MCP (stdio) server that exposes the platform's `/api/v1` HTTP API as tools: reads (accounts, trades,
orders, experts, analyses, risk-manager runs, activity, overview stats, growth, P&L, dividends, portfolio) and
writes (TP/SL, close, cancel, expert enable/disable/sizing/settings, run now, cache reload). It speaks HTTP
only and imports no `ba2_*` package, so it can run on any machine that can reach the platform.

## Install

```bash
uv venv mcp_server/.venv
uv pip install -e mcp_server --python mcp_server/.venv        # Windows: mcp_server\.venv\Scripts\python.exe
```

Dependencies: `mcp` (FastMCP) and `httpx`.

## Configuration (environment variables only)

| Variable | Meaning |
|---|---|
| `BA2_API_URL` | API root of ONE instance, e.g. `http://localhost:8081/api/v1` (8080 dev, 8081 prod, 8082 options). Required. |
| `BA2_API_TOKEN` | Bearer token. Needed for every write and for reads from another machine. |
| `BA2_API_TIMEOUT` | Request timeout in seconds (default 60). |

The token is never logged or returned in a tool result or error.

## Token setup

1. In the platform UI open Settings -> Management API and press Generate. Copy the token (it is the only time
   it is shown in full); set it as `BA2_API_TOKEN`.
2. From the same machine as the platform no more is needed (reads even work without a token).
3. From another machine switch on "Allow API access from other machines" (`api_allow_remote`) on the same card;
   otherwise every call is refused with 403 `remote_disabled`.

## Claude Code

```bash
claude mcp add ba2 \
  -e BA2_API_URL=http://localhost:8082/api/v1 \
  -e BA2_API_TOKEN=<token> \
  -- mcp_server/.venv/Scripts/ba2-mcp
```

Add one server per instance (`ba2-dev`, `ba2-prod`) to work with several.

## Safety model for write tools

Every write tool has `confirm: bool = False`.

- `confirm=false` (default): the tool calls the endpoint with `?dry_run=true` and returns
  `{"dry_run": true, "preview": ..., "note": "Nothing was sent. Call again with confirm=true to execute."}`.
  `reload_platform_caches` and `process_recommendations` have no server-side dry run (the server would ignore
  the flag and execute), so unconfirmed they send nothing at all and only describe the call.
- `confirm=true`: executes, with a fresh `Idempotency-Key` (uuid4) per call.
- Writes are never retried automatically. On a timeout the request MAY have been executed: check the trade
  (`get_transaction`, `list_orders`) before trying again.
- Errors carry the API's error code; 401/403 explain the token and `api_allow_remote`; a refused connection
  names `BA2_API_URL`.

## Tests

`tests/test_mcp_server_*.py` (run from the repo root with the repo venv): the tools against the in-process
FastAPI app, plus a parity test that fails when an `/api/v1` route has no tool.
Adding an API route requires adding a tool in `ba2_mcp/tools.py` (or listing a reason in
`INTENTIONALLY_UNEXPOSED`).

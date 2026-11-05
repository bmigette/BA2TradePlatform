"""Shared setup of the MCP-server tests: ``mcp_server`` on sys.path and a Ba2Client wired to the in-process app."""
import sys
from pathlib import Path

_MCP_DIR = str(Path(__file__).resolve().parent.parent / "mcp_server")
if _MCP_DIR not in sys.path:
    sys.path.insert(0, _MCP_DIR)

from tests.api_helpers import TOKEN, set_api_token, v1_client      # noqa: E402


def wire_client(token=TOKEN):
    """Point the MCP tools at the in-process /api/v1 app (a starlette TestClient is an httpx.Client).
    Returns ``(Ba2Client, TestClient)``."""
    from ba2_mcp import tools
    from ba2_mcp.client import Ba2Client
    set_api_token()                       # the instance HAS a token; ``token=None`` is a caller that does not send it
    http = v1_client(token=token)
    client = Ba2Client("http://localhost", token, 60.0, http=http)
    tools.set_client(client)
    return client, http

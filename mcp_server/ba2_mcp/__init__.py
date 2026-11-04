"""BA2 MCP server: one tool per /api/v1 endpoint. Speaks HTTP only; imports no ba2_* package."""
from .version import PACKAGE_VERSION as __version__  # noqa: F401

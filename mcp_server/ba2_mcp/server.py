"""Entry point: ``ba2-mcp`` serves the tools over stdio (the default transport)."""
import sys

from . import tools
from .client import ApiCallError
from .tools import mcp


def main() -> None:
    try:
        tools.get_client()          # fail at startup, not at the first call, when BA2_API_URL is missing
    except (ApiCallError, ValueError) as e:
        print(f"ba2-mcp: {e}", file=sys.stderr)
        raise SystemExit(2)
    mcp.run()          # stdio


if __name__ == "__main__":
    main()

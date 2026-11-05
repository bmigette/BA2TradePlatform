"""Read-only smoke test of the management API against a RUNNING instance.

    python tools/api_smoke.py --base http://127.0.0.1:8080 [--token TOKEN]

Does GETs only. From 127.0.0.1 no token is needed (use the host name 127.0.0.1 or localhost in --base: the API
refuses a foreign Host header without the token); from another host pass --token (or set BA2_API_TOKEN), and
the instance must have "Allow API access from other machines" switched on.
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

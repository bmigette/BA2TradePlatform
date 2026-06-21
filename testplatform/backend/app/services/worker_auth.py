"""Shared bearer-token auth for the worker/cache endpoints.

Accepts EITHER ``BA2_WORKER_TOKEN`` (the worker-fleet token) OR ``BA2_ADMIN_TOKEN`` (the admin
token already used by ``/api/admin/*``), so a single-token deployment works out of the box and a
two-token deployment can scope a least-privilege worker token. Constant-time comparison.
"""

import hmac
import os
from typing import List, Optional

from fastapi import HTTPException


def _expected_tokens() -> List[str]:
    return [v for v in (os.environ.get("BA2_WORKER_TOKEN"), os.environ.get("BA2_ADMIN_TOKEN")) if v]


def verify_worker_token(authorization: Optional[str]) -> None:
    """Validate ``Authorization: Bearer <token>`` against the worker/admin tokens. Raises on failure."""
    tokens = _expected_tokens()
    if not tokens:
        raise HTTPException(
            status_code=503,
            detail="Worker auth is not configured (set BA2_WORKER_TOKEN or BA2_ADMIN_TOKEN).",
        )
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header.")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="Invalid Authorization header format. Expected 'Bearer <token>'.",
        )
    token = parts[1]
    if not any(hmac.compare_digest(token, t) for t in tokens):
        raise HTTPException(status_code=403, detail="Invalid worker token.")

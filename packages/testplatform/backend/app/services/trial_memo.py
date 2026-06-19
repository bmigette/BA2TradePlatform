"""Content-addressed memo for backtest trials.

Key = sha256 of canonical JSON of an identity dict that FULLY determines the
result (model_id, pred/exec dataset ids, date range, decoded_params, seed, ...).
Per the determinism rule (same cache + same decoded params => identical result),
an elitism-reselected identical individual is a free memo hit AND a self-check
that the run is deterministic. Canonical JSON (sort_keys) makes the key
order-independent so dict insertion order never changes the hash.
"""
import hashlib
import json
from typing import Any, Dict, Optional


def trial_key(identity: Dict[str, Any]) -> str:
    """Return a stable, order-independent sha256 hex digest of the trial identity.

    identity must fully determine the result: model/datasets/date-range/params/seed.
    Non-JSON-native values are coerced via default=str so e.g. dates/Decimals hash
    deterministically.
    """
    blob = json.dumps(identity, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class TrialMemo:
    """In-memory fitness memo keyed by content hash, with hit/miss accounting."""

    def __init__(self):
        self._store: Dict[str, float] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[float]:
        if key in self._store:
            self.hits += 1
            return self._store[key]
        self.misses += 1
        return None

    def put(self, key: str, fitness: float) -> None:
        self._store[key] = fitness

"""Test helper: a CERTIFIED Alpaca history cache fixture (``_meta`` with a manifest, suspensions.json and a real gate
certificate written by ``verify.write_gate_certificate``), the shape ``verify_alpaca_cache_certified`` demands."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

BUILT_AT = "2026-10-21T10:00:00+00:00"


def write_certified_meta(cache_dir: Any, symbols: Optional[Dict[str, Any]] = None, *, run_id: str = "test-build",
                         manifest_symbols: Optional[Dict[str, Any]] = None, certify: bool = True,
                         status_pass: bool = True) -> Path:
    """Write ``<cache_dir>/_meta/{build_manifest,suspensions}.json`` (+ the gate certificate when ``certify``); returns ``_meta``.
    ``symbols``: the suspensions.json ``symbols`` block ({} = no events)."""
    from ba2_providers.ohlcv.alpaca_history import verify as ver
    meta = Path(cache_dir) / "_meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "build_manifest.json").write_text(json.dumps(
        {"split_table": {"run_id": run_id}, "built_at": BUILT_AT, "subset": False, "symbols": manifest_symbols or {}}), encoding="utf-8")
    (meta / "suspensions.json").write_text(json.dumps(
        {"build": f"{run_id} built {BUILT_AT}", "rule": {}, "symbols": symbols or {}, "undecided": {}}), encoding="utf-8")
    if certify:
        ver.write_gate_certificate(str(meta), {"structural": {"status": ver.STATUS_PASS if status_pass else ver.STATUS_FAIL}})
    return meta

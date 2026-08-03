"""run_options_matrix passes the gate-only screener flags through to every optimize job."""
import sys, os
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "tools"))

import run_options_matrix as M


def test_gate_passthrough_empty_without_store():
    assert M._gate_passthrough(SimpleNamespace(screener_gate_store=None,
                                               max_stock_price=100.0)) == []


def test_gate_passthrough_emits_both_flags():
    out = M._gate_passthrough(SimpleNamespace(screener_gate_store="/tmp/store.parquet",
                                              max_stock_price=80.0))
    assert out == ["--screener-gate-store", "/tmp/store.parquet",
                   "--max-stock-price", "80.0"]

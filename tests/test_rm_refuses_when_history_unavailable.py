"""A named history refusal (lock timeout, gap, stale corporate actions, too-new symbol) must REFUSE the order, never size or
protect it on a silently missing ATR (review of feat/history-source-task11, important finding 4)."""
from unittest.mock import patch

import pytest

from ba2_common.core.ohlcv_history import HistoryUnavailable, InsufficientHistory
from tests.test_funded_entry_loop import file_db  # noqa: F401  (fixture)
from tests.test_funded_entry_lot_size import ODD_LOT_PRICE, _run


@pytest.mark.parametrize("sizing_mode", [None, "risk_atr"])
def test_an_atr_history_refusal_refuses_the_order(file_db, sizing_mode):
    err = HistoryUnavailable("lock timeout on AAPL")
    with patch("ba2_common.core.position_sizing.get_latest_atr", side_effect=err):
        _seen, submits = _run(file_db, ODD_LOT_PRICE, lot_size=None, sizing_mode=sizing_mode)
    assert submits == []


def test_a_too_new_symbol_is_refused_not_stopped_on_the_risk_only_fallback(file_db):
    with patch("ba2_common.core.position_sizing.get_latest_atr",
               side_effect=InsufficientHistory("AAPL", "atr", 5, 14)):
        _seen, submits = _run(file_db, ODD_LOT_PRICE, lot_size=None, sizing_mode="risk_atr")
    assert submits == []


def test_control_without_the_refusal_the_same_scenario_submits(file_db):
    with patch("ba2_common.core.position_sizing.get_latest_atr", return_value=2.0):
        _seen, submits = _run(file_db, ODD_LOT_PRICE, lot_size=None, sizing_mode="risk_atr")
    assert len(submits) == 1

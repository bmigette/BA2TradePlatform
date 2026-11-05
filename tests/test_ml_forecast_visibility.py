"""read_row through the repo's date-keyed visibility rule (metric_store.scan_cutoff_date)."""
from datetime import date, datetime

import pytest

from ba2_common.core.ml_forecast import store
from ba2_common.core.ml_forecast.forecast_row import ForecastRow
from ba2_common.core.ml_forecast.model_card import model_key
from ba2_providers.screener.metric_store import scan_cutoff_date
from tools.ml_forecast.adapters.timesfm3 import timesfm3_card

CARD = timesfm3_card(512)
KEY = model_key(CARD, ())


@pytest.fixture
def root(tmp_path):
    store.reset_cache()
    dec = tuple(0.01 * (i - 4) for i in range(9))
    rows = [ForecastRow("AAA", d, {h: dec for h in CARD.horizons}) for d in (date(2024, 1, 5), date(2024, 1, 8), date(2024, 1, 9))]
    store.write_rows(KEY, CARD, {}, rows, (), tmp_path)
    yield tmp_path
    store.reset_cache()


def seen(root, instant, intraday):
    return store.read_row(KEY, "AAA", scan_cutoff_date(instant, intraday=intraday), 5, root).session


def test_intraday_row_s_is_visible_only_after_session_s_finished(root):
    assert seen(root, datetime(2024, 1, 9, 10, 0), True) == date(2024, 1, 8)       # Tue 10:00 sees Monday's row
    assert seen(root, datetime(2024, 1, 9, 15, 59), True) == date(2024, 1, 8)      # Tuesday not finished
    assert seen(root, datetime(2024, 1, 9, 16, 5), True) == date(2024, 1, 9)       # after the close
    assert seen(root, datetime(2024, 1, 8, 9, 30), True) == date(2024, 1, 5)       # Monday open: Friday's row
    assert seen(root, datetime(2024, 1, 6, 12, 0), True) == date(2024, 1, 5)       # Saturday: Friday finished


def test_daily_clock_day_d_sees_row_d(root):
    assert seen(root, date(2024, 1, 9), False) == date(2024, 1, 9)
    assert seen(root, datetime(2024, 1, 8, 10, 0), False) == date(2024, 1, 8)

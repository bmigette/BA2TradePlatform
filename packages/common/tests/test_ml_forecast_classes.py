"""Class-probability forecast rows (custom tsai models): row checks, store round trip, mapping."""
from datetime import date

import pytest

from ba2_common.core.ml_forecast import mapping, store
from ba2_common.core.ml_forecast.forecast_row import ForecastRow
from ba2_common.core.ml_forecast.model_card import model_key
from ba2_common.core.types import OrderRecommendation
from .test_ml_forecast_model_card import make_card

CF = ((0.2, -0.03), (0.3, 0.002), (0.5, 0.03))          # P(down/mid/up), return of each


def crow(cf=CF, h=5, d=date(2025, 3, 3)):
    return ForecastRow("AAPL", d, {}, class_probs={h: cf})


def genes(**kw):
    g = dict(horizon=5, min_edge_pct=0.5, min_prob=60.0, max_uncertainty=1.0, tp_quantile=70.0, sl_quantile=30.0,
             exit_flip_prob=60.0, enable_sell=False)
    g.update(kw)
    return mapping.MappingGenes(**g)


@pytest.fixture(autouse=True)
def _reset():
    store.reset_cache()
    yield
    store.reset_cache()


def test_row_checks():
    assert crow().has_classes and crow().horizons == (5,) and crow().deciles == {}
    with pytest.raises(ValueError, match="sum to 1"):
        crow(((0.2, -0.03), (0.3, 0.0), (0.6, 0.03)))
    with pytest.raises(ValueError, match="below up return"):
        crow(((0.2, 0.03), (0.3, 0.0), (0.5, -0.03)))
    with pytest.raises(ValueError, match="no deciles"):
        ForecastRow("AAPL", date(2025, 3, 3), {5: tuple(0.01 * i for i in range(9))}, class_probs={5: CF})


def test_store_round_trip(tmp_path):
    card = make_card(output_type="class_probs", horizons=(5,))
    key = model_key(card, ())
    store.write_rows(key, card, {"card_id": "x"}, [crow(), crow(d=date(2025, 3, 4))], covariate_set=(),
                     cache_root=tmp_path)
    r = store.read_row(key, "AAPL", date(2025, 3, 5), 5, cache_root=tmp_path)
    assert r.session == date(2025, 3, 4) and r.has_classes
    for (p, x), (q, y) in zip(r.class_probs[5], CF):
        assert p == pytest.approx(q, abs=1e-6) and x == pytest.approx(y, abs=1e-6)
    with pytest.raises(ValueError, match="has_classes"):
        store.write_rows(key, card, {}, [ForecastRow("AAPL", date(2025, 3, 6), {5: tuple(0.01 * i for i in range(9))})],
                         covariate_set=(), cache_root=tmp_path)


def test_map_long_uses_class_levels_and_conditional_probability():
    res = mapping.map_forecast(crow(), genes(), 100.0, 2.0)
    # E[r] = 0.2*-3% + 0.3*0.2% + 0.5*3% = +0.96%; P(up | decisive) = 0.5 / 0.7
    assert res.signal == OrderRecommendation.BUY
    assert res.expected_profit_percent == pytest.approx(0.96)
    assert res.confidence == pytest.approx(100 * 0.5 / 0.7)
    assert res.tp_price == pytest.approx(103.0) and res.sl_price == pytest.approx(97.0)


def test_map_gates():
    assert mapping.map_forecast(crow(), genes(min_edge_pct=1.0), 100.0, 2.0).signal == OrderRecommendation.HOLD
    assert mapping.map_forecast(crow(), genes(min_prob=75.0), 100.0, 2.0).signal == OrderRecommendation.HOLD
    bear = ((0.5, -0.03), (0.3, 0.0), (0.2, 0.03))
    assert "gate=sell" in mapping.map_forecast(crow(bear), genes(), 100.0, 2.0).reason
    s = mapping.map_forecast(crow(bear), genes(enable_sell=True), 100.0, 2.0)
    assert s.signal == OrderRecommendation.SELL and s.tp_price == pytest.approx(97.0) and s.sl_price == pytest.approx(103.0)


def test_held_flip_on_classes():
    bear = ((0.6, -0.03), (0.2, 0.0), (0.2, 0.03))         # P(down | decisive) = 0.75
    assert mapping.map_held_position(crow(bear), genes(exit_flip_prob=70.0), "long").signal == OrderRecommendation.SELL
    assert mapping.map_held_position(crow(), genes(exit_flip_prob=70.0), "long").signal == OrderRecommendation.HOLD
    assert mapping.p_up(crow(bear), 5) == pytest.approx(0.25)

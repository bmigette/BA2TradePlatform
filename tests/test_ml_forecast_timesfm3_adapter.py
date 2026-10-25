"""TimesFM 3.0 adapter maths with an injected stub model (no torch)."""
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.ml_forecast.adapters.timesfm3 import (TimesFM3Adapter, timesfm3_card, CARD_IDS)  # noqa: E402
from ba2_common.core.ml_forecast.model_card import model_key  # noqa: E402

QS = np.array([0.99, 0.995, 0.998, 0.999, 1.0, 1.001, 1.002, 1.005, 1.01])   # price multipliers at step 1


def stub(contexts, horizon):
    """Quantile path at step t = last * (1 + (QS-1) * t)."""
    out = []
    for c in contexts:
        steps = np.arange(1, horizon + 1)[:, None]
        out.append(float(c[-1]) * (1 + (QS[None, :] - 1) * steps))
    return out


def test_cards_distinct_keys_and_fields():
    a, b = timesfm3_card(512), timesfm3_card(2048)
    assert (a.family, a.version, a.horizons, a.output_type) == ("timesfm", "3.0.2", (5, 10, 20), "deciles")
    assert a.pretraining_cutoff is None and a.licence_commercial_ok is False
    assert a.licence == "timesfm-non-commercial-license-v1.0" and a.timeframe == "1d"
    assert model_key(a, ()) != model_key(b, ())
    assert set(CARD_IDS) == {"timesfm3-ctx512", "timesfm3-ctx2048"}
    with pytest.raises(ValueError):
        timesfm3_card(1000)


def test_return_definition():
    ad = TimesFM3Adapter(timesfm3_card(512), stub)
    close = np.linspace(100, 120, 600)
    r = ad.forecast_batch([("aapl", date(2024, 1, 5), close)])[0]
    assert r.symbol == "AAPL" and r.session == date(2024, 1, 5) and r.horizons == (5, 10, 20)
    for h in (5, 10, 20):
        np.testing.assert_allclose(r.deciles[h], (QS - 1) * h, rtol=1e-4, atol=1e-6)   # float32 context


def test_context_is_last_n_and_model_sees_only_it():
    seen = []

    def spy(ctxs, horizon):
        seen.extend(ctxs)
        assert horizon == 20
        return stub(ctxs, horizon)
    close = np.arange(1.0, 701.0)
    TimesFM3Adapter(timesfm3_card(512), spy).forecast_batch([("X", date(2024, 1, 5), close)])
    assert len(seen[0]) == 512 and seen[0][-1] == 700.0 and seen[0][0] == 189.0


def test_short_history_refused_loudly():
    with pytest.raises(ValueError, match="needs at least 512"):
        TimesFM3Adapter(timesfm3_card(512), stub).forecast_batch([("X", date(2024, 1, 5), np.ones(100))])


def test_non_finite_or_nonpositive_context_refused():
    c = np.ones(600)
    c[300] = np.nan
    with pytest.raises(ValueError, match="non-finite"):
        TimesFM3Adapter(timesfm3_card(512), stub).forecast_batch([("X", date(2024, 1, 5), c)])
    c = np.ones(600)
    c[-1] = 0.0
    with pytest.raises(ValueError, match="positive"):
        TimesFM3Adapter(timesfm3_card(512), stub).forecast_batch([("X", date(2024, 1, 5), c)])


def test_volume_covariate_refused_for_now():
    with pytest.raises(NotImplementedError, match="volume"):
        TimesFM3Adapter(timesfm3_card(512), stub, covariate_set=("volume",))


def test_wrong_output_shape_refused():
    def bad(ctxs, h):
        return [np.ones((h, 5)) for _ in ctxs]
    with pytest.raises(ValueError, match="shape"):
        TimesFM3Adapter(timesfm3_card(512), bad).forecast_batch([("X", date(2024, 1, 5), np.ones(600))])


def test_crossing_quantiles_raise():
    def crossed(ctxs, h):
        q = stub(ctxs, h)
        for x in q:
            x[:, [3, 4]] = x[:, [4, 3]]
        return q
    with pytest.raises(ValueError, match="monotone"):
        TimesFM3Adapter(timesfm3_card(512), crossed).forecast_batch([("X", date(2024, 1, 5), np.ones(600))])

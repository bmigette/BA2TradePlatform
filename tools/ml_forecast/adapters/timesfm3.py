"""TimesFM 3.0 adapter: daily closes ending at session S -> ForecastRow (fractional H-session returns at 9 deciles).

Runs in the ``ba2-ml`` venv (torch + timesfm 3.0.2). This module imports only numpy at the top; torch and timesfm3 are
imported by ``load_timesfm3_predict_fn``, so the maths is testable with an injected ``predict_fn``.

Return definition: one model call per context gives the price quantile path ``Q[step, k]`` (k = deciles 0.1..0.9).
For horizon h the decile return is ``Q[h-1, k] / close_S - 1``.

Model: ``google/timesfm-3.0-pytorch``. Pretraining data (per the model card; no stock data listed, so the cutoff is
recorded as unknown): GiftEval pretrain, Wikipedia pageviews (to 2023-11), Google Trends (to 2022), synthetic data.
Weights are non-commercial / non-production (owner decision 2026-10-25: runs everywhere, warn, never block).
"""
from __future__ import annotations

import os
from datetime import date
from typing import Callable, Iterable, List, Sequence, Tuple

import numpy as np

from ba2_common.core.ml_forecast.forecast_row import DECILE_LEVELS, ForecastRow
from ba2_common.core.ml_forecast.model_card import ModelCard

CHECKPOINT = "google/timesfm-3.0-pytorch"
TIMESFM_VERSION = "3.0.2"
LICENCE = "timesfm-non-commercial-license-v1.0"
HORIZONS = (5, 10, 20)
CONTEXT_LENGTHS = (512, 2048)
#: CLI name -> context length
CARD_IDS = {f"timesfm3-ctx{c}": c for c in CONTEXT_LENGTHS}

#: predict_fn(contexts, horizon) -> one array (horizon, 9) of PRICE quantiles per context
PredictFn = Callable[[Sequence[np.ndarray], int], List[np.ndarray]]


def timesfm3_card(context_length: int) -> ModelCard:
    if context_length not in CONTEXT_LENGTHS:
        raise ValueError(f"TimesFM 3.0 context length must be one of {CONTEXT_LENGTHS}, got {context_length!r}")
    return ModelCard(
        model_id=CHECKPOINT, family="timesfm", version=TIMESFM_VERSION,
        input_fields=("Close",), timeframe="1d", context_length=context_length,
        past_only_covariates=(), past_future_covariates=(), horizons=HORIZONS, scaler=None,
        output_type="deciles", pretraining_cutoff=None, licence=LICENCE, licence_commercial_ok=False)


class InputDataError(ValueError):
    """The CONTEXT is unusable (short, non-finite, non-positive). A data defect of one symbol-session: the builder
    may isolate, count and report it. Every other error (model output shape, crossing quantiles) is a contract
    failure and aborts the build."""


class TimesFM3Adapter:
    def __init__(self, card: ModelCard, predict_fn: PredictFn, covariate_set: Iterable[str] = ()):
        cov = tuple(sorted(covariate_set))
        if cov:
            raise NotImplementedError(f"covariate set {cov!r} is not built yet (milestone 2 is univariate; "
                                      f"'volume' is planned)")
        if card.family != "timesfm" or card.output_type != "deciles":
            raise ValueError(f"TimesFM3Adapter needs a timesfm deciles card, got {card.family}/{card.output_type}")
        self.card, self.predict_fn, self.covariate_set = card, predict_fn, cov
        self.max_horizon = max(card.horizons)

    def forecast_batch(self, items: Sequence[Tuple[str, date, np.ndarray]]) -> List[ForecastRow]:
        """items: (symbol, session S, daily closes with the LAST element = the close of S). Only the last
        ``context_length`` closes are shown to the model."""
        n = self.card.context_length
        ctxs = []
        for sym, sess, close in items:
            c = np.asarray(close, dtype=np.float64)
            if c.ndim != 1 or len(c) < n:
                raise InputDataError(f"{sym} {sess}: needs at least {n} closes, got {len(c)}")
            c = c[-n:]
            if not np.all(np.isfinite(c)):
                raise InputDataError(f"{sym} {sess}: non-finite close in the context")
            if not np.all(c > 0):
                raise InputDataError(f"{sym} {sess}: closes must be positive")
            ctxs.append(c.astype(np.float32))
        if not ctxs:
            return []
        qs = self.predict_fn(ctxs, self.max_horizon)
        if len(qs) != len(ctxs):
            raise ValueError(f"model returned {len(qs)} forecasts for {len(ctxs)} contexts")
        rows = []
        for (sym, sess, _), c, q in zip(items, ctxs, qs):
            q = np.asarray(q, dtype=np.float64)
            if q.shape != (self.max_horizon, len(DECILE_LEVELS)):
                raise ValueError(f"{sym} {sess}: unexpected forecast shape {q.shape}")
            last = float(c[-1])
            dec = {h: tuple(float(x) for x in q[h - 1] / last - 1.0) for h in self.card.horizons}
            rows.append(ForecastRow(sym, sess, dec))   # validates finiteness and monotone deciles
        return rows


def load_timesfm3_predict_fn(threads: int = 6, batch_size: int = 64) -> PredictFn:
    """Load the real model (CPU). Caps threads BEFORE torch spins up its pools."""
    for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[k] = str(threads)
    import torch
    torch.set_num_threads(threads)
    from timesfm3 import ModelConfig, TimesFM3Evaluator
    model = TimesFM3Evaluator(ModelConfig(checkpoint_path=CHECKPOINT, per_core_batch_size=batch_size, device="cpu"))

    def predict(contexts: Sequence[np.ndarray], horizon: int) -> List[np.ndarray]:
        outs = list(model.predict_batch(list(contexts), horizon=horizon, return_quantiles=True))
        return [np.asarray(o.quantiles) for o in outs]
    return predict

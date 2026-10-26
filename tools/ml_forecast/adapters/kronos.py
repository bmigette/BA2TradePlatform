"""Kronos adapter: daily OHLCV ending at session S -> ForecastRow with deciles AND the P(TP before SL) grid.

Runs in the ``ba2-ml`` venv. This module imports only numpy and pandas at the top; torch and the Kronos source are
imported by ``load_kronos_sample_fn``, so the maths is testable with an injected ``sample_fn`` (no torch).

Model: Kronos (Shi et al., a decoder-only transformer over a hierarchical binary-spherical-quantised K-line token
stream; MIT licence). Checkpoints ``NeoQuasar/Kronos-small`` (24.7M) and ``NeoQuasar/Kronos-base`` (102.3M) with the
tokenizer ``NeoQuasar/Kronos-Tokenizer-base``; max context 512 for both. PRETRAINING CUTOFF: none is published, so the
card records ``None`` (= every date possibly contaminated). The paper is mid-2025 and its corpus covers 45+ exchanges
including US equities, so 2020-2024 is almost certainly in the training data: only dates after mid-2025 and live
forward testing are clean evidence (design doc section 3).

What a row is. One call per context samples ``n_paths`` joint OHLC price paths of ``max(horizons)`` sessions after S.
For horizon H (sessions after S):

* decile k (10..90) = the empirical k-th percentile (``np.percentile``, linear, monotone by construction) over paths
  of ``close_path[H-1] / close_S - 1``;
* ``p_long_h{H}_tp{TP}_sl{SL}`` = share of paths in which the High reaches ``close_S*(1+TP%)`` strictly BEFORE the Low
  reaches ``close_S*(1-SL%)``, within the first H steps (daily bars: the order inside one bar is unknown);
* ``p_short_...`` mirrored: the Low reaches ``close_S*(1-TP%)`` before the High reaches ``close_S*(1+SL%)``.
  A step in which both levels are touched counts as the STOP (conservative); no TP touch within H steps is not a win.
  The sampled High/Low are not forced to bracket the sampled Open/Close, so the effective High of a step is
  ``max(O,H,L,C)`` and the effective Low ``min(O,H,L,C)`` (a close above the sampled high IS a price the path reached).

Rejected paths. The token decoder occasionally emits an outlier value (measured: ~1 path in 600, e.g. a Low of -15.7
on a 160 stock), which is not a price. A path holding a non-positive price anywhere is DROPPED and the statistics use
the remaining paths (dropping is neutral; clipping a glitch to a tiny positive price would register a crash). Every
affected forecast is NAMED in a warning and in ``KronosAdapter.reject_log`` (the builder reports them at the end),
the total in ``KronosAdapter.rejected_paths``. A forecast with fewer than ``MIN_VALID_SHARE`` of its paths valid
is a contract error (the model is broken, not noisy). A non-finite value is always a contract error.

Sampling (``load_kronos_sample_fn``) is our own re-implementation of the loop of
``model.kronos.auto_regressive_inference`` (same normalisation, token buffer, stamps, ``top_p`` filter, decode) that
returns the RAW paths instead of their mean, and replaces ``torch.multinomial`` on the global RNG by inverse-CDF
sampling from uniforms drawn per ITEM from ``numpy.random.default_rng(seed)``. Determinism: the seed of an item is
``derive_seed(model_key, symbol, session)`` (sha256), so the random numbers an item consumes do not depend on the
batch it sits in or on the order of the batch. What the batch can still change is the float rounding of the GPU
matmuls (cuBLAS picks kernels by batch shape): a ~1e-6 logit difference can flip one inverse-CDF draw whose uniform
lands on a token boundary, which changes that one path. The same batch composition on the same GPU repeats exactly;
different compositions agree to within that rare path flip (measured: see the milestone report). Rebuilding with
``--resume`` therefore keeps stored rows and fills the rest; it never rewrites a stored row.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from ba2_common.core.ml_forecast.forecast_row import (DECILE_LEVELS, DIRECTIONS, SL_GRID_PCT, TP_GRID_PCT, ForecastRow,
                                                      ProbKey)
from ba2_common.core.ml_forecast.model_card import ModelCard, model_key
from tools.ml_forecast.adapters.timesfm3 import InputDataError

log = logging.getLogger("ml_forecast.kronos")

KRONOS_SRC_COMMIT = "67b630e"          # the Kronos checkout the sampler was written against
LICENCE = "MIT"
HORIZONS = (5, 10, 20)
CONTEXT_LENGTH = 512
DEFAULT_N_PATHS = 64
#: CLI name -> (card size, HF model repo, HF tokenizer repo)
SIZES = {
    "small": ("NeoQuasar/Kronos-small", "NeoQuasar/Kronos-Tokenizer-base"),
    "base": ("NeoQuasar/Kronos-base", "NeoQuasar/Kronos-Tokenizer-base"),
}
CARD_IDS = {f"kronos-{s}": s for s in SIZES}
#: Sampling hyper-parameters are part of the row definition, hence of the card version: they are constants, not
#: arguments (the KronosPredictor.predict defaults).
TEMPERATURE = 1.0
TOP_K = 0
TOP_P = 0.9
CLIP = 5.0
#: Revision of THIS module's row maths and sampler (bump on any change that alters a stored value).
ADAPTER_REVISION = 1
#: At least this share of the sampled paths must be valid (positive prices) or the forecast is refused.
MIN_VALID_SHARE = 0.75
KRONOS_ENV = "BA2_KRONOS_SRC"

#: sample_fn(contexts, x_dates, y_dates, n_paths, seeds) -> one array (n_paths, pred_len, 4) of sampled OHLC PRICES
#: per context. contexts: (n, 5) float64 Open/High/Low/Close/Volume; x_dates: (n,) datetime64[D] of the context bars;
#: y_dates: (pred_len,) datetime64[D] of the forecast steps (the next business days after S); seeds: one int per item.
SampleFn = Callable[[Sequence[np.ndarray], Sequence[np.ndarray], Sequence[np.ndarray], int, Sequence[int]],
                    List[np.ndarray]]

_PATHS_RE = re.compile(r"paths=(\d+)")


def kronos_card(size: str, n_paths: int = DEFAULT_N_PATHS) -> ModelCard:
    """Card of Kronos ``size`` ("small" | "base"). ``n_paths`` changes every stored value, so it is part of the
    card's ``version`` (together with the sampler constants and the adapter revision): a different path count is a
    different store key, never a silent mix of 32-path and 64-path rows."""
    if size not in SIZES:
        raise ValueError(f"Kronos size must be one of {sorted(SIZES)}, got {size!r}")
    if isinstance(n_paths, bool) or not isinstance(n_paths, int) or n_paths < 2:
        raise ValueError(f"n_paths must be an int >= 2, got {n_paths!r}")
    version = (f"kronos@{KRONOS_SRC_COMMIT};ba2-adapter-{ADAPTER_REVISION};T={TEMPERATURE};top_k={TOP_K};"
               f"top_p={TOP_P};clip={CLIP:g};paths={n_paths}")
    return ModelCard(
        model_id=SIZES[size][0], family="kronos", version=version,
        input_fields=("Open", "High", "Low", "Close", "Volume"), timeframe="1d", context_length=CONTEXT_LENGTH,
        past_only_covariates=(), past_future_covariates=(), horizons=HORIZONS, scaler=None,
        output_type="paths", pretraining_cutoff=None, licence=LICENCE, licence_commercial_ok=True)


def card_n_paths(card: ModelCard) -> int:
    m = _PATHS_RE.search(card.version)
    if card.family != "kronos" or m is None:
        raise ValueError(f"not a Kronos card (family {card.family!r}, version {card.version!r})")
    return int(m.group(1))


def derive_seed(key: str, symbol: str, session: date) -> int:
    """The sampling seed of one forecast: first 8 bytes of sha256(model_key | SYMBOL | session ISO date). A pure
    function of the identity of the row, so a rebuild draws the same random numbers."""
    blob = f"kronos-seed|{key}|{symbol.strip().upper()}|{session.isoformat()}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(blob).digest()[:8], "big")


def forecast_dates(session: date, steps: int) -> np.ndarray:
    """The ``steps`` business days (Mon-Fri; exchange holidays are not modelled) strictly after ``session``."""
    d = pd.bdate_range(pd.Timestamp(session) + pd.Timedelta(days=1), periods=steps)
    return d.to_numpy().astype("datetime64[D]")


def first_touch_steps(hi: np.ndarray, lo: np.ndarray, close_s: float, pcts: Iterable[int]
                      ) -> Tuple[Dict[int, np.ndarray], Dict[int, np.ndarray]]:
    """Per path, the 0-based step at which the High first reaches ``close_s*(1+k%)`` (``up[k]``) and the Low first
    reaches ``close_s*(1-k%)`` (``dn[k]``); ``T`` (= number of steps) when it never does. ``hi``, ``lo``: (P, T)."""
    n_steps = hi.shape[1]
    up: Dict[int, np.ndarray] = {}
    dn: Dict[int, np.ndarray] = {}
    for k in pcts:
        hit_up = hi >= close_s * (100 + k) / 100
        hit_dn = lo <= close_s * (100 - k) / 100
        up[k] = np.where(hit_up.any(axis=1), hit_up.argmax(axis=1), n_steps)
        dn[k] = np.where(hit_dn.any(axis=1), hit_dn.argmax(axis=1), n_steps)
    return up, dn


def path_probabilities(paths: np.ndarray, close_s: float, horizons: Sequence[int]) -> Dict[ProbKey, float]:
    """P(TP before SL within H steps) for the whole grid. ``paths``: (P, T, 4) sampled Open/High/Low/Close prices
    of the steps after S. Win in horizon H = the TP level is touched at a step < H and strictly before the first
    step that touches the stop (same step = stop); a path that never touches the TP is a loss."""
    if paths.ndim != 3 or paths.shape[2] != 4:
        raise ValueError(f"paths must be (P, T, 4), got {paths.shape}")
    if max(horizons) > paths.shape[1]:
        raise ValueError(f"paths hold {paths.shape[1]} steps, horizon {max(horizons)} needed")
    hi = paths.max(axis=2)          # effective High / Low: see the module docstring
    lo = paths.min(axis=2)
    up, dn = first_touch_steps(hi, lo, close_s, sorted(set(TP_GRID_PCT) | set(SL_GRID_PCT)))
    out: Dict[ProbKey, float] = {}
    for h in horizons:
        for tp in TP_GRID_PCT:
            for sl in SL_GRID_PCT:
                long_win = (up[tp] < h) & (up[tp] < dn[sl])
                short_win = (dn[tp] < h) & (dn[tp] < up[sl])
                out[("long", h, tp, sl)] = float(long_win.mean())
                out[("short", h, tp, sl)] = float(short_win.mean())
    assert {k[0] for k in out} == set(DIRECTIONS)
    return out


def decile_returns(close_paths: np.ndarray, close_s: float, horizons: Sequence[int]) -> Dict[int, Tuple[float, ...]]:
    """Empirical deciles of ``close_path[H-1]/close_S - 1``. ``close_paths``: (P, T)."""
    return {h: tuple(float(x) for x in np.percentile(close_paths[:, h - 1] / close_s - 1.0, DECILE_LEVELS))
            for h in horizons}


class KronosAdapter:
    def __init__(self, card: ModelCard, sample_fn: SampleFn, n_paths: int = DEFAULT_N_PATHS,
                 covariate_set: Iterable[str] = ()):
        cov = tuple(sorted(covariate_set))
        if cov:
            raise NotImplementedError(f"covariate set {cov!r} is not supported by Kronos (OHLCV only)")
        if card.family != "kronos" or card.output_type != "paths":
            raise ValueError(f"KronosAdapter needs a kronos paths card, got {card.family}/{card.output_type}")
        if card_n_paths(card) != n_paths:
            raise ValueError(f"n_paths={n_paths} does not match the card's paths={card_n_paths(card)}: build the "
                             f"card with kronos_card(size, n_paths)")
        self.card, self.sample_fn, self.n_paths, self.covariate_set = card, sample_fn, n_paths, cov
        self.max_horizon = max(card.horizons)
        self.key = model_key(card, cov)
        self.rejected_paths = 0        # paths dropped for a non-positive price, over the adapter's lifetime
        self.forecasts_with_rejects = 0
        self.reject_log: List[str] = []   # "SYM session: k/n paths dropped", one entry per affected forecast

    def forecast_batch(self, items: Sequence[Tuple[str, date, object]]) -> List[ForecastRow]:
        """items: (symbol, session S, OHLCV context ending at S). The context is a DataFrame indexed by the bar dates
        with columns Open High Low Close Volume (what the builder passes), or a bare (N, 5) array whose bar dates
        are then taken as the N business days ending at S (tests). The LAST bar must be S. Only the last
        ``context_length`` bars are shown to the model."""
        n = self.card.context_length
        fields = list(self.card.input_fields)
        ctxs: List[np.ndarray] = []
        xd: List[np.ndarray] = []
        yd: List[np.ndarray] = []
        seeds: List[int] = []
        for sym, sess, raw in items:
            if isinstance(raw, pd.DataFrame):
                missing = [c for c in fields if c not in raw.columns]
                if missing:
                    raise InputDataError(f"{sym} {sess}: context lacks columns {missing}")
                v = raw[fields].to_numpy(dtype=np.float64)
                dates = pd.DatetimeIndex(raw.index).to_numpy().astype("datetime64[D]")
            else:
                v = np.asarray(raw, dtype=np.float64)
                dates = None
            if v.ndim != 2 or v.shape[1] != len(fields):
                raise InputDataError(f"{sym} {sess}: OHLCV must be (N, {len(fields)}), got shape {v.shape}")
            if len(v) < n:
                raise InputDataError(f"{sym} {sess}: needs at least {n} bars, got {len(v)}")
            if dates is None:
                dates = pd.bdate_range(end=pd.Timestamp(sess), periods=len(v)).to_numpy().astype("datetime64[D]")
            if dates[-1] != np.datetime64(sess, "D"):
                raise InputDataError(f"{sym} {sess}: the last bar is dated {dates[-1]}, not the session")
            v, dates = v[-n:], dates[-n:]
            if not np.all(np.isfinite(v)):
                raise InputDataError(f"{sym} {sess}: non-finite value in the context")
            if not np.all(v[:, :4] > 0):
                raise InputDataError(f"{sym} {sess}: open/high/low/close must be positive")
            if not np.all(v[:, 4] >= 0):
                raise InputDataError(f"{sym} {sess}: negative volume in the context")
            ctxs.append(v)
            xd.append(dates)
            yd.append(forecast_dates(sess, self.max_horizon))
            seeds.append(derive_seed(self.key, sym, sess))
        if not ctxs:
            return []
        outs = self.sample_fn(ctxs, xd, yd, self.n_paths, seeds)
        if len(outs) != len(ctxs):
            raise ValueError(f"sampler returned {len(outs)} path sets for {len(ctxs)} contexts")
        rows = []
        for (sym, sess, _), ctx, p in zip(items, ctxs, outs):
            p = np.asarray(p, dtype=np.float64)
            if p.shape != (self.n_paths, self.max_horizon, 4):
                raise ValueError(f"{sym} {sess}: unexpected paths shape {p.shape}, expected "
                                 f"{(self.n_paths, self.max_horizon, 4)}")
            if not np.all(np.isfinite(p)):
                raise ValueError(f"{sym} {sess}: sampled paths hold non-finite prices")
            valid = (p > 0).all(axis=(1, 2))
            if valid.sum() < MIN_VALID_SHARE * self.n_paths:
                raise ValueError(f"{sym} {sess}: only {int(valid.sum())}/{self.n_paths} sampled paths hold "
                                 f"positive prices only (need {MIN_VALID_SHARE:.0%})")
            if not valid.all():
                self.rejected_paths += int((~valid).sum())
                self.forecasts_with_rejects += 1
                self.reject_log.append(f"{sym} {sess}: {int((~valid).sum())}/{self.n_paths} paths dropped "
                                       f"(min price {float(p.min()):.3f})")
                log.warning("dropped sampled paths with a non-positive price: %s", self.reject_log[-1])
                p = p[valid]
            close_s = float(ctx[-1, 3])
            dec = decile_returns(p[:, :, 3], close_s, self.card.horizons)
            probs = path_probabilities(p, close_s, self.card.horizons)
            rows.append(ForecastRow(sym, sess, dec, probs))   # validates finiteness, monotone deciles, full grid
        return rows


def _import_kronos(kronos_src: Optional[str]):
    src = kronos_src or os.environ.get(KRONOS_ENV)
    if not src:
        raise RuntimeError(f"the Kronos source checkout is not configured: pass kronos_src / --kronos-src or set "
                           f"{KRONOS_ENV} to a clone of github.com/shiyu-coder/Kronos (MIT)")
    src_p = Path(src).resolve()
    if not (src_p / "model" / "kronos.py").exists():
        raise RuntimeError(f"{src_p} holds no model/kronos.py: not a Kronos checkout")
    if str(src_p) not in sys.path:
        sys.path.insert(0, str(src_p))
    import model.kronos as kmod
    if Path(kmod.__file__).resolve().parent.parent != src_p:
        raise RuntimeError(f"`model` resolved to {kmod.__file__}, not the Kronos checkout {src_p}: another "
                           f"top-level `model` package shadows it")
    return kmod


def load_kronos_sample_fn(size: str, device: str = "cuda", kronos_src: Optional[str] = None,
                          max_rows: int = 1024) -> SampleFn:
    """Load Kronos ``size`` on ``device`` and return a ``SampleFn``. ``max_rows``: contexts*n_paths rows per GPU
    forward (the contexts of one call are processed in chunks of ``max(1, max_rows // n_paths)``). The returned
    function carries ``.provenance`` (HF repo ids and resolved commit hashes when available)."""
    if size not in SIZES:
        raise ValueError(f"Kronos size must be one of {sorted(SIZES)}, got {size!r}")
    import torch
    import torch.nn.functional as F
    torch.set_float32_matmul_precision("highest")      # full fp32: values must not depend on a TF32 default
    if device.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError(f"device {device!r} requested but torch {torch.__version__} sees no CUDA device")
    kmod = _import_kronos(kronos_src)
    model_repo, tok_repo = SIZES[size]
    tokenizer = kmod.KronosTokenizer.from_pretrained(tok_repo).to(device).eval()
    model = kmod.Kronos.from_pretrained(model_repo).to(device).eval()
    provenance = {"kronos_src_commit": KRONOS_SRC_COMMIT, "model_repo": model_repo, "tokenizer_repo": tok_repo}
    try:
        from huggingface_hub import HfApi
        api = HfApi()
        provenance["model_sha"] = api.model_info(model_repo).sha
        provenance["tokenizer_sha"] = api.model_info(tok_repo).sha
    except Exception as e:                                    # metadata only: never block the run on the network
        provenance["hf_sha_error"] = f"{type(e).__name__}: {e}"

    @torch.no_grad()
    def _chunk(ctxs, x_dates, y_dates, n_paths, seeds) -> List[np.ndarray]:
        b, pred_len = len(ctxs), len(y_dates[0])
        x = np.stack(ctxs).astype(np.float64)                                            # (B, n, 5)
        vol = x[:, :, 4:5]
        x6 = np.concatenate([x, vol * x[:, :, :4].mean(axis=2, keepdims=True)], axis=2)  # + amount, as KronosPredictor
        mean, std = x6.mean(axis=1, keepdims=True), x6.std(axis=1, keepdims=True)
        xn = np.clip((x6 - mean) / (std + 1e-5), -CLIP, CLIP).astype(np.float32)

        def stamps(dates):
            ts = kmod.calc_time_stamps(pd.Series(pd.DatetimeIndex(dates)))
            return ts.values.astype(np.float32)
        xs = np.stack([stamps(d) for d in x_dates])
        ys = np.stack([stamps(d) for d in y_dates])
        # one uniform per (item, path, step, token level), from the item's own generator
        u = np.stack([np.random.default_rng(int(s)).random((n_paths, pred_len, 2)) for s in seeds])
        t = lambda a: torch.from_numpy(a).to(device)          # noqa: E731
        z = _sample(tokenizer, model, t(xn), t(xs), t(ys), t(u.reshape(b * n_paths, pred_len, 2)), n_paths, pred_len,
                    F, kmod)                                           # (B, S, pred, 6) normalised
        z = z.double().cpu().numpy()[:, :, :, :4]
        price = z * (std[:, None, :, :4] + 1e-5) + mean[:, None, :, :4]                   # (B, S, pred, 4)
        return [price[i] for i in range(b)]

    def sample(contexts, x_dates, y_dates, n_paths, seeds) -> List[np.ndarray]:
        step = max(1, max_rows // n_paths)
        out: List[np.ndarray] = []
        for i in range(0, len(contexts), step):
            out.extend(_chunk(contexts[i:i + step], x_dates[i:i + step], y_dates[i:i + step], n_paths,
                              seeds[i:i + step]))
        return out
    sample.provenance = provenance
    return sample


def _icdf_sample(logits, u, F, kmod):
    """Draw one token per row: temperature, the Kronos top-k/top-p filter, softmax, then the first token whose CDF
    reaches ``u * total`` (u in (0, 1], so the draw always has positive probability). logits: (R, V); u: (R,)."""
    logits = logits / TEMPERATURE
    if TOP_K > 0 or TOP_P < 1.0:
        logits = kmod.top_k_top_p_filtering(logits, top_k=TOP_K, top_p=TOP_P)
    cdf = F.softmax(logits, dim=-1).double().cumsum(dim=-1)
    target = (1.0 - u) * cdf[:, -1]
    idx = (cdf < target[:, None]).sum(dim=-1)
    return idx.clamp_(max=cdf.shape[1] - 1).unsqueeze(-1)


def _decode_last_s1(model, s1_ids, s2_ids, stamp):
    """``model.decode_s1`` with the vocabulary head applied to the LAST position only (the only logits the sampler
    reads): same maths, without the (rows, 512, 1024) logits tensor. Returns (logits (R, V), context (R, L, d))."""
    x = model.embedding([s1_ids, s2_ids]) + model.time_emb(stamp)
    for layer in model.transformer:
        x = layer(x)
    x = model.norm(x)
    return model.head(x[:, -1, :]), x


def _decode_last_s2(model, context, s1_ids):
    """``model.decode_s2`` for the last position only: the dependency layer's cross attention reads the whole context
    (keys / values) but its residual, norm and head are needed at the last position only. (R, V)."""
    dep = model.dep_layer
    sib = model.embedding.emb_s1(s1_ids)
    attn = dep.cross_attn(query=sib, key=context, value=context)
    return model.head.cond_forward(dep.norm(context[:, -1:, :] + attn))[:, -1, :]


def _sample(tokenizer, model, x, x_stamp, y_stamp, u, n_paths, pred_len, F, kmod):
    """Raw-path twin of ``model.kronos.auto_regressive_inference`` (max context = ``CONTEXT_LENGTH``). x: (B, n, 6)
    normalised; u: (B*n_paths, pred_len, 2) uniforms. Returns (B, n_paths, pred_len, 6) normalised values."""
    import torch
    max_context = CONTEXT_LENGTH
    rep = lambda a: a.unsqueeze(1).repeat(1, n_paths, 1, 1).reshape(-1, a.size(1), a.size(2))   # noqa: E731
    x, x_stamp, y_stamp = rep(torch.clip(x, -CLIP, CLIP)), rep(x_stamp), rep(y_stamp)
    x_token = tokenizer.encode(x, half=True)
    initial = x.size(1)
    rows = x_token[0].size(0)
    full_stamp = torch.cat([x_stamp, y_stamp], dim=1)
    gen_pre = x_token[0].new_empty(rows, pred_len)
    gen_post = x_token[1].new_empty(rows, pred_len)
    pre_buf = x_token[0].new_zeros(rows, max_context)
    post_buf = x_token[1].new_zeros(rows, max_context)
    keep = min(initial, max_context)
    start = max(0, initial - max_context)
    pre_buf[:, :keep] = x_token[0][:, start:start + keep]
    post_buf[:, :keep] = x_token[1][:, start:start + keep]
    for i in range(pred_len):
        cur = initial + i
        win = min(cur, max_context)
        pre_in, post_in = (pre_buf[:, :win], post_buf[:, :win]) if cur <= max_context else (pre_buf, post_buf)
        stamp = full_stamp[:, max(0, cur - max_context):cur, :].contiguous()
        s1_logits, context = _decode_last_s1(model, pre_in, post_in, stamp)
        s_pre = _icdf_sample(s1_logits, u[:, i, 0], F, kmod)
        s_post = _icdf_sample(_decode_last_s2(model, context, s_pre), u[:, i, 1], F, kmod)
        gen_pre[:, i] = s_pre.squeeze(-1)
        gen_post[:, i] = s_post.squeeze(-1)
        if cur < max_context:
            pre_buf[:, cur] = s_pre.squeeze(-1)
            post_buf[:, cur] = s_post.squeeze(-1)
        else:
            pre_buf.copy_(torch.roll(pre_buf, shifts=-1, dims=1))
            post_buf.copy_(torch.roll(post_buf, shifts=-1, dims=1))
            pre_buf[:, -1] = s_pre.squeeze(-1)
            post_buf[:, -1] = s_post.squeeze(-1)
    total = initial + pred_len
    cs = max(0, total - max_context)
    full_pre = torch.cat([x_token[0], gen_pre], dim=1)[:, cs:total].contiguous()
    full_post = torch.cat([x_token[1], gen_post], dim=1)[:, cs:total].contiguous()
    z = tokenizer.decode([full_pre, full_post], half=True)[:, -pred_len:, :]
    return z.reshape(-1, n_paths, pred_len, z.size(2))

"""Sentiment scoring for the news store: model registry, label normalisation, CPU inference.

TORCH IS AN OPTIONAL DEPENDENCY. Nothing here is imported at module load -- ``torch`` and
``transformers`` are imported inside ``score_texts`` so the live trade platform, which
reads scores but never produces them, does not pull a deep-learning stack into its
process. Import this module freely; it costs nothing until you actually score.

LABELS ARE MAPPED BY NAME, NEVER BY INDEX. The candidate models disagree on ordering,
and the disagreement is silent -- every one of them emits a 3-vector, so an index-based
read produces plausible-looking numbers with positive and negative swapped:

    ProsusAI/finbert          0=positive  1=negative  2=neutral
    cardiffnlp/twitter-...    0=negative  1=neutral   2=positive
    yiyanghkust/finbert-tone  0=Neutral   1=Positive  2=Negative

So the mapping is derived from each checkpoint's own ``id2label`` at load time and
verified against a known-sign probe sentence before any real scoring happens. A model
whose labels cannot be resolved is refused rather than guessed at.

MEMORY. Exactly one model is resident at a time: ``score_texts`` loads, scores, then
frees it and forces a collection before returning. Peak footprint is dominated by the
activation tensors, not the weights, so batch size and sequence length are the knobs --
both capped low here because this runs alongside a GA grid that is already near the
machine's memory ceiling. The news substrate is headline + summary (~250-650 characters,
well under 160 tokens), so a 256-token cap truncates almost nothing.

THE ``score`` COLUMN. For every model scored here, ``score = pos - neg`` -- signed
sentiment in [-1, +1]. Note this differs from the migrated ``finbert-legacy`` rows, where
the ML platform stored the winning class's CONFIDENCE instead. Nothing computes on
``score`` (``aggregate_sentiment`` uses ``pos - neg`` directly), so the column is
informational, but the two are not comparable numbers.
"""
from __future__ import annotations

import gc
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from ba2_common.logger import logger

# Candidate models, chosen under a hard memory constraint (a GA grid holds ~26 GB while
# this runs). The two obvious "better than FinBERT" options are excluded on size:
# soleimanian/financial-roberta-large-sentiment is RoBERTa-large (1.4 GB, 24 layers,
# hidden 1024) and nickmuchi/deberta-v3-base-finetuned-finance (1.5 GB, and DeBERTa's
# disentangled attention roughly doubles activation memory over BERT for no advantage on
# 160-token text).
MODELS: Dict[str, Dict[str, object]] = {
    # The incumbent, re-scored properly. The migrated legacy rows are one-hot (label +
    # confidence); this produces real distributions, so it is the honest baseline to
    # compare the challengers against -- not the legacy column.
    "finbert": {"hf": "ProsusAI/finbert", "mb": 438},

    # Same architecture, DIFFERENT training corpus: analyst reports and earnings-call
    # tone rather than Financial PhraseBank. The most likely source of a genuine edge.
    "finbert-tone": {"hf": "yiyanghkust/finbert-tone", "mb": 439},

    # DistilRoBERTa: 6 layers, ~half the weights and activations of the others, and
    # fine-tuned specifically on financial NEWS -- which is exactly this substrate.
    # The cheapest candidate and the best-matched one.
    "distilroberta-news": {
        "hf": "mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis",
        "mb": 330},

    # Deliberate NON-finance control. If a general social-media sentiment model matches
    # or beats the finance-tuned ones on forward-return IC, that is worth knowing before
    # anyone concludes domain tuning is what matters here.
    "twitter-roberta": {"hf": "cardiffnlp/twitter-roberta-base-sentiment-latest", "mb": 501},
}

# Label synonyms across the candidates. Matched case-insensitively on the checkpoint's
# own id2label; anything unrecognised (e.g. "LABEL_0") is an error, not a guess.
_POS = {"positive", "pos", "bullish", "somewhat-bullish", "label_positive"}
_NEG = {"negative", "neg", "bearish", "somewhat-bearish", "label_negative"}
_NEU = {"neutral", "neu", "label_neutral"}

# A sentence whose sign is not in dispute. If a resolved mapping cannot get this right,
# the mapping is wrong and scoring must not proceed -- this is the guard that turns a
# silent sign-flip into a loud failure.
_PROBE = "The company reported a sharp drop in profits and cut its full-year guidance."


def available_models() -> List[str]:
    return list(MODELS)


def _resolve_label_map(id2label: Dict) -> Dict[str, int]:
    """Map canonical pos/neu/neg to this checkpoint's output indices, by NAME."""
    out: Dict[str, int] = {}
    for idx, name in id2label.items():
        key = str(name).strip().lower()
        idx = int(idx)
        if key in _POS:
            out["pos"] = idx
        elif key in _NEG:
            out["neg"] = idx
        elif key in _NEU:
            out["neu"] = idx
    missing = {"pos", "neu", "neg"} - set(out)
    if missing:
        raise ValueError(
            f"Cannot resolve sentiment labels {list(id2label.values())} -- missing "
            f"{sorted(missing)}. Refusing to guess an ordering; add the synonym to "
            f"ba2_providers.news.sentiment._POS/_NEG/_NEU if the model is legitimate.")
    return out


def _softmax(x: np.ndarray) -> np.ndarray:
    x = x - x.max(axis=1, keepdims=True)
    e = np.exp(x)
    return e / e.sum(axis=1, keepdims=True)


def score_texts(texts: Sequence[str], model: str = "finbert",
                batch_size: int = 32, max_length: int = 256,
                threads: Optional[int] = None,
                progress_every: int = 20000) -> pd.DataFrame:
    """Score ``texts`` with one model. Returns columns pos, neu, neg, score.

    Empty/blank texts are scored as fully neutral rather than dropped, so the returned
    frame aligns 1:1 with the input -- the caller joins it back positionally.
    """
    if model not in MODELS:
        raise ValueError(f"Unknown model {model!r}. Known: {available_models()}")
    hf_id = MODELS[model]["hf"]

    # Imported HERE, not at module scope -- see the module docstring.
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    if threads:
        torch.set_num_threads(int(threads))

    logger.info("Loading %s (%s)", model, hf_id)
    tok = AutoTokenizer.from_pretrained(hf_id)
    mdl = AutoModelForSequenceClassification.from_pretrained(hf_id)
    mdl.eval()
    lab = _resolve_label_map(mdl.config.id2label)

    def _run(batch: List[str]) -> np.ndarray:
        enc = tok(batch, truncation=True, max_length=max_length,
                  padding=True, return_tensors="pt")
        with torch.inference_mode():
            logits = mdl(**enc).logits.detach().cpu().numpy()
        return _softmax(logits)

    # Sign probe before any real work, so a bad mapping fails in one batch, not after
    # scoring 131k rows into a silently inverted column.
    probe = _run([_PROBE])[0]
    if probe[lab["neg"]] <= probe[lab["pos"]]:
        del mdl, tok
        gc.collect()
        raise ValueError(
            f"{model}: label mapping failed its sign probe "
            f"(pos={probe[lab['pos']]:.3f} neg={probe[lab['neg']]:.3f} on a clearly "
            f"negative sentence). Refusing to score with an inverted mapping.")
    logger.info("%s label map verified: %s", model, lab)

    n = len(texts)
    pos = np.zeros(n, dtype=np.float32)
    neu = np.zeros(n, dtype=np.float32)
    neg = np.zeros(n, dtype=np.float32)

    clean = [(i, t) for i, t in enumerate(texts) if isinstance(t, str) and t.strip()]
    # Sorting by length groups similar-length texts, so dynamic padding wastes far less
    # compute AND far less activation memory than a random ordering at the same batch size.
    clean.sort(key=lambda it: len(it[1]))

    # Blank/missing text scores as fully neutral, keeping the frame aligned 1:1 with the
    # input. Marked by difference against the scored indices rather than by scanning.
    scored_idx = np.zeros(n, dtype=bool)
    scored_idx[[i for i, _ in clean]] = True
    neu[~scored_idx] = 1.0
    blank = int((~scored_idx).sum())

    done = 0
    for start in range(0, len(clean), batch_size):
        chunk = clean[start:start + batch_size]
        probs = _run([t for _, t in chunk])
        for (idx, _), p in zip(chunk, probs):
            pos[idx] = p[lab["pos"]]
            neu[idx] = p[lab["neu"]]
            neg[idx] = p[lab["neg"]]
        done += len(chunk)
        if progress_every and done % progress_every < batch_size:
            logger.info("%s: scored %d/%d", model, done, len(clean))

    if blank:
        logger.info("%s: %d rows had no text, scored as neutral", model, blank)

    # One model resident at a time -- free before returning so a caller looping over
    # models never holds two sets of weights.
    del mdl, tok
    gc.collect()

    return pd.DataFrame({"pos": pos, "neu": neu, "neg": neg, "score": pos - neg})

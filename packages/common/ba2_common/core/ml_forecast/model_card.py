"""Model card: the identity and provenance of a forecasting model's output.

The card is part of the store key (``model_key``), so any change to a field yields a new store.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from datetime import date
from typing import Any, Dict, Iterable, Optional, Tuple

OUTPUT_TYPES = ("deciles", "paths")
#: No row format or mapping exists for these yet.
_DEFERRED_OUTPUT_TYPES = ("class_probs",)


def _tuple_of_str(name: str, v: Any, *, allow_empty: bool) -> Tuple[str, ...]:
    if isinstance(v, str) or not isinstance(v, (list, tuple)):
        raise TypeError(f"ModelCard.{name} must be a list/tuple of str, got {v!r}")
    t = tuple(v)
    if not allow_empty and not t:
        raise ValueError(f"ModelCard.{name} must not be empty")
    for x in t:
        if not isinstance(x, str) or not x.strip():
            raise ValueError(f"ModelCard.{name} holds a blank or non-str entry: {x!r}")
    if len(set(t)) != len(t):
        raise ValueError(f"ModelCard.{name} holds duplicates: {t!r}")
    return t


def _nonblank(name: str, v: Any) -> None:
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"ModelCard.{name} must be a non-blank str, got {v!r}")


@dataclass(frozen=True)
class ModelCard:
    model_id: str
    family: str
    version: str
    input_fields: Tuple[str, ...]
    timeframe: str
    context_length: int
    past_only_covariates: Tuple[str, ...]
    past_future_covariates: Tuple[str, ...]
    horizons: Tuple[int, ...]
    scaler: Optional[str]                 # reference to the scaler artifact; None = model normalises internally
    output_type: str                      # "deciles" | "paths"
    pretraining_cutoff: Optional[date]    # None = unknown (treat every date as possibly contaminated)
    licence: str
    licence_commercial_ok: bool

    def __post_init__(self):
        for n in ("model_id", "family", "version", "timeframe", "licence"):
            _nonblank(n, getattr(self, n))
        s = object.__setattr__
        s(self, "input_fields", _tuple_of_str("input_fields", self.input_fields, allow_empty=False))
        s(self, "past_only_covariates", _tuple_of_str("past_only_covariates", self.past_only_covariates, allow_empty=True))
        s(self, "past_future_covariates", _tuple_of_str("past_future_covariates", self.past_future_covariates, allow_empty=True))
        if isinstance(self.context_length, bool) or not isinstance(self.context_length, int) or self.context_length <= 0:
            raise ValueError(f"ModelCard.context_length must be a positive int, got {self.context_length!r}")
        if isinstance(self.horizons, str) or not isinstance(self.horizons, (list, tuple)):
            raise TypeError(f"ModelCard.horizons must be a list/tuple of int, got {self.horizons!r}")
        h = tuple(self.horizons)
        if not h:
            raise ValueError("ModelCard.horizons must not be empty")
        if any(isinstance(x, bool) or not isinstance(x, int) or x <= 0 for x in h):
            raise ValueError(f"ModelCard.horizons must be positive ints, got {h!r}")
        if any(b <= a for a, b in zip(h, h[1:])):
            raise ValueError(f"ModelCard.horizons must be strictly increasing, got {h!r}")
        s(self, "horizons", h)
        if self.scaler is not None:
            _nonblank("scaler", self.scaler)
        if self.output_type in _DEFERRED_OUTPUT_TYPES:
            raise ValueError(f"output_type {self.output_type!r} is not supported until the tsai adapter milestone")
        if self.output_type not in OUTPUT_TYPES:
            raise ValueError(f"ModelCard.output_type must be one of {OUTPUT_TYPES}, got {self.output_type!r}")
        pc = self.pretraining_cutoff
        if pc is not None and (not isinstance(pc, date) or hasattr(pc, "hour")):
            raise TypeError(f"ModelCard.pretraining_cutoff must be a date or None, got {pc!r}")
        if not isinstance(self.licence_commercial_ok, bool):
            raise TypeError(f"ModelCard.licence_commercial_ok must be bool, got {self.licence_commercial_ok!r}")

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, tuple):
                v = list(v)
            elif isinstance(v, date):
                v = v.isoformat()
            d[f.name] = v
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ModelCard":
        names = {f.name for f in fields(cls)}
        extra, missing = set(d) - names, names - set(d)
        if extra or missing:
            raise ValueError(f"ModelCard.from_dict: unknown keys {sorted(extra)}, missing keys {sorted(missing)}")
        kw = dict(d)
        if kw["pretraining_cutoff"] is not None:
            kw["pretraining_cutoff"] = date.fromisoformat(kw["pretraining_cutoff"])
        return cls(**kw)


def model_key(card: ModelCard, covariate_set: Iterable[str]) -> str:
    """Stable 16-hex key of (card, covariate set). The context length and the horizon list are part of the card
    (one source for each value), so they are in the key through it."""
    cov = sorted(covariate_set)
    for c in cov:
        if not isinstance(c, str) or not c.strip():
            raise ValueError(f"covariate_set holds a blank or non-str entry: {c!r}")
    payload = {
        "card": card.to_dict(),
        "context_length": card.context_length,
        "covariate_set": cov,
        "horizons": list(card.horizons),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

import dataclasses
from datetime import date

import pytest

from ba2_common.core.ml_forecast.model_card import ModelCard, model_key


def make_card(**over):
    base = dict(
        model_id="timesfm-3.0", family="timesfm", version="3.0",
        input_fields=("Close", "Volume"), timeframe="1d", context_length=512,
        past_only_covariates=("Volume",), past_future_covariates=(),
        horizons=(5, 10, 20), scaler=None, output_type="deciles",
        pretraining_cutoff=date(2023, 11, 30), licence="CC-BY-NC-4.0", licence_commercial_ok=False,
    )
    base.update(over)
    return ModelCard(**base)


def test_valid_card_is_frozen():
    c = make_card()
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.version = "x"


def test_lists_are_coerced_to_tuples():
    c = make_card(horizons=[5, 10], input_fields=["Close"])
    assert c.horizons == (5, 10) and c.input_fields == ("Close",)


@pytest.mark.parametrize("over", [
    dict(output_type="point"), dict(horizons=()), dict(horizons=(10, 5)), dict(horizons=(5, 5)),
    dict(horizons=(0, 5)), dict(horizons=(1.5,)), dict(context_length=0), dict(model_id=""),
    dict(family=" "), dict(input_fields=()), dict(timeframe=""), dict(licence=""),
    dict(licence_commercial_ok="no"), dict(pretraining_cutoff="2023-11-30"), dict(scaler=""),
    dict(past_only_covariates=("Volume", "Volume")),
])
def test_invalid_cards_raise(over):
    with pytest.raises((ValueError, TypeError)):
        make_card(**over)


def test_unknown_cutoff_allowed():
    assert make_card(pretraining_cutoff=None).pretraining_cutoff is None


def test_dict_round_trip():
    c = make_card(scaler="std-v1")
    assert ModelCard.from_dict(c.to_dict()) == c
    assert ModelCard.from_dict(make_card(pretraining_cutoff=None).to_dict()).pretraining_cutoff is None


def test_from_dict_rejects_unknown_and_missing_keys():
    d = make_card().to_dict()
    with pytest.raises(ValueError):
        ModelCard.from_dict({**d, "extra": 1})
    d.pop("licence")
    with pytest.raises(ValueError):
        ModelCard.from_dict(d)


def test_model_key_stable_and_sensitive():
    c = make_card()
    k = model_key(c, ("Volume", "SPY"))
    assert k == model_key(make_card(), ["SPY", "Volume"])  # covariate order irrelevant
    assert len(k) == 16 and all(ch in "0123456789abcdef" for ch in k)
    variants = [
        model_key(make_card(context_length=1024), ("Volume", "SPY")),     # context length comes from the card
        model_key(c, ("Volume",)),
        model_key(make_card(horizons=(5, 10)), ("Volume", "SPY")),        # horizons come from the card
        model_key(make_card(version="3.1"), ("Volume", "SPY")),
        model_key(make_card(licence="MIT"), ("Volume", "SPY")),
        model_key(make_card(pretraining_cutoff=None), ("Volume", "SPY")),
    ]
    assert len({k, *variants}) == 7


def test_every_card_field_changes_key():
    base = make_card()
    kb = model_key(base, ())
    changes = dict(
        model_id="b", family="b", version="9", input_fields=("Close",), timeframe="1h", context_length=256,
        past_only_covariates=(), past_future_covariates=("X",), horizons=(5,), scaler="s",
        output_type="paths", pretraining_cutoff=date(2024, 1, 1), licence="MIT", licence_commercial_ok=True,
    )
    assert set(changes) == {f.name for f in dataclasses.fields(ModelCard)}
    for name, val in changes.items():
        assert model_key(make_card(**{name: val}), ()) != kb, name


def test_model_key_rejects_blank_covariate():
    with pytest.raises(ValueError):
        model_key(make_card(), ("",))


def test_class_probs_not_supported_yet():
    with pytest.raises(ValueError, match="not supported until the tsai adapter milestone"):
        make_card(output_type="class_probs")

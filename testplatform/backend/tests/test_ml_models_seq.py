"""Sequence model adapters (app/services/ml_models_seq.py): TsaiAdapter and DartsAdapter on windows of past bars."""
import time

import numpy as np
import pandas as pd
import pytest
from sklearn.metrics import roc_auc_score

from app.services import ml_models as MM
from app.services import ml_models_seq as MS

torch = pytest.importorskip("torch")
pytestmark = pytest.mark.skipif(not torch.cuda.is_available(), reason="the sequence adapters are tested on the GPU")


@pytest.fixture(autouse=True)
def _deterministic_cudnn():
    prev = torch.backends.cudnn.deterministic, torch.backends.cudnn.benchmark
    torch.backends.cudnn.deterministic, torch.backends.cudnn.benchmark = True, False
    yield
    torch.backends.cudnn.deterministic, torch.backends.cudnn.benchmark = prev


# ------------------------------------------------------------------ synthetic data
L = 16


def make_features(n_sym=3, n=300, C=4, seed=0):
    """(n_sym * n, C) standard-normal features, series_start per row (each symbol is one contiguous block)."""
    rng = np.random.default_rng(seed)
    F = rng.normal(size=(n_sym * n, C)).astype(np.float32)
    start = np.repeat(np.arange(n_sym) * n, n)
    return F, start


def block_rows(n_sym, n, lo, hi):
    """rows lo..hi (exclusive) of every symbol block."""
    return np.concatenate([np.arange(s * n + lo, s * n + hi) for s in range(n_sym)])


def planted(kind="binary", n_sym=3, n=300, C=4, seed=0, lag=2, leak=False):
    """Train / val / test SequenceData whose label is a function of the PAST (feature 0 at lag ``lag`` and ``2*lag+1``)
    -- or of the FUTURE (``leak=True``: the next rows) as a control a window model must not be able to learn."""
    F, start = make_features(n_sym, n, C, seed)
    rng = np.random.default_rng(seed + 1)
    out = {}
    sig = np.zeros(len(F))
    for s in range(n_sym):
        f0 = F[s * n:(s + 1) * n, 0]
        a = np.zeros(n)
        for r in range(n):
            if leak:
                a[r] = f0[min(r + 1, n - 1)] + f0[min(r + 3, n - 1)]
            else:
                a[r] = f0[max(r - lag, 0)] + f0[max(r - 2 * lag - 1, 0)]
        sig[s * n:(s + 1) * n] = a
    if kind == "binary":
        y = (sig > 0).astype(np.int64)
    elif kind == "multiclass":
        y = np.digitize(sig, np.quantile(sig, [1 / 3, 2 / 3])).astype(np.int64)
    else:
        y = (0.5 * sig + 0.1 * rng.normal(size=len(sig))).astype(np.float64)
    lo = L - 1
    cut1, cut2 = lo + int((n - lo) * 0.7), lo + int((n - lo) * 0.85)
    for name, (a, b) in {"train": (lo, cut1), "val": (cut1, cut2), "test": (cut2, n - 4)}.items():
        rows = block_rows(n_sym, n, a, b)
        out[name] = (MS.SequenceData(F, rows, L, start), y[rows])
    return out


def tiny(kind, n=120):
    return planted(kind, n_sym=2, n=n)


FAST = {"epochs": 1, "width": 16, "batch_size": 64, "rocket_features": 500}


def fit_tsai(d, task, seed=1, **params):
    ad = MM.make_adapter("tsai", params={**FAST, **params})
    ad.fit(*d["train"], *d["val"], task=task, seed=seed)
    return ad


# ------------------------------------------------------------------ SequenceData contract
def test_window_of_row_r_is_rows_r_minus_l_plus_1_to_r_of_the_same_series():
    F, start = make_features(2, 60, 3)
    sd = MS.SequenceData(F, np.array([20, 59, 75]), 8, start)
    w = sd.windows()
    assert w.shape == (3, 3, 8) and w.dtype == np.float32
    for i, r in enumerate(sd.rows):
        np.testing.assert_array_equal(w[i], F[r - 7:r + 1].T)


def test_a_window_crossing_into_the_previous_series_or_before_its_start_is_refused():
    F, start = make_features(2, 60, 3)
    with pytest.raises(ValueError, match="window"):
        MS.SequenceData(F, np.array([62]), 8, start)           # symbol 2 row 2: window reaches symbol 1
    with pytest.raises(ValueError, match="window"):
        MS.SequenceData(F, np.array([5]), 8, start)            # before the start of the first series
    start2 = start.copy()
    start2[:60] = 10                                           # series start = first usable row (warm-up before it)
    with pytest.raises(ValueError, match="window"):
        MS.SequenceData(F, np.array([12]), 8, start2)
    MS.SequenceData(F, np.array([17]), 8, start2)


def test_sequence_data_validates_its_arrays():
    F, start = make_features(1, 40, 3)
    with pytest.raises(ValueError, match="2-D"):
        MS.SequenceData(F[:, 0], np.array([20]), 8, start)
    bad = F.copy()
    bad[3, 1] = np.nan
    with pytest.raises(ValueError, match="finite"):
        MS.SequenceData(bad, np.array([20]), 8, start)
    with pytest.raises(ValueError, match="rows"):
        MS.SequenceData(F, np.array([40]), 8, start)           # past the end
    with pytest.raises(ValueError, match="rows"):
        MS.SequenceData(F, np.array([[20]]), 8, start)
    with pytest.raises(ValueError, match="lookback"):
        MS.SequenceData(F, np.array([20]), 0, start)
    with pytest.raises(ValueError, match="series_start"):
        MS.SequenceData(F, np.array([20]), 8, start[:-1])
    with pytest.raises(ValueError, match="returns"):
        MS.SequenceData(F, np.array([20]), 8, start, returns=np.zeros(5))
    assert MS.SequenceData(F.astype(np.float64), np.array([20]), 8, start).features.dtype == np.float32


def test_take_and_usable_rows():
    F, start = make_features(2, 60, 3)
    sd = MS.SequenceData(F, np.array([], dtype=np.int64), 8, start)
    cand = np.array([3, 7, 8, 59, 60, 66, 67, 100])
    ok = sd.usable_rows(cand)
    assert ok.tolist() == [7, 8, 59, 67, 100]
    t = sd.take(ok)
    assert t.rows.tolist() == ok.tolist() and t.features is sd.features


def _panel(n_sym=3, n=250, C=3, seed=0):
    rng = np.random.default_rng(seed)
    syms = np.repeat([f"S{i}" for i in range(n_sym)], n)
    df = pd.DataFrame({"symbol": syms})
    for j in range(C):
        df[f"f{j}"] = rng.normal(size=len(df)) * 10 + 5
    # a warm-up: the first 20 rows of every symbol have NaN in f1 (as an indicator before its window is full)
    for s in range(n_sym):
        df.loc[s * n:s * n + 19, "f1"] = np.nan
    df["close"] = np.concatenate([100 * np.exp(np.cumsum(rng.normal(0, 0.01, n))) for _ in range(n_sym)])
    return df


def test_from_panel_scales_on_the_fit_rows_only_and_drops_warm_up_windows():
    panel = _panel()
    feats = ["f0", "f1", "f2"]
    fit_rows = np.arange(30, 150)
    sd = MS.sequence_data_from_panel(panel, feats, fit_rows, lookback=10, rows=np.arange(0, 750))
    med = np.nanmedian(panel[feats].to_numpy()[fit_rows], 0)
    iqr = np.nanpercentile(panel[feats].to_numpy()[fit_rows], 75, 0) - np.nanpercentile(panel[feats].to_numpy()[fit_rows], 25, 0)
    np.testing.assert_allclose(sd.features[100], np.clip((panel[feats].to_numpy()[100] - med) / iqr, -5, 5), rtol=1e-4)
    # symbol 0: features finite from row 20, so its first usable window ends at row 29; symbol 1 from 250 + 29
    assert sd.rows.min() == 29 and 249 in sd.rows and 250 not in sd.rows and 278 not in sd.rows and 279 in sd.rows
    assert sd.channels == ("f0", "f1", "f2") and sd.returns is None
    sd2 = MS.sequence_data_from_panel(panel, feats, fit_rows, lookback=10, rows=np.arange(300, 400), with_returns=True)
    assert sd2.returns is not None and sd2.returns[250] == 0.0                 # first row of a symbol
    np.testing.assert_allclose(sd2.returns[251], np.log(panel["close"].iloc[251] / panel["close"].iloc[250]))


def test_from_panel_pooled_adds_a_symbol_one_hot():
    panel = _panel(n_sym=2, n=120)
    sd = MS.sequence_data_from_panel(panel, ["f0", "f2"], np.arange(0, 100), lookback=8, pooled=True,
                                     rows=np.arange(120, 240))
    assert sd.channels == ("f0", "f2", "sym_S0", "sym_S1") and sd.features[130, 2:].tolist() == [0.0, 1.0]


def test_from_panel_needs_a_positional_index():
    panel = _panel(n_sym=1, n=60).iloc[10:]
    with pytest.raises(ValueError, match="Index"):
        MS.sequence_data_from_panel(panel, ["f0"], np.arange(5, 20), lookback=4, rows=np.arange(12, 30))


# ------------------------------------------------------------------ TsaiAdapter
ARCHS = MS.TSAI_ARCHS
TASKS = MM.SUPPORTED_TASKS["tsai"]


def test_the_seven_architectures_are_registered():
    assert set(ARCHS) == {"inception", "tst", "lstm_fcn", "minirocket", "patchtst", "tcn", "resnet"}
    assert MM.SUPPORTED_TASKS["tsai"] == ("binary", "multiclass", "regression")
    assert isinstance(MM.make_adapter("tsai"), MS.TsaiAdapter)


@pytest.mark.parametrize("arch", ARCHS)
@pytest.mark.parametrize("task", TASKS)
def test_every_architecture_trains_one_epoch_for_every_task(arch, task):
    d = tiny(task)
    ad = fit_tsai(d, task, arch=arch)
    Xte, _ = d["test"]
    P = ad.predict(Xte)
    n = len(Xte)
    if task == "regression":
        assert P.shape == (n,) and np.isfinite(P).all()
    else:
        k = 2 if task == "binary" else 3
        assert P.shape == (n, k) and (P >= 0).all()
        np.testing.assert_allclose(P.sum(1), 1.0, atol=1e-5)
    s = ad.score(Xte)
    assert s.shape == (n,) and np.isfinite(s).all()
    assert ad.fit_info["epochs_run"] == 1 and ad.n_features == 4 and ad.n_classes == (None if task == "regression" else (2 if task == "binary" else 3))


def test_score_follows_the_interface_for_each_task():
    for task, kind in (("binary", "probability"), ("multiclass", "probability_spread"), ("regression", "point_forecast")):
        d = tiny(task)
        ad = fit_tsai(d, task)
        X = d["test"][0]
        P = ad.predict(X)
        want = P[:, 1] if task == "binary" else (P[:, -1] - P[:, 0] if task == "multiclass" else P)
        np.testing.assert_allclose(ad.score(X), want)
        assert ad.score_kind == kind


def test_inception_learns_a_planted_signal_in_a_few_epochs():
    t0 = time.perf_counter()
    d = planted("binary", n_sym=3, n=500)
    ad = MM.make_adapter("tsai", params={"arch": "inception", "epochs": 12, "width": 32, "lr": 3e-3, "batch_size": 128})
    ad.fit(*d["train"], *d["val"], task="binary", seed=3)
    Xte, yte = d["test"]
    auc = roc_auc_score(yte, ad.score(Xte))
    assert auc > 0.85, auc
    assert time.perf_counter() - t0 < 60


def test_regression_and_multiclass_learn_the_planted_signal():
    d = planted("regression", n_sym=3, n=500)
    ad = MM.make_adapter("tsai", params={"arch": "inception", "epochs": 15, "width": 32, "lr": 3e-3, "batch_size": 128})
    ad.fit(*d["train"], *d["val"], task="regression", seed=3)
    Xte, yte = d["test"]
    assert np.corrcoef(ad.score(Xte), yte)[0, 1] > 0.8
    assert abs(ad.predict(Xte).mean() - yte.mean()) < 0.3                  # predictions are in the label's units
    d = planted("multiclass", n_sym=3, n=500)
    ad = MM.make_adapter("tsai", params={"arch": "inception", "epochs": 15, "width": 32, "lr": 3e-3, "batch_size": 128})
    ad.fit(*d["train"], *d["val"], task="multiclass", seed=3)
    Xte, yte = d["test"]
    assert (ad.predict(Xte).argmax(1) == yte).mean() > 0.6                 # chance is 1/3


def test_a_signal_in_the_future_cannot_be_learnt_the_window_is_causal():
    d = planted("binary", n_sym=3, n=500, leak=True)
    ad = MM.make_adapter("tsai", params={"arch": "inception", "epochs": 8, "width": 32, "lr": 3e-3, "batch_size": 128})
    ad.fit(*d["train"], *d["val"], task="binary", seed=3)
    Xte, yte = d["test"]
    assert roc_auc_score(yte, ad.score(Xte)) < 0.6


@pytest.mark.parametrize("arch", ["inception", "tst", "minirocket"])
def test_same_seed_is_identical_and_another_seed_differs(arch):
    d = tiny("binary")
    X = d["test"][0]
    a = fit_tsai(d, "binary", seed=5, arch=arch, epochs=2).predict(X)
    b = fit_tsai(d, "binary", seed=5, arch=arch, epochs=2).predict(X)
    c = fit_tsai(d, "binary", seed=6, arch=arch, epochs=2).predict(X)
    np.testing.assert_allclose(a, b, atol=1e-6)
    assert np.abs(a - c).max() > 1e-4


def test_the_global_random_state_is_left_alone():
    d = tiny("binary")
    torch.manual_seed(123)
    np.random.seed(123)
    ref_t, ref_n = torch.rand(1).item(), np.random.rand()
    torch.manual_seed(123)
    np.random.seed(123)
    fit_tsai(d, "binary", arch="minirocket")
    assert torch.rand(1).item() == ref_t and np.random.rand() == ref_n


def test_the_validation_loss_of_the_returned_model_is_the_best_one_and_patience_stops_a_noise_fit():
    d = tiny("binary", n=200)
    Xtr, ytr = d["train"]
    rng = np.random.default_rng(0)
    noise = (rng.random(len(ytr)) > 0.5).astype(np.int64)
    Xv, yv = d["val"]
    ad = MM.make_adapter("tsai", params={**FAST, "epochs": 30, "lr": 3e-3, "patience": 2})
    ad.fit(Xtr, noise, Xv, (rng.random(len(yv)) > 0.5).astype(np.int64), task="binary", seed=1)
    info = ad.fit_info
    assert info["epochs_run"] < 30 and info["epochs_run"] == info["best_epoch"] + 2
    assert info["val_loss"] == pytest.approx(min(info["val_curve"]))
    # predict() really runs the restored (best) weights: its validation log-loss is the recorded best
    P = ad.predict(Xv)
    # (the labels used above were random draws: redraw the same ones)
    rng2 = np.random.default_rng(0)
    rng2.random(len(ytr))
    yv2 = (rng2.random(len(yv)) > 0.5).astype(np.int64)
    ll = -np.mean(np.log(np.clip(P[np.arange(len(yv2)), yv2], 1e-12, 1)))
    assert ll == pytest.approx(info["val_loss"], abs=2e-3)


def test_class_weights_balanced_weights_the_rare_class_and_is_refused_for_regression():
    d = tiny("binary")
    Xtr, ytr = d["train"]
    skew = (np.random.default_rng(0).random(len(ytr)) > 0.85).astype(np.int64)        # ~15% ones
    Xv, yv = d["val"]
    slow = {**FAST, "epochs": 25, "lr": 1e-2, "patience": 25}                 # long enough to learn the base rate
    plain = MM.make_adapter("tsai", params=slow)
    plain.fit(Xtr, skew, Xv, yv, task="binary", seed=2)
    bal = MM.make_adapter("tsai", params={**slow, "class_weights": "balanced"})
    bal.fit(Xtr, skew, Xv, yv, task="binary", seed=2)
    assert bal.score(Xv).mean() > plain.score(Xv).mean() + 0.1                         # leans towards the rare class
    r = tiny("regression")
    with pytest.raises(ValueError, match="class_weights"):
        MM.make_adapter("tsai", params={**FAST, "class_weights": "balanced"}).fit(*r["train"], *r["val"], task="regression", seed=1)


@pytest.mark.parametrize("task", ["rank", "quantile"])
def test_unsupported_tasks_are_refused(task):
    d = tiny("binary")
    with pytest.raises(MM.UnsupportedTaskError):
        MM.make_adapter("tsai", params=FAST).fit(*d["train"], *d["val"], task=task, seed=1, groups_train=[1], groups_val=[1])
    with pytest.raises(MM.UnsupportedTaskError):
        MM.make_adapter("tsai", params=FAST).fit(*d["train"], *d["val"], task="nonsense", seed=1)


def test_params_are_validated():
    for bad in ({"arch": "gru"}, {"nope": 1}, {"epochs": 0}, {"epochs": 2.5}, {"lr": -1.0}, {"dropout": 1.0},
                {"class_weights": "auto"}, {"device": "tpu"}, {"batch_size": True}, {"width": "big"}):
        with pytest.raises(ValueError):
            MM.make_adapter("tsai", params=bad)
    MM.make_adapter("tsai", params={"arch": "tcn", "dropout": 0.0, "device": "cpu"})
    d = tiny("binary")
    with pytest.raises(ValueError, match="width"):
        MM.make_adapter("tsai", params={**FAST, "arch": "tst", "width": 18}).fit(*d["train"], *d["val"], task="binary", seed=1)
    with pytest.raises(ValueError, match="batch_size"):
        MM.make_adapter("tsai", params={**FAST, "batch_size": 1}).fit(*d["train"], *d["val"], task="binary", seed=1)


def test_inputs_are_checked_at_fit_and_predict():
    d = tiny("binary")
    (Xt, yt), (Xv, yv) = d["train"], d["val"]
    ad = MM.make_adapter("tsai", params=FAST)
    with pytest.raises(TypeError, match="SequenceData"):
        ad.fit(Xt.features, yt, Xv, yv, task="binary", seed=1)
    with pytest.raises(ValueError, match="labels"):
        ad.fit(Xt, yt[:-1], Xv, yv, task="binary", seed=1)
    with pytest.raises(ValueError, match="empty"):
        ad.fit(Xt, yt, Xv.take(Xv.rows[:0]), yv[:0], task="binary", seed=1)
    with pytest.raises(ValueError, match="lookback"):
        ad.fit(Xt, yt, MS.SequenceData(Xv.features, Xv.rows, L + 2, Xv.series_start), yv, task="binary", seed=1)
    with pytest.raises(ValueError, match="channels"):
        ad.fit(Xt, yt, MS.SequenceData(Xv.features[:, :3], Xv.rows, L, Xv.series_start), yv, task="binary", seed=1)
    with pytest.raises(ValueError, match="both classes"):
        ad.fit(Xt, np.zeros_like(yt), Xv, yv, task="binary", seed=1)
    with pytest.raises(ValueError, match="NaN"):
        ad.fit(Xt, np.where(np.arange(len(yt)) == 0, np.nan, yt), Xv, yv, task="regression", seed=1)
    with pytest.raises(RuntimeError, match="before fit"):
        ad.predict(Xv)
    ad.fit(Xt, yt, Xv, yv, task="binary", seed=1)
    with pytest.raises(ValueError, match="lookback"):
        ad.predict(MS.SequenceData(Xv.features, Xv.rows, L + 2, Xv.series_start))
    with pytest.raises(ValueError, match="channels"):
        ad.predict(MS.SequenceData(Xv.features[:, :3], Xv.rows, L, Xv.series_start))
    with pytest.raises(TypeError, match="SequenceData"):
        ad.predict(Xv.features)
    assert ad.predict(Xv.take(Xv.rows[:0])).shape == (0, 2)


def test_a_failed_fit_leaves_the_adapter_unfitted_and_a_refit_resets_state(monkeypatch):
    d = tiny("binary")
    (Xt, yt), (Xv, yv) = d["train"], d["val"]
    ad = MM.make_adapter("tsai", params=FAST).fit(Xt, yt, Xv, yv, task="binary", seed=1)
    assert ad.task == "binary"
    with pytest.raises(ValueError):
        ad.fit(Xt, yt, Xv, yv, task="rank", seed=2)
    assert ad.task is None and ad.seed is None and not hasattr(ad, "_net")
    with pytest.raises(RuntimeError):
        ad.predict(Xv)
    ad.fit(Xt, yt, Xv, yv, task="binary", seed=1)
    monkeypatch.setattr(MS.TsaiAdapter, "_train", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        ad.fit(Xt, yt, Xv, yv, task="binary", seed=3)
    assert ad.task is None and not hasattr(ad, "_net")


def test_fit_and_predict_on_cpu():
    d = tiny("multiclass")
    ad = fit_tsai(d, "multiclass", device="cpu", arch="tcn")
    P = ad.predict(d["test"][0])
    np.testing.assert_allclose(P.sum(1), 1.0, atol=1e-5)
    assert ad.fit_info["device"] == "cpu"


def test_minirocket_biases_are_fitted_on_training_windows_and_only_the_head_is_trained():
    d = tiny("binary")
    ad = fit_tsai(d, "binary", arch="minirocket", epochs=2)
    net = ad._net
    assert bool(net.backbone.prefit.item())                                    # fitted explicitly, before training
    assert not net.backbone.kernels.requires_grad
    trainable = {n for n, p in net.named_parameters() if p.requires_grad}
    assert trainable and all(n.startswith("head.") for n in trainable)
    assert ad.fit_info["trainable_parameters"] == sum(p.numel() for p in net.parameters() if p.requires_grad)
    # the head moved off its all-zero initialisation
    assert float(net.head[-1].weight.detach().abs().sum()) > 0


def test_arch_params_cover_every_architecture_and_ml_search_delegates_to_them():
    from app.services import ml_search as S
    for arch in ARCHS:
        assert isinstance(MS.arch_params(arch, 32, 0.1, 64), dict)
    assert MS.arch_params("patchtst", 64, 0.1, 64)["n_heads"] == 4 and MS.arch_params("patchtst", 64, 0.1, 64)["d_model"] == 64
    assert MS.arch_params("tcn", 48, 0.1, 64)["layers"] == [48, 48, 48]
    with pytest.raises(ValueError, match="unknown"):
        MS.arch_params("gru", 32, 0.1, 64)
    g = {"timeframe": "1h", "features": "t", "target": "tb_h3_r1", "pooled": 1, "tsai_arch": "tst", "darts_arch": "nhits",
         "lookback": 64, "lr": 1e-3, "width": 64, "dropout": 0.1, "epochs": 10}
    for arch in ("inception", "tst", "lstm_fcn", "minirocket", "patchtst", "tcn", "resnet"):
        s = S.Setup.from_genes({**g, "tsai_arch": arch})
        assert S._tsai_params(arch, s) == MS.arch_params(arch, s.width, s.dropout, s.lookback)

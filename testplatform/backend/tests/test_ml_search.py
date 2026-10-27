"""Model search scoring and store export (app/services/ml_search.py), on a small synthetic panel."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.services import ml_panel as P
from app.services import ml_search as S
from tests.test_ml_panel import FIRST, LAST, cache  # noqa: F401  (the synthetic cache fixture)


@pytest.fixture
def panel_paths(cache, tmp_path):  # noqa: F811
    panel, _ = P.build_panel(P.PanelSpec("1h", FIRST, LAST, ("AAA", "BBB")), cache)
    p = tmp_path / "p1h.parquet"
    panel.to_parquet(p, index=False)
    S._PANELS.clear()
    S._EXCURSIONS.clear()
    S.set_panel_paths({"1h": str(p)})
    yield panel
    S._PANELS.clear()


def _setup(target="tb_h3_r1"):
    return S.Setup("1h", "tdm", target, True, "inception", 32, 1e-3, 32, 0.1, 10)


def test_genes_decode_and_darts_setups_run_on_h1():
    g = {"timeframe": "15m", "features": "tdm", "target": "fwd_h3", "pooled": 0, "tsai_arch": "tst",
         "darts_arch": "nhits", "lookback": 64, "lr": 1e-3, "width": 64, "dropout": 0.1, "epochs": 10}
    s = S.Setup.from_genes(g)
    assert s.timeframe == "1h" and s.arch == "nhits" and s.pooled and s.epochs == 10
    assert not S.Setup.from_genes({**g, "target": "dir_h3", "epochs": 30}).pooled
    assert S.Setup.from_genes({**g, "target": "dir_h3"}).timeframe == "15m"
    assert all({"min", "max", "step"} <= set(v) for v in S.GENE_SPACE.values())


def test_eod_outcomes_trade_next_open_to_close_h_later(panel_paths):
    ev = S.eod_outcomes(S.get_panel("1h"), 3).set_index(["symbol", "session"])
    p = S.get_panel("1h")
    a = p[p["symbol"] == "AAA"]
    sess = sorted(a["session"].unique())
    s0 = sess[100]
    opn = a[(a["session"] == sess[101]) & (a["bar"] == 0)]["open"].iloc[0]
    cls = a[a["session"] == sess[103]]["close"].iloc[-1]
    assert ev.loc[("AAA", s0), "ret"] == pytest.approx(cls / opn - 1)
    # excess is demeaned within the session
    assert ev.groupby(level="session")["excess"].sum().abs().max() < 1e-9


def test_scoring_rewards_a_perfect_ranker_and_beats_naive(panel_paths):
    s = _setup("dir_h3")
    ev = S.eod_outcomes(S.get_panel("1h"), 3).dropna(subset=["excess"])
    ev = ev[ev["session"] >= date(2023, 9, 1)]
    good = ev.assign(fold="f1", label=np.nan, p_up=(ev["excess"].rank(pct=True) * 0.8 + 0.1).to_numpy())
    good["p_down"] = 1 - good["p_up"] - 0.1
    good["p_mid"] = 0.1
    bad = good.assign(p_up=good["p_down"].to_numpy(), p_down=good["p_up"].to_numpy())
    cols = ["symbol", "session", "fold", "label", "p_down", "p_mid", "p_up"]
    sg, sb = S.score_predictions(good[cols], s), S.score_predictions(bad[cols], s)
    assert sg["excess"] > 0 > sb["excess"] and sg["fitness"] > sb["fitness"]


def test_store_rows_for_classes_and_deciles(panel_paths, tmp_path):
    from ba2_common.core.ml_forecast import store
    s = _setup()
    preds = pd.DataFrame({"symbol": ["AAA"], "session": [date(2024, 1, 5)], "p_down": [0.2], "p_mid": [0.3],
                          "p_up": [0.5], "r_down": [-0.03], "r_mid": [0.001], "r_up": [0.03]})
    key = S.write_setup_store(s, preds, {"features": ["t_ret_1"], "folds": {}}, "custom-x", cache_root=tmp_path)
    store.reset_cache()
    r = store.read_row(key, "AAA", date(2024, 1, 8), 3, cache_root=tmp_path)
    assert r.has_classes and r.class_probs[3][2][0] == pytest.approx(0.5, abs=1e-6)
    sd = _setup("fwd_h3")
    q = {f"q{l}": [0.001 * (l - 50)] for l in S.DECILES}
    k2 = S.write_setup_store(sd, pd.DataFrame({"symbol": ["AAA"], "session": [date(2024, 1, 5)], **q}),
                             {"features": ["t_ret_1"], "folds": {}}, "custom-y", cache_root=tmp_path)
    assert k2 != key and store.read_row(k2, "AAA", date(2024, 1, 5), 3, cache_root=tmp_path).deciles[3][4] == 0.0

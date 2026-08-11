"""The news store's absence semantics must not drift.

The macro section ran inert for months because "data structurally unreachable" and
"no data this window" both resolved to None and nothing complained. The news store
separates them deliberately, and these tests pin that separation: a missing store or an
uncovered symbol RAISES, a covered symbol with a quiet week returns EMPTY.

Also pinned: raw text lives outside CACHE_FOLDER, so it never enters the sync manifest.
"""
import os
from datetime import datetime

import pandas as pd
import pytest

import ba2_common.config as cfg

from ba2_providers.news import store as st


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    """Redirect the store at a tmp tree laid out EXACTLY like production.

    COMMON_DIR/cache is the real relationship (config.py: CACHE_FOLDER defaults to
    COMMON_DIR/"cache"), and reproducing it here is what gives the two layout tests
    below their teeth -- raw has to sit outside the cache root while still being inside
    the common root, which a pair of unrelated tmp dirs would satisfy trivially.
    """
    common = tmp_path / "common"
    monkeypatch.setattr(cfg, "COMMON_DIR", str(common))
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(common / "cache"))
    st.reset_cache()
    yield
    st.reset_cache()


def _scored(dates, pos=None, neg=None, model="finbert-legacy"):
    n = len(dates)
    pos = pos if pos is not None else [0.8] * n
    neg = neg if neg is not None else [0.0] * n
    return pd.DataFrame({
        "url_hash": [f"h{i}" for i in range(n)],
        "published_at": pd.to_datetime(dates),
        "provider": ["fmp"] * n,
        "model": [model] * n,
        "score": pos,
        "pos": pos,
        "neu": [0.0] * n,
        "neg": neg,
    })


# --- structural absence: must be loud -------------------------------------------------

def test_missing_store_raises_rather_than_returning_empty():
    with pytest.raises(st.NewsStoreError, match="empty or missing"):
        st.read_sentiment("NVDA", datetime(2024, 6, 1))


def test_uncovered_symbol_raises_even_when_the_store_exists():
    st.write_scored("NVDA", _scored(["2024-05-30"]))
    with pytest.raises(st.NewsStoreError, match="not in the news store"):
        st.read_sentiment("TSLA", datetime(2024, 6, 1))


# --- ordinary absence: must be quiet --------------------------------------------------

def test_covered_symbol_with_a_quiet_window_returns_empty_not_an_error():
    st.write_scored("NVDA", _scored(["2024-01-02", "2024-01-03"]))
    out = st.read_sentiment("NVDA", datetime(2024, 6, 1), window_days=7)
    assert out.empty, "a quiet week must not be an error"


# --- no lookahead ---------------------------------------------------------------------

def test_articles_published_after_as_of_are_invisible():
    st.write_scored("NVDA", _scored(["2024-05-30", "2024-06-02"]))
    out = st.read_sentiment("NVDA", datetime(2024, 6, 1))
    assert len(out) == 1
    assert str(out["published_at"].iloc[0].date()) == "2024-05-30"


def test_window_excludes_articles_older_than_the_lookback():
    st.write_scored("NVDA", _scored(["2024-05-01", "2024-05-28", "2024-05-30"]))
    out = st.read_sentiment("NVDA", datetime(2024, 6, 1), window_days=7)
    assert len(out) == 2


def test_as_of_none_returns_everything():
    st.write_scored("NVDA", _scored(["2024-05-30", "2030-01-01"]))
    assert len(st.read_sentiment("NVDA")) == 2


# --- the two tiers stay physically apart ----------------------------------------------

def test_raw_is_not_under_the_cache_folder():
    """If raw text lands under CACHE_FOLDER it syncs to every worker -- the one thing
    the two-tier split exists to prevent."""
    root = os.path.abspath(cfg.CACHE_FOLDER) + os.sep
    assert not (os.path.abspath(st.raw_folder()) + os.sep).startswith(root)


def test_scored_is_under_the_cache_folder():
    """Conversely, scores MUST sync -- that is how remote workers read them."""
    root = os.path.abspath(cfg.CACHE_FOLDER) + os.sep
    assert (os.path.abspath(st.scored_folder()) + os.sep).startswith(root)


def test_folders_track_a_config_rebind():
    """The roots must be read per call. A value snapshotted at import would ignore the
    CACHE_FOLDER override and write into the real cache tree from inside a test."""
    assert st.scored_path("NVDA").startswith(cfg.CACHE_FOLDER)
    assert st.raw_path("NVDA").startswith(cfg.COMMON_DIR)


def test_read_raw_is_explicit_about_master_only_when_absent():
    with pytest.raises(FileNotFoundError, match="master-only"):
        st.read_raw("NVDA")


def test_raw_roundtrips_text():
    df = pd.DataFrame({
        "url_hash": ["a"], "published_at": pd.to_datetime(["2024-05-30"]),
        "provider": ["fmp"], "source": ["fool.com"], "title": ["T"],
        "summary": ["S"], "text": ["body"],
    })
    st.write_raw("NVDA", df)
    assert st.read_raw("NVDA")["text"].iloc[0] == "body"


# --- write-time validation ------------------------------------------------------------

def test_write_rejects_unparseable_published_at():
    df = _scored(["2024-05-30"])
    df["published_at"] = ["not a date"]
    with pytest.raises(ValueError, match="published_at"):
        st.write_scored("NVDA", df)


def test_write_rejects_missing_columns():
    with pytest.raises(ValueError, match="missing columns"):
        st.write_scored("NVDA", _scored(["2024-05-30"]).drop(columns=["neg"]))


def test_symbol_that_could_escape_the_store_directory_is_rejected():
    for bad in ("../etc", "NV/DA", "", "a" * 30):
        with pytest.raises(ValueError, match="Refusing"):
            st.scored_path(bad)


# --- aggregation ----------------------------------------------------------------------

def test_aggregate_weights_recent_news_more_heavily():
    """Old bad news and new good news must not cancel; the recent one wins."""
    df = _scored(["2024-05-01", "2024-05-31"], pos=[0.0, 0.9], neg=[0.9, 0.0])
    out = st.aggregate_sentiment(df, datetime(2024, 6, 1), half_life_days=3.0)
    assert out > 0.8, f"recency weighting is not applied ({out})"


def test_aggregate_ignores_neutral_mass():
    """A neutral article is an absence of signal, not a negative one."""
    both = _scored(["2024-05-31", "2024-05-31"], pos=[0.9, 0.0], neg=[0.0, 0.0])
    both.loc[1, "neu"] = 0.95
    one = _scored(["2024-05-31"], pos=[0.9], neg=[0.0])
    a = st.aggregate_sentiment(both, datetime(2024, 6, 1))
    b = st.aggregate_sentiment(one, datetime(2024, 6, 1))
    assert a == pytest.approx(b / 2), "neutral rows should dilute by count, not by sign"


def test_aggregate_of_nothing_is_none_not_zero():
    """Zero is a real sentiment reading; absence is not."""
    assert st.aggregate_sentiment(_scored([]).iloc[0:0], datetime(2024, 6, 1)) is None


def test_aggregate_is_bounded():
    df = _scored(["2024-05-31"] * 5, pos=[1.0] * 5, neg=[0.0] * 5)
    assert st.aggregate_sentiment(df, datetime(2024, 6, 1)) == pytest.approx(1.0)


# --- model column ---------------------------------------------------------------------

def test_models_coexist_and_can_be_selected():
    """A re-score appends under a new model name; a mixed file must be filterable."""
    a = _scored(["2024-05-30"], model="finbert-legacy")
    b = _scored(["2024-05-30"], model="candidate-v2")
    st.write_scored("NVDA", pd.concat([a, b], ignore_index=True))
    assert len(st.read_sentiment("NVDA", model="candidate-v2")) == 1
    assert len(st.read_sentiment("NVDA")) == 2


def test_coverage_report_lists_symbols_and_models():
    st.write_scored("NVDA", _scored(["2024-05-30", "2024-05-31"]))
    rep = st.coverage_report()
    assert rep.loc[0, "symbol"] == "NVDA"
    assert rep.loc[0, "articles"] == 2
    assert rep.loc[0, "models"] == "finbert-legacy"

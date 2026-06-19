"""Phase 5 Task 6 tests: FEATURES_SOURCE seam for sentiment / fundamentals / macro.

These verify the SHAPE of the secondary re-source seam (parallel to the OHLCV
seam from Task 5) WITHOUT network:

  * default is ``legacy`` -> each service uses its existing dataproviders client
    (nothing changes for existing training);
  * ``FEATURES_SOURCE=ba2_providers`` routes the provider selection through
    ``ba2_providers.get_provider(<category>, <name>)`` (news / fundamentals_details
    / macro), with ba2_providers faked so no package/network/API-key is needed;
  * any ba2_providers failure FALLS BACK to the legacy path, so a dataset build is
    never broken by the flag.

Per the plan, byte-equality / per-block equivalence of the resulting feature
columns (news_/bs_/is_/cf_/earn_/macro_) is DEFERRED to Task 8 — the default stays
``legacy`` and is NOT flipped here.
"""
from __future__ import annotations

import pytest


# --------------------------------------------------------------------------- #
# features_source helper                                                       #
# --------------------------------------------------------------------------- #

def test_features_source_defaults_to_legacy(monkeypatch):
    monkeypatch.delenv("FEATURES_SOURCE", raising=False)
    from app.services import features_source
    assert features_source.features_source() == "legacy"
    assert features_source.use_ba2_providers() is False


def test_features_source_selects_ba2_providers(monkeypatch):
    monkeypatch.setenv("FEATURES_SOURCE", "BA2_Providers")  # case/space tolerant
    from app.services import features_source
    assert features_source.features_source() == "ba2_providers"
    assert features_source.use_ba2_providers() is True


def test_get_ba2_provider_returns_none_on_failure(monkeypatch):
    """If ba2_providers.get_provider raises, the helper returns None (caller falls
    back to legacy) rather than propagating."""
    from app.services import features_source

    import ba2_providers

    def _boom(category, name):
        raise RuntimeError("no API key")

    monkeypatch.setattr(ba2_providers, "get_provider", _boom)
    assert features_source.get_ba2_provider("news", "fmp") is None


# --------------------------------------------------------------------------- #
# sentiment: _get_news_provider                                               #
# --------------------------------------------------------------------------- #

def test_sentiment_default_legacy_news_provider(monkeypatch):
    """Default (no flag) returns the legacy dataproviders news client.

    Uses 'alphavantage' because its legacy constructor does not require an app
    setting API key in this test env (FMP/Finnhub/Alpaca raise on a missing key),
    so the assertion isolates the seam behavior from the key-provisioning env.
    """
    monkeypatch.delenv("FEATURES_SOURCE", raising=False)
    from app.services.sentiment import SentimentService
    from ba2_providers.news import AlphaVantageNewsProvider

    svc = SentimentService(use_cache=False)
    prov = svc._get_news_provider("alphavantage")
    assert isinstance(prov, AlphaVantageNewsProvider)


def test_sentiment_ba2_providers_news_routes_through_get_provider(monkeypatch):
    monkeypatch.setenv("FEATURES_SOURCE", "ba2_providers")
    from app.services import features_source
    from app.services.sentiment import SentimentService

    sentinel = object()
    captured = {}

    def _fake_get_provider(category, name):
        captured["category"] = category
        captured["name"] = name
        return sentinel

    monkeypatch.setattr(features_source, "get_ba2_provider", _fake_get_provider)

    svc = SentimentService(use_cache=False)
    prov = svc._get_news_provider("fmp")
    assert prov is sentinel
    assert captured == {"category": "news", "name": "fmp"}


def test_sentiment_ba2_providers_falls_back_to_legacy(monkeypatch):
    """When the ba2_providers news provider is unavailable, fall back to legacy.

    Uses 'alphavantage' so the legacy fallback constructor does not need an API
    key in this env (see test_sentiment_default_legacy_news_provider)."""
    monkeypatch.setenv("FEATURES_SOURCE", "ba2_providers")
    from app.services import features_source
    from app.services.sentiment import SentimentService
    from ba2_providers.news import AlphaVantageNewsProvider

    monkeypatch.setattr(features_source, "get_ba2_provider", lambda c, n: None)

    svc = SentimentService(use_cache=False)
    prov = svc._get_news_provider("alphavantage")
    assert isinstance(prov, AlphaVantageNewsProvider)


def test_sentiment_localfiles_never_uses_ba2_providers(monkeypatch):
    """'localfiles' has no ba2_providers equivalent -> always legacy, even with flag."""
    monkeypatch.setenv("FEATURES_SOURCE", "ba2_providers")
    from app.services import features_source
    from app.services.sentiment import SentimentService
    from ba2_providers.news import LocalFilesNewsProvider

    called = {"n": 0}

    def _should_not_be_called(c, n):
        called["n"] += 1
        return object()

    monkeypatch.setattr(features_source, "get_ba2_provider", _should_not_be_called)

    svc = SentimentService(use_cache=False)
    prov = svc._get_news_provider("localfiles")
    assert isinstance(prov, LocalFilesNewsProvider)
    assert called["n"] == 0


# --------------------------------------------------------------------------- #
# fundamentals: _build_provider_service                                        #
# --------------------------------------------------------------------------- #

def test_fundamentals_default_legacy_provider_service(monkeypatch):
    """Default returns the legacy multi-provider orchestrator (ProviderService)."""
    monkeypatch.delenv("FEATURES_SOURCE", raising=False)
    from app.services.fundamentals import FundamentalsService
    from ba2_providers.fundamentals.service import (
        FundamentalsService as ProviderService,
    )

    svc = FundamentalsService._build_provider_service(["yfinance"])
    assert isinstance(svc, ProviderService)


def test_fundamentals_ba2_probes_then_uses_legacy_orchestrator(monkeypatch):
    """With the flag set, the ba2_providers fundamentals_details provider is probed
    (so a misconfig surfaces) but the legacy orchestrator is still returned this
    phase (deferred verification)."""
    monkeypatch.setenv("FEATURES_SOURCE", "ba2_providers")
    from app.services import features_source
    from app.services.fundamentals import FundamentalsService
    from ba2_providers.fundamentals.service import (
        FundamentalsService as ProviderService,
    )

    probed = {}

    def _fake_get_provider(category, name):
        probed["category"] = category
        probed["name"] = name
        return object()  # available

    monkeypatch.setattr(features_source, "get_ba2_provider", _fake_get_provider)

    svc = FundamentalsService._build_provider_service(["fmp"])
    assert isinstance(svc, ProviderService)
    assert probed == {"category": "fundamentals_details", "name": "fmp"}


def test_fundamentals_ba2_unavailable_still_returns_legacy(monkeypatch):
    monkeypatch.setenv("FEATURES_SOURCE", "ba2_providers")
    from app.services import features_source
    from app.services.fundamentals import FundamentalsService
    from ba2_providers.fundamentals.service import (
        FundamentalsService as ProviderService,
    )

    monkeypatch.setattr(features_source, "get_ba2_provider", lambda c, n: None)
    svc = FundamentalsService._build_provider_service(["yfinance"])
    assert isinstance(svc, ProviderService)


# --------------------------------------------------------------------------- #
# macro: __init__ provider resolution                                         #
# --------------------------------------------------------------------------- #

def test_macro_default_legacy_no_ba2_provider(monkeypatch):
    monkeypatch.delenv("FEATURES_SOURCE", raising=False)
    from app.services.macro import MacroService

    svc = MacroService(api_key="x")
    assert svc._ba2_provider is None


def test_macro_ba2_providers_resolves_fred(monkeypatch):
    monkeypatch.setenv("FEATURES_SOURCE", "ba2_providers")
    from app.services import features_source
    from app.services.macro import MacroService

    sentinel = object()
    captured = {}

    def _fake_get_provider(category, name):
        captured["category"] = category
        captured["name"] = name
        return sentinel

    monkeypatch.setattr(features_source, "get_ba2_provider", _fake_get_provider)

    svc = MacroService(api_key="x")
    assert svc._ba2_provider is sentinel
    assert captured == {"category": "macro", "name": "fred"}


def test_macro_ba2_unavailable_leaves_none(monkeypatch):
    monkeypatch.setenv("FEATURES_SOURCE", "ba2_providers")
    from app.services import features_source
    from app.services.macro import MacroService

    monkeypatch.setattr(features_source, "get_ba2_provider", lambda c, n: None)
    svc = MacroService(api_key="x")
    assert svc._ba2_provider is None

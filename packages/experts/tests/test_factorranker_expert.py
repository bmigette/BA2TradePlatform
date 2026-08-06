

class TestRankingInertnessGuard:
    """Deploy-time guard: warn when top_n >= screener_max_stocks makes the factor weights inert.

    Found live 2026-08-06 on 3 of 6 FactorRanker instances. The config is fully valid — every
    key known, every value in range — so nothing in the deploy path objected.
    """

    def _expert(self, monkeypatch, top_n, max_stocks, source="screener"):
        # ``ba2_experts.FactorRanker`` re-exports the CLASS at the package name, so `fr` is the
        # class itself, not a module holding it.
        import ba2_experts.FactorRanker as fr
        e = fr.__new__(fr)
        e.id = 99
        vals = {"top_n": top_n, "screener_max_stocks": max_stocks}
        e.get_setting_with_interface_default = lambda k: vals.get(k)
        e._resolve_universe_source = lambda: source
        import logging
        e.logger = logging.getLogger("factorranker-guard-test")
        return e

    def test_warns_when_top_n_equals_max_stocks(self, monkeypatch, caplog):
        e = self._expert(monkeypatch, top_n=30, max_stocks=30)
        with caplog.at_level("WARNING"):
            e.validate_deployed_settings()
        assert "RANKING IS INERT" in caplog.text

    def test_warns_when_top_n_exceeds_max_stocks(self, monkeypatch, caplog):
        e = self._expert(monkeypatch, top_n=35, max_stocks=30)
        with caplog.at_level("WARNING"):
            e.validate_deployed_settings()
        assert "RANKING IS INERT" in caplog.text

    def test_silent_when_ranking_actually_discriminates(self, monkeypatch, caplog):
        e = self._expert(monkeypatch, top_n=25, max_stocks=40)
        with caplog.at_level("WARNING"):
            e.validate_deployed_settings()
        assert "RANKING IS INERT" not in caplog.text

    def test_static_universe_is_not_flagged(self, monkeypatch, caplog):
        """A static universe is not capped by the screener, so top_n is meaningful there."""
        e = self._expert(monkeypatch, top_n=30, max_stocks=30, source="static")
        with caplog.at_level("WARNING"):
            e.validate_deployed_settings()
        assert "RANKING IS INERT" not in caplog.text

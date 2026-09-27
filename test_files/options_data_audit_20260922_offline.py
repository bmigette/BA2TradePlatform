"""Offline audit reproductions; asserts observed gaps, not desired future behavior.

Run explicitly with pytest. Never contacts a broker or a production database.
"""
from dataclasses import asdict
from datetime import date, datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.option_selector import check_liquidity_data_available, OptionLiquidityDataUnavailable
from ba2_common.core.types import OptionRight
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from ba2_trade_platform.ui.pages import live_trades


def test_reproduce_metric_gaps(monkeypatch):
    symbol = "SPY261016C00766000"
    stamp = datetime(2026, 9, 21, 19, 59, tzinfo=timezone.utc)
    snapshot = SimpleNamespace(
        latest_quote=SimpleNamespace(bid_price=10.0, ask_price=10.2, bid_size=5, ask_size=8, timestamp=stamp),
        latest_trade=SimpleNamespace(price=10.1, size=4, timestamp=stamp),
        implied_volatility=0.3,
        greeks=SimpleNamespace(delta=0.5, gamma=0.02, theta=-0.1, vega=0.4, rho=0.03),
        daily_bar=SimpleNamespace(volume=1000, timestamp=stamp),
    )
    meta = SimpleNamespace(
        underlying_symbol="SPY", type=SimpleNamespace(value="call"), strike_price=766.0,
        expiration_date=date(2026, 10, 16), open_interest="1500", open_interest_date=date(2026, 9, 18),
    )
    account = AlpacaAccount.__new__(AlpacaAccount)
    account.id = 999
    account._settings_cache = {"api_key": "fake", "api_secret": "fake", "paper_account": True,
                               "data_feed": "iex", "options_feed": "indicative"}
    account._option_data_client = SimpleNamespace(
        get_option_chain=lambda request: {symbol: snapshot},
        get_option_snapshot=lambda request: {symbol: snapshot},
    )
    monkeypatch.setattr(account, "_get_option_contracts_meta", lambda *a, **k: {symbol: meta})
    chain = account.get_option_chain("SPY", date(2026, 10, 1), date(2026, 11, 1), OptionRight.CALL)
    quote = account.get_option_quote(symbol)
    assert len(chain) == 1
    assert chain[0].volume is None
    assert not hasattr(chain[0], "rho")
    assert not hasattr(chain[0], "timestamp")
    assert quote.timestamp == stamp
    try:
        check_liquidity_data_available(chain, min_volume=1, underlying="SPY", source="audit_fixture")
    except OptionLiquidityDataUnavailable as exc:
        volume_gate = type(exc).__name__
    else:
        raise AssertionError("Expected the enabled volume gate to reject the unpublished data")

    values = [0.3] * 20
    fake_session = SimpleNamespace(
        exec=lambda query: SimpleNamespace(all=lambda: [SimpleNamespace(atm_iv=v) for v in values]),
        close=lambda: None,
    )
    monkeypatch.setattr(live_trades, "get_db", lambda: fake_session)
    ui_rank, sample_count = live_trades.LiveTradesTab()._iv_rank(999, "SPY", 0.3)
    rule_rank = OptionsAccountInterface._iv_rank_from_series(values, 0.3)
    assert ui_rank == 100.0
    assert rule_rank == 0.0
    output = {
        "source": "synthetic input passed through actual adapter and UI/rule methods",
        "chain_fields": sorted(asdict(chain[0])), "quote_fields": sorted(asdict(quote)),
        "chain_volume": chain[0].volume, "enabled_volume_gate_result": volume_gate,
        "equal_iv_samples": sample_count, "current_iv": 0.3,
        "ui_percentile": ui_rank, "rule_percentile": rule_rank,
    }
    destination = Path(__file__).resolve().parents[1] / "reports/review_evidence/options-data-2026-09-22/offline-probes.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")

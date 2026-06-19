"""Phase-2 backtest engine subpackage.

Houses the daily multi-asset backtest engine and the ``BacktestAccount`` simulated
broker. All code here is the BA2TestPlatform *host* side: it consumes the three
Phase-0 packages (``ba2_common``, ``ba2_providers``, ``ba2_experts``) and wires the
``ba2_common`` seams (instance resolver, LLM service, ``TradeConditions`` provider
resolver, ATR indicator provider) plus a per-run, separate backtest trading DB.
"""

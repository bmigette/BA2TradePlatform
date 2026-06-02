from ba2_trade_platform.modules.experts.FactorRanker.portfolio import rebalance_deltas


def test_rebalance_buys_new_and_sells_dropped():
    target = {"A": 0.5, "B": 0.5}          # weights
    held = {"A": 10.0, "C": 20.0}          # shares
    prices = {"A": 10.0, "B": 5.0, "C": 4.0}
    deltas = rebalance_deltas(target, held, prices, equity=1000.0)
    # target A = $500 -> 50 sh, have 10 -> +40 ; B = $500 -> 100 sh, have 0 -> +100
    # C not in target -> sell all 20
    assert deltas["A"] == 40.0
    assert deltas["B"] == 100.0
    assert deltas["C"] == -20.0


def test_no_trade_when_on_target():
    deltas = rebalance_deltas({"A": 1.0}, {"A": 100.0}, {"A": 10.0}, equity=1000.0)
    assert deltas.get("A", 0.0) == 0.0

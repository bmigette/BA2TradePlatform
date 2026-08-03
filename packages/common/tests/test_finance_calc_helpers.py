import pytest


def test_percentile_linear_interpolation():
    from ba2_common.core.finance_calc.series import percentile
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.0) == 1.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 1.0) == 4.0


def test_formatters():
    from ba2_common.core.finance_calc.format import money, num, pct
    assert money(1_230_000_000) == "$1.23B"
    assert money(-1_200_000) == "-$1.20M"
    assert money(None) == "n/a"
    assert money(float("nan")) == "n/a"
    assert pct(0.181) == "18.1%"
    assert num(1.2345) == "1.23"  # banker's rounding of 1.2345 -> 1.23
    assert num(True) == "n/a"     # bools are not numbers


def test_safe_eval_exact_arithmetic():
    from ba2_common.core.finance_calc.arithmetic import safe_eval
    assert safe_eval("(37-13)/13*100") == pytest.approx(184.6153846, rel=1e-6)
    assert safe_eval("sqrt(16) + abs(-2)") == 6.0
    assert safe_eval("2**10") == 1024


def test_safe_eval_rejects_unsafe_input():
    from ba2_common.core.finance_calc.arithmetic import safe_eval
    for bad in ("__import__('os')", "open('x')", "x + 1", "f'{1}'"):
        with pytest.raises(ValueError):
            safe_eval(bad)


def test_render_calc():
    from ba2_common.core.finance_calc.arithmetic import CalcRequest, render_calc
    out = render_calc(CalcRequest(expression="1+2"))
    assert "`1+2` = **3**" in out

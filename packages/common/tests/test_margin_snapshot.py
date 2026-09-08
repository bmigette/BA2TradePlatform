"""AccountSnapshot.option_buying_power: the broker's option (derivative) buying power.

None means the broker did not publish one -- never 0.0. The base tolerant probe
that reads it from `options_buying_power` (Alpaca) or `derivative_buying_power`
(TastyTrade) is covered in test_account_seams.py, next to the other field probes.
"""
from ba2_common.core.account_types import AccountSnapshot


def test_default_is_none():
    assert AccountSnapshot().option_buying_power is None

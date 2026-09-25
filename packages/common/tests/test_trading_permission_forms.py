"""The shared permission reader changes no risk-manager decision for any stored value form.

``TradeRiskManagement`` used to test ``enable_buy`` / ``enable_sell`` by plain truthiness of
``get_setting_with_interface_default``; it and ``SellAction`` now share
``ExtendableSettingsInterface.trading_permission`` (``coerce_bool`` over the same read). This pins
that, for every form a stored row can hold -- JSON bools, JSON strings ('"false"' is what prod
stores for rows written before coerce_bool), doubly-encoded strings, 0/1, garbage, an empty or
null JSON column, and no row at all (the interface default) -- the decision is identical, through
a REAL expert's settings loader and the RM's own ``_filter_orders_by_permissions``.
"""
import json
from types import SimpleNamespace

import pytest

from ba2_common.core.db import add_instance
from ba2_common.core.interfaces import MarketExpertInterface
from ba2_common.core.interfaces.ExtendableSettingsInterface import trading_permission
from ba2_common.core.models import AccountDefinition, ExpertInstance, ExpertSetting
from ba2_common.core.TradeRiskManagement import TradeRiskManagement
from ba2_common.core.types import OrderDirection

_MISSING = object()

#: Every value the JSON column can hand the loader (already JSON-decoded by SQLAlchemy).
FORMS = [
    True, False,
    "true", "false", "True", "False",
    json.dumps("true"), json.dumps("false"),                 # '"true"' / '"false"' (prod legacy)
    json.dumps(json.dumps("false")),                        # corrupted, doubly escaped
    1, 0, "1", "0", json.dumps("1"), json.dumps("0"),
    "yes", "no", "on", "off",
    "maybe", "", {}, None,                                  # unreadable -> loader says False
    _MISSING,                                               # no row: the interface default
]


class _Expert(MarketExpertInterface):
    def __init__(self, expert_id):
        self.id = expert_id
        self._settings_cache = None

    @classmethod
    def description(cls):
        return "permission-forms stub"

    def render_market_analysis(self, ma):
        return ""

    def run_analysis(self, symbol, market_analysis):
        return None


def _expert_with(key, form):
    account_id = add_instance(AccountDefinition(name="perm", provider="stub", description=None))
    expert_id = add_instance(ExpertInstance(account_id=account_id, expert="_Expert"))
    if form is not _MISSING:
        add_instance(ExpertSetting(instance_id=expert_id, key=key, value_str=None,
                                   value_json=form, value_float=None))
    return _Expert(expert_id)


def _orders():
    return [SimpleNamespace(id=1, symbol="AAA", side=OrderDirection.BUY),
            SimpleNamespace(id=2, symbol="BBB", side=OrderDirection.SELL)]


@pytest.mark.parametrize("key", ["enable_buy", "enable_sell"])
@pytest.mark.parametrize("form", FORMS, ids=lambda f: "missing" if f is _MISSING else repr(f))
def test_the_rm_decision_is_unchanged_for_every_stored_form(key, form):
    expert = _expert_with(key, form)
    raw = expert.get_setting_with_interface_default(key, log_warning=False)

    old = bool(raw)                          # the RM's pre-change truthiness test
    new = trading_permission(expert, key)
    assert new is old, f"{key}={form!r}: loader gave {raw!r}, old {old}, new {new}"

    # ...and through the RM's own filter, with the other permission held fixed.
    other = {"enable_buy": True, "enable_sell": True}
    rm = TradeRiskManagement()
    before = rm._filter_orders_by_permissions(
        _orders(), raw if key == "enable_buy" else other["enable_buy"],
        raw if key == "enable_sell" else other["enable_sell"])
    after = rm._filter_orders_by_permissions(
        _orders(), new if key == "enable_buy" else other["enable_buy"],
        new if key == "enable_sell" else other["enable_sell"])
    assert [o.id for o in before] == [o.id for o in after]


@pytest.mark.parametrize("form, expected", [
    (json.dumps("false"), False), (json.dumps("true"), True), ("false", False), (True, True),
    (1, True), (0, False), (_MISSING, None),
])
def test_the_expected_meanings(form, expected):
    """Spot values, so the equality above cannot pass by both sides being wrong together."""
    for key, default in (("enable_buy", True), ("enable_sell", False)):
        want = default if expected is None else expected
        assert trading_permission(_expert_with(key, form), key) is want


def test_no_expert_grants_nothing():
    assert trading_permission(None, "enable_sell") is False

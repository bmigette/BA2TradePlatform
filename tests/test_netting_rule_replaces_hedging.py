"""Equity short selling, Task S2: the NETTING rule replaces the ``allow_hedging`` setting.

A US equity account nets positions per symbol, so a long and a short in the same symbol cannot
coexist: an order opposite to an open position only reduces or closes it, and a new position in
the other direction opens only from flat. Every stored ``allow_hedging`` was false, which is that
rule, so the setting is removed rather than kept as a switch that could only lie.

Pinned here:
  * the setting is gone from the definitions (and named as RETIRED, known-and-ignored);
  * the Smart Risk Manager toolkit refuses an opposite open -- long held and short held -- with
    the same result dict the false case returned, now pointing at the close/reduce tools, and
    keeps its EXPERT-scoped query (another expert's position is the prompt's business, not the
    toolkit's -- unchanged);
  * both Smart Risk Manager prompts carry the netting sentence and no "hedging";
  * a stale stored ``allow_hedging`` row -- even a TRUE one -- is loaded as an undeclared key,
    read by nothing, and survives the settings loader, the batch export/import round trip, the
    single-expert import path and the deploy-parity overlay without an error.
"""
import pytest

from ba2_trade_platform.core import SmartRiskManagerGraph as graph_mod
from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import ExpertSetting, Transaction
from ba2_trade_platform.core.SmartRiskManagerPrompts import NETTING_RULE_SENTENCE
from ba2_trade_platform.core.SmartRiskManagerToolkit import SmartRiskManagerToolkit
from ba2_trade_platform.core.types import OrderDirection, TransactionStatus
from ba2_common.core.interfaces.MarketExpertInterface import (
    MarketExpertInterface, RETIRED_EXPERT_SETTINGS,
)
from tests.conftest import MockExpert
from tests.factories import create_account_definition, create_expert_instance

NETTING = ("An order opposite to an open position only reduces or closes it; "
           "a new position in the other direction opens only from flat")


def _stale_hedging_row(instance_id, value_json="true"):
    """The shape prod rows carry: the column holds the JSON STRING ``"true"``/``"false"``
    (``json.dumps(bool)`` written into a JSON column), i.e. the Python str ``"true"``.
    TRUE on purpose: if anything still read it, this is the value that would change behaviour."""
    add_instance(ExpertSetting(instance_id=instance_id, key="allow_hedging",
                               value_json=value_json, value_str=None, value_float=None))


@pytest.fixture
def expert_with_stale_key():
    account = create_account_definition()
    inst = create_expert_instance(account_id=account.id, expert="MockExpert", alias="netting")
    _stale_hedging_row(inst.id)
    return MockExpert(inst.id)


# --------------------------------------------------------------------------- definitions
class TestTheSettingIsGone:
    def test_allow_hedging_is_not_a_setting_definition(self):
        MarketExpertInterface._ensure_builtin_settings()
        assert "allow_hedging" not in MarketExpertInterface._builtin_settings
        assert "allow_hedging" not in MockExpert.get_merged_settings_definitions()

    def test_it_is_named_as_retired(self):
        assert "allow_hedging" in RETIRED_EXPERT_SETTINGS

    def test_the_sentence_is_the_one_the_design_names(self):
        assert NETTING_RULE_SENTENCE == NETTING


# --------------------------------------------------------------------------- stale stored value
class TestAStaleStoredValueIsIgnored:
    def test_the_loader_keeps_it_as_an_undeclared_key(self, expert_with_stale_key):
        settings = expert_with_stale_key.settings  # must not raise
        assert settings["allow_hedging"] == "true", "loaded verbatim, typed from the stored row"

    def test_a_reader_left_behind_would_fail_loudly(self):
        """The interface reader raises on an undeclared key, so a forgotten
        ``get_setting_with_interface_default("allow_hedging")`` cannot silently default."""
        fresh = MockExpert(create_expert_instance(
            account_id=create_account_definition().id, expert="MockExpert").id)
        with pytest.raises(ValueError, match="not found in"):
            fresh.get_setting_with_interface_default("allow_hedging")

    def test_no_code_reads_the_setting(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        readers = set()
        for base in ("ba2_trade_platform", "packages/common/ba2_common",
                     "packages/experts/ba2_experts", "packages/providers/ba2_providers",
                     "testplatform/backend/app", "tools"):
            for path in (root / base).rglob("*.py"):
                text = path.read_text(encoding="utf-8", errors="ignore")
                if any(n in text for n in ('"allow_hedging"', "'allow_hedging'",
                                           "allow_hedging_checkbox")):
                    readers.add(path.relative_to(root).as_posix())
        # The one permitted literal is the RETIRED set's own.
        assert readers == {"packages/common/ba2_common/core/interfaces/MarketExpertInterface.py"}

    def test_the_batch_export_import_round_trip_carries_it_inertly(self):
        from ba2_trade_platform.core.expert_batch_export_import import (
            apply_batch_import, build_batch_export, plan_batch_import,
        )
        from ba2_trade_platform.core.utils import get_expert_instance_from_id

        account = create_account_definition()
        inst = create_expert_instance(account_id=account.id, expert="FMPRating",
                                      alias="netting-src")
        _stale_hedging_row(inst.id, value_json="false")

        payload = build_batch_export([inst.id])
        assert payload["experts"][0]["expert_settings"]["allow_hedging"] == "false"
        payload["experts"][0]["general"]["alias"] = "netting-dst"

        messages = apply_batch_import(plan_batch_import(payload))
        assert not any(m.startswith("FAILED") for m in messages), messages
        assert any(m.startswith("CREATED") for m in messages), messages

        from ba2_trade_platform.core.db import get_all_instances
        from ba2_trade_platform.core.models import ExpertInstance
        dst = next(i for i in get_all_instances(ExpertInstance) if i.alias == "netting-dst")
        imported = get_expert_instance_from_id(dst.id)
        imported._settings_cache = None
        assert imported.settings["allow_hedging"] == "false", "carried, not interpreted"
        # And the netting rule is what the permission surface says, whatever that row holds.
        assert "allow_hedging" not in type(imported).get_merged_settings_definitions()

    @pytest.mark.parametrize("value", ["false", "true", False, True])
    def test_the_single_expert_import_path_saves_it_without_error(self, value):
        """settings.py's import writes each exported key through ``save_setting(key, value)``
        with no type; an undeclared key is typed from the value and stored."""
        account = create_account_definition()
        inst = create_expert_instance(account_id=account.id, expert="MockExpert")
        expert = MockExpert(inst.id)
        expert.save_setting("allow_hedging", value)
        expert._settings_cache = None
        assert "allow_hedging" in expert.settings

    def test_the_deploy_parity_overlay_neither_carries_nor_refuses_it(self):
        """``tools/import_deploy_payload.py`` writes ``expert_params`` (overlaid by the forced
        table) through ``save_settings``. The table has no hedging row, and a payload exported
        before the removal still applies."""
        from ba2_common.core.deploy_parity import (
            BACKTEST_FORCED_SETTINGS, BacktestRunFacts, backtest_only_settings,
            forced_expert_settings,
        )
        facts = BacktestRunFacts(enable_short=True, hold_assigned_stock=False, entry_action=None)
        forced = forced_expert_settings(facts)
        assert "allow_hedging" not in forced
        assert "allow_hedging" not in backtest_only_settings(facts)
        assert all("hedg" not in (r.key + str(r.live_setting)) for r in BACKTEST_FORCED_SETTINGS)

        account = create_account_definition()
        inst = create_expert_instance(account_id=account.id, expert="MockExpert")
        expert = MockExpert(inst.id)
        stale_payload = {"allow_hedging": False, "enable_buy": True}
        expert.save_settings({k: (v, None) for k, v in {**stale_payload, **forced}.items()})
        expert._settings_cache = None
        assert expert.settings["enable_sell"] is True
        assert "allow_hedging" in expert.settings


# --------------------------------------------------------------------------- toolkit
def _toolkit(expert):
    tk = object.__new__(SmartRiskManagerToolkit)
    tk.expert = expert
    tk.expert_instance_id = expert.id
    return tk


def _hold(expert_id, side, symbol="TSLA"):
    return add_instance(Transaction(symbol=symbol, quantity=10.0, side=side,
                                    status=TransactionStatus.OPENED, expert_id=expert_id))


class TestTheToolkitAppliesTheNettingRule:
    @pytest.mark.parametrize("held,opener,direction", [
        (OrderDirection.BUY, "open_sell_position", "SELL"),
        (OrderDirection.SELL, "open_buy_position", "BUY"),
    ], ids=["long-held-refuses-sell", "short-held-refuses-buy"])
    def test_an_opposite_open_is_refused(self, expert_with_stale_key, held, opener, direction):
        """A stale allow_hedging=TRUE is stored on this expert: the refusal must not care."""
        txn_id = _hold(expert_with_stale_key.id, held)
        result = getattr(_toolkit(expert_with_stale_key), opener)(
            "TSLA", 5, tp_price=None, sl_price=100.0, reason="t")

        # The result dict the allow_hedging=false branch returned, key for key; only the
        # message's advice changed (close/reduce instead of "enable hedging").
        assert set(result) == {"success", "message", "transaction_id", "order_id", "symbol",
                               "quantity", "direction"}
        assert result["success"] is False
        assert result["transaction_id"] == txn_id
        assert result["order_id"] is None
        assert (result["symbol"], result["quantity"], result["direction"]) == ("TSLA", 5, direction)
        assert result["message"].startswith(
            f"Cannot open {direction} position: An open {held.value} position already exists for "
            f"TSLA (transaction_id={txn_id}). ")
        assert NETTING in result["message"]
        assert "close_position" in result["message"] and "adjust_quantity" in result["message"]
        assert "hedg" not in result["message"].lower()

    def test_the_same_direction_refusal_is_unchanged(self, expert_with_stale_key):
        txn_id = _hold(expert_with_stale_key.id, OrderDirection.BUY)
        result = _toolkit(expert_with_stale_key).open_buy_position("TSLA", 5, sl_price=100.0)
        assert result["success"] is False
        assert result["message"] == (
            f"Cannot open new position: An open BUY position already exists for TSLA "
            f"(transaction_id={txn_id}). Use adjust_quantity to modify the existing position "
            f"instead.")

    def test_the_check_stays_expert_scoped(self, expert_with_stale_key):
        """Unchanged scoping: another expert's opposite position does not trip the toolkit's
        netting check (the research prompt's account-wide Locked Symbols section covers that,
        and the broker refuses a conflicting entry). The open proceeds to the next gate -- here
        the instrument check, since MockExpert enables only AAPL/MSFT."""
        other = create_expert_instance(account_id=create_account_definition().id)
        _hold(other.id, OrderDirection.BUY)
        result = _toolkit(expert_with_stale_key).open_sell_position("TSLA", 5, sl_price=100.0)
        assert result["success"] is False
        assert result["message"] == "Symbol TSLA is not enabled in expert settings"


# --------------------------------------------------------------------------- prompts
class _PortfolioToolkit:
    def __init__(self, *a, **k):
        pass

    def get_portfolio_status(self):
        return {"account_virtual_equity": 1000.0, "account_available_balance": 1000.0,
                "open_positions": []}


class TestThePromptsStateTheNettingRule:
    def test_the_system_prompt(self, expert_with_stale_key, monkeypatch):
        expert = expert_with_stale_key
        expert.save_settings({"allow_automated_trade_opening": (True, None),
                              "allow_automated_trade_modification": (True, None),
                              "enable_sell": (True, None)})
        monkeypatch.setattr(graph_mod, "get_expert_instance_from_id", lambda _id: expert)
        monkeypatch.setattr(graph_mod, "SmartRiskManagerToolkit", _PortfolioToolkit)

        out = graph_mod.initialize_context({"expert_instance_id": expert.id, "account_id": 1})
        prompt = out["messages"][0].content

        assert f"- **Opposite orders (netting):** {NETTING}" in prompt
        # The focus guidance keeps its note slot, now the netting sentence.
        assert f"Manage the full portfolio lifecycle. Note: {NETTING}." in prompt
        assert "hedg" not in prompt.lower()

    def test_the_research_prompt(self, expert_with_stale_key, monkeypatch):
        expert = expert_with_stale_key

        class _SummaryToolkit:
            def get_trade_summary_by_symbol(self):
                return {"AAPL": {"buy_qty": 10.0, "sell_qty": 0.0}}

        g = object.__new__(graph_mod.SmartRiskManagerGraph)
        g.expert = expert
        g.toolkit = _SummaryToolkit()
        g.research_tools = []
        monkeypatch.setattr(graph_mod, "get_expert_instance_from_id", lambda _id: expert)
        monkeypatch.setattr(graph_mod, "create_llm", lambda *a, **k: object())
        monkeypatch.setattr(graph_mod, "bind_tools_safely", lambda *a, **k: object())

        state = {"risk_manager_model": "x", "backend_url": None, "api_key": None,
                 "expert_instance_id": expert.id, "expert_settings": {},
                 "portfolio_status": {"account_virtual_equity": 1000.0},
                 "open_positions": [], "agent_scratchpad": ""}
        prompt = g._initialize_research_agent(state)["research_messages"][0].content

        assert "**Netting Rule (CRITICAL):**" in prompt
        assert f"- ⚠️ {NETTING}\n- BEFORE recommending new positions" in prompt
        # The account-wide locked-symbols section is still built (it was built whenever
        # allow_hedging was false -- i.e. always), even with a stale TRUE stored.
        assert "Locked Symbols (account-wide positions, netting rule)" in prompt
        assert "**AAPL**: Existing BUY position (qty 10) on account" in prompt
        assert "hedg" not in prompt.lower()

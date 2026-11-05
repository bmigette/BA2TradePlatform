"""Round trip through the REAL recorder: capture option actions, export, replay, every stage matches.

An option ENTRY and a CLOSE run through ``TradeActions`` on a broker-shaped options account inside
a real ``capture_scope`` over a real store in ``tmp_path``. The session is exported with the
existing export service and ``option_selection`` replays it. Nothing in the bundle is written by
hand, so a tap, a recorder field or a tape lookup that drifts from the others fails here.
"""
from __future__ import annotations

from ba2_common.core import instance_resolver
from ba2_common.core.db import add_instance
from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.models import (
    ExpertInstance, ExpertRecommendation, ExpertSetting, TradingOrder, Transaction,
)
from ba2_common.core.replay import ReplayStatus, load_bundle
from ba2_common.core.types import (
    AssetClass, OrderDirection, OrderRecommendation, OrderStatus, OrderType, TransactionStatus,
)

from app.services.replay import option_selection
from app.services.replay.isolation import replay_isolation
from tests.replay.option_support import (
    BPS, RecordingOptionsAccount, close_action, entry_action, recommendation, record_session,
    seed_open_option,
)

MATCH = ReplayStatus.COVERAGE_MATCH


def _assert_every_stage_matches(report, expected_rows):
    assert report.total == expected_rows
    for row in report.results:
        assert row.status == MATCH, f"{row.expert_class}: {row.detail}"
        assert row.field_diffs == ()
    for stage in option_selection.STAGES:
        assert set(report.stage_results[stage].values()) == {MATCH}, stage
        assert len(report.stage_results[stage]) == expected_rows


def test_an_entry_and_a_close_recorded_in_one_pass_both_match_at_every_stage(tmp_path):
    held = {}

    def seed():
        held["order"] = seed_open_option()

    def scenario():
        account = RecordingOptionsAccount()
        entry = entry_action(account).execute()
        close = close_action(account, held["order"]).execute()
        return entry, close, account

    recorded = record_session(tmp_path / "store", tmp_path / "export", scenario, seed=seed)
    entry, close, account = recorded["outcome"]
    assert entry["success"], entry["message"]
    assert close["success"], close["message"]
    assert account.submitted and account.closed

    bundle = load_bundle(recorded["bundle_dir"])
    (analysis,) = bundle.analyses
    actions = [o for o in bundle.observations_for(analysis.analysis_id)
               if o.provider == "option_action"]
    assert [o.method for o in actions] == ["OpenBullPutSpreadAction", "CloseOptionAction"]
    assert len(analysis.clock_reads) >= 2        # one per action span, at least

    report = option_selection.run(recorded["bundle_dir"], tmp_path / "report")
    _assert_every_stage_matches(report, 2)
    close_row = next(r for r in report.results if r.expert_class == "CloseOptionAction")
    assert close_row.symbol == "XYZ"
    markdown = (tmp_path / "report" / "option_selection.md").read_text(encoding="utf-8")
    assert "historical_provider" in markdown


def test_the_replayed_close_stamps_the_same_exit_record(tmp_path):
    held = {}
    recorded = record_session(
        tmp_path / "store", tmp_path / "export",
        lambda: close_action(RecordingOptionsAccount(), held["order"]).execute(),
        seed=lambda: held.update(order=seed_open_option()))
    assert recorded["outcome"]["success"], recorded["outcome"]["message"]
    bundle = load_bundle(recorded["bundle_dir"])
    (observation,) = [o for o in bundle.observations if o.provider == "option_action"]
    payload = bundle.decode(observation.payload_object)
    assert payload["outcome"]["exit_record"], "the recorder must have captured the exit record"
    assert [s["method"] for s in payload["outcome"]["submits"]] == ["close_option_position"]

    _assert_every_stage_matches(option_selection.run(recorded["bundle_dir"]), 1)


class _Expert(MarketExpertInterface):
    def __init__(self, expert_id):
        self.id = expert_id

    @classmethod
    def description(cls):
        return "stub"

    def render_market_analysis(self, ma):
        return ""

    def run_analysis(self, symbol, market_analysis):
        return None


class _Resolver:
    """What the live host injects: an expert by id and the broker account."""

    def __init__(self, account):
        self.account = account

    def get_expert_instance(self, expert_id):
        return _Expert(expert_id)

    def get_account_instance(self, account_id):
        return self.account

    def get_account_instance_from_transaction(self, transaction):
        return self.account


def test_an_entry_owned_by_an_expert_replays_through_the_injected_resolver_and_scratch_rows(tmp_path):
    """The expert capital headroom reads its ExpertInstance / settings / transactions from the
    DB and its account through the resolver: the replay must serve both."""
    ids = {}

    def seed():
        ids["expert"] = add_instance(ExpertInstance(account_id=1, expert="X", virtual_equity_pct=80.0))
        add_instance(ExpertSetting(instance_id=ids["expert"],
                                   key="max_virtual_equity_per_instrument_percent",
                                   value_float=12.0, value_str=None))
        ids["rec"] = add_instance(ExpertRecommendation(
            instance_id=ids["expert"], symbol="XYZ", recommended_action=OrderRecommendation.BUY,
            expected_profit_percent=5.0, price_at_date=100.0, details=None, confidence=70.0,
            risk_level="MEDIUM", time_horizon="SHORT_TERM"))
        txn = add_instance(Transaction(symbol="HELD", quantity=10, side=OrderDirection.BUY,
                                       status=TransactionStatus.OPENED, expert_id=ids["expert"]))
        add_instance(TradingOrder(
            account_id=1, symbol="HELD", quantity=10, filled_qty=10, side=OrderDirection.BUY,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, open_price=50.0,
            asset_class=AssetClass.EQUITY, transaction_id=txn))

    account = RecordingOptionsAccount()
    previous = instance_resolver.get_instance_resolver()
    instance_resolver.set_instance_resolver(_Resolver(account))
    try:
        def scenario():
            rec = recommendation(id=ids["rec"], instance_id=ids["expert"])
            return entry_action(account, rec=rec).execute()

        recorded = record_session(tmp_path / "store", tmp_path / "export", scenario, seed=seed)
    finally:
        instance_resolver.set_instance_resolver(previous)
    assert recorded["outcome"]["success"], recorded["outcome"]["message"]

    bundle = load_bundle(recorded["bundle_dir"])
    payload = bundle.decode(next(o.payload_object for o in bundle.observations
                                 if o.provider == "option_action"))
    assert {r["model"] for r in payload["db_rows"]} >= {"ExpertInstance", "ExpertSetting",
                                                         "ExpertRecommendation", "Transaction",
                                                         "TradingOrder"}

    with replay_isolation() as probe:
        report = option_selection.run_bundle(bundle)
    _assert_every_stage_matches(report, 1)
    assert probe.clean, probe.summary()


def _record_expert_entry(tmp_path):
    ids = {}

    def seed():
        ids["expert"] = add_instance(ExpertInstance(account_id=1, expert="X", virtual_equity_pct=80.0))
        ids["rec"] = add_instance(ExpertRecommendation(
            instance_id=ids["expert"], symbol="XYZ", recommended_action=OrderRecommendation.BUY,
            expected_profit_percent=5.0, price_at_date=100.0, details=None, confidence=70.0,
            risk_level="MEDIUM", time_horizon="SHORT_TERM"))
        txn = add_instance(Transaction(symbol="HELD", quantity=10, side=OrderDirection.BUY,
                                       status=TransactionStatus.OPENED, expert_id=ids["expert"]))
        add_instance(TradingOrder(
            account_id=1, symbol="HELD", quantity=10, filled_qty=10, side=OrderDirection.BUY,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, open_price=50.0,
            asset_class=AssetClass.EQUITY, transaction_id=txn))

    account = RecordingOptionsAccount()
    previous = instance_resolver.get_instance_resolver()
    instance_resolver.set_instance_resolver(_Resolver(account))
    try:
        return record_session(
            tmp_path / "store", tmp_path / "export",
            lambda: entry_action(account, rec=recommendation(
                id=ids["rec"], instance_id=ids["expert"])).execute(), seed=seed)
    finally:
        instance_resolver.set_instance_resolver(previous)


def test_a_recorded_db_row_that_goes_missing_is_never_a_match(tmp_path):
    recorded = _record_expert_entry(tmp_path)
    bundle = load_bundle(recorded["bundle_dir"])

    class _NoExpertRow:
        def __getattr__(self, name):
            return getattr(bundle, name)

        def decode(self, *args, **kwargs):
            value = bundle.decode(*args, **kwargs)
            if isinstance(value, dict) and "db_rows" in value:
                value["db_rows"] = [r for r in value["db_rows"] if r["model"] != "ExpertInstance"]
            return value

    with replay_isolation():
        report = option_selection.run_bundle(_NoExpertRow())
    (row,) = report.results
    # The expert's slice (virtual_equity_pct 80) is part of the sized reserve, so without its row
    # the replay asks the tape for a reserve live never asked: a miss naming that request.
    assert row.status == ReplayStatus.COVERAGE_MISSING_CAPTURE, row.detail
    assert "check_option_buying_power" in row.detail


def test_a_refused_entry_round_trips_with_the_same_refusal(tmp_path):
    recorded = record_session(
        tmp_path / "store", tmp_path / "export",
        lambda: entry_action(RecordingOptionsAccount(),
                             **dict(BPS, dte_min=500, dte_max=600)).execute())
    assert not recorded["outcome"]["success"]
    report = option_selection.run(recorded["bundle_dir"])
    _assert_every_stage_matches(report, 1)


def test_the_inventory_counts_option_actions_and_names_the_capability_that_replays_them(tmp_path):
    from app.services.replay import inventory

    held = {}
    recorded = record_session(
        tmp_path / "store", tmp_path / "export",
        lambda: (entry_action(RecordingOptionsAccount()).execute(),
                 entry_action(RecordingOptionsAccount(),
                              **dict(BPS, dte_min=500, dte_max=600)).execute(),
                 close_action(RecordingOptionsAccount(), held["order"]).execute()),
        seed=lambda: held.update(order=seed_open_option()))
    option_selection.run(recorded["bundle_dir"])

    counted = inventory.run(recorded["bundle_dir"], tmp_path / "inv")
    options = counted["option_actions"]
    assert options["total"] == 3
    assert options["by_class_and_outcome"] == {
        "CloseOptionAction/submitted": 1,
        "OpenBullPutSpreadAction/refused": 1,
        "OpenBullPutSpreadAction/submitted": 1}
    assert options["replayed_by"] == "option_selection"
    assert options["coverage"][ReplayStatus.COVERAGE_MATCH] == 3
    # the stock capability table is untouched
    assert "option_selection" not in counted["coverage_by_capability"]
    markdown = inventory.to_markdown(counted)
    assert "## Option actions" in markdown and "`option_selection` capability" in markdown


def test_the_inventory_counts_an_undecodable_option_payload_instead_of_aborting(tmp_path):
    from app.services.replay import inventory

    recorded = record_session(
        tmp_path / "store", tmp_path / "export",
        lambda: (entry_action(RecordingOptionsAccount()).execute(),
                 entry_action(RecordingOptionsAccount(), rec=recommendation(id=2)).execute()))
    bundle = load_bundle(recorded["bundle_dir"])
    objects = {o.payload_object for o in bundle.observations if o.provider == "option_action"}
    bad = sorted(objects)[0]

    class _Broken:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def decode(self, ref, *a, **k):
            if ref == bad:
                raise ValueError("corrupt object")
            return self._inner.decode(ref, *a, **k)

    counted = inventory._option_actions(_Broken(bundle))
    assert counted["total"] == 2
    labels = counted["by_class_and_outcome"]
    assert labels.get("OpenBullPutSpreadAction/undecodable", 0) >= 1
    assert sum(labels.values()) == 2


# --------------------------------------------------------------------------- recorded read errors
def _quote_down_account(exc_factory):
    class QuoteDown(RecordingOptionsAccount):
        def get_option_quote(self, contract_symbol):
            raise exc_factory()

    return QuoteDown()


def _close_with_a_failing_quote(tmp_path, exc_factory):
    held = {}
    recorded = record_session(
        tmp_path / "store", tmp_path / "export",
        lambda: close_action(_quote_down_account(exc_factory), held["order"]).execute(),
        seed=lambda: held.update(order=seed_open_option()))
    assert recorded["outcome"]["success"], recorded["outcome"]["message"]   # fell back to entry premium
    bundle = load_bundle(recorded["bundle_dir"])
    errors = [o for o in bundle.observations if o.response_class == ReplayStatus.RESPONSE_ERROR]
    assert errors and errors[0].method == "get_option_quote"
    return recorded


def test_a_read_that_raised_and_was_handled_replays_through_the_same_branch(tmp_path):
    """The close fell back to the entry premium because the quote read raised a ConnectionError
    (an OSError: the one family the live handler absorbs under BA2_ERROR_MODE=enforce). Before the
    error observation that replayed as missing_capture; now the tape raises the same type."""
    recorded = _close_with_a_failing_quote(tmp_path, lambda: ConnectionError("quote service down"))
    _assert_every_stage_matches(option_selection.run(recorded["bundle_dir"]), 1)


def test_a_third_party_error_class_replays_as_a_difference_never_a_false_match(tmp_path):
    """Documented limit: an SDK error class (here an OSError subclass, so live absorbed it) cannot be
    rebuilt offline; the tape raises RecordedProviderError, which the handler does not absorb, and
    the report says DIFFERENCE (an honest gap), not match and not missing_capture."""
    class SdkError(OSError):
        pass

    SdkError.__module__ = "alpaca.common.exceptions"
    recorded = _close_with_a_failing_quote(tmp_path, lambda: SdkError("rate limited"))
    report = option_selection.run(recorded["bundle_dir"])
    (row,) = report.results
    assert row.status == ReplayStatus.COVERAGE_DIFFERENCE
    assert "RecordedProviderError" in row.detail and "rate limited" in row.detail


# --------------------------------------------------------------------------- nested taps
class _MovingSnapshotAccount(RecordingOptionsAccount):
    """Every snapshot read answers a DIFFERENT equity, so a mis-served read cannot match."""

    def __init__(self):
        super().__init__()
        self.reads = 0

    def get_account_snapshot(self):
        from ba2_common.core.account_types import AccountSnapshot
        self.reads += 1
        equity = 100_000.0 + self.reads
        return AccountSnapshot(cash=equity, equity=equity, net_liquidation=equity,
                               buying_power=equity)


def test_a_tapped_read_inside_a_tapped_read_is_recorded_deeper_and_ignored_by_the_tape(tmp_path):
    def scenario():
        account = _MovingSnapshotAccount()
        equity = account.option_capital_equity()          # outer; calls get_account_snapshot inside
        direct = account.get_account_snapshot()           # a direct read of the same identity
        return equity, direct.equity

    recorded = record_session(tmp_path / "store", tmp_path / "export", scenario)
    assert recorded["outcome"] == (100_001.0, 100_002.0)
    bundle = load_bundle(recorded["bundle_dir"])
    reads = [(o.method, o.tap_depth) for o in bundle.observations
             if o.provider == "broker_options"]
    assert reads == [("get_account_snapshot", 1), ("option_capital_equity", 0),
                     ("get_account_snapshot", 0)]
    window = option_selection.action_window(
        sorted(bundle.observations, key=lambda o: o.invocation_seq), 0, 10 ** 6)
    assert [(o.method, o.tap_depth) for o in window] == [("option_capital_equity", 0),
                                                         ("get_account_snapshot", 0)]

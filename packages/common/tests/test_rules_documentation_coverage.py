"""Every ``ExpertActionType`` member must carry a `rules_documentation.py` entry.

WHY THIS EXISTS. `get_action_type_documentation()` backs the ruleset editor's help text --
a member with no entry there renders with no thesis, no example, and no risk note for
anyone building or reviewing a rule against it. The two 1x2 backspreads
(`OPEN_CALL_BACKSPREAD` / `OPEN_PUT_BACKSPREAD`, plan Task 14a item 5) shipped without one;
this guard is the ratchet that stops the NEXT new action from doing the same -- a hand-
maintained "did I remember the docs" checklist is exactly the kind of thing that lags
(the same lesson `test_strike_method_registry.py`'s own docstring makes about a
hand-maintained list of which builders honour a strike method).

Backfilled while adding this guard (measured, not assumed): `STOP_PROCESSING` and six
exotic/credit structures (`OPEN_SHORT_STRADDLE`, `OPEN_SHORT_STRANGLE`, `OPEN_IRON_CONDOR`,
`OPEN_JADE_LIZARD`, `OPEN_CALL_BUTTERFLY`, `OPEN_PUT_RATIO_SPREAD`) were ALSO undocumented
before this task -- not merely the two backspreads the plan named. Left undocumented would
have made this test fail on landing; documenting all nine keeps the ratchet unconditional
(every member, no baseline) rather than a partial guard with pre-existing debt baked in.
"""
from ba2_common.core.rules_documentation import get_action_type_documentation
from ba2_common.core.types import ExpertActionType


def test_every_action_type_has_a_documentation_entry():
    docs = get_action_type_documentation()
    missing = sorted(m.value for m in ExpertActionType if m.value not in docs)
    assert not missing, f"ExpertActionType member(s) with no rules_documentation.py entry: {missing}"


def test_every_documentation_entry_maps_to_a_real_action_type():
    """The reverse direction: a stale/typo'd key would sit there forever, documenting
    nothing real, if this guard only checked coverage one way."""
    docs = get_action_type_documentation()
    valid_values = {m.value for m in ExpertActionType}
    stale = sorted(k for k in docs if k not in valid_values)
    assert not stale, f"rules_documentation.py key(s) with no matching ExpertActionType: {stale}"


def test_every_entry_has_the_expected_shape():
    """Guards the ENTRY, not just its presence: a `{}` placeholder would satisfy the
    coverage test above while rendering nothing useful in the editor."""
    docs = get_action_type_documentation()
    required_keys = {"name", "description", "use_cases", "parameters", "example"}
    for value, entry in docs.items():
        missing_keys = required_keys - set(entry)
        assert not missing_keys, f"{value}: entry missing key(s) {sorted(missing_keys)}"
        assert isinstance(entry["name"], str) and entry["name"].strip()
        assert isinstance(entry["description"], str) and entry["description"].strip()
        assert isinstance(entry["use_cases"], list) and entry["use_cases"]
        assert isinstance(entry["example"], str) and entry["example"].strip()


def test_the_two_backspreads_document_the_arc_floor_exemption_and_the_long_strike_pin():
    """Pins the two specific claims plan Task 14a item 5 asked for: risk note naming the
    LONG strike as the worst-case pin (NOT the short strike -- the actual builder
    docstrings, TradeActions.py's OpenCallBackspreadAction/OpenPutBackspreadAction, say the
    worst case is a pin AT THE LONG STRIKE), and the ARC-floor exemption
    (`option_economics.ARC_FLOOR_EXEMPT_STRATEGIES` names `call_backspread`/
    `put_backspread`)."""
    docs = get_action_type_documentation()
    for action_type, strategy_name in (
        (ExpertActionType.OPEN_CALL_BACKSPREAD, "call_backspread"),
        (ExpertActionType.OPEN_PUT_BACKSPREAD, "put_backspread"),
    ):
        entry = docs[action_type.value]
        blob = entry["description"] + " " + entry["parameters"] + " " + entry["example"]
        assert "LONG strike" in blob, f"{action_type.value}: missing the long-strike risk note"
        assert "worst case pins at the short strike" not in blob.lower(), (
            f"{action_type.value}: must not claim the worst case pins at the SHORT strike "
            f"(it pins at the LONG strike -- see TradeActions.py's builder docstrings)")
        assert "at the short strike)" not in blob, (
            f"{action_type.value}: max-loss risk note names the wrong strike (short, not long)")
        assert "ARC" in entry["parameters"] and "EXEMPT" in entry["parameters"].upper(), (
            f"{action_type.value}: missing the ARC-floor exemption note")

    from ba2_common.core.option_economics import ARC_FLOOR_EXEMPT_STRATEGIES
    assert {"call_backspread", "put_backspread"} <= ARC_FLOOR_EXEMPT_STRATEGIES

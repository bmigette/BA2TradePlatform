"""The rule editor's trigger vocabulary, categorised: what the Trigger Type picker offers.

WHY THIS EXISTS. The editor used to render ``ExpertEventType`` as one flat, unordered,
scrolled dropdown of sixty-five strings, with the fifteen market-condition fields filtered
OUT of it entirely. Two failures, one module:

* **The market gates were invisible.** The filter was well-meant -- a market gate authored
  with no ``market_condition_profile`` behind it reads ``no_context`` and never fires, and on
  an open-positions ruleset that silently stops an exit -- but removing the fields from the
  menu did not remove the risk, it removed the FEATURE. An operator could not see a gate a
  deployed expert was already running, could not author one for an expert whose profile IS
  set, and had no way to learn the vocabulary existed. The cure here is a per-entry MESSAGE
  (:attr:`TriggerEntry.requires_profile`) instead of a deletion; the actual refusals stay
  where they belong, in ``market_condition_rules``.
* **Sixty-five undescribed strings.** Finding ``days_since_last_close`` meant already knowing
  it was called that.

WHAT IT GUARANTEES. Every ``ExpertEventType`` member appears exactly once, whatever else is
missing: a member nobody documented renders as its raw key, and a member nobody filed in
:data:`_CATEGORIES` lands in :data:`_DEFAULT_CATEGORIES` rather than vanishing. A categorised
menu that can LOSE a trigger would be worse than the flat list it replaces, because the flat
list could not -- it was the enum. ``test_trigger_catalog.py`` is the ratchet on both.

PURE. No DB, no NiceGUI, no I/O -- it reads three in-process sources (the enum, the
documentation dict, the market-condition registry) and returns plain frozen dataclasses, so
the launcher, a report or a test can ask the same questions the picker asks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Mapping, Optional, Tuple

from ba2_common.core.market_condition_rules import market_condition_fields
from ba2_common.core.rules_documentation import get_event_type_documentation
from ba2_common.core.types import ExpertEventType, get_operator_options

#: The picker's chips, in render order. ``'all'`` is first and is the ABSENCE of a filter: it
#: is never stored on an entry, because "this trigger is in the All tab" is not a fact about
#: the trigger.
CATEGORIES: Tuple[str, ...] = ('all', 'position', 'signal', 'targets', 'options', 'market', 'timing')

#: The three shapes a trigger's value can take, which is what decides the operator/value
#: controls the editor puts beside it. A flag has no threshold; a number is compared with
#: ``<``/``>``; a categorical is compared with ``==`` against a regime code and must NEVER be
#: offered an ordering (``structure_state > bull`` compares 1 with 2 as if bull and bear sat on
#: a scale). That last sentence is ENFORCED by :func:`operator_options_for`, not merely asserted
#: here: until it was, ``kind`` was a label the rule editor never read, the row drew its
#: operators from ``is_numeric_event`` -- True for ``structure_state``, whose stored value is a
#: float -- and ``structure_state > 1`` ("bear only") was one click away from anyone who had
#: just been told "1 = bull".
KIND_FLAG = 'flag'
KIND_NUMBER = 'number'
KIND_CATEGORICAL = 'categorical'

#: ``rules_documentation``'s own ``"type"`` vocabulary -> our ``kind``. Both spellings of the
#: numeric type are accepted because the existing entries use ``"numeric"`` and the design
#: names ``"number"``; neither is worth a migration, and guessing between them is not a risk.
_DOC_TYPE_KINDS: Dict[str, str] = {
    'boolean': KIND_FLAG,
    'numeric': KIND_NUMBER,
    'number': KIND_NUMBER,
}

#: ``market_conditions.FieldSpec.kind`` -> our ``kind``.
_FIELD_SPEC_KINDS: Dict[str, str] = {
    'numeric': KIND_NUMBER,
    'categorical': KIND_CATEGORICAL,
}

#: The enum member-NAME prefix, which is this platform's own convention and is stated in
#: ``types.py`` beside the members ("F = Flag/Boolean", "N = Number/Count"). Last resort only:
#: it reads the member's name, not its meaning, so a documented entry always wins.
_NAME_PREFIX_KINDS: Dict[str, str] = {'F_': KIND_FLAG, 'N_': KIND_NUMBER}


@dataclass(frozen=True)
class TriggerEntry:
    """One row of the picker. Frozen because the picker renders the registry and must not be
    able to edit it: a UI callback that rewrote ``value`` would change what a saved rule
    stores."""

    #: The ``ExpertEventType`` value -- what a rule actually stores as ``event_type``. The
    #: picker shows it in mono beneath the friendly name, because that is the string the
    #: operator will see again in an exported payload, a log line and a deploy diff.
    value: str
    #: Friendly name; the raw ``value`` when nothing supplies one.
    name: str
    #: One line of help; ``''`` when the trigger carries no documentation entry.
    description: str
    #: ``'flag'`` | ``'number'`` | ``'categorical'`` -- see the KIND_* constants.
    kind: str
    #: Never empty and never containing ``'all'``.
    categories: FrozenSet[str]
    #: The market-condition profile an expert must carry for this trigger to mean anything,
    #: or ``''`` for every trigger that needs none. NOT a warning string: the picker composes
    #: the sentence, this is the profile NAME, read from the registry so that renaming a
    #: profile cannot leave the picker naming one that no longer exists.
    requires_profile: str


# ---------------------------------------------------------------------------------------------
# The filing cabinet
# ---------------------------------------------------------------------------------------------
# Built ONCE at import from literal name lists plus ``market_condition_fields()`` -- which is
# itself anchored on ``STRICT_FIELD_NAMES``, a permanent literal list, precisely so that a
# process whose registry is older or newer than the payload still knows the names. Eighteen
# members sit in more than one category; that is deliberate and is why the values are sets.

_POSITION = (
    'has_no_position', 'has_position', 'has_buy_position', 'has_sell_position',
    'has_no_position_account', 'has_position_account', 'has_option_position',
    'has_covered_call', 'has_protective_put', 'has_assigned_shares', 'days_opened',
    'profit_loss_amount', 'profit_loss_percent', 'instrument_account_share',
    'loss_pct_of_max_loss', 'profit_multiple_of_premium', 'percent_to_current_target',
    'percent_open_to_new_target',
)

_SIGNAL = (
    'bearish', 'bullish',
    'rating_negative_to_neutral', 'rating_negative_to_positive', 'rating_neutral_to_negative',
    'rating_neutral_to_positive', 'rating_positive_to_negative', 'rating_positive_to_neutral',
    'rating_upgraded', 'rating_downgraded',
    'current_rating_positive', 'current_rating_overweight', 'current_rating_neutral',
    'current_rating_underweight', 'current_rating_negative',
    'rec_direction', 'confidence', 'short_term', 'medium_term', 'long_term',
    'highrisk', 'mediumrisk', 'lowrisk', 'expected_profit_target_percent',
    'new_target_higher', 'new_target_lower',
)

_TARGETS = (
    'new_target_higher', 'new_target_lower', 'percent_to_current_target',
    'percent_to_new_target', 'new_target_percent', 'price_vs_target_low_percent',
    'price_vs_target_high_percent', 'price_vs_target_consensus_percent',
    'percent_open_to_new_target', 'expected_profit_target_percent',
)

_OPTIONS = (
    'has_option_position', 'has_covered_call', 'has_protective_put', 'has_assigned_shares',
    'days_to_expiry', 'short_leg_days_to_expiry', 'covered_call_days_to_expiry',
    'loss_pct_of_max_loss', 'profit_multiple_of_premium', 'credit_decayed_pct',
    'long_leg_delta', 'iv_rank', 'iv_to_realized_vol',
)

#: Everything that describes the TAPE rather than the position or the expert's call -- the four
#: price/volatility readers plus every market-condition field. Grouping them together is the
#: point of this pass: the fifteen fields were unreachable from the editor at all.
_MARKET = (
    'relative_volume', 'percent_below_recent_high', 'percent_above_recent_low',
    'iv_to_realized_vol',
) + tuple(sorted(market_condition_fields()))

_TIMING = (
    'days_opened', 'days_since_last_close', 'days_since_last_profitable_close',
    'days_since_last_losing_close', 'days_to_earnings', 'rec_days_to_earnings',
    'days_after_event', 'days_to_expiry', 'short_leg_days_to_expiry',
    'covered_call_days_to_expiry', 'structure_bars_since_bos', 'structure_bars_since_choch',
)


def _build_categories() -> Dict[str, FrozenSet[str]]:
    out: Dict[str, set] = {}
    for category, values in (
        ('position', _POSITION), ('signal', _SIGNAL), ('targets', _TARGETS),
        ('options', _OPTIONS), ('market', _MARKET), ('timing', _TIMING),
    ):
        for value in values:
            out.setdefault(value, set()).add(category)
    return {value: frozenset(cats) for value, cats in out.items()}


#: ``ExpertEventType`` value -> the categories it is filed under.
_CATEGORIES: Dict[str, FrozenSet[str]] = _build_categories()

#: Where a member absent from :data:`_CATEGORIES` goes. It is NOT permission to skip filing --
#: ``test_every_event_type_is_filed_in_at_least_one_concrete_category`` names any omission so it
#: gets fixed deliberately -- it is the guarantee that the omission costs a mislabelled trigger
#: rather than a missing one. A trigger nobody can find is a feature the operator believes the
#: platform does not have.
_DEFAULT_CATEGORIES: FrozenSet[str] = frozenset({'signal'})


def _categories_for(value: str) -> FrozenSet[str]:
    """The categories ``value`` is filed under, or the default bucket. Deliberately total: a
    key miss here is a filing gap, not a bug, and must never drop the trigger."""
    return _CATEGORIES.get(value, _DEFAULT_CATEGORIES)


# ---------------------------------------------------------------------------------------------
# Resolving one entry's fields
# ---------------------------------------------------------------------------------------------

def _name_for(value: str, doc: Optional[Mapping[str, Any]], ui_name: str) -> str:
    """documentation name -> registry ``ui_name`` -> the raw key.

    Ending at the raw key rather than at a blank (or at an exception) is what keeps an
    undocumented trigger AUTHORABLE. The old dropdown raised ``ValueError`` on a value outside
    its option list, which made a deployed gated rule impossible even to open."""
    if doc is not None and str(doc['name']).strip():
        return str(doc['name'])
    if ui_name.strip():
        return ui_name
    return value


def _kind_for(member_name: str, doc: Optional[Mapping[str, Any]], field_spec_kind: str) -> str:
    """``'flag'`` | ``'number'`` | ``'categorical'``, from the most authoritative source that
    has an answer.

    The market REGISTRY is consulted first for a market field, ahead of the documentation,
    because the documentation's ``"type"`` vocabulary has no word for ``categorical``: a doc
    entry written later for ``structure_state`` could only say ``"numeric"``, and the editor
    would then offer a ``>`` threshold on a regime CODE -- a gate comparing bull (1) with bear
    (2) as if they were ordered. The design lists the documentation first; it lists the sources
    the catalog merges, not a precedence for a field only one of them can describe correctly.
    """
    if field_spec_kind:
        if field_spec_kind not in _FIELD_SPEC_KINDS:
            raise ValueError(
                f"market-condition field kind {field_spec_kind!r} is not one of "
                f"{sorted(_FIELD_SPEC_KINDS)}; the picker cannot choose an operator control for it")
        return _FIELD_SPEC_KINDS[field_spec_kind]
    if doc is not None:
        doc_type = str(doc['type'])
        if doc_type not in _DOC_TYPE_KINDS:
            raise ValueError(
                f"rules_documentation entry for {member_name} has type {doc_type!r}, which is not "
                f"one of {sorted(_DOC_TYPE_KINDS)}. Guessing would put the wrong operator control "
                f"beside the trigger; add the type to _DOC_TYPE_KINDS instead.")
        return _DOC_TYPE_KINDS[doc_type]
    for prefix, kind in _NAME_PREFIX_KINDS.items():
        if member_name.startswith(prefix):
            return kind
    raise ValueError(
        f"cannot tell what kind of value {member_name} holds: it has no documentation entry, it is "
        f"not a market-condition field, and its name carries none of the {sorted(_NAME_PREFIX_KINDS)} "
        f"prefixes types.py documents. Add a rules_documentation entry for it.")


def _market_field_description(spec: Any) -> str:
    """One line of help for a market-condition field, composed from its ``FieldSpec``.

    THE FIFTEEN FIELDS THIS FEATURE EXISTS TO EXPOSE WERE THE ONLY CATALOG ENTRIES WITH NO
    DESCRIPTION. Every other trigger carries prose from ``rules_documentation``; these carried a
    name and nothing else -- no unit, no range, no code legend -- which is the worst place for
    that gap to be, because they are the vocabulary the operator has never seen before. A number
    typed into the value box needs three facts to mean anything:

    * WHAT IT MEASURES (``FieldSpec.unit``). ``structure_dist_support_atr`` and
      ``structure_bars_since_bos`` are both "a number around 2"; reading the first as sessions or
      the second as ATRs authors a gate that is off by an order of magnitude and still plausible.
    * WHAT SCALE IT IS ON (``value_min``/``value_max``). An ADX threshold of ``0.5`` is not a
      strict gate, it is a gate that never blocks anything.
    * THE CODE LEGEND, for the categorical. ``structure_state == 1`` is bull and ``== 2`` is
      bear; without the legend the number is a guess.

    Composed from the spec rather than written out, for the reason ``requires_profile`` is read
    from the registry: a retuned range or an added code would leave a hand-written line wrong,
    and a wrong description reads as authoritative in a way a missing one does not. Factual only
    -- the anchor is reported as the template's own fixed reading, not as advice.
    """
    codes = spec.codes
    if codes is not None:
        legend = ", ".join(f"{name}={code}" for name, code in codes.items())
        return (f"{spec.ui_name or spec.name}: a regime CODE, compared with '=='. "
                f"Codes: {legend} (an unclassified market is 0 and matches neither).")
    unit = f" Unit: {spec.unit}." if spec.unit else ""
    return (f"{spec.ui_name or spec.name}.{unit} Searched {spec.value_min:g} to "
            f"{spec.value_max:g} in steps of {spec.value_step:g}; the optimizer template's "
            f"fixed reading is {spec.anchor_op} {spec.anchor_value:g}.")


def _market_field_specs() -> Dict[str, Any]:
    """``field -> FieldSpec`` for every field this process's registry declares."""
    from ba2_common.core.market_conditions import PROFILES

    return {f.name: f for prof in PROFILES.values() for f in prof.fields}


def _market_field_kinds_and_profiles() -> Tuple[Dict[str, str], Dict[str, str]]:
    """``field -> FieldSpec.kind`` and ``field -> registering profile name``, read fresh from
    ``market_conditions.PROFILES``.

    Read, never hardcoded: a field->profile table written out here would go stale the day a
    profile is renamed or split, and the note it then prints ("needs the ohlcv-v1 profile") would
    send the operator to configure a profile that no longer exists, while the gate they authored
    quietly reads ``no_context`` and never fires.

    A name that is DEPLOYABLE (``STRICT_FIELD_NAMES``) but not registered in this process --
    an older ba2_common, or a profile retired since the payload was written -- is absent from
    both maps. It is still offered, still filed under Market, and simply names no profile,
    because this process genuinely does not know which one serves it.
    """
    from ba2_common.core.market_conditions import PROFILES

    kinds: Dict[str, str] = {}
    profiles: Dict[str, str] = {}
    for profile in PROFILES.values():
        for field in profile.fields:
            kinds[field.name] = field.kind
            profiles[field.name] = profile.name
    return kinds, profiles


def _ui_names() -> Dict[str, str]:
    from ba2_common.core.market_conditions import PROFILES

    return {f.name: f.ui_name for prof in PROFILES.values() for f in prof.fields}


# ---------------------------------------------------------------------------------------------
# The public API
# ---------------------------------------------------------------------------------------------

def trigger_catalog() -> Tuple[TriggerEntry, ...]:
    """Every ``ExpertEventType``, in enum declaration order, merged with its documentation and
    its market-condition registration.

    NOT memoised. The market-condition registry is mutable in process -- ``register_profile``
    runs at import time for each profile module and ``registered_profile`` swaps it under tests
    -- and a cached catalog would hand the picker a ``requires_profile`` that is no longer true,
    which is exactly the stale-label failure this module exists to avoid. Eighty entries built
    from three dict lookups each costs nothing worth trading that for.
    """
    docs = get_event_type_documentation()
    field_kinds, field_profiles = _market_field_kinds_and_profiles()
    specs = _market_field_specs()
    ui_names = _ui_names()

    entries = []
    for member in ExpertEventType:
        value = member.value
        doc = docs.get(value)
        spec = specs.get(value)
        entries.append(TriggerEntry(
            value=value,
            name=_name_for(value, doc, ui_names.get(value, '')),
            # The registry wins for a market field, as it does for ``kind``: the prose composed
            # from the spec carries the unit, the range and the code legend, which no
            # rules_documentation entry written by hand would stay right about.
            description=(_market_field_description(spec) if spec is not None
                         else (str(doc['description']) if doc is not None else '')),
            kind=_kind_for(member.name, doc, field_kinds.get(value, '')),
            categories=_categories_for(value),
            requires_profile=field_profiles.get(value, ''),
        ))
    return tuple(entries)


def operator_options_for(value) -> list:
    """The comparison operators the rule editor may offer for trigger ``value``, in render order.

    THE FAILURE THIS CLOSES. ``is_numeric_event('structure_state')`` is True -- it is an ``N_``
    member, because the stored value is a float -- so the editor offered all six operators on it,
    and ``structure_state > 1`` is authorable. That expression means "bear only" (bull=1,
    bear=2), which is the OPPOSITE of what somebody who has just read "1 = bull" is trying to
    say, and nothing refused it: the kind carried by the catalog was a label the controls never
    read.

    The list comes from ``market_conditions.OPERATORS_BY_KIND``, which is the same table
    ``MarketConditionCompare`` enforces at construction -- so the editor cannot offer an operator
    the engine will refuse, and cannot withhold one it accepts. Ordinary (non-market) numeric
    triggers keep the full engine list; a flag gets ``[]``, because it has no threshold and an
    operator box beside it would invent one nothing reads.
    """
    from ba2_common.core.market_conditions import OPERATORS_BY_KIND

    spec = _market_field_specs().get(value)
    if spec is not None:
        allowed = OPERATORS_BY_KIND[spec.kind]
        return [op for op in get_operator_options() if op in allowed]
    from ba2_common.core.types import is_numeric_event

    return get_operator_options() if is_numeric_event(value) else []


def categorical_codes_for(value) -> Tuple[Tuple[str, int], ...]:
    """``(name, code)`` pairs in code order for a categorical trigger; ``()`` for anything else.

    The legend the editor prints beside the value box. Read from the registry, never re-typed:
    a stale legend is worse than none, because the operator acts on it.
    """
    spec = _market_field_specs().get(value)
    if spec is None or spec.codes is None:
        return ()
    return tuple(spec.codes.items())


def _assert_known_category(category: str) -> None:
    """A typo'd chip must not read as 'no triggers here'. An empty list is the shape of a real
    answer, so returning one for a category that does not exist hides the bug behind it."""
    if category not in CATEGORIES:
        raise ValueError(f"unknown trigger category {category!r}; known categories: {list(CATEGORIES)}")


def triggers_in_category(category: str) -> Tuple[TriggerEntry, ...]:
    """The catalog filtered to ``category``; ``'all'`` is the whole catalog, because ``'all'`` is
    the absence of a filter rather than a category any entry carries."""
    _assert_known_category(category)
    if category == 'all':
        return trigger_catalog()
    return tuple(e for e in trigger_catalog() if category in e.categories)


def search_triggers(query: str, category: str = 'all') -> Tuple[TriggerEntry, ...]:
    """Case-insensitive substring match over the friendly name, the raw key and the description,
    within ``category``.

    The raw key is searched as well as the name because that is the string a deployed rule, an
    exported payload and a log line show -- an operator arriving from any of those has the key,
    not the friendly name. The description is searched because the vocabulary is the thing being
    learned here: 'cooldown' should find the three ``days_since_last_*`` triggers even though
    none of them is spelled that way.
    """
    entries = triggers_in_category(category)
    needle = query.strip().lower()
    if not needle:
        return entries
    return tuple(
        e for e in entries
        if needle in e.name.lower() or needle in e.value.lower() or needle in e.description.lower()
    )


def category_counts() -> Dict[str, int]:
    """``category -> how many triggers it holds``, for the picker's chips. Includes ``'all'``,
    which is the full catalog size. The counts sum to more than the catalog because eighteen
    triggers are filed in two categories -- a trigger is not required to have one home."""
    catalog = trigger_catalog()
    counts = {'all': len(catalog)}
    for category in CATEGORIES:
        if category == 'all':
            continue
        counts[category] = sum(1 for e in catalog if category in e.categories)
    return counts

"""What a settings form shows for a setting that may have no stored value. Pure.

THE TRAP THIS EXISTS FOR (2026-09-22). ``ExtendableSettingsInterface.settings`` pre-fills
EVERY defined key with ``None`` when the instance has no row for it -- ``None`` means "unset",
and live code relies on that. The settings dialog read those values with
``settings_source.get(key, <literal default>)``, but the key IS present, so ``.get`` returned
``None`` instead of the literal: a checkbox was handed ``None``, and saving the expert then
died in ``coerce_bool(None)`` ("cannot read None as a boolean setting value"). The literals
were also second copies of the declared defaults and had drifted from them (e.g. the smart RM
max iterations literal was 10, the declaration says 20).

The declaration (``get_merged_settings_definitions()``, builtins included) is the only source
of a default. This module reads it; nothing here invents one.
"""
import math
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Mapping

from ba2_common.core.interfaces.ExtendableSettingsInterface import coerce_bool


class SettingHasNoDisplayValue(ValueError):
    """A bool setting with neither a stored value nor a declared default.

    A checkbox cannot show "unknown" as a value the operator can save, and picking True or
    False for them would be a silent guess at what may be a risk switch (the option-sleeve
    lifecycle bools are deliberately undeclared: unset means "not managed").
    """


def has_stored_value(stored: Mapping[str, Any], key: str) -> bool:
    """Whether ``stored`` holds a real value for ``key`` -- not absent, ``None``, nor the
    historical string ``"None"`` (rows written as ``str(None)``)."""
    value = stored.get(key)
    return value is not None and value != "None"


class ShownDefaults:
    """The controls of an EDIT form that were filled from a DECLARED default, and what each
    showed. A no-edit save must be a byte-for-byte no-op: writing those back would freeze
    today's default into a row that was missing (or NULL) -- so a later change of the
    declaration would no longer reach that instance. A save skips a recorded key while its
    control still shows the recorded value; an edited one is validated and written as usual.
    """

    def __init__(self):
        self._shown: Dict[str, Any] = {}

    def record(self, key: str, unset: bool, control_value: Any) -> None:
        """Note what ``key``'s control shows; ``unset`` = it had no stored value."""
        if unset:
            self._shown[key] = control_value
        else:
            self._shown.pop(key, None)

    def forget(self, keys: Iterable[str]) -> None:
        for key in keys:
            self._shown.pop(key, None)

    def unedited(self, key: str, control_value: Any) -> bool:
        """True when ``key`` was filled from a default and still shows exactly that."""
        if key not in self._shown:
            return False
        shown = self._shown[key]
        # type() too: True == 1 must not pass for a checkbox that became a number, etc.
        return type(shown) is type(control_value) and shown == control_value


def resolve_setting_for_display(definitions: Mapping[str, Dict[str, Any]],
                                stored: Mapping[str, Any], key: str) -> Any:
    """The value a form control should show for ``key``.

    * the stored value when it is not ``None`` (nor the historical string ``"None"``; a bool definition's stored value is read
      through ``coerce_bool``, so an imported ``"true"``/``"1"`` shows as ticked);
    * else the declared default;
    * else ``None`` for a non-bool setting (an empty field -- a required one is the save
      path's business, e.g. an account's API key on a new account);
    * else, for a bool, :class:`SettingHasNoDisplayValue`.

    ``KeyError`` for a key with no definition: there is nothing to resolve it against, and a
    caller asking is reading an undeclared key it should read directly.
    """
    definition = definitions[key]
    # Historical rows hold str(None); unset, exactly as get_setting_with_interface_default
    # reads them.
    value = stored.get(key) if has_stored_value(stored, key) else None
    is_bool = definition.get("type") == "bool"
    if value is not None:
        return coerce_bool(value) if is_bool else value
    default = definition.get("default")
    if default is not None:
        return coerce_bool(default) if is_bool else default
    if is_bool:
        raise SettingHasNoDisplayValue(
            f"bool setting '{key}' has no stored value and no declared default; "
            f"a checkbox cannot show it")
    return None


def display_text(definitions: Mapping[str, Dict[str, Any]], key: str, value: Any) -> str:
    """``value`` as a text field shows it: a WHOLE float in an int-declared field shows as an
    int (``14.0`` -> ``"14"``; GA deploys store int genes as floats)."""
    if (definitions[key].get("type") == "int" and isinstance(value, float)
            and value.is_integer()):
        value = int(value)
    return str(value)


def unset_bool_settings(values: Mapping[str, Any],
                        definitions: Mapping[str, Dict[str, Any]],
                        keys: Iterable[str] = None) -> List[str]:
    """The bool-declared keys whose control value is ``None`` -- what a save must refuse.

    ``values`` maps setting key -> the control's current value. ``keys`` limits the check
    (default: every key in ``values``). A key with no definition is not judged here.
    """
    checked = values.keys() if keys is None else keys
    return [k for k in checked
            if k in values and values[k] is None
            and definitions.get(k, {}).get("type") == "bool"]


def unset_bool_message(keys: List[str]) -> str:
    """The user-facing refusal for :func:`unset_bool_settings`' result."""
    return (f"Not saved: {', '.join(keys)} "
            f"{'has' if len(keys) == 1 else 'have'} no value (neither ticked nor unticked). "
            f"Set {'it' if len(keys) == 1 else 'them'} explicitly and save again.")


class NumericSettingNotSavable(ValueError):
    """A numeric form field that cannot be saved: unparsable, a fractional int, or empty with
    no declared default to fall back to. The message names the setting."""


def numeric_setting_for_save(definitions: Mapping[str, Dict[str, Any]], key: str,
                             raw: Any, kind: type) -> Any:
    """The value a numeric field (``kind`` = ``int`` or ``float``) saves.

    The save path used literals for a cleared field (``or 10.0``, ``else 10``, ``... else 0``)
    and swallowed parse errors into them -- the smart RM max-iterations literal 10 disagreed
    with the declared 20, and an unparsable expert int silently became 0. Now:

    * empty (``None`` / blank string) -> the DECLARED default, converted to ``kind``;
      no declared default -> :class:`NumericSettingNotSavable`;
    * an int field parses a plain integer string with ``int()`` directly (exact), and accepts
      a WHOLE float or whole decimal string (``14.0`` / ``"14.0"`` -> 14, as GA deploys store
      int genes); ``"14.5"`` / ``14.5`` are refused. A float field parses with ``float()``
      and must be finite. Anything else -> :class:`NumericSettingNotSavable`.
    """
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        default = definitions[key].get("default")
        if default is None:
            raise NumericSettingNotSavable(
                f"'{key}' is empty and has no declared default -- enter a value")
        return kind(default)
    if kind is int:
        # A plain integer string parses with int() directly: no float round-trip (which
        # loses precision past 2**53). A WHOLE float, or a whole decimal string such as
        # "14.0" (GA deploys store int genes as floats; a ui.number hands back 20.0), is
        # accepted -- via Decimal, still exact; "14.5" / 14.5 are refused, never truncated.
        if isinstance(raw, float):
            if not (math.isfinite(raw) and raw.is_integer()):
                raise NumericSettingNotSavable(f"'{key}' = {raw!r} must be a whole number")
            return int(raw)
        if isinstance(raw, bool):
            raise NumericSettingNotSavable(f"'{key}' = {raw!r} is not a number")
        text = raw.strip() if isinstance(raw, str) else raw
        try:
            return int(text)
        except (TypeError, ValueError):
            pass
        if isinstance(text, str):
            try:
                number = Decimal(text)
            except InvalidOperation:
                number = None
            if number is not None and number.is_finite() and number == number.to_integral_value():
                return int(number)
        raise NumericSettingNotSavable(f"'{key}' = {raw!r} must be a whole number")
    try:
        number = float(raw.strip() if isinstance(raw, str) else raw)
    except (TypeError, ValueError):
        raise NumericSettingNotSavable(f"'{key}' = {raw!r} is not a number") from None
    if not math.isfinite(number):
        raise NumericSettingNotSavable(f"'{key}' = {raw!r} is not a finite number")
    return number

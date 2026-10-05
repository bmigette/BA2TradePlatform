"""Which settings are secrets, and how the UI shows them. Pure (no NiceGUI import).

Every credential the UI can display or accept is hidden by default: inputs are built
``password=True`` with the eye toggle, read-only displays show :func:`mask_secret` and reveal
one value on an explicit click. Secrecy is decided in ONE place, :func:`is_secret_setting`:

* the setting's definition marks it (``"secret": True``, ``"type": "password"`` or
  ``"ui_editor_type": "password"``), OR
* its key matches a conservative name pattern (words of the key, split on ``_``/``-``/space and
  camelCase, case-insensitive): ``password``, ``passphrase``, ``secret``, ``bearer``, ``auth``,
  ``api`` + ``key``, ``access`` + ``key``, ``private`` + ``key``, or a ``token`` word.

``token`` is a unit as often as a credential, so a ``token`` word is NOT a secret when the key
also carries a quantity word (``max_tokens`` has the word ``tokens`` and never matches;
``token_limit``, ``max_token``, ``token_count`` are excluded by the quantity words).
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional

#: Words that make a ``token`` key a quantity (``token_limit``), not a credential.
_TOKEN_QUANTITY_WORDS = frozenset({
    'max', 'min', 'limit', 'limits', 'count', 'counts', 'used', 'usage', 'budget', 'total',
    'num', 'number', 'len', 'length', 'cost', 'price', 'rate', 'per', 'window', 'estimate',
})
_SINGLE_WORDS = frozenset({'password', 'passwd', 'passphrase', 'secret', 'secrets', 'bearer',
                           'auth', 'apikey', 'credential', 'credentials'})
_PAIRS = (('api', 'key'), ('access', 'key'), ('private', 'key'), ('secret', 'key'))

#: Shown in place of a value that must not be displayed.
MASK_CHAR = '•'
_FULL_MASK = MASK_CHAR * 8

#: CSS class carried by every secret input, so the eye toggle can be a >= 40 px tap target.
SECRET_INPUT_CLASS = 'ba2-secret-input'


def secret_input_css() -> str:
    """The eye toggle of a secret input is a >= 40 px square (a thumb on a phone)."""
    return (f'.{SECRET_INPUT_CLASS} .q-field__append .q-icon,'
            f'.{SECRET_INPUT_CLASS} .q-field__append .q-btn'
            '{min-width:40px;min-height:40px;display:flex;align-items:center;'
            'justify-content:center;}')


def _words(key: str) -> list:
    spaced = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', str(key))
    return [w for w in re.split(r'[^A-Za-z0-9]+', spaced.lower()) if w]


def is_secret_setting(key: str, definition: Optional[Mapping[str, Any]] = None) -> bool:
    """Whether the setting ``key`` holds a credential that must be hidden by default."""
    if definition:
        if definition.get('secret') is True:
            return True
        if definition.get('type') == 'password' or definition.get('ui_editor_type') == 'password':
            return True
    words = _words(key)
    wordset = set(words)
    if wordset & _SINGLE_WORDS:
        return True
    for first, second in _PAIRS:
        for a, b in zip(words, words[1:]):
            if a == first and b == second:
                return True
    if 'token' in wordset and not (wordset & _TOKEN_QUANTITY_WORDS):
        return True
    return False


def mask_secret(value: Any) -> str:
    """The hidden form of a secret value: bullets, plus the last 4 characters when the value
    is at least 8 long (shorter ones are fully masked). Empty/None stays empty: nothing to hide
    and an empty credential must stay recognisable as unset."""
    if value is None:
        return ''
    text = str(value)
    if not text:
        return ''
    if len(text) >= 8:
        return MASK_CHAR * 8 + text[-4:]
    return _FULL_MASK


def mask_secrets_in_data(data: Any, definitions: Optional[Mapping[str, Mapping[str, Any]]] = None) -> Any:
    """A copy of ``data`` (nested dicts/lists) with every secret-keyed scalar masked, for
    previews and raw JSON viewers. The input is never modified."""
    definitions = definitions or {}
    if isinstance(data, Mapping):
        out = {}
        for k, v in data.items():
            if isinstance(v, (Mapping, list, tuple)):
                out[k] = mask_secrets_in_data(v, definitions)
            elif is_secret_setting(str(k), definitions.get(k)):
                out[k] = mask_secret(v)
            else:
                out[k] = v
        return out
    if isinstance(data, (list, tuple)):
        return [mask_secrets_in_data(v, definitions) for v in data]
    return data

"""THE FRED API key: one name, one resolver, used by both apps.

WHY THIS EXISTS. The key used to be spelled two ways. The providers
(``FREDMacroProvider``, ``fred_series``) and the trade app's settings page read the
AppSetting ``fred_api_key``; the test platform's settings page listed and saved
``FRED_API_KEY``. AppSetting lookups are exact, so a key saved through that page was
never read by anything. Several test-platform paths read only the environment
variable instead, and one of them (the options cache builder) then fell back to a flat
4.5% risk-free rate without saying more than a warning.

THE RULE, now in one place:

  * The canonical key is the AppSetting ``fred_api_key`` (:data:`FRED_API_KEY_SETTING`).
  * The environment variable ``FRED_API_KEY`` (:data:`FRED_API_KEY_ENV`) is an explicit
    OVERRIDE and wins when set. That is the order every other key resolver in the
    codebase already uses (``warm_service._api_key``, ``prewarm_fetchers.resolve_keys``,
    ``data_build_handler._resolve_fmp_key``: env first, then the app-settings DB).
  * An AppSetting row under the legacy name ``FRED_API_KEY`` is NOT read. When it is the
    only key present, resolution REFUSES and names the migration
    (``tools/migrate_fred_api_key.py``) instead of quietly treating the key as absent.

``resolve_fred_api_key`` returns ``None`` when nothing is configured (for the callers
that decide for themselves whether a key is needed); ``require_fred_api_key`` raises.
"""
from __future__ import annotations

import os
from typing import Optional

#: The canonical AppSetting key. Every reader and every settings page uses this name.
FRED_API_KEY_SETTING = "fred_api_key"
#: The environment override. Wins over the AppSetting when set (see the module docstring).
FRED_API_KEY_ENV = "FRED_API_KEY"
#: AppSetting names that were used by mistake. Never read; refused with a pointer to the
#: migration when they are the only key present.
LEGACY_FRED_API_KEY_SETTINGS = ("FRED_API_KEY",)

MIGRATION_HINT = (
    "run `python tools/migrate_fred_api_key.py --db <that DB>` to move it to "
    f"'{FRED_API_KEY_SETTING}'")


class FredApiKeyMissing(ValueError):
    """No FRED key is configured, and the caller needs one."""


class FredApiKeyMisnamed(ValueError):
    """A FRED key is stored only under a legacy AppSetting name."""


def _setting(key: str) -> Optional[str]:
    from ba2_common.config import get_app_setting

    value = get_app_setting(key)
    return value or None


def resolve_fred_api_key() -> Optional[str]:
    """The FRED key: env ``FRED_API_KEY`` if set, else AppSetting ``fred_api_key``.

    ``None`` when neither is set. Raises :class:`FredApiKeyMisnamed` when the only key in
    the settings DB is stored under a legacy name -- a configured key that nothing reads
    must be reported, not mistaken for "no key".
    """
    env = os.environ.get(FRED_API_KEY_ENV)
    if env:
        return env
    key = _setting(FRED_API_KEY_SETTING)
    if key:
        return key
    for legacy in LEGACY_FRED_API_KEY_SETTINGS:
        if _setting(legacy):
            raise FredApiKeyMisnamed(
                f"The FRED API key is stored under the AppSetting '{legacy}', which nothing "
                f"reads; the canonical name is '{FRED_API_KEY_SETTING}'. {MIGRATION_HINT}.")
    return None


def require_fred_api_key(purpose: str) -> str:
    """The FRED key, or :class:`FredApiKeyMissing` naming what needed it and where to set it."""
    key = resolve_fred_api_key()
    if not key:
        raise FredApiKeyMissing(
            f"FRED API key not configured, required for {purpose}. Set the AppSetting "
            f"'{FRED_API_KEY_SETTING}' (Settings page of either app) or the environment "
            f"variable {FRED_API_KEY_ENV}.")
    return key


def migrate_legacy_fred_api_key(conn) -> str:
    """Move a legacy-named FRED key row to ``fred_api_key`` in one sqlite DB. Idempotent.

    ``conn`` is a ``sqlite3.Connection`` to a DB with an ``appsetting`` table. Returns
    what happened: ``"renamed"`` (the legacy row became the canonical one),
    ``"dropped-duplicate"`` (both existed with the same value; the legacy row is removed),
    or ``"nothing-to-do"``. Two rows with DIFFERENT values raise ``ValueError``: which key
    is the good one is the operator's call, not this function's.

    The caller commits.
    """
    canonical = conn.execute(
        "SELECT value_str FROM appsetting WHERE key = ?", (FRED_API_KEY_SETTING,)).fetchone()
    outcome = "nothing-to-do"
    for legacy in LEGACY_FRED_API_KEY_SETTINGS:
        row = conn.execute(
            "SELECT id, value_str FROM appsetting WHERE key = ?", (legacy,)).fetchone()
        if row is None:
            continue
        legacy_id, legacy_value = row
        if canonical is None or not canonical[0]:
            if canonical is not None:
                conn.execute("DELETE FROM appsetting WHERE key = ?", (FRED_API_KEY_SETTING,))
            conn.execute("UPDATE appsetting SET key = ? WHERE id = ?",
                         (FRED_API_KEY_SETTING, legacy_id))
            canonical = (legacy_value,)
            outcome = "renamed"
        elif canonical[0] == legacy_value or not legacy_value:
            conn.execute("DELETE FROM appsetting WHERE id = ?", (legacy_id,))
            outcome = "dropped-duplicate"
        else:
            raise ValueError(
                f"Both '{legacy}' and '{FRED_API_KEY_SETTING}' are set, to DIFFERENT values. "
                f"Delete the one that is wrong by hand; this migration will not choose.")
    return outcome

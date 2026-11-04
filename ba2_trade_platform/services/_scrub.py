"""Output scrubbing for the management API: credentials must never leave through free-form content.

Two layers, one rule:

* KEYS. A dict value under a credential-like key is replaced by ``[redacted]`` at ANY depth. The key rule works
  on WORDS (split on non-alphanumerics and camelCase), not substrings: ``api_key``, ``clientSecret``,
  ``account_pin`` match; ``spinoff``, ``mapping``, ``max_tokens``, ``shipping`` do not. Keys naming a URL
  (url / uri / endpoint / host / hostname) are NOT redacted whole -- only the ``user:password@`` part of the URL
  value is -- unless the key also says ``webhook`` or ``dsn`` (those URLs ARE the secret).
* STRINGS. Whatever the key, a string is scanned for token-shaped content: ``sk-...`` API keys, ``Bearer <x>``,
  ``key=`` / ``token=`` / ``apikey=`` / ``api_key=`` / ``secret=`` / ``password=`` query parameters,
  ``scheme://user:password@host`` userinfo, and ``"password": "..."`` pairs inside JSON-looking text.

Numbers, booleans and None are never redacted (a credential is text), and an already-masked setting
``{"set": true}`` is kept as it is."""
import re
from typing import Any, Dict, Optional

REDACTED = "[redacted]"

_SECRET_WORDS = frozenset({
    "key", "apikey", "secret", "token", "password", "passwd", "passphrase", "passcode", "pass", "pin",
    "auth", "authorization", "credential", "credentials", "bearer", "cookie", "webhook", "dsn", "private",
    "cert", "certificate", "signature", "hmac",
})
_URL_WORDS = frozenset({"url", "uri", "endpoint", "host", "hostname"})
_URL_BUT_SECRET = frozenset({"webhook", "dsn"})

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPLIT = re.compile(r"[^a-z0-9]+")


def _words(key: str):
    return [w for w in _SPLIT.split(_CAMEL.sub("_", key).lower()) if w]


def is_url_key(key: str) -> bool:
    return any(w in _URL_WORDS for w in _words(key))


def is_secret_key(key: str) -> bool:
    words = _words(key)
    if "client" in words and "id" in words:        # client_id / clientId
        return True
    if any(w in _SECRET_WORDS for w in words):
        if any(w in _URL_WORDS for w in words) and not any(w in _URL_BUT_SECRET for w in words):
            return False                           # a URL: only its userinfo is masked (scrub_string)
        return True
    return False


_SK = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}")
_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=\-]{6,}")
_QUERY = re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|key|secret|password|passwd|pwd)"
                    r"=([^&\s\"'<>]+)")
_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^\s/@:]+):([^\s/@]+)@")
_JSONISH = re.compile(r"""(?i)(["'])([A-Za-z0-9_\-]*(?:api[_-]?key|token(?!s)|secret|password|passwd|authorization)"""
                      r"""[A-Za-z0-9_\-]*)\1(\s*:\s*)(["'])(.*?)\4""")


def scrub_string(text: str) -> str:
    text = _USERINFO.sub(lambda m: f"{m.group(1)}{m.group(2)}:{REDACTED}@", text)
    text = _JSONISH.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(1)}{m.group(3)}{m.group(4)}{REDACTED}{m.group(4)}",
                        text)
    text = _SK.sub(f"sk-{REDACTED}", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _QUERY.sub(lambda m: f"{m.group(1)}={REDACTED}", text)
    return text


def _is_mask(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"set"}


def scrub(value: Any, key: Optional[str] = None) -> Any:
    """Recursively scrub a JSON-ready value (see the module docstring)."""
    if key is not None and is_secret_key(key) and isinstance(value, (str, bytes, dict, list, tuple)) \
            and not _is_mask(value):
        return REDACTED
    if isinstance(value, dict):
        return {k: scrub(v, k if isinstance(k, str) else None) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        return scrub_string(value)
    return value


def mask_setting(key: str, value: Any, definition: Optional[Dict[str, Any]]) -> Any:
    """One stored setting as the API shows it: a credential becomes ``{"set": bool}`` (never its value), a URL
    keeps everything but its userinfo, anything else is scrubbed like the rest of the output."""
    flagged = definition is not None and (
        ("secret" in definition and definition["secret"]) or ("sensitive" in definition and definition["sensitive"])
        or ("type" in definition and definition["type"] == "password"))
    if flagged or is_secret_key(key):
        return {"set": value is not None and value != ""}
    return scrub(value)

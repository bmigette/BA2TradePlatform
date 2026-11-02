"""Secret redaction for every log line the process writes (message, args, exceptions).

Installed process-wide by :func:`install` (called from ``ba2_trade_platform.logger``):

* a LogRecord factory wrapper redacts the *formatted* message (``msg % args``) at record
  creation, so it covers every logger (``ba2_common``, ``requests``/``urllib3``, ``tastytrade``,
  uvicorn, ...) and every handler (console, app.log, all.debug.log, ...), none of which need to
  know about it; records without a marker word are left untouched (cheap);
* ``logging.Formatter.formatException``/``formatStack`` are wrapped, so tracebacks and
  ``exc_text`` are redacted too;
* :class:`RedactingFilter` is the same logic as a ``logging.Filter`` for handlers that want it.

Covered: URL query secrets (``?apikey=``, ``&token=``, ...), ``Saved setting 'KEY' ...: VALUE``,
``key=value`` / ``'key': 'value'`` pairs and settings-dict dumps whose key
``is_secret_setting``, ``Authorization`` headers, ``Bearer`` tokens, ``sk-`` keys and JWTs.
The replacement keeps the key name: ``***REDACTED***``.
"""
from __future__ import annotations

import functools
import logging
import re

from .ui.utils.secret_mask import is_secret_setting

REDACTED = '***REDACTED***'

# Cheap short-circuit: no marker word, nothing to redact.
_MARKER = re.compile(r'key|token|secret|passw|bearer|auth|sk-|eyJ|credential', re.I)

_URL_PARAM = re.compile(
    r'(?P<pre>[?&;]\s*(?:x-)?(?:api[_-]?key|apikey|key|token|access[_-]?token|refresh[_-]?token|'
    r'id[_-]?token|secret|client[_-]?secret|password|passwd|pwd|auth|signature)=)(?P<v>[^&\s\'"<>)]+)', re.I)
_SAVED_SETTING = re.compile(
    r"(?P<pre>Saved setting\s+['\"](?P<k>[^'\"]+)['\"][^\n]*?:\s)(?P<v>[^\n]*)")
_AUTH_HEADER = re.compile(
    r'(?P<pre>authorization[\'"]?\s*[:=]\s*[\'"]?)(?:[A-Za-z]+\s+)?[^\s\'",}\)]+', re.I)
_BEARER = re.compile(r'(?P<pre>\bBearer\s+)[A-Za-z0-9._~+/\-]+=*', re.I)
_SK = re.compile(r'\bsk-[A-Za-z0-9_\-]{16,}')
_JWT = re.compile(r'\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*')
_KV = re.compile(
    r'''(?P<k>(?<![\w\-])['"]?[A-Za-z_][\w\-]*['"]?)(?P<sep>\s*[:=]\s*)'''
    r'''(?P<v>'[^']*'|"[^"]*"|(?![A-Za-z_][\w\-]*=)[^\s,;&}\)\]'"]+)''')
_EMPTY_VALUES = frozenset({'None', 'null', '', "''", '""', 'True', 'False', REDACTED,
                           "'" + REDACTED + "'", '"' + REDACTED + '"'})


@functools.lru_cache(maxsize=2048)
def _secret_key(key: str) -> bool:
    return is_secret_setting(key.strip('\'"'))


def _kv_sub(m: 're.Match') -> str:
    if m.group('v') in _EMPTY_VALUES or not _secret_key(m.group('k')):
        return m.group(0)
    return m.group('k') + m.group('sep') + REDACTED


def _saved_sub(m: 're.Match') -> str:
    v = m.group('v')
    if v.strip() in _EMPTY_VALUES or not _secret_key(m.group('k')):
        return m.group(0)
    return m.group('pre') + REDACTED


def redact_text(text: str) -> str:
    """``text`` with every credential replaced by ``***REDACTED***`` (key names kept)."""
    if not text or not _MARKER.search(text):
        return text
    text = _SAVED_SETTING.sub(_saved_sub, text)
    text = _URL_PARAM.sub(lambda m: m.group('pre') + REDACTED, text)
    text = _AUTH_HEADER.sub(lambda m: m.group('pre') + REDACTED, text)
    text = _BEARER.sub(lambda m: m.group('pre') + REDACTED, text)
    text = _SK.sub(REDACTED, text)
    text = _JWT.sub(REDACTED, text)
    text = _KV.sub(_kv_sub, text)
    return text


def redact_record(record: logging.LogRecord) -> None:
    """Rewrite ``record`` in place: formatted message, exc_text and stack_info."""
    try:
        msg = record.getMessage()
        new = redact_text(msg)
        if new != msg:
            record.msg = new
            record.args = ()
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info)
    except Exception:
        # Never drop a line; but never pass a possible secret knowingly either.
        try:
            record.msg = '[log message withheld: redaction failed]'
            record.args = ()
            record.exc_text = None
            record.exc_info = None
        except Exception:
            pass


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        redact_record(record)
        return True


_installed = False
_orig_factory = None
_orig_format_exception = None
_orig_format_stack = None


def install() -> None:
    """Process-wide, idempotent."""
    global _installed, _orig_factory, _orig_format_exception, _orig_format_stack
    if _installed:
        return
    _installed = True
    _orig_factory = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = _orig_factory(*args, **kwargs)
        redact_record(record)
        return record

    logging.setLogRecordFactory(factory)

    _orig_format_exception = logging.Formatter.formatException
    _orig_format_stack = logging.Formatter.formatStack

    def format_exception(self, ei):
        text = _orig_format_exception(self, ei)
        try:
            return redact_text(text)
        except Exception:
            return '[exception text withheld: redaction failed]'

    def format_stack(self, stack_info):
        text = _orig_format_stack(self, stack_info)
        try:
            return redact_text(text)
        except Exception:
            return '[stack withheld: redaction failed]'

    logging.Formatter.formatException = format_exception
    logging.Formatter.formatStack = format_stack


def uninstall() -> None:
    """Test helper."""
    global _installed
    if not _installed:
        return
    logging.setLogRecordFactory(_orig_factory)
    logging.Formatter.formatException = _orig_format_exception
    logging.Formatter.formatStack = _orig_format_stack
    _installed = False


def is_installed() -> bool:
    return _installed

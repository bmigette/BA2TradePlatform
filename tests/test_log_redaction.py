"""Log secret redaction. All secret values below are synthetic."""
import importlib.util
import io
import logging
import pathlib
import sys
import time

import pytest

from ba2_trade_platform import log_redaction as lr
from ba2_trade_platform.log_redaction import REDACTED, redact_text

K = 'Zx9QpL3mWv7TnB2cYd8RfH4'  # synthetic secret


def _no_leak(text):
    assert K not in text, text


@pytest.fixture(autouse=True)
def _installed():
    import ba2_trade_platform.logger  # noqa: F401  (installs the redaction)
    assert lr.is_installed()


def test_requests_httperror_url():
    t = (f"HTTPError: 401 Client Error: Unauthorized for url: "
         f"https://financialmodelingprep.com/api/v3/profile/AAPL?apikey={K}")
    out = redact_text(t)
    _no_leak(out)
    assert 'apikey=' + REDACTED in out and 'profile/AAPL' in out
    out = redact_text(f"GET https://api.stlouisfed.org/fred/series?series_id=GDP&api_key={K}&file_type=json")
    _no_leak(out)
    assert 'series_id=GDP' in out and 'file_type=json' in out
    for name in ('token', 'access_token', 'key', 'secret', 'password', 'client_secret'):
        _no_leak(redact_text(f"https://x.test/p?a=1&{name}={K}"))


@pytest.mark.parametrize('key', ['api_key', 'api_secret', 'client_secret', 'refresh_token',
                                 'finnhub_api_key', 'FMP_API_KEY', 'password', 'access_token'])
def test_saved_setting_lines(key):
    out = redact_text(f"Saved setting '{key}' for account_id=1: {K}")
    _no_leak(out)
    assert key in out and REDACTED in out


def test_saved_setting_non_secret_untouched():
    line = "Saved setting 'max_tokens' for expert_instance_id=3: 4096"
    assert redact_text(line) == line
    line = "Saved setting 'paper_account' for account_id=1: True"
    assert redact_text(line) == line


def test_settings_dict_dump():
    d = {'api_key': K, 'api_secret': K + 'b', 'paper_account': True, 'max_tokens': 4000,
         'client_secret': K + 'c', 'refresh_token': K + 'd', 'model': 'gpt-5'}
    out = redact_text(f"Saved settings for account_id=1: {d}")
    _no_leak(out)
    assert "'paper_account': True" in out and "'max_tokens': 4000" in out and "'model': 'gpt-5'" in out
    assert "'api_key': " + REDACTED in out
    out = redact_text('payload {"client_secret": "%s", "n": 3}' % K)
    _no_leak(out)
    assert '"n": 3' in out


def test_bearer_and_tokens():
    for t in (f"Authorization: Bearer {K}", f"headers={{'Authorization': 'Bearer {K}'}}",
              f"Authorization: Basic {K}", f"using Bearer {K} now",
              f"key sk-{K}{K}", "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc123_-def"):
        out = redact_text(t)
        _no_leak(out)
        assert 'eyJhbGci' not in out and 'sk-Zx' not in out
        assert REDACTED in out


def test_non_secret_lines_untouched():
    for t in ("Loaded max_tokens=4096 for model", "token_limit: 5000", "key insight: prices rose",
              "order 12 filled at 10.5", "monkey=banana", "https://x.test/p?symbol=AAPL&limit=5",
              "tokens used: 12345", "Setting 'api_key' is not configured"):
        assert redact_text(t) == t, t


def test_idempotent():
    once = redact_text(f"https://x.test/?apikey={K} and Saved setting 'api_key' for a=1: {K}")
    assert redact_text(once) == once


def test_args_formatted_record_through_real_logging():
    stream = io.StringIO()
    h = logging.StreamHandler(stream)
    h.setFormatter(logging.Formatter('%(name)s %(message)s'))
    names = ['ba2_trade_platform', 'ba2_common', 'requests', 'urllib3.connectionpool',
             'tastytrade', 'some.third.party']
    loggers = [logging.getLogger(n) for n in names]
    for lg in loggers:
        lg.addHandler(h)
        lg.setLevel(logging.DEBUG)
    try:
        for lg in loggers:
            lg.info("fetch %s failed: %s", f"https://x.test/q?apikey={K}", f"token={K}")
            lg.info("Saved settings for account_id=%s: %s", 1, {'api_secret': K, 'x': 1})
            lg.info(f"Saved setting 'api_key' for account_id=1: {K}")
    finally:
        for lg in loggers:
            lg.removeHandler(h)
    text = stream.getvalue()
    _no_leak(text)
    assert text.count(REDACTED) >= len(names) * 3


def test_exception_text_and_traceback():
    stream = io.StringIO()
    h = logging.StreamHandler(stream)
    h.setFormatter(logging.Formatter('%(message)s'))
    lg = logging.getLogger('ba2_common.test_exc')
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    try:
        try:
            raise RuntimeError(f"401 for url: https://x.test/a?apikey={K}")
        except RuntimeError:
            lg.error("failed", exc_info=True)
    finally:
        lg.removeHandler(h)
    _no_leak(stream.getvalue())
    assert REDACTED in stream.getvalue()
    rec = logging.LogRecord('n', 40, 'f', 1, 'm', (), None)
    rec.exc_text = f"Traceback ... apikey={K}"
    lr.redact_record(rec)
    _no_leak(rec.exc_text)


def test_formatter_exception_wrapper_directly():
    try:
        raise ValueError(f"bad https://x.test/?token={K}")
    except ValueError:
        text = logging.Formatter().formatException(sys.exc_info())
    _no_leak(text)


def test_filter_class():
    rec = logging.LogRecord('n', 20, 'f', 1, "url %s", (f"https://x.test/?apikey={K}",), None)
    assert lr.RedactingFilter().filter(rec) is True
    _no_leak(rec.getMessage())


def test_failure_never_leaks(monkeypatch):
    def boom(_):
        raise RuntimeError('x')
    monkeypatch.setattr(lr, 'redact_text', boom)
    rec = logging.LogRecord('n', 20, 'f', 1, f"apikey={K}", (), None)
    lr.redact_record(rec)
    _no_leak(rec.getMessage())


def test_performance_10k_lines():
    lines = [f"2026-10-06 order {i} filled at 10.5 for symbol AAPL qty 3 status ok" for i in range(9000)]
    lines += [f"GET https://x.test/p?apikey={K}{i}" for i in range(1000)]
    t0 = time.perf_counter()
    for ln in lines:
        redact_text(ln)
    assert time.perf_counter() - t0 < 1.0


def test_installed_in_real_logging_setup():
    import ba2_trade_platform.logger as app_logger
    assert lr.is_installed()
    # idempotent: a second install does not stack another factory/wrapper
    factory = logging.getLogRecordFactory()
    fmt = logging.Formatter.formatException
    lr.install()
    assert logging.getLogRecordFactory() is factory and logging.Formatter.formatException is fmt
    # a record built for ANY logger is redacted at creation, before any handler sees it
    rec = logging.getLogger('requests.packages').makeRecord(
        'requests', 40, 'f', 1, "url %s", (f"https://x.test/?apikey={K}",), None)
    _no_leak(rec.getMessage())
    assert callable(app_logger.reconfigure_file_logging)


def test_redact_old_logs_tool(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location(
        'redact_old_logs', pathlib.Path(__file__).resolve().parent.parent / 'tools' / 'redact_old_logs.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    log = tmp_path / 'app.log'
    log.write_text(f"ok line\nSaved setting 'api_key' for account_id=1: {K}\nGET /?apikey={K}\n", encoding='utf-8')
    assert mod.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    _no_leak(out)
    assert '2 redacted' in out
    red = (tmp_path / 'app.log.redacted').read_text(encoding='utf-8')
    _no_leak(red)
    assert 'ok line' in red
    assert K in log.read_text(encoding='utf-8')          # original untouched
    assert mod.main([str(tmp_path), '--in-place']) == 2   # needs the confirm flag
    assert K in log.read_text(encoding='utf-8')
    assert mod.main([str(tmp_path), '--in-place', '--yes-overwrite']) == 0
    _no_leak(log.read_text(encoding='utf-8'))

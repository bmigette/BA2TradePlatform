"""Regression tests for the ``migrate.py`` alembic wrapper.

Two defects are pinned here.

1. ``migrate.py`` used to shell out to a bare ``alembic`` binary
   (``subprocess.run("alembic upgrade head", shell=True)``). That console script
   only exists on ``PATH`` when the venv is *activated*, so the command CLAUDE.md
   documents as the way to run migrations --

       venv/bin/python migrate.py upgrade

   -- died with ``/bin/sh: alembic: command not found``. The fix invokes alembic
   through the *same interpreter running migrate.py*: ``[sys.executable, "-m",
   "alembic", ...]``.

2. Because the command was a shell string built with an f-string, any migration
   message containing a shell metacharacter was mis-parsed -- and
   ``$(...)``/backticks were *executed*::

       python migrate.py create 'Add $(rm -rf ~) flag'

   The fix passes an argument list with ``shell=False``, so the message reaches
   alembic verbatim and is never seen by /bin/sh.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import migrate


REPO_ROOT = Path(migrate.__file__).resolve().parent


# --------------------------------------------------------------------------
# argv construction
# --------------------------------------------------------------------------


def test_alembic_argv_runs_alembic_as_a_module_of_this_interpreter():
    argv = migrate._alembic_argv("current")

    assert argv[:3] == [sys.executable, "-m", "alembic"], (
        "alembic must be invoked via the running interpreter, not a bare "
        "'alembic' binary that is only on PATH when the venv is activated"
    )
    assert argv[3:] == ["current"]


def test_alembic_argv_is_a_list_never_a_shell_string():
    argv = migrate._alembic_argv("upgrade", "head")
    assert isinstance(argv, list)
    assert all(isinstance(a, str) for a in argv)


# --------------------------------------------------------------------------
# run_command: no shell, exit-code semantics, output passthrough
# --------------------------------------------------------------------------


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def spy_run(monkeypatch):
    """Capture the subprocess.run call migrate.py makes, without executing it."""
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(migrate.subprocess, "run", _fake_run)
    return calls


def test_run_command_never_uses_a_shell(spy_run):
    migrate.run_command([sys.executable, "-m", "alembic", "current"])

    (cmd, kwargs), = spy_run
    assert kwargs["shell"] is False, "shell=True re-parses migration messages"
    assert isinstance(cmd, list), "the command must be an argv list, not a string"


def test_run_command_returns_true_on_zero_exit(monkeypatch):
    monkeypatch.setattr(
        migrate.subprocess, "run", lambda cmd, **kw: _FakeCompleted(returncode=0)
    )
    assert migrate.run_command(["x"]) is True


def test_run_command_returns_false_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        migrate.subprocess, "run", lambda cmd, **kw: _FakeCompleted(returncode=1)
    )
    assert migrate.run_command(["x"]) is False


def test_run_command_forwards_stdout_and_stderr(monkeypatch, capsys):
    monkeypatch.setattr(
        migrate.subprocess,
        "run",
        lambda cmd, **kw: _FakeCompleted(returncode=0, stdout="OUT\n", stderr="ERR\n"),
    )
    migrate.run_command([sys.executable, "-m", "alembic", "current"])

    captured = capsys.readouterr()
    assert "OUT" in captured.out
    assert "ERR" in captured.err


# --------------------------------------------------------------------------
# every subcommand builds the argv alembic expects
# --------------------------------------------------------------------------


@pytest.fixture
def spy_argv(monkeypatch):
    """Capture the argv each subcommand hands to run_command."""
    seen = []
    monkeypatch.setattr(migrate, "run_command", lambda cmd: seen.append(cmd) or True)
    return seen


@pytest.mark.parametrize(
    "call, expected_tail",
    [
        (lambda: migrate.upgrade_database(), ["upgrade", "head"]),
        (lambda: migrate.upgrade_database("f1c8a24b7e05"), ["upgrade", "f1c8a24b7e05"]),
        (lambda: migrate.downgrade_database("-1"), ["downgrade", "-1"]),
        (lambda: migrate.show_history(), ["history", "--verbose"]),
        (lambda: migrate.show_current(), ["current"]),
        (lambda: migrate.show_heads(), ["heads"]),
        (lambda: migrate.stamp_database(), ["stamp", "head"]),
        (lambda: migrate.stamp_database("0a3e0bd24598"), ["stamp", "0a3e0bd24598"]),
        (
            lambda: migrate.create_migration("add a column"),
            ["revision", "--autogenerate", "-m", "add a column"],
        ),
    ],
)
def test_subcommand_argv(spy_argv, call, expected_tail):
    call()

    (argv,) = spy_argv
    assert argv[:3] == [sys.executable, "-m", "alembic"]
    assert argv[3:] == expected_tail


def test_create_migration_passes_the_message_as_one_verbatim_argv_element(spy_argv):
    """A message full of shell metacharacters must survive untouched.

    Under the old ``shell=True`` f-string this string lost its quotes, was split
    on ``&``, and the ``$(...)`` was *executed* by /bin/sh.
    """
    nasty = 'Add "risk" & $(touch /tmp/pwned) `id` ; drop > it'

    migrate.create_migration(nasty)

    (argv,) = spy_argv
    assert argv[-2] == "-m"
    assert argv[-1] == nasty
    # No shell quoting was baked into the value itself.
    assert not argv[-1].startswith('"')


# --------------------------------------------------------------------------
# end to end: the exact failure the user hit
# --------------------------------------------------------------------------


def _run_migrate(*args, extra_env=None):
    """Run migrate.py in a subprocess with alembic NOT on PATH."""
    env = dict(os.environ)
    # The whole point: no venv bin dir, so a bare `alembic` cannot be found.
    env["PATH"] = "/usr/bin:/bin"
    env.pop("VIRTUAL_ENV", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "migrate.py"), *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("command, needle", [("heads", "(head)"), ("history", "Rev:")])
def test_readonly_subcommands_work_without_alembic_on_path(command, needle):
    """`venv/bin/python migrate.py <cmd>` must not need an activated venv.

    `heads` and `history` never open the database, so they are safe to run for
    real. Before the fix both printed `/bin/sh: alembic: command not found`.
    """
    result = _run_migrate(command)

    combined = result.stdout + result.stderr
    assert "command not found" not in combined, combined
    assert needle in result.stdout, combined


def test_no_arguments_prints_usage_and_exits_1():
    result = _run_migrate()

    assert result.returncode == 1
    assert "Usage: python migrate.py <command>" in result.stdout


def test_unknown_command_exits_1():
    result = _run_migrate("frobnicate")

    assert result.returncode == 1
    assert "Unknown command: frobnicate" in result.stdout


def test_migrate_py_source_has_no_shell_true():
    """Belt and braces: no call site may reintroduce shell=True."""
    source = (REPO_ROOT / "migrate.py").read_text(encoding="utf-8")

    assert "shell=True" not in source
    # And nothing builds a bare `alembic ...` command string any more.
    assert 'f"alembic' not in source
    assert '"alembic ' not in source

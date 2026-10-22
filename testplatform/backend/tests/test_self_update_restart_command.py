"""``build_restart_command`` re-execs a script launch by its ABSOLUTE path (remote150 2026-10-22: a
worker started as ``python ba2test_launcher.py worker ...`` died on its self-update restart because the
relative argv[0] no longer resolved once the app had changed its working directory)."""
import os
import sys
import types

from app.services import self_update


def test_a_script_launch_restarts_by_the_absolute_path_of_main(monkeypatch, tmp_path):
    script = tmp_path / "ba2test_launcher.py"
    script.write_text("")
    monkeypatch.delitem(sys.modules, "ba2test_launcher", raising=False)
    monkeypatch.setitem(sys.modules, "__main__", types.SimpleNamespace(__file__=str(script)))
    monkeypatch.setattr(sys, "argv", ["ba2test_launcher.py", "worker", "--port", "8100"])
    monkeypatch.chdir(os.path.dirname(os.path.dirname(__file__)))   # elsewhere: the relative path no longer resolves
    assert self_update.build_restart_command() == [sys.executable, os.path.abspath(str(script)), "worker", "--port", "8100"]


def test_an_imported_launcher_still_restarts_as_a_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "ba2test_launcher", types.ModuleType("ba2test_launcher"))
    monkeypatch.setattr(sys, "argv", ["C:/venv/Scripts/ba2-test.exe", "worker", "--port", "8100"])
    assert self_update.build_restart_command() == [sys.executable, "-m", "ba2test_launcher", "worker", "--port", "8100"]

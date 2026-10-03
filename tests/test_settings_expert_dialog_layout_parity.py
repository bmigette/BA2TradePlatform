"""The phone layout of the Edit Expert dialog is MARKUP ONLY.

``expert_dialog_inputs.json`` was recorded from the dialog BEFORE the phone pass (collapsible
sections, stacked rows, short tab labels): every input-like element the dialog builds, in
order, with its constructor arguments (callables reduced to a marker), and every widget
attribute the tab keeps for Save to read. If a layout change drops, adds, reorders or
re-parameterises an input, or stops binding one to the attribute Save reads, this fails.
Regenerate (only for an INTENDED change to the form itself) with
``BA2_WRITE_SNAPSHOT=1 pytest tests/test_settings_expert_dialog_layout_parity.py``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import ba2_trade_platform.core.utils as core_utils
import ba2_trade_platform.ui.pages.settings as settings_page
from ba2_trade_platform.ui.pages.settings import ExpertSettingsTab
from tests.conftest import MockExpert
from tests.factories import create_account_definition, create_expert_instance
from tests.test_settings_dialog_missing_defaults import (
    BOOL_CONTROLS, VALUE_CONTROLS, UNLOADED, _El, _FakeUI,
)
from types import SimpleNamespace

SNAPSHOT = Path(__file__).parent / 'snapshots' / 'expert_dialog_inputs.json'
RECORDED = {'select', 'input', 'number', 'textarea', 'checkbox', 'toggle', 'switch',
            'slider', 'radio', 'button', 'tab', 'tab_panel', 'upload', 'date', 'time'}


def _plain(value):
    if callable(value):
        return '<callable>'
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return f'<{type(value).__name__}>'


class _SnapUI(_FakeUI):
    def __init__(self):
        super().__init__()
        self.calls = []

    def __getattr__(self, name):
        def make(*args, **kwargs):
            if name in RECORDED:
                self.calls.append([name, _plain(list(args)), _plain(kwargs)])
            return _El(name, *args, **kwargs)
        return make


@pytest.fixture
def snap_tab(monkeypatch):
    acc = create_account_definition()
    instance = create_expert_instance(acc.id)
    monkeypatch.setattr(core_utils, 'get_expert_instance_from_id', lambda i: MockExpert(i))
    ui = _SnapUI()
    monkeypatch.setattr(settings_page, 'ui', ui)
    # ModelSelectorInput builds REAL NiceGUI elements; with no client slot (any earlier test
    # that entered and left a Client context empties the stack) that raises. It is not a
    # layout concern here, so it is a stand-in with the same surface.
    import importlib
    ModelSelector = importlib.import_module('ba2_trade_platform.ui.components.ModelSelector')
    import sys
    ModelSelector = sys.modules['ba2_trade_platform.ui.components.ModelSelector']

    class _ModelSelectorStub:
        def __init__(self, *a, **k):
            self.args, self.kwargs, self.value = a, k, k.get('value')
        def render(self):
            return self
    monkeypatch.setattr(ModelSelector, 'ModelSelectorInput', _ModelSelectorStub)

    class _InstrumentSelectorStub(_ModelSelectorStub):
        def __getattr__(self, name):
            return lambda *a, **k: None
    monkeypatch.setattr(settings_page, 'InstrumentSelector', _InstrumentSelectorStub)
    t = object.__new__(ExpertSettingsTab)
    t._imported_expert_settings = None
    for attr in list(BOOL_CONTROLS.values()) + list(VALUE_CONTROLS.values()):
        setattr(t, attr, SimpleNamespace(value=UNLOADED))
    t.risk_atr_settings_container = SimpleNamespace(set_visibility=lambda v: None)
    t.expert_select = SimpleNamespace(value='MockExpert')
    t._get_expert_class = lambda name: MockExpert
    t.dialog = _El()
    return t, instance, ui


def _snapshot(tab, ui):
    attrs = sorted(k for k, v in vars(tab).items() if isinstance(v, _El))
    return {'constructed': ui.calls, 'bound_widget_attributes': attrs}


@pytest.mark.parametrize('edit', [True, False])
def test_the_dialog_builds_the_same_inputs_and_bindings_as_before(snap_tab, edit):
    tab, instance, ui = snap_tab
    tab.show_dialog(instance if edit else None)
    got = _snapshot(tab, ui)
    key = 'edit' if edit else 'add'
    data = json.loads(SNAPSHOT.read_text(encoding='utf-8')) if SNAPSHOT.exists() else {}
    if os.environ.get('BA2_WRITE_SNAPSHOT'):
        data[key] = got
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(json.dumps(data, indent=1, sort_keys=True), encoding='utf-8')
        return
    want = data[key]
    # The tab() entries are compared by NAME (the phone adds a short label child, not an input).
    def norm(rows):
        return [[r[0], r[1], r[2]] for r in rows]
    assert got['bound_widget_attributes'] == want['bound_widget_attributes']
    assert [r[:2] for r in norm(got['constructed'])] == [r[:2] for r in norm(want['constructed'])]
    assert norm(got['constructed']) == norm(want['constructed'])

"""A warning toast must not be white text on amber.

Reported 2026-09-01, with a screenshot: "'BAST_TECH_IT_MNFR' at 15% was NOT saved ..."
rendered white on yellow and was close to unreadable.

styles.css already ASKED for black text on a warning — the rule was simply never
matching. ``ui.notify(..., type='warning')`` does not produce
``q-notification--warning``: Quasar's Notify emits the BEM modifier for the shape
(``q-notification--standard``) and carries the colour as the utility classes
``bg-warning text-white``. So the amber came from Quasar, the black never applied, and
the blanket ``.q-notification { color: #ffffff }`` in the same file supplied white.

This pins the SELECTOR, because that is what was wrong. There are 109
``type='warning'`` call sites; a CSS rule is the only fix that reaches all of them, and
a rule that matches nothing is indistinguishable from no rule at all except by reading
Quasar's DOM.
"""
from pathlib import Path

import pytest

CSS = (Path(__file__).resolve().parents[1]
       / 'ba2_trade_platform' / 'ui' / 'static' / 'styles.css')


@pytest.fixture(scope='module')
def css() -> str:
    return CSS.read_text(encoding='utf-8')


def test_the_stylesheet_targets_the_class_quasar_actually_emits(css):
    assert '.q-notification.bg-warning' in css, (
        "warning toasts carry bg-warning, not q-notification--warning")


def test_the_warning_text_is_dark_enough_to_read_on_amber(css):
    block = css.split('.q-notification.bg-warning,', 1)[1]
    assert '#1a1f2e' in block.split('}', 1)[0], "warning toast text must be dark"


def test_the_rule_reaches_the_children_too(css):
    """Quasar's own ``text-white`` sits on the ROOT and is inherited by the message
    and the icon, so colouring only the container leaves the words white."""
    assert '.q-notification.bg-warning *' in css


def test_the_dead_selector_is_kept_and_explained_rather_than_silently_deleted(css):
    """It is harmless, and removing it would erase the evidence of what went wrong --
    the next person to wonder why a toast is the wrong colour needs to know that this
    modifier is not the one Quasar emits."""
    assert '.q-notification--warning' in css
    assert 'q-notification--standard' in css, "the explanation names the real class"

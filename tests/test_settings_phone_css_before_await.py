"""The Settings page's phone stylesheet must be installed before anything can await.

NiceGUI sends the page HTML once the page builder yields; ``ui.add_css`` /
``ui.add_head_html`` made after a page's first ``await`` only reach the head buffer that has
already been rendered and are silently dropped (the allocator's phone cards rendered unstyled
on prod, 2026-10-03). ``settings.content()`` is synchronous today; this pins that it stays
safe: the phone CSS and the breakpoint listener are installed first, nothing else in the
module adds head CSS (a dialog method runs long after the first await), and ``content`` has
no ``await`` ahead of them.
"""
import ast
import inspect
import textwrap

from ba2_trade_platform.ui.pages import settings as page


def _module_tree():
    return ast.parse(inspect.getsource(page))


def _content_source():
    return textwrap.dedent(inspect.getsource(page.content))


def _content_fn():
    return ast.parse(_content_source()).body[0]


def _calls_named(node, attr=None, name=None):
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if attr and isinstance(f, ast.Attribute) and f.attr == attr:
                out.append(n.lineno)
            if name and isinstance(f, ast.Name) and f.id == name:
                out.append(n.lineno)
    return out


def test_the_phone_css_and_listener_are_installed_first_in_content():
    fn = _content_fn()
    css = _calls_named(fn, attr='add_css')
    listener = _calls_named(fn, name='install_phone_listener')
    tab_build = _calls_named(fn, name='ExpertSettingsTab')
    assert css and listener and tab_build
    assert max(css + listener) < min(tab_build)
    awaits = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Await)]
    assert not awaits or min(awaits) > max(css + listener)


def test_content_installs_exactly_the_phone_css():
    assert 'ui.add_css(phone_css())' in _content_source()


def test_nothing_else_in_the_module_adds_head_css():
    """Dialog/tab methods run after the page's first await; head CSS added there is dropped."""
    offenders = []
    for node in ast.walk(_module_tree()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name != 'content':
            for n in ast.walk(node):
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr in ('add_css', 'add_head_html')):
                    offenders.append((node.name, n.lineno))
    assert offenders == []

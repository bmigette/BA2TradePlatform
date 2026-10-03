"""The allocator's stylesheets must be installed BEFORE ``content()``'s first await.

NiceGUI sends the page HTML once the page builder yields. Until the websocket connects,
``ui.add_css`` / ``ui.add_head_html`` only append to the client's head buffer, which has
already been rendered, so anything added in that window is silently dropped. On prod
(2026-10-03) the phone cards and the toolbar rendered completely unstyled because the CSS
was added after ``await asyncio.to_thread(_load_gate, ...)``.
"""
import ast
import inspect
import textwrap

from ba2_trade_platform.ui.pages import portfolio_allocation as page


def _content_tree() -> ast.AsyncFunctionDef:
    tree = ast.parse(textwrap.dedent(inspect.getsource(page.content)))
    fn = tree.body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)
    return fn


def _first_line(fn, predicate) -> int:
    lines = [n.lineno for n in ast.walk(fn) if predicate(n)]
    assert lines, 'node not found'
    return min(lines)


def test_page_styles_are_installed_before_the_first_await():
    fn = _content_tree()
    styles_call = _first_line(fn, lambda n: isinstance(n, ast.Call)
                              and isinstance(n.func, ast.Name)
                              and n.func.id == '_install_page_styles')
    first_await = _first_line(fn, lambda n: isinstance(n, ast.Await))
    assert styles_call < first_await


def test_content_adds_no_head_css_of_its_own():
    """Any ui.add_css / ui.add_head_html inside content() risks running after an await."""
    fn = _content_tree()
    offenders = [n.lineno for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr in ('add_css', 'add_head_html')]
    assert offenders == []


def test_install_page_styles_adds_the_link_and_the_phone_css():
    src = inspect.getsource(page._install_page_styles)
    assert 'page_css_link_html()' in src
    assert 'page_phone_css()' in src
    assert 'install_phone_listener(' in src

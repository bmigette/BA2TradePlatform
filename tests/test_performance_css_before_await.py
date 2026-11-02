"""The Performance tab's stylesheets must never be added after a page's first await.

NiceGUI drops ui.add_css / ui.add_head_html made after the first await of a page builder
(see tests/test_portfolio_allocation_css_before_await.py). This tab is built synchronously
and loads its data in a later async callback, so the rule is: styles are installed first in
render(), and nothing else on the page (least of all an async method) adds head content.
"""
import ast
import inspect
import textwrap

from ba2_trade_platform.ui.pages import performance as page


def _fn(obj):
    return ast.parse(textwrap.dedent(inspect.getsource(obj))).body[0]


def test_render_installs_styles_before_building_anything():
    fn = _fn(page.PerformanceTab.render)
    lines = {}
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            name = (n.func.id if isinstance(n.func, ast.Name)
                    else n.func.attr if isinstance(n.func, ast.Attribute) else None)
            lines.setdefault(name, []).append(n.lineno)
    styles = min(lines['_install_page_styles'])
    for other, ls in lines.items():
        if other != '_install_page_styles':
            assert styles <= min(ls), f'{other} runs before the styles are installed'
    assert not [n for n in ast.walk(fn) if isinstance(n, ast.Await)]


def test_only_install_page_styles_adds_head_content():
    offenders = []
    for name, member in inspect.getmembers(page.PerformanceTab, inspect.isfunction):
        for n in ast.walk(_fn(member)):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr in ('add_css', 'add_head_html'):
                offenders.append(name)
    assert offenders == []
    src = inspect.getsource(page._install_page_styles)
    assert 'performance_page_css()' in src and 'ui.add_css' in src


def test_async_first_load_adds_no_styles():
    src = inspect.getsource(page.PerformanceTab._first_load)
    assert 'add_css' not in src and 'add_head_html' not in src


def test_css_contains_the_phone_rules_and_pinned_name_column():
    css = page.performance_page_css()
    assert '@media (max-width: 639px)' in css
    assert '.perf-c-expert' in css and 'position: sticky' in css
    assert '.perf-period-scroll' in css

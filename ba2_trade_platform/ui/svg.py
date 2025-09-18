from pathlib import Path

from nicegui import ui

PATH = Path(__file__).parent / 'static'

GITHUB_SVG = (PATH / 'github.svg').read_text(encoding='utf-8')


def github() -> ui.html:
    return ui.html(GITHUB_SVG)
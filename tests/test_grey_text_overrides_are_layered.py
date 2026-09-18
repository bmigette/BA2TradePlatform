"""The Quasar grey overrides must live inside ``@layer theme``, or they paint nothing.

They sat unlayered in ``styles.css`` for months and did NOTHING. Quasar ships its palette in a
cascade layer -- the page declares ``theme, base, quasar, nicegui, components, utilities,
overrides, quasar_importants`` -- and for IMPORTANT declarations the layer order is REVERSED:
every layered ``!important`` beats an unlayered one regardless of specificity. So Quasar's
``.text-grey-8 { #424242 }`` won, and ``body .text-grey-8 { ... !important }`` would have lost
too, because specificity cannot cross a layer boundary.

MEASURED in the running app, one synthetic div per class, before and after the change:

    .text-grey-7   rgb(117,117,117)  ->  rgb(160,174,192)
    .text-grey-8   rgb( 97, 97, 97)  ->  rgb(144,164,174)
    .text-grey-9   rgb( 66, 66, 66)  ->  rgb(120,144,156)

Nothing in Python can see a cascade, which is why this pins the one structural fact that makes
the difference -- that the block is layered, and layered into ``theme`` specifically (the FIRST
declared layer, and for important declarations the earliest layer wins).

Sibling of ``test_ui_colour_classes_paint``, which deliberately excludes ``text-grey-*`` from
its registry contract because Quasar owns those names.
"""
import re
from pathlib import Path

import pytest

STYLES_CSS = (Path(__file__).resolve().parents[1]
              / "ba2_trade_platform" / "ui" / "static" / "styles.css")
CSS = STYLES_CSS.read_text(encoding="utf-8")

GREY_CLASSES = [f"text-grey-{n}" for n in range(1, 15)] + ["text-grey"]


def _layer_theme_block() -> str:
    """The body of ``@layer theme { ... }``, brace-matched.

    Anchored to the at-rule at the start of a line, NOT the first occurrence of the text: the
    comment above the block names ``@layer theme`` and quotes CSS with braces in it, so a plain
    ``index()`` starts brace-matching inside the comment and returns prose.
    """
    m = re.search(r"^@layer\s+theme\s*\{", CSS, re.M)
    assert m, "no @layer theme at-rule in styles.css"
    open_brace = m.end() - 1
    depth, i = 0, open_brace
    while i < len(CSS):
        if CSS[i] == "{":
            depth += 1
        elif CSS[i] == "}":
            depth -= 1
            if depth == 0:
                return CSS[open_brace + 1:i]
        i += 1
    raise AssertionError("unbalanced braces in the @layer theme block")


def test_the_stylesheet_declares_a_theme_layer():
    assert "@layer theme" in CSS, (
        "the grey overrides must be layered; unlayered !important loses to Quasar's "
        "quasar_importants layer and paints nothing")


@pytest.mark.parametrize("cls", GREY_CLASSES)
def test_every_quasar_grey_is_overridden_inside_the_layer(cls):
    """A grey left outside the layer is a class the source believes it themes and does not."""
    block = _layer_theme_block()
    assert re.search(rf"\.{re.escape(cls)}\b", block), f"{cls} is not overridden inside @layer theme"


@pytest.mark.parametrize("cls", GREY_CLASSES)
def test_no_grey_override_is_left_outside_the_layer(cls):
    """An unlayered copy would be dead weight that reads as if it works."""
    block = _layer_theme_block()
    outside = CSS.replace(block, "")
    assert not re.search(rf"^\s*\.{re.escape(cls)}\b[^{{]*{{[^}}]*color", outside, re.M), \
        f"{cls} is also overridden OUTSIDE @layer theme, where it cannot win"


def test_nothing_secondary_is_darker_than_the_floor():
    """#78909c is the dimmest the dark end may go: below it is the complaint that started
    this (an expert's open-market buyer list rendering near-invisible)."""
    block = _layer_theme_block()
    FLOOR_LUMA = 0.35
    for hexcode in set(re.findall(r"#([0-9a-fA-F]{6})", block)):
        r, g, b = (int(hexcode[i:i + 2], 16) / 255 for i in (0, 2, 4))
        luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
        assert luma >= FLOOR_LUMA, f"#{hexcode} (luma {luma:.2f}) is too dark for this theme"


def test_the_overrides_are_important():
    """Without !important they lose to Quasar even inside an earlier layer."""
    block = _layer_theme_block()
    for line in block.splitlines():
        if "text-grey" in line and "color" in line:
            assert "!important" in line, f"missing !important: {line.strip()}"

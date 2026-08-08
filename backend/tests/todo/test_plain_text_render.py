"""The plain-text format, which is the whole of the v1 export.

``navigator.share({ text })`` is ranked first in the research note (§4.7) by a
margin nothing else closes, and the string it sends is produced here. So the
properties below are the product, not helper-function trivia: if one item can
become two lines, or a decimal can render as ``5E+2``, the feature is broken in
every destination at once.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from chaudron.domain.shopping_export import ExportLine, render_line, render_plain_text


def test_one_item_per_line() -> None:
    text = render_plain_text(
        [
            ExportLine(name="Lait", quantity=Decimal("1"), unit_symbol="L"),
            ExportLine(name="Pain"),
            ExportLine(name="Oeufs", quantity=Decimal("6"), unit_symbol="pc", is_count=True),
        ]
    )
    assert text == "Lait 1 L\nPain\nOeufs \u00d7 6"
    assert not text.endswith("\n"), "a trailing newline renders as an empty final item"


def test_a_label_carrying_a_newline_cannot_become_two_items() -> None:
    """The central promise of the format, enforced where the format is defined.

    A household types what it likes into a free-text item, and a pasted label can
    carry a line break. One item silently becoming two is worse than a failure:
    nobody re-reads a list that looks plausible.
    """
    line = render_line(
        ExportLine(name="Pommes\nde\r\nterre", quantity=Decimal("2"), unit_symbol="kg")
    )
    assert line == "Pommes de terre 2 kg"
    assert "\n" not in line


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (Decimal("1"), "Sel 1 g"),
        (Decimal("1.000"), "Sel 1 g"),
        (Decimal("0.500"), "Sel 0.5 g"),
        (Decimal("500"), "Sel 500 g"),
        (Decimal("5E+2"), "Sel 500 g"),
        (Decimal("1.25"), "Sel 1.25 g"),
    ],
)
def test_amounts_are_written_the_way_a_human_writes_them(amount: Decimal, expected: str) -> None:
    """No exponent, no trailing zeros -- ``Decimal.normalize`` alone gives both wrong."""
    assert render_line(ExportLine(name="Sel", quantity=amount, unit_symbol="g")) == expected


def test_the_decimal_separator_is_a_point() -> None:
    """A comma is a field separator in half the applications this text is pasted into."""
    rendered = render_line(ExportLine(name="Creme", quantity=Decimal("0.5"), unit_symbol="L"))
    assert "," not in rendered


def test_an_item_without_a_quantity_is_just_its_name() -> None:
    assert render_line(ExportLine(name="Du pain")) == "Du pain"


def test_a_quantity_without_a_unit_still_renders() -> None:
    """A row whose unit was deleted must not lose the number the user wrote."""
    assert render_line(ExportLine(name="Citrons", quantity=Decimal("3"))) == "Citrons 3"


def test_an_empty_list_renders_as_an_empty_string() -> None:
    assert render_plain_text([]) == ""


def test_a_line_must_name_something() -> None:
    with pytest.raises(ValueError, match="must name something"):
        ExportLine(name="   ")


def test_a_negative_quantity_is_refused_at_the_boundary() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        ExportLine(name="Lait", quantity=Decimal("-1"), unit_symbol="L")

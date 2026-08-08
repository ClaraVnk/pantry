"""The two forms must not drift into each other, and the quantity column must go.

Every assertion below is anchored on a line transcribed from a real receipt photo
(the ``openprices:NNNNN`` references in ``lexicon.toml``), not on an invented label.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from chaudron.domain.labels import (
    QuantityKind,
    TruncationHint,
    normalize_label,
    to_comparison_form,
    to_display_form,
)


class TestDisplayForm:
    def test_keeps_accents_and_case(self) -> None:
        # Lidl prints mixed case with accents; showing this back lower-cased and
        # unaccented would be a regression the user can see.
        assert to_display_form("Croûtons à l'ail pou") == "Croûtons à l'ail pou"

    def test_drops_the_vat_star_auchan_prints(self) -> None:
        assert to_display_form("*PDT 1,5KG") == "PDT 1,5KG"

    def test_drops_the_truncation_marker(self) -> None:
        assert to_display_form("*BONNE MAMAN BISCUI..") == "BONNE MAMAN BISCUI"

    def test_keeps_an_abbreviation_dot(self) -> None:
        # The trailing dot of MPBIO YAOURT NAT. is an abbreviation mark, not noise.
        assert to_display_form("MPBIO YAOURT NAT.") == "MPBIO YAOURT NAT."


class TestComparisonForm:
    def test_strips_accents_because_most_tills_already_did(self) -> None:
        assert to_comparison_form("MANGUES SÉCHÉES") == "mangues sechees"

    def test_two_tills_printing_the_same_product_agree(self) -> None:
        assert to_comparison_form("Taboulé oriental") == to_comparison_form("TABOULE ORIENTAL")

    def test_monoprix_quote_separator_becomes_a_boundary(self) -> None:
        assert to_comparison_form('BIO"ENDIVE 400G.') == "bio endive 400g."


class TestQuantities:
    def test_plain_mass_is_normalised_to_grams(self) -> None:
        label = normalize_label("*PDT 1,5KG", retailer="auchan")
        (quantity,) = label.quantities
        assert quantity.kind is QuantityKind.MASS
        assert quantity.value == Decimal("1500.0")
        assert quantity.unit == "g"

    def test_multipack_keeps_its_shape(self) -> None:
        # 6X25CL is six bottles of 250 ml, not one of 1.5 l. Collapsing it would
        # lose the only thing that distinguishes a pack from a bottle.
        label = normalize_label("LAIT UHT 1/2ECREM.BBC U 6X25CL", retailer="super-u")
        (quantity,) = label.quantities
        assert quantity.kind is QuantityKind.VOLUME
        assert quantity.value == Decimal(250)
        assert quantity.pack_count == 6

    def test_size_then_pack_order_is_read_too(self) -> None:
        label = normalize_label("DANONE SKYR 825GX1", retailer="leclerc")
        (quantity,) = label.quantities
        assert quantity.value == Decimal(825)
        assert quantity.pack_count == 1

    def test_percentage_is_separated_from_the_designation(self) -> None:
        label = normalize_label("BEUR.PLAQ.PRESID.DX 82%MG 500G", retailer="super-u")
        kinds = {quantity.kind for quantity in label.quantities}
        assert kinds == {QuantityKind.MASS, QuantityKind.PERCENTAGE}
        assert "82" not in label.designation

    def test_sachet_count_carries_its_unit(self) -> None:
        label = normalize_label("INF.ELEPH.NUIT TRANQUIL.X20ST", retailer="super-u")
        (quantity,) = label.quantities
        assert quantity.kind is QuantityKind.COUNT
        assert quantity.value == Decimal(20)
        assert quantity.unit == "sachet"

    def test_designation_is_free_of_the_quantity_column(self) -> None:
        label = normalize_label("CHIPS PDT SEL ET POIVRE 150G", retailer="leclerc")
        assert label.designation == "chips pdt sel et poivre"

    def test_a_fraction_is_a_token_not_a_division(self) -> None:
        label = normalize_label("LAIT UHT 1/2ECREM.BBC U 6X25CL", retailer="super-u")
        assert "1/2" in label.words

    def test_a_count_glued_to_a_word_is_split(self) -> None:
        label = normalize_label("RIZ LONG CR REP,10MN 4SACH,500G", retailer="leclerc")
        assert "sach" in label.designation


class TestTruncation:
    def test_an_explicit_marker_is_a_fact(self) -> None:
        label = normalize_label("*AUCHAN POMMES DAUP..", retailer="auchan")
        assert label.truncation is TruncationHint.MARKER

    def test_reaching_the_column_width_is_an_inference(self) -> None:
        label = normalize_label("BARRES SSN CHOCOLA", retailer="carrefour-market")
        assert label.truncation is TruncationHint.WIDTH

    def test_no_signal_without_a_retailer(self) -> None:
        # Losing the width heuristic is the documented cost of not knowing the chain.
        label = normalize_label("BARRES SSN CHOCOLA")
        assert label.truncation is TruncationHint.NONE

    @pytest.mark.parametrize("raw", ["COURGETTE", "POIVRON VERT", "KAKI"])
    def test_a_short_line_is_not_suspected(self, raw: str) -> None:
        assert normalize_label(raw, retailer="intermarche").truncation is TruncationHint.NONE

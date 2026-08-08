"""What the expander must and must not do.

The most important tests here are the negative ones. Coverage is a nice-to-have;
never emitting a confident wrong reading is the requirement, because ADR-0009 puts
the allergen check downstream of this matching step.
"""

from __future__ import annotations

import pytest

from chaudron.domain.labels import Confidence, Verdict, expand_label

# Lines transcribed from the receipt photos referenced in lexicon.toml.
_REAL_LINES = [
    ("CHIPS PDT SEL ET POIVRE 150G", "leclerc"),
    ("*PDT 1,5KG", "auchan"),
    ("BEUR.PLAQ.PRESID.DX 82%MG 500G", "super-u"),
    ("YRT BREBIS NAT.BIO VRAI 2X125G", "super-u"),
    ("BARRES SSN CHOCOLA", "carrefour-market"),
    ("RANOU COLESLAW 300GR", "intermarche"),
    ("Croûtons à l'ail pou", "lidl"),
    ("MPVT E.T. COMPACT", "monoprix"),
]


class TestResolution:
    def test_the_case_the_whole_module_exists_for(self) -> None:
        # "PDT" and "pomme de terre" share no trigram. This is the step that no
        # index choice can replace.
        result = expand_label("CHIPS PDT SEL ET POIVRE 150G", retailer="leclerc")
        assert result.verdict is Verdict.RESOLVED
        assert result.best is not None
        assert result.best.text == "chips pomme de terre sel et poivre"
        assert result.best.confidence is Confidence.HIGH
        assert not result.requires_review

    def test_a_plain_french_label_passes_through_unchanged(self) -> None:
        result = expand_label("COURGETTE")
        assert result.verdict is Verdict.RESOLVED
        assert result.best is not None
        assert result.best.text == "courgette"
        assert not result.expanded

    def test_a_dotted_group_is_read_atom_by_atom(self) -> None:
        result = expand_label("BEUR.PLAQ.PRESID.DX 82%MG 500G", retailer="super-u")
        assert result.best is not None
        assert result.best.text == "beurre plaquette doux matiere grasse"

    def test_a_slash_prefix_restores_the_missing_word(self) -> None:
        result = expand_label("RICOLA S/SUC CITRON", retailer="intermarche")
        assert result.best is not None
        assert result.best.text == "sans sucre citron"

    def test_a_fraction_becomes_a_word(self) -> None:
        result = expand_label("LAIT 1/2 ECREME BIO BOUT 6X1L", retailer="leclerc")
        assert result.best is not None
        assert "demi ecreme" in result.best.text


class TestAmbiguityIsSurfacedNotArbitrated:
    def test_every_reading_is_returned(self) -> None:
        # SAV is "saveur" on a crouton line and "savon" on a soap line, and only the
        # caller knows which shelf it came from.
        result = expand_label("SAV.CHEVREFEUIL.4X100G", retailer="super-u")
        assert result.verdict is Verdict.AMBIGUOUS
        texts = {candidate.text for candidate in result.candidates}
        assert any(text.startswith("saveur") for text in texts)
        assert any(text.startswith("savon") for text in texts)

    def test_an_ambiguous_result_has_no_best_and_asks_for_review(self) -> None:
        result = expand_label("SAV.CHEVREFEUIL.4X100G", retailer="super-u")
        assert result.best is None
        assert result.requires_review

    def test_too_many_combinations_are_declared_rather_than_listed(self) -> None:
        # Enumerating 200 readings is a way of hiding the ambiguity in a long list.
        result = expand_label("SAV TART PRUN PAST SUP CHEV ST", retailer="super-u")
        assert result.verdict is Verdict.AMBIGUOUS
        assert result.candidates == ()
        assert any(len(token.readings) > 1 for token in result.tokens)


class TestNeverGuess:
    def test_an_unknown_abbreviation_is_left_alone_and_listed(self) -> None:
        result = expand_label("BARRES SSN CHOCOLA", retailer="carrefour-market")
        assert "ssn" in result.unresolved
        assert result.best is not None
        assert "ssn" in result.best.text

    def test_a_low_confidence_reading_asks_for_review(self) -> None:
        # PDT NOUV is the brief's own example, and NOUV is in the unverified block.
        result = expand_label("PDT NOUV 1KG")
        assert result.requires_review
        assert {c.confidence for c in result.candidates} == {Confidence.LOW}

    def test_a_retailer_scoped_entry_is_ignored_without_the_chain(self) -> None:
        with_chain = expand_label("LARDONS NATURE U 200G", retailer="super-u")
        without = expand_label("LARDONS NATURE U 200G")
        assert "u" in with_chain.brands
        assert without.brands == ()
        assert with_chain.best is not None
        assert without.best is not None
        assert "u" in without.best.text.split(" ")

    @pytest.mark.parametrize(("raw", "retailer"), _REAL_LINES)
    def test_no_reading_is_ever_empty(self, raw: str, retailer: str) -> None:
        result = expand_label(raw, retailer=retailer)
        assert all(candidate.text.strip() for candidate in result.candidates)

    def test_a_reading_never_claims_more_than_its_weakest_entry(self) -> None:
        result = expand_label("SUP DD", retailer="leclerc")
        assert all(c.confidence is Confidence.MEDIUM for c in result.candidates)


class TestTruncation:
    def test_a_cut_tail_is_reported_as_a_prefix_not_a_word(self) -> None:
        result = expand_label("PAIN MAIS CARREFOU", retailer="carrefour-market")
        assert result.truncated_tail == "carrefou"
        assert not result.is_complete

    def test_a_tail_the_lexicon_repaired_is_not_reported(self) -> None:
        result = expand_label("BARRES SSN CHOCOLA", retailer="carrefour-market")
        assert result.truncated_tail is None
        assert result.best is not None
        assert result.best.text.endswith("chocolat")

    def test_an_incomplete_designation_is_still_resolvable(self) -> None:
        # Truncation is ubiquitous; treating it as a blocker would send every
        # Carrefour line to review and the feature would be switched off.
        result = expand_label("BARRES SSN CHOCOLA", retailer="carrefour-market")
        assert result.verdict is Verdict.RESOLVED
        assert not result.requires_review
        assert not result.is_complete


class TestBrands:
    def test_an_own_brand_is_lifted_out_of_the_designation(self) -> None:
        result = expand_label("RANOU COLESLAW 300GR", retailer="intermarche")
        assert result.brands == ("ranou",)
        assert result.best is not None
        assert result.best.text == "coleslaw"

    def test_a_brand_confidence_does_not_lower_the_reading(self) -> None:
        # PAT is a medium-confidence Intermarché range; dropping it must not make
        # "bifidus nature" a medium-confidence reading.
        result = expand_label("PAT BIFIDUS NAT 12X1", retailer="intermarche")
        assert result.best is not None
        assert result.best.confidence is Confidence.HIGH

    def test_a_label_of_nothing_but_brand_resolves_to_nothing(self) -> None:
        result = expand_label("RANOU", retailer="intermarche")
        assert result.verdict is Verdict.UNRESOLVED
        assert result.candidates == ()
        assert result.requires_review


class TestPurity:
    def test_an_injected_lexicon_is_used_instead_of_the_default(self) -> None:
        from chaudron.domain.labels import load_lexicon

        toy = load_lexicon(
            '[meta]\nversion = "toy"\n\n'
            '[[token]]\nform = "PDT"\nexpansions = ["patate douce"]\n'
            'confidence = "high"\nkind = "skeleton"\nevidence = "test"\n'
        )
        result = expand_label("PDT 1KG", lexicon=toy)
        assert result.best is not None
        assert result.best.text == "patate douce"

    def test_expanding_twice_gives_the_same_answer(self) -> None:
        first = expand_label("YRT BREBIS NAT.BIO VRAI 2X125G", retailer="super-u")
        second = expand_label("YRT BREBIS NAT.BIO VRAI 2X125G", retailer="super-u")
        assert first == second

"""Invariants the data file must satisfy, and the malformations that must fail loudly.

The lexicon is edited by hand, by people who are not running the test suite when they
edit it. What protects it is that a bad file cannot load: an unreadable entry raises
at import of the default lexicon, which turns a silent wrong answer into a failed
deployment. These tests hold that door shut.
"""

from __future__ import annotations

import pytest

from chaudron.domain.labels import (
    Confidence,
    EntryKind,
    LexiconError,
    default_lexicon,
    load_lexicon,
)
from chaudron.domain.labels.lexicon import NON_DESIGNATION_KINDS

_MINIMAL = """
[meta]
version = "test"

[[token]]
form = "PDT"
expansions = ["pomme de terre"]
confidence = "high"
kind = "skeleton"
evidence = "openprices:60984"
"""

_DUPLICATE = """
[[token]]
form = "PDT"
expansions = ["patate douce"]
confidence = "low"
kind = "skeleton"
evidence = "openprices:60984"
"""


class TestShippedLexicon:
    def test_it_loads(self) -> None:
        lexicon = default_lexicon()
        assert lexicon.entries
        assert lexicon.version

    def test_it_is_memoised_rather_than_reparsed(self) -> None:
        assert default_lexicon() is default_lexicon()

    def test_every_entry_says_where_it_came_from(self) -> None:
        for entry in default_lexicon().entries:
            assert entry.evidence.strip(), f"{entry.form} has no evidence"

    def test_nothing_unverified_claims_confidence(self) -> None:
        # An entry no receipt backs must never outrank one that a receipt does.
        # This is the whole difference between a lexicon and a plausible-looking guess.
        for entry in default_lexicon().entries:
            if entry.evidence.startswith("assumed"):
                assert entry.confidence is Confidence.LOW, (
                    f"{entry.form} is assumed but claims {entry.confidence}"
                )

    def test_designation_entries_actually_expand_something(self) -> None:
        for entry in default_lexicon().entries:
            if entry.kind not in NON_DESIGNATION_KINDS:
                assert entry.expansions, f"{entry.form} expands to nothing"

    def test_brands_are_dropped_not_expanded(self) -> None:
        for entry in default_lexicon().entries:
            if entry.kind in NON_DESIGNATION_KINDS:
                assert not entry.expansions, f"{entry.form} is a brand but carries readings"

    def test_forms_are_stored_in_comparison_form(self) -> None:
        for entry in default_lexicon().entries:
            assert entry.form == entry.form.lower()
            assert entry.form == entry.form.strip()

    def test_the_single_letter_entry_is_retailer_scoped(self) -> None:
        # A one-letter form applied to the wrong chain silently deletes a word from
        # the designation. Only Système U prints a bare "U" as its own brand.
        for entry in default_lexicon().entries:
            if len(entry.form) == 1:
                assert entry.retailers, f"{entry.form!r} is unscoped and one character long"

    def test_the_verified_core_dominates_the_assumed_block(self) -> None:
        entries = default_lexicon().entries
        assumed = [e for e in entries if e.evidence.startswith("assumed")]
        assert len(assumed) * 10 < len(entries)

    def test_retailer_scoping_hides_an_entry_when_the_chain_is_unknown(self) -> None:
        lexicon = default_lexicon()
        assert lexicon.lookup("mr", retailer="leclerc")
        assert not lexicon.lookup("mr", retailer=None)
        assert not lexicon.lookup("mr", retailer="carrefour")


class TestLoaderRejects:
    def test_a_duplicate_form_in_the_same_scope(self) -> None:
        # Two entries for one form would make the reading order-dependent. The fix is
        # to merge the readings into one entry, which is what the error says.
        with pytest.raises(LexiconError, match="duplicate"):
            load_lexicon(_MINIMAL + _DUPLICATE)

    def test_an_unknown_confidence_level(self) -> None:
        with pytest.raises(LexiconError):
            load_lexicon(_MINIMAL.replace('"high"', '"pretty-sure"'))

    def test_an_unknown_kind(self) -> None:
        with pytest.raises(LexiconError):
            load_lexicon(_MINIMAL.replace('"skeleton"', '"vibes"'))

    def test_a_designation_entry_with_no_expansion(self) -> None:
        with pytest.raises(LexiconError, match="needs an expansion"):
            load_lexicon(_MINIMAL.replace('["pomme de terre"]', "[]"))

    def test_an_entry_with_no_evidence(self) -> None:
        with pytest.raises(LexiconError, match="evidence"):
            load_lexicon(_MINIMAL.replace('evidence = "openprices:60984"', 'evidence = "  "'))

    def test_a_missing_version(self) -> None:
        with pytest.raises(LexiconError, match="version"):
            load_lexicon(_MINIMAL.replace('version = "test"', ""))

    def test_an_empty_lexicon(self) -> None:
        with pytest.raises(LexiconError, match="at least one"):
            load_lexicon('[meta]\nversion = "test"\n')

    def test_invalid_toml(self) -> None:
        with pytest.raises(LexiconError, match="not valid TOML"):
            load_lexicon("[meta\nversion =")


class TestKinds:
    def test_every_kind_declared_is_reachable_from_the_data(self) -> None:
        # Not every kind has to be used, but an unused *designation* kind means the
        # classification in the docs has drifted from the file.
        used = {entry.kind for entry in default_lexicon().entries}
        assert EntryKind.TRUNCATION in used
        assert EntryKind.SKELETON in used
        assert EntryKind.INITIALISM in used
        assert EntryKind.QUALIFIER in used
        assert EntryKind.PACKAGING in used
        assert EntryKind.BRAND in used
        assert EntryKind.PRIVATE_LABEL in used

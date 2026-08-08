"""Reading French supermarket till-receipt labels.

``PDT NOUV 1KG`` and ``Pommes de terre nouvelles`` share no trigram and no lexeme.
No index -- trigram, full-text, vector or any combination -- recovers that; the hit
rate is decided before the search, by expanding the abbreviations. This package is
that step, and only that step: it turns a printed label into the French designations
it may stand for, with a confidence, and says "I don't know" rather than guessing.

Self-contained by design (ADR-0009 makes label-to-product matching a safety control,
so it must be testable without a database): no I/O beyond reading its own data file,
no network, no global mutable state. It is deliberately not called from anywhere yet.

    >>> from chaudron.domain.labels import expand_label
    >>> result = expand_label("CHIPS PDT SEL ET POIVRE 150G", retailer="leclerc")
    >>> result.best.text
    'chips pomme de terre sel et poivre'

The lexicon lives in ``lexicon.toml`` beside this module. See ``docs/label-lexicon.md``
for what it covers, what it does not, and how to add to it.
"""

from chaudron.domain.labels.expansion import (
    Expansion,
    ExpansionResult,
    TokenReading,
    Verdict,
    expand_label,
)
from chaudron.domain.labels.lexicon import (
    Confidence,
    EntryKind,
    Lexicon,
    LexiconEntry,
    LexiconError,
    default_lexicon,
    load_lexicon,
)
from chaudron.domain.labels.normalization import (
    NormalizedLabel,
    Quantity,
    QuantityKind,
    TruncationHint,
    normalize_label,
    to_comparison_form,
    to_display_form,
)

__all__ = [
    "Confidence",
    "EntryKind",
    "Expansion",
    "ExpansionResult",
    "Lexicon",
    "LexiconEntry",
    "LexiconError",
    "NormalizedLabel",
    "Quantity",
    "QuantityKind",
    "TokenReading",
    "TruncationHint",
    "Verdict",
    "default_lexicon",
    "expand_label",
    "load_lexicon",
    "normalize_label",
    "to_comparison_form",
    "to_display_form",
]

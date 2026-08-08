"""Hard dietary constraints: withheld before the call, re-checked after it.

This module is pure -- constants, frozen dataclasses and total functions. It has
no session, no household and no provider, which is what lets the one part of the
product with a physical failure cost be tested exhaustively without a mock
(ADR-0009).

Three mechanisms live here, and the difference between them is the whole design.

*The screen.* :func:`withhold_reason` decides whether one product may be shown to
the model at all. It reads ``allergens_risk`` -- the generated column that
carries **all fourteen** allergens when nobody has documented the product -- and
never ``allergens_contains``. An undocumented product is therefore withheld from
anybody with a declared allergy by the shape of the data, not by this function
having remembered that ``unknown`` exists.

*The resolver.* :func:`resolve` re-attaches an ingredient label the model wrote
to something the household actually owns. It is deliberately **stricter** than a
convenience matcher: an ingredient resolves only when every one of its content
tokens appears in the product's name, so ``beurre de cacahuète`` cannot resolve
to ``Beurre doux``. The set it resolves against is the inventory that was
*already screened*, so anything that resolves is safe by construction and
anything that does not resolve is refused rather than guessed at.

*The staples.* A recipe needs salt and oil, and no household stocks them as
catalogue rows. :data:`PANTRY_STAPLES` is a short, closed, hand-reviewed list,
each entry declaring the allergens it may carry and the infant rules it trips.
It is the only way an ingredient may resolve to something outside the inventory,
and adding an entry means declaring those two things -- there is no permissive
default.

What is deliberately **not** here: any notion of "probably fine". Every function
below answers "withhold" or "resolve" and nothing in between, because the
in-between is the state that gets somebody to hospital.
"""

from __future__ import annotations

import enum
import re
import unicodedata
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

from chaudron.domain.models import (
    AgeBand,
    Allergen,
    AllergenDataState,
    Diet,
    InfantRiskKind,
    InfantTexture,
    PnnsMarker,
)

__all__ = [
    "ALLERGEN_LABELS",
    "DIET_EXCLUDED_MARKERS",
    "INFANT_BANDS",
    "MAX_FREE_TEXT_PREFERENCES",
    "PANTRY_STAPLES",
    "HouseholdConstraints",
    "InfantRule",
    "MealTemperature",
    "PantryStaple",
    "Person",
    "ProductFacts",
    "Resolution",
    "StockIndex",
    "StockLine",
    "WithholdReason",
    "content_tokens",
    "normalise",
    "staples_allowed_for",
    "union_of",
    "withhold_reason",
]


# --------------------------------------------------------------------------- #
# Vocabularies
# --------------------------------------------------------------------------- #


class MealTemperature(enum.StrEnum):
    """A *preference*, transmitted to the model and never applied as a filter.

    Nothing in the catalogue says whether a recipe is served cold, so there is no
    predicate to write. Contract 4ter is explicit that a divergence between what
    was asked and what the model self-declares is *displayed*, not prevented.
    """

    ANY = "any"
    HOT = "hot"
    COLD = "cold"


#: The regulated names, in French, as annex II of EU 1169/2011 publishes them.
#: Used to build the one sentence the interface is allowed to show about
#: allergens, which the server writes and the client renders verbatim -- the
#: client composing its own from a list of codes is how "sans arachide" gets on
#: screen (ADR-0009).
ALLERGEN_LABELS: Final[Mapping[Allergen, str]] = {
    Allergen.GLUTEN: "gluten",
    Allergen.CRUSTACEANS: "crustacés",
    Allergen.EGGS: "œufs",
    Allergen.FISH: "poissons",
    Allergen.PEANUTS: "arachides",
    Allergen.SOYBEANS: "soja",
    Allergen.MILK: "lait",
    Allergen.NUTS: "fruits à coque",
    Allergen.CELERY: "céleri",
    Allergen.MUSTARD: "moutarde",
    Allergen.SESAME: "sésame",
    Allergen.SULPHITES: "sulfites",
    Allergen.LUPIN: "lupin",
    Allergen.MOLLUSCS: "mollusques",
}

#: The bands that make somebody a young child for the purposes of the reference
#: table. ``CHILD`` is in it because one official rule -- the choking one --
#: extends to five years and the schema has no finer band.
INFANT_BANDS: Final[frozenset[AgeBand]] = frozenset(
    {
        AgeBand.INFANT_4_6M,
        AgeBand.INFANT_6_9M,
        AgeBand.INFANT_9_12M,
        AgeBand.INFANT_12_36M,
    }
)

#: Which food-group markers a diet excludes. Expressed on the markers already
#: resolved at ingestion rather than on category tags, so this table and the
#: weekly balance read the same classification -- a second answer to "is this a
#: fish" would disagree with the first within a month (``domain/dietary.py``).
#:
#: The absence of a marker is *not* evidence of compliance: a product nobody has
#: categorised resolves to no marker at all and is therefore not withheld here.
#: That is why the diet is also enforced after the call, where an ingredient the
#: model invented cannot resolve to anything and invalidates the suggestion. The
#: pre-filter is the cheap half; the post-check is the guarantee.
DIET_EXCLUDED_MARKERS: Final[Mapping[Diet, frozenset[PnnsMarker]]] = {
    Diet.OMNIVORE: frozenset(),
    Diet.PESCATARIAN: frozenset(
        {PnnsMarker.RED_MEAT, PnnsMarker.POULTRY, PnnsMarker.PROCESSED_MEAT}
    ),
    Diet.VEGETARIAN: frozenset(
        {
            PnnsMarker.RED_MEAT,
            PnnsMarker.POULTRY,
            PnnsMarker.PROCESSED_MEAT,
            PnnsMarker.FISH,
            PnnsMarker.OILY_FISH,
        }
    ),
    Diet.VEGAN: frozenset(
        {
            PnnsMarker.RED_MEAT,
            PnnsMarker.POULTRY,
            PnnsMarker.PROCESSED_MEAT,
            PnnsMarker.FISH,
            PnnsMarker.OILY_FISH,
            PnnsMarker.DAIRY,
            PnnsMarker.EGGS,
        }
    ),
}

#: How restrictive each diet is. Used to take the union of a group's
#: constraints: cooking one meal for a vegan and an omnivore means cooking vegan.
_DIET_RANK: Final[Mapping[Diet, int]] = {
    Diet.OMNIVORE: 0,
    Diet.PESCATARIAN: 1,
    Diet.VEGETARIAN: 2,
    Diet.VEGAN: 3,
}

#: Same idea for texture: the youngest mouth at the table sets the stage.
_TEXTURE_RANK: Final[Mapping[InfantTexture, int]] = {
    InfantTexture.SMOOTH: 0,
    InfantTexture.SOFT_PIECES: 1,
    InfantTexture.PIECES: 2,
}

#: How many free-text preferences travel to the model. A bound rather than a
#: guess: this text reaches a prompt, and an unbounded number of lines can crowd
#: the instructions out of a small model's window without a single instruction
#: word (``infra/llm/prompts.py``).
MAX_FREE_TEXT_PREFERENCES: Final = 8


# --------------------------------------------------------------------------- #
# People, and the union of what they cannot eat
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Person:
    """One eater, as the constraint layer needs them.

    Everything below ``display_name`` is health data (GDPR article 9), and for an
    infant a minor's. It is never logged, never put in an error message and never
    interpolated into a prompt: the model is told what it may cook *with*, never
    who it is cooking for.
    """

    id: uuid.UUID
    display_name: str
    age_band: AgeBand
    diet: Diet
    allergens: frozenset[Allergen]
    infant_texture: InfantTexture | None
    free_text_restrictions: str | None


@dataclass(frozen=True, slots=True)
class HouseholdConstraints:
    """The union of what the selected people cannot eat.

    A union rather than a per-household setting: a home where a vegetarian, a
    nut-allergic adult and a six-month-old all live is the normal case, and a
    single household-wide set would force everybody to eat without nuts forever
    until they gave up and unticked the box (ADR-0009).
    """

    members: tuple[Person, ...]
    excluded_allergens: frozenset[Allergen]
    diet: Diet
    infant_texture: InfantTexture | None
    age_bands: frozenset[AgeBand]
    #: Free-text restrictions, unattributed. Third class of contract 4bis: passed
    #: to the model as a preference, never as a filter, because nothing in the
    #: catalogue says which products contain coriander.
    preferences: tuple[str, ...]

    @property
    def excluded_markers(self) -> frozenset[PnnsMarker]:
        return DIET_EXCLUDED_MARKERS[self.diet]

    @property
    def has_infant(self) -> bool:
        return bool(self.age_bands & INFANT_BANDS)


def union_of(people: Iterable[Person]) -> HouseholdConstraints:
    """Fold a group of eaters into one set of constraints, strictest wins."""
    members = tuple(people)
    diet = max(
        (person.diet for person in members),
        key=lambda value: _DIET_RANK[value],
        default=Diet.OMNIVORE,
    )
    textures = [person.infant_texture for person in members if person.infant_texture is not None]
    texture = min(textures, key=lambda value: _TEXTURE_RANK[value], default=None)
    allergens: set[Allergen] = set()
    for person in members:
        allergens |= person.allergens
    preferences = tuple(
        text
        for text in ((person.free_text_restrictions or "").strip() for person in members)
        if text
    )[:MAX_FREE_TEXT_PREFERENCES]
    return HouseholdConstraints(
        members=members,
        excluded_allergens=frozenset(allergens),
        diet=diet,
        infant_texture=texture,
        age_bands=frozenset(person.age_band for person in members),
        preferences=preferences,
    )


# --------------------------------------------------------------------------- #
# The infant rules, as loaded from the reference table
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class InfantRule:
    """One row of ``infant_food_restriction``, applied exactly like an allergen.

    The rule is a row and not an ``if`` on purpose: a safety control scattered
    across services is one a refactor can drop with no test noticing, and this
    list has to be reviewable by somebody who does not read Python.
    """

    rule_code: str
    label: str
    risk: InfantRiskKind
    applies_to_bands: frozenset[AgeBand]
    category_tags: frozenset[str]
    name_patterns: tuple[str, ...]
    statement: str
    source_url: str

    def applies_to(self, bands: frozenset[AgeBand]) -> bool:
        return bool(self.applies_to_bands & bands)


# --------------------------------------------------------------------------- #
# Normalisation and tokens
# --------------------------------------------------------------------------- #

#: Words that carry no identity in a French food label. Dropping them is what
#: makes "crème fraîche" and "crème" comparable; keeping the *content* words is
#: what stops "beurre de cacahuète" from resolving to "beurre".
_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "a",
        "au",
        "aux",
        "avec",
        "d",
        "de",
        "des",
        "du",
        "en",
        "et",
        "l",
        "la",
        "le",
        "les",
        "ou",
        "pour",
        "un",
        "une",
    }
)

_TOKEN_SPLIT: Final = re.compile(r"[^0-9a-z]+")

#: Ligatures Unicode does not decompose. ``œ`` has no canonical *or*
#: compatibility decomposition, so "œufs" and "oeufs" survive NFKD as two
#: different words -- and a model writes both. Spelled out here because it is an
#: orthographic equivalence and not a loosening of the matcher: it makes two
#: spellings of one word compare equal, and cannot make two words compare equal.
_LIGATURES: Final[Mapping[str, str]] = {"œ": "oe", "æ": "ae"}


def normalise(text: str) -> str:
    """Case-, accent- and ligature-folded form, so "Œufs" and "oeufs" are one word."""
    folded = text.casefold()
    for ligature, expansion in _LIGATURES.items():
        folded = folded.replace(ligature, expansion)
    decomposed = unicodedata.normalize("NFKD", folded)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(stripped.split())


def content_tokens(label: str) -> frozenset[str]:
    """The words that identify a food, with the grammar and the numbers removed.

    Numbers go because "200 g de crème" and "crème" name the same thing; single
    letters go because they are elided articles; stopwords go for the same
    reason. Everything else stays, including the word that turns a butter into a
    peanut butter -- which is the token this whole function exists to preserve.
    """
    return frozenset(
        token
        for token in _TOKEN_SPLIT.split(normalise(label))
        if len(token) > 1 and token not in _STOPWORDS and not token.isdigit()
    )


def _pattern_matches(pattern: str, label: str) -> bool:
    """Whether a reference-table pattern occurs at a word start in ``label``.

    Anchored on a word boundary rather than matched anywhere, so "pain" does not
    fire on "copain"; not anchored at the *end*, so "raisin" still fires on
    "raisins secs". The residual over-matching -- "pâté" on "pâtes" -- withholds
    a food from a two-year-old who could have had it, which is the direction the
    reference table declares for itself: a false withholding costs a suggestion,
    a missed one costs a hospital visit.
    """
    normalised = normalise(pattern)
    if not normalised:
        return False
    return re.search(rf"\b{re.escape(normalised)}", normalise(label)) is not None


# --------------------------------------------------------------------------- #
# The screen
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProductFacts:
    """What the screen needs to know about one catalogue entry.

    ``allergens_risk`` has no default and never will: a default would be an
    empty set, an empty set reads as "carries no risk", and that is the sentence
    this entire feature exists to make unsayable. The caller must have read the
    generated column.
    """

    product_id: uuid.UUID
    name: str
    allergen_state: AllergenDataState
    allergens_risk: frozenset[Allergen]
    pnns_markers: frozenset[PnnsMarker]
    category_tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StockLine:
    """One lot of the household's stock, with everything the screen reads.

    ``expires_on`` is the *effective* date and not the printed one:
    ``min(best_before, opened_at + shelf_life_guideline.opened_days)``, computed
    by the query that builds these lines (``domain/shelf_life.py``). Only one of
    the two is carried, because every reader here asks the same question -- how
    urgent is this lot -- and a second date beside it is a second answer somebody
    eventually picks by mistake. What is printed on the packaging belongs to the
    inventory screen, which reads it from its own projection.
    """

    inventory_item_id: uuid.UUID
    product: ProductFacts
    quantity: str
    unit: str
    expires_on: date | None

    def days_left(self, today: date) -> int | None:
        return None if self.expires_on is None else (self.expires_on - today).days


class WithholdReason(enum.StrEnum):
    """Why one product never reached the model. Drives the wording, not the rule."""

    ALLERGEN = "allergen"
    DIET = "diet"
    INFANT_RULE = "infant_rule"


def withhold_reason(
    facts: ProductFacts,
    constraints: HouseholdConstraints,
    rules: Sequence[InfantRule],
) -> WithholdReason | None:
    """Whether this product is withheld from the inventory sent to the model.

    ``None`` means it may be shown. The order of the tests is the order of the
    costs: an allergen first, because it is the only one whose failure is
    measured in a hospital admission.

    ``rules`` must already be narrowed to the bands present at the table; passing
    every rule would withhold honey from a household of adults.
    """
    if constraints.excluded_allergens & facts.allergens_risk:
        return WithholdReason.ALLERGEN
    if constraints.excluded_markers & facts.pnns_markers:
        return WithholdReason.DIET
    if any(_rule_matches(rule, facts) for rule in rules):
        return WithholdReason.INFANT_RULE
    return None


def _rule_matches(rule: InfantRule, facts: ProductFacts) -> bool:
    """Dual matching, both halves inclusive (``domain/models.py``).

    ``category_tags`` is the exact half and covers anything the catalogue knows.
    ``name_patterns`` is the half that catches the "miel de châtaignier" somebody
    typed in by hand, which carries no category at all.
    """
    if rule.category_tags & {tag.strip().lower() for tag in facts.category_tags}:
        return True
    return any(_pattern_matches(pattern, facts.name) for pattern in rule.name_patterns)


# --------------------------------------------------------------------------- #
# Pantry staples
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PantryStaple:
    """Something a recipe may use that no household stocks as a catalogue row.

    The only escape hatch in the post-call check, and it is deliberately narrow:
    an entry declares the allergens it may carry and the infant rules it trips,
    so adding one is a decision rather than an omission. Matching is on the
    **exact** token set -- "huile d'olive" resolves, bare "huile" does not,
    because bare "huile" could be peanut oil.
    """

    label: str
    tokens: frozenset[str]
    allergens: frozenset[Allergen]
    infant_rule_codes: frozenset[str]


def _staple(label: str, *, infant_rules: frozenset[str] = frozenset()) -> PantryStaple:
    return PantryStaple(
        label=label,
        tokens=content_tokens(label),
        # None of the six carries one of the fourteen. Stated per entry rather
        # than assumed for the list, so a seventh entry has to say so.
        allergens=frozenset(),
        infant_rule_codes=infant_rules,
    )


#: Six entries, and the omissions are the interesting part. "Huile" alone is
#: absent because it is ambiguous -- peanut and sesame oil are both ordinary
#: French pantry items and both are on the regulated list. "Beurre", "farine" and
#: "lait" are absent because they are milk, gluten and milk: they are real
#: products, they belong in the inventory, and the screen has to see them.
PANTRY_STAPLES: Final[tuple[PantryStaple, ...]] = (
    _staple("eau"),
    # Salt trips the ANSES "no added salt before three years" rule, so a
    # suggestion that seasons a purée is invalidated when a toddler is at the
    # table -- which is the rule working, not the resolver failing.
    _staple("sel", infant_rules=frozenset({"added_salt"})),
    _staple("gros sel", infant_rules=frozenset({"added_salt"})),
    _staple("poivre"),
    _staple("vinaigre"),
    _staple("huile olive"),
    _staple("huile tournesol"),
)


def staples_allowed_for(
    constraints: HouseholdConstraints, rules: Sequence[InfantRule]
) -> tuple[PantryStaple, ...]:
    """The staples this table may use, after the same two screens as a product."""
    blocked = {rule.rule_code for rule in rules}
    return tuple(
        staple
        for staple in PANTRY_STAPLES
        if not (staple.allergens & constraints.excluded_allergens)
        and not (staple.infant_rule_codes & blocked)
    )


# --------------------------------------------------------------------------- #
# The resolver
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Resolution:
    """What one ingredient label turned out to be.

    ``line is None and staple is None`` is the answer that invalidates a
    suggestion. It is not an error state to be recovered from: an ingredient this
    application cannot name is one it cannot vouch for, and the correct behaviour
    is to drop the whole suggestion rather than to serve it with a caveat
    (ADR-0009).
    """

    label: str
    line: StockLine | None
    staple: PantryStaple | None

    @property
    def resolved(self) -> bool:
        return self.line is not None or self.staple is not None


class StockIndex:
    """Resolves ingredient labels against one screened inventory.

    A class rather than a function because the product names are tokenised once
    per request instead of once per ingredient: ten suggestions of sixty
    ingredients against three hundred lots is a hundred and eighty thousand
    comparisons, and re-normalising Unicode inside that loop is measurable.

    The matching rule is one line: **every content token of the ingredient must
    appear in the product's name**. It is asymmetric on purpose.

    Generic ingredient, specific product -- "lait" against "Lait de soja bio" --
    resolves, and is safe because the lines have already been screened: whatever
    is in them is something these eaters may have.

    Specific ingredient, generic product -- "beurre de cacahuète" against
    "Beurre doux" -- does **not** resolve, because "cacahuete" is nowhere in the
    product. That is the direction that matters: a permissive matcher would
    certify a peanut butter as butter.

    The first match wins, and the lines arrive ordered by urgency, so an
    ambiguous label attaches to whatever is closest to spoiling.
    """

    __slots__ = ("_entries", "_staples")

    def __init__(self, lines: Sequence[StockLine], staples: Sequence[PantryStaple]) -> None:
        self._entries = [(content_tokens(line.product.name), line) for line in lines]
        self._staples = tuple(staples)

    def resolve(self, label: str) -> Resolution:
        tokens = content_tokens(label)
        if not tokens:
            return Resolution(label=label, line=None, staple=None)
        for name_tokens, line in self._entries:
            if tokens <= name_tokens:
                return Resolution(label=label, line=line, staple=None)
        for staple in self._staples:
            if tokens == staple.tokens:
                return Resolution(label=label, line=None, staple=staple)
        return Resolution(label=label, line=None, staple=None)

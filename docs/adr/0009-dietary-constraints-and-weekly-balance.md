# 0009. Dietary constraints and weekly balance

## Status

Accepted — 2026-08-04.

## Context

Recipe suggestions have to account for what household members can and want to
eat — allergies, diet, texture for an infant — and aim for balance across the
week rather than dish by dish.

Three facts frame the decision.

**There is currently no allergen mechanism at all.** Finding SEC-031 has been
open since the scoping review. `docs/architecture.md` and
`docs/security-model.md` both repeat that allergen information coming from a
model must never be authoritative — and the audit notes that this sentence,
written in a document, does not survive as far as the recipe screen. What is
needed is a mechanism, not an instruction.

**An allergen error has physical consequences.** It is the only class of defect
in this product whose cost is measured neither in money nor in trust. It calls
for different treatment from the rest.

**Infant feeding carries threshold risks.** Honey before twelve months exposes
the child to infant botulism; a round, firm food is a choking hazard. These
risks do not degrade gracefully: they are absent, then severe.

## Decision

### Allergens are a filter, never an instruction

A "no peanuts" instruction in a prompt is a polite request addressed to a
component that is allowed to get it wrong. The chosen mechanism asks the model
for nothing:

1. **At ingestion** — Open Food Facts's `allergens_tags` and `traces_tags` are
   captured and normalised onto the **14 allergens subject to mandatory
   declaration** under EU Regulation 1169/2011.
2. **Before the call** — products carrying an allergen declared by an affected
   member are **removed from the inventory sent**. The model cannot suggest what
   it never saw.
3. **After the call** — every returned ingredient is re-matched against the
   catalogue. An ingredient that does not resolve to a known product, or that
   resolves to a product carrying the allergen, **invalidates the entire
   suggestion**.

### "Unknown" is not "allergen-free"

This is the point that decides whether this feature protects or kills.

Open Food Facts is a wiki: a product may have no allergen data at all. Three
states are distinguished and **never conflated**: `contains`, `may_contain`
(traces), `unknown`. An `unknown` product is treated as `may_contain` for
filtering purposes, and the interface displays it as **unverified** — never in
green, never with a tick.

A household must never be able to read "this recipe is peanut-free" when the
truth is "none of the products declared peanuts". The wording carried by the
interface is therefore negative and situated: "no allergen declared among the
identified products", together with the count of products with no data.

### Balance is computed by the application, not by the model

The model writes recipes; it does not do dietetics. The application computes the
gaps against the guidelines over a rolling 7 days and passes them on as a stated
constraint ("one fish and two pulses missing").

**The source for what has been eaten is `stock_movement` with reason
`consumed`.** What left the stock got cooked. No separate log is created: a log
the user has to fill in will not get filled in.

Reference used: the **Programme National Nutrition Santé (PNNS) guidelines**
(France). Public, expressed as weekly frequencies, hence verifiable and
displayable in plain language — "you are one fish short this week" rather than
an opaque score nobody can challenge.

### Hard constraints and soft constraints

| Constraint | Kind | Behaviour when stock does not allow it |
|---|---|---|
| Allergen | **Hard** | We do not suggest. We say why. |
| Diet (vegetarian, vegan) | **Hard** | Same |
| Infant texture | **Hard** | Same |
| Age-forbidden | **Hard** | Same |
| Weekly balance | **Soft** | We suggest, and state the gap |

This is the answer to the "fries every day if that is all there is in the
freezer" case: balance is an ordering of preference, not a condition. When stock
does not allow it to be respected, the application says so instead of inventing
ingredients that are not there.

### Constraints are held per member, not per household

A household with a vegetarian, someone allergic to tree nuts and an infant
living under one roof is the normal case. A single set per household would force
the permanent union of every constraint — so everybody would eat nut-free all
the time, and people would end up unticking the box.

The suggestion panel asks **who we are cooking for**, and applies the union of
the selected members' constraints.

### Infant mode rests on a table, never on the model

Three textures — `smooth`, `soft_pieces`, `pieces` — and a deterministic table
of foods forbidden by age band, applied exactly like an allergen: removed before
the call, invalidated after.

An infant's age is **personal data about a minor**. It is stored as a band
(`age_band`), never as a date of birth: the band is enough to apply the rules
and carries far less information.

The interface carries a warning that **cannot be dismissed**: these rules do not
replace the advice of a health professional, and introducing solid foods is to
be discussed with a paediatrician.

## Consequences

### Positive

- The one defect in this product with physical consequences stops being
  untreated.
- The mechanism does not depend on model quality: it works identically with
  Claude Opus 5 and with a local 0.5-billion-parameter model — which is what
  ADR-0007's BYOK promise demands.
- Balance builds on data already collected, with no user effort.
- The PNNS guidelines are public: a user can check what the application asserts.

### Negative

- **Filtering shrinks the inventory sent, and therefore the quality of the
  suggestions.** A household with several allergies will see fewer proposals,
  and sometimes none. That is the correct behaviour, and it will be experienced
  as a regression.
- **Open Food Facts's allergen coverage has not been measured.** The share of
  `unknown` products determines how useful the mechanism actually is, and it has
  not been established (`docs/technical-notes-scanning.md` notes that
  completeness could not be measured because of the rate limit).
- **Perceived responsibility increases.** An application that shows an
  "allergies" checkbox is understood as protecting, however careful the wording.
  The warning reduces the misunderstanding, it does not remove it.
- **Infant age bands are health data about a minor**, which raises the GDPR
  obligations (article 9): minimisation, retention, erasure.
- Matching an ingredient label returned by the model to a catalogue product is
  an open problem (`docs/technical-notes-ingestion.md` §7) and becomes here a
  **safety control**, not a convenience.

## Rejected alternatives

- **Passing the constraints as a prompt instruction.** The simplest
  implementation, and the only one that fails silently. Rejected without
  discussion for allergens; what holds for them holds for the rest, otherwise
  two mechanisms coexist and the wrong one gets used by mistake.
- **Tracking macronutrients and calories** rather than frequencies. Rejected: it
  assumes complete nutritional data per product, whose completion rate has not
  been measured, and a wrong figure displayed to two decimal places inspires
  confidence it does not deserve.
- **A separate meal log.** Rejected: `stock_movement` already carries the
  information, and one more log to fill in would not get filled in.
- **Storing the infant's date of birth** to refine the rules. Rejected: the band
  is enough, and the date is markedly more identifying.
- **Treating "no allergen data" as "allergen-free".** Rejected: that is the
  behaviour that puts someone in hospital.

## Revisiting

- Measure the share of products with no allergen data against a local Open Food
  Facts dump before any public opening. Past a threshold still to be defined,
  the mechanism becomes more frustrating than useful and will have to be
  complemented by assisted manual entry.
- Re-examine the PNNS guidelines if they are revised, and have persisted
  suggestions carry the version of the table.
- Reopen the ingredient matching question as soon as a false match is observed:
  it is the weak link in the post-call check.

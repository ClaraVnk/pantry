# Receipt label expansion lexicon

**Status**: v1, measured. — **Date**: 2026-08-04
**Code**: `backend/src/chaudron/domain/labels/` — **Data**: `lexicon.toml` (same folder)
**Evaluation set**: `backend/tests/labels/eval_corpus.toml`

---

## 1. Why this document exists

`PDT NOUV 1KG` and `Pommes de terre nouvelles` **share no trigram and no lexeme
after stemming**. No choice of index — trigram, full text, vector, or any fusion
of the three — makes up for that. The success rate of label → product matching is
decided **before** the search, in abbreviation expansion
([`technical-notes-ingestion.md`](technical-notes-ingestion.md) §3.6 and
decision no. 9 in §7).

And since [ADR-0009](adr/0009-dietary-constraints-and-weekly-balance.md), that
matching is no longer a convenience: it is a **safety control**. Allergen
validation runs *after* the model call, by re-matching each returned ingredient
against the catalogue. A missed match blocks a perfectly good recipe; a **wrong**
match makes you read the allergen sheet of a food nobody is going to eat.

The technical note also establishes that **no public source documents these
abbreviations**: no French receipt dataset, no published truncation policy from
the retailers, no free-software project. This lexicon is therefore built
empirically, and it is the only genuinely defensible asset in the project.

---

## 2. The design principle: the asymmetry

> **A wrong expansion is more dangerous than no expansion at all.**

Downstream, “unidentified” triggers a human review: that is visible and tedious.
A confident wrong match crosses the allergen control in silence: that is
invisible and physical. The two do not offset each other and do not average out.

The whole module follows from this:

| Situation | What the module returns |
|---|---|
| A single reading, confidence ≥ `medium` | `verdict = RESOLVED`, `requires_review = False` |
| Several possible readings | `verdict = AMBIGUOUS`, **all** returned, `requires_review = True` |
| Too many combinations to enumerate honestly | `AMBIGUOUS` with no candidate, per-token alternatives in `tokens` |
| A single reading but `low` confidence | `RESOLVED` + `requires_review = True` |
| Nothing readable | `UNRESOLVED`, `requires_review = True` |

Ambiguity is **never arbitrated** by the module: `SAV` means *saveur* on a
crouton line and *savon* on a soap-bar line — both are attested on the same
Super U receipt. Only the caller has the aisle, the barcode or the user to settle
it. Dropping a reading “to keep things tidy” is the most expensive bug you can
introduce here.

**Rule for the allergen path**: require `not requires_review` **and** treat
`is_complete == False` as “partial designation” — a match on a truncated
designation must not be accepted at low similarity.

---

## 3. Why TOML

The lexicon is **data**, not code: it will grow through successive corrections,
contributed by people who are not developers, and reviewed as diffs.

| Format | Rejected because |
|---|---|
| Python constants | Editing the lexicon would become an act of development. A dealbreaker. |
| JSON | **No comments.** Yet an entry's provenance lives in the comment; an entry nobody can trace is an entry nobody dares delete. |
| YAML | External dependency (`pyyaml`), and a syntax where `NO` means `false`. |
| CSV | No structure for multiple readings, no comments. |
| Database | The module must be testable without starting anything, and the lexicon must be versioned alongside the code that reads it. |

**TOML chosen**: parsed by `tomllib` (standard library, no dependency added),
carries comments, one entry per block, line-by-line readable diffs.

The file is **validated at load time**: duplicate form within the same scope,
unknown confidence level, designation entry with no reading, entry with no
provenance → `LexiconError`. A broken file fails the deployment instead of
silently producing a wrong answer.

---

## 4. What the lexicon covers

**195 entries**, of which 129 `high`, 58 `medium`, 8 `low`. 25 entries are
restricted to one retailer. 9 entries carry several readings.

| Class (`kind`) | Count | Attested examples |
|---|---|---|
| `truncation` — the word is cut | 80 | `LEGU`→légume, `ROUG`→rouge, `BISCUI`→biscuit, `VITROCER`→vitrocéramique |
| `brand` — manufacturer brand | 47 | `PRESID`→Président, `ENTRMONT`→Entremont, `ELEPH`→Éléphant |
| `private_label` — retailer range | 25 | `MR` (Leclerc), `TOP BUDGET` (Intermarché), `AUC` (Auchan), `UDNR` (U) |
| `skeleton` — vowels dropped | 21 | `PDT`→pomme de terre, `YRT`→yaourt, `BLC`→blanc, `DX`→doux, `RGE`→rouge |
| `initialism` — acronym | 9 | `VPF`→viande de porc française, `SSA`→sans sucres ajoutés, `MG`→matière grasse, `NTAR`→non traité après récolte |
| `packaging` — packaging format | 7 | `BARQ`→barquette, `SACH`→sachet, `PLAQ`→plaquette, `BTE`→boîte |
| `qualifier` — property | 6 | `1/2`→demi, `NAT`→nature, `ENT`→entier, `LIQ`→liquide |

Three slash-prefix rules: `S/`→sans, `P/`→pur, `H/`→huile.

**What is deliberately NOT in the lexicon**: `BIO`, `UHT`, `AOP`, `IGP`, `VRAC`,
`NAT` in the Open Food Facts sense… these are written identically in the
catalogue. Expanding them does not improve matching and lengthens the lexicon for
nothing. **We only record what differs from the ordinary French form.**

### Normalisation

Two forms are produced, and they must **never** be confused:

- **`comparison`** — lowercased, accents stripped, whitespace normalised. This is
  the lexicon key and what a similarity engine must be given. Lossy on purpose:
  most tills already print without accents, and distinguishing
  `MANGUES SÉCHÉES` from `MANGUES SECHEES` would mean losing half the matches.
- **`display`** — the line as printed, minus the artefacts (Auchan's VAT `*`,
  the `..` truncation marker, doubled spaces). **Case and accents intact.** What
  a till in unaccented capitals threw away cannot be recovered: reconstructing
  would give `PRESID` → *preside*, not *Président*.

Quantity is extracted from the designation: mass and volume reduced to grams and
millilitres, multipacks kept in their own form (`6X25CL` = six units of 250 ml,
not 1.5 l), percentages separated, counts (`X6`, `6ST`) isolated.

---

## 5. What the lexicon does not cover

Classes of abbreviation **identified but not handled**:

1. **Positional mid-word truncation.** Carrefour cuts at 18 characters with no
   marker: `BARRES SSN CHOCOLA`, `PAIN MAIS CARREFOU`, `YAGISAWA-LA LIBRAI`.
   These fragments will never be in a dictionary — the cut is **positional, not
   lexical**. The module flags them (`truncated_tail`) so that a matcher can
   treat them as a prefix (`chocola%`); it does not repair them. This is the
   biggest remaining reservoir of recall.
2. **Multi-buys and promotions**: `LOT2`, `LOT 3 PATES`, `2+1 GRATUIT`. Not
   modelled.
3. **Weighed lines**: `MANDARINE` followed by `0,480 kg X 3,10EUR/kg` on the next
   line. The module handles one line at a time and does not stitch the two back
   together.
4. **Aisle headers** (`>>>> CREMERIE L.S.`, `BVP`, `TRAITEUR LS UVCI`). They must
   be filtered upstream; expanding them would make no sense.
5. **Internal codes** (`FE`, `P&C`, `RLX`, `K1K`, `TDM`, `SC CO`): not
   identifiable without retailer documentation. They surface in `unresolved`.
6. **Contextual disambiguation.** `CROUT` means *croûton* in
   `CROUT.AIL&FROMAGE` and *croûte* in `PATE EN CROUT SUP`, on the same receipt.
   The module knows only one reading and therefore gets the second one wrong —
   this is one of the two errors measured in §7.

### Known errors, owned

| Real line | Expected | Returned | Cause |
|---|---|---|---|
| `PATE EN CROUT SUP,FR C NRT,200G` | croûte | **croûton** | `CROUT` has two senses, only one is in the lexicon |
| `PH LT ALOE 4 RLX` | (do nothing) | **lait** | `LT` is part of a brand name here |

These two errors are **named in a test**
(`test_the_known_wrong_expansions_are_still_only_these_two`) so that a third one
cannot appear unnoticed.

---

## 6. What was verified, and what was assumed

### Verified against photos of real receipts

Eight French receipts, photographed by contributors, published by **Open Prices**
(Open Food Facts) and read one by one. Every lexicon entry carries in its
`evidence` field the receipt reference and the exact line.

| Receipt | Retailer | Observed format |
|---|---|---|
| `openprices:23005` | Intermarché Contact | capitals, unaccented, column ~20 |
| `openprices:8932` | Super U | capitals, column ~30, aisle headers |
| `openprices:111936` | Carrefour Market | **quantity first**, column 18, mid-word cut |
| `openprices:60947` | E.Leclerc (paperless) | capitals, column ~31 |
| `openprices:60984` | Auchan | capitals, explicit `..` marker, VAT `*` |
| `openprices:16595` | Monoprix | column 18, `"` as separator |
| `openprices:122557` | U Express | capitals, abbreviation dots everywhere |
| `openprices:19960` | Lidl | **mixed case, accents preserved** |

Viewable at `https://prices.openfoodfacts.org/api/v1/proofs/<id>`.

**Three findings that changed the design**:

- **Lidl standardises neither case nor accents.** The assumption “a receipt label
  is always in unaccented capitals” is false. Hence the strict separation between
  display form and comparison form.
- **Carrefour and Monoprix cut mid-word, with no marker.** Truncation is
  therefore not a dictionary problem; it is a prefix problem.
- **Auchan marks its truncations (`..`), Carrefour does not.** The truncation
  signal is therefore sometimes a fact, sometimes an inference from column width.
  The two are distinguished (`TruncationHint.MARKER` / `WIDTH`).

The per-retailer **column widths** (`_RETAILER_WIDTHS`) are counted off these
photos. No retailer publishes its truncation policy: these are heuristics, not
specifications.

### Assumed, and marked as such

Four entries — `CRQ`→croque, `NOUV`→nouveau/nouvelle, `ECR`→écrémé, `SS`→sans —
are **attested on none of the eight receipts**. They come from the project's own
internal literature. They carry `evidence = "assumed"` and `confidence = "low"`,
which makes them systematically `requires_review`. A test forbids an `assumed`
entry from claiming better than `low`. **This is the block to delete first** if a
bad match is reported.

The forms observed on real receipts are, moreover, `ECREM` (not `ECR`) and `S/`
(not `SS`), which is a hint that these variants come from somewhere else.

Also **contextual deductions** (confidence `medium`) rather than certain
readings: `DD`→découenné dégraissé, `BBC`→Bleu-Blanc-Cœur, `SDG`→sel de Guérande,
`NTAR`→non traité après récolte, `IDS`→Itinéraire des Saveurs, `FQC`→Filière
Qualité Carrefour, `PXM`→Prix Mini, `JB`→Jardin BiO'. They are plausible and
useful; they are not sourced from the retailers.

---

## 7. The measured rate

### Protocol

1. The lexicon **and** the code were frozen on the basis of the eight receipts
   above.
2. **Only then** were three receipts that had played no part in the construction
   read and hand-annotated: `openprices:10051` (E.Leclerc Levallois, thermal
   print with comma-separated fields), `openprices:14722` (Intermarché Contact),
   `openprices:3716` (Carrefour Carré Sénart). **83 lines, 85 annotated
   abbreviations.**
3. The annotation deliberately records the abbreviations the lexicon **does not
   know** (`BRK`→brique, `PLT`→poulet, `CHX`→chou…): without them, recall would
   measure the lexicon's consistency with itself, not its real coverage.
4. The lexicon was **not** touched after this measurement.

Exactly one change was made after seeing the evaluation set, and it is in the
**code**, not in the lexicon: two tokenisation defects (the comma as a field
separator at Leclerc, and counts glued to a word such as `4SACH`). Both figures
are given.

### Results

| Metric | Initial freeze | After the tokenisation fix |
|---|---|---|
| **Precision** (correct expansions / expansions emitted) | 0.938 (30/32) | **0.946** (35/37) |
| **Recall** (abbreviations read / abbreviations annotated) | 0.353 (30/85) | **0.412** (35/85) |
| **Safe lines** (no wrong expansion) | 0.976 (81/83) | **0.976** (81/83) |
| **Perfect lines** (everything read, nothing wrong) | 0.506 (42/83) | **0.554** (46/83) |

**How to read these numbers.**

- **Precision is the safety number.** 0.946 means that out of 37 expansions
  emitted, 2 are wrong — the two named in §5. It is the floor of the regression
  test (`MIN_PRECISION = 0.94`).
- **Recall is the usefulness number, and it is low — that is expected, and it is
  the baseline.** 195 entries built from eight receipts cannot cover the
  abbreviations of a ninth. 58% of the annotated abbreviations are simply absent
  from the lexicon. **This is exactly what enrichment has to raise.**
- **The safe-line rate (97.6%) is the number to keep in mind for the allergen
  path**: across 83 lines from three unseen retailers, only two carry a wrong
  reading, and none slipped through as `requires_review = False` by accident.

**Reproducing the measurement**:

```bash
cd backend
uv run pytest tests/labels/test_evaluation.py -q
```

The floors live in `tests/labels/test_evaluation.py`. **An improvement that
raises recall by lowering precision has made the module more dangerous**, and
that test is what says so.

---

## 8. Enriching the lexicon

### When a user reports a bad match

1. **Find the raw line**, not the normalised one. The printed label is the source
   of truth; normalisation can change.
2. **Run the expansion** and look at `result.tokens`: every token carries its
   reading, its confidence, its class and its `evidence`. The culprit is
   identifiable in one line.
3. **Choose the fix**:

| Symptom | Fix |
|---|---|
| A **wrong** reading is returned | Do not delete the entry: **add** the missing reading to its `expansions`. The module will return both and ask for a review. That is what would have saved `CROUT`. |
| A wrong reading and **no correct reading** | Lower the confidence to `low`, or delete the entry if its `evidence` is `assumed`. |
| An abbreviation **not read** | Add a `[[token]]`, with `evidence` pointing at the receipt. |
| The abbreviation only holds for one retailer | Fill in `retailers`. Mandatory for any one- or two-letter form. |

4. **Always fill in `evidence`.** Loading fails without it. An entry nobody knows
   the origin of is an entry nobody will dare delete in two years.
5. **Add the line to the evaluation set** (`tests/labels/eval_corpus.toml`) — and
   raise the recall floor if the measurement improved.

### Adding an entry

```toml
[[token]]
form = "BRK"                       # as printed; case and accents do not matter
expansions = ["brique"]            # ALL possible readings, never just one for convenience
confidence = "high"                # high / medium / low — see §2
kind = "skeleton"                  # truncation | skeleton | initialism | qualifier
                                   # | packaging | brand | private_label | department
evidence = "openprices:3716 (BRK 1L LAIT UHT MO)"
retailers = ["carrefour"]          # optional; empty = all retailers
notes = "…"                        # optional
```

A brand carries **no** reading: `expansions = []` with `kind = "brand"`. It is
stripped from the designation, which makes *coleslaw* match rather than
*ranou coleslaw*.

### The loop that really matters

This lexicon is filled by hand today. Eventually, the feed is the `receipt_alias`
table described in §3.6 of the ingestion note: every alias a user confirms yields
a **label ↔ product-name alignment**, from which token-to-token correspondences
are extracted. Rejections count as much as acceptances.

Two guardrails not to lose along the way:

- **The learning key is (normalised label, retailer)**, not the label alone. A
  wrong alias learned globally poisons the whole corpus.
- **An automatically derived correspondence enters at `medium` at best**, never
  at `high`: it is inferred from an alignment, not read off a receipt.

---

## 9. What this module is not

- **It is not wired to anything.** Shipped standalone and tested; the wiring into
  receipt ingestion and into ingredient matching will come later.
- **It does not do matching.** It produces an expanded designation; `pg_trgm`
  blocking, French full-text search and RRF fusion are one floor above
  ([`technical-notes-ingestion.md`](technical-notes-ingestion.md) §3.6).
- **It accesses neither the database nor the network.** A single file read, on
  the first call, memoised. A different lexicon is injected by parameter
  (`expand_label(..., lexicon=…)`), which makes the module testable without
  starting anything — a direct requirement of ADR-0009, which makes it a safety
  control.
- **It does not replace human review.** It says when review is needed.

---

## 10. Next steps, by decreasing return

1. **Handle positional truncation as a prefix** in the matching engine
   (`LIKE 'chocola%'` / `word_similarity` on the fragment). This is the biggest
   reservoir of recall, and it requires no additional lexicon entry.
2. **Seed the lexicon from Open Prices at scale.** 10,607 receipt photos are
   published there, about 575 of them French. Eight have been worked through by
   hand; the rest can be processed in batches with the OCR/VLM pipeline in §3 of
   the ingestion note. It is the only public source identified that pairs a
   French receipt label with a GTIN.
3. **Measure `product_name_fr` completion on the Open Food Facts France dump**
   (ten minutes of SQL, §3.6 of the note). That number determines the usefulness
   of the entire matching chain and is published nowhere.
4. **Extend the evaluation set to 300–500 lines** covering Lidl, Aldi, Casino,
   Franprix and the organic retailers. 83 lines are enough to detect a
   regression, not to measure finely.
5. **Model multi-buys and weighed lines**, which are out of scope today.

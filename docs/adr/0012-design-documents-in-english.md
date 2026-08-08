# 0012. Design documents in English

## Status

Accepted — 2026-08-04. Supersedes the language clause of
[ADR-0001](0001-record-architecture-decisions.md); everything else in that record
stands.

## Context

ADR-0001 required that records be "written in French with all technical
identifiers in English". Thirteen documents in `docs/` followed it. The README,
the code, the commit messages and the ADR *identifiers* were English throughout.

The split held while the repository was private. It stopped holding when the
repository went public on GitHub under AGPL-3.0, and the reason is narrower than
"English is the language of software": a contributor who does not read French
could read the code and the README perfectly well — and could not read a single
one of the eleven documents that explain **why anything is the way it is**.

That inverts the point of writing them. The ADRs exist so that someone arriving
later does not re-litigate a decision whose cost was already weighed; the
technical notes exist so that a measured figure is not replaced by an intuition.
A reader who can see the `allergens_risk` column but not the paragraph explaining
that it carries all fourteen allergens when the state is `unknown` is a reader who
will eventually "simplify" it.

The trigger was concrete. `docs/data-model.md` gained about 700 lines documenting
the v1.1 tables, written in French to match the file. Asked whether that was
right, the owner chose to translate everything instead.

## Decision

**Every versioned document is written in English**: ADRs, API contracts, the data
model, the security model and audits, technical notes, the testing strategy,
operational runbooks, README, CONTRIBUTING.

**Two things stay French, and they are not exceptions to the rule but a different
rule.** The product's user-visible strings are French because its users are
French-speaking households; and quoted source material — ANSES and PNNS
publications, till-receipt abbreviations such as `PDT NOUV`, retailer names — is
data, reproduced verbatim, never translated. A translated abbreviation is a
falsified observation.

The eleven French documents were translated in one pass rather than left to drift
bilingual. A repository half in each language is worse than either choice: it
gives every future author a decision to make per file, and they will not all make
the same one.

## Consequences

### Positive

- The reasoning becomes readable by the audience the AGPL invites. A licence that
  obliges you to publish source, paired with rationale nobody outside can read, is
  a formality rather than an openness.
- One rule, no per-file judgement call.
- The English documents and the English identifiers they discuss now sit in the
  same language, so a term stops changing form between the prose and the code.

### Negative

- **The maintainer now writes design documents in a second language.** These
  records are valued for being blunt — for naming what a decision costs, and for
  saying plainly when an earlier version of a document was wrong. That register is
  the hardest thing to carry across a language, and some of it will be lost in
  writing as well as in the translation already done.
- **The translation is a large diff nobody will review line by line.** The
  translating agents controlled it by diffing every identifier, JSON key and
  numeric value before and after — byte-identical is the claim, and it is checkable
  — but prose fidelity rests on the care taken, not on a test.
- Documents that were untracked at translation time have **no committed French
  baseline** to compare against. For those files the pre-translation snapshot is
  the only reference, and it does not survive this session.
- Contributors who read French and not English now face the mirror of the problem
  this record solves. That is accepted: the repository is public and international,
  the maintainer is not.

## Rejected alternatives

- **Keep French and state it in CONTRIBUTING.** Zero work, immediately coherent,
  and defensible for a single French-speaking maintainer. Rejected because it
  closes external contribution on precisely the architectural decisions where an
  outside opinion is worth the most.
- **Translate only the ADRs.** The compromise that looks sensible: eleven records
  are what an external contributor must read to understand the structure. Rejected
  because it leaves the API contracts — the documents a contributor must read to
  *build* anything — in French, and reintroduces the per-file question this record
  exists to remove.
- **Machine-translate on publication, keep French sources.** Rejected: it makes the
  published text an artefact nobody proofreads, and these documents make claims
  about safety behaviour where a mistranslation is a defect rather than a nuisance.
- **Edit ADR-0001 in place.** Rejected on principle by ADR-0001 itself, which
  declares records immutable and superseded rather than rewritten. Editing it would
  have hidden that the project once decided the opposite, and the reasons it did.

## Revisiting

Reopen if the project acquires maintainers who share a language other than
English, in which case this record was a cost paid for an audience that never
arrived.

Note for whoever picks this up: **ADR-0011 does not follow the structure ADR-0001
imposes** — it numbers itself `11.` rather than `0011.`, carries its date outside
`## Status`, and splits `## Consequences` into three headings of its own. That
divergence predates this record and was left alone, but it is more visible now
that all twelve files share a language.

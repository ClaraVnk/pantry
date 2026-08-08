# 0001. Record architecture decisions

## Status

Accepted — 2026-08-03. Its **language clause is superseded by
[ADR-0012](0012-design-documents-in-english.md)** (2026-08-04): design documents
are now written in English, not French. Everything else below stands, and the
clause is left in place rather than edited out — this record's own rule is that
decisions are superseded, never rewritten, so that a reader can still see what the
project once decided and why.

## Context

Chaudron is developed solo. Structural decisions (stack choice, functional scope, multi-tenant data model) are made in a few minutes and forgotten in a few weeks. Six months later the question is no longer "what did we pick" — the code says that — but "why", and above all "what did we rule out, and for what reason". With no record, every re-reading replays the same trade-off from scratch, usually with less context than the original.

The risk is amplified by the envisaged phase 2 (public multi-user opening): decisions taken for family use will have to be reassessed, and it must be possible to tell a deliberate compromise from a genuine constraint.

Michael Nygard's ADR format is short, versioned with the code, and readable without tooling.

## Decision

Every significant architecture decision is recorded in a `docs/adr/NNNN-title-in-kebab-case.md` file, numbered sequentially, versioned with the code, written in French with all technical identifiers in English.

Mandatory structure: `# NNNN. Title`, then `## Status` (Accepted / Rejected / Superseded by ADR-NNNN, with the date), `## Context`, `## Decision`, `## Consequences` (positive and negative kept separate), `## Rejected alternatives` (one reason per alternative), and `## Revisiting` when a concrete signal can reopen the decision.

An ADR is written when the decision: commits to an external dependency that is hard to remove, constrains the data model, defines a boundary between layers or services, excludes a feature someone could reasonably expect, or exposes the project to a recurring cost (financial, operational, maintenance).

An ADR is **not** written for: a utility library choice replaceable in an hour, a naming convention, an implementation detail local to one module.

ADRs are **immutable**. An accepted ADR is not modified: a new one is written that supersedes it, and the old one is marked `Superseded by ADR-NNNN`. The history of abandoned decisions is worth as much as that of the live ones.

## Consequences

### Positive

- The context of a decision outlives both forgetting and a change of maintainer.
- Rejected alternatives are documented: a path already examined is not proposed again without a new argument.
- The `Revisiting` section turns a frozen choice into a conditional one: we know which signal reopens it.
- An ADR forces the negative consequences to be spelled out, which sometimes reveals that a decision is not ripe.

### Negative

- Writing a decent ADR costs 30 to 60 minutes. On a solo project, that is time taken from implementation.
- The format invites *post hoc* rationalisation: one justifies a choice already made instead of examining the alternative. The only safeguard is honesty about the negative consequences.
- Unmaintained ADRs (decision changed in the code, ADR never superseded) are worse than no ADRs at all: they describe with authority a system that no longer exists.
- The "significant decision" threshold remains subjective. We will write useless ADRs and forget useful ones.

## Rejected alternatives

- **No decision documentation at all** — the default mode on a solo project. Rejected: phase 2 means reopening decisions taken in phase 1, with no reliable memory of the original reasoning.
- **Comments in the code** — close to the code but confined to a single file. Rejected: an architecture decision by definition spans several modules, or an absence of code (see ADR-0002), which no comment can host.
- **A wiki or external notes (Notion, Obsidian)** — more comfortable to write in. Rejected: documentation drifts out of sync with the code as soon as it lives elsewhere, and it never shows up in review diffs.
- **Detailed commit messages** — versioned and dated. Rejected: unreadable as a corpus, and nobody walks a `git log` to answer "why PostgreSQL".
- **MADR or a richer format** (weighted criteria tables) — more rigorous for decisions with several stakeholders. Rejected: oversized for a single decision-maker; the writing cost would kill the practice.

## Revisiting

If the project gains a second regular contributor, reassess the format: a more structured template (MADR) and ADR review in pull requests then become defensible.

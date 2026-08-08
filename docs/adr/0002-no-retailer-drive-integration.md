# 0002. No integration with retailer drive accounts

## Status

Accepted — 2026-08-03

## Context

The most obvious use case for Chaudron is filling the stock automatically after a drive order: the user orders from Courses U, Intermarché or Chronodrive, and their stock updates without any typing. It is the feature every user will ask for first.

None of these retailers publishes a partner API accessible to an individual developer. The only technical routes are:

1. **Authenticated scraping**: the user hands their retailer credentials to Chaudron, which logs in on their behalf and retrieves the order history.
2. **Browser automation** (headless Playwright) server-side, a variant of the above with the same credential prerequisites.
3. **Reverse-engineering the mobile APIs** of the retailers' apps.

All three share the same properties: they require storing reusable credentials that grant access to a merchant account (saved payment methods, address, purchase history), they violate every retailer's terms of service, and they break at the first front-end change — with no notice, no status page, no pinnable version. Multiplied by the number of French retailers, that is a permanent and unplannable maintenance load.

In phase 1 (family use), the risk is contained. In phase 2 (public opening), Chaudron would become a centralised store of merchant account credentials: a target whose value to an attacker far exceeds that of the application's own business data.

## Decision

Chaudron integrates with no retailer drive account. No feature asks for, stores or transmits a merchant account credential.

Stock is filled through four routes, all initiated by the user:

1. **Manual entry** — the foundation, always available.
2. **Barcode scanning** — EAN resolution via Open Food Facts (public API, ODbL licence, no authentication).
3. **Receipt photo** — parsed by a multimodal model (see ADR-0005). Works for in-store purchases as well as drive orders (receipt handed over at pickup).
4. **Forwarded confirmation e-mail capture** — the user forwards their order confirmation e-mail to a per-household address (`<household_token>@inbox.<domain>`). The content is parsed to extract the order lines.

Route 4 obtains a large share of the benefit of drive integration with none of its prerequisites: it is the user who pushes the data, there is nothing to authenticate against the retailer, and a confirmation e-mail is a far more stable format than a merchant site's DOM. Forwarding stays a manual gesture — but one gesture per order, not per item.

## Consequences

### Positive

- No merchant account credential in the database: the product's costliest attack surface simply does not exist.
- No dependency on non-contractual surfaces: no silent breakage at a retailer's next redesign.
- No legal exposure from breaching terms of service, and no user account suspension for automated use.
- The scope does not grow with the number of retailers: adding a retailer costs at most an e-mail parser, never an authentication pipeline.
- Phase 2 stays open: no security debt to clear before opening to the public.

### Negative

- **We lose the most anticipated feature.** A user comparing Chaudron to a drive-integrated competitor will see a lesser product, and "it's safer" does not make up for it in a demo.
- Forwarding an e-mail is a manual gesture on every order. Low friction, but real, and the forget rate will be high.
- Receipt OCR and e-mail parsing are **approximate by nature**: truncated or abbreviated retailer labels (`PAT SABL BEURRE 250G`), no EAN code on the receipt, implicit quantities. Matching against a product reference will require a manual correction step, itself friction.
- Every confirmation e-mail format is a parser to write and maintain. The load is lower than a scraper's; it is not zero.
- Coverage of fresh produce and loose goods will stay poor whichever route is used (no EAN, weighed at the till).
- Receiving user e-mails creates its own surface: the inbox is an unauthenticated entry point that must be treated as hostile data (strict sender validation, quotas, no naive deserialisation of attachments).

## Rejected alternatives

- **Scraping with credentials stored encrypted** — technically feasible, encryption at rest available. Rejected: encryption at rest does not protect against an application compromise, since the application must be able to decrypt in order to authenticate. The leak risk remains intact; only the absence of a secret removes it.
- **Client-side browser extension** — the credential never leaves the user's machine, which answers the security objection. Rejected: it is a second codebase to maintain, with its own review cycles at the extension stores, for a product whose foundation is a mobile PWA (see ADR-0004) where extensions do not exist.
- **Waiting for an official partner API** — the clean path. Rejected: no French retailer offers one to an individual developer, and nothing suggests that will change. This is not an alternative, it is an indefinite deferral.
- **A third-party aggregator** (a commercial service exposing purchase histories) — would outsource the problem. Rejected: no credible player in the French grocery retail market, and it would amount to moving credential storage to a third party without reducing the risk to the user.

## Revisiting

Reopen the decision if one of these signals appears:

- A retailer publishes a **documented partner API with OAuth** (delegation by revocable token, no credential sharing). That is the only change that invalidates the underlying reasoning.
- An inter-industry standard for exporting purchase history emerges (typically in the wake of the GDPR right to portability, article 20).
- Usage measurements show that e-mail forwarding is used for fewer than 20% of orders despite a polished UX: the trade-off no longer holds, and we will have to either accept manual entry as the primary route or reconsider the browser extension.

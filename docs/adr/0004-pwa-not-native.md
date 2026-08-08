# 0004. Installable PWA rather than a native mobile app

## Status

Accepted — 2026-08-03

## Context

Chaudron is a mobile-use application: you enter your stock standing in front of a cupboard, you scan a barcode while putting the shopping away, you photograph a receipt on the way out of the shop. Desktop is secondary.

The trigger for the choice is **economic and deliberate**: publishing a native app requires an Apple developer account at $99/year, renewed annually, and a Google Play account at $25 (one-off payment). For a project in a phase 1 family stage, with no revenue and no certainty of reaching phase 2, that is a recurring subscription committed to before the first line of value — and on iOS, an expense that, left unrenewed, pulls the app off users' devices when it expires.

The second factor is maintenance load: a native app means one extra codebase per platform (or a cross-platform framework and its own debt), a store review cycle for every fix, and a version matrix to support. On a solo project, that cost is paid out of feature development time.

## Decision

Chaudron is an installable PWA: React + Vite, web manifest, service worker, frontend codebase separate from the backend.

The needed capabilities come from standard web APIs: `BarcodeDetector` with a WASM fallback (ZXing) on browsers that do not expose it, `getUserMedia` for the camera stream, `<input type="file" capture>` for the receipt photo, a service worker for the application cache and offline stock consultation.

No developer account is opened, no application is submitted to a store.

## Consequences

### Positive

- Zero recurring distribution cost, zero review delay: a deployed fix is available at the next reload.
- One frontend codebase, one build pipeline.
- Installation happens by URL — convenient for family use, where you share a link.
- No dependency on a store's policy: no risk of removal, no commission, no rules to interpret.

### Negative

These limitations are real and they hurt the product; they are not details to be worked around.

- **Push notifications on iOS: workable but fragile.** Since iOS 16.4 Web Push exists, but *only* if the user has added the PWA to the home screen — a gesture most people will not perform. And expiry alerts are precisely the feature that brings the user back. On iOS we must assume that a significant share of users will never receive them, and plan a fallback channel (e-mail, or an actively consulted "use soon" view).
- **Degraded camera access.** `getUserMedia` works, but only in a secure context (HTTPS), and fine control (autofocus, torch, optical zoom) is uneven across browsers. `BarcodeDetector` is not available on Safari: a WASM decoder has to be bundled, which bloats the bundle and gives a scan that is slower and less tolerant of damaged codes than a mobile SDK's native API.
- **Opaque installability.** On Android, an install prompt is offered. On iOS there is none: the user has to go through Share → "Add to Home Screen". That gesture has to be explained in the interface, and some users will not do it — they will stay in a Safari tab, with no push and with storage liable to be purged after several weeks of inactivity.
- **Zero discoverability.** Nobody finds Chaudron by searching "food stock management" in the App Store. Distribution rests entirely on direct sharing and web search ranking. In phase 1 that is inconsequential; in phase 2 it is a major acquisition handicap.
- No home screen widget, no rich native share target, no integration with the system voice assistant.
- Local storage (IndexedDB, service worker cache) can be evicted by the system: offline mode is a convenience, not a guarantee.

## Rejected alternatives

- **Native iOS + Android (Swift + Kotlin)** — the best possible experience: finely driven camera, reliable push, store presence. Rejected: two codebases for a solo developer, plus the developer account costs, before any product validation.
- **React Native or Flutter** — one codebase for two platforms, native access to camera and push. Rejected: it does **not** remove the developer account costs or the review cycles, which are the trigger for the decision. It adds a framework and its own native build chain to maintain.
- **Capacitor (PWA packaged as a native app)** — reuses the web codebase and gives access to native push and the stores. This is the most serious alternative, and the preferred migration path. Rejected **for now**: it restores exactly the costs the decision is trying to avoid (developer accounts, submissions, reviews), without being required in phase 1. To be picked up when the revisiting signals below fire.
- **A plain, non-installable web app** — simpler still. Rejected: dropping the manifest and the service worker removes installation, offline mode and any possibility of push, and saves very little.

## Revisiting

Reconsider, aiming at Capacitor rather than a native development *ab initio* — the React codebase is reused:

- **Primary signal**: if measurements show that expiry alerts are not received by a significant share of iOS users, and that this failure translates into a drop in usage. That is the most likely breaking point, since it hits the retention loop.
- If the home-screen install rate stays low (< 30% of active users) despite explicit in-app instructions.
- If barcode scan quality via WASM turns out to be a recurring reason for abandonment in real use.
- If phase 2 actually starts and store-driven acquisition becomes necessary for growth. The annual cost then becomes defensible, since it is backed by real usage.

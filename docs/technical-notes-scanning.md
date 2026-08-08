# Technical feasibility note — barcode scanning and EAN resolution

**Project:** Chaudron (React + Vite PWA / FastAPI backend) — household food stock management
**Date:** 3 August 2026
**Status:** scoping note, to be re-read before freezing the architecture of the "scan" module

---

## Method and confidence level

This note distinguishes three levels:

| Marker | Meaning |
|---|---|
| **[V]** | Verified on 3 August 2026 by direct request (curl / npm registry / GitHub API) or by reading the primary source (spec, official documentation, bug tracker). The command or the URL is given. |
| **[S]** | Sourced on a credible third-party page but not reproduced first-hand. |
| **[NV]** | **Not verified.** A plausible claim that I was unable to confirm — typically because it requires a physical device, a paid account, or because it was blocked by a rate limit. Treat as a hypothesis to be tested. |

The local measurements (file sizes, OFF counts) were made from the dev machine on 3 August 2026; they will move.

---

## 1. Barcode reading in the browser

### 1.1 The native `BarcodeDetector` API: the real state of things

The conclusion fits in one sentence: **`BarcodeDetector` is unusable as the sole foundation.** It is available neither on iOS, nor on Firefox, nor on Chrome/Windows, nor on Chrome/Linux.

Compatibility data, extracted from the primary source (`mdn/browser-compat-data`, file `api/BarcodeDetector.json`) **[V]**:

| Browser | Support | Restriction |
|---|---|---|
| Chrome desktop | 88+ | **ChromeOS and macOS only** (83–87: macOS alone) |
| Chrome Android | 83+ | OK — the main target |
| Edge | 83+ | **macOS only** |
| Opera | 69+ | **macOS only** |
| Firefox / Firefox Android | ❌ | `version_added: false` |
| Safari (macOS) | 17+ | **behind a feature flag** ("Shape Detection API") |
| Safari iOS | 17+ | ditto — a flag, and see below |
| Samsung Internet | 83+ | aligned on Chrome Android |
| WebView Android | 83+ | aligned on Chrome Android |

Source: <https://github.com/mdn/browser-compat-data/blob/main/api/BarcodeDetector.json>

The Chrome documentation confirms it in so many words: *"Barcode detection is available on macOS, ChromeOS, and Android"*, and *"Google Play Services are required on Android"* — the implementation delegates to the OS libraries, it does not embed its own decoder **[V]** (<https://developer.chrome.com/docs/capabilities/shape-detection>).

MDN classifies the API as **"Limited availability"** and **"Experimental"**, requiring a secure context (HTTPS) **[V]** (<https://developer.mozilla.org/en-US/docs/Web/API/BarcodeDetector>).

caniuse gives 76.36% of global usage as "support or partial support", but that figure is misleading: it aggregates the "partial support" of Chrome desktop, which is in reality non-support on Windows and Linux **[V]** (<https://caniuse.com/mdn-api_barcodedetector>).

#### The iOS case: the flag exists, it is useless

The most important point of this section. On iOS, the "Shape Detection API" flag is indeed present under Settings > Safari > Advanced > Feature Flags, **but enabling it does not make detection work**. WebKit bug **#281848** ("Shape Detection API doesn't work on iOS") has been open since 21 October 2024 and is **still at status NEW**; the comments report failure on Safari 17.6.x, 18.3, 18.4, 18.5, then on the iOS 26 betas (June 2025), the last comment dating from July 2026 **[V]** (<https://bugs.webkit.org/show_bug.cgi?id=281848>).

Nothing in the WebKit release notes from Safari 26.0 to 26.6 announces Shape Detection being enabled by default **[S]** (<https://webkit.org/blog/17333/webkit-features-in-safari-26-0/>, <https://webkit.org/blog/18178/webkit-features-for-safari-26-6/>).

> **Design consequence:** do not write code that assumes `BarcodeDetector` is present. And above all, do not settle for an `if ('BarcodeDetector' in window)`: on macOS, the object can exist without being reliable. The availability test must be `await BarcodeDetector.getSupportedFormats()` and must check that `ean_13` is among them.

#### A trap for the local dev loop

The development machine runs Linux. **`BarcodeDetector` exists there in no browser** (Chrome: macOS/ChromeOS/Android only; Firefox: never). The "native" path will therefore **never be exercised in local dev** — only on a real Android phone. That is exactly the kind of branch that rots without anyone noticing. One more argument for not maintaining two code paths.

### 1.2 Fallback libraries — comparison

All the metadata below was collected **on 3 August 2026** from `registry.npmjs.org` and the GitHub API **[V]**.

| Package | Version / date | Licence | Deps | Repo (⭐ / last push / issues) | Nature |
|---|---|---|---|---|---|
| **`zxing-wasm`** | 3.1.2 — 2026-07-18 | MIT | `@types/emscripten`, `type-fest` | Sec-ant/zxing-wasm — 246 ⭐ / 2026-08-01 / 9 | ZXing-C++ compiled to WASM |
| **`barcode-detector`** | 3.2.1 — 2026-07-12 | MIT | `zxing-wasm` | Sec-ant/barcode-detector — 227 ⭐ / 2026-08-03 | Poly/ponyfill of the standard API, backed by `zxing-wasm` |
| **`@zxing/library`** | 0.23.0 — 2026-04-29 | Apache-2.0 | `ts-custom-error` | zxing-js/library — 2,923 ⭐ / 2026-07-25 / **170 issues** | Pure TypeScript port of ZXing |
| **`html5-qrcode`** | 2.3.8 — **2023-04-15** | Apache-2.0 | none | mebjas/html5-qrcode — 6,191 ⭐ / 2025-12-01 / **441 issues** | Complete UI component, bundles `@zxing/library` |
| **`@ericblade/quagga2`** | 1.12.1 — 2025-12-20 | MIT | `gl-matrix` | ericblade/quagga2 — 908 ⭐ / 2026-07-25 | Pure-JS 1D decoder |

#### Maintenance

- **`zxing-wasm` / `barcode-detector`**: a sustained and regular cadence. For `zxing-wasm`: 3.0.1 (2026-03-09), 3.0.2 (04-01), 3.0.3 (05-04), 3.1.0 (06-01), 3.1.1 (07-12), 3.1.2 (07-18) **[V]**. The same maintainer (Sec-ant) for both, which is at once a guarantee of consistency and a **bus factor = 1 risk** — to be noted in the risk register.
- **`@zxing/library`**: to be watched. Publication history: 0.21.3 on **2024-08-21**, then nothing until 0.22.0 on **2026-04-27** **[V]** — that is **20 months without a release**. The project has restarted, but 170 open issues on a manual JS port of ZXing means accumulated divergence from the C++ upstream.

  > *The table above says 0.23.0 (2026-04-29) and this bullet says 0.22.0 (2026-04-27); both come from the same npm pull, and nothing else in this note reconciles them.* They describe different things rather than disagreeing: 0.22.0 is the release that **ended** the silence, which is why the 20-month arithmetic is computed from it, while 0.23.0 is simply the **latest** version two days later, which is what a comparison table should report. The maintenance conclusion is the same either way. None of it reaches the build: `@zxing/library` is ruled out below and is not a dependency of this project — the frontend ships `barcode-detector` on top of `zxing-wasm`.
- **`html5-qrcode`**: **last npm publication on 15 April 2023**, i.e. more than three years ago **[V]**. The README explicitly announces maintenance mode (*"the author shall not be able to make any bug fixes or improvements for the time-being. Pull requests also won't be merged"*) and 441 issues are open **[S]** (<https://github.com/mebjas/html5-qrcode>). **To be ruled out.** All the more so since it bundles `@zxing/library`: we would inherit a version of an already-lagging dependency frozen in 2023.
- **`@ericblade/quagga2`**: alive, but **1D only** and a pure-JS decoder. Relevant only if we want zero WASM.

#### Bundle size — real measurements

The `zxing-wasm@3.1.2` archive is 3.77 MB uncompressed, but that figure is a scarecrow: it contains **three alternative WASM binaries**, of which we load only one **[V]**.

| Artefact | Raw | gzip -9 (measured) |
|---|---|---|
| `dist/reader/zxing_reader.wasm` | 1,065,866 B | **448,787 B** |
| `dist/full/zxing_full.wasm` (read + write) | 1,511,909 B | — |
| `dist/writer/zxing_writer.wasm` | 648,328 B | — |
| `dist/es/reader/index.js` (JS glue) | 42,595 B | — |

So: **~450 kB gzip for the read-only decoder**, to be loaded once then cached in the service worker. Brotli would do appreciably better — not measured, `brotli` is not installed on the machine **[NV]**.

That is a real cost but an acceptable one for an inventory PWA: the WASM is loaded **only when the scan screen opens**, not at app start-up, and it is then served from cache, including offline.

`barcode-detector@3.2.1` adds only 260 kB uncompressed of JS glue, the WASM coming from `zxing-wasm` **[V]**.

#### Supported formats

`zxing-wasm` / `barcode-detector` cover well beyond the need. For retail, the readable ones are: `EAN13`, `EAN8`, `UPCA`, `UPCE`, `ISBN`, as well as the whole `DataBar` family (Omni, Stacked, Limited, Expanded) — that last one matters, it is found on small packaging and fresh produce **[V]** (README of `zxing-wasm@3.1.2`). Note: `EAN5` and `EAN2` (the add-ons) are write-only, not read.

The native Chrome API, for its part, exposes 13 formats (`aztec`, `code_128`, `code_39`, `code_93`, `codabar`, `data_matrix`, `ean_13`, `ean_8`, `itf`, `pdf417`, `qr_code`, `upc_a`, `upc_e`) — **no DataBar** **[V]**. The WASM fallback is therefore, on this precise point, *more capable* than the native one.

#### Mobile performance

I was **not** able to measure decoding throughput on a real phone **[NV]** — that requires a device and a test protocol. What can be asserted:

- ZXing-C++ compiled to WASM is structurally faster than the JS port `@zxing/library`, which reimplements the same algorithm in a slower language and without SIMD.
- Since the native API delegates to the OS decoder (or even to the camera module's silicon according to the Chrome documentation **[V]**), it remains the fastest where it exists.
- The real performance lever in practice is not the decoder but **the acquisition loop**: decoding at ~10 fps on a reduced region of interest rather than at 60 fps on the full image, and running the decoding in a **Web Worker** so as not to block the main thread (`zxing-wasm` works in a worker, and the `BarcodeDetector` API is likewise exposed to Web Workers according to MDN **[V]**).

#### Commercial alternatives

STRICH, Scandit, Scanbot and Dynamsoft offer proprietary web SDKs reputed to be more robust on damaged codes and difficult lighting. **I have not checked their pricing** **[NV]**. To be kept as a plan B *only* if field tests show a prohibitive read failure rate — for a household project, the licence cost is in all likelihood disqualifying.

### 1.3 Recommendation

> **Use `barcode-detector` (Sec-ant's ponyfill), not `zxing-wasm` directly, and no "native if available" branch.**

Justification:

1. **One API, one code path.** The ponyfill exposes exactly the standard `BarcodeDetector` interface. The day Safari fixes #281848 and Chrome/Linux falls into line, we move from the ponyfill to the polyfill (or remove the import) without touching application code.
2. **One behaviour to test.** See the trap in §1.1: the "native" branch would never be exercised in local dev. Two paths, one of them never tested, is a broken path that does not know it. The native performance gain does not justify that risk on a household inventory app where a few dozen items are scanned per week.
3. **Better format coverage** than the native one (DataBar).
4. **MIT licence**, compatible with everything; active and frequent maintenance.
5. **Works offline** once the WASM is precached — unlike any server-side solution.

Import: `import { BarcodeDetector } from "barcode-detector/ponyfill"`, restricted to the useful formats:

```ts
const detector = new BarcodeDetector({ formats: ["ean_13", "ean_8", "upc_a", "upc_e", "databar", "databar_expanded"] });
```

Restricting the formats is not cosmetic: it reduces the work per image and above all **the false positive rate**.

---

## 2. Camera access in a PWA

### 2.1 `getUserMedia` — constraints

- **HTTPS mandatory.** `navigator.mediaDevices` is only exposed in a secure context; `http://localhost` is treated as secure for development **[V]** (<https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia>). In practice: developing on a phone via the LAN IP (`http://192.168.x.x:5173`) **will not work**. It takes either an HTTPS tunnel, a local certificate, or testing via a deployment. To be planned into the dev loop from the start; it blocks early.
- **Rear camera**: `video: { facingMode: { ideal: "environment" } }`. Use `ideal` and not `exact` — with `exact`, the call fails outright on devices with no rear camera (desktop webcams), which breaks desktop development.
- **Android multi-camera trap**: `facingMode: "environment"` does not guarantee landing on the *right* rear sensor. On three-lens phones, the browser may select the ultra-wide, which often has no close focus — the result: a barcode at 10 cm stays blurred and never decodes. Fallback: `enumerateDevices()` after authorisation, then let the user pick the camera, remembering the choice. **[NV]** on the real frequency of the problem, but the mechanism is certain.
- **Resolution**: request `width: { ideal: 1280 }` at minimum. An EAN-13 has bars of 1 to 4 modules; for reliable decoding at least 2 px per module are needed, which implies a code occupying roughly 190 px of width in the image **[S]** (<https://www.scandit.com/blog/make-barcode-scanner-app-performant/>). On a 640×480 stream with a code occupying a third of the width, we are below that. 1280×720 is a good CPU-load / legibility compromise.
- **Focus, torch, zoom**: Chrome Android exposes `focusMode`, `focusDistance`, `torch` and `zoom` via `track.getCapabilities()` then `applyConstraints()`. **Safari iOS does not expose these constraints** **[S]** (<https://www.dynamsoft.com/codepool/camera-focus-control-on-web.html>). Not verified on a device **[NV]**. Concrete consequence: **no torch button on iPhone**, even though it is the number one remedy for read failures in low light. To be built into the UX: the torch button must be conditioned on `"torch" in track.getCapabilities()` and simply absent otherwise, not greyed out.
- Always `track.stop()` on every track when leaving the scan screen. A stream left open keeps the LED on, drains the battery, and on iOS contributes to the freezes described below.

### 2.2 The iOS/Safari case — the deciding point

**Short answer: yes, the camera works in a PWA installed on the home screen, since iOS 13.4 (March 2020). But reliability remains questionable in 2026, and this is the main risk of the project.**

The detail, sourced from the WebKit bug tracker:

**a) Basic support exists.** Bug **#185448** ("getUserMedia not working in apps added to home screen that run in standalone mode") is **RESOLVED FIXED**. The fix is confirmed in iOS 13.4 beta 1 (February 2020), shipped publicly in March 2020 **[V]** (<https://bugs.webkit.org/show_bug.cgi?id=185448>). The version floor is therefore iOS 13.4 — not constraining in 2026.

**b) But the permissions do not persist.** Bug **#215884** concerns repeated camera authorisation prompts in standalone mode. It is marked RESOLVED/CONFIGURATION CHANGED, **but the thread keeps receiving reports**: partial improvement in the iOS 14.5 beta (authorisation no longer resets on every page, but does not survive closing the app), and comments up to **January 2026** reporting that the problem persists on iOS 18.5+ **[V]** (<https://bugs.webkit.org/show_bug.cgi?id=215884>). Two distinct symptoms, both of them annoying:
   - authorisation granted **in Safari** is **not** carried over to the installed PWA;
   - authorisation is **requested again after every restart** of the PWA.

**c) And the video stream can be black.** Bug **#252465** describes a `getUserMedia()` that renders a `<video>` black or empty in PWA mode, while the same code works in Safari. Marked RESOLVED FIXED, but with regressions reported recurrently on iOS 18.0.1, 18.1, 18.4.1 and 18.5 up to June 2025 **[V]** (<https://bugs.webkit.org/show_bug.cgi?id=252465>).

**d) What the ecosystem recommends.** The STRICH knowledge base (publisher of a web scanning SDK, hence well placed) recommends: check you are on the latest iOS version and restart the phone; use the app in Safari rather than installed; or **remove the `apple-mobile-web-app-capable` tag** to force execution in Safari while keeping the icon on the home screen **[V]** (<https://kb.strich.io/article/29-camera-access-issues-in-ios-pwa>).

That last piece of advice is a trade-off to be made consciously: removing `apple-mobile-web-app-capable` means **giving up standalone mode** (Safari bar visible, no full screen) in exchange for reliable camera access. For a household inventory app, full-screen aesthetics are probably worth less than "scanning works". To be kept as an **emergency switch**, not as the default choice.

**e) What I was unable to verify.** **[NV]** The real behaviour on **iOS 26 with a physical iPhone** in August 2026. The bugs above have histories of going back and forth; it is possible that the situation has improved since the last public comments. It is equally possible that it has regressed.

> **Recommended blocking action:** before investing in the scan module, build a **throwaway prototype** — an HTML page served over HTTPS, `getUserMedia` + `barcode-detector`, installed as a PWA on a real, up-to-date iPhone. Check: (1) the video stream displays, (2) an EAN-13 decodes, (3) the authorisation survives killing and relaunching the app. Half a day. If (3) fails, that is not disqualifying, but the UX must be designed around the constraint from the outset rather than after the fact.

### 2.3 Offline behaviour

**Yes, scanning without a network is possible — under conditions.**

| Step | Offline? |
|---|---|
| Open the camera (`getUserMedia`) | ✅ purely local |
| Decode the EAN (WASM) | ✅ if the `.wasm` is precached by the service worker |
| Resolve EAN → product record | ❌ unless in the local cache |
| Record the addition to stock | ✅ if written to IndexedDB then synchronised |

Points to watch:

- **Precache the `.wasm` explicitly.** It is loaded dynamically by the JS glue, not via a static `import`: an automatically generated service worker (`vite-plugin-pwa` / Workbox) risks **not seeing it**. It must be added to the precache list by hand and the generated manifest checked. A classic mistake, and one that only shows up on a plane.
- **No Background Sync on iOS.** The Background Synchronization API is not supported by Safari and there is no indication that it will be soon **[S]** (<https://caniuse.com/background-sync>). Synchronisation must therefore **not** be built on it. The model that works everywhere: a queue of operations in IndexedDB, drained on the `online` event, on returning to the foreground (`visibilitychange`), and at app start-up.
- **Offline-first by design, deliberately.** The scan produces an EAN. Stock is modified **immediately, locally**, with the product record pending; OFF resolution is an asynchronous operation that enriches the entry later. This is not graceful degradation, it is the nominal mode — and it makes the app pleasant even with a network, since the back of a cupboard rarely has good reception.
- **[NV]** The storage quota and eviction policy for a PWA installed on iOS in 2026. Caution: do not treat IndexedDB as durable storage; plan for prompt server synchronisation and an export.

---

## 3. Product database — Open Food Facts

### 3.1 The API

Verified by direct requests on 3 August 2026 **[V]**.

**Versions.** v3 (latest sub-version **v3.6**) is the current recommended version. **v2 is explicitly marked deprecated** in the official documentation, still supported for compatibility **[V]** (<https://github.com/openfoodfacts/openfoodfacts-server/blob/main/docs/api/index.md>). → **Develop against v3 from the start.**

**Lookup endpoint:**

```
GET https://world.openfoodfacts.org/api/v3/product/{barcode}.json?fields=…
```

Response for an existing product (Nutella, `3017624010701`):

```json
{"code":"3017624010701","errors":[],
 "product":{"brands":"Ferrero","code":"3017624010701","product_name":"Nutella"},
 "result":{"id":"product_found","lc_name":"Product found","name":"Product found"},
 "status":"success","warnings":[]}
```

Response for a missing code (`3760091721234`) — **HTTP 404**:

```json
{"code":"3760091721234",
 "errors":[{"field":{"id":"code","value":"3760091721234"},
            "impact":{"id":"failure"},
            "message":{"id":"product_not_found"}}],
 "result":{"id":"product_not_found","name":"Product not found"},
 "status":"failure","warnings":[]}
```

> The v3 error contract is **structured** (`result.id`, `errors[]`) and differs from v2 (`status: 0` / `status_verbose`). Branch on `result.id === "product_not_found"` and on the HTTP code, never on a free-form string.

**Useful fields** (tested, non-empty on a real product):

| Field | Content |
|---|---|
| `product_name`, `product_name_fr` | name; always request the `_fr` variant |
| `brands` | brand, a free-form comma-separated string |
| `quantity` | contents, **free text** (`"400.0 g"`) — to be parsed, never a number |
| `categories_tags` | taxonomy, prefixed by language: `["en:spreads","fr:pates-a-tartiner","de:Other"]` — the mixture of languages is normal |
| `nutriscore_grade` | `a`…`e` |
| `nova_group` | 1–4 (degree of ultra-processing) |
| `allergens_tags` | `["en:nuts"]` |
| `image_front_small_url` | thumbnail (see §3.2 for the licence) |
| `ecoscore_grade` | still served, but the documentation now speaks of **Green-Score** — a rename in progress, not to be treated as stable |
| `serving_size` | serving |

**The `fields=` parameter is indispensable**: a complete record weighs several hundred kilobytes. Requesting 10 fields brings back a few hundred bytes. It reduces bandwidth, response time, and load on the OFF infrastructure.

**CORS:** the API returns `access-control-allow-origin: *` **[V]** (observed in the headers). The frontend *could* therefore call OFF directly. **Do not do it** — see §3.5; the reason is architectural, not technical.

**Staging environment:** `https://world.openfoodfacts.net`, protected by Basic Auth `off` / `off`. The documentation explicitly asks that **all development calls go through staging** **[V]**.

### 3.2 Terms of use — licences, attribution, rate limits

All these obligations are in the official documentation **[V]** (<https://github.com/openfoodfacts/openfoodfacts-server/blob/main/docs/api/index.md>) and on <https://world.openfoodfacts.org/data>.

**Licences — and they differ, that is the trap:**

| Element | Licence |
|---|---|
| The database (structure) | **ODbL 1.0** — <https://opendatacommons.org/licenses/odbl/1.0/> |
| The individual contents | **DbCL 1.0** — <https://opendatacommons.org/licenses/dbcl/1.0/> |
| **The product images** | **CC BY-SA 3.0** — <https://creativecommons.org/licenses/by-sa/3.0/> |

The documentation adds a warning about the images: *"They may contain graphical elements subject to copyright or other rights"* — the photographed packaging contains logos and brand imagery that remain the property of their holders. Displaying a thumbnail in a private household inventory app is inconsequential; republishing those images would be less so.

**ODbL = attribution + share-alike.** Concretely for Chaudron:
- display an attribution "Product data: Open Food Facts — ODbL" somewhere in the UI;
- share-alike bites **if the OFF database is combined with another database**: the derived database would then have to be published as open data. A cache of product records sitting alongside a personal stock is a borderline case; for private, non-redistributed use, the question does not arise in practice. **It would arise if Chaudron became a public multi-user service.** To be settled before, not after.

**A User-Agent is mandatory.** *"We ask you to always use a custom User-Agent to identify your app"*, in the format `AppName/Version (ContactEmail)` **[V]**. Reads require **no other authentication**; writes (editing a record, uploading a photo) require an account.

**Rate limits — exact quotation [V]:**

- **15 req/min/IP** for all product reads (`GET /api/v*/product` or the product page);
- **10 req/min/IP** for searches (`GET /api/v*/search`) — *"don't use it for a search-as-you-type feature, you would be blocked very quickly"*;
- **no limit** on writes;
- additional global limits independent of the IP → **HTTP 503**;
- exceeding them = **possible IP ban** (reversible by mailing `reuse@openfoodfacts.org`).

I hit this wall while writing this note: after a few search requests, the API returned an HTML page saying "Page temporarily unavailable". **The limit is not theoretical, and it does not always return JSON** — the HTTP client must handle an unexpected HTML response without crashing.

**Declaration form.** The documentation asks that an API usage form be filled in so the team can identify reuses and avoid accidental bans **[V]**. Five minutes, to be done.

**Warning about quality.** *"Data […] is provided voluntarily by users […] there are no assurances that the data is accurate, complete, or reliable. The user assumes the entire risk of using the data."* **[V]** See §4.

### 3.3 Actual coverage of French products

Measured live on 3 August 2026 via `GET /api/v2/search` **[V]**:

| Indicator | Value |
|---|---|
| Products in the database, all countries | **4,663,574** |
| Products declared as sold in France (`countries_tags_en=france`) | **1,255,052** |

**Verdict: usable, and amply so.** 1.26 million references for France, in a country that has a few tens of thousands of references in common circulation in mass retail: coverage of packaged national-brand products will be very good. OFF is a project of French origin, France is its historical and best-documented market.

> **Two counts of the same thing, and both are real.** [`technical-notes-ingestion.md`](technical-notes-ingestion.md) §3.6 gives **4.72 M** and **1,255,083** for the same day. That is not a contradiction between the two notes: the figures above were measured through `GET /api/v2/search`, the other pair was read off the `fr.openfoodfacts.org` landing page, and the two endpoints do not return the same number. *Which* population each one counts was never established, so neither figure supersedes the other. The France counts differ by 31 products, which is noise; the world counts differ by about 1.2%, which is not. The derived row counts inherit the split and round in opposite directions: **~1.26 M** in this document, **~1.25 M** in the other, off the same ~1.255 M measurement. Re-measure before sizing anything on either.

**An important nuance — presence ≠ completeness.** A record may exist with just a name and no nutritional data, no category, no photo. **I was unable to measure the completeness rates** (Nutri-Score filled in, front photo selected): the requests were blocked by the search rate limit **[NV]**. To be measured properly on a local JSONL dump rather than by hammering the API.

**Expected blind spots [NV, not quantified]:** regional private labels, small producers' products, short-supply-chain products, fine groceries, niche imported products.

### 3.4 Alternatives and complements

| Source | Model | Verdict for Chaudron |
|---|---|---|
| **CodeOnline Food (GS1 France)** | A database fed **by the brands themselves**, hence reliable and up-to-date data, specifically for France. "CodeOnline Search" API. **Access reserved to GS1 France members**; the price grid quotes a PREMIUM package at **€20,000 excl. VAT/year** **[S]** (<https://developers.gs1.fr/tarifs>) | **Out of reach.** It is nonetheless *the* qualitatively superior plan B if the project were to become commercial. |
| **Edamam Food Database** | Freemium, up to ~$999/month; ~700,000 UPC/EAN codes **[S, not verified at source]** | A predominantly American database, doubtful FR coverage. |
| **Nutritionix** | Enterprise, from ~$1,850/month **[S, NV]** | Same remark, and disqualified by price. |
| **Barcode Lookup / Go-UPC / EAN-DB** | Generic lookup (not nutritional). Barcode Lookup from ~$9/month, 1,000 lookups/day; EAN-DB at ~€0.005/code **[S, NV]** | Useful only as a **safety net for the product name** when OFF returns 404. Real marginal cost for household use. To be kept in reserve, not in v1. |
| **Open Prices** (an OFF project) | Open data, prices collected by the community | Out of scope for v1, but interesting later for a grocery budget. |
| **The user themselves** | Free | **This is the real plan B.** See §4.1. |

**Recommendation:** OFF alone in v1, with manual entry as the fallback. No paid API. If a coverage need appears, measure it first (count the real 404s on *your own* cupboard) before buying anything.

### 3.5 Backend caching strategy

**The most important architectural point of this section.**

The OFF documentation states: *"If your requests come from your users directly (ex: mobile app), the rate limits apply per user"* **[V]**. The corollary, often missed: **by centralising the calls in the FastAPI backend, all requests leave from a single IP — the 15 req/min limit becomes a global ceiling, shared by all of Chaudron's users.**

That is not a reason to call OFF from the browser (we would lose the cache, control of the User-Agent, and offline resilience). It is a reason for **the backend almost never to have to call OFF**.

The OFF documentation says as much itself: *"If you expect your app to generate a lot of API traffic, we **strongly encourage you to host a local instance** […] and use the daily exports to update your local database"* **[V]**.

**Proposed architecture:**

1. **A `product_cache` table in PostgreSQL** (in line with the project default), primary key = normalised EAN, **global and not scoped per household** (see §3.6). Columns: the useful denormalised fields, plus the raw JSON, plus `fetched_at`, `source` (`off` / `manual` / `import`), `off_last_modified_t`.
2. **A near-permanent positive cache.** A product record almost never changes. A long TTL (30 days), served **stale-while-revalidate**: the cached version is returned immediately, and refreshed in the background. The scan must *never* wait on the network.
3. **A short negative cache.** A 404 must be remembered — otherwise every re-scan of a missing product hits OFF again — but with a short TTL (24 h), since a product may be added to OFF in the meantime, including by the user themselves.
4. **A single exit point, with a limiter.** All OFF requests go through a single client carrying:
   - the compliant User-Agent (`Chaudron/x.y (contact@…)`);
   - a limiter at **10 req/min** (headroom under the 15);
   - exponential backoff on 429/503, and tolerance for **HTML** responses (see §3.2);
   - a short timeout (2–3 s): OFF is a non-profit's infrastructure, not a CDN.
5. **Pre-filling from the dump.** The decisive lever. OFF publishes a nightly MongoDB dump, a **JSONL gzip** export, a **Parquet** file on Hugging Face, a CSV (~0.9 GB compressed / ~9 GB uncompressed) and **delta exports over a rolling 14-day window** **[V]** (<https://world.openfoodfacts.org/data>). Importing the "sold in France" subset once with the 10 useful fields, then applying the deltas daily, brings the network hit rate close to zero. Estimated volume: ~1.26 M rows × a few hundred bytes ≈ **a few hundred MB in Postgres** — perfectly reasonable. A v2 job, not v1, but **design the table in v1 so it can be fed by both routes**.
6. **Images.** Do not hotlink `images.openfoodfacts.org` from the client on every list display: that is free load on the OFF infrastructure. Proxy + disk cache on the backend side, or download the thumbnail on first resolution. Keep the CC BY-SA attribution.
7. **Development against staging.** `world.openfoodfacts.net` (Basic Auth `off`/`off`) for all tests, as requested.

### 3.6 Articulation with multi-tenancy (ADR 0006)

[ADR 0006](adr/0006-multi-tenant-from-day-one.md) records a multi-tenant model from the first migration onwards, with a phase 2 of public multi-user opening. Two direct consequences for this module:

**a) The OFF cache is not household data.** The ADR imposes `household_id` on "every business table" and cites `UNIQUE (household_id, barcode)`. That constraint is correct for the **stocked item**, but **the product record coming from OFF is not household data**: it is a cache of an external reference base, identical for everybody. Caching it per household would multiply the calls to OFF by the number of households — exactly what the 15 req/min ceiling forbids.

The split to adopt is therefore **two distinct tables**:
- `product_cache` — **global, without `household_id`**, keyed on `barcode`, fed by OFF or by the dump. No personal data, hence no isolation stakes.
- `item` / `stock_entry` — **per household**, with `household_id`, carrying the local overrides (§4.5) and the stock.

This separation is not a breach of the ADR: it respects its spirit (all *business* data is scoped) while avoiding scoping a shared cache. **To be made explicit in the ADR or in the data model**, otherwise somebody will add a `household_id` to `product_cache` by mechanical application of the rule.

**b) The 15 req/min ceiling becomes a wall in phase 2.** For a single household, a decent cache is enough. In a public multi-user service, 15 product requests per minute shared between *all* households, from the backend's single IP, does not hold — and exceeding it exposes us to an IP ban that would cut the service for everyone at once. **Importing the JSONL dump is therefore not a convenience optimisation but a phase 2 prerequisite**, to be treated as such in the roadmap.

**c) ODbL share-alike wakes up in phase 2.** As long as Chaudron serves one household, the question of redistribution is theoretical. A public service that combines OFF with other product data sources falls within the scope of share-alike (§3.2). To be settled **before** opening up, not after.

---

## 4. What is going to go wrong

A deliberately pessimistic section. Each failure mode is followed by its UX fallback. It is those fallbacks that make the difference between a usable app and a demo.

### 4.1 The product is not in Open Food Facts (HTTP 404)

**Expected frequency:** low on national brands, **high** on regional private labels (MDD), local producers, fine groceries, imported products. **[NV]** — not quantified.

**Fallback:** the 404 must **never** be a dead end. The scan screen leads directly into a form pre-filled with the EAN, asking for only **three fields**: name, brand (optional), quantity. The product enters stock immediately. Optionally, offer to contribute to OFF (front photo + name): writes are not rate-limited **[V]** and the OFF documentation explicitly encourages this flow for "inventory apps". It is a virtuous circle: the user enriches the database they depend on.

**Anti-pattern to avoid:** an "unknown product" message with an "OK" button. That is what makes people abandon an inventory app on the third use.

### 4.2 The barcode is unreadable

Crumpled packaging (soft bags, frozen food), reflection on plastic film, a code partly covered by a price label, a narrow cylindrical bottle, low light (cupboard, pantry), shake, a code too small on individual packaging.

**Fallbacks, in order:**
1. **Guide before correcting.** An on-screen aiming frame, haptic/audible feedback on decoding, a contextual message after ~3 s of failure ("move closer", "avoid the reflection").
2. **Torch** — but the button present only if `"torch" in track.getCapabilities()`. **Absent on iPhone** (§2.1). That is an iOS/Android asymmetry that has to be accepted.
3. **Digital zoom** via `applyConstraints({ zoom })` if the capability exists — helps on small codes.
4. **Manual entry of the 13 digits**, always accessible one tap away from the scan screen. It is not an admission of failure, it is the indispensable safety net. **Validate the EAN-13 check digit locally** before any network call: that immediately detects a typo and avoids a misleading 404.
5. **Do not grind away in a loop.** After ~10 s without decoding, explicitly offer manual entry rather than leaving the camera running.

### 4.3 The product has no barcode at all

Loose fruit and vegetables, butchery, fishmonger, cut cheese, bakery, dry loose goods, garden produce and home preserves.

**This is not a marginal case.** In a real cupboard and fridge, this category is a significant share of the contents. A food stock app that can only add by scanning is structurally incomplete.

**Fallbacks:**
1. **Manual addition is a first-class path**, not a hidden option. A "+" button always visible next to the scan.
2. **A local catalogue of generic products**: "apples", "carrots", "minced beef", "bread". About thirty entries cover the essentials of household fresh produce. Reusable, with a unit (piece / g / kg) and a default shelf life.
3. **PLU codes** (Price Look-Up, the IFPS standard): the 4–5 digit labels on fruit and vegetables. 4 digits = conventional farming, 5 digits starting with 9 = organic **[S]** (<https://www.ifpsglobal.com/>). Optically recognisable but **it is not a barcode** — it would take OCR. **Not to be done in v1**; a selection list is faster for the user than approximate recognition.
4. **Recurring products**: offer at the top of the list what the user adds often. Two taps for "6 apples".

### 4.4 Variable weights and in-store internal codes

Barcodes prefixed **02** and **20–29** are *Restricted Circulation Numbers*: GS1 reserves them for retailers' internal use **[S]** (<https://www.gs1.org/docs/barcodes/SummaryOfGS1MOPrefixes20-29.pdf>, <https://www.gs1uk.org/knowledge-hub/barcodes/how-to-barcode-variable-measure-items>). They are found on everything weighed in store: butchery trays, cut cheese, fruit weighed at the till. Their typical structure encodes an internal item reference **and the price or the weight**, according to a convention **specific to each retailer**.

**Two direct consequences:**
1. These codes **are not in OFF and never will be.** Querying them guarantees a 404 and needlessly consumes the 15 req/min quota.
2. **The same product has a different code from one receipt to the next** (the price changes with the weight). Caching them would produce thousands of useless entries.

**Fallback:** detect the prefix **client-side** (`ean.startsWith("02") || /^2[0-9]/.test(ean)`), **do not call the backend**, and switch straight to the manual form with an honest message: "in-store internal code — describe the product". Decoding the embedded weight or price is possible but depends on the retailer: **not to be attempted in v1**.

### 4.5 The OFF record exists but is wrong or incomplete

Contributed data: a name in capitals, a misspelled brand, an inconsistent free-text `quantity`, absurd categories, an old version of a recipe, an obsolete Nutri-Score. OFF says so itself: *"no assurances that the data is accurate, complete, or reliable"* **[V]**.

**Fallback:** every imported record must be **locally editable**, and the local edit must **take precedence** over a later OFF refresh (a `source`/`overridden_at` column in the schema — to be provided for from the first migration; adding it later is painful).

**Parsing trap:** `quantity` is text (`"400.0 g"`, `"1L"`, `"6x125g"`, `"environ 250 g"`). Never assume a format. Parse as best you can, keep the original string, and **display the raw text on failure** rather than a `null` or a `0`.

### 4.6 iOS asks for camera authorisation again

See §2.2. **[V]** — WebKit bugs still active.

**Fallback:** do not trigger `getUserMedia()` on mounting the screen. First display an explicit state with an "Enable the camera" button: an authorisation request triggered by a user gesture is better understood, and if it is asked again, it does not look like a bug. Handle `NotAllowedError` with a message that explains *where* to re-authorise (Settings > Safari), and a "Try again" button. And keep the removal of `apple-mobile-web-app-capable` as a documented emergency switch.

### 4.7 Spurious scans and duplicates

A running camera decodes the same code 30 times a second. And a user putting the shopping away sometimes scans the same item twice — without knowing whether it is a duplicate or two units.

**Fallback:** debounce on the EAN (ignore the same code for ~2 s), and an explicit confirmation showing the recognised product with an incrementable quantity counter. The "shopping" mode (burst-scanning 20 items) and the "single addition" mode have different UX expectations — to be distinguished.

### 4.8 Erroneous decoding

Rare with ZXing's checksum validation, but possible on a partly masked code, and more likely if all formats are enabled.

**Fallbacks:** restrict `formats` to the retail formats (§1.3); **validate the EAN-13 check digit client-side** before lookup; require **two consecutive identical reads** before accepting (cheap, eliminates the bulk of false positives).

### 4.9 Open Food Facts is unavailable

A non-profit's infrastructure. Outages, maintenance, rate limit reached, **and HTML responses instead of JSON** (observed in §3.2).

**Fallback:** the Postgres cache absorbs the majority of cases. Otherwise, the addition is made with the EAN alone, in an enrichment queue to be processed later in the background. **OFF being unavailable must never prevent adding an item to stock.**

---

## 5. Recommended decisions

1. **One decoder only: `barcode-detector` (Sec-ant ponyfill, MIT, v3.2.1) on top of `zxing-wasm`.** No "native API if available" branch: it exists neither on iOS, nor on Firefox, nor on Chrome/Linux — hence never testable in local dev — and the performance gain does not justify a second code path. Formats restricted to `ean_13, ean_8, upc_a, upc_e, databar*`. WASM lazy-loaded when the scan screen opens (~450 kB gzip, measured) and **explicitly precached** by the service worker.

2. **Rule out `html5-qrcode`** (last npm release April 2023, declared maintenance mode, 441 open issues, bundles a frozen version of `@zxing/library`). Rule out `@zxing/library` directly (pure JS hence slower, 20 months without a release between 2024 and 2026, 170 issues).

3. **Validate the iOS camera on a real device before any commitment.** This is the only point that can call mobile viability into question. Support has existed since iOS 13.4 (#185448 RESOLVED FIXED), but the bugs around non-persistent permissions (#215884, reports up to January 2026) and a black video stream in standalone mode (#252465, regressions up to June 2025) are documented and live. **A throwaway prototype, half a day, before writing the module.** Keep the removal of `apple-mobile-web-app-capable` as a documented emergency switch.

4. **Offline-first, without Background Sync.** Not supported by Safari. A queue in IndexedDB, drained on `online` / `visibilitychange` / start-up. Scanning and decoding must work on a plane; only EAN → record resolution requires the network, and it is asynchronous by design.

5. **Open Food Facts on v3 (v3.6) only.** v2 is officially deprecated. Always `fields=` so as to request only what is needed. User-Agent `Chaudron/x.y (contact@…)` mandatory. Development against the staging `world.openfoodfacts.net` (Basic Auth `off`/`off`). Fill in the API usage declaration form.

6. **The rate limit is a global ceiling, not a per-user one.** 15 req/min for a single IP: by centralising in FastAPI, that is the limit for the **whole** application. Therefore: a Postgres cache with a long TTL, stale-while-revalidate, a short negative cache (24 h) on 404s, a single outbound client with a limiter at 10 req/min and backoff, tolerance for HTML responses. **In due course, pre-fill the database from the "France" JSONL dump + daily deltas** — OFF explicitly recommends it. Design the table in v1 to accept both feeding routes.

7. **The OFF cache is a global table, without `household_id`** — contrary to what the ADR 0006 rule would suggest by mechanical application. A product record is a shared external reference base, not household data; scoping it would multiply the calls to OFF by the number of households. Separate `product_cache` (global) from `item` / `stock_entry` (per household, carrying the local overrides), and make it explicit in the data model. **Two points must be dealt with before the public opening of phase 2: importing the dump becomes a prerequisite** (15 req/min shared between all households, with the risk of an IP ban cutting the service for everyone) **and ODbL share-alike stops being theoretical** if OFF is combined with other databases.

8. **OFF coverage is sufficient: 1,255,052 products sold in France out of 4,663,574 in total** (measured on 3 August 2026). No paid plan B in v1. CodeOnline Food (GS1 France) would be qualitatively superior but access goes through a GS1 membership with five-figure packages — out of reach. Measure the real 404 rate on your own cupboard before considering anything else.

9. **Manual entry is a first-class feature, not a fallback.** Products missing from OFF, unreadable codes, fresh produce without a barcode, in-store internal codes prefixed 02/20–29: these cases taken together represent a substantial share of a real household stock. **An app that can only add by scanning is unusable.** A schema corollary: records must be locally editable, and the local edit must take precedence over any later OFF refresh — provide for the column from the first migration.

---

## Sources

**Browser support**
- MDN browser-compat-data, `api/BarcodeDetector.json` — <https://github.com/mdn/browser-compat-data/blob/main/api/BarcodeDetector.json>
- MDN, `BarcodeDetector` — <https://developer.mozilla.org/en-US/docs/Web/API/BarcodeDetector>
- caniuse, BarcodeDetector API — <https://caniuse.com/mdn-api_barcodedetector>
- Chrome for Developers, Shape Detection API — <https://developer.chrome.com/docs/capabilities/shape-detection>
- caniuse, Background Sync API — <https://caniuse.com/background-sync>
- WebKit Features in Safari 26.0 — <https://webkit.org/blog/17333/webkit-features-in-safari-26-0/>
- WebKit Features for Safari 26.6 — <https://webkit.org/blog/18178/webkit-features-for-safari-26-6/>

**WebKit bugs**
- #185448 — getUserMedia in standalone mode (RESOLVED FIXED, iOS 13.4) — <https://bugs.webkit.org/show_bug.cgi?id=185448>
- #215884 — persistence of camera permissions in a PWA — <https://bugs.webkit.org/show_bug.cgi?id=215884>
- #252465 — black video stream in an iOS PWA — <https://bugs.webkit.org/show_bug.cgi?id=252465>
- #281848 — Shape Detection API non-functional on iOS (open) — <https://bugs.webkit.org/show_bug.cgi?id=281848>

**Libraries** (metadata collected on 2026-08-03 from `registry.npmjs.org` and `api.github.com`)
- zxing-wasm — <https://github.com/Sec-ant/zxing-wasm>
- barcode-detector — <https://github.com/Sec-ant/barcode-detector>
- @zxing/library — <https://github.com/zxing-js/library>
- html5-qrcode — <https://github.com/mebjas/html5-qrcode>
- @ericblade/quagga2 — <https://github.com/ericblade/quagga2>

**Camera**
- STRICH KB, Camera Access Issues in iOS PWA — <https://kb.strich.io/article/29-camera-access-issues-in-ios-pwa>
- Dynamsoft, camera focus control on web — <https://www.dynamsoft.com/codepool/camera-focus-control-on-web.html>
- Scandit, make a barcode scanner app performant — <https://www.scandit.com/blog/make-barcode-scanner-app-performant/>

**Open Food Facts**
- API documentation (source) — <https://github.com/openfoodfacts/openfoodfacts-server/blob/main/docs/api/index.md>
- Published version — <https://openfoodfacts.github.io/openfoodfacts-server/api/>
- Data, API and SDKs (exports, licences) — <https://world.openfoodfacts.org/data>
- Terms of use — <https://world.openfoodfacts.org/terms-of-use>
- ODbL 1.0 — <https://opendatacommons.org/licenses/odbl/1.0/> · DbCL 1.0 — <https://opendatacommons.org/licenses/dbcl/1.0/> · CC BY-SA 3.0 — <https://creativecommons.org/licenses/by-sa/3.0/>

**Barcodes and alternatives**
- GS1, prefixes 20–29 — <https://www.gs1.org/docs/barcodes/SummaryOfGS1MOPrefixes20-29.pdf>
- GS1 UK, variable measure items — <https://www.gs1uk.org/knowledge-hub/barcodes/how-to-barcode-variable-measure-items>
- IFPS (PLU codes) — <https://www.ifpsglobal.com/>
- GS1 France, CodeOnline for Developers — pricing — <https://developers.gs1.fr/tarifs>

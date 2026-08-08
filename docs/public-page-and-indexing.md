# Public page and indexing

This document explains why Chaudron serves **two documents governed by opposite
rules**, what each mechanism guarantees — and above all what none of them
guarantees.

Audience: anyone touching `frontend/index.html`, `frontend/app/index.html`,
`frontend/vite.config.ts` or `ops/Caddyfile.example`.

---

## 1. The starting point: “doing SEO” here would be a mistake

Chaudron is an application in which **every screen is private**. A household's
inventory is not content: it is personal data. It tells you the children's
allergies, the food budget, how many people live there, and the hours at which
someone does the shopping.

Optimising those screens for indexing would amount to inviting robots where
nobody should be. The work therefore splits in two, and both halves count:

| | `/` | `/app/` |
|---|---|---|
| Content | project presentation | a household's data |
| Indexing | **wanted** | **refused**, at three levels |
| JavaScript | none | the application bundle |
| Cache | revalidated | `no-store` |

What makes the split real is that it is **structural**: these are two distinct
HTML documents, produced by two distinct Vite entry points
(`build.rollupOptions.input`). The public page does not import a single line of
the bundle; it loads and reads even if the application build fails.

### The service worker used to make that last sentence false

Worth recording, because the mistake is easy to repeat and produced no symptom.

`frontend/vite.config.ts` declared `manifest.scope: '/app/'`, and that reads like
it bounds the service worker. It does not. `manifest.scope` describes which URLs
count as *inside the installed application* — navigation, the title bar. Which
URLs the worker **controls** comes from the second argument of
`navigator.serviceWorker.register()`, which `vite-plugin-pwa` takes from its own
top-level `scope` option, defaulting to Vite's `base`. The built artefact said so
plainly:

```
dist/registerSW.js → navigator.serviceWorker.register('/sw.js', { scope: '/' })
```

So after one visit to `/app/`, the public page was served by a worker built from
application code. Three things in the table above stopped being true at once:

- **JavaScript: none.** The document still contained none, but it was delivered
  by a script. `landing_csp`'s `script-src 'none'` no longer described what ran
  on `/`, and an XSS that reached any application chunk became persistent *and*
  reached the public page.
- **Cache: revalidated.** Caddy's `Cache-Control: must-revalidate` on `/` was
  bypassed by the worker's own cache, so an edit to the public page could not
  reach a returning visitor.
- **"It loads even if the application build fails."** It loaded *from* the
  application build.

**Resolved by narrowing the scope, not by dropping the claim.** The claim is the
reason the split exists; a version of this document without it would describe an
architecture with no purpose. What that costs is offline access to the landing
page — a project description, read once, by a visitor who is online by
definition. The application keeps offline entirely, which is the case that
matters. `index.html` is also out of the Workbox precache now: a worker cannot
serve a document it does not control, so precaching it only spent bytes.

Narrowing needs no server cooperation — a worker may always claim a scope at or
below its own path; only *widening* would need `Service-Worker-Allowed`.

Assert it on the artefact rather than on the config, which is what CI does:

```sh
grep -o "scope: '[^']*'" frontend/dist/registerSW.js   # expect scope: '/app/'
```

---

## 2. What prevents the application from being indexed

Three mechanisms, **none of which is an access control**. They are stacked
because they fail on different cases.

### 2.1 `<meta name="robots" content="noindex, nofollow, noarchive, noimageindex">`

In `frontend/app/index.html`. It only reaches a robot that has **fetched and
parsed** the document.

### 2.2 `X-Robots-Tag` on `/app/*`

In `ops/Caddyfile.example`. This is the half that counts: the header reaches a
robot that only issued a `HEAD`, and it applies to responses that are not HTML.

### 2.3 `robots.txt`

Generated at build time (`seoAssets` plugin, `frontend/vite.config.ts`), because
the `Sitemap:` line needs an absolute URL.

> **A `robots.txt` is NOT an access control.**
>
> It is a **request**, which well-behaved robots honour and which **nothing
> enforces**. Anyone can fetch every path listed in it — and *reading that file
> is precisely how you learn those paths exist*. A `robots.txt` is a sign, not a
> lock.
>
> What actually protects a household's inventory is **authentication** (not yet
> finished as of today, see the note below) and the reverse proxy. Nothing
> described in this section prevents anyone from reading anything. These
> mechanisms solve the problem “not ending up in a public index”, and nothing
> else.

### 2.4 The accepted trade-off between 2.1 and 2.3

The two partially contradict each other, and this is known: **a robot that obeys
`Disallow: /app/` never fetches the page, so it never sees the `noindex`.** A URL
with inbound links can then be indexed “blind”, without content.

This is accepted here, for a precise and verified reason: **`/app/` is a constant
path, with no identifier in it**. The application currently has no client-side
routing — tab navigation is a `useState` in `App.tsx`, the URL never changes. A
“URL only” entry would therefore disclose nothing.

> **To be revisited the day the application gains routing that puts an
> identifier in a URL** (`/app/items/<uuid>`, `/app/household/<id>`). At that
> point you have to choose: either drop `Disallow: /app/` so that the `noindex`
> actually gets read, or keep the URLs out of every external link.

---

## 3. What prevents household data from leaking through a URL

Three vectors were examined. Result verified against the code, as of the date of
this document:

| Vector | Status |
|---|---|
| **Page title** | Safe. `<title>Chaudron</title>` is static; no write to `document.title` anywhere in `src/` (`grep -rn "document.title" src/` → no result). |
| **Shareable URL** | Safe. No routing: no `history.pushState`, no `location.hash`, no `window.location` in `src/`. The active tab is React state. |
| **Outbound `Referer`** | Closed, at two levels. |

### `Referrer-Policy`

It is set **twice, at two levels**:

- `<meta name="referrer" content="no-referrer">` in both HTML documents —
  applies even if the proxy is misconfigured or absent (`vite preview`, a
  stopgap `python -m http.server`);
- `Referrer-Policy: no-referrer` in `ops/Caddyfile.example` — applies to images,
  stylesheets and `fetch` as well, not just to the document.

This is not decorative redundancy. The security audit notes under **AUD-017**
that Open Food Facts' `image_url` is dropped as-is into an `<img src>`, and that
Open Food Facts is a wiki: a hostile contributor makes the victim's browser issue
a request to the host of their choosing. Without `Referrer-Policy`, that request
carries the instance's URL. The `meta` tag covers the whole document — that image
included — with no need to touch the component.

> This **does not close** AUD-017, which remains a leak of IP address and
> `User-Agent` to a third-party host. The fix belongs on the backend: proxy the
> image, or validate scheme and host at ingestion.

---

## 4. The public page

`frontend/index.html`. Static, no JavaScript, inline CSS.

### Why the CSS is inline

Two reasons, in this order: **no render-blocking request** (measured LCP is 0.2 s
on desktop, 0.9 s on emulated mobile), and **no coupling with `src/styles/`**.
The public page and the application can evolve separately.

Consequence to be aware of: the proxy's CSP carries
`style-src 'self' 'unsafe-inline'` on this page. A `'sha256-…'` would be strictly
better and has not been set while the page keeps moving — a stale hash produces
an unstyled page, with no error anyone would notice.

### No web font is loaded

The brand typeface (URW Gothic) is not licensed for the web. The page uses the
system stack. That beats an `@font-face`: zero requests, zero `font-display`,
zero fallback flash, zero layout shift. Measured CLS is **0**.

The logo is an **image** (`icon.svg`, the mark alone) and the name is **real
text** — not `logo.svg`, whose wordmark is live text in a font the visitor's
browser does not have.

### The structured data

One JSON-LD block, two nodes:

- `SoftwareApplication` — name, description, `applicationCategory`, `license`
  (AGPL-3.0), `operatingSystem`, `featureList`, `screenshot`, `offers` at €0,
  `sameAs` pointing at the repository;
- `SoftwareSourceCode` — carries `codeRepository`.

**`codeRepository` does not exist on `SoftwareApplication`**: schema.org defines
it on `SoftwareSourceCode`. That is why the repository is expressed by a second
node linked through `mainEntityOfPage`, rather than by an invalid property hung
off the application. Verified against the official vocabulary, not from memory —
see §6.

---

## 5. The domain is not known — `VITE_SITE_URL`

The canonical URL, the Open Graph tags, the JSON-LD and `sitemap.xml` all need an
**absolute** URL. The domain is a deployment decision, not a code constant.

```sh
VITE_SITE_URL=https://chaudron.mondomaine.tld npm run build
```

Without the variable, a `build` now **fails**. It used to warn and fall back to
`https://chaudron.example` — a domain reserved by RFC 2606, hence unresolvable,
so an oversight pointed at nowhere rather than at somebody else's site.

That was the right fallback and the wrong loudness. The warning was one line in
several hundred of build output, and the `dist/` it produced looked entirely
normal: the domain only appears inside `canonical`, `og:url`, the JSON-LD and the
`Sitemap:` line, none of which is visible on the rendered page. Deployed as-is,
the public page told every crawler that the canonical copy of itself lived
somewhere else, and the first symptom would have been a ranking that never
arrived.

To build locally without deploying — the one case the variable is genuinely not
known for:

```sh
VITE_ALLOW_DEFAULT_SITE_URL=1 npm run build
```

which restores the old behaviour, warning included, under a name that cannot end
up in a deployment script by accident.

`sitemap.xml` deliberately carries no `<lastmod>`: the only value the build could
put there is its own date, which would claim the page changes on every rebuild.
An inaccurate `lastmod` is ignored by robots; its absence therefore says strictly
more.

---

## 6. How all of this is verified

### JSON-LD against the schema.org vocabulary

This is not a lint: every type and every property is looked up in the official
dump, and every property is checked as belonging to the domain of the type it is
used on, walking up the class hierarchy.

```sh
curl -sSLO https://schema.org/version/latest/schemaorg-current-https.jsonld
# the script is reproduced in the SEO task report
python3 validate_jsonld.py schemaorg-current-https.jsonld https://votre-instance/
```

### Headers and robots files, on a real instance

```sh
cd frontend && VITE_SITE_URL=https://chaudron.mondomaine.tld npm run build
podman run --rm -d --name chaudron-web -p 127.0.0.1:8477:8477 \
  -e CHAUDRON_SITE_HOST=":8477" -e CHAUDRON_API_UPSTREAM="127.0.0.1:9" \
  -v ./ops/Caddyfile.example:/etc/caddy/Caddyfile:ro,Z \
  -v ./frontend/dist:/srv:Z docker.io/library/caddy:2-alpine

curl -sSI http://127.0.0.1:8477/app/   | grep -i x-robots-tag
curl -sS  http://127.0.0.1:8477/robots.txt
```

> Do not mount the same directory with `:Z` into two containers at once: `:Z`
> applies a **private** label, and the second container takes access away from
> the first. The symptom is a `403` with `open /srv: permission denied` in
> Caddy's log.

### Lighthouse

```sh
CHROME_PATH=~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome \
  npx lighthouse http://127.0.0.1:8477/ --output=html --output-path=./lh.html \
  --chrome-flags="--headless=new --no-sandbox"
```

> **The SEO score caps at 92, and that is normal.** Lighthouse fetches
> `robots.txt` *from the page's context*, which the public page's CSP
> (`connect-src 'none'`) refuses; the audit fails with `Fetch of robots.txt
> failed: CSP violation`. A real robot never does that — it requests
> `/robots.txt` directly, outside any page context, where no CSP applies.
> Verified: the same build served with `connect-src 'self'` climbs back to 100,
> and `robots.txt` is the only audit that changes.
>
> **Do not loosen `connect-src` to please a measurement tool.**

---

## 7. The screenshots

`tools/screenshots.py` produces them from a **real instance**: it opens a real
browser on `/app/`, **authenticates** with the account that
`backend/scripts/seed.py` creates, and photographs what it finds. No image in
this repository is a mockup.

The file names are a **three-way contract**: the PWA manifest
(`frontend/vite.config.ts`), the public page (`frontend/index.html`, both `<img>`
tags **and** JSON-LD) and the README all point at the same ones. You re-run the
tool, you do not rename.

| Set | Files (`frontend/public/screenshots/`) | Size |
|---|---|---|
| `narrow` | `inventory`, `recipes`, `courses`, `add` | 440 × 952 |
| `wide` | `inventory-wide`, `recipes-wide`, `courses-wide` | 1280 × 800 |

Two extra captures stay in `docs/screenshots/` and are not published:
`degraded-banner` and `inventory-dark`. An installed application never renders
them, and preloading them would make every install pay for images nobody looks
at.

Three constraints, learned the hard way:

- **Both sets are necessary.** Chrome Android reads `narrow`; Chrome desktop
  shows **no** rich install prompt at all without a `wide` set.
- **A given set must keep a single aspect ratio.** One entry deviates and the
  whole set is ignored, with no message.
- **~2× the display size, in WebP.** The previous set was PNG at 780 × 1688 for
  a box three times narrower — Lighthouse flagged 99 kB of fat on
  `inventory.png` alone. The current set fits in 19–55 kB per image.

Still to do: **pin a CSP hash** for the inline `<style>` once the page has
stopped moving.

# 11. TLS termination, static hosting and a verified deployment gate

Date: 2026-08-04

## Status

Accepted

Relates to [ADR-0004](0004-pwa-not-native.md) (PWA, not native),
[ADR-0006](0006-multi-tenant-from-day-one.md) (multi-tenant from day one) and
[ADR-0007](0007-byok-and-local-inference.md) (BYOK and local inference).

## Context

Chaudron shipped a container image, three quadlets and a fifteen-minute
continuous-deployment loop. It did not ship a way to reach any of it over the
network. The gap was not partial — it was total, and `ops/` was internally
consistent about it: no Caddyfile, no nginx configuration, no ACME, and no
occurrence of the strings "TLS", "HTTPS", "certificate" or "443" anywhere in
`ops/README.md`. No ADR covered deployment at all.

Four separate consequences followed from that one absence, and they are worth
separating because only the first is obvious.

**The application does not work without it.** `frontend/vite.config.ts`
registers a Workbox service worker, and a service worker only registers in a
*secure context*. Off `localhost`, over plain HTTP, the PWA has no offline
mode, no install prompt and no precache — the whole point of ADR-0004's "PWA,
not native" is unavailable. `getUserMedia` is secure-context only too, so the
barcode scanner never gets a camera either. This is not hardening; it is the
product not running.

**Headers that could not take effect.** `api/middleware.py` emits
`Strict-Transport-Security` in production. Over HTTP a browser ignores it, by
specification. The backend was emitting a control that could never engage.

**An origin with no owner.** `frontend/dist/` had no host. Nothing served it,
so nothing set a `Content-Security-Policy`, an `X-Content-Type-Options` or an
`X-Robots-Tag` on it. That is the origin that matters most in this application:
it is where the service worker lives, which means a cross-site script there
becomes *persistent* — poisoned into the Workbox precache and re-served offline
— and it is where `localStorage` keeps a household's preferences. The API had a
careful header policy; the origin with the larger blast radius had none,
because no component existed to have one.

**A deployment loop whose trust ended at a registry token.** `publish.yml`
signs each published digest with cosign, keyless, on the digest rather than the
tag — correctly. Nothing ever verified it: `cosign verify` appeared nowhere in
the repository. Meanwhile `chaudron.container` declared `Image=…:latest` with
`AutoUpdate=registry` and the timer polled every fifteen minutes. So anyone
holding `packages: write` on GHCR could land an image in production inside a
quarter of an hour with no human in the loop. `ops/README.md` described this
accurately, diagnosed the technical cause precisely, listed four ways out — and
implemented none of them.

## Decision

**Caddy terminates TLS, serves the PWA and proxies the API, on one origin.**

A single reverse proxy container (`ops/chaudron-proxy.container`,
`ops/Caddyfile.example`) obtains and renews Let's Encrypt certificates
automatically, serves `frontend/dist/` from a read-only volume, and proxies
`/v1/*`, `/healthz`, `/readyz` and `/caldav/*` to the API container over the
shared `chaudron-net` network.

**The PWA and the API share an origin.** The frontend is served from `/` and
`/app/`, the API from `/v1/`. This is a deliberate choice over the split-origin
alternative (`app.example.tld` + `api.example.tld`), and the reason is CORS:
the session travels in a cookie, so a split origin requires
`CHAUDRON_CORS_ORIGINS` to name the frontend and `CHAUDRON_CORS_ALLOW_CREDENTIALS`
to be true — a credentialed cross-origin configuration that must be exactly
right, forever, and whose failure mode is either "every call answers 401" or
"any site can read a household's data". Same-origin makes that configuration
empty, which is the only configuration that cannot be got wrong.

**The proxy owns the frontend's security headers.** CSP, `nosniff`, HSTS,
`Referrer-Policy`, `Permissions-Policy` and `X-Robots-Tag` are set there,
because that is where the frontend is served from. API responses keep their own
headers; `defer` on every header block is what stops the proxy and the
application from each writing a second copy of the two they share.

**The proxy bounds request bodies.** 2 MiB, sized against the largest
legitimate v1 request (a 1 MiB shopping-list document plus its multipart
envelope), enforced before a byte reaches Python.

**Deployment is gated on a cosign verification.** `podman-auto-update.timer` is
masked and no quadlet carries `AutoUpdate=`, so `podman auto-update` has no
candidates at all. `chaudron-verified-update.timer` runs
`ops/verified-auto-update.sh` every fifteen minutes: pull, re-resolve the digest
from the *local* store, `cosign verify` it against `--certificate-identity`
`…/publish.yml@refs/heads/main` and `--certificate-oidc-issuer`
`https://token.actions.githubusercontent.com`, and only on success
`systemctl --user restart chaudron.service`.

That last step used to be `podman auto-update`, which re-queried the registry and
pulled the image itself — so the digest it deployed was never the digest that had
just been verified, and the verification round trip was the window. Restarting
deploys the local `:latest`, because quadlet emits no `--pull` and Podman's
default pull policy is `missing`. The cost is that Podman's automatic rollback
goes with it; the gate waits for the container's health check and fails loudly
instead. See `ops/README.md` §5.2.

## Alternatives considered

**nginx.** The default answer, and rejected on total cost of ownership rather
than capability. Automatic certificate issuance and renewal means certbot as a
second component with its own timer, its own renewal hooks and its own failure
mode — a renewal that fails silently until the certificate expires. Caddy makes
ACME part of the server: the same process that serves traffic holds the
certificate lifecycle, and a renewal failure is visible in the same logs as
everything else. For a self-hosted deployment maintained by one person, that is
the trade worth making. nginx wins on raw throughput at volumes this
application will not see.

**Traefik.** Designed for dynamic service discovery, which this deployment does
not have: three containers whose names are known at write time. Its
configuration surface is a cost paid for a benefit that does not apply here.

**Terminating TLS at the API.** uvicorn can serve TLS directly, which would
remove a container. It would also leave `frontend/dist/` unhosted — the finding
that matters most — put certificate renewal inside the container that
auto-updates every fifteen minutes, and make the API's process the thing facing
the internet directly. Rejected.

**A CDN or a tunnel in front (Cloudflare, ngrok).** Moves TLS termination to a
third party, which for an application built around "you host it, you hold your
own data" (ADR-0007) contradicts the premise: the operator's TLS terminator
would see every household's plaintext. Nothing prevents an operator choosing
this; it is not the documented default.

**For the deployment gate**, the four options `ops/README.md` had already
enumerated:

1. *Verify by hand after each deploy.* Cheap and honest, and it will be skipped
   on the day it matters. Rejected: a control that depends on remembering is
   not a control.
2. *Gate the update with a script.* **Chosen.** Keeps keyless signing — no
   long-lived private key to store, steal or rotate — and turns the signature
   from evidence available after the fact into a precondition.
3. *Sign with a key pair and enforce through `policy.json`.* Podman would then
   refuse a wrongly-signed image at pull time with no host-side scripting,
   which is architecturally cleaner. The cost is a long-lived private key in a
   GitHub secret — precisely the thing keyless signing exists to avoid — plus a
   rotation procedure nobody has written. Rejected for now; revisit if the
   scripting proves fragile.
4. *Stop following a mutable tag.* Pin the quadlet to a digest and make
   deployment deliberate. This is the only option that removes the unattended
   path entirely, and it is the right answer the day this instance has users
   who would notice a bad deploy. Rejected today because it trades the
   fifteen-minute loop for a manual step on every release, and the project is
   still a single maintainer shipping continuously.

Worth stating plainly: option 3 was not available as a drop-in for the *keyless*
signature that already exists. Podman can enforce sigstore signatures through
`/etc/containers/policy.json`, but the `fulcio` stanza matches a signer by
`subjectEmail`, and a GitHub Actions certificate carries no email — its identity
is a URI SAN. There is no field in `containers-policy.json(5)` that matches a
URI SAN, so the policy cannot express "signed by this workflow". Verified
against podman 5.6.0 on Rocky 10. That limitation is what makes the host-side
script the pragmatic choice rather than a workaround for laziness.

## Consequences

### What this buys

- The PWA works: secure context, service worker, camera, install prompt.
- The `Strict-Transport-Security` header the API was already emitting now
  engages.
- The frontend origin has a CSP, and it is a strict one — `default-src 'none'`
  with `script-src 'self' 'wasm-unsafe-eval'` and no `'unsafe-inline'` on
  scripts. The landing page gets `script-src 'none'`, which turns "this page has
  no JavaScript by design" into something a browser enforces.
- CORS stays out of the deployment entirely.
- A registry token alone no longer reaches production. Landing an image now
  requires producing a cosign signature whose certificate names this
  repository's `publish.yml` on `refs/heads/main` — which a stolen GHCR token
  cannot do.

### What it costs

- A fourth container, and a fourth thing that can break. If Caddy is down,
  everything is down — including the ACME challenge path needed to renew the
  certificate that would let you notice.
- `~/chaudron/data/caddy` becomes the second most valuable directory on the
  host after the database: it holds the instance's TLS private key. It is in
  the backup set for availability, and it is a reason the backup destination
  must itself be trusted.
- Ports 80 and 443 under rootless Podman require
  `net.ipv4.ip_unprivileged_port_start=80`, a system-wide sysctl that lowers the
  privileged-port boundary for *every* user on the host. On a single-purpose
  machine that is acceptable; on a shared one it is not, and the alternative is
  running the proxy rootful or fronting it with something that already holds the
  ports.
- The proxy needs a static address on `chaudron-net`
  (`10.89.7.10`), because `FORWARDED_ALLOW_IPS` in the API names it literally.
  The network must therefore be created with an explicit subnet. An existing
  deployment has to recreate it.
- CSP is a claim about what the frontend does, maintained by hand. A frontend
  change that adds an external font, an analytics script or a third-party image
  host will be blocked by the browser and must be reflected here. That is the
  intended behaviour and it will still read as "the proxy broke the app" the
  first time.
- `cosign` becomes a hard dependency of the deployment host. The update script
  distinguishes "cosign is missing" from "the signature is bad" and refuses to
  update in both cases, which is the safe direction but does mean a missing
  binary silently freezes deployments until somebody reads the unit status.

### What is explicitly not solved

- **Signing proves origin, never intent.** A legitimately signed image built
  from a compromised `main` verifies perfectly. Branch protection is the control
  for that, and it lives on GitHub, not in `ops/`.
- **No human between a merged pull request and production.** The gate answers
  "is this ours?", never "is this right?". The health check answers "does it
  start?". Nothing answers the third question. When a second maintainer or real
  users arrive, put a GitHub Environment with a required reviewer in front of
  `publish.yml` — do not lengthen the timer, which only makes the same
  unreviewed change arrive later.
- **`'unsafe-inline'` remains in `style-src`.** The landing page's CSS is inline
  by design. Hashing it is strictly better and was deferred deliberately: the
  page is under active edit and a stale hash renders an unstyled page with no
  error anyone would notice. Pin the hash once the markup settles.

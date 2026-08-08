# Technical note — Automatic stock ingestion and shopping-list export

**Project**: Chaudron (household food stock management PWA — FastAPI / PostgreSQL backend)
**Status**: feasibility note, for decision
**Written**: 3 August 2026
**Every page cited was consulted on 3 August 2026.** Prices, quotas and policies move fast: the figures below have a shelf life of a few months, not years.

---

## 0. Scope and method

### 0.1 What is already settled and is not reopened here

**Integrating retailer drive accounts (Courses U, Intermarché Drive, Chronodrive, Auchan Drive…) is ruled out.** None of these retailers exposes a public API; access would go through reverse-engineering private endpoints, fragile by construction and contrary to their terms of service. The point is settled by **[ADR-0002](adr/0002-no-retailer-drive-integration.md)** (accepted on 2026-08-03) and is not reopened here. This note therefore assumes that purchase data must come **from the user** (a summary email they already receive, or a photo of their receipt), never from scraping a retailer.

This note **works up routes 3 and 4 of ADR-0002** (receipt photo, forwarded-email capture) and documents the shopping-list export, which no ADR covers to date.

One signal pointing the same way for the future: the anti-waste law abolished automatic printing of the till receipt on 1 August 2023, which structurally pushes retailers towards the dematerialised receipt — hence towards email and QR codes, that is, towards route no. 1 of this note.
Source: <https://www.presse-citron.net/le-ticket-de-caisse-disparait-quel-est-son-remplacant/>

### 0.2 Ties to decisions already taken

This note does not start from a blank page. It must be read together with:

| Document | What it imposes on this note |
|---|---|
| [ADR-0002](adr/0002-no-retailer-drive-integration.md) | No retailer drive. The per-household address is specified as `<household_token>@inbox.<domain>` — that is the format §1 implements. |
| [ADR-0005](adr/0005-llm-provider-abstraction.md) | **The model provider is data, not a constant.** Five adapters (Anthropic, OpenAI, Gemini, Mistral, Ollama), degradation by detected capabilities. §3 therefore cannot "pick a model": it can recommend a **default** and quantify reference points. |
| [ADR-0007](adr/0007-byok-and-local-inference.md) | **BYOK: the household pays.** No shared budget. Two configurations keep the data under EU jurisdiction: `byok` Mistral (EU-hosted) and `ollama` (nothing leaves). Local inference is not a degraded fallback. |
| [ADR-0006](adr/0006-multi-tenant-from-day-one.md) | `household_id` on every business table — hence on the inbox address, on the receipts and on the alias table of §3.6. |
| [ADR-0008](adr/0008-open-food-facts-integration.md) + [`technical-notes-scanning.md`](technical-notes-scanning.md) | The Open Food Facts strategy is **already settled** (cache first, shared catalogue `household_id IS NULL`, API v3, local dump as a phase 2 prerequisite). §3.6 plugs into it instead of replaying it. |

### 0.3 The four questions covered

1. Receiving inbound email (dedicated per-household address + webhook) — **recommended route**
2. Reading the mailbox directly (Gmail API / IMAP) — alternative route, documented for comparison
3. Parsing till receipts with a multimodal model
4. Exporting the shopping list to the apps people already use

### 0.4 Limits of this research

Verification was carried out by consulting official pages directly. Several sites rendered 100% in JavaScript (Stalwart docs, Brevo pricing, `dev.mailjet.com`, OVH/Infomaniak portals) and a few pages returning 403 could not be read. **Every unverified point is flagged explicitly inline**, and a summary list appears in §6. Unsourced assertions are engineering reasoning, flagged as such.

---

## 1. Receiving inbound email — the recommended route

### 1.1 The principle adopted

Each household is assigned a dedicated address — format fixed by ADR-0002: `<household_token>@inbox.<domain>`, for example `u7f3a@inbox.exemple.fr`. The user creates a **forwarding rule** in their mail client targeting retailer senders, and the forwarded mail lands on an HTTP webhook that parses it.

That is simple to describe. Three things complicate it, and they decide the choice of provider.

### 1.2 The structuring point: forwarding breaks SPF, and some providers reject on that basis

This is **the** selection criterion, and it is almost always ignored by comparison articles.

- **SPF fails systematically on a forward.** SPF is a list of servers authorised to send for a domain; the Google server re-forwarding Carrefour's mail is obviously not on it, and maintaining a list of forwarders is not an option.
  Source: <https://dmarcian.com/forwarding-and-dmarc/>
- **DKIM survives** if the forwarder modifies neither the body nor the signed headers. Google documents that touching MIME boundaries, the subject or the `To`/`Cc`/`Date`/`Message-ID` headers breaks the signature, and that "*Messages that don't pass DKIM are more likely to be sent to spam*".
  Source: <https://support.google.com/a/answer/175365?hl=en>
- **Consequence**: "*for forwarded email, your DMARC compliance is equal to the "survival" of your DKIM signatures*" (dmarcian, same URL). A sender that signs well gets through; one that signs badly is in total DMARC failure.
- **SRS does not repair what people think it does.** The Sender Rewriting Scheme rewrites the envelope so SPF passes, but DMARC alignment stays broken since the visible `From:` does not change. Microsoft says so in black and white: "*SRS rewriting doesn't resolve the issue of forwarded messages not passing DMARC checks*".
  Source: <https://learn.microsoft.com/en-us/exchange/reference/sender-rewriting-scheme>
  In any case SRS does not concern us directly: Google is the one forwarding, we are on the receiving side.
- **ARC is the real saviour.** Google applies an ARC chain when it forwards, letting the final receiver trust the original authentication verdict. That still requires the inbound provider to honour it.

**Direct application — this is what disqualifies Cloudflare for this use case**:

> Cloudflare requires inbound mail to pass authentication ("*The email must either pass SPF or be correctly signed with DKIM*") and **applies the sender's DMARC policy**: "*messages failing sender DMARC policies are rejected*".
> Source: <https://developers.cloudflare.com/email-routing/postmaster/>

There are public complaints about exactly this scenario:
<https://community.cloudflare.com/t/emails-forwarded-from-gmail-are-being-dropped-due-to-dmarc-checks-failing/849579>
<https://community.cloudflare.com/t/forward-to-gmail-dmarc-failure/565909>

Conversely, Postmark, Mailgun, SendGrid and Amazon SES **do not reject outright on DMARC**: they compute a score and let us decide. And a self-hosted server gives us total control — we choose to reject nothing. This is a substantive argument in favour of self-hosting for this particular case.

### 1.3 Onboarding friction to budget for (often underestimated)

| Constraint | Detail | Source |
|---|---|---|
| **Gmail requires verification of the destination address** | "*After you add a forwarding email address, we send a verification link to the address. After you verify, you can forward messages*" | <https://support.google.com/mail/answer/9414102?hl=en> |
| **Gmail does not forward spam** | "*We forward all new messages to the account, **except for spam***" | ditto |
| **Selective forwarding is possible** | A Gmail "Forward it" filter allows forwarding only the retailer's mail — that is what should be recommended to the user (GDPR minimisation) | ditto |
| **Microsoft 365 blocks external auto-forwarding by default** | Outbound anti-spam policy; admin action required | <https://woshub.com/enable-external-forwarding-microsoft-365-exchange/> — ⚠️ blog, **not confirmed on learn.microsoft.com** |
| **Outlook.com requires 2FA** to enable forwarding | | <https://support.microsoft.com/en-us/office/turn-on-or-off-automatic-forwarding-in-outlook-com-6246987c-6c8f-4144-b255-14fc07007dad> |

**Functional requirement that follows**: the Gmail confirmation mail arrives at the dedicated address **before** forwarding is active. Our webhook must know how to recognise it and surface the code/link in the UI, otherwise onboarding is blocked. This is not a detail, it is a story in its own right.

### 1.4 Comparison of managed providers

| Provider | Entry tier | Webhook format | Attachments | Webhook auth | Max size | EU residency | DMARC rejection? |
|---|---|---|---|---|---|---|---|
| **CloudMailin** | **free, 10,000/month** (512 KB max); useful at $45/month | normalised JSON / multipart / **raw MIME** | base64 **or URL** to a store; S3/Azure/GCS upload | 🟠 basic auth (signature **deprecated**) | 512 KB → 50 MB depending on plan | ✅ **forceable by DNS** | no |
| **ImprovMX** | $9/month (Premium; webhooks **excluded from the free tier**) | full JSON + `?raw_mime=true` | inline base64 + `inlines[]` with `cid` | 🔴 **none** (IP `15.237.103.194`) | not documented | ✅ **FR datacentres (OVH)** | no |
| **Amazon SES** | $0.10/1,000 received + $0.09/1,000 256 KB chunks | ❌ no native webhook — SNS / Lambda / S3 | via S3 | 🟢 SNS signature / IAM | **150 KB via SNS**, 40 MB via S3 | ✅ **Paris (eu-west-3)**, Frankfurt… | no (verdicts exposed, decision left to us) |
| **Mailgun** | **free, 1 route, 100/day**; Foundation $35 | parsed multipart, or raw MIME if the URL ends in `mime` | multipart + `content-id-map`; `store()` 3 days | 🟡 HMAC (`timestamp`/`token`/`signature`) | not documented | ⚠️ EU sending announced, **EU MX not verifiable** | no |
| **Postmark** | **$16.50/month** (Pro — inbound absent from Free and Basic) | rich JSON (`TextBody`, `HtmlBody`, `StrippedTextReply`, `MailboxHash`) | **inline base64** | 🟠 IP allowlist (4 US IPs) | ⚠️ **not documented** | ❌ **no mention at all** | no |
| **ForwardEmail** | **free** (webhooks included, configured by DNS TXT) | `mailparser` JSON + `raw`, with `spf`/`dkim`/**`arc`**/`dmarc` verdicts | included (`?attachments=false`) | 🟢 **HMAC `X-Webhook-Signature`** (paid) + rDNS | **50 MB** | ❌ **Denver, Colorado** | no |
| **Brevo** | 2026 prices **not extractable** | very rich JSON + `ExtractedMarkdownMessage` (signature stripped by ML), rspamd `Spam.Score` | metadata + `DownloadToken` | 🟡 IP / basic / **bearer** / headers | not documented | 🇫🇷 reputed, **not confirmed as of today** | no |
| **Resend** | free 3,000/month (in+out combined) | ❌ **metadata only** — 2 to 3 API calls for the body and the attachments | `download_url` valid 1 h | 🟢 **Svix HMAC** (anti-replay) | not documented | ❌ "*All account data … is stored in the United States*" | no |
| **Mailtrap** | free 4,000/month; production inbound from Basic $15 | metadata + API | via API | 🟢 **HMAC-SHA256** | not documented | not mentioned | no |
| **Cloudflare Email Routing** | **free** | ❌ **raw MIME**, to be parsed yourself (`postal-mime` recommended) | to be extracted yourself | 🟢 our own secret (it is our Worker doing the `fetch`) | **25 MiB** | — | 🔴 **YES — disqualifying** |
| **SendGrid Inbound Parse** | ⚠️ **pricing not verifiable** | `multipart/form-data`, raw MIME option | not URL-encoded (documented trap) | 🔴 **none** documented | **30 MB** (2.5 MB for the antispam) | ⚠️ not verifiable | no |
| **Mailjet Parse API** | Free 6,000/month — ⚠️ docs say "Crystal and above", **a plan that does not exist in the price grid** | JSON + `Parts[]` + `AttachmentN` base64 | base64 | 🟠 basic auth | not documented | ⚠️ **not verifiable** (`/legal/dpa/` redirects to sinch.com) | no |
| **Scaleway TEM** | — | ❌ **no inbound at all**: "*you can only **send***" | — | — | — | ✅ fr-par | — |

Main sources: <https://postmarkapp.com/pricing> · <https://postmarkapp.com/developer/webhooks/inbound-webhook> · <https://www.mailgun.com/pricing/> · <https://documentation.mailgun.com/docs/mailgun/user-manual/receive-forward-store/receive-http/> · <https://www.twilio.com/docs/sendgrid/for-developers/parsing-email/setting-up-the-inbound-parse-webhook> · <https://developers.cloudflare.com/email-routing/limits/> · <https://developers.cloudflare.com/email-routing/email-workers/> · <https://www.cloudmailin.com/plans-and-pricing> · <https://docs.cloudmailin.com/http_post_formats/> · <https://improvmx.com/guides/webhooks/> · <https://improvmx.com/pricing/> · <https://forwardemail.net/en/pricing> · <https://developers.brevo.com/docs/inbound-parse-webhooks> · <https://resend.com/docs/webhooks/emails/received.md> · <https://resend.com/docs/dashboard/domains/regions> · <https://docs.aws.amazon.com/ses/latest/dg/quotas.html> · <https://aws.amazon.com/ses/pricing/> · <https://www.scaleway.com/en/transactional-email-tem/>

#### Salient points from the table

- **Postmark**: inbound only appears on Pro and Platform according to the grid consulted. The real entry ticket is therefore **$16.50/month**, not $0. ⚠️ The page does not say whether inbound consumes the 10,000-email quota, and **no max size could be found** (the relevant support articles return 404). Retries: 10 attempts at increasing intervals, **and a 403 stops the retries permanently** — never return 403 on a transient error.
- **Mailgun**: the number of routes is **not** the number of addresses. A single `catch_all()` or `match_recipient(".*@inbox.exemple.fr")` is enough, so the free plan (1 route) is technically viable under 100 mails/day. Raw MIME mode is triggered by the **URL** (if it ends in `mime` or `raw-mime`), not by size. `store()` retains for 3 days and notifies with a retrieval URL — useful for large attachments that would time out our endpoint. ⚠️ The page describing the HMAC algorithm returns 404 today: the mechanism exists (`timestamp`, `token`, `signature` are in every POST) but **its exact terms could not be re-confirmed**.
- **Cloudflare**: free and technically elegant, but **three cumulative defects** — DMARC rejection (§1.2), a ceiling of **200 routing rules per domain** (a wall for "one address per household"; forces a catch-all + Worker), and raw MIME to parse yourself. ⚠️ The question "does the domain have to be on full setup on Cloudflare nameservers?" **could not be settled** on an official page; it is very likely since Cloudflare has to manage the MX records, but it is not sourced.
- **SendGrid**: the pricing pages loop through redirects (`sendgrid.com/pricing` → `twilio.com/en-us/sendgrid` → …). **Impossible to establish the 2026 grid or to confirm the existence of a permanent free plan** on an official page. The product page mentions only a "*free trial — no credit card required*", which *suggests* a switch to a trial without proving it. A competitor blog dated 27 February 2026 (<https://www.pingram.io/blog/best-inbound-email-notification-apis>) announces "100 emails/day for 30 days" then $19.95/month — **an indication, not a fact**. Add to that the total absence of documented webhook security. To be ruled out.
- **Resend**: the webhook does **not** contain the mail — "*Webhooks do not include the email body, headers, or attachments, only their metadata*". A second call is needed for the body, a third per attachment. And "*All account data, including email metadata, logs, and API records, is stored in the United States regardless of the sending region you select*": the regions concern **sending only**. The Pingram blog announcing "EU region available (Ireland)" for Resend is misleading with respect to the official docs.
- **CloudMailin**: notable HTTP→SMTP behaviour — it **does not retry** itself, it translates our return code into an SMTP response (4xx → SMTP 554 permanent rejection + notification to the sender; 5xx → SMTP 450, the sender will retry). Clean, but an accidental 500 from our app bounces the ball back to Gmail for days. ⚠️ **`OpenAI (USA)` appears in the list of sub-processors** for "*Analysis and content detection*" (<https://www.cloudmailin.com/privacy>) — to be clarified contractually before routing personally identifiable receipts through it.
- **Mailjet**: two unresolved inconsistencies. The official docs say the Parse API is reserved for "Crystal and above" plans, **yet no "Crystal" plan exists in the public grid of 3 August 2026** (Free / Starter $9 / Essential $17 / Premium $27 / Custom). And European hosting, its very reputation, **is not verifiable on any official page** (`/legal/dpa/` redirects to sinch.com, `/gdpr/` and `/legal/` return 403).
- **Brevo**: an honest warning on their part, to be built into our design — "*100% success rate on inbound parsing is impossible*". Plan a fallback path for when `ExtractedMarkdownMessage` is empty or truncated.

**Ruled out immediately** (off-topic or absurd business model): Mailparser.io ($29.95/month for **250 emails**), Parseur, Zapier Email Parser (1 mail = 1 billed task), Mailosaur (a QA tool), Nylas (connects existing mailboxes by OAuth, does not provide a dedicated address on our domain), Zoho Mail (read-only API, no push), Tuta (no mail API at all — proprietary end-to-end encryption makes server integration structurally impossible), *anymail finder* (a B2B prospecting tool, not to be confused with the `django-anymail` library which does normalise the inbound webhooks of several ESPs behind a single API — relevant if we want to retain portability between providers).

### 1.5 The self-hosted option: reasonable here, and even preferable

**Yes, and for good reasons.** The context is favourable: we only **receive**, on a VPS we already have.

#### State of the projects (GitHub API, queried on 3 August 2026)

| Project | ★ | Licence | Last push | Native HTTP hook |
|---|---|---|---|---|
| **Stalwart** | 13,996 | **AGPL-3.0-only OR SELv2** (dual) | 2026-08-03 (release v0.16.16 on 02/08) | ✅ **MTA Hooks** |
| **Postal** | 16,715 | **MIT** | 2026-08-03 | ✅ **native** |
| Haraka | 5,613 | MIT | 2026-08-03 | ⚠️ to be written yourself |
| Maddy | 6,052 | GPL-3.0 | 2026-07-24 | ⚠️ none found |
| `remi-san/haraka-http-queue` | **2** | Apache-2.0 | **2014-08-14** | ❌ dead for 12 years |

#### Stalwart — MTA Hooks, "like milter but over HTTP"

This is exactly what we need. The CHANGELOG notes "*Pipes have been deprecated in favor of MTA hooks*".

Structures verified **in the source code** (<https://raw.githubusercontent.com/stalwartlabs/stalwart/main/crates/smtp/src/inbound/hooks/mod.rs>):

- **Stages**: `connect`, `ehlo`, `auth`, `mail`, `rcpt`, **`data`**.
- **JSON request**: `{context: {stage, client{ip,port,ptr,helo}, tls, server, queue{id}}, envelope: {from, to[]}, message: {headers[], serverHeaders[], contents, size}}` — **at the `data` stage, `message.contents` holds the complete message**.
- **Expected response**: `{action: "accept"|"discard"|"reject"|"quarantine", response: {...}, modifications: [...]}`.
- **HTTP client** (`client.rs`): `url`, `timeout`, **arbitrary `headers`** — hence our own `Authorization: Bearer …`, `max_response_size`.

⚠️ **Stalwart's web documentation is a non-extractable SPA** (`stalw.art/docs/` returns only a link to installation; a dozen plausible URLs for MTA Hooks return 404). **The elements above come from the source code and the CHANGELOG, not from a readable documentation page.** To be re-checked in a browser before implementation.

⚠️ **AGPL-3.0 licence**: no effect if we host for ourselves; if Chaudron becomes a service accessible to third parties, AGPL §13 applies. A trade-off to be made consciously.

#### Postal — the MIT alternative

<https://docs.postalserver.io/developer/http-payloads> — "Receiving e-mail by HTTP", form-data or JSON as preferred.
`processed` format: `rcpt_to`, `mail_from`, `subject`, `message_id`, **`spam_status`**, `plain_body`, `html_body`, `attachments[]{filename, content_type, size, data}` (base64). `raw` format: entire message in base64. Automatically separates quotes and signatures. **5 s timeout, 18 attempts with exponential backoff**, immediate failure on 5xx, and a **bounce sent to the sender** on permanent failure. No webhook signature → secret URL + HTTPS + network filtering. Antispam can be integrated (SpamAssassin, rspamd, ClamAV).

#### Frank advantages

- **Total control over rejection.** This is problem no. 1 (§1.2): we decide to reject nothing on DMARC. No order confirmation mail disappears silently.
- **Trivial GDPR.** The data does not leave our VPS. No DPA, no TIA, no American sub-processor (see §1.6).
- **Marginal cost.** The VPS already exists.
- **No rule ceiling and no monthly quota.**
- **No outbound reputation problem** — see the nuance below.

#### Frank disadvantages

- **Inbound port 25 may be blocked at the hosting provider — to be checked BEFORE anything else.**
  Hetzner: "*we block ports **25 and 465 by default on all cloud servers***", unblocking possible after one month of seniority and payment of the first invoice, case by case (<https://docs.hetzner.com/cloud/servers/faq>).
  DigitalOcean: "*SMTP ports **25, 465, and 587** are blocked on Droplets*" (<https://docs.digitalocean.com/support/why-is-smtp-blocked/>).
  ⚠️ **Neither one specifies the DIRECTION of the block.** Common practice suggests it is outbound, hence that receiving works, but **this is asserted by no official source.** OVH and Scaleway: policy pages unreachable today, **not verifiable**.
  → **A 5-minute action before any decision**: `nc -l 25` on the target VPS and a connection test from outside.
- **"Receive-only = no reputation to manage" is true, with two nuances.**
  (a) **Bounces turn us into a sender.** Rejection must happen **at the SMTP phase** (`RCPT TO` / `DATA`) rather than after acceptance: an in-session rejection generates no outbound mail, whereas a rejection after acceptance forces us to emit an NDR — with a risk of *backscatter* if the sender was forged. Cloudflare in fact chose the radical route: "*Non-delivery reports not forwarded to original senders*".
  (b) We still inherit the maintenance: TLS, updates, antispam, anti-abuse.
- **Surface to operate**: MIME parsing, size limits, spam filtering (rspamd/SpamAssassin), backup, monitoring. Not enormous for receive-only, but not zero.
- ⚠️ **Not verified for lack of research budget**: Gmail/Outlook's actual TLS requirements for *delivering* to our MX (mandatory or opportunistic STARTTLS?), the usefulness of MTA-STS / DANE on reception, the need for a PTR record on receive-only (it is required for sending), the RAM/CPU cost of rspamd on a small VPS, and the volume of spam to expect on a never-published random-token address.

### 1.6 GDPR — the state of the law has moved, and it counts in the choice

- The **Data Privacy Framework remains formally valid** (EU adequacy decision 2023/1795), with more than 5,300 self-certified American organisations.
- The **Latombe** action was dismissed by the EU General Court on procedural grounds; **an appeal has been pending before the CJEU** since October 2025, with no hearing date announced.
- ⚠️ **On 29 June 2026, the United States Supreme Court handed down *Trump v. Slaughter* (no. 25-332, 6-3)**: the restrictions preventing the president from removing FTC commissioners are unconstitutional. **The independence of the FTC — one of the pillars of the adequacy finding — is no longer guaranteed**, and the PCLOB faces the same objection. noyb is calling for an immediate exit from the DPF. Law firms' recommendation is to update *Transfer Impact Assessments* and to "*evaluate whether EU-based or otherwise lower-risk alternatives are economically and technically viable*".
  Source: <https://www.activemind.legal/guides/dpf-supreme-court/> (published on 2 July 2026)

**Translation for Chaudron**: named grocery-shopping data, per household, is personal data revealing lifestyle habits (diet, allergies, inferable religious convictions). Building on a US provider in 2026 exposes us to an emergency migration if the appeal succeeds. This is not a theoretical risk this year.

Also worth noting: processing the mail in a mailbox means processing the data of **third parties who have never consented**. The user must be advised to set up a **selective forwarding filter** (sender = retailer), to persist only the extracted lines, and to purge the raw emails.

### 1.7 Recommendation for the email strand

**Self-hosting with Stalwart + MTA Hook**, with **CloudMailin as the managed fallback**.

Stalwart settles all three problems at once: DMARC rejection (we decide to reject nothing), GDPR (nothing leaves the VPS), and cost (marginal). The hook at the `data` stage delivers the complete message as JSON to our FastAPI endpoint, with arbitrary authentication headers and an `accept`/`discard`/`reject` response. The project is massively active.

*Conditions to clear before committing*: (a) test inbound port 25 at the hosting provider; (b) read the MTA Hooks documentation in a browser; (c) arbitrate the AGPL. **If the AGPL is a problem, Postal (MIT) is a direct substitute**, with a very similar payload, 18 retries — at the price of having no webhook signature.

*If self-hosting is refused*: **CloudMailin** is the only managed option that allows **forcing processing in the EU region by DNS**, with an Article 28 DPA. Caveats: basic auth only, 512 KB on the free tier (tight for a retailer's HTML mail — the useful step up is Professional at $45/month), and the OpenAI sub-processor to clarify. **ImprovMX Premium ($9/month, FR datacentres at OVH)** is simpler and cheaper, at the price of zero webhook security (a single IP to allowlist) and only 2 retries.

**Design rules valid whatever the choice**:
- The webhook URL contains a long random secret, in addition to the provider's auth mechanism.
- **The endpoint is idempotent**, keyed on `Message-ID`: aggressive retries (Postmark 10, Postal 18, Mailtrap 10/24 h) guarantee duplicates.
- The per-household address is an **unguessable random token**, revocable and rotatable — it is a capability URL, and must be treated as a secret.
- Provide an explicit fallback path for when parsing fails (Brevo is right: 100% is impossible).

---

## 2. Reading the mailbox directly — alternative route

### 2.1 Gmail API: the cost is regulatory, not technical

**`gmail.readonly` is indeed a *restricted scope* in 2026.** The official list of restricted scopes confirms it, and it also includes `gmail.metadata`, `gmail.modify` and `https://mail.google.com/` (the latter covering *all* use of IMAP, SMTP and POP3).
Sources: <https://developers.google.com/workspace/gmail/api/auth/scopes> · <https://support.google.com/cloud/answer/13464325?hl=en>

**There is no less sensitive fallback.** `gmail.metadata` is *also* restricted **and** does not give the message body — hence useless here. The *sensitive* (non-restricted) scopes are those of Workspace Add-ons, which grant only **temporary access to the currently open message**, with no background processing: the user would have to open each mail and click, which destroys the point of the automation.
Source: <https://developers.google.com/workspace/add-ons/concepts/workspace-scopes>
⚠️ **Documentation inconsistency noted**: the Gmail scopes page classifies `gmail.addons.current.message.readonly` as *sensitive*, the Workspace add-ons page calls it *restricted*. Unresolved.

#### Often-fatal prerequisite: the permitted application type

Google requires the app to belong to an approved type for Gmail scopes. No. 4 is "*Applications that use information from emails to provide reporting or monitoring services for the benefit of users that **improve the email experience** (such as applications that automate travel itineraries or track flights or package delivery statuses)*".
Source: <https://developers.google.com/workspace/workspace-api-user-data-developer-policy>

Chaudron resembles this pattern (extracting a summary from a mail), but **a pantry app improves stock management, not the email experience**. **This is a real risk of rejection, at the discretion of the Trust & Safety team, and it cannot be quantified.**

#### OAuth verification

| Step | Official turnaround |
|---|---|
| Brand verification | 2–3 business days |
| Sensitive scope verification | 10 business days |
| **Restricted scope verification** | **6 weeks** |

Source: <https://support.google.com/cloud/answer/13463817?hl=en>

Documents required (<https://support.google.com/cloud/answer/13464321?hl=en>): a homepage on an owned domain actually describing the app; a privacy policy **on the same domain**, linked from the homepage *and* the consent screen; **domain ownership verified via Search Console**; a **demo video** of the complete OAuth flow, **in English**, with the client ID visible in the address bar; a justification per scope.

#### CASA — the point that kills it

**Still mandatory in 2026** for any restricted-scope app having "*the ability to access data from or through a third-party server*".
Source: <https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification>

Governance: the programme is run by the App Defense Alliance, **migrated under the Joint Development Foundation (Linux Foundation)**, with Google, Meta and Microsoft on the steering committee (<https://www.linuxfoundation.org/press/app-defense-alliance-migrates-under-jdf-with-google-meta-microsoft-as-steering-committee>). The nomenclature has moved from "Tier 2 / Tier 3" to **Assurance Levels AL1 / AL2**, **imposed by Google** and not chosen by the developer (<https://support.google.com/cloud/answer/13465431?hl=en> · <https://appdefensealliance.dev/casa/casa-tiering>).

🔴 **The free self-scan is dead.** "*The CASA self scanning process is **deprecated***" (<https://appdefensealliance.dev/casa/tier-2/tier2-overview>); it survives only as a self-assessment ahead of the paid scan. **AL1 and AL2 alike are "Lab Tested – Lab Verified"**: an accredited lab is mandatory. Onboarding of new labs is moreover **suspended** following the migration — so no downward competitive pressure.

**Public prices observed on 3 August 2026**:

| Lab | Offer | Price | Turnaround |
|---|---|---|---|
| TAC Security | **AL1 Basic** | **$675** (struck through from $1,800) | 2–3 weeks |
| TAC Security | AL1 Premium | $855 | 2–3 weeks |
| TAC Security | AL2 Enterprise | $4,500/year | 2–4 weeks |
| Leviathan | AL1 "No Rush" | $3,000 | starts within 30 days |
| Leviathan | AL1 "Priority" | $6,000 | starts within 2 days |

Sources: <https://casa.tacsecurity.com/site/home> · <https://www.leviathansecurity.com/programs/google-casa-cloud-application-security-assessment> · list of labs: <https://appdefensealliance.dev/casa/casa-assessors>

⚠️ **Two figures not to reuse**: the "$15,000 – $75,000" still doing the rounds comes from a [GMass post from 2019/2020](https://www.gmass.co/blog/google-oauth-verification-security-assessment/) **predating the split into tiers** — obsolete. And the grids on third-party blogs (switchlabs, deepstrike) are unofficial compilations.

**Renewal: annual, non-negotiable**, and "*the annual CASA assessment must be a full test of your app, regardless of any changes made*" — no "light renewal" price.
Source: <https://support.google.com/cloud/answer/13463816?hl=en>

**First-hand account, July 2026**: a developer notified by Google on 16 July 2026 for a personal app with the `drive` scope writes "*the cost is ~$540/year even at the cheapest, TAC Security. And it renews every 12 months*", "*The old free self-scan is gone; you must go through an accredited lab*". **He abandoned his app** and fell back on `drive.file`, which is not restricted. This is the reference scenario.
Source: <https://yurudeep.com/posts/aicoding/2026/20260717/en/>

⚠️ **The "no server" escape hatch is not confirmed.** The historical announcement indicated that apps storing data solely on the device escaped the full assessment. A developer asked exactly this question on the official forum on 16 March 2026 (<https://discuss.google.dev/t/is-casa-required-for-all-access-restricted-scopes/340650>): **it went unanswered**. No official 2026 page confirms the exemption. To be treated as a gamble.

#### The "Testing" vs "unverified" nuance — it changes everything

Two regimes that most sources conflate:

**Publishing status = "Testing"** (<https://support.google.com/cloud/answer/15549945?hl=en>): 100 test users max, and above all — "*A Google Cloud Platform project with an OAuth consent screen configured for an external user type and a publishing status of 'Testing' is issued **a refresh token expiring in 7 days***" (<https://developers.google.com/identity/protocols/oauth2>). Weekly reconnection: a deal-breaker.

**Publishing status = "In production" but unverified**: a warning screen before consent, a **ceiling of 100 new users cumulative over the whole life of the project, non-resettable** — but **refresh tokens do not expire at 7 days**. The 7-day rule attaches to the "Testing" status, not to the absence of verification.

→ **This is the only viable path without paying.** Note that <https://support.google.com/cloud/answer/7454865?hl=en> states that one *must* pass verification before launching an app intended for users: it is tolerated technically, not blessed contractually.

**Other causes of refresh token expiry** (same official page), two of which bite here:
- "*The user changed passwords and the refresh token contains Gmail scopes*" → **any Google password change breaks the integration**.
- A limit of **100 refresh tokens per Google account per client ID**; beyond that, the oldest is invalidated silently.
- Non-use for 6 months.

**"Internal" mode**: exempt from verification *and* from the ceiling, but reserved for members of a Workspace/Cloud Identity organisation. An external user gets `org_internal`. **Not applicable to a public app.**

#### Gmail API quotas — a non-issue

6,000 units/minute/user/project, 80,000,000 units/day/project before billing. `messages.list` = 5, `messages.get` = 20. A sync reading 20 messages costs ~405 units: we could do ~200,000 syncs/day within the free quota. **Quotas will never be the constraint — verification will.**
Source: <https://developers.google.com/workspace/gmail/api/reference/quota>

### 2.2 IMAP at the other providers

| Provider | Server | Auth 2026 | Source |
|---|---|---|---|
| **Gmail** | `imap.gmail.com:993` | **App password (2FA mandatory)** or OAuth — but IMAP OAuth requires `https://mail.google.com/`, **restricted** | <https://support.google.com/mail/answer/185833?hl=en> · <https://developers.google.com/workspace/gmail/imap/xoauth2-protocol> |
| **Outlook.com personal** | `outlook.office365.com:993` | **OAuth 2.0 exclusively** (basic auth withdrawn), delegated scope `https://outlook.office.com/IMAP.AccessAsUser.All` | <https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth> |
| **Microsoft 365** | ditto | "*Basic authentication is now disabled in all tenants*" (page updated 16/07/2026) | <https://learn.microsoft.com/en-us/exchange/clients-and-mobile-in-exchange-online/deprecation-of-basic-authentication-exchange-online> |
| **iCloud Mail** | `imap.mail.me.com:993` | **App-specific password mandatory**, 2FA required, **max 25 active** | <https://support.apple.com/en-us/102654> · <https://support.apple.com/en-us/102525> |
| **Yahoo** | `imap.mail.yahoo.com:993` | App password if 2FA/Account Key | <https://help.yahoo.com/kb/SLN15241.html> |
| **Free.fr** | `imap.free.fr:993` | **Main account password** — no app password | <https://assistance.free.fr/articles/609> |
| **Orange** | `imap.orange.fr:993` | Dedicated "mail software" password. ⚠️ **POP/IMAP disabled by default** on new mailboxes — manual activation required first | [assistance.orange.fr](https://assistance.orange.fr/ordinateurs-peripheriques/installer-et-utiliser/l-utilisation-du-mail-et-du-cloud/mail-orange/le-mail-orange-nouvelle-version/parametrer-la-boite-mail/mail-orange-comment-acceder-a-sa-boite-mail-orange-depuis-une-application-ou-un-logiciel-de-messagerie-non-fourni-par-orange_434630-964290) |
| **La Poste** | `imap.laposte.net:993` | Main password; TLS 1.2 minimum since July 2023 | [aide.laposte.net](https://aide.laposte.net/contents/comment-parametrer-laposte-net-sur-mon-logiciel-de-messagerie-suite-a-l-arret-des-protocoles-en-clair-non-cryptes) |

**The counter-intuitive point**: on Gmail, **IMAP+OAuth is heavier than the Gmail API**, not lighter — Google itself writes "*If your app doesn't require `https://mail.google.com/`, migrate to the Gmail API*". Only the app password sidesteps everything.

**The favourable point**: **Microsoft is the only large provider where a solo developer can do things properly and for free.** Delegated OAuth on `IMAP.AccessAsUser.All` works for Microsoft 365 **and** for personal Outlook.com accounts; there is **no CASA equivalent, no paid audit**; and *publisher verification* is **free** ("*Microsoft doesn't charge developers for publisher verification*", <https://learn.microsoft.com/en-us/entra/identity-platform/publisher-verification-overview>) and non-blocking for personal accounts — it does however require a Microsoft AI Cloud Partner Program account and an app registered with a work/school account, not a personal Microsoft account.

⚠️ **Not verified**: the exact end date of basic auth on personal Outlook.com (16 September 2024 comes up consistently but the canonical page stays vague); the LSA cutover date for personal @gmail.com accounts; the limit of 25 iCloud passwords (Apple pages truncated on fetch, corroborated by the developer forum).

### 2.3 The risk of storing application passwords

An app password is a **reusable, long-lived secret, not revocable granularly by our app**, and it often grants **write and delete access** to the whole mailbox — not just read. A leak of our database = total compromise of all our users' mailboxes, with password resets possible on all their other services. A far worse risk profile than an OAuth refresh token (scoped, revocable on the provider's side).

GDPR obligation (Article 32): hashing is inapplicable since the secret has to be replayed. The CNIL accepts **reversible encryption** in this case, but **requires additional measures** — key outside the database (KMS/HSM or an environment secret never committed), rotation, access logging.
Source: <https://www.cnil.fr/fr/mots-de-passe-une-nouvelle-recommandation-pour-maitriser-sa-securite>

### 2.4 Clear-cut conclusion

**No, reading the mailbox directly is not worth it for Chaudron.**

The arithmetic is brutal: 6 weeks of verification, a homepage and a domain verified in Search Console, a demo video in English, the need to convince Google that a pantry app "improves the email experience", and **$675 minimum every year in perpetuity** for a full audit at every renewal — for a project with no revenue. The free self-scan that made this bearable no longer exists.

**If automation by mailbox reading is absolutely wanted anyway**, the order is: (1) publish "In production" without verification and accept the lifetime ceiling of 100 users — refresh tokens do not expire under this regime, and the cost is nil; (2) start with **Microsoft**, not Google, if several providers have to be covered; (3) IMAP + app passwords only as a last resort, and only if one is prepared to treat one's database as a vault.

**The inbound email route (§1) removes this problem entirely**: no verification from any provider, no user secret stored, no mailbox access. That is the decisive argument in its favour.

---

## 3. Parsing till receipts with a multimodal model

### 3.1 Cost — not the deciding criterion, but the household is the one paying

⚠️ **Framing imposed by ADR-0005 and 0007**: Chaudron does not choose a model, it exposes five adapters and **each household configures its own** (BYOK, local Ollama, or the instance owner's key). The figures below therefore serve **not** to arbitrate an operating expense — there is none — but two purposes: (a) to recommend an **honest default** in the selection interface, since ADR-0007 notes that "four providers to choose from is also a decision burden"; (b) to give the user an order of magnitude of what their own scan will cost them. The reference points are given on Claude because `claude-opus-5` is the documented default of ADR-0005; they transpose to the other adapters.

**Image token counting at Claude**: the image is cut into **28×28 px patches**, i.e. `⌈width/28⌉ × ⌈height/28⌉` visual tokens, with a double ceiling (long edge **2576 px** and **4784 tokens** on high-resolution models; **1568 px / 1568 tokens** on the standard tier such as Haiku 4.5). Beyond that, automatic resizing.
Source: <https://platform.claude.com/docs/en/build-with-claude/vision>

For a 1500×2000 px image: `54 × 72 = 3888 visual tokens`, with no resizing (value confirmed in the official table).
**Good news about the shape of a receipt**: a very elongated format costs *less*, because the long-edge ceiling bites before the token ceiling. 1000×3000 → resized to 858×2576 → **2852 tokens**, against 3888 for 1500×2000.

**Cost per receipt** (assumptions: 1500×2000 image, 600-token prompt, 800-token JSON output, one pass, no cache):

> **Why the table says 1564 and the paragraph above says 1568 — both are right.** 1568 is the *ceiling* on the standard tier; 1564 is the *realised* patch count for the receipt assumed here, and it is the number the costs below are computed from. The derivation is the formula given above, applied to the resized image: a 1500×2000 source fits under the 1568 px long-edge ceiling at **1269×952** (the figure §3.7 states for Haiku 4.5), and `⌈1269/28⌉ × ⌈952/28⌉ = 46 × 34 = 1564`. It lands just under the cap, as it must. Checked against the published Haiku 4.5 rates ($1 / $5 per MTok): `(1564 + 600) × $1/MTok + 800 × $5/MTok = $0.006164`, i.e. **$6.16 per 1,000** — the figure in the table. Recomputing with 1568 gives $6.17, which is not. So do not "fix" 1564 to 1568; if the image assumption changes, both the patch count and the costs have to be recomputed together.

| Option | Cost / receipt | 1,000 receipts |
|---|---|---|
| Claude Haiku 4.5 (standard tier, 1564 image tokens) | **0.6 ¢** | $6.16 |
| Claude Haiku 4.5 on the Batch API (−50%) | 0.3 ¢ | $3.08 |
| **Google Document AI / AWS Textract AnalyzeExpense / Azure prebuilt-receipt** | **1.0 ¢** | $10 |
| Claude Sonnet 5 (intro pricing until 31/08/2026) | 1.7 ¢ | $16.98 |
| Claude Sonnet 5 (standard pricing) | 2.5 ¢ | $25.46 |
| Claude Opus 5 | 4.2 ¢ | $42.44 |
| Taggun | 4–6 ¢ | |
| Mindee | ≈ 5 ¢ (ambiguous pricing) | |
| Veryfi / Asprise | 8 ¢ (+ $500/month minimum at Veryfi) | |
| Self-hosted (RTX 4090, > 50k/month) | ≈ 0.02 ¢ | + human operating cost |

Sources: <https://aws.amazon.com/textract/pricing/> · <https://cloud.google.com/document-ai/pricing> · <https://azure.microsoft.com/en-us/pricing/details/document-intelligence/> · <https://www.veryfi.com/pricing/> · <https://www.taggun.io/pricing> · <https://www.mindee.com/pricing>

⚠️ **Correction of an error in circulation**: several blogs translate Google's "$0.10 for every 10 pages" into "$1 / 1000 pages". That is wrong: $0.10 ÷ 10 = $0.01/page = **$10/1000**. The correct figure converges with AWS and Azure to the cent, which is a good consistency check.
⚠️ **Not verified**: the Google Document AI pricing page never loaded in full (figure taken from an extract pointing at the official page); the Azure page displays `$-` placeholders (figure taken from a consensus of secondary sources); Mindee contradicts itself between "6,000 credits per month" and "per year" — a ×12 discrepancy on the price per page, the only reliable anchor being overage from $0.05/credit.

**Three lessons**:
1. **The image accounts for ~87% of input tokens.** The system prompt is noise in the budget.
2. **Prompt caching is close to useless here** — the image changes with every receipt, and the stable prefix (600 tokens) is below Sonnet 5's cacheable minimum (1024 tokens). Do not architect around caching.
3. **The gap between the viable options (0.6 ¢ to 2.5 ¢) is negligible next to the cost of an undetected error in a food stock.** Cost is not the criterion. What follows is.

### 3.2 The real differentiator: retailer labels

The `receipt` parsers from Google, AWS and Azure are trained on predominantly English-language receipts and **return the label as-is**. They do not know that "PDT NOUV 1KG" is a new potato. A language model does — that is exactly what world knowledge brings. **But it is also what produces hallucination (§3.4).**

⚠️ **Research finding, to be known before planning**:
- **No public source documents the product-label abbreviations of French retailers** (Leclerc, Intermarché, Carrefour, Super U, Lidl, Aldi, Auchan). The max length of ~20–24 characters follows from the thermal printing format (58 mm ≈ 32 columns, 80 mm ≈ 42–48 columns) but **no source documents these retailers' truncation policy**.
- **No public dataset of French receipts exists.** The [French OCR datasets collection on HF](https://huggingface.co/collections/lbourdois/french-ocr-datasets-67c8d3152330f11227e0d108) contains 3 datasets, all of generic transcription. No French open source receipt-scanning project found on GitHub.

**This lexicon is therefore both our main start-up cost and our main defensible asset.** It exists nowhere and is built empirically.

Usable international datasets: **CORD** (1,000 Indonesian receipts, **30 hierarchical entities** under `menu`/`subtotal`/`total` — the structure closest to our need), **SROIE** (1,000 English receipts, but **only 4 fields, no line items** — useless here), **CORU/ReceiptSense** (20,000 Arabic-English receipts). Licences to be checked individually before commercial use.
Sources: <https://rrc.cvc.uab.es/?ch=13> · <https://openreview.net/pdf?id=SJl3z659UH> · <https://arxiv.org/pdf/2406.04493>

### 3.3 Physical pitfalls

**Thermal paper — the degradation is chemically reversible.** The mechanism is a leuco-dye + encapsulated developer pair: it is not a pigment fixed in the fibre, it is a physical mixture that nothing locks in place.

| Grade | Legibility |
|---|---|
| **Economy (the most common at the till)** | **7 to 30 days** |
| Oil/water resistant | 1–2 years |
| Archival | 3–7 years |

**The most destructive accelerator is contact with oils, plasticisers and solvents** — hence a **PVC wallet or a plastic sleeve**, exactly what people who "keep their receipts to scan later" do.
Sources: <https://www.ygtape.com/article/why-your-receipts-disappear> · <https://www.jotamachinery.com/academy/thermal-paper-fading/> · patent <https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/9656498>
⚠️ Sources = manufacturer blogs (who sell the higher grade), mechanism confirmed by the patent literature. **No academic study quantifies the drop in OCR rate as a function of receipt age.**

→ **Direct product consequence: the app must push for immediate scanning**, not offer a comfortable "weekend catch-up mode".

**Angle, crumpling, blur — angle is by far the worst factor.**

| Perturbation | Effect |
|---|---|
| **Shooting angle** | precision **0.514 at 0° → 0.331 at 15° → 0.170 at 30°** — divided by 3 |
| 5° tilt | −3 to −8% |
| Gaussian blur 0 → 1.5 | WER 0.24 → 0.34 |

⚠️ Figures taken from snippets, source PDF not extractable — **not verified in their methodological context**.

→ **Recommendation: impose a visual framing guide on the PWA side and refuse capture beyond ~10° of detected angle.** The gain is probably greater than that of a *dewarping* pipeline. If dewarping is wanted anyway, the most deployable state of the art is <https://arxiv.org/abs/2501.03145> (YOLOv8 + cubic polynomial interpolation, CER 0.0235, better than RectiNet/DocGeoNet/DocTr++ for markedly less compute).
⚠️ Dewarping benchmarks are tiny (DocUNet = 30 documents); any SOTA claim on them is statistically fragile. And **nobody has published a "VLM with vs without dewarping on crumpled receipts" comparison** — ReceiptBench explicitly acknowledges having run no systematic evaluation of visual augmentation.

**Long receipts in several photos — uncharted territory.** Mind the vocabulary trap: *multi-receipt detection* (N distinct receipts in one photo — what Mindee and Veryfi do) is **not** *long receipt capture* (1 receipt in N pieces — our problem). Almost all vendor documentation talks about the former.
- **Mindee**: the Multi-Receipt Detector isolates several receipts from one photo, and for PDFs "each page is processed as a separate image" → **it does not stitch** (<https://www.mindee.com/blog/multi-receipt-detector-api>).
- **Veryfi** is the only one claiming the feature ("*automatically stitches together multiple pictures of a receipt in real time*"), but the "Detect, Crop & Stitch" page is **"available per request"** — no API parameter, no page limit, no published technical specification.
- ⚠️ **No academic publication and no engineering article on *receipt image stitching*.** Generic stitching (SIFT/ORB + homography) works badly on repetitive low-texture monospace text — precisely the nature of a receipt. No source either on overlapping tiling applied to VLMs on receipts.

→ **Pragmatic lead, to be validated empirically**: rather than pixel stitching, send the N photos **in a single request** (up to 100 images; beyond 20 images, resize each to ≤ 2000 px on a side, on pain of `invalid_request_error`), labelled "Image 1:", "Image 2:" as the vision documentation recommends, with an explicit instruction to deduplicate the overlapping area. We delegate the joining to the model rather than to OpenCV.

**FR domain specifics — ⚠️ not covered by this research**: printing of variable-weight products (price/kg + a quantity such as 0.432 kg), discounts and promotions as negative lines, "2+1 free" bundles, "loyalty card −X €", multi-rate VAT summary (5.5 / 10 / 20%), packaging deposits, bag charges. Leads to explore: article 289 of the CGI, the order of 3 October 1983 (note given to the consumer), the service-public fact sheet "Facture et note : mentions obligatoires".

**One element is however already settled, and must be reused**: ADR-0008 established that **variable-weight** items carry **in-store internal codes prefixed `02` and `20`–`29`**, which **embed the price** — they therefore change with every purchase and will never appear in a public reference database. On the scanning side, the decision is to detect them client-side and switch to manual entry with no network call. **On the receipt side, the consequence is different and important: a weight-priced line must never be routed to an OFF lookup by code, only to label matching, and preferably to the Ciqual pivot** (§3.6) since these are typically fruit, vegetables, butchery and loose goods — precisely the categories with no barcode.

### 3.4 The central trap: the model doctors the arithmetic

**ReceiptBench (2026)** — 10,656 real receipts, 19 fields, 4 tasks. The per-field scores are eloquent:

| Model | Overall | Perception | Normalisation | Reasoning | **Structure (line items)** |
|---|---|---|---|---|---|
| **Qwen3-VL-8B (SFT+GRPO)** | 0.7950 | 0.8488 | 0.9416 | 0.8547 | **0.6373** |
| Gemini-3-Pro | 0.7373 | 0.7360 | 0.9086 | 0.8714 | **0.5781** |
| GPT-5 | 0.7076 | 0.7304 | 0.8743 | 0.8706 | **0.4893** |

Source: <https://arxiv.org/html/2605.22413v1>

**The "line items" field — exactly what we want to extract — is by far the worst.** The frontier models plateau between 0.49 and 0.58 F1, while a fine-tuned 8B beats GPT-5 by +30% relative.
⚠️ **Language composition: 98.0% English, only 60 French samples out of 10,656.** The authors acknowledge the anglocentric bias. **No public benchmark seriously measures performance on French receipts.**

**The most dangerous behaviour**, named by the authors "*hallucination for arithmetic consistency*":

> **Models fabricate or modify line items to force the sum to match the printed total.**

This bears directly on our question "the total does not add up". **The model does not flag the inconsistency — it doctors the line items to make it disappear.** This is the worst possible behaviour: it turns a detectable error into a silent one, **and it partially neutralises the arithmetic check we were counting on as a safeguard.**

**Why this is architectural and not fixable by prompting** — PP-OCRv6 benchmark, rate of outputs free of hallucinated content:

| System | Anti-hallucination precision |
|---|---|
| **PP-OCRv6 medium** (specialised OCR, 34.5 M params) | **93.20%** |
| Qwen3-VL-235B | 80.56% |
| **GPT-5.5** | **78.00%** |

Source: <https://arxiv.org/html/2606.13108> (Table 7)

The authors' mechanistic explanation: VLMs "*tend to correct what they perceive as spelling or grammar mistakes in the source image, producing linguistically plausible text that is factually inconsistent with the visual input*", whereas specialised OCR "*faithfully reproduces the exact content — including deliberate errors — without injecting linguistic priors*".

**Translation for Chaudron: a VLM faced with "CRQ MONSIEUR X4" is under statistical pressure to write "CROQUE MONSIEUR X4". Faced with a partly erased digit, it is under the same pressure to produce the plausible digit. It is the same property that makes us win on abbreviation expansion and lose on amounts. You cannot have one without the other.** Hence the hybrid architecture in §3.7.

**Honest counterpoint**: on degraded documents, VLMs remain markedly better than classic engines — CER 3 to 4× lower on noisy scans and receipts; on scanned invoices, Gemini 2.5 Pro 94% and Claude 3.5 Sonnet 90% against AWS Textract 82% and Tesseract 80–85%.
⚠️ Figures aggregated from third-party sources by <https://parsli.co/blog/llm-ocr-vs-traditional-ocr>, not measured by them, on models one generation behind.

### 3.5 Validating the output

**Guaranteed structured output — GA at Anthropic in 2026.** `output_config.format` with `type: "json_schema"` proceeds by **sampling constrained by a compiled grammar**: the output is *guaranteed* valid against the schema, not validated afterwards. `strict: true` does the equivalent on tool definitions.
Source: <https://platform.claude.com/docs/en/build-with-claude/structured-outputs>
(The parameter is indeed called `output_config.format`; the old `output_format` and the beta header `structured-outputs-2025-11-13` are deprecated.)

⚠️ **The trap that concerns us directly**: `minimum`, `maximum`, `multipleOf`, `minLength`, `maxLength` **are not supported**. **We therefore cannot constrain a price to be positive via a numeric bound.** `additionalProperties: false` is mandatory on every object. On the other hand `enum` is supported on numbers → **`{"type": "number", "enum": [5.5, 10, 20]}` is a valid constraint for VAT rates**. For everything else, the SDKs transform the schema automatically (constraint removed, injected as text into `description`, client-side validation): **this is exactly the Pydantic pattern, to be owned on the FastAPI side.**

Other operational points: grammar compilation on the first call then a **24 h cache**; incompatible with citations and prefill; **check `stop_reason` BEFORE parsing** (`"refusal"` or `"max_tokens"` → the output may not respect the schema).

**Arithmetic checks** — useful, but **not sufficient** in light of §3.4:
1. `Σ(line_price) == subtotal`
2. `subtotal + VAT − discounts == printed_total`
3. `Σ(net base per rate × rate) == total VAT`
4. Weight-priced lines: `quantity × unit_price == line_price` (±1 cent)
5. Rate consistency: food product → 5.5% expected

| Discrepancy | Action |
|---|---|
| 0 | Relative confidence — **but no blind automatic validation** (cf. doctoring) |
| ±1 to 3 cents | VAT rounding, tolerate |
| Discrepancy = exact price of one line | Duplicated or missing line → targeted re-read |
| Any | Human review screen, line by line |

**Principle**: **validate against the image, not against the internal consistency of the output.** The arithmetic check remains a detector of outright failure, not a certificate of correctness.

**Confidence score — the Claude API does not expose logprobs.** Verified against the complete Messages API reference: no `logprobs`, no `top_logprobs`, no per-token score, neither on input nor on output.
Source: <https://platform.claude.com/docs/en/api/messages> (a long-standing and unmet community request, cf. <https://github.com/anerli/anthropic-logprobs>)
→ **Architectural consequence, reinforced by ADR-0005**: not only does Anthropic not expose logprobs, but **the `ModelProvider` port cannot in any case depend on a feature specific to one adapter**. The confidence signal must therefore live **above the port**, in the domain, and work identically whether the household is on Claude, Mistral or a small Ollama model. That is a decisive argument in favour of the cross-corroboration below: it is the only technique that asks nothing of the provider.

The same remark applies to structured output: `output_config.format` is specific to Anthropic. ADR-0005 provides for **degradation by detected capabilities** — the domain must therefore treat "schema guaranteed by grammar" as a *bonus* and not as an invariant, and server-side Pydantic validation remains mandatory in all cases.

Alternatives, by cost/signal ratio:

| Technique | Cost | Reliability |
|---|---|---|
| **Cross-corroboration VLM × classic OCR** | +ε (CPU) | **The best fit for our problem** — see below |
| Self-consistency N=2 | ×2 | Very good ratio: *Two Samples Are Enough* shows that 2 samples suffice for a robust estimate (<https://openreview.net/forum?id=66D3rZrNjV>) |
| Verbalised confidence alone | ×1 | ⚠️ **Poorly calibrated without training** (<https://arxiv.org/pdf/2603.17839>). **Do not use alone.** |

**Cross-corroboration is directly justified by the mechanism of §3.4**: classic OCR (PP-OCRv6, PaddleOCR, docTR) is faithful to the pixel and **does not rewrite**, but does not structure; the VLM structures and expands abbreviations, but rewrites. **Running both in parallel and flagging numerical divergences turns two complementary weaknesses into an error detector.** Cost is near zero (CPU).
⚠️ **No paper publishes this architecture applied to receipts** — it is reasoning derived from the sources, not a sourced recommendation.

**Human review screen.** Mindee is the most explicit and confronts the problem head-on ("*Thermal printer ink degrades quickly*"): a **per-field confidence score** on a Low/High/Certain scale, **conditional routing** (automatic write when certain, human operator for damaged documents), and **memorisation of corrections** applied instantly to similar documents.
Source: <https://www.mindee.com/blog/receipt-data-extraction-ai-guide>
(Veryfi advertises 97% precision: ⚠️ a commercial, unaudited figure, with no public definition of the metric.)

**What our review screen must show**:
1. The photo of the receipt **next to** the JSON, with highlighting of the source area of each line when the model can produce bounding boxes (absolute, approximate coordinates — <https://platform.claude.com/docs/en/build-with-claude/vision-coordinates>)
2. The result of the arithmetic check, in green/red, **with the discrepancy quantified**
3. **The lines where VLM and OCR diverge, first**
4. The labels not matched to a product record, grouped
5. **Zero pre-validation by default at the start**: the first receipt from a retailer is validated in full; the following ones benefit from the alias table.

### 3.6 Matching label → product record

> **This subsection plugs into what exists, it does not replay it.** The Open Food Facts strategy is already settled by [ADR-0008](adr/0008-open-food-facts-integration.md) and worked up by [`technical-notes-scanning.md`](technical-notes-scanning.md): cache first (a condition of operation, not an optimisation), shared catalogue materialised by `household_id IS NULL`, API **v3** (v2 is deprecated, different error contract), honest `User-Agent`, staging environment `world.openfoodfacts.net` in development, and **import of the local dump as a prerequisite from phase 2**. What follows only adds what matching *receipt label → product record* requires on top of EAN scanning.

#### Open Food Facts — the online API is unusable, the dump is not

**Official rate limits, and they are hard**:

| Operation | Limit |
|---|---|
| Product read | **15 requests / min / IP** |
| **Search** | **10 requests / min / IP** |
| Write | none |

**A custom User-Agent is mandatory** (`AppName/Version (ContactEmail)`), and OFF "*reserves the right to deny access by banning an IP address*".
Sources: <https://openfoodfacts.github.io/openfoodfacts-server/api/> · <https://support.openfoodfacts.org/help/en-gb/12-api-data-reuse/94-are-there-conditions-to-use-the-api>

→ **10 searches/min ≈ 1 receipt of 10 lines per minute. The correct design is a local dump in the database, with the API serving only as a fallback for unknown barcodes.** This is not negotiable.

**And it brings forward the deadline set by ADR-0008.** That ADR defers importing the dump to phase 2, on the grounds that the ceiling (~15 req/min on product read) remains tenable in phase 1 for EAN scanning. **Label matching does not fall under the same ceiling**: it consumes the *search* endpoint, capped at **10 req/min**, and it issues one request **per receipt line** instead of one per scan. A single grocery receipt therefore saturates the minute. **Consequence: as soon as receipt ingestion goes into service, the local dump becomes a phase 1 prerequisite, not phase 2.** This is the main impact of this note on decisions already taken, and it warrants an amendment to ADR-0008.

Another subtlety: **full-text search does not exist in API v2** (`/api/v2/search` is a *structured* search on `categories_tags`/`brands_tags`/`code`). Search by name has migrated to **Search-a-licious** (Elasticsearch backend, `search.openfoodfacts.org`, with `q` accepting Lucene syntax — useful for filtering by category/brand while letting free tokens go to full text).

**Volumes as of 3 August 2026**: 4.72 M products across all bases; **1,255,083 for France** (~28% of the world catalogue, the best national coverage ratio). Filtering on `countries_tags: en:france` puts the table in `pg_trgm`'s comfort zone.
Source: <https://fr.openfoodfacts.org/>

> **Two counts of the same thing, and both are real.** [`technical-notes-scanning.md`](technical-notes-scanning.md) §3.3 gives **4,663,574** and **1,255,052** for the same day. That is not a contradiction between the two notes: the figures above were read off the `fr.openfoodfacts.org` landing page, the other pair was measured through `GET /api/v2/search`, and the two endpoints do not return the same number. *Which* population each one counts was never established, so neither figure supersedes the other. The France counts differ by 31 products, which is noise; the world counts differ by about 1.2%, which is not, and it is why the ratio above (~28%) is loose — both pairs actually give closer to 27%. The derived row counts inherit the split and round in opposite directions: **~1.25 M** in this document, **~1.26 M** in the other, off the same ~1.255 M measurement. Re-measure before sizing anything on either.

**Exports**: prefer **Parquet** (>150 columns, typed schema, loadable via DuckDB or pyarrow → binary `COPY`) over CSV (~0.9 GB compressed / ~9 GB uncompressed, and a minefield: quoting, line breaks inside names, columns that move). French ODbL mirror on data.gouv.fr, Parquet updated on 2 August 2026.
Sources: <https://world.openfoodfacts.org/data> · <https://www.data.gouv.fr/datasets/open-food-facts-produits-alimentaires-ingredients-nutrition-labels>

**ODbL licence — the concrete implications**:

| Object | Licence |
|---|---|
| **Structure** of the database | **ODbL 1.0** |
| Individual contents | DbCL 1.0 |
| Product photos | CC-BY-SA 3.0 |

Source: <https://world.openfoodfacts.org/terms-of-use>

The distinction that decides everything: **our enriched local copy is a *Derivative Database*** (storing it obliges nothing as long as we do not distribute it publicly; as soon as we publish it, it goes back out under ODbL, enrichments included), whereas **our PWA, our screens and our exports are a *Produced Work*** — **ODbL does not contaminate Chaudron's code**, only the **attribution notice** with a link to openfoodfacts.org is owed as soon as there is public distribution. Trademarks and image rights on packaging are **not** granted by OFF.

→ **Legal architecture advice: keep the learned alias table in a separate table referencing the barcodes, rather than as columns added to the OFF copy.** That keeps the ODbL boundary legible if the service ever opens up.

**Quality limits for name matching**: `product_name` is entered by contributors with no schema and mixes brand, denomination, flavour and weight ("Nutella" / "Nutella 400g" / "Pâte à tartiner Nutella"); **`product_name_fr` is not guaranteed to be filled in** on a French record (`COALESCE` mandatory); `quantity` is free text. A query for "lait demi-écrémé" returns thousands of near-identical records — **the problem is not recall, it is precision.**
⚠️ **The completion rate of `product_name_fr` / `brands` / `quantity` on the France subset is published nowhere.** It is the most important figure for this strand, it can be computed in ten minutes of SQL on the dump, and it determines whether the approach stands up. **To be measured before writing a line of code.**

**Two complementary sources**:
- **Open Prices** (<https://prices.openfoodfacts.org>) — 285,467 prices and 112,637 proofs as of 3/08/2026. Weak as a catalogue (~6% coverage), **but strong as a source of truth on retailer labels**: the proofs are **photos of receipts**, with a receipt ↔ barcode association. **It is the only public deposit identified that contains both a French till label and a GTIN.** Worth digging into seriously to bootstrap the lexicon.
- **Ciqual / ANSES** (<https://www.anses.fr/en/content/ciqual-nutritional-composition-table>) — 3,484 foods, **Licence Ouverte / Etalab** (free commercial reuse + attribution). Useless as a product catalogue, **valuable as a reference base of generic foods**: "PDT NOUV 1KG" can have **no OFF record at all** (no barcode) but has an obvious Ciqual entry.
→ **A two-catalogue architecture: OFF for packaged goods, Ciqual for fresh and loose.**

#### The register gap — the real hard point

**"PDT NOUV 1KG" and "Pommes de terre nouvelles de Noirmoutier, 1 kg" share almost no trigram and no lexeme after stemming.** No string-similarity engine will make that up, whatever the engine. **An abbreviation expansion layer is needed upstream** (PDT→pomme de terre, CRQ→croque, NOUV→nouvelle, LT DEM 1/2 ECR→lait demi-écrémé), **and only then** search. That is where the success rate is decided, not in the choice of HNSW vs IVFFlat.

The closest literature: **Gorman, Kirov, Roark & Sproat, "Structured abbreviation expansion in context"** (<https://arxiv.org/abs/2110.01140>) deals with exactly the *ad hoc*, intentional abbreviations that depart substantially from the original word and are resolved by context — literally "PDT NOUV". And **Tomanek, Cai & Venugopalan** (<https://arxiv.org/abs/2312.14327>) address personalisation with very little user data, transposable to a per-household learning loop.
⚠️ **No corpus and no open source project dedicated to French till-receipt abbreviations.**

#### PostgreSQL reference points

**`pg_trgm`** (standard contrib, PG 16/17/18) — **the decisive point: use `word_similarity` (`<%` / `%>`, threshold 0.6), not `similarity` (`%`, threshold 0.3)**. `similarity('CRQ MONSIEUR X4', 'Croque-monsieur jambon fromage')` will be catastrophic (the trigram denominator crushes the score), whereas `word_similarity` looks for the best continuous subsequence of the second string — exactly the shape of the problem (a short label to be found *within* a long name).
⚠️ `set_limit()` / `show_limit()` are deprecated → `SET pg_trgm.similarity_threshold`.
**GIN for the pre-filter** (1.25 M → a few hundred candidates) then application-side sorting; GiST is only necessary for KNN `ORDER BY col <-> 'txt' LIMIT n`.
⚠️ **No reliable benchmark of pg_trgm on 50k – 2M rows was found.** The documented risk is too low a threshold: at 0.1 on 1 M rows, candidates explode and the planner switches to a seq scan. To be measured.
Source: <https://www.postgresql.org/docs/18/pgtrgm.html>

**`unaccent` — the IMMUTABLE trap.** `unaccent()` is `STABLE`, so `CREATE INDEX ... (unaccent(name))` fails. The correct wrapper names the dictionary explicitly:

```sql
CREATE OR REPLACE FUNCTION f_unaccent(text) RETURNS text AS
$$ SELECT public.unaccent('public.unaccent', $1) $$
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT;

CREATE INDEX idx_produits_nom_trgm
  ON produits USING gin (f_unaccent(lower(product_name)) gin_trgm_ops);
```

⚠️ The index is only used if the query rewrites **exactly** the same expression, in the same order.

**French FTS**: the standard `french` configuration, to be combined with unaccent as a *filtering* dictionary (before the stemmer). `websearch_to_tsquery` is the only parser that **never raises a syntax error** → the only safe one on raw input. `ts_rank_cd` (cover density, which accounts for proximity) rather than `ts_rank`. **PostgreSQL 18 brings no FTS novelty and still no BM25 in core.**

**BM25**: ParadeDB's `pg_search` (v0.25.0 of 28/07/2026, 9.1k ★, weekly releases, PG 15+) is mature — ⚠️ **but AGPL-3.0**: no effect under pure self-hosting, §13 applies if Chaudron becomes a service accessible to third parties. **A trade-off to be made consciously, not discovered later.**

**`pgvector` — pin ≥ 0.8.6** (released on 29 July 2026). Recent history: **six fixes in five months, including an HNSW index corruption on vacuum (0.8.3) and two buffer overflows**. This is not grounds for rejection (the project fixes fast and publicly), but do not stay on an earlier 0.8.x.
Source: <https://github.com/pgvector/pgvector/blob/master/CHANGELOG.md>

**Embeddings**: **Qwen3-Embedding-0.6B** (Apache 2.0, MTEB multilingual 64.33) — **MRL allows truncation to 256 or 384 dims without retraining**, which divides the size of the HNSW index by 3–4; on labels of 3–6 tokens, 1024 dims is waste. Alternative: **BGE-M3** (MIT), which produces **dense + sparse in the same forward pass** → both legs of the fusion without an AGPL BM25 extension. For pure French, **Solon-embeddings-large-0.1** (MIT) has the only published FR figures (MTEB-FR 0.7490).
⚠️ **None of these benchmarks evaluates abbreviated labels.** Plan an in-house evaluation set of 200–300 annotated pairs: that is the only figure that will count.

**Hybrid fusion — RRF, k=60**: `score(d) = Σ_r 1/(k + rank_r(d))`. The principle that makes it work: we do not merge **scores** (incomparable between BM25 and cosine) but **ranks**, which makes the fusion insensitive to each engine's calibration. Reference PostgreSQL implementation: <https://github.com/pgvector/pgvector-python/blob/master/examples/hybrid_search/rrf.py> — two CTEs, rank by window function, **`FULL OUTER JOIN`** + `COALESCE` (a detail that matters: a document found by only one engine stays a candidate).
→ **Add a third trigram leg to the same fusion: on abbreviated labels, the trigram catches what the French stemmer and the embedding both miss — the truncations (`CRQ`, `PDT`) that are neither lexemes nor semantically meaningful.**
⚠️ The value k=60 is confirmed by the pgvector implementation, not by the source paper (Cormack et al., SIGIR 2009, not retrievable).

**`RapidFuzz`** (MIT, C++ core) for **re-ranking 50–200 candidates already returned by PostgreSQL, never for a full scan**; `token_set_ratio` when the label is an unordered subset of the product name.

#### Learning loop

⚠️ **This subsection is not sourced** — it is engineering reasoning grounded in the papers cited.

```sql
CREATE TABLE receipt_alias (
    id               bigserial PRIMARY KEY,
    raw_label        text NOT NULL,          -- raw label, as printed
    normalized_label text NOT NULL,          -- lower + unaccent + normalised spaces
    retailer         text,                   -- abbreviations are retailer-specific
    product_code     text,                   -- Open Food Facts GTIN, nullable
    ciqual_code      text,                   -- fresh/loose pivot, nullable
    confirmed_by     text NOT NULL,
    confirmed_at     timestamptz NOT NULL DEFAULT now(),
    hit_count        integer NOT NULL DEFAULT 0,
    last_hit_at      timestamptz,
    CONSTRAINT one_target CHECK (num_nonnulls(product_code, ciqual_code) = 1)
);
CREATE UNIQUE INDEX ON receipt_alias (normalized_label, coalesce(retailer, ''));
```

Four non-negotiable points:

1. **The key is (normalised label, retailer)**, not the label alone. "CRQ" at Leclerc and at Carrefour do not necessarily denote the same thing, and **a false alias learned globally poisons the whole corpus**.
2. **Keep the raw label AND the normalised one**: the day the normalisation function changes, it must be possible to replay on the raw one.
3. **Record the rejections too.** A user who refuses proposal no. 1 and picks no. 3 produces a negative signal — exactly the "hard negative" of Block-SCL (<https://arxiv.org/abs/2207.02008>), **and the most expensive signal to obtain**. A table that records only the successes throws away half the information.
4. **The alias lookup short-circuits the whole pipeline**: a `SELECT ... WHERE normalized_label = ? AND retailer = ?` in O(1) **before** touching pg_trgm or the embedding. After a few dozen shopping trips, the bulk of a household's recurring basket is covered, and the expensive pipeline only serves novelties. **That is what makes the project economically viable.**

And the abbreviation dictionary feeds itself: every confirmed alias gives a label ↔ product name alignment, from which token-to-token correspondences are extracted. **Our real learning loop is not model fine-tuning, it is dictionary accumulation.**

### 3.7 Proposed architecture for the receipt strand

```
PWA (client)
  └─ framing guide, rejection if angle > 10°, controlled downscale
     │
     ├─► [1] classic OCR (PP-OCRv6 / docTR, CPU, ~€0)
     │        └─ pixel-faithful text, unstructured
     │
     └─► [2] VLM (Claude Sonnet 5, or a self-hosted model)
              └─ output_config.format + JSON Schema → structured lines
     │
  [3] CORROBORATION: numerical divergences [1] × [2] → confidence score
     │
  [4] Pydantic validation + arithmetic checks
     │   (⚠️ outright-failure detector, NOT a certificate of correctness — cf. §3.4)
     │
  [5] Label → product matching
     ├─ receipt_alias lookup (normalized_label, retailer)   ← O(1), short-circuit
     ├─ abbreviation expansion (self-feeding dictionary)
     ├─ pg_trgm word_similarity blocking on f_unaccent(lower(...)) → ~200 candidates
     └─ RRF k=60 over 3 legs: trigram + FTS fr_unaccent + vector (Qwen3-0.6B @256d)
     │
  [6] Human review screen (photo + JSON + discrepancies + divergences)
     │
  [7] Write to database + learning (aliases confirmed AND rejected)
```

**Recommended default in the selection interface (ADR-0005/0007): Claude Sonnet 5** for receipt extraction, with Haiku 4.5 evaluated in parallel — it is structurally advantaged on cost because it is on the standard tier (1564 tokens where Sonnet pays 3888), but it sees an image resized to 1269×952, which may be disqualifying on a receipt. This is **not** an architectural choice: it is the value the interface proposes by default, and which the household remains free to change.

⚠️ **Point of vigilance on degradation by capabilities.** ADR-0007 already anticipates that "receipt extraction on damaged thermal paper works well with a recent proprietary model, poorly with a small open model". The figures in §3.4 confirm and aggravate this: **even the frontier models plateau at 0.49–0.58 F1 on line items**. An honest expectation must therefore be displayed from configuration onwards, and **the human review screen must never be disabled based on the provider** — the temptation to "trust Opus but not Ollama" is exactly the shortcut that arithmetic doctoring makes dangerous.

**Starting path**:
1. Assemble **200–500 annotated French receipts** (retailer, date, total, VAT per rate, lines) deliberately including crumpled ones, faded thermal ones, ones photographed askew.
2. Measure the **completion rate of the OFF France fields** (10 min of SQL) — it conditions the whole matching strategy.
3. Compare on that corpus: Claude Sonnet 5, Claude Haiku 4.5, Google Expense Parser (1 ¢), and PaddleOCR-VL-1.6 / LightOnOCR-2-1B locally.
4. **Measure at field level** (exact match on the total, F1 on the lines), **not at character level**. A CER of 2% that lands on the cents digit is a business failure.
5. Explore Open Prices as a bootstrap for a till-label ↔ GTIN lexicon.

**Note on self-hosting**: the landscape flipped in 2026 — on OmniDocBench v1.6, **PaddleOCR-VL-1.6 (0.9 B, Apache 2.0) scores 96.34, ahead of Gemini 3 Pro (92.91) and Qwen3-VL-235B (89.78)**. The relevant models weigh 2 to 3.5 GB in fp16: an RTX 4090 24 GB is plenty. Two candidates to watch for French: **PaddleOCR-VL 1.6** (109 languages, French named) and **LightOnOCR-2-1B** (Apache 2.0, French, training explicitly targeting "*scans, French documents*", 5.71 pages/s on an H100 — but **Tables 45.4**, weak, to be tested seriously if line items are extracted in tabular form).
⚠️ **Blocking licences to know about**: Nanonets-OCR (Qwen Research License, **commercial use prohibited**), Surya (non-commercial weights above a revenue threshold), Qwen2.5-VL (the 7B is Apache 2.0, **the 3B and the 72B are not**), MinerU (possible addendum). Florence-2 is to be ruled out (a score of 0 zero-shot on DocVQA).
Source: <https://github.com/opendatalab/OmniDocBench>

**These open models are the real substance of ADR-0007's `ollama` mode.** That ADR describes local inference as a first-class mode, but quality there was an unknown. The OmniDocBench ranking partly resolves it: an **Apache 2.0 model of 0.9 B** fitting on any consumer GPU beats Gemini 3 Pro on document parsing. **For receipt extraction specifically, local mode is therefore not the poor relation** — it is, on the other hand, for recipe suggestions, which demand generalist reasoning. This asymmetry deserves to be stated to the user at configuration time, rather than letting them believe in a uniform degradation.

**Switchover threshold to self-hosting**: below 50,000 receipts/month, the cloud API stays more economical (engineering time costs more than the difference). Above 350,000/month, self-hosting wins by a factor of 4 to 10. **At any volume if the argument is GDPR/sovereignty** — a till receipt is revealing personal data (consumption habits, implicit geolocation). This is exactly the logic already adopted by ADR-0007, which puts forward `ollama` and `byok` Mistral (EU-hosted) as the two configurations keeping the data under European jurisdiction.

---

## 4. Shopping-list export

### 4.1 The finding that changes the architecture

The question "how do we write into iCloud Reminders?" is badly posed, and that is what traps everyone. Two things the web systematically conflates must be separated:

| | State in 2026 |
|---|---|
| **iCloud as a server**, exposing *its* Reminders over CalDAV to a third party | ❌ **Dead since iOS 13** |
| **The Reminders app as the CalDAV client of a third-party server** (ours) | ✅ **Works, proven in April 2026 on iOS 26.4.1** |

**Let us not try to write into iCloud. Let us be the CalDAV server.**

### 4.2 iCloud Reminders — three dead ends and one opening

**No public Apple API.** EventKit is strictly local (an on-device framework for a signed native app, watching the device's Calendar database — <https://developer.apple.com/documentation/eventkit>). CloudKit Web Services only gives access to **our own containers**: the URL structure is `https://api.apple-cloudkit.com/database/1/[container]/...` where "*The container ID begins with `iCloud.`*" and is created in **our** developer account. **No documented mechanism allows targeting another app's container**, let alone an Apple system app.
Source: <https://developer.apple.com/library/archive/documentation/DataManagement/Conceptual/CloudKitWebServicesReference/SettingUpWebServices.html>

**iCloud's CalDAV no longer exposes Reminders.** That was your question, and the answer is sourced:

> "*Reminders sync has been **disabled by Apple** and is only available when you use very old iOS versions and never upgraded it.*"
> — DAVx⁵, the reference CalDAV client on Android: <https://www.davx5.com/tested-with/icloud>

Converging corroborations:
- **Tasks.org**: "*The new Apple Reminders app introduced in iOS 13 and macOS 10.15 uses a proprietary format that is not compatible with Tasks*" (<https://tasks.org/docs/caldav_icloud.html>)
- **BusyMac**: on upgrading to iOS 13/Catalina, "*the new Reminders app migrates all your to-do-only calendars off of CalDAV and into a **private silo that only the Apple Reminders app can access***" (<https://www.busymac.com/docs/faqs/112990-reminders-in-ios-13-and-macos-catalina-drops-support-for-caldav/>)
- **python-caldav** — a telling primary source: the iCloud profile in `compatibility_hints.py` is **commented out/disabled** and carries the **`'no_todo'`** flag. The reference issue has been **closed since March 2021**: "*I will close this issue, as no more work is planned to be done on icloud support*" (<https://github.com/python-caldav/caldav/issues/3>)
- **Home Assistant**: iCloud reminder lists come back as CalDAV collections but "*never show events*"; the February 2025 ticket reporting that they arrive "*with a warning and do not have the correct content*" was **closed as "not planned"** (<https://github.com/home-assistant/core/issues/138121>)

⚠️ **Counter-indication reported for honesty**: a technical post shows a vdirsyncer configuration with `item_types = ["VTODO"]` against `caldav.icloud.com`, while noting that "*the built-in iCloud integration for Reminders and Calendars doesn't use the same CalDav endpoint*" (<https://heywoodlh.io/cross-platform-icloud/>). The most coherent reading: VTODOs do exist on the server side but in a **parallel silo invisible to the native Reminders app**. Unresolved without a test on a real account — **build nothing on it**.

**Authentication is hostile in any case.** An app-specific password is mandatory, generated on **`account.apple.com`** (no longer `appleid.apple.com`), **2FA required**, **max 25 active**, and above all: "*Any time you change or reset your primary Apple Account password, **all of your app-specific passwords are revoked automatically***." The services covered are enumerated as "*mail, contacts, and calendars*" — **Reminders are never mentioned among them**.
Source: <https://support.apple.com/en-us/102654> (page published on 8 October 2025)
No deprecation announcement found — to be treated as unverified rather than as a no.

**Operational fragility**: rate limiting **not documented** (a developer who has maintained an iCloud CardDAV sync for 8 years reports sudden "rate limit exceeded" errors, with "*nothing in Apple's documentation relating to these limits*" — <https://developer.apple.com/forums/thread/722170>), waves of 503s (<https://mjtsai.com/blog/2022/01/24/increased-icloud-errors/>), and Apple has **never officially supported CalDAV**.

**The opening: Apple Reminders as a CLIENT of a third-party CalDAV server still works.** Settings → Calendar → Accounts → Other → Add CalDAV Account exposes a **"Reminders"** switch alongside "Calendars".

> **Decisive and recent proof**: Vikunja ticket #2658, opened on **19 April 2026** on **iOS 26.4.1** — "*iOS Reminders correctly **pushes** changes to Vikunja over CalDAV, but doesn't fetch changes made on the Vikunja side*". This is a **fetch regression**: the connection exists and works in production (the push works). Fixed by PR #2721.
> <https://github.com/go-vikunja/vikunja/issues/2658>

Corroborated by <https://tasks.org/docs/client_apple_reminders/> ("*Your Tasks.org lists will appear in Reminders*"), <https://github.com/nextcloud/tasks>, and [a step-by-step procedure](https://portal.thobson.com/knowledgebase/226/How-to-sync-calendars-and-tasks-to-an-iOS-device-using-CalDAV.html) mentioning the step "*Choose Calendars and/or Reminders (tasks)*".

Known friction: **flattened subtasks** (the `RELATED-TO` hierarchy displays flat), cases of lists invisible on iOS while macOS works (<https://github.com/sabre-io/Baikal/issues/995>, unresolved).

### 4.3 Google Tasks / Keep / Assistant

**Google Tasks API: yes, and it is the only viable Google route.** `tasks v1`, `https://tasks.googleapis.com`. Creation is trivial: `POST /tasks/v1/lists/{tasklist}/tasks` with `{"title": "Lait"}` (title ≤ 1024 characters). **No documented batch** → 1 HTTP request per item. Quota: **50,000 requests/day**, no published per-minute limit, no pricing (absence of a pricing page, not an explicit declaration of being free).
Sources: <https://developers.google.com/workspace/tasks/overview> · <https://developers.google.com/workspace/tasks/reference/rest/v1/tasks/insert> · <https://developers.google.com/workspace/tasks/limits>

**Scope classification — the point that costs.** ✅ `auth/tasks` is **NOT restricted**: the list of restricted scopes is closed and enumerated (Gmail, Drive, Fit, Chat, Data Portability, Photos Ambient, Health). ⚠️ **It is therefore *sensitive*** by application of the official definition ("*Sensitive scopes are scopes that request access to private user data*") — but **no Google page names it literally as such**. This is a rigorous deduction, not a citation. **A decisive 2-minute test: add the scope in Google Cloud Console → Google Auth Platform → Data Access and see which section it falls under.** To be done before any commitment.

Consequences if sensitive (the likely scenario): domain verified in Search Console, homepage, privacy policy on the same domain, YouTube video of the OAuth flow, up to **10 days** of review (page updated 17 July 2026) — **but no CASA and no annual re-certification**, which radically changes the calculation compared with Gmail (§2.1).
Sources: <https://support.google.com/cloud/answer/13464325> · <https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification> · <https://support.google.com/cloud/answer/13465431>

The exemption under 100 users still applies ("*Personal Use apps: if the app is for your personal use (fewer than 100 users)… users will be allowed to click through "unverified app" warning screens*"), a **ceiling cumulative over the life of the project, non-resettable**.
And **the "Testing" trap applies here too**: "*Authorizations by a test user will expire seven days from the time of consent… that token will also expire*" → **switch to "In production" from the outset**.
Sources: <https://support.google.com/cloud/answer/13464323> · <https://support.google.com/cloud/answer/15549945>

**Watch item**: since 1 May 2026 Google has been tightening Workspace quotas (Gmail/Calendar/Drive first), and "*Later in 2026 […] API usage over standard daily thresholds will generate charges on your Google Cloud bill*" (<https://developers.google.com/workspace/tools-safety>).

**Google Keep API: perfect data model, inaccessible authorisation path.** The API exists (`keep v1`, `notes.create`, a `Note` resource with `body.list` / `ListItem{text, checked}` up to 1000 items) — it is **exactly** a shopping list. But: "*The Google Keep API is used **in an enterprise environment***", "*allowing **enterprise administrators** to manage Google Keep notes*", and **the only two documented authorisation modes are variants of domain-wide delegation**, with the quickstart requiring "*domain-wide delegation of authority in the Google Workspace Admin console by a **super administrator account***". A @gmail.com account has no domain, no admin console and no super-admin.
Sources: <https://developers.google.com/workspace/keep/api/reference/rest> · <https://developers.google.com/workspace/keep/api/guides>
⚠️ **Not verified**: **no Google sentence explicitly forbids personal accounts**, nor lists the required Workspace editions (searched in the REST reference, the guides, the quickstart, the product page, the discovery doc, the Workspace Updates archives). The restriction is massively implied by the whole authorisation path, **never stated in black and white**. → **Rule it out: designing on it means betting on undocumented behaviour.**

**Shopping list / Assistant lists: no API, and there never has been one.** Machine-readable proof: the official discovery directory of all public Google APIs (**523 APIs** as of 3 August 2026) contains as near entries only `keep v1`, `tasks v1`, `content v2.1` (Merchant Center, unrelated) and `homegraph v1`. **No list API.**
Source: <https://www.googleapis.com/discovery/v1/apis>
History: Assistant lists left Keep for Google Home/Express in April 2017, then came back (unmigrated data deleted after 1 May 2024); the **Conversational Actions — the only third-party Assistant developer surface — are "*deprecated on June 13, 2023*"** and never exposed the user's lists anyway; the current Google Home APIs have a Matter/Thread/device scope (0 occurrences of "shopping list" or "grocery" on their indexes).
Sources: <https://support.google.com/assistant/answer/14171370> · <https://developers.google.com/assistant/ca-sunset> · <https://developers.home.google.com/>

**Google Calendar + CalDAV VTODO: explicit refusal.**
> "*Data exposed in the CalDAV interface is formatted according to the iCalendar specification. **Doesn't support `VTODO` or `VJOURNAL` data.***"
> <https://developers.google.com/workspace/calendar/caldav/v2/guide>

### 4.4 Apple Shortcuts — entirely feasible, two points to test

**`Get Contents of URL`** supports GET, POST, PUT, PATCH, DELETE, and "*Request Body allows you to send **JSON**, a Form, or a File*".
Source: <https://support.apple.com/guide/shortcuts/request-your-first-api-apd58d46713f/ios>
⚠️ **HTTP headers are NOT documented by Apple** — checked on that page (iOS and Mac) and across the whole "Use Web APIs in Shortcuts" section: zero occurrences of "Headers", no page on authentication. The parameter **does exist** (action `is.workflow.actions.downloadurl`, parameters URL / Method / **Headers** / Request Body) but is only sourceable through a serious third-party database (<https://matthewcassinelli.com/actions/get-contents-of-url/>, ex-Workflow/Shortcuts team). **An `Authorization: Bearer …` is feasible, to be validated on a device.**

A notable official limit: "*OAuth 2 […] is currently **not supported***" (<https://support.apple.com/guide/shortcuts/api-limitations-apd891a6c84e/9.0/ios/26>) → plan for a **static token**, not an OAuth flow.

**Complete chain, entirely documented by Apple**:
`Get Contents of URL (POST/JSON)` → `Get Dictionary Value` ("*The data dictionary is actually a **list of dictionaries***") → `Repeat with Each` ("*runs the same group of actions **one time for each item***", variable `Repeat Item`) → **`Add New Reminder`** ("*Creates a new reminder and adds it to the **selected list of reminders***", parameters Reminder / **List** / Alert / Priority / Flag / URL / Notes).
→ **Yes, a specific "Courses" list can be targeted.**
Sources: <https://support.apple.com/guide/shortcuts/get-dictionary-value-action-apdf01294032/ios> · <https://support.apple.com/guide/shortcuts/use-repeat-actions-apdc11deb2c1/ios> · <https://matthewcassinelli.com/actions/add-new-reminder/>
⚠️ **Not verified**: the exact name of the action shipped in iOS 26 stable — Apple has been testing an App Intents replacement named `Create Reminder` since iOS 18, and publishes no official action reference.

**Distribution: the "Untrusted Shortcuts" setting no longer exists.** Proof by diffing **the same Apple guide page at two versions**:

| Version | Title | Content |
|---|---|---|
| Guide 3.2 (iOS 14 era) | "Enable shared shortcuts" | Enable Settings → Shortcuts → **Allow Untrusted Shortcuts** |
| Guide 9.0 / iOS 26 | "Advanced Privacy and security settings" | **No** mention; only "Allow Running Scripts" and the anti-malware analysis remain |

<https://support.apple.com/en-kz/guide/shortcuts/enable-shared-shortcuts-apdfeb05586f/3.2/ios> vs <https://support.apple.com/guide/shortcuts/apdfeb05586f/9.0/ios/26>
(The removal is dated to iOS 15 by community threads — a date not officially confirmed, but **the current absence of the setting is established by Apple's documentation**.)

**Actual journey in 2026**: iCloud link → presentation screen → **"Get Shortcut"**, with no warning. Sharing with "Anyone" implies that "*Apple will receive a copy of your shortcut for validation*"; revocable via "Stop Sharing".
**And the key building block: Import Questions** — "*When the recipient runs the shortcut, they're presented with the import questions […] the shortcut is populated with the user's own information*", and the field "*is cleared when the shortcut is shared*". **This is the mechanism designed to make each user enter their own API URL and token, without hard-coding a secret in the shared shortcut.**
Sources: <https://support.apple.com/guide/shortcuts/share-shortcuts-apdf01f8c054/ios> · <https://support.apple.com/guide/shortcuts/add-import-questions-to-shared-shortcuts-apdf330fd3a0/9.0/ios/26>

**Hourly automations without interaction: yes.** "*Some personal automations can run without asking you for confirmation*" (disable "Ask Before Running", then "Don't Ask"); the **Time of Day** trigger is documented. The trade-off: in "Run Immediately" mode, **the notification becomes mandatory** (the "Notify When Run" toggle disappears). And **automations are local to the device, they do not sync.**
Sources: <https://support.apple.com/guide/shortcuts/enable-or-disable-a-personal-automation-apd602971e63/ios> · <https://support.apple.com/guide/shortcuts/event-triggers-apd932ff833f/ios>

⚠️ **Two blind spots to test on a device (half a day)**: (a) the `Headers` parameter, undocumented by Apple; (b) the privacy prompt on first network access — Apple documents the generic dialogue (Allow Once / Always Allow / Don't Allow) but **no page describes a per-web-domain prompt**, and community threads describe repeated requests. **This is the main UX risk of this scenario.**

**Android: the gap is structural, and it is not where you expect.** The HTTP call is a solved and free problem — **HTTP Shortcuts** (`ch.rmy.android.http_shortcuts`, MIT, v4.6.0 of 18 June 2026 on F-Droid) does every method, Basic/Digest/**Bearer**/client-certificate auth, custom headers and bodies, JavaScript before/after; **Tasker** (~4.49 USD) too, its documentation example being literally `Authorization:Bearer MY_ACCESS_TOKEN`.
**The missing link is writing into a mainstream list**: there is **no standard Android contract** for tasks (the framework has `CalendarContract`, nothing for lists); `actions.intent.UPDATE_ITEM_LIST` is an intent an app **declares in order to receive** Assistant commands, not an injection channel, it is **being deprecated** and en-US only; for Keep, the only observed mechanism is `Intent.ACTION_SEND` to `com.google.android.keep` — **undocumented**, creates a new note, does not target a list. And Tasks.org's official Tasker plugin "*can only set the title, due date, due time, priority, and description*" — **choosing the list is not exposed**.
Sources: <https://f-droid.org/en/packages/ch.rmy.android.http_shortcuts/> · <https://tasker.joaoapps.com/userguide/en/help/ah_http_request.html> · <https://developer.android.com/reference/app-actions/built-in-intents/productivity/update-item-list> · <https://tasks.org/docs/tasker/>

| | iOS / Shortcuts | Android |
|---|---|---|
| Automation app | **Preinstalled** | To be installed |
| Installation in 1 link | **Yes** (iCloud link) | **No** — no equivalent |
| URL/token entry by the user | **Import questions**, designed for it | Variables to be created by hand |
| Writing into the native task app | **Yes, list of your choice** | **No** |
| Scheduling | Built in | Tasker/MacroDroid on top |

→ **On Android, the real solution is not on the phone, it is on the server side.** For a non-technical audience, "nothing to install" structurally beats "install Tasker and configure a macro".

### 4.5 Open standards

**RFC 4791** = the CalDAV protocol; **RFC 5545** = the iCalendar format, including `VTODO` (§3.6.2). A constraint to know: RFC 4791 §4.1 **forbids** mixing VEVENT and VTODO in the same object resource. Ongoing evolution: `draft-ietf-calext-ical-tasks-17` (10 December 2025), submitted to the IESG.
Sources: <https://www.ietf.org/rfc/rfc4791.txt> · <https://www.rfc-editor.org/rfc/rfc5545.html> · <https://datatracker.ietf.org/doc/draft-ietf-calext-ical-tasks/>

**Who actually consumes VTODOs**: Apple Reminders (as the client of a third-party server — §4.2), Thunderbird ("*implements `VEVENT` events and `VTODO` tasks*"), DAVx⁵ which routes VTODOs to **jtx Board, OpenTasks and Tasks.org**, Nextcloud Tasks, Nextcloud Deck, Vikunja, Evolution. **Google Calendar: no, explicit refusal.**
⚠️ **Business model to know about**: at Tasks.org, Google Tasks and Microsoft To Do come with no subscription, but **CalDAV requires an in-app subscription** (or a GitHub sponsorship) — <https://tasks.org/docs/sync/>. This is real friction on Android.

**.ics / webcal:// feeds containing VTODOs — the intuition is probably right, without formal proof.**
- **Google Calendar ignores VTODOs**: "*When you import from an ICS file into Google Calendar, it only imports calendar entries from that file; **it ignores tasks ("VTODO" entries)***" (<https://groups.google.com/g/tasks-backup/c/YVUSYThNtl8>, project moderator). ⚠️ The quote is about **file import**; no explicit Google source on "Add by URL". Consistent, not proven.
- **iOS: NOT FORMALLY VERIFIABLE.** Two elements only: a direct user report that **went without a valid answer** (1 October 2023, "*I have created a subscribed calendar with some reminders (VTODO) being generated, but these reminders are not appearing in the Reminders app*" — <https://discussions.apple.com/thread/255169909>); and a **strong industry signal** — Todoist, which has exactly this need, **does not emit VTODOs** in its iCal feed, it converts tasks into events ("*Tasks with a date but without a time will appear as all-day events*"). If a subscription → Reminders path existed, Todoist would use it.
- A useful counter-example: **Tasks.org cannot** subscribe to an ICS feed of VTODOs — a ticket open **since 28 January 2015**, no PR (<https://github.com/tasks/tasks/issues/235>).
→ **Do not invest in this route.** If a feed is wanted, emit **VEVENT**s (the Todoist approach) — but that lands in the calendar, not in a tickable list.

**Web Share API — the best coverage/effort ratio.** Support **90.3% globally** as of 3 August 2026: Safari iOS ✅ (12.2+), Chrome Android ✅, Chrome desktop ✅ (128+), Edge ✅ (95+), **Firefox desktop ❌**.
Source: <https://caniuse.com/web-share>
Constraints: HTTPS mandatory; **transient activation required** ("*must be triggered off a UI event like a button click*", otherwise `NotAllowedError`); third-party iframes need `allow="web-share"`.
On iOS, **Reminders is indeed a target of the native share sheet**.

⚠️ **Two documented iOS bugs still being reported in March 2024** (<https://developer.apple.com/forums/thread/724641>): (1) the **query string is stripped** when sharing via Messages/Messenger; (2) a **cross-domain URL is replaced by the current page's URL**. Reported workaround: put the URL in `text`.
→ **Direct consequence: on iOS, `url` is treated as "the URL of the page being shared", not as arbitrary data. For a shopping list, put everything in `text` and do not supply `url` at all.**

⚠️ An article of 7 January 2026 notes that for selected text, "*Longer selections often **generate multiple suggested reminders at once***" — **but that comes under Apple Intelligence**, hence conditioned on the hardware and on language/region settings. An unguaranteed bonus, not to be promised.
Source: <https://appleinsider.com/articles/26/01/08/how-to-turn-emails-webpages-notes-into-reminders-with-apple-intelligence>

**Web Share *Target*** (a PWA that *receives* a share): Chrome desktop 89, Chrome Android 76, Edge, Samsung — **Firefox `false`, Safari `false`, Safari iOS `false`**. **Android/Chromium only, no iOS path.**

**Multi-line copy-paste: no, and it is a regression.** In Apple Reminders, pasting a multi-line block creates **ONE SINGLE reminder** containing the whole list — "*pasting a list into Reminders **stopped** creating a list of reminders items*" (23 January 2021), confirmed on macOS Sonoma 14.1 in November 2023.
Sources: <https://nowicki.dev/how-to-import-a-list-into-apple-reminders/> · <https://discussions.apple.com/thread/255303302> · <https://talk.tidbits.com/t/importing-a-list-into-reminders/21034>
A reliable workaround, the "Notes trick": paste into **Notes** → convert to a checklist → copy → paste into **Reminders** → one reminder per line. ⚠️ Undocumented by Apple, unstable over time.
⚠️ **Google Keep: NOT VERIFIED** — no source, first- or second-hand, confirms that a multi-line paste creates one checkbox per line. Google documents only manual conversion. **To be tested before making it an architectural assumption.**

### 4.6 Third-party alternatives, underestimated

- **Todoist REST v1**: "*You can use our API for free*", OAuth2 **or a personal token**, task creation in a project, `/sync` endpoint for batching. Near-zero effort. <https://developer.todoist.com/api/v1/>
- **Microsoft To Do via Graph**: `POST /me/todo/lists/{id}/tasks`, with `checklistItem` for sub-items, delta query, delegated permissions. Works on personal **and** work accounts. <https://learn.microsoft.com/en-us/graph/api/resources/todo-overview>
- **Bring!** (the dominant shopping app in Switzerland): **no official API**. The integrations (node-bring-api, Home Assistant) rest on an undocumented, reverse-engineered API, with an explicit disclaimer "*in no way endorsed by or affiliated with Bring! Labs AG*". Technically it works and it is widely used, **but it is a bet on a private endpoint** — exactly the reason the ADR rules out retailer drives. **Consistency requires: no.**
  <https://github.com/foxriver76/node-bring-api>

### 4.7 Effort / value ranking

| Rank | Route | Effort | Coverage | Verdict |
|---|---|---|---|---|
| **1** | **`navigator.share({ text })`** + `clipboard.writeText()` fallback | **~1 day** | iOS ✅ Android ✅ desktop ✅ (except Firefox) | ✅ **To be done first, no discussion.** ~90% of the benefit for ~5% of the effort. Without `url`. |
| **2** | **CalDAV / VTODO endpoint** served by our backend | **High** (CalDAV server, auth, ETags, sync-tokens) | iOS ✅ (native account) Android ✅ (DAVx⁵ + Tasks.org) desktop ✅ | ✅ **The only genuine open standard that lands in the native apps of both platforms, with ZERO installation on the phone.** The contract is an RFC, not a proprietary API that can shut down. |
| **3** | **iOS Shortcut** via iCloud link + import questions | ~2–3 days + user documentation | iOS only | ✅ **Excellent on iPhone.** Zero installation friction since iOS 15, one reminder per item in the right list, hourly automation possible. **2 tests to run first** (Headers, network prompt). |
| **4** | **Google Tasks API** | Medium (OAuth + ~10-day review) | Android mainly | ⚠️ The only Google route. **No CASA, no annual re-certification** — that is the good side of being sensitive. Blockers: a ceiling of **100 cumulative users for life** without verification, and Google Tasks is not where people do their shopping. |
| **5** | **Todoist / Microsoft To Do** | Low | Users of those apps | ⚠️ Near-zero effort, restricted audience. A good "bonus" candidate. |
| ❌ | .ics / webcal:// feed of VTODOs | Low | **Probably nil on mobile** | No mainstream mobile client consumes it in a proven way. |
| ❌ | Android automation (Tasker / HTTP Shortcuts) | High **for the user** | Tech-savvy Android | The HTTP call is free and mature, but **nothing allows writing into Keep or Tasks from the phone**. |
| ❌ | Google Keep API | — | Workspace only | Ideal data model, enterprise-only authorisation path. |
| ❌ | Bring! | Medium | Bring! users | Unofficial API — inconsistent with the ADR on drives. |
| ❌ | Writing into iCloud Reminders from a server | — | — | **Impossible.** No API, no CloudKit, no CalDAV. Not a question of effort. |

---

## 5. Summary of hidden costs

| Cost | Where it hides | Amount / impact |
|---|---|---|
| **Annual CASA audit** | Gmail API (`gmail.readonly` = restricted) | **$675/year minimum, in perpetuity**, free self-scan removed, full audit at every renewal |
| **100-user lifetime ceiling** | Any unverified Google app (Gmail **and** Tasks) | Non-resettable, cumulative over the life of the project |
| **7-day refresh tokens** | Publishing status "Testing" (Gmail and Tasks) | Weekly reconnection — a deal-breaker |
| **User password change** | Google (Gmail scopes) and Apple (app passwords) | Breaks the integration **silently**; a stream of support tickets guaranteed |
| **DMARC rejection on forwarded mail** | Cloudflare Email Routing | **Silent** loss of order confirmation mails |
| **Postmark inbound entry ticket** | Absent from Free and Basic | $16.50/month, not $0 |
| **The model doctors the arithmetic** | VLM on receipts | Partially neutralises the `Σ lines == total` check |
| **No logprobs at Anthropic** | Claude API | No native confidence signal: a structural cost to budget from the design stage |
| **10 searches/min at Open Food Facts** | Online API | ≈ 1 receipt/minute → local dump mandatory |
| **Lexicon of FR retailer abbreviations** | Exists nowhere | Start-up cost entirely on us (and our only defensible asset) |
| **AGPL** | Stalwart, ParadeDB `pg_search` | Applies if Chaudron becomes a third-party service |
| **Tasks.org subscription** | CalDAV connector on Android | Real user friction |
| **Thermal paper** | Physics | Receipt unreadable in **7 to 30 days**; the PVC wallet accelerates the destruction |

## 6. Points explicitly not verified

**Blocking for a decision**:
1. **Is inbound port 25 blocked at our hosting provider?** Neither Hetzner nor DigitalOcean specifies the direction of the block. → `nc -l 25` test, 5 minutes.
2. **Exact classification of the `auth/tasks` scope** — rigorously deduced, never stated by Google. → test in the Cloud console, 2 minutes.
3. **Completion rate of `product_name_fr` / `brands` / `quantity` on the France subset of OFF** — published nowhere, determines the matching strategy. → 10 min of SQL on the dump.
4. **The `Headers` parameter of "Get Contents of URL"** — absent from all Apple documentation. → test on a device.
5. **The network privacy prompt in Shortcuts** (per domain or not) — no Apple page. → test on a device.

**Unresolved for lack of a source**:
6. CASA exemption for apps storing data solely client-side (question asked on the official Google forum in March 2026, **left unanswered**).
7. Classification of `gmail.addons.current.message.readonly` (two official pages contradict each other).
8. Admissibility of a pantry app under Google's permitted type no. 4.
9. SendGrid's 2026 pricing (pages in a redirect loop) and Brevo's (amounts in JS).
10. The Mailjet plan that unlocks the Parse API ("Crystal" no longer exists) and the location of its data.
11. Max inbound message size at Postmark, Brevo, ImprovMX, Resend, Mailtrap.
12. Mailgun's EU-region MX; where mail **received** at Resend is stored.
13. Does Cloudflare Email Routing require a domain on full setup?
14. Exact scope of the OpenAI sub-processor at CloudMailin.
15. Blocking of external auto-forwarding at Microsoft 365 (source = blog, not learn.microsoft.com).
16. Behaviour of a webcal:// subscription containing VTODOs on iOS 26/27.
17. Explicit prohibition of @gmail.com accounts on the Keep API (not findable, but massively implied).
18. Multi-line pasting into a Google Keep checklist — zero sources.
19. Exact name of the Reminders action in iOS 26 (`Add New Reminder` vs `Create Reminder`).
20. Deprecation of Apple app passwords — no announcement found.

**Gaps in the field, not in this research**:
21. No study quantifies the drop in OCR rate with the age of a thermal receipt.
22. No publication on *receipt image stitching* or on overlapping tiling for VLMs.
23. No published "VLM with vs without dewarping on crumpled receipts" comparison.
24. No embedding benchmark evaluates abbreviated labels.
25. No public dataset or lexicon of French receipts/abbreviations.
26. No reliable benchmark of `pg_trgm` on 50k – 2M rows.
27. FR domain specifics of receipts (variable weight, negative lines, multi-rate VAT, deposits) — not covered.

**Caveats on figures cited**: the Google Document AI pricing page never loaded in full; the Azure page with `$-` placeholders; ambiguous Mindee pricing (a ×12 discrepancy); angle-degradation figures taken from snippets; +34 points for LightOnOCR-2 on Old Scans announced by the authors themselves; the RRF `k=60` confirmed by the pgvector implementation and not by the source paper; the "learning loop" subsection (§3.6) is not sourced.

---

## 7. Recommended decisions

1. **Ingestion by inbound email, not by mailbox reading.** This is the main route. It removes OAuth verification, the CASA audit ($675/year in perpetuity), the 100-user ceiling and the storage of user secrets entirely.

2. **Self-host reception with Stalwart + MTA Hook**, subject to three prior checks: inbound port 25 open, MTA Hooks documentation read in a browser, AGPL arbitrated. **Immediate MIT fallback: Postal.** **Managed fallback: CloudMailin** (the only one able to force the EU region by DNS) or **ImprovMX Premium** at $9/month (FR datacentres at OVH).

3. **Rule out Cloudflare Email Routing** despite it being free: it rejects on DMARC, so it will silently make some of the mail forwarded from Gmail disappear. Also rule out SendGrid (unverifiable pricing, no webhook security), Resend (webhook without the mail body, account data in the United States) and Postmark ($16.50/month, no EU residency).

4. **Treat capturing the Gmail confirmation mail as a story in its own right.** Without it, onboarding is blocked. And recommend a **selective forwarding filter** to the user (sender = retailer): that is GDPR minimisation, not convenience.

5. **Do not implement the Gmail API.** If automation by mailbox reading becomes a topic again, start with **Microsoft** (delegated OAuth on `IMAP.AccessAsUser.All`, no CASA, no paid audit, free publisher verification) — it is the only large provider where a solo developer can do things properly.

6. **Receipts: hybrid VLM + classic OCR pipeline, with human review mandatory at the start.** The VLM alone is disqualified by the arithmetic doctoring documented by ReceiptBench — it fabricates line items to make the total add up. Classic OCR as the second leg costs almost nothing in CPU and provides **the only confidence signal available**: Anthropic does not expose logprobs, and above all the `ModelProvider` port of ADR-0005 cannot depend on any adapter-specific feature. **The confidence signal must live above the port.** Propose **Claude Sonnet 5** as the interface default (≈ 2.5 ¢/receipt, at the household's expense), without ever conditioning human review on the chosen provider.

7. **Validate with a constrained JSON schema + Pydantic, and validate against the image, not against internal consistency.** Numeric constraints (`minimum`, `maximum`) are not supported by the schema — only `enum` is, usable for VAT rates. The `Σ lines == total` check remains a detector of outright failure, never a certificate of correctness.

8. **Amend ADR-0008: the local Open Food Facts dump becomes a phase 1 prerequisite, not phase 2.** The ADR reasoned about EAN scanning (~15 req/min, one request per scan); label matching consumes the **search** endpoint (**10 req/min**) at the rate of **one request per receipt line**. A single receipt saturates the minute. France-filtered dump (~1.25 M rows, **Parquet** format, not CSV) **+ Ciqual for fresh and loose goods** (weight-priced lines, prefixed `02`/`20`–`29`, will never have an OFF record). Alias table in a separate table to preserve the ODbL boundary.

9. **Matching is decided by abbreviation expansion, not by the similarity engine.** "PDT NOUV 1KG" and "Pommes de terre nouvelles" share no trigram. Build the lexicon per retailer, feed it automatically from user validations, **and record the rejections too**. Use `word_similarity` (not `similarity`), and make the alias lookup an O(1) short-circuit at the head of the pipeline.

10. **Measure three figures before writing any code**: the completion of the OFF France fields (10 min of SQL), the classification of the `auth/tasks` scope (2 min in the console), whether inbound port 25 is open (5 min). Each one can invalidate an entire branch of this note.

11. **List export: `navigator.share({ text })` in v1, without `url`** (two documented iOS bugs). ~1 day of work, ~90% of the benefit, iOS + Android + desktop coverage, no dependency on any verification programme.

12. **Serve a CalDAV/VTODO endpoint in v2.** It is the only path that lands in the **native iOS Reminders** app — proven working in April 2026 on iOS 26.4.1 — and that covers Android via DAVx⁵/Tasks.org, **with nothing to install on the phone**. The contract is an RFC, not a proprietary API that can shut down.

13. **Formally rule out**: writing into iCloud Reminders from a server (impossible: no API, no CloudKit, no CalDAV since iOS 13), the Google Keep API (domain delegation + Workspace super-admin), .ics feeds of VTODOs (no proven mainstream mobile client), Android automation on the phone side (nothing allows writing into Keep or Tasks), and Bring! (unofficial API — the same reason as the ADR on retailer drives).

14. **iOS Shortcut in v3, if the installed base is mostly iPhone.** The whole chain is documented and the *import questions* cleanly solve token distribution. Two device tests to run first (the `Headers` parameter, the network privacy prompt): half a day.

15. **Record these choices in three ADRs** once the five checks in §6 are done: *self-hosted inbound email reception* (Stalwart vs Postal vs managed, with the AGPL trade-off), *shopping-list export* (Web Share then CalDAV), and an **amendment to ADR-0008** on bringing the local dump forward to phase 1. Decision no. 5 (do not implement the Gmail API) deserves to be recorded too: it is a costly non-decision to re-examine every six months if it is not written down.

16. **Revisit any choice of American provider if the Latombe appeal succeeds.** The *Trump v. Slaughter* judgment of 29 June 2026 weakened the independence of the FTC, one of the pillars of DPF adequacy. Recommendations no. 2 (self-hosting) and no. 8 (local dump) put Chaudron out of reach of this risk by construction — one more argument in their favour.

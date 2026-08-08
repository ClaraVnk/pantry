# CalDAV feed for expiry alerts

> How-to guide. **Every identifier quoted here** (environment variables,
> endpoints, headers) **is authoritative exactly as written.**
> Companions: [`adr/0004-pwa-not-native.md`](adr/0004-pwa-not-native.md) (why
> there is no push), [`security-model.md`](security-model.md) (what we protect),
> [`technical-notes-ingestion.md`](technical-notes-ingestion.md) §4 (the research
> that settled the shape).

---

## 1. What it is, in one sentence

Chaudron publishes the products approaching their date as **CalDAV tasks**, which
the iOS **Reminders** app and Android task applications display natively —
**with nothing to install on the phone**, and without Chaudron needing to send
anything at all.

The phone comes and fetches. It is a subscription, not a pushed notification.

---

## 2. Why this shape and not another

**The problem.** Chaudron is a PWA, and on iOS Web Push only exists if the user
has added the application to the home screen — a gesture most people will not
make (ADR-0004). Yet expiry alerts are *the* feature that brings people back:
without them, the application is a chore you have to remember to open.

**What does not work, verified rather than assumed:**

| Route | Status |
|---|---|
| Writing into iCloud Reminders from a server | ❌ **Impossible.** No public API, no CloudKit into a system app's container, no CalDAV — Apple disabled it in iOS 13. |
| A `webcal://` / `.ics` subscription containing `VTODO`s | ❌ **No proven path to Reminders.** The only user report found went unanswered on Apple's forum, and **Todoist — which has exactly this need — does not emit `VTODO`** in its feed: it converts its tasks into events. If a subscription → Reminders path existed, Todoist would use it. On the Android side, Tasks.org has had a ticket open since 2015 for subscribing to an ICS task feed, with no implementation. |
| Google Calendar, in every case | ❌ **Documented refusal** of `VTODO`s by its CalDAV interface *and* on `.ics` import. |
| **A real CalDAV account**, with the Reminders app as the client | ✅ **Works.** Verified in production on iOS 26.4.1 (Vikunja ticket of 19 April 2026). |

**Conclusion: Chaudron is the server.** Serving a static file would have been a
tenth of the code — which is precisely why the question was asked — but a feed
that no phone displays is worse than no feed.

### `VTODO` and not `VEVENT`

"The yoghurt expires tomorrow" is **a thing to do, then to tick off**, not an
appointment. A task has a due date (`DUE`) and a completion state; an event has a
start and an end, and files itself into the calendar among the real
appointments, where **you cannot tick it off** and where a fortnight of groceries
buries the week.

The choice has a cost, and it is named rather than hidden: **Google Calendar does
not consume `VTODO`s.** A household that lives in Google Calendar will see
nothing. The alternative — publishing `VEVENT`s, which is what Todoist does —
would land in the calendar and change the nature of the product. It remains
possible if real usage calls for it; it would be another feed, not a setting.

---

## 3. Enabling the feed (operator)

Two variables, in the backend's environment:

```bash
CHAUDRON_CALENDAR_FEED_ENABLED=true
CHAUDRON_CALENDAR_FEED_EPOCH=1
```

**The feed is disabled by default**, because publishing it amounts to publishing
the inventory of a home (asset **A3** of the threat model, potentially sensitive
within the meaning of Article 9 GDPR).

**HTTPS is mandatory in practice.** Authentication is Basic: the secret travels
in the clear in the `Authorization` header if the connection is not encrypted.
iOS refuses a cleartext CalDAV account anyway, absent explicit fiddling.

No extra key to generate: the feed key is **derived** from
`CHAUDRON_SECRET_KEY` by HKDF, with a dedicated label — a leaked feed
credential teaches nothing about sessions, and vice versa.

---

## 4. Retrieving your credentials (user)

**Owner only.** The endpoint hands out a bearer secret that keeps working until
somebody revokes it on purpose (§10), which is a longer-lived thing to grant than
the read access it duplicates — so it is the owner's decision, and the owner is
also the only person who can take it back.

```bash
curl -s https://chaudron.example.org/v1/calendar/subscription \
  -H "X-Household-Id: <household id>" -b "<session cookie>"
```

```json
{
  "server_url": "https://chaudron.example.org/caldav/",
  "username": "AAAABBBBCCCCDDDDEEEEFFFFGG",
  "password": "<32 base32 characters — the secret, not shown>",
  "calendar_url": "https://chaudron.example.org/caldav/p/AAAA.../cal/expiry/",
  "window_days_past": 7,
  "window_days_future": 30,
  "max_tasks": 200
}
```

- `username` is **opaque**: it does not contain the household id and does not
  allow it to be recovered. It appears in the URL, and therefore potentially in a
  proxy log — that is accepted, it is worth nothing on its own. Both values are
  base32; the placeholders above are deliberately patterned rather than
  realistic, because a document that says "treat this like a password" should not
  print something shaped like one — and because the secret scanner cannot tell an
  illustration from a paste.
- `password` is **the** secret. It only ever travels in the `Authorization`
  header, never in a URL. Treat it like a password.

Check that it answers before configuring a phone:

```bash
curl -su "<username>:<password>" "<calendar_url>"
```

This response is a readable iCalendar file — **it is a verification tool, not a
subscription address.** No phone subscribes that way (see §2).

---

## 5. iOS and iPadOS

1. **Settings** → **Apps** → **Calendar** → **Accounts** → **Add Account** →
   **Other** → **Add CalDAV Account**.
2. **Server**: `chaudron.example.org/caldav/`
   **User Name**: the `username`
   **Password**: the `password`
   (Description: whatever you like.)
3. Tap **Next**. iOS queries the server, then offers two switches:
   **Calendars** and **Reminders**.
   → **Turn "Reminders" on. Turn "Calendars" off.**
4. The **Chaudron expiry** list appears in the **Reminders** app.

**Refresh frequency.** There is no push: iOS polls according to
**Settings → Apps → Calendar → Accounts → Fetch New Data**. An interval of 15 to
60 minutes is normal behaviour. An expiry is not a to-the-minute emergency.

**Renaming the list** on the phone has no effect on the server side, and no
consequence.

---

## 6. Android

Android has **no standard task format** at the system level: it takes two
applications, a synchroniser and a viewer.

1. **DAVx⁵** (F-Droid or Play Store) — the reference CalDAV client.
   Add an account → **Login with URL and user name** →
   URL: `https://chaudron.example.org/caldav/`, then the `username` and the
   `password`. Tick the task collection offered.
2. **A task application** for DAVx⁵ to feed: **jtx Board**, **OpenTasks** or
   **Tasks.org**.

> ⚠️ **Economic friction to know about before starting.** With **Tasks.org**,
> Google Tasks and Microsoft To Do are free, but **CalDAV synchronisation
> requires an in-app purchase** (or a GitHub sponsorship) —
> <https://tasks.org/docs/sync/>. This is not a Chaudron limitation, but a user
> who follows this guide and hits the paywall will blame Chaudron for it.
> **jtx Board** and **OpenTasks** do not have this constraint: try them first.

---

## 7. Desktop

**Thunderbird** (New Calendar → On the Network → CalDAV, with the URL and the
credentials), **Evolution**, **GNOME Calendar** and **Nextcloud Tasks** consume
`VTODO`s with no particular setup.

**Google Calendar: no**, and there is no workaround (§2).

---

## 8. What you see

One task per dated lot:

```
Yaourt nature — 4 pc
  Location: Frigo
  Due: 10 August 2026
```

A **reminder** is set for **9 a.m., household time, the day before the date** —
the hour at which you can still decide what to cook that evening. A reminder
whose moment has already passed is not emitted: an overdue task already rises to
the top of the list by itself, and re-ringing on every synchronisation is the
surest way to get the feed switched off.

The feed is **read-only**, and it says so in the protocol
(`DAV:current-user-privilege-set` contains only `DAV:read`). Ticking a task off
on the phone consumes nothing in Chaudron. Stock is still changed from the
application.

---

## 9. What it exposes

**Four fields per product, and nothing else:** product name, quantity, location,
date. Not the brand, not the barcode, not the price, not who entered it, not the
originating receipt, not the internal identifiers.

What is **never** published: lots without a date, consumed lots, everything
falling outside the window, and everything beyond the cap.

**Feed bounds:**

| Bound | Value | Why |
|---|---|---|
| Past window | 7 days | A weekend away must not make a task disappear before it has been seen. |
| Future window | 30 days | Beyond that it is no longer an alert, it is an inventory — and the inventory is the application. |
| Number of tasks | 200 | Cap on the largest possible response (~60 kB). |
| Polls | 240 per hour per feed | A well-behaved client synchronises four times an hour; that leaves room for several devices and cuts off a loop. |
| Authentication failures | 30 per hour per address | The cost of *checking* an attempt, not brute force — guessing 160 bits is not a threat. |

**If the secret leaks**, the bearer can read those four fields for the products
in the window, for as long as the secret is valid. They **cannot** write, nor
reach the rest of the API: the feed secret is valid nowhere else, and the
household id cannot be deduced from it.

---

## 10. Revoking

### One household, from the application — the ordinary case

```bash
curl -X POST https://chaudron.example.org/v1/calendar/subscription/revoke \
  -H "X-Household-Id: <household id>" -b "<session cookie>"
```

**Owner only.** The answer is the *new* subscription block, in the same shape as
§4: user name, password, URLs.

**What it breaks, and it has to be said on screen before the button is pressed:**
every device subscribed to **this household's** feed stops synchronising on its
next poll, and each one has to be given the new user name and password by hand.
Nothing else moves — nobody is signed out, no other household is affected, and
the stock is unchanged.

Use it when the credential has been seen by somebody who should no longer read
the stock: a lost phone, a flatmate who moved out, a person removed from the
household.

**Why this exists at all.** The credential is derived, never stored, and outlives
membership: the CalDAV tree authenticates with the credential and not with a
session, so removing somebody from `household_member` cuts their session on the
next request and leaves their calendar feed answering `207`. Anybody who opened
the subscription page once kept a permanent read of the household's inventory
(asset **A3**). The counter `household.calendar_feed_epoch` (revision `0013`) is
mixed into the derivation, so incrementing it — which is all this endpoint does —
retires both halves of the pair at once.

### The whole instance — the operator's levers

```bash
# Immediate cut-off of every feed
CHAUDRON_CALENDAR_FEED_ENABLED=false

# Rotation: invalidates every credential already handed out,
# without logging anyone out of the application
CHAUDRON_CALENDAR_FEED_EPOCH=2
```

After a rotation, **every** household re-reads `/v1/calendar/subscription` and
reconfigures its devices. Reach for this after an instance-key incident, not for
one household.

> ⚠️ **Rolling back past revision `0013` undoes every revocation.** The column
> disappears and the derivation falls back to the instance epoch alone, so a
> household that had revoked its feed returns to the credential it revoked. An
> operator rolling back has to bump `CHAUDRON_CALENDAR_FEED_EPOCH` in the same
> move if any revocation has been used.

### Remaining limitation, operational

Resolving an identifier scans the list of live households and computes one HMAC
per row. That is a few microseconds per household, and it is **bounded at 5,000
households**: beyond that the server answers `503` rather than paying a linear
cost in silence. Since it is a bound an operator can grow into, the count is
checked **at startup** whenever the feed is enabled — `calendar_feed_scan_limit_approaching`
at four fifths of the bound, `calendar_feed_scan_limit_exceeded` past it — so it
is a capacity decision rather than something the first phone discovers.

**Why the scan is not simply replaced by an index.** Nothing about the identifier
is stored: it is a keyed derivation of the instance secret, the instance epoch and
the household's counter, and PostgreSQL holds none of the first two. Indexing it
means persisting it, and a persisted identifier has to be rewritten on three
independent rotation paths — `CHAUDRON_SECRET_KEY`, `CHAUDRON_CALENDAR_FEED_EPOCH`
and every per-household revocation — two of which live in the environment, where
no migration and no trigger can observe them. A stale row would fail *closed* and
in silence: the feed stops resolving for a household nobody touched, and nothing
in the database says why. Removing the bound is therefore a **different
identifier design** — a random, stored, indexed handle in the URL, with the secret
still derived — and not an index on this one. It is the change an instance past
5,000 households has to make.

### The cost of *checking* a credential, and what bounds it

Guessing a 160-bit secret is not the threat; the cost of checking one is, since
every attempt reads the household list and derives a MAC per row. Two controls
sit **in front of** that read rather than behind it:

- a credential outside the base32 alphabet the derivation emits is refused before
  any query — which also keeps a non-ASCII string away from `hmac.compare_digest`,
  which raises rather than returning `False`;
- the failure budget (30 per hour per source address) is **charged before** the
  scan and refunded once the request has been admitted. A subscriber polling
  normally therefore spends nothing; only failures accumulate.

The refund is withheld when the poll cap (240 per hour per feed) refuses, so a
client hammering a valid credential is bounded too.

**The address counted is the caller's own, not the proxy's**, so one subscriber's
failures never spend another's budget. That holds because the entrypoint enables
uvicorn's forwarded-header handling only for a *named* peer — the reverse proxy's
pinned address on `chaudron-net` — and refuses to start on
`FORWARDED_ALLOW_IPS=*`; uvicorn then takes the rightmost address that is not
trusted, so a header a client prepends never wins, and Caddy overwrites
`X-Forwarded-For` with the peer it accepted.

What remains, and it is the intended behaviour: a client looping on a credential
its household revoked locks **itself** out for the hour. A bare container run
without the proxy is the one shape where callers do share a bucket, and that is a
development shape.

---

## 11. Known limitations

- **No push notifications.** The phone polls; the delay is the one set by its
  fetch setting.
- **Google Calendar will never show this feed** (§2). That is not fixable on our
  side.
- **iOS has not been verified on a device by this project.** What is verified
  automatically on every test run is that a conformant CalDAV client
  (**python-caldav**, the reference implementation) carries out the full
  discovery — `current-user-principal`, `calendar-home-set`, collection
  enumeration, `calendar-query` — retrieves the tasks, redoes a token-based
  synchronisation without error, and is refused with a wrong password. The
  operation of the **Reminders** app as a third-party CalDAV client is, for its
  part, established by the research (§2) — but against a server implementation
  other than this one. **The missing test is a test on a real device**: add the
  account, enable "Reminders", check that the list appears.
- **Flat subtasks**: iOS flattens `RELATED-TO` hierarchies. Moot here — this feed
  produces none.
- **Partial incremental synchronisation.** The server keeps no history: a client
  presenting an old sync token receives `403 valid-sync-token` and redoes a full
  synchronisation. That is the response RFC 6578 provides for, and the only
  honest one — answering otherwise would leave a deleted task on the phone
  indefinitely.
- ~~**In-memory rate counters, per process.** Two `uvicorn` workers grant two
  budgets (same caveat as `api/throttling.py`).~~ Closed: the poll and failure
  counters are rows in `rate_limit_bucket`, shared by every worker of every
  replica (`infra/rate_limits.py`, migration `0018`).

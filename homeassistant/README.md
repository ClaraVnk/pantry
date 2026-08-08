# Chaudron for Home Assistant

A custom integration that brings one Chaudron household's food stock into Home
Assistant: what is in the cupboards, what is about to go off, and the shopping
list — as a native to-do list you can tick from a card or by voice.

It talks to your own instance over HTTPS with a **machine access token**, which
is the credential Chaudron issues for exactly this purpose. No account password
is involved, nothing is sent to a third party, and the token opens one household
and nothing else.

---

## What you get

| Entity | What it says |
|---|---|
| `sensor.<host>_items_in_stock` | How many lots the household currently holds. |
| `sensor.<host>_expiring_soon` | How many are due within the horizon you choose (3 days by default). Carries an `items` attribute naming them, for automations. |
| `sensor.<host>_expired` | How many are already past their effective date. |
| `sensor.<host>_next_expiry` | The date of the next thing to eat. |
| `sensor.<host>_shopping_list_items` | How many lines are still to buy. |
| `sensor.<host>_food_spend_<currency>` | What was spent this calendar month, one sensor per currency. |
| `todo.<host>_shopping_list` | The shopping list, addable, tickable and deletable. |

"Effective date" is Chaudron's own: the printed date, shortened when a pot was
opened and its product family carries a "consume within N days" rule. It is what
the application sorts by, so it is what these sensors count.

### What is deliberately absent

* **No recipe suggestions.** The endpoint spends money — a billed inference, or
  the CPU of a colocated Ollama — and Chaudron issues no token scope that reaches
  it. A long-lived credential in a home-automation appliance is the worst place
  to hold that power.
* **No household members, no allergens.** That is health data under GDPR
  article 9, and it leaves Chaudron through a browser session or not at all.
* **No adding stock from Home Assistant.** The write scope exists, but a lot
  needs a product, a quantity, a unit, a location and a date, and there is no
  honest Home Assistant entity for that. Scan it in the app.

---

## Install

### Through HACS

`homeassistant/` **is the repository root HACS expects** — `hacs.json` and
`custom_components/chaudron/` sit inside it. HACS only looks at the root of a
repository, so it cannot see this directory from inside the Chaudron monorepo.
Two ways round that, and the second is the one to prefer:

1. **Custom repository, from a published split.** Publish this directory as a
   repository of its own (`git subtree split --prefix=homeassistant`), then add
   that repository in HACS under *Integrations → ⋮ → Custom repositories*.
2. **Manually**, below, which needs no HACS at all.

### Manually

Copy the integration into your Home Assistant configuration directory and
restart:

```bash
scp -r homeassistant/custom_components/chaudron \
    homeassistant.local:/config/custom_components/
```

Then **Settings → Devices & services → Add integration → Chaudron**.

Minimum Home Assistant version: **2025.8**.

---

## Get a token

In Chaudron: **Settings → Access tokens → New token**.

Tick the scopes you want. Only the first is required:

| Scope | Unlocks |
|---|---|
| `inventory:read` | **Required.** The stock, expiry and location sensors. |
| `shopping:read` | The shopping list, read-only. |
| `shopping:write` | Adding, ticking and deleting items on it. |
| `budget:read` | The monthly spend sensor. |

`inventory:write` is not used by this integration; do not grant it.

A token that is missing an optional scope is a supported configuration, not a
broken one: the integration notices the refusal on its first poll, logs one line
saying so, and simply does not create the entities that would have needed it.

**The value is shown once.** Copy it straight into the Home Assistant form. If
you lose it, revoke it and issue another — that is cheaper than it sounds, and
revoking is the right reflex anyway.

Only a browser session can mint or revoke a token: a token cannot mint another
one, which is what keeps a leaked value from regenerating itself faster than you
can revoke it.

---

## Polling, and the instance's rate limits

The integration uses a single `DataUpdateCoordinator`: **four requests every ten
minutes**, whatever number of entities you display. Nothing polls per-entity.

If your instance answers `429`, the integration reads the `Retry-After` header it
sends and *waits that long* — keeping the last good reading on the cards rather
than spending the whole budget re-earning the same refusal.

Both the interval and the "expiring soon" horizon are configurable under
**Configure** on the integration entry.

---

## Automations

The expiring-soon sensor carries the list of what is expiring, so a nudge at
dinner time is a template and nothing more:

```yaml
automation:
  - alias: "What has to be eaten"
    triggers:
      - trigger: time
        at: "18:00:00"
    conditions:
      - condition: numeric_state
        entity_id: sensor.chaudron_example_org_expiring_soon
        above: 0
    actions:
      - action: notify.mobile_app_phone
        data:
          title: "To eat first"
          message: >-
            {{ state_attr('sensor.chaudron_example_org_expiring_soon', 'items')
               | map(attribute='name') | join(', ') }}
```

The `items` attribute is deliberately **not written to the recorder database**:
it is the household's shopping habits in plain text, and storing it every ten
minutes forever would be a slow leak into a file nobody thinks of as sensitive.
It is available live to templates; it has no history.

---

## Expiry alerts on a phone, without Home Assistant

If what you want is a reminder on a phone rather than a dashboard, Chaudron
publishes the same information as a **CalDAV task feed** that iOS Reminders and
Android task apps subscribe to natively — see
[`docs/calendar-feed.md`](../docs/calendar-feed.md). That feed is a separate,
owner-only credential and is *not* what this integration uses. Home Assistant can
consume it through the built-in CalDAV integration if you prefer.

---

## Privacy

* The token is stored in the config entry, which Home Assistant keeps in
  `.storage`. There is no YAML configuration and there will not be one:
  `configuration.yaml` is a file people paste into forum posts.
* The token is never logged, never put in an exception message, and never
  appears in the config entry's unique identifier — that is a SHA-256 digest.
* Diagnostics downloads carry counts and connection state, and **no product
  names, brands or shopping-list lines**. They are safe to attach to an issue.

---

## Develop

```bash
cd homeassistant
uv venv --python 3.13 && uv pip install -r requirements-dev.txt
PYTHONPATH=. .venv/bin/python -m pytest
uvx ruff check . && uvx ruff format --check .
```

`requirements-dev.txt` pins `pytest-homeassistant-custom-component`, which is
what fixes the Home Assistant release the suite runs against. The integration
itself has **no runtime dependency**: `manifest.json` declares an empty
`requirements`, and the API client is one module over the `aiohttp` Home
Assistant already ships.

`brand/` holds the icon and logo in the sizes the
[home-assistant/brands](https://github.com/home-assistant/brands) repository
asks for. They live here so they are versioned with the integration; they only
take effect once submitted there as a pull request under `custom_integrations/chaudron/`.

# 0007. Per-household key (BYOK) and local inference

## Status

Accepted — 2026-08-03

## Context

The model features (recipe suggestions, receipt extraction) have a usage cost. The question is not only *how much*, but *who pays* and *what risk that creates*.

The default model of a SaaS application — the operator provides model access and bills it back — immediately imposes: a global spend cap to watch, protection against abuse (one household generating thousands of recipes drains the shared budget), a quota system, eventually billing, and responsibility for processing every user's data at a third-party provider.

Chaudron is a solo project, in a phase 1 family stage. None of that work is fundable, and each piece would be a permanent source of incidents.

Beyond that, part of the target audience for a self-hosted application will want to send nothing to an external provider. Local inference is not a degraded fallback for them: it is the reason they install the product.

## Decision

**Each household configures its own model access.** There is no mode in which the application pays for all users. This is a deliberate design decision, not a temporary limitation.

Three modes, stored on the household:

| Mode | What the household provides | Who pays |
|---|---|---|
| `byok` | Its own API key — **Anthropic, OpenAI, Gemini or Mistral AI**, the four first-class providers of v1 (see ADR-0005) | The household, directly to the provider |
| `ollama` | A base URL and a model name, no key | Nobody (local compute) |
| `instance_owner` | Nothing: the key read from the instance environment | The instance owner |

**The instance owner's key is strictly personal.** The `instance_owner` mode can only be used by the household explicitly designated as the instance owner (dedicated environment variable). It is **locked by default**: any other household that tries to select it is refused. A household with no valid configuration simply has no access to the model features — the rest of Chaudron (stock, shopping list, EAN scanning) stays whole.

**Ollama topology — v1: co-located case only.** Two topologies exist, and neither reduces to the other:

- *Ollama co-located* with the backend (same host, same Podman network): server → server call, trivial. **This is the only case supported in v1.**
- *Ollama on the user's machine or LAN*: the backend cannot reach it, it sits behind a NAT. The only component able to reach it is the user's **browser**.

Supporting the second case requires an inversion: the backend would return a *prompt bundle* (rendered prompt, model name, expected output schema), the frontend would send it to the local Ollama, then post the raw response back to the backend for validation and writing to the database. The cost is real: the prompt becomes exposable client-side (so no longer secret), a second execution path appears for every model feature, the backend has to validate a response whose provenance it does not control and treat it as hostile input, and the user has to configure `OLLAMA_ORIGINS` on their instance to allow CORS — a step support will be explaining forever.

**That cost is not paid in v1.** The interface documents that `ollama` mode requires an instance reachable from the server. The browser route is the identified extension path, not work in progress.

**Security of household-supplied keys.**

- **Encrypted at rest.** The encryption key comes from the environment (Podman secret), never from the database: a leaked dump is not enough to decrypt.
- **Write-only through the API.** No endpoint returns a key. A read returns only the provider, a timestamp and the **last four characters** to allow identification.
- **Never logged.** An explicit filter in the structured logging configuration; configuration objects override their representation to mask the value; exception traces returned to the client are rewritten — an SDK that included the key in an error message must not propagate it.
- **Rotation.** Replacing a key is an idempotent write on the household; the old value is overwritten, not versioned. The procedure is documented in the `README` and repeated in the interface.

**SSRF.** In the co-located case, the Ollama URL is **supplied by the user and called by the server**: that is an SSRF primitive. The usual filtering (reject private ranges) is useless here, since the legitimate address of a co-located Ollama is precisely a private one. The validation chosen is therefore an **explicit host allowlist**, defined by an instance environment variable (typically the Podman service name of the Ollama container). On top of that: scheme restricted to `http`/`https`, DNS resolution performed at validation time **and** before the call to prevent DNS rebinding, redirects disabled, timeout and response size bounded. A URL outside the allowlist is rejected at registration, with an explicit message.

**Capability detection at configuration time.** Registering an `ollama` configuration triggers a call to the instance to establish the declared model's capabilities (vision, structured output, context size), which are persisted with the household configuration — see ADR-0005, where this dynamic detection is described as differing in kind from `AnthropicProvider` and `GeminiProvider`. That call goes through the same SSRF validation as inference calls: an unreachable instance, or one outside the allowlist, fails registration with the reason displayed, rather than registering a configuration whose abilities are unknown.

## Consequences

### Positive

- **No global spend cap to manage**: there is no shared budget to protect, hence no abuse risk, no quotas, no billing to build.
- The GDPR surface shrinks sharply: Chaudron does not become responsible for sending all its users' data to a third-party provider; each household contracts directly, or sends nothing at all.
- **The household chooses its jurisdiction.** Two configurations keep food consumption data under European jurisdiction: `byok` with Mistral AI (EU-hosted) and `ollama` (nothing leaves the machine). That criterion is shown in the provider selection interface (see ADR-0005), not buried in the documentation.
- The `ollama` mode makes a fully self-contained deployment possible, with no outbound calls at all.
- Cost control stays in the hands of whoever bears it: spend cap at the provider, choice of model, choice of when.
- The locked `instance_owner` mode avoids the classic scenario where the owner discovers their bill after sharing their instance.

### Negative

- **A high barrier to entry.** A new user has to create an account with a provider, generate a key and paste it, or install Ollama. Most people will give up before seeing their first suggested recipe. That is the direct price of the decision, and it is a heavy one.
- **The subscription / API key confusion is predictable and expensive.** A user with ChatGPT Plus, Claude Pro/Max or Gemini Advanced will legitimately believe they have access. They have none: these are distinct products, billed separately. The interface must clear up the ambiguity at the moment the key is entered (see ADR-0005); otherwise this will be the leading source of support tickets.
- **Four providers to choose from is also a decision burden.** A non-technical user has no criterion for picking between Anthropic, OpenAI, Gemini and Mistral. The interface must recommend a default and expose the others only in second rank, otherwise the choice itself becomes a drop-off point.
- **The product becomes uneven.** Receipt extraction on damaged thermal paper works well with a recent proprietary model, poorly with a small open model. Two users will judge the same product very differently (see ADR-0005, capability-based degradation).
- **Storing third-party API keys is a responsibility.** Even encrypted, they are secrets with direct monetary value. Encryption at rest does not protect against an application compromise, since the application has to decrypt in order to call.
- **The SSRF allowlist is operational friction**: adding an Ollama host requires editing the instance environment and restarting it. That is deliberate — dynamic, permissive validation would reopen exactly the hole the allowlist closes.
- **The v1 topology limit will be perceived as a defect.** A user with Ollama on their laptop will not be able to use it, and the explanation (NAT, CORS) is a hard sell.
- **Support gets harder.** An incident can come from their key, their quota, their Ollama instance, their `OLLAMA_ORIGINS`, or the code. Surfacing the provider and the failure mode in error messages is a requirement, not a nicety.

## Rejected alternatives

- **The application pays for every household** — by far the best onboarding experience. Rejected: it imposes a spend cap, quotas, anti-abuse, billing and extended GDPR responsibility, none of which is fundable on a solo project. It is a real sacrifice in adoption, and a deliberate one.
- **Free credits then BYOK** (free up to N requests) — would soften the barrier to entry. Rejected: it reintroduces the shared budget, anti-abuse and consumption tracking in full, for a temporary easing.
- **Ollama exclusively, no external provider** — removes the key question entirely. Rejected: open models' receipt extraction quality is not good enough for the main use case, and installing Ollama is a barrier at least as high as an API key.
- **Keys in clear text in the database** — simpler to implement. Rejected without discussion: a database dump would then be enough to steal billable secrets belonging to third parties.
- **The browser route for Ollama from v1** — would cover every topology. Rejected for v1: it doubles the execution path of every model feature before the product has a single external user.

## Revisiting

- Implement the browser route for Ollama if self-hosting users actually ask to use a workstation or LAN Ollama. The cost is known and documented above; only the demand is missing.
- Reassess onboarding if the drop-off rate at the provider configuration step is measurable and high: a demo mode with a very tight quota, backed by the instance owner's key and explicitly opted into by them, would then be the minimal compromise to examine.
- Add a sixth provider only on demonstrated user demand: ADR-0005 allows it without a rewrite, but every adapter kept carries a permanent maintenance cost. Symmetrically, remove an adapter that no household uses.

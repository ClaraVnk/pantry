# 0005. Domain abstraction for model providers

## Status

Accepted — 2026-08-03

## Context

Two Chaudron features rest on a language model: generating recipe suggestions from available stock, and structured extraction of the lines of a photographed till receipt (multimodal model).

The most direct solution would be to call a provider's SDK from the HTTP handlers. Three things rule that out:

1. **Each household configures its own model access** (see ADR-0007). There is no single provider decided by the application, but as many configurations as there are households, resolved at runtime. The provider is data, not a deployment constant.
2. **Five adapters are targeted from v1**, four first-class ones and one reference degraded case:

   | Adapter | Provider | Status |
   |---|---|---|
   | `AnthropicProvider` | Anthropic (Claude) | fully capable — `claude-opus-5` is the default model in the documentation and in `instance_owner` mode |
   | `OpenAIProvider` | OpenAI (GPT) | fully capable |
   | `GeminiProvider` | Google (Gemini) | fully capable |
   | `MistralProvider` | Mistral AI (Mistral, Pixtral) | fully capable, **EU-hosted** |
   | `OllamaProvider` | local | variable capabilities, detected at configuration time |

3. **The domain does not need to know who answers.** "Suggest recipes from this stock" and "extract the lines of this receipt" are business operations; transport, message format and token handling are infrastructure.

This ADR introduces an abstraction in a project that otherwise avoids premature abstraction. The rule of three does not apply here: five implementers are required from v1, and the choice between them is made per household on every request. This is not anticipating a possible change, it is modelling variability that is already present.

## Decision

**Domain ports.** Two interfaces are defined in the domain layer, with no dependency on any SDK:

- `RecipeSuggester`: from an inventory, produces recipe suggestions.
- `ReceiptExtractor`: from an image, produces structured purchase lines.

They expose no "prompt", no "message" and no "token": only domain objects (`InventoryItem`, `RecipeSuggestion`, `ReceiptLine`). Provider errors are translated into domain exceptions (`ProviderUnavailable`, `ProviderQuotaExceeded`, `ProviderResponseInvalid`); no SDK exception crosses the boundary.

**The interface is designed for the most capable provider, not the weakest.** It exposes the full surface — strict structured output, vision, prompt caching hints, context size — and each adapter declares what it can do with it. Designing to the lowest common denominator would align the product with the least capable provider while four adapters out of five are fully capable: that is precisely the trap this decision avoids.

**Capabilities belong to the (provider, model) pair, not to the provider.** A first-class provider may serve a model without vision, or with a short context, if the user picks it to cut costs. The degradation taxonomy below therefore applies to every configuration, not just Ollama — which is what keeps it relevant with four fully capable providers.

**Degradation taxonomy.** For each missing capability, the adapter falls into exactly one of these three cases. The choice is made per (capability × feature) pair and constitutes a documented decision, never an accidental consequence of the code:

1. **Emulation with documented loss** — the capability is approximated by other means. Example: no native structured output → the expected JSON format is requested in the prompt, the response is validated server-side against the schema, with a bounded retry policy. The feature stays available, the failure rate is higher, the user is told.
2. **Visible functional degradation** — the feature is still offered in a reduced form, flagged as such in the interface (the "degraded mode"). Example: context too short for the full inventory → suggestions computed on a subset of items, with an explicit mention of the scope retained.
3. **Explicit unavailability** — the feature is disabled, with the reason displayed and the steps to fix it. Example: no vision → receipt import is disabled, never a raw error and never invented JSON from a model that never saw the image.

**Degraded-mode indicator.** As soon as a household's configuration lacks full capability, the interface shows a **persistent** indicator detailing what is reduced or unavailable and why. The user must never discover the limit at the moment of failure: they must know about it before trying. This also protects the product's reputation — a poor extraction attributed to the small local model the user loaded themselves is not the same thing as a poor extraction attributed to Chaudron.

**Capability model: two kinds of declaration, explicit in the type.** The asymmetry between adapters is structural and part of the model, not a special case handled ad hoc:

- **Static capabilities** (`AnthropicProvider`, `OpenAIProvider`, `GeminiProvider`, `MistralProvider`) — known in advance, derived from the (provider, model) pair by a table embedded in the adapter. No network call is needed to know them.
- **Probed capabilities** (`OllamaProvider`) — they depend on the model loaded in the user's instance, which can change without Chaudron being told. They are established at configuration time by querying the instance, persisted with the household configuration, timestamped, and refreshed on explicit request.

The domain consumes both through the same `ProviderCapabilities` type, but the provenance (`static` / `probed`, with the probe date) is carried by the value: the interface can therefore flag that a probed capability is stale and offer a refresh, which makes no sense for a static one.

**A naming trap to defuse.** Three providers sell a consumer subscription whose name will be confused with API access. This is the first predictable source of support tickets, and the configuration interface must clear up the ambiguity **at the moment the user pastes their key**, with a link to the right console:

| The user thinks… | What they actually need… |
|---|---|
| ChatGPT Plus | an OpenAI API key (usage billing, developer console) |
| Claude Pro / Max | an Anthropic API key (developer console) |
| Gemini Advanced | a Google AI API key |

A consumer subscription grants **no** programmatic access whatsoever: these are two distinct products, with two distinct bills. The error message for an invalid key must point explicitly at this distinction rather than at the SDK's raw message.

**Data sovereignty: two options, to be surfaced in the provider choice.** Only two configurations guarantee that a household's food consumption data never leaves European jurisdiction: **Mistral AI** (EU-hosted) and **Ollama** (nothing leaves the machine). This is a genuine selection criterion for a European user, and it must be shown as a property of the provider in the selection interface, on a par with its capabilities — not buried in documentation.

**Adapters.** The implementations live in the infrastructure layer. A factory builds the right adapter from the current household's configuration; handlers receive the port by injection and never know the concrete implementation.

**Adapter conformance tests.** A single contract suite, parameterised over all adapters, defines what an adapter must honour: conformance to the port signatures, translation of each failure mode into the corresponding domain exception, a well-formed capability declaration, and — for each capability declared absent — conformance to the taxonomy case chosen for that pair. It runs against replayed recordings in CI, and in live mode on demand. **Adding a provider means writing an adapter and making that suite pass**: bounded work that cannot regress the other four. Without that safeguard, five adapters would be reckless for a solo project.

**Consumer subscriptions are not a runtime option.** Claude Pro/Max, ChatGPT Plus and Gemini Advanced are **personal** use licences, with no stable or contractual API. An application serving users cannot lean on them: out-of-licence use, undocumented surface liable to break without notice, no availability commitment. Only usage-billed API access or a self-hosted model are legitimate runtime providers.

Those subscriptions are, on the other hand, perfectly legitimate for **developing** Chaudron — writing code, designing prompts, exploring output formats. The distinction is about the cost line, not the tool: a subscription can build the product, it cannot serve it.

## Consequences

### Positive

- The product fully exploits capable providers instead of aligning with the weakest: strict structured output and prompt caching are used where they exist.
- The household chooses on its own criteria — cost, capabilities, jurisdiction — without Chaudron imposing a provider.
- Business logic is testable without a network: an in-memory `FakeRecipeSuggester` is enough, and most tests never need a real provider.
- The degradation taxonomy makes behaviour under a missing capability predictable and reviewable: for each pair, we know which case applies and why.
- The conformance tests bound the cost of adding a provider and turn a potential regression into a CI failure.
- Provider errors are translated once, in the right place, instead of leaking into `except AnthropicError` scattered across the routes.

### Negative

- **The test and maintenance matrix is large, and that is the real price of the decision.** Five adapters × each model feature × each capability consumed: every new feature multiplies the cases to decide, implement, surface and test. For a solo developer, that is a structural load, not a one-off cost. The conformance tests make it bearable — they do not remove it.
- **Five SDKs to track.** Each has its own release cadence, its own API breakages and its own deprecated models. A dependency update can break one adapter without touching the others, and it has to be caught before the user does.
- **Static capabilities are a hand-maintained table.** Every new model released by one of the four providers needs an entry; a stale table either declares a capability absent or promises one that does not exist.
- **Ollama's probed declaration is a bug source of its own.** It depends on a third-party instance being reachable at configuration time; the user can switch models afterwards without Chaudron knowing, leaving stale capabilities. The unreachable instance, the stale data and a refresh path all have to be handled — three error paths that exist for no other adapter.
- **The emulation path has its own failure modes**: variable latency and residual failures that the interface must present honestly rather than hide.
- **The degraded-mode indicator is recurring UI work**: every missing capability has to be explained in plain language, with an actionable remedy. A vague indicator is worse than none.
- **User support is harder.** "It doesn't work" can come from their Ollama instance, their quota, a subscription key pasted instead of an API key, the model they picked, or the code. Diagnosis requires surfacing the provider, the detected capabilities and the failure mode in the errors shown.

## Rejected alternatives

- **Direct SDK calls in the handlers** — the least code today. Rejected: five providers selected per household at runtime would translate into conditional branching in every handler.
- **A lowest-common-denominator interface** — one surface, the one every provider can honour, hence no degradation matrix to maintain. Explicitly rejected: it aligns the product with the weakest provider and deprives most households of the capabilities they pay for.
- **A single first-class provider in v1, the others later** — would divide the matrix by four immediately. Rejected: provider choice is an adoption criterion (cost, jurisdiction, an account already held), and adding an adapter late with no pre-existing conformance suite is far riskier than building it up front.
- **A multi-provider gateway (LiteLLM, OpenRouter)** — normalises providers without writing adapters. Rejected: a software gateway imposes its own data model and tracks provider-specific capabilities poorly — exactly what this decision seeks to exploit; a hosted gateway adds an intermediary that sees every request, which destroys the sovereignty argument and contradicts ADR-0007's per-household model.
- **A generic `LLMClient` abstraction (`complete(prompt) -> str`)** — one interface for everything. Rejected: it puts prompt construction and response parsing in the domain, and allows no capability declaration.
- **A consumer subscription driven through browser automation** — would remove the usage cost. Rejected: out-of-licence use, undocumented surface, no availability guarantee.

## Revisiting

- If the capability × feature matrix becomes unmanageable by hand, formalise the pairs in a single decision table, checked by the conformance tests, rather than scattered between the adapters and the interface.
- Remove an adapter if measurements show that no household uses it: every adapter kept carries a permanent maintenance cost, and five is a ceiling, not a starting point.
- Reassess Ollama capability detection if the instance exposes a reliable way to signal a model change.
- Reassess port granularity if a third model use case appears (product label normalisation, for instance).

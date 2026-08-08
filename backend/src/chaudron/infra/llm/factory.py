"""Build the right adapter for one household's configuration.

The handlers never see this file: they receive a
:class:`~chaudron.domain.llm_ports.RecipeGenerator` or
:class:`~chaudron.domain.llm_ports.ReceiptParser` by injection and cannot tell which
of the five providers answered. Selection happens here, at request time, because the
provider is *data on the household* rather than a deployment constant (ADR-0005).

The one rule this file exists to enforce, from ADR-0007: ``instance_owner`` spends
the operator's own money and is therefore locked to the single household named by
the instance environment. Every other household asking for it is refused. Unset
means nobody -- the failure mode of getting this wrong is a stranger's invoice, so
the default has to be closed.

``byok`` -- the mode the whole product is built around -- gets its key here too, and
deliberately as late as possible: the household configuration carries the *sealed*
credential, and the plaintext exists only inside :meth:`LlmProviderFactory.build_transport`
for as long as it takes to hand it to an adapter. Nothing upstream of this file, and
no value object that outlives a request, ever holds a decrypted key.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Final

import httpx

from chaudron.domain.llm_ports import (
    ProviderCapabilities,
    ProviderNotConfigured,
    ProviderNotPermitted,
    ReceiptParser,
    RecipeGenerator,
)
from chaudron.domain.models import LlmProviderMode
from chaudron.infra.crypto import CredentialCipher, SealedCredential
from chaudron.infra.llm.anthropic_provider import PROVIDER_CODE as ANTHROPIC_CODE
from chaudron.infra.llm.anthropic_provider import (
    AnthropicTransport,
    anthropic_capabilities,
    build_client,
)
from chaudron.infra.llm.base import (
    ModelReceiptParser,
    ModelRecipeGenerator,
    ProviderTransport,
)
from chaudron.infra.llm.gemini_provider import PROVIDER_CODE as GEMINI_CODE
from chaudron.infra.llm.gemini_provider import (
    GeminiTransport,
    build_gemini_client,
    gemini_capabilities,
)
from chaudron.infra.llm.http import Resolver
from chaudron.infra.llm.ollama_provider import PROVIDER_CODE as OLLAMA_CODE
from chaudron.infra.llm.ollama_provider import (
    OllamaTransport,
    build_guarded_client,
    build_pinned_client,
)
from chaudron.infra.llm.openai_compatible import (
    MISTRAL_PROVIDER_CODE,
    OPENAI_PROVIDER_CODE,
    ChatCompletionsTransport,
    build_chat_client,
    mistral_capabilities,
    openai_capabilities,
)
from chaudron.infra.llm.settings import (
    INSTANCE_OWNER_ENV_VAR,
    INSTANCE_OWNER_KEY_ENV_VAR,
    LlmSettings,
)

__all__ = [
    "BYOK_PROVIDERS",
    "HouseholdProviderConfig",
    "LlmProviderFactory",
]

#: The four first-rank providers a household may bring its own key for. Ollama is
#: not among them: it has no key, and its own mode carries the URL instead.
BYOK_PROVIDERS: Final = frozenset(
    {ANTHROPIC_CODE, OPENAI_PROVIDER_CODE, GEMINI_CODE, MISTRAL_PROVIDER_CODE}
)


@dataclass(frozen=True, slots=True)
class HouseholdProviderConfig:
    """One household's provider settings, as the factory needs them.

    A plain value object rather than the ORM row: the factory must be constructible
    (and testable) without a database, and the decrypted key must be handled as a
    parameter with a visible lifetime rather than as a lazily-loaded column.
    """

    household_id: uuid.UUID
    mode: LlmProviderMode
    provider_code: str
    model: str
    base_url: str | None = None
    #: An already-decrypted key. Only the tests and the conformance suite populate
    #: it; the production ``byok`` path carries :attr:`sealed_api_key` instead, so
    #: that no plaintext is ever built into a value object. ``repr=False``: a
    #: settings dump must not print a key.
    api_key: str | None = field(default=None, repr=False)
    #: The ``byok`` credential exactly as the database holds it. Decrypted by the
    #: factory, at the last possible moment, and never stored back here.
    sealed_api_key: SealedCredential | None = None
    #: The persisted probe for ``ollama``. Ignored by the static providers, which
    #: derive their capabilities from their own table.
    probed_capabilities: ProviderCapabilities | None = None


class LlmProviderFactory:
    """Resolves a household configuration into a port implementation.

    ``transport`` and ``pinned_addresses`` exist so the conformance suite and the
    unit tests can substitute the wire without substituting the adapter: the logic
    under test is the real one, only the socket is a double.

    ``resolver`` is not a test seam. It is the production DNS lookup
    (``system_resolver``, wired in ``services.providers.provider_ports_builder``),
    and supplying it is what arms the rebinding check on the Ollama path: the
    factory takes the first DNS answer here and hands it to the client as the pin
    that every later call is compared against. Passing ``pinned_addresses``
    explicitly overrides that, which is what the doubles do.

    ``cipher`` is what makes ``byok`` work. It is optional so that a test exercising
    the four other modes needs no key material; a household whose credential is
    sealed and whose factory has no cipher is refused rather than silently downgraded
    -- an instance that cannot decrypt must say so.
    """

    def __init__(
        self,
        settings: LlmSettings,
        *,
        cipher: CredentialCipher | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
        pinned_addresses: frozenset[str] | None = None,
    ) -> None:
        self._settings = settings
        self._cipher = cipher
        self._transport = transport
        self._resolver = resolver
        self._pinned = pinned_addresses

    # -- capabilities ----------------------------------------------------- #

    def capabilities_for(self, config: HouseholdProviderConfig) -> ProviderCapabilities:
        """What this configuration can do, without calling anyone.

        For the four static providers this is a table lookup. For Ollama it is the
        stored probe -- and its absence is an error, not a default: a configuration
        whose abilities nobody established cannot be used to promise a feature.
        """
        if config.provider_code == OLLAMA_CODE:
            if config.probed_capabilities is None:
                raise ProviderNotConfigured(
                    "this Ollama configuration has never been probed; save it again "
                    "to detect what the model supports"
                )
            return config.probed_capabilities
        if config.provider_code == ANTHROPIC_CODE:
            return anthropic_capabilities(config.model)
        if config.provider_code == OPENAI_PROVIDER_CODE:
            return openai_capabilities(config.model)
        if config.provider_code == GEMINI_CODE:
            return gemini_capabilities(config.model)
        if config.provider_code == MISTRAL_PROVIDER_CODE:
            return mistral_capabilities(config.model)
        raise ProviderNotConfigured(f"unknown provider {config.provider_code!r}")

    # -- ports ------------------------------------------------------------ #

    async def recipe_generator(self, config: HouseholdProviderConfig) -> RecipeGenerator:
        return ModelRecipeGenerator(await self.build_transport(config))

    async def receipt_parser(self, config: HouseholdProviderConfig) -> ReceiptParser:
        return ModelReceiptParser(await self.build_transport(config))

    # -- wiring ----------------------------------------------------------- #

    async def build_transport(self, config: HouseholdProviderConfig) -> ProviderTransport:
        """Build the adapter for this configuration.

        Asynchronous for one reason, and only on one branch: the Ollama path has to
        resolve the household's hostname *before* the client exists, so that the
        first DNS answer can become the pin the rebinding check compares against.
        The four hosted providers do no I/O here and are async purely to keep one
        signature; splitting them would put the decision of which method to call
        back into the caller, which is the knowledge this factory exists to hold.
        """
        # Permission and coherence first, capabilities second. A household that is
        # not allowed to use this mode must be told *that*, not handed an error
        # about a model table it was never going to reach.
        if config.mode is LlmProviderMode.OLLAMA:
            return await self._build_ollama(config)
        api_key = self._resolve_api_key(config)
        capabilities = self.capabilities_for(config)
        if config.provider_code == ANTHROPIC_CODE:
            return AnthropicTransport(
                build_client(api_key, timeout_seconds=self._settings.timeout_seconds),
                capabilities,
                api_key=api_key,
            )
        if config.provider_code in (OPENAI_PROVIDER_CODE, MISTRAL_PROVIDER_CODE):
            return ChatCompletionsTransport(
                build_chat_client(
                    config.provider_code, api_key, self._settings, transport=self._transport
                ),
                capabilities,
                api_key=api_key,
            )
        if config.provider_code == GEMINI_CODE:
            return GeminiTransport(
                build_gemini_client(api_key, self._settings, transport=self._transport),
                capabilities,
                api_key=api_key,
            )
        raise ProviderNotConfigured(f"unknown provider {config.provider_code!r}")

    async def _build_ollama(self, config: HouseholdProviderConfig) -> ProviderTransport:
        """The one branch where the URL is hostile input, and the only one that pins.

        The first-party providers are deliberately left unpinned: their hostnames
        are ours, they are not user-supplied, and they are not an SSRF primitive.
        Pinning them would buy nothing and would make a routine DNS change at a
        vendor look like an attack.
        """
        if config.provider_code != OLLAMA_CODE:
            raise ProviderNotConfigured(
                f"mode 'ollama' cannot serve provider {config.provider_code!r}"
            )
        if not config.base_url:
            raise ProviderNotConfigured("mode 'ollama' requires a base URL")
        capabilities = self.capabilities_for(config)
        if self._pinned is not None:
            # An explicit pin short-circuits the lookup: this is the doubles' path,
            # where there is no DNS to consult and the pin is the fixture.
            client = build_guarded_client(
                config.base_url,
                self._settings,
                transport=self._transport,
                resolver=self._resolver,
                pinned_addresses=self._pinned,
            )
        else:
            client = await build_pinned_client(
                config.base_url,
                self._settings,
                transport=self._transport,
                resolver=self._resolver,
            )
        return OllamaTransport(client, capabilities, max_num_ctx=self._settings.ollama_max_num_ctx)

    def _resolve_api_key(self, config: HouseholdProviderConfig) -> str:
        """Where the credential comes from, and who is allowed to use which."""
        if config.mode is LlmProviderMode.INSTANCE_OWNER:
            return self._instance_owner_key(config)
        if config.mode is LlmProviderMode.BYOK:
            if config.provider_code not in BYOK_PROVIDERS:
                allowed = ", ".join(sorted(BYOK_PROVIDERS))
                raise ProviderNotConfigured(
                    f"provider {config.provider_code!r} cannot be used with a "
                    f"household key; supported: {allowed}"
                )
            return self._household_key(config)
        raise ProviderNotConfigured(f"mode {config.mode!r} does not use an API key")

    def _household_key(self, config: HouseholdProviderConfig) -> str:
        """The household's own key, decrypted here and nowhere earlier.

        A :class:`~chaudron.domain.llm_ports.CredentialDecryptionError` from the
        cipher is allowed to propagate unchanged: it already names the environment
        variable and the remedy, and re-wrapping it would only blur a rotation into a
        generic "not configured".
        """
        if config.api_key:
            return config.api_key
        if config.sealed_api_key is not None:
            if self._cipher is None:
                raise ProviderNotConfigured(
                    "this instance cannot decrypt stored household keys; it was "
                    "started without a credential cipher"
                )
            return self._cipher.decrypt(config.sealed_api_key)
        raise ProviderNotConfigured(
            f"no API key is stored for this {config.provider_code} configuration"
        )

    def _instance_owner_key(self, config: HouseholdProviderConfig) -> str:
        """The locked door of ADR-0007.

        Refusing by default -- and refusing loudly, naming the environment variable
        -- is deliberate. The classic failure this prevents is an operator sharing
        their instance and discovering the arrangement on their next invoice.
        """
        owner = self._settings.instance_owner_household_id
        if owner is None:
            raise ProviderNotPermitted(
                "mode 'instance_owner' is disabled on this instance; it becomes "
                f"available only when {INSTANCE_OWNER_ENV_VAR} names a household"
            )
        if config.household_id != owner:
            raise ProviderNotPermitted(
                "mode 'instance_owner' is reserved for the household that owns this "
                "instance; configure your own API key, or use a local Ollama"
            )
        key = self._settings.instance_owner_api_key
        if not key:
            raise ProviderNotConfigured(
                "mode 'instance_owner' is selected but the instance holds no key; "
                f"set {INSTANCE_OWNER_KEY_ENV_VAR} (podman secret) and restart"
            )
        return key

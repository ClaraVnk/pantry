"""Which model provider a household has, and what it can honestly promise.

This is the read side of ADR-0005: it answers "what will happen if the user presses
the button?" *before* they press it. Two callers, one resolution path:

* ``GET /v1/providers/capabilities`` renders :class:`ProviderView`, which the PWA
  shows permanently rather than at the moment of failure;
* the recipe service asks for :class:`ActiveProvider`, and gets a refusal it can
  turn into a 409 when the household has nothing usable;
* the receipt import asks for :class:`ActiveReceiptParser`, and additionally
  refuses when the configured model has no vision -- the ``unavailable`` case of
  the ADR-0005 taxonomy, because a model that has not seen the image would
  happily produce a plausible receipt.

All three go through :meth:`ProviderService._load`, so the answers cannot disagree
-- an interface that says "ready" for a configuration the next call refuses is
worse than one that says nothing.

The refusal texts are in **French and in plain language** on purpose: they are user
interface, not diagnostics. Everything else in this file -- identifiers, comments,
exception messages -- stays in English, as does every log line.

One limit is deliberate and documented rather than hidden: the status is *static*.
It describes the stored configuration and never calls the provider, so it cannot know
that a key was revoked this morning. Probing on a status endpoint would spend the
household's money to draw a banner.

Mode ``byok`` -- the mode ADR-0007 is built around -- is fully served here. The
ciphertext is a deferred column, so reading it is an explicit gesture
(:meth:`ProviderService._sealed_credential`) rather than something a stray
``select`` can do by accident, and the plaintext never leaves the factory. ``status``
does decrypt, and throws the result away: ``configured`` means *usable*, and an
instance whose ``CHAUDRON_CREDENTIAL_ENCRYPTION_KEY`` was rotated must say so on the
banner instead of promising a call that would fail on the first click.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.config import Settings
from chaudron.domain.llm_ports import (
    DEGRADATION_POLICY,
    CapabilitySource,
    CredentialDecryptionError,
    DegradationNotice,
    DegradationStrategy,
    ProviderCapabilities,
    ProviderCapabilityUnavailable,
    ProviderNotConfigured,
    ReceiptParser,
    RecipeGenerator,
)
from chaudron.domain.models import (
    LlmConfigStatus,
    LlmProviderConfig,
    LlmProviderMode,
    LlmPurpose,
    LlmPurposeBinding,
)
from chaudron.infra.crypto import MIN_API_KEY_LENGTH, CredentialCipher, SealedCredential
from chaudron.infra.llm.factory import HouseholdProviderConfig, LlmProviderFactory
from chaudron.infra.llm.http import Resolver, system_resolver
from chaudron.infra.llm.settings import LlmSettings

logger = logging.getLogger(__name__)

#: ``llm_provider.code`` of the self-hosted provider, whose timeout budget is the
#: generous one: a local model on a small machine is slow, not broken.
OLLAMA_PROVIDER_CODE: Final = "ollama"

#: Which instance-owned key pays for which provider. The keys are the reference
#: codes seeded by migration ``0002``; a provider absent from this map simply has no
#: instance key, which the factory reports as "not configured".
_INSTANCE_OWNER_KEYS: Final[dict[str, Callable[[Settings], SecretStr | None]]] = {
    "anthropic": lambda settings: settings.anthropic_api_key,
    "openai": lambda settings: settings.openai_api_key,
    "gemini": lambda settings: settings.gemini_api_key,
    "mistral": lambda settings: settings.mistral_api_key,
}


class ProviderPorts(Protocol):
    """The slice of :class:`LlmProviderFactory` this service uses.

    Structural, so a test can substitute a factory wired to a double transport --
    the real adapter, a fake socket -- without the service knowing and without
    spending anything.

    ``recipe_generator`` and ``receipt_parser`` are coroutines because building the
    Ollama adapter resolves the household's hostname first, and that resolution *is*
    the anti-rebinding pin (``infra/llm/factory.py``). ``capabilities_for`` stays
    synchronous: it never calls anyone, which is what lets the capabilities endpoint
    answer without spending the household's money.
    """

    def capabilities_for(self, config: HouseholdProviderConfig) -> ProviderCapabilities: ...

    async def recipe_generator(self, config: HouseholdProviderConfig) -> RecipeGenerator: ...

    async def receipt_parser(self, config: HouseholdProviderConfig) -> ReceiptParser: ...


#: Built per request from the household's provider code: the instance key that
#: ``instance_owner`` may spend is per vendor, and only one of them applies.
type ProviderPortsBuilder = Callable[[str], ProviderPorts]


# --------------------------------------------------------------------------- #
# What the banner says
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Refusal:
    """A configuration that exists but cannot be used, and why -- in French.

    ``code`` is the English token that goes in the log and in the domain error; the
    two other fields are what the user reads.
    """

    code: str
    reason: str
    remedy: str

    def notice(self) -> DegradationNotice:
        return DegradationNotice(
            capability="configuration",
            strategy=DegradationStrategy.UNAVAILABLE,
            reason=self.reason,
            remedy=self.remedy,
        )


_AMBIGUOUS: Final = _Refusal(
    code="no_binding",
    reason=(
        "Plusieurs configurations de fournisseur existent pour ce foyer et aucune "
        "n'est affectée à la génération de recettes."
    ),
    remedy="Indiquez laquelle doit générer les recettes.",
)

_AMBIGUOUS_RECEIPTS: Final = _Refusal(
    code="no_binding",
    reason=(
        "Plusieurs configurations de fournisseur existent pour ce foyer et aucune "
        "n'est affectée à la lecture des tickets de caisse."
    ),
    remedy="Indiquez laquelle doit lire les tickets.",
)

#: What the receipt import says when nothing is configured at all. Its own
#: sentence rather than the recipe one, because the two features fail for the same
#: reason and are fixed on the same screen, but a user who has never asked for a
#: recipe should not be told about recipes.
NO_PROVIDER_FOR_RECEIPTS: Final = _Refusal(
    code="not_configured",
    reason=(
        "Aucun fournisseur de modèle n'est configuré pour ce foyer : la photo d'un "
        "ticket ne peut donc pas être lue."
    ),
    remedy=(
        "Enregistrez un fournisseur multimodal dans la configuration du foyer, ou "
        "importez le PDF de votre commande drive, qui se lit sans modèle."
    ),
)

_DISABLED: Final = _Refusal(
    code="config_disabled",
    reason="La configuration du fournisseur de ce foyer est désactivée.",
    remedy="Réactivez-la, ou enregistrez-en une autre.",
)

_INVALID_CREDENTIALS: Final = _Refusal(
    code="invalid_credentials",
    reason="Le fournisseur a refusé la clé enregistrée pour ce foyer.",
    remedy="Remplacez la clé dans la configuration du foyer.",
)

_KEY_UNDECRYPTABLE: Final = _Refusal(
    code="credential_undecryptable",
    reason=(
        "La clé enregistrée pour ce foyer ne peut pas être déchiffrée : la clé de "
        "chiffrement de cette instance n'est plus celle qui l'a enregistrée."
    ),
    remedy="Saisissez à nouveau votre clé pour la réenregistrer.",
)

_KEY_MISSING: Final = _Refusal(
    code="credential_missing",
    reason="Aucune clé n'est enregistrée pour cette configuration.",
    remedy="Saisissez la clé d'API de votre fournisseur.",
)

#: No agreement on record for sending this household's data to a third party
#: (RGPD art. 6(1)(a); art. 9(2)(a) for the health signals a recipe prompt carries).
#: Raised for every mode *except* ``ollama``, which transmits to nobody -- see the
#: docstring of :meth:`ProviderService._consent_refusal`.
_CONSENT_MISSING: Final = _Refusal(
    code="consent_missing",
    reason=(
        "Ce foyer n'a pas donné son accord pour que ses données soient envoyées à ce "
        "fournisseur de modèle, qui est un tiers."
    ),
    remedy=(
        "Donnez votre accord dans la configuration du foyer, ou choisissez le mode "
        "Ollama, qui fait tourner le modèle sur votre machine et n'envoie rien."
    ),
)

#: The agreement existed and the household took it back. A separate sentence from
#: :data:`_CONSENT_MISSING` because the two are different situations to be in: one
#: is a step never taken, the other a decision made on purpose, and telling somebody
#: who has just withdrawn their consent that they never gave any reads as the
#: application having lost it.
_CONSENT_REVOKED: Final = _Refusal(
    code="consent_revoked",
    reason=("Ce foyer a retiré son accord pour l'envoi de ses données à ce fournisseur de modèle."),
    remedy=(
        "Redonnez votre accord dans la configuration du foyer si vous souhaitez "
        "réutiliser ce fournisseur."
    ),
)

_UNPROBED: Final = _Refusal(
    code="ollama_unprobed",
    reason=(
        "Les capacités de ce serveur Ollama n'ont jamais été détectées : on ignore "
        "ce que le modèle installé sait faire."
    ),
    remedy="Enregistrez la configuration à nouveau pour interroger le serveur.",
)

_UNKNOWN_MODEL: Final = _Refusal(
    code="unknown_model",
    reason=(
        "Cette instance ne connaît pas le modèle configuré : elle ne peut donc pas "
        "dire ce qu'il sait faire."
    ),
    remedy="Choisissez un modèle proposé par la liste, puis enregistrez à nouveau.",
)

#: One sentence per missing capability, in the vocabulary of someone cooking
#: dinner. The strategy that goes with each is not decided here: it comes from
#: ``DEGRADATION_POLICY``, which is the single decision table of ADR-0005.
_DEGRADED_REASONS: Final[dict[str, str]] = {
    # Deliberately narrower than "l'import de tickets est désactivé", which is what
    # this said and which was wider than the control behind it. Only the *photo*
    # path goes through the model; a PDF order recap is read straight out of the
    # document, with no provider involved at all, and works on an instance that has
    # none. Telling a household its receipt import is off when half of it works is
    # the same failure as telling it nothing was omitted when something was.
    "vision": (
        "Le modèle configuré ne sait pas lire les images : photographier un ticket "
        "de caisse est indisponible. L'import d'un récapitulatif de commande en PDF "
        "fonctionne toujours, il ne passe par aucun modèle."
    ),
    "structured_output": (
        "Le modèle configuré ne garantit pas de réponse structurée : le format lui "
        "est demandé dans les instructions puis vérifié ici, ce qui échoue plus "
        "souvent."
    ),
    "prompt_caching": (
        "Le modèle configuré ne met pas les instructions en cache : elles sont "
        "renvoyées à chaque demande. Mêmes réponses, mais davantage de jetons "
        "facturés."
    ),
    "long_context": (
        "La fenêtre de contexte du modèle configuré est courte : les suggestions ne "
        "portent que sur les produits les plus proches de leur date limite, pas sur "
        "tout l'inventaire."
    ),
}

_DEGRADED_REMEDIES: Final[dict[str, str]] = {
    "vision": "Choisissez un modèle multimodal, puis relancez la détection des capacités.",
    "long_context": (
        "Choisissez un modèle à plus grande fenêtre de contexte pour que tout le "
        "stock soit pris en compte."
    ),
}


def degradation_notices(capabilities: ProviderCapabilities) -> tuple[DegradationNotice, ...]:
    """One notice per capability the configuration lacks, in policy order."""
    return tuple(
        DegradationNotice(
            capability=capability,
            strategy=DEGRADATION_POLICY[capability],
            reason=_DEGRADED_REASONS[capability],
            remedy=_DEGRADED_REMEDIES.get(capability),
        )
        for capability in capabilities.missing
    )


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProviderView:
    """What ``GET /v1/providers/capabilities`` is rendered from.

    ``configured`` means *usable*, not *present*: a stored configuration nobody can
    call is reported as unconfigured, with the reason attached, because that is the
    state the interface has a screen for.
    """

    configured: bool
    mode: str | None = None
    provider: str | None = None
    model: str | None = None
    supports_vision: bool = False
    supports_structured_output: bool = False
    notices: tuple[DegradationNotice, ...] = ()

    @property
    def degraded(self) -> bool:
        return bool(self.notices)


@dataclass(frozen=True, slots=True)
class ActiveProvider:
    """A configuration the factory accepted, ready to be called."""

    config_id: uuid.UUID
    config: HouseholdProviderConfig
    capabilities: ProviderCapabilities
    generator: RecipeGenerator


@dataclass(frozen=True, slots=True)
class ActiveReceiptParser:
    """A configuration the factory accepted *and* that can see an image.

    Separate from :class:`ActiveProvider` rather than a wider version of it: the
    vision check is a precondition of this one and of nothing else, and a single
    type carrying an optional parser would let a caller reach for it without the
    check having run.
    """

    config_id: uuid.UUID
    config: HouseholdProviderConfig
    capabilities: ProviderCapabilities
    parser: ReceiptParser


class ProviderService:
    def __init__(
        self,
        session: AsyncSession,
        build_ports: ProviderPortsBuilder,
        cipher: CredentialCipher,
    ) -> None:
        self._session = session
        self._build_ports = build_ports
        self._cipher = cipher

    async def status(self, household_id: uuid.UUID) -> ProviderView:
        """Describe the household's provider. Never raises, never calls anyone."""
        row, refusal = await self._load(household_id)
        if row is None:
            return ProviderView(
                configured=False, notices=() if refusal is None else (refusal.notice(),)
            )
        if refusal is not None:
            return _view(row, configured=False, notices=(refusal.notice(),))

        sealed = await self._sealed_credential(row)
        if row.mode is LlmProviderMode.BYOK:
            if sealed is None:
                return _view(row, configured=False, notices=(_KEY_MISSING.notice(),))
            try:
                # The result is discarded on purpose: the question this endpoint
                # answers is "would the next call work?", and only an actual
                # decryption answers it. A rotated master key must show on the banner,
                # not on the first click.
                self._cipher.decrypt(sealed)
            except CredentialDecryptionError:
                return _view(row, configured=False, notices=(_KEY_UNDECRYPTABLE.notice(),))

        config = _to_provider_config(row, sealed)
        try:
            capabilities = self._build_ports(config.provider_code).capabilities_for(config)
        except ProviderNotConfigured:
            # The only two ways a stored configuration has no knowable capabilities:
            # an Ollama nobody probed, or a model this instance's table ignores.
            unusable = _UNPROBED if row.mode is LlmProviderMode.OLLAMA else _UNKNOWN_MODEL
            return _view(row, configured=False, notices=(unusable.notice(),))

        return _view(
            row,
            configured=True,
            notices=degradation_notices(capabilities),
            capabilities=capabilities,
        )

    async def for_recipes(self, household_id: uuid.UUID) -> ActiveProvider:
        """The provider that generates this household's recipes.

        Raises :class:`ProviderNotConfigured` -- or one of its subclasses,
        ``ProviderNotPermitted`` from the factory and ``CredentialDecryptionError``
        from the cipher -- when there is nothing usable. The API layer turns all of
        them into the 409 the client has a configuration screen for.
        """
        row, refusal = await self._load(household_id)
        if row is None:
            raise ProviderNotConfigured("this household has no model provider configured")
        if refusal is not None:
            raise ProviderNotConfigured(
                f"the model provider of this household cannot be used: {refusal.code}"
            )

        config = _to_provider_config(row, await self._sealed_credential(row))
        ports = self._build_ports(config.provider_code)
        capabilities = ports.capabilities_for(config)
        return ActiveProvider(
            config_id=row.id,
            config=config,
            capabilities=capabilities,
            generator=await ports.recipe_generator(config),
        )

    async def for_receipts(self, household_id: uuid.UUID) -> ActiveReceiptParser:
        """The provider that reads this household's photographed receipts.

        Two refusals, and they are different in kind. :class:`ProviderNotConfigured`
        means there is nothing usable and the household has a configuration screen
        for it. :class:`ProviderCapabilityUnavailable` means the configuration is
        perfectly fine and the model simply cannot see -- ADR-0005's ``unavailable``
        case, raised *here* rather than at the first byte of the image, so the
        interface can grey the button out with the reason instead of spending an
        upload to discover it.

        The vision check is not conditional on the provider. ADR-0005 warns that
        trusting a frontier model and distrusting a local one is exactly the shortcut
        the arithmetic laundering of section 3.4 makes dangerous, so the rule is the
        capability and nothing else.
        """
        row, refusal = await self._load(household_id, purpose=LlmPurpose.RECEIPT_PARSING)
        if row is None:
            raise ProviderNotConfigured(
                "this household has no model provider configured for receipt parsing"
            )
        if refusal is not None:
            raise ProviderNotConfigured(
                f"the model provider of this household cannot be used: {refusal.code}"
            )

        config = _to_provider_config(row, await self._sealed_credential(row))
        ports = self._build_ports(config.provider_code)
        capabilities = ports.capabilities_for(config)
        if not capabilities.supports_vision:
            raise ProviderCapabilityUnavailable(
                "vision",
                remedy=_DEGRADED_REMEDIES["vision"],
            )
        return ActiveReceiptParser(
            config_id=row.id,
            config=config,
            capabilities=capabilities,
            parser=await ports.receipt_parser(config),
        )

    async def _sealed_credential(self, row: LlmProviderConfig) -> SealedCredential | None:
        """Read the encrypted key of a ``byok`` row -- the deliberate gesture.

        ``api_key_ciphertext`` is a deferred column so that no ordinary
        ``select(LlmProviderConfig)`` can load a secret by accident
        (``docs/data-model.md`` §9.2). Fetching it is therefore its own query, named,
        greppable and reachable only from here -- and it is skipped entirely for the
        two modes the database already forbids from carrying a ciphertext.
        """
        if row.mode is not LlmProviderMode.BYOK:
            return None
        ciphertext = await self._session.scalar(
            select(LlmProviderConfig.api_key_ciphertext).where(
                LlmProviderConfig.household_id == row.household_id,
                LlmProviderConfig.id == row.id,
            )
        )
        if ciphertext is None or row.api_key_encryption_key_id is None:
            return None
        return SealedCredential(
            household_id=row.household_id,
            config_id=row.id,
            ciphertext=ciphertext,
            key_id=row.api_key_encryption_key_id,
        )

    async def _load(
        self,
        household_id: uuid.UUID,
        *,
        purpose: LlmPurpose = LlmPurpose.RECIPE_GENERATION,
    ) -> tuple[LlmProviderConfig | None, _Refusal | None]:
        """The household's configuration for ``purpose``, and why it is unusable.

        The purpose binding decides; without one, a single active configuration is
        taken as the answer and several are not. Picking one arbitrarily would spend
        money on a provider the household did not choose.

        ``purpose`` defaults to recipe generation so that the status endpoint and
        every existing caller keep the behaviour they had. A household that binds
        one configuration to recipes and another to receipts gets each where it
        asked for it; one that binds nothing and has a single configuration gets it
        for both, which is the ordinary single-key case.
        """
        bound_id = await self._session.scalar(
            select(LlmPurposeBinding.llm_provider_config_id).where(
                LlmPurposeBinding.household_id == household_id,
                LlmPurposeBinding.purpose == purpose,
            )
        )
        query = select(LlmProviderConfig).where(
            LlmProviderConfig.household_id == household_id,
            LlmProviderConfig.archived_at.is_(None),
        )
        if bound_id is not None:
            query = query.where(LlmProviderConfig.id == bound_id)
        rows = (await self._session.scalars(query.order_by(LlmProviderConfig.created_at))).all()

        if not rows:
            return None, None
        if len(rows) > 1:
            return None, (
                _AMBIGUOUS if purpose is LlmPurpose.RECIPE_GENERATION else _AMBIGUOUS_RECEIPTS
            )
        row = rows[0]
        # Before status, and before `_sealed_credential` is ever reached: consent is
        # the question of whether this household's data may leave at all, so it is
        # answered before the questions about whether the departure would succeed.
        # `infra/todo/factory.py` orders the Todoist chain the same way, and a
        # penetration test confirmed it there by counting outbound calls after a
        # withdrawal: zero.
        consent = self._consent_refusal(row)
        if consent is not None:
            return row, consent
        if row.status is LlmConfigStatus.DISABLED:
            return row, _DISABLED
        if row.status is LlmConfigStatus.INVALID_CREDENTIALS:
            return row, _INVALID_CREDENTIALS
        return row, None

    @staticmethod
    def _consent_refusal(row: LlmProviderConfig) -> _Refusal | None:
        """Whether this household has agreed to its data reaching a third party.

        ``ollama`` is exempt and the exemption is deliberate: the model runs on a
        machine the household controls, so there is no transmission and therefore no
        art. 6(1)(a) consent to collect. ``docs/security-model.md`` section 12 makes
        it an explicit requirement -- "the ``ollama`` mode must remain fully
        functional without this consent" -- and ADR-0007 rests on it: a household
        unwilling to send anything anywhere still gets the whole feature. Asking for
        agreement to send data to one's own computer would also teach people to click
        past consent screens, which costs more than it collects.

        Every other mode reaches a third party. ``byok`` sends under the household's
        own key and ``instance_owner`` under the operator's, which changes who pays
        and not who receives -- and the prompt carries a child's age band, a member's
        free-text health note, or an entire receipt photograph either way.

        Read on every load rather than cached, so a withdrawal takes effect at the
        next request. That is the property that makes the consent revocable in the
        sense art. 7(3) means, rather than revocable at the next restart.
        """
        if row.mode is LlmProviderMode.OLLAMA:
            return None
        if row.consented_at is None:
            return _CONSENT_MISSING
        if row.consent_revoked_at is not None:
            return _CONSENT_REVOKED
        return None


# --------------------------------------------------------------------------- #
# Writing a key
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StoredKey:
    """Everything the caller is allowed to know after storing a key.

    Deliberately not the key. ``last4`` is the whole of what ADR-0007 lets a user see
    again -- enough to recognise which of their keys is installed, useless to anyone
    else -- and a key that cannot be re-read can only be replaced, which is how every
    serious secret manager behaves.
    """

    config_id: uuid.UUID
    last4: str
    set_at: dt.datetime


class ProviderCredentialService:
    """Write-only side of the household credential: encrypt, persist, never return.

    Separated from :class:`ProviderService` because the two have opposite postures.
    The read side must never produce a key; this one is the single place in the
    application that accepts one, and it holds the plaintext for the length of one
    method call. Storing a key over an existing one *is* the rotation procedure of
    ADR-0007: an idempotent write, the previous value overwritten rather than
    versioned.
    """

    def __init__(self, session: AsyncSession, cipher: CredentialCipher) -> None:
        self._session = session
        self._cipher = cipher

    async def store_api_key(
        self, household_id: uuid.UUID, config_id: uuid.UUID, api_key: str
    ) -> StoredKey:
        """Encrypt ``api_key`` for this configuration and persist the secret triplet.

        Every rejection below is phrased from our own fields: the submitted value is
        never quoted, not even to say it is too short, because a validation message
        is exactly the kind of string that ends up in a client log.
        """
        # Pasted keys arrive with trailing whitespace far more often than not, and a
        # stray newline turns a valid key into an authentication failure nobody can
        # see in a database dump.
        key = api_key.strip()
        if len(key) < MIN_API_KEY_LENGTH:
            raise ProviderNotConfigured(
                f"an API key of at least {MIN_API_KEY_LENGTH} characters is required"
            )

        row = await self._session.scalar(
            select(LlmProviderConfig).where(
                LlmProviderConfig.household_id == household_id,
                LlmProviderConfig.id == config_id,
                LlmProviderConfig.archived_at.is_(None),
            )
        )
        if row is None:
            # Same answer whether it does not exist or belongs to someone else:
            # distinguishing them would turn this into a configuration oracle.
            raise ProviderNotConfigured(
                "no active provider configuration with this identifier belongs to this household"
            )
        if row.mode is not LlmProviderMode.BYOK:
            # The database enforces this too (`ck_llm_provider_config_mode_requirements`);
            # refusing here turns a constraint violation into a sentence.
            raise ProviderNotConfigured(
                f"mode {row.mode.value!r} does not store a key; only 'byok' does"
            )

        sealed = self._cipher.encrypt(key, household_id=household_id, config_id=config_id)
        row.api_key_ciphertext = sealed.ciphertext
        row.api_key_last4 = sealed.last4
        row.api_key_encryption_key_id = sealed.key_id
        set_at = dt.datetime.now(dt.UTC)
        row.api_key_set_at = set_at
        # A new key invalidates every earlier verdict about the old one, including a
        # stale `invalid_credentials` banner the household has already fixed.
        row.status = LlmConfigStatus.UNVERIFIED
        row.last_error = None
        await self._session.flush()

        logger.info(
            "household_api_key_stored",
            extra={"config_id": str(config_id), "provider": row.provider_code},
        )
        return StoredKey(config_id=config_id, last4=sealed.last4, set_at=set_at)


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def provider_ports_builder(
    settings: Settings,
    cipher: CredentialCipher,
    *,
    resolver: Resolver = system_resolver,
) -> ProviderPortsBuilder:
    """The production builder: a factory carrying the key that provider would spend.

    ``resolver`` is what arms the DNS-rebinding guard. Without it the factory builds
    an Ollama client with no pinned address, and
    ``GuardedHttpClient.assert_stable_resolution`` returns on its first line -- a
    control that reads as present in every review and protects nothing. It defaults
    to the real system resolver so that production is armed by omission rather than
    by remembering; the parameter exists so a test can substitute a resolver that
    rebinds, which is the only way to prove the wiring is still there.
    """

    def build(provider_code: str) -> ProviderPorts:
        return LlmProviderFactory(
            llm_settings_for(settings, provider_code), cipher=cipher, resolver=resolver
        )

    return build


def llm_settings_for(settings: Settings, provider_code: str) -> LlmSettings:
    """Project the application configuration onto what the provider layer reads."""
    read_key = _INSTANCE_OWNER_KEYS.get(provider_code)
    key = None if read_key is None else read_key(settings)
    return LlmSettings(
        ollama_allowed_hosts=frozenset(host.lower() for host in settings.ollama_allowed_hosts),
        instance_owner_household_id=_instance_owner(settings),
        timeout_seconds=(
            settings.ollama_timeout_seconds
            if provider_code == OLLAMA_PROVIDER_CODE
            else settings.llm_timeout_seconds
        ),
        instance_owner_api_key=None if key is None else key.get_secret_value(),
    )


def _instance_owner(settings: Settings) -> uuid.UUID | None:
    """The household allowed to spend the operator's credit, or nobody.

    A malformed value closes the door rather than opening it, and the value itself
    never reaches the log: the failure mode of getting ADR-0007 wrong is a
    stranger's invoice.
    """
    raw = settings.instance_owner_household_id
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        logger.error("instance_owner_household_id_is_not_a_uuid")
        return None


def _view(
    row: LlmProviderConfig,
    *,
    configured: bool,
    notices: tuple[DegradationNotice, ...],
    capabilities: ProviderCapabilities | None = None,
) -> ProviderView:
    return ProviderView(
        configured=configured,
        mode=row.mode.value,
        provider=row.provider_code,
        model=row.model,
        supports_vision=capabilities is not None and capabilities.supports_vision,
        supports_structured_output=(
            capabilities is not None and capabilities.supports_structured_output
        ),
        notices=notices,
    )


def _to_provider_config(
    row: LlmProviderConfig, sealed: SealedCredential | None
) -> HouseholdProviderConfig:
    return HouseholdProviderConfig(
        household_id=row.household_id,
        mode=row.mode,
        provider_code=row.provider_code,
        model=row.model,
        base_url=row.base_url,
        # Never a plaintext key from this path. The sealed credential travels to the
        # factory, which decrypts it at the moment of use and does not keep it.
        api_key=None,
        sealed_api_key=sealed,
        probed_capabilities=_probed_capabilities(row),
    )


def _probed_capabilities(row: LlmProviderConfig) -> ProviderCapabilities | None:
    """The stored Ollama probe, or ``None`` when there is nothing trustworthy.

    Absence is not a default here: a configuration whose abilities nobody
    established cannot be used to promise a feature, so the factory refuses it and
    the interface says why.
    """
    if row.mode is not LlmProviderMode.OLLAMA:
        return None
    if row.max_context_tokens is None or row.last_verified_at is None:
        return None
    return ProviderCapabilities(
        provider=row.provider_code,
        model=row.model,
        context_window=row.max_context_tokens,
        supports_structured_output=row.supports_structured_output,
        supports_vision=row.supports_vision,
        # Ollama exposes no prompt cache: the stable prefix is resent every call,
        # which `DEGRADATION_POLICY` treats as emulation with a token cost.
        supports_prompt_caching=False,
        source=CapabilitySource.PROBED,
        probed_at=_as_utc(row.last_verified_at),
    )


def _as_utc(moment: dt.datetime) -> dt.datetime:
    """Probed capabilities must carry a timezone; a naive column value gets UTC."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=dt.UTC)

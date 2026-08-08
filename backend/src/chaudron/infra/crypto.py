"""AES-256-GCM for the API keys households bring themselves (ADR-0007, §9.2).

Three properties are load-bearing here, and each one closes a specific attack that
the others do not:

* **The master key comes from the environment, never from the database.** A stolen
  ``pg_dump`` then yields no usable provider key or export token, because the only
  thing that would decrypt them is not in it. Storing the master key in a
  configuration table beside the rows it protects would leave only the appearance of
  encryption.

  Read that sentence narrowly: *these two columns* survive the dump, and nothing
  else does. The same dump still carries account e-mails, household composition,
  the art. 9 dietary and allergy columns, and every purchase. This module protects
  the credentials a household entrusted to the application; what bounds the rest is
  operational, and lives in ``docs/security-model.md`` §6.8 and §8.4.
* **The ciphertext is bound to its row *and to what the row is for*.**
  ``(domain, household_id, config_id)`` is the additional authenticated data, so a
  ciphertext copied from one row onto another -- by an attacker with write access,
  or by a careless data migration -- fails to decrypt instead of silently handing
  household B the key that household A pays for. Authenticated encryption is what
  makes that binding enforceable: GCM verifies the AAD before it releases a single
  plaintext byte.
* **Rotation fails loudly.** The key that produced a ciphertext is identified by
  :attr:`CredentialCipher.key_id`, stored alongside it. When
  ``CHAUDRON_CREDENTIAL_ENCRYPTION_KEY`` changes, the mismatch is detected before any
  cryptography runs and reported as a domain error naming the variable and the
  remedy -- not as an ``InvalidTag`` traceback that says nothing actionable.

Nothing in this module ever puts a plaintext key in a message, a log line or an
exception chain: every failure path raises ``from None``, and the only fragment of a
key that survives encryption is :attr:`EncryptedCredential.last4`, which exists so a
user can recognise *which* of their keys is installed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import uuid
from dataclasses import dataclass
from typing import Final, Self

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from chaudron.config import CREDENTIAL_KEY_BYTES, Settings
from chaudron.domain.llm_ports import CredentialDecryptionError

__all__ = [
    "CREDENTIAL_ENCRYPTION_KEY_ENV_VAR",
    "EXPORT_TOKEN_DOMAIN",
    "LAST4_LENGTH",
    "MIN_API_KEY_LENGTH",
    "PROVIDER_KEY_DOMAIN",
    "CredentialCipher",
    "EncryptedCredential",
    "SealedCredential",
]

#: Named in every rotation failure, because "decryption failed" without the name of
#: the variable to look at is a support ticket rather than a diagnostic.
CREDENTIAL_ENCRYPTION_KEY_ENV_VAR: Final = "CHAUDRON_CREDENTIAL_ENCRYPTION_KEY"

#: 96 bits, the size GCM is specified for. Longer nonces are hashed internally and
#: buy nothing; shorter ones narrow the birthday bound for no reason.
_NONCE_BYTES: Final = 12

#: The GCM authentication tag, appended to the ciphertext by this backend.
_TAG_BYTES: Final = 16

#: Domain separation. Each purpose the master key serves carries its own prefix in
#: the AAD, so a ciphertext cannot be replayed across purposes even if the two rows
#: it names happen to share their identifiers. The prefix is authenticated, not
#: stored: a ciphertext opened under the wrong domain fails the GCM tag, which is
#: the same loud failure as a ciphertext moved between households.
#:
#: The original use, and therefore the default everywhere the domain is not spelled
#: out: the API keys households bring for model providers (ADR-0007).
PROVIDER_KEY_DOMAIN: Final = b"chaudron/llm_provider_config/api_key/v1"

#: The second use: the personal token a household registers to have its shopping
#: list sent to a task application (ADR-0010 section 6). Introduced with the
#: ``shopping_export_target`` table, which is what that ADR made its prerequisite --
#: until then an export token was sealed under the provider domain and cross-purpose
#: replay was prevented by the row identifiers alone, which held by arithmetic
#: rather than by design.
EXPORT_TOKEN_DOMAIN: Final = b"chaudron/shopping_export_target/token/v1"

#: ``api_key_encryption_key_id`` is ``varchar(32)``; 16 hex characters of a keyed
#: digest identify a key without revealing anything about it.
_KEY_ID_BYTES: Final = 8

#: What the user sees again, in clear, in its own column.
LAST4_LENGTH: Final = 4

#: Below this, a key could not be scrubbed from a diagnostic by literal match --
#: :func:`chaudron.infra.redaction.redact` ignores shorter secrets on purpose,
#: since blanking two characters would only mangle readable text. No real provider
#: issues a key this short; refusing one here keeps the redaction guarantee total.
MIN_API_KEY_LENGTH: Final = 8


@dataclass(frozen=True, slots=True)
class EncryptedCredential:
    """What a caller persists after encrypting: the three secret-triplet columns.

    Carries no plaintext, so it is safe to log or repr -- ``last4`` is public by
    design (``docs/data-model.md`` §9.2) and the ciphertext is useless without the
    master key and the row it was bound to.
    """

    ciphertext: bytes
    last4: str
    key_id: str


@dataclass(frozen=True, slots=True)
class SealedCredential:
    """What the database holds for one configuration: enough to decrypt, no more.

    The identifiers are part of the value rather than parameters passed alongside it:
    a ciphertext without the pair it was bound to cannot be decrypted at all, and
    keeping them together makes it impossible to decrypt row A's key while claiming
    row B's identity.

    ``domain`` is the third element of that binding, and it defaults to
    :data:`PROVIDER_KEY_DOMAIN` because that is the use every existing row was
    written under. A caller reading a row from another table has to say so -- and a
    caller that forgets gets an ``InvalidTag``, which is a failed decryption rather
    than a silent success under the wrong purpose.
    """

    household_id: uuid.UUID
    config_id: uuid.UUID
    ciphertext: bytes
    key_id: str
    domain: bytes = PROVIDER_KEY_DOMAIN


def _aad(household_id: uuid.UUID, config_id: uuid.UUID, domain: bytes) -> bytes:
    """The additional authenticated data binding a ciphertext to exactly one row.

    Fixed-width UUID bytes rather than their string form: no separator can be
    smuggled into a component, so no two distinct pairs can produce the same AAD.
    The domain prefix comes first and is itself fixed and internal, so the same pair
    of identifiers under two purposes yields two AADs that share no prefix boundary.
    """
    return domain + household_id.bytes + config_id.bytes


class CredentialCipher:
    """Encrypts and decrypts one household's provider key, under one master key.

    Built once per process from the environment. The instance holds key material, so
    it deliberately has no default ``repr``: a settings dump, a traceback frame or a
    debugger session must not be able to print it.
    """

    __slots__ = ("_aesgcm", "_key_id")

    def __init__(self, key: bytes) -> None:
        if len(key) != CREDENTIAL_KEY_BYTES:
            raise ValueError(
                f"the credential encryption key must be exactly {CREDENTIAL_KEY_BYTES} "
                f"bytes for AES-256-GCM, got {len(key)}"
            )
        self._aesgcm = AESGCM(key)
        self._key_id = _derive_key_id(key)

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """Build from the validated configuration.

        ``Settings`` already rejects a key that is not base64 or not 32 bytes at
        startup, so this decode cannot fail on a running instance -- the guard is
        kept because a silently wrong key would only surface days later, on the first
        household to store a credential.
        """
        raw = settings.credential_encryption_key.get_secret_value()
        try:
            key = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                f"{CREDENTIAL_ENCRYPTION_KEY_ENV_VAR} is not valid base64 "
                "(generate one with `openssl rand -base64 32`)"
            ) from exc
        return cls(key)

    @property
    def key_id(self) -> str:
        """Identifier of the master key in use, stored beside every ciphertext."""
        return self._key_id

    def __repr__(self) -> str:
        return f"CredentialCipher(key_id={self._key_id!r})"

    def encrypt(
        self,
        api_key: str,
        *,
        household_id: uuid.UUID,
        config_id: uuid.UUID,
        domain: bytes = PROVIDER_KEY_DOMAIN,
    ) -> EncryptedCredential:
        """Encrypt ``api_key`` for exactly this ``(purpose, household, row)`` triple.

        The caller is expected to have validated the key already; the length check
        below is an invariant guard, not input validation, and its message quotes a
        count rather than the value.

        ``domain`` defaults to the provider keys this module was written for. A
        second purpose passes its own constant, and whatever reads the row back has
        to pass the same one -- see :class:`SealedCredential`.
        """
        if len(api_key) < MIN_API_KEY_LENGTH:
            raise ValueError(
                f"an API key of at least {MIN_API_KEY_LENGTH} characters is required "
                f"(got {len(api_key)})"
            )
        nonce = os.urandom(_NONCE_BYTES)
        body = self._aesgcm.encrypt(
            nonce, api_key.encode("utf-8"), _aad(household_id, config_id, domain)
        )
        return EncryptedCredential(
            ciphertext=nonce + body,
            last4=api_key[-LAST4_LENGTH:],
            key_id=self._key_id,
        )

    def decrypt(self, sealed: SealedCredential) -> str:
        """Return the stored key, or raise a domain error explaining why not.

        Every failure -- rotated master key, truncated column, ciphertext lifted from
        another household -- becomes a :class:`CredentialDecryptionError` with an
        actionable sentence and **no cause**. Chaining the underlying ``InvalidTag``
        would put a cryptography traceback one frame away from any handler, and
        ADR-0007 requires that a stored key never reach a log or an HTTP response by
        any path, including ``__cause__``.
        """
        if sealed.key_id != self._key_id:
            raise CredentialDecryptionError(
                "the stored API key was encrypted with a different "
                f"{CREDENTIAL_ENCRYPTION_KEY_ENV_VAR}; restore the previous value, or "
                "enter the key again to re-encrypt it with the current one"
            ) from None
        if len(sealed.ciphertext) <= _NONCE_BYTES + _TAG_BYTES:
            raise CredentialDecryptionError(
                "the stored API key is truncated and cannot be decrypted; enter the key again"
            ) from None

        nonce, body = sealed.ciphertext[:_NONCE_BYTES], sealed.ciphertext[_NONCE_BYTES:]
        try:
            plaintext = self._aesgcm.decrypt(
                nonce, body, _aad(sealed.household_id, sealed.config_id, sealed.domain)
            )
        except InvalidTag:
            # Authentication covers the AAD, so this is also the branch a ciphertext
            # copied onto another household's row -- or onto a row serving another
            # purpose -- lands in: exactly the replay ADR-0007 requires to fail.
            raise CredentialDecryptionError(
                "the stored API key could not be decrypted: it does not belong to "
                "this household's configuration, or it was written with a different "
                f"{CREDENTIAL_ENCRYPTION_KEY_ENV_VAR}. Enter the key again to replace it"
            ) from None
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError:
            raise CredentialDecryptionError(
                "the stored API key decrypted to something that is not text; enter the key again"
            ) from None


def _derive_key_id(key: bytes) -> str:
    """A stable, non-reversible name for a master key.

    BLAKE2b with a personalisation string rather than a bare hash: the identifier is
    stored in the clear next to the ciphertext, so it must not be usable to confirm a
    guess at the key in any other context.
    """
    digest = hashlib.blake2b(key, digest_size=_KEY_ID_BYTES, person=b"chaudron-kid")
    return digest.hexdigest()

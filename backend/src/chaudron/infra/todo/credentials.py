"""Export tokens at rest: the same cipher as the provider keys, on purpose.

A Todoist personal token is exactly the kind of secret ADR-0007 already reasons
about -- it belongs to a third party, it is billable in the sense that it grants
access to that party's account, and it is worth more to an attacker than anything
else in the row it sits in. So this module performs no cryptography of its own.
It calls :class:`chaudron.infra.crypto.CredentialCipher`, which already gives
three properties that would take a fortnight to get right and an afternoon to get
wrong:

* the master key comes from the environment, so a stolen dump is ciphertext;
* the ciphertext is bound by AAD to ``(purpose, household_id, row_id)``, so a row
  lifted onto another household's target -- or onto a row serving another purpose
  -- fails to decrypt rather than exporting somebody's groceries into a
  stranger's account;
* a rotated master key fails loudly, naming the variable and the remedy.

**The deviation ADR-0010 section 6 recorded is closed here.** ``crypto.py`` used
to fix its AAD domain string to ``chaudron/llm_provider_config/api_key/v1``, so an
export token was sealed under the *provider* purpose and cross-purpose replay was
prevented by the row identifier alone -- a property that held by arithmetic (two
independently drawn UUIDs do not collide) rather than by design. That ADR made
adding ``chaudron/shopping_export_target/token/v1`` a prerequisite of the
``shopping_export_target`` table, and the table is now here, so the prefix is too:
:data:`~chaudron.infra.crypto.EXPORT_TOKEN_DOMAIN`.

Nothing outside this module names that constant. Sealing goes through
:func:`seal_export_token` and reading a row back goes through
:func:`sealed_export_token`, which is what keeps the two halves of a binding from
drifting apart in a caller that only remembered one of them.
"""

from __future__ import annotations

import logging
import uuid

from chaudron.domain.llm_ports import CredentialDecryptionError
from chaudron.domain.shopping_export import ExportCredentialUnreadable
from chaudron.infra.crypto import (
    EXPORT_TOKEN_DOMAIN,
    CredentialCipher,
    EncryptedCredential,
    SealedCredential,
)

__all__ = ["open_export_token", "seal_export_token", "sealed_export_token"]

logger = logging.getLogger(__name__)


def seal_export_token(
    cipher: CredentialCipher,
    token: str,
    *,
    household_id: uuid.UUID,
    target_id: uuid.UUID,
) -> EncryptedCredential:
    """Encrypt a destination token for exactly this ``(household, target)`` row.

    ``target_id`` takes the place of the provider configuration identifier, so
    the ciphertext is bound to the row that will hold it. The result carries the
    three columns to persist and no plaintext, which is what makes it safe to
    log, repr, or return from a service.
    """
    cleaned = token.strip()
    if not cleaned:
        raise ValueError("an export token cannot be empty")
    return cipher.encrypt(
        cleaned, household_id=household_id, config_id=target_id, domain=EXPORT_TOKEN_DOMAIN
    )


def sealed_export_token(
    *,
    household_id: uuid.UUID,
    target_id: uuid.UUID,
    ciphertext: bytes,
    key_id: str,
) -> SealedCredential:
    """Rebuild what one ``shopping_export_target`` row holds, stamped with its purpose.

    The counterpart of :func:`seal_export_token`, and the only supported way to
    turn three columns back into something :func:`open_export_token` will accept.
    Constructing a :class:`~chaudron.infra.crypto.SealedCredential` by hand is not
    wrong so much as easy to get wrong: the ``domain`` field defaults to the
    provider purpose, and an export row read under that default fails to decrypt
    with a message about rotation that would send an operator looking at their
    master key.
    """
    return SealedCredential(
        household_id=household_id,
        config_id=target_id,
        ciphertext=ciphertext,
        key_id=key_id,
        domain=EXPORT_TOKEN_DOMAIN,
    )


def open_export_token(cipher: CredentialCipher, sealed: SealedCredential) -> str:
    """Decrypt a stored token, as late as possible and never one call earlier.

    The cipher's :class:`~chaudron.domain.llm_ports.CredentialDecryptionError`
    used to be allowed through unchanged, on the argument that it already names
    ``CHAUDRON_CREDENTIAL_ENCRYPTION_KEY`` and the way out. The argument was
    right about the message and wrong about the audience (audit O-06). That error
    is a ``ProviderNotConfigured`` -- it belongs to the *model provider*
    hierarchy, which shares nothing with :class:`ShoppingExportError` -- so
    ``routers/shopping_export.py``, which catches the export errors, did not
    catch it, and the household saw a bare 500 with no idea what to do. Nothing
    exotic produced it: an instance restored or rotated without its credential
    encryption key stops opening every stored token at once.

    So the two audiences are separated here rather than conflated. The operator's
    diagnostic -- which names the environment variable and the remedy, and never
    the key -- goes to the log. The household gets
    :class:`ExportCredentialUnreadable`, which says its destination's credential
    can no longer be read and to register it again, and which the router already
    knows how to turn into an answer.

    ``from None`` preserves the rule ADR-0007 puts on this path: an exception
    leaving here carries no ciphertext and no ``__cause__``, so no handler that
    logs a traceback can reach one.
    """
    try:
        return cipher.decrypt(sealed)
    except CredentialDecryptionError as error:
        # `str(error)` is safe to write: `infra/crypto.py` composes it from our
        # own fields and quotes neither the key nor the ciphertext.
        logger.error("export_token_undecryptable", extra={"reason": str(error)})
        raise ExportCredentialUnreadable(
            "the stored credential for this destination can no longer be read by "
            "this instance; register the destination again to replace it"
        ) from None

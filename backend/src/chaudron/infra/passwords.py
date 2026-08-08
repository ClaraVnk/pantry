"""Password hashing, and the two properties that make it worth having.

**Argon2id, not bcrypt and not PBKDF2.** bcrypt silently truncates at 72 bytes,
so a passphrase and its first 72 bytes are the same secret -- a property nobody
notices until a password manager generates something longer. PBKDF2 is
memory-cheap, which is exactly what a GPU wants. Argon2id is the one the Password
Hashing Competition selected and the one RFC 9106 recommends by default, and it
is memory-hard: the attacker pays in RAM per guess, not only in cycles.

**The verification cost is uniform.** :meth:`Passwords.verify` is called even
when no account matched, against :data:`_DUMMY_HASH`, because the alternative
leaks: an unknown address answers in microseconds and a known one in tens of
milliseconds, which turns the login endpoint into an account enumerator that no
amount of identical response *bodies* can close. The service layer owns that
decision; this module makes it cheap to honour by exposing a verify that accepts
``None``.

The parameters come from :data:`argon2.profiles.RFC_9106_LOW_MEMORY` rather than
from numbers chosen here: 64 MiB, three passes, four lanes. That profile is the
one the RFC names for a server that also has to serve requests, and pinning it by
name means an upgrade of the library moves us with the standard instead of
leaving a magic tuple nobody dares touch.

Rehashing is handled: :meth:`Passwords.needs_rehash` reports when a stored digest
was produced with weaker parameters than the current profile, so a successful
login can transparently upgrade it.
"""

from __future__ import annotations

from typing import Final

from argon2 import PasswordHasher, Type, profiles
from argon2.exceptions import HashingError, InvalidHashError, VerificationError, VerifyMismatchError

#: The longest password accepted. Not a security bound -- Argon2 has no
#: truncation point -- but a denial-of-service one: the hash cost is linear in
#: the input length once past a few kilobytes, and nothing legitimate is longer.
MAX_PASSWORD_BYTES: Final = 1024

#: The shortest password accepted. Deliberately a length rule and nothing else:
#: composition rules ("one digit, one symbol") measurably push people towards
#: `Password1!` and are dropped by every current guideline (NIST SP 800-63B).
MIN_PASSWORD_LENGTH: Final = 12


class PasswordTooLongError(ValueError):
    """The candidate exceeds :data:`MAX_PASSWORD_BYTES` once encoded."""


class Passwords:
    """Hash and verify, with one hasher shared by the whole process.

    Building a :class:`PasswordHasher` is cheap, but sharing one keeps the
    parameters in a single place and makes ``needs_rehash`` answer against the
    profile the application actually uses.
    """

    def __init__(self, hasher: PasswordHasher | None = None) -> None:
        self._hasher = (
            hasher
            if hasher is not None
            else PasswordHasher.from_parameters(profiles.RFC_9106_LOW_MEMORY)
        )
        # Computed once, at construction, from a value no account can hold: a
        # verify against it costs the same as a verify against a real digest,
        # which is the whole point of having it.
        self._dummy = self._hasher.hash("chaudron-nonexistent-account-placeholder")

    @property
    def type(self) -> Type:
        """The Argon2 variant in use. ``Type.ID`` is the only acceptable answer."""
        return self._hasher.type

    def hash(self, password: str) -> str:
        """Return the encoded digest, parameters and salt included."""
        self._reject_oversized(password)
        return self._hasher.hash(password)

    def verify(self, password: str, encoded: str | None) -> bool:
        """Check *password*, taking the same time whether or not a hash exists.

        ``encoded is None`` covers two real cases that must not be
        distinguishable from outside: no such account, and an account created
        through an external identity provider that has no password at all
        (``UserAccount.password_hash`` is nullable). Both burn a full Argon2
        verification against the dummy digest and then answer ``False``.

        The size bound is checked **first**, before the digest is even chosen.
        Order matters twice over. It is what makes :data:`MAX_PASSWORD_BYTES` a
        real denial-of-service bound rather than a decorative one: sign-in is the
        only unauthenticated entry point in this application, and it is the one
        place an attacker who holds no account at all can ask for an Argon2 pass
        over a ten-megabyte input. And rejecting before the branch on ``encoded``
        keeps the refusal identical for a known and an unknown address -- a check
        placed after it would answer instantly for one and slowly for the other,
        which is the enumeration oracle this class exists to close.
        """
        self._reject_oversized(password)
        candidate = encoded if encoded is not None else self._dummy
        try:
            self._hasher.verify(candidate, password)
        except VerifyMismatchError, VerificationError, InvalidHashError, HashingError:
            return False
        # An account with no password can never authenticate, however convincing
        # the value it was compared against.
        return encoded is not None

    def needs_rehash(self, encoded: str) -> bool:
        """Whether *encoded* was produced with parameters weaker than the current ones."""
        try:
            return self._hasher.check_needs_rehash(encoded)
        except InvalidHashError:
            # An unparseable digest cannot be upgraded in place; it can only be
            # replaced, which is what the caller does with a `False` here anyway
            # since `verify` has already refused it.
            return False

    @staticmethod
    def _reject_oversized(password: str) -> None:
        if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
            raise PasswordTooLongError(
                f"a password may not exceed {MAX_PASSWORD_BYTES} bytes once encoded"
            )

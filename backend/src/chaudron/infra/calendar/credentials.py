"""The credential a phone stores to read one household's expiry feed.

A CalDAV client has no login screen. It is handed a user name and a password
once, keeps them forever, and replays them on every poll. So the feed needs a
secret of its own, and the whole design of this module follows from four
properties that secret must have.

*It must not be the household identifier.* ``X-Household-Id`` is an address, not
a proof (``api/deps.py``, audit AUD-001): anybody holding it reads and writes the
whole API. A feed identifier derived from it -- or worse, equal to it -- would
turn a URL in a proxy log into full access to the account. Nothing here is
reversible: the identifier in the URL is a truncated HMAC, and the household it
names cannot be recovered from it without the instance key.

*It must not be the URL.* A feed whose URL is the secret ends up in a browser
history, a clipboard and an access log. The identifier travels in the path (it
has to: a CalDAV principal is a URL), the secret travels in ``Authorization``
and nowhere else, and one without the other is worth nothing.

*It must be its own key.* ``CHAUDRON_SECRET_KEY`` already signs sessions; the
threat model asks for "two uses, two keys, derived if need be" (security model
section 6.7). This module derives its key with HKDF and a labelled ``info``, so a
feed credential yields nothing about a session token and vice versa.

*It must be revocable, one household at a time.* Two counters are mixed in, and
they answer two different questions. ``CHAUDRON_CALENDAR_FEED_EPOCH`` is
instance-wide: bumping it invalidates every credential this instance ever issued,
without touching ``CHAUDRON_SECRET_KEY`` and therefore without logging anybody
out. ``household.calendar_feed_epoch`` is per household: bumping *that* one --
an ``UPDATE`` of a single row, exposed to the household's owner -- invalidates
that household's credential and nobody else's.
``CHAUDRON_CALENDAR_FEED_ENABLED=false`` remains the immediate kill switch for
the whole instance.

**Why the per-household counter is not optional.** The credential is derived, not
stored, so nothing about it can be deleted. Without a value the household itself
controls, a person who read the subscription page once -- a flatmate who moved
out, a former partner, an owner whose membership was withdrawn -- keeps reading
that household's inventory for as long as the instance key lives. Membership is
checked on every other request and revoking it is immediate; this credential runs
alongside that check and would have outlived it. The counter is what puts the two
back on the same footing: withdrawing the feed is one row, one ``UPDATE``, and
every device subscribed to *that* household has to be re-subscribed.

Resolution is a scan. There is no reverse index from an identifier to a
household, so a request derives the expected identifier for each live household
and compares. That is one HMAC per household per request, which is a few
microseconds each and bounded by :data:`MAX_RESOLVABLE_HOUSEHOLDS` -- past that
bound the instance is told to persist an indexed identifier rather than quietly
paying a linear cost on a hot path.

**The scan is the cost an unauthenticated caller can spend, so it is guarded on
both sides of this module.** ``routers/calendar.py`` charges its failure limiter
*before* calling :meth:`FeedKeyring.resolve`, and :func:`looks_issued` rejects
anything outside the alphabet the derivation can produce before a single row is
read. The second guard is not merely an optimisation: ``hmac.compare_digest``
raises ``TypeError`` on a ``str`` holding a character outside ASCII, and an
unauthenticated request that raises is a stack trace per attempt with no upper
bound on how often it can be asked for.
"""

from __future__ import annotations

import base64
import hmac
import string
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from chaudron.config import Settings

#: Domain separation for the feed key. Versioned: if the derivation ever changes
#: shape, the label changes with it and old credentials stop verifying instead of
#: verifying differently.
_KEY_INFO_PREFIX: Final = b"chaudron/calendar-feed/v1/epoch="

#: 128 bits. Long enough that the identifier alone is not enumerable, short
#: enough that the 26 characters it renders as fit in a phone's user name field.
FEED_ID_BYTES: Final = 16

#: 160 bits. This is the part that actually authorises, and it is never typed
#: from memory, so it is sized for the attacker rather than for the thumb.
FEED_SECRET_BYTES: Final = 20

#: Opaque per-household name of one task resource. Same reasoning as the feed
#: identifier: a lot's UUIDv7 in a URL would put its creation time in the reverse
#: proxy's access log (security model section 7.2).
RESOURCE_NAME_BYTES: Final = 16

FEED_ID_LENGTH: Final = 26
FEED_SECRET_LENGTH: Final = 32
RESOURCE_NAME_LENGTH: Final = 26

#: Every character :func:`_base32` can emit, and therefore the only alphabet a
#: credential this server issued can be spelled in. RFC 4648 minus the padding,
#: which :func:`_base32` strips.
_BASE32_ALPHABET: Final = frozenset(string.ascii_uppercase + "234567")

#: Width the per-household counter is serialised at inside the MAC input. Fixed
#: rather than minimal: a variable-length integer glued to a fixed-length UUID
#: would let two different ``(household, epoch)`` pairs spell the same payload,
#: which is the ambiguity the ``\x00``-separated purpose labels above exist to
#: avoid in the other direction.
_EPOCH_BYTES: Final = 8

#: What ``household.calendar_feed_epoch`` starts at, and what the derivation must
#: use for any caller that has no row to read it from. Kept here rather than in
#: the model so that the value the MAC depends on is declared next to the MAC.
INITIAL_FEED_EPOCH: Final = 1

#: Above this, the linear scan below stops being a reasonable answer. An instance
#: that large has to persist the identifier and index it; refusing loudly is the
#: only way that decision gets made on purpose rather than discovered in a flame
#: graph.
MAX_RESOLVABLE_HOUSEHOLDS: Final = 5_000

#: Labels for the three values derived from the same key. Byte-distinct prefixes
#: with a NUL separator, so no concatenation of one purpose's input can ever spell
#: another's.
_PURPOSE_FEED_ID: Final = b"feed-id\x00"
_PURPOSE_FEED_SECRET: Final = b"feed-secret\x00"
_PURPOSE_RESOURCE: Final = b"resource\x00"


class TooManyHouseholdsError(RuntimeError):
    """The instance outgrew the scan-based resolution in this module."""


def looks_issued(feed_id: str, secret: str) -> bool:
    """Whether these two strings *could* be a pair this server derived.

    Length and alphabet, checked together, on the two halves of a decoded
    ``Authorization: Basic`` header and before anything else happens. Two things
    ride on it.

    *It keeps a malformed guess off the household scan.* A credential of the wrong
    shape cannot be one :meth:`FeedKeyring.credentials_for` produced, so resolving
    it would be a linear read of the household table with a foregone answer.

    *It keeps a malformed guess from raising.* ``hmac.compare_digest`` refuses two
    ``str`` operands as soon as either holds a character outside ASCII -- it
    raises ``TypeError`` rather than returning ``False``, because it can only
    guarantee constant time over bytes it can encode without branching. Reaching
    it with a user-supplied string therefore turns an unauthenticated request into
    a ``500`` and a logged stack trace, repeatable for free. The length check that
    stood here before did not catch it: ``"é" * 26`` is twenty-six characters.
    """
    return (
        len(feed_id) == FEED_ID_LENGTH
        and len(secret) == FEED_SECRET_LENGTH
        and _BASE32_ALPHABET.issuperset(feed_id)
        and _BASE32_ALPHABET.issuperset(secret)
    )


@dataclass(frozen=True, slots=True)
class FeedHousehold:
    """A household a credential may name, and the counter that revokes it.

    Carried as a pair because the derivation needs both and reading one without
    the other is the bug this type exists to make impossible: a scan that forgot
    the epoch would keep answering revoked credentials.
    """

    id: uuid.UUID
    feed_epoch: int = INITIAL_FEED_EPOCH


def _base32(raw: bytes) -> str:
    """Unpadded RFC 4648 base32, upper case.

    Base32 rather than base64 or hex: its alphabet has no ``0``/``O`` or
    ``1``/``l`` pair to misread, and it is a third shorter than hex. Upper case
    rather than lower: an iOS user name field auto-capitalises, and a value that
    is already upper case survives that untouched.
    """
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def _feed_payload(household: FeedHousehold) -> bytes:
    """What the two credential MACs are taken over: the household and its epoch.

    ``ValueError`` rather than a silently different payload if the epoch is out of
    range. The column is ``NOT NULL`` with a ``> 0`` check, so this is unreachable
    from the database; it is reachable from a hand-built
    :class:`FeedHousehold`, and an ``OverflowError`` escaping from inside an
    authentication path is a ``500`` where a refusal belongs.
    """
    if not 0 < household.feed_epoch < 2 ** (_EPOCH_BYTES * 8):
        raise ValueError(f"feed epoch out of range: {household.feed_epoch}")
    return household.id.bytes + household.feed_epoch.to_bytes(_EPOCH_BYTES, "big")


@dataclass(frozen=True, slots=True)
class FeedCredentials:
    """What a household types into a phone, once.

    ``secret`` is a bearer credential: it is returned by exactly one endpoint,
    never logged, and never written to the database. ``__repr__`` is overridden
    for the same reason the provider configuration objects override theirs -- a
    dataclass that prints its own secret ends up in a traceback.
    """

    feed_id: str
    secret: str

    def __repr__(self) -> str:  # pragma: no cover - trivial, but load-bearing
        return f"FeedCredentials(feed_id={self.feed_id!r}, secret=<redacted>)"


class FeedKeyring:
    """Derives and verifies every value the feed needs from one instance key."""

    __slots__ = ("_key",)

    def __init__(self, key: bytes) -> None:
        self._key = key

    @classmethod
    def from_settings(cls, settings: Settings) -> FeedKeyring:
        """Derive the feed key from the instance secret and the current epoch.

        HKDF-Expand rather than a bare hash: the input is a configured string of
        unknown entropy distribution, and extract-then-expand is what turns that
        into a uniform 256-bit key without assuming anything about it.
        """
        key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=_KEY_INFO_PREFIX + settings.calendar_feed_epoch.encode("utf-8"),
        ).derive(settings.secret_key.get_secret_value().encode("utf-8"))
        return cls(key)

    def _mac(self, purpose: bytes, payload: bytes, *, size: int) -> str:
        return _base32(hmac.new(self._key, purpose + payload, "sha256").digest()[:size])

    def credentials_for(self, household: FeedHousehold) -> FeedCredentials:
        """The pair one household types into a phone, for its current epoch.

        The whole pair moves when ``feed_epoch`` moves -- identifier as well as
        secret. Rotating only the secret would leave the old URL answering ``401``
        instead of ``404``, which tells a former member that the feed they used to
        read still exists and merely changed its password.
        """
        payload = _feed_payload(household)
        return FeedCredentials(
            feed_id=self._mac(_PURPOSE_FEED_ID, payload, size=FEED_ID_BYTES),
            secret=self._mac(_PURPOSE_FEED_SECRET, payload, size=FEED_SECRET_BYTES),
        )

    def resource_name(self, household_id: uuid.UUID, lot_id: uuid.UUID) -> str:
        """The opaque file name a task is published under, stable across polls.

        Keyed by household as well as by lot, so two households cannot compare
        notes and learn that they hold a row with the same identifier.
        """
        return self._mac(
            _PURPOSE_RESOURCE,
            household_id.bytes + lot_id.bytes,
            size=RESOURCE_NAME_BYTES,
        )

    def resolve(
        self, feed_id: str, secret: str, candidates: Iterable[FeedHousehold]
    ) -> uuid.UUID | None:
        """The household these credentials name, or ``None``.

        Both halves are compared, both in constant time, and the loop does not
        stop early. Stopping on the first identifier match would make the
        response time say "this identifier exists" -- the same oracle
        ``api/deps.py`` closed on ``X-Household-Id``.

        The shape check comes first and returns rather than raises: a caller that
        has already run :func:`looks_issued` loses nothing, and one that has not
        gets the same ``None`` it would get from a wrong guess instead of the
        ``TypeError`` ``hmac.compare_digest`` raises on non-ASCII text. It is
        placed before the loop on purpose -- it reads the request only, never a
        candidate, so it leaks nothing about which households exist.
        """
        if not looks_issued(feed_id, secret):
            return None
        found: uuid.UUID | None = None
        for seen, household in enumerate(candidates, start=1):
            if seen > MAX_RESOLVABLE_HOUSEHOLDS:
                raise TooManyHouseholdsError(
                    f"more than {MAX_RESOLVABLE_HOUSEHOLDS} households: the calendar feed "
                    f"resolves credentials by scanning and needs a persisted identifier "
                    f"before an instance this size"
                )
            expected = self.credentials_for(household)
            matches = hmac.compare_digest(expected.feed_id, feed_id)
            matches &= hmac.compare_digest(expected.secret, secret)
            if matches:
                found = household.id
        return found

"""The feed credential: what it must be, and what it must not reveal.

These are the properties the whole design rests on, so they are asserted rather
than assumed. No database: the derivation is pure.
"""

from __future__ import annotations

import base64
import re
import uuid

import pytest
from pydantic import SecretStr

from chaudron.config import Settings
from chaudron.infra.calendar.credentials import (
    FEED_ID_LENGTH,
    FEED_SECRET_LENGTH,
    INITIAL_FEED_EPOCH,
    MAX_RESOLVABLE_HOUSEHOLDS,
    FeedHousehold,
    FeedKeyring,
    TooManyHouseholdsError,
    looks_issued,
)

_BASE32 = re.compile(r"\A[A-Z2-7]+\Z")


def settings_with(secret: str = "x" * 40, epoch: str = "1") -> Settings:
    return Settings(
        env="ci",
        database_url=SecretStr("postgresql+asyncpg://u:p@localhost/db"),
        secret_key=SecretStr(secret),
        credential_encryption_key=SecretStr(base64.b64encode(b"0" * 32).decode()),
        calendar_feed_epoch=epoch,
    )


def keyring(**kwargs: str) -> FeedKeyring:
    return FeedKeyring.from_settings(settings_with(**kwargs))


def home(feed_epoch: int = INITIAL_FEED_EPOCH) -> FeedHousehold:
    """One household, at the epoch a freshly migrated row carries."""
    return FeedHousehold(id=uuid.uuid7(), feed_epoch=feed_epoch)


def test_credentials_are_stable_for_a_household() -> None:
    """A phone stores the pair once and replays it forever; it cannot drift."""
    household = home()
    assert keyring().credentials_for(household) == keyring().credentials_for(household)


def test_credentials_are_typeable_base32() -> None:
    credentials = keyring().credentials_for(home())
    assert len(credentials.feed_id) == FEED_ID_LENGTH
    assert len(credentials.secret) == FEED_SECRET_LENGTH
    # Upper case matters: an iOS user name field auto-capitalises, and a value
    # that is already upper case survives that untouched.
    assert _BASE32.match(credentials.feed_id)
    assert _BASE32.match(credentials.secret)


def test_the_identifier_does_not_contain_the_household() -> None:
    """The URL must not leak what ``X-Household-Id`` would grant.

    This is the property that keeps a feed URL in a proxy log from becoming full
    read and write access to the account (audit AUD-001).
    """
    household = home()
    credentials = keyring().credentials_for(household)
    for spelling in (str(household.id), household.id.hex, household.id.hex.upper()):
        assert spelling not in credentials.feed_id
        assert spelling not in credentials.secret


def test_identifier_and_secret_are_independent() -> None:
    """Holding the URL must say nothing about the password."""
    credentials = keyring().credentials_for(home())
    assert credentials.feed_id != credentials.secret
    assert credentials.secret[: len(credentials.feed_id)] != credentials.feed_id


def test_two_households_never_collide() -> None:
    generated = {keyring().credentials_for(home()).feed_id for _ in range(200)}
    assert len(generated) == 200


def test_bumping_the_epoch_revokes_every_credential() -> None:
    """The rotation lever: same instance key, all feeds invalidated."""
    household = home()
    before = keyring(epoch="1").credentials_for(household)
    after = keyring(epoch="2").credentials_for(household)
    assert before.feed_id != after.feed_id
    assert before.secret != after.secret


def test_rotating_the_instance_key_revokes_every_credential() -> None:
    household = home()
    assert keyring(secret="a" * 40).credentials_for(household) != keyring(
        secret="b" * 40
    ).credentials_for(household)


def test_bumping_one_households_epoch_moves_the_whole_pair() -> None:
    """The per-household lever: one row changes, one credential dies.

    Both halves move, identifier included. Rotating the secret alone would leave
    the old URL answering ``401`` instead of ``404`` -- which tells whoever held
    the revoked credential that the feed is still there and merely changed its
    password.
    """
    ring = keyring()
    before = ring.credentials_for(FeedHousehold(id=(shared := uuid.uuid7()), feed_epoch=1))
    after = ring.credentials_for(FeedHousehold(id=shared, feed_epoch=2))
    assert before.feed_id != after.feed_id
    assert before.secret != after.secret


def test_one_households_revocation_leaves_every_other_alone() -> None:
    """The property that makes the column worth a migration."""
    ring = keyring()
    neighbour = home()
    revoked = home()
    before = ring.credentials_for(neighbour)
    after = ring.credentials_for(FeedHousehold(id=revoked.id, feed_epoch=revoked.feed_epoch + 1))
    assert ring.credentials_for(neighbour) == before
    assert after != ring.credentials_for(revoked)


def test_the_epoch_cannot_be_swallowed_by_the_household_bytes() -> None:
    """Fixed-width serialisation: no two pairs may spell the same MAC input.

    A minimal-width integer glued to a UUID would let a payload be read two ways,
    which is the ambiguity the ``\\x00``-separated purpose labels avoid in the
    other direction.
    """
    ring = keyring()
    pairs = [FeedHousehold(id=uuid.uuid7(), feed_epoch=epoch) for epoch in range(1, 300)]
    assert len({ring.credentials_for(pair).feed_id for pair in pairs}) == len(pairs)


@pytest.mark.parametrize("epoch", [0, -1, 2**64])
def test_an_impossible_epoch_is_refused_rather_than_serialised(epoch: int) -> None:
    """``ValueError``, not an ``OverflowError`` escaping an authentication path."""
    with pytest.raises(ValueError, match="feed epoch out of range"):
        keyring().credentials_for(FeedHousehold(id=uuid.uuid7(), feed_epoch=epoch))


def test_resolution_finds_the_right_household() -> None:
    ring = keyring()
    households = [home() for _ in range(5)]
    wanted = households[3]
    credentials = ring.credentials_for(wanted)
    assert ring.resolve(credentials.feed_id, credentials.secret, households) == wanted.id


def test_a_wrong_secret_resolves_to_nothing() -> None:
    """The identifier alone is not a credential -- that is the whole point."""
    ring = keyring()
    household = home()
    credentials = ring.credentials_for(household)
    other = ring.credentials_for(home())
    assert ring.resolve(credentials.feed_id, other.secret, [household]) is None


def test_a_credential_from_another_epoch_resolves_to_nothing() -> None:
    household = home()
    stale = keyring(epoch="1").credentials_for(household)
    assert keyring(epoch="2").resolve(stale.feed_id, stale.secret, [household]) is None


def test_a_credential_from_before_a_revocation_resolves_to_nothing() -> None:
    """What the pentest replayed: the former member's credential, after revocation.

    The household is the same row and the instance key has not moved; only the
    counter has. That is the whole of the revocation, and it is enough.
    """
    ring = keyring()
    household = home()
    stale = ring.credentials_for(household)
    revoked = FeedHousehold(id=household.id, feed_epoch=household.feed_epoch + 1)
    assert ring.resolve(stale.feed_id, stale.secret, [revoked]) is None
    fresh = ring.credentials_for(revoked)
    assert ring.resolve(fresh.feed_id, fresh.secret, [revoked]) == household.id


def test_an_unknown_household_resolves_to_nothing() -> None:
    ring = keyring()
    credentials = ring.credentials_for(home())
    assert ring.resolve(credentials.feed_id, credentials.secret, [home()]) is None


@pytest.mark.parametrize(
    ("feed_id", "secret"),
    [
        # The one that raised: twenty-six characters, none of them ASCII.
        ("é" * FEED_ID_LENGTH, "é" * FEED_SECRET_LENGTH),
        ("A" * FEED_ID_LENGTH, "é" * FEED_SECRET_LENGTH),
        # Base32 has no 0, 1, 8 or 9, and no lower case.
        ("0" * FEED_ID_LENGTH, "B" * FEED_SECRET_LENGTH),
        ("a" * FEED_ID_LENGTH, "B" * FEED_SECRET_LENGTH),
        ("A" * (FEED_ID_LENGTH - 1), "B" * FEED_SECRET_LENGTH),
        ("A" * FEED_ID_LENGTH, "B" * (FEED_SECRET_LENGTH + 1)),
    ],
)
def test_a_credential_outside_the_alphabet_is_refused_not_raised(feed_id: str, secret: str) -> None:
    """``hmac.compare_digest`` raises on non-ASCII text; resolution must not.

    Reached from an unauthenticated request, so the difference between refusing
    and raising is the difference between a ``401`` and a ``500`` with a stack
    trace, repeatable for as long as somebody cares to send it.
    """
    assert not looks_issued(feed_id, secret)
    assert keyring().resolve(feed_id, secret, [home()]) is None


def test_a_credential_this_server_issued_is_well_formed() -> None:
    """The counterweight: the guard must not refuse what the derivation produces."""
    credentials = keyring().credentials_for(home())
    assert looks_issued(credentials.feed_id, credentials.secret)


def test_the_scan_refuses_rather_than_degrades() -> None:
    """Past the bound, the answer is "persist an identifier", not a slower request."""
    ring = keyring()
    too_many = (home() for _ in range(MAX_RESOLVABLE_HOUSEHOLDS + 1))
    with pytest.raises(TooManyHouseholdsError):
        ring.resolve("A" * FEED_ID_LENGTH, "B" * FEED_SECRET_LENGTH, too_many)


def test_resource_names_are_opaque_and_scoped() -> None:
    """A task's file name must not put a lot's UUIDv7 in an access log."""
    ring = keyring()
    household_a, household_b = uuid.uuid7(), uuid.uuid7()
    lot = uuid.uuid7()
    name = ring.resource_name(household_a, lot)
    assert str(lot) not in name
    assert lot.hex not in name
    assert name == ring.resource_name(household_a, lot)
    assert name != ring.resource_name(household_b, lot)


def test_the_secret_is_not_in_the_repr() -> None:
    """A dataclass that prints its own secret ends up in a traceback."""
    printed = repr(keyring().credentials_for(FeedHousehold(id=uuid.uuid7())))
    assert "redacted" in printed
    assert keyring().credentials_for(FeedHousehold(id=uuid.uuid7())).secret not in printed

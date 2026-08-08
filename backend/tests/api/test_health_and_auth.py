"""Health probes, and the household *selector* now that a session decides access."""

from __future__ import annotations

import uuid

import httpx

from tests.conftest import MakeHousehold, household_headers


async def test_healthz_answers_without_touching_the_database(
    anonymous_client: httpx.AsyncClient,
) -> None:
    response = await anonymous_client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_reports_the_database(anonymous_client: httpx.AsyncClient) -> None:
    response = await anonymous_client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == "ok"


async def test_a_request_with_no_session_is_rejected(anonymous_client: httpx.AsyncClient) -> None:
    """No cookie, no data. This is the whole of audit finding AUD-001."""
    response = await anonymous_client.get("/v1/locations")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == 401
    assert body["type"].endswith("/authentication-required")


async def test_a_household_header_alone_no_longer_opens_anything(
    anonymous_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """The regression test for the finding: knowing the UUID must not be enough.

    The household exists, the identifier is exact and correctly formed, and it is
    handed over in the header that used to be the entire access control. The
    answer is ``401`` because there is no session -- not ``403``, because the
    caller has not even got as far as being somebody.
    """
    household = await make_household()
    response = await anonymous_client.get("/v1/locations", headers=household_headers(household))
    assert response.status_code == 401
    assert response.json()["type"].endswith("/authentication-required")


async def test_a_signed_in_caller_reaches_their_own_household(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household()
    response = await api_client.get("/v1/locations", headers=household_headers(household))
    assert response.status_code == 200


async def test_a_signed_in_caller_needs_no_header_with_one_membership(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """The common case: one household, so the client never has to name it."""
    await make_household()
    response = await api_client.get("/v1/locations")
    assert response.status_code == 200


async def test_an_unknown_household_is_refused_like_someone_elses(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """A UUID nobody owns and a UUID somebody else owns give the same answer.

    Byte for byte, because the check is a lookup in the caller's own membership
    list: neither branch consults the database, so there is nothing left that
    could tell the two apart.
    """
    await make_household()
    stranger = await make_household(member=False)

    unknown = await api_client.get("/v1/locations", headers={"X-Household-Id": str(uuid.uuid7())})
    other = await api_client.get("/v1/locations", headers=household_headers(stranger))

    assert unknown.status_code == other.status_code == 403
    assert unknown.json()["type"].endswith("/household-forbidden")
    # Everything but the per-request incident identifier, which is meant to differ.
    assert _without_request_id(unknown) == _without_request_id(other)


def _without_request_id(response: httpx.Response) -> dict[str, object]:
    body = dict(response.json())
    body.pop("request_id", None)
    return body


async def test_a_malformed_household_header_is_refused(api_client: httpx.AsyncClient) -> None:
    response = await api_client.get("/v1/locations", headers={"X-Household-Id": "not-a-uuid"})
    assert response.status_code == 403


async def test_every_response_carries_a_request_id(anonymous_client: httpx.AsyncClient) -> None:
    """The identifier that ties an opaque error to the log line explaining it."""
    response = await anonymous_client.get("/healthz")
    assert response.headers["X-Request-Id"]

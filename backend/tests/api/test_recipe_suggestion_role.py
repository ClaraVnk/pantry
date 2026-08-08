"""``POST /v1/recipes/suggest`` is a write, and a viewer may not call it.

The route census exempted it with a sentence -- "a POST that writes nothing: it
reads the stock and returns suggestions" -- that was simply not true of the code.
``services/recipes.py`` adds a ``recipe_suggestion`` row on every call, carrying
a stock snapshot, the provider mode, token counts and a latency. Two further
things happen on the way: the household's inference budget is spent (its own key
under ``byok``, the operator's under ``instance_owner``), and the prompt sends
the infant-texture signal of whichever members the caller names to a third-party
provider -- health data, chosen by the caller, leaving the instance.

The guard is **latent rather than live**, and this file says so on purpose so
that nobody re-opens the question on the strength of a severity: no route mints a
``viewer`` today. Registration creates an ``owner``, and ``POST /v1/members``
creates an eater rather than a membership. The role is reachable only through
SQL, which is what the fixture below does. It becomes live the day an invite
route lands, which is exactly when nobody will re-read this endpoint.

``tests/api/test_route_authentication.py`` asserts that the guard is *present* in
the dependency graph. The two tests here are the halves that census cannot state:
what a viewer actually gets back, and *where in the graph* the guard sits --
because on this route, unusually, its position is part of what it is for.
"""

from __future__ import annotations

import base64
from collections.abc import Iterable, Iterator
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute
from pydantic import SecretStr
from starlette.routing import BaseRoute

from chaudron.api.deps import enforce_recipe_limits, require_member
from chaudron.api.main import create_app
from chaudron.config import Settings
from chaudron.domain.models import MembershipRole
from tests.api.test_recipes import SUGGEST_URL
from tests.conftest import MakeHousehold, household_headers

_MINIMAL_REQUEST: dict[str, object] = {"max_suggestions": 1}


@pytest.mark.integration
async def test_a_viewer_may_not_ask_for_suggestions(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household(role=MembershipRole.VIEWER)

    response = await api_client.post(
        SUGGEST_URL, json=_MINIMAL_REQUEST, headers=household_headers(household)
    )

    assert response.status_code == 403, response.text
    assert response.json()["type"].endswith("/insufficient-role")


def _suggest_route() -> APIRoute:
    """The route object, from an application built out of literals.

    No database is dialled: the settings below only have to satisfy validation,
    and nothing here sends a request.
    """
    app = create_app(
        Settings(
            env="ci",
            log_level="WARNING",
            database_url=SecretStr("postgresql+asyncpg://u:p@localhost/does-not-connect"),
            secret_key=SecretStr("k" * 48),
            credential_encryption_key=SecretStr(base64.b64encode(b"0" * 32).decode()),
        )
    )
    matches = [
        route
        for route in _walk(app.routes)
        if route.path == SUGGEST_URL and "POST" in (route.methods or ())
    ]
    (route,) = matches
    return route


def _walk(routes: Iterable[BaseRoute]) -> Iterator[APIRoute]:
    """Every endpoint, descending through the containers ``app.routes`` holds.

    ``include_router`` does not copy a router's routes onto the application: it
    wraps the router in an opaque node carrying the prefix it was mounted under.
    A flat read of ``app.routes`` therefore finds a dozen containers and **zero**
    endpoints -- a shape that makes a census pass while checking nothing, which
    is what ``tests/api/test_route_authentication.py`` documents having happened
    to it. The unpacking in :func:`_suggest_route` is what fails here instead.
    """
    for route in routes:
        included: Any = getattr(route, "original_router", None)
        if included is not None:
            yield from _walk(included.routes)
            continue
        if isinstance(route, APIRoute):
            yield route


def test_the_role_is_checked_before_the_rate_budget_is_spent() -> None:
    """Otherwise a viewer could exhaust what they are not allowed to use.

    The rate limiter and the role guard are both declared on the route decorator,
    and which of the two runs first is decided by nothing more visible than the
    order they are written in: FastAPI inserts a decorator's ``dependencies`` at
    the head of the dependant, in order and ahead of everything the signature
    asks for, and solves that list front to back. Get it the wrong way round and
    a viewer still gets their ``403`` -- after the household has been charged a
    suggestion of its budget for a call that was never going to happen.

    Asserted on the graph rather than through two hundred HTTP calls because the
    property is a property of the declaration, and reading it here says which
    edit broke it.
    """
    calls = [dependency.call for dependency in _suggest_route().dependant.dependencies]

    assert require_member in calls, "the suggestion endpoint lost its role guard"
    assert enforce_recipe_limits in calls, "the suggestion endpoint lost its rate limiter"
    assert calls.index(require_member) < calls.index(enforce_recipe_limits), (
        "the rate limiter is solved before the role guard, so a viewer's refused call "
        "still spends the household's suggestion budget. Reorder the `dependencies=[...]` "
        "list on the route: FastAPI solves it in the order given."
    )

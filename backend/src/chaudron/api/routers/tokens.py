"""``/v1/tokens`` -- the credentials a program uses. Contract v1.1 section 10.

Three routes, and the interesting property is what they all have in common:
**every one of them requires a browser session, and none of them is reachable
with a machine token.** That is not a policy written down somewhere and checked
here; it falls out of the dependencies. Each handler asks for a
:class:`~chaudron.services.auth.Principal`, only a cookie can produce one
(``api/deps.py``), and none of the three declares a scope -- so
:func:`~chaudron.api.deps.resolve_caller` refuses a bearer token before the
handler is reached at all.

The reason is the one failure this whole design exists to prevent. If a token
could mint tokens, a single leaked value would regenerate itself faster than an
owner could revoke, and "revoke it" would stop being an answer. The same applies
to ``DELETE``: a compromised token that could revoke *other* tokens would be able
to cut the owner's own integrations off while keeping its own.

**The value is returned once.** ``POST`` is the only response in the API that
carries a token, and there is no route that reads one back. A credential a
screenshot can steal is a credential the household cannot reason about, so after
this response the API knows the prefix, the last four characters and nothing
else. If it is lost, it is re-issued -- which is cheap, and revoking the old one
is the gesture that should follow anyway.

**A member may issue one, not only an owner.** ``GET /v1/calendar/subscription``
is owner-only because the credential it hands out keeps working after the
membership behind it is revoked. That is not true here: the resolver joins back
to ``household_member`` on every request (migration ``0011``), so a token grants
no more than the person who issued it still has, and it dies with their
membership. The narrower rule would cost a flatmate their integration and buy
nothing.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response, status

from chaudron.api.deps import (
    HouseholdDep,
    MachineTokenServiceDep,
    MemberDep,
    PrincipalDep,
    require_session,
)
from chaudron.api.errors import ProblemError
from chaudron.api.schemas import MachineTokenCreatedOut, MachineTokenCreateIn, MachineTokenOut
from chaudron.services.tokens import (
    ExpiryOutOfRangeError,
    IssuedMachineToken,
    MachineTokenSummary,
    NoScopesError,
    TooManyTokensError,
)

#: Declared on the router, so it applies to every route in this module and to
#: every route added to it later. The two handlers below that do not need the
#: account still need this: without it a machine token could list the household's
#: other tokens -- a map of what else to steal and which integration to
#: impersonate -- or revoke them.
router = APIRouter(prefix="/v1/tokens", tags=["tokens"], dependencies=[Depends(require_session)])


@router.get(
    "", response_model=list[MachineTokenOut], summary="The machine tokens of this household"
)
async def list_tokens(
    household_id: HouseholdDep, service: MachineTokenServiceDep
) -> list[MachineTokenOut]:
    """Every live token, with no secret in the response."""
    return [_to_out(summary) for summary in await service.list_for_household(household_id)]


@router.post(
    "",
    response_model=MachineTokenCreatedOut,
    status_code=status.HTTP_201_CREATED,
    summary="Issue a machine token, shown once",
)
async def create_token(
    household_id: MemberDep,
    principal: PrincipalDep,
    service: MachineTokenServiceDep,
    payload: MachineTokenCreateIn,
) -> MachineTokenCreatedOut:
    """Mint a token for the resolved household. **The only response with a value.**

    The household comes from ``HouseholdDep`` -- the session's own membership, or
    the ``X-Household-Id`` selector checked against it -- and is written onto the
    row. From here on the token names that household and cannot be pointed at
    another.
    """
    try:
        issued = await service.issue(
            household_id=household_id,
            user_id=principal.user_id,
            name=payload.name,
            scopes=payload.scopes,
            expires_in_days=payload.expires_in_days,
        )
    except NoScopesError as exc:
        raise ProblemError(
            slug="token-without-scope",
            title="A token needs at least one scope",
            status=422,
            detail=str(exc),
        ) from None
    except ExpiryOutOfRangeError as exc:
        raise ProblemError(
            slug="token-expiry-out-of-range",
            title="Expiry out of range",
            status=422,
            detail=str(exc),
        ) from None
    except TooManyTokensError as exc:
        raise ProblemError(
            slug="token-limit-reached",
            title="Too many machine tokens",
            status=409,
            detail=(
                f"This household already holds {exc.limit} machine tokens. Revoke one "
                f"that is no longer used before issuing another."
            ),
        ) from None

    return _to_created(issued)


@router.delete(
    "/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a machine token, immediately",
)
async def revoke_token(
    token_id: uuid.UUID, household_id: MemberDep, service: MachineTokenServiceDep
) -> Response:
    """Cut the token off now. The next request carrying it is a stranger.

    ``204`` whether or not a row was there to revoke. The alternative -- ``404``
    for an identifier this household does not hold -- would answer "does this
    token exist?" for any identifier a caller cares to try, and the answer to
    "revoke this" is the same either way: it is not usable any more.
    """
    await service.revoke(household_id=household_id, token_id=token_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _to_out(summary: MachineTokenSummary) -> MachineTokenOut:
    return MachineTokenOut(
        id=str(summary.id),
        name=summary.name,
        scopes=[scope.value for scope in summary.scopes],
        prefix=summary.prefix,
        last4=summary.last4,
        created_at=summary.created_at,
        last_used_at=summary.last_used_at,
        expires_at=summary.expires_at,
    )


def _to_created(issued: IssuedMachineToken) -> MachineTokenCreatedOut:
    summary = issued.summary
    return MachineTokenCreatedOut(
        id=str(summary.id),
        name=summary.name,
        scopes=[scope.value for scope in summary.scopes],
        prefix=summary.prefix,
        last4=summary.last4,
        created_at=summary.created_at,
        last_used_at=summary.last_used_at,
        expires_at=summary.expires_at,
        token=issued.token,
    )

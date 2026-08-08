"""RFC 9457 problem details, and the mapping from domain failures onto them.

One rule governs everything here: **no response ever carries a traceback, a SQL
fragment, or a provider credential** -- including in ``detail``. An unexpected
fault becomes an opaque 500 with a request identifier, and the actual cause goes
to the log, where the operator can read it and the internet cannot.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from chaudron.domain.ports import (
    BarcodeNotFoundError,
    ConstraintViolationDetectedError,
    DomainError,
    ExpiryDateInconsistentError,
    HouseholdNotFoundError,
    InfantTextureInconsistentError,
    InvalidBarcodeError,
    InvalidQuantityError,
    InventoryConflictError,
    InventoryItemNotFoundError,
    LocationNameTakenError,
    LocationNotFoundError,
    MemberNotFoundError,
    MemberNotInHouseholdError,
    NoSuggestionWithinConstraintsError,
    ProductCatalogUnavailableError,
    ProductNotFoundError,
    RecipeSuggestionNotFoundError,
    RetailerInternalBarcodeError,
    UnknownUnitError,
    UnsupportedRemovalReasonError,
    display_gtin,
)
from chaudron.infra.logging import request_id_var

logger = logging.getLogger(__name__)

PROBLEM_BASE_URI: Final = "https://chaudron.dev/problems/"
PROBLEM_CONTENT_TYPE: Final = "application/problem+json"

#: One sentence for "no header", "malformed header" and "unknown household"
#: alike. Three distinguishable answers would let a caller confirm a household
#: identifier without any observable side effect -- an oracle the comment in
#: ``deps.py`` claimed to have closed while the code kept it open (audit
#: AUD-013). The wording says nothing about which of the three happened.
HOUSEHOLD_NOT_RESOLVED_DETAIL: Final = "The X-Household-Id header is missing or invalid."


class ProblemError(Exception):
    """An error that is safe to show a client, in the shape the contract fixes."""

    def __init__(
        self,
        *,
        slug: str,
        title: str,
        status: int,
        detail: str | None = None,
        headers: dict[str, str] | None = None,
        **extensions: Any,
    ) -> None:
        super().__init__(title)
        self.slug = slug
        self.title = title
        self.status = status
        self.detail = detail
        self.headers = headers
        self.extensions = extensions

    def to_response(self) -> JSONResponse:
        body: dict[str, Any] = {
            "type": f"{PROBLEM_BASE_URI}{self.slug}",
            "title": self.title,
            "status": self.status,
        }
        if self.detail is not None:
            body["detail"] = self.detail
        body.update(self.extensions)
        request_id = request_id_var.get()
        if request_id is not None:
            body["request_id"] = request_id
        return JSONResponse(
            body,
            status_code=self.status,
            headers=self.headers,
            media_type=PROBLEM_CONTENT_TYPE,
        )


class RequestBodyTooLarge(StarletteHTTPException):
    """Raised mid-read when a request body passes the configured bound.

    An :class:`HTTPException` subclass rather than a :class:`ProblemError`
    because of where it is raised: inside the ASGI ``receive`` callable, whose
    caller is FastAPI's body reader. That reader re-raises ``HTTPException``
    unchanged and rewrites every other exception into a generic ``400 There was
    an error parsing the body`` -- which would report a refused 50 MB upload as a
    malformed one. The registered handler turns it back into the RFC 9457 shape
    every other error in this application uses.
    """

    def __init__(self, limit_bytes: int) -> None:
        super().__init__(status_code=413, detail="Request body too large")
        self.limit_bytes = limit_bytes


def unauthorized(detail: str = HOUSEHOLD_NOT_RESOLVED_DETAIL) -> ProblemError:
    return ProblemError(
        slug="household-not-resolved",
        title="Household not resolved",
        status=401,
        detail=detail,
    )


#: One sentence for "no cookie", "unknown session", "expired", "idle-expired",
#: "revoked" and "the account was disabled". Six distinguishable answers would
#: tell a caller holding a stale cookie which of the six happened, and telling a
#: stranger that a session *existed* is already more than they had.
NOT_AUTHENTICATED_DETAIL: Final = "Authentication is required. Sign in and try again."


def authentication_required() -> ProblemError:
    """``401``: there is no live session behind this request."""
    return ProblemError(
        slug="authentication-required",
        title="Authentication required",
        status=401,
        detail=NOT_AUTHENTICATED_DETAIL,
    )


#: One sentence for "no such token", "revoked", "expired", "issued by an account
#: since disabled", "issued by somebody no longer a member of that household" and
#: "the household was archived". The six are one branch in
#: ``chaudron_resolve_machine_token`` (migration ``0011``), so they are
#: indistinguishable by *timing* as well as by wording -- a Python post-filter
#: would have found the row first, and the difference between "found then
#: rejected" and "not found" is measurable.
TOKEN_NOT_ACCEPTED_DETAIL: Final = (
    # The suppression below is on a message, not on a credential: this constant
    # holds one sentence shown to a client, and S105 looks only at the name.
    "This access token is not valid for this request. It may have been revoked or "  # noqa: S105
    "have expired; issue a new one from the household settings."
)


def token_not_accepted() -> ProblemError:
    """``401``: a bearer token was presented and is not usable here.

    Also the answer when a token is presented to a route that declares no scopes
    at all -- creating or revoking a token, reading the household's members,
    asking for a recipe suggestion. Those routes are not "forbidden to this
    token", they are **closed to every token**, and saying so with the same 401
    as an unknown value keeps a holder from learning which of the two it hit.

    ``WWW-Authenticate`` is set because RFC 9110 requires it on a 401 and because
    a well-behaved client uses it to know it should stop retrying with the same
    credential.
    """
    return ProblemError(
        slug="token-not-accepted",
        title="Access token not accepted",
        status=401,
        detail=TOKEN_NOT_ACCEPTED_DETAIL,
        headers={"WWW-Authenticate": 'Bearer realm="chaudron"'},
    )


def insufficient_scope(required: list[str], granted: list[str]) -> ProblemError:
    """``403``: the token is valid, and was not issued for this.

    The one place a token holder is told something specific, and deliberately so:
    the caller already holds the token, so ``granted`` tells them nothing they
    could not read from ``GET /v1/tokens``, and ``required`` is what turns "403"
    into "re-issue it with ``inventory:write``". Withholding it here would buy no
    secrecy and cost every integrator an hour.

    Distinct from :func:`token_not_accepted` on purpose. ``401`` means "this
    credential is not usable"; ``403`` means "it is, and it does not cover this".
    Collapsing the two would tell somebody holding a revoked token that their
    scopes were wrong, and send them to re-issue rather than to look at the
    revocation.
    """
    return ProblemError(
        slug="insufficient-scope",
        title="Insufficient token scope",
        status=403,
        detail=(
            "This access token was not issued with the scope this request needs. "
            "Create a new token with the missing scope."
        ),
        headers={
            "WWW-Authenticate": (
                f'Bearer realm="chaudron", error="insufficient_scope", scope="{" ".join(required)}"'
            )
        },
        required_scopes=required,
        granted_scopes=granted,
    )


def invalid_credentials() -> ProblemError:
    """``401`` on sign-in, identical for an unknown address and a wrong password.

    The bodies match byte for byte and the *timings* match too -- the service
    burns one Argon2 verification whether or not an account was found
    (``services/auth.py``). Either half alone leaves the endpoint an enumerator.
    """
    return ProblemError(
        slug="invalid-credentials",
        title="Invalid credentials",
        status=401,
        detail="That email address and password do not match an account.",
    )


def household_forbidden() -> ProblemError:
    """``403``: authenticated, but not a member of the household named.

    Deliberately the same answer whether the household does not exist or simply
    is not this account's. The check runs against the caller's own membership
    list, so the two cases are not merely reported alike -- they are literally
    the same branch, and no future edit can pull them apart by accident.
    """
    return ProblemError(
        slug="household-forbidden",
        title="Household not accessible",
        status=403,
        detail="This account is not a member of the household requested.",
    )


def insufficient_role(required: str, held: str) -> ProblemError:
    """``403``: a member of the household, and not one who may do this.

    Distinct from :func:`household_forbidden`, and the difference is deliberate.
    "Not a member" must stay indistinguishable from "no such household", because
    telling the two apart is an existence oracle on somebody else's home. "A
    member, with the wrong role" discloses nothing the caller does not already
    know: they can read their own role from ``GET /v1/auth/session``, which lists
    it for every household they belong to. Saying so turns a dead end into "ask an
    owner", which is the only action available.

    ``held`` is echoed for the same reason ``granted`` is echoed on
    :func:`insufficient_scope`: withholding a value the caller can already read
    buys no secrecy and costs an afternoon.
    """
    return ProblemError(
        slug="insufficient-role",
        title="Insufficient role",
        status=403,
        detail=(
            f"This account is a {held} of the household and this operation needs "
            f"at least {required}. Ask an owner of the household to perform it."
        ),
        required_role=required,
        held_role=held,
    )


def household_selection_required(households: list[dict[str, str]]) -> ProblemError:
    """``409``: several households, and the request named none of them.

    Listing them is not a disclosure: they are the caller's own memberships, and
    the same list is served by ``GET /v1/auth/session``. It saves the client a
    second round trip at the exact moment it discovers it needs one.
    """
    return ProblemError(
        slug="household-selection-required",
        title="Household selection required",
        status=409,
        detail=(
            "This account belongs to several households. Name the one to use in the "
            "X-Household-Id header."
        ),
        households=households,
    )


def no_household() -> ProblemError:
    """``403``: authenticated, and a member of nothing.

    Reachable after an owner removes the last membership of an account that is
    still signed in. Not a ``404``: the account is fine, it simply has nothing to
    open.
    """
    return ProblemError(
        slug="no-household",
        title="No household",
        status=403,
        detail="This account does not belong to any household.",
    )


def csrf_token_invalid() -> ProblemError:
    """``403``: a state-changing request arrived without a valid CSRF token.

    The regression this closes is one introduced by authentication itself. While
    the API was authorised by ``X-Household-Id``, a custom header, cross-site
    forgery was impossible by construction -- a third-party form cannot set a
    header. A cookie is ambient authority: the browser attaches it to a request
    the user never made. ``SameSite=Lax`` covers most of it and not all of it
    (browsers that ignore the attribute, a same-site subdomain the operator does
    not control), so the token is checked explicitly on every unsafe method.
    """
    return ProblemError(
        slug="csrf-token-invalid",
        title="CSRF token missing or invalid",
        status=403,
        detail=(
            "This request changes state and carried no valid X-CSRF-Token header. "
            "Read the token from the session endpoint and send it back."
        ),
    )


def email_already_registered() -> ProblemError:
    """``409`` on registration. Knowingly an oracle; see ``services/auth.py``."""
    return ProblemError(
        slug="email-already-registered",
        title="Email already registered",
        status=409,
        detail="An account already exists for this email address.",
    )


def problem_for_body_too_large(limit_bytes: int) -> ProblemError:
    """``413``, quoting the bound so a client can act rather than guess."""
    return ProblemError(
        slug="request-body-too-large",
        title="Request body too large",
        status=413,
        detail=f"The request body exceeds the {limit_bytes} byte limit of this endpoint.",
        limit_bytes=limit_bytes,
    )


def rate_limited(*, detail: str, retry_after: int) -> ProblemError:
    """``429`` with the one header a client needs to behave: ``Retry-After``.

    The delay is measured by the limiter that refused, not invented here.
    """
    return ProblemError(
        slug="rate-limited",
        title="Too many requests",
        status=429,
        detail=detail,
        headers={"Retry-After": str(retry_after)},
        retry_after=retry_after,
    )


def problem_for(error: DomainError) -> ProblemError:
    """Translate a domain failure into its public form.

    A single mapping rather than per-route ``except`` blocks: a handler that
    forgets one becomes a 500, and a 500 on a knowable error is how a client
    ends up unable to tell "you asked for the wrong thing" from "we broke".
    """
    match error:
        case BarcodeNotFoundError():
            return ProblemError(
                slug="product-not-found",
                title="Product not found",
                status=404,
                detail=f"No product matches GTIN {display_gtin(error.gtin)}.",
                gtin=display_gtin(error.gtin),
            )
        case ProductNotFoundError():
            return ProblemError(
                slug="product-not-found",
                title="Product not found",
                status=404,
                detail="No product with this identifier is visible to this household.",
            )
        case LocationNotFoundError():
            return ProblemError(
                slug="location-not-found",
                title="Storage location not found",
                status=404,
                detail="No storage location with this identifier belongs to this household.",
            )
        case LocationNameTakenError():
            return ProblemError(
                slug="location-name-taken",
                title="Storage location already exists",
                status=409,
                detail=(
                    "This household already has an active storage location with that "
                    "name. Names are compared without regard to case."
                ),
            )
        case InventoryItemNotFoundError():
            return ProblemError(
                slug="inventory-item-not-found",
                title="Inventory item not found",
                status=404,
                detail="No inventory item with this identifier belongs to this household.",
            )
        case RetailerInternalBarcodeError():
            return ProblemError(
                slug="retailer-internal-barcode",
                title="Retailer-internal barcode",
                status=422,
                detail=(
                    "This barcode is a variable-weight in-store code; it encodes a price "
                    "and will never appear in a public product reference. Enter the "
                    "product manually."
                ),
                gtin=display_gtin(error.gtin),
            )
        case InvalidBarcodeError():
            return ProblemError(
                slug="invalid-barcode",
                title="Invalid barcode",
                status=422,
                detail="A barcode must be 8 to 14 digits.",
            )
        case ProductCatalogUnavailableError():
            return ProblemError(
                slug="product-catalog-unavailable",
                title="Product catalogue unavailable",
                status=503,
                detail=(
                    "Open Food Facts did not answer. Retry shortly, or enter the product manually."
                ),
                headers={"Retry-After": str(error.retry_after)},
            )
        case UnknownUnitError():
            return ProblemError(
                slug="unknown-unit",
                title="Unknown unit",
                status=422,
                detail=f"{error.code!r} is not a known measurement unit.",
            )
        case InvalidQuantityError():
            return ProblemError(
                slug="invalid-quantity",
                title="Invalid quantity",
                status=422,
                detail=str(error),
            )
        case ExpiryDateInconsistentError():
            return ProblemError(
                slug="expiry-date-inconsistent",
                title="Inconsistent expiry date",
                status=422,
                detail=str(error),
            )
        case UnsupportedRemovalReasonError():
            return ProblemError(
                slug="unsupported-removal-reason",
                title="Unsupported removal reason",
                status=422,
                detail="reason must be one of: consumed, wasted, correction.",
            )
        case InventoryConflictError():
            return ProblemError(
                slug="inventory-conflict",
                title="Concurrent inventory change",
                status=409,
                detail="Another change to the same lot won the race. Retry the request.",
            )
        # -- Dietary constraints (contract v1.1) ---------------------------- #
        #
        # Not one of the four quotes an allergen, a diet or an age band. A
        # problem body is read by a client that may log it, and the reason this
        # feature exists is that these facts have a physical cost when they are
        # wrong -- they have a social one when they are published.
        case MemberNotFoundError():
            return ProblemError(
                slug="member-not-found",
                title="Household member not found",
                status=404,
                detail="No member with this identifier belongs to this household.",
            )
        case MemberNotInHouseholdError():
            return ProblemError(
                slug="member-not-in-household",
                title="Member not in this household",
                status=422,
                detail=(
                    "One of the member identifiers does not belong to this household. "
                    "Suggestions are not generated for a partial selection."
                ),
            )
        case InfantTextureInconsistentError():
            return ProblemError(
                slug="infant-texture-inconsistent",
                title="Inconsistent infant texture",
                status=422,
                detail=(
                    "infant_texture is required for an infant age band and must be null "
                    "for any other."
                ),
            )
        case NoSuggestionWithinConstraintsError():
            # 409, and deliberately not an error page. The stock is fine, the
            # provider is fine; the two simply do not intersect today, and the
            # interface has to say which class of constraint emptied the
            # inventory instead of showing a failure.
            return ProblemError(
                slug="no-suggestion-within-constraints",
                title="No suggestion within these constraints",
                status=409,
                detail=(
                    "Every product in the selected stock was set aside by the dietary "
                    "constraints of the people you are cooking for. Nothing was "
                    "suggested rather than something unsafe."
                ),
                reasons=list(error.reasons),
                products_withheld=error.withheld,
            )
        case ConstraintViolationDetectedError():
            return ProblemError(
                slug="constraint-violation-detected",
                title="Suggestion failed the post-generation check",
                status=502,
                detail=(
                    "The model returned ingredients this application could not match "
                    "back to the stock it was given. The suggestions were discarded "
                    "whole rather than served with a caveat. Retry, or choose another "
                    "model."
                ),
            )
        case RecipeSuggestionNotFoundError():
            # Same body whether the suggestion never existed or belongs to another
            # household: two distinguishable answers would let a caller probe the
            # existence of somebody else's rows one identifier at a time.
            return ProblemError(
                slug="recipe-suggestion-not-found",
                title="Recipe suggestion not found",
                status=404,
                detail="No recipe suggestion with this identifier belongs to this household.",
            )
        case HouseholdNotFoundError():
            return unauthorized()
        case _:
            return ProblemError(
                slug="invalid-request",
                title="Invalid request",
                status=400,
                detail=str(error),
            )


def register_exception_handlers(app: FastAPI) -> None:
    """Install the handlers that make every error path answer in one shape."""

    async def handle_problem(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ProblemError)
        return exc.to_response()

    async def handle_domain_error(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, DomainError)
        return problem_for(exc).to_response()

    async def handle_validation_error(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        # `loc` and `msg` only: pydantic's `input` echoes the submitted value,
        # which on a credential-bearing body would put it in a client log.
        errors = [
            {"loc": [str(part) for part in error["loc"]], "msg": error["msg"]}
            for error in exc.errors()
        ]
        return ProblemError(
            slug="validation-failed",
            title="Request validation failed",
            status=422,
            detail="The request body or parameters did not match the expected shape.",
            errors=errors,
        ).to_response()

    async def handle_body_too_large(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestBodyTooLarge)
        return problem_for_body_too_large(exc.limit_bytes).to_response()

    async def handle_http_exception(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, StarletteHTTPException)
        return ProblemError(
            slug="http-error",
            title=str(exc.detail),
            status=exc.status_code,
            headers=dict(exc.headers) if exc.headers else None,
        ).to_response()

    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # The identifier is the only thing tying this opaque answer to the log
        # line that holds the traceback.
        incident = request_id_var.get() or str(uuid.uuid4())
        logger.exception(
            "unhandled_exception",
            extra={"path": request.url.path, "method": request.method, "incident": incident},
            exc_info=exc,
        )
        return ProblemError(
            slug="internal-error",
            title="Internal server error",
            status=500,
            detail="The request could not be processed. The incident has been logged.",
        ).to_response()

    app.add_exception_handler(ProblemError, handle_problem)
    app.add_exception_handler(DomainError, handle_domain_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    # Registered before its base class: Starlette resolves a handler by walking
    # the exception's MRO, so the specific one has to exist for it to be found.
    app.add_exception_handler(RequestBodyTooLarge, handle_body_too_large)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(Exception, handle_unexpected)

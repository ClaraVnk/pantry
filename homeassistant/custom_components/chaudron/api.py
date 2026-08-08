"""The HTTP client this integration speaks to a Chaudron instance with.

Deliberately hand-written rather than generated or vendored. The surface a
machine token can reach is five endpoints wide (``api/deps.py`` in the backend
grants exactly five scopes), the responses are flat JSON, and a dependency in
``manifest.json`` is a package Home Assistant has to resolve on every install of
every user. There is nothing here worth a wheel.

**Two properties are load-bearing.**

*The token never leaves this module.* It is held on the instance, written into
one header, and appears in no exception message, no ``repr`` and no log line.
The backend's own ``infra/redaction.py`` blanks anything that looks like a
bearer credential on its side; this side has no such net, so the rule is that
the value is never handed to something that formats.

*Every failure has a shape.* The backend answers RFC 9457 problem documents with
a stable ``type`` slug, so "the token was revoked" (401) and "the token is fine
and was not issued for this" (403 ``insufficient-scope``) are told apart here and
turned into two different behaviours upstream: re-authentication for the first,
a silently narrower set of entities for the second.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from http import HTTPStatus
from typing import Any, Final, Self

import aiohttp
from yarl import URL

from .const import (
    INVENTORY_PAGE_SIZE,
    MAX_INVENTORY_PAGES,
    PATH_BUDGET,
    PATH_INVENTORY,
    PATH_LOCATIONS,
    PATH_SHOPPING_ITEMS,
    PATH_SHOPPING_LIST,
    REQUEST_TIMEOUT_SECONDS,
)

#: The slug the backend puts in ``type`` when a valid token lacks the scope a
#: route declared (``api/errors.py``). Matched on the tail rather than the whole
#: URI so a change of problem base URI does not silently turn a 403 into a
#: generic failure.
_INSUFFICIENT_SCOPE_SLUG: Final = "insufficient-scope"

#: What a 429 falls back to when ``Retry-After`` is absent or unparseable. The
#: backend always sends it (``api/errors.py`` builds the header alongside the
#: body), so this covers a proxy in front that does not.
_DEFAULT_RETRY_AFTER: Final = 60

#: Nothing this integration sends is large, and the backend caps bodies anyway.
#: The header exists so an operator reading an access log can tell a Home
#: Assistant poll from a browser.
_USER_AGENT: Final = "HomeAssistant-Chaudron"


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #


class ChaudronError(Exception):
    """Base class. Carries no credential, by construction and by review."""


class ChaudronConnectionError(ChaudronError):
    """The instance could not be reached, or answered nothing parseable."""


class ChaudronAuthError(ChaudronError):
    """``401``: the token is unknown, revoked, expired, or its issuer is gone.

    All six causes are one branch in the backend (``chaudron_resolve_machine_token``),
    so there is nothing finer to report and the only useful action is the same
    for each: issue a new token.
    """


class ChaudronScopeError(ChaudronError):
    """``403``: the token is valid and was not issued for this route.

    ``granted`` is the scope list the backend echoes back, which is what lets the
    coordinator stop asking for a surface this token will never open instead of
    failing the whole refresh over it.
    """

    def __init__(self, required: list[str], granted: list[str]) -> None:
        """Record what the route wanted and what the token turned out to hold."""
        super().__init__(f"token lacks {', '.join(required) or 'a required scope'}")
        self.required = required
        self.granted = granted


class ChaudronRateLimitError(ChaudronError):
    """``429``: come back after ``retry_after`` seconds, and not before."""

    def __init__(self, retry_after: int) -> None:
        """Record the delay the instance asked for, in seconds."""
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


class ChaudronResponseError(ChaudronError):
    """Any other refusal. ``status`` is what the instance answered."""

    def __init__(self, status: int, detail: str | None = None) -> None:
        """Record the status, and the problem title when there was one."""
        super().__init__(detail or f"unexpected response {status}")
        self.status = status


# --------------------------------------------------------------------------- #
# What the endpoints return, as far as this integration cares
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class InventoryLot:
    """One lot of one product, as ``GET /v1/inventory`` renders it.

    ``expires_on`` is what is printed on the pack; ``effective_expires_on`` is
    what the backend computes once an opening date and a shelf-life guideline are
    taken into account. Every date-driven sensor here uses the second one, which
    is the whole reason the backend publishes both.
    """

    id: str
    product_name: str
    brand: str | None
    location_name: str | None
    quantity: str
    unit: str
    expires_on: date | None
    effective_expires_on: date | None

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Self:
        """Build one lot from an ``InventoryItemOut`` object."""
        product = payload.get("product") or {}
        location = payload.get("location") or {}
        quantity = payload.get("quantity") or {}
        return cls(
            id=str(payload["id"]),
            product_name=str(product.get("name") or ""),
            brand=_optional_str(product.get("brand")),
            location_name=_optional_str(location.get("name")),
            quantity=str(quantity.get("amount") or ""),
            unit=str(quantity.get("unit") or ""),
            expires_on=_optional_date(payload.get("expires_on")),
            effective_expires_on=_optional_date(payload.get("effective_expires_on")),
        )


@dataclass(frozen=True, slots=True)
class ExpiringPage:
    """The lots at or approaching their date, earliest first.

    ``total`` is the backend's count for the same filter, so it is right even when
    ``lots`` was cut short -- and ``truncated`` says which of the two happened.
    """

    total: int
    lots: tuple[InventoryLot, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class ShoppingItem:
    """One line of the household's current list."""

    id: str
    summary: str
    checked: bool
    quantity: str | None
    unit: str | None


@dataclass(frozen=True, slots=True)
class ShoppingList:
    """The household's current list, as ``ShoppingListOut`` renders it."""

    id: str
    name: str
    items: tuple[ShoppingItem, ...] = field(default_factory=tuple)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Self:
        """Build the list, and every line on it."""
        return cls(
            id=str(payload["id"]),
            name=str(payload.get("name") or ""),
            items=tuple(_shopping_item(raw) for raw in payload.get("items") or ()),
        )


@dataclass(frozen=True, slots=True)
class CurrencySpend:
    """What the household spent in one currency over the current period."""

    currency: str
    spent: str
    target: str | None
    receipt_count: int


@dataclass(frozen=True, slots=True)
class Budget:
    """One period of spend, as ``GET /v1/budget`` reports it."""

    period: str
    period_start: date | None
    period_end: date | None
    currencies: tuple[CurrencySpend, ...]
    receipts_missing_total: int

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Self:
        """Build the period, keeping only the figures a sensor displays."""
        coverage = payload.get("coverage") or {}
        return cls(
            period=str(payload.get("period") or "month"),
            period_start=_optional_date(payload.get("period_start")),
            period_end=_optional_date(payload.get("period_end")),
            currencies=tuple(
                CurrencySpend(
                    currency=str(raw.get("currency") or ""),
                    spent=str(raw.get("spent") or "0"),
                    target=_optional_str(raw.get("target")),
                    receipt_count=int(raw.get("receipt_count") or 0),
                )
                for raw in payload.get("currencies") or ()
                if raw.get("currency")
            ),
            receipts_missing_total=int(coverage.get("receipts_missing_total") or 0),
        )


# --------------------------------------------------------------------------- #
# The client
# --------------------------------------------------------------------------- #


class ChaudronClient:
    """Everything a machine token may ask a Chaudron instance for."""

    def __init__(
        self, session: aiohttp.ClientSession, base_url: str, token: str
    ) -> None:
        """Bind one instance and one credential to a shared HTTP session."""
        self._session = session
        self._base = URL(normalise_base_url(base_url))
        # Assembled once. Nothing else in this class ever reads `token` again,
        # which is what keeps it out of a formatted message by accident.
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        }

    def __repr__(self) -> str:
        """Without the header dictionary, which holds the credential."""
        return f"<ChaudronClient {self._base}>"

    @property
    def base_url(self) -> str:
        """The instance root, without a trailing slash."""
        return str(self._base)

    # -- Reads -------------------------------------------------------------- #

    async def async_verify(self) -> None:
        """Cheapest call that proves the token opens the inventory surface.

        ``GET /v1/locations`` returns a handful of rows and is guarded by
        ``inventory:read``, the one scope this integration cannot work without.
        Raises the same errors as any other call, so the config flow tells a
        revoked token from an under-scoped one without a second request.
        """
        await self._request("GET", PATH_LOCATIONS)

    async def async_stock_count(self) -> int:
        """How many lots the household holds, without transferring any of them."""
        payload = await self._request("GET", PATH_INVENTORY, params={"limit": 1})
        return int(payload.get("total") or 0)

    async def async_expiring(self, within_days: int) -> ExpiringPage:
        """The lots whose effective date falls on or before ``today + within_days``.

        Already-expired lots are part of the answer: the backend's filter is
        ``effective_expiry <= horizon`` with no floor, and separating them here
        rather than asking twice saves a request the household pays for.

        Paged, because the backend caps a page at 200 and the filter is not a
        guarantee of smallness -- a household coming back from holiday can have
        a whole shelf go over at once. It stops after
        :data:`~.const.MAX_INVENTORY_PAGES` and says so.
        """
        lots: list[InventoryLot] = []
        total = 0
        truncated = False
        for page in range(MAX_INVENTORY_PAGES):
            payload = await self._request(
                "GET",
                PATH_INVENTORY,
                params={
                    "expiring_within_days": within_days,
                    "limit": INVENTORY_PAGE_SIZE,
                    "offset": page * INVENTORY_PAGE_SIZE,
                },
            )
            total = int(payload.get("total") or 0)
            batch = [InventoryLot.from_json(raw) for raw in payload.get("items") or ()]
            lots.extend(batch)
            if len(lots) >= total or len(batch) < INVENTORY_PAGE_SIZE:
                break
        else:
            truncated = len(lots) < total
        return ExpiringPage(total=total, lots=tuple(lots), truncated=truncated)

    async def async_shopping_list(self) -> ShoppingList:
        """The household's current list.

        A ``GET`` that creates the list when the household has none -- the
        backend's own decision, documented on the route. Harmless to repeat.
        """
        return ShoppingList.from_json(await self._request("GET", PATH_SHOPPING_LIST))

    async def async_budget(self) -> Budget:
        """Spend over the calendar month containing today, per currency."""
        return Budget.from_json(
            await self._request("GET", PATH_BUDGET, params={"period": "month"})
        )

    # -- Writes, all on the shopping list ----------------------------------- #

    async def async_add_shopping_item(self, summary: str) -> None:
        """Add one free-text line.

        Free text rather than a catalogue product: Home Assistant hands over a
        string a person spoke or typed, and guessing which product it names is a
        decision that belongs in Chaudron's own interface, where the household can
        see and correct it.
        """
        await self._request(
            "POST",
            PATH_SHOPPING_ITEMS,
            json={"items": [{"free_text": summary, "source": "manual"}]},
        )

    async def async_set_shopping_item_checked(
        self, item_id: str, *, checked: bool
    ) -> None:
        """Tick or untick one line."""
        await self._request(
            "PATCH", f"{PATH_SHOPPING_ITEMS}/{item_id}", json={"checked": checked}
        )

    async def async_delete_shopping_item(self, item_id: str) -> None:
        """Remove one line from the list."""
        await self._request("DELETE", f"{PATH_SHOPPING_ITEMS}/{item_id}")

    # -- Transport ---------------------------------------------------------- #

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One call, with every documented refusal turned into a typed failure."""
        # Concatenated rather than :meth:`yarl.URL.join`, which resolves an
        # absolute path against the *authority* and would drop the prefix of an
        # instance published at ``https://home.example/chaudron``.
        url = URL(f"{self._base}{path}")
        try:
            async with self._session.request(
                method,
                url,
                params=params,
                json=json,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            ) as response:
                # Read before branching: a problem document is what carries the
                # granted scopes and the retry delay, and a 403 whose body was
                # never read is a 403 nobody can act on.
                body = await _json_or_empty(response)
                _raise_for_status(response, body)
                return body
        except aiohttp.ClientError as err:
            raise ChaudronConnectionError(f"{method} {path} failed") from err
        except TimeoutError as err:
            raise ChaudronConnectionError(f"{method} {path} timed out") from err


def _raise_for_status(response: aiohttp.ClientResponse, body: dict[str, Any]) -> None:
    """Translate a refusal. Never quotes the request, which carried the token."""
    status = response.status
    if status < HTTPStatus.BAD_REQUEST:
        return
    if status == HTTPStatus.UNAUTHORIZED:
        raise ChaudronAuthError("the instance refused this access token")
    if status == HTTPStatus.FORBIDDEN and str(body.get("type", "")).endswith(
        _INSUFFICIENT_SCOPE_SLUG
    ):
        raise ChaudronScopeError(
            required=[str(scope) for scope in body.get("required_scopes") or ()],
            granted=[str(scope) for scope in body.get("granted_scopes") or ()],
        )
    if status == HTTPStatus.TOO_MANY_REQUESTS:
        raise ChaudronRateLimitError(_retry_after(response))
    raise ChaudronResponseError(status, _optional_str(body.get("title")))


def _retry_after(response: aiohttp.ClientResponse) -> int:
    """Seconds from ``Retry-After``, floored at one and capped at an hour.

    The backend sends an integer count of seconds, computed from the token bucket
    that refused (``api/throttling.py``), so the HTTP-date form RFC 9110 also
    allows is not parsed: a value that is not a number falls back rather than
    guessing a clock skew.
    """
    raw = response.headers.get("Retry-After")
    try:
        seconds = int(str(raw).strip())
    except (TypeError, ValueError):
        return _DEFAULT_RETRY_AFTER
    return max(1, min(seconds, 3600))


async def _json_or_empty(response: aiohttp.ClientResponse) -> dict[str, Any]:
    """The body as a mapping, or an empty one.

    ``204`` has no body at all (revoking a shopping item), and a proxy answering
    a 502 sends HTML. Neither is worth an exception of its own: the status is
    what the caller branches on, and the body is only ever extra detail.
    """
    if response.status == HTTPStatus.NO_CONTENT:
        return {}
    try:
        payload = await response.json(content_type=None)
    except (aiohttp.ClientError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def normalise_base_url(raw: str) -> str:
    """``https://host/path`` with no trailing slash, or raise :class:`ValueError`.

    A scheme is required rather than assumed. Defaulting to ``https`` would be
    kind until the day it silently downgraded somebody's ``http://`` LAN
    instance into a connection error they could not read; defaulting to ``http``
    would send a bearer token in the clear.
    """
    candidate = raw.strip().rstrip("/")
    if not candidate:
        raise ValueError("the instance URL is empty")
    url = URL(candidate)
    if url.scheme not in ("http", "https") or not url.host:
        raise ValueError("the instance URL needs a scheme and a host")
    return candidate


def _shopping_item(payload: dict[str, Any]) -> ShoppingItem:
    """One list line, with the two ways it can be named collapsed into one.

    A line is either a catalogue product or free text -- the backend enforces
    exactly one -- and Home Assistant's to-do item has a single ``summary``.
    """
    quantity = payload.get("quantity") or {}
    return ShoppingItem(
        id=str(payload["id"]),
        summary=str(payload.get("product_name") or payload.get("free_text") or ""),
        checked=bool(payload.get("checked")),
        quantity=_optional_str(quantity.get("amount")),
        unit=_optional_str(quantity.get("unit")),
    )


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_date(value: Any) -> date | None:
    """An ISO date, or ``None`` for anything that is not one.

    Unparseable rather than absent is treated the same way on purpose: a lot with
    a date this client cannot read must not take a whole refresh down with it.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None

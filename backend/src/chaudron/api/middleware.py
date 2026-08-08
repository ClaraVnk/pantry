"""ASGI middleware: what every response carries, and what no request may weigh.

Both classes are raw ASGI rather than ``BaseHTTPMiddleware`` subclasses. That is
not a style preference: ``BaseHTTPMiddleware`` cannot refuse a body before it is
read, because it hands the endpoint a fully materialised :class:`Request`, and
refusing after the read is exactly the resource exhaustion the size limit exists
to prevent.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from chaudron.api.errors import RequestBodyTooLarge, problem_for_body_too_large

#: Everything under this prefix answers with one household's data.
PRIVATE_PATH_PREFIX = "/v1/"

#: One year, the value every preload list requires. Only emitted in production,
#: and only reaches a browser over TLS -- over plain HTTP it is ignored by
#: specification, so it cannot lock a developer out of a local instance.
HSTS_VALUE = "max-age=31536000; includeSubDomains"

#: A JSON body a browser was tricked into rendering as a document can do nothing:
#: no script, no image, no frame, no form, no ``<base>``. `frame-ancestors` also
#: closes clickjacking without the legacy ``X-Frame-Options`` header, which says
#: strictly less and applies to nothing this API serves.
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"

#: The one HTML document this application can serve is the development docs page,
#: and Swagger UI needs its own scripts and styles to work at all. Framing is the
#: only thing worth denying there; a stricter policy would just break the page.
HTML_CSP = "frame-ancestors 'none'"


class SecurityHeadersMiddleware:
    """Response headers that are correct for a JSON API consumed cross-origin.

    Each header earns its place; the ones a generic checklist would add and that
    are absent are absent on purpose.

    * ``X-Content-Type-Options: nosniff`` -- stops a browser re-typing a response
      from its bytes. The one header on this list whose omission has a concrete
      exploit path against an API.
    * ``Referrer-Policy: no-referrer`` -- API URLs carry inventory item and
      location identifiers in their paths. A direct navigation to one (a pasted
      link, the docs page) would otherwise send that path to the next host the
      browser talks to.
    * ``Cross-Origin-Opener-Policy: same-origin`` -- inert on a JSON response, and
      that is fine: it costs a header and it severs the window reference on the
      only document this app serves.
    * ``Content-Security-Policy`` -- see :data:`API_CSP` and :data:`HTML_CSP`.
    * ``Cache-Control: no-store`` on ``/v1/*`` -- the concrete one. Those responses
      are one household's inventory, and neither the cookie identifying the
      account nor the header selecting the household is part of a cache's key by
      default. Without this, a shared proxy may serve household A's fridge to
      household B.
    * ``Vary: Cookie, X-Household-Id`` on ``/v1/*`` -- belt to the same braces, for
      a cache that ignores ``no-store``. ``Cookie`` is the one that matters since
      authentication moved there: it is now the input that decides *whose* data a
      response contains, and a cache keyed only on the URL would hand one
      account's answer to the next caller.
    * ``Strict-Transport-Security`` in production only.

    Deliberately **not** set: ``Cross-Origin-Embedder-Policy`` and
    ``Cross-Origin-Resource-Policy``, which govern embedding this API's responses
    in another document -- the PWA is on another origin by design, and
    ``same-origin`` there would break the product rather than protect it. CORS
    already decides who may read these responses. ``X-Frame-Options`` is
    superseded by ``frame-ancestors`` above.
    """

    def __init__(self, app: ASGIApp, *, production: bool) -> None:
        self._app = app
        self._production = production

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        private = str(scope.get("path", "")).startswith(PRIVATE_PATH_PREFIX)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                self._apply(message, private=private)
            await send(message)

        await self._app(scope, receive, send_with_headers)

    def _apply(self, message: Message, *, private: bool) -> None:
        headers = MutableHeaders(scope=message)
        headers.setdefault("x-content-type-options", "nosniff")
        headers.setdefault("referrer-policy", "no-referrer")
        headers.setdefault("cross-origin-opener-policy", "same-origin")
        is_html = headers.get("content-type", "").startswith("text/html")
        headers.setdefault("content-security-policy", HTML_CSP if is_html else API_CSP)
        if private:
            headers.setdefault("cache-control", "no-store")
            headers.append("vary", "Cookie")
            headers.append("vary", "X-Household-Id")
        if self._production:
            headers.setdefault("strict-transport-security", HSTS_VALUE)


class RequestSizeLimitMiddleware:
    """Refuse an oversized request body with ``413``, before it is in memory.

    Two paths, because a client controls what it declares.

    *Declared too large.* ``Content-Length`` above the bound is answered
    immediately, without reading a byte and without invoking the application.

    *Undeclared, or a lie.* Every chunk handed to the application is counted, and
    the read is interrupted the moment the total passes the bound. The
    interruption is an :class:`HTTPException` subclass on purpose: FastAPI's body
    reader re-raises ``HTTPException`` untouched and converts everything else into
    a generic ``400``, so any other exception type would be reported as a parse
    failure instead of a size refusal.

    *Per-path bounds.* ``path_bounds`` raises the ceiling for the routes that
    carry a file rather than a JSON document. The general cap is sized for the
    largest legitimate JSON body (``config.py``), and a route accepting an upload
    needs its own, higher number -- raising the global one instead would hand
    every JSON endpoint the same memory footprint. A path absent from the mapping
    keeps the general cap, so the default behaviour of this class is unchanged by
    the existence of the feature.

    This is the application's own floor, not a substitute for the same limit on the
    reverse proxy -- a request refused here has still crossed the network.
    """

    def __init__(
        self, app: ASGIApp, *, max_bytes: int, path_bounds: Mapping[str, int] | None = None
    ) -> None:
        self._app = app
        self._max_bytes = max_bytes
        self._path_bounds = dict(path_bounds or {})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        limit = self._limit_for(str(scope.get("path", "")))
        declared = _declared_length(scope)
        if declared is not None and declared > limit:
            response = problem_for_body_too_large(limit).to_response()
            await response(scope, receive, send)
            return

        await self._app(scope, self._counted(receive, limit), send)

    def _limit_for(self, path: str) -> int:
        """The bound for ``path``: an exact match, else the general cap.

        Exact rather than prefix: a prefix match would silently raise the ceiling
        for every route later added under the same segment, which is how a bound
        stops meaning anything.
        """
        return self._path_bounds.get(path, self._max_bytes)

    def _counted(self, receive: Receive, limit: int) -> Callable[[], Awaitable[Message]]:
        read = 0

        async def counted_receive() -> Message:
            nonlocal read
            message = await receive()
            if message["type"] == "http.request":
                read += len(message.get("body", b""))
                if read > limit:
                    raise RequestBodyTooLarge(limit)
            return message

        return counted_receive


def _declared_length(scope: Scope) -> int | None:
    """``Content-Length`` as an integer, or ``None`` when absent or unparseable.

    An unparseable value is not rejected here: the HTTP parser already refuses
    those, and inventing a second, differently worded refusal for a case that
    cannot reach us is dead code.
    """
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None

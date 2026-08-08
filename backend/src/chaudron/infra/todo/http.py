"""Bounded HTTP for the export adapters, and the rule that no token comes back out.

This is the counterpart of ``infra/llm/http.py`` for destinations whose hostname
is **ours, fixed and compiled in** rather than supplied by a household. That
difference removes one control and keeps every other:

* **No SSRF allowlist**, because there is no attacker-controlled URL to guard. The
  base URL is a module constant in the adapter; nothing in a request, a database
  row or a household configuration can move it. Copying the allowlist here would
  be a control that decides nothing while suggesting it decides something.
* **Redirects are still disabled.** ``api.todoist.com`` answering ``302`` to
  somewhere else is either a vendor change we must notice or a hijack we must
  refuse, and following it would send a bearer token to the new host. Both cases
  want the same answer: fail, loudly.
* **The response is still bounded**, in time and in bytes, and read as a stream so
  the ceiling is enforced before the body is materialised.
* **Nothing the far end wrote is ever quoted.** A rejected token is routinely
  echoed inside the very error explaining the rejection, so every message built
  here comes from our own fields, and the one place an excerpt is genuinely
  useful -- a body that is not JSON -- goes through
  :func:`chaudron.infra.redaction.snippet` with the token passed as a literal
  secret to remove.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx

from chaudron.domain.shopping_export import (
    ExportContext,
    ExportTargetNotConfigured,
    ExportTargetRejected,
    ShoppingExportQuotaExceeded,
    ShoppingExportResponseInvalid,
    ShoppingExportUnavailable,
)
from chaudron.infra.redaction import snippet
from chaudron.infra.todo.settings import TodoExportSettings

__all__ = ["BoundedTodoClient", "HttpAnswer", "translate_export_status"]

#: Sent on every outbound call so the far end can identify the caller, as
#: Todoist's terms ask of API clients and as ``infra/openfoodfacts.py`` already
#: does. It names the project, not the household: an export must not tell a
#: third party more about who we are than the token already does.
USER_AGENT: Final = "Chaudron/0.1.0 (+https://github.com/ClaraVnk/chaudron)"


@dataclass(frozen=True, slots=True)
class HttpAnswer:
    """A non-2xx answer, reduced to what is safe to reason about."""

    status: int
    body: str


class BoundedTodoClient:
    """POST to one fixed host, with every bound applied and no token in any error.

    ``transport`` is the seam the tests use: the adapter under test is the real
    one, only the socket is a double. It is never populated in production.
    """

    __slots__ = ("_base_url", "_headers", "_secret", "_settings", "_target", "_transport")

    def __init__(
        self,
        base_url: str,
        *,
        target: str,
        settings: TodoExportSettings,
        headers: Mapping[str, str] | None = None,
        secret: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        url = httpx.URL(base_url)
        if url.scheme != "https":
            # A bearer token over cleartext is a credential given away. The
            # destinations are constants, so this can only fire on a typo in this
            # package -- which is exactly when it is cheapest to find out.
            raise ValueError(f"an export destination must be https, got {url.scheme!r}")
        self._base_url = url
        self._target = target
        self._settings = settings
        self._headers = {"User-Agent": USER_AGENT, **dict(headers or {})}
        #: The token, held only so it can be *removed* from any text that escapes.
        self._secret = secret
        self._transport = transport

    async def post_form(self, path: str, form: Mapping[str, str]) -> dict[str, Any] | HttpAnswer:
        """POST ``application/x-www-form-urlencoded``; decode JSON or report the failure.

        The only verb and the only encoding this package needs, because the only
        destination it has documents that one. A JSON sibling was written and
        removed: an unused branch of a client that carries a bearer token is a
        code path nobody exercises and everybody trusts.
        """
        return await self._send(path, data=dict(form))

    async def _send(self, path: str, *, data: dict[str, str]) -> dict[str, Any] | HttpAnswer:
        url = self._base_url.join(path)
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=self._settings.timeout_seconds,
            # A permitted host must not be able to forward our bearer token to
            # another one.
            follow_redirects=False,
            headers=self._headers,
        ) as client:
            try:
                request = client.build_request("POST", url, data=data)
                response = await client.send(request, stream=True)
                try:
                    body = await self._read_bounded(response)
                finally:
                    await response.aclose()
            except httpx.HTTPError as exc:
                raise self._translate_transport(exc) from None

        # 3xx is a failure here, not a success: redirects are off on purpose.
        if response.status_code >= 300:
            return HttpAnswer(status=response.status_code, body=body)
        try:
            decoded: object = json.loads(body)
        except json.JSONDecodeError:
            raise ShoppingExportResponseInvalid(
                f"{self._target} returned a body that is not JSON: {self._safe(body)}",
                context=ExportContext(self._target, "malformed_payload"),
            ) from None
        if not isinstance(decoded, dict):
            raise ShoppingExportResponseInvalid(
                f"{self._target} returned a JSON value that is not an object",
                context=ExportContext(self._target, "malformed_payload"),
            )
        return decoded

    async def _read_bounded(self, response: httpx.Response) -> str:
        limit = self._settings.max_response_bytes
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > limit:
                raise ShoppingExportResponseInvalid(
                    f"the response from {self._target} exceeded the {limit} byte "
                    "ceiling and was abandoned",
                    context=ExportContext(self._target, "response_too_large"),
                )
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")

    def _safe(self, text: str) -> str:
        """A short excerpt with our own token removed as a literal, then scrubbed."""
        secrets = [self._secret] if self._secret else []
        return snippet(text, secrets=secrets)

    def _translate_transport(self, exc: Exception) -> Exception:
        """An ``httpx`` failure, as a domain error. The exception is never chained.

        ``from None`` at the raise site and no interpolation here: an ``httpx``
        error renders the request it failed on, and the request carries the
        ``Authorization`` header.
        """
        if isinstance(exc, httpx.TimeoutException):
            return ShoppingExportUnavailable(
                f"{self._target} did not answer within the configured timeout",
                context=ExportContext(self._target, "timeout"),
            )
        if isinstance(exc, httpx.TransportError):
            return ShoppingExportUnavailable(
                f"{self._target} could not be reached",
                context=ExportContext(self._target, "connection_failed"),
            )
        return ShoppingExportUnavailable(
            f"the call to {self._target} failed before a response was received",
            context=ExportContext(self._target, "client_error"),
        )


def translate_export_status(answer: HttpAnswer, *, target: str) -> Exception:
    """Map a non-2xx status onto the domain, without echoing the recipient's body.

    The mapping is narrower than the model providers' because the failures are
    narrower: there is no image to be too large for and no context window to
    overflow. What is left is the four cases a household can act on -- the token
    is dead, the destination is gone, we are being throttled, or the far end is
    broken -- and one honest "unexpected" for everything else.
    """
    status = answer.status
    if status in (401, 403):
        return ExportTargetRejected(
            f"{target} refused the stored token ({status}); it was revoked or "
            "regenerated. Enter a new one",
            context=ExportContext(target, "invalid_credentials"),
        )
    if status == 404:
        return ExportTargetNotConfigured(
            f"{target} has no such endpoint or list ({status}); the destination "
            "may have been deleted",
            context=ExportContext(target, "not_found"),
        )
    if status == 429:
        return ShoppingExportQuotaExceeded(
            f"{target} is rate limiting this account; retry shortly",
            context=ExportContext(target, "rate_limited"),
        )
    if status >= 500:
        return ShoppingExportUnavailable(
            f"{target} returned a server error ({status})",
            context=ExportContext(target, "server_error"),
        )
    return ShoppingExportUnavailable(
        f"{target} returned an unexpected status ({status})",
        context=ExportContext(target, "unexpected_status"),
    )

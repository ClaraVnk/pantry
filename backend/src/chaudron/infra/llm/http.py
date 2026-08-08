"""HTTP plumbing shared by the adapters that speak plain REST, plus the SSRF guard.

The guard is the reason this module exists. In the co-located Ollama topology of
ADR-0007 the base URL is **supplied by the user and dialled by the server**: a
textbook SSRF primitive. The usual mitigation -- reject private ranges -- is
inoperative, because the legitimate address of a co-located Ollama *is* private
(``http://ollama:11434`` on a Podman network). The replacement is an explicit
allowlist of **endpoints** -- host *and* port -- from the instance environment, plus:

* schemes restricted to ``http`` and ``https``;
* no credentials in the URL;
* DNS resolved at validation **and** again immediately before the call, with the two
  answers required to agree -- otherwise a name that resolves to an allowed host at
  save time and to a metadata endpoint at call time (DNS rebinding) would pass;
* the socket opened on the address that agreement was reached about, rather than on
  whatever a third lookup would have said (:func:`_dial_targets`);
* redirects disabled, so a permitted host cannot bounce us onto a forbidden one;
* a bounded timeout and a bounded response size, so a hostile or broken endpoint
  cannot hold a connection or exhaust memory.

The port half of the allowlist is not decoration. While it was missing (security
audit, AUD-005) a household could point its "Ollama" at any port of an allowed host
and read the outcomes apart: a listening HTTP server, a listening non-HTTP service
and a closed port produced three distinguishable answers, which is a port scanner
with an oracle. Constraining the pair collapses every endpoint the operator did not
declare into one refusal, taken structurally, before a socket is opened.

Everything here raises domain errors. No ``httpx`` exception crosses the boundary.

The one place a body is quoted -- an excerpt of a response that is not JSON, which
is how an operator diagnoses a gateway -- scrubs the credential this client sends
*by literal match*, not merely by pattern. It is derived from the headers rather
than passed in, so an adapter cannot forget it: whatever authenticates the request
is by construction what has to come out of the excerpt. That path is not
hypothetical. ``base_url`` is household-supplied on the Ollama topology, and an
OpenAI-compatible gateway that answers 200 with a proxy error page echoing
``Authorization`` puts the household's key straight into a domain error, and from
there into a log line.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx

from chaudron.domain.llm_ports import (
    ProviderContext,
    ProviderNotConfigured,
    ProviderQuotaExceeded,
    ProviderResponseInvalid,
    ProviderUnavailable,
)
from chaudron.infra.llm.settings import ALLOWED_HOSTS_ENV_VAR, LlmSettings
from chaudron.infra.redaction import snippet

__all__ = [
    "GuardedHttpClient",
    "HttpFailure",
    "Resolver",
    "endpoint_port",
    "resolve_pin",
    "system_resolver",
    "translate_http_status",
    "translate_transport_error",
    "validate_ollama_base_url",
]

_ALLOWED_SCHEMES: Final = frozenset({"http", "https"})
_DEFAULT_SCHEME_PORTS: Final = {"http": 80, "https": 443}

#: Headers whose value is a credential, whatever else it is. Lower-cased, because
#: header names are case-insensitive and a mismatch here fails open.
_CREDENTIAL_HEADERS: Final = frozenset(
    {"authorization", "x-api-key", "x-goog-api-key", "api-key", "proxy-authorization"}
)

#: Schemes that prefix a credential in an ``Authorization`` header. The scheme
#: itself is not secret and removing it as one would blank the word "Bearer" out of
#: every diagnostic that mentions it.
_AUTH_SCHEMES: Final = ("bearer ", "basic ", "token ")

#: Resolve a (host, port) pair to the set of addresses it currently points at.
Resolver = Callable[[str, int], Awaitable[frozenset[str]]]


async def system_resolver(host: str, port: int) -> frozenset[str]:
    """The real resolver: the operating system's, off the event loop thread."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise ProviderUnavailable(f"host {host!r} could not be resolved") from exc
    return frozenset(str(info[4][0]) for info in infos)


async def resolve_pin(url: httpx.URL, resolver: Resolver) -> frozenset[str]:
    """The DNS answer that authorises this URL, kept so later calls can be compared.

    Separate from :func:`system_resolver` because the *moment* it runs is the whole
    control: this is the "first" of the two answers
    :meth:`GuardedHttpClient.assert_stable_resolution` requires to agree, and it has
    to be taken when the transport is built rather than lazily on the first call --
    a pin established by the very request it is meant to guard would compare an
    answer against itself and refuse nothing.
    """
    addresses = await resolver(url.host, endpoint_port(url))
    if not addresses:
        # An empty answer must not become an empty pin: `frozenset() <= frozenset()`
        # holds, so it would satisfy the later check while asserting nothing.
        raise ProviderNotConfigured(
            f"host {url.host!r} resolves to no address; the Ollama endpoint cannot be reached"
        )
    return addresses


def validate_ollama_base_url(raw_url: str, settings: LlmSettings) -> httpx.URL:
    """Structural half of the guard, run at configuration time and before every call.

    Rejecting here, at registration, is what ADR-0007 asks for: a household should
    learn that its URL is not permitted while it is looking at the form, not on the
    first recipe it tries to generate.
    """
    text = (raw_url or "").strip()
    if not text:
        raise ProviderNotConfigured("the Ollama base URL is empty")
    try:
        url = httpx.URL(text)
    except httpx.InvalidURL, ValueError:
        raise ProviderNotConfigured(f"the Ollama base URL is malformed: {snippet(text)}") from None

    if url.scheme not in _ALLOWED_SCHEMES:
        raise ProviderNotConfigured(
            f"the Ollama base URL must use http or https, not {url.scheme!r}"
        )
    if url.userinfo:
        raise ProviderNotConfigured("the Ollama base URL must not embed credentials")
    host = url.host
    if not host:
        raise ProviderNotConfigured("the Ollama base URL has no host")
    if not settings.ollama_allowed_hosts:
        raise ProviderNotConfigured(
            "no Ollama host is allowed on this instance; set "
            f"{ALLOWED_HOSTS_ENV_VAR} to the container or service name of your "
            "Ollama and restart the instance"
        )
    port = endpoint_port(url)
    if not settings.allows_endpoint(host, port):
        # The refusal names what the caller asked for and nothing else. Listing the
        # allowlist back would hand a household a map of the instance's private
        # network (docs/security-model.md, A10) -- and would make the refusal differ
        # from endpoint to endpoint, which is the oracle this control removes.
        raise ProviderNotConfigured(
            f"{host}:{port} is not an Ollama endpoint this instance permits; "
            f"add it to {ALLOWED_HOSTS_ENV_VAR} as host:port and restart the instance"
        )
    return url


def endpoint_port(url: httpx.URL) -> int:
    """The port this URL actually dials.

    ``httpx`` normalises a scheme's default port away, so ``http://ollama`` and
    ``http://ollama:80`` both report ``None`` and both mean 80. Resolving that here
    means the allowlist compares dialled ports, not written ones.
    """
    return url.port or _DEFAULT_SCHEME_PORTS[url.scheme]


def translate_transport_error(
    exc: Exception, context: ProviderContext, *, provider_label: str
) -> Exception:
    """Turn an ``httpx`` transport failure into the matching domain error."""
    if isinstance(exc, httpx.TimeoutException):
        return ProviderUnavailable(
            f"{provider_label} did not answer within the configured timeout",
            context=ProviderContext(context.provider, context.model, "timeout"),
        )
    if isinstance(exc, httpx.TransportError):
        return ProviderUnavailable(
            f"{provider_label} could not be reached",
            context=ProviderContext(context.provider, context.model, "connection_refused"),
        )
    return ProviderUnavailable(
        f"{provider_label} call failed before a response was received",
        context=ProviderContext(context.provider, context.model, "client_error"),
    )


def _credentials_in(headers: Mapping[str, str]) -> tuple[str, ...]:
    """The secret values this client sends, for removal from anything it quotes.

    Reading them off the headers rather than taking them as an argument is what
    makes the guarantee structural: an adapter that authenticates its requests has
    already declared its credential here, and cannot separately forget to declare
    it for redaction.
    """
    found: list[str] = []
    for name, value in headers.items():
        if name.lower() not in _CREDENTIAL_HEADERS:
            continue
        candidate = value.strip()
        lowered = candidate.lower()
        for scheme in _AUTH_SCHEMES:
            if lowered.startswith(scheme):
                candidate = candidate[len(scheme) :].strip()
                break
        if candidate:
            found.append(candidate)
    return tuple(found)


@dataclass(frozen=True, slots=True)
class HttpFailure:
    """A non-2xx answer, reduced to what is safe to reason about."""

    status: int
    body: str


@dataclass(frozen=True, slots=True)
class _DialTarget:
    """One socket to open: a literal address, still speaking the configured name."""

    url: httpx.URL
    headers: Mapping[str, str]
    extensions: Mapping[str, Any]


def _dial_targets(url: httpx.URL, addresses: frozenset[str] | None) -> tuple[_DialTarget, ...]:
    """Point the request at the pinned addresses without changing who it talks to.

    This is the transport half of the rebinding control, and the half that was
    missing (AUD-028, pentest S-17): comparing two DNS answers decides nothing if
    ``httpx`` then takes a third one when it opens the socket. The address goes in
    the URL, so it is what ``connect()`` gets; the *name* goes in two places, and
    both matter for a different reason:

    * ``Host`` -- a name-based virtual host, and any reverse proxy in front of the
      endpoint, routes on this. Sending the literal instead would reach the right
      machine and the wrong service.
    * ``sni_hostname`` -- the TLS extension, and, because ``httpcore`` passes it to
      ``start_tls`` as ``server_hostname``, **also the name the certificate is
      checked against**. Leaving it to default to the URL's host would verify the
      chain against an IP literal, which no ordinary certificate carries: every
      HTTPS endpoint would fail, and the obvious way to make it pass again is to
      stop verifying -- trading a hard SSRF for an easy man-in-the-middle. The
      guarantee has to stay "the socket goes where DNS was pinned, and the peer
      still has to prove it is the name".

    With no pin (the first-party providers) the URL is used exactly as written and
    resolution is left to ``httpx``, which is the pre-existing behaviour for hosts
    that are not household-supplied.
    """
    if not addresses:
        return (_DialTarget(url, {}, {}),)
    # The authority as configured, port included when it was written: that is what
    # a virtual host expects to see, and `url.host` alone would drop the port.
    authority = url.netloc.decode("ascii")
    name = url.host
    return tuple(
        _DialTarget(
            url=url.copy_with(host=address),
            headers={"Host": authority},
            extensions={"sni_hostname": name},
        )
        for address in _dial_order(addresses)
    )


def _dial_order(addresses: frozenset[str]) -> tuple[str, ...]:
    """The pinned addresses, as literals, in a deterministic order.

    IPv4 before IPv6, then sorted: a set has no order, and an order that varies
    between two runs makes "which address did it dial" unanswerable in an incident.

    Anything that is not a bare literal is refused rather than dropped. A resolver
    answer this cannot put in a URL -- a scoped link-local ``fe80::1%eth0``, say --
    would otherwise silently reduce the set the socket may go to, and an empty
    reduction would fall back to dialling the name, which is the hole being closed.
    """
    literals: list[tuple[int, bytes, str]] = []
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            raise ProviderNotConfigured(
                f"the endpoint resolves to {address!r}, which is not an address this "
                "client can dial directly; the request was refused rather than sent "
                "to whatever the name resolves to next"
            ) from None
        literals.append((parsed.version, parsed.packed, address))
    return tuple(address for _, _, address in sorted(literals))


class GuardedHttpClient:
    """A tiny POST-JSON client with every bound ADR-0007 asks for already applied."""

    def __init__(
        self,
        base_url: httpx.URL,
        settings: LlmSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
        pinned_addresses: frozenset[str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._base_url = base_url
        self._settings = settings
        self._transport = transport
        self._resolver = resolver
        self._pinned = pinned_addresses
        self._headers = dict(headers or {})
        self._secrets = _credentials_in(self._headers)

    @property
    def base_url(self) -> httpx.URL:
        return self._base_url

    async def assert_stable_resolution(self) -> frozenset[str] | None:
        """Re-resolve, compare, and hand back the addresses the call may dial.

        Skipped when no resolver is configured -- which is the case for the
        first-party providers, whose hostnames are ours to trust and are not
        user-supplied. Only the Ollama path, where the URL comes from a household,
        pins its resolution. The pin itself is taken by :func:`resolve_pin` when the
        transport is built (``LlmProviderFactory._build_ollama``), which is what
        makes the two answers compared here genuinely two.

        The return value is the *current* answer, not the pin: it is non-empty and
        contained in the pin, so every address in it is both freshly observed and
        authorised. :meth:`_send_json` opens its socket on one of those literals
        rather than on the name -- without that, this method compares two answers
        and then lets ``httpx`` ask a third time (security audit AUD-028, pentest
        S-17: a resolver announcing an unreachable address passed the comparison
        while the socket still went where the *third* lookup pointed, so the pin
        never entered the choice at all).

        **What this still does not do.** Stated because the check once read much
        stronger than it was: for a while it was wired to nothing and returned on
        its first line in production, which is the failure mode this paragraph
        exists to prevent recurring. The pin lives and dies with this client, and a
        client is built per request. So the window closed is *from the resolution
        that authorised this transport to each socket it opens* -- which bites on
        every multi-call flow (the capability probe's ``/api/version`` then
        ``/api/show``; the emulated-schema retry) and, now that the socket is
        pinned too, leaves no gap inside a single call. A resolver that flips
        **between** two requests is still not caught, because nothing durable
        remembers the earlier answer: that would need the pin persisted next to the
        configuration, i.e. a schema change.

        Requiring the current answer to be a *subset* of the pinned one -- rather
        than merely to intersect it, which let ``[allowed, hostile]`` through -- is
        what keeps the returned set from being widened by the very lookup it is
        meant to constrain.
        """
        if self._resolver is None or self._pinned is None:
            return None
        host = self._base_url.host
        current = await self._resolver(host, endpoint_port(self._base_url))
        if not current or not current <= self._pinned:
            raise ProviderNotConfigured(
                f"host {host!r} now resolves elsewhere than when it was registered; "
                "the request was refused (possible DNS rebinding)"
            )
        return current

    async def get_json(
        self,
        path: str,
        *,
        context: ProviderContext,
        provider_label: str,
    ) -> dict[str, Any] | HttpFailure:
        """GET ``path``; return the decoded object or the failure to translate.

        Exists because some provider endpoints only answer GET. Ollama's
        ``/api/version`` is the case that forced it: a POST there returns 405, and
        the adapter used to send one on the assumption that a single verb kept every
        outbound call under the same guards. The guards belong to
        :meth:`_send_json`, not to the verb, so both share them.
        """
        return await self._send_json(
            "GET", path, None, context=context, provider_label=provider_label
        )

    async def post_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        context: ProviderContext,
        provider_label: str,
    ) -> dict[str, Any] | HttpFailure:
        """POST ``payload``; return the decoded object or the failure to translate."""
        return await self._send_json(
            "POST", path, payload, context=context, provider_label=provider_label
        )

    async def _send_json(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None,
        *,
        context: ProviderContext,
        provider_label: str,
    ) -> dict[str, Any] | HttpFailure:
        addresses = await self.assert_stable_resolution()
        targets = _dial_targets(self._base_url.join(path), addresses)
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=self._settings.timeout_seconds,
            # A permitted host must not be able to bounce us onto a forbidden one.
            follow_redirects=False,
            headers=self._headers,
        ) as client:
            response, body = await self._dial(
                client, method, targets, payload, context=context, provider_label=provider_label
            )

        # 3xx counts as a failure, not a success: redirects are disabled on purpose,
        # so a redirect is a permitted host trying to send us somewhere else.
        if response.status_code >= 300:
            return HttpFailure(status=response.status_code, body=body)
        try:
            decoded: object = json.loads(body)
        except json.JSONDecodeError:
            raise ProviderResponseInvalid(
                f"{provider_label} returned a body that is not JSON: "
                f"{snippet(body, secrets=self._secrets)}",
                context=ProviderContext(context.provider, context.model, "malformed_payload"),
            ) from None
        if not isinstance(decoded, dict):
            raise ProviderResponseInvalid(
                f"{provider_label} returned a JSON value that is not an object",
                context=ProviderContext(context.provider, context.model, "malformed_payload"),
            )
        return decoded

    async def _dial(
        self,
        client: httpx.AsyncClient,
        method: str,
        targets: tuple[_DialTarget, ...],
        payload: Mapping[str, Any] | None,
        *,
        context: ProviderContext,
        provider_label: str,
    ) -> tuple[httpx.Response, str]:
        """Open the socket, on the first target that accepts one, and read the body.

        More than one target only when the pinned answer held several addresses --
        a name with both an ``A`` and an ``AAAA`` record is ordinary on a Podman
        network, and refusing to try the second would turn "pinned" into "broken on
        dual-stack". Only a *connection* failure moves on: nothing has been written
        at that point, so re-sending a ``POST`` cannot duplicate anything. Anything
        that happens after the connection is up belongs to the endpoint that
        answered, and is translated rather than retried elsewhere.
        """
        for index, target in enumerate(targets):
            try:
                request = (
                    client.build_request(
                        method, target.url, headers=target.headers, extensions=target.extensions
                    )
                    if payload is None
                    else client.build_request(
                        method,
                        target.url,
                        json=dict(payload),
                        headers=target.headers,
                        extensions=target.extensions,
                    )
                )
                response = await client.send(request, stream=True)
                try:
                    body = await self._read_bounded(response, context)
                finally:
                    await response.aclose()
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if index + 1 < len(targets):
                    continue
                raise translate_transport_error(
                    exc, context, provider_label=provider_label
                ) from None
            except httpx.HTTPError as exc:
                raise translate_transport_error(
                    exc, context, provider_label=provider_label
                ) from None
            return response, body
        # `_dial_targets` never returns an empty tuple; this keeps the contract
        # total rather than relying on that from a distance.
        raise ProviderUnavailable(
            f"{provider_label} has no address to dial",
            context=ProviderContext(context.provider, context.model, "connection_refused"),
        )

    async def _read_bounded(self, response: httpx.Response, context: ProviderContext) -> str:
        limit = self._settings.max_response_bytes
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > limit:
                raise ProviderResponseInvalid(
                    f"response exceeded the {limit} byte ceiling and was abandoned",
                    context=ProviderContext(context.provider, context.model, "response_too_large"),
                )
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")


def translate_http_status(
    failure: HttpFailure,
    context: ProviderContext,
    *,
    provider_label: str,
    not_found_hint: str | None = None,
    credentials_hint: str | None = None,
) -> Exception:
    """Map a non-2xx status onto the domain, without echoing the provider's body."""
    status = failure.status
    if status in (401, 403):
        message = f"{provider_label} rejected the credentials ({status})"
        if credentials_hint:
            message += f"; {credentials_hint}"
        return ProviderNotConfigured(
            message, context=ProviderContext(context.provider, context.model, "invalid_credentials")
        )
    if status == 404:
        message = f"{provider_label} has no such endpoint or model ({status})"
        if not_found_hint:
            message += f"; {not_found_hint}"
        return ProviderNotConfigured(
            message, context=ProviderContext(context.provider, context.model, "not_found")
        )
    if status == 429:
        return ProviderQuotaExceeded(
            f"{provider_label} rate limit reached",
            context=ProviderContext(context.provider, context.model, "rate_limited"),
        )
    if status in (402, 413) or (status == 400 and _looks_like_quota(failure.body)):
        if status == 413:
            return ProviderUnavailable(
                f"{provider_label} refused the request as too large",
                context=ProviderContext(context.provider, context.model, "request_too_large"),
            )
        return ProviderQuotaExceeded(
            f"{provider_label} reports the account is out of credit or quota",
            context=ProviderContext(context.provider, context.model, "quota_exhausted"),
        )
    if status >= 500:
        return ProviderUnavailable(
            f"{provider_label} returned a server error ({status})",
            context=ProviderContext(context.provider, context.model, "server_error"),
        )
    return ProviderUnavailable(
        f"{provider_label} returned an unexpected status ({status})",
        context=ProviderContext(context.provider, context.model, "unexpected_status"),
    )


_QUOTA_MARKERS: Final = ("quota", "credit", "billing", "insufficient", "exceeded")


def _looks_like_quota(body: str) -> bool:
    """Route on the body's wording; never re-emit it.

    Several providers report an exhausted balance as a 400 rather than a 402, and
    telling a household "you are out of credit" instead of "something went wrong" is
    the difference between a two-minute fix and a support ticket.
    """
    lowered = body.lower()
    return any(marker in lowered for marker in _QUOTA_MARKERS)

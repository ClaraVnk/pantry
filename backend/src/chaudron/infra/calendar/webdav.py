"""The WebDAV/CalDAV XML this server has to read and write, and nothing more.

A CalDAV client is not an ``.ics`` reader. Before it fetches a single task it
issues ``PROPFIND`` to find the principal, ``PROPFIND`` again to find that
principal's calendar home, ``PROPFIND`` a third time to enumerate the
collections there, and only then ``REPORT`` to pull the objects. Serving a static
file at a URL and hoping does not work -- the research note this feature comes
from could find **no proven case of a mobile client consuming ``VTODO`` from a
``webcal://`` subscription**, and an Apple support thread asking exactly that
went unanswered. So the protocol is implemented, and this module is the half of
it that speaks XML.

**Parsing is deliberately shallow.** The server is read-only, so the only things
it has to understand are: which properties are being asked for, which hrefs a
multiget names, and which sync token a client is holding. Filters are read to the
depth that matters -- the component type -- and no further: a request for
``VEVENT`` gets an empty answer rather than a wrong one.

**The entity guard, and the hole a byte search leaves in it.**
``xml.etree.ElementTree`` no longer resolves external entities (Python 3.7.1+),
but it remains open to internal entity expansion -- the "billion laughs" family,
where a kilobyte of markup becomes gigabytes of memory. So the parser is fed
nothing carrying a document type declaration, found with one substring search over
the raw bytes.

A substring search over *bytes* only sees a declaration written in an
ASCII-compatible encoding, and expat accepts one that is not: **UTF-16**, whose
``<!DOCTYPE`` shares no byte with the pattern. It needs neither a byte order mark
nor an ``encoding`` pseudo-attribute -- expat sniffs it from the first characters
-- so the guard was bypassable by re-encoding the same request, which was
demonstrated rather than supposed. The fix is the line above the search:
**a body holding a NUL byte is refused outright.** XML 1.0 forbids U+0000 in a
document, so no conformant request in any ASCII-compatible encoding contains one,
and every UTF-16 document does -- its markup is ASCII, and ASCII in UTF-16 is
every other byte NUL. That is what makes the byte search sound for what remains:
of the encodings expat decodes without help (UTF-8, UTF-16, ISO-8859-1,
US-ASCII), UTF-16 is the only one where the pattern could hide, and it is now the
one that cannot arrive.

What that guard was worth was measured rather than assumed, and the honest answer
is "little, but it is cheap": no external entity is resolved either way, expat
2.6 caps amplification on its own, and this parser is reached only after
authentication and under a poll limiter. A dedicated hardened parser was
considered and rejected -- ``defusedxml`` has had no release since 2021, and
``CLAUDE.md`` section 17 asks for a maintained dependency or none.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

DAV_NS: Final = "DAV:"
CALDAV_NS: Final = "urn:ietf:params:xml:ns:caldav"
CALENDARSERVER_NS: Final = "http://calendarserver.org/ns/"

# Prefixes chosen to match what sabre/dav emits, since that is the server most
# clients have been tested against.
ET.register_namespace("d", DAV_NS)
ET.register_namespace("cal", CALDAV_NS)
ET.register_namespace("cs", CALENDARSERVER_NS)

XML_CONTENT_TYPE: Final = "application/xml; charset=utf-8"

STATUS_OK: Final = "HTTP/1.1 200 OK"
STATUS_NOT_FOUND: Final = "HTTP/1.1 404 Not Found"

#: A document type declaration is refused outright; see the module docstring.
_DOCTYPE: Final = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)

#: The byte that tells a UTF-16 body from every encoding the search above can
#: read. Forbidden inside an XML document by the specification itself, so
#: refusing it costs no conformant request.
_NUL: Final = b"\x00"


class MalformedRequestError(ValueError):
    """The body is not XML this server is willing to parse."""


def dav(name: str) -> str:
    return f"{{{DAV_NS}}}{name}"


def caldav(name: str) -> str:
    return f"{{{CALDAV_NS}}}{name}"


def calendarserver(name: str) -> str:
    return f"{{{CALENDARSERVER_NS}}}{name}"


def parse(body: bytes) -> ET.Element:
    """Parse a request body into an element, or refuse it.

    An empty body is not an error at this level: ``PROPFIND`` with no body means
    ``allprop`` (RFC 4918 §9.1), and several clients send exactly that.

    The encoding check comes first and is what makes the declaration check below
    mean anything; the module docstring explains why, and
    ``tests/calendar/test_webdav.py`` replays the bypass it closes.
    """
    if _NUL in body:
        raise MalformedRequestError("the body is not in an ASCII-compatible encoding")
    if _DOCTYPE.search(body):
        raise MalformedRequestError("document type declarations are not accepted")
    try:
        return ET.fromstring(body)  # noqa: S314 - guarded above; see module docstring
    except ET.ParseError as exc:
        raise MalformedRequestError("body is not well-formed XML") from exc


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PropfindRequest:
    """What a ``PROPFIND`` asked for.

    ``all_properties`` covers both an empty body and ``<allprop/>``. This server
    answers it with the same set it would return for an explicit list, which is
    what RFC 4918 §9.1 permits: ``allprop`` does not oblige a server to return
    expensive or protected properties.
    """

    properties: tuple[str, ...]
    all_properties: bool


def parse_propfind(body: bytes) -> PropfindRequest:
    if not body.strip():
        return PropfindRequest(properties=(), all_properties=True)
    root = parse(body)
    if root.tag != dav("propfind"):
        raise MalformedRequestError("expected a DAV:propfind element")
    if root.find(dav("allprop")) is not None or root.find(dav("prop")) is None:
        return PropfindRequest(properties=(), all_properties=True)
    requested = root.find(dav("prop"))
    names = tuple(child.tag for child in requested) if requested is not None else ()
    return PropfindRequest(properties=names, all_properties=False)


@dataclass(frozen=True, slots=True)
class ReportRequest:
    """What a ``REPORT`` asked for, reduced to the three reports that matter."""

    kind: str
    properties: tuple[str, ...]
    hrefs: tuple[str, ...] = ()
    sync_token: str | None = None
    #: Component names a ``calendar-query`` filter accepts, upper-cased. Empty
    #: means the filter named none, which this server reads as "no restriction".
    components: frozenset[str] = field(default_factory=frozenset)


CALENDAR_QUERY: Final = "calendar-query"
CALENDAR_MULTIGET: Final = "calendar-multiget"
SYNC_COLLECTION: Final = "sync-collection"


def _requested_properties(root: ET.Element) -> tuple[str, ...]:
    prop = root.find(dav("prop"))
    return tuple(child.tag for child in prop) if prop is not None else ()


def _filter_components(root: ET.Element) -> frozenset[str]:
    """Component names named anywhere in a ``calendar-query`` filter.

    Nested comp-filters are flattened on purpose: the only question this server
    can usefully answer is "does this filter mention VTODO", because everything
    it publishes is a VTODO and the outer VCALENDAR filter is always present.
    """
    names = {
        (element.get("name") or "").upper()
        for element in root.iter(caldav("comp-filter"))
        if element.get("name")
    }
    return frozenset(names - {"VCALENDAR"})


def parse_report(body: bytes) -> ReportRequest:
    root = parse(body)
    match root.tag:
        case tag if tag == caldav(CALENDAR_QUERY):
            return ReportRequest(
                kind=CALENDAR_QUERY,
                properties=_requested_properties(root),
                components=_filter_components(root),
            )
        case tag if tag == caldav(CALENDAR_MULTIGET):
            hrefs = tuple(
                (element.text or "").strip()
                for element in root.findall(dav("href"))
                if (element.text or "").strip()
            )
            return ReportRequest(
                kind=CALENDAR_MULTIGET,
                properties=_requested_properties(root),
                hrefs=hrefs,
            )
        case tag if tag == dav(SYNC_COLLECTION):
            token = root.find(dav("sync-token"))
            return ReportRequest(
                kind=SYNC_COLLECTION,
                properties=_requested_properties(root),
                sync_token=None if token is None else (token.text or "").strip(),
            )
        case _:
            raise MalformedRequestError("unsupported REPORT")


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


def element(tag: str, text: str | None = None, *children: ET.Element) -> ET.Element:
    """Build one element. ``text`` is escaped by the serialiser, never by hand."""
    node = ET.Element(tag)
    if text is not None:
        node.text = text
    node.extend(children)
    return node


@dataclass(frozen=True, slots=True)
class ResourceResponse:
    """One ``DAV:response``: an href, the properties found, the ones that were not."""

    href: str
    found: Mapping[str, ET.Element]
    missing: Sequence[str] = ()


def build_multistatus(
    responses: Iterable[ResourceResponse], *, sync_token: str | None = None
) -> bytes:
    """Serialise a ``207 Multi-Status`` body.

    Empty ``propstat`` blocks are omitted rather than emitted empty: a client
    reading a ``404`` block with no properties in it is entitled to be confused,
    and some are.
    """
    root = ET.Element(dav("multistatus"))
    for response in responses:
        node = ET.SubElement(root, dav("response"))
        node.append(element(dav("href"), response.href))
        if response.found:
            propstat = ET.SubElement(node, dav("propstat"))
            prop = ET.SubElement(propstat, dav("prop"))
            for value in response.found.values():
                prop.append(value)
            propstat.append(element(dav("status"), STATUS_OK))
        if response.missing:
            propstat = ET.SubElement(node, dav("propstat"))
            prop = ET.SubElement(propstat, dav("prop"))
            for name in response.missing:
                prop.append(element(name))
            propstat.append(element(dav("status"), STATUS_NOT_FOUND))
    if sync_token is not None:
        root.append(element(dav("sync-token"), sync_token))
    return _serialise(root)


def build_error(condition: str) -> bytes:
    """A ``DAV:error`` carrying one precondition element.

    Used for ``DAV:valid-sync-token``: RFC 6578 §3.2 says a server that cannot
    honour a token answers ``403`` with that condition, and the client then falls
    back to a full synchronisation. Answering anything else makes it fall back to
    nothing.
    """
    return _serialise(element(dav("error"), None, element(condition)))


def _serialise(root: ET.Element) -> bytes:
    """``ET.tostring`` with the encoding pinned, and typed.

    The stub says ``Any`` for the overload that returns bytes, which under
    ``--strict`` would silently launder an untyped value into every caller.
    """
    payload: bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return payload

"""Parsing what a client sends, including what a hostile one sends."""

from __future__ import annotations

import pytest

from chaudron.infra.calendar.webdav import (
    CALENDAR_MULTIGET,
    CALENDAR_QUERY,
    SYNC_COLLECTION,
    MalformedRequestError,
    ResourceResponse,
    build_multistatus,
    caldav,
    dav,
    element,
    parse_propfind,
    parse_report,
)

BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
]>
<d:propfind xmlns:d="DAV:"><d:prop><d:displayname>&lol2;</d:displayname></d:prop></d:propfind>
"""

EXTERNAL_ENTITY = b"""<?xml version="1.0"?>
<!DOCTYPE foo [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>
<d:propfind xmlns:d="DAV:"><d:prop><d:displayname>&xxe;</d:displayname></d:prop></d:propfind>
"""


#: The same declaration, written where a search over bytes cannot see it. In
#: UTF-16 every ASCII character is two bytes, one of them NUL, so ``<!DOCTYPE``
#: shares not a single byte with the pattern -- and expat sniffs the encoding
#: from the first characters, so neither a byte order mark nor an ``encoding``
#: pseudo-attribute is needed to make it parse.
_UTF16_DOCTYPE = (
    "<!DOCTYPE d:propfind [<!ENTITY chaudron 'expanded'>]>"
    '<d:propfind xmlns:d="DAV:"><d:prop><d:displayname>&chaudron;</d:displayname>'
    "</d:prop></d:propfind>"
)


@pytest.mark.parametrize("body", [BILLION_LAUGHS, EXTERNAL_ENTITY])
def test_entity_declarations_are_refused_before_parsing(body: bytes) -> None:
    """The guard is a substring search, and it runs before expat sees anything.

    ``ElementTree`` still expands *internal* entities, so a document type
    declaration is refused outright rather than parsed carefully.
    """
    with pytest.raises(MalformedRequestError):
        parse_propfind(body)


@pytest.mark.parametrize(
    ("encoding", "bom"),
    [
        ("utf-16-le", b"\xff\xfe"),
        ("utf-16-be", b"\xfe\xff"),
        ("utf-16-le", b""),
        ("utf-16-be", b""),
    ],
    ids=["le-bom", "be-bom", "le-bare", "be-bare"],
)
def test_a_declaration_written_in_utf16_is_refused_too(encoding: str, bom: bytes) -> None:
    """The bypass the pentest replayed, in the four spellings it accepted.

    The byte search above only sees an ASCII-compatible encoding. Expat accepts
    one that is not, with or without a byte order mark, and it parsed these four
    bodies and expanded the entity in them. What refuses them now is the NUL byte
    every UTF-16 document carries -- forbidden inside XML by the specification, so
    no conformant request loses anything.
    """
    with pytest.raises(MalformedRequestError):
        parse_propfind(bom + _UTF16_DOCTYPE.encode(encoding))


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
def test_an_innocent_utf16_body_is_refused_as_well(encoding: str) -> None:
    """No entity, no declaration: refused all the same, and that is intended.

    The guard is on the encoding, not on what happens to be written in it. Every
    CalDAV client sends UTF-8, the XML specification requires a UTF-16 entity to
    announce itself, and accepting one would mean re-implementing the encoding
    sniffing that made the byte search unsound in the first place.
    """
    body = '<d:propfind xmlns:d="DAV:"><d:allprop/></d:propfind>'.encode(encoding)
    with pytest.raises(MalformedRequestError, match="ASCII-compatible"):
        parse_propfind(body)


def test_a_utf8_byte_order_mark_is_still_accepted() -> None:
    """The counterweight: UTF-8 with a mark is ordinary, and carries no NUL."""
    body = b"\xef\xbb\xbf" + b'<d:propfind xmlns:d="DAV:"><d:allprop/></d:propfind>'
    assert parse_propfind(body).all_properties is True


def test_an_empty_propfind_body_means_every_property() -> None:
    """RFC 4918 §9.1, and several real clients send exactly this."""
    assert parse_propfind(b"").all_properties is True


def test_allprop_means_every_property() -> None:
    body = b'<d:propfind xmlns:d="DAV:"><d:allprop/></d:propfind>'
    assert parse_propfind(body).all_properties is True


def test_a_named_property_list_is_read_in_order() -> None:
    body = (
        b'<d:propfind xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/">'
        b"<d:prop><d:resourcetype/><d:displayname/><cs:getctag/></d:prop></d:propfind>"
    )
    parsed = parse_propfind(body)
    assert parsed.all_properties is False
    assert parsed.properties == (
        dav("resourcetype"),
        dav("displayname"),
        "{http://calendarserver.org/ns/}getctag",
    )


def test_a_body_that_is_not_a_propfind_is_refused() -> None:
    with pytest.raises(MalformedRequestError):
        parse_propfind(b'<d:multistatus xmlns:d="DAV:"/>')


def test_malformed_xml_is_refused() -> None:
    with pytest.raises(MalformedRequestError):
        parse_propfind(b"<d:propfind")


def test_a_calendar_query_names_its_component() -> None:
    body = (
        b'<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        b"<D:prop><D:getetag/><C:calendar-data/></D:prop>"
        b'<C:filter><C:comp-filter name="VCALENDAR">'
        b'<C:comp-filter name="VTODO"/></C:comp-filter></C:filter></C:calendar-query>'
    )
    report = parse_report(body)
    assert report.kind == CALENDAR_QUERY
    assert report.components == frozenset({"VTODO"})
    assert report.properties == (dav("getetag"), caldav("calendar-data"))


def test_a_multiget_collects_its_hrefs() -> None:
    body = (
        b'<C:calendar-multiget xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        b"<D:prop><D:getetag/></D:prop>"
        b"<D:href>/caldav/p/F/cal/expiry/A.ics</D:href>"
        b"<D:href>/caldav/p/F/cal/expiry/B.ics</D:href></C:calendar-multiget>"
    )
    report = parse_report(body)
    assert report.kind == CALENDAR_MULTIGET
    assert report.hrefs == ("/caldav/p/F/cal/expiry/A.ics", "/caldav/p/F/cal/expiry/B.ics")


def test_a_sync_collection_carries_its_token() -> None:
    body = (
        b'<d:sync-collection xmlns:d="DAV:">'
        b"<d:sync-token>https://chaudron.dev/ns/sync/abc</d:sync-token>"
        b"<d:sync-level>1</d:sync-level><d:prop><d:getetag/></d:prop></d:sync-collection>"
    )
    report = parse_report(body)
    assert report.kind == SYNC_COLLECTION
    assert report.sync_token == "https://chaudron.dev/ns/sync/abc"


def test_an_unknown_report_is_refused() -> None:
    with pytest.raises(MalformedRequestError):
        parse_report(b'<d:acl-principal-prop-set xmlns:d="DAV:"/>')


def test_a_multistatus_separates_found_from_missing() -> None:
    body = build_multistatus(
        [
            ResourceResponse(
                href="/caldav/",
                found={dav("displayname"): element(dav("displayname"), "Chaudron")},
                missing=[dav("quota-used-bytes")],
            )
        ]
    ).decode()
    assert "HTTP/1.1 200 OK" in body
    assert "HTTP/1.1 404 Not Found" in body
    assert "Chaudron" in body


def test_a_multistatus_escapes_text_it_did_not_choose() -> None:
    """Nothing reaches the output as markup unless the serialiser put it there."""
    body = build_multistatus(
        [
            ResourceResponse(
                href="/caldav/",
                found={dav("displayname"): element(dav("displayname"), "<script>&amp;")},
            )
        ]
    ).decode()
    assert "<script>" not in body
    assert "&lt;script&gt;" in body

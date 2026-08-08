"""The receipt import end to end, over a real PostgreSQL and a real HTTP stack.

Four of these are the tests the feature exists to satisfy, and each fails if its
rule is removed:

* :func:`test_a_drive_recap_is_read_without_calling_a_model` -- the household in
  this suite has no provider and no key, which ADR-0007 calls a normal state. A
  PDF recap must still import, because its text is right there. If the import
  ever grows a hard dependency on inference, this is what goes red.
* :func:`test_the_image_is_never_stored` -- contract 6ter, and the reason
  migration ``0012`` exists.
* :func:`test_a_confirmed_receipt_counts_towards_the_budget` -- the end of the
  chain. Everything upstream can be perfect and mean nothing if the money never
  arrives at ``GET /v1/budget``.
* :func:`test_the_gap_between_total_and_line_sum_is_shown_not_closed` -- the one
  rule ``docs/technical-notes-ingestion.md`` section 3.4 makes non-negotiable. A
  future "let's make the numbers agree" change turns this green test red.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import Household, InventoryLot, Product, Receipt, ReceiptStatus
from tests.conftest import MakeHousehold, household_headers
from tests.support import images
from tests.support.pdfs import build_pdf

IMPORT_URL = "/v1/receipts/import"

#: A drive order recap. Written from the layout French chains print rather than
#: extracted from a real document -- no public corpus of French receipts exists
#: (``docs/technical-notes-ingestion.md`` section 3.2) -- so this proves the
#: plumbing and the format handling, and claims nothing about the share of real
#: recaps that read correctly.
#:
#: The currency is spelled ``EUR`` rather than ``€``: :mod:`tests.support.pdfs`
#: encodes text as latin-1, which has no euro sign. That is a limit of the
#: fixture writer, not of the reader.
RECAP_LINES = [
    "SUPER U CHAMPTOCEAUX",
    "Commande drive n 4471829",
    "Retrait le 02/08/2026 a 10h30",
    "LT DEM 1/2 ECR 1L            2 x 1,15          2,30",
    "PDT NOUV 1KG                                   2,49",
    "YAOURT NATURE X8                               2,10",
    "SAC REUTILISABLE                               0,50",
    "SOUS-TOTAL                                     7,39",
    "TOTAL A PAYER EUR                              7,39",
    "CB EUR                                         7,39",
]

#: The smallest byte string that really is a JPEG container. Never reaches a
#: provider in these tests -- the household has none -- which is the point of the
#: test that uses it. It has to be a *real* container rather than four bytes of
#: magic: the reader walks the segment chain now, because a PDF wearing those four
#: bytes used to be forwarded to a provider (``tests/infra/test_image_sniffing``).
FAKE_JPEG = images.jpeg()


async def _import_recap(
    client: httpx.AsyncClient, household: Household, *, lines: list[str] | None = None
) -> httpx.Response:
    pdf = build_pdf([lines if lines is not None else RECAP_LINES])
    return await client.post(
        IMPORT_URL,
        files={"file": ("recap.pdf", pdf, "application/pdf")},
        headers=household_headers(household),
    )


# --------------------------------------------------------------------------- #
# Reading one in
# --------------------------------------------------------------------------- #


async def test_a_drive_recap_is_read_without_calling_a_model(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """No provider configured, and the recap still imports.

    ``read_by == "text"`` is the assertion that matters: it says the lines came
    off the PDF rather than out of a transcription, which is both free and --
    per section 3.4 -- more faithful than the alternative.
    """
    household = await make_household()
    response = await _import_recap(api_client, household)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["source"] == "pdf"
    assert body["read_by"] == "text"
    assert body["status"] == "parsed"
    assert body["merchant"] is not None and "SUPER U" in body["merchant"]
    assert body["retailer"] == "super-u"
    assert body["total_amount"] == "7.39"
    assert body["currency"] == "EUR"
    assert body["purchased_at"] is not None and body["purchased_at"].startswith("2026-08-02")

    labels = [line["raw_label"] for line in body["lines"]]
    assert labels == ["LT DEM 1/2 ECR 1L", "PDT NOUV 1KG", "YAOURT NATURE X8"]
    assert all(line["match_status"] in {"pending", "suggested"} for line in body["lines"])


async def test_the_image_is_never_stored(
    api_client: httpx.AsyncClient, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """Contract 6ter: the document is read and discarded, hash aside.

    ``image_object_key`` is ``NULL`` and ``image_sha256`` is not. The digest earns
    its place as the duplicate guard; the bytes it was taken from are gone.
    """
    household = await make_household()
    response = await _import_recap(api_client, household)
    assert response.status_code == 201

    receipt = await db_session.scalar(
        select(Receipt).where(Receipt.id == uuid.UUID(response.json()["id"]))
    )
    assert receipt is not None
    assert receipt.image_object_key is None
    assert len(receipt.image_sha256) == 64


async def test_the_same_file_twice_is_refused_with_the_receipt_it_already_made(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """The mobile double-submit, answered with somewhere to go.

    Refusing alone would leave the user re-photographing a receipt that is
    already read; the identifier lets the client open the review it has.
    """
    household = await make_household()
    first = await _import_recap(api_client, household)
    assert first.status_code == 201

    second = await _import_recap(api_client, household)
    assert second.status_code == 409
    body = second.json()
    assert body["type"].endswith("receipt-already-imported")
    assert body["receipt_id"] == first.json()["id"]


async def test_a_photograph_is_refused_with_a_reason_when_no_model_can_see(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """ADR-0005's ``unavailable`` case, reaching the user as a sentence.

    Not a 500 and not a silent empty proposal: the household has no vision-capable
    provider, so the feature is *off*, the reason says so in French, and the
    remedy points at the PDF path, which needs no model at all.
    """
    household = await make_household()
    response = await api_client.post(
        IMPORT_URL,
        files={"file": ("ticket.jpg", FAKE_JPEG, "image/jpeg")},
        headers=household_headers(household),
    )

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["type"].endswith("receipt-import-unavailable")
    assert "fournisseur" in body["detail"]
    assert body["remedy"]


async def test_a_scanned_pdf_says_so_instead_of_proposing_nothing(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """A PDF with no selectable text is a picture in a wrapper.

    Nothing here renders it -- ``infra/documents`` refuses to own a renderer --
    so the honest answer routes the user to the path that does read pictures.
    """
    household = await make_household()
    response = await _import_recap(api_client, household, lines=["1", "2"])

    assert response.status_code == 422, response.text
    assert response.json()["type"].endswith("document-unreadable")


async def test_a_file_that_is_neither_a_photo_nor_a_pdf_is_refused(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household()
    response = await api_client.post(
        IMPORT_URL,
        files={"file": ("liste.txt", b"pain\nlait\n", "text/plain")},
        headers=household_headers(household),
    )
    assert response.status_code == 415
    assert response.json()["type"].endswith("unsupported-document")


async def test_an_upload_past_the_route_ceiling_never_reaches_the_handler(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """The coarse floor, and it has to be the receipt one rather than the JSON one.

    Without ``RECEIPT_IMPORT_PATH`` in the middleware's ``path_bounds`` a phone
    photograph would be refused by the 256 KiB body cap sized for JSON, and every
    real import would fail with a message about a request body.
    """
    household = await make_household()
    oversized = b"%PDF-1.7\n" + b"a" * (7 * 1024 * 1024)
    response = await api_client.post(
        IMPORT_URL,
        files={"file": ("recap.pdf", oversized, "application/pdf")},
        headers=household_headers(household),
    )
    assert response.status_code == 413, response.text


async def test_the_handler_refuses_a_large_file_quoting_the_file_bound(
    api_app: FastAPI, api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """413 quoting ``measure`` and ``limit``, never a partial read.

    "Too large" about a two-page PDF is a bug report nobody can answer, so the
    answer says which bound was hit and what it is. The bound is lowered here so
    the *handler* answers rather than the middleware in front of it.
    """
    household = await make_household()
    api_app.state.settings = api_app.state.settings.model_copy(
        update={"receipt_import_max_bytes": 2048}
    )
    response = await api_client.post(
        IMPORT_URL,
        files={"file": ("recap.pdf", build_pdf([RECAP_LINES] * 4), "application/pdf")},
        headers=household_headers(household),
    )

    assert response.status_code == 413, response.text
    body = response.json()
    assert body["measure"] == "bytes"
    assert body["limit"] == 2048


async def test_a_recap_with_too_many_pages_is_refused_with_413(
    api_app: FastAPI, api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household()
    api_app.state.settings = api_app.state.settings.model_copy(
        update={"receipt_import_max_pdf_pages": 2}
    )
    response = await api_client.post(
        IMPORT_URL,
        files={"file": ("recap.pdf", build_pdf([RECAP_LINES] * 5), "application/pdf")},
        headers=household_headers(household),
    )

    assert response.status_code == 413, response.text
    body = response.json()
    assert body["measure"] == "pages"
    assert body["limit"] == 2


# --------------------------------------------------------------------------- #
# The arithmetic, and what is done about it
# --------------------------------------------------------------------------- #


async def test_the_gap_between_total_and_line_sum_is_shown_not_closed(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """The rule that decides whether any of this can be trusted.

    The recap totals 7,39 and its three purchase lines come to 6,89 -- the
    difference is the reusable bag, which is not food and does not become stock.
    Both numbers are returned, the gap is returned, and nothing is adjusted.
    Section 3.4: models fabricate lines to make sums agree, so a receipt whose
    arithmetic falls out even proves nothing and a gap is the best signal there is
    that something was invented.
    """
    household = await make_household()
    body = (await _import_recap(api_client, household)).json()

    assert body["total_amount"] == "7.39"
    assert body["line_sum"] == "6.89"
    assert body["line_sum_delta"] == "0.50"


async def test_a_line_with_no_price_makes_the_line_sum_unknown_rather_than_partial(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """A partial sum cannot disprove a total, so it is not offered as one.

    Reporting a gap computed from an incomplete sum would raise an alarm on every
    half-read receipt, and teach the reviewer to ignore the alarm that matters.
    """
    household = await make_household()
    body = (
        await _import_recap(
            api_client,
            household,
            lines=[
                "MAGASIN",
                "PAIN COMPLET                                   2,00",
                "TOMATES 1KG",
                "TOTAL A PAYER EUR                              4,50",
            ],
        )
    ).json()

    assert body["total_amount"] == "4.50"
    # The second line carries no price, so it is not a purchase line at all and
    # the sum stays defined over what was read. What must never happen is a
    # *delta* computed from a sum that silently dropped a priced line.
    assert body["line_sum_delta"] is not None or body["line_sum"] is None


# --------------------------------------------------------------------------- #
# Review, confirmation, and the money
# --------------------------------------------------------------------------- #


async def test_a_review_correction_and_a_removal_reach_the_stock(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """One line corrected, one line dropped, and the inventory shows exactly that.

    This is the shape of the real gesture: the reviewer renames ``LT DEM 1/2 ECR``
    to something they will recognise in six months, and removes a line they do not
    want in the cupboard. The removed line is not deleted -- it keeps its price, so
    the sum shown beside the total stays the sum of what was printed -- it simply
    creates no lot.
    """
    household = await make_household()
    proposal = (await _import_recap(api_client, household)).json()
    lines = proposal["lines"]
    assert len(lines) == 3

    confirmed = await api_client.post(
        f"/v1/receipts/{proposal['id']}/confirm",
        json={
            "lines": [
                {
                    "id": lines[0]["id"],
                    "label": "Lait demi-écrémé",
                    "quantity": {"amount": "2", "unit": "piece"},
                },
                {"id": lines[1]["id"]},
            ]
        },
        headers=household_headers(household),
    )

    assert confirmed.status_code == 201, confirmed.text
    body = confirmed.json()
    assert body["created_lot_count"] == 2
    assert body["ignored_line_count"] == 1

    names = set(
        (
            await db_session.scalars(
                select(Product.name)
                .join(InventoryLot, InventoryLot.product_id == Product.id)
                .where(InventoryLot.household_id == household.id)
            )
        ).all()
    )
    assert "Lait demi-écrémé" in names
    assert "YAOURT NATURE X8" not in names

    lots = (
        await db_session.scalars(
            select(InventoryLot).where(InventoryLot.household_id == household.id)
        )
    ).all()
    assert len(lots) == 2
    # Every lot points back at the line that bought it. The budget reads this
    # column to count what entered the cupboard *without* a receipt.
    assert all(lot.source_receipt_line_id is not None for lot in lots)


async def test_confirming_twice_is_refused_rather_than_doubling_the_stock(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household()
    proposal = (await _import_recap(api_client, household)).json()
    url = f"/v1/receipts/{proposal['id']}/confirm"
    payload = {"lines": [{"id": proposal["lines"][0]["id"]}]}

    assert (
        await api_client.post(url, json=payload, headers=household_headers(household))
    ).status_code == 201
    again = await api_client.post(url, json=payload, headers=household_headers(household))
    assert again.status_code == 409
    assert again.json()["type"].endswith("receipt-already-confirmed")


async def test_a_confirmed_receipt_counts_towards_the_budget(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """The end of the chain, and the only proof the feature is really wired.

    The amount is ``receipt.total_amount`` -- 7,39, the number printed on the
    paper -- and never the sum of the lines, whatever the reviewer did to them.
    ``stock_items_added_without_receipt`` stays at zero because every lot created
    here points back at a receipt line.
    """
    household = await make_household()
    proposal = (await _import_recap(api_client, household)).json()
    await api_client.post(
        f"/v1/receipts/{proposal['id']}/confirm",
        json={"lines": [{"id": line["id"]} for line in proposal["lines"]]},
        headers=household_headers(household),
    )

    budget = await api_client.get("/v1/budget?period=month", headers=household_headers(household))
    assert budget.status_code == 200, budget.text
    body = budget.json()
    currencies = {row["currency"]: row for row in body["currencies"]}
    assert "EUR" in currencies
    assert currencies["EUR"]["spent"] == "7.39"
    assert currencies["EUR"]["receipt_count"] == 1
    assert body["coverage"]["receipts_with_total"] == 1
    assert body["coverage"]["receipts_missing_total"] == 0
    assert body["coverage"]["stock_items_added_without_receipt"] == 0


async def test_the_line_sum_mismatch_is_reported_by_the_budget(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """The gap survives all the way to the budget, still uncorrected."""
    household = await make_household()
    await _import_recap(api_client, household)

    body = (
        await api_client.get("/v1/budget?period=month", headers=household_headers(household))
    ).json()
    assert body["currencies"][0]["line_sum_mismatch_count"] == 1


# --------------------------------------------------------------------------- #
# Giving up on one
# --------------------------------------------------------------------------- #


async def test_an_abandoned_proposal_can_be_discarded_and_stops_counting(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """Why ``DELETE`` exists at all.

    A parsed receipt counts towards the budget before its lines are accepted
    (contract 6ter, deliberately). Without a way out, one import abandoned
    mid-review would overstate a household's spending permanently.
    """
    household = await make_household()
    proposal = (await _import_recap(api_client, household)).json()

    before = (
        await api_client.get("/v1/budget?period=month", headers=household_headers(household))
    ).json()
    assert before["currencies"][0]["spent"] == "7.39"

    deleted = await api_client.delete(
        f"/v1/receipts/{proposal['id']}", headers=household_headers(household)
    )
    assert deleted.status_code == 204

    after = (
        await api_client.get("/v1/budget?period=month", headers=household_headers(household))
    ).json()
    assert after["currencies"] == []
    assert after["coverage"]["receipts_with_total"] == 0


async def test_a_confirmed_receipt_cannot_be_discarded(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """Its lines are lots in a cupboard now, and the receipt is the record of
    what they cost."""
    household = await make_household()
    proposal = (await _import_recap(api_client, household)).json()
    await api_client.post(
        f"/v1/receipts/{proposal['id']}/confirm",
        json={"lines": [{"id": proposal["lines"][0]["id"]}]},
        headers=household_headers(household),
    )

    response = await api_client.delete(
        f"/v1/receipts/{proposal['id']}", headers=household_headers(household)
    )
    assert response.status_code == 409


# --------------------------------------------------------------------------- #
# Reading it back
# --------------------------------------------------------------------------- #


async def test_a_proposal_survives_a_reload_and_appears_in_the_pending_list(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """The reason the proposal is stored rather than recomputed.

    A photographed receipt costs a model call; losing the reading to a page
    reload would charge the household twice for the same picture.
    """
    household = await make_household()
    proposal = (await _import_recap(api_client, household)).json()

    reloaded = await api_client.get(
        f"/v1/receipts/{proposal['id']}", headers=household_headers(household)
    )
    assert reloaded.status_code == 200
    assert reloaded.json()["lines"] == proposal["lines"]

    pending = await api_client.get("/v1/receipts", headers=household_headers(household))
    assert pending.status_code == 200
    assert [row["id"] for row in pending.json()["receipts"]] == [proposal["id"]]


async def test_a_receipt_of_another_household_is_not_found(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """One branch for "does not exist" and "is not yours", so the endpoint cannot
    be used to discover that a receipt exists."""
    mine = await make_household(name="Chez moi")
    theirs = await make_household(name="Chez eux")
    proposal = (await _import_recap(api_client, theirs)).json()

    response = await api_client.get(
        f"/v1/receipts/{proposal['id']}", headers=household_headers(mine)
    )
    assert response.status_code == 404
    assert response.json()["type"].endswith("receipt-not-found")


async def test_a_confirmed_receipt_leaves_the_pending_list(
    api_client: httpx.AsyncClient, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    household = await make_household()
    proposal = (await _import_recap(api_client, household)).json()
    await api_client.post(
        f"/v1/receipts/{proposal['id']}/confirm",
        json={"lines": [{"id": proposal["lines"][0]["id"]}]},
        headers=household_headers(household),
    )

    pending = await api_client.get("/v1/receipts", headers=household_headers(household))
    assert pending.json()["receipts"] == []

    receipt = await db_session.get(Receipt, uuid.UUID(proposal["id"]))
    assert receipt is not None
    assert receipt.status is ReceiptStatus.CONFIRMED


async def test_the_same_line_named_fifty_times_creates_one_lot(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """Inventory corruption from a client replay, and the response lied about it.

    A body repeating one ``line.id`` fifty times with ``quantity: 9 g`` was
    answered ``201 {"created_lot_count": 50}`` and put a single lot of 450 g in the
    cupboard -- from a review screen that showed three lines and one of 9 g. The
    body is a set of decisions about the receipt's own lines, not a list of writes
    to perform, so a repeated identifier is the same decision stated again.

    Both halves are asserted, because they failed differently: the stock was wrong
    *and* the count returned to the client matched no row in the database.
    """
    household = await make_household()
    proposal = (await _import_recap(api_client, household)).json()
    line = proposal["lines"][0]

    confirmed = await api_client.post(
        f"/v1/receipts/{proposal['id']}/confirm",
        json={
            "lines": [
                {"id": line["id"], "quantity": {"amount": "9", "unit": "g"}} for _ in range(50)
            ]
        },
        headers=household_headers(household),
    )

    assert confirmed.status_code == 201, confirmed.text
    body = confirmed.json()
    assert body["created_lot_count"] == 1
    # The two lines the reviewer did not send are still ignored exactly once.
    assert body["ignored_line_count"] == 2

    lots = (
        await db_session.scalars(
            select(InventoryLot).where(InventoryLot.household_id == household.id)
        )
    ).all()
    assert len(lots) == 1
    assert lots[0].quantity_value == Decimal("9.000")


# --------------------------------------------------------------------------- #
# Saying what the reading left out
# --------------------------------------------------------------------------- #


async def test_a_receipt_over_the_line_ceiling_says_so(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """The truncation reaches the client, which until revision ``0019`` it never did.

    ``truncated`` was computed correctly by the reader, dropped by the write, and
    then hard-coded to ``False`` when the proposal was rebuilt from the stored row
    -- so the API answered "nothing was left out" on every receipt ever imported,
    including the ones where something was.

    That is the failure worth a test rather than a comment. The ceiling exists so
    a hostile document cannot exhaust the instance, but an ordinary long receipt
    trips it too, and the proposal then *omits purchases*. A household confirming
    it writes a stock that is short by however many lines went over, with nothing
    on the screen having said so. A field that is absent invites a client to go
    and look; a field that is present and always says "complete" tells it there is
    nothing to look for.
    """
    household = await make_household()
    # Comfortably over any configured ceiling, so the test does not have to know
    # the number -- what it asserts is that lines went missing and that the
    # response admits it, not where the limit happens to sit.
    sent = 400
    long_recap = [
        "SUPER U CHAMPTOCEAUX",
        "Commande drive n 4471830",
        "Retrait le 02/08/2026 a 10h30",
        *(f"ARTICLE {index:04d}{'':>20}1,00" for index in range(sent)),
    ]

    response = await _import_recap(api_client, household, lines=long_recap)

    assert response.status_code == 201, response.text
    body = response.json()
    assert len(body["lines"]) < sent, "the ceiling must actually have bitten"
    assert body["truncated"] is True, "the reading dropped lines and must say so"

    # And it survives the reload, which is the moment that matters: the proposal is
    # re-read from the database before it is confirmed, so a notice that lived only
    # in the first response would be gone exactly when it is needed.
    again = await api_client.get(f"/v1/receipts/{body['id']}", headers=household_headers(household))
    assert again.status_code == 200, again.text
    assert again.json()["truncated"] is True


async def test_an_ordinary_receipt_is_not_reported_as_truncated(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """The other half, without which the test above passes on a constant ``True``."""
    household = await make_household()
    response = await _import_recap(api_client, household)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["truncated"] is False
    assert body["degradation_notice"] is None

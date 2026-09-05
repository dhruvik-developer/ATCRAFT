"""``GET /finance/ageing`` over HTTP, with and without a currency.

The multi-currency work this exercises was, for one round, unreachable: the
service grew a ``currency`` argument and a currency-selection rule, and the route
handler passed neither. Every test in this file therefore goes through the real
route rather than calling the service - a service-level test would have gone on
passing while no client could reach any of it.

Two properties are under test, and they pull in opposite directions:

* **No total may mix denominations.** There is no exchange-rate table in this
  schema (``docs/assumptions.md``, open question 3), so rupees added to dollars
  is a number that means nothing - and the report *ranks* parties by exactly
  that number.
* **No caller may be told a company owes nothing when it owes something.** The
  narrow fix for the first property - draw the report in the company's own
  currency and stop - produces precisely that: a company billed entirely in
  dollars reads a clean, confident, empty page.

The seeding is deliberately ORM rows rather than the invoice service: what is
under test is the report, and an approved invoice needs a purchase order, a
receipt and a three-way match that have nothing to do with it.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.finance.models import VendorInvoice, VendorInvoiceStatus
from src.modules.vendors.models import Vendor, VendorType

pytestmark = [pytest.mark.api, pytest.mark.integration]


def mount_module_routers() -> bool:
    """Fill in the module routers the way startup does. True if it did the work.

    ``create_app`` already includes ``api_router`` under the API prefix at import
    time, and ``register_module_routers`` is what puts the module routers *into*
    ``api_router``; the lifespan calls it, and ``httpx``'s ASGI transport does
    not run a lifespan, so without this every request 404s and a test would
    "pass" by never reaching a handler.

    Two things this must not do, both of which the previous version did:

    * **Include ``api_router`` a second time.** ``create_app`` has already done
      it. FastAPI 0.141 keeps an included router by reference rather than
      copying its routes out, so populating it afterwards is enough - and a
      second ``include_router`` mounts a whole duplicate copy of the API.
    * **Run more than once.** The old guard compared ``route.path`` against
      ``/api/v1/finance/ageing`` over ``app.routes``, and under reference
      inclusion those entries are ``_IncludedRouter`` objects whose ``path`` is
      ``None``. Nothing could ever equal the wanted path, so the guard was
      always false and every test in this file mounted all 25 module routers
      again: route growth without a bound, in a process-global app that the rest
      of the suite shares.

    The guard is now ``api_router.routes`` - what ``register_module_routers``
    actually appends to - so it is true the moment the work has been done, by
    this fixture or by anything else.
    """
    from src.api.router import api_router, register_module_routers

    if api_router.routes:
        return False
    register_module_routers()
    return True


@pytest.fixture(scope="session", autouse=True)
def _routes() -> None:
    """Mount the module routers once for the whole session."""
    mount_module_routers()


#: Every invoice below is 90 days overdue as at this date, so a figure that
#: moves between buckets is a real defect rather than a fixture drifting with
#: the calendar.
AS_OF = date(2026, 6, 1)
DUE = date(2026, 5, 20)


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


async def _vendor(session: AsyncSession, company_id: uuid.UUID, name: str) -> Vendor:
    vendor = Vendor(
        company_id=company_id,
        code=_code("V"),
        name=name,
        vendor_types=[VendorType.RAW_MATERIAL_SUPPLIER],
        payment_terms_days=30,
    )
    session.add(vendor)
    await session.flush()
    return vendor


async def _open_invoice(
    session: AsyncSession,
    company_id: uuid.UUID,
    vendor: Vendor,
    *,
    amount: str,
    currency: str,
) -> VendorInvoice:
    """One approved, wholly unpaid bill. Built through the ORM so the Python-side
    column defaults every finance table relies on are actually applied."""
    invoice = VendorInvoice(
        company_id=company_id,
        code=_code("PINV"),
        vendor_id=vendor.id,
        vendor_invoice_number=_code("BILL"),
        invoice_date=date(2026, 4, 20),
        due_date=DUE,
        currency=currency,
        subtotal=Decimal(amount),
        tax_amount=Decimal("0"),
        round_off_amount=Decimal("0"),
        total_amount=Decimal(amount),
        paid_amount=Decimal("0"),
        status=VendorInvoiceStatus.APPROVED,
    )
    session.add(invoice)
    await session.flush()
    return invoice


async def _ageing(client, **params) -> dict:
    response = await client.get("/finance/ageing", params={"as_of": AS_OF.isoformat(), **params})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _block(report: dict, currency: str) -> dict:
    matches = [block for block in report["currencies"] if block["currency"] == currency]
    assert matches, f"{currency} is missing from {[b['currency'] for b in report['currencies']]}"
    return matches[0]


class TestDefaultAnswerOverHttp:
    """No query parameter at all. This is the call every client makes first."""

    async def test_the_ordinary_single_currency_company_reads_its_own_book(
        self, api_client, session: AsyncSession, company
    ) -> None:
        vendor = await _vendor(session, company.id, "Hardware Supplies")
        await _open_invoice(session, company.id, vendor, amount="59000", currency="INR")

        report = await _ageing(api_client)

        assert report["currency"] == "INR"
        assert report["other_currencies"] == []
        assert [row["party_id"] for row in report["rows"]] == [str(vendor.id)]
        assert Decimal(report["total_outstanding"]) == Decimal("59000.00")
        # The top-level figures are a restatement of the first block, not a
        # second and possibly different set.
        assert report["currencies"][0]["currency"] == "INR"
        assert report["currencies"][0]["rows"] == report["rows"]

    async def test_a_book_kept_entirely_in_another_currency_is_not_an_empty_page(
        self, api_client, session: AsyncSession, company
    ) -> None:
        """The reachable state that reported a company with debts as debt-free.

        ``companies.currency`` is INR and no write path derives an invoice's
        currency from it, so a company can perfectly well owe money only in
        dollars. Defaulting the report to the company's currency and stopping
        there returns a confident zero.
        """
        assert company.currency == "INR"
        vendor = await _vendor(session, company.id, "Export Fittings")
        await _open_invoice(session, company.id, vendor, amount="118000", currency="USD")

        report = await _ageing(api_client)

        assert report["currency"] == "USD", "the report fell back to a denomination with no debt"
        assert [row["party_id"] for row in report["rows"]] == [str(vendor.id)]
        assert Decimal(report["total_outstanding"]) == Decimal("118000.00")
        assert Decimal(report["total_bucket_0_30"]) == Decimal("118000.00")
        assert report["other_currencies"] == []
        assert _block(report, "USD")["party_count"] == 1

    async def test_the_companys_own_currency_wins_when_anything_is_open_in_it(
        self, api_client, session: AsyncSession, company
    ) -> None:
        """Even against a foreign balance that is numerically far larger.

        The dollar figure is twice the rupee one *as a number*, and there is no
        rate with which to say what it is worth. The tie-break is therefore not
        "biggest" but "the denomination this company keeps its books in".
        """
        rupees = await _vendor(session, company.id, "Local Brass")
        dollars = await _vendor(session, company.id, "Import Brass")
        await _open_invoice(session, company.id, rupees, amount="59000", currency="INR")
        await _open_invoice(session, company.id, dollars, amount="118000", currency="USD")

        report = await _ageing(api_client)

        assert report["currency"] == "INR"
        assert Decimal(report["total_outstanding"]) == Decimal("59000.00")
        assert report["other_currencies"] == ["USD"]


class TestTwoCurrenciesComeBackUnmixed:
    async def test_neither_the_totals_nor_the_ranking_cross_a_denomination(
        self, api_client, session: AsyncSession, company
    ) -> None:
        """Three vendors, two denominations, and a dollar figure in the middle.

        Ranked on the raw numbers the order would be Import (118,000 USD),
        Local Brass (59,000 INR), Fasteners (11,800 INR) - the dollar vendor
        wedged between two rupee ones on a comparison that has no meaning. The
        rupee block must read [Local Brass, Fasteners] with nothing between
        them, and no total anywhere may equal 188,800.
        """
        local = await _vendor(session, company.id, "Local Brass")
        fasteners = await _vendor(session, company.id, "Fasteners Co")
        importer = await _vendor(session, company.id, "Import Brass")
        await _open_invoice(session, company.id, local, amount="59000", currency="INR")
        await _open_invoice(session, company.id, fasteners, amount="11800", currency="INR")
        await _open_invoice(session, company.id, importer, amount="118000", currency="USD")

        report = await _ageing(api_client)

        inr = _block(report, "INR")
        usd = _block(report, "USD")

        assert [row["party_id"] for row in inr["rows"]] == [str(local.id), str(fasteners.id)]
        assert {row["currency"] for row in inr["rows"]} == {"INR"}
        assert Decimal(inr["total_outstanding"]) == Decimal("70800.00")

        assert [row["party_id"] for row in usd["rows"]] == [str(importer.id)]
        assert Decimal(usd["total_outstanding"]) == Decimal("118000.00")

        mixed = Decimal("188800.00")
        assert all(
            Decimal(block["total_outstanding"]) != mixed for block in report["currencies"]
        ), "a block totalled across two denominations"
        assert Decimal(report["total_outstanding"]) != mixed

    async def test_every_denomination_is_covered_even_when_one_was_asked_for(
        self, api_client, session: AsyncSession, company
    ) -> None:
        """Narrowing chooses what is restated at the top, not what is reported.

        A caller who asks for dollars must not thereby lose sight of a rupee
        debt - that is the same silent omission in a smaller costume.
        """
        local = await _vendor(session, company.id, "Local Brass")
        importer = await _vendor(session, company.id, "Import Brass")
        await _open_invoice(session, company.id, local, amount="59000", currency="INR")
        await _open_invoice(session, company.id, importer, amount="118000", currency="USD")

        report = await _ageing(api_client, currency="usd")

        assert report["currency"] == "USD", "a lower-case code selected a different book"
        assert Decimal(report["total_outstanding"]) == Decimal("118000.00")
        assert report["other_currencies"] == ["INR"]
        assert Decimal(_block(report, "INR")["total_outstanding"]) == Decimal("59000.00")


class TestTheReportRefusesRatherThanReportingZero:
    async def test_a_code_that_is_not_a_code_is_a_client_error(
        self, api_client, session: AsyncSession, company
    ) -> None:
        """``IN1`` is three characters and matches no row.

        Passed through to the ``WHERE`` clause it produces an empty report for a
        company with a full payables book - the failure this whole file exists
        to keep out, arriving through the front door.
        """
        vendor = await _vendor(session, company.id, "Local Brass")
        await _open_invoice(session, company.id, vendor, amount="59000", currency="INR")

        response = await api_client.get(
            "/finance/ageing", params={"as_of": AS_OF.isoformat(), "currency": "IN1"}
        )

        assert 400 <= response.status_code < 500, response.text

    async def test_a_currency_asked_for_with_nothing_open_in_it_still_names_the_debt(
        self, api_client, session: AsyncSession, company
    ) -> None:
        """ "We owe nothing in euros" is a real answer - but not the whole one."""
        vendor = await _vendor(session, company.id, "Local Brass")
        await _open_invoice(session, company.id, vendor, amount="59000", currency="INR")

        report = await _ageing(api_client, currency="EUR")

        assert report["currency"] == "EUR"
        assert report["rows"] == []
        assert Decimal(report["total_outstanding"]) == Decimal("0.00")
        # The invariant that keeps the zero honest: the block that was asked for
        # is there, and so is the money.
        assert report["currencies"][0]["currency"] == "EUR"
        assert report["other_currencies"] == ["INR"]
        assert Decimal(_block(report, "INR")["total_outstanding"]) == Decimal("59000.00")


class TestTheOutstandingListReconcilesWithABlock:
    async def test_the_currency_filter_is_reachable_on_the_payables_list(
        self, api_client, session: AsyncSession, company
    ) -> None:
        """The two reports are read side by side, so both need the same knob."""
        local = await _vendor(session, company.id, "Local Brass")
        importer = await _vendor(session, company.id, "Import Brass")
        await _open_invoice(session, company.id, local, amount="59000", currency="INR")
        await _open_invoice(session, company.id, importer, amount="118000", currency="USD")

        everything = await api_client.get(
            "/finance/outstanding", params={"as_of": AS_OF.isoformat()}
        )
        assert everything.status_code == 200, everything.text
        assert sorted(row["currency"] for row in everything.json()["data"]) == ["INR", "USD"]

        narrowed = await api_client.get(
            "/finance/outstanding", params={"as_of": AS_OF.isoformat(), "currency": "usd"}
        )
        assert narrowed.status_code == 200, narrowed.text
        rows = narrowed.json()["data"]
        assert [row["currency"] for row in rows] == ["USD"]
        assert Decimal(rows[0]["outstanding_amount"]) == Decimal("118000.00")


class TestTheHeadlineDenominationIsNotChosenByMagnitude:
    """The default answer for a company that keeps no book in its own currency.

    Its whole difficulty is that the fallback has to pick *something*, and the
    obvious something - the biggest outstanding - is a comparison between two
    denominations with no exchange rate between them. Over HTTP because that is
    where the report is read, and because the choice is made from a query the
    route builds.
    """

    async def test_the_bigger_number_does_not_take_the_headline(
        self, api_client, session: AsyncSession, company
    ) -> None:
        """One dollar bill for 118,000; three dirham bills for 4,000 each.

        The company's own currency is INR and nothing is open in it, so the
        report falls through to the denominations that are. 118,000 > 12,000 as
        arithmetic and means nothing as money: the schema holds no rate. The
        report leads with the denomination the company actually transacts in -
        three open bills against one - and every figure it prints stays inside
        its own denomination either way.
        """
        assert company.currency == "INR"
        importer = await _vendor(session, company.id, "Import Brass")
        await _open_invoice(session, company.id, importer, amount="118000", currency="USD")
        for index in range(3):
            gulf = await _vendor(session, company.id, f"Gulf Fittings {index}")
            await _open_invoice(session, company.id, gulf, amount="4000", currency="AED")

        report = await _ageing(api_client)

        assert report["currency"] == "AED", (
            "the headline denomination was picked by comparing 118,000 USD "
            "against 12,000 AED - amounts in different currencies"
        )
        assert Decimal(report["total_outstanding"]) == Decimal("12000.00")
        assert report["other_currencies"] == ["USD"]
        # Nothing was lost by not leading with it: the dollar bill is still
        # reported, in dollars.
        assert Decimal(_block(report, "USD")["total_outstanding"]) == Decimal("118000.00")
        assert _block(report, "AED")["party_count"] == 3
        # The figure the ordering was decided on is in the response, so a reader
        # can see why AED leads rather than having to infer it.
        assert _block(report, "AED")["document_count"] == 3
        assert _block(report, "USD")["document_count"] == 1

    async def test_the_ranking_survives_the_dollar_figure_growing(
        self, api_client, session: AsyncSession, company
    ) -> None:
        """A thousand-fold larger foreign number must not move the headline.

        This is the property, stated as directly as an HTTP test can: the choice
        does not read the amounts at all.
        """
        importer = await _vendor(session, company.id, "Import Brass")
        await _open_invoice(session, company.id, importer, amount="118000000", currency="USD")
        for index in range(2):
            gulf = await _vendor(session, company.id, f"Gulf Fittings {index}")
            await _open_invoice(session, company.id, gulf, amount="1", currency="AED")

        report = await _ageing(api_client)

        assert report["currency"] == "AED"
        assert Decimal(_block(report, "USD")["total_outstanding"]) == Decimal("118000000.00")


class TestTheRouteTableDoesNotGrow:
    """The fixture above mounts 25 module routers onto a process-global app.

    Its idempotence guard used to compare ``route.path`` against
    ``/api/v1/finance/ageing`` over ``app.routes``. Under FastAPI's
    include-by-reference routing those entries carry no ``path`` at all, so the
    comparison was always false and every test in this file mounted the whole
    API again - unbounded growth in an object every other HTTP test shares, and
    a plausible cause of a failure in a suite that never touched finance.
    """

    def test_mounting_again_is_a_no_op(self) -> None:
        from src.api.router import api_router
        from src.main import app

        before = (len(app.routes), len(api_router.routes))
        assert before[1] > 0, "the session fixture did not mount the module routers"

        assert mount_module_routers() is False, "the guard did not recognise the mounted routers"

        assert (len(app.routes), len(api_router.routes)) == before

    def test_no_module_is_mounted_twice(self) -> None:
        """The invariant, independent of how the mounting happened.

        ``register_module_routers`` appends one entry per module, so a second
        pass over an already-populated router shows up here as a count larger
        than the number of modules that exist.
        """
        from src.api.router import MODULE_ORDER, api_router

        assert len(api_router.routes) <= len(MODULE_ORDER)

    async def test_the_endpoint_still_answers_after_the_no_op(
        self, api_client, session: AsyncSession, company
    ) -> None:
        """Registering once has to be registering at all."""
        mount_module_routers()
        vendor = await _vendor(session, company.id, "Local Brass")
        await _open_invoice(session, company.id, vendor, amount="59000", currency="INR")

        report = await _ageing(api_client)

        assert report["currency"] == "INR"
        assert [row["party_id"] for row in report["rows"]] == [str(vendor.id)]

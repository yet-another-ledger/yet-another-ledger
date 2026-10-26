"""Integration tests for M5 step 3 – Invoice CRUD / list / status / product options.

Requires a running PostgreSQL instance (``pytest -m integration``).

Test coverage:
- Happy flow: create (LINE / DOCUMENT), list, get, update, delete
- Number allocation: deferred to DRAFT->SENT issue; create yields no number,
  jump sequence, delete-draft leaves no gap
- LINE mode: per-line taxes with correct amounts
- DOCUMENT mode: single document tax entry
- Incl-VAT: reverse calculation round-trip
- Line + document discount combo
- VAT treatment derivation snapshots stored correctly
- Cascade delete: invoice deletion removes lines/taxes
- Cross-company 404 / isolation
- Status transitions: DRAFT→SENT, DRAFT→CANCELLED, CANCELLED→DRAFT
- Invalid transitions: COMPLETED cannot be set manually; SENT→CANCELLED blocked
- CANCELLED/COMPLETED cannot be edited
- Customer / vat_rate / treatment FK constraints
- Product options endpoint: never leaks cost/margin/supplier
- Auth: unauthenticated → 401, no company → 400
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jai.db import Base
from jai.models._enums import VatTreatmentEffect
from jai.models.company import Company
from jai.models.document import InvoiceCreditBasisLine
from jai.models.invoice import Invoice, InvoiceLine, InvoiceLineTax
from jai.models.user import User
from jai.models.vat import VatRate, VatTreatment
from jai.services.dictionary import seed_for_company as seed_dictionaries
from jai.services.vat import seed_for_company as seed_vat

# ---------------------------------------------------------------------------
# Shared helpers (same as calculate integration tests)
# ---------------------------------------------------------------------------


async def _full_auth(
    client: AsyncClient,
    email: str = "owner@example.com",
    password: str = "testpassword1",
) -> None:
    resp = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": password}
    )
    assert resp.status_code == 201

    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    assert resp.status_code == 200

    resp = await client.post("/api/v1/auth/mfa/setup")
    assert resp.status_code == 200
    secret = resp.json()["secret"]

    code = pyotp.TOTP(secret).now()
    resp = await client.post("/api/v1/auth/mfa/verify", json={"code": code})
    assert resp.status_code == 204


async def _setup_company(client: AsyncClient, country_code: str = "NL") -> dict:
    resp = await client.put(
        "/api/v1/company",
        json={
            "name": "Test Invoice Co",
            "base_currency": "EUR",
            "country_code": country_code,
        },
    )
    assert resp.status_code == 200

    rates_resp = await client.get("/api/v1/vat-rates")
    rates = rates_resp.json()["items"]
    by_label = {r["label"]: r for r in rates}

    treatments_resp = await client.get("/api/v1/vat-treatments?side=SALES")
    treatments = treatments_resp.json()["items"]
    by_code = {t["code"]: t for t in treatments}

    return {"rates": by_label, "treatments": by_code}


async def _create_customer(
    client: AsyncClient,
    *,
    name: str = "Test Customer",
    vat_id: str | None = None,
    country_code: str = "NL",
    invoice_prefix: str | None = None,
) -> str:
    payload: dict = {
        "name": name,
        "vat_id": vat_id,
        "addresses": [
            {"type": "BILLING", "country_code": country_code, "city": "Amsterdam"},
        ],
    }
    if invoice_prefix:
        payload["invoice_prefix"] = invoice_prefix
    resp = await client.post("/api/v1/customers", json=payload)
    assert resp.status_code == 201
    return resp.json()["id"]


def _line_payload(
    rate_id: str,
    *,
    name: str = "Service",
    quantity: str = "1",
    unit_price: str = "100.000",
    discount: dict | None = None,
) -> dict:
    p: dict = {
        "name": name,
        "quantity": quantity,
        "unit_price": unit_price,
        "vat_rate_id": rate_id,
    }
    if discount:
        p["discount"] = discount
    return p


def _invoice_payload(
    customer_id: str,
    rate_id: str,
    *,
    tax_mode: str = "LINE",
    amounts_include_vat: bool = False,
    treatment_id: str | None = None,
    document_vat_rate_id: str | None = None,
    discount: dict | None = None,
    lines: list[dict] | None = None,
    invoice_date: str = "2026-06-10",
    due_date: str | None = None,
    notes: str | None = None,
) -> dict:
    if lines is None:
        lines = [_line_payload(rate_id)]
    payload: dict = {
        "customer_id": customer_id,
        "invoice_date": invoice_date,
        "tax_mode": tax_mode,
        "amounts_include_vat": amounts_include_vat,
        "lines": lines,
    }
    if treatment_id:
        payload["vat_treatment_id"] = treatment_id
    if document_vat_rate_id:
        payload["document_vat_rate_id"] = document_vat_rate_id
    if discount:
        payload["discount"] = discount
    if due_date:
        payload["due_date"] = due_date
    if notes:
        payload["notes"] = notes
    return payload


async def _issue_invoice(client: AsyncClient, invoice_id: str) -> dict:
    """Issue (DRAFT -> SENT) an invoice; the number is allocated on issue."""
    resp = await client.post(
        f"/api/v1/invoices/{invoice_id}/status", json={"status": "SENT"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _all_business_rows(
    session_maker: async_sessionmaker[AsyncSession],
) -> tuple[list[tuple[str, tuple[tuple[object, ...], ...]]], list[tuple[object, ...]]]:
    """Snapshot every mapped table and sequence state around a read-only preview."""
    async with session_maker() as session:
        tables: list[tuple[str, tuple[tuple[object, ...], ...]]] = []
        for table in sorted(Base.metadata.tables.values(), key=lambda table: table.name):
            statement = select(table)
            if table.primary_key.columns:
                statement = statement.order_by(*table.primary_key.columns)
            rows = (await session.execute(statement)).all()
            tables.append((table.name, tuple(tuple(row) for row in rows)))
        sequences = (
            await session.execute(
                text(
                    "SELECT sequencename, last_value FROM pg_sequences "
                    "WHERE schemaname = current_schema() ORDER BY sequencename"
                )
            )
        ).all()
        return tables, [tuple(row) for row in sequences]


async def _create_issued_invoice(
    client: AsyncClient,
    customer_id: str,
    rate_id: str,
    **options: object,
) -> dict:
    created = await client.post(
        "/api/v1/invoices",
        json=_invoice_payload(customer_id, rate_id, **options),
    )
    assert created.status_code == 201, created.text
    return await _issue_invoice(client, created.json()["id"])


async def _revision_preview(
    client: AsyncClient, invoice: dict, changes: dict
) -> object:
    return await client.post(
        f"/api/v1/invoices/{invoice['id']}/revisions/calculate",
        json={"expected_updated_at": invoice["updated_at"], "changes": changes},
    )


# ---------------------------------------------------------------------------
# Happy flow: CRUD
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestInvoiceCRUDHappyFlow:

    async def test_create_and_get_line_mode(self, db_client: AsyncClient) -> None:
        """Create a LINE-mode invoice, verify amounts, then GET it back."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        create_resp = await db_client.post(
            "/api/v1/invoices",
            json=_invoice_payload(customer_id, rate_21),
        )
        assert create_resp.status_code == 201
        inv = create_resp.json()

        assert inv["status"] == "DRAFT"
        assert inv["paid_status"] == "UNPAID"
        assert inv["tax_mode"] == "LINE"
        assert inv["subtotal_excl_vat"] == "100.000"
        assert inv["vat_total"] == "21.000"
        assert inv["total_incl_vat"] == "121.000"
        assert inv["due_amount"] == "121.000"
        # A fresh DRAFT carries no number; it is allocated only at issue.
        assert inv["invoice_number"] is None
        assert inv["sequence_number"] is None
        assert len(inv["lines"]) == 1
        assert len(inv["lines"][0]["line_taxes"]) == 1

        # Issuing the invoice allocates the legal number.
        issued = await _issue_invoice(db_client, inv["id"])
        assert issued["invoice_number"].startswith("INV-")
        assert issued["sequence_number"] == 1

        # Verify treatment snapshot
        snap = inv["vat_treatment_snapshot"]
        assert snap["code"] == "NL_DOMESTIC"
        assert snap["effect"] == "APPLY_RATE"

        # GET by id
        get_resp = await db_client.get(f"/api/v1/invoices/{inv['id']}")
        assert get_resp.status_code == 200
        fetched = get_resp.json()
        assert fetched["id"] == inv["id"]
        assert fetched["total_incl_vat"] == "121.000"

    async def test_create_document_mode(self, db_client: AsyncClient) -> None:
        """DOCUMENT mode with 2 lines + 21% document rate."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        lines = [
            {"name": "A", "quantity": "1", "unit_price": "100"},
            {"name": "B", "quantity": "2", "unit_price": "50"},
        ]
        resp = await db_client.post(
            "/api/v1/invoices",
            json=_invoice_payload(
                customer_id, rate_21,
                tax_mode="DOCUMENT",
                document_vat_rate_id=rate_21,
                lines=lines,
            ),
        )
        assert resp.status_code == 201
        inv = resp.json()
        assert inv["tax_mode"] == "DOCUMENT"
        assert inv["subtotal_excl_vat"] == "200.000"
        assert inv["vat_total"] == "42.000"
        assert inv["total_incl_vat"] == "242.000"
        # Document-level taxes
        assert len(inv["taxes"]) == 1
        assert inv["taxes"][0]["vat_rate_percent"] == "21.000"

    async def test_update_invoice_preserves_number(self, db_client: AsyncClient) -> None:
        """PUT /invoices/{id} must preserve the numbering fields verbatim.

        Only DRAFT invoices are editable, and a DRAFT carries no number yet, so
        the preserved value is None: editing a draft must never accidentally
        allocate a number (that only happens at DRAFT -> SENT).
        """
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]
        rate_9 = seeds["rates"]["NL reduced (9%)"]["id"]

        create_resp = await db_client.post(
            "/api/v1/invoices",
            json=_invoice_payload(customer_id, rate_21),
        )
        created = create_resp.json()
        assert created["invoice_number"] is None
        inv_id = created["id"]

        update_resp = await db_client.put(
            f"/api/v1/invoices/{inv_id}",
            json=_invoice_payload(
                customer_id, rate_9,
                lines=[{
                    "name": "Updated Service",
                    "quantity": "2",
                    "unit_price": "50",
                    "vat_rate_id": rate_9,
                }],
            ),
        )
        assert update_resp.status_code == 200
        updated = update_resp.json()
        # Editing a draft must not allocate a number.
        assert updated["invoice_number"] is None
        assert updated["sequence_number"] is None
        assert updated["vat_total"] == "9.000"  # 100 * 9%
        assert updated["total_incl_vat"] == "109.000"

    async def test_delete_invoice(self, db_client: AsyncClient) -> None:
        """DELETE /invoices/{id} removes invoice and returns 204."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        create_resp = await db_client.post(
            "/api/v1/invoices",
            json=_invoice_payload(customer_id, rate_21),
        )
        inv_id = create_resp.json()["id"]

        del_resp = await db_client.delete(f"/api/v1/invoices/{inv_id}")
        assert del_resp.status_code == 204

        get_resp = await db_client.get(f"/api/v1/invoices/{inv_id}")
        assert get_resp.status_code == 404

    async def test_list_invoices(self, db_client: AsyncClient) -> None:
        """Create 3 invoices, verify list pagination and filtering."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        for _ in range(3):
            await db_client.post(
                "/api/v1/invoices",
                json=_invoice_payload(customer_id, rate_21),
            )

        list_resp = await db_client.get("/api/v1/invoices")
        assert list_resp.status_code == 200
        data = list_resp.json()
        assert data["total"] == 3
        assert len(data["items"]) == 3

        # Pagination
        page1 = await db_client.get("/api/v1/invoices?limit=2&offset=0")
        assert page1.json()["total"] == 3
        assert len(page1.json()["items"]) == 2

        page2 = await db_client.get("/api/v1/invoices?limit=2&offset=2")
        assert len(page2.json()["items"]) == 1

    async def test_list_filter_by_customer(self, db_client: AsyncClient) -> None:
        """List filters by customer_id."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        c1 = await _create_customer(db_client, name="Customer 1")
        c2 = await _create_customer(db_client, name="Customer 2")
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        await db_client.post("/api/v1/invoices", json=_invoice_payload(c1, rate_21))
        await db_client.post("/api/v1/invoices", json=_invoice_payload(c1, rate_21))
        await db_client.post("/api/v1/invoices", json=_invoice_payload(c2, rate_21))

        resp = await db_client.get(f"/api/v1/invoices?customer_id={c1}")
        data = resp.json()
        assert data["total"] == 2
        for item in data["items"]:
            assert item["customer_id"] == c1

    async def test_list_filter_by_status(self, db_client: AsyncClient) -> None:
        """List filters by status."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        create_resp = await db_client.post(
            "/api/v1/invoices",
            json=_invoice_payload(customer_id, rate_21),
        )
        inv_id = create_resp.json()["id"]
        await db_client.post(
            f"/api/v1/invoices/{inv_id}/status", json={"status": "SENT"}
        )
        await db_client.post(
            "/api/v1/invoices",
            json=_invoice_payload(customer_id, rate_21),
        )

        # Filter DRAFT
        draft_resp = await db_client.get("/api/v1/invoices?status=DRAFT")
        assert draft_resp.json()["total"] == 1

        # Filter SENT
        sent_resp = await db_client.get("/api/v1/invoices?status=SENT")
        assert sent_resp.json()["total"] == 1

    async def test_list_search_by_invoice_number(self, db_client: AsyncClient) -> None:
        """Search by invoice number prefix."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        resp = await db_client.post(
            "/api/v1/invoices",
            json=_invoice_payload(customer_id, rate_21),
        )
        # Numbers are only allocated on issue; issue first, then search by it.
        issued = await _issue_invoice(db_client, resp.json()["id"])
        number = issued["invoice_number"]

        search_resp = await db_client.get(f"/api/v1/invoices?q={number}")
        assert search_resp.json()["total"] == 1

        # Non-matching search
        no_resp = await db_client.get("/api/v1/invoices?q=XXXXNOTEXIST")
        assert no_resp.json()["total"] == 0

    async def test_list_customer_name_in_items(self, db_client: AsyncClient) -> None:
        """List items include customer_name from join."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client, name="Acme Corp")
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        await db_client.post("/api/v1/invoices", json=_invoice_payload(customer_id, rate_21))

        resp = await db_client.get("/api/v1/invoices")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["customer_name"] == "Acme Corp"

    async def test_list_search_by_customer_name(self, db_client: AsyncClient) -> None:
        """Search q= also matches customer name."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        c1 = await _create_customer(db_client, name="Searchable Corp")
        c2 = await _create_customer(db_client, name="Other Co")
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        await db_client.post("/api/v1/invoices", json=_invoice_payload(c1, rate_21))
        await db_client.post("/api/v1/invoices", json=_invoice_payload(c2, rate_21))

        # Search by partial customer name
        search_resp = await db_client.get("/api/v1/invoices?q=Searchable")
        assert search_resp.json()["total"] == 1
        assert search_resp.json()["items"][0]["customer_name"] == "Searchable Corp"

        # Non-matching search
        no_resp = await db_client.get("/api/v1/invoices?q=XXXXNOTEXIST")
        assert no_resp.json()["total"] == 0


# ---------------------------------------------------------------------------
# Numbering: deferred to issue (DRAFT->SENT); delete-draft leaves no gap
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestInvoiceNumbering:

    async def test_create_yields_no_number(self, db_client: AsyncClient) -> None:
        """A freshly created invoice (DRAFT) has no number/sequence yet."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        resp = await db_client.post(
            "/api/v1/invoices",
            json=_invoice_payload(customer_id, rate_21),
        )
        assert resp.status_code == 201
        inv = resp.json()
        assert inv["invoice_number"] is None
        assert inv["sequence_number"] is None

    async def test_first_invoice_gets_default_number_on_issue(
        self, db_client: AsyncClient
    ) -> None:
        """Issuing the first invoice (default config) allocates INV-000001."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        resp = await db_client.post(
            "/api/v1/invoices",
            json=_invoice_payload(customer_id, rate_21),
        )
        assert resp.status_code == 201
        issued = await _issue_invoice(db_client, resp.json()["id"])
        assert issued["invoice_number"] == "INV-000001"
        assert issued["sequence_number"] == 1

        # The company sequence advanced by exactly 1 (next is now 2).
        seq_resp = await db_client.get("/api/v1/settings/invoice-number-sequence")
        assert seq_resp.json()["next_sequence"] == 2

    async def test_sequential_numbering_on_issue(self, db_client: AsyncClient) -> None:
        """Two issued invoices get consecutive numbers (no duplicate)."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        resp1 = await db_client.post(
            "/api/v1/invoices", json=_invoice_payload(customer_id, rate_21)
        )
        resp2 = await db_client.post(
            "/api/v1/invoices", json=_invoice_payload(customer_id, rate_21)
        )
        # Both are unnumbered drafts until issued.
        assert resp1.json()["invoice_number"] is None
        assert resp2.json()["invoice_number"] is None

        issued1 = await _issue_invoice(db_client, resp1.json()["id"])
        issued2 = await _issue_invoice(db_client, resp2.json()["id"])
        assert issued1["invoice_number"] == "INV-000001"
        assert issued2["invoice_number"] == "INV-000002"
        assert issued1["invoice_number"] != issued2["invoice_number"]

    async def test_delete_draft_leaves_no_gap(self, db_client: AsyncClient) -> None:
        """Core regression: deleting an unissued draft skips no number.

        Create a draft (no number) → delete it → create another draft → issue
        it. The issued invoice must receive INV-000001 (the very first number),
        because the deleted draft never consumed the company sequence.
        """
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        # Company sequence starts at 1 and has not moved.
        seq_before = await db_client.get("/api/v1/settings/invoice-number-sequence")
        assert seq_before.json()["next_sequence"] == 1

        resp1 = await db_client.post(
            "/api/v1/invoices", json=_invoice_payload(customer_id, rate_21)
        )
        inv1_id = resp1.json()["id"]
        assert resp1.json()["invoice_number"] is None
        del_resp = await db_client.delete(f"/api/v1/invoices/{inv1_id}")
        assert del_resp.status_code == 204

        # The sequence has NOT advanced because of the deleted draft.
        seq_after_delete = await db_client.get(
            "/api/v1/settings/invoice-number-sequence"
        )
        assert seq_after_delete.json()["next_sequence"] == 1

        resp2 = await db_client.post(
            "/api/v1/invoices", json=_invoice_payload(customer_id, rate_21)
        )
        issued2 = await _issue_invoice(db_client, resp2.json()["id"])
        # No gap inherited from the deleted draft: it gets the first number.
        assert issued2["invoice_number"] == "INV-000001"
        assert issued2["sequence_number"] == 1

    async def test_reissue_allocates_number_only_once(
        self, db_client: AsyncClient
    ) -> None:
        """DRAFT->CANCELLED->DRAFT->SENT allocates exactly one number.

        An invoice issued after a cancel/reactivate cycle still consumes only a
        single sequence value (idempotent on already-numbered invoices).
        """
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        resp = await db_client.post(
            "/api/v1/invoices", json=_invoice_payload(customer_id, rate_21)
        )
        inv_id = resp.json()["id"]

        # DRAFT -> CANCELLED -> DRAFT (never numbered yet).
        await db_client.post(
            f"/api/v1/invoices/{inv_id}/status", json={"status": "CANCELLED"}
        )
        await db_client.post(
            f"/api/v1/invoices/{inv_id}/status", json={"status": "DRAFT"}
        )

        issued = await _issue_invoice(db_client, inv_id)
        assert issued["invoice_number"] == "INV-000001"
        assert issued["sequence_number"] == 1

        # Sequence advanced by exactly one (next is 2).
        seq_resp = await db_client.get("/api/v1/settings/invoice-number-sequence")
        assert seq_resp.json()["next_sequence"] == 2

    async def test_jump_sequence_with_settings(self, db_client: AsyncClient) -> None:
        """Set start=100 → first issued invoice is INV-000100; jump→200 issues INV-000200."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        # Set sequence_start = 100
        await db_client.put(
            "/api/v1/settings/numbering",
            json={"template": "{{SERIES:INV}}-{{SEQUENCE:6}}", "sequence_start": 100},
        )

        resp1 = await db_client.post(
            "/api/v1/invoices", json=_invoice_payload(customer_id, rate_21)
        )
        issued1 = await _issue_invoice(db_client, resp1.json()["id"])
        assert issued1["invoice_number"] == "INV-000100"

        # Jump to 200
        await db_client.put(
            "/api/v1/settings/invoice-number-sequence",
            json={"next_sequence": 200},
        )
        resp2 = await db_client.post(
            "/api/v1/invoices", json=_invoice_payload(customer_id, rate_21)
        )
        issued2 = await _issue_invoice(db_client, resp2.json()["id"])
        assert issued2["invoice_number"] == "INV-000200"


# ---------------------------------------------------------------------------
# Pricing: LINE / DOCUMENT / incl-VAT / discounts
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestInvoicePricing:

    async def test_line_mode_per_rate_taxes(self, db_client: AsyncClient) -> None:
        """LINE mode with 21% + 9% lines produces correct tax amounts."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]
        rate_9 = seeds["rates"]["NL reduced (9%)"]["id"]

        resp = await db_client.post(
            "/api/v1/invoices",
            json=_invoice_payload(
                customer_id, rate_21,
                lines=[
                    _line_payload(rate_21, name="A", unit_price="100"),
                    _line_payload(rate_9, name="B", unit_price="100"),
                ],
            ),
        )
        assert resp.status_code == 201
        inv = resp.json()
        assert inv["subtotal_excl_vat"] == "200.000"
        assert inv["vat_total"] == "30.000"
        assert inv["total_incl_vat"] == "230.000"
        # Taxes stored in lines
        assert len(inv["lines"]) == 2

    async def test_document_mode_single_tax(self, db_client: AsyncClient) -> None:
        """DOCUMENT mode stores one invoice_tax row, no line_taxes."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        resp = await db_client.post(
            "/api/v1/invoices",
            json=_invoice_payload(
                customer_id, rate_21,
                tax_mode="DOCUMENT",
                document_vat_rate_id=rate_21,
                lines=[
                    {"name": "A", "quantity": "1", "unit_price": "100"},
                    {"name": "B", "quantity": "1", "unit_price": "100"},
                ],
            ),
        )
        assert resp.status_code == 201
        inv = resp.json()
        assert len(inv["taxes"]) == 1
        assert inv["taxes"][0]["tax_amount"] == "42.000"

    async def test_incl_vat_input(self, db_client: AsyncClient) -> None:
        """amounts_include_vat=True: €121 incl→ €100 excl + €21 VAT."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        resp = await db_client.post(
            "/api/v1/invoices",
            json=_invoice_payload(
                customer_id, rate_21,
                amounts_include_vat=True,
                lines=[{"name": "X", "quantity": "1", "unit_price": "121", "vat_rate_id": rate_21}],
            ),
        )
        assert resp.status_code == 201
        inv = resp.json()
        assert inv["taxable_amount"] == "100.000"
        assert inv["vat_total"] == "21.000"
        assert inv["total_incl_vat"] == "121.000"

    async def test_line_and_document_discount(self, db_client: AsyncClient) -> None:
        """Line 10% discount + document 10% discount cascade correctly."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        resp = await db_client.post(
            "/api/v1/invoices",
            json=_invoice_payload(
                customer_id, rate_21,
                discount={"type": "PERCENTAGE", "value": "10"},
                lines=[_line_payload(
                    rate_21,
                    unit_price="100",
                    discount={"type": "PERCENTAGE", "value": "10"},
                )],
            ),
        )
        assert resp.status_code == 201
        inv = resp.json()
        assert inv["line_discount_total"] == "10.000"
        assert inv["document_discount_amount"] == "9.000"
        assert inv["taxable_amount"] == "81.000"

    async def test_zero_rate_treatment_vat_is_zero(self, db_client: AsyncClient) -> None:
        """EU B2B reverse → 21% rate but effective VAT = 0."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client, country_code="DE", vat_id="DE123456789")
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        resp = await db_client.post(
            "/api/v1/invoices",
            json=_invoice_payload(customer_id, rate_21),
        )
        assert resp.status_code == 201
        inv = resp.json()
        assert inv["vat_total"] == "0.000"
        assert inv["total_incl_vat"] == "100.000"
        snap = inv["vat_treatment_snapshot"]
        assert snap["code"] == "EU_B2B_REVERSE"
        assert snap["requires_icp"] is True

    async def test_amounts_persist_after_refresh(self, db_client: AsyncClient) -> None:
        """Amounts loaded from DB match the create response (no drift)."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        create_resp = await db_client.post(
            "/api/v1/invoices",
            json=_invoice_payload(customer_id, rate_21),
        )
        inv_id = create_resp.json()["id"]
        created = create_resp.json()

        get_resp = await db_client.get(f"/api/v1/invoices/{inv_id}")
        fetched = get_resp.json()

        for field in ["subtotal_excl_vat", "vat_total", "total_incl_vat", "due_amount"]:
            assert fetched[field] == created[field], f"Field {field} drifted after refresh"


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestInvoiceStatusTransitions:

    async def test_draft_to_sent(self, db_client: AsyncClient) -> None:
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        resp = await db_client.post(
            "/api/v1/invoices", json=_invoice_payload(customer_id, rate_21)
        )
        inv_id = resp.json()["id"]

        status_resp = await db_client.post(
            f"/api/v1/invoices/{inv_id}/status", json={"status": "SENT"}
        )
        assert status_resp.status_code == 200
        assert status_resp.json()["status"] == "SENT"

    async def test_draft_to_cancelled(self, db_client: AsyncClient) -> None:
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        resp = await db_client.post(
            "/api/v1/invoices", json=_invoice_payload(customer_id, rate_21)
        )
        inv_id = resp.json()["id"]

        cancel_resp = await db_client.post(
            f"/api/v1/invoices/{inv_id}/status", json={"status": "CANCELLED"}
        )
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["status"] == "CANCELLED"

    async def test_sent_cannot_be_cancelled(self, db_client: AsyncClient) -> None:
        """SENT → CANCELLED is blocked; use a credit note instead (M9/M10)."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        resp = await db_client.post(
            "/api/v1/invoices", json=_invoice_payload(customer_id, rate_21)
        )
        inv_id = resp.json()["id"]
        await db_client.post(
            f"/api/v1/invoices/{inv_id}/status", json={"status": "SENT"}
        )

        cancel_resp = await db_client.post(
            f"/api/v1/invoices/{inv_id}/status", json={"status": "CANCELLED"}
        )
        assert cancel_resp.status_code == 409
        assert cancel_resp.json()["detail"]["code"] == "INVOICE_LIFECYCLE_CONFLICT"

    async def test_cancelled_can_be_reactivated(self, db_client: AsyncClient) -> None:
        """CANCELLED → DRAFT is allowed when the invoice was never sent."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        resp = await db_client.post(
            "/api/v1/invoices", json=_invoice_payload(customer_id, rate_21)
        )
        inv_id = resp.json()["id"]
        await db_client.post(
            f"/api/v1/invoices/{inv_id}/status", json={"status": "CANCELLED"}
        )

        reactivate_resp = await db_client.post(
            f"/api/v1/invoices/{inv_id}/status", json={"status": "DRAFT"}
        )
        assert reactivate_resp.status_code == 200
        assert reactivate_resp.json()["status"] == "DRAFT"

    async def test_completed_cannot_be_set_manually(self, db_client: AsyncClient) -> None:
        """COMPLETED is M7-reserved; manual attempt is a lifecycle conflict."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        resp = await db_client.post(
            "/api/v1/invoices", json=_invoice_payload(customer_id, rate_21)
        )
        inv_id = resp.json()["id"]

        completed_resp = await db_client.post(
            f"/api/v1/invoices/{inv_id}/status", json={"status": "COMPLETED"}
        )
        assert completed_resp.status_code == 409
        assert completed_resp.json()["detail"]["code"] == "INVOICE_LIFECYCLE_CONFLICT"
        assert "COMPLETED" in completed_resp.json()["detail"]["message"]

    async def test_draft_to_draft_invalid(self, db_client: AsyncClient) -> None:
        """DRAFT → DRAFT is invalid."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        resp = await db_client.post(
            "/api/v1/invoices", json=_invoice_payload(customer_id, rate_21)
        )
        inv_id = resp.json()["id"]

        bad_resp = await db_client.post(
            f"/api/v1/invoices/{inv_id}/status", json={"status": "DRAFT"}
        )
        assert bad_resp.status_code == 409
        assert bad_resp.json()["detail"]["code"] == "INVOICE_LIFECYCLE_CONFLICT"

    async def test_cancelled_to_sent_invalid(self, db_client: AsyncClient) -> None:
        """CANCELLED → SENT is invalid."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        resp = await db_client.post(
            "/api/v1/invoices", json=_invoice_payload(customer_id, rate_21)
        )
        inv_id = resp.json()["id"]
        await db_client.post(
            f"/api/v1/invoices/{inv_id}/status", json={"status": "CANCELLED"}
        )

        bad_resp = await db_client.post(
            f"/api/v1/invoices/{inv_id}/status", json={"status": "SENT"}
        )
        assert bad_resp.status_code == 409
        assert bad_resp.json()["detail"]["code"] == "INVOICE_LIFECYCLE_CONFLICT"

    async def test_edit_cancelled_invoice_rejected(self, db_client: AsyncClient) -> None:
        """PUT on a cancelled invoice returns 422."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        resp = await db_client.post(
            "/api/v1/invoices", json=_invoice_payload(customer_id, rate_21)
        )
        inv_id = resp.json()["id"]
        await db_client.post(
            f"/api/v1/invoices/{inv_id}/status", json={"status": "CANCELLED"}
        )

        update_resp = await db_client.put(
            f"/api/v1/invoices/{inv_id}",
            json=_invoice_payload(customer_id, rate_21),
        )
        assert update_resp.status_code == 422
        assert "cancelled" in update_resp.json()["detail"].lower()

    async def test_sent_invoice_cannot_be_edited(self, db_client: AsyncClient) -> None:
        """PUT on a SENT invoice returns 422 (SENT is a locked formal document)."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        resp = await db_client.post(
            "/api/v1/invoices", json=_invoice_payload(customer_id, rate_21)
        )
        inv_id = resp.json()["id"]
        await db_client.post(
            f"/api/v1/invoices/{inv_id}/status", json={"status": "SENT"}
        )

        update_resp = await db_client.put(
            f"/api/v1/invoices/{inv_id}",
            json=_invoice_payload(customer_id, rate_21),
        )
        assert update_resp.status_code == 422
        assert "sent" in update_resp.json()["detail"].lower()

    async def test_sent_invoice_cannot_be_deleted(self, db_client: AsyncClient) -> None:
        """DELETE on a SENT invoice returns 422."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        resp = await db_client.post(
            "/api/v1/invoices", json=_invoice_payload(customer_id, rate_21)
        )
        inv_id = resp.json()["id"]
        await db_client.post(
            f"/api/v1/invoices/{inv_id}/status", json={"status": "SENT"}
        )

        del_resp = await db_client.delete(f"/api/v1/invoices/{inv_id}")
        assert del_resp.status_code == 422
        assert "sent" in del_resp.json()["detail"].lower()

    async def test_reactivated_invoice_can_be_edited(
        self, db_client: AsyncClient
    ) -> None:
        """DRAFT → CANCELLED → DRAFT allows full edit again."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        resp = await db_client.post(
            "/api/v1/invoices", json=_invoice_payload(customer_id, rate_21)
        )
        inv_id = resp.json()["id"]

        await db_client.post(
            f"/api/v1/invoices/{inv_id}/status", json={"status": "CANCELLED"}
        )
        await db_client.post(
            f"/api/v1/invoices/{inv_id}/status", json={"status": "DRAFT"}
        )

        update_resp = await db_client.put(
            f"/api/v1/invoices/{inv_id}",
            json=_invoice_payload(
                customer_id, rate_21,
                lines=[_line_payload(rate_21, unit_price="200.000")],
            ),
        )
        assert update_resp.status_code == 200
        assert update_resp.json()["total_incl_vat"] == "242.000"


# ---------------------------------------------------------------------------
# Isolation / cross-company
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestInvoiceIsolation:

    async def test_cross_company_get_404(self, db_client: AsyncClient) -> None:
        """GET with a random invoice ID returns 404 (not from this company)."""
        await _full_auth(db_client)
        await _setup_company(db_client)

        get_resp = await db_client.get(f"/api/v1/invoices/{uuid.uuid4()}")
        assert get_resp.status_code == 404

    async def test_cross_company_delete_404(self, db_client: AsyncClient) -> None:
        """DELETE with a random invoice ID returns 404."""
        await _full_auth(db_client)
        await _setup_company(db_client)

        del_resp = await db_client.delete(f"/api/v1/invoices/{uuid.uuid4()}")
        assert del_resp.status_code == 404

    async def test_cross_company_customer_422(self, db_client: AsyncClient) -> None:
        """Customer ID not belonging to this company → 422."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        fake_cust_id = str(uuid.uuid4())
        resp = await db_client.post(
            "/api/v1/invoices",
            json=_invoice_payload(fake_cust_id, rate_21),
        )
        assert resp.status_code == 422
        assert "customer" in resp.json()["detail"].lower()

    async def test_cross_company_vat_rate_422(self, db_client: AsyncClient) -> None:
        """VAT rate not belonging to this company → 422."""
        await _full_auth(db_client)
        await _setup_company(db_client)
        customer_id = await _create_customer(db_client)

        resp = await db_client.post(
            "/api/v1/invoices",
            json=_invoice_payload(customer_id, str(uuid.uuid4())),
        )
        assert resp.status_code == 422

    async def test_wrong_currency_422(self, db_client: AsyncClient) -> None:
        """Invoice currency ≠ base_currency → 422."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        payload = _invoice_payload(customer_id, rate_21)
        payload["currency"] = "USD"
        resp = await db_client.post("/api/v1/invoices", json=payload)
        assert resp.status_code == 422
        assert "base currency" in resp.json()["detail"].lower()

    async def test_base_amounts_equal_invoice_amounts_single_currency(
        self, db_client: AsyncClient
    ) -> None:
        """M5 single-currency: base_* must equal invoice amounts (exchange_rate=1)."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        resp = await db_client.post(
            "/api/v1/invoices", json=_invoice_payload(customer_id, rate_21)
        )
        inv = resp.json()
        assert inv["base_total_incl_vat"] == inv["total_incl_vat"]
        assert inv["base_vat_total"] == inv["vat_total"]
        from decimal import Decimal
        assert Decimal(inv["exchange_rate"]) == Decimal("1")


# ---------------------------------------------------------------------------
# Cascade / FK constraints
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestInvoiceCascadeAndFK:

    async def test_delete_invoice_cascades_lines_and_taxes(
        self, db_client: AsyncClient
    ) -> None:
        """Deleting an invoice removes its lines and line taxes (CASCADE)."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        create_resp = await db_client.post(
            "/api/v1/invoices",
            json=_invoice_payload(
                customer_id, rate_21,
                lines=[
                    _line_payload(rate_21, name="L1"),
                    _line_payload(rate_21, name="L2"),
                ],
            ),
        )
        inv = create_resp.json()
        assert len(inv["lines"]) == 2
        inv_id = inv["id"]

        del_resp = await db_client.delete(f"/api/v1/invoices/{inv_id}")
        assert del_resp.status_code == 204

        # Invoice is gone
        get_resp = await db_client.get(f"/api/v1/invoices/{inv_id}")
        assert get_resp.status_code == 404

    async def test_delete_customer_with_invoice_fails(
        self, db_client: AsyncClient
    ) -> None:
        """Deleting a customer that has invoices is restricted (RESTRICT FK)."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        await db_client.post(
            "/api/v1/invoices", json=_invoice_payload(customer_id, rate_21)
        )

        del_resp = await db_client.delete(f"/api/v1/customers/{customer_id}")
        assert del_resp.status_code in (409, 422), (
            f"Expected RESTRICT error, got {del_resp.status_code}: {del_resp.text}"
        )

    async def test_delete_vat_rate_with_invoice_fails(
        self, db_client: AsyncClient
    ) -> None:
        """Deleting a VAT rate referenced by an invoice should be restricted."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21_id = seeds["rates"]["NL standard (21%)"]["id"]

        await db_client.post(
            "/api/v1/invoices", json=_invoice_payload(customer_id, rate_21_id)
        )

        del_resp = await db_client.delete(f"/api/v1/vat-rates/{rate_21_id}")
        assert del_resp.status_code in (409, 422), (
            f"Expected RESTRICT error, got {del_resp.status_code}"
        )

    async def test_delete_vat_treatment_with_invoice_fails(
        self, db_client: AsyncClient
    ) -> None:
        """Deleting a VAT treatment referenced by an invoice should be restricted (409)."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]
        domestic_treatment_id = seeds["treatments"]["NL_DOMESTIC"]["id"]

        # Create an invoice that snapshots this treatment
        create_resp = await db_client.post(
            "/api/v1/invoices",
            json=_invoice_payload(customer_id, rate_21, treatment_id=domestic_treatment_id),
        )
        assert create_resp.status_code == 201
        inv_id = create_resp.json()["id"]

        del_resp = await db_client.delete(
            f"/api/v1/vat-treatments/{domestic_treatment_id}"
        )
        assert del_resp.status_code in (409, 422), (
            f"Expected RESTRICT error, got {del_resp.status_code}: {del_resp.text}"
        )

        # Invoice is still intact
        get_resp = await db_client.get(f"/api/v1/invoices/{inv_id}")
        assert get_resp.status_code == 200


# ---------------------------------------------------------------------------
# Product options: never leaks cost/margin
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestInvoiceProductOptions:

    async def test_returns_customer_safe_fields_only(self, db_client: AsyncClient) -> None:
        """GET /invoice-product-options must not include cost/margin/supplier/extra."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        # Create a product with cost/margin info
        await db_client.post(
            "/api/v1/products",
            json={
                "name": "Solar Panel",
                "purchase_cost_excl_vat": "100.000",
                "margin_rate": "0.3000",
                "default_vat_rate_id": rate_21,
                "supplier": "SolarCo",
                "extra": {"secret_code": "xyz"},
            },
        )

        resp = await db_client.get("/api/v1/invoice-product-options")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) >= 1

        for item in items:
            assert "purchase_cost_excl_vat" not in item
            assert "margin_rate" not in item
            assert "effective_margin_rate" not in item
            assert "supplier" not in item
            assert "extra" not in item
            assert "id" in item
            assert "name" in item

    async def test_search_filters_products(self, db_client: AsyncClient) -> None:
        """q= filter applies to product name."""
        await _full_auth(db_client)
        await _setup_company(db_client)

        await db_client.post("/api/v1/products", json={"name": "Solar Panel"})
        await db_client.post("/api/v1/products", json={"name": "Wind Turbine"})

        solar_resp = await db_client.get("/api/v1/invoice-product-options?q=Solar")
        solar_items = solar_resp.json()["items"]
        assert all("solar" in item["name"].lower() for item in solar_items)

        wind_resp = await db_client.get("/api/v1/invoice-product-options?q=Wind")
        wind_items = wind_resp.json()["items"]
        assert all("wind" in item["name"].lower() for item in wind_items)

    async def test_inactive_products_excluded(self, db_client: AsyncClient) -> None:
        """Inactive products are not shown in invoice product options."""
        await _full_auth(db_client)
        await _setup_company(db_client)

        create_resp = await db_client.post(
            "/api/v1/products", json={"name": "Active Gadget"}
        )
        product_id = create_resp.json()["id"]

        await db_client.put(
            f"/api/v1/products/{product_id}",
            json={"name": "Active Gadget", "active": False},
        )

        resp = await db_client.get("/api/v1/invoice-product-options")
        names = [item["name"] for item in resp.json()["items"]]
        assert "Active Gadget" not in names


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestInvoiceAuth:

    async def test_unauthenticated_create_401(self, db_client: AsyncClient) -> None:
        resp = await db_client.post(
            "/api/v1/invoices",
            json={
                "customer_id": str(uuid.uuid4()),
                "invoice_date": "2026-01-01",
                "tax_mode": "LINE",
                "amounts_include_vat": False,
                "lines": [{"name": "X", "quantity": "1", "unit_price": "10"}],
            },
        )
        assert resp.status_code == 401

    async def test_unauthenticated_list_401(self, db_client: AsyncClient) -> None:
        resp = await db_client.get("/api/v1/invoices")
        assert resp.status_code == 401

    async def test_no_company_400(self, db_client: AsyncClient) -> None:
        await _full_auth(db_client)
        resp = await db_client.post(
            "/api/v1/invoices",
            json={
                "customer_id": str(uuid.uuid4()),
                "invoice_date": "2026-01-01",
                "tax_mode": "LINE",
                "amounts_include_vat": False,
                "lines": [{"name": "X", "quantity": "1", "unit_price": "10"}],
            },
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# FK ownership validation
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestInvoiceFKValidation:

    async def test_line_mode_document_vat_rate_not_persisted(
        self, db_client: AsyncClient
    ) -> None:
        """LINE-mode invoice: document_vat_rate_id must be NULL even if caller sends one."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        # Deliberately pass document_vat_rate_id in a LINE-mode request
        payload = _invoice_payload(customer_id, rate_21)
        payload["document_vat_rate_id"] = rate_21

        resp = await db_client.post("/api/v1/invoices", json=payload)
        assert resp.status_code == 201
        assert resp.json()["document_vat_rate_id"] is None

    async def test_nonexistent_product_id_rejected(
        self, db_client: AsyncClient
    ) -> None:
        """Line with a non-existent product_id → 422."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        line = _line_payload(rate_21)
        line["product_id"] = str(uuid.uuid4())  # does not exist

        resp = await db_client.post(
            "/api/v1/invoices",
            json=_invoice_payload(customer_id, rate_21, lines=[line]),
        )
        assert resp.status_code == 422
        assert "product" in resp.json()["detail"].lower()

    async def test_nonexistent_unit_id_rejected(
        self, db_client: AsyncClient
    ) -> None:
        """Line with a non-existent unit_id → 422."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        line = _line_payload(rate_21)
        line["unit_id"] = str(uuid.uuid4())  # does not exist

        resp = await db_client.post(
            "/api/v1/invoices",
            json=_invoice_payload(customer_id, rate_21, lines=[line]),
        )
        assert resp.status_code == 422
        assert "unit" in resp.json()["detail"].lower()

    async def test_document_mode_line_vat_rate_nonexistent_rejected(
        self, db_client: AsyncClient
    ) -> None:
        """DOCUMENT mode: line with a non-existent vat_rate_id → 422 (not 500)."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        line = {"name": "Service", "quantity": "1", "unit_price": "100.000",
                "vat_rate_id": str(uuid.uuid4())}  # non-existent

        resp = await db_client.post(
            "/api/v1/invoices",
            json=_invoice_payload(
                customer_id, rate_21,
                tax_mode="DOCUMENT",
                document_vat_rate_id=rate_21,
                lines=[line],
            ),
        )
        assert resp.status_code == 422
        assert "vat rate" in resp.json()["detail"].lower()

    async def test_document_mode_line_vat_rate_valid_preserved(
        self, db_client: AsyncClient
    ) -> None:
        """DOCUMENT mode: line with a valid same-company vat_rate_id is preserved."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]
        rate_9 = seeds["rates"]["NL reduced (9%)"]["id"]

        line = {"name": "Service", "quantity": "1", "unit_price": "100.000",
                "vat_rate_id": rate_9}  # valid, belongs to same company

        resp = await db_client.post(
            "/api/v1/invoices",
            json=_invoice_payload(
                customer_id, rate_21,
                tax_mode="DOCUMENT",
                document_vat_rate_id=rate_21,
                lines=[line],
            ),
        )
        assert resp.status_code == 201
        # Per-line rate is preserved as draft default; does not affect calculated tax
        assert resp.json()["lines"][0]["vat_rate_id"] == rate_9
        # Tax is driven by document rate (21%), not line rate (9%)
        assert resp.json()["vat_total"] == "21.000"


@pytest.mark.integration
class TestInvoiceRevisionPreview:
    async def test_preview_preserves_text_only_snapshot_and_has_no_writes(
        self,
        db_client: AsyncClient,
        admin_session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_id = seeds["rates"]["NL standard (21%)"]["id"]
        created = await db_client.post(
            "/api/v1/invoices", json=_invoice_payload(customer_id, rate_id)
        )
        assert created.status_code == 201
        invoice = await _issue_invoice(db_client, created.json()["id"])
        line = invoice["lines"][0]
        async with admin_session_maker() as session:
            invoice_row = await session.get(Invoice, uuid.UUID(invoice["id"]))
            line_row = await session.get(InvoiceLine, uuid.UUID(line["id"]))
            assert invoice_row is not None and line_row is not None
            invoice_row.subtotal_excl_vat = Decimal("100.992")
            invoice_row.vat_total = Decimal("20.007")
            invoice_row.total_incl_vat = Decimal("120.999")
            line_row.subtotal_excl_vat = Decimal("99.997")
            line_row.vat_total = Decimal("20.007")
            line_row.total_incl_vat = Decimal("120.999")
            tax_row = await session.scalar(
                select(InvoiceLineTax).where(InvoiceLineTax.invoice_line_id == line_row.id)
            )
            assert tax_row is not None
            tax_row.tax_amount = Decimal("20.007")
            tax_row.taxable_amount = Decimal("99.997")
            await session.commit()
        invoice = (await db_client.get(f"/api/v1/invoices/{invoice['id']}")).json()
        line = invoice["lines"][0]
        before = await db_client.get(f"/api/v1/invoices/{invoice['id']}")
        database_before = await _all_business_rows(admin_session_maker)
        response = await db_client.post(
            f"/api/v1/invoices/{invoice['id']}/revisions/calculate",
            json={
                "expected_updated_at": invoice["updated_at"],
                "changes": {
                    "notes": "Preview only",
                    "lines": [
                        {
                            "line_id": line["id"],
                            "name": "Renamed",
                            "quantity": line["quantity"],
                            "unit_price": line["unit_price"],
                            "vat_rate_id": line["vat_rate_id"],
                        }
                    ],
                },
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["vat_total"] == "20.007"
        assert response.json()["total_incl_vat"] == "120.999"
        assert response.json()["subtotal_excl_vat"] == "100.992"
        assert response.json()["lines"][0]["subtotal_excl_vat"] == "99.997"
        assert response.json()["line_taxes"][0]["tax_amount"] == "20.007"
        assert response.json()["lines"][0]["name"] == "Renamed"
        assert (await db_client.get(f"/api/v1/invoices/{invoice['id']}")).json() == before.json()
        history = await db_client.get(f"/api/v1/invoices/{invoice['id']}/revisions")
        assert history.json()["total"] == 0
        duplicate = await db_client.post(
            f"/api/v1/invoices/{invoice['id']}/revisions/calculate",
            json={
                "expected_updated_at": invoice["updated_at"],
                "changes": {
                    "lines": [
                        {"line_id": line["id"], "name": "A", "quantity": "1", "unit_price": "1"},
                        {"line_id": line["id"], "name": "B", "quantity": "1", "unit_price": "1"},
                    ]
                },
            },
        )
        assert duplicate.status_code == 422
        changed = await db_client.post(
            f"/api/v1/invoices/{invoice['id']}/revisions/calculate",
            json={
                "expected_updated_at": invoice["updated_at"],
                "changes": {
                    "due_date": None,
                    "lines": [
                        {
                            "line_id": line["id"],
                            "name": line["name"],
                            "quantity": "2.500",
                            "unit_price": line["unit_price"],
                            "vat_rate_id": line["vat_rate_id"],
                        }
                    ],
                },
            },
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["lines"][0]["quantity"] == "2.500"
        assert changed.json()["vat_total"] == "52.50"
        stale = await db_client.post(
            f"/api/v1/invoices/{invoice['id']}/revisions/calculate",
            json={"expected_updated_at": "2000-01-01T00:00:00Z", "changes": {"notes": "stale"}},
        )
        assert stale.status_code == 409
        invalid_dates = await _revision_preview(
            db_client, invoice, {"invoice_date": "2026-12-10", "due_date": "2026-12-09"}
        )
        assert invalid_dates.status_code == 422
        true_noop = await _revision_preview(db_client, invoice, {})
        assert true_noop.status_code == 200
        assert true_noop.json()["vat_total"] == "20.007"
        assert true_noop.json()["total_incl_vat"] == "120.999"
        assert await _all_business_rows(admin_session_maker) == database_before

    async def test_same_rate_id_retains_per_line_snapshot_after_master_rate_changes(
        self,
        db_client: AsyncClient,
        admin_session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate = seeds["rates"]["NL standard (21%)"]
        invoice = await _create_issued_invoice(
            db_client,
            customer_id,
            rate["id"],
            lines=[
                _line_payload(rate["id"], name="Frozen 21", unit_price="100"),
                _line_payload(rate["id"], name="Frozen 9", unit_price="100"),
            ],
        )
        rows = invoice["lines"]
        async with admin_session_maker() as session:
            master_rate = await session.get(VatRate, uuid.UUID(rate["id"]))
            assert master_rate is not None
            master_rate.label = "Current rate changed"
            master_rate.percent = Decimal("99")
            master_rate.active = False
            second_line = await session.get(InvoiceLine, uuid.UUID(rows[1]["id"]))
            assert second_line is not None
            second_line.vat_rate_label = "Frozen historical 9%"
            second_line.vat_rate_percent = Decimal("9")
            line_tax = await session.scalar(
                select(InvoiceLineTax).where(InvoiceLineTax.invoice_line_id == second_line.id)
            )
            assert line_tax is not None
            line_tax.vat_rate_label = "Frozen historical 9%"
            line_tax.vat_rate_percent = Decimal("9")
            line_tax.effective_vat_percent = Decimal("9")
            basis = await session.scalar(
                select(InvoiceCreditBasisLine).where(
                    InvoiceCreditBasisLine.invoice_line_id == second_line.id
                )
            )
            assert basis is not None
            basis.vat_rate_label = "Frozen historical 9%"
            basis.vat_rate_percent = Decimal("9")
            basis.effective_vat_percent = Decimal("9")
            await session.commit()
        invoice = (await db_client.get(f"/api/v1/invoices/{invoice['id']}")).json()
        database_before = await _all_business_rows(admin_session_maker)
        response = await _revision_preview(
            db_client,
            invoice,
            {
                "lines": [
                    {
                        "line_id": rows[0]["id"],
                        "name": "Frozen 21",
                        "quantity": "2",
                        "unit_price": "100",
                        "vat_rate_id": rate["id"],
                    },
                    {
                        "line_id": rows[1]["id"],
                        "name": "Frozen 9",
                        "quantity": "2",
                        "unit_price": "100",
                        "vat_rate_id": rate["id"],
                    },
                ]
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert [tax["vat_rate_id"] for tax in body["line_taxes"]] == [rate["id"], rate["id"]]
        assert [tax["vat_rate_label"] for tax in body["line_taxes"]] == [
            rate["label"],
            "Frozen historical 9%",
        ]
        assert [tax["vat_rate_percent"] for tax in body["line_taxes"]] == ["21.000", "9.000"]
        assert [tax["tax_amount"] for tax in body["line_taxes"]] == ["42.00", "18.00"]
        assert await _all_business_rows(admin_session_maker) == database_before
        new_line = await _revision_preview(
            db_client,
            invoice,
            {
                "lines": [
                    {
                        "name": "New on inactive rate",
                        "quantity": "1",
                        "unit_price": "10",
                        "vat_rate_id": rate["id"],
                    },
                    {
                        "line_id": rows[0]["id"],
                        "name": "Frozen 21",
                        "quantity": "2",
                        "unit_price": "100",
                        "vat_rate_id": rate["id"],
                    },
                ]
            },
        )
        assert new_line.status_code == 422

    async def test_mixed_rate_inclusive_add_delete_reorder_uses_existing_pricing(
        self,
        db_client: AsyncClient,
    ) -> None:
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]
        rate_9 = seeds["rates"]["NL reduced (9%)"]["id"]
        unit_response = await db_client.post(
            "/api/v1/units", json={"code": "rev-test", "name": "Revision unit"}
        )
        assert unit_response.status_code == 201
        unit_id = unit_response.json()["id"]
        product_response = await db_client.post(
            "/api/v1/products",
            json={
                "name": "Costly catalog product",
                "unit_id": unit_id,
                "purchase_cost_excl_vat": "900.000",
                "margin_rate": "0.5000",
            },
        )
        assert product_response.status_code == 201, product_response.text
        product_id = product_response.json()["id"]
        initial_lines = [
            {
                **_line_payload(
                    rate_21,
                    name="A",
                    unit_price="121",
                    discount={"type": "PERCENTAGE", "value": "10"},
                )
            },
            _line_payload(rate_9, name="B", unit_price="218"),
            _line_payload(rate_21, name="Delete me", unit_price="50"),
        ]
        invoice = await _create_issued_invoice(
            db_client,
            customer_id,
            rate_21,
            amounts_include_vat=True,
            discount={"type": "FIXED", "value": "12"},
            lines=initial_lines,
        )
        ids = {line["name"]: line["id"] for line in invoice["lines"]}
        target = [
            {
                "line_id": ids["B"],
                "name": "B reordered",
                "quantity": "1.250",
                "unit_price": "218",
                "vat_rate_id": rate_9,
            },
            {
                "name": "Catalog item with entered price",
                "product_id": product_id,
                "unit_id": unit_id,
                "quantity": "1.5",
                "unit_price": "17.345",
                "vat_rate_id": rate_9,
                "discount": {"type": "FIXED", "value": "2"},
            },
            {
                "line_id": ids["A"],
                "name": "A updated",
                "quantity": "2",
                "unit_price": "121",
                "vat_rate_id": rate_21,
                "discount": {"type": "PERCENTAGE", "value": "10"},
            },
        ]
        preview = await _revision_preview(db_client, invoice, {"lines": target})
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert [line["name"] for line in body["lines"]] == [
            "B reordered",
            "Catalog item with entered price",
            "A updated",
        ]
        assert body["lines"][1]["unit_price"] == "17.345"
        assert body["lines"][1]["product_id"] == product_id
        raw_lines = [
            {key: value for key, value in line.items() if key != "line_id"} for line in target
        ]
        expected = await db_client.post(
            "/api/v1/invoices/calculate",
            json={
                "customer_id": customer_id,
                "invoice_date": invoice["invoice_date"],
                "tax_mode": "LINE",
                "amounts_include_vat": True,
                "vat_treatment_id": invoice["vat_treatment_id"],
                "discount": {"type": "FIXED", "value": "12.000"},
                "lines": raw_lines,
            },
        )
        assert expected.status_code == 200, expected.text
        assert body == expected.json()

    async def test_foreign_and_unknown_references_and_owner_company_boundaries(
        self,
        db_client: AsyncClient,
        admin_session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _full_auth(db_client, email="revision-owner-a@example.com")
        seeds_a = await _setup_company(db_client)
        customer_a = await _create_customer(db_client)
        invoice_a = await _create_issued_invoice(
            db_client, customer_a, seeds_a["rates"]["NL standard (21%)"]["id"]
        )
        company_a = (await db_client.get("/api/v1/company")).json()
        async with admin_session_maker() as session:
            owner = await session.scalar(
                select(User).where(User.email == "revision-owner-a@example.com")
            )
            assert owner is not None
            company_b = Company(name="Second isolated company", base_currency="EUR")
            session.add(company_b)
            await session.flush()
            owner.company_id = company_b.id
            await seed_vat(session, company_b.id)
            await seed_dictionaries(session, company_b.id)
            await session.commit()
        rates_b = (await db_client.get("/api/v1/vat-rates")).json()["items"]
        rate_b = next(row for row in rates_b if row["label"] == "NL standard (21%)")
        customer_b = await _create_customer(db_client, name="Other company customer")
        units_b = (await db_client.get("/api/v1/units")).json()["items"]
        unit_b = next(row for row in units_b if row["code"] == "pcs")
        product_b = await db_client.post(
            "/api/v1/products",
            json={"name": "Other company product", "unit_id": unit_b["id"]},
        )
        assert product_b.status_code == 201
        invoice_b = await _create_issued_invoice(
            db_client,
            customer_b,
            rate_b["id"],
            lines=[
                {
                    **_line_payload(rate_b["id"]),
                    "product_id": product_b.json()["id"],
                    "unit_id": unit_b["id"],
                }
            ],
        )
        async with admin_session_maker() as session:
            owner = await session.scalar(
                select(User).where(User.email == "revision-owner-a@example.com")
            )
            assert owner is not None
            owner.company_id = uuid.UUID(company_a["id"])
            await session.commit()
        hidden = await db_client.get(f"/api/v1/invoices/{invoice_b['id']}/revisions")
        hidden_preview = await _revision_preview(db_client, invoice_b, {"notes": "cross company"})
        assert hidden.status_code == 404
        assert hidden_preview.status_code == 404
        line_a = invoice_a["lines"][0]
        foreign_line = await _revision_preview(
            db_client,
            invoice_a,
            {
                "lines": [
                    {
                        "line_id": invoice_b["lines"][0]["id"],
                        "name": "Foreign",
                        "quantity": "1",
                        "unit_price": "100",
                        "vat_rate_id": line_a["vat_rate_id"],
                    }
                ]
            },
        )
        unknown_line = await _revision_preview(
            db_client,
            invoice_a,
            {
                "lines": [
                    {
                        "line_id": str(uuid.uuid4()),
                        "name": "Unknown",
                        "quantity": "1",
                        "unit_price": "100",
                        "vat_rate_id": line_a["vat_rate_id"],
                    }
                ]
            },
        )
        foreign_product = await _revision_preview(
            db_client,
            invoice_a,
            {
                "lines": [
                    {
                        "line_id": line_a["id"],
                        "name": "Invalid product",
                        "quantity": "1",
                        "unit_price": "100",
                        "vat_rate_id": line_a["vat_rate_id"],
                        "product_id": product_b.json()["id"],
                    }
                ]
            },
        )
        foreign_unit = await _revision_preview(
            db_client,
            invoice_a,
            {
                "lines": [
                    {
                        "line_id": line_a["id"],
                        "name": "Invalid unit",
                        "quantity": "1",
                        "unit_price": "100",
                        "vat_rate_id": line_a["vat_rate_id"],
                        "unit_id": unit_b["id"],
                    }
                ]
            },
        )
        unknown_product = await _revision_preview(
            db_client,
            invoice_a,
            {
                "lines": [
                    {
                        "line_id": line_a["id"],
                        "name": "Invalid product",
                        "quantity": "1",
                        "unit_price": "100",
                        "vat_rate_id": line_a["vat_rate_id"],
                        "product_id": str(uuid.uuid4()),
                    }
                ]
            },
        )
        unknown_unit = await _revision_preview(
            db_client,
            invoice_a,
            {
                "lines": [
                    {
                        "line_id": line_a["id"],
                        "name": "Invalid unit",
                        "quantity": "1",
                        "unit_price": "100",
                        "vat_rate_id": line_a["vat_rate_id"],
                        "unit_id": str(uuid.uuid4()),
                    }
                ]
            },
        )
        assert all(
            response.status_code == 422
            for response in (
                foreign_line,
                unknown_line,
                foreign_product,
                foreign_unit,
                unknown_product,
                unknown_unit,
            )
        )
        async with admin_session_maker() as session:
            owner = await session.scalar(
                select(User).where(User.email == "revision-owner-a@example.com")
            )
            assert owner is not None
            owner.role = "accountant"
            await session.commit()
        non_owner = await _revision_preview(db_client, invoice_a, {"notes": "forbidden"})
        assert non_owner.status_code == 403

    async def test_unchanged_treatment_uses_frozen_snapshot_and_changed_treatment_resolves(
        self,
        db_client: AsyncClient,
        admin_session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_id = seeds["rates"]["NL standard (21%)"]["id"]
        domestic = seeds["treatments"]["NL_DOMESTIC"]
        exported = seeds["treatments"]["EXPORT_NON_EU"]
        invoice = await _create_issued_invoice(
            db_client, customer_id, rate_id, treatment_id=domestic["id"]
        )
        async with admin_session_maker() as session:
            treatment = await session.get(VatTreatment, uuid.UUID(domestic["id"]))
            assert treatment is not None
            treatment.label = "Current treatment label"
            treatment.effect = VatTreatmentEffect.ZERO_EXPORT
            treatment.active = False
            await session.commit()
        invoice = (await db_client.get(f"/api/v1/invoices/{invoice['id']}")).json()
        unchanged = await _revision_preview(
            db_client,
            invoice,
            {
                "lines": [
                    {
                        "line_id": invoice["lines"][0]["id"],
                        "name": "Service",
                        "quantity": "2",
                        "unit_price": "100",
                        "vat_rate_id": rate_id,
                    }
                ]
            },
        )
        assert unchanged.status_code == 200, unchanged.text
        assert unchanged.json()["vat_treatment_snapshot"]["label"] == domestic["label"]
        assert unchanged.json()["vat_treatment_snapshot"]["effect"] == "APPLY_RATE"
        assert unchanged.json()["vat_total"] == "42.00"
        deliberate = await _revision_preview(
            db_client,
            invoice,
            {
                "vat_treatment_id": exported["id"],
                "lines": [
                    {
                        "line_id": invoice["lines"][0]["id"],
                        "name": "Service",
                        "quantity": "2",
                        "unit_price": "100",
                        "vat_rate_id": rate_id,
                    }
                ],
            },
        )
        assert deliberate.status_code == 200, deliberate.text
        assert deliberate.json()["vat_treatment_snapshot"]["code"] == "EXPORT_NON_EU"
        assert deliberate.json()["vat_treatment_snapshot"]["label"] == exported["label"]
        assert deliberate.json()["vat_total"] == "0.00"

    async def test_document_rate_freeze_and_deliberate_active_selection(
        self,
        db_client: AsyncClient,
        admin_session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]
        rate_9 = seeds["rates"]["NL reduced (9%)"]
        invoice = await _create_issued_invoice(
            db_client,
            customer_id,
            rate_21["id"],
            tax_mode="DOCUMENT",
            document_vat_rate_id=rate_21["id"],
        )
        async with admin_session_maker() as session:
            master = await session.get(VatRate, uuid.UUID(rate_21["id"]))
            assert master is not None
            master.label = "Renamed document rate"
            master.percent = Decimal("88")
            master.active = False
            await session.commit()
        invoice = (await db_client.get(f"/api/v1/invoices/{invoice['id']}")).json()
        line = invoice["lines"][0]
        frozen = await _revision_preview(
            db_client,
            invoice,
            {
                "lines": [
                    {
                        "line_id": line["id"],
                        "name": line["name"],
                        "quantity": "2",
                        "unit_price": "100",
                        "vat_rate_id": line["vat_rate_id"],
                    }
                ]
            },
        )
        assert frozen.status_code == 200, frozen.text
        assert frozen.json()["document_taxes"][0]["vat_rate_label"] == rate_21["label"]
        assert frozen.json()["document_taxes"][0]["vat_rate_percent"] == "21.000"
        assert frozen.json()["vat_total"] == "42.00"
        changed = await _revision_preview(
            db_client,
            invoice,
            {
                "document_vat_rate_id": rate_9["id"],
                "lines": [
                    {
                        "line_id": line["id"],
                        "name": line["name"],
                        "quantity": "2",
                        "unit_price": "100",
                        "vat_rate_id": line["vat_rate_id"],
                    }
                ],
            },
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["document_taxes"][0]["vat_rate_label"] == rate_9["label"]
        assert changed.json()["vat_total"] == "18.00"
        async with admin_session_maker() as session:
            inactive = await session.get(VatRate, uuid.UUID(rate_9["id"]))
            assert inactive is not None
            inactive.active = False
            await session.commit()
        invalid = await _revision_preview(
            db_client,
            invoice,
            {"document_vat_rate_id": rate_9["id"]},
        )
        assert invalid.status_code == 422
        unknown = await _revision_preview(
            db_client,
            invoice,
            {"document_vat_rate_id": str(uuid.uuid4())},
        )
        assert unknown.status_code == 422

    async def test_document_to_line_requires_active_rate_without_line_snapshot(
        self,
        db_client: AsyncClient,
        admin_session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate = seeds["rates"]["NL standard (21%)"]
        invoice = await _create_issued_invoice(
            db_client,
            customer_id,
            rate["id"],
            tax_mode="DOCUMENT",
            document_vat_rate_id=rate["id"],
        )
        line_invoice = await _create_issued_invoice(
            db_client, customer_id, rate["id"], tax_mode="LINE"
        )
        active_switch = await _revision_preview(
            db_client, invoice, {"tax_mode": "LINE"}
        )
        assert active_switch.status_code == 200, active_switch.text
        assert active_switch.json()["line_taxes"][0]["vat_rate_percent"] == "21.000"

        async with admin_session_maker() as session:
            master = await session.get(VatRate, uuid.UUID(rate["id"]))
            assert master is not None
            master.percent = Decimal("88")
            master.label = "Changed and inactive"
            master.active = False
            await session.commit()
        invoice = (await db_client.get(f"/api/v1/invoices/{invoice['id']}")).json()
        inactive_switch = await _revision_preview(
            db_client, invoice, {"tax_mode": "LINE"}
        )
        assert inactive_switch.status_code == 422

        # A real frozen LINE snapshot remains reusable after the dictionary is retired.
        frozen = await _revision_preview(
            db_client,
            line_invoice,
            {
                "lines": [
                    {
                        "line_id": line_invoice["lines"][0]["id"],
                        "name": line_invoice["lines"][0]["name"],
                        "quantity": "2",
                        "unit_price": line_invoice["lines"][0]["unit_price"],
                        "vat_rate_id": rate["id"],
                    }
                ]
            },
        )
        assert frozen.status_code == 200, frozen.text
        assert frozen.json()["line_taxes"][0]["vat_rate_percent"] == "21.000"

    async def test_line_mode_validates_changed_document_rate_reference(
        self,
        db_client: AsyncClient,
        admin_session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _full_auth(db_client, email="revision-doc-rate-owner@example.com")
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rates = seeds["rates"]
        invoice = await _create_issued_invoice(
            db_client, customer_id, rates["NL standard (21%)"]["id"]
        )
        own_rate_id = rates["NL reduced (9%)"]["id"]
        active_changed = await _revision_preview(
            db_client,
            invoice,
            {"document_vat_rate_id": rates["NL standard (21%)"]["id"]},
        )
        assert active_changed.status_code == 200, active_changed.text
        unknown_id = str(uuid.uuid4())

        # Make a real rate belonging to another company.
        company_a = (await db_client.get("/api/v1/company")).json()
        async with admin_session_maker() as session:
            owner = await session.scalar(
                select(User).where(User.email == "revision-doc-rate-owner@example.com")
            )
            assert owner is not None
            company_b = Company(name="Document rate boundary company", base_currency="EUR")
            session.add(company_b)
            await session.flush()
            await seed_vat(session, company_b.id)
            await seed_dictionaries(session, company_b.id)
            await session.commit()
            foreign_rate = await session.scalar(
                select(VatRate).where(
                    VatRate.company_id == company_b.id,
                    VatRate.label == "NL standard (21%)",
                )
            )
            assert foreign_rate is not None
            foreign_rate_id = str(foreign_rate.id)
            owner.company_id = uuid.UUID(company_a["id"])
            inactive = await session.get(VatRate, uuid.UUID(own_rate_id))
            assert inactive is not None
            inactive.active = False
            await session.commit()

        for invalid_id in (unknown_id, foreign_rate_id, own_rate_id):
            rejected = await _revision_preview(
                db_client, invoice, {"document_vat_rate_id": invalid_id}
            )
            assert rejected.status_code == 422, rejected.text
        cleared = await _revision_preview(
            db_client, invoice, {"document_vat_rate_id": None}
        )
        assert cleared.status_code == 200, cleared.text

        # An unchanged, previously selected document rate remains lawful in LINE mode.
        async with admin_session_maker() as session:
            line_row = await session.get(Invoice, uuid.UUID(invoice["id"]))
            assert line_row is not None
            line_row.document_vat_rate_id = uuid.UUID(rates["NL standard (21%)"]["id"])
            await session.commit()
        async with admin_session_maker() as session:
            active_rate = await session.get(
                VatRate, uuid.UUID(rates["NL standard (21%)"]["id"])
            )
            assert active_rate is not None
            active_rate.active = False
            await session.commit()
        line_invoice = (await db_client.get(f"/api/v1/invoices/{invoice['id']}")).json()
        retained = await _revision_preview(
            db_client,
            line_invoice,
            {
                "lines": [
                    {
                        "line_id": line_invoice["lines"][0]["id"],
                        "name": line_invoice["lines"][0]["name"],
                        "quantity": "2",
                        "unit_price": line_invoice["lines"][0]["unit_price"],
                        "vat_rate_id": rates["NL standard (21%)"]["id"],
                    }
                ]
            },
        )
        assert retained.status_code == 200, retained.text

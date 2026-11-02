"""SI2 Step 1 read-only history and detail eligibility integration checks."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi import Request
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from test_m12_credit_integration import _credit_payload
from test_quote_payment_integration import (
    _accept_quote,
    _create_customer,
    _create_invoice,
    _create_quote,
    _full_auth,
    _setup_company,
)

from jai.db import set_rls_company
from jai.models._enums import EmailRelatedType, EmailStatus
from jai.models.document import InvoiceCreditBasisLine, InvoicePartySnapshot
from jai.models.email_log import EmailLog
from jai.models.invoice import Invoice, InvoiceTax
from jai.models.vat import VatRate
from jai.schemas.invoice_revision import InvoiceBusinessSnapshot, InvoiceRevisionDocumentSnapshot

pytestmark = pytest.mark.integration


async def test_revision_eligibility_and_empty_history_are_read_only(
    db_client: AsyncClient,
    db_session_maker: async_sessionmaker,
) -> None:
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    invoice = await _create_invoice(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )
    invoice_id = invoice["id"]

    detail = await db_client.get(f"/api/v1/invoices/{invoice_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["revision_eligibility"] == {"available": True, "reason_code": None}

    downloaded = await db_client.get(f"/api/v1/invoices/{invoice_id}/pdf?locale=en")
    assert downloaded.status_code == 200, downloaded.text
    after_download = await db_client.get(f"/api/v1/invoices/{invoice_id}")
    assert after_download.json()["revision_eligibility"]["available"] is True

    first = await db_client.get(f"/api/v1/invoices/{invoice_id}/revisions?offset=0&limit=10")
    assert first.status_code == 200, first.text
    assert first.json() == {"items": [], "total": 0, "offset": 0, "limit": 10}

    document = {
        key: detail.json()[key]
        for key in InvoiceRevisionDocumentSnapshot.model_fields
        if key in detail.json()
    }
    async with db_session_maker() as session:
        await set_rls_company(session, uuid.UUID(invoice["company_id"]))
        party = await session.scalar(
            select(InvoicePartySnapshot).where(
                InvoicePartySnapshot.invoice_id == uuid.UUID(invoice_id)
            )
        )
        assert party is not None
        party_snapshot = {
            "provenance": party.provenance.value,
            "seller_name": party.seller_name,
            "seller_legal_name": party.seller_legal_name,
            "seller_vat_id": party.seller_vat_id,
            "seller_coc_number": party.seller_coc_number,
            "seller_email": party.seller_email,
            "seller_phone": party.seller_phone,
            "seller_address": party.seller_address,
            "buyer_name": party.buyer_name,
            "buyer_company_name": party.buyer_company_name,
            "buyer_contact_name": party.buyer_contact_name,
            "buyer_vat_id": party.buyer_vat_id,
            "buyer_email": party.buyer_email,
            "buyer_phone": party.buyer_phone,
            "buyer_address": party.buyer_address,
            "locale": party.locale,
            "logo_id": party.logo_id,
        }
    document["party_snapshot"] = party_snapshot
    treatment = detail.json()["vat_treatment_snapshot"]
    document.update(
        {
            "vat_treatment_code": treatment["code"],
            "vat_treatment_label": treatment["label"],
            "vat_treatment_effect": treatment["effect"],
            "vat_treatment_requires_icp": treatment["requires_icp"],
        }
    )
    snapshot = InvoiceBusinessSnapshot(invoice=document, credit_basis=[]).model_dump(mode="json")
    async with db_session_maker() as session:
        await set_rls_company(session, uuid.UUID(invoice["company_id"]))
        for order in (501, 502):
            await session.execute(
                text(
                    "INSERT INTO invoice_revision "
                    "(id, company_id, invoice_id, revision_order, reason, "
                    "undelivered_confirmed, before_snapshot, after_snapshot) "
                    "VALUES (:id, :company, :invoice, :order, 'fixture', true, "
                    "CAST(:snapshot AS jsonb), CAST(:snapshot AS jsonb))"
                ),
                {
                    "id": uuid.uuid4(),
                    "company": invoice["company_id"],
                    "invoice": invoice_id,
                    "order": order,
                    "snapshot": json.dumps(snapshot),
                },
            )
        await session.commit()

    page_one = await db_client.get(f"/api/v1/invoices/{invoice_id}/revisions?offset=0&limit=1")
    page_two = await db_client.get(f"/api/v1/invoices/{invoice_id}/revisions?offset=1&limit=1")
    assert page_one.status_code == page_two.status_code == 200
    assert page_one.json()["total"] == page_two.json()["total"] == 2
    assert page_one.json()["items"][0]["revision_order"] == 502
    assert page_two.json()["items"][0]["revision_order"] == 501
    assert page_one.json()["items"][0]["before_snapshot"]["schema_version"] == 1
    round_tripped = page_one.json()["items"][0]["before_snapshot"]["invoice"]["party_snapshot"]
    assert round_tripped == snapshot["invoice"]["party_snapshot"]

    missing = await db_client.get(f"/api/v1/invoices/{uuid.uuid4()}/revisions")
    assert missing.status_code == 404
    unchanged = await db_client.get(f"/api/v1/invoices/{invoice_id}")
    assert unchanged.json()["updated_at"] == detail.json()["updated_at"]


async def test_revision_save_header_snapshot_noop_and_stale_conflict(
    db_client: AsyncClient,
) -> None:
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    invoice = await _create_invoice(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )
    invoice_id = invoice["id"]
    original = await db_client.get(f"/api/v1/invoices/{invoice_id}")
    before = original.json()
    saved = await db_client.post(
        f"/api/v1/invoices/{invoice_id}/revisions",
        json={
            "expected_updated_at": before["updated_at"],
            "changes": {"notes": "Revised payment note"},
            "reason": "Corrected invoice note",
            "confirm_not_delivered": True,
        },
    )
    assert saved.status_code == 200, saved.text
    after = saved.json()
    assert after["status"] == "SENT"
    assert after["invoice_number"] == before["invoice_number"]
    assert after["issued_at"] == before["issued_at"]
    assert after["issued_by_user_id"] == before["issued_by_user_id"]
    for field in ("total_incl_vat", "due_amount", "vat_total"):
        assert after[field] == before[field]
    assert after["revision_eligibility"] == {"available": True, "reason_code": None}

    current = await db_client.get(f"/api/v1/invoices/{invoice_id}")
    no_op = await db_client.post(
        f"/api/v1/invoices/{invoice_id}/revisions",
        json={
            "expected_updated_at": current.json()["updated_at"],
            "changes": {},
            "reason": "No effective edits",
            "confirm_not_delivered": True,
        },
    )
    assert no_op.status_code == 200, no_op.text
    assert no_op.json()["updated_at"] == current.json()["updated_at"]
    assert no_op.json()["revision_eligibility"] == {"available": True, "reason_code": None}
    ignored_document_rate = await db_client.post(
        f"/api/v1/invoices/{invoice_id}/revisions",
        json={
            "expected_updated_at": current.json()["updated_at"],
            "changes": {"document_vat_rate_id": seeds["rates"]["NL reduced (9%)"]["id"]},
            "reason": "LINE mode ignores document rate",
            "confirm_not_delivered": True,
        },
    )
    assert ignored_document_rate.status_code == 200, ignored_document_rate.text
    assert ignored_document_rate.json()["updated_at"] == current.json()["updated_at"]
    history = await db_client.get(f"/api/v1/invoices/{invoice_id}/revisions")
    assert history.json()["total"] == 1
    snapshot = history.json()["items"][0]
    assert snapshot["reason"] == "Corrected invoice note"
    assert snapshot["before_snapshot"]["invoice"]["notes"] == before["notes"]
    assert snapshot["after_snapshot"]["invoice"]["notes"] == "Revised payment note"

    stale = await db_client.post(
        f"/api/v1/invoices/{invoice_id}/revisions",
        json={
            "expected_updated_at": before["updated_at"],
            "changes": {"notes": "Stale overwrite"},
            "reason": "Stale editor",
            "confirm_not_delivered": True,
        },
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "STALE_VERSION"
    final = await db_client.get(f"/api/v1/invoices/{invoice_id}/revisions")
    assert final.json()["total"] == 1


async def test_revision_financial_save_retains_line_identity_and_rebuilds_credit_basis(
    db_client: AsyncClient,
    db_session_maker: async_sessionmaker,
) -> None:
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    invoice = await _create_invoice(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )
    before = (await db_client.get(f"/api/v1/invoices/{invoice['id']}")).json()
    old_line = before["lines"][0]
    line_input = {
        "line_id": old_line["id"],
        "product_id": old_line["product_id"],
        "name": old_line["name"],
        "description": old_line["description"],
        "quantity": old_line["quantity"],
        "unit_id": old_line["unit_id"],
        "unit_name": old_line["unit_name"],
        "unit_price": "200.000",
        "discount": {"type": old_line["discount_type"], "value": old_line["discount_value"]},
        "vat_rate_id": old_line["vat_rate_id"],
    }
    saved = await db_client.post(
        f"/api/v1/invoices/{invoice['id']}/revisions",
        json={
            "expected_updated_at": before["updated_at"],
            "changes": {"lines": [line_input]},
            "reason": "Corrected unit price",
            "confirm_not_delivered": True,
        },
    )
    assert saved.status_code == 200, saved.text
    after = saved.json()
    assert after["status"] == "SENT"
    assert after["lines"][0]["id"] == old_line["id"]
    assert after["lines"][0]["unit_price"] == "200.000"
    assert Decimal(after["total_incl_vat"]) == Decimal("242.00")
    assert after["due_amount"] == "242.000"
    async with db_session_maker() as session:
        await set_rls_company(session, uuid.UUID(invoice["company_id"]))
        basis = await session.scalar(
            select(InvoiceCreditBasisLine).where(
                InvoiceCreditBasisLine.invoice_id == uuid.UUID(invoice["id"])
            )
        )
        assert basis is not None
        assert basis.invoice_line_id == uuid.UUID(old_line["id"])
        assert basis.net_amount == 200
        assert basis.vat_amount == 42
        assert basis.gross_amount == 242
        basis_id = str(basis.id)
    history = await db_client.get(f"/api/v1/invoices/{invoice['id']}/revisions")
    assert history.json()["total"] == 1
    before_line_id = history.json()["items"][0]["before_snapshot"]["invoice"]["lines"][0]["id"]
    after_line_id = history.json()["items"][0]["after_snapshot"]["invoice"]["lines"][0]["id"]
    assert before_line_id == after_line_id == old_line["id"]
    current = after
    replacement = await db_client.post(
        f"/api/v1/invoices/{invoice['id']}/revisions",
        json={
            "expected_updated_at": current["updated_at"],
            "changes": {
                "lines": [
                    {key: value for key, value in line_input.items() if key != "line_id"}
                ]
            },
            "reason": "Replace line identity",
            "confirm_not_delivered": True,
        },
    )
    assert replacement.status_code == 200, replacement.text
    assert replacement.json()["lines"][0]["id"] != old_line["id"]
    async with db_session_maker() as session:
        await set_rls_company(session, uuid.UUID(invoice["company_id"]))
        current_basis = await session.scalar(
            select(InvoiceCreditBasisLine).where(
                InvoiceCreditBasisLine.invoice_id == uuid.UUID(invoice["id"])
            )
        )
        assert current_basis is not None
        basis_id = str(current_basis.id)
    method = await db_client.post("/api/v1/payment-methods", json={"name": "Post revision bank"})
    assert method.status_code == 201, method.text
    payment = await db_client.post(
        f"/api/v1/invoices/{invoice['id']}/payments",
        json={
            "payment_date": "2026-02-01",
            "amount": "20.00",
            "payment_method_id": method.json()["id"],
        },
    )
    assert payment.status_code == 201, payment.text
    assert payment.json()["due_amount"] == "222.000"
    credit_intent = {
        "full_remaining": False,
        "lines": [{
            "source_basis_line_id": basis_id,
            "input_mode": "QUANTITY",
            "quantity": "0.500",
        }],
    }
    preview = await db_client.post(
        f"/api/v1/invoices/{invoice['id']}/credit-notes/calculate", json=credit_intent
    )
    assert preview.status_code == 200, preview.text
    assert Decimal(preview.json()["gross_amount"]) == Decimal("121")
    credit = await db_client.post(
        f"/api/v1/invoices/{invoice['id']}/credit-notes",
        json=credit_intent | {"invoice_date": "2026-02-02"},
    )
    assert credit.status_code == 201, credit.text
    assert Decimal(credit.json()["total_incl_vat"]) == Decimal("121")
    issued_credit = await db_client.post(
        f"/api/v1/invoices/{credit.json()['id']}/status", json={"status": "SENT"}
    )
    assert issued_credit.status_code == 200, issued_credit.text
    source_after_credit = (await db_client.get(f"/api/v1/invoices/{invoice['id']}")).json()
    assert Decimal(source_after_credit["credited_total"]) == Decimal("121")
    assert Decimal(source_after_credit["due_amount"]) == Decimal("101")


async def test_revision_reorders_adds_and_removes_mixed_rate_lines_and_basis(
    db_client: AsyncClient,
    db_session_maker: async_sessionmaker,
) -> None:
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    rate_21 = seeds["rates"]["NL standard (21%)"]["id"]
    rate_9 = seeds["rates"]["NL reduced (9%)"]["id"]
    created = await db_client.post(
        "/api/v1/invoices",
        json={
            "customer_id": customer_id,
            "invoice_date": "2026-01-10",
            "tax_mode": "LINE",
            "amounts_include_vat": False,
            "lines": [
                {"name": "Keep 21", "quantity": "1", "unit_price": "100", "vat_rate_id": rate_21},
                {"name": "Remove 9", "quantity": "2", "unit_price": "50", "vat_rate_id": rate_9},
                {"name": "Keep 9", "quantity": "1", "unit_price": "80", "vat_rate_id": rate_9},
            ],
        },
    )
    assert created.status_code == 201, created.text
    issued = await db_client.post(
        f"/api/v1/invoices/{created.json()['id']}/status", json={"status": "SENT"}
    )
    assert issued.status_code == 200, issued.text
    before = issued.json()
    ids = {line["name"]: line["id"] for line in before["lines"]}
    revised = await db_client.post(
        f"/api/v1/invoices/{before['id']}/revisions",
        json={
            "expected_updated_at": before["updated_at"],
            "changes": {
                "lines": [
                    {
                        "line_id": ids["Keep 9"],
                        "name": "Keep 9 moved",
                        "quantity": "1",
                        "unit_price": "80",
                        "vat_rate_id": rate_9,
                    },
                    {"name": "New 21", "quantity": "1", "unit_price": "40", "vat_rate_id": rate_21},
                    {
                        "line_id": ids["Keep 21"],
                        "name": "Keep 21",
                        "quantity": "1",
                        "unit_price": "100",
                        "vat_rate_id": rate_21,
                    },
                ]
            },
            "reason": "Reorder and replace mixed VAT lines",
            "confirm_not_delivered": True,
        },
    )
    assert revised.status_code == 200, revised.text
    after = revised.json()
    assert [line["name"] for line in after["lines"]] == ["Keep 9 moved", "New 21", "Keep 21"]
    assert after["lines"][0]["id"] == ids["Keep 9"]
    assert after["lines"][1]["id"] not in ids.values()
    assert after["lines"][2]["id"] == ids["Keep 21"]
    assert ids["Remove 9"] not in {line["id"] for line in after["lines"]}
    async with db_session_maker() as session:
        await set_rls_company(session, uuid.UUID(before["company_id"]))
        bases = list(
            (
                await session.scalars(
                    select(InvoiceCreditBasisLine)
                    .where(InvoiceCreditBasisLine.invoice_id == uuid.UUID(before["id"]))
                    .order_by(InvoiceCreditBasisLine.sort_order)
                )
            ).all()
        )
        assert [str(row.invoice_line_id) for row in bases] == [
            line["id"] for line in after["lines"]
        ]
        assert [row.sort_order for row in bases] == [0, 1, 2]
        assert {str(row.invoice_line_id) for row in bases} == {
            line["id"] for line in after["lines"]
        }
        assert sum((Decimal(str(row.gross_amount)) for row in bases), Decimal("0")) == Decimal(
            after["total_incl_vat"]
        )
    history = await db_client.get(f"/api/v1/invoices/{before['id']}/revisions")
    snap = history.json()["items"][0]
    assert [line["id"] for line in snap["before_snapshot"]["invoice"]["lines"]] == [
        line["id"] for line in before["lines"]
    ]
    assert [line["id"] for line in snap["after_snapshot"]["invoice"]["lines"]] == [
        line["id"] for line in after["lines"]
    ]
    async with db_session_maker() as session:
        await set_rls_company(session, uuid.UUID(before["company_id"]))
        basis = next(row for row in bases if row.name == "New 21")
        basis_id = str(basis.id)
    intent = {
        "full_remaining": False,
        "lines": [{"source_basis_line_id": basis_id, "input_mode": "QUANTITY", "quantity": "0.5"}],
    }
    credit_preview = await db_client.post(
        f"/api/v1/invoices/{before['id']}/credit-notes/calculate", json=intent
    )
    assert credit_preview.status_code == 200, credit_preview.text
    assert Decimal(credit_preview.json()["gross_amount"]) > 0
    credit = await db_client.post(
        f"/api/v1/invoices/{before['id']}/credit-notes",
        json=intent | {"invoice_date": "2026-02-03"},
    )
    assert credit.status_code == 201, credit.text
    assert Decimal(credit.json()["total_incl_vat"]) == Decimal(
        credit_preview.json()["gross_amount"]
    )
    issued_credit = await db_client.post(
        f"/api/v1/invoices/{credit.json()['id']}/status", json={"status": "SENT"}
    )
    assert issued_credit.status_code == 200, issued_credit.text
    source_after_credit = (await db_client.get(f"/api/v1/invoices/{before['id']}")).json()
    assert Decimal(source_after_credit["credited_total"]) == Decimal(
        credit_preview.json()["gross_amount"]
    )


async def test_document_revision_uses_frozen_rate_and_rebuilds_credit_basis(
    db_client: AsyncClient,
    db_session_maker: async_sessionmaker,
    admin_session_maker: async_sessionmaker,
) -> None:
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    rate = seeds["rates"]["NL standard (21%)"]
    created = await db_client.post(
        "/api/v1/invoices",
        json={
            "customer_id": customer_id,
            "invoice_date": "2026-01-10",
            "tax_mode": "DOCUMENT",
            "amounts_include_vat": True,
            "document_vat_rate_id": rate["id"],
            "discount": {"type": "PERCENTAGE", "value": "10"},
            "lines": [
                {"name": "Document A", "quantity": "1", "unit_price": "121"},
                {"name": "Document B", "quantity": "2", "unit_price": "121"},
            ],
        },
    )
    assert created.status_code == 201, created.text
    issued = await db_client.post(
        f"/api/v1/invoices/{created.json()['id']}/status", json={"status": "SENT"}
    )
    assert issued.status_code == 200, issued.text
    before = issued.json()
    async with admin_session_maker() as session:
        master = await session.get(VatRate, uuid.UUID(rate["id"]))
        assert master is not None
        master.label = "Changed after issue"
        master.percent = Decimal("88")
        master.active = False
        await session.commit()
    line_a, line_b = before["lines"]
    revised = await db_client.post(
        f"/api/v1/invoices/{before['id']}/revisions",
        json={
            "expected_updated_at": before["updated_at"],
            "changes": {
                "lines": [
                    {
                        "line_id": line_a["id"],
                        "name": line_a["name"],
                        "quantity": "1",
                        "unit_price": "121",
                    },
                    {
                        "line_id": line_b["id"],
                        "name": line_b["name"],
                        "quantity": "2",
                        "unit_price": "242",
                    },
                ]
            },
            "reason": "Correct DOCUMENT inclusive amount",
            "confirm_not_delivered": True,
        },
    )
    assert revised.status_code == 200, revised.text
    after = revised.json()
    assert after["document_vat_rate_id"] == rate["id"]
    async with db_session_maker() as session:
        await set_rls_company(session, uuid.UUID(before["company_id"]))
        tax = await session.scalar(
            select(InvoiceTax).where(InvoiceTax.invoice_id == uuid.UUID(before["id"]))
        )
        assert tax is not None
        assert tax.vat_rate_label == rate["label"]
        assert Decimal(str(tax.vat_rate_percent)) == Decimal("21.000")
    assert Decimal(after["total_incl_vat"]) != Decimal(before["total_incl_vat"])
    async with db_session_maker() as session:
        await set_rls_company(session, uuid.UUID(before["company_id"]))
        basis_rows = list(
            (
                await session.scalars(
                    select(InvoiceCreditBasisLine)
                    .where(InvoiceCreditBasisLine.invoice_id == uuid.UUID(before["id"]))
                    .order_by(InvoiceCreditBasisLine.sort_order)
                )
            ).all()
        )
        assert sum((Decimal(str(row.gross_amount)) for row in basis_rows), Decimal("0")) == Decimal(
            after["total_incl_vat"]
        )
        assert all(row.vat_rate_label == rate["label"] for row in basis_rows)
        assert all(
            Decimal(str(row.effective_vat_percent)) == Decimal("21.000")
            for row in basis_rows
        )
        basis_id = str(basis_rows[1].id)
    intent = {
        "full_remaining": False,
        "lines": [{"source_basis_line_id": basis_id, "input_mode": "QUANTITY", "quantity": "0.5"}],
    }
    credit_preview = await db_client.post(
        f"/api/v1/invoices/{before['id']}/credit-notes/calculate", json=intent
    )
    assert credit_preview.status_code == 200, credit_preview.text
    assert Decimal(credit_preview.json()["gross_amount"]) > 0
    credit = await db_client.post(
        f"/api/v1/invoices/{before['id']}/credit-notes",
        json=intent | {"invoice_date": "2026-02-03"},
    )
    assert credit.status_code == 201, credit.text
    assert Decimal(credit.json()["total_incl_vat"]) == Decimal(
        credit_preview.json()["gross_amount"]
    )
    issued_credit = await db_client.post(
        f"/api/v1/invoices/{credit.json()['id']}/status", json={"status": "SENT"}
    )
    assert issued_credit.status_code == 200, issued_credit.text


async def test_line_unused_document_rate_keeps_legacy_precision_in_preview_and_save(
    db_client: AsyncClient,
    db_session_maker: async_sessionmaker,
) -> None:
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    invoice = await _create_invoice(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )
    async with db_session_maker() as session:
        await set_rls_company(session, uuid.UUID(invoice["company_id"]))
        row = await session.get(Invoice, uuid.UUID(invoice["id"]))
        assert row is not None
        row.total_incl_vat = row.due_amount = row.payable_before_payments = Decimal("121.003")
        row.base_total_incl_vat = row.base_due_amount = row.base_payable_before_payments = Decimal(
            "121.003"
        )
        await session.commit()
    current = (await db_client.get(f"/api/v1/invoices/{invoice['id']}" )).json()
    unused_rate = seeds["rates"]["NL reduced (9%)"]["id"]
    changes = {"document_vat_rate_id": unused_rate}
    preview = await db_client.post(
        f"/api/v1/invoices/{invoice['id']}/revisions/calculate",
        json={"expected_updated_at": current["updated_at"], "changes": changes},
    )
    assert preview.status_code == 200, preview.text
    assert Decimal(preview.json()["total_incl_vat"]) == Decimal("121.003")
    saved = await db_client.post(
        f"/api/v1/invoices/{invoice['id']}/revisions",
        json={
            "expected_updated_at": current["updated_at"],
            "changes": changes,
            "reason": "Unused LINE document rate has no persisted effect",
            "confirm_not_delivered": True,
        },
    )
    assert saved.status_code == 200, saved.text
    assert Decimal(saved.json()["total_incl_vat"]) == Decimal("121.003")
    assert saved.json()["updated_at"] == current["updated_at"]
    reloaded = (await db_client.get(f"/api/v1/invoices/{invoice['id']}" )).json()
    assert Decimal(reloaded["total_incl_vat"]) == Decimal("121.003")
    history = await db_client.get(f"/api/v1/invoices/{invoice['id']}/revisions")
    assert history.json()["total"] == 0


async def test_concurrent_revisions_serialize_on_invoice_version(db_client: AsyncClient) -> None:
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    invoice = await _create_invoice(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )
    current = (await db_client.get(f"/api/v1/invoices/{invoice['id']}")).json()

    async def revise(note: str):
        return await db_client.post(
            f"/api/v1/invoices/{invoice['id']}/revisions",
            json={
                "expected_updated_at": current["updated_at"],
                "changes": {"notes": note},
                "reason": note,
                "confirm_not_delivered": True,
            },
        )

    first, second = await asyncio.gather(revise("Concurrent A"), revise("Concurrent B"))
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    history = await db_client.get(f"/api/v1/invoices/{invoice['id']}/revisions")
    assert history.json()["total"] == 1


async def test_revision_lock_serializes_with_waiting_payment(
    db_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jai.services import payment as payment_service

    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    invoice = await _create_invoice(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )
    method = await db_client.post("/api/v1/payment-methods", json={"name": "Concurrent bank"})
    assert method.status_code == 201, method.text
    current = (await db_client.get(f"/api/v1/invoices/{invoice['id']}")).json()
    original_payment_lock = payment_service.lock_settlement_chain
    revision_locked = asyncio.Event()
    payment_waiting = asyncio.Event()
    release_revision = asyncio.Event()

    lock_calls = 0

    async def gate_parent_lock(*args: object, **kwargs: object):
        nonlocal lock_calls
        lock_calls += 1
        if lock_calls == 1:
            locked = await original_payment_lock(*args, **kwargs)
            revision_locked.set()
            await release_revision.wait()
            return locked
        payment_waiting.set()
        return await original_payment_lock(*args, **kwargs)

    monkeypatch.setattr(payment_service, "lock_settlement_chain", gate_parent_lock)
    revision_task = asyncio.create_task(
        db_client.post(
            f"/api/v1/invoices/{invoice['id']}/revisions",
            json={
                "expected_updated_at": current["updated_at"],
                "changes": {"lines": [{
                    "line_id": current["lines"][0]["id"],
                    "name": current["lines"][0]["name"],
                    "quantity": current["lines"][0]["quantity"],
                    "unit_price": "200.000",
                    "discount": {
                        "type": current["lines"][0]["discount_type"],
                        "value": current["lines"][0]["discount_value"],
                    },
                    "vat_rate_id": current["lines"][0]["vat_rate_id"],
                }]},
                "reason": "Concurrent payment check",
                "confirm_not_delivered": True,
            },
        )
    )
    await asyncio.wait_for(revision_locked.wait(), timeout=10)
    payment_task = asyncio.create_task(
        db_client.post(
            f"/api/v1/invoices/{invoice['id']}/payments",
            json={
                "payment_date": "2026-02-01",
                "amount": "20.00",
                "payment_method_id": method.json()["id"],
            },
        )
    )
    await asyncio.wait_for(payment_waiting.wait(), timeout=10)
    release_revision.set()
    revision_response, payment_response = await asyncio.wait_for(
        asyncio.gather(revision_task, payment_task), timeout=20
    )
    assert revision_response.status_code == 200, revision_response.text
    assert payment_response.status_code == 201, payment_response.text
    assert payment_response.json()["due_amount"] == "222.000"


async def test_revision_serializes_with_credit_draft_creation(db_client: AsyncClient) -> None:
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    invoice = await _create_invoice(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )
    current = (await db_client.get(f"/api/v1/invoices/{invoice['id']}")).json()
    line = current["lines"][0]

    async def revise():
        return await db_client.post(
            f"/api/v1/invoices/{invoice['id']}/revisions",
            json={
                "expected_updated_at": current["updated_at"],
                "changes": {"lines": [{
                    "line_id": line["id"],
                    "name": line["name"],
                    "quantity": line["quantity"],
                    "unit_price": "200.000",
                    "discount": {"type": line["discount_type"], "value": line["discount_value"]},
                    "vat_rate_id": line["vat_rate_id"],
                }]},
                "reason": "Race with Credit creation",
                "confirm_not_delivered": True,
            },
        )

    async def create_credit():
        return await db_client.post(
            f"/api/v1/invoices/{invoice['id']}/credit-notes",
            json={"full_remaining": True, "invoice_date": "2026-02-02"},
        )

    revision, credit = await asyncio.wait_for(
        asyncio.gather(revise(), create_credit()), timeout=30
    )
    assert credit.status_code == 201, credit.text
    assert revision.status_code in {200, 409}, revision.text
    expected_credit_total = Decimal("242") if revision.status_code == 200 else Decimal("121")
    assert Decimal(credit.json()["total_incl_vat"]) == expected_credit_total
    history = await db_client.get(f"/api/v1/invoices/{invoice['id']}/revisions")
    assert history.json()["total"] == (1 if revision.status_code == 200 else 0)


@pytest.mark.parametrize("smtp_succeeds", [True, False], ids=["sent", "failed"])
async def test_revision_waits_for_invoice_smtp_render_and_send_commit(
    db_client: AsyncClient,
    runtime_db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    smtp_succeeds: bool,
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from test_m12_artifact_integration import _smtp

    from jai.db import get_session
    from jai.main import app
    from jai.services import email as email_service
    from jai.services import payment as payment_service
    from jai.services import pdf as pdf_service

    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    invoice = await _create_invoice(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )
    current = (await db_client.get(f"/api/v1/invoices/{invoice['id']}")).json()
    engine_url = runtime_db_engine.url.render_as_string(hide_password=False)
    send_engine = create_async_engine(engine_url, pool_size=1, max_overflow=0)
    revision_engine = create_async_engine(engine_url, pool_size=1, max_overflow=0)
    send_sessions = async_sessionmaker(send_engine, expire_on_commit=False, class_=AsyncSession)
    revision_sessions = async_sessionmaker(
        revision_engine, expire_on_commit=False, class_=AsyncSession
    )
    prior_provider = app.dependency_overrides[get_session]
    renderer_has_lock = asyncio.Event()
    release_send = asyncio.Event()
    revision_waiting = asyncio.Event()
    revision_backend_pid: int | None = None
    captured: dict[str, Any] = {}
    original_render = pdf_service.render_invoice_pdf_artifact
    original_lock = payment_service.lock_settlement_chain

    async def gate_after_render(*args: object, **kwargs: object):
        result = await original_render(*args, **kwargs)
        renderer_has_lock.set()
        await release_send.wait()
        return result

    async def observe_revision(*args: object, **kwargs: object):
        nonlocal revision_backend_pid
        session = args[0]
        revision_backend_pid = int(await session.scalar(text("SELECT pg_backend_pid()")))
        revision_waiting.set()
        return await original_lock(*args, **kwargs)

    async def sessions_for_request(request: Request) -> AsyncIterator[AsyncSession]:
        factory = send_sessions if request.url.path.endswith("/send") else revision_sessions
        async with factory() as session:
            yield session

    async def smtp_config(*args: object) -> object:
        return _smtp()

    async def capture_mail(**kwargs: Any) -> None:
        captured.update(kwargs)
        if not smtp_succeeds:
            raise RuntimeError("test SMTP failure")

    monkeypatch.setattr(pdf_service, "render_invoice_pdf_artifact", gate_after_render)
    monkeypatch.setattr(payment_service, "lock_settlement_chain", observe_revision)
    monkeypatch.setattr(email_service, "_get_smtp_config", smtp_config)
    monkeypatch.setattr(email_service, "_send_mail", capture_mail)
    app.dependency_overrides[get_session] = sessions_for_request
    try:
        send_task = asyncio.create_task(
            db_client.post(
                f"/api/v1/invoices/{invoice['id']}/send",
                json={"to": "si2-test@example.com", "locale": "en"},
            )
        )
        try:
            await asyncio.wait_for(renderer_has_lock.wait(), timeout=15)
        except TimeoutError:
            response = await send_task
            raise AssertionError(
                "Invoice send did not reach the renderer lock: "
                f"{response.status_code} {response.text}"
            ) from None
        revision_task = asyncio.create_task(
            db_client.post(
                f"/api/v1/invoices/{invoice['id']}/revisions",
                json={
                    "expected_updated_at": current["updated_at"],
                    "changes": {"reference_number": "Must wait for send"},
                    "reason": "Race with successful SMTP send",
                    "confirm_not_delivered": True,
                },
            )
        )
        await asyncio.wait_for(revision_waiting.wait(), timeout=10)
        assert revision_backend_pid is not None
        # Prove the revision's own backend is blocked by the send transaction's
        # shared invoice lock before opening the SMTP gate. The event above only
        # proves the coroutine reached the lock helper.
        async with runtime_db_engine.connect() as probe:
            for _ in range(250):
                blocked = await probe.scalar(
                    text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"),
                    {"pid": revision_backend_pid},
                )
                if blocked:
                    break
                await asyncio.sleep(0.02)
            assert blocked, "revision backend never became blocked on the SMTP-held invoice lock"
        release_send.set()
        sent, revision = await asyncio.wait_for(
            asyncio.gather(send_task, revision_task), timeout=35
        )
    finally:
        release_send.set()
        app.dependency_overrides[get_session] = prior_provider
        await send_engine.dispose()
        await revision_engine.dispose()
    assert sent.status_code == 200, sent.text
    assert sent.json()["status"] == ("SENT" if smtp_succeeds else "FAILED")
    if smtp_succeeds:
        assert revision.status_code == 409, revision.text
        assert revision.json()["detail"]["code"] == "REVISION_ALREADY_DELIVERED"
    else:
        assert revision.status_code == 200, revision.text
        assert revision.json()["reference_number"] == "Must wait for send"
    assert captured["attachment_bytes"]
    assert captured["attachment_filename"] == sent.json()["attachment_filename"]


async def test_revision_is_allowed_after_failed_invoice_send_outcome(
    db_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_m12_artifact_integration import _smtp

    from jai.services import email as email_service

    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    invoice = await _create_invoice(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )

    async def smtp_config(*args: object) -> object:
        return _smtp()

    async def fail_mail(**kwargs: Any) -> None:
        raise RuntimeError("test SMTP failure")

    monkeypatch.setattr(email_service, "_get_smtp_config", smtp_config)
    monkeypatch.setattr(email_service, "_send_mail", fail_mail)
    failed = await db_client.post(
        f"/api/v1/invoices/{invoice['id']}/send",
        json={"to": "si2-test@example.com", "locale": "en"},
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["status"] == "FAILED"
    current = (await db_client.get(f"/api/v1/invoices/{invoice['id']}")).json()
    revised = await db_client.post(
        f"/api/v1/invoices/{invoice['id']}/revisions",
        json={
            "expected_updated_at": current["updated_at"],
            "changes": {"notes": "Allowed after SMTP failure"},
            "reason": "Failed send did not deliver",
            "confirm_not_delivered": True,
        },
    )
    assert revised.status_code == 200, revised.text


async def test_revision_save_rechecks_actual_partial_payment(db_client: AsyncClient) -> None:
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    invoice = await _create_invoice(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )
    method = await db_client.post("/api/v1/payment-methods", json={"name": "Bank"})
    assert method.status_code == 201, method.text
    payment = await db_client.post(
        f"/api/v1/invoices/{invoice['id']}/payments",
        json={
            "payment_date": "2026-02-01",
            "amount": "20.00",
            "payment_method_id": method.json()["id"],
        },
    )
    assert payment.status_code == 201, payment.text
    current = (await db_client.get(f"/api/v1/invoices/{invoice['id']}")).json()
    response = await db_client.post(
        f"/api/v1/invoices/{invoice['id']}/revisions",
        json={
            "expected_updated_at": current["updated_at"],
            "changes": {"notes": "Must stay blocked"},
            "reason": "Attempt after partial payment",
            "confirm_not_delivered": True,
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "REVISION_HAS_PAYMENT"
    history = await db_client.get(f"/api/v1/invoices/{invoice['id']}/revisions")
    assert history.json()["total"] == 0


async def test_revision_save_rechecks_successful_delivery_log(
    db_client: AsyncClient,
    db_session_maker: async_sessionmaker,
) -> None:
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    invoice = await _create_invoice(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )
    async with db_session_maker() as session:
        await set_rls_company(session, uuid.UUID(invoice["company_id"]))
        session.add(
            EmailLog(
                company_id=uuid.UUID(invoice["company_id"]),
                related_type=EmailRelatedType.INVOICE,
                related_id=uuid.UUID(invoice["id"]),
                to_email="test@example.invalid",
                subject="Test delivery",
                body_snapshot="<p>Test</p>",
                status=EmailStatus.SENT,
            )
        )
        await session.commit()
    current = (await db_client.get(f"/api/v1/invoices/{invoice['id']}")).json()
    response = await db_client.post(
        f"/api/v1/invoices/{invoice['id']}/revisions",
        json={
            "expected_updated_at": current["updated_at"],
            "changes": {"notes": "Must stay blocked"},
            "reason": "Attempt after successful email",
            "confirm_not_delivered": True,
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "REVISION_ALREADY_DELIVERED"


async def test_zero_total_revision_keeps_issued_invoice_identity(db_client: AsyncClient) -> None:
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    invoice = await _create_invoice(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )
    before = (await db_client.get(f"/api/v1/invoices/{invoice['id']}")).json()
    row = before["lines"][0]
    response = await db_client.post(
        f"/api/v1/invoices/{invoice['id']}/revisions",
        json={
            "expected_updated_at": before["updated_at"],
            "changes": {
                "lines": [
                    {
                        "line_id": row["id"],
                        "name": row["name"],
                        "quantity": row["quantity"],
                        "unit_price": "0",
                        "discount": {"type": row["discount_type"], "value": row["discount_value"]},
                        "vat_rate_id": row["vat_rate_id"],
                    }
                ]
            },
            "reason": "Remove charge",
            "confirm_not_delivered": True,
        },
    )
    assert response.status_code == 200, response.text
    revised = response.json()
    assert revised["status"] == "SENT"
    assert revised["invoice_number"] == before["invoice_number"]
    assert revised["issued_at"] == before["issued_at"]
    assert revised["issued_by_user_id"] == before["issued_by_user_id"]
    assert revised["total_incl_vat"] in {"0.00", "0.000"}
    assert revised["due_amount"] in {"0.000", "0.00"}


async def test_quote_converted_standard_can_be_revised_under_quote_prefix(
    db_client: AsyncClient,
) -> None:
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    quote = await _create_quote(
        db_client,
        customer_id,
        seeds["rates"]["NL standard (21%)"]["id"],
        rate_9=seeds["rates"]["NL reduced (9%)"]["id"],
    )
    await _accept_quote(db_client, quote["id"])
    converted = await db_client.post(f"/api/v1/quotes/{quote['id']}/convert")
    assert converted.status_code == 201, converted.text
    issued = await db_client.post(
        f"/api/v1/invoices/{converted.json()['id']}/status", json={"status": "SENT"}
    )
    assert issued.status_code == 200, issued.text
    before = (await db_client.get(f"/api/v1/invoices/{converted.json()['id']}")).json()
    original_ids = [row["id"] for row in before["lines"]]
    reordered_lines = [
        {
            "line_id": row["id"],
            "product_id": row["product_id"],
            "name": row["name"],
            "description": row["description"],
            "quantity": row["quantity"],
            "unit_id": row["unit_id"],
            "unit_name": row["unit_name"],
            "unit_price": row["unit_price"],
            "discount": {"type": row["discount_type"], "value": row["discount_value"]},
            "vat_rate_id": row["vat_rate_id"],
        }
        for row in reversed(before["lines"])
    ]
    revised = await db_client.post(
        f"/api/v1/invoices/{converted.json()['id']}/revisions",
        json={
            "expected_updated_at": before["updated_at"],
            "changes": {
                "reference_number": "Quote conversion correction",
                "lines": reordered_lines,
            },
            "reason": "Corrected reference",
            "confirm_not_delivered": True,
        },
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["quote_id"] == quote["id"]
    assert revised.json()["invoice_number"] == before["invoice_number"]
    assert revised.json()["reference_number"] == "Quote conversion correction"
    assert [row["id"] for row in revised.json()["lines"]] == list(reversed(original_ids))


async def test_text_only_line_revision_preserves_pricing_rows_and_advances_version(
    db_client: AsyncClient,
    db_session_maker: async_sessionmaker,
) -> None:
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    invoice = await _create_invoice(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )
    before = (await db_client.get(f"/api/v1/invoices/{invoice['id']}")).json()
    prior_line = before["lines"][0]
    payload = {
        "line_id": prior_line["id"],
        "product_id": prior_line["product_id"],
        "name": "Corrected description",
        "description": prior_line["description"],
        "quantity": prior_line["quantity"],
        "unit_id": prior_line["unit_id"],
        "unit_name": prior_line["unit_name"],
        "unit_price": prior_line["unit_price"],
        "discount": {"type": prior_line["discount_type"], "value": prior_line["discount_value"]},
        "vat_rate_id": prior_line["vat_rate_id"],
    }
    response = await db_client.post(
        f"/api/v1/invoices/{invoice['id']}/revisions",
        json={
            "expected_updated_at": before["updated_at"],
            "changes": {"lines": [payload]},
            "reason": "Corrected line text",
            "confirm_not_delivered": True,
        },
    )
    assert response.status_code == 200, response.text
    after = response.json()
    assert after["updated_at"] != before["updated_at"]
    assert after["lines"][0]["id"] == prior_line["id"]
    assert after["lines"][0]["name"] == "Corrected description"
    for field in ("subtotal_excl_vat", "vat_total", "total_incl_vat", "due_amount"):
        assert after[field] == before[field]
    assert after["lines"][0]["line_taxes"] == prior_line["line_taxes"]
    async with db_session_maker() as session:
        await set_rls_company(session, uuid.UUID(invoice["company_id"]))
        basis = await session.scalar(
            select(InvoiceCreditBasisLine).where(
                InvoiceCreditBasisLine.invoice_id == uuid.UUID(invoice["id"])
            )
        )
        assert basis is not None and basis.name == "Corrected description"


async def test_revision_event_failure_rolls_back_invoice_and_audit(
    db_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    invoice = await _create_invoice(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )
    before = (await db_client.get(f"/api/v1/invoices/{invoice['id']}")).json()

    async def fail_event(*args: object, **kwargs: object) -> object:
        raise RuntimeError("injected revision event failure")

    monkeypatch.setattr("jai.services.document_chain.append_document_chain_event", fail_event)
    with pytest.raises(RuntimeError, match="injected revision event failure"):
        await db_client.post(
            f"/api/v1/invoices/{invoice['id']}/revisions",
            json={
                "expected_updated_at": before["updated_at"],
                "changes": {"notes": "Must roll back"},
                "reason": "Injected failure",
                "confirm_not_delivered": True,
            },
        )
    current = (await db_client.get(f"/api/v1/invoices/{invoice['id']}")).json()
    history = await db_client.get(f"/api/v1/invoices/{invoice['id']}/revisions")
    assert current["notes"] == before["notes"]
    assert current["updated_at"] == before["updated_at"]
    assert history.json()["total"] == 0


async def test_invoice_revision_runtime_role_is_tenant_scoped_and_append_only(
    db_client: AsyncClient,
    runtime_session_maker: async_sessionmaker,
) -> None:
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    invoice = await _create_invoice(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )
    company_id = uuid.UUID(seeds["company_id"])
    invoice_id = uuid.UUID(invoice["id"])
    revision_id = uuid.uuid4()
    async with runtime_session_maker() as session:
        await set_rls_company(session, company_id)
        await session.execute(
            text(
                "INSERT INTO invoice_revision "
                "(id, company_id, invoice_id, reason, undelivered_confirmed, "
                "before_snapshot, after_snapshot) VALUES "
                "(:id, :company_id, :invoice_id, 'test', true, '{}'::jsonb, '{}'::jsonb)"
            ),
            {"id": revision_id, "company_id": company_id, "invoice_id": invoice_id},
        )
        await session.commit()
        assert (
            await session.scalar(
                text("SELECT count(*) FROM invoice_revision WHERE id = :id"),
                {"id": revision_id},
            )
            == 0
        )
        await set_rls_company(session, company_id)
        assert (
            await session.scalar(
                text("SELECT count(*) FROM invoice_revision WHERE id = :id"), {"id": revision_id}
            )
            == 1
        )
        with pytest.raises(DBAPIError):
            await session.execute(
                text("UPDATE invoice_revision SET reason = 'changed' WHERE id = :id"),
                {"id": revision_id},
            )
            await session.commit()
        await session.rollback()
        await set_rls_company(session, company_id)
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "INSERT INTO invoice_revision "
                    "(company_id, invoice_id, reason, undelivered_confirmed, "
                    "before_snapshot, after_snapshot) VALUES "
                    "(:company, :invoice, 'test', true, '{}'::jsonb, '{}'::jsonb)"
                ),
                {"company": company_id, "invoice": uuid.uuid4()},
            )
            await session.commit()
        await session.rollback()
        await set_rls_company(session, company_id)
        with pytest.raises(DBAPIError):
            await session.execute(
                text("DELETE FROM invoice_revision WHERE id = :id"), {"id": revision_id}
            )
            await session.commit()


async def test_revision_eligibility_uses_actual_payments_mail_and_credit_drafts(
    db_client: AsyncClient,
    db_session_maker: async_sessionmaker,
) -> None:
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    rate_id = seeds["rates"]["NL standard (21%)"]["id"]
    company_id = uuid.UUID(seeds["company_id"])

    failed_mail_invoice = await _create_invoice(db_client, customer_id, rate_id)
    successful_mail_invoice = await _create_invoice(db_client, customer_id, rate_id)
    async with db_session_maker() as session:
        await set_rls_company(session, company_id)
        session.add_all(
            [
                EmailLog(
                    company_id=company_id,
                    related_type=EmailRelatedType.INVOICE,
                    related_id=uuid.UUID(failed_mail_invoice["id"]),
                    to_email="test@example.invalid",
                    subject="failed test",
                    body_snapshot="test",
                    status=EmailStatus.FAILED,
                ),
                EmailLog(
                    company_id=company_id,
                    related_type=EmailRelatedType.INVOICE,
                    related_id=uuid.UUID(successful_mail_invoice["id"]),
                    to_email="test@example.invalid",
                    subject="successful test",
                    body_snapshot="test",
                    status=EmailStatus.SENT,
                ),
            ]
        )
        await session.commit()

    failed_projection = await db_client.get(f"/api/v1/invoices/{failed_mail_invoice['id']}")
    successful_projection = await db_client.get(f"/api/v1/invoices/{successful_mail_invoice['id']}")
    assert failed_projection.json()["revision_eligibility"]["available"] is True
    assert successful_projection.json()["revision_eligibility"] == {
        "available": False,
        "reason_code": "REVISION_ALREADY_DELIVERED",
    }

    paid_invoice = await _create_invoice(db_client, customer_id, rate_id)
    payment = await db_client.post(
        f"/api/v1/invoices/{paid_invoice['id']}/payments",
        json={"payment_date": "2026-05-01", "amount": "1.000"},
    )
    assert payment.status_code == 201, payment.text
    paid_projection = await db_client.get(f"/api/v1/invoices/{paid_invoice['id']}")
    assert paid_projection.json()["revision_eligibility"] == {
        "available": False,
        "reason_code": "REVISION_HAS_PAYMENT",
    }

    dependent_invoice = await _create_invoice(db_client, customer_id, rate_id)
    credit = await db_client.post(
        f"/api/v1/invoices/{dependent_invoice['id']}/credit-notes",
        json=_credit_payload(),
    )
    assert credit.status_code == 201, credit.text
    dependent_projection = await db_client.get(f"/api/v1/invoices/{dependent_invoice['id']}")
    assert dependent_projection.json()["revision_eligibility"] == {
        "available": False,
        "reason_code": "REVISION_HAS_DEPENDENCIES",
    }

    quote = await _create_quote(db_client, customer_id, rate_id)
    await _accept_quote(db_client, quote["id"])
    converted = await db_client.post(f"/api/v1/quotes/{quote['id']}/convert")
    assert converted.status_code == 201, converted.text
    issued = await db_client.post(
        f"/api/v1/invoices/{converted.json()['id']}/status", json={"status": "SENT"}
    )
    assert issued.status_code == 200, issued.text
    direct_conversion = await db_client.get(f"/api/v1/invoices/{converted.json()['id']}")
    assert direct_conversion.json()["revision_eligibility"]["available"] is True

    receipt_quote = await _create_quote(db_client, customer_id, rate_id)
    await _accept_quote(db_client, receipt_quote["id"])
    receipt = await db_client.post(
        f"/api/v1/quotes/{receipt_quote['id']}/payments",
        json={"payment_date": "2026-05-02", "amount": "20.000"},
    )
    assert receipt.status_code == 201, receipt.text
    receipt_conversion = await db_client.post(f"/api/v1/quotes/{receipt_quote['id']}/convert")
    assert receipt_conversion.status_code == 201, receipt_conversion.text
    receipt_issued = await db_client.post(
        f"/api/v1/invoices/{receipt_conversion.json()['id']}/status",
        json={"status": "SENT"},
    )
    assert receipt_issued.status_code == 200, receipt_issued.text
    receipt_projection = await db_client.get(f"/api/v1/invoices/{receipt_conversion.json()['id']}")
    assert receipt_projection.json()["revision_eligibility"] == {
        "available": False,
        "reason_code": "REVISION_RECEIPT_PROVENANCE",
    }


async def test_revision_history_hides_foreign_company_and_rejects_non_owner(
    db_client: AsyncClient,
    db_session_maker: async_sessionmaker,
) -> None:
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    invoice = await _create_invoice(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )
    foreign_company_id = uuid.uuid4()
    async with db_session_maker() as session:
        await set_rls_company(session, uuid.UUID(seeds["company_id"]))
        await session.execute(
            text(
                "INSERT INTO company (id, name, base_currency) "
                "VALUES (:id, 'Foreign revision tenant', 'EUR')"
            ),
            {"id": foreign_company_id},
        )
        await session.execute(
            text('UPDATE "user" SET company_id = :id WHERE email = :email'),
            {"id": foreign_company_id, "email": "owner@example.com"},
        )
        await session.commit()

    foreign_read = await db_client.get(f"/api/v1/invoices/{invoice['id']}/revisions")
    assert foreign_read.status_code == 404
    foreign_save = await db_client.post(
        f"/api/v1/invoices/{invoice['id']}/revisions",
        json={
            "expected_updated_at": invoice["updated_at"],
            "changes": {"notes": "must stay hidden"},
            "reason": "Foreign invoice must be hidden",
            "confirm_not_delivered": True,
        },
    )
    assert foreign_save.status_code == 404

    async with db_session_maker() as session:
        await set_rls_company(session, foreign_company_id)
        await session.execute(
            text('UPDATE "user" SET role = :role WHERE email = :email'),
            {"role": "accountant", "email": "owner@example.com"},
        )
        await session.commit()
    non_owner = await db_client.get(f"/api/v1/invoices/{invoice['id']}/revisions")
    assert non_owner.status_code == 403


async def test_revision_save_missing_invoice_is_404(db_client: AsyncClient) -> None:
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    invoice = await _create_invoice(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )
    response = await db_client.post(
        f"/api/v1/invoices/{uuid.uuid4()}/revisions",
        json={
            "expected_updated_at": invoice["updated_at"],
            "changes": {"notes": "must not be written"},
            "reason": "Missing invoice must be a 404",
            "confirm_not_delivered": True,
        },
    )
    assert response.status_code == 404


async def test_revision_precision_is_normalized_before_preview_save_and_audit(
    db_client: AsyncClient,
) -> None:
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    created = await _create_invoice(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )
    invoice = (await db_client.get(f"/api/v1/invoices/{created['id']}" )).json()
    line = invoice["lines"][0]
    target_line = {
        "line_id": line["id"],
        "name": line["name"],
        "description": line["description"],
        "quantity": "1.2345",
        "unit_id": line["unit_id"],
        "unit_name": line["unit_name"],
        "unit_price": "1.2345",
        "discount": {"type": "PERCENTAGE", "value": "12.3456"},
        "vat_rate_id": line["vat_rate_id"],
    }
    changes = {"lines": [target_line]}
    preview = await db_client.post(
        f"/api/v1/invoices/{created['id']}/revisions/calculate",
        json={"expected_updated_at": invoice["updated_at"], "changes": changes},
    )
    assert preview.status_code == 200, preview.text
    assert Decimal(preview.json()["lines"][0]["quantity"]) == Decimal("1.235")
    save = await db_client.post(
        f"/api/v1/invoices/{created['id']}/revisions",
        json={
            "expected_updated_at": invoice["updated_at"],
            "changes": changes,
            "reason": "Normalize accepted values at persisted scale",
            "confirm_not_delivered": True,
        },
    )
    assert save.status_code == 200, save.text
    current = (await db_client.get(f"/api/v1/invoices/{created['id']}" )).json()
    history = (await db_client.get(f"/api/v1/invoices/{created['id']}/revisions")).json()
    after = history["items"][0]["after_snapshot"]
    line_after = current["lines"][0]
    basis_after = after["credit_basis"][0]
    assert Decimal(line_after["quantity"]) == Decimal("1.235")
    assert Decimal(line_after["unit_price"]) == Decimal("1.235")
    assert Decimal(line_after["discount_value"]) == Decimal("12.346")
    assert after["invoice"]["lines"][0]["quantity"] == line_after["quantity"]
    assert after["invoice"]["lines"][0]["unit_price"] == line_after["unit_price"]
    assert after["invoice"]["lines"][0]["discount_value"] == line_after["discount_value"]
    assert basis_after["quantity"] == line_after["quantity"]
    assert Decimal(preview.json()["total_incl_vat"]) == Decimal(current["total_incl_vat"])

    second = await db_client.post(
        f"/api/v1/invoices/{created['id']}/revisions",
        json={
            "expected_updated_at": current["updated_at"],
            "changes": {"notes": "A second revision"},
            "reason": "Check adjacent immutable snapshots",
            "confirm_not_delivered": True,
        },
    )
    assert second.status_code == 200, second.text
    updated_history = (await db_client.get(f"/api/v1/invoices/{created['id']}/revisions")).json()
    assert updated_history["items"][0]["before_snapshot"] == after

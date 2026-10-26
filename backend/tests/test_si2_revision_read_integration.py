"""SI2 Step 1 read-only history and detail eligibility integration checks."""

from __future__ import annotations

import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker
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
from jai.models.document import InvoicePartySnapshot
from jai.models.email_log import EmailLog
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

    async with db_session_maker() as session:
        await set_rls_company(session, foreign_company_id)
        await session.execute(
            text('UPDATE "user" SET role = :role WHERE email = :email'),
            {"role": "accountant", "email": "owner@example.com"},
        )
        await session.commit()
    non_owner = await db_client.get(f"/api/v1/invoices/{invoice['id']}/revisions")
    assert non_owner.status_code == 403

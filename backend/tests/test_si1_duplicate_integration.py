"""SI1 quote duplication: fresh business identity with current pricing."""

import asyncio
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_quote_crud_integration import (
    _create_customer,
    _full_auth,
    _quote_payload,
    _setup_company,
)

from jai.db import set_rls_company
from jai.models._enums import QuoteStatus
from jai.models.company import Company
from jai.models.quote import Quote
from jai.models.user import User

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("source_status", list(QuoteStatus))
async def test_duplicate_every_status_content_identity_and_independence(
    db_client: AsyncClient,
    db_session_maker: async_sessionmaker[AsyncSession],
    source_status: QuoteStatus,
) -> None:
    await _full_auth(db_client)
    seed = await _setup_company(db_client)
    customer = await _create_customer(db_client)
    rate = seed["rates"]["NL standard (21%)"]["id"]
    payload = _quote_payload(customer, rate)
    payload.update(
        {
            "reference_number": "reference",
            "notes": "notes",
            "warranty_text": "warranty",
            "terms_text": "terms",
            "bank_text": "bank",
            "payment_terms_text": "payment",
            "discount": {"type": "FIXED", "value": "0.01"},
            "lines": [
                {
                    "name": name,
                    "description": "Long description " * 30,
                    "quantity": "2.5",
                    "unit_name": "hour",
                    "unit_price": "12.345",
                    "vat_rate_id": rate,
                    "discount": {"type": "PERCENTAGE", "value": "3"},
                }
                for name in ["Second", "First", "First"]
            ],
        }
    )
    created = await db_client.post("/api/v1/quotes", json=payload)
    assert created.status_code == 201, created.text
    source = created.json()
    async with db_session_maker() as session:
        row = (
            await session.execute(select(Quote).where(Quote.id == uuid.UUID(source["id"])))
        ).scalar_one()
        row.status = source_status
        await session.commit()
    assert (
        await db_client.put(
            "/api/v1/settings/quote-default-valid-days", json={"default_valid_days": 17}
        )
    ).status_code == 200
    response = await db_client.post(f"/api/v1/quotes/{source['id']}/duplicate")
    assert response.status_code == 201, response.text
    duplicate = response.json()
    assert duplicate["id"] != source["id"]
    assert duplicate["quote_number"] != source["quote_number"]
    assert duplicate["sequence_number"] == source["sequence_number"] + 1
    assert duplicate["quote_date"] == date.today().isoformat()
    assert duplicate["valid_until"] == (date.today() + timedelta(days=17)).isoformat()
    assert duplicate["status"] == "DRAFT"
    assert duplicate["settlement_mode"] == "UNSET"
    assert duplicate["settlement_mode_locked_at"] is None
    assert duplicate["converted_invoice_id"] is None
    assert Decimal(duplicate["incoming_payment_total"]) == 0
    for field in [
        "customer_id",
        "currency",
        "tax_mode",
        "amounts_include_vat",
        "vat_treatment_id",
        "document_vat_rate_id",
        "reference_number",
        "notes",
        "warranty_text",
        "terms_text",
        "bank_text",
        "payment_terms_text",
        "discount_type",
        "discount_value",
    ]:
        assert duplicate[field] == source[field]
    for old, new in zip(source["lines"], duplicate["lines"], strict=True):
        assert old["id"] != new["id"]
        for field in [
            "sort_order",
            "name",
            "description",
            "quantity",
            "unit_name",
            "unit_price",
            "vat_rate_id",
            "discount_type",
            "discount_value",
        ]:
            assert old[field] == new[field]
    assert (await db_client.get(f"/api/v1/quotes/{duplicate['id']}/emails")).json()["items"] == []
    chain = (await db_client.get(f"/api/v1/quotes/{duplicate['id']}/document-chain")).json()
    assert chain["events"] == []
    assert chain["relations"] == []
    payload["notes"] = "duplicate only"
    assert (
        await db_client.put(f"/api/v1/quotes/{duplicate['id']}", json=payload)
    ).status_code == 200
    assert (await db_client.delete(f"/api/v1/quotes/{duplicate['id']}")).status_code == 204
    async with db_session_maker() as session:
        row = (
            await session.execute(select(Quote).where(Quote.id == uuid.UUID(source["id"])))
        ).scalar_one()
        assert row.status == source_status
        assert row.notes == "notes"


async def test_duplicate_converted_current_vat_and_concurrent_numbers(
    db_client: AsyncClient,
) -> None:
    await _full_auth(db_client)
    seed = await _setup_company(db_client)
    customer = await _create_customer(db_client)
    rate = seed["rates"]["NL standard (21%)"]
    payload = _quote_payload(customer, rate["id"])
    source = (await db_client.post("/api/v1/quotes", json=payload)).json()
    assert (
        await db_client.post(f"/api/v1/quotes/{source['id']}/status", json={"status": "SENT"})
    ).status_code == 200
    converted = await db_client.post(f"/api/v1/quotes/{source['id']}/convert")
    assert converted.status_code == 201, converted.text
    update = await db_client.put(
        f"/api/v1/vat-rates/{rate['id']}",
        json={"label": rate["label"], "percent": "23", "active": True},
    )
    assert update.status_code == 200, update.text
    responses = await asyncio.gather(
        *[db_client.post(f"/api/v1/quotes/{source['id']}/duplicate") for _ in range(3)]
    )
    assert [r.status_code for r in responses] == [201, 201, 201], [r.text for r in responses]
    duplicates = [r.json() for r in responses]
    assert len({q["quote_number"] for q in duplicates}) == 3
    for duplicate in duplicates:
        assert duplicate["converted_invoice_id"] is None
        assert duplicate["settlement_mode"] == "UNSET"
        assert Decimal(duplicate["vat_total"]) == Decimal("23")
        assert Decimal(duplicate["lines"][0]["unit_price"]) == Decimal("100")
    original = (await db_client.get(f"/api/v1/quotes/{source['id']}")).json()
    assert original["converted_invoice_id"] == converted.json()["id"]
    assert Decimal(original["vat_total"]) == Decimal("21")
    assert (await db_client.post(f"/api/v1/quotes/{uuid.uuid4()}/duplicate")).status_code == 404


async def test_duplicate_auth_and_company_boundary(
    db_client: AsyncClient, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    assert (await db_client.post(f"/api/v1/quotes/{uuid.uuid4()}/duplicate")).status_code == 401
    await _full_auth(db_client)
    seed = await _setup_company(db_client)
    customer = await _create_customer(db_client)
    source = (
        await db_client.post(
            "/api/v1/quotes",
            json=_quote_payload(customer, seed["rates"]["NL standard (21%)"]["id"]),
        )
    ).json()
    async with db_session_maker() as session:
        user = (await session.execute(select(User))).scalar_one()
        user.role = "viewer"
        await session.commit()
    assert (await db_client.post(f"/api/v1/quotes/{source['id']}/duplicate")).status_code == 403
    async with db_session_maker() as session:
        user = (await session.execute(select(User))).scalar_one()
        company = Company(name="Other", base_currency="EUR", country_code="NL")
        session.add(company)
        await session.flush()
        user.role = "owner"
        user.company_id = company.id
        await session.commit()
    assert (await db_client.post(f"/api/v1/quotes/{source['id']}/duplicate")).status_code == 404


@pytest.mark.parametrize("tax_mode", ["LINE", "DOCUMENT"])
async def test_catalog_price_preserved_and_invalid_treatment_rejected(
    db_client: AsyncClient, tax_mode: str
) -> None:
    await _full_auth(db_client)
    seed = await _setup_company(db_client)
    customer = await _create_customer(db_client)
    rate = seed["rates"]["NL standard (21%)"]["id"]
    product_body = {"name": "Catalog", "purchase_cost_excl_vat": "10", "margin_rate": "0.2"}
    product_response = await db_client.post("/api/v1/products", json=product_body)
    assert product_response.status_code == 201, product_response.text
    product = product_response.json()
    payload = _quote_payload(customer, rate, tax_mode=tax_mode)
    if tax_mode == "DOCUMENT":
        payload["document_vat_rate_id"] = rate
    payload["amounts_include_vat"] = True
    payload["lines"][0].update({"product_id": product["id"], "unit_price": "17.123"})
    created = await db_client.post("/api/v1/quotes", json=payload)
    assert created.status_code == 201, created.text
    source = created.json()
    product_body["purchase_cost_excl_vat"] = "500"
    assert (
        await db_client.put(f"/api/v1/products/{product['id']}", json=product_body)
    ).status_code == 200
    response = await db_client.post(f"/api/v1/quotes/{source['id']}/duplicate")
    assert response.status_code == 201, response.text
    duplicate = response.json()
    assert duplicate["tax_mode"] == tax_mode
    assert duplicate["amounts_include_vat"] is True
    assert Decimal(duplicate["lines"][0]["unit_price"]) == Decimal("17.123")
    assert duplicate["lines"][0]["product_id"] == product["id"]
    treatment = next(
        t for t in seed["treatments"].values() if t["id"] == source["vat_treatment_id"]
    )
    treatment["active"] = False
    response = await db_client.put(f"/api/v1/vat-treatments/{treatment['id']}", json=treatment)
    assert response.status_code == 200, response.text
    failed = await db_client.post(f"/api/v1/quotes/{source['id']}/duplicate")
    assert failed.status_code == 422, failed.text
    assert "inactive" in failed.json()["detail"]


async def test_duplicate_clears_real_payment_and_email_history(
    db_client: AsyncClient, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    from jai.models._enums import EmailRelatedType, EmailStatus
    from jai.models.email_log import EmailLog

    await _full_auth(db_client)
    seed = await _setup_company(db_client)
    customer = await _create_customer(db_client)
    rate = seed["rates"]["NL standard (21%)"]["id"]
    source = (await db_client.post("/api/v1/quotes", json=_quote_payload(customer, rate))).json()
    for state in ["SENT", "ACCEPTED"]:
        response = await db_client.post(
            f"/api/v1/quotes/{source['id']}/status", json={"status": state}
        )
        assert response.status_code == 200, response.text
    payment = await db_client.post(
        f"/api/v1/quotes/{source['id']}/payments",
        json={"payment_date": date.today().isoformat(), "amount": "10"},
    )
    assert payment.status_code == 201, payment.text
    async with db_session_maker() as session:
        await set_rls_company(session, uuid.UUID(source["company_id"]))
        session.add(
            EmailLog(
                company_id=uuid.UUID(source["company_id"]),
                related_type=EmailRelatedType.QUOTE,
                related_id=uuid.UUID(source["id"]),
                to_email="test@example.com",
                subject="History",
                body_snapshot="Original email",
                status=EmailStatus.SENT,
            )
        )
        await session.commit()
    result = await db_client.post(f"/api/v1/quotes/{source['id']}/duplicate")
    assert result.status_code == 201, result.text
    duplicate = result.json()
    assert duplicate["settlement_mode"] == "UNSET"
    assert duplicate["settlement_mode_locked_at"] is None
    assert Decimal(duplicate["chain_totals"]["incoming_payment_total"]) == 0
    assert (await db_client.get(f"/api/v1/quotes/{duplicate['id']}/payments")).json()["items"] == []
    assert (await db_client.get(f"/api/v1/quotes/{duplicate['id']}/emails")).json()["items"] == []
    source_read = (await db_client.get(f"/api/v1/quotes/{source['id']}")).json()
    assert source_read["settlement_mode"] == "RECEIPT_ONLY"
    assert Decimal(source_read["incoming_payment_total"]) == 10
    assert len((await db_client.get(f"/api/v1/quotes/{source['id']}/emails")).json()["items"]) == 1


async def test_duplicate_waits_for_saved_source_snapshot(
    db_client: AsyncClient, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    from sqlalchemy.orm import selectinload

    await _full_auth(db_client)
    seed = await _setup_company(db_client)
    customer = await _create_customer(db_client)
    payload = _quote_payload(customer, seed["rates"]["NL standard (21%)"]["id"])
    source = (await db_client.post("/api/v1/quotes", json=payload)).json()
    async with db_session_maker() as session:
        row = (
            await session.execute(
                select(Quote)
                .where(Quote.id == uuid.UUID(source["id"]))
                .options(selectinload(Quote.lines))
                .with_for_update()
            )
        ).scalar_one()
        row.notes = "Committed content"
        row.lines[0].name = "Committed line"
        await session.flush()
        copying = asyncio.create_task(db_client.post(f"/api/v1/quotes/{source['id']}/duplicate"))
        try:
            await asyncio.sleep(0.05)
            assert not copying.done()
            await session.commit()
            result = await copying
        finally:
            if not copying.done():
                copying.cancel()
    assert result.status_code == 201, result.text
    assert result.json()["notes"] == "Committed content"
    assert result.json()["lines"][0]["name"] == "Committed line"

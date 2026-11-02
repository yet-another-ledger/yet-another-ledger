"""PostgreSQL integration coverage for M11.5 quote deposits and settlement."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pyotp
import pypdfium2
import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jai.auth.deps import current_mfa_user
from jai.main import app
from jai.models.invoice import Invoice
from jai.models.payment import Payment, PaymentTax
from jai.models.quote import Quote
from jai.schemas.setting import SmtpSettings
from jai.services import email as email_service
from jai.services import invoice as invoice_service
from jai.services import pdf as pdf_service
from jai.services.payment import delete_payment


async def _full_auth(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "owner@example.com", "password": "testpassword1"},
    )
    assert response.status_code == 201
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": "testpassword1"},
    )
    assert response.status_code == 200
    response = await client.post("/api/v1/auth/mfa/setup")
    assert response.status_code == 200
    code = pyotp.TOTP(response.json()["secret"]).now()
    response = await client.post("/api/v1/auth/mfa/verify", json={"code": code})
    assert response.status_code == 204


async def _setup_company(client: AsyncClient) -> dict:
    response = await client.put(
        "/api/v1/company",
        json={"name": "Deposit Test Co", "base_currency": "EUR", "country_code": "NL"},
    )
    assert response.status_code == 200
    rates_response = await client.get("/api/v1/vat-rates")
    rates = {item["label"]: item for item in rates_response.json()["items"]}
    treatments_response = await client.get("/api/v1/vat-treatments?side=SALES")
    treatments = {item["code"]: item for item in treatments_response.json()["items"]}
    return {"company_id": response.json()["id"], "rates": rates, "treatments": treatments}


async def _create_customer(
    client: AsyncClient,
    *,
    name: str = "Deposit Customer",
    country_code: str = "NL",
    email: str | None = None,
    locale: str | None = None,
) -> str:
    payload: dict[str, object] = {
        "name": name,
        "addresses": [
            {
                "type": "BILLING",
                "country_code": country_code,
                "city": "Amsterdam",
            }
        ],
    }
    if email is not None:
        payload["email"] = email
    if locale is not None:
        payload["locale"] = locale
    response = await client.post(
        "/api/v1/customers",
        json=payload,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _create_quote(
    client: AsyncClient,
    customer_id: str,
    rate_21: str,
    *,
    rate_9: str | None = None,
    treatment_id: str | None = None,
    unit_price: str = "100.000",
) -> dict:
    lines = [
        {
            "name": "Standard service",
            "quantity": "1",
            "unit_price": unit_price,
            "vat_rate_id": rate_21,
        }
    ]
    if rate_9 is not None:
        lines.append(
            {
                "name": "Reduced service",
                "quantity": "1",
                "unit_price": "100.000",
                "vat_rate_id": rate_9,
            }
        )
    payload: dict = {
        "customer_id": customer_id,
        "quote_date": "2026-01-10",
        "tax_mode": "LINE",
        "amounts_include_vat": False,
        "lines": lines,
    }
    if treatment_id is not None:
        payload["vat_treatment_id"] = treatment_id
    response = await client.post("/api/v1/quotes", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def _accept_quote(client: AsyncClient, quote_id: str) -> dict:
    response = await client.post(f"/api/v1/quotes/{quote_id}/status", json={"status": "SENT"})
    assert response.status_code == 200, response.text
    response = await client.post(f"/api/v1/quotes/{quote_id}/status", json={"status": "ACCEPTED"})
    assert response.status_code == 200, response.text
    return response.json()


async def _record(
    client: AsyncClient,
    quote_id: str,
    amount: str,
    payment_date: str,
    *,
    reference: str | None = None,
) -> dict:
    response = await client.post(
        f"/api/v1/quotes/{quote_id}/payments",
        json={
            "payment_date": payment_date,
            "amount": amount,
            "reference": reference,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_invoice(client: AsyncClient, customer_id: str, rate_id: str) -> dict:
    response = await client.post(
        "/api/v1/invoices",
        json={
            "customer_id": customer_id,
            "invoice_date": "2026-01-10",
            "tax_mode": "LINE",
            "amounts_include_vat": False,
            "lines": [
                {
                    "name": "Invoice-only service",
                    "quantity": "1",
                    "unit_price": "100.000",
                    "vat_rate_id": rate_id,
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    invoice = response.json()
    issued = await client.post(f"/api/v1/invoices/{invoice['id']}/status", json={"status": "SENT"})
    assert issued.status_code == 200, issued.text
    return issued.json()


def _smtp() -> SmtpSettings:
    return SmtpSettings(
        host="smtp.example.com",
        port=587,
        username="smtp-user",
        password="smtp-secret",
        from_email="sender@example.com",
        from_name="Receipt Test",
        use_tls=True,
        use_ssl=False,
    )


def _tax_amounts(item: dict) -> tuple[Decimal, Decimal, Decimal]:
    """Return one payment's authoritative taxable/VAT/gross tax totals."""
    rows = item["tax_breakdown"]
    return tuple(
        sum((Decimal(row[key]) for row in rows), Decimal("0"))
        for key in ("taxable_amount", "vat_amount", "gross_amount")
    )  # type: ignore[return-value]


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract customer-visible text from every page of a rendered PDF."""
    document = pypdfium2.PdfDocument(pdf_bytes)
    try:
        return "\n".join(
            document[index].get_textpage().get_text_range() for index in range(len(document))
        )
    finally:
        document.close()


def _tax_by_rate(item: dict) -> dict[Decimal, tuple[Decimal, Decimal, Decimal]]:
    """Return one payment's immutable VAT snapshot grouped by rate."""
    return {
        Decimal(row["vat_rate_percent"]): (
            Decimal(row["taxable_amount"]),
            Decimal(row["vat_amount"]),
            Decimal(row["gross_amount"]),
        )
        for row in item["tax_breakdown"]
    }


def _tax_totals(items: list[dict]) -> dict[Decimal, tuple[Decimal, Decimal, Decimal]]:
    """Sum persisted payment-tax snapshots by VAT rate."""
    totals: dict[Decimal, tuple[Decimal, Decimal, Decimal]] = {}
    for item in items:
        for rate, (taxable, vat, gross) in _tax_by_rate(item).items():
            existing = totals.get(rate, (Decimal("0"), Decimal("0"), Decimal("0")))
            totals[rate] = (
                existing[0] + taxable,
                existing[1] + vat,
                existing[2] + gross,
            )
    return totals


async def _payment_tax_ids(
    session_maker: async_sessionmaker[AsyncSession], payment_ids: list[str]
) -> dict[str, set[str]]:
    """Read child IDs to prove a mutation replaces every snapshot row."""
    async with session_maker() as session:
        result = await session.execute(
            select(PaymentTax.payment_id, PaymentTax.id).where(
                PaymentTax.payment_id.in_([uuid.UUID(payment_id) for payment_id in payment_ids])
            )
        )
    ids: dict[str, set[str]] = {payment_id: set() for payment_id in payment_ids}
    for payment_id, tax_id in result.all():
        ids[str(payment_id)].add(str(tax_id))
    return ids


@pytest.mark.integration
class TestQuotePaymentCrud:
    async def test_mixed_rate_deposits_are_cent_exact_and_reallocate(
        self, db_client: AsyncClient
    ) -> None:
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]
        rate_9 = seeds["rates"]["NL reduced (9%)"]["id"]
        quote = await _create_quote(db_client, customer_id, rate_21, rate_9=rate_9)
        await _accept_quote(db_client, quote["id"])

        first = await _record(db_client, quote["id"], "46.00", "2026-01-15")
        second = await _record(db_client, quote["id"], "115.00", "2026-02-15")
        final = await _record(db_client, quote["id"], "69.00", "2026-03-15")

        assert first["paid_total"] == "46.000"
        assert second["remaining_amount"] == "69.000"
        assert final["paid_total"] == final["total_incl_vat"] == "230.000"
        assert final["remaining_amount"] == "0.000"
        assert [item["payment_date"] for item in final["items"]] == [
            "2026-01-15",
            "2026-02-15",
            "2026-03-15",
        ]
        for item in final["items"]:
            allocated = sum(
                (Decimal(row["gross_amount"]) for row in item["tax_breakdown"]),
                Decimal("0"),
            )
            assert allocated == Decimal(item["amount"])

        totals_by_rate: dict[Decimal, tuple[Decimal, Decimal]] = {}
        for item in final["items"]:
            for row in item["tax_breakdown"]:
                rate = Decimal(row["vat_rate_percent"])
                taxable, vat = totals_by_rate.get(rate, (Decimal("0"), Decimal("0")))
                totals_by_rate[rate] = (
                    taxable + Decimal(row["taxable_amount"]),
                    vat + Decimal(row["vat_amount"]),
                )
        assert totals_by_rate[Decimal("21.000")] == (
            Decimal("100.000"),
            Decimal("21.000"),
        )
        assert totals_by_rate[Decimal("9.000")] == (
            Decimal("100.000"),
            Decimal("9.000"),
        )

    async def test_edit_delete_and_global_quote_search(self, db_client: AsyncClient) -> None:
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]
        quote = await _create_quote(db_client, customer_id, rate_21)
        await _accept_quote(db_client, quote["id"])
        aggregate = await _record(db_client, quote["id"], "40.00", "2026-01-15", reference="DEP-A")
        payment_id = aggregate["items"][0]["id"]

        response = await db_client.put(
            f"/api/v1/payments/{payment_id}",
            json={"payment_date": "2026-01-20", "amount": "50.00"},
        )
        assert response.status_code == 200, response.text
        mutation = response.json()
        assert mutation["invoice"] is None
        assert mutation["quote"]["paid_total"] == "50.000"
        assert mutation["quote"]["items"][0]["tax_breakdown"]

        response = await db_client.get(f"/api/v1/payments?q={quote['quote_number']}")
        assert response.status_code == 200, response.text
        item = response.json()["items"][0]
        assert item["origin_type"] == "QUOTE"
        assert item["invoice_id"] is None
        assert item["quote_id"] == quote["id"]

        response = await db_client.delete(f"/api/v1/payments/{payment_id}")
        assert response.status_code == 200, response.text
        assert response.json()["deleted"] is True
        assert response.json()["quote"]["items"] == []

    async def test_mutations_reallocate_every_remaining_payment(
        self,
        db_client: AsyncClient,
        db_session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Changing order/amounts rebuilds all quote payment-tax snapshots."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]
        rate_9 = seeds["rates"]["NL reduced (9%)"]["id"]
        quote = await _create_quote(db_client, customer_id, rate_21, rate_9=rate_9)
        await _accept_quote(db_client, quote["id"])

        first = await _record(db_client, quote["id"], "46.00", "2026-01-15")
        second = await _record(db_client, quote["id"], "115.00", "2026-02-15")
        third = await _record(db_client, quote["id"], "68.00", "2026-03-15")
        first_id = first["items"][0]["id"]
        second_id = second["items"][1]["id"]
        third_id = third["items"][2]["id"]
        pre_edit_ids = await _payment_tax_ids(db_session_maker, [first_id, second_id, third_id])

        edited = await db_client.put(
            f"/api/v1/payments/{first_id}",
            json={"payment_date": "2026-03-20", "amount": "47.00"},
        )
        assert edited.status_code == 200, edited.text
        items = edited.json()["quote"]["items"]
        assert [item["id"] for item in items] == [second_id, third_id, first_id]
        assert [Decimal(item["amount"]) for item in items] == [
            Decimal("115.00"),
            Decimal("68.00"),
            Decimal("47.00"),
        ]
        expected_after_edit = {
            second_id: {
                Decimal("9.000"): (Decimal("50.00"), Decimal("4.50"), Decimal("54.50")),
                Decimal("21.000"): (Decimal("50.00"), Decimal("10.50"), Decimal("60.50")),
            },
            third_id: {
                Decimal("9.000"): (Decimal("29.57"), Decimal("2.66"), Decimal("32.23")),
                Decimal("21.000"): (Decimal("29.56"), Decimal("6.21"), Decimal("35.77")),
            },
            first_id: {
                Decimal("9.000"): (Decimal("20.43"), Decimal("1.84"), Decimal("22.27")),
                Decimal("21.000"): (Decimal("20.44"), Decimal("4.29"), Decimal("24.73")),
            },
        }
        assert {item["id"]: _tax_by_rate(item) for item in items} == expected_after_edit
        assert _tax_totals(items) == {
            Decimal("9.000"): (Decimal("100.00"), Decimal("9.00"), Decimal("109.00")),
            Decimal("21.000"): (Decimal("100.00"), Decimal("21.00"), Decimal("121.00")),
        }
        for item in items:
            taxable, vat, gross = _tax_amounts(item)
            assert taxable + vat == gross == Decimal(item["amount"])
        post_edit_ids = await _payment_tax_ids(db_session_maker, [first_id, second_id, third_id])
        for payment_id in (first_id, second_id, third_id):
            assert post_edit_ids[payment_id]
            assert post_edit_ids[payment_id].isdisjoint(pre_edit_ids[payment_id])

        deleted = await db_client.delete(f"/api/v1/payments/{second_id}")
        assert deleted.status_code == 200, deleted.text
        remaining = deleted.json()["quote"]["items"]
        assert [item["id"] for item in remaining] == [third_id, first_id]
        assert [item["amount"] for item in remaining] == ["68.000", "47.000"]
        assert {item["id"]: _tax_by_rate(item) for item in remaining} == {
            third_id: expected_after_edit[third_id],
            first_id: expected_after_edit[first_id],
        }
        assert _tax_totals(remaining) == {
            Decimal("9.000"): (Decimal("50.00"), Decimal("4.50"), Decimal("54.50")),
            Decimal("21.000"): (Decimal("50.00"), Decimal("10.50"), Decimal("60.50")),
        }
        for item in remaining:
            taxable, vat, gross = _tax_amounts(item)
            assert taxable + vat == gross == Decimal(item["amount"])
        post_delete_ids = await _payment_tax_ids(db_session_maker, [first_id, third_id])
        for payment_id in (first_id, third_id):
            assert post_delete_ids[payment_id]
            assert post_delete_ids[payment_id].isdisjoint(post_edit_ids[payment_id])

    async def test_quote_payment_guards(self, db_client: AsyncClient) -> None:
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]
        draft = await _create_quote(db_client, customer_id, rate_21)

        response = await db_client.post(
            f"/api/v1/quotes/{draft['id']}/payments",
            json={"payment_date": "2026-01-15", "amount": "10.00"},
        )
        assert response.status_code == 422

        await _accept_quote(db_client, draft["id"])
        response = await db_client.post(
            f"/api/v1/quotes/{draft['id']}/payments",
            json={"payment_date": "2026-01-15", "amount": "121.01"},
        )
        assert response.status_code == 422

        foreign_customer_id = await _create_customer(
            db_client, name="Belgian Customer", country_code="BE"
        )
        foreign_quote = await _create_quote(
            db_client,
            foreign_customer_id,
            rate_21,
            treatment_id=seeds["treatments"]["EU_B2C"]["id"],
        )
        await _accept_quote(db_client, foreign_quote["id"])
        response = await db_client.post(
            f"/api/v1/quotes/{foreign_quote['id']}/payments",
            json={"payment_date": "2026-01-15", "amount": "10.00"},
        )
        assert response.status_code == 422
        assert "NL_DOMESTIC" in response.json()["detail"]

    async def test_all_non_accepted_or_converted_quote_guards(self, db_client: AsyncClient) -> None:
        """Only an unconverted ACCEPTED quote may receive a deposit."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]

        async def assert_rejected(quote_id: str) -> None:
            response = await db_client.post(
                f"/api/v1/quotes/{quote_id}/payments",
                json={"payment_date": "2026-01-15", "amount": "10.00"},
            )
            assert response.status_code in (409, 422), response.text

        draft = await _create_quote(db_client, customer_id, rate_21)
        await assert_rejected(draft["id"])

        sent = await _create_quote(db_client, customer_id, rate_21)
        response = await db_client.post(
            f"/api/v1/quotes/{sent['id']}/status", json={"status": "SENT"}
        )
        assert response.status_code == 200
        await assert_rejected(sent["id"])

        rejected = await _create_quote(db_client, customer_id, rate_21)
        for status in ("SENT", "REJECTED"):
            response = await db_client.post(
                f"/api/v1/quotes/{rejected['id']}/status", json={"status": status}
            )
            assert response.status_code == 200
        await assert_rejected(rejected["id"])

        expired = await _create_quote(db_client, customer_id, rate_21)
        response = await db_client.put(
            f"/api/v1/quotes/{expired['id']}",
            json={
                "customer_id": customer_id,
                "quote_date": "2026-01-10",
                "valid_until": "2020-01-01",
                "tax_mode": "LINE",
                "amounts_include_vat": False,
                "lines": [
                    {
                        "name": "Expired",
                        "quantity": "1",
                        "unit_price": "100.000",
                        "vat_rate_id": rate_21,
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text
        response = await db_client.post(
            f"/api/v1/quotes/{expired['id']}/status", json={"status": "SENT"}
        )
        assert response.status_code == 200
        expired_read = await db_client.get(f"/api/v1/quotes/{expired['id']}")
        assert expired_read.status_code == 200, expired_read.text
        assert expired_read.json()["status"] == "EXPIRED"
        await assert_rejected(expired["id"])

        converted = await _create_quote(db_client, customer_id, rate_21)
        await _accept_quote(db_client, converted["id"])
        response = await db_client.post(f"/api/v1/quotes/{converted['id']}/convert")
        assert response.status_code == 201, response.text
        await assert_rejected(converted["id"])

    async def test_quote_payment_method_name_is_frozen_across_mutation(
        self, db_client: AsyncClient
    ) -> None:
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]
        quote = await _create_quote(db_client, customer_id, rate_21)
        await _accept_quote(db_client, quote["id"])
        method = await db_client.post("/api/v1/payment-methods", json={"name": "Original bank"})
        assert method.status_code == 201, method.text
        method_id = method.json()["id"]
        aggregate = await db_client.post(
            f"/api/v1/quotes/{quote['id']}/payments",
            json={"payment_date": "2026-01-15", "amount": "30.00", "payment_method_id": method_id},
        )
        assert aggregate.status_code == 201, aggregate.text
        payment_id = aggregate.json()["items"][0]["id"]
        assert aggregate.json()["items"][0]["payment_method_id"] == method_id
        assert aggregate.json()["items"][0]["payment_method_name"] == "Original bank"
        renamed = await db_client.put(
            f"/api/v1/payment-methods/{method_id}", json={"name": "Renamed bank"}
        )
        assert renamed.status_code == 200, renamed.text
        frozen = await db_client.get(f"/api/v1/payments/{payment_id}")
        assert frozen.status_code == 200, frozen.text
        assert frozen.json()["payment_method_id"] == method_id
        assert frozen.json()["payment_method_name"] == "Original bank"
        updated = await db_client.put(
            f"/api/v1/payments/{payment_id}",
            json={"payment_date": "2026-01-16", "amount": "30.00", "payment_method_id": method_id},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["quote"]["items"][0]["payment_method_name"] == "Renamed bank"
        deleted = await db_client.delete(f"/api/v1/payment-methods/{method_id}")
        assert deleted.status_code == 204, deleted.text
        read = await db_client.get(f"/api/v1/payments/{payment_id}")
        assert read.status_code == 200, read.text
        assert read.json()["payment_method_id"] is None
        assert read.json()["payment_method_name"] == "Renamed bank"

    async def test_quote_payment_access_is_owner_only_and_company_scoped(
        self, db_client: AsyncClient
    ) -> None:
        """Every quote-payment route injects both owner and company context."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]
        quote = await _create_quote(db_client, customer_id, rate_21)
        await _accept_quote(db_client, quote["id"])
        aggregate = await _record(db_client, quote["id"], "30.00", "2026-01-15")
        payment_id = aggregate["items"][0]["id"]

        foreign_owner = SimpleNamespace(id=uuid.uuid4(), company_id=uuid.uuid4(), role="owner")
        app.dependency_overrides[current_mfa_user] = lambda: foreign_owner
        try:
            for method, url, payload in (
                (
                    "post",
                    f"/api/v1/quotes/{quote['id']}/payments",
                    {"payment_date": "2026-01-16", "amount": "1.00"},
                ),
                ("get", f"/api/v1/quotes/{quote['id']}/payments", None),
                ("get", f"/api/v1/payments/{payment_id}", None),
                (
                    "put",
                    f"/api/v1/payments/{payment_id}",
                    {"payment_date": "2026-01-16", "amount": "30.00"},
                ),
                ("delete", f"/api/v1/payments/{payment_id}", None),
                (
                    "post",
                    f"/api/v1/payments/{payment_id}/send-receipt",
                    {"to": "customer@example.com"},
                ),
            ):
                request = getattr(db_client, method)
                response = (
                    await request(url, json=payload) if payload is not None else await request(url)
                )
                assert response.status_code == 404, response.text
            listed = await db_client.get("/api/v1/payments")
            assert listed.status_code == 200, listed.text
            assert listed.json() == {"items": [], "total": 0}

            app.dependency_overrides[current_mfa_user] = lambda: SimpleNamespace(
                id=uuid.uuid4(), company_id=uuid.uuid4(), role="member"
            )
            response = await db_client.get("/api/v1/payments")
            assert response.status_code == 403, response.text
            response = await db_client.post(
                f"/api/v1/quotes/{quote['id']}/payments",
                json={"payment_date": "2026-01-16", "amount": "1.00"},
            )
            assert response.status_code == 403, response.text
            for method, url, payload in (
                ("get", f"/api/v1/quotes/{quote['id']}/payments", None),
                ("get", "/api/v1/payments", None),
                ("get", f"/api/v1/payments/{payment_id}", None),
                (
                    "put",
                    f"/api/v1/payments/{payment_id}",
                    {"payment_date": "2026-01-16", "amount": "30.00"},
                ),
                ("delete", f"/api/v1/payments/{payment_id}", None),
                (
                    "post",
                    f"/api/v1/payments/{payment_id}/send-receipt",
                    {"to": "customer@example.com"},
                ),
            ):
                request = getattr(db_client, method)
                response = (
                    await request(url, json=payload) if payload is not None else await request(url)
                )
                assert response.status_code == 403, response.text
        finally:
            app.dependency_overrides.pop(current_mfa_user, None)

    async def test_global_list_mixed_origins_filters_pagination_and_tiebreak(
        self,
        db_client: AsyncClient,
        db_session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Quote-only, transferred and invoice-only rows share one stable list."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]
        quote_customer = await _create_customer(db_client, name="Quote Needle")
        transferred_customer = await _create_customer(db_client, name="Transfer Needle")
        invoice_customer = await _create_customer(db_client, name="Invoice Needle")
        method = await db_client.post("/api/v1/payment-methods", json={"name": "List bank"})
        assert method.status_code == 201, method.text
        method_id = method.json()["id"]

        quote_only = await _create_quote(db_client, quote_customer, rate_21)
        await _accept_quote(db_client, quote_only["id"])
        quote_payment = await db_client.post(
            f"/api/v1/quotes/{quote_only['id']}/payments",
            json={"payment_date": "2026-01-01", "amount": "20.00", "payment_method_id": method_id},
        )
        assert quote_payment.status_code == 201, quote_payment.text
        quote_payment_id = quote_payment.json()["items"][0]["id"]
        tie_quote_payment = await db_client.post(
            f"/api/v1/quotes/{quote_only['id']}/payments",
            json={"payment_date": "2026-02-01", "amount": "10.00", "payment_method_id": method_id},
        )
        assert tie_quote_payment.status_code == 201, tie_quote_payment.text
        tie_quote_payment_id = tie_quote_payment.json()["items"][-1]["id"]

        transfer_quote = await _create_quote(db_client, transferred_customer, rate_21)
        await _accept_quote(db_client, transfer_quote["id"])
        transferred_payment = await db_client.post(
            f"/api/v1/quotes/{transfer_quote['id']}/payments",
            json={"payment_date": "2026-03-01", "amount": "30.00", "payment_method_id": method_id},
        )
        assert transferred_payment.status_code == 201, transferred_payment.text
        transferred_payment_id = transferred_payment.json()["items"][0]["id"]
        converted = await db_client.post(f"/api/v1/quotes/{transfer_quote['id']}/convert")
        assert converted.status_code == 201, converted.text
        issued = await db_client.post(
            f"/api/v1/invoices/{converted.json()['id']}/status", json={"status": "SENT"}
        )
        assert issued.status_code == 200, issued.text

        invoice = await _create_invoice(db_client, invoice_customer, rate_21)
        invoice_payment = await db_client.post(
            f"/api/v1/invoices/{invoice['id']}/payments",
            json={"payment_date": "2026-02-01", "amount": "40.00", "payment_method_id": method_id},
        )
        assert invoice_payment.status_code == 201, invoice_payment.text
        invoice_payment_id = invoice_payment.json()["items"][0]["id"]

        async with db_session_maker() as session:
            await session.execute(
                update(Payment)
                .where(
                    Payment.id.in_(
                        [
                            quote_payment_id,
                            tie_quote_payment_id,
                            transferred_payment_id,
                            invoice_payment_id,
                        ]
                    )
                )
                .values(
                    created_at=datetime(2026, 2, 2, tzinfo=UTC),
                )
            )
            await session.execute(
                update(Payment)
                .where(Payment.id == uuid.UUID(quote_payment_id))
                .values(created_at=datetime(2026, 3, 1, tzinfo=UTC))
            )
            await session.execute(
                update(Payment)
                .where(Payment.id == uuid.UUID(transferred_payment_id))
                .values(created_at=datetime(2026, 1, 1, tzinfo=UTC))
            )
            await session.commit()

        tied_ids = sorted([tie_quote_payment_id, invoice_payment_id], reverse=True)
        expected_by_payment_date = [transferred_payment_id] + tied_ids + [quote_payment_id]
        expected_by_created_at = [quote_payment_id] + tied_ids + [transferred_payment_id]
        page_one = await db_client.get("/api/v1/payments?limit=2&offset=0")
        assert page_one.status_code == 200, page_one.text
        assert page_one.json()["total"] == 4
        assert [item["id"] for item in page_one.json()["items"]] == expected_by_payment_date[:2]
        page_two = await db_client.get("/api/v1/payments?limit=2&offset=2")
        assert page_two.status_code == 200, page_two.text
        assert page_two.json()["total"] == 4
        assert [item["id"] for item in page_two.json()["items"]] == expected_by_payment_date[2:]
        assert {item["id"] for item in page_one.json()["items"] + page_two.json()["items"]} == set(
            expected_by_payment_date
        )
        created = await db_client.get("/api/v1/payments?sort_by=created_at")
        assert created.status_code == 200, created.text
        assert [item["id"] for item in created.json()["items"]] == expected_by_created_at

        for search, expected_ids in (
            (quote_only["quote_number"], {quote_payment_id, tie_quote_payment_id}),
            (issued.json()["invoice_number"], {transferred_payment_id}),
            ("Invoice Needle", {invoice_payment_id}),
        ):
            response = await db_client.get("/api/v1/payments", params={"q": search})
            assert response.status_code == 200, response.text
            assert response.json()["total"] == len(expected_ids)
            assert {item["id"] for item in response.json()["items"]} == expected_ids

        for customer_id, expected_ids in (
            (quote_customer, [tie_quote_payment_id, quote_payment_id]),
            (transferred_customer, [transferred_payment_id]),
            (invoice_customer, [invoice_payment_id]),
        ):
            by_customer = await db_client.get(
                "/api/v1/payments", params={"customer_id": customer_id}
            )
            assert by_customer.status_code == 200, by_customer.text
            assert [item["id"] for item in by_customer.json()["items"]] == expected_ids
        by_method = await db_client.get("/api/v1/payments", params={"payment_method_id": method_id})
        assert by_method.status_code == 200, by_method.text
        assert [item["id"] for item in by_method.json()["items"]] == expected_by_payment_date
        by_date = await db_client.get(
            "/api/v1/payments", params={"date_from": "2026-02-01", "date_to": "2026-02-01"}
        )
        assert by_date.status_code == 200, by_date.text
        assert by_date.json()["total"] == 2
        assert [item["id"] for item in by_date.json()["items"]] == tied_ids

    async def test_concurrent_deposits_cannot_jointly_overpay(self, db_client: AsyncClient) -> None:
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]
        quote = await _create_quote(db_client, customer_id, rate_21)
        await _accept_quote(db_client, quote["id"])

        async def pay(reference: str):
            return await db_client.post(
                f"/api/v1/quotes/{quote['id']}/payments",
                json={
                    "payment_date": "2026-01-15",
                    "amount": "70.00",
                    "reference": reference,
                },
            )

        responses = await asyncio.gather(pay("CONCURRENT-A"), pay("CONCURRENT-B"))
        assert sorted(response.status_code for response in responses) == [201, 422]
        aggregate = await db_client.get(f"/api/v1/quotes/{quote['id']}/payments")
        assert aggregate.json()["paid_total"] == "70.000"
        assert len(aggregate.json()["items"]) == 1


@pytest.mark.integration
class TestQuotePaymentReporting:
    async def test_settled_q2026_017_receipt_only_deposits_are_info_notices(
        self, db_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two deposits plus the balance keep BTW facts while their audit notices settle."""

        class _FrozenQ1Date(date):
            @classmethod
            def today(cls) -> date:
                return cls(2026, 2, 20)

        monkeypatch.setattr(invoice_service, "date", _FrozenQ1Date)
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        quote = await _create_quote(
            db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
        )
        await _accept_quote(db_client, quote["id"])
        await _record(db_client, quote["id"], "24.20", "2026-02-01")
        await _record(db_client, quote["id"], "60.50", "2026-02-15")

        converted = await db_client.post(f"/api/v1/quotes/{quote['id']}/convert")
        assert converted.status_code == 201, converted.text
        issued = await db_client.post(
            f"/api/v1/invoices/{converted.json()['id']}/status", json={"status": "SENT"}
        )
        assert issued.status_code == 200, issued.text
        balance = await db_client.post(
            f"/api/v1/invoices/{converted.json()['id']}/payments",
            json={"payment_date": "2026-02-20", "amount": "36.30"},
        )
        assert balance.status_code == 201, balance.text
        invoice = await db_client.get(f"/api/v1/invoices/{converted.json()['id']}")
        assert invoice.status_code == 200, invoice.text
        assert invoice.json()["settlement_status"] == "SETTLED"

        report = await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=1")
        assert report.status_code == 200, report.text
        payload = report.json()
        assert Decimal(payload["boxes"]["box_1a"]["base"]) == Decimal("100.00")
        assert Decimal(payload["boxes"]["box_1a"]["vat"]) == Decimal("21.00")
        assert len(payload["infos"]) == 2
        assert all("receipt-only quote deposit" in info for info in payload["infos"])
        assert all("no duplicate VAT" in info for info in payload["infos"])
        assert not any("receipt-only quote deposit" in warning for warning in payload["warnings"])

    async def test_payment_date_edit_and_delete_move_dynamic_btw_period(
        self, db_client: AsyncClient
    ) -> None:
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]
        quote = await _create_quote(db_client, customer_id, rate_21)
        await _accept_quote(db_client, quote["id"])
        aggregate = await _record(db_client, quote["id"], "24.20", "2026-02-01")
        payment_id = aggregate["items"][0]["id"]

        q1 = await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=1")
        assert q1.status_code == 200, q1.text
        assert Decimal(q1.json()["boxes"]["box_1a"]["base"]) == Decimal("20.00")
        assert Decimal(q1.json()["boxes"]["box_1a"]["vat"]) == Decimal("4.20")

        edited = await db_client.put(
            f"/api/v1/payments/{payment_id}",
            json={"payment_date": "2026-04-01", "amount": "24.20"},
        )
        assert edited.status_code == 200, edited.text
        q1_after = await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=1")
        q2 = await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=2")
        assert Decimal(q1_after.json()["boxes"]["box_1a"]["vat"]) == Decimal("0")
        assert Decimal(q2.json()["boxes"]["box_1a"]["base"]) == Decimal("20.00")
        assert Decimal(q2.json()["boxes"]["box_1a"]["vat"]) == Decimal("4.20")

        deleted = await db_client.delete(f"/api/v1/payments/{payment_id}")
        assert deleted.status_code == 200, deleted.text
        q2_after = await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=2")
        assert Decimal(q2_after.json()["boxes"]["box_1a"]["base"]) == Decimal("0")
        assert Decimal(q2_after.json()["boxes"]["box_1a"]["vat"]) == Decimal("0")

    async def test_cross_quarter_final_invoice_recognises_only_remainder(
        self, db_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _FrozenQ3Date(date):
            @classmethod
            def today(cls) -> _FrozenQ3Date:
                return cls(2026, 9, 30)

        monkeypatch.setattr(invoice_service, "date", _FrozenQ3Date)
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]
        quote = await _create_quote(db_client, customer_id, rate_21)
        await _accept_quote(db_client, quote["id"])
        await _record(db_client, quote["id"], "24.20", "2026-02-01")
        await _record(db_client, quote["id"], "60.50", "2026-04-01")

        converted = await db_client.post(f"/api/v1/quotes/{quote['id']}/convert")
        assert converted.status_code == 201, converted.text
        issued = await db_client.post(
            f"/api/v1/invoices/{converted.json()['id']}/status",
            json={"status": "SENT"},
        )
        assert issued.status_code == 200, issued.text
        assert issued.json()["invoice_date"] == "2026-09-30"

        reports = []
        for quarter in (1, 2, 3):
            response = await db_client.get(
                f"/api/v1/reports/vat-return?year=2026&quarter={quarter}"
            )
            assert response.status_code == 200, response.text
            reports.append(response.json()["boxes"]["box_1a"])

        assert [Decimal(box["base"]) for box in reports] == [
            Decimal("20.00"),
            Decimal("50.00"),
            Decimal("30.00"),
        ]
        assert [Decimal(box["vat"]) for box in reports] == [
            Decimal("4.20"),
            Decimal("10.50"),
            Decimal("6.30"),
        ]
        assert sum((Decimal(box["base"]) for box in reports), Decimal("0")) == Decimal("100.00")
        assert sum((Decimal(box["vat"]) for box in reports), Decimal("0")) == Decimal("21.00")

    async def test_issued_mixed_rate_full_advance_cannot_move_past_final_invoice(
        self, db_client: AsyncClient
    ) -> None:
        """A converted payment cannot defer an issued final invoice's VAT.

        This uses the real PostgreSQL report query rather than mocked report
        rows.  A full Q1 advance is attached to a Q2 final invoice, so Q1
        recognises the two rate buckets and Q2's final-invoice offset brings
        both buckets exactly to zero.  Moving the payment into Q3 must fail:
        otherwise Q2 would subtract VAT before it was positively recognised.
        """
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]
        rate_9 = seeds["rates"]["NL reduced (9%)"]["id"]
        quote = await _create_quote(db_client, customer_id, rate_21, rate_9=rate_9)
        await _accept_quote(db_client, quote["id"])
        advance = await _record(db_client, quote["id"], "230.00", "2026-02-01")
        payment_id = advance["items"][0]["id"]

        converted = await db_client.post(f"/api/v1/quotes/{quote['id']}/convert")
        assert converted.status_code == 201, converted.text
        invoice = converted.json()
        final_invoice = await db_client.put(
            f"/api/v1/invoices/{invoice['id']}",
            json={
                "customer_id": customer_id,
                "invoice_date": "2026-04-01",
                "tax_mode": "LINE",
                "amounts_include_vat": False,
                "vat_treatment_id": seeds["treatments"]["NL_DOMESTIC"]["id"],
                "lines": [
                    {
                        "name": "Standard final service",
                        "quantity": "1",
                        "unit_price": "100.000",
                        "vat_rate_id": rate_21,
                    },
                    {
                        "name": "Reduced final service",
                        "quantity": "1",
                        "unit_price": "100.000",
                        "vat_rate_id": rate_9,
                    },
                ],
            },
        )
        assert final_invoice.status_code == 200, final_invoice.text
        issued = await db_client.post(
            f"/api/v1/invoices/{invoice['id']}/status", json={"status": "SENT"}
        )
        assert issued.status_code == 200, issued.text
        assert issued.json()["status"] == "COMPLETED"

        reports: list[dict] = []
        for quarter in (1, 2, 3):
            response = await db_client.get(
                f"/api/v1/reports/vat-return?year=2026&quarter={quarter}"
            )
            assert response.status_code == 200, response.text
            reports.append(response.json()["boxes"])

        q1, q2, q3 = reports
        assert Decimal(q1["box_1a"]["base"]) == Decimal("100.00")
        assert Decimal(q1["box_1a"]["vat"]) == Decimal("21.00")
        assert Decimal(q1["box_1b"]["base"]) == Decimal("100.00")
        assert Decimal(q1["box_1b"]["vat"]) == Decimal("9.00")
        for box_name in ("box_1a", "box_1b"):
            assert Decimal(q2[box_name]["base"]) == Decimal("0")
            assert Decimal(q2[box_name]["vat"]) == Decimal("0")
            assert Decimal(q3[box_name]["base"]) == Decimal("0")
            assert Decimal(q3[box_name]["vat"]) == Decimal("0")
            for report in reports:
                assert Decimal(report[box_name]["base"]) >= Decimal("0")
                assert Decimal(report[box_name]["vat"]) >= Decimal("0")

        assert sum(
            (Decimal(report["box_1a"]["vat"]) for report in reports), Decimal("0")
        ) == Decimal("21.00")
        assert sum(
            (Decimal(report["box_1b"]["vat"]) for report in reports), Decimal("0")
        ) == Decimal("9.00")
        assert sum(
            (
                Decimal(report["box_1a"]["vat"]) + Decimal(report["box_1b"]["vat"])
                for report in reports
            ),
            Decimal("0"),
        ) == Decimal(issued.json()["vat_total"])

        invalid_edit = await db_client.put(
            f"/api/v1/payments/{payment_id}",
            json={"payment_date": "2026-07-01", "amount": "230.00"},
        )
        assert invalid_edit.status_code == 422, invalid_edit.text
        assert invalid_edit.json()["detail"]["code"] == "PAYMENT_INVALID_INPUT"
        assert "later than the final invoice date" in invalid_edit.json()["detail"]["message"]

        for quarter, expected in zip((1, 2, 3), reports, strict=True):
            response = await db_client.get(
                f"/api/v1/reports/vat-return?year=2026&quarter={quarter}"
            )
            assert response.status_code == 200, response.text
            assert response.json()["boxes"] == expected


@pytest.mark.integration
class TestPaymentReferentialIntegrity:
    async def test_payment_tax_cascades_for_orm_and_database_deletes(
        self,
        db_client: AsyncClient,
        db_session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Both ORM and database payment deletion remove VAT snapshots."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        quote = await _create_quote(
            db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
        )
        await _accept_quote(db_client, quote["id"])
        first = await _record(db_client, quote["id"], "20.00", "2026-01-15")
        second = await _record(db_client, quote["id"], "30.00", "2026-01-16")
        first_id = uuid.UUID(first["items"][0]["id"])
        second_id = uuid.UUID(second["items"][1]["id"])

        async with db_session_maker() as session:
            tax_rows = await session.execute(
                select(PaymentTax).where(PaymentTax.payment_id.in_([first_id, second_id]))
            )
            taxes_by_payment = {tax.payment_id: tax.id for tax in tax_rows.scalars()}
            assert set(taxes_by_payment) == {first_id, second_id}

            orm_payment = await session.get(Payment, first_id)
            assert orm_payment is not None
            await session.delete(orm_payment)
            await session.commit()

        async with db_session_maker() as session:
            assert await session.get(PaymentTax, taxes_by_payment[first_id]) is None
            await session.execute(delete(Payment).where(Payment.id == second_id))
            await session.commit()

        async with db_session_maker() as session:
            assert await session.get(Payment, second_id) is None
            assert await session.get(PaymentTax, taxes_by_payment[second_id]) is None

    async def test_payment_tax_rate_snapshot_survives_rate_deletion(
        self,
        db_client: AsyncClient,
        db_session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Deleting a referenced dictionary rate SET NULLs only the live FK."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        quote = await _create_quote(
            db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
        )
        await _accept_quote(db_client, quote["id"])
        recorded = await _record(db_client, quote["id"], "20.00", "2026-01-15")
        payment_id = uuid.UUID(recorded["items"][0]["id"])
        transient_rate = await db_client.post(
            "/api/v1/vat-rates",
            json={"label": "Transient payment snapshot rate", "percent": "7.000"},
        )
        assert transient_rate.status_code == 201, transient_rate.text
        transient_rate_id = uuid.UUID(transient_rate.json()["id"])

        async with db_session_maker() as session:
            payment_tax = (
                await session.execute(select(PaymentTax).where(PaymentTax.payment_id == payment_id))
            ).scalar_one()
            payment_tax.vat_rate_id = transient_rate_id
            payment_tax_id = payment_tax.id
            snapshot_label = payment_tax.vat_rate_label
            snapshot_gross = payment_tax.gross_amount
            await session.commit()

        deleted = await db_client.delete(f"/api/v1/vat-rates/{transient_rate_id}")
        assert deleted.status_code == 204, deleted.text
        async with db_session_maker() as session:
            payment_tax = await session.get(PaymentTax, payment_tax_id)
            assert payment_tax is not None
            assert payment_tax.vat_rate_id is None
            assert payment_tax.vat_rate_label == snapshot_label
            assert payment_tax.gross_amount == snapshot_gross

    async def test_quote_restrict_and_invoice_payment_guard_preserve_rows(
        self,
        db_client: AsyncClient,
        db_session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Quote RESTRICT and invoice payment check prevent historical data loss."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_id = seeds["rates"]["NL standard (21%)"]["id"]
        quote = await _create_quote(db_client, customer_id, rate_id)
        await _accept_quote(db_client, quote["id"])
        quote_payment = await _record(db_client, quote["id"], "20.00", "2026-01-15")
        quote_payment_id = uuid.UUID(quote_payment["items"][0]["id"])
        quote_id = uuid.UUID(quote["id"])

        quote_delete = await db_client.delete(f"/api/v1/quotes/{quote['id']}")
        assert quote_delete.status_code == 422, quote_delete.text
        async with db_session_maker() as session:
            payment_tax = (
                await session.execute(
                    select(PaymentTax).where(PaymentTax.payment_id == quote_payment_id)
                )
            ).scalar_one()
            payment_tax_id = payment_tax.id

            source_quote = await session.get(Quote, quote_id)
            assert source_quote is not None
            await session.delete(source_quote)
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()

        # Use a new session after the failed flush: the FK rejection must leave
        # both provenance links and the payment-tax snapshot untouched.
        async with db_session_maker() as session:
            assert await session.get(Quote, quote_id) is not None
            payment_row = await session.get(Payment, quote_payment_id)
            assert payment_row is not None
            assert payment_row.quote_id == quote_id
            payment_tax_after = await session.get(PaymentTax, payment_tax_id)
            assert payment_tax_after is not None
            assert payment_tax_after.payment_id == quote_payment_id

        invoice = await _create_invoice(db_client, customer_id, rate_id)
        invoice_payment = await db_client.post(
            f"/api/v1/invoices/{invoice['id']}/payments",
            json={"payment_date": "2026-01-16", "amount": "20.00"},
        )
        assert invoice_payment.status_code == 201, invoice_payment.text
        invoice_payment_id = uuid.UUID(invoice_payment.json()["items"][0]["id"])
        invoice_id = uuid.UUID(invoice["id"])

        async with db_session_maker() as session:
            invoice_row = await session.get(Invoice, invoice_id)
            assert invoice_row is not None
            await session.delete(invoice_row)
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()

        async with db_session_maker() as session:
            invoice_row = await session.get(Invoice, invoice_id)
            payment_row = await session.get(Payment, invoice_payment_id)
            assert invoice_row is not None
            assert payment_row is not None
            assert payment_row.invoice_id == invoice_id
            assert payment_row.quote_id is None


@pytest.mark.integration
class TestQuotePaymentConversion:
    async def test_quote_receipt_stays_quote_based_after_conversion(
        self, db_client: AsyncClient
    ) -> None:
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]
        quote = await _create_quote(
            db_client,
            customer_id,
            rate_21,
            unit_price="6611.570",
        )
        await _accept_quote(db_client, quote["id"])
        first_aggregate = await _record(
            db_client,
            quote["id"],
            "1600.00",
            "2026-02-01",
            reference="DEP-FIRST",
        )
        payment_id = first_aggregate["items"][0]["id"]
        await _record(
            db_client,
            quote["id"],
            "4000.00",
            "2026-03-01",
            reference="DEP-SECOND",
        )

        before = await db_client.get(f"/api/v1/payments/{payment_id}/receipt-pdf?locale=en")
        assert before.status_code == 200, before.text
        assert before.headers["content-type"] == "application/pdf"
        assert quote["quote_number"] in before.headers["content-disposition"]
        assert before.content[:4] == b"%PDF"
        before_text = _extract_pdf_text(before.content)
        for expected in (
            quote["quote_number"],
            "NOT A VAT INVOICE",
            "8000.00",
            "1600.00",
            "5600.00",
            "2400.00",
        ):
            assert expected in before_text, expected
        assert "非 VAT 发票" not in before_text

        converted = await db_client.post(f"/api/v1/quotes/{quote['id']}/convert")
        assert converted.status_code == 201, converted.text

        after = await db_client.get(f"/api/v1/payments/{payment_id}/receipt-pdf?locale=en")
        assert after.status_code == 200, after.text
        assert after.headers["content-type"] == "application/pdf"
        assert quote["quote_number"] in after.headers["content-disposition"]
        assert after.content[:4] == b"%PDF"
        assert after.headers["content-disposition"] == before.headers["content-disposition"]
        after_text = _extract_pdf_text(after.content)
        for expected in (
            quote["quote_number"],
            "NOT A VAT INVOICE",
            "8000.00",
            "1600.00",
            "5600.00",
            "2400.00",
        ):
            assert expected in after_text, expected
        assert "非 VAT 发票" not in after_text

        final_invoice = await db_client.get(
            f"/api/v1/invoices/{converted.json()['id']}/pdf?locale=en"
        )
        assert final_invoice.status_code == 200, final_invoice.text
        assert final_invoice.headers["content-type"] == "application/pdf"
        assert final_invoice.content[:4] == b"%PDF"
        final_text = _extract_pdf_text(final_invoice.content)
        for expected in (
            "Subtotal",
            "6611.57",
            "VAT",
            "1388.43",
            "Total (incl. VAT)",
            "8000.00",
            "DEP-FIRST",
            "2026-02-01",
            "1600.00",
            "DEP-SECOND",
            "2026-03-01",
            "4000.00",
            "Already paid",
            "5600.00",
            "Amount Due",
            "2400.00",
        ):
            assert expected in final_text, expected

    async def test_final_invoice_pdf_reads_payments_in_database_stable_order(
        self,
        db_client: AsyncClient,
        db_session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The renderer, rather than a pre-sorted template input, owns payment order."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]
        quote = await _create_quote(db_client, customer_id, rate_21)
        await _accept_quote(db_client, quote["id"])

        await _record(db_client, quote["id"], "10.00", "2026-02-02", reference="DATE-LATER")
        await _record(db_client, quote["id"], "10.00", "2026-02-01", reference="TIE-ONE")
        await _record(db_client, quote["id"], "10.00", "2026-02-01", reference="TIE-TWO")
        converted = await db_client.post(f"/api/v1/quotes/{quote['id']}/convert")
        assert converted.status_code == 201, converted.text

        invoice_id = uuid.UUID(converted.json()["id"])
        tied_timestamp = datetime(2026, 2, 5, tzinfo=UTC)
        async with db_session_maker() as session:
            await session.execute(
                update(Payment)
                .where(Payment.invoice_id == invoice_id)
                .values(created_at=tied_timestamp)
            )
            await session.commit()
            ordered_references = list(
                (
                    await session.execute(
                        select(Payment.reference)
                        .where(Payment.invoice_id == invoice_id)
                        .order_by(Payment.payment_date, Payment.created_at, Payment.id)
                    )
                ).scalars()
            )

        assert ordered_references[0] in {"TIE-ONE", "TIE-TWO"}
        assert ordered_references[1] in {"TIE-ONE", "TIE-TWO"}
        assert ordered_references[0] != ordered_references[1]
        assert ordered_references[2] == "DATE-LATER"

        response = await db_client.get(f"/api/v1/invoices/{invoice_id}/pdf?locale=en")
        assert response.status_code == 200, response.text
        text = _extract_pdf_text(response.content)
        positions = [text.index(reference) for reference in ordered_references]
        assert positions == sorted(positions)

    async def test_payment_racing_conversion_is_never_left_unattached(
        self, db_client: AsyncClient
    ) -> None:
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]
        quote = await _create_quote(db_client, customer_id, rate_21)
        await _accept_quote(db_client, quote["id"])

        payment_request, convert_request = await asyncio.gather(
            db_client.post(
                f"/api/v1/quotes/{quote['id']}/payments",
                json={"payment_date": "2026-02-01", "amount": "60.00"},
            ),
            db_client.post(f"/api/v1/quotes/{quote['id']}/convert"),
        )
        assert convert_request.status_code == 201, convert_request.text
        assert payment_request.status_code in (201, 422), payment_request.text
        aggregate = await db_client.get(f"/api/v1/quotes/{quote['id']}/payments")
        assert aggregate.status_code == 200
        for payment in aggregate.json()["items"]:
            assert payment["invoice_id"] == convert_request.json()["id"]

    async def test_partial_deposit_transfers_and_draft_delete_restores_quote(
        self, db_client: AsyncClient
    ) -> None:
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]
        quote = await _create_quote(db_client, customer_id, rate_21)
        await _accept_quote(db_client, quote["id"])
        aggregate = await _record(db_client, quote["id"], "60.00", "2026-02-01", reference="DEP-1")
        payment_id = aggregate["items"][0]["id"]

        response = await db_client.post(f"/api/v1/quotes/{quote['id']}/convert")
        assert response.status_code == 201, response.text
        invoice = response.json()
        assert invoice["status"] == "DRAFT"
        assert invoice["paid_status"] == "PARTIALLY_PAID"
        assert invoice["due_amount"] == "61.000"
        assert invoice["invoice_number"] is None

        payment_response = await db_client.get(f"/api/v1/payments/{payment_id}")
        payment = payment_response.json()
        assert payment["quote_id"] == quote["id"]
        assert payment["invoice_id"] == invoice["id"]
        assert payment["origin_type"] == "QUOTE"

        response = await db_client.post(
            f"/api/v1/invoices/{invoice['id']}/status",
            json={"status": "CANCELLED"},
        )
        assert response.status_code == 409

        response = await db_client.delete(f"/api/v1/invoices/{invoice['id']}")
        assert response.status_code == 204, response.text
        restored = await db_client.get(f"/api/v1/quotes/{quote['id']}/payments")
        assert restored.status_code == 200
        assert restored.json()["converted_invoice_id"] is None
        assert restored.json()["items"][0]["invoice_id"] is None

        response = await db_client.post(f"/api/v1/quotes/{quote['id']}/convert")
        assert response.status_code == 201, response.text
        second_invoice = response.json()
        assert second_invoice["id"] != invoice["id"]
        assert second_invoice["due_amount"] == "61.000"
        restored_again = await db_client.get(f"/api/v1/quotes/{quote['id']}/payments")
        assert len(restored_again.json()["items"]) == 1

        issued = await db_client.post(
            f"/api/v1/invoices/{second_invoice['id']}/status",
            json={"status": "SENT"},
        )
        assert issued.status_code == 200, issued.text
        assert issued.json()["status"] == "SENT"
        assert issued.json()["paid_status"] == "PARTIALLY_PAID"
        assert issued.json()["invoice_number"] is not None

    async def test_draft_delete_and_deposit_edit_share_lock_order(
        self, db_client: AsyncClient
    ) -> None:
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]
        quote = await _create_quote(db_client, customer_id, rate_21)
        await _accept_quote(db_client, quote["id"])
        aggregate = await _record(db_client, quote["id"], "40.00", "2026-02-01")
        payment_id = aggregate["items"][0]["id"]
        converted = await db_client.post(f"/api/v1/quotes/{quote['id']}/convert")
        assert converted.status_code == 201, converted.text

        deleted, edited = await asyncio.gather(
            db_client.delete(f"/api/v1/invoices/{converted.json()['id']}"),
            db_client.put(
                f"/api/v1/payments/{payment_id}",
                json={"payment_date": "2026-02-01", "amount": "45.00"},
            ),
        )
        assert deleted.status_code == 204, deleted.text
        assert edited.status_code == 200, edited.text

        restored = await db_client.get(f"/api/v1/quotes/{quote['id']}/payments")
        assert restored.status_code == 200, restored.text
        assert restored.json()["converted_invoice_id"] is None
        assert restored.json()["items"][0]["invoice_id"] is None
        assert restored.json()["paid_total"] == "45.000"

    async def test_full_prepayment_issues_directly_as_completed(
        self, db_client: AsyncClient
    ) -> None:
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]
        quote = await _create_quote(db_client, customer_id, rate_21)
        await _accept_quote(db_client, quote["id"])
        await _record(db_client, quote["id"], "121.00", "2026-02-01")

        converted = await db_client.post(f"/api/v1/quotes/{quote['id']}/convert")
        assert converted.status_code == 201, converted.text
        draft = converted.json()
        assert draft["status"] == "DRAFT"
        assert draft["paid_status"] == "PAID"
        assert draft["due_amount"] == "0.000"

        issued = await db_client.post(
            f"/api/v1/invoices/{draft['id']}/status",
            json={"status": "SENT"},
        )
        assert issued.status_code == 200, issued.text
        assert issued.json()["status"] == "COMPLETED"
        assert issued.json()["paid_status"] == "PAID"
        assert issued.json()["invoice_number"] is not None

        rejected = await db_client.post(
            f"/api/v1/quotes/{quote['id']}/payments",
            json={"payment_date": "2026-02-02", "amount": "1.00"},
        )
        assert rejected.status_code == 422

    async def test_draft_edit_guards_paid_total_date_customer_and_vat_bucket(
        self, db_client: AsyncClient
    ) -> None:
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        other_customer_id = await _create_customer(db_client, name="Other Customer")
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]
        rate_9 = seeds["rates"]["NL reduced (9%)"]["id"]
        quote = await _create_quote(db_client, customer_id, rate_21)
        await _accept_quote(db_client, quote["id"])
        await _record(db_client, quote["id"], "60.00", "2026-02-01")
        converted = await db_client.post(f"/api/v1/quotes/{quote['id']}/convert")
        invoice = converted.json()

        def payload(
            *,
            customer: str = customer_id,
            invoice_date: str = "2026-02-01",
            price: str = "100.000",
            rate_id: str = rate_21,
        ) -> dict:
            return {
                "customer_id": customer,
                "invoice_date": invoice_date,
                "tax_mode": "LINE",
                "amounts_include_vat": False,
                "vat_treatment_id": seeds["treatments"]["NL_DOMESTIC"]["id"],
                "lines": [
                    {
                        "name": "Final service",
                        "quantity": "1",
                        "unit_price": price,
                        "vat_rate_id": rate_id,
                    }
                ],
            }

        for guarded_payload in (
            payload(customer=other_customer_id),
            payload(invoice_date="2026-01-31"),
            payload(price="40.000"),
            payload(price="100.000", rate_id=rate_9),
        ):
            response = await db_client.put(
                f"/api/v1/invoices/{invoice['id']}", json=guarded_payload
            )
            assert response.status_code == 422, response.text

        response = await db_client.put(
            f"/api/v1/invoices/{invoice['id']}", json=payload(price="110.000")
        )
        assert response.status_code == 200, response.text
        assert response.json()["due_amount"] == "73.100"

    async def test_editing_converted_deposit_rechecks_final_vat_buckets(
        self, db_client: AsyncClient
    ) -> None:
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        rate_21 = seeds["rates"]["NL standard (21%)"]["id"]
        rate_9 = seeds["rates"]["NL reduced (9%)"]["id"]
        quote = await _create_quote(db_client, customer_id, rate_21, rate_9=rate_9)
        await _accept_quote(db_client, quote["id"])
        aggregate = await _record(db_client, quote["id"], "46.00", "2026-02-01")
        payment_id = aggregate["items"][0]["id"]
        converted = await db_client.post(f"/api/v1/quotes/{quote['id']}/convert")
        invoice = converted.json()

        response = await db_client.put(
            f"/api/v1/invoices/{invoice['id']}",
            json={
                "customer_id": customer_id,
                "invoice_date": "2026-02-01",
                "tax_mode": "LINE",
                "amounts_include_vat": False,
                "vat_treatment_id": seeds["treatments"]["NL_DOMESTIC"]["id"],
                "lines": [
                    {
                        "name": "Retained standard bucket",
                        "quantity": "1",
                        "unit_price": "20.000",
                        "vat_rate_id": rate_21,
                    },
                    {
                        "name": "Expanded reduced bucket",
                        "quantity": "1",
                        "unit_price": "220.000",
                        "vat_rate_id": rate_9,
                    },
                ],
            },
        )
        assert response.status_code == 200, response.text

        response = await db_client.put(
            f"/api/v1/payments/{payment_id}",
            json={"payment_date": "2026-02-01", "amount": "100.00"},
        )
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == "PAYMENT_INVALID_INPUT"
        assert "VAT buckets" in response.json()["detail"]["message"]


@pytest.mark.integration
class TestPdfPaymentSnapshot:
    async def test_final_invoice_pdf_waits_for_payment_delete_and_renders_old_snapshot(
        self,
        db_client: AsyncClient,
        db_session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A payment delete cannot slip between invoice and payment PDF reads."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        invoice = await _create_invoice(
            db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
        )
        payment_response = await db_client.post(
            f"/api/v1/invoices/{invoice['id']}/payments",
            json={
                "payment_date": "2026-02-01",
                "amount": "60.00",
                "reference": "INVOICE-PDF-RACE",
            },
        )
        assert payment_response.status_code == 201, payment_response.text
        payment_id = uuid.UUID(payment_response.json()["items"][0]["id"])
        invoice_id = uuid.UUID(invoice["id"])
        company_id = uuid.UUID(seeds["company_id"])

        parent_locked = asyncio.Event()
        release_renderer = asyncio.Event()
        monkeypatch.setattr(pdf_service, "html_to_pdf", lambda html: html.encode())

        async with db_session_maker() as pdf_session:
            original_execute = pdf_session.execute

            async def gated_execute(statement, *args, **kwargs):
                result = await original_execute(statement, *args, **kwargs)
                if not parent_locked.is_set() and "FROM invoice" in str(statement):
                    parent_locked.set()
                    await release_renderer.wait()
                return result

            monkeypatch.setattr(pdf_session, "execute", gated_execute)
            render_task = asyncio.create_task(
                pdf_service.render_invoice_pdf(pdf_session, invoice_id, company_id)
            )
            await asyncio.wait_for(parent_locked.wait(), timeout=2)

            async with db_session_maker() as mutation_session:
                delete_task = asyncio.create_task(
                    delete_payment(mutation_session, payment_id, company_id)
                )
                await asyncio.sleep(0.05)
                assert not delete_task.done(), "payment delete bypassed the invoice parent lock"

                release_renderer.set()
                html, _ = await asyncio.wait_for(render_task, timeout=2)
                assert b"INVOICE-PDF-RACE" in html
                assert b"-60.00" in html
                assert b">61.00<" in html

                # Request-scoped sessions release this read lock at request end.
                # Do that explicitly here before awaiting the writer's commit.
                await pdf_session.rollback()
                deleted = await asyncio.wait_for(delete_task, timeout=2)

        assert deleted.deleted is True


@pytest.mark.integration
class TestPaymentReceiptEmail:
    """Receipt send API preserves immutable payment provenance and mail safety."""

    async def test_quote_receipt_email_uses_quote_source_and_selected_locale(
        self, db_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        quote = await _create_quote(
            db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
        )
        await _accept_quote(db_client, quote["id"])
        payment = await _record(db_client, quote["id"], "60.00", "2026-02-01")
        payment_id = payment["items"][0]["id"]
        render = AsyncMock(return_value=(b"%PDF-1.4 receipt-zh", "receipt-Q.pdf"))
        send = AsyncMock()
        monkeypatch.setattr(pdf_service, "render_payment_receipt_pdf", render)
        monkeypatch.setattr(email_service, "_get_smtp_config", AsyncMock(return_value=_smtp()))
        monkeypatch.setattr(email_service, "_send_mail", send)

        response = await db_client.post(
            f"/api/v1/payments/{payment_id}/send-receipt",
            json={"to": "customer@example.com", "locale": "zh"},
        )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["related_type"] == "QUOTE"
        assert payload["related_id"] == quote["id"]
        assert payload["locale"] == "zh"
        assert payload["subject"] == f"Deposit Test Co 的收款收据（{quote['quote_number']}）"
        assert render.await_args.kwargs["locale"] == "zh"
        assert send.await_args.kwargs["attachment_bytes"] == b"%PDF-1.4 receipt-zh"

        converted = await db_client.post(f"/api/v1/quotes/{quote['id']}/convert")
        assert converted.status_code == 201, converted.text
        second = await db_client.post(
            f"/api/v1/payments/{payment_id}/send-receipt",
            json={"to": "customer@example.com"},
        )
        assert second.status_code == 200, second.text
        assert second.json()["related_type"] == "QUOTE"
        assert second.json()["related_id"] == quote["id"]

    async def test_receipt_email_sends_real_localized_attachment_and_keeps_provenance(
        self, db_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The send endpoint attaches the real renderer output, not a stub."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client, email="zh@example.com", locale="zh")
        quote = await _create_quote(
            db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
        )
        await _accept_quote(db_client, quote["id"])
        payment = await _record(db_client, quote["id"], "60.00", "2026-02-01")
        payment_id = payment["items"][0]["id"]
        attachments: list[tuple[bytes, str]] = []

        async def capture_send(**kwargs: object) -> None:
            attachments.append((kwargs["attachment_bytes"], kwargs["attachment_filename"]))  # type: ignore[arg-type]

        monkeypatch.setattr(email_service, "_get_smtp_config", AsyncMock(return_value=_smtp()))
        monkeypatch.setattr(email_service, "_send_mail", capture_send)

        zh = await db_client.post(
            f"/api/v1/payments/{payment_id}/send-receipt",
            json={"to": "zh@example.com"},
        )
        assert zh.status_code == 200, zh.text
        zh_text = _extract_pdf_text(attachments[-1][0])
        assert "非 VAT 发票" in zh_text
        assert "NOT A VAT INVOICE" not in zh_text
        assert attachments[-1][1].startswith(f"receipt-{quote['quote_number']}-")

        converted = await db_client.post(f"/api/v1/quotes/{quote['id']}/convert")
        assert converted.status_code == 201, converted.text
        en = await db_client.post(
            f"/api/v1/payments/{payment_id}/send-receipt",
            json={"to": "zh@example.com", "locale": "en"},
        )
        assert en.status_code == 200, en.text
        assert en.json()["related_type"] == "QUOTE"
        assert en.json()["related_id"] == quote["id"]
        en_text = _extract_pdf_text(attachments[-1][0])
        assert "NOT A VAT INVOICE" in en_text
        assert "非 VAT 发票" not in en_text
        assert attachments[-1][1].startswith(f"receipt-{quote['quote_number']}-")

        invoice_customer = await _create_customer(db_client, email="invoice@example.com")
        invoice = await _create_invoice(
            db_client, invoice_customer, seeds["rates"]["NL standard (21%)"]["id"]
        )
        recorded = await db_client.post(
            f"/api/v1/invoices/{invoice['id']}/payments",
            json={"payment_date": "2026-02-01", "amount": "60.00"},
        )
        invoice_payment_id = recorded.json()["items"][0]["id"]
        invoice_send = await db_client.post(
            f"/api/v1/payments/{invoice_payment_id}/send-receipt",
            json={"to": "invoice@example.com"},
        )
        assert invoice_send.status_code == 200, invoice_send.text
        invoice_text = _extract_pdf_text(attachments[-1][0])
        assert "NOT A VAT INVOICE" not in invoice_text
        assert "非 VAT 发票" not in invoice_text

    async def test_receipt_email_real_renderer_falls_back_to_english(
        self, db_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No request/customer/company locale override falls back to English."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        # The fresh company has no persisted document-defaults setting, and this
        # customer deliberately has no locale either.
        customer_id = await _create_customer(db_client, email="fallback@example.com")
        quote = await _create_quote(
            db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
        )
        await _accept_quote(db_client, quote["id"])
        payment = await _record(db_client, quote["id"], "60.00", "2026-02-01")
        payment_id = payment["items"][0]["id"]
        attachments: list[tuple[bytes, str]] = []

        async def capture_send(**kwargs: object) -> None:
            attachments.append((kwargs["attachment_bytes"], kwargs["attachment_filename"]))  # type: ignore[arg-type]

        monkeypatch.setattr(email_service, "_get_smtp_config", AsyncMock(return_value=_smtp()))
        monkeypatch.setattr(email_service, "_send_mail", capture_send)

        response = await db_client.post(
            f"/api/v1/payments/{payment_id}/send-receipt",
            json={"to": "fallback@example.com"},
        )

        assert response.status_code == 200, response.text
        assert response.json()["locale"] == "en"
        text = _extract_pdf_text(attachments[-1][0])
        assert "NOT A VAT INVOICE" in text
        assert "非 VAT 发票" not in text
        assert attachments[-1][1].startswith(f"receipt-{quote['quote_number']}-")

    async def test_invoice_receipt_email_defaults_custom_body_and_failure_safety(
        self, db_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        invoice = await _create_invoice(
            db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
        )
        recorded = await db_client.post(
            f"/api/v1/invoices/{invoice['id']}/payments",
            json={"payment_date": "2026-02-01", "amount": "60.00"},
        )
        assert recorded.status_code == 201, recorded.text
        payment_id = recorded.json()["items"][0]["id"]
        monkeypatch.setattr(
            pdf_service,
            "render_payment_receipt_pdf",
            AsyncMock(return_value=(b"%PDF-1.4 receipt-en", "receipt-I.pdf")),
        )
        monkeypatch.setattr(email_service, "_get_smtp_config", AsyncMock(return_value=_smtp()))
        monkeypatch.setattr(email_service, "_send_mail", AsyncMock())

        response = await db_client.post(
            f"/api/v1/payments/{payment_id}/send-receipt",
            json={
                "to": "customer@example.com",
                "subject": "Due {AMOUNT_DUE} for {DOCUMENT_NUMBER}",
                "body": "<script>bad()</script>Receipt for {DOCUMENT_NUMBER}; due {AMOUNT_DUE}",
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["related_type"] == "INVOICE"
        assert payload["related_id"] == invoice["id"]
        assert payload["locale"] == "en"
        assert payload["subject"] == f"Due 61.000 for {invoice['invoice_number']}"
        assert "<script>" not in payload["body_snapshot"]
        assert invoice["invoice_number"] in payload["body_snapshot"]
        assert "due 61.000" in payload["body_snapshot"]

        async def fail_send(*_args: object, **_kwargs: object) -> None:
            raise ConnectionError("smtp-secret smtp-user rejected")

        monkeypatch.setattr(email_service, "_send_mail", fail_send)
        failed = await db_client.post(
            f"/api/v1/payments/{payment_id}/send-receipt",
            json={"to": "customer@example.com"},
        )
        assert failed.status_code == 200, failed.text
        assert failed.json()["status"] == "FAILED"
        assert "smtp-secret" not in failed.json()["error_message"]
        assert "smtp-user" not in failed.json()["error_message"]

    async def test_receipt_email_unconfigured_and_missing_are_safe(
        self, db_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        invoice = await _create_invoice(
            db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
        )
        recorded = await db_client.post(
            f"/api/v1/invoices/{invoice['id']}/payments",
            json={"payment_date": "2026-02-01", "amount": "60.00"},
        )
        assert recorded.status_code == 201, recorded.text
        payment_id = recorded.json()["items"][0]["id"]
        monkeypatch.setattr(
            pdf_service,
            "render_payment_receipt_pdf",
            AsyncMock(return_value=(b"%PDF", "receipt-I.pdf")),
        )
        monkeypatch.setattr(email_service, "_get_smtp_config", AsyncMock(return_value=None))

        unconfigured = await db_client.post(
            f"/api/v1/payments/{payment_id}/send-receipt",
            json={"to": "customer@example.com"},
        )
        assert unconfigured.status_code == 400
        assert "SMTP" in unconfigured.json()["detail"]
        logs = await db_client.get(f"/api/v1/invoices/{invoice['id']}/emails")
        assert logs.status_code == 200
        assert logs.json()["items"] == []

        missing = await db_client.post(
            f"/api/v1/payments/{uuid.uuid4()}/send-receipt",
            json={"to": "customer@example.com"},
        )
        assert missing.status_code == 404

    async def test_invoice_receipt_waits_for_current_payment_delete(
        self,
        db_client: AsyncClient,
        db_session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An invoice-origin receipt is all-old or all-new, never a stale receipt."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        invoice = await _create_invoice(
            db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
        )
        payment_response = await db_client.post(
            f"/api/v1/invoices/{invoice['id']}/payments",
            json={
                "payment_date": "2026-02-01",
                "amount": "60.00",
                "reference": "INVOICE-RECEIPT-RACE",
            },
        )
        assert payment_response.status_code == 201, payment_response.text
        payment_id = uuid.UUID(payment_response.json()["items"][0]["id"])
        company_id = uuid.UUID(seeds["company_id"])

        parent_locked = asyncio.Event()
        release_renderer = asyncio.Event()
        monkeypatch.setattr(pdf_service, "html_to_pdf", lambda html: html.encode())

        async with db_session_maker() as pdf_session:
            original_execute = pdf_session.execute

            async def gated_execute(statement, *args, **kwargs):
                result = await original_execute(statement, *args, **kwargs)
                if not parent_locked.is_set() and "FROM invoice" in str(statement):
                    parent_locked.set()
                    await release_renderer.wait()
                return result

            monkeypatch.setattr(pdf_session, "execute", gated_execute)
            render_task = asyncio.create_task(
                pdf_service.render_payment_receipt_pdf(pdf_session, payment_id, company_id)
            )
            await asyncio.wait_for(parent_locked.wait(), timeout=2)

            async with db_session_maker() as mutation_session:
                delete_task = asyncio.create_task(
                    delete_payment(mutation_session, payment_id, company_id)
                )
                await asyncio.sleep(0.05)
                assert not delete_task.done(), "payment delete bypassed the invoice parent lock"

                release_renderer.set()
                html, _ = await asyncio.wait_for(render_task, timeout=2)
                assert b"INVOICE-RECEIPT-RACE" in html
                assert b">60.00<" in html
                assert b">61.00<" in html

                await pdf_session.rollback()
                deleted = await asyncio.wait_for(delete_task, timeout=2)

        assert deleted.deleted is True

    async def test_receipt_endpoint_releases_snapshot_locks_before_slow_smtp(
        self,
        db_client: AsyncClient,
        db_session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A writer waits for the snapshot, but never for SMTP network I/O."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        invoice = await _create_invoice(
            db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
        )
        recorded = await db_client.post(
            f"/api/v1/invoices/{invoice['id']}/payments",
            json={
                "payment_date": "2026-02-01",
                "amount": "60.00",
                "reference": "SMTP-SNAPSHOT",
            },
        )
        payment_id = uuid.UUID(recorded.json()["items"][0]["id"])
        company_id = uuid.UUID(seeds["company_id"])
        smtp_entered = asyncio.Event()
        release_smtp = asyncio.Event()
        sent_attachments: list[bytes] = []

        async def slow_send(**kwargs: object) -> None:
            sent_attachments.append(kwargs["attachment_bytes"])  # type: ignore[arg-type]
            smtp_entered.set()
            await release_smtp.wait()

        monkeypatch.setattr(email_service, "_get_smtp_config", AsyncMock(return_value=_smtp()))
        monkeypatch.setattr(email_service, "_send_mail", slow_send)
        monkeypatch.setattr(pdf_service, "html_to_pdf", lambda html: html.encode())

        send_task = asyncio.create_task(
            db_client.post(
                f"/api/v1/payments/{payment_id}/send-receipt",
                json={"to": "customer@example.com"},
            )
        )
        await asyncio.wait_for(smtp_entered.wait(), timeout=2)

        async with db_session_maker() as writer_session:
            deleted = await asyncio.wait_for(
                delete_payment(writer_session, payment_id, company_id), timeout=2
            )
        assert deleted.deleted is True
        assert b"SMTP-SNAPSHOT" in sent_attachments[0]

        release_smtp.set()
        response = await asyncio.wait_for(send_task, timeout=2)
        assert response.status_code == 200, response.text

    async def test_quote_receipt_waits_for_current_payment_delete(
        self,
        db_client: AsyncClient,
        db_session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A quote receipt reloads its current payment after its quote parent lock."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        customer_id = await _create_customer(db_client)
        quote = await _create_quote(
            db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
        )
        await _accept_quote(db_client, quote["id"])
        aggregate = await _record(
            db_client,
            quote["id"],
            "60.00",
            "2026-02-01",
            reference="QUOTE-RECEIPT-RACE",
        )
        payment_id = uuid.UUID(aggregate["items"][0]["id"])
        company_id = uuid.UUID(seeds["company_id"])

        parent_locked = asyncio.Event()
        release_renderer = asyncio.Event()
        monkeypatch.setattr(pdf_service, "html_to_pdf", lambda html: html.encode())

        async with db_session_maker() as pdf_session:
            original_execute = pdf_session.execute

            async def gated_execute(statement, *args, **kwargs):
                result = await original_execute(statement, *args, **kwargs)
                if not parent_locked.is_set() and "FROM quote" in str(statement):
                    parent_locked.set()
                    await release_renderer.wait()
                return result

            monkeypatch.setattr(pdf_session, "execute", gated_execute)
            render_task = asyncio.create_task(
                pdf_service.render_payment_receipt_pdf(pdf_session, payment_id, company_id)
            )
            await asyncio.wait_for(parent_locked.wait(), timeout=2)

            async with db_session_maker() as mutation_session:
                delete_task = asyncio.create_task(
                    delete_payment(mutation_session, payment_id, company_id)
                )
                await asyncio.sleep(0.05)
                assert not delete_task.done(), "payment delete bypassed the quote parent lock"

                release_renderer.set()
                html, _ = await asyncio.wait_for(render_task, timeout=2)
                assert b"QUOTE-RECEIPT-RACE" in html
                assert b">60.00<" in html
                assert b">61.00<" in html

                await pdf_session.rollback()
                deleted = await asyncio.wait_for(delete_task, timeout=2)

        assert deleted.deleted is True


@pytest.mark.integration
@pytest.mark.parametrize("source", ["quote", "invoice"])
async def test_si1_receipt_actual_transport_copy_failure_keeps_delivery(db_client, source):
    from unittest.mock import MagicMock, patch

    from jai.schemas.setting import SentCopySettings

    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    quote = await _create_quote(db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"])
    await _accept_quote(db_client, quote["id"])
    payment = await _record(db_client, quote["id"], "60.00", "2026-02-01")
    payment_id = payment["items"][0]["id"]
    if source == "invoice":
        # A new invoice-origin payment keeps the source association distinct.
        converted = await db_client.post(f"/api/v1/quotes/{quote['id']}/convert")
        assert converted.status_code == 201, converted.text
        invoice_id = converted.json()["id"]
        issued = await db_client.post(
            f"/api/v1/invoices/{invoice_id}/status", json={"status": "SENT"}
        )
        assert issued.status_code == 200, issued.text
        paid = await db_client.post(
            f"/api/v1/invoices/{invoice_id}/payments",
            json={"payment_date": "2026-02-02", "amount": "10.00"},
        )
        assert paid.status_code == 201, paid.text
        payment_id = paid.json()["items"][-1]["id"]
    cfg = _smtp()
    cfg.sent_copy = SentCopySettings(enabled=True, host="imap.example.test")
    imap = MagicMock()
    imap.login.return_value = ("OK", [])
    imap.capability.return_value = ("OK", [b"IMAP4rev1"])
    imap.list.return_value = ("OK", [b'(\\Sent) "/" Sent'])
    imap.append.side_effect = TimeoutError("SECRET")
    with (
        patch("jai.services.email._get_smtp_config", return_value=cfg),
        patch("aiosmtplib.send", new_callable=AsyncMock) as smtp,
        patch("jai.services.sent_copy.imaplib.IMAP4_SSL", return_value=imap),
    ):
        response = await db_client.post(
            f"/api/v1/payments/{payment_id}/send-receipt",
            json={"to": "to@example.com", "cc": "cc@example.com"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "SENT"
    assert response.json()["sent_copy_status"] == "FAILED"
    assert response.json()["sent_copy_error_code"] == "IMAP_TIMEOUT"
    smtp.assert_called_once()
    assert smtp.call_args.args[0] == imap.append.call_args.args[3]

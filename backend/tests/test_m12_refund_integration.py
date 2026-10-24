"""Dedicated PostgreSQL integration coverage for M12 Step 7 refunds."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from test_m12_advance_integration import _additional_formal_quote
from test_m12_credit_integration import _credit_payload, _issued_standard
from test_m12_final_integration import _issued_advance
from test_quote_payment_integration import (
    _accept_quote,
    _create_customer,
    _create_quote,
    _full_auth,
    _record,
    _setup_company,
)

from jai.db import set_rls_company
from jai.services import invoice as invoice_service
from jai.services.reporting import dashboard as dashboard_service

pytestmark = pytest.mark.integration


async def _assert_quote_projection_matches_chain(
    client: AsyncClient, quote_id: str
) -> tuple[dict[str, object], dict[str, object]]:
    """QuoteRead's compact projection must share the document-chain totals."""
    quote_response = await client.get(f"/api/v1/quotes/{quote_id}")
    chain_response = await client.get(f"/api/v1/quotes/{quote_id}/document-chain")
    assert quote_response.status_code == chain_response.status_code == 200
    quote = quote_response.json()
    chain = chain_response.json()
    assert quote["chain_totals"] == chain["totals"]
    # These M11.5 compatibility fields remain derived from the compact
    # projection rather than reopening a competing Payment query.
    assert quote["incoming_payment_total"] == chain["totals"]["incoming_payment_total"]
    return quote, chain


async def _bootstrap(client: AsyncClient) -> tuple[dict, dict[str, object]]:
    await _full_auth(client)
    seeds = await _setup_company(client)
    source = await _issued_standard(
        client, seeds["rates"]["NL standard (21%)"]["id"]
    )
    return seeds, source


async def _pay(
    client: AsyncClient,
    source_id: object,
    amount: str,
    *,
    payment_date: str = "2026-02-02",
) -> dict:
    response = await client.post(
        f"/api/v1/invoices/{source_id}/payments",
        json={"payment_date": payment_date, "amount": amount},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _issue_full_credit(
    client: AsyncClient,
    source_id: object,
    *,
    invoice_date: str = "2026-02-03",
) -> dict:
    draft = await client.post(
        f"/api/v1/invoices/{source_id}/credit-notes",
        json=_credit_payload(invoice_date=invoice_date),
    )
    assert draft.status_code == 201, draft.text
    issued = await client.post(
        f"/api/v1/invoices/{draft.json()['id']}/status", json={"status": "SENT"}
    )
    assert issued.status_code == 200, issued.text
    return issued.json()


async def _issue_partial_credit(
    client: AsyncClient,
    source_id: object,
    *,
    invoice_date: str,
) -> dict:
    preview = await client.post(
        f"/api/v1/invoices/{source_id}/credit-notes/calculate",
        json={"full_remaining": True},
    )
    assert preview.status_code == 200, preview.text
    payload = _credit_payload(quantity="1", invoice_date=invoice_date)
    payload["lines"][0]["source_basis_line_id"] = preview.json()["lines"][0][  # type: ignore[index]
        "source_basis_line_id"
    ]
    draft = await client.post(
        f"/api/v1/invoices/{source_id}/credit-notes", json=payload
    )
    assert draft.status_code == 201, draft.text
    issued = await client.post(
        f"/api/v1/invoices/{draft.json()['id']}/status", json={"status": "SENT"}
    )
    assert issued.status_code == 200, issued.text
    return issued.json()


async def _issue_gross_credit(
    client: AsyncClient,
    source_id: object,
    *,
    gross: str,
    invoice_date: str,
) -> dict:
    preview = await client.post(
        f"/api/v1/invoices/{source_id}/credit-notes/calculate",
        json={"full_remaining": True},
    )
    assert preview.status_code == 200, preview.text
    payload = _credit_payload(gross=gross, invoice_date=invoice_date)
    payload["lines"][0]["source_basis_line_id"] = preview.json()["lines"][0][  # type: ignore[index]
        "source_basis_line_id"
    ]
    draft = await client.post(
        f"/api/v1/invoices/{source_id}/credit-notes", json=payload
    )
    assert draft.status_code == 201, draft.text
    issued = await client.post(
        f"/api/v1/invoices/{draft.json()['id']}/status", json={"status": "SENT"}
    )
    assert issued.status_code == 200, issued.text
    return issued.json()


@pytest.mark.parametrize(
    ("paid", "paid_status", "refund_due"),
    [
        (None, "UNPAID", "0"),
        ("60.50", "PARTIALLY_PAID", "60.50"),
        ("121.00", "PAID", "121.00"),
    ],
)
async def test_unpaid_part_paid_and_paid_full_credit_settlement(
    db_client: AsyncClient,
    paid: str | None,
    paid_status: str,
    refund_due: str,
) -> None:
    """Credit reduces due; only already collected cash becomes refundable."""
    _, source = await _bootstrap(db_client)
    if paid is not None:
        await _pay(db_client, source["id"], paid)
    credit = await _issue_full_credit(db_client, source["id"])

    source_read = (await db_client.get(f"/api/v1/invoices/{source['id']}")).json()
    collection = (
        await db_client.get(f"/api/v1/credit-notes/{credit['id']}/refunds")
    ).json()
    assert source_read["paid_status"] == paid_status
    assert Decimal(source_read["due_amount"]) == Decimal("0")
    assert Decimal(source_read["refund_due_amount"]) == Decimal(refund_due)
    assert Decimal(source_read["base_refund_due_amount"]) == Decimal(refund_due)
    assert Decimal(collection["chain_refund_due_amount"]) == Decimal(refund_due)
    assert Decimal(collection["base_chain_refund_due_amount"]) == Decimal(refund_due)

    attempt = await db_client.post(
        f"/api/v1/credit-notes/{credit['id']}/refunds",
        json={"payment_date": "2026-02-04", "amount": "0.01"},
    )
    if paid is None:
        assert attempt.status_code == 422
    else:
        assert attempt.status_code == 201, attempt.text


async def test_partial_credit_only_reduces_due_without_creating_refund_pg(
    db_client: AsyncClient,
) -> None:
    _, source = await _bootstrap(db_client)
    credit = await _issue_partial_credit(
        db_client, source["id"], invoice_date="2026-02-03"
    )
    source_read = (await db_client.get(f"/api/v1/invoices/{source['id']}")).json()
    collection = (
        await db_client.get(f"/api/v1/credit-notes/{credit['id']}/refunds")
    ).json()
    assert Decimal(source_read["credited_total"]) == Decimal("60.50")
    assert Decimal(source_read["due_amount"]) == Decimal("60.50")
    assert Decimal(source_read["refund_due_amount"]) == Decimal("0")
    assert Decimal(collection["remaining_entitlement"]) == Decimal("60.50")
    assert Decimal(collection["chain_refund_due_amount"]) == Decimal("0")
    rejected = await db_client.post(
        f"/api/v1/credit-notes/{credit['id']}/refunds",
        json={"payment_date": "2026-02-04", "amount": "1"},
    )
    assert rejected.status_code == 422


async def test_refund_partial_full_edit_all_fields_and_delete(
    db_client: AsyncClient,
) -> None:
    _, source = await _bootstrap(db_client)
    incoming = await _pay(db_client, source["id"], "121")
    credit = await _issue_full_credit(db_client, source["id"])
    method = await db_client.post(
        "/api/v1/payment-methods", json={"name": "Refund bank", "active": True}
    )
    assert method.status_code == 201, method.text

    partial = await db_client.post(
        f"/api/v1/credit-notes/{credit['id']}/refunds",
        json={
            "payment_date": "2026-02-04",
            "amount": "20",
            "reference": "R-1",
            "note": "partial",
        },
    )
    assert partial.status_code == 201, partial.text
    refund_id = partial.json()["items"][0]["id"]
    assert partial.json()["refunded_total"] == "20.000"
    assert partial.json()["remaining_entitlement"] == "101.000"

    full = await db_client.put(
        f"/api/v1/payments/{refund_id}",
        json={
            "payment_date": "2026-02-05",
            "amount": "121",
            "payment_method_id": method.json()["id"],
            "reference": "R-2",
            "note": "full",
        },
    )
    assert full.status_code == 200, full.text
    item = full.json()["refund"]["items"][0]
    assert (item["payment_date"], item["amount"]) == ("2026-02-05", "121.000")
    assert item["payment_method_name"] == "Refund bank"
    assert (item["reference"], item["note"]) == ("R-2", "full")
    assert full.json()["refund"]["chain_refund_due_amount"] == "0.000"

    stranded_edit = await db_client.put(
        f"/api/v1/payments/{incoming['items'][0]['id']}",
        json={"payment_date": "2026-02-02", "amount": "120"},
    )
    stranded_delete = await db_client.delete(
        f"/api/v1/payments/{incoming['items'][0]['id']}"
    )
    assert stranded_edit.status_code == stranded_delete.status_code == 422
    assert (
        await db_client.get(f"/api/v1/credit-notes/{credit['id']}/refunds")
    ).json()["refunded_total"] == "121.000"

    deleted = await db_client.delete(f"/api/v1/payments/{refund_id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["refund"]["refunded_total"] == "0.000"
    assert deleted.json()["refund"]["chain_refund_due_amount"] == "121.000"


async def test_multiple_credits_allocate_by_issued_at_not_document_date(
    db_client: AsyncClient,
) -> None:
    _, source = await _bootstrap(db_client)
    await _pay(db_client, source["id"], "60.50")
    first_issued = await _issue_partial_credit(
        db_client, source["id"], invoice_date="2026-02-10"
    )
    second_issued = await _issue_full_credit(
        db_client, source["id"], invoice_date="2026-02-03"
    )
    first = (
        await db_client.get(f"/api/v1/credit-notes/{first_issued['id']}/refunds")
    ).json()
    second = (
        await db_client.get(f"/api/v1/credit-notes/{second_issued['id']}/refunds")
    ).json()
    first_credit_read = (
        await db_client.get(f"/api/v1/invoices/{first_issued['id']}")
    ).json()
    second_credit_read = (
        await db_client.get(f"/api/v1/invoices/{second_issued['id']}")
    ).json()
    assert Decimal(first["chain_refund_due_amount"]) == Decimal("60.50")
    assert Decimal(first_credit_read["refund_due_amount"]) == Decimal("60.50")
    assert Decimal(second_credit_read["refund_due_amount"]) == Decimal("0")

    blocked_later = await db_client.post(
        f"/api/v1/credit-notes/{second_issued['id']}/refunds",
        json={"payment_date": "2026-02-11", "amount": "0.01"},
    )
    assert blocked_later.status_code == 422
    refund_first = await db_client.post(
        f"/api/v1/credit-notes/{first_issued['id']}/refunds",
        json={"payment_date": "2026-02-11", "amount": "60.50"},
    )
    assert refund_first.status_code == 201, refund_first.text
    assert refund_first.json()["chain_refund_due_amount"] == "0.000"
    assert second["refunded_total"] == "0.000"


async def test_rejects_before_credit_over_credit_over_chain_and_rolls_back(
    db_client: AsyncClient,
) -> None:
    _, source = await _bootstrap(db_client)
    incoming = await _pay(db_client, source["id"], "60.50")
    draft = await db_client.post(
        f"/api/v1/invoices/{source['id']}/credit-notes", json=_credit_payload()
    )
    assert draft.status_code == 201
    before_issue = await db_client.post(
        f"/api/v1/credit-notes/{draft.json()['id']}/refunds",
        json={"payment_date": "2026-02-04", "amount": "1"},
    )
    assert before_issue.status_code == 409
    assert before_issue.json()["detail"]["code"] == "REFUND_CREDIT_NOT_ISSUED"
    for status_value in ("DRAFT", "CANCELLED"):
        if status_value == "CANCELLED":
            cancelled = await db_client.post(
                f"/api/v1/invoices/{draft.json()['id']}/status",
                json={"status": "CANCELLED"},
            )
            assert cancelled.status_code == 200, cancelled.text
        collection_response = await db_client.get(
            f"/api/v1/credit-notes/{draft.json()['id']}/refunds"
        )
        assert collection_response.status_code == 409
        assert (
            collection_response.json()["detail"]["code"]
            == "REFUND_CREDIT_NOT_ISSUED"
        )
    restored = await db_client.post(
        f"/api/v1/invoices/{draft.json()['id']}/status", json={"status": "DRAFT"}
    )
    assert restored.status_code == 200, restored.text
    credit = await db_client.post(
        f"/api/v1/invoices/{draft.json()['id']}/status", json={"status": "SENT"}
    )
    assert credit.status_code == 200
    before_credit_date = await db_client.post(
        f"/api/v1/credit-notes/{draft.json()['id']}/refunds",
        json={"payment_date": "2026-02-02", "amount": "1"},
    )
    over_chain = await db_client.post(
        f"/api/v1/credit-notes/{draft.json()['id']}/refunds",
        json={"payment_date": "2026-02-04", "amount": "60.51"},
    )
    assert before_credit_date.status_code == over_chain.status_code == 422
    assert before_credit_date.json()["detail"]["code"] == "REFUND_DATE_BEFORE_CREDIT"
    assert over_chain.json()["detail"]["code"] == "REFUND_COVERAGE_EXCEEDED"
    collection = (
        await db_client.get(f"/api/v1/credit-notes/{draft.json()['id']}/refunds")
    ).json()
    assert collection["items"] == []
    assert collection["refunded_total"] == "0.000"
    assert (
        await db_client.get(f"/api/v1/payments/{incoming['items'][0]['id']}")
    ).status_code == 200


async def test_receipt_only_conversion_preserves_tax_and_supports_standard_refund(
    db_client: AsyncClient,
    runtime_session_maker: async_sessionmaker[AsyncSession],
    admin_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    quote = await _create_quote(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )
    await _accept_quote(db_client, quote["id"])
    deposit = await _record(db_client, quote["id"], "60.50", "2026-01-11")
    deposit_id = deposit["items"][0]["id"]
    assert deposit["items"][0]["tax_breakdown"]
    _, pending_chain = await _assert_quote_projection_matches_chain(db_client, quote["id"])
    assert Decimal(pending_chain["totals"]["charge_total"]) == Decimal("121")
    assert Decimal(pending_chain["totals"]["incoming_payment_total"]) == Decimal("60.50")
    assert Decimal(pending_chain["totals"]["due_amount"]) == Decimal("60.50")
    assert Decimal(pending_chain["totals"]["refund_due_amount"]) == Decimal("0")
    assert (
        await db_client.post(
            f"/api/v1/credit-notes/{deposit_id}/refunds",
            json={"payment_date": "2026-01-12", "amount": "1"},
        )
    ).status_code == 404
    assert (
        await db_client.get(
            f"/api/v1/payments/{deposit_id}/refund-confirmation/preview"
        )
    ).status_code == 404

    converted = await db_client.post(f"/api/v1/quotes/{quote['id']}/convert")
    assert converted.status_code == 201, converted.text
    await _assert_quote_projection_matches_chain(db_client, quote["id"])
    source = await db_client.post(
        f"/api/v1/invoices/{converted.json()['id']}/status", json={"status": "SENT"}
    )
    assert source.status_code == 200, source.text
    await _assert_quote_projection_matches_chain(db_client, quote["id"])
    credit = await _issue_full_credit(
        db_client, source.json()["id"], invoice_date=source.json()["invoice_date"]
    )
    _, credited_chain = await _assert_quote_projection_matches_chain(db_client, quote["id"])
    assert Decimal(credited_chain["totals"]["charge_total"]) == Decimal("121")
    assert Decimal(credited_chain["totals"]["credit_total"]) == Decimal("121")
    assert Decimal(credited_chain["totals"]["refund_due_amount"]) == Decimal("60.50")
    baseline = (
        await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=1")
    ).json()
    # A tax-bearing receipt cannot be relabelled into a Refund by raw SQL.  The
    # payment trigger rejects the transition before legacy tax rows can leak
    # into the refund domain.
    async with runtime_session_maker() as session:
        await set_rls_company(session, seeds["company_id"])
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "UPDATE payment SET direction = 'REFUND', credit_note_id = :credit_id, "
                    "invoice_id = NULL, quote_id = NULL WHERE id = :payment_id"
                ),
                {"credit_id": credit["id"], "payment_id": deposit_id},
            )
        await session.rollback()
    partial_refund = await db_client.post(
        f"/api/v1/credit-notes/{credit['id']}/refunds",
        json={"payment_date": "2026-12-31", "amount": "30"},
    )
    assert partial_refund.status_code == 201, partial_refund.text
    _, partial_refund_chain = await _assert_quote_projection_matches_chain(db_client, quote["id"])
    assert Decimal(partial_refund_chain["totals"]["refund_total"]) == Decimal("30")
    assert Decimal(partial_refund_chain["totals"]["refund_due_amount"]) == Decimal("30.50")
    refund = await db_client.post(
        f"/api/v1/credit-notes/{credit['id']}/refunds",
        json={"payment_date": "2026-12-31", "amount": "30.50"},
    )
    assert refund.status_code == 201, refund.text
    refund_id = refund.json()["items"][0]["id"]
    assert refund.json()["items"][0]["tax_breakdown"] == []
    _, refunded_chain = await _assert_quote_projection_matches_chain(db_client, quote["id"])
    assert Decimal(refunded_chain["totals"]["refund_total"]) == Decimal("60.50")
    assert Decimal(refunded_chain["totals"]["refund_due_amount"]) == Decimal("0")
    settled_report = (
        await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=1")
    ).json()
    # Refund cash remains tax-neutral: the authoritative boxes, totals and
    # immutable tax events do not move.  The receipt-only advisory does move
    # from warning to info once the linked Standard becomes fully settled.
    tax_projection_keys = ("boxes", "totals", "event_rows", "correction_warnings")
    assert {key: settled_report[key] for key in tax_projection_keys} == {
        key: baseline[key] for key in tax_projection_keys
    }
    assert len(baseline["warnings"]) == 1
    assert "without a fully settled issued Standard" in baseline["warnings"][0]
    assert baseline["infos"] == []
    assert settled_report["warnings"] == []
    assert len(settled_report["infos"]) == 1
    assert "linked issued Standard is settled" in settled_report["infos"][0]
    async with runtime_session_maker() as session:
        await set_rls_company(session, seeds["company_id"])
        assert await session.scalar(
            text("SELECT count(*) FROM payment_tax WHERE payment_id = :id"),
            {"id": deposit_id},
        ) > 0
        assert await session.scalar(
            text("SELECT count(*) FROM payment_tax WHERE payment_id = :id"),
            {"id": refund_id},
        ) == 0
    async with runtime_session_maker() as session:
        # The runtime role is not the table owner.  With no tenant GUC it sees
        # neither the parent payment nor its child tax snapshots.
        assert await session.scalar(
            text("SELECT count(*) FROM payment WHERE id = :id"), {"id": deposit_id}
        ) == 0
        assert await session.scalar(
            text("SELECT count(*) FROM payment_tax WHERE payment_id = :id"),
            {"id": deposit_id},
        ) == 0

    # The same runtime-role HTTP endpoint returns its tenant's compact cash,
    # then hides the exact UUID once the Quote belongs to another tenant.
    foreign_company_id = uuid.uuid4()
    async with admin_session_maker() as session:
        await session.execute(
            text("INSERT INTO company (id, name, base_currency) VALUES (:id, 'Foreign Co', 'EUR')"),
            {"id": foreign_company_id},
        )
        await session.execute(
            text("UPDATE quote SET company_id = :company_id WHERE id = :quote_id"),
            {"company_id": foreign_company_id, "quote_id": quote["id"]},
        )
        await session.commit()
    foreign_read = await db_client.get(f"/api/v1/quotes/{quote['id']}")
    assert foreign_read.status_code == 404, foreign_read.text


async def test_refund_input_errors_use_stable_detail_objects(
    db_client: AsyncClient,
) -> None:
    """POST Refunds and their generic PUT route never expose list/prose errors."""
    _, source = await _bootstrap(db_client)
    await _pay(db_client, source["id"], "121")
    credit = await _issue_full_credit(db_client, source["id"])
    refund_url = f"/api/v1/credit-notes/{credit['id']}/refunds"
    missing_method = str(uuid.uuid4())

    for payload in (
        {"payment_date": "2026-02-04", "amount": "0"},
        {"payment_date": "not-a-date", "amount": "1"},
        {
            "payment_date": "2026-02-04",
            "amount": "1",
            "payment_method_id": missing_method,
        },
    ):
        response = await db_client.post(refund_url, json=payload)
        assert response.status_code == 422, response.text
        assert set(response.json()["detail"]) == {"code", "message"}
        expected = (
            "REFUND_PAYMENT_METHOD_INVALID"
            if "payment_method_id" in payload
            else "REFUND_INVALID_INPUT"
        )
        assert response.json()["detail"]["code"] == expected

    created = await db_client.post(
        refund_url, json={"payment_date": "2026-02-04", "amount": "1"}
    )
    assert created.status_code == 201, created.text
    refund_id = created.json()["items"][0]["id"]
    for payload in (
        {"payment_date": "2026-02-04", "amount": "0"},
        {"payment_date": "not-a-date", "amount": "1"},
        {
            "payment_date": "2026-02-04",
            "amount": "1",
            "payment_method_id": missing_method,
        },
    ):
        response = await db_client.put(f"/api/v1/payments/{refund_id}", json=payload)
        assert response.status_code == 422, response.text
        assert set(response.json()["detail"]) == {"code", "message"}
        expected = (
            "REFUND_PAYMENT_METHOD_INVALID"
            if "payment_method_id" in payload
            else "PAYMENT_INVALID_INPUT"
        )
        assert response.json()["detail"]["code"] == expected


async def test_receipt_only_draft_chain_pairs_deposit_with_pending_basis(
    db_client: AsyncClient,
) -> None:
    """Receipt-only cash never becomes a refund due while its Standard is pending."""
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    quote = await _create_quote(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )
    await _accept_quote(db_client, quote["id"])
    await _record(db_client, quote["id"], "40", "2026-01-11")

    async def assert_receipt_totals() -> None:
        quote_read, chain = await _assert_quote_projection_matches_chain(db_client, quote["id"])
        totals = chain["totals"]
        for field, expected in {
            "charge_total": "121",
            "incoming_payment_total": "40",
            "due_amount": "81",
            "refund_due_amount": "0",
            "base_charge_total": "121",
            "base_incoming_payment_total": "40",
            "base_due_amount": "81",
            "base_refund_due_amount": "0",
        }.items():
            assert Decimal(totals[field]) == Decimal(expected)
        assert Decimal(quote_read["remaining_amount"]) == Decimal("81")

    await assert_receipt_totals()
    converted = await db_client.post(f"/api/v1/quotes/{quote['id']}/convert")
    assert converted.status_code == 201, converted.text
    source_id = converted.json()["id"]
    source = await db_client.get(f"/api/v1/invoices/{source_id}")
    assert source.status_code == 200
    assert Decimal(source.json()["due_amount"]) == Decimal("81")
    assert Decimal(source.json()["refund_due_amount"]) == Decimal("0")
    await assert_receipt_totals()

    issued = await db_client.post(f"/api/v1/invoices/{source_id}/status", json={"status": "SENT"})
    assert issued.status_code == 200, issued.text
    await assert_receipt_totals()

    # A fresh receipt-only chain may be converted again after the pending DRAFT
    # is deleted; its retained deposit remains deferred settlement cash.
    second_quote = await _create_quote(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )
    await _accept_quote(db_client, second_quote["id"])
    await _record(db_client, second_quote["id"], "40", "2026-01-11")
    second_converted = await db_client.post(f"/api/v1/quotes/{second_quote['id']}/convert")
    assert second_converted.status_code == 201, second_converted.text
    deleted = await db_client.delete(f"/api/v1/invoices/{second_converted.json()['id']}")
    assert deleted.status_code == 204, deleted.text
    _, after_delete = await _assert_quote_projection_matches_chain(
        db_client, second_quote["id"]
    )
    totals = after_delete["totals"]
    assert Decimal(totals["due_amount"]) == Decimal("81")
    assert Decimal(totals["refund_due_amount"]) == Decimal("0")
    assert Decimal(totals["base_due_amount"]) == Decimal("81")
    assert Decimal(totals["base_refund_due_amount"]) == Decimal("0")

    direct_quote = await _create_quote(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )
    await _accept_quote(db_client, direct_quote["id"])
    direct = await db_client.post(f"/api/v1/quotes/{direct_quote['id']}/convert")
    assert direct.status_code == 201, direct.text
    direct_chain = await db_client.get(f"/api/v1/quotes/{direct_quote['id']}/document-chain")
    assert direct_chain.status_code == 200
    assert Decimal(direct_chain.json()["totals"]["charge_total"]) == Decimal("0")
    assert Decimal(direct_chain.json()["totals"]["due_amount"]) == Decimal("0")
    _, direct_chain_payload = await _assert_quote_projection_matches_chain(
        db_client, direct_quote["id"]
    )
    assert Decimal(direct_chain_payload["totals"]["charge_total"]) == Decimal("0")

    formal_quote = await _additional_formal_quote(db_client, seeds)
    formal_draft = await db_client.post(
        f"/api/v1/quotes/{formal_quote['id']}/advance-invoices",
        json={
            "input_mode": "PERCENTAGE",
            "percentage": "50",
            "invoice_date": "2026-02-01",
        },
    )
    assert formal_draft.status_code == 201, formal_draft.text
    formal_chain = await db_client.get(f"/api/v1/quotes/{formal_quote['id']}/document-chain")
    assert formal_chain.status_code == 200
    assert Decimal(formal_chain.json()["totals"]["charge_total"]) == Decimal("0")
    assert Decimal(formal_chain.json()["totals"]["due_amount"]) == Decimal("0")
    _, formal_chain_payload = await _assert_quote_projection_matches_chain(
        db_client, formal_quote["id"]
    )
    assert Decimal(formal_chain_payload["totals"]["charge_total"]) == Decimal("0")

    issued_advance_response = await db_client.post(
        f"/api/v1/invoices/{formal_draft.json()['id']}/status", json={"status": "SENT"}
    )
    assert issued_advance_response.status_code == 200, issued_advance_response.text
    issued_advance = issued_advance_response.json()
    final_draft = await db_client.post(
        f"/api/v1/quotes/{formal_quote['id']}/final-invoice",
        json={"invoice_date": "2026-03-01"},
    )
    assert final_draft.status_code == 201, final_draft.text
    _, formal_final_draft_chain = await _assert_quote_projection_matches_chain(
        db_client, formal_quote["id"]
    )
    # The issued Advance is the only charge: the Final DRAFT is excluded.
    assert Decimal(formal_final_draft_chain["totals"]["charge_total"]) == Decimal(
        issued_advance["payable_before_payments"]
    )


async def test_formal_advance_final_chain_revalidates_all_incoming_sources(
    db_client: AsyncClient,
) -> None:
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    quote = await _additional_formal_quote(db_client, seeds)
    advance = await _issued_advance(db_client, quote["id"], "50")
    final_draft = await db_client.post(
        f"/api/v1/quotes/{quote['id']}/final-invoice",
        json={"invoice_date": "2026-03-01"},
    )
    assert final_draft.status_code == 201, final_draft.text
    final = await db_client.post(
        f"/api/v1/invoices/{final_draft.json()['id']}/status", json={"status": "SENT"}
    )
    assert final.status_code == 200, final.text
    advance_cash = await _pay(db_client, advance["id"], "60.50")
    final_cash = await _pay(
        db_client, final.json()["id"], "60.50", payment_date="2026-03-02"
    )
    credit = await _issue_full_credit(
        db_client, advance["id"], invoice_date="2026-03-03"
    )
    refund = await db_client.post(
        f"/api/v1/credit-notes/{credit['id']}/refunds",
        json={"payment_date": "2026-03-04", "amount": "60.50"},
    )
    assert refund.status_code == 201, refund.text

    # The Final source has no local Credit, but its cash is still part of the
    # same Formal chain and may not be removed while it backs the refund.
    final_payment_id = final_cash["items"][0]["id"]
    rejected_edit = await db_client.put(
        f"/api/v1/payments/{final_payment_id}",
        json={"payment_date": "2026-03-02", "amount": "60"},
    )
    rejected_delete = await db_client.delete(f"/api/v1/payments/{final_payment_id}")
    assert rejected_edit.status_code == rejected_delete.status_code == 422
    assert (
        await db_client.get(f"/api/v1/payments/{advance_cash['items'][0]['id']}")
    ).status_code == 200
    chain = await db_client.get(
        f"/api/v1/invoices/{final.json()['id']}/document-chain"
    )
    assert chain.status_code == 200
    assert Decimal(chain.json()["totals"]["refund_total"]) == Decimal("60.50")
    assert Decimal(chain.json()["totals"]["charge_total"]) == Decimal("121")


async def test_source_local_refund_is_not_backed_by_another_source_after_delete(
    db_client: AsyncClient,
) -> None:
    """Deleting A's cash cannot leave its Refund backed only by B's cash."""
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    quote = await _additional_formal_quote(db_client, seeds)
    advance = await _issued_advance(db_client, quote["id"], "50")
    final_draft = await db_client.post(
        f"/api/v1/quotes/{quote['id']}/final-invoice",
        json={"invoice_date": "2026-03-01"},
    )
    assert final_draft.status_code == 201, final_draft.text
    final = await db_client.post(
        f"/api/v1/invoices/{final_draft.json()['id']}/status", json={"status": "SENT"}
    )
    assert final.status_code == 200, final.text
    advance_cash = await _pay(db_client, advance["id"], "60.50")
    await _pay(db_client, final.json()["id"], "60.50", payment_date="2026-03-02")
    advance_credit = await _issue_full_credit(
        db_client, advance["id"], invoice_date="2026-03-03"
    )
    await _issue_full_credit(
        db_client, final.json()["id"], invoice_date="2026-03-04"
    )
    refund = await db_client.post(
        f"/api/v1/credit-notes/{advance_credit['id']}/refunds",
        json={"payment_date": "2026-03-05", "amount": "60.50"},
    )
    assert refund.status_code == 201, refund.text

    payment_id = advance_cash["items"][0]["id"]
    rejected = await db_client.delete(f"/api/v1/payments/{payment_id}")
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "REFUND_COVERAGE_EXCEEDED"
    persisted_payment = await db_client.get(f"/api/v1/payments/{payment_id}")
    assert persisted_payment.status_code == 200
    assert persisted_payment.json()["amount"] == "60.500"
    collection = await db_client.get(
        f"/api/v1/credit-notes/{advance_credit['id']}/refunds"
    )
    assert collection.status_code == 200
    assert collection.json()["refunded_total"] == "60.500"
    chain = await db_client.get(f"/api/v1/invoices/{advance['id']}/document-chain")
    assert chain.status_code == 200
    assert not any(
        event["event_type"] == "INVOICE_PAYMENT_DELETED"
        and event["metadata"].get("payment_id") == payment_id
        for event in chain.json()["events"]
    )


async def test_formal_refund_entitlement_is_source_local_before_issue_order(
    db_client: AsyncClient,
) -> None:
    """An earlier Advance Credit cannot consume refund cash owned by Final."""
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    quote = await _additional_formal_quote(db_client, seeds)
    advance = await _issued_advance(db_client, quote["id"], "50")
    final_draft = await db_client.post(
        f"/api/v1/quotes/{quote['id']}/final-invoice",
        json={"invoice_date": "2026-03-01"},
    )
    final = await db_client.post(
        f"/api/v1/invoices/{final_draft.json()['id']}/status", json={"status": "SENT"}
    )
    assert final.status_code == 200, final.text
    await _pay(db_client, final.json()["id"], "60.50", payment_date="2026-03-02")
    advance_credit = await _issue_full_credit(
        db_client, advance["id"], invoice_date="2026-03-03"
    )
    final_credit = await _issue_full_credit(
        db_client, final.json()["id"], invoice_date="2026-03-04"
    )
    advance_read = (
        await db_client.get(f"/api/v1/invoices/{advance_credit['id']}")
    ).json()
    final_read = (
        await db_client.get(f"/api/v1/invoices/{final_credit['id']}")
    ).json()
    assert Decimal(advance_read["refund_due_amount"]) == Decimal("0")
    assert Decimal(final_read["refund_due_amount"]) == Decimal("60.50")
    blocked = await db_client.post(
        f"/api/v1/credit-notes/{advance_credit['id']}/refunds",
        json={"payment_date": "2026-03-05", "amount": "0.01"},
    )
    assert blocked.status_code == 422
    refunded = await db_client.post(
        f"/api/v1/credit-notes/{final_credit['id']}/refunds",
        json={"payment_date": "2026-03-05", "amount": "60.50"},
    )
    assert refunded.status_code == 201, refunded.text
    assert refunded.json()["chain_refund_due_amount"] == "0.000"


async def _formal_three_source_shared_capacity(
    db_client: AsyncClient,
    admin_session_maker: async_sessionmaker[AsyncSession],
) -> tuple[dict, dict, dict]:
    """Build three Formal sources with one shared 30.25 refundable capacity."""
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    quote = await _additional_formal_quote(db_client, seeds)
    advances = [
        await _issued_advance(db_client, quote["id"], "25") for _ in range(2)
    ]
    final_draft = await db_client.post(
        f"/api/v1/quotes/{quote['id']}/final-invoice",
        json={"invoice_date": "2026-03-01"},
    )
    final = await db_client.post(
        f"/api/v1/invoices/{final_draft.json()['id']}/status", json={"status": "SENT"}
    )
    assert final.status_code == 200, final.text
    for advance in advances:
        await _pay(db_client, advance["id"], "30.25")

    # Deliberately issue the Credit for the lexically later source UUID first:
    # source/document UUID ordering must never replace global Credit issue order.
    source_uuid_first, source_uuid_last = sorted(advances, key=lambda item: item["id"])
    issued_early = await _issue_full_credit(
        db_client, source_uuid_last["id"], invoice_date="2026-03-09"
    )
    issued_late = await _issue_full_credit(
        db_client, source_uuid_first["id"], invoice_date="2026-03-02"
    )
    final_credit = await _issue_gross_credit(
        db_client,
        final.json()["id"],
        gross="30.25",
        invoice_date="2026-03-04",
    )

    # Persist explicit raw issue snapshots so the regression never depends on
    # wall-clock resolution or Credit document dates.
    async with admin_session_maker() as session:
        for credit, issued_at in (
            (issued_early, datetime(2026, 3, 10, 8, tzinfo=UTC)),
            (issued_late, datetime(2026, 3, 10, 9, tzinfo=UTC)),
            (final_credit, datetime(2026, 3, 10, 10, tzinfo=UTC)),
        ):
            await session.execute(
                text("UPDATE invoice SET issued_at = :issued_at WHERE id = :credit_id"),
                {"issued_at": issued_at, "credit_id": credit["id"]},
            )
        await session.commit()
    return issued_early, issued_late, final_credit


async def test_formal_global_credit_order_ignores_uuid_and_request_order(
    db_client: AsyncClient,
    admin_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A later cross-source Credit cannot take the sole cap by POSTing first."""
    issued_early, issued_late, final_credit = (
        await _formal_three_source_shared_capacity(db_client, admin_session_maker)
    )

    blocked_later = await db_client.post(
        f"/api/v1/credit-notes/{issued_late['id']}/refunds",
        json={"payment_date": "2026-03-11", "amount": "30.25"},
    )
    assert blocked_later.status_code == 422
    assert (
        await db_client.get(f"/api/v1/invoices/{issued_early['id']}")
    ).json()["refund_due_amount"] == "30.250"
    for blocked in (issued_late, final_credit):
        read = (await db_client.get(f"/api/v1/invoices/{blocked['id']}")).json()
        assert read["refund_due_amount"] == "0.000"
        assert read["base_refund_due_amount"] == "0.000"

    refunded = await db_client.post(
        f"/api/v1/credit-notes/{issued_early['id']}/refunds",
        json={"payment_date": "2026-03-11", "amount": "30.25"},
    )
    assert refunded.status_code == 201, refunded.text
    assert refunded.json()["chain_refund_due_amount"] == "0.000"
    assert refunded.json()["base_chain_refund_due_amount"] == "0.000"


async def test_refund_put_cannot_move_earlier_credit_capacity_to_later_credit(
    db_client: AsyncClient,
) -> None:
    """A later Credit remains capped at its issue-order allocation after PUTs."""
    _, source = await _bootstrap(db_client)
    await _pay(db_client, source["id"], "81")
    first = await _issue_gross_credit(
        db_client, source["id"], gross="50", invoice_date="2026-02-03"
    )
    second = await _issue_gross_credit(
        db_client, source["id"], gross="50", invoice_date="2026-02-04"
    )
    first_refund = await db_client.post(
        f"/api/v1/credit-notes/{first['id']}/refunds",
        json={"payment_date": "2026-02-05", "amount": "50"},
    )
    assert first_refund.status_code == 201, first_refund.text
    second_refund = await db_client.post(
        f"/api/v1/credit-notes/{second['id']}/refunds",
        json={"payment_date": "2026-02-05", "amount": "10"},
    )
    assert second_refund.status_code == 201, second_refund.text
    first_refund_id = first_refund.json()["items"][0]["id"]
    second_refund_id = second_refund.json()["items"][0]["id"]

    reduced_first = await db_client.put(
        f"/api/v1/payments/{first_refund_id}",
        json={"payment_date": "2026-02-06", "amount": "30"},
    )
    assert reduced_first.status_code == 200, reduced_first.text
    rejected_later = await db_client.put(
        f"/api/v1/payments/{second_refund_id}",
        json={"payment_date": "2026-02-06", "amount": "30"},
    )
    assert rejected_later.status_code == 422
    assert rejected_later.json()["detail"]["code"] == "REFUND_COVERAGE_EXCEEDED"
    first_collection = await db_client.get(
        f"/api/v1/credit-notes/{first['id']}/refunds"
    )
    second_collection = await db_client.get(
        f"/api/v1/credit-notes/{second['id']}/refunds"
    )
    assert first_collection.json()["refunded_total"] == "30.000"
    assert second_collection.json()["refunded_total"] == "10.000"
    chain = await db_client.get(f"/api/v1/invoices/{source['id']}/document-chain")
    assert chain.status_code == 200
    assert not any(
        event["event_type"] == "REFUND_UPDATED"
        and event["metadata"].get("payment_id") == second_refund_id
        for event in chain.json()["events"]
    )


async def test_formal_global_credit_order_is_stable_under_concurrent_posts(
    db_client: AsyncClient,
    admin_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    issued_early, issued_late, _ = await _formal_three_source_shared_capacity(
        db_client, admin_session_maker
    )

    early, late = await asyncio.wait_for(
        asyncio.gather(
            db_client.post(
                f"/api/v1/credit-notes/{issued_early['id']}/refunds",
                json={"payment_date": "2026-03-11", "amount": "30.25"},
            ),
            db_client.post(
                f"/api/v1/credit-notes/{issued_late['id']}/refunds",
                json={"payment_date": "2026-03-11", "amount": "30.25"},
            ),
        ),
        timeout=5,
    )
    assert early.status_code == 201, early.text
    assert late.status_code in {409, 422}, late.text
    early_collection = (
        await db_client.get(f"/api/v1/credit-notes/{issued_early['id']}/refunds")
    ).json()
    late_collection = (
        await db_client.get(f"/api/v1/credit-notes/{issued_late['id']}/refunds")
    ).json()
    assert early_collection["refunded_total"] == "30.250"
    assert late_collection["refunded_total"] == "0.000"


async def test_global_filters_read_chain_events_and_step9_boundary(
    db_client: AsyncClient,
) -> None:
    _, source = await _bootstrap(db_client)
    await _pay(db_client, source["id"], "121")
    credit = await _issue_full_credit(db_client, source["id"])
    created = await db_client.post(
        f"/api/v1/credit-notes/{credit['id']}/refunds",
        json={"payment_date": "2026-02-04", "amount": "20", "note": "event-a"},
    )
    assert created.status_code == 201, created.text
    refund_id = created.json()["items"][0]["id"]
    updated = await db_client.put(
        f"/api/v1/payments/{refund_id}",
        json={"payment_date": "2026-02-05", "amount": "21", "note": "event-b"},
    )
    assert updated.status_code == 200

    single = await db_client.get(f"/api/v1/payments/{refund_id}")
    assert single.status_code == 200
    assert single.json()["origin_type"] == "CREDIT_NOTE"
    assert single.json()["direction"] == "REFUND"
    assert single.json()["credit_note_number"] == credit["invoice_number"]
    for params in (
        {"direction": "REFUND"},
        {"document_kind": "CREDIT_NOTE"},
        {"q": credit["invoice_number"]},
    ):
        listed = await db_client.get("/api/v1/payments", params=params)
        assert listed.status_code == 200, listed.text
        assert [item["id"] for item in listed.json()["items"]] == [refund_id]

    chain = (
        await db_client.get(f"/api/v1/invoices/{source['id']}/document-chain")
    ).json()
    assert (
        "CREDIT_NOTE_TO_REFUND",
        credit["id"],
        refund_id,
    ) in {
        (row["relation_type"], row["from_node_id"], row["to_node_id"])
        for row in chain["relations"]
    }
    refund_events = [
        row["event_type"]
        for row in chain["events"]
        if row["metadata"].get("payment_id") == refund_id
    ]
    assert refund_events == ["REFUND_CREATED", "REFUND_UPDATED"]
    preview = await db_client.get(
        f"/api/v1/payments/{refund_id}/refund-confirmation/preview"
    )
    assert preview.status_code == 200
    assert preview.content.startswith(b"%PDF-")
    assert (await db_client.get(f"/api/v1/payments/{refund_id}/artifacts")).json()["items"] == []
    download = await db_client.get(f"/api/v1/payments/{refund_id}/refund-confirmation")
    assert download.status_code == 200
    artifacts = (await db_client.get(f"/api/v1/payments/{refund_id}/artifacts")).json()["items"]
    assert len(artifacts) == 1
    artifact = await db_client.get(
        f"/api/v1/payments/{refund_id}/artifacts/{artifacts[0]['id']}"
    )
    assert artifact.status_code == 200
    assert artifact.content == download.content
    send = await db_client.post(
        f"/api/v1/payments/{refund_id}/send-refund-confirmation",
        json={"to": "customer@example.com"},
    )
    assert send.status_code == 400


async def test_runtime_triggers_rls_and_no_refund_payment_tax(
    db_client: AsyncClient,
    runtime_session_maker: async_sessionmaker[AsyncSession],
    admin_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    seeds, source = await _bootstrap(db_client)
    incoming = await _pay(db_client, source["id"], "121")
    unissued = await db_client.post(
        f"/api/v1/invoices/{source['id']}/credit-notes", json=_credit_payload()
    )
    assert unissued.status_code == 201
    issued = await _issue_full_credit(db_client, source["id"])
    company_id = uuid.UUID(seeds["company_id"])

    async def raw_refund(credit_id: object) -> None:
        async with runtime_session_maker() as session:
            await set_rls_company(session, company_id)
            await session.execute(
                text(
                    "INSERT INTO payment "
                    "(id, company_id, credit_note_id, direction, payment_date, amount, "
                    "base_amount, currency, exchange_rate) VALUES "
                    "(:id, :company_id, :credit_id, 'REFUND', '2026-02-04', 1, 1, 'EUR', 1)"
                ),
                {
                    "id": uuid.uuid4(),
                    "company_id": company_id,
                    "credit_id": credit_id,
                },
            )
            await session.flush()

    with pytest.raises(DBAPIError):
        await raw_refund(source["id"])
    with pytest.raises(DBAPIError):
        await raw_refund(unissued.json()["id"])

    async with runtime_session_maker() as session:
        await set_rls_company(session, company_id)
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "UPDATE payment SET credit_note_id = :credit_id "
                    "WHERE id = :payment_id"
                ),
                {
                    "credit_id": issued["id"],
                    "payment_id": incoming["items"][0]["id"],
                },
            )
        await session.rollback()

    # The same-company issued Credit context is valid at the DB boundary.  A
    # rollback keeps this raw constraint probe outside authoritative service data.
    async with runtime_session_maker() as session:
        await set_rls_company(session, company_id)
        await session.execute(
            text(
                "INSERT INTO payment "
                "(id, company_id, credit_note_id, direction, payment_date, amount, "
                "base_amount, currency, exchange_rate) VALUES "
                "(:id, :company_id, :credit_id, 'REFUND', '2026-02-04', 1, 1, 'EUR', 1)"
            ),
            {"id": uuid.uuid4(), "company_id": company_id, "credit_id": issued["id"]},
        )
        await session.flush()
        await session.rollback()

    refund = await db_client.post(
        f"/api/v1/credit-notes/{issued['id']}/refunds",
        json={"payment_date": "2026-02-04", "amount": "1"},
    )
    assert refund.status_code == 201
    refund_id = refund.json()["items"][0]["id"]
    async with runtime_session_maker() as session:
        await set_rls_company(session, company_id)
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "INSERT INTO payment_tax "
                    "(id, payment_id, vat_rate_label, vat_rate_percent, "
                    "vat_treatment_code, vat_treatment_effect, "
                    "vat_treatment_requires_icp, taxable_amount, vat_amount, gross_amount, "
                    "base_taxable_amount, base_vat_amount, base_gross_amount, "
                    "bucket_key, sort_order) "
                    "VALUES (:id, :payment_id, 'Invalid', 0, 'NL_DOMESTIC', 'DOMESTIC', "
                    "false, 1, 0, 1, 1, 0, 1, 'invalid', 0)"
                ),
                {"id": uuid.uuid4(), "payment_id": refund_id},
            )
        await session.rollback()

    foreign_company_id = uuid.uuid4()
    async with admin_session_maker() as session:
        await session.execute(
            text("INSERT INTO company (id, name, base_currency) VALUES (:id, 'Foreign', 'EUR')"),
            {"id": foreign_company_id},
        )
        await session.commit()
    async with runtime_session_maker() as session:
        await set_rls_company(session, foreign_company_id)
        assert await session.scalar(
            text("SELECT count(*) FROM payment WHERE id = :id"), {"id": refund_id}
        ) == 0
    async with runtime_session_maker() as session:
        assert await session.scalar(
            text("SELECT count(*) FROM payment WHERE id = :id"), {"id": refund_id}
        ) == 0


async def test_runtime_dashboard_and_vat_share_payment_tax_context_and_refunds_are_noops(
    db_client: AsyncClient,
    runtime_session_maker: async_sessionmaker[AsyncSession],
    admin_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real runtime role sees receipt VAT identically via both report paths."""
    class _FrozenAugustDatetime(datetime):
        @classmethod
        def now(cls, tz: object | None = None) -> _FrozenAugustDatetime:
            return cls(2026, 8, 30, tzinfo=tz)

    class _FrozenAugustDate(date):
        @classmethod
        def today(cls) -> _FrozenAugustDate:
            return cls(2026, 8, 30)

    monkeypatch.setattr(dashboard_service, "datetime", _FrozenAugustDatetime)
    monkeypatch.setattr(invoice_service, "date", _FrozenAugustDate)
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    quote = await _create_quote(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )
    await _accept_quote(db_client, quote["id"])
    deposit = await _record(db_client, quote["id"], "60.50", "2026-08-11")
    deposit_id = deposit["items"][0]["id"]

    async def _assert_report_equality(expected_vat: str) -> tuple[dict, dict]:
        vat = await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=3")
        dashboard = await db_client.get("/api/v1/reports/dashboard?year=2026")
        assert vat.status_code == dashboard.status_code == 200
        vat_payload = vat.json()
        dashboard_payload = dashboard.json()
        assert (
            vat_payload["totals"]["net_payable_or_refundable"]["vat"]
            == dashboard_payload["kpi"]["current_quarter_vat_payable"]
            == expected_vat
        )
        return vat_payload, dashboard_payload

    # The Q3 receipt-only deposit is recognised once by both report paths.
    await _assert_report_equality("10.50")
    converted = await db_client.post(f"/api/v1/quotes/{quote['id']}/convert")
    assert converted.status_code == 201, converted.text
    assert converted.json()["invoice_date"] == "2026-08-30"
    source = await db_client.post(
        f"/api/v1/invoices/{converted.json()['id']}/status", json={"status": "SENT"}
    )
    assert source.status_code == 200, source.text
    credit = await _issue_full_credit(
        db_client, source.json()["id"], invoice_date=source.json()["invoice_date"]
    )
    # Step 8 gives the issued Standard Credit its own negative invoice-date
    # event.  The receipt-only PaymentTax path is still counted once, then
    # offset by the converted Standard; the Credit now correctly brings Q3
    # back to zero before any Refund cash is recorded.
    baseline_vat, baseline_dashboard = await _assert_report_equality("0.00")

    refund = await db_client.post(
        f"/api/v1/credit-notes/{credit['id']}/refunds",
        json={"payment_date": credit["invoice_date"], "amount": "20"},
    )
    assert refund.status_code == 201, refund.text
    refund_id = refund.json()["items"][0]["id"]
    assert await _assert_report_equality("0.00") == (baseline_vat, baseline_dashboard)
    updated = await db_client.put(
        f"/api/v1/payments/{refund_id}",
        json={"payment_date": credit["invoice_date"], "amount": "30"},
    )
    assert updated.status_code == 200, updated.text
    assert await _assert_report_equality("0.00") == (baseline_vat, baseline_dashboard)
    deleted = await db_client.delete(f"/api/v1/payments/{refund_id}")
    assert deleted.status_code == 200, deleted.text
    assert await _assert_report_equality("0.00") == (baseline_vat, baseline_dashboard)

    foreign_company_id = uuid.uuid4()
    async with admin_session_maker() as session:
        await session.execute(
            text("INSERT INTO company (id, name, base_currency) VALUES (:id, 'Foreign', 'EUR')"),
            {"id": foreign_company_id},
        )
        await session.commit()
    async with runtime_session_maker() as session:
        await set_rls_company(session, foreign_company_id)
        assert await session.scalar(
            text("SELECT count(*) FROM payment WHERE id = :id"), {"id": deposit_id}
        ) == 0
        assert await session.scalar(
            text("SELECT count(*) FROM payment_tax WHERE payment_id = :id"),
            {"id": deposit_id},
        ) == 0


async def test_runtime_payment_tax_and_refund_trigger_races_are_serialized(
    db_client: AsyncClient,
    runtime_db_engine: AsyncEngine,
    runtime_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Raw runtime writes cannot commit a Refund together with PaymentTax."""
    seeds, source = await _bootstrap(db_client)
    first = await _pay(db_client, source["id"], "1")
    second = await _pay(db_client, source["id"], "1")
    credit = await _issue_full_credit(db_client, source["id"])
    company_id = uuid.UUID(seeds["company_id"])
    first_id = first["items"][-1]["id"]
    second_id = second["items"][-1]["id"]

    race_engine = create_async_engine(
        runtime_db_engine.url.render_as_string(hide_password=False),
        pool_size=2,
        max_overflow=0,
    )
    race_sessions = async_sessionmaker(race_engine, expire_on_commit=False)

    async def _insert_tax(session: AsyncSession, payment_id: object) -> None:
        await session.execute(
            text(
                "INSERT INTO payment_tax "
                "(id, payment_id, vat_rate_label, vat_rate_percent, "
                "vat_treatment_code, vat_treatment_effect, "
                "vat_treatment_requires_icp, taxable_amount, vat_amount, gross_amount, "
                "base_taxable_amount, base_vat_amount, base_gross_amount, "
                "bucket_key, sort_order) "
                "VALUES (:id, :payment_id, 'Legal incoming', 21, 'NL_DOMESTIC', "
                "'APPLY_RATE', false, 1, .21, 1.21, 1, .21, 1.21, 'race', 0)"
            ),
            {"id": uuid.uuid4(), "payment_id": payment_id},
        )

    async def _set_timeout(session: AsyncSession) -> None:
        await session.execute(text("SET LOCAL lock_timeout = '3s'"))

    async def _set_refund(session: AsyncSession, payment_id: object) -> None:
        await session.execute(
            text(
                "UPDATE payment SET direction = 'REFUND', credit_note_id = :credit_id, "
                "invoice_id = NULL, quote_id = NULL WHERE id = :payment_id"
            ),
            {"credit_id": credit["id"], "payment_id": payment_id},
        )

    async def _final_state(payment_id: object) -> tuple[str, int]:
        async with runtime_session_maker() as session:
            await set_rls_company(session, company_id)
            direction = await session.scalar(
                text("SELECT direction::text FROM payment WHERE id = :id"), {"id": payment_id}
            )
            tax_rows = await session.scalar(
                text("SELECT count(*) FROM payment_tax WHERE payment_id = :id"),
                {"id": payment_id},
            )
            assert isinstance(direction, str)
            assert isinstance(tax_rows, int)
            return direction, tax_rows

    async def _tax_first(payment_id: object) -> None:
        tax_locked = asyncio.Event()
        release_tax = asyncio.Event()
        refund_attempted = asyncio.Event()

        async def tax_writer() -> str:
            async with race_sessions() as session:
                try:
                    await set_rls_company(session, company_id)
                    await _set_timeout(session)
                    await _insert_tax(session, payment_id)
                    tax_locked.set()
                    await release_tax.wait()
                    await session.commit()
                    return "committed"
                except BaseException:
                    tax_locked.set()
                    raise

        async def refund_writer() -> str:
            await tax_locked.wait()
            async with race_sessions() as session:
                await set_rls_company(session, company_id)
                await _set_timeout(session)
                refund_attempted.set()
                try:
                    await _set_refund(session, payment_id)
                    await session.commit()
                    return "committed"
                except DBAPIError:
                    await session.rollback()
                    return "rejected"

        tax_task = asyncio.create_task(tax_writer())
        await asyncio.wait_for(tax_locked.wait(), timeout=5)
        if tax_task.done():
            await tax_task
        refund_task = asyncio.create_task(refund_writer())
        await asyncio.wait_for(refund_attempted.wait(), timeout=5)
        await asyncio.sleep(0.05)
        release_tax.set()
        assert await asyncio.wait_for(tax_task, timeout=5) == "committed"
        assert await asyncio.wait_for(refund_task, timeout=5) == "rejected"
        assert await _final_state(payment_id) == ("INCOMING", 1)

    async def _refund_first(payment_id: object) -> None:
        refund_locked = asyncio.Event()
        release_refund = asyncio.Event()
        tax_attempted = asyncio.Event()

        async def refund_writer() -> str:
            async with race_sessions() as session:
                try:
                    await set_rls_company(session, company_id)
                    await _set_timeout(session)
                    await _set_refund(session, payment_id)
                    refund_locked.set()
                    await release_refund.wait()
                    await session.commit()
                    return "committed"
                except BaseException:
                    refund_locked.set()
                    raise

        async def tax_writer() -> str:
            await refund_locked.wait()
            async with race_sessions() as session:
                await set_rls_company(session, company_id)
                await _set_timeout(session)
                tax_attempted.set()
                try:
                    await _insert_tax(session, payment_id)
                    await session.commit()
                    return "committed"
                except DBAPIError:
                    await session.rollback()
                    return "rejected"

        refund_task = asyncio.create_task(refund_writer())
        await asyncio.wait_for(refund_locked.wait(), timeout=5)
        if refund_task.done():
            await refund_task
        tax_task = asyncio.create_task(tax_writer())
        await asyncio.wait_for(tax_attempted.wait(), timeout=5)
        await asyncio.sleep(0.05)
        release_refund.set()
        assert await asyncio.wait_for(refund_task, timeout=5) == "committed"
        assert await asyncio.wait_for(tax_task, timeout=5) == "rejected"
        assert await _final_state(payment_id) == ("REFUND", 0)

    try:
        await _tax_first(first_id)
        await _refund_first(second_id)
    finally:
        await race_engine.dispose()


@pytest.mark.parametrize("operation_order", ["edit_first", "delete_first", "concurrent"])
async def test_converted_draft_delete_and_deposit_edit_keep_tax_event_atomic(
    db_client: AsyncClient,
    operation_order: str,
) -> None:
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    quote = await _create_quote(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )
    await _accept_quote(db_client, quote["id"])
    aggregate = await _record(db_client, quote["id"], "40", "2026-02-01")
    payment_id = aggregate["items"][0]["id"]
    converted = await db_client.post(f"/api/v1/quotes/{quote['id']}/convert")
    assert converted.status_code == 201, converted.text
    delete_url = f"/api/v1/invoices/{converted.json()['id']}"
    edit_url = f"/api/v1/payments/{payment_id}"
    edit_body = {
        "payment_date": "2026-02-01",
        "amount": "45",
        "note": operation_order,
    }
    if operation_order == "edit_first":
        edited = await db_client.put(edit_url, json=edit_body)
        deleted = await db_client.delete(delete_url)
    elif operation_order == "delete_first":
        deleted = await db_client.delete(delete_url)
        edited = await db_client.put(edit_url, json=edit_body)
    else:
        deleted, edited = await asyncio.wait_for(
            asyncio.gather(
                db_client.delete(delete_url),
                db_client.put(edit_url, json=edit_body),
            ),
            timeout=5,
        )
    assert deleted.status_code == 204, deleted.text
    assert edited.status_code == 200, edited.text

    quote_payments = await db_client.get(f"/api/v1/quotes/{quote['id']}/payments")
    assert quote_payments.status_code == 200
    assert len(quote_payments.json()["items"]) == 1
    item = quote_payments.json()["items"][0]
    assert (item["id"], item["amount"], item["note"]) == (
        payment_id,
        "45.000",
        operation_order,
    )
    assert sum(
        (Decimal(row["gross_amount"]) for row in item["tax_breakdown"]),
        Decimal("0"),
    ) == Decimal("45")
    chain = (
        await db_client.get(f"/api/v1/quotes/{quote['id']}/document-chain")
    ).json()
    update_events = [
        row
        for row in chain["events"]
        if row["event_type"] == "QUOTE_PAYMENT_UPDATED"
        and row["metadata"].get("payment_id") == payment_id
    ]
    assert len(update_events) == 1


async def test_concurrent_refunds_and_incoming_delete_are_serialized(
    db_client: AsyncClient,
) -> None:
    _, source = await _bootstrap(db_client)
    incoming = await _pay(db_client, source["id"], "121")
    credit = await _issue_full_credit(db_client, source["id"])
    create_url = f"/api/v1/credit-notes/{credit['id']}/refunds"
    left, right = await asyncio.gather(
        db_client.post(
            create_url, json={"payment_date": "2026-02-04", "amount": "121"}
        ),
        db_client.delete(f"/api/v1/payments/{incoming['items'][0]['id']}"),
    )
    assert sorted((left.status_code, right.status_code))[0] in {200, 201}
    assert sum(code in {200, 201} for code in (left.status_code, right.status_code)) == 1
    assert sum(code in {409, 422} for code in (left.status_code, right.status_code)) == 1
    collection = (await db_client.get(create_url)).json()
    incoming_read = await db_client.get(
        f"/api/v1/payments/{incoming['items'][0]['id']}"
    )
    if left.status_code == 201:
        assert collection["refunded_total"] == "121.000"
        assert incoming_read.status_code == 200
    else:
        assert collection["refunded_total"] == "0.000"
        assert incoming_read.status_code == 404


async def test_two_concurrent_full_refunds_have_one_winner_and_no_partial_write(
    db_client: AsyncClient,
) -> None:
    _, source = await _bootstrap(db_client)
    await _pay(db_client, source["id"], "121")
    credit = await _issue_full_credit(db_client, source["id"])
    url = f"/api/v1/credit-notes/{credit['id']}/refunds"
    first, second = await asyncio.gather(
        db_client.post(
            url,
            json={"payment_date": "2026-02-04", "amount": "121", "note": "one"},
        ),
        db_client.post(
            url,
            json={"payment_date": "2026-02-04", "amount": "121", "note": "two"},
        ),
    )
    assert sum(item.status_code == 201 for item in (first, second)) == 1
    assert sum(item.status_code in {409, 422} for item in (first, second)) == 1
    winner = first if first.status_code == 201 else second
    assert winner.json()["refunded_total"] == "121.000"
    collection = (await db_client.get(url)).json()
    assert collection["refunded_total"] == "121.000"
    assert len(collection["items"]) == 1
    chain = (
        await db_client.get(f"/api/v1/invoices/{source['id']}/document-chain")
    ).json()
    created_events = [
        item
        for item in chain["events"]
        if item["event_type"] == "REFUND_CREATED"
    ]
    assert len(created_events) == 1


async def test_concurrent_incoming_updates_return_their_own_locked_snapshots(
    db_client: AsyncClient,
) -> None:
    _, source = await _bootstrap(db_client)
    incoming = await _pay(db_client, source["id"], "100")
    payment_id = incoming["items"][0]["id"]

    async def update(amount: str, note: str) -> Response:
        return await db_client.put(
            f"/api/v1/payments/{payment_id}",
            json={"payment_date": "2026-02-02", "amount": amount, "note": note},
        )

    first, second = await asyncio.gather(
        update("90", "incoming first"), update("80", "incoming second")
    )
    assert first.status_code == second.status_code == 200
    first_item = first.json()["invoice"]["items"][0]
    second_item = second.json()["invoice"]["items"][0]
    assert (first_item["amount"], first_item["note"]) == (
        "90.000",
        "incoming first",
    )
    assert (second_item["amount"], second_item["note"]) == (
        "80.000",
        "incoming second",
    )
    assert first.json()["invoice"]["paid_total"] == "90.000"
    assert second.json()["invoice"]["paid_total"] == "80.000"


async def test_concurrent_refund_updates_return_their_own_locked_snapshots(
    db_client: AsyncClient,
) -> None:
    _, source = await _bootstrap(db_client)
    await _pay(db_client, source["id"], "121")
    credit = await _issue_full_credit(db_client, source["id"])
    created = await db_client.post(
        f"/api/v1/credit-notes/{credit['id']}/refunds",
        json={"payment_date": "2026-02-04", "amount": "10", "note": "seed"},
    )
    refund_id = created.json()["items"][0]["id"]

    async def update(amount: str, note: str) -> Response:
        return await db_client.put(
            f"/api/v1/payments/{refund_id}",
            json={"payment_date": "2026-02-05", "amount": amount, "note": note},
        )

    first, second = await asyncio.gather(
        update("40", "first command"), update("50", "second command")
    )
    assert first.status_code == second.status_code == 200
    first_item = first.json()["refund"]["items"][0]
    second_item = second.json()["refund"]["items"][0]
    assert (first_item["amount"], first_item["note"]) == (
        "40.000",
        "first command",
    )
    assert (second_item["amount"], second_item["note"]) == (
        "50.000",
        "second command",
    )
    assert first.json()["refund"]["refunded_total"] == "40.000"
    assert second.json()["refund"]["refunded_total"] == "50.000"

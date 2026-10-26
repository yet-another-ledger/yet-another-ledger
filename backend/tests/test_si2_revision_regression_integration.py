"""SI2 Step 5 cross-module regressions through the runtime API role."""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pypdfium2
import pytest
from httpx import AsyncClient
from test_m12_artifact_integration import _all_formal_documents, _invoice_artifacts, _smtp
from test_m12_credit_integration import _issued_standard, _issued_treatment_standard
from test_m12_refund_integration import _issue_full_credit
from test_m13_artifact_upload_integration import _historical_pdf, _upload
from test_quote_payment_integration import (
    _accept_quote,
    _create_customer,
    _create_quote,
    _full_auth,
    _setup_company,
)

from jai.services.reporting import dashboard as dashboard_service

pytestmark = pytest.mark.integration


def _pdf_text(content: bytes) -> str:
    pdf = pypdfium2.PdfDocument(content)
    try:
        return "\n".join(pdf[index].get_textpage().get_text_range() for index in range(len(pdf)))
    finally:
        pdf.close()


async def _revise(
    client: AsyncClient, invoice: dict[str, Any], changes: dict[str, Any]
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/invoices/{invoice['id']}/revisions",
        json={
            "expected_updated_at": invoice["updated_at"],
            "changes": changes,
            "reason": "Step 5 regression fixture",
            "confirm_not_delivered": True,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_revision_preserves_download_history_and_current_send_uses_revised_pdf(
    db_client: AsyncClient,
) -> None:
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    invoice = await _issued_standard(db_client, seeds["rates"]["NL standard (21%)"]["id"])
    invoice_id = str(invoice["id"])

    old_download = await db_client.get(f"/api/v1/invoices/{invoice_id}/pdf?locale=en")
    assert old_download.status_code == 200
    old_hash = hashlib.sha256(old_download.content).hexdigest()
    old_rows = await _invoice_artifacts(db_client, invoice_id)
    assert len(old_rows) == 1
    old_artifact_id = old_rows[0]["id"]
    old_attachment = await db_client.get(
        f"/api/v1/invoices/{invoice_id}/artifacts/{old_artifact_id}"
    )
    assert old_attachment.content == old_download.content

    old_line = invoice["lines"][0]
    revised = await _revise(
        db_client,
        invoice,
        {
            "notes": "Revised before delivery",
            "lines": [
                {
                    "line_id": old_line["id"],
                    "name": "Revised bespoke service",
                    "description": "Updated scope after revision",
                    "quantity": "2",
                    "unit_price": "150",
                    "vat_rate_id": old_line["vat_rate_id"],
                }
            ],
        },
    )
    assert revised["status"] == "SENT"
    assert revised["invoice_number"] == invoice["invoice_number"]
    assert revised["issued_at"] == invoice["issued_at"]
    assert Decimal(revised["total_incl_vat"]) == Decimal("363.00")

    preview = await db_client.get(
        f"/api/v1/invoices/{invoice_id}/pdf?locale=en&preview=true"
    )
    assert preview.status_code == 200
    preview_text = _pdf_text(preview.content)
    assert "Revised bespoke service" in preview_text
    assert "Revised before delivery" in preview_text
    assert "363.00" in preview_text

    later_download = await db_client.get(f"/api/v1/invoices/{invoice_id}/pdf?locale=en")
    assert later_download.status_code == 200
    assert later_download.content != old_download.content
    assert old_hash == hashlib.sha256(old_download.content).hexdigest()
    old_attachment_again = await db_client.get(
        f"/api/v1/invoices/{invoice_id}/artifacts/{old_artifact_id}"
    )
    assert old_attachment_again.content == old_download.content
    rows = await _invoice_artifacts(db_client, invoice_id)
    assert next(row for row in rows if row["id"] == old_artifact_id) == old_rows[0]
    assert len(rows) == 2
    assert {row["id"] for row in rows} >= {old_artifact_id}
    assert {row["sha256"] for row in rows} >= {old_hash}
    assert hashlib.sha256(later_download.content).hexdigest() in {
        row["sha256"] for row in rows
    }
    current_artifact = next(
        row
        for row in rows
        if row["sha256"] == hashlib.sha256(later_download.content).hexdigest()
    )
    artifact_rows_before_send = rows.copy()

    captured: dict[str, Any] = {}

    async def capture(**kwargs: Any) -> None:
        captured.update(kwargs)

    with (
        patch("jai.services.email._get_smtp_config", return_value=_smtp()),
        patch("jai.services.email._send_mail", side_effect=capture),
    ):
        sent = await db_client.post(
            f"/api/v1/invoices/{invoice_id}/send",
            json={"to": "si2-test-recipient@example.com", "locale": "en"},
        )
    assert sent.status_code == 200, sent.text
    assert sent.json()["artifact_id"] == current_artifact["id"]
    assert captured["attachment_bytes"] == later_download.content
    assert "Revised bespoke service" in _pdf_text(captured["attachment_bytes"])
    emails = await db_client.get(f"/api/v1/invoices/{invoice_id}/emails")
    assert emails.status_code == 200
    assert emails.json()["items"][0]["artifact_id"] == sent.json()["artifact_id"]
    sent_email_snapshot = emails.json()["items"]
    sent_artifact_snapshot = await _invoice_artifacts(db_client, invoice_id)
    assert sent_artifact_snapshot == artifact_rows_before_send
    current = (await db_client.get(f"/api/v1/invoices/{invoice_id}")).json()
    history_snapshot = (await db_client.get(f"/api/v1/invoices/{invoice_id}/revisions")).json()
    blocked = await db_client.post(
        f"/api/v1/invoices/{invoice_id}/revisions",
        json={
            "expected_updated_at": current["updated_at"],
            "changes": {"notes": "Cannot revise after successful send"},
            "reason": "Successful send is delivery",
            "confirm_not_delivered": True,
        },
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "REVISION_ALREADY_DELIVERED"
    assert (await db_client.get(f"/api/v1/invoices/{invoice_id}")).json() == current
    history_after = await db_client.get(f"/api/v1/invoices/{invoice_id}/revisions")
    assert history_after.json() == history_snapshot
    assert await _invoice_artifacts(db_client, invoice_id) == sent_artifact_snapshot
    emails_after = await db_client.get(f"/api/v1/invoices/{invoice_id}/emails")
    assert emails_after.json()["items"] == sent_email_snapshot


async def test_failed_mail_history_survives_revision_and_full_credit_replacement_uses_new_basis(
    db_client: AsyncClient,
) -> None:
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    invoice = await _issued_standard(db_client, seeds["rates"]["NL standard (21%)"]["id"])
    invoice_id = str(invoice["id"])

    async def fail_mail(**_kwargs: Any) -> None:
        raise OSError("isolated SMTP failure")

    with (
        patch("jai.services.email._get_smtp_config", return_value=_smtp()),
        patch("jai.services.email._send_mail", side_effect=fail_mail),
    ):
        failed = await db_client.post(
            f"/api/v1/invoices/{invoice_id}/send",
            json={"to": "si2-test-recipient@example.com"},
        )
    assert failed.status_code == 200, failed.text
    assert failed.json()["status"] == "FAILED"
    assert failed.json()["artifact_id"] is None
    failed_rows = await db_client.get(f"/api/v1/invoices/{invoice_id}/emails")
    assert failed_rows.status_code == 200
    failed_before = failed_rows.json()["items"][0]
    assert failed_before["status"] == "FAILED"
    assert failed_before["artifact_id"] is None

    revised = await _revise(
        db_client,
        invoice,
        {
            "lines": [
                {
                    "line_id": invoice["lines"][0]["id"],
                    "name": "Current corrected service",
                    "quantity": "1",
                    "unit_price": "200",
                    "vat_rate_id": invoice["lines"][0]["vat_rate_id"],
                }
            ]
        },
    )
    assert revised["total_incl_vat"] == "242.000"
    failed_after = (await db_client.get(f"/api/v1/invoices/{invoice_id}/emails")).json()["items"]
    assert len(failed_after) == 1
    assert failed_after[0] == failed_before

    # Existing correction workflows operate on the revised credit basis; full
    # Credit plus replacement is tested separately from partial-credit coverage.
    credit = await _issue_full_credit(db_client, invoice_id)
    assert Decimal(credit["total_incl_vat"]) == Decimal("242")
    assert credit["source_invoice_id"] == invoice_id
    replacement = await db_client.post(f"/api/v1/credit-notes/{credit['id']}/replacement")
    assert replacement.status_code == 201, replacement.text
    assert replacement.json()["replacement_of_credit_note_id"] == credit["id"]
    assert Decimal(replacement.json()["total_incl_vat"]) == Decimal("242")
    replacement_sent = await db_client.post(
        f"/api/v1/invoices/{replacement.json()['id']}/status", json={"status": "SENT"}
    )
    assert replacement_sent.status_code == 200, replacement_sent.text
    assert replacement_sent.json()["document_kind"] == "STANDARD"
    assert replacement_sent.json()["invoice_number"] != invoice["invoice_number"]
    source_after = (await db_client.get(f"/api/v1/invoices/{invoice_id}")).json()
    assert source_after["invoice_number"] == invoice["invoice_number"]
    assert source_after["issued_at"] == invoice["issued_at"]


async def test_upload_artifact_reuses_unchanged_presentation_and_survives_revision(
    db_client: AsyncClient,
) -> None:
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    invoice = await _issued_standard(db_client, seeds["rates"]["NL standard (21%)"]["id"])
    invoice_id = str(invoice["id"])
    exact = await _historical_pdf(db_client, invoice_id, "si2-before-revision")
    uploaded = await _upload(db_client, invoice_id, exact, "source-original.pdf")

    # Identical current presentation reuses the exact uploaded artifact.
    same_download = await db_client.get(f"/api/v1/invoices/{invoice_id}/pdf?locale=en")
    assert same_download.status_code == 200
    assert same_download.content == exact
    assert (await _invoice_artifacts(db_client, invoice_id)) == [uploaded]

    revised = await _revise(db_client, invoice, {"notes": "Updated after exact upload"})
    # A text-only revision changes the presentation fingerprint while preserving
    # the uploaded row and its opaque source bytes.
    current = await db_client.get(f"/api/v1/invoices/{invoice_id}/pdf?locale=en")
    assert current.status_code == 200
    assert current.content != exact
    assert "Updated after exact upload" in _pdf_text(current.content)
    old = await db_client.get(
        f"/api/v1/invoices/{invoice_id}/artifacts/{uploaded['id']}"
    )
    assert old.status_code == 200 and old.content == exact
    rows = await _invoice_artifacts(db_client, invoice_id)
    assert len(rows) == 2
    assert any(row["id"] == uploaded["id"] and row["creation_reason"] == "UPLOAD" for row in rows)
    assert any(row["sha256"] == hashlib.sha256(current.content).hexdigest() for row in rows)
    assert revised["notes"] == "Updated after exact upload"


async def test_cross_quarter_revision_moves_one_crossborder_fact_and_keeps_frozen_snapshot(
    db_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FrozenMay(datetime):
        @classmethod
        def now(cls, tz: object | None = None) -> FrozenMay:
            return cls(2026, 5, 15, tzinfo=tz)

    monkeypatch.setattr(dashboard_service, "datetime", FrozenMay)
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    invoice = await _issued_treatment_standard(
        db_client,
        rate_id=seeds["rates"]["NL standard (21%)"]["id"],
        treatment_id=seeds["treatments"]["EU_B2B_REVERSE"]["id"],
        country_code="DE",
    )
    invoice_id = str(invoice["id"])
    assert invoice["invoice_date"] == "2026-02-01"
    q1 = await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=1")
    assert q1.status_code == 200
    assert [row["document_id"] for row in q1.json()["event_rows"]] == [invoice_id]
    q1_pl_before = await db_client.get(
        "/api/v1/reports/profit-loss?from=2026-01-01&to=2026-03-31&granularity=quarter"
    )
    assert Decimal(q1_pl_before.json()["revenue_net"]) == Decimal("100")

    after = await _revise(
        db_client,
        invoice,
        {
            "invoice_date": "2026-04-01",
            "due_date": "2026-04-15",
            "lines": [
                {
                    "line_id": invoice["lines"][0]["id"],
                    "name": invoice["lines"][0]["name"],
                    "description": invoice["lines"][0]["description"],
                    "quantity": "2",
                    "unit_price": "75",
                    "vat_rate_id": invoice["lines"][0]["vat_rate_id"],
                }
            ],
        },
    )
    assert after["total_incl_vat"] == "150.000"
    # Header plus commercial change projects one current source in Q2, without
    # changing the frozen cross-border tax treatment.
    q1_after = await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=1")
    q2 = await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=2")
    assert q1_after.status_code == q2.status_code == 200
    assert q1_after.json()["event_rows"] == []
    q2_rows = q2.json()["event_rows"]
    assert len(q2_rows) == 1
    assert q2_rows[0]["document_id"] == invoice_id
    assert q2_rows[0]["taxable_amount"] == "150.00"
    assert q2_rows[0]["vat_treatment_code"] == "EU_B2B_REVERSE"
    icp = await db_client.get("/api/v1/reports/icp?year=2026&quarter=2")
    assert icp.status_code == 200, icp.text
    assert icp.json()["total_net"] == "150.00"
    assert icp.json()["lines"][0]["source_documents"][0]["document_id"] == invoice_id
    q1_pl = await db_client.get(
        "/api/v1/reports/profit-loss?from=2026-01-01&to=2026-03-31&granularity=quarter"
    )
    q2_pl = await db_client.get(
        "/api/v1/reports/profit-loss?from=2026-04-01&to=2026-06-30&granularity=quarter"
    )
    assert Decimal(q1_pl.json()["revenue_net"]) == Decimal("0")
    assert Decimal(q2_pl.json()["revenue_net"]) == Decimal("150")
    dashboard = await db_client.get("/api/v1/reports/dashboard?year=2026")
    assert dashboard.status_code == 200
    assert dashboard.json()["kpi"]["ytd_revenue"] == "150.00"
    history = await db_client.get(f"/api/v1/invoices/{invoice_id}/revisions")
    assert history.status_code == 200 and history.json()["total"] == 1


async def test_domestic_vat_revision_crosses_quarter_with_frozen_rate_after_master_edit(
    db_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FrozenMay(datetime):
        @classmethod
        def now(cls, tz: object | None = None) -> FrozenMay:
            return cls(2026, 5, 15, tzinfo=tz)

    monkeypatch.setattr(dashboard_service, "datetime", FrozenMay)
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    rate = seeds["rates"]["NL standard (21%)"]
    invoice = await _issued_standard(db_client, rate["id"])
    invoice_id = str(invoice["id"])
    before = await db_client.get(f"/api/v1/invoices/{invoice_id}")
    assert before.status_code == 200
    old_line = before.json()["lines"][0]

    # Changing and deactivating the dictionary row must not rewrite the issued
    # Invoice's frozen tax snapshot during a later financial revision.
    changed_master = await db_client.put(
        f"/api/v1/vat-rates/{rate['id']}",
        json={"label": rate["label"], "percent": "24", "active": False},
    )
    assert changed_master.status_code == 200, changed_master.text
    assert changed_master.json()["percent"] == "24.000"
    q1_before = await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=1")
    assert q1_before.status_code == 200
    assert len(q1_before.json()["event_rows"]) == 1
    assert q1_before.json()["event_rows"][0]["document_id"] == invoice_id
    assert q1_before.json()["boxes"]["box_1a"]["base"] == "100.00"
    assert q1_before.json()["boxes"]["box_1a"]["vat"] == "21.00"

    after = await _revise(
        db_client,
        before.json(),
        {
            "invoice_date": "2026-04-01",
            "due_date": "2026-04-15",
            "lines": [
                {
                    "line_id": old_line["id"],
                    "name": old_line["name"],
                    "description": old_line["description"],
                    "quantity": "2",
                    "unit_price": "75",
                    "vat_rate_id": old_line["vat_rate_id"],
                }
            ],
        },
    )
    assert Decimal(after["total_incl_vat"]) == Decimal("181.50")
    assert after["lines"][0]["vat_rate_percent"] == "21.000"
    assert after["lines"][0]["vat_total"] == "31.500"

    q1 = await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=1")
    q2 = await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=2")
    assert q1.status_code == q2.status_code == 200
    assert q1.json()["event_rows"] == []
    assert len(q2.json()["event_rows"]) == 1
    row = q2.json()["event_rows"][0]
    assert row["document_id"] == invoice_id
    assert row["taxable_amount"] == "150.00"
    assert row["vat_amount"] == "31.50"
    assert row["vat_rate_percent"] == "21.000"
    assert q2.json()["boxes"]["box_1a"]["base"] == "150.00"
    assert q2.json()["boxes"]["box_1a"]["vat"] == "31.50"

    q1_pl = await db_client.get(
        "/api/v1/reports/profit-loss?from=2026-01-01&to=2026-03-31&granularity=quarter"
    )
    q2_pl = await db_client.get(
        "/api/v1/reports/profit-loss?from=2026-04-01&to=2026-06-30&granularity=quarter"
    )
    assert Decimal(q1_pl.json()["revenue_net"]) == 0
    assert Decimal(q2_pl.json()["revenue_net"]) == 150
    dashboard = await db_client.get("/api/v1/reports/dashboard?year=2026")
    assert dashboard.status_code == 200
    assert dashboard.json()["kpi"]["ytd_revenue"] == "150.00"
    assert dashboard.json()["kpi"]["current_quarter_vat_payable"] == "31.50"


async def test_actual_formal_documents_and_receipt_chain_reject_all_revision_bypasses(
    db_client: AsyncClient,
) -> None:
    documents, seeds = await _all_formal_documents(db_client)
    excluded = [documents[kind] for kind in ("ADVANCE", "FINAL", "CREDIT_NOTE")]

    # Create a real receipt-only quote deposit and convert it through the public
    # API to a Standard Invoice with protected receipt provenance.
    customer_id = await _create_customer(db_client)
    quote = await _create_quote(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )
    await _accept_quote(db_client, quote["id"])
    receipt = await db_client.post(
        f"/api/v1/quotes/{quote['id']}/payments",
        json={"payment_date": "2026-02-01", "amount": "20.00"},
    )
    assert receipt.status_code == 201, receipt.text
    converted = await db_client.post(f"/api/v1/quotes/{quote['id']}/convert")
    assert converted.status_code == 201, converted.text
    receipt_invoice = await db_client.post(
        f"/api/v1/invoices/{converted.json()['id']}/status", json={"status": "SENT"}
    )
    assert receipt_invoice.status_code == 200, receipt_invoice.text
    excluded.append(receipt_invoice.json())

    for original in excluded:
        invoice_id = original["id"]
        before = (await db_client.get(f"/api/v1/invoices/{invoice_id}")).json()
        assert before["revision_eligibility"]["available"] is False
        history_before = (await db_client.get(f"/api/v1/invoices/{invoice_id}/revisions")).json()
        assert history_before["total"] == 0
        changes = {"notes": "Must be rejected"}

        preview = await db_client.post(
            f"/api/v1/invoices/{invoice_id}/revisions/calculate",
            json={"expected_updated_at": before["updated_at"], "changes": changes},
        )
        assert preview.status_code == 409, preview.text
        save = await db_client.post(
            f"/api/v1/invoices/{invoice_id}/revisions",
            json={
                "expected_updated_at": before["updated_at"],
                "changes": changes,
                "reason": "Must remain excluded",
                "confirm_not_delivered": True,
            },
        )
        assert save.status_code == 409, save.text
        bypass_status = await db_client.post(
            f"/api/v1/invoices/{invoice_id}/status", json={"status": "DRAFT"}
        )
        assert bypass_status.status_code in {409, 422}, bypass_status.text
        bypass_put = await db_client.put(
            f"/api/v1/invoices/{invoice_id}",
            json={
                "customer_id": before["customer_id"],
                "invoice_date": before["invoice_date"],
                "due_date": before["due_date"],
                "currency": before["currency"],
                "tax_mode": before["tax_mode"],
                "amounts_include_vat": before["amounts_include_vat"],
                "vat_treatment_id": before["vat_treatment_id"],
                "notes": "PUT bypass attempt",
                "lines": [
                    {
                        "name": line["name"],
                        "description": line["description"],
                        "quantity": line["quantity"],
                        "unit_price": line["unit_price"],
                        "vat_rate_id": line["vat_rate_id"],
                    }
                    for line in before["lines"]
                ],
            },
        )
        assert bypass_put.status_code in {409, 422}, bypass_put.text

        after = (await db_client.get(f"/api/v1/invoices/{invoice_id}")).json()
        assert after == before
        history_after = (await db_client.get(f"/api/v1/invoices/{invoice_id}/revisions")).json()
        assert history_after == history_before

"""PostgreSQL acceptance coverage for M12 Step 9 formal artifacts.

The assertions deliberately use the real ``jai_app`` NOBYPASSRLS role through
the HTTP fixture.  They prove the output matrix rather than merely testing the
renderer with in-memory objects.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pypdfium2
import pytest
from fastapi import Request
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from test_m12_advance_integration import _additional_formal_quote
from test_m12_correction_followup_integration import _issue_credit, _issued_final
from test_m12_credit_integration import _issued_standard
from test_m12_final_integration import _issued_advance
from test_m12_refund_integration import _issue_full_credit, _pay
from test_migrations import _run_alembic
from test_quote_payment_integration import _full_auth, _setup_company

from jai.db import get_session, set_rls_company
from jai.main import app
from jai.models._enums import DocumentArtifactReason
from jai.models.document_artifact import DocumentArtifact
from jai.models.email_log import EmailLog
from jai.models.payment import Payment
from jai.schemas.setting import SmtpSettings
from jai.services import payment as payment_service
from jai.services.artifacts import retain_invoice_artifact, retain_refund_artifact

pytestmark = pytest.mark.integration


def _pdf_text(pdf_bytes: bytes) -> str:
    document = pypdfium2.PdfDocument(pdf_bytes)
    try:
        return "\n".join(
            document[index].get_textpage().get_text_range() for index in range(len(document))
        )
    finally:
        document.close()


def _smtp() -> SmtpSettings:
    return SmtpSettings(
        host="smtp.example.test",
        port=587,
        username="user",
        password="secret",
        from_email="sender@example.com",
        from_name="JAI",
        use_tls=True,
        use_ssl=False,
    )


async def _all_formal_documents(client: AsyncClient) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build one issued document of every formal kind and one real refund."""
    await _full_auth(client)
    seeds = await _setup_company(client)
    standard = await _issued_standard(client, seeds["rates"]["NL standard (21%)"]["id"])
    await _pay(client, standard["id"], "121")
    credit = await _issue_full_credit(client, standard["id"])
    refund_response = await client.post(
        f"/api/v1/credit-notes/{credit['id']}/refunds",
        json={
            "payment_date": "2026-02-04",
            "amount": "20",
            "reference": "refund-reference",
            "note": "refund-note",
        },
    )
    assert refund_response.status_code == 201, refund_response.text
    refund = refund_response.json()["items"][0]

    quote = await _additional_formal_quote(client, seeds)
    advance = await _issued_advance(client, quote["id"], "50")
    final_draft = await client.post(
        f"/api/v1/quotes/{quote['id']}/final-invoice",
        json={"invoice_date": "2026-03-01"},
    )
    assert final_draft.status_code == 201, final_draft.text
    final_response = await client.post(
        f"/api/v1/invoices/{final_draft.json()['id']}/status", json={"status": "SENT"}
    )
    assert final_response.status_code == 200, final_response.text
    return {
        "STANDARD": standard,
        "ADVANCE": advance,
        "FINAL": final_response.json(),
        "CREDIT_NOTE": credit,
        "REFUND": refund,
    }, seeds


async def _invoice_artifacts(client: AsyncClient, invoice_id: str) -> list[dict[str, Any]]:
    response = await client.get(f"/api/v1/invoices/{invoice_id}/artifacts")
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def _refund_artifacts(client: AsyncClient, refund_id: str) -> list[dict[str, Any]]:
    response = await client.get(f"/api/v1/payments/{refund_id}/artifacts")
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def _assert_followup_artifact_outputs(
    client: AsyncClient,
    invoice: dict[str, Any],
    *,
    locale: str,
    relation_label: str,
    credit: dict[str, Any],
) -> None:
    """Preview/download/send one issued positive correction follow-up exactly."""
    preview = await client.get(
        f"/api/v1/invoices/{invoice['id']}/pdf", params={"locale": locale, "preview": "true"}
    )
    assert preview.status_code == 200, preview.text
    preview_text = _pdf_text(preview.content)
    assert relation_label in preview_text
    assert credit["invoice_number"] in preview_text
    assert str(credit["invoice_date"]) in preview_text
    assert await _invoice_artifacts(client, invoice["id"]) == []

    downloaded = await client.get(
        f"/api/v1/invoices/{invoice['id']}/pdf", params={"locale": locale}
    )
    assert downloaded.status_code == 200, downloaded.text
    artifacts = await _invoice_artifacts(client, invoice["id"])
    assert len(artifacts) == 1
    historical = await client.get(
        f"/api/v1/invoices/{invoice['id']}/artifacts/{artifacts[0]['id']}"
    )
    assert historical.status_code == 200
    assert historical.content == downloaded.content
    assert hashlib.sha256(downloaded.content).hexdigest() == artifacts[0]["sha256"]

    captured: dict[str, bytes] = {}

    async def capture_mail(**kwargs: Any) -> None:
        captured["bytes"] = kwargs["attachment_bytes"]

    with (
        patch("jai.services.email._get_smtp_config", return_value=_smtp()),
        patch("jai.services.email._send_mail", side_effect=capture_mail),
    ):
        sent = await client.post(
            f"/api/v1/invoices/{invoice['id']}/send",
            json={"to": "customer@example.com", "locale": locale},
        )
    assert sent.status_code == 200, sent.text
    assert sent.json()["artifact_id"] == artifacts[0]["id"]
    assert captured["bytes"] == historical.content


@pytest.mark.parametrize(
    ("locale", "scenario", "relation_label"),
    [
        ("en", "standard_replacement", "Replacement for Credit Note"),
        ("zh", "standard_replacement", "替换以下贷项通知单"),
        ("en", "advance_replacement", "Replacement for Credit Note"),
        ("zh", "advance_replacement", "替换以下贷项通知单"),
        ("en", "post_final_compensation", "Compensates Credit Note"),
        ("zh", "post_final_compensation", "补偿以下贷项通知单"),
    ],
)
async def test_followup_pdf_uses_invoice_relation_provenance_for_all_output_actions(
    db_client: AsyncClient,
    locale: str,
    scenario: str,
    relation_label: str,
) -> None:
    """Replacement/compensation provenance is independent of Quote rendering."""
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    rate_id = seeds["rates"]["NL standard (21%)"]["id"]
    if scenario == "standard_replacement":
        source = await _issued_standard(db_client, rate_id)
        credit = await _issue_credit(db_client, source["id"])
        draft = await db_client.post(f"/api/v1/credit-notes/{credit['id']}/replacement")
    elif scenario == "advance_replacement":
        quote = await _additional_formal_quote(db_client, seeds)
        source = await _issued_advance(db_client, quote["id"], "50")
        credit = await _issue_credit(db_client, source["id"])
        draft = await db_client.post(f"/api/v1/credit-notes/{credit['id']}/replacement")
    else:
        quote = await _additional_formal_quote(db_client, seeds)
        source = await _issued_advance(db_client, quote["id"], "50")
        await _issued_final(db_client, quote["id"])
        credit = await _issue_credit(db_client, source["id"], invoice_date="2026-03-04")
        draft = await db_client.post(f"/api/v1/credit-notes/{credit['id']}/compensating-invoice")
    assert draft.status_code == 201, draft.text
    issued = await db_client.post(
        f"/api/v1/invoices/{draft.json()['id']}/status", json={"status": "SENT"}
    )
    assert issued.status_code == 200, issued.text
    await _assert_followup_artifact_outputs(
        db_client,
        issued.json(),
        locale=locale,
        relation_label=relation_label,
        credit=credit,
    )


@pytest.mark.parametrize("kind", ["STANDARD", "ADVANCE", "FINAL"])
async def test_charge_document_refund_projection_changes_live_artifact_and_tombstone_reverts(
    db_client: AsyncClient, kind: str
) -> None:
    """All charge kinds show source-bound Refunds without changing old bytes."""
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    rate_id = seeds["rates"]["NL standard (21%)"]["id"]
    if kind == "STANDARD":
        source = await _issued_standard(db_client, rate_id)
    else:
        quote = await _additional_formal_quote(db_client, seeds)
        advance = await _issued_advance(db_client, quote["id"], "20" if kind == "FINAL" else "50")
        source = advance if kind == "ADVANCE" else await _issued_final(db_client, quote["id"])
    await _pay(db_client, source["id"], source["due_amount"])

    before = await db_client.get(f"/api/v1/invoices/{source['id']}/pdf")
    assert before.status_code == 200, before.text
    before_artifact = (await _invoice_artifacts(db_client, source["id"]))[0]
    credit = await _issue_credit(db_client, source["id"])
    half = Decimal(credit["total_incl_vat"]) / Decimal("2")
    partial = await db_client.post(
        f"/api/v1/credit-notes/{credit['id']}/refunds",
        json={"payment_date": "2026-03-05", "amount": str(half), "reference": f"{kind}-partial"},
    )
    assert partial.status_code == 201, partial.text
    partial_refund = partial.json()["items"][0]
    after_partial = await db_client.get(f"/api/v1/invoices/{source['id']}/pdf")
    assert after_partial.status_code == 200
    partial_text = _pdf_text(after_partial.content)
    assert credit["invoice_number"] in partial_text
    assert f"{kind}-partial" in partial_text
    assert after_partial.content != before.content
    artifacts = await _invoice_artifacts(db_client, source["id"])
    assert len(artifacts) == 2
    historical = await db_client.get(
        f"/api/v1/invoices/{source['id']}/artifacts/{before_artifact['id']}"
    )
    assert historical.content == before.content

    credit_after_partial = await db_client.get(f"/api/v1/invoices/{credit['id']}")
    assert credit_after_partial.status_code == 200, credit_after_partial.text
    full = await db_client.post(
        f"/api/v1/credit-notes/{credit['id']}/refunds",
        json={
            "payment_date": "2026-03-06",
            "amount": credit_after_partial.json()["refund_due_amount"],
            "reference": f"{kind}-full",
        },
    )
    assert full.status_code == 201, full.text
    after_full = await db_client.get(f"/api/v1/invoices/{source['id']}/pdf")
    assert after_full.status_code == 200
    assert f"{kind}-full" in _pdf_text(after_full.content)
    assert after_full.content != after_partial.content

    deleted = await db_client.delete(f"/api/v1/payments/{partial_refund['id']}")
    assert deleted.status_code == 200, deleted.text
    reverted = await db_client.get(f"/api/v1/invoices/{source['id']}/pdf")
    assert reverted.status_code == 200
    reverted_text = _pdf_text(reverted.content)
    assert f"{kind}-partial" not in reverted_text
    assert f"{kind}-full" in reverted_text


async def test_invoice_send_uses_one_post_guard_payment_snapshot(
    db_client: AsyncClient,
    runtime_db_engine: AsyncEngine,
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Send cannot retain a guard-read Invoice identity across renderer lock."""
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    source = await _issued_standard(db_client, seeds["rates"]["NL standard (21%)"]["id"])
    runtime_url = runtime_db_engine.url.render_as_string(hide_password=False)
    output_engine = create_async_engine(runtime_url, pool_size=1, max_overflow=0)
    mutation_engine = create_async_engine(runtime_url, pool_size=1, max_overflow=0)
    output_sessions = async_sessionmaker(output_engine, expire_on_commit=False, class_=AsyncSession)
    mutation_sessions = async_sessionmaker(
        mutation_engine, expire_on_commit=False, class_=AsyncSession
    )
    prior_provider = app.dependency_overrides[get_session]
    guard_seeded = asyncio.Event()
    release_renderer = asyncio.Event()
    from jai.services import pdf as pdf_service

    original_render = pdf_service.render_invoice_pdf_artifact

    async def gated_render(*args: object, **kwargs: object) -> tuple[bytes, str, str, str]:
        guard_seeded.set()
        await release_renderer.wait()
        return await original_render(*args, **kwargs)

    async def concurrent_session(request: Request) -> AsyncIterator[AsyncSession]:
        factory = output_sessions if request.url.path.endswith("/send") else mutation_sessions
        async with factory() as session:
            yield session

    captured: dict[str, Any] = {}

    async def capture_mail(**kwargs: Any) -> None:
        captured.update(kwargs)

    async def smtp_config(*_args: object) -> SmtpSettings:
        return _smtp()

    monkeypatch.setattr("jai.services.pdf.render_invoice_pdf_artifact", gated_render)
    monkeypatch.setattr("jai.services.email._get_smtp_config", smtp_config)
    monkeypatch.setattr("jai.services.email._send_mail", capture_mail)
    app.dependency_overrides[get_session] = concurrent_session
    try:
        send_task = asyncio.create_task(
            db_client.post(
                f"/api/v1/invoices/{source['id']}/send",
                json={"to": "customer@example.com", "locale": "en"},
            )
        )
        await asyncio.wait_for(guard_seeded.wait(), timeout=10)
        payment = await db_client.post(
            f"/api/v1/invoices/{source['id']}/payments",
            json={
                "payment_date": "2026-02-05",
                "amount": "20",
                "reference": "send-interleaving",
                "note": "post-guard",
            },
        )
        assert payment.status_code == 201, payment.text
        release_renderer.set()
        sent = await asyncio.wait_for(send_task, timeout=30)
    finally:
        app.dependency_overrides[get_session] = prior_provider
        await output_engine.dispose()
        await mutation_engine.dispose()

    assert sent.status_code == 200, sent.text
    attachment = captured["attachment_bytes"]
    text_content = _pdf_text(attachment)
    assert "send-interleaving" in text_content
    assert "20.00" in text_content
    # 121.00 is the document total; the locked settlement aggregate must show
    # its post-payment due snapshot, not the pre-render guard's 121.00 due.
    assert "101.00" in text_content
    artifacts = await _invoice_artifacts(db_client, source["id"])
    assert len(artifacts) == 1
    retained = await db_client.get(
        f"/api/v1/invoices/{source['id']}/artifacts/{artifacts[0]['id']}"
    )
    assert retained.content == attachment
    assert hashlib.sha256(attachment).hexdigest() == artifacts[0]["sha256"]
    async with db_session_maker() as session:
        await set_rls_company(session, uuid.UUID(seeds["company_id"]))
        log = await session.get(EmailLog, uuid.UUID(sent.json()["id"]))
        assert log is not None and str(log.artifact_id) == artifacts[0]["id"]
        assert log.to_email == "customer@example.com" and log.locale == "en"


@pytest.mark.parametrize(
    ("locale", "kind_labels"),
    [
        (
            "en",
            {
                "STANDARD": "Standard Invoice",
                "ADVANCE": "Advance Invoice",
                "FINAL": "Final Invoice",
                "CREDIT_NOTE": "Credit Note",
                "REFUND": "Refund Confirmation",
            },
        ),
        (
            "zh",
            {
                "STANDARD": "普通发票",
                "ADVANCE": "预付款发票",
                "FINAL": "最终结算发票",
                "CREDIT_NOTE": "贷项通知单",
                "REFUND": "退款确认单",
            },
        ),
    ],
)
async def test_formal_output_matrix_byte_retention_and_snapshot_stability(
    db_client: AsyncClient,
    db_session_maker: async_sessionmaker[AsyncSession],
    runtime_session_maker: async_sessionmaker[AsyncSession],
    locale: str,
    kind_labels: dict[str, str],
) -> None:
    documents, seeds = await _all_formal_documents(db_client)

    # Preview every formal kind in both locales.  It is a live render and must
    # not create a retention row; every document has its legal label/number/date.
    for kind in ("STANDARD", "ADVANCE", "FINAL", "CREDIT_NOTE"):
        document = documents[kind]
        preview = await db_client.get(
            f"/api/v1/invoices/{document['id']}/pdf",
            params={"locale": locale, "preview": "true"},
        )
        assert preview.status_code == 200, preview.text
        preview_text = _pdf_text(preview.content)
        assert kind_labels[kind].upper() in preview_text.upper()
        assert document["invoice_number"] in preview_text
        assert document["invoice_date"] in preview_text
        if kind == "CREDIT_NOTE":
            assert ("Credit Note Number" if locale == "en" else "贷项通知单号") in preview_text
            assert "Invoice #" not in preview_text
            assert "发票号" not in preview_text
            assert "Already paid" not in preview_text
            assert "Amount Due" not in preview_text
            assert "已付款" not in preview_text
            assert "应付款" not in preview_text
        assert await _invoice_artifacts(db_client, document["id"]) == []

    # Download is byte-first: returned bytes hash to, and are retrievable from,
    # the newly retained artifact.  A second identical download deduplicates.
    standard = documents["STANDARD"]
    first = await db_client.get(f"/api/v1/invoices/{standard['id']}/pdf", params={"locale": locale})
    assert first.status_code == 200
    artifacts = await _invoice_artifacts(db_client, standard["id"])
    assert len(artifacts) == 1
    assert artifacts[0]["sha256"] == hashlib.sha256(first.content).hexdigest()
    stored = await db_client.get(
        f"/api/v1/invoices/{standard['id']}/artifacts/{artifacts[0]['id']}"
    )
    assert stored.content == first.content
    second = await db_client.get(
        f"/api/v1/invoices/{standard['id']}/pdf", params={"locale": locale}
    )
    assert second.content == first.content
    assert len(await _invoice_artifacts(db_client, standard["id"])) == 1

    # Settlement changes produce a new retained byte stream while historic
    # output remains readable rather than being rerendered.
    advance = documents["ADVANCE"]
    advance_first = await db_client.get(
        f"/api/v1/invoices/{advance['id']}/pdf", params={"locale": locale}
    )
    assert advance_first.status_code == 200
    advance_artifact = (await db_client.get(f"/api/v1/invoices/{advance['id']}/artifacts")).json()[
        "items"
    ][0]
    payment = await db_client.post(
        f"/api/v1/invoices/{advance['id']}/payments",
        json={"payment_date": "2026-02-05", "amount": "1"},
    )
    assert payment.status_code == 201, payment.text
    advance_second = await db_client.get(
        f"/api/v1/invoices/{advance['id']}/pdf", params={"locale": locale}
    )
    assert advance_second.content != advance_first.content
    assert (
        len((await db_client.get(f"/api/v1/invoices/{advance['id']}/artifacts")).json()["items"])
        == 2
    )
    historic = await db_client.get(
        f"/api/v1/invoices/{advance['id']}/artifacts/{advance_artifact['id']}"
    )
    assert historic.content == advance_first.content

    # The Credit contains its frozen source/correction/refund context, Final
    # contains frozen applications, and Advance carries the source Quote ref.
    credit_text = _pdf_text(
        (
            await db_client.get(
                f"/api/v1/invoices/{documents['CREDIT_NOTE']['id']}/pdf", params={"locale": locale}
            )
        ).content
    )
    assert kind_labels["CREDIT_NOTE"].upper() in credit_text.upper()
    assert documents["STANDARD"]["invoice_number"] in credit_text
    assert "20.00" in credit_text
    advance_text = _pdf_text(
        (
            await db_client.get(
                f"/api/v1/invoices/{documents['ADVANCE']['id']}/pdf", params={"locale": locale}
            )
        ).content
    )
    assert documents["ADVANCE"]["supply_or_advance_date"] in advance_text
    final_text = _pdf_text(
        (
            await db_client.get(
                f"/api/v1/invoices/{documents['FINAL']['id']}/pdf", params={"locale": locale}
            )
        ).content
    )
    assert documents["ADVANCE"]["invoice_number"] in final_text

    refund = documents["REFUND"]
    refund_preview = await db_client.get(
        f"/api/v1/payments/{refund['id']}/refund-confirmation/preview", params={"locale": locale}
    )
    refund_text = _pdf_text(refund_preview.content)
    assert kind_labels["REFUND"].upper() in refund_text.upper()
    assert "refund-reference" in refund_text and "refund-note" in refund_text
    assert documents["CREDIT_NOTE"]["invoice_number"] in refund_text
    assert "Invoice" not in refund_text.split(kind_labels["REFUND"])[0]
    assert (await db_client.get(f"/api/v1/payments/{refund['id']}/artifacts")).json()["items"] == []
    refund_download = await db_client.get(
        f"/api/v1/payments/{refund['id']}/refund-confirmation", params={"locale": locale}
    )
    refund_artifact = (await _refund_artifacts(db_client, refund["id"]))[0]
    assert refund_artifact["sha256"] == hashlib.sha256(refund_download.content).hexdigest()
    assert (
        await db_client.get(f"/api/v1/payments/{refund['id']}/artifacts/{refund_artifact['id']}")
    ).content == refund_download.content

    # Master-data edits do not change party identity in an issued render, and
    # FORCE RLS blocks an app session with no/wrong tenant GUC from artifacts.
    async with db_session_maker() as session:
        await session.execute(
            text("UPDATE company SET name = 'Changed company' WHERE id = :id"),
            {"id": seeds["company_id"]},
        )
        await session.commit()
    preview_after_master_edit = await db_client.get(
        f"/api/v1/invoices/{standard['id']}/pdf", params={"locale": locale, "preview": "true"}
    )
    assert "Changed company" not in _pdf_text(preview_after_master_edit.content)
    async with runtime_session_maker() as session:
        assert list((await session.execute(select(DocumentArtifact))).scalars()) == []
        await set_rls_company(session, uuid.uuid4())
        assert list((await session.execute(select(DocumentArtifact))).scalars()) == []


async def test_successful_and_failed_send_artifact_semantics(
    db_client: AsyncClient,
) -> None:
    documents, seeds = await _all_formal_documents(db_client)
    standard = documents["STANDARD"]
    captured: dict[str, bytes] = {}

    async def _capture(**kwargs: Any) -> None:
        captured["bytes"] = kwargs["attachment_bytes"]

    with (
        patch("jai.services.email._get_smtp_config", return_value=_smtp()),
        patch("jai.services.email._send_mail", side_effect=_capture),
    ):
        sent = await db_client.post(
            f"/api/v1/invoices/{standard['id']}/send", json={"to": "customer@example.com"}
        )
    assert sent.status_code == 200, sent.text
    assert sent.json()["status"] == "SENT" and sent.json()["artifact_id"] is not None
    attachment = await db_client.get(
        f"/api/v1/invoices/{standard['id']}/artifacts/{sent.json()['artifact_id']}"
    )
    assert attachment.content == captured["bytes"]

    # A failed fresh send audits the failure but creates no dangling successful
    # attachment relationship (nor any new artifact when there was none).
    with (
        patch("jai.services.email._get_smtp_config", return_value=_smtp()),
        patch("jai.services.email._send_mail", side_effect=ConnectionError("offline")),
    ):
        failed = await db_client.post(
            f"/api/v1/invoices/{documents['FINAL']['id']}/send",
            json={"to": "customer@example.com"},
        )
    assert failed.status_code == 200, failed.text
    assert failed.json()["status"] == "FAILED" and failed.json()["artifact_id"] is None
    assert await _invoice_artifacts(db_client, documents["FINAL"]["id"]) == []


async def test_refund_delete_tombstones_cash_but_retains_artifact_and_email_audit(
    db_client: AsyncClient,
    db_engine: AsyncEngine,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """D15 deletion rolls cash back without deleting delivered confirmation bytes."""
    documents, seeds = await _all_formal_documents(db_client)
    refund = documents["REFUND"]
    credit = documents["CREDIT_NOTE"]
    downloaded = await db_client.get(f"/api/v1/payments/{refund['id']}/refund-confirmation")
    assert downloaded.status_code == 200, downloaded.text
    artifacts = await _refund_artifacts(db_client, refund["id"])
    assert len(artifacts) == 1
    artifact = artifacts[0]
    captured: dict[str, bytes] = {}

    async def _capture(**kwargs: Any) -> None:
        captured["bytes"] = kwargs["attachment_bytes"]

    with (
        patch("jai.services.email._get_smtp_config", return_value=_smtp()),
        patch("jai.services.email._send_mail", side_effect=_capture),
    ):
        sent = await db_client.post(
            f"/api/v1/payments/{refund['id']}/send-refund-confirmation",
            json={"to": "customer@example.com"},
        )
    assert sent.status_code == 200, sent.text
    assert sent.json()["artifact_id"] == artifact["id"]
    assert captured["bytes"] == downloaded.content

    deleted = await db_client.delete(f"/api/v1/payments/{refund['id']}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"] is True
    source_after_delete = await db_client.get(f"/api/v1/invoices/{documents['STANDARD']['id']}")
    assert source_after_delete.json()["refunded_total"] == "0.000"
    remaining_refunds = await db_client.get(f"/api/v1/credit-notes/{credit['id']}/refunds")
    assert remaining_refunds.json()["items"] == []
    # Live owner paths reject a tombstone, but its same-tenant audit path is
    # still byte-for-byte retrievable and keeps the successful EmailLog link.
    assert (
        await db_client.get(f"/api/v1/payments/{refund['id']}/refund-confirmation")
    ).status_code == 404
    historic = await db_client.get(f"/api/v1/payments/{refund['id']}/artifacts/{artifact['id']}")
    assert historic.status_code == 200 and historic.content == downloaded.content
    assert hashlib.sha256(historic.content).hexdigest() == artifact["sha256"]
    # The formal API-created tombstone blocks downgrade before any 0040 DDL;
    # its historical owner/bytes and successful EmailLog relation stay intact.
    downgrade = _run_alembic(
        "downgrade",
        "0039",
        url=db_engine.url.render_as_string(hide_password=False),
    )
    assert downgrade.returncode != 0
    assert "Cannot downgrade 0040 while deleted Refund tombstones exist" in downgrade.stderr
    assert "before downgrading" in downgrade.stderr
    assert (
        await db_client.get(f"/api/v1/payments/{refund['id']}/artifacts/{artifact['id']}")
    ).content == downloaded.content
    async with db_session_maker() as session:
        await set_rls_company(session, uuid.UUID(seeds["company_id"]))
        payment = await session.get(Payment, uuid.UUID(refund["id"]))
        assert payment is not None and payment.deleted_at is not None
        email = await session.get(EmailLog, uuid.UUID(sent.json()["id"]))
        assert email is not None and str(email.artifact_id) == artifact["id"]
        retained = await session.get(DocumentArtifact, uuid.UUID(artifact["id"]))
        assert retained is not None
        assert hashlib.sha256(retained.pdf_bytes).hexdigest() == artifact["sha256"]
        # 0044 is now the additive head.  The 0040 downgrade guard aborts
        # before running its DDL, so a failed attempt must retain that head.
        assert (await session.scalar(text("SELECT version_num FROM alembic_version"))) == "0044"


@pytest.mark.parametrize("output_action", ["preview", "download", "send"])
@pytest.mark.parametrize("mutation_action", ["PUT", "DELETE"])
async def test_refund_confirmation_output_and_mutation_use_canonical_lock_order(
    db_client: AsyncClient,
    runtime_db_engine: AsyncEngine,
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    output_action: str,
    mutation_action: str,
) -> None:
    """A real output cannot take a Refund lock ahead of its chain prefix.

    Mutation first holds the real Quote/source/Credit prefix and pauses before
    Payment/Refund.  Correct output waits on that prefix.  A regression which
    locks the Refund during output's seed would instead take Payment, then
    wait on the prefix while mutation waits on Payment: a deterministic cycle.
    """
    documents, seeds = await _all_formal_documents(db_client)
    refund_id = documents["REFUND"]["id"]
    runtime_url = runtime_db_engine.url.render_as_string(hide_password=False)
    output_engine = create_async_engine(runtime_url, pool_size=1, max_overflow=0)
    mutation_engine = create_async_engine(runtime_url, pool_size=1, max_overflow=0)
    output_sessions = async_sessionmaker(output_engine, expire_on_commit=False, class_=AsyncSession)
    mutation_sessions = async_sessionmaker(
        mutation_engine, expire_on_commit=False, class_=AsyncSession
    )
    prior_provider = app.dependency_overrides[get_session]
    mutation_prefix_locked = asyncio.Event()
    output_at_prefix = asyncio.Event()
    release_mutation = asyncio.Event()
    original_prefix = payment_service._lock_settlement_chain_prefix
    lock_calls = 0

    async def gated_prefix(*args: object, **kwargs: object) -> object:
        nonlocal lock_calls
        lock_calls += 1
        if lock_calls == 1:
            result = await original_prefix(*args, **kwargs)
            mutation_prefix_locked.set()
            await release_mutation.wait()
            return result
        output_at_prefix.set()
        return await original_prefix(*args, **kwargs)

    async def concurrent_session(request: Request) -> AsyncIterator[AsyncSession]:
        sessions = mutation_sessions if request.method in {"PUT", "DELETE"} else output_sessions
        async with sessions() as session:
            yield session

    captured: dict[str, Any] = {}

    async def capture_mail(**kwargs: Any) -> None:
        captured.update(kwargs)

    async def smtp_config(*_args: object) -> SmtpSettings:
        return _smtp()

    monkeypatch.setattr("jai.services.payment._lock_settlement_chain_prefix", gated_prefix)
    monkeypatch.setattr("jai.services.email._get_smtp_config", smtp_config)
    monkeypatch.setattr("jai.services.email._send_mail", capture_mail)
    app.dependency_overrides[get_session] = concurrent_session

    try:
        mutation_task = asyncio.create_task(
            db_client.request(
                mutation_action,
                f"/api/v1/payments/{refund_id}",
                json=(
                    {"payment_date": "2026-02-05", "amount": "19", "note": "concurrent"}
                    if mutation_action == "PUT"
                    else None
                ),
            )
        )
        await asyncio.wait_for(mutation_prefix_locked.wait(), timeout=10)
        assert not mutation_task.done()
        if output_action == "preview":
            output_task = asyncio.create_task(
                db_client.get(f"/api/v1/payments/{refund_id}/refund-confirmation/preview")
            )
        elif output_action == "download":
            output_task = asyncio.create_task(
                db_client.get(f"/api/v1/payments/{refund_id}/refund-confirmation")
            )
        else:
            output_task = asyncio.create_task(
                db_client.post(
                    f"/api/v1/payments/{refund_id}/send-refund-confirmation",
                    json={"to": "customer@example.com"},
                )
            )
        await asyncio.wait_for(output_at_prefix.wait(), timeout=10)
        assert not output_task.done(), "output unexpectedly bypassed the source/Credit prefix"
        release_mutation.set()
        output, mutation = await asyncio.wait_for(
            asyncio.gather(output_task, mutation_task), timeout=30
        )
    finally:
        app.dependency_overrides[get_session] = prior_provider
        await output_engine.dispose()
        await mutation_engine.dispose()

    assert mutation.status_code == 200, mutation.text
    if mutation_action == "PUT":
        updated_refund = await db_client.get(f"/api/v1/payments/{refund_id}")
        assert updated_refund.status_code == 200, updated_refund.text
        assert updated_refund.json()["payment_date"] == "2026-02-05"
        assert updated_refund.json()["amount"] == "19.000"
        assert updated_refund.json()["note"] == "concurrent"
    async with db_session_maker() as session:
        await set_rls_company(session, uuid.UUID(seeds["company_id"]))
        email_rows = list(
            (
                await session.execute(
                    select(EmailLog).where(EmailLog.related_id == uuid.UUID(refund_id))
                )
            ).scalars()
        )
    if mutation_action == "DELETE":
        assert output.status_code == 404, output.text
        assert await _refund_artifacts(db_client, refund_id) == []
        assert email_rows == []
        return
    assert output.status_code == 200, output.text
    if output_action == "send":
        assert output.json()["status"] == "SENT"
    else:
        assert output.content.startswith(b"%PDF-")
    artifacts = await _refund_artifacts(db_client, refund_id)
    if output_action == "preview":
        assert artifacts == []
        assert email_rows == []
        preview_text = _pdf_text(output.content)
        assert "2026-02-05" in preview_text
        assert "19.00" in preview_text
        assert "concurrent" in preview_text
    else:
        assert len(artifacts) == 1
        artifact_response = await db_client.get(
            f"/api/v1/payments/{refund_id}/artifacts/{artifacts[0]['id']}"
        )
        assert artifact_response.status_code == 200
        assert hashlib.sha256(artifact_response.content).hexdigest() == artifacts[0]["sha256"]
        if output_action == "download":
            assert email_rows == []
            assert artifact_response.content == output.content
            download_text = _pdf_text(artifact_response.content)
            assert "2026-02-05" in download_text
            assert "19.00" in download_text
            assert "concurrent" in download_text
        else:
            assert len(email_rows) == 1
            email = email_rows[0]
            assert str(email.artifact_id) == artifacts[0]["id"]
            assert email.subject == captured["subject"]
            assert email.body_snapshot == captured["html_body"]
            assert output.json()["artifact_id"] == artifacts[0]["id"]
            assert captured["attachment_bytes"] == artifact_response.content
            attachment_text = _pdf_text(captured["attachment_bytes"])
            assert "2026-02-05" in attachment_text
            assert "19.00" in attachment_text
            assert "concurrent" in attachment_text


async def test_artifact_database_constraints_are_not_api_only(
    db_client: AsyncClient,
    db_session_maker: async_sessionmaker[AsyncSession],
    runtime_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Raw SQL must not forge, alter, or detach retained legal output."""
    documents, seeds = await _all_formal_documents(db_client)
    standard = documents["STANDARD"]
    download = await db_client.get(f"/api/v1/invoices/{standard['id']}/pdf")
    assert download.status_code == 200
    artifact = (await _invoice_artifacts(db_client, standard["id"]))[0]

    async with db_session_maker() as session:
        await set_rls_company(session, seeds["company_id"])
        with pytest.raises(DBAPIError):
            await session.execute(
                text("UPDATE document_artifact SET filename = 'forged.pdf' WHERE id = :id"),
                {"id": artifact["id"]},
            )
        await session.rollback()

        # The same raw writer cannot attach an artifact to both legal owner
        # types, nor forge a company different from the issued invoice owner.
        await set_rls_company(session, seeds["company_id"])
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "INSERT INTO document_artifact "
                    "(id, company_id, invoice_id, refund_payment_id, artifact_kind, pdf_bytes, "
                    "sha256, render_fingerprint, locale, filename, creation_reason, "
                    "renderer_version) "
                    "VALUES (:id, :company_id, :invoice_id, :refund_id, 'FORMAL_DOCUMENT', "
                    "decode('25504446', 'hex'), :sha, :fingerprint, 'en', 'dual.pdf', "
                    "'DOWNLOAD', 'test')"
                ),
                {
                    "id": uuid.uuid4(),
                    "company_id": seeds["company_id"],
                    "invoice_id": standard["id"],
                    "refund_id": documents["REFUND"]["id"],
                    "sha": hashlib.sha256(b"%PDF").hexdigest(),
                    "fingerprint": "3" * 64,
                },
            )
        await session.rollback()

        await set_rls_company(session, seeds["company_id"])
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "INSERT INTO document_artifact "
                    "(id, company_id, invoice_id, artifact_kind, pdf_bytes, sha256, "
                    "render_fingerprint, locale, filename, creation_reason, renderer_version) "
                    "VALUES (:id, :company_id, :invoice_id, 'FORMAL_DOCUMENT', "
                    "decode('25504446', 'hex'), :sha, :fingerprint, 'en', 'cross.pdf', "
                    "'DOWNLOAD', 'test')"
                ),
                {
                    "id": uuid.uuid4(),
                    "company_id": uuid.uuid4(),
                    "invoice_id": standard["id"],
                    "sha": hashlib.sha256(b"%PDF").hexdigest(),
                    "fingerprint": "4" * 64,
                },
            )
        await session.rollback()

        await set_rls_company(session, seeds["company_id"])
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "INSERT INTO document_artifact "
                    "(id, company_id, invoice_id, artifact_kind, pdf_bytes, sha256, "
                    "render_fingerprint, locale, filename, creation_reason, renderer_version) "
                    "VALUES (:id, :company_id, :invoice_id, 'FORMAL_DOCUMENT', "
                    "decode('25504446', 'hex'), :sha, :fingerprint, 'en', 'forged.pdf', "
                    "'DOWNLOAD', 'test')"
                ),
                {
                    "id": uuid.uuid4(),
                    "company_id": seeds["company_id"],
                    "invoice_id": standard["id"],
                    "sha": "0" * 64,
                    "fingerprint": "1" * 64,
                },
            )
        await session.rollback()

        await set_rls_company(session, seeds["company_id"])
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "INSERT INTO document_artifact "
                    "(id, company_id, artifact_kind, pdf_bytes, sha256, render_fingerprint, "
                    "locale, filename, creation_reason, renderer_version) "
                    "VALUES (:id, :company_id, 'FORMAL_DOCUMENT', decode('25504446', 'hex'), "
                    ":sha, :fingerprint, 'en', 'orphan.pdf', 'DOWNLOAD', 'test')"
                ),
                {
                    "id": uuid.uuid4(),
                    "company_id": seeds["company_id"],
                    "sha": hashlib.sha256(b"%PDF").hexdigest(),
                    "fingerprint": "2" * 64,
                },
            )
        await session.rollback()

    # The real NOBYPASSRLS application role has no DELETE policy.  PostgreSQL
    # reports this as a successful statement affecting zero rows.
    async with runtime_session_maker() as session:
        await set_rls_company(session, seeds["company_id"])
        deleted = await session.execute(
            text("DELETE FROM document_artifact WHERE id = :id"), {"id": artifact["id"]}
        )
        assert deleted.rowcount == 0  # type: ignore[attr-defined]
        await session.rollback()


async def test_upload_artifact_trigger_is_first_invoice_artifact_only(
    db_client: AsyncClient,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Raw SQL cannot bypass UPLOAD's issued/zero-artifact boundary."""
    documents, seeds = await _all_formal_documents(db_client)
    fresh_invoice_id = documents["FINAL"]["id"]
    pdf_bytes = b"%PDF-1.4 uploaded historical bytes"
    insert = text(
        "INSERT INTO document_artifact "
        "(id, company_id, invoice_id, artifact_kind, pdf_bytes, sha256, render_fingerprint, "
        "locale, filename, creation_reason, renderer_version) "
        "VALUES (:id, :company_id, :invoice_id, 'FORMAL_DOCUMENT', :pdf_bytes, :sha, "
        ":fingerprint, 'en', 'legacy.pdf', 'UPLOAD', 'external-upload')"
    )
    params = {
        "id": uuid.uuid4(),
        "company_id": seeds["company_id"],
        "invoice_id": fresh_invoice_id,
        "pdf_bytes": pdf_bytes,
        "sha": hashlib.sha256(pdf_bytes).hexdigest(),
        "fingerprint": "a" * 64,
    }

    async with db_session_maker() as session:
        await set_rls_company(session, seeds["company_id"])
        await session.execute(insert, params)
        await session.commit()

        await set_rls_company(session, seeds["company_id"])
        with pytest.raises(
            DBAPIError, match="uploaded artifact requires an invoice with no artifacts"
        ):
            await session.execute(insert, {**params, "id": uuid.uuid4(), "fingerprint": "b" * 64})
        await session.rollback()

        # Refund artifacts retain their existing DOWNLOAD/SEND multi-version behaviour.
        refund_bytes = b"%PDF-1.4 refund exact bytes"
        await set_rls_company(session, seeds["company_id"])
        await session.execute(
            text(
                "INSERT INTO document_artifact "
                "(id, company_id, refund_payment_id, artifact_kind, pdf_bytes, sha256, "
                "render_fingerprint, locale, filename, creation_reason, renderer_version) "
                "VALUES (:id, :company_id, :refund_id, 'REFUND_CONFIRMATION', :pdf_bytes, "
                ":sha, :fingerprint, 'en', 'refund.pdf', 'DOWNLOAD', 'test')"
            ),
            {
                "id": uuid.uuid4(),
                "company_id": seeds["company_id"],
                "refund_id": documents["REFUND"]["id"],
                "pdf_bytes": refund_bytes,
                "sha": hashlib.sha256(refund_bytes).hexdigest(),
                "fingerprint": "c" * 64,
            },
        )
        await session.commit()


async def test_artifact_byte_identity_is_owner_kind_and_sha_scoped(
    db_client: AsyncClient,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Locale/version/fingerprint are first-retention audit metadata only."""
    documents, seeds = await _all_formal_documents(db_client)
    invoice_id = uuid.UUID(documents["FINAL"]["id"])
    company_id = uuid.UUID(seeds["company_id"])
    pdf_bytes = b"%PDF-1.4 exact retained bytes"

    async with db_session_maker() as session:
        first, created = await retain_invoice_artifact(
            session,
            invoice_id=invoice_id,
            company_id=company_id,
            pdf_bytes=pdf_bytes,
            render_fingerprint="a" * 64,
            locale="en",
            filename="same.pdf",
            reason=DocumentArtifactReason.DOWNLOAD,
        )
        same, duplicate = await retain_invoice_artifact(
            session,
            invoice_id=invoice_id,
            company_id=company_id,
            pdf_bytes=pdf_bytes,
            render_fingerprint="b" * 64,
            locale="en",
            filename="same.pdf",
            reason=DocumentArtifactReason.SEND,
        )
        zh, zh_created = await retain_invoice_artifact(
            session,
            invoice_id=invoice_id,
            company_id=company_id,
            pdf_bytes=pdf_bytes,
            render_fingerprint="c" * 64,
            locale="zh",
            filename="same.pdf",
            reason=DocumentArtifactReason.DOWNLOAD,
        )
        assert created and not duplicate and not zh_created
        assert same.id == first.id == zh.id
        # The first row retains its original audit metadata; later equal bytes
        # may not rewrite it merely because the request locale/version differs.
        assert first.locale == "en" and first.render_fingerprint == "a" * 64

        refund_first, refund_created = await retain_refund_artifact(
            session,
            refund_id=uuid.UUID(documents["REFUND"]["id"]),
            company_id=company_id,
            pdf_bytes=pdf_bytes,
            render_fingerprint="d" * 64,
            locale="en",
            filename="refund.pdf",
            reason=DocumentArtifactReason.DOWNLOAD,
        )
        refund_zh, refund_zh_created = await retain_refund_artifact(
            session,
            refund_id=uuid.UUID(documents["REFUND"]["id"]),
            company_id=company_id,
            pdf_bytes=pdf_bytes,
            render_fingerprint="e" * 64,
            locale="zh",
            filename="refund.pdf",
            reason=DocumentArtifactReason.SEND,
        )
        assert refund_created and not refund_zh_created
        assert refund_first.id == refund_zh.id
        # This proves the database constraint, rather than only the service
        # lookup, rejects an equal-byte cross-locale duplicate.
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(
                    DocumentArtifact(
                        company_id=company_id,
                        invoice_id=invoice_id,
                        artifact_kind=first.artifact_kind,
                        pdf_bytes=pdf_bytes,
                        sha256=hashlib.sha256(pdf_bytes).hexdigest(),
                        render_fingerprint="f" * 64,
                        locale="zh",
                        filename="same-zh.pdf",
                        creation_reason=DocumentArtifactReason.SEND,
                        renderer_version="other-pipeline",
                    )
                )
                await session.flush()
        await session.commit()


async def test_render_identity_allows_the_same_fingerprint_after_pipeline_upgrade(
    db_client: AsyncClient,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Different bytes survive even when fingerprint/version combinations match."""
    import jai.services.artifacts as artifacts_service

    documents, seeds = await _all_formal_documents(db_client)
    invoice_id = uuid.UUID(documents["FINAL"]["id"])
    company_id = uuid.UUID(seeds["company_id"])
    fingerprint = "e" * 64

    async with db_session_maker() as session:
        with patch.object(artifacts_service, "RENDERER_VERSION", "old-pipeline"):
            old, created = await retain_invoice_artifact(
                session,
                invoice_id=invoice_id,
                company_id=company_id,
                pdf_bytes=b"%PDF old",
                render_fingerprint=fingerprint,
                locale="en",
                filename="old.pdf",
                reason=DocumentArtifactReason.DOWNLOAD,
            )
            assert created
        await session.commit()

    async with db_session_maker() as session:
        with patch.object(artifacts_service, "RENDERER_VERSION", "current-pipeline"):
            current, created = await retain_invoice_artifact(
                session,
                invoice_id=invoice_id,
                company_id=company_id,
                pdf_bytes=b"%PDF current",
                render_fingerprint=fingerprint,
                locale="en",
                filename="current.pdf",
                reason=DocumentArtifactReason.DOWNLOAD,
            )
            assert created
        await session.commit()

    assert old.id != current.id

    async with db_session_maker() as session:
        with patch.object(artifacts_service, "RENDERER_VERSION", "current-pipeline"):
            canonical, created = await retain_invoice_artifact(
                session,
                invoice_id=invoice_id,
                company_id=company_id,
                pdf_bytes=b"%PDF nondeterministic bytes",
                render_fingerprint=fingerprint,
                locale="en",
                filename="later.pdf",
                reason=DocumentArtifactReason.SEND,
            )
            different, different_created = await retain_invoice_artifact(
                session,
                invoice_id=invoice_id,
                company_id=company_id,
                pdf_bytes=b"%PDF distinct presentation",
                render_fingerprint="f" * 64,
                locale="en",
                filename="distinct.pdf",
                reason=DocumentArtifactReason.DOWNLOAD,
            )
        await session.commit()

    assert not created and canonical.id == current.id
    assert canonical.pdf_bytes == b"%PDF current"
    assert different_created and different.id != current.id


async def test_download_and_send_use_the_canonical_artifact_bytes(
    db_client: AsyncClient,
) -> None:
    """A nondeterministic renderer never leaks bytes outside canonical retention."""
    documents, _ = await _all_formal_documents(db_client)
    invoice_id = documents["STANDARD"]["id"]
    first_bytes = b"%PDF canonical bytes"
    later_bytes = b"%PDF nondeterministic later bytes"

    async def first_render(*_args: object, **_kwargs: object) -> tuple[bytes, str, str, str]:
        return first_bytes, "canonical.pdf", "en", "a" * 64

    async def later_render(*_args: object, **_kwargs: object) -> tuple[bytes, str, str, str]:
        return later_bytes, "later.pdf", "en", "a" * 64

    with patch("jai.services.pdf.render_invoice_pdf_artifact", side_effect=first_render):
        first_download = await db_client.get(f"/api/v1/invoices/{invoice_id}/pdf")
    assert first_download.status_code == 200 and first_download.content == first_bytes

    captured: dict[str, Any] = {}

    async def _capture(**kwargs: Any) -> None:
        captured.update(kwargs)

    with (
        patch("jai.services.pdf.render_invoice_pdf_artifact", side_effect=later_render),
        patch("jai.services.email._get_smtp_config", return_value=_smtp()),
        patch("jai.services.email._send_mail", side_effect=_capture),
    ):
        second_download = await db_client.get(f"/api/v1/invoices/{invoice_id}/pdf")
        sent = await db_client.post(
            f"/api/v1/invoices/{invoice_id}/send",
            json={"to": "customer@example.com"},
        )
    assert second_download.status_code == 200 and second_download.content == first_bytes
    assert sent.status_code == 200 and captured["attachment_bytes"] == first_bytes
    artifacts = await _invoice_artifacts(db_client, invoice_id)
    assert len(artifacts) == 1
    assert artifacts[0]["sha256"] == hashlib.sha256(first_bytes).hexdigest()


@pytest.mark.parametrize(
    ("locale", "kind", "required", "forbidden"),
    [
        ("en", "STANDARD", "Invoice", None),
        ("en", "ADVANCE", "Advance Invoice", None),
        ("en", "FINAL", "Final Invoice", None),
        ("en", "CREDIT_NOTE", "Credit Note", "Amount due"),
        ("zh", "STANDARD", "发票", None),
        ("zh", "ADVANCE", "预付款发票", None),
        ("zh", "FINAL", "最终结算发票", None),
        ("zh", "CREDIT_NOTE", "贷项通知单", "应付金额"),
    ],
)
async def test_formal_email_defaults_are_kind_aware(
    db_client: AsyncClient,
    locale: str,
    kind: str,
    required: str,
    forbidden: str | None,
) -> None:
    """Real formal sends capture EN/ZH subject/body/attachment per kind."""
    documents, _ = await _all_formal_documents(db_client)
    captured: dict[str, Any] = {}

    async def _capture(**kwargs: Any) -> None:
        captured.update(kwargs)

    with (
        patch("jai.services.email._get_smtp_config", return_value=_smtp()),
        patch("jai.services.email._send_mail", side_effect=_capture),
    ):
        response = await db_client.post(
            f"/api/v1/invoices/{documents[kind]['id']}/send",
            json={"to": "customer@example.com", "locale": locale},
        )
    assert response.status_code == 200, response.text
    content = f"{captured['subject']}\n{captured['html_body']}"
    assert required in content
    if forbidden is not None:
        assert forbidden not in content
        if locale == "en":
            assert "Invoice" not in content
        else:
            assert "发票" not in content
    assert captured["attachment_bytes"]
    if kind == "CREDIT_NOTE":
        assert documents[kind]["invoice_number"] in content
        assert documents["STANDARD"]["invoice_number"] in content


@pytest.mark.parametrize("locale", ["en", "zh"])
async def test_refund_email_default_has_real_credit_and_source_references(
    db_client: AsyncClient,
    locale: str,
) -> None:
    """Refund defaults use the configured kind template and stable references."""
    documents, _ = await _all_formal_documents(db_client)
    captured: dict[str, Any] = {}

    async def _capture(**kwargs: Any) -> None:
        captured.update(kwargs)

    with (
        patch("jai.services.email._get_smtp_config", return_value=_smtp()),
        patch("jai.services.email._send_mail", side_effect=_capture),
    ):
        response = await db_client.post(
            f"/api/v1/payments/{documents['REFUND']['id']}/send-refund-confirmation",
            json={"to": "customer@example.com", "locale": locale},
        )
    assert response.status_code == 200, response.text
    content = f"{captured['subject']}\n{captured['html_body']}"
    assert documents["CREDIT_NOTE"]["invoice_number"] in content
    assert documents["STANDARD"]["invoice_number"] in content
    assert captured["attachment_bytes"]


@pytest.mark.parametrize("locale", ["en", "zh"])
async def test_formal_send_uses_the_pdf_party_snapshot_for_email_placeholders(
    db_client: AsyncClient,
    db_session_maker: async_sessionmaker[AsyncSession],
    locale: str,
) -> None:
    """Formal sends keep email identity aligned with the frozen attachment."""
    documents, seeds = await _all_formal_documents(db_client)
    standard = documents["STANDARD"]
    refund = documents["REFUND"]
    company = (await db_client.get("/api/v1/company")).json()
    customer = (await db_client.get(f"/api/v1/customers/{standard['customer_id']}")).json()
    frozen_company = company["name"]
    frozen_customer = customer["name"]
    live_company = "Live company name"
    live_customer = "Live customer name"

    async with db_session_maker() as session:
        await set_rls_company(session, uuid.UUID(seeds["company_id"]))
        await session.execute(
            text("UPDATE company SET name = :name WHERE id = :id"),
            {"name": live_company, "id": seeds["company_id"]},
        )
        await session.execute(
            text("UPDATE customer SET name = :name WHERE id = :id"),
            {"name": live_customer, "id": standard["customer_id"]},
        )
        await session.commit()

    async def _send(path: str, kind: str) -> tuple[dict[str, Any], dict[str, Any]]:
        captured: dict[str, Any] = {}

        async def _capture(**kwargs: Any) -> None:
            captured.update(kwargs)

        with (
            patch("jai.services.email._get_smtp_config", return_value=_smtp()),
            patch("jai.services.email._send_mail", side_effect=_capture),
        ):
            response = await db_client.post(
                path,
                json={
                    "to": "selected-recipient@example.com",
                    "locale": locale,
                    "subject": "{COMPANY_NAME} / {CUSTOMER_NAME}",
                    "body": "{COMPANY_NAME} / {CUSTOMER_NAME}",
                },
            )
        assert response.status_code == 200, response.text
        sent = response.json()
        assert sent["status"] == "SENT"
        assert sent["to_email"] == "selected-recipient@example.com"
        assert sent["locale"] == locale
        assert sent["artifact_id"] is not None
        assert frozen_company in captured["subject"] and frozen_customer in captured["subject"]
        assert frozen_company in captured["html_body"] and frozen_customer in captured["html_body"]
        assert live_company not in captured["subject"] + captured["html_body"]
        assert live_customer not in captured["subject"] + captured["html_body"]
        assert frozen_company in _pdf_text(captured["attachment_bytes"])
        assert frozen_customer in _pdf_text(captured["attachment_bytes"])
        assert kind in {"invoice", "refund"}
        async with db_session_maker() as session:
            await set_rls_company(session, uuid.UUID(seeds["company_id"]))
            email = await session.get(EmailLog, uuid.UUID(sent["id"]))
            artifact = await session.get(DocumentArtifact, uuid.UUID(sent["artifact_id"]))
            assert email is not None and artifact is not None
            assert email.subject == captured["subject"]
            assert email.body_snapshot == captured["html_body"]
            assert email.to_email == "selected-recipient@example.com" and email.locale == locale
            assert email.artifact_id == artifact.id
            assert artifact.pdf_bytes == captured["attachment_bytes"]
            assert artifact.sha256 == hashlib.sha256(captured["attachment_bytes"]).hexdigest()
        return sent, captured

    invoice_sent, invoice_capture = await _send(
        f"/api/v1/invoices/{standard['id']}/send", "invoice"
    )
    refund_sent, refund_capture = await _send(
        f"/api/v1/payments/{refund['id']}/send-refund-confirmation", "refund"
    )
    assert invoice_sent["artifact_id"] != refund_sent["artifact_id"]
    assert invoice_capture["attachment_bytes"] != refund_capture["attachment_bytes"]


@pytest.mark.parametrize(
    ("locale", "required", "forbidden"),
    [
        (
            "en",
            (
                "Refund Date",
                "Refund Method",
                "Refund Amount",
                "Refund Reference",
                "Remaining Refund Entitlement",
            ),
            ("Payment Date", "Payment Method", "Amount Paid"),
        ),
        (
            "zh",
            ("退款日期", "退款方式", "退款金额", "退款参考号", "退款后剩余额度"),
            ("收款日期", "收款方式", "本次收款"),
        ),
    ],
)
async def test_refund_output_uses_entitlement_projection_and_refund_labels(
    db_client: AsyncClient,
    locale: str,
    required: tuple[str, ...],
    forbidden: tuple[str, ...],
) -> None:
    """PDF output must share Step 7 entitlement, not cash coverage, wording."""
    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    source = await _issued_standard(db_client, seeds["rates"]["NL standard (21%)"]["id"])
    await _pay(db_client, source["id"], "60.50")
    credit = await _issue_full_credit(db_client, source["id"])
    method = await db_client.post(
        "/api/v1/payment-methods", json={"name": "Refund bank", "active": True}
    )
    assert method.status_code == 201, method.text
    created = await db_client.post(
        f"/api/v1/credit-notes/{credit['id']}/refunds",
        json={
            "payment_date": "2026-02-04",
            "amount": "20",
            "payment_method_id": method.json()["id"],
            "reference": "refund-reference",
        },
    )
    assert created.status_code == 201, created.text
    refund = created.json()["items"][0]
    collection = await db_client.get(f"/api/v1/credit-notes/{credit['id']}/refunds")
    assert collection.status_code == 200, collection.text
    assert collection.json()["remaining_entitlement"] == "101.000"
    credit_read = await db_client.get(f"/api/v1/invoices/{credit['id']}")
    assert credit_read.status_code == 200, credit_read.text
    assert credit_read.json()["refund_due_amount"] == "40.500"

    confirmation = await db_client.get(
        f"/api/v1/payments/{refund['id']}/refund-confirmation/preview", params={"locale": locale}
    )
    assert confirmation.status_code == 200, confirmation.text
    confirmation_text = _pdf_text(confirmation.content)
    assert "101.00" in confirmation_text
    for label in required:
        assert label in confirmation_text
    for label in forbidden:
        assert label not in confirmation_text

    credit_pdf = await db_client.get(
        f"/api/v1/invoices/{credit['id']}/pdf", params={"locale": locale, "preview": "true"}
    )
    assert credit_pdf.status_code == 200, credit_pdf.text
    credit_text = _pdf_text(credit_pdf.content)
    for label in (required[0], required[2], required[3]):
        assert label.upper() in credit_text.upper()
    for label in forbidden:
        assert label not in credit_text


async def test_kind_specific_company_templates_override_defaults(
    db_client: AsyncClient,
) -> None:
    """Stored Credit/Refund settings take precedence over typed defaults."""
    documents, _ = await _all_formal_documents(db_client)
    templates_response = await db_client.get("/api/v1/settings/email-templates")
    assert templates_response.status_code == 200
    templates = templates_response.json()
    templates["credit_note"]["en"] = {
        "subject": "Custom credit {CREDIT_NOTE_NUMBER}",
        "body": "Custom source {SOURCE_DOCUMENT_NUMBER}",
    }
    templates["refund"]["zh"] = {
        "subject": "自定义退款 {CREDIT_NOTE_NUMBER}",
        "body": "自定义来源 {SOURCE_DOCUMENT_NUMBER}",
    }
    updated = await db_client.put("/api/v1/settings/email-templates", json=templates)
    assert updated.status_code == 200, updated.text

    captured: dict[str, Any] = {}

    async def _capture(**kwargs: Any) -> None:
        captured.update(kwargs)

    with (
        patch("jai.services.email._get_smtp_config", return_value=_smtp()),
        patch("jai.services.email._send_mail", side_effect=_capture),
    ):
        credit_response = await db_client.post(
            f"/api/v1/invoices/{documents['CREDIT_NOTE']['id']}/send",
            json={"to": "customer@example.com", "locale": "en"},
        )
    assert credit_response.status_code == 200, credit_response.text
    assert "Custom credit" in captured["subject"]
    assert documents["STANDARD"]["invoice_number"] in captured["html_body"]

    captured.clear()
    with (
        patch("jai.services.email._get_smtp_config", return_value=_smtp()),
        patch("jai.services.email._send_mail", side_effect=_capture),
    ):
        refund_response = await db_client.post(
            f"/api/v1/payments/{documents['REFUND']['id']}/send-refund-confirmation",
            json={"to": "customer@example.com", "locale": "zh"},
        )
    assert refund_response.status_code == 200, refund_response.text
    assert "自定义退款" in captured["subject"]
    assert documents["STANDARD"]["invoice_number"] in captured["html_body"]


async def test_issued_snapshot_locale_and_null_logo_survive_master_data_mutation(
    db_client: AsyncClient,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """No-override legal output uses snapshot locale and explicit NULL logo."""
    documents, seeds = await _all_formal_documents(db_client)
    standard = documents["STANDARD"]
    first = await db_client.get(f"/api/v1/invoices/{standard['id']}/pdf")
    assert first.status_code == 200
    assert (await _invoice_artifacts(db_client, standard["id"]))[0]["locale"] == "en"

    logo_id = uuid.uuid4()
    async with db_session_maker() as session:
        await session.execute(
            text(
                "INSERT INTO binary_asset (id, content, mime_type, filename, byte_size) "
                "VALUES (:id, decode('3c7376672f3e', 'hex'), 'image/svg+xml', 'later.svg', 6)"
            ),
            {"id": logo_id},
        )
        await session.execute(
            text("UPDATE company SET logo_id = :logo WHERE id = :company"),
            {"logo": logo_id, "company": seeds["company_id"]},
        )
        await session.execute(
            text(
                "UPDATE customer SET locale = 'zh' WHERE id = "
                "(SELECT customer_id FROM invoice WHERE id = :invoice)"
            ),
            {"invoice": standard["id"]},
        )
        await session.commit()

    stable = await db_client.get(f"/api/v1/invoices/{standard['id']}/pdf")
    assert stable.status_code == 200
    assert stable.content == first.content
    artifacts = await _invoice_artifacts(db_client, standard["id"])
    assert len(artifacts) == 1 and artifacts[0]["locale"] == "en"
    override = await db_client.get(
        f"/api/v1/invoices/{standard['id']}/pdf", params={"locale": "zh"}
    )
    assert override.status_code == 200
    assert {row["locale"] for row in await _invoice_artifacts(db_client, standard["id"])} == {
        "en",
        "zh",
    }


async def test_artifact_collection_hides_missing_and_foreign_parent_identity(
    db_client: AsyncClient,
) -> None:
    """Collection routes validate their parent before querying child rows."""
    await _full_auth(db_client)
    await _setup_company(db_client)
    missing = str(uuid.uuid4())
    assert (await db_client.get(f"/api/v1/invoices/{missing}/artifacts")).status_code == 404
    assert (await db_client.get(f"/api/v1/payments/{missing}/artifacts")).status_code == 404


async def test_concurrent_same_identity_artifact_retention_reuses_one_row(
    db_client: AsyncClient,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Concurrent equal-byte writers recover the one owner-scoped winner."""
    documents, seeds = await _all_formal_documents(db_client)
    invoice_id = uuid.UUID(documents["FINAL"]["id"])
    company_id = uuid.UUID(seeds["company_id"])

    async def _retain() -> uuid.UUID:
        async with db_session_maker() as session:
            artifact, _ = await retain_invoice_artifact(
                session,
                invoice_id=invoice_id,
                company_id=company_id,
                pdf_bytes=b"%PDF concurrent",
                render_fingerprint="d" * 64,
                locale="en",
                filename="concurrent.pdf",
                reason=DocumentArtifactReason.DOWNLOAD,
            )
            await session.commit()
            return artifact.id

    first, second = await asyncio.gather(_retain(), _retain())
    assert first == second


async def test_concurrent_same_identity_refund_artifact_retention_reuses_one_row(
    db_client: AsyncClient,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Refund owner retention has the same unique-constraint recovery path."""
    documents, seeds = await _all_formal_documents(db_client)
    refund_id = uuid.UUID(documents["REFUND"]["id"])
    company_id = uuid.UUID(seeds["company_id"])

    async def _retain(locale: str) -> uuid.UUID:
        async with db_session_maker() as session:
            artifact, _ = await retain_refund_artifact(
                session,
                refund_id=refund_id,
                company_id=company_id,
                pdf_bytes=b"%PDF concurrent refund",
                render_fingerprint=("a" if locale == "en" else "b") * 64,
                locale=locale,
                filename="concurrent-refund.pdf",
                reason=DocumentArtifactReason.DOWNLOAD,
            )
            await session.commit()
            return artifact.id

    first, second = await asyncio.gather(_retain("en"), _retain("zh"))
    assert first == second


async def test_si1_copy_failure_keeps_all_formal_artifacts_and_smtp_log(db_client):
    """Real route transactions must retain PDFs after SMTP accepted + IMAP failed."""
    from unittest.mock import AsyncMock, MagicMock

    from jai.schemas.setting import SentCopySettings

    documents, _ = await _all_formal_documents(db_client)
    cfg = _smtp()
    cfg.sent_copy = SentCopySettings(enabled=True, host="imap.example.test")
    imap = MagicMock()
    imap.login.return_value = ("OK", [])
    imap.capability.return_value = ("OK", [b"IMAP4rev1"])
    imap.list.return_value = ("OK", [b'(\\Sent) "/" Sent'])
    imap.append.side_effect = TimeoutError("SECRET MIME")
    for kind in ["STANDARD", "ADVANCE", "FINAL", "CREDIT_NOTE", "REFUND"]:
        document = documents[kind]
        path = (
            f"/api/v1/payments/{document['id']}/send-refund-confirmation"
            if kind == "REFUND"
            else f"/api/v1/invoices/{document['id']}/send"
        )
        with (
            patch("jai.services.email._get_smtp_config", return_value=cfg),
            patch("aiosmtplib.send", new_callable=AsyncMock) as smtp,
            patch("jai.services.sent_copy.imaplib.IMAP4_SSL", return_value=imap),
        ):
            response = await db_client.post(
                path, json={"to": "test@example.com", "cc": "cc@example.com"}
            )
        assert response.status_code == 200, response.text
        log = response.json()
        assert log["status"] == "SENT" and log["sent_at"] is not None
        assert log["artifact_id"] and log["sent_copy_status"] == "FAILED"
        assert log["sent_copy_error_code"] == "IMAP_TIMEOUT" and log["error_message"] is None
        smtp.assert_called_once()
        artifacts = (
            await _refund_artifacts(db_client, document["id"])
            if kind == "REFUND"
            else await _invoice_artifacts(db_client, document["id"])
        )
        assert any(item["id"] == log["artifact_id"] for item in artifacts)
        raw = smtp.call_args.args[0]
        assert imap.append.call_args.args[3] == raw
        assert "SECRET" not in response.text

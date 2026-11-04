"""Step 3 Quote selection/lifecycle integration coverage."""

from __future__ import annotations

import asyncio
import io
import logging
import uuid
from datetime import UTC, datetime

import pyotp
import pytest
from httpx import AsyncClient
from pypdf import PdfWriter
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from test_quote_payment_integration import _create_customer, _create_quote, _setup_company

from jai.models.company import Company
from jai.models.pdf_appendix import PdfAppendixFile
from jai.models.user import User

pytestmark = pytest.mark.integration


def _pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_quote_appendix_openapi_declares_stable_error_contract() -> None:
    from jai.main import app

    paths = app.openapi()["paths"]
    assert {"404", "422"} <= set(
        paths["/api/v1/quotes/{quote_id}/appendix-files"]["get"]["responses"]
    )
    upload_responses = paths["/api/v1/quotes/{quote_id}/appendix-files"]["post"]["responses"]
    assert {"404", "409", "413", "422", "500"} <= set(upload_responses)
    assert (
        upload_responses["422"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/PdfAppendixErrorResponse"
    )
    for method in ("post", "put"):
        responses = paths[
            "/api/v1/quotes" if method == "post" else "/api/v1/quotes/{quote_id}"
        ][method]["responses"]
        assert {"404", "409", "413", "422"} <= set(responses)
        for status_code in ("404", "409", "422"):
            schema = responses[status_code]["content"]["application/json"]["schema"]
            assert "anyOf" in schema
            refs = {item["$ref"] for item in schema["anyOf"]}
            assert "#/components/schemas/PdfAppendixErrorResponse" in refs
            assert "#/components/schemas/LegacyDetailErrorResponse" in refs
            if status_code == "422":
                assert "#/components/schemas/HTTPValidationError" in refs
    legacy_schema = app.openapi()["components"]["schemas"]["LegacyDetailErrorResponse"]
    assert legacy_schema["properties"]["detail"]["type"] == "string"
    assert legacy_schema["required"] == ["detail"]
    assert legacy_schema["type"] == "object"


@pytest.mark.asyncio
async def test_quote_write_errors_keep_legacy_and_appendix_response_shapes(
    db_client: AsyncClient,
) -> None:
    await _login(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    rate_id = seeds["rates"]["NL standard (21%)"]["id"]
    payload = {
        "customer_id": customer_id,
        "quote_date": "2026-01-10",
        "tax_mode": "LINE",
        "amounts_include_vat": False,
        "lines": [
            {
                "name": "Service",
                "quantity": "1",
                "unit_price": "100.000",
                "vat_rate_id": rate_id,
            }
        ],
    }

    legacy_post_422 = await db_client.post("/api/v1/quotes", json={**payload, "currency": "USD"})
    assert legacy_post_422.status_code == 422
    assert legacy_post_422.json() == {
        "detail": "M6 only supports quotes in company base currency (EUR); got USD."
    }
    validation_post_422 = await db_client.post("/api/v1/quotes", json={})
    assert validation_post_422.status_code == 422
    assert isinstance(validation_post_422.json()["detail"], list)
    assert validation_post_422.json()["detail"]

    missing_appendix_post_404 = await db_client.post(
        "/api/v1/quotes",
        json={
            **payload,
            "appendix_file_ids": [str(uuid.uuid4())],
            "expected_appendix_version": 0,
        },
    )
    assert missing_appendix_post_404.status_code == 404
    assert missing_appendix_post_404.json() == {
        "detail": {
            "code": "PDF_APPENDIX_NOT_FOUND",
            "message": "PDF_APPENDIX_NOT_FOUND",
        }
    }

    quote = await _create_quote(db_client, customer_id, rate_id)
    missing_put_404 = await db_client.put(f"/api/v1/quotes/{uuid.uuid4()}", json=payload)
    assert missing_put_404.status_code == 404
    assert missing_put_404.json() == {"detail": "Quote not found."}

    assert (
        await db_client.post(f"/api/v1/quotes/{quote['id']}/status", json={"status": "SENT"})
    ).status_code == 200
    assert (
        await db_client.post(f"/api/v1/quotes/{quote['id']}/status", json={"status": "ACCEPTED"})
    ).status_code == 200
    accepted_put_409 = await db_client.put(f"/api/v1/quotes/{quote['id']}", json=payload)
    assert accepted_put_409.status_code == 409
    assert accepted_put_409.json() == {
        "detail": (
            "Cannot edit an ACCEPTED quote. Reactivate it first "
            "or edit the converted invoice directly."
        )
    }

    appendix_upload = await db_client.post(
        "/api/v1/pdf-library",
        files={"file": ("appendix.pdf", _pdf(), "application/pdf")},
    )
    assert appendix_upload.status_code == 201
    appendix_quote = await _create_quote(db_client, customer_id, rate_id)
    selected = await db_client.put(
        f"/api/v1/quotes/{appendix_quote['id']}",
        json={
            **payload,
            "appendix_file_ids": [appendix_upload.json()["id"]],
            "expected_appendix_version": 0,
        },
    )
    assert selected.status_code == 200
    appendix_conflict = await db_client.put(
        f"/api/v1/quotes/{appendix_quote['id']}",
        json={
            **payload,
            "appendix_file_ids": [],
            "expected_appendix_version": 0,
        },
    )
    assert appendix_conflict.status_code == 409
    assert appendix_conflict.json() == {
        "detail": {
            "code": "QUOTE_APPENDIX_VERSION_CONFLICT",
            "message": "QUOTE_APPENDIX_VERSION_CONFLICT",
        }
    }


async def _login(client: AsyncClient) -> None:
    assert (
        await client.post(
            "/api/v1/auth/register",
            json={"email": "m17-selection@example.com", "password": "testpassword1"},
        )
    ).status_code == 201
    assert (
        await client.post(
            "/api/v1/auth/login",
            json={"email": "m17-selection@example.com", "password": "testpassword1"},
        )
    ).status_code == 200
    setup = await client.post("/api/v1/auth/mfa/setup")
    assert setup.status_code == 200
    code = pyotp.TOTP(setup.json()["secret"]).now()
    assert (await client.post("/api/v1/auth/mfa/verify", json={"code": code})).status_code == 204


@pytest.mark.asyncio
async def test_quote_selection_versions_snapshot_and_output_guard(
    db_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _login(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    library_upload = await db_client.post(
        "/api/v1/pdf-library",
        files={"file": ("manual.pdf", _pdf(), "application/pdf"), "title": (None, "First title")},
    )
    assert library_upload.status_code == 201, library_upload.text
    file_id = library_upload.json()["id"]
    rate_id = seeds["rates"]["NL standard (21%)"]["id"]
    quote = await _create_quote(db_client, customer_id, rate_id)
    quote_id = quote["id"]
    payload = {
        "customer_id": customer_id,
        "quote_date": "2026-01-10",
        "tax_mode": "LINE",
        "amounts_include_vat": False,
        "lines": [
            {
                "name": "Standard service",
                "quantity": "1",
                "unit_price": "100.000",
                "vat_rate_id": rate_id,
            }
        ],
    }

    # New Quote creation accepts a nonempty initial selection without a version
    # token and initializes its first saved selection at version 1.
    null_selection = await db_client.post(
        "/api/v1/quotes", json={**payload, "appendix_file_ids": None}
    )
    assert null_selection.status_code == 422
    assert null_selection.json()["detail"]["code"] == "PDF_APPENDIX_INVALID"
    new_quote = await db_client.post(
        "/api/v1/quotes",
        json={**payload, "appendix_file_ids": [file_id]},
    )
    assert new_quote.status_code == 201, new_quote.text
    assert new_quote.json()["appendix_version"] == 1
    assert new_quote.json()["appendices"][0]["source"] == "LIBRARY"
    missing_file = await db_client.put(
        f"/api/v1/quotes/{quote_id}",
        json={
            **payload,
            "appendix_file_ids": [str(uuid.uuid4())],
            "expected_appendix_version": 0,
        },
    )
    assert missing_file.status_code == 404
    assert missing_file.json()["detail"]["code"] == "PDF_APPENDIX_NOT_FOUND"
    second_upload = await db_client.post(
        "/api/v1/pdf-library",
        files={"file": ("second.pdf", _pdf(), "application/pdf"), "title": (None, "Second title")},
    )
    assert second_upload.status_code == 201
    reordered = await db_client.put(
        f"/api/v1/quotes/{new_quote.json()['id']}",
        json={
            **payload,
            "appendix_file_ids": [second_upload.json()["id"], file_id],
            "expected_appendix_version": 1,
        },
    )
    assert reordered.status_code == 200
    assert reordered.json()["appendix_version"] == 2
    assert [item["file_id"] for item in reordered.json()["appendices"]] == [
        second_upload.json()["id"],
        file_id,
    ]

    created = await db_client.put(
        f"/api/v1/quotes/{quote_id}",
        json={**payload, "appendix_file_ids": [file_id], "expected_appendix_version": 0},
    )
    assert created.status_code == 200, created.text
    assert created.json()["appendix_version"] == 1
    assert created.json()["appendices"][0]["title_snapshot"] == "First title"

    await db_client.patch(f"/api/v1/pdf-library/{file_id}", json={"title": "Renamed"})
    stale = await db_client.put(
        f"/api/v1/quotes/{quote_id}",
        json={
            **payload,
            "notes": "must rollback",
            "appendix_file_ids": [file_id],
            "expected_appendix_version": 0,
        },
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "QUOTE_APPENDIX_VERSION_CONFLICT"
    read = await db_client.get(f"/api/v1/quotes/{quote_id}")
    assert read.json()["notes"] != "must rollback"
    assert read.json()["appendices"][0]["title_snapshot"] == "First title"

    pdf = await db_client.get(f"/api/v1/quotes/{quote_id}/pdf")
    assert pdf.status_code == 409
    assert pdf.json()["detail"]["code"] == "QUOTE_APPENDIX_OUTPUT_UNAVAILABLE"
    sent = await db_client.post(
        f"/api/v1/quotes/{quote_id}/send",
        json={"to": "buyer@example.com", "subject": "Quote", "body": "Please review."},
    )
    assert sent.status_code == 409
    assert sent.json()["detail"]["code"] == "QUOTE_APPENDIX_OUTPUT_UNAVAILABLE"
    email_logs = await db_client.get(f"/api/v1/quotes/{quote_id}/emails")
    assert email_logs.status_code == 200 and email_logs.json()["total"] == 0

    no_op = await db_client.put(
        f"/api/v1/quotes/{quote_id}",
        json={**payload, "appendix_file_ids": [file_id], "expected_appendix_version": 1},
    )
    assert no_op.status_code == 200 and no_op.json()["appendix_version"] == 1
    from jai.config import get_settings

    monkeypatch.setattr(get_settings(), "max_quote_appendix_input_bytes", 1)
    no_op_over_new_limit = await db_client.put(
        f"/api/v1/quotes/{quote_id}",
        json={**payload, "appendix_file_ids": [file_id], "expected_appendix_version": 1},
    )
    assert no_op_over_new_limit.status_code == 413
    assert no_op_over_new_limit.json()["detail"]["code"] == "PDF_APPENDIX_INPUT_LIMIT"
    monkeypatch.setattr(get_settings(), "max_quote_appendix_input_bytes", 15 * 1024 * 1024)
    omitted = await db_client.put(f"/api/v1/quotes/{quote_id}", json=payload)
    assert omitted.status_code == 200 and omitted.json()["appendix_version"] == 1
    missing_version = await db_client.put(
        f"/api/v1/quotes/{quote_id}",
        json={**payload, "appendix_file_ids": [file_id]},
    )
    assert missing_version.status_code == 409
    assert missing_version.json()["detail"]["code"] == "QUOTE_APPENDIX_VERSION_CONFLICT"
    explicit_null = await db_client.put(
        f"/api/v1/quotes/{quote_id}",
        json={**payload, "appendix_file_ids": None, "expected_appendix_version": 1},
    )
    assert explicit_null.status_code == 422
    assert explicit_null.json()["detail"]["code"] == "PDF_APPENDIX_INVALID"
    null_version = await db_client.put(
        f"/api/v1/quotes/{quote_id}",
        json={**payload, "appendix_file_ids": [file_id], "expected_appendix_version": None},
    )
    assert null_version.status_code == 422
    assert null_version.json()["detail"]["code"] == "PDF_APPENDIX_INVALID"
    duplicate_ids = await db_client.put(
        f"/api/v1/quotes/{quote_id}",
        json={**payload, "appendix_file_ids": [file_id, file_id], "expected_appendix_version": 1},
    )
    assert duplicate_ids.status_code == 422
    assert duplicate_ids.json()["detail"]["code"] == "PDF_APPENDIX_DUPLICATE_IDS"
    await db_client.patch(f"/api/v1/pdf-library/{file_id}", json={"is_archived": True})
    archived_retained = await db_client.put(
        f"/api/v1/quotes/{quote_id}",
        json={**payload, "appendix_file_ids": [file_id], "expected_appendix_version": 1},
    )
    assert archived_retained.status_code == 200
    assert archived_retained.json()["appendix_version"] == 1
    cleared = await db_client.put(
        f"/api/v1/quotes/{quote_id}",
        json={**payload, "appendix_file_ids": [], "expected_appendix_version": 1},
    )
    assert cleared.status_code == 200 and cleared.json()["appendix_version"] == 2
    archived_reselect = await db_client.put(
        f"/api/v1/quotes/{quote_id}",
        json={**payload, "appendix_file_ids": [file_id], "expected_appendix_version": 2},
    )
    assert archived_reselect.status_code == 409
    assert archived_reselect.json()["detail"]["code"] == "PDF_LIBRARY_FILE_ARCHIVED"


@pytest.mark.asyncio
async def test_own_upload_is_unselected_and_accepted_quote_is_read_only(
    db_client: AsyncClient,
) -> None:
    await _login(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    rate_id = seeds["rates"]["NL standard (21%)"]["id"]
    quote = await _create_quote(db_client, customer_id, rate_id)
    quote_id = quote["id"]
    upload = await db_client.post(
        f"/api/v1/quotes/{quote_id}/appendix-files",
        files={"file": ("private.pdf", _pdf(), "application/pdf")},
    )
    assert upload.status_code == 201, upload.text
    assert upload.json()["source"] == "QUOTE"
    read = await db_client.get(f"/api/v1/quotes/{quote_id}")
    assert read.json()["appendix_version"] == 0 and read.json()["appendices"] == []
    listing = await db_client.get(f"/api/v1/quotes/{quote_id}/appendix-files")
    assert listing.status_code == 200 and listing.json()["total"] == 1
    invalid_limit = await db_client.get(
        f"/api/v1/quotes/{quote_id}/appendix-files?limit=0"
    )
    assert invalid_limit.status_code == 422
    assert invalid_limit.json()["detail"]["code"] == "PDF_APPENDIX_INVALID"
    invalid_id = await db_client.get("/api/v1/quotes/not-a-uuid/appendix-files")
    assert invalid_id.status_code == 422
    assert invalid_id.json()["detail"]["code"] == "PDF_APPENDIX_INVALID"
    invalid_upload_id = await db_client.post(
        "/api/v1/quotes/not-a-uuid/appendix-files",
        files={"file": ("invalid-id.pdf", _pdf(), "application/pdf")},
    )
    assert invalid_upload_id.status_code == 422
    assert invalid_upload_id.json()["detail"]["code"] == "PDF_APPENDIX_INVALID"
    missing_quote = await db_client.get(f"/api/v1/quotes/{uuid.uuid4()}/appendix-files")
    assert missing_quote.status_code == 404
    assert missing_quote.json()["detail"]["code"] == "PDF_APPENDIX_NOT_FOUND"
    own_id = upload.json()["id"]
    selection = await db_client.put(
        f"/api/v1/quotes/{quote_id}",
        json={
            "customer_id": customer_id,
            "quote_date": "2026-01-10",
            "tax_mode": "LINE",
            "amounts_include_vat": False,
            "lines": [
                {
                    "name": "Service",
                    "quantity": "1",
                    "unit_price": "100",
                    "vat_rate_id": rate_id,
                }
            ],
            "appendix_file_ids": [own_id],
            "expected_appendix_version": 0,
        },
    )
    assert selection.status_code == 200
    assert selection.json()["appendix_version"] == 1
    assert selection.json()["appendices"][0]["source"] == "QUOTE"

    assert (
        await db_client.post(f"/api/v1/quotes/{quote_id}/status", json={"status": "SENT"})
    ).status_code == 200
    accepted = await db_client.post(
        f"/api/v1/quotes/{quote_id}/status", json={"status": "ACCEPTED"}
    )
    assert accepted.status_code == 200
    blocked = await db_client.post(
        f"/api/v1/quotes/{quote_id}/appendix-files",
        files={"file": ("late.pdf", _pdf(), "application/pdf")},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "QUOTE_APPENDICES_READ_ONLY"
    assert (await db_client.get(f"/api/v1/pdf-files/{own_id}/content")).status_code == 200
    deleted = await db_client.delete(f"/api/v1/quotes/{quote_id}")
    assert deleted.status_code == 204
    assert (await db_client.get(f"/api/v1/pdf-files/{own_id}/content")).status_code == 404


@pytest.mark.asyncio
async def test_foreign_quote_appendix_routes_hide_parent_before_upload_body(
    db_client: AsyncClient,
    admin_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A Quote owned by another company is indistinguishable from a missing parent."""
    await _login(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    quote = await _create_quote(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )
    quote_id = uuid.UUID(quote["id"])

    async with admin_session_maker() as session:
        owner = await session.scalar(
            select(User).where(User.email == "m17-selection@example.com")
        )
        assert owner is not None
        other_company = Company(name="M17 foreign Quote owner", base_currency="EUR")
        session.add(other_company)
        await session.flush()
        owner.company_id = other_company.id
        await session.commit()

    listing = await db_client.get(f"/api/v1/quotes/{quote_id}/appendix-files")
    assert listing.status_code == 404
    assert listing.json()["detail"]["code"] == "PDF_APPENDIX_NOT_FOUND"
    assert "ACCEPTED" not in listing.text

    # Deliberately malformed multipart proves parent authorization precedes body parsing.
    upload = await db_client.post(
        f"/api/v1/quotes/{quote_id}/appendix-files",
        content=b"not a multipart body",
        headers={"content-type": "multipart/form-data; boundary=invalid"},
    )
    assert upload.status_code == 404
    assert upload.json()["detail"]["code"] == "PDF_APPENDIX_NOT_FOUND"
    assert "ACCEPTED" not in upload.text

    async with admin_session_maker() as session:
        persisted = await session.scalar(
            select(func.count()).select_from(PdfAppendixFile).where(
                PdfAppendixFile.owner_quote_id == quote_id
            )
        )
    assert persisted == 0


@pytest.mark.asyncio
async def test_duplicate_clears_appendices_and_conversion_preserves_quote_only(
    db_client: AsyncClient,
) -> None:
    await _login(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    rate_id = seeds["rates"]["NL standard (21%)"]["id"]
    quote = await _create_quote(db_client, customer_id, rate_id)
    own = await db_client.post(
        f"/api/v1/quotes/{quote['id']}/appendix-files",
        files={"file": ("quote-only.pdf", _pdf(), "application/pdf")},
    )
    assert own.status_code == 201
    saved = await db_client.put(
        f"/api/v1/quotes/{quote['id']}",
        json={
            "customer_id": customer_id,
            "quote_date": "2026-01-10",
            "tax_mode": "LINE",
            "amounts_include_vat": False,
            "lines": [
                {
                    "name": "Service",
                    "quantity": "1",
                    "unit_price": "100",
                    "vat_rate_id": rate_id,
                }
            ],
            "appendix_file_ids": [own.json()["id"]],
            "expected_appendix_version": 0,
        },
    )
    assert saved.status_code == 200 and len(saved.json()["appendices"]) == 1

    duplicate = await db_client.post(f"/api/v1/quotes/{quote['id']}/duplicate")
    assert duplicate.status_code == 201
    assert duplicate.json()["appendices"] == [] and duplicate.json()["appendix_version"] == 0
    duplicate_files = await db_client.get(
        f"/api/v1/quotes/{duplicate.json()['id']}/appendix-files"
    )
    assert duplicate_files.status_code == 200 and duplicate_files.json()["total"] == 0

    assert (
        await db_client.post(f"/api/v1/quotes/{quote['id']}/status", json={"status": "SENT"})
    ).status_code == 200
    converted = await db_client.post(f"/api/v1/quotes/{quote['id']}/convert")
    assert converted.status_code == 201, converted.text
    original = await db_client.get(f"/api/v1/quotes/{quote['id']}")
    assert original.json()["appendices"][0]["file_id"] == own.json()["id"]
    assert "appendices" not in converted.json()


@pytest.mark.asyncio
async def test_quote_send_releases_snapshot_lock_and_writes_log_under_rls(
    db_client: AsyncClient,
    runtime_db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    import jai.services.email as email_service
    import jai.services.pdf as pdf_service
    from jai.models.quote import Quote
    from jai.schemas.setting import SmtpSettings

    await _login(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    quote = await _create_quote(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )
    alternate_engine = create_async_engine(
        runtime_db_engine.url.render_as_string(hide_password=False), pool_size=1
    )
    maker = async_sessionmaker(alternate_engine, expire_on_commit=False, class_=AsyncSession)
    attachments: list[bytes] = []

    async def fake_config(session: object) -> SmtpSettings:
        return SmtpSettings(host="smtp.invalid", from_email="sender@example.com")

    async def fake_send(**kwargs: object) -> email_service.MailSendResult:
        async with maker() as probe:
            await probe.execute(
                select(Quote.id)
                .where(Quote.id == uuid.UUID(quote["id"]))
                .with_for_update(nowait=True)
            )
            await probe.commit()
        attachment = kwargs.get("attachment_bytes")
        assert isinstance(attachment, bytes)
        attachments.append(attachment)
        return email_service.MailSendResult(smtp_sent_at=datetime.now(UTC))

    monkeypatch.setattr(email_service, "_get_smtp_config", fake_config)
    monkeypatch.setattr(email_service, "_send_mail", fake_send)
    monkeypatch.setattr(pdf_service, "html_to_pdf", lambda _html: b"%PDF-fake-quote")
    try:
        sent = await db_client.post(
            f"/api/v1/quotes/{quote['id']}/send",
            json={"to": "buyer@example.com", "subject": "Quote", "body": "Please review."},
        )
        assert sent.status_code == 200, sent.text
        assert sent.json()["status"] == "SENT"
        assert attachments == [b"%PDF-fake-quote"]
        logs = await db_client.get(f"/api/v1/quotes/{quote['id']}/emails")
        assert logs.status_code == 200 and logs.json()["total"] == 1
        assert logs.json()["items"][0]["status"] == "SENT"
    finally:
        await alternate_engine.dispose()


@pytest.mark.asyncio
async def test_own_upload_database_failure_is_safe_and_rolls_back(
    db_client: AsyncClient, admin_session_maker: object, caplog: pytest.LogCaptureFixture
) -> None:
    await _login(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    quote = await _create_quote(
        db_client, customer_id, seeds["rates"]["NL standard (21%)"]["id"]
    )
    quote_id = quote["id"]
    async with admin_session_maker() as admin:
        await admin.execute(
            text("""
                CREATE FUNCTION jai_test_reject_pdf_appendix_insert() RETURNS trigger
                LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'sensitive database detail'; END $$
            """)
        )
        await admin.execute(
            text("""
                CREATE TRIGGER jai_test_reject_pdf_appendix_insert
                BEFORE INSERT ON pdf_appendix_file
                FOR EACH ROW EXECUTE FUNCTION jai_test_reject_pdf_appendix_insert()
            """)
        )
        await admin.commit()

    caplog.set_level(logging.ERROR, logger="jai")
    response = await db_client.post(
        f"/api/v1/quotes/{quote_id}/appendix-files",
        files={"file": ("private.pdf", _pdf(), "application/pdf")},
    )
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "PDF_APPENDIX_SAVE_FAILED"
    assert "sensitive database detail" not in response.text
    assert "sensitive database detail" not in caplog.text
    current = await db_client.get(f"/api/v1/quotes/{quote_id}")
    assert current.status_code == 200 and current.json()["appendix_version"] == 0
    own_files = await db_client.get(f"/api/v1/quotes/{quote_id}/appendix-files")
    assert own_files.json()["total"] == 0

    async with admin_session_maker() as admin:
        await admin.execute(
            text("DROP TRIGGER jai_test_reject_pdf_appendix_insert ON pdf_appendix_file")
        )
        await admin.execute(text("DROP FUNCTION jai_test_reject_pdf_appendix_insert()"))
        await admin.commit()


@pytest.mark.asyncio
async def test_quote_lock_serializes_two_runtime_sessions(
    db_client: AsyncClient, runtime_db_engine: AsyncEngine
) -> None:
    from sqlalchemy import select

    from jai.db import set_rls_company
    from jai.models._enums import QuoteStatus
    from jai.models.pdf_appendix import PdfAppendixFile
    from jai.models.quote import Quote
    from jai.schemas.pdf_appendix import PdfLibraryFileUpdate
    from jai.schemas.quote import QuoteWrite
    from jai.services.pdf_appendix import save_quote_file, update_library_file, validate_pdf
    from jai.services.quote import delete_quote, update_quote

    await _login(db_client)
    seeds = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    rate_id = seeds["rates"]["NL standard (21%)"]["id"]
    quote = await _create_quote(db_client, customer_id, rate_id)
    company_id = uuid.UUID(quote["company_id"])
    upload = await db_client.post(
        "/api/v1/pdf-library",
        files={"file": ("race.pdf", _pdf(), "application/pdf")},
    )
    assert upload.status_code == 201

    runtime_url = runtime_db_engine.url.render_as_string(hide_password=False)
    second_engine = create_async_engine(runtime_url, pool_size=1)
    maker = async_sessionmaker(second_engine, expire_on_commit=False, class_=AsyncSession)
    body = QuoteWrite.model_validate(
        {
            "customer_id": customer_id,
            "quote_date": "2026-01-10",
            "tax_mode": "LINE",
            "amounts_include_vat": False,
            "lines": [
                {
                    "name": "Service",
                    "quantity": "1",
                    "unit_price": "100",
                    "vat_rate_id": rate_id,
                }
            ],
            "appendix_file_ids": [upload.json()["id"]],
            "expected_appendix_version": 0,
        }
    )
    clear_body = QuoteWrite.model_validate(
        {
            "customer_id": customer_id,
            "quote_date": "2026-01-10",
            "tax_mode": "LINE",
            "amounts_include_vat": False,
            "lines": [
                {
                    "name": "Service",
                    "quantity": "1",
                    "unit_price": "100",
                    "vat_rate_id": rate_id,
                }
            ],
            "appendix_file_ids": [],
            "expected_appendix_version": 1,
        }
    )
    try:
        async with maker() as locker, maker() as writer:
            await set_rls_company(locker, company_id)
            await locker.execute(
                select(Quote.id).where(Quote.id == uuid.UUID(quote["id"])).with_for_update()
            )
            mutation = asyncio.create_task(
                update_quote(
                    writer,
                    uuid.UUID(quote["id"]),
                    body,
                    company_id,
                    "EUR",
                )
            )
            await asyncio.sleep(0.15)
            assert not mutation.done(), "selection update bypassed the locked parent Quote"
            await locker.commit()
            saved = await asyncio.wait_for(mutation, timeout=5)
            assert saved is not None
            assert saved.appendix_version == 1 and len(saved.appendices) == 1

            # Archive locks the file row. A concurrent clear waits and then
            # observes the committed archived state while preserving membership semantics.
            await set_rls_company(locker, company_id)
            await locker.execute(
                select(PdfAppendixFile.id)
                .where(PdfAppendixFile.id == uuid.UUID(upload.json()["id"]))
                .with_for_update()
            )
            clear_task = asyncio.create_task(
                update_quote(
                    writer,
                    uuid.UUID(quote["id"]),
                    clear_body,
                    company_id,
                    "EUR",
                )
            )
            await asyncio.sleep(0.15)
            assert not clear_task.done(), (
                f"selection clear bypassed the file-row lock or failed: "
                f"{clear_task.result() if clear_task.done() else 'still waiting'}"
            )
            await update_library_file(
                locker,
                company_id=company_id,
                file_id=uuid.UUID(upload.json()["id"]),
                data=PdfLibraryFileUpdate(is_archived=True),
            )
            await locker.commit()
            cleared = await asyncio.wait_for(clear_task, timeout=5)
            assert cleared is not None and cleared.appendix_version == 2

            assert (
                await db_client.post(
                    f"/api/v1/quotes/{quote['id']}/status", json={"status": "SENT"}
                )
            ).status_code == 200
            # Upload waits on the same parent lock; acceptance commits first,
            # then upload must recheck and reject without storing an own file.
            private_content = _pdf()
            validated = await validate_pdf(
                private_content, "application/pdf", max_bytes=10 * 1024 * 1024, max_pages=200
            )
            await set_rls_company(locker, company_id)
            locked_quote = await locker.scalar(
                select(Quote)
                .where(Quote.id == uuid.UUID(quote["id"]))
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            upload_task = asyncio.create_task(
                save_quote_file(
                    writer,
                    company_id=company_id,
                    quote_id=uuid.UUID(quote["id"]),
                    title="race own file",
                    filename="race-own.pdf",
                    content=private_content,
                    validated=validated,
                )
            )
            await asyncio.sleep(0.15)
            assert not upload_task.done(), "own upload bypassed the parent Quote lock"
            assert locked_quote is not None
            locked_quote.status = QuoteStatus.ACCEPTED
            await locker.flush()
            await locker.commit()
            with pytest.raises(ValueError, match="QUOTE_APPENDICES_READ_ONLY"):
                await asyncio.wait_for(upload_task, timeout=5)
            own_files = await db_client.get(f"/api/v1/quotes/{quote['id']}/appendix-files")
            assert own_files.status_code == 200 and own_files.json()["total"] == 0
            current = await db_client.get(f"/api/v1/quotes/{quote['id']}")
            assert current.json()["appendix_version"] == 2

            await set_rls_company(locker, company_id)
            await locker.execute(
                select(Quote.id).where(Quote.id == uuid.UUID(quote["id"])).with_for_update()
            )
            delete_task = asyncio.create_task(
                delete_quote(writer, uuid.UUID(quote["id"]), company_id)
            )
            await asyncio.sleep(0.15)
            assert not delete_task.done(), "Quote deletion bypassed the parent lock"
            await locker.commit()
            assert await asyncio.wait_for(delete_task, timeout=5) is True
    finally:
        await second_engine.dispose()

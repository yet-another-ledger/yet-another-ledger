"""Isolated PostgreSQL coverage for the M17 library API and runtime-role policy."""

from __future__ import annotations

import hashlib
import io
from typing import Any

import pyotp
import pytest
from httpx import AsyncClient
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    ByteStringObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from test_quote_payment_integration import _create_customer, _create_quote, _setup_company

pytestmark = pytest.mark.integration


def _static_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_blank_page(width=400, height=700)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _pdf_with_custom_info_metadata(key: str) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({key: "Product brochure", "/CatalogLabel": "Ordinary static document"})
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _signature_pdf(*, escaped_name: bool, comment_after_name: bool = False) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    key = NameObject("/Byte#52ange") if escaped_name else NameObject("/ByteRange")
    signature = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Sig"),
            NameObject("/Filter"): NameObject("/Adobe.PPKLite"),
            NameObject("/SubFilter"): NameObject("/adbe.pkcs7.detached"),
            key: ArrayObject([NumberObject(0), NumberObject(1), NumberObject(2), NumberObject(3)]),
            NameObject("/Contents"): ByteStringObject(b"signed"),
        }
    )
    writer.root_object[NameObject("/Perms")] = writer._add_object(
        DictionaryObject({NameObject("/DocMDP"): writer._add_object(signature)})
    )
    output = io.BytesIO()
    writer.write(output)
    content = output.getvalue()
    if comment_after_name:
        encoded_name = b"/Byte#52ange" if escaped_name else b"/ByteRange"
        content = content.replace(encoded_name, encoded_name + b" % allowed PDF comment\n  ")
    return content


def _interactive_3d_pdf() -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    javascript = DecodedStreamObject()
    javascript.set_data(b"/* inert test script */")
    javascript_ref = writer._add_object(javascript)
    three_d = DecodedStreamObject()
    three_d.set_data(b"U3D")
    three_d[NameObject("/Type")] = NameObject("/3D")
    three_d[NameObject("/Subtype")] = NameObject("/U3D")
    three_d[NameObject("/OnInstantiate")] = javascript_ref
    annotation = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/3D"),
            NameObject("/3DD"): writer._add_object(three_d),
        }
    )
    page[NameObject("/Annots")] = ArrayObject([writer._add_object(annotation)])
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _indirect_sound_pdf() -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    sound = DecodedStreamObject()
    sound.set_data(bytes(range(256)) * 4)
    sound[NameObject("/Type")] = writer._add_object(NameObject("/Sound"))
    sound[NameObject("/R")] = NumberObject(8000)
    sound[NameObject("/C")] = NumberObject(1)
    sound[NameObject("/B")] = NumberObject(8)
    sound[NameObject("/E")] = NameObject("/Raw")
    annotation = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Rect"): ArrayObject([NumberObject(0)] * 4),
            NameObject("/Subtype"): writer._add_object(NameObject("/Sound")),
            NameObject("/Name"): NameObject("/Speaker"),
            NameObject("/Sound"): writer._add_object(sound),
        }
    )
    page[NameObject("/Annots")] = ArrayObject([writer._add_object(annotation)])
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


async def _login(client: AsyncClient) -> None:
    assert (
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": "m17-owner@example.com",
                "password": "testpassword1",
            },
        )
    ).status_code == 201
    assert (
        await client.post(
            "/api/v1/auth/login",
            json={
                "email": "m17-owner@example.com",
                "password": "testpassword1",
            },
        )
    ).status_code == 200
    setup = await client.post("/api/v1/auth/mfa/setup")
    assert setup.status_code == 200
    code = pyotp.TOTP(setup.json()["secret"]).now()
    assert (await client.post("/api/v1/auth/mfa/verify", json={"code": code})).status_code == 204


@pytest.mark.asyncio
async def test_library_upload_metadata_content_archive_and_restore(
    db_client: AsyncClient,
    runtime_db_engine: Any,
) -> None:
    await _login(db_client)
    from jai.main import app

    spec = app.openapi()
    assert {"201", "413", "422", "500"} <= set(
        spec["paths"]["/api/v1/pdf-library"]["post"]["responses"]
    )
    assert {"404", "422"} <= set(
        spec["paths"]["/api/v1/pdf-library/{file_id}"]["patch"]["responses"]
    )
    assert {"404", "422"} <= set(
        spec["paths"]["/api/v1/pdf-files/{file_id}/content"]["get"]["responses"]
    )
    company = await _setup_company(db_client)
    limits = (await db_client.get("/api/v1/pdf-library/limits")).json()
    assert limits == {
        "max_appendix_file_bytes": 10 * 1024 * 1024,
        "max_quote_appendices": 10,
        "max_quote_appendix_input_bytes": 15 * 1024 * 1024,
        "max_quote_pdf_bytes": 15 * 1024 * 1024,
        "max_quote_pdf_pages": 200,
    }
    content = _static_pdf()
    uploaded = await db_client.post(
        "/api/v1/pdf-library",
        files={
            "file": ("../../manual.pdf", content, "application/pdf"),
            "title": (None, "Installation guide"),
        },
    )
    assert uploaded.status_code == 201, uploaded.text
    result = uploaded.json()
    assert result["title"] == "Installation guide"
    assert result["original_filename"] == "manual.pdf"
    assert result["sha256"] == hashlib.sha256(content).hexdigest()
    assert result["size_bytes"] == len(content) and result["page_count"] == 2
    assert result["source"] == "LIBRARY"
    assert "pdf_bytes" not in result

    # The runtime role can never mutate the immutable original columns.
    async with runtime_db_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('jai.company_id', :id, true)"),
            {
                "id": company["company_id"],
            },
        )
        with pytest.raises(DBAPIError):
            await conn.execute(
                text("UPDATE pdf_appendix_file SET pdf_bytes = :bytes WHERE id = :id"),
                {"bytes": b"mutated", "id": result["id"]},
            )
        await conn.rollback()

    listing = await db_client.get("/api/v1/pdf-library")
    assert listing.status_code == 200
    assert listing.json()["total"] == 1 and "pdf_bytes" not in listing.text
    inline = await db_client.get(f"/api/v1/pdf-files/{result['id']}/content?disposition=inline")
    assert inline.status_code == 200 and inline.content == content
    assert inline.headers["content-disposition"].startswith("inline;")
    assert inline.headers["x-content-type-options"] == "nosniff"
    for path in (
        "/api/v1/pdf-files/not-a-uuid/content",
        "/api/v1/pdf-files/00000000-0000-0000-0000-000000000000/content?disposition=invalid",
    ):
        invalid = await db_client.get(path)
        assert invalid.status_code == 422
        assert invalid.json()["detail"]["code"] == "PDF_APPENDIX_INVALID"

    renamed = await db_client.patch(f"/api/v1/pdf-library/{result['id']}", json={"title": "Manual"})
    assert renamed.status_code == 200 and renamed.json()["title"] == "Manual"
    archived = await db_client.patch(
        f"/api/v1/pdf-library/{result['id']}", json={"is_archived": True}
    )
    assert archived.status_code == 200 and archived.json()["is_archived"] is True
    assert (await db_client.get("/api/v1/pdf-library")).json()["total"] == 0
    assert (await db_client.get("/api/v1/pdf-library?include_archived=true")).json()["total"] == 1
    restored = await db_client.patch(
        f"/api/v1/pdf-library/{result['id']}", json={"is_archived": False}
    )
    assert restored.status_code == 200 and restored.json()["is_archived"] is False


@pytest.mark.parametrize("key", ["/Type", "/Subtype"])
@pytest.mark.asyncio
async def test_library_upload_accepts_custom_info_type_and_returns_exact_original(
    db_client: AsyncClient, key: str
) -> None:
    await _login(db_client)
    await _setup_company(db_client)
    original = _pdf_with_custom_info_metadata(key)
    uploaded = await db_client.post(
        "/api/v1/pdf-library",
        files={"file": ("brochure.pdf", original, "application/pdf")},
    )
    assert uploaded.status_code == 201, uploaded.text
    result = uploaded.json()
    assert result["sha256"] == hashlib.sha256(original).hexdigest()
    assert result["size_bytes"] == len(original)
    downloaded = await db_client.get(f"/api/v1/pdf-files/{result['id']}/content")
    assert downloaded.status_code == 200
    assert downloaded.content == original
    page = PdfReader(io.BytesIO(downloaded.content), strict=True).pages[0]
    assert float(page.mediabox.width) == 612
    assert float(page.mediabox.height) == 792


@pytest.mark.asyncio
async def test_pdf_library_requires_authenticated_mfa_owner(db_client: AsyncClient) -> None:
    response = await db_client.get("/api/v1/pdf-library/limits")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_invalid_and_oversized_uploads_leave_no_file_rows(
    db_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _login(db_client)
    await _setup_company(db_client)
    bad = await db_client.post(
        "/api/v1/pdf-library", files={"file": ("bad.pdf", b"not a pdf", "application/pdf")}
    )
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "PDF_APPENDIX_INVALID"
    for filename, content in (
        ("signed.pdf", _signature_pdf(escaped_name=False)),
        ("escaped-signed.pdf", _signature_pdf(escaped_name=True)),
        (
            "commented-signed.pdf",
            _signature_pdf(escaped_name=True, comment_after_name=True),
        ),
        ("interactive-3d.pdf", _interactive_3d_pdf()),
        ("indirect-sound.pdf", _indirect_sound_pdf()),
    ):
        rejected = await db_client.post(
            "/api/v1/pdf-library",
            files={"file": (filename, content, "application/pdf")},
        )
        assert rejected.status_code == 422
        assert rejected.json()["detail"]["code"] == "PDF_APPENDIX_INVALID"
        assert (await db_client.get("/api/v1/pdf-library")).json()["total"] == 0
    from jai.config import get_settings

    monkeypatch.setattr(get_settings(), "max_appendix_file_bytes", 100)
    oversized = await db_client.post(
        "/api/v1/pdf-library",
        files={"file": ("big.pdf", b"%PDF-" + b"x" * 1000, "application/pdf")},
    )
    assert oversized.status_code == 413
    assert oversized.json()["detail"]["code"] == "PDF_APPENDIX_FILE_TOO_LARGE"
    invalid_patch = await db_client.patch("/api/v1/pdf-library/not-a-uuid", json={})
    assert invalid_patch.status_code == 422
    assert invalid_patch.json()["detail"]["code"] == "PDF_APPENDIX_INVALID"
    bad_limit = await db_client.get("/api/v1/pdf-library?limit=0")
    assert bad_limit.status_code == 422
    assert bad_limit.json()["detail"]["code"] == "PDF_APPENDIX_INVALID"
    assert (await db_client.get("/api/v1/pdf-library")).json()["total"] == 0


@pytest.mark.asyncio
async def test_failed_database_insert_leaves_no_partial_file_row(
    db_client: AsyncClient,
    runtime_session_maker: Any,
) -> None:
    from jai.db import set_rls_company
    from jai.models.pdf_appendix import PdfAppendixFile

    await _login(db_client)
    company = await _setup_company(db_client)
    async with runtime_session_maker() as session:
        await set_rls_company(session, company["company_id"])
        session.add(
            PdfAppendixFile(
                company_id=company["company_id"],
                title="invalid",
                original_filename="bad.pdf",
                pdf_bytes=b"not persisted",
                sha256="0" * 64,
                size_bytes=0,
                page_count=1,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()
    listing = await db_client.get("/api/v1/pdf-library")
    assert listing.status_code == 200 and listing.json()["total"] == 0


@pytest.mark.asyncio
async def test_runtime_role_rls_hides_other_company_appendix_rows(
    db_client: AsyncClient,
    runtime_db_engine: Any,
    db_session_maker: Any,
) -> None:
    """Verify forced RLS at the runtime DB role boundary, independently of route filters."""
    from jai.models.company import Company
    from jai.models.pdf_appendix import PdfAppendixFile

    await _login(db_client)
    first = await _setup_company(db_client)
    async with db_session_maker() as admin:  # migration-owner fixture for deterministic setup
        company = Company(name="Other M17 Co", base_currency="EUR", country_code="NL")
        admin.add(company)
        await admin.flush()
        other_id = company.id
        from jai.db import set_rls_company

        await set_rls_company(admin, other_id)
        admin.add(
            PdfAppendixFile(
                company_id=other_id,
                title="private",
                original_filename="p.pdf",
                pdf_bytes=_static_pdf(),
                sha256=hashlib.sha256(_static_pdf()).hexdigest(),
                size_bytes=len(_static_pdf()),
                page_count=2,
            )
        )
        await admin.commit()
    async with runtime_db_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('jai.company_id', :id, true)"), {"id": first["company_id"]}
        )
        rows = (await conn.execute(text("SELECT title FROM pdf_appendix_file"))).all()
        assert rows == []


@pytest.mark.asyncio
async def test_quote_delete_cascades_memberships_and_own_files_only(
    db_client: AsyncClient,
    runtime_session_maker: Any,
    runtime_db_engine: Any,
) -> None:
    from jai.db import set_rls_company
    from jai.models.pdf_appendix import PdfAppendixFile, QuoteAppendix

    await _login(db_client)
    company = await _setup_company(db_client)
    customer_id = await _create_customer(db_client)
    quote = await _create_quote(db_client, customer_id, company["rates"]["NL standard (21%)"]["id"])
    pdf = _static_pdf()
    uploaded = await db_client.post(
        "/api/v1/pdf-library",
        files={"file": ("library.pdf", pdf, "application/pdf")},
    )
    assert uploaded.status_code == 201, uploaded.text
    library_id = uploaded.json()["id"]
    async with runtime_session_maker() as session:
        await set_rls_company(session, company["company_id"])
        own = PdfAppendixFile(
            company_id=company["company_id"],
            owner_quote_id=quote["id"],
            title="Own",
            original_filename="own.pdf",
            pdf_bytes=pdf,
            sha256=hashlib.sha256(pdf).hexdigest(),
            size_bytes=len(pdf),
            page_count=2,
        )
        session.add_all(
            [
                own,
                QuoteAppendix(
                    company_id=company["company_id"],
                    quote_id=quote["id"],
                    file_id=library_id,
                    sort_order=0,
                    title_snapshot="Snapshot",
                ),
            ]
        )
        await session.commit()
        own_id = str(own.id)
    deleted = await db_client.delete(f"/api/v1/quotes/{quote['id']}")
    assert deleted.status_code == 204, deleted.text
    async with runtime_db_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('jai.company_id', :id, true)"),
            {
                "id": company["company_id"],
            },
        )
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM quote_appendix WHERE quote_id = :id"),
                {
                    "id": quote["id"],
                },
            )
            == 0
        )
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM pdf_appendix_file WHERE id = :id"),
                {
                    "id": own_id,
                },
            )
            == 0
        )
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM pdf_appendix_file WHERE id = :id"),
                {
                    "id": library_id,
                },
            )
            == 1
        )


@pytest.mark.asyncio
async def test_runtime_role_enforces_exact_own_file_and_deferred_delete_order(
    db_client: AsyncClient,
    runtime_session_maker: Any,
    db_session_maker: Any,
) -> None:
    from jai.db import set_rls_company
    from jai.models.pdf_appendix import PdfAppendixFile, QuoteAppendix

    await _login(db_client)
    company = await _setup_company(db_client)
    customer = await _create_customer(db_client)
    rate = company["rates"]["NL standard (21%)"]["id"]
    quote_a = await _create_quote(db_client, customer, rate)
    quote_b = await _create_quote(db_client, customer, rate)
    library = await db_client.post(
        "/api/v1/pdf-library", files={"file": ("guide.pdf", _static_pdf(), "application/pdf")}
    )
    assert library.status_code == 201
    from jai.models.company import Company

    async with db_session_maker() as session:
        other_company = Company(
            name="Other appendix tenant", base_currency="EUR", country_code="NL"
        )
        session.add(other_company)
        await session.flush()
        other_company_id = other_company.id
        await session.commit()

    async with runtime_session_maker() as session:
        await set_rls_company(session, other_company_id)
        session.add(
            PdfAppendixFile(
                company_id=other_company_id,
                owner_quote_id=quote_a["id"],
                title="Cross-company owner",
                original_filename="cross.pdf",
                pdf_bytes=_static_pdf(),
                sha256=hashlib.sha256(_static_pdf()).hexdigest(),
                size_bytes=len(_static_pdf()),
                page_count=2,
            )
        )
        with pytest.raises(DBAPIError):
            await session.commit()
        await session.rollback()

    async with runtime_session_maker() as session:
        await set_rls_company(session, company["company_id"])
        own = PdfAppendixFile(
            company_id=company["company_id"],
            owner_quote_id=quote_a["id"],
            title="Own",
            original_filename="own.pdf",
            pdf_bytes=_static_pdf(),
            sha256=hashlib.sha256(_static_pdf()).hexdigest(),
            size_bytes=len(_static_pdf()),
            page_count=2,
        )
        session.add(own)
        await session.flush()
        own_id = own.id
        with pytest.raises(DBAPIError):
            session.add(
                QuoteAppendix(
                    company_id=company["company_id"],
                    quote_id=quote_b["id"],
                    file_id=own_id,
                    sort_order=0,
                    title_snapshot="wrong quote",
                )
            )
            await session.flush()
        await session.rollback()

    # A real runtime-role transaction can delete a referenced file first and
    # its membership later; NO ACTION is checked at commit, independent of order.
    async with runtime_session_maker() as session:
        await set_rls_company(session, company["company_id"])
        own = PdfAppendixFile(
            company_id=company["company_id"],
            owner_quote_id=quote_a["id"],
            title="Own",
            original_filename="own.pdf",
            pdf_bytes=_static_pdf(),
            sha256=hashlib.sha256(_static_pdf()).hexdigest(),
            size_bytes=len(_static_pdf()),
            page_count=2,
        )
        session.add(own)
        await session.flush()
        member = QuoteAppendix(
            company_id=company["company_id"],
            quote_id=quote_a["id"],
            file_id=own.id,
            sort_order=0,
            title_snapshot="Own",
        )
        session.add(member)
        await session.flush()
        await session.execute(text("DELETE FROM pdf_appendix_file WHERE id=:id"), {"id": own.id})
        await session.execute(text("DELETE FROM quote_appendix WHERE id=:id"), {"id": member.id})
        await session.commit()

    # Shared files remain protected at transaction commit while referenced.
    async with runtime_session_maker() as session:
        await set_rls_company(session, company["company_id"])
        session.add(
            QuoteAppendix(
                company_id=company["company_id"],
                quote_id=quote_a["id"],
                file_id=library.json()["id"],
                sort_order=0,
                title_snapshot="Guide",
            )
        )
        await session.commit()
    async with runtime_session_maker() as session:
        await set_rls_company(session, company["company_id"])
        await session.execute(
            text("DELETE FROM pdf_appendix_file WHERE id=:id"), {"id": library.json()["id"]}
        )
        with pytest.raises(DBAPIError):
            await session.commit()
        await session.rollback()

    # Quote cascade exercises the opposite order: memberships disappear before
    # its selected own-file row, while the library row remains referenced.
    async with runtime_session_maker() as session:
        await set_rls_company(session, company["company_id"])
        selected_own = PdfAppendixFile(
            company_id=company["company_id"],
            owner_quote_id=quote_a["id"],
            title="Selected own",
            original_filename="selected-own.pdf",
            pdf_bytes=_static_pdf(),
            sha256=hashlib.sha256(_static_pdf()).hexdigest(),
            size_bytes=len(_static_pdf()),
            page_count=2,
        )
        session.add(selected_own)
        await session.flush()
        selected_own_id = selected_own.id
        session.add(
            QuoteAppendix(
                company_id=company["company_id"],
                quote_id=quote_a["id"],
                file_id=selected_own_id,
                sort_order=1,
                title_snapshot="Selected own",
            )
        )
        await session.commit()
    deleted = await db_client.delete(f"/api/v1/quotes/{quote_a['id']}")
    assert deleted.status_code == 204, deleted.text
    async with runtime_session_maker() as session:
        await set_rls_company(session, company["company_id"])
        assert (
            await session.scalar(
                text("SELECT count(*) FROM pdf_appendix_file WHERE id=:id"),
                {"id": selected_own_id},
            )
            == 0
        )
        assert (
            await session.scalar(
                text("SELECT count(*) FROM pdf_appendix_file WHERE id=:id"),
                {"id": library.json()["id"]},
            )
            == 1
        )

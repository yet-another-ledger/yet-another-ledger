"""Unit coverage for the M13 formal-artifact upload contract boundary."""

from __future__ import annotations

import json
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from starlette import formparsers

from jai.api import pdf as pdf_api
from jai.auth.deps import current_mfa_user
from jai.config import Settings
from jai.db import get_session
from jai.main import app
from jai.models._enums import (
    DocumentArtifactReason,
    DocumentArtifactValidationCheckStatus,
    DocumentArtifactValidationConfidence,
    DocumentArtifactValidationField,
    DocumentArtifactValidationStatus,
)
from jai.schemas.artifact import DocumentArtifactValidationRead
from jai.schemas.setting import AiSettings
from jai.services import ai as ai_service
from jai.services import artifact_validation as validation_service
from jai.services import artifacts as artifact_service
from jai.services.artifact_validation import (
    ArtifactAIConfigurationError,
    ArtifactAIValidationError,
)
from jai.services.artifacts import (
    ArtifactFileValidationError,
    normalize_uploaded_artifact_filename,
    validate_artifact_pdf,
)


def _multipart_body(parts: list[tuple[str, str, bytes]]) -> tuple[dict[str, str], bytes]:
    boundary = "artifact-test-boundary"
    body = b"".join(
        b"--" + boundary.encode() + b"\r\n"
        + (
            b'Content-Disposition: form-data; name="'
            + name.encode()
            + b'"; filename="'
            + filename.encode()
            + b'"\r\nContent-Type: application/pdf\r\n\r\n'
        )
        + content
        + b"\r\n"
        for name, filename, content in parts
    ) + b"--" + boundary.encode() + b"--\r\n"
    return {"content-type": f"multipart/form-data; boundary={boundary}"}, body


@pytest.fixture
def artifact_upload_owner(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Authenticate and make raw-reader tests database-independent."""
    app.dependency_overrides[current_mfa_user] = lambda: SimpleNamespace(
        role="owner", company_id=uuid.uuid4()
    )

    async def eligible(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(pdf_api, "ensure_invoice_artifact_upload_eligible", eligible)

    async def retain(*args: object, **kwargs: object) -> SimpleNamespace:
        del args
        return SimpleNamespace(
            id=uuid.uuid4(),
            artifact_kind="FORMAL_DOCUMENT",
            sha256="a" * 64,
            render_fingerprint="b" * 64,
            locale="en",
            filename=str(kwargs["original_filename"]),
            creation_reason="UPLOAD",
            renderer_version="external-upload-v1",
            created_at=datetime.now(UTC),
        )

    monkeypatch.setattr(artifact_service, "create_uploaded_invoice_artifact", retain)

    async def validate(*args: object, **kwargs: object) -> DocumentArtifactValidationRead:
        del args
        return DocumentArtifactValidationRead(
            file_sha256="c" * 64,
            status=DocumentArtifactValidationStatus.INCONCLUSIVE,
            confidence=None,
            summary="Advisory result.",
            total_pages=1,
            checked_pages=[1],
            checks=[
                {
                    "field": field,
                    "status": DocumentArtifactValidationCheckStatus.NOT_FOUND,
                    "expected_value": None,
                    "observed_value": None,
                    "note": None,
                }
                for field in DocumentArtifactValidationField
            ],
        )

    monkeypatch.setattr(pdf_api, "validate_uploaded_invoice_artifact", validate)
    try:
        yield
    finally:
        app.dependency_overrides.pop(current_mfa_user, None)


def _artifact_upload_urls() -> tuple[str, str]:
    invoice_id = uuid.uuid4()
    return (
        f"/api/v1/invoices/{invoice_id}/artifacts",
        f"/api/v1/invoices/{invoice_id}/artifacts/validate-upload?language=en",
    )


def _accepted_body_status(url: str) -> int:
    """Both endpoints accept a valid bounded body before their later work."""
    return 200 if "validate-upload" in url else 201


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "status_code", "code"),
    [
        ("missing", 404, "ARTIFACT_UPLOAD_NOT_FOUND"),
        ("cross-company", 404, "ARTIFACT_UPLOAD_NOT_FOUND"),
        ("draft", 404, "ARTIFACT_UPLOAD_NOT_FOUND"),
        ("cancelled", 404, "ARTIFACT_UPLOAD_NOT_FOUND"),
        ("existing-artifact", 409, "ARTIFACT_ALREADY_EXISTS"),
    ],
)
async def test_artifact_upload_eligibility_rejects_before_body_is_consumed(
    client: AsyncClient,
    artifact_upload_owner: None,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    status_code: int,
    code: str,
) -> None:
    """Both stubs perform their shared DB gate before opening request.stream()."""
    async def reject(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise HTTPException(status_code=status_code, detail={"code": code, "message": name})

    monkeypatch.setattr(pdf_api, "ensure_invoice_artifact_upload_eligible", reject)
    headers, body = _multipart_body([("file", "historic.pdf", b"not read")])

    for url in _artifact_upload_urls():
        emitted = 0

        async def stream() -> AsyncIterator[bytes]:
            nonlocal emitted
            emitted += 1
            yield body

        response = await client.post(url, headers=headers, content=stream())
        assert response.status_code == status_code
        assert response.json()["detail"]["code"] == code
        assert emitted == 0


@pytest.mark.asyncio
async def test_artifact_upload_parameter_validation_has_stable_non_file_envelope(
    client: AsyncClient, artifact_upload_owner: None
) -> None:
    cases = (
        "/api/v1/invoices/not-a-uuid/artifacts",
        f"/api/v1/invoices/{uuid.uuid4()}/artifacts/validate-upload",
        f"/api/v1/invoices/{uuid.uuid4()}/artifacts/validate-upload?language=nl",
    )
    for url in cases:
        response = await client.post(url)
        assert response.status_code == 422
        assert response.json() == {
            "detail": {
                "code": "INVALID_ARTIFACT_UPLOAD_REQUEST",
                "message": "The artifact upload request is invalid.",
            }
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_code"),
    [
        (ArtifactAIConfigurationError("private configuration detail"), 409, "AI_NOT_CONFIGURED"),
        (ArtifactAIValidationError("private provider detail"), 502, "AI_VALIDATION_FAILED"),
    ],
)
async def test_artifact_validation_maps_only_stable_ai_errors(
    client: AsyncClient,
    valid_pdf: bytes,
    artifact_upload_owner: None,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    async def fail(*args: object, **kwargs: object) -> DocumentArtifactValidationRead:
        del args, kwargs
        raise failure

    monkeypatch.setattr(pdf_api, "validate_uploaded_invoice_artifact", fail)
    headers, body = _multipart_body([("file", "historic.pdf", valid_pdf)])
    response = await client.post(_artifact_upload_urls()[1], headers=headers, content=body)
    assert response.status_code == expected_status
    assert response.json() == {
        "detail": {
            "code": expected_code,
            "message": (
                "AI validation is not configured."
                if expected_status == 409
                else "AI validation failed."
            ),
        }
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", [[], {}, 7], ids=["array", "object", "numeric"])
async def test_malformed_chat_finish_reason_maps_to_stable_validation_502(
    client: AsyncClient,
    valid_pdf: bytes,
    artifact_upload_owner: None,
    monkeypatch: pytest.MonkeyPatch,
    finish_reason: object,
) -> None:
    """Exercise the invoice route through the shared Chat transport parser."""
    invoice = SimpleNamespace(
        invoice_number="INV-SYNTHETIC-1",
        document_kind="STANDARD",
        invoice_date=date(2026, 1, 2),
        supply_or_advance_date=date(2026, 1, 1),
        currency="EUR",
        subtotal_excl_vat="100.000",
        vat_total="21.000",
        total_incl_vat="121.000",
        party_snapshot=SimpleNamespace(),
    )

    class Session:
        async def scalar(self, _statement: object) -> object:
            return invoice

    session = Session()
    app.dependency_overrides[get_session] = lambda: session

    async def config(_session: object) -> AiSettings:
        return AiSettings(
            api_protocol="chat_completions",
            enabled=True,
            base_url="https://provider.invalid/v1",
            api_key="synthetic-key",
            model="synthetic-vision",
        )

    class OnePage:
        def __init__(self, _data: bytes) -> None:
            pass

        def __len__(self) -> int:
            return 1

        def close(self) -> None:
            pass

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": finish_reason, "message": {"content": "{}"}}]},
        )

    original_chat_call = ai_service._call_chat_completions

    async def controlled_chat_call(**kwargs: object) -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as provider:
            kwargs["client"] = provider
            return await original_chat_call(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(validation_service.ai_service, "_get_ai_config", config)
    monkeypatch.setattr(
        pdf_api, "validate_uploaded_invoice_artifact",
        validation_service.validate_uploaded_invoice_artifact,
    )
    monkeypatch.setattr(validation_service.pdfium, "PdfDocument", OnePage)
    monkeypatch.setattr(
        validation_service.ai_service, "rasterize_pdf_pages", lambda *_args: [b"png"]
    )
    monkeypatch.setattr(
        validation_service.ai_service, "_call_chat_completions", controlled_chat_call
    )
    try:
        headers, body = _multipart_body([("file", "historic.pdf", valid_pdf)])
        response = await client.post(
            f"/api/v1/invoices/{uuid.uuid4()}/artifacts/validate-upload?language=en",
            headers=headers,
            content=body,
        )
        assert response.status_code == 502
        assert response.json() == {
            "detail": {
                "code": "AI_VALIDATION_FAILED",
                "message": "AI validation failed.",
            }
        }
        assert len(requests) == 1
        assert requests[0].url.path == "/v1/chat/completions"
    finally:
        app.dependency_overrides.pop(get_session, None)


@pytest.mark.asyncio
async def test_empty_expected_match_with_unexpected_value_maps_to_stable_502(
    client: AsyncClient,
    valid_pdf: bytes,
    artifact_upload_owner: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The parser's conservative empty-expected rule reaches the stable API error."""
    async def parse_inconsistent_result(
        *args: object, **kwargs: object
    ) -> DocumentArtifactValidationRead:
        del args, kwargs
        expected = {field: "expected" for field in DocumentArtifactValidationField}
        field = DocumentArtifactValidationField.SUPPLY_OR_ADVANCE_DATE
        expected[field] = None
        raw = json.dumps(
            {
                "summary": "Checked document.",
                "confidence": "HIGH",
                "checks": [
                    {
                        "field": item.value,
                        "status": "MATCH",
                        "observed_value": "unexpected-date" if item == field else "seen",
                        "note": None,
                    }
                    for item in DocumentArtifactValidationField
                ],
            }
        )
        validation_service._parse_result(raw, expected)
        raise AssertionError("inconsistent AI result must not parse")

    monkeypatch.setattr(
        pdf_api, "validate_uploaded_invoice_artifact", parse_inconsistent_result
    )
    headers, body = _multipart_body([("file", "historic.pdf", valid_pdf)])
    response = await client.post(_artifact_upload_urls()[1], headers=headers, content=body)

    assert response.status_code == 502
    assert response.json() == {
        "detail": {
            "code": "AI_VALIDATION_FAILED",
            "message": "AI validation failed.",
        }
    }


@pytest.mark.asyncio
async def test_artifact_upload_multipart_exact_limit_never_uses_starlette_spool(
    client: AsyncClient,
    valid_pdf: bytes,
    artifact_upload_owner: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both stubs read the raw ASGI stream before default multipart parsing."""
    content = valid_pdf + b" " * 128
    monkeypatch.setattr(
        pdf_api, "get_settings", lambda: SimpleNamespace(max_artifact_bytes=len(content))
    )

    def fail_if_spooled(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("Starlette multipart spool must not be used")

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", fail_if_spooled)
    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", fail_if_spooled)
    headers, body = _multipart_body([("file", "historic.pdf", content)])

    for url in _artifact_upload_urls():
        response = await client.post(url, headers=headers, content=body)
        assert response.status_code == _accepted_body_status(url)


@pytest.mark.asyncio
async def test_artifact_upload_multipart_rejects_one_byte_over_limit(
    client: AsyncClient,
    valid_pdf: bytes,
    artifact_upload_owner: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pdf_api, "get_settings", lambda: SimpleNamespace(max_artifact_bytes=len(valid_pdf))
    )

    def fail_if_spooled(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("rejected multipart input must not create a temp file")

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", fail_if_spooled)
    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", fail_if_spooled)
    headers, body = _multipart_body([("file", "historic.pdf", valid_pdf + b"x")])

    for url in _artifact_upload_urls():
        response = await client.post(url, headers=headers, content=body)
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "INVALID_ARTIFACT_FILE"


@pytest.mark.asyncio
async def test_artifact_upload_multipart_stops_far_over_chunked_body(
    client: AsyncClient,
    valid_pdf: bytes,
    artifact_upload_owner: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limit = len(valid_pdf)
    monkeypatch.setattr(pdf_api, "get_settings", lambda: SimpleNamespace(max_artifact_bytes=limit))
    headers, full_body = _multipart_body([("file", "historic.pdf", valid_pdf + b"x" * 200)])
    # Split the multipart envelope before streaming its file data in many ASGI
    # chunks.  The endpoint must stop well before the intentionally distant end.
    first_content = full_body.index(valid_pdf) + len(valid_pdf)
    chunks = [full_body[:first_content]] + [b"x" * 16 for _ in range(100)]
    emitted = 0

    async def stream() -> AsyncIterator[bytes]:
        nonlocal emitted
        for chunk in chunks:
            emitted += 1
            yield chunk

    for url in _artifact_upload_urls():
        emitted = 0
        response = await client.post(url, headers=headers, content=stream())
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "INVALID_ARTIFACT_FILE"
        assert emitted < len(chunks)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parts",
    [
        [("file", "one.pdf", b"%PDF"), ("file", "two.pdf", b"%PDF")],
        [("file", "one.pdf", b"%PDF"), ("other", "two.pdf", b"%PDF")],
    ],
)
async def test_artifact_upload_multipart_rejects_duplicate_or_extra_file_parts(
    client: AsyncClient,
    artifact_upload_owner: None,
    monkeypatch: pytest.MonkeyPatch,
    parts: list[tuple[str, str, bytes]],
) -> None:
    monkeypatch.setattr(pdf_api, "get_settings", lambda: SimpleNamespace(max_artifact_bytes=1024))
    headers, body = _multipart_body(parts)

    for url in _artifact_upload_urls():
        response = await client.post(url, headers=headers, content=body)
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "INVALID_ARTIFACT_FILE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "body"),
    [
        (
            {"content-type": f"multipart/form-data; boundary={'x' * 257}"},
            b"--" + b"x" * 257 + b"--\r\n",
        ),
        (
            {"content-type": "multipart/form-data; boundary=bad"},
            b"--bad\r\nBroken header\r\n\r\n%PDF\r\n--bad--\r\n",
        ),
        (
            {"content-type": "multipart/form-data; boundary=bad"},
            b"--bad\r\nContent-Disposition: form-data; name=\"file\"; filename=\"x.pdf\""
            b"\r\nContent-Type: application/pdf\r\n"
            b"Content-Type: application/pdf\r\n\r\n%PDF\r\n--bad--\r\n",
        ),
        (
            {"content-type": "multipart/form-data; boundary=bad"},
            b"--bad\r\n"
            + b"".join(f"X-{index}: value\r\n".encode() for index in range(9))
            + b"\r\n%PDF\r\n--bad--\r\n",
        ),
        (
            {"content-type": "multipart/form-data; boundary=bad"},
            b"--bad\r\nContent-Disposition: form-data; name=\"file\"",
        ),
        (
            {"content-type": "multipart/form-data; boundary=bad"},
            b"--bad\r\nContent-Disposition: form-data; name=\"file\"; filename=\"x.pdf\""
            b"\r\nContent-Type: application/pdf\r\n\r\n%PDF",
        ),
    ],
)
async def test_artifact_upload_multipart_normalizes_adversarial_framing(
    client: AsyncClient,
    artifact_upload_owner: None,
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
    body: bytes,
) -> None:
    monkeypatch.setattr(pdf_api, "get_settings", lambda: SimpleNamespace(max_artifact_bytes=1024))
    for url in _artifact_upload_urls():
        response = await client.post(url, headers=headers, content=body)
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "INVALID_ARTIFACT_FILE"


@pytest.mark.asyncio
async def test_artifact_upload_multipart_handles_one_byte_chunks_and_stops_after_closing_boundary(
    client: AsyncClient,
    valid_pdf: bytes,
    artifact_upload_owner: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pdf_api, "get_settings", lambda: SimpleNamespace(max_artifact_bytes=len(valid_pdf))
    )
    headers, body = _multipart_body([("file", "historic.pdf", valid_pdf)])

    for url in _artifact_upload_urls():
        one_byte_emitted = 0

        async def one_byte_stream() -> AsyncIterator[bytes]:
            nonlocal one_byte_emitted
            for byte in body:
                one_byte_emitted += 1
                yield bytes([byte])

        response = await client.post(url, headers=headers, content=one_byte_stream())
        assert response.status_code == _accepted_body_status(url)
        # The parser recognizes the final delimiter before its conventional
        # trailing CRLF, so even a byte-at-a-time legal request stops early.
        assert one_byte_emitted < len(body)

        epilogue_emitted = 0

        async def epilogue_stream() -> AsyncIterator[bytes]:
            nonlocal epilogue_emitted
            yield body
            epilogue_emitted += 1
            for _ in range(100):
                epilogue_emitted += 1
                yield b"x" * 1024

        response = await client.post(url, headers=headers, content=epilogue_stream())
        assert response.status_code == _accepted_body_status(url)
        assert epilogue_emitted == 0


@pytest.mark.asyncio
async def test_artifact_upload_multipart_accepts_same_bytes_regardless_of_epilogue_chunking(
    client: AsyncClient,
    valid_pdf: bytes,
    artifact_upload_owner: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legal closing delimiter wins over epilogue in the same ASGI chunk."""
    monkeypatch.setattr(
        pdf_api, "get_settings", lambda: SimpleNamespace(max_artifact_bytes=len(valid_pdf))
    )

    def fail_if_spooled(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("raw multipart parsing must not create a temp file")

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", fail_if_spooled)
    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", fail_if_spooled)
    headers, multipart = _multipart_body([("file", "historic.pdf", valid_pdf)])
    epilogue = b"e" * 20_000
    body = multipart + epilogue

    for url in _artifact_upload_urls():
        split_emitted = 0

        async def split_stream() -> AsyncIterator[bytes]:
            nonlocal split_emitted
            split_emitted += 1
            yield multipart
            split_emitted += 1
            yield epilogue

        split = await client.post(url, headers=headers, content=split_stream())
        assert split.status_code == _accepted_body_status(url)
        # The endpoint must not ask ASGI for the already-irrelevant epilogue.
        assert split_emitted == 1

        coalesced_emitted = 0

        async def coalesced_stream() -> AsyncIterator[bytes]:
            nonlocal coalesced_emitted
            coalesced_emitted += 1
            yield body

        coalesced = await client.post(url, headers=headers, content=coalesced_stream())
        assert coalesced.status_code == split.status_code
        assert coalesced_emitted == 1


@pytest.mark.asyncio
async def test_artifact_upload_multipart_bounds_large_preamble_without_content_length(
    client: AsyncClient,
    artifact_upload_owner: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pdf_api, "get_settings", lambda: SimpleNamespace(max_artifact_bytes=64))
    headers = {"content-type": "multipart/form-data; boundary=bounded"}
    chunks = [b"\r\n" * 128 for _ in range(100)]

    for url in _artifact_upload_urls():
        emitted = 0

        async def preamble_stream() -> AsyncIterator[bytes]:
            nonlocal emitted
            for chunk in chunks:
                emitted += 1
                yield chunk

        response = await client.post(url, headers=headers, content=preamble_stream())
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "INVALID_ARTIFACT_FILE"
        assert emitted < len(chunks)


@pytest.fixture(scope="module")
def valid_pdf() -> bytes:
    """Return a minimal valid one-page PDF without mutating renderer globals."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] "
        b"/Resources << >> /Contents 4 0 R >>",
        b"<< /Length 0 >>\nstream\n\nendstream",
    ]
    content = b"%PDF-1.4\n"
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(content))
        content += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_offset = len(content)
    xref_rows = b"".join(
        f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:]
    )
    return (
        content
        + f"xref\n0 {len(objects) + 1}\n".encode()
        + b"0000000000 65535 f \n"
        + xref_rows
        + f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode()
        + f"startxref\n{xref_offset}\n%%EOF\n".encode()
    )


class TestArtifactPdfValidation:
    def test_valid_pdf_preserves_exact_bytes_and_accepts_exact_limit(
        self, valid_pdf: bytes
    ) -> None:
        original = bytes(valid_pdf)
        assert validate_artifact_pdf(valid_pdf, "application/pdf", len(valid_pdf)) == 1
        assert valid_pdf == original

    def test_mime_parameter_is_normalized(self, valid_pdf: bytes) -> None:
        assert (
            validate_artifact_pdf(valid_pdf, "application/pdf; charset=binary", len(valid_pdf))
            == 1
        )

    @pytest.mark.parametrize(
        ("content", "mime_type", "max_bytes"),
        [
            (b"not a PDF", "application/pdf", 100),
            (b"%PDF-not-a-real-document", "application/pdf", 100),
            (b"%PDF-not-a-real-document", "text/plain", 100),
            (b"%PDF-not-a-real-document", "application/pdf", 1),
            (b"\xef\xbb\xbf%PDF-1.7", "application/pdf", 100),
        ],
    )
    def test_invalid_mime_size_magic_and_malformed_pdf_have_one_stable_error(
        self, content: bytes, mime_type: str, max_bytes: int
    ) -> None:
        with pytest.raises(ArtifactFileValidationError) as exc_info:
            validate_artifact_pdf(content, mime_type, max_bytes)
        assert str(exc_info.value) == (
            "The file must be a valid PDF within the configured size limit."
        )

    def test_over_limit_is_rejected_without_parsing(self, valid_pdf: bytes) -> None:
        with pytest.raises(ArtifactFileValidationError):
            validate_artifact_pdf(valid_pdf, "application/pdf", len(valid_pdf) - 1)

    def test_zero_page_pdf_is_rejected(
        self, valid_pdf: bytes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class ZeroPageDocument:
            def __init__(self, content: bytes) -> None:
                assert content == valid_pdf

            def __len__(self) -> int:
                return 0

            def close(self) -> None:
                pass

        monkeypatch.setattr(artifact_service.pdfium, "PdfDocument", ZeroPageDocument)
        with pytest.raises(ArtifactFileValidationError) as exc_info:
            validate_artifact_pdf(valid_pdf, "application/pdf", len(valid_pdf))
        assert str(exc_info.value) == (
            "The file must be a valid PDF within the configured size limit."
        )


def test_artifact_upload_contract_is_locked_in_openapi() -> None:
    schema = app.openapi()
    paths = schema["paths"]
    components = schema["components"]["schemas"]
    upload = paths["/api/v1/invoices/{invoice_id}/artifacts"]["post"]
    validate = paths["/api/v1/invoices/{invoice_id}/artifacts/validate-upload"]["post"]

    assert upload["responses"]["201"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/DocumentArtifactRead"
    }
    assert upload["requestBody"]["content"]["multipart/form-data"]
    assert validate["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/DocumentArtifactValidationRead"
    }
    assert validate["parameters"][1]["name"] == "language"
    assert validate["parameters"][1]["required"] is True

    assert upload["responses"]["404"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/DocumentArtifactNotFoundErrorResponse"
    }
    assert upload["responses"]["409"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/DocumentArtifactUploadConflictErrorResponse"
    }
    expected_422 = {"$ref": "#/components/schemas/DocumentArtifactUnprocessableErrorResponse"}
    assert upload["responses"]["422"]["content"]["application/json"]["schema"] == expected_422
    assert validate["responses"]["404"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/DocumentArtifactNotFoundErrorResponse"
    }
    assert validate["responses"]["409"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/DocumentArtifactValidationConflictErrorResponse"
    }
    assert validate["responses"]["422"]["content"]["application/json"]["schema"] == expected_422
    assert validate["responses"]["502"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/DocumentArtifactValidationFailedErrorResponse"
    }

    unprocessable = components["DocumentArtifactUnprocessableErrorResponse"]
    assert unprocessable["properties"]["detail"]["anyOf"] == [
        {"$ref": "#/components/schemas/DocumentArtifactInvalidFileErrorDetail"},
        {"$ref": "#/components/schemas/DocumentArtifactInvalidRequestErrorDetail"},
    ]
    generated_types = (
        Path(__file__).resolve().parents[2] / "frontend" / "src" / "api" / "schema.d.ts"
    ).read_text()
    assert (
        'detail: components["schemas"]["DocumentArtifactInvalidFileErrorDetail"] '
        '| components["schemas"]["DocumentArtifactInvalidRequestErrorDetail"];'
    ) in generated_types

    assert components["DocumentArtifactReason"]["enum"] == ["DOWNLOAD", "SEND", "UPLOAD"]
    assert components["DocumentArtifactValidationStatus"]["enum"] == [
        "MATCH",
        "WARNING",
        "INCONCLUSIVE",
    ]
    assert components["DocumentArtifactValidationCheckStatus"]["enum"] == [
        "MATCH",
        "MISMATCH",
        "NOT_FOUND",
    ]
    assert components["DocumentArtifactValidationField"]["enum"] == [
        "DOCUMENT_NUMBER",
        "DOCUMENT_KIND",
        "DOCUMENT_DATE",
        "SUPPLY_OR_ADVANCE_DATE",
        "SELLER",
        "BUYER",
        "CURRENCY",
        "TOTAL_EXCL_VAT",
        "VAT_TOTAL",
        "TOTAL_INCL_VAT",
    ]
    assert components["DocumentArtifactValidationConfidence"]["enum"] == ["HIGH", "MEDIUM", "LOW"]
    assert components["DocumentArtifactUploadConflictErrorDetail"]["properties"]["code"] == {
        "type": "string",
        "const": "ARTIFACT_ALREADY_EXISTS",
        "title": "Code",
    }
    assert components["DocumentArtifactValidationConflictErrorDetail"]["properties"]["code"] == {
        "type": "string",
        "enum": ["ARTIFACT_ALREADY_EXISTS", "AI_NOT_CONFIGURED"],
        "title": "Code",
    }


def test_artifact_upload_enums_and_setting_are_independent() -> None:
    assert DocumentArtifactReason.UPLOAD == "UPLOAD"
    assert DocumentArtifactValidationStatus.MATCH == "MATCH"
    assert DocumentArtifactValidationCheckStatus.MISMATCH == "MISMATCH"
    assert DocumentArtifactValidationField.VAT_TOTAL == "VAT_TOTAL"
    assert DocumentArtifactValidationConfidence.LOW == "LOW"
    assert Settings.model_fields["max_artifact_bytes"].default == 10 * 1024 * 1024
    assert Settings.model_fields["max_receipt_bytes"].default == 10 * 1024 * 1024


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        ("", "uploaded-document.pdf"),
        ("...", "uploaded-document.pdf"),
        ("../../legacy.pdf", "legacy.pdf"),
        (r"..\\legacy.pdf", "legacy.pdf"),
        ("safe\x00name\x85.pdf", "safename.pdf"),
        ("Ｆｕｌｌｗｉｄｔｈ.pdf", "Fullwidth.pdf"),
        ("x" * 300, "x" * 255),
    ],
)
def test_normalize_uploaded_artifact_filename_is_safe_and_deterministic(
    original: str, expected: str
) -> None:
    assert normalize_uploaded_artifact_filename(original) == expected

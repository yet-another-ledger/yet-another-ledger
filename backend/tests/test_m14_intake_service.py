"""Focused boundary tests for the M14 durable intake service."""

from __future__ import annotations

from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from fastapi import UploadFile
from httpx import AsyncClient

from jai.api.receipt_inbox import _read_uploads
from jai.services.receipt_inbox import _validate
from jai.services.storage import ReceiptValidationError

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 8
JPEG = b"\xff\xd8\xff\xe0" + b"x" * 8
WEBP = b"RIFF\x0c\x00\x00\x00WEBP" + b"x" * 8
PDF = b"%PDF-1.4\n"


@pytest.mark.asyncio
async def test_api_rejects_six_parts_before_reading_any_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = [UploadFile(file=BytesIO(PNG), filename=f"{index}.png") for index in range(6)]
    reads = [AsyncMock(side_effect=AssertionError("multipart part was read")) for _ in files]
    for file, read in zip(files, reads, strict=True):
        monkeypatch.setattr(file, "read", read)

    with pytest.raises(ReceiptValidationError):
        await _read_uploads(files, 1024)
    assert all(read.await_count == 0 for read in reads)


@pytest.mark.asyncio
async def test_api_reads_at_most_one_byte_past_per_file_limit() -> None:
    files = [UploadFile(file=BytesIO(PNG), filename="receipt.png")]
    result = await _read_uploads(files, 7)
    assert len(result[0][0]) == 8


@pytest.mark.asyncio
async def test_inbox_routes_require_authentication(client: AsyncClient) -> None:
    inbox_id = "00000000-0000-0000-0000-000000000001"
    file_id = "00000000-0000-0000-0000-000000000002"
    requests = [
        await client.post(
            "/api/v1/receipt-inbox",
            files=[("files", ("r.png", PNG, "image/png"))],
        ),
        await client.get("/api/v1/receipt-inbox"),
        await client.get(f"/api/v1/receipt-inbox/{inbox_id}"),
        await client.post(
            f"/api/v1/receipt-inbox/{inbox_id}/files",
            files=[("files", ("r.png", PNG, "image/png"))],
        ),
        await client.delete(f"/api/v1/receipt-inbox/{inbox_id}/files/{file_id}"),
        await client.delete(f"/api/v1/receipt-inbox/{inbox_id}"),
        await client.get(f"/api/v1/attachments/{file_id}/content"),
    ]
    assert [response.status_code for response in requests] == [401] * len(requests)


def test_intake_accepts_mixed_supported_group_and_keeps_display_names() -> None:
    parts = _validate(
        [
            (PNG, "image/png; charset=binary", "../../receipt.png"),
            (JPEG, "image/jpeg", "photo.jpg"),
            (WEBP, "image/webp", "photo.webp"),
            (PDF, "application/pdf", "scan.pdf"),
        ],
        1024,
    )

    assert [part[1] for part in parts] == [
        "image/png", "image/jpeg", "image/webp", "application/pdf",
    ]
    assert parts[0][2] == "../../receipt.png"  # display data only; never used for keys


@pytest.mark.parametrize(
    "parts",
    [
        [],
        [
            (PNG, "image/png", "one"),
            (PNG, "image/png", "two"),
            (PNG, "image/png", "three"),
            (PNG, "image/png", "four"),
            (PNG, "image/png", "five"),
            (PNG, "image/png", "six"),
        ],
    ],
)
def test_intake_rejects_empty_or_six_file_batch(
    parts: list[tuple[bytes, str, str | None]],
) -> None:
    with pytest.raises(ReceiptValidationError):
        _validate(parts, 1024)


@pytest.mark.parametrize(
    ("content", "mime", "limit"),
    [(PNG, "image/png", 4), (b"not an image", "image/png", 1024), (PNG, "image/svg+xml", 1024)],
)
def test_intake_rejects_oversized_spoofed_and_disallowed_parts(
    content: bytes, mime: str, limit: int
) -> None:
    with pytest.raises(ReceiptValidationError):
        _validate([(content, mime, "invoice.png")], limit)

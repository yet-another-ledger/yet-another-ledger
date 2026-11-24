"""Receipt Inbox HTTP coverage for additive M20 product recognition."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
import respx
from httpx import AsyncClient

from jai.schemas.setting import AiSettings
from jai.services.ai import _PROBE_PNG_BYTES
from jai.services.storage import Storage

pytestmark = pytest.mark.integration


class MemoryStorage(Storage):
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def save(self, namespace: str, key: str, content: bytes, content_type: str) -> str:
        del content_type
        storage_key = f"{namespace}/{key}"
        self.files[storage_key] = content
        return storage_key

    def open(self, key: str) -> bytes:
        return self.files[key]

    def delete(self, key: str) -> None:
        self.files.pop(key, None)

    def exists(self, key: str) -> bool:
        return key in self.files


@pytest.mark.parametrize(
    ("protocol", "endpoint"),
    [("chat_completions", "/chat/completions"), ("responses", "/responses")],
)
@pytest.mark.parametrize(
    ("model_result", "expected_state"),
    [
        (
            {
                "product_name": "Acme measuring instrument",
                "product_brand": "Acme",
                "product_model": "A4",
            },
            "READY",
        ),
        ({}, "FAILED"),
    ],
    ids=["product-only", "empty-recognition"],
)
@pytest.mark.asyncio
async def test_receipt_inbox_http_preserves_product_only_and_rejects_empty_results(
    db_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    endpoint: str,
    model_result: dict[str, str],
    expected_state: str,
) -> None:
    """Run controlled provider output through upload, parse, persistence and GET."""
    import pyotp

    from jai.services import ai as ai_service

    email = f"m20-product-only-{uuid.uuid4()}@example.com"
    registered = await db_client.post(
        "/api/v1/auth/register", json={"email": email, "password": "m20-test-password"}
    )
    assert registered.status_code == 201, registered.text
    login = await db_client.post(
        "/api/v1/auth/login", json={"email": email, "password": "m20-test-password"}
    )
    assert login.status_code == 200, login.text
    setup = await db_client.post("/api/v1/auth/mfa/setup")
    assert setup.status_code == 200, setup.text
    verified = await db_client.post(
        "/api/v1/auth/mfa/verify", json={"code": pyotp.TOTP(setup.json()["secret"]).now()}
    )
    assert verified.status_code == 204, verified.text
    company = await db_client.put(
        "/api/v1/company",
        json={"name": "M20 Receipt Inbox", "base_currency": "EUR", "country_code": "NL"},
    )
    assert company.status_code == 200, company.text

    storage = MemoryStorage()
    monkeypatch.setattr("jai.api.receipt_inbox.get_storage", lambda: storage)
    monkeypatch.setattr(ai_service, "get_storage", lambda: storage)

    async def controlled_config(_session: Any) -> AiSettings:
        return AiSettings(
            enabled=True,
            api_protocol=protocol,
            api_key="synthetic-m20-key",
            model="synthetic-m20-model",
            base_url="http://m20-inbox-provider",
        )

    monkeypatch.setattr(ai_service, "_get_ai_config", controlled_config)

    def provider_reply(_request: httpx.Request) -> httpx.Response:
        if protocol == "responses":
            return httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {"type": "output_text", "text": json.dumps(model_result)}
                            ],
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(model_result)}}]},
        )

    with respx.mock(base_url="http://m20-inbox-provider") as provider:
        route = provider.post(endpoint).mock(side_effect=provider_reply)
        submitted = await db_client.post(
            "/api/v1/receipt-inbox?language=en",
            files={"files": ("synthetic-receipt.png", _PROBE_PNG_BYTES, "image/png")},
        )

    assert route.call_count == 1
    assert submitted.status_code == 201, submitted.text
    result = submitted.json()
    assert result["ai_state"] == expected_state
    if expected_state == "READY":
        assert result["ai_error_code"] is None
        assert result["ai_suggestion"] == {
            **result["ai_suggestion"],
            "suggested_product_name": "Acme measuring instrument",
            "suggested_product_brand": "Acme",
            "suggested_product_model": "A4",
        }
        inbox_id = result["id"]
        detail = await db_client.get(f"/api/v1/receipt-inbox/{inbox_id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["ai_state"] == "READY"
        assert detail.json()["ai_suggestion"]["suggested_product_name"] == (
            "Acme measuring instrument"
        )
        assert detail.json()["ai_suggestion"]["suggested_product_brand"] == "Acme"
        assert detail.json()["ai_suggestion"]["suggested_product_model"] == "A4"
    else:
        assert result["ai_error_code"] == "AI_INVALID_RESPONSE"
        assert result["ai_suggestion"] is None

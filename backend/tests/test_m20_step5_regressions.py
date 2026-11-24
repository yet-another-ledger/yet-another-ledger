"""Focused M20 Step 5 regressions using only controlled synthetic providers."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jai.config import Settings
from jai.models._enums import SettingLevel
from jai.models.setting import Setting
from jai.schemas.setting import AiSettings
from jai.services import ai as ai_service
from jai.services.ai import _PROBE_PNG_BYTES
from jai.services.settings import _cache
from jai.services.storage import LocalStorage


@pytest.mark.asyncio
async def test_protocol_only_owner_save_keeps_environment_fields_live(
    db_client: AsyncClient,
    admin_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """A first protocol-only PUT remains sparse and GET keeps following env."""
    import pyotp

    email = f"m20-ai-settings-{uuid.uuid4()}@example.com"
    response = await db_client.post(
        "/api/v1/auth/register", json={"email": email, "password": "synthetic-password"}
    )
    assert response.status_code == 201, response.text
    response = await db_client.post(
        "/api/v1/auth/login", json={"email": email, "password": "synthetic-password"}
    )
    assert response.status_code == 200, response.text
    response = await db_client.post("/api/v1/auth/mfa/setup")
    assert response.status_code == 200, response.text
    response = await db_client.post(
        "/api/v1/auth/mfa/verify",
        json={"code": pyotp.TOTP(response.json()["secret"]).now()},
    )
    assert response.status_code == 204, response.text

    async with admin_session_maker() as session:
        await session.execute(
            delete(Setting).where(
                Setting.level == SettingLevel.GLOBAL,
                Setting.scope_id.is_(None),
                Setting.key == "ai",
            )
        )
        await session.commit()
    _cache.clear()

    env = Settings(
        ai_enabled=True,
        ai_api_protocol=None,
        ai_base_url="https://provider-one.invalid/v1",
        ai_api_key="synthetic-env-key-one",
        ai_model="synthetic-model-one",
        scheduler_enabled=False,
    )
    monkeypatch.setattr(ai_service, "get_settings", lambda: env)
    monkeypatch.setattr("jai.api.settings.get_settings", lambda: env)

    observed: list[httpx.Request] = []
    actual_test_ai_config = ai_service.test_ai_config

    async def synthetic_test_ai_config(config: AiSettings) -> Any:
        async def handler(request: httpx.Request) -> httpx.Response:
            observed.append(request)
            if config.api_protocol == "responses":
                return httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "output": [{"type": "message", "role": "assistant", "content": [
                            {"type": "output_text", "text": "ok"}
                        ]}],
                    },
                )
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "ok"}}]}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await actual_test_ai_config(config, client=client)

    monkeypatch.setattr(ai_service, "test_ai_config", synthetic_test_ai_config)
    # No row plus an explicit empty key/root uses the matching environment
    # credential. A different root emits no provider request.
    env_clear_probe = await db_client.post(
        "/api/v1/settings/ai/test", json={"api_key": "", "base_url": ""}
    )
    assert env_clear_probe.status_code == 200, env_clear_probe.text
    assert env_clear_probe.json()["ok"] is True
    assert len(observed) == 1
    assert str(observed[0].url) == "https://provider-one.invalid/v1/chat/completions"
    assert observed[0].headers["authorization"] == "Bearer synthetic-env-key-one"
    other_root_clear_probe = await db_client.post(
        "/api/v1/settings/ai/test",
        json={"api_key": "", "base_url": "https://other.invalid/v1"},
    )
    assert other_root_clear_probe.status_code == 200, other_root_clear_probe.text
    assert other_root_clear_probe.json()["ok"] is False
    assert len(observed) == 1
    observed.clear()

    before = await db_client.get("/api/v1/settings/ai")
    assert before.status_code == 200, before.text
    assert before.json() == {
        "enabled": True,
        "api_protocol": "chat_completions",
        "base_url": "https://provider-one.invalid/v1",
        "environment_base_url": "https://provider-one.invalid/v1",
        "api_key_set": True,
        "model": "synthetic-model-one",
        "receipt_prompt": "",
    }
    saved = await db_client.put(
        "/api/v1/settings/ai", json={"api_protocol": "responses"}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json() == {
        **before.json(),
        "api_protocol": "responses",
    }

    async with admin_session_maker() as session:
        row = await session.scalar(
            select(Setting).where(Setting.key == "ai")
        )
        assert row is not None
        assert row.value == {"api_protocol": "responses"}

    env = Settings(
        ai_enabled=True,
        ai_api_protocol=None,
        ai_base_url="https://provider-two.invalid/v1",
        ai_api_key="synthetic-env-key-two",
        ai_model="synthetic-model-two",
        scheduler_enabled=False,
    )
    observed.clear()
    # A stale page may submit a newly typed A credential after the effective
    # environment has rotated to B. Reject it before either transport or write.
    stale_test = await db_client.post(
        "/api/v1/settings/ai/test",
        json={"api_key": "synthetic-key-a", "expected_base_url": "https://provider-one.invalid/v1"},
    )
    assert stale_test.status_code == 409, stale_test.text
    assert "synthetic-key-a" not in stale_test.text
    assert "provider-one.invalid" not in stale_test.text
    assert observed == []
    stale_save = await db_client.put(
        "/api/v1/settings/ai",
        json={"api_key": "synthetic-key-a", "expected_base_url": "https://provider-one.invalid/v1"},
    )
    assert stale_save.status_code == 409, stale_save.text
    assert "synthetic-key-a" not in stale_save.text
    async with admin_session_maker() as session:
        unchanged = await session.scalar(select(Setting).where(Setting.key == "ai"))
        assert unchanged is not None
        assert unchanged.value == {"api_protocol": "responses"}

    saved_again = await db_client.put(
        "/api/v1/settings/ai", json={"api_protocol": "responses"}
    )
    assert saved_again.status_code == 200, saved_again.text
    assert saved_again.json()["base_url"] == "https://provider-two.invalid/v1"
    assert saved_again.json()["model"] == "synthetic-model-two"
    assert saved_again.json()["api_key_set"] is True
    assert saved_again.json()["enabled"] is True
    after = await db_client.get("/api/v1/settings/ai")
    assert after.status_code == 200, after.text
    assert after.json() == {
        "enabled": True,
        "api_protocol": "responses",
        "base_url": "https://provider-two.invalid/v1",
        "environment_base_url": "https://provider-two.invalid/v1",
        "api_key_set": True,
        "model": "synthetic-model-two",
        "receipt_prompt": "",
    }
    probe = await db_client.post(
        "/api/v1/settings/ai/test", json={"api_protocol": "responses"}
    )
    assert probe.status_code == 200, probe.text
    assert probe.json()["ok"] is True
    assert len(observed) == 1
    assert str(observed[0].url) == "https://provider-two.invalid/v1/responses"
    assert observed[0].headers["authorization"] == "Bearer synthetic-env-key-two"
    assert json.loads(observed[0].content)["model"] == "synthetic-model-two"
    blank_overlay_probe = await db_client.post(
        "/api/v1/settings/ai/test",
        json={"api_protocol": "responses", "base_url": "", "model": ""},
    )
    assert blank_overlay_probe.status_code == 200, blank_overlay_probe.text
    assert len(observed) == 2
    assert str(observed[1].url) == "https://provider-two.invalid/v1/responses"
    assert json.loads(observed[1].content)["model"] == "synthetic-model-two"

    current_provider_test = await db_client.post(
        "/api/v1/settings/ai/test",
        json={
            "api_key": "synthetic-key-b",
            "expected_base_url": "https://provider-two.invalid/v1",
        },
    )
    assert current_provider_test.status_code == 200, current_provider_test.text
    assert current_provider_test.json()["ok"] is True
    assert len(observed) == 3
    assert str(observed[2].url) == "https://provider-two.invalid/v1/responses"
    assert observed[2].headers["authorization"] == "Bearer synthetic-key-b"
    assert json.loads(observed[2].content)["model"] == "synthetic-model-two"
    current_provider_save = await db_client.put(
        "/api/v1/settings/ai",
        json={
            "api_key": "synthetic-key-b",
            "expected_base_url": "https://provider-two.invalid/v1",
        },
    )
    assert current_provider_save.status_code == 200, current_provider_save.text
    assert current_provider_save.json()["base_url"] == "https://provider-two.invalid/v1"
    async with admin_session_maker() as session:
        saved_key = await session.scalar(select(Setting).where(Setting.key == "ai"))
        assert saved_key is not None
        assert saved_key.value["api_key"] == "synthetic-key-b"
        assert saved_key.value["api_key_base_url"] == "https://provider-two.invalid/v1"
        assert "expected_base_url" not in saved_key.value

    # A later provider rotation keeps the stored B credential bound to B.
    # The environment A key is eligible because its configured root is A.
    env = Settings(
        ai_enabled=True,
        ai_api_protocol=None,
        ai_base_url="https://provider-a.invalid/v1",
        ai_api_key="synthetic-env-key-a",
        ai_model="synthetic-model-a",
        scheduler_enabled=False,
        storage_root=str(tmp_path / "storage"),
    )
    _cache.clear()
    rotated_probe = await db_client.post(
        "/api/v1/settings/ai/test", json={"api_protocol": "responses"}
    )
    assert rotated_probe.status_code == 200, rotated_probe.text
    assert len(observed) == 4
    assert str(observed[3].url) == "https://provider-a.invalid/v1/responses"
    assert observed[3].headers["authorization"] == "Bearer synthetic-env-key-a"
    assert observed[3].headers["authorization"] != "Bearer synthetic-key-b"

    # Exercise the stored credential through a real uploaded synthetic receipt
    # and both selected protocols. The mock captures the exact outgoing key.
    from jai.api import attachments as attachments_api

    storage = LocalStorage(str(tmp_path / "storage"))
    monkeypatch.setattr(attachments_api, "get_storage", lambda: storage)
    monkeypatch.setattr(ai_service, "get_storage", lambda: storage)
    company = await db_client.put(
        "/api/v1/company",
        json={"name": "M20 AI provenance", "base_currency": "EUR", "country_code": "NL"},
    )
    assert company.status_code == 200, company.text
    categories = await db_client.get("/api/v1/expense-categories")
    assert categories.status_code == 200, categories.text
    category = categories.json()["items"][0]
    treatments = await db_client.get("/api/v1/vat-treatments?side=PURCHASE")
    treatment = treatments.json()["items"][0]
    rates = await db_client.get("/api/v1/vat-rates")
    rate = rates.json()["items"][0]
    expense_response = await db_client.post(
        "/api/v1/expenses",
        json={
            "expense_date": "2026-11-24",
            "category_id": category["id"],
            "vat_treatment_id": treatment["id"],
            "vat_rate_id": rate["id"],
            "net_amount": "100.00",
            "vat_amount": "21.00",
        },
    )
    assert expense_response.status_code == 201, expense_response.text
    expense_id = expense_response.json()["id"]
    upload = await db_client.post(
        f"/api/v1/expenses/{expense_id}/attachments",
        files={"file": ("synthetic.png", _PROBE_PNG_BYTES, "image/png")},
    )
    assert upload.status_code == 201, upload.text
    attachment_id = upload.json()["id"]

    actual_requests: list[httpx.Request] = []
    with respx.mock(base_url="https://provider-a.invalid") as provider:
        def recognize(request: httpx.Request) -> httpx.Response:
            actual_requests.append(request)
            output = '{"expense_date":"2026-11-24","net_amount":"100.00"}'
            if request.url.path.endswith("/responses"):
                return httpx.Response(
                    200,
                    json={
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": output}],
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": output}}]},
            )

        provider.post(re.compile(r"/v1/(responses|chat/completions)$")).mock(side_effect=recognize)
        for protocol, endpoint in (
            ("responses", "/v1/responses"),
            ("chat_completions", "/v1/chat/completions"),
        ):
            changed = await db_client.put(
                "/api/v1/settings/ai", json={"api_protocol": protocol}
            )
            assert changed.status_code == 200, changed.text
            extraction = await db_client.post(
                "/api/v1/expenses/ai-extract", json={"attachment_id": attachment_id}
            )
            assert extraction.status_code == 200, extraction.text
            request = actual_requests[-1]
            assert request.url.path.endswith(endpoint)
            assert request.headers["authorization"] == "Bearer synthetic-env-key-a"
            assert request.headers["authorization"] != "Bearer synthetic-key-b"

    # With no environment key, a mismatch is a missing-key result and emits
    # no request; clearing the stored credential removes the provenance.
    env.ai_api_key = ""
    _cache.clear()
    before_requests = len(actual_requests)
    missing_probe = await db_client.post(
        "/api/v1/settings/ai/test", json={"api_protocol": "responses"}
    )
    assert missing_probe.status_code == 200, missing_probe.text
    assert missing_probe.json()["ok"] is False
    assert len(actual_requests) == before_requests
    cleared = await db_client.put("/api/v1/settings/ai", json={"api_key": ""})
    assert cleared.status_code == 200, cleared.text
    async with admin_session_maker() as session:
        cleared_row = await session.scalar(select(Setting).where(Setting.key == "ai"))
        assert cleared_row is not None
        assert cleared_row.value["api_key"] == ""
        assert cleared_row.value["api_key_base_url"] == ""

    env = Settings(
        ai_enabled=False,
        ai_api_protocol=None,
        ai_base_url="https://provider-two.invalid/v1",
        ai_api_key="synthetic-env-key-two",
        ai_model="synthetic-model-two",
        scheduler_enabled=False,
    )
    _cache.clear()
    async with admin_session_maker() as session:
        legacy = await session.scalar(select(Setting).where(Setting.key == "ai"))
        assert legacy is not None
        legacy.value = {}
        await session.commit()
    _cache.clear()
    legacy_before = await db_client.get("/api/v1/settings/ai")
    assert legacy_before.status_code == 200, legacy_before.text
    assert legacy_before.json() == {
        "enabled": False,
        "api_protocol": "chat_completions",
        "base_url": "https://api.openai.com/v1",
        "environment_base_url": "https://provider-two.invalid/v1",
        "api_key_set": False,
        "model": "synthetic-model-two",
        "receipt_prompt": "",
    }
    legacy_saved = await db_client.put(
        "/api/v1/settings/ai", json={"api_protocol": "responses"}
    )
    assert legacy_saved.status_code == 200, legacy_saved.text
    assert legacy_saved.json() == {
        **legacy_before.json(),
        "api_protocol": "responses",
    }
    async with admin_session_maker() as session:
        legacy_after = await session.scalar(select(Setting).where(Setting.key == "ai"))
        assert legacy_after is not None
        assert legacy_after.value == {
            "enabled": False,
            "base_url": "https://api.openai.com/v1",
            "model": "",
            "receipt_prompt": "",
            "api_protocol": "responses",
        }

    # Upgrade an unbound legacy key from provider A while saving provider B
    # without a key. The credential remains usable at A and cannot follow B.
    async with admin_session_maker() as session:
        legacy_before_upgrade = await session.scalar(
            select(Setting).where(Setting.key == "ai")
        )
        assert legacy_before_upgrade is not None
        legacy_before_upgrade.value = {
            "enabled": True,
            "base_url": "https://legacy-a.invalid/v1",
            "api_key": "legacy-a-key",
            "model": "legacy-a-model",
        }
        await session.commit()
    _cache.clear()
    env = Settings(
        ai_enabled=True,
        ai_api_protocol=None,
        ai_base_url="https://legacy-b.invalid/v1",
        ai_api_key="legacy-b-environment-key",
        ai_model="legacy-b-model",
        scheduler_enabled=False,
    )
    saved_b_without_key = await db_client.put(
        "/api/v1/settings/ai", json={"base_url": "https://legacy-b.invalid/v1"}
    )
    assert saved_b_without_key.status_code == 200, saved_b_without_key.text
    async with admin_session_maker() as session:
        upgraded = await session.scalar(select(Setting).where(Setting.key == "ai"))
        assert upgraded is not None
        assert upgraded.value["api_key"] == "legacy-a-key"
        assert upgraded.value["api_key_base_url"] == "https://legacy-a.invalid/v1"
    observed.clear()
    b_probe = await db_client.post("/api/v1/settings/ai/test", json={})
    assert b_probe.status_code == 200, b_probe.text
    assert len(observed) == 1
    assert str(observed[0].url) == "https://legacy-b.invalid/v1/chat/completions"
    assert observed[0].headers["authorization"] == "Bearer legacy-b-environment-key"
    env = Settings(
        ai_enabled=True,
        ai_api_protocol=None,
        ai_base_url="https://legacy-a.invalid/v1",
        ai_api_key="",
        ai_model="legacy-a-model",
        scheduler_enabled=False,
        storage_root=str(tmp_path / "storage"),
    )
    _cache.clear()
    returned_to_a = await db_client.put(
        "/api/v1/settings/ai", json={"base_url": "https://legacy-a.invalid/v1"}
    )
    assert returned_to_a.status_code == 200, returned_to_a.text
    a_probe = await db_client.post("/api/v1/settings/ai/test", json={})
    assert a_probe.status_code == 200, a_probe.text
    assert len(observed) == 2
    assert str(observed[1].url) == "https://legacy-a.invalid/v1/chat/completions"
    assert observed[1].headers["authorization"] == "Bearer legacy-a-key"
    from jai.api import attachments as attachments_api

    storage = LocalStorage(str(tmp_path / "storage"))
    monkeypatch.setattr(attachments_api, "get_storage", lambda: storage)
    monkeypatch.setattr(ai_service, "get_storage", lambda: storage)
    legacy_requests: list[httpx.Request] = []
    with respx.mock(base_url="https://legacy-a.invalid") as legacy_provider:
        def legacy_recognition(request: httpx.Request) -> httpx.Response:
            legacy_requests.append(request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{"expense_date":"2026-11-24"}'}}]},
            )

        legacy_provider.post("/v1/chat/completions").mock(side_effect=legacy_recognition)
        legacy_extract = await db_client.post(
            "/api/v1/expenses/ai-extract", json={"attachment_id": attachment_id}
        )
    assert legacy_extract.status_code == 200, legacy_extract.text
    assert len(legacy_requests) == 1
    assert legacy_requests[0].headers["authorization"] == "Bearer legacy-a-key"

    # Explicit clear with no row keeps the matching environment key through
    # save, test, and the actual receipt route.
    async with admin_session_maker() as session:
        await session.execute(delete(Setting).where(Setting.key == "ai"))
        await session.commit()
    _cache.clear()
    env = Settings(
        ai_enabled=True,
        ai_api_protocol=None,
        ai_base_url="https://fallback-a.invalid/v1",
        ai_api_key="fallback-a-key",
        ai_model="fallback-model",
        scheduler_enabled=False,
        storage_root=str(tmp_path / "storage"),
    )
    observed.clear()
    clear_saved = await db_client.put(
        "/api/v1/settings/ai", json={"api_key": "", "base_url": ""}
    )
    assert clear_saved.status_code == 200, clear_saved.text
    assert clear_saved.json()["environment_base_url"] == "https://fallback-a.invalid/v1"
    saved_clear_probe = await db_client.post(
        "/api/v1/settings/ai/test", json={"api_key": "", "base_url": ""}
    )
    assert saved_clear_probe.status_code == 200, saved_clear_probe.text
    assert saved_clear_probe.json()["ok"] is True
    assert len(observed) == 1
    assert str(observed[0].url) == "https://fallback-a.invalid/v1/chat/completions"
    assert observed[0].headers["authorization"] == "Bearer fallback-a-key"

    fallback_requests: list[httpx.Request] = []
    with respx.mock(base_url="https://fallback-a.invalid") as fallback_provider:
        def fallback_recognition(request: httpx.Request) -> httpx.Response:
            fallback_requests.append(request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{"expense_date":"2026-11-24"}'}}]},
            )

        fallback_provider.post("/v1/chat/completions").mock(side_effect=fallback_recognition)
        fallback_extract = await db_client.post(
            "/api/v1/expenses/ai-extract", json={"attachment_id": attachment_id}
        )
    assert fallback_extract.status_code == 200, fallback_extract.text
    assert len(fallback_requests) == 1
    assert fallback_requests[0].headers["authorization"] == "Bearer fallback-a-key"

    # The blank input is guarded using the concrete environment root shown in
    # the form. A stale value after environment rotation still receives 409.
    current_key_test = await db_client.post(
        "/api/v1/settings/ai/test",
        json={"base_url": "", "api_key": "new-fallback-key",
              "expected_base_url": "https://fallback-a.invalid/v1"},
    )
    assert current_key_test.status_code == 200, current_key_test.text
    assert observed[-1].headers["authorization"] == "Bearer new-fallback-key"
    env.ai_base_url = "https://fallback-rotated.invalid/v1"
    _cache.clear()
    stale_key_test = await db_client.post(
        "/api/v1/settings/ai/test",
        json={"base_url": "", "api_key": "stale-key",
              "expected_base_url": "https://fallback-a.invalid/v1"},
    )
    assert stale_key_test.status_code == 409, stale_key_test.text
    stale_key_save = await db_client.put(
        "/api/v1/settings/ai",
        json={"base_url": "", "api_key": "stale-key",
              "expected_base_url": "https://fallback-a.invalid/v1"},
    )
    assert stale_key_save.status_code == 409, stale_key_save.text


def test_ai_credential_provenance_blocks_unsaved_provider_transfer() -> None:
    environment = Settings(
        ai_enabled=True,
        ai_base_url="https://provider-b.invalid/v1",
        ai_api_key="environment-b-key",
        ai_model="model-b",
    )
    # An env-only key follows its configured root and is removed if an
    # unsaved probe changes the destination.
    env_only = ai_service._resolve_ai_config(None, environment)
    assert env_only.api_key == "environment-b-key"
    assert ai_service._resolve_ai_config(
        None, environment, overrides={"base_url": "https://provider-b.invalid/v1"}
    ).api_key == "environment-b-key"
    assert ai_service._resolve_ai_config(
        None, environment, overrides={"base_url": "https://provider-a.invalid/v1"}
    ).api_key == ""
    assert ai_service._resolve_ai_config(
        None,
        environment,
        overrides={"base_url": "https://provider-a.invalid/v1", "api_key": ""},
    ).api_key == ""
    assert ai_service._resolve_ai_config(
        None,
        environment,
        overrides={"base_url": "", "api_key": ""},
    ).api_key == "environment-b-key"

    stored = AiSettings(
        api_protocol="responses",
        api_key="stored-b-key",
        api_key_base_url="https://provider-b.invalid/v1",
    )
    assert ai_service._resolve_ai_config(stored, environment).api_key == "stored-b-key"
    # A matching environment key can serve the newly effective root; the
    # saved B key itself never follows that override.
    rotated = Settings(
        ai_enabled=True,
        ai_base_url="https://provider-a.invalid/v1",
        ai_api_key="environment-a-key",
        ai_model="model-a",
    )
    assert ai_service._resolve_ai_config(stored, rotated).api_key == "environment-a-key"
    assert ai_service._resolve_ai_config(
        stored, rotated, overrides={"base_url": "https://provider-c.invalid/v1"}
    ).api_key == ""
    assert ai_service._resolve_ai_config(
        stored,
        rotated,
        overrides={"base_url": "https://provider-c.invalid/v1", "api_key": ""},
    ).api_key == ""

    # Legacy credentials remain usable at their previously effective root,
    # without being transferred to a new probe destination.
    legacy = AiSettings(api_key="legacy-key", base_url="https://provider-b.invalid/v1")
    assert ai_service._resolve_ai_config(legacy, environment).api_key == "legacy-key"
    assert ai_service._resolve_ai_config(
        legacy, environment, overrides={"base_url": "https://provider-a.invalid/v1"}
    ).api_key == ""
    assert ai_service._resolve_ai_config(
        stored, rotated, overrides={"api_key": "temporary-key"}
    ).api_key == "temporary-key"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"error": {"param": "text.format", "message": "text.format is not supported"}}, True),
        ({"error": {"message": "JSON response format is not supported"}}, True),
        ({"error": {"message": "This model does not support text.format."}}, True),
        ({"error": {"message": "This model does not support response_format."}}, True),
        (
            {
                "error": {
                    "code": "response_format_not_supported",
                    "message": "Invalid request",
                }
            },
            True,
        ),
        (
            {"error": {"message": "Unsupported JSON object in image_url; expected a URL string."}},
            False,
        ),
        (
            {
                "error": {
                    "message": (
                        "Unsupported JSON object at input[0].content[0]; "
                        "expected a URL string."
                    )
                }
            },
            False,
        ),
        (
            {
                "error": {"param": "input[0].image_url", "message": "Unsupported image format"},
                "request_metadata": {"preferred_format": "json_object"},
            },
            False,
        ),
        (
            {"error": {"param": "model", "message": "Unknown model"}, "format": "unsupported"},
            False,
        ),
        ({"error": {"param": "model", "message": "JSON format is unsupported"}}, False),
    ],
)
def test_json_mode_fallback_requires_specific_format_rejection(
    payload: dict[str, Any], expected: bool
) -> None:
    request = httpx.Request("POST", "https://provider.invalid/v1/responses")
    response = httpx.Response(400, request=request, json=payload)
    error = httpx.HTTPStatusError("controlled rejection", request=request, response=response)
    assert ai_service._explicit_json_format_rejection(error) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("protocol", "message", "should_retry"),
    [
        (protocol, "Unsupported JSON object in image_url; expected a URL string.", False)
        for protocol in ("responses", "chat_completions")
    ]
    + [
        (
            protocol,
            "Unsupported JSON object at input[0].content[0]; expected a URL string.",
            False,
        )
        for protocol in ("responses", "chat_completions")
    ]
    + [
        (protocol, message, True)
        for protocol in ("responses", "chat_completions")
        for message in (
            "This model does not support text.format.",
            "This model does not support response_format.",
        )
    ]
    + [
        (protocol, "Unsupported image format. Requested response format was json_object.", False)
        for protocol in ("responses", "chat_completions")
    ],
)
async def test_json_fallback_uses_specific_subject_and_retries_same_protocol_once(
    protocol: str, message: str, should_retry: bool
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(400, json={"error": {"message": message}})
        if protocol == "responses":
            return httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "{}"}],
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{page}"},
                }
                for page in ("synthetic-page-one", "synthetic-page-two")
            ] + [{"type": "text", "text": "Extract this synthetic receipt."}],
        }
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        if should_retry:
            result = await ai_service._call_chat_completions(
                "https://provider.invalid/v1",
                "synthetic-key",
                "synthetic-model",
                messages,
                client,
                request_json_mode=True,
                api_protocol=protocol,
            )
            assert result == "{}"
        else:
            with pytest.raises(httpx.HTTPStatusError):
                await ai_service._call_chat_completions(
                    "https://provider.invalid/v1",
                    "synthetic-key",
                    "synthetic-model",
                    messages,
                    client,
                    request_json_mode=True,
                    api_protocol=protocol,
                )

    assert len(requests) == (2 if should_retry else 1)
    endpoint = "responses" if protocol == "responses" else "chat/completions"
    assert {str(request.url) for request in requests} == {
        f"https://provider.invalid/v1/{endpoint}"
    }
    for request in requests:
        sent = json.loads(request.content)
        if protocol == "responses":
            content = sent["input"][0]["content"]
        else:
            content = sent["messages"][0]["content"]
        images = [item for item in content if item["type"] in {"input_image", "image_url"}]
        assert len(images) == 2
    if should_retry:
        assert "text" not in json.loads(requests[1].content) if protocol == "responses" else (
            "response_format" not in json.loads(requests[1].content)
        )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            {
                "param": "image_url",
                "code": "invalid_image_url",
                "message": "Invalid image URL. Expected an HTTPS source.",
            },
            False,
        ),
        (
            {"param": "image_url", "code": "invalid_image_type", "message": "Invalid image type."},
            False,
        ),
        (
            {
                "param": "image_url",
                "code": "image_too_large",
                "message": "Image exceeds the size limit.",
            },
            False,
        ),
        (
            {
                "param": "image_url",
                "code": "image_input_not_supported",
                "message": "This model does not support image input.",
            },
            True,
        ),
        (
            {
                "param": "messages[0].content[0]",
                "code": "image_input_not_supported",
                "message": "This model does not support image input.",
            },
            True,
        ),
        (
            {
                "param": "image_url",
                "code": "invalid_image_shape",
                "message": "Unsupported image shape.",
            },
            False,
        ),
        (
            {
                "param": "image_url",
                "code": "invalid_image_url",
                "message": "Unsupported image input format. Expected an HTTPS URL.",
            },
            False,
        ),
        ({"message": "This model does not support image input."}, True),
    ],
)
def test_image_probe_requires_explicit_capability_rejection(
    error: dict[str, str], expected: bool
) -> None:
    request = httpx.Request("POST", "https://provider.invalid/v1/responses")
    response = httpx.Response(400, request=request, json={"error": error})
    failure = httpx.HTTPStatusError("synthetic rejection", request=request, response=response)
    assert ai_service._explicit_image_input_rejection(failure) is expected


@pytest.mark.asyncio
async def test_generic_400_with_format_metadata_is_not_retried() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            400,
            json={
                "error": {
                    "param": "input[0].content[0].image_url",
                    "message": "Unsupported image format",
                },
                "request_metadata": {"preferred_format": "json_object"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await ai_service._call_responses(
                "https://provider.invalid/v1",
                "synthetic-key",
                "synthetic-model",
                [],
                client,
                request_json_mode=True,
                max_tokens=64,
            )
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"status": "failed", "error": {"code": "synthetic"}, "output": []},
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "incomplete",
                    "content": [{"type": "output_text", "text": "{}"}],
                }
            ],
        },
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "refusal", "refusal": "synthetic refusal"},
                        {"type": "output_text", "text": "{}"},
                    ],
                }
            ],
        },
    ],
)
async def test_responses_rejects_explicit_failure_incomplete_and_refusal(
    body: dict[str, Any],
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError):
            await ai_service._call_responses(
                "https://provider.invalid/v1",
                "synthetic-key",
                "synthetic-model",
                [],
                client,
                request_json_mode=False,
                max_tokens=64,
            )


@pytest.mark.asyncio
async def test_responses_accepts_omitted_optional_status_and_chat_rejects_refusal() -> None:
    async def response_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": '{"ok":true}'}],
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response_handler)) as client:
        assert await ai_service._call_responses(
            "https://provider.invalid/v1",
            "synthetic-key",
            "synthetic-model",
            [],
            client,
            request_json_mode=False,
            max_tokens=64,
        ) == '{"ok":true}'

    async def chat_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"refusal": "synthetic refusal", "content": "{}"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(chat_handler)) as client:
        with pytest.raises(RuntimeError, match="refused"):
            await ai_service._call_chat_completions(
                "https://provider.invalid/v1",
                "synthetic-key",
                "synthetic-model",
                [],
                client,
                request_json_mode=False,
                max_tokens=64,
            )


@pytest.mark.asyncio
async def test_json_format_rejection_retries_once_on_same_responses_endpoint() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "param": "text.format",
                        "message": "text.format is not supported",
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "{}"}],
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ai_service._call_responses(
            "https://provider.invalid/v1",
            "synthetic-key",
            "synthetic-model",
            [],
            client,
            request_json_mode=True,
            max_tokens=64,
        )
    assert result == "{}"
    assert len(requests) == 2
    assert [str(request.url) for request in requests] == [
        "https://provider.invalid/v1/responses",
        "https://provider.invalid/v1/responses",
    ]
    assert "text" in json.loads(requests[0].content)
    assert "text" not in json.loads(requests[1].content)


@pytest.mark.asyncio
async def test_direct_extraction_rejects_unparseable_json_and_keeps_partial_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    config = AiSettings(
        enabled=True,
        api_protocol="responses",
        base_url="https://provider.invalid/v1",
        api_key="synthetic-key",
        model="synthetic-model",
    )
    monkeypatch.setattr(ai_service, "_get_ai_config", AsyncMock(return_value=config))
    monkeypatch.setattr(ai_service, "_match_category", AsyncMock(return_value=None))

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "not JSON"}],
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ai_service.AIExtractionError, match="invalid response"):
            await ai_service.extract_from_files(
                AsyncMock(),  # type: ignore[arg-type]
                uuid.uuid4(),
                [("image/png", b"synthetic-image")],
                client=client,
                strict_response=True,
            )


@pytest.mark.asyncio
async def test_probe_400_is_a_safe_failure_with_selected_protocol() -> None:
    marker = "provider-secret-detail-must-not-leak"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": marker}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ai_service.test_ai_config(
            AiSettings(
                enabled=True,
                api_protocol="responses",
                base_url="https://provider.invalid/v1",
                api_key="synthetic-key",
                model="synthetic-model",
            ),
            client=client,
        )
    assert result.ok is False
    assert result.multimodal is False
    assert result.api_protocol == "responses"
    assert "HTTP 400" in result.detail
    assert "responses" in result.detail
    assert marker not in result.detail

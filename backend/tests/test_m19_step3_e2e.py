"""M19 regression across the existing MCP intake, owner review and reports."""

from __future__ import annotations

import base64
import json
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import respx
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_m15_step4_integration import _call, _initialize

import jai.api.mcp as mcp_module
import jai.api.receipt_inbox as receipt_inbox_api
import jai.services.agent_receipts as agent_receipts
import jai.services.ai as ai_service
from jai.config import Settings
from jai.models.expense import Expense
from jai.models.expense_attachment import ExpenseAttachment
from jai.models.user import User
from jai.schemas.agent_token import AgentTokenCreate, AgentTokenScope
from jai.services.agent_tokens import issue_token
from jai.services.storage import LocalStorage

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_mcp_receipt_review_confirmation_and_report_projection(
    db_client: AsyncClient,
    admin_session_maker: async_sessionmaker[AsyncSession],
    runtime_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """MCP intake and web review share validated extraction; only confirm books."""
    import pyotp
    from test_agent_business_integration import _fresh_mcp_wire_app

    email = f"m19-mcp-review-{uuid.uuid4()}@example.com"
    login = await db_client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "m19-test-password"},
    )
    assert login.status_code == 201, login.text
    user_id = uuid.UUID(login.json()["id"])
    login = await db_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "m19-test-password"},
    )
    assert login.status_code == 200, login.text
    setup = await db_client.post("/api/v1/auth/mfa/setup")
    assert setup.status_code == 200, setup.text
    verified = await db_client.post(
        "/api/v1/auth/mfa/verify",
        json={"code": pyotp.TOTP(setup.json()["secret"]).now()},
    )
    assert verified.status_code == 204, verified.text
    company = await db_client.put(
        "/api/v1/company",
        json={"name": "M19 MCP Review", "base_currency": "EUR", "country_code": "NL"},
    )
    assert company.status_code == 200, company.text
    async with admin_session_maker() as session:
        user = await session.get(User, user_id)
        assert user is not None and user.company_id is not None
        company_id = user.company_id
        token, secret = issue_token(
            AgentTokenCreate(name="M19 receipt intake", scopes=[AgentTokenScope.RECEIPT_INTAKE]),
            company_id,
        )
        token.creator_id = user_id
        session.add(token)
        await session.commit()
    treatments_response = await db_client.get("/api/v1/vat-treatments?side=PURCHASE")
    treatments = {row["code"]: row for row in treatments_response.json()["items"]}
    reverse_treatment = treatments["EU_B2B_REVERSE_PURCH"]
    rates_response = await db_client.get("/api/v1/vat-rates")
    rate = next(row for row in rates_response.json()["items"] if Decimal(row["percent"]) == 21)
    category_response = await db_client.post(
        "/api/v1/expense-categories", json={"name": "M19 Supplies", "default_deductible": True}
    )
    assert category_response.status_code == 201, category_response.text

    storage_root = tmp_path / "receipt-storage"
    storage = LocalStorage(str(storage_root))
    test_settings = Settings(
        max_receipt_bytes=1024 * 1024,
        base_url="https://jai.example.test",
        storage_root=str(storage_root),
        scheduler_enabled=False,
    )
    monkeypatch.setattr(mcp_module, "get_storage", lambda: storage)
    monkeypatch.setattr(mcp_module, "get_session_maker", lambda: runtime_session_maker)
    monkeypatch.setattr(mcp_module, "get_settings", lambda: test_settings)
    monkeypatch.setattr(agent_receipts, "get_settings", lambda: test_settings)
    monkeypatch.setattr(receipt_inbox_api, "get_storage", lambda: storage)
    monkeypatch.setattr(ai_service, "get_storage", lambda: storage)

    async def test_ai_config(_session: AsyncSession) -> object:
        from jai.schemas.setting import AiSettings

        return AiSettings(
            enabled=True,
            api_protocol="chat_completions",
            api_key="synthetic-m19-key",
            model="synthetic-m19-model",
            base_url="http://m19-provider",
        )

    monkeypatch.setattr(ai_service, "_get_ai_config", test_ai_config)
    reason = "Invoice states intra-community reverse charge."
    model_json: dict[str, object] = {
        "expense_date": "2026-10-12",
        "supplier_name": "EU Parts BV",
        "net_amount": "100.00",
        "vat_amount": "0.00",
        "vat_rate_percent": "0",
        "vat_treatment_suggestion": {
            "code": "EU_B2B_REVERSE_PURCH",
            "reason": reason,
        },
    }
    provider_calls = 0

    def provider(_request: Any) -> Any:
        nonlocal provider_calls
        provider_calls += 1
        import httpx

        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(model_json)}}]},
        )

    from jai.services.ai import _PROBE_PNG_BYTES

    transport_app = _fresh_mcp_wire_app()
    async with respx.mock(base_url="http://m19-provider") as mocked_provider:
        mocked_provider.post("/chat/completions").mock(side_effect=provider)
        async with mcp_module.server.session_manager.run():
            async with AsyncClient(
                transport=ASGITransport(app=transport_app), base_url="http://localhost"
            ) as mcp_client:
                session_id = await _initialize(mcp_client, secret)
                encoded = base64.b64encode(_PROBE_PNG_BYTES).decode("ascii")
                submit_status, result, submit_wire = await _call(
                    mcp_client,
                    session_id,
                    secret,
                    2,
                    "submit_receipt",
                    {
                        "request_id": str(uuid.uuid4()),
                        "source": {"platform": "test"},
                        "language": "en",
                        "files": [
                            {
                                "filename": "eu-parts.png",
                                "mime_type": "image/png",
                                "data_base64": encoded,
                            }
                        ],
                    },
                )
                assert submit_status == 200 and result is not None, submit_wire
                submit_snapshot = result.get("structuredContent") or json.loads(
                    result["content"][0]["text"]
                )
                status, status_result, status_wire = await _call(
                    mcp_client,
                    session_id,
                    secret,
                    3,
                    "get_receipt_status",
                    {"inbox_id": submit_snapshot["inbox_id"]},
                )
    assert status == 200 and result is not None, status_wire
    submitted = result.get("structuredContent") or json.loads(result["content"][0]["text"])
    assert submitted["status"] == "created"
    assert submitted["ai_state"] == "NOT_RUN"
    assert submitted["review_url"].endswith(f"/receipt-inbox/{submitted['inbox_id']}")
    assert provider_calls == 1
    inbox_id = submitted["inbox_id"]

    assert status == 200 and status_result is not None, status_wire
    current_status = status_result.get("structuredContent") or json.loads(
        status_result["content"][0]["text"]
    )
    assert current_status["ai_state"] == "READY"
    assert current_status["inbox_id"] == inbox_id

    before = await db_client.get("/api/v1/expenses?limit=100")
    assert before.status_code == 200, before.text
    assert before.json()["total"] == 0
    detail_response = await db_client.get(f"/api/v1/receipt-inbox/{inbox_id}")
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()
    assert detail["status"] == "PENDING"
    assert detail["ai_state"] == "READY"
    assert detail["file_revision"] == 1
    assert len(detail["files"]) == 1
    assert detail["files"][0]["filename"] == "eu-parts.png"
    suggestion = detail["ai_suggestion"]["suggested_vat_treatment"]
    assert suggestion == {
        "id": reverse_treatment["id"],
        "code": "EU_B2B_REVERSE_PURCH",
        "reason": reason,
    }
    assert Decimal(detail["ai_suggestion"]["vat_amount"]) == Decimal("0.00")
    assert detail["ai_suggestion"]["vat_rate_percent"] is None

    listed_attachment_files = list((storage_root / "receipts").rglob("*"))
    stored_originals = [path for path in listed_attachment_files if path.is_file()]
    assert len(stored_originals) == 1
    original_bytes = stored_originals[0].read_bytes()
    assert original_bytes == _PROBE_PNG_BYTES
    confirmation_body = {
        "expense_date": "2026-10-12",
        "category_id": category_response.json()["id"],
        "supplier_name": "EU Parts BV",
        "vat_treatment_id": reverse_treatment["id"],
        "vat_rate_id": rate["id"],
        "net_amount": "100.00",
        "vat_amount": "0.00",
        "deductible": True,
        "include_in_pl": True,
        "pl_cost_basis": "NET",
    }
    confirmation = await db_client.post(
        f"/api/v1/receipt-inbox/{inbox_id}/confirm?expected_file_revision={detail['file_revision']}",
        json=confirmation_body,
    )
    assert confirmation.status_code == 201, confirmation.text
    booked = confirmation.json()["expense"]
    assert booked["vat_treatment_code"] == "EU_B2B_REVERSE_PURCH"
    assert booked["vat_rate_id"] == rate["id"]
    assert Decimal(booked["vat_rate_percent"]) == Decimal("21")
    assert Decimal(booked["vat_amount"]) == Decimal("0.00")

    attachments_response = await db_client.get(
        f"/api/v1/expenses/{booked['id']}/attachments"
    )
    assert attachments_response.status_code == 200, attachments_response.text
    assert len(attachments_response.json()["items"]) == 1
    async with admin_session_maker() as session:
        attachment = await session.scalar(
            select(ExpenseAttachment).where(ExpenseAttachment.expense_id == uuid.UUID(booked["id"]))
        )
        assert attachment is not None
        assert str(attachment.id) == detail["files"][0]["id"]
        assert attachment.filename == "eu-parts.png"
        assert attachment.receipt_inbox_id is None
        assert storage.open(attachment.storage_key) == original_bytes
        count = await session.scalar(select(func.count()).select_from(Expense))
        assert count == 1

    replay = await db_client.post(
        f"/api/v1/receipt-inbox/{inbox_id}/confirm?expected_file_revision={detail['file_revision']}",
        json=confirmation_body,
    )
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"] == "ALREADY_CONFIRMED"
    async with admin_session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(Expense)) == 1

    pl = await db_client.get(
        "/api/v1/reports/profit-loss?from=2026-01-01&to=2026-12-31"
    )
    vat = await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=4")
    assert pl.status_code == vat.status_code == 200, (pl.text, vat.text)
    assert Decimal(pl.json()["expense_actual"]) == Decimal("100.00")
    boxes = vat.json()["boxes"]
    assert Decimal(boxes["box_4b"]["base"]) == Decimal("100.00")
    assert Decimal(boxes["box_4b"]["vat"]) == Decimal("21.00")
    assert Decimal(boxes["box_5b"]["vat"]) == Decimal("21.00")

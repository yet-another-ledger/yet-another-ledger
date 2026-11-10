"""Actual Streamable HTTP coverage for M18 options and preview tools."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.types import ASGIApp

from jai.api import mcp as mcp_module
from jai.config import Settings
from jai.models._enums import SettingLevel, VatTreatmentEffect, VatTreatmentSide
from jai.models.agent_operation import AgentOperation
from jai.models.agent_token import AgentToken
from jai.models.company import Company
from jai.models.dictionary import ExpenseCategory
from jai.models.expense import Expense
from jai.models.mileage import MileageRate, MileageTransportType, MileageTrip
from jai.models.setting import Setting
from jai.models.user import User
from jai.models.vat import VatRate, VatTreatment
from jai.schemas.agent_token import AgentTokenCreate, AgentTokenScope
from jai.schemas.setting import SETTING_KEY_MILEAGE_DEFAULTS
from jai.services.agent_tokens import issue_token
from jai.services.mileage import seed_for_company as seed_mileage
from jai.services.vat import seed_for_company as seed_vat


def _fresh_mcp_wire_app() -> ASGIApp:
    sdk_app = mcp_module.server.streamable_http_app(
        streamable_http_path="/",
        transport_security=mcp_module.TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )
    return mcp_module.ClosedMCPBoundary(
        Starlette(
            routes=[
                Route(
                    "/api/v1/mcp",
                    mcp_module.ExactMCPEntry(sdk_app),
                    methods=["GET", "POST", "DELETE"],
                ),
                Mount("/api/v1/mcp", sdk_app),
            ]
        )
    )


@pytest.mark.integration
async def test_mileage_options_and_preview_work_over_mcp_without_writes(
    admin_session_maker: async_sessionmaker[AsyncSession],
    runtime_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    company_id, foreign_company_id, owner_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    from jai.services import agent_mileage

    assembled_payloads: list[dict[str, object]] = []
    original_sign = agent_mileage._preview_token

    def capture_signed_payload(payload: dict[str, object]) -> str:
        assembled_payloads.append(payload)
        return original_sign(payload)

    monkeypatch.setattr(agent_mileage, "_preview_token", capture_signed_payload)
    token, secret = issue_token(
        AgentTokenCreate(name="M18 preview", scopes=[AgentTokenScope.MILEAGE_CREATE]), company_id
    )
    token.creator_id = owner_id
    old_token, old_secret = issue_token(
        AgentTokenCreate(name="M15 old", scopes=[AgentTokenScope.EXPENSE_READ]), company_id
    )
    old_token.creator_id = owner_id
    async with admin_session_maker() as session:
        session.add_all(
            [
                Company(id=company_id, name="M18 Company", base_currency="EUR"),
                Company(id=foreign_company_id, name="M18 Foreign", base_currency="EUR"),
                User(
                    id=owner_id,
                    email=f"{owner_id}@example.test",
                    hashed_password="unused-test-hash",
                    company_id=company_id,
                    role="owner",
                    mfa_enabled=True,
                    totp_secret="test-secret",
                ),
            ]
        )
        await session.flush()
        await seed_mileage(session, company_id)
        await seed_vat(session, company_id)
        await seed_mileage(session, foreign_company_id)
        await seed_vat(session, foreign_company_id)
        foreign_transport_id = await session.scalar(
            select(MileageTransportType.id).where(
                MileageTransportType.company_id == foreign_company_id,
                MileageTransportType.name == "Car",
            )
        )
        assert foreign_transport_id is not None
        motorcycle_id = await session.scalar(
            select(MileageTransportType.id).where(
                MileageTransportType.company_id == company_id,
                MileageTransportType.name == "Motorcycle",
            )
        )
        mileage_category = await session.scalar(
            select(ExpenseCategory).where(
                ExpenseCategory.company_id == company_id,
                ExpenseCategory.name == "Mileage",
            )
        )
        assert motorcycle_id is not None and mileage_category is not None
        mileage_category_id = mileage_category.id
        defaults_setting = await session.scalar(
            select(Setting).where(
                Setting.level == SettingLevel.COMPANY,
                Setting.scope_id == company_id,
                Setting.key == SETTING_KEY_MILEAGE_DEFAULTS,
            )
        )
        assert defaults_setting is not None
        mileage_treatment = await session.scalar(
            select(VatTreatment).where(
                VatTreatment.company_id == company_id,
                VatTreatment.code == "NL_PRIVATE_TRANSPORT_MILEAGE",
            )
        )
        assert mileage_treatment is not None
        zero_vat_rate = await session.scalar(
            select(VatRate).where(
                VatRate.company_id == company_id,
                VatRate.label == "Zero (0%)",
            )
        )
        assert zero_vat_rate is not None
        scoped_rate = MileageRate(
            company_id=company_id,
            transport_type_id=motorcycle_id,
            effective_from=date(2026, 6, 1),
            rate_per_km=Decimal("0.333"),
        )
        session.add(scoped_rate)
        await session.flush()
        session.add_all([token, old_token])
        await session.commit()

    monkeypatch.setattr(mcp_module, "get_session_maker", lambda: runtime_session_maker)
    settings = Settings(base_url="https://m18.example.test", scheduler_enabled=False)
    monkeypatch.setattr(mcp_module, "get_settings", lambda: settings)

    transport_app = _fresh_mcp_wire_app()
    async with mcp_module.server.session_manager.run():
        async with AsyncClient(
            transport=ASGITransport(app=transport_app), base_url="http://localhost"
        ) as client:
            initialize = await client.post(
                "/api/v1/mcp",
                headers={
                    "authorization": f"Bearer {secret}",
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "m18-preview-test", "version": "1"},
                    },
                },
            )
            assert initialize.status_code == 200
            headers = {
                "authorization": f"Bearer {secret}",
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
                "mcp-session-id": initialize.headers["mcp-session-id"],
                "mcp-protocol-version": "2025-11-25",
            }

            async def call(
                name: str, arguments: dict[str, object], call_id: int
            ) -> Response:
                return await client.post(
                    "/api/v1/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": call_id,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    },
                )

            discovered = await client.post(
                "/api/v1/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            assert discovered.status_code == 200
            tools = json.loads(discovered.text.split("data: ", 1)[1])["result"]["tools"]
            assert {tool["name"] for tool in tools} >= {
                "get_mileage_options",
                "preview_mileage_expense",
            }

            options_response = await call("get_mileage_options", {}, 3)
            assert options_response.status_code == 200
            options = json.loads(options_response.text.split("data: ", 1)[1])["result"][
                "structuredContent"
            ]
            assert options["default_transport_type_id"]
            assert {item["name"] for item in options["transport_types"]} == {
                "Car",
                "Motorcycle",
                "Bicycle",
                "Other",
            }

            request_id = uuid.uuid4()
            base_mileage = {
                "trip_date": date(2026, 11, 10).isoformat(),
                "one_way_distance_km": "10.000",
                "round_trip": True,
                "purpose": "Client meeting",
            }
            preview_response = await call(
                "preview_mileage_expense",
                {"request_id": str(request_id), "mileage": base_mileage},
                4,
            )
            assert preview_response.status_code == 200
            preview = json.loads(preview_response.text.split("data: ", 1)[1])["result"][
                "structuredContent"
            ]
            assert preview["mileage"]["transport_type_id"] is None
            assert preview["transport_type"]["id"] == options["default_transport_type_id"]
            assert preview["calculation"]["total_distance_km"] == "20.000"
            assert preview["calculation"]["amount"] == "5.00"
            assert preview["preview_token"].startswith("m18v1.")

            # The MCP path's production payload assembler canonicalizes equivalent
            # Decimal inputs and binds its actual principal and resolved facts.
            equivalent_preview_response = await call(
                "preview_mileage_expense",
                {
                    "request_id": str(request_id),
                    "source": {"platform": "test-client", "chat_id": "c1"},
                    "mileage": {**base_mileage, "one_way_distance_km": "10.0"},
                },
                47,
            )
            assert equivalent_preview_response.status_code == 200
            equivalent_preview = json.loads(
                equivalent_preview_response.text.split("data: ", 1)[1]
            )["result"]["structuredContent"]
            sourced_preview_response = await call(
                "preview_mileage_expense",
                {
                    "request_id": str(request_id),
                    "source": {"platform": "test-client", "chat_id": "c1"},
                    "mileage": {**base_mileage, "one_way_distance_km": "10.000"},
                },
                48,
            )
            assert sourced_preview_response.status_code == 200
            sourced_preview = json.loads(
                sourced_preview_response.text.split("data: ", 1)[1]
            )["result"]["structuredContent"]
            assert equivalent_preview["preview_token"] == sourced_preview["preview_token"]
            actual_payload = assembled_payloads[-1]
            assert actual_payload["company_id"] == company_id
            assert actual_payload["token_id"] == token.id
            assert actual_payload["request_id"] == request_id
            assert actual_payload["source"] == {
                "platform": "test-client",
                "chat_id": "c1",
                "message_id": None,
            }
            facts = actual_payload["facts"]
            assert isinstance(facts, dict)
            assert facts["rate"]["id"] == uuid.UUID(
                sourced_preview["calculation"]["rate_rule_id"]
            )
            assert facts["rate"]["rate_per_km"] == Decimal("0.250")
            assert facts["expense_projection"]["deductible"] is False
            # Exercise a non-null rate scope through the actual MCP resolver and
            # compare every typed input and resolved signing fact as one object.
            scoped_request_id = uuid.uuid4()
            scoped_source = {
                "platform": "test-client",
                "chat_id": "scoped-chat",
                "message_id": "scoped-message",
            }
            scoped_mileage = {
                "trip_date": date(2026, 11, 10).isoformat(),
                "transport_type_id": str(motorcycle_id),
                "one_way_distance_km": "10.000",
                "round_trip": True,
                "origin_address": "Origin",
                "destination_address": "Destination",
                "purpose": "Scoped-rate meeting",
                "note": "Use the confirmed route",
            }
            scoped_response = await call(
                "preview_mileage_expense",
                {
                    "request_id": str(scoped_request_id),
                    "source": scoped_source,
                    "mileage": scoped_mileage,
                },
                49,
            )
            assert scoped_response.status_code == 200
            scoped_preview = json.loads(scoped_response.text.split("data: ", 1)[1])["result"][
                "structuredContent"
            ]
            scoped_payload = assembled_payloads[-1]
            assert scoped_payload == {
                "version": 1,
                "company_id": company_id,
                "token_id": token.id,
                "request_id": scoped_request_id,
                "source": scoped_source,
                "mileage": {
                    "trip_date": date(2026, 11, 10),
                    "transport_type_id": motorcycle_id,
                    "one_way_distance_km": Decimal("10.000"),
                    "round_trip": True,
                    "origin_address": "Origin",
                    "destination_address": "Destination",
                    "purpose": "Scoped-rate meeting",
                    "note": "Use the confirmed route",
                },
                "facts": {
                    "transport_type": {"id": motorcycle_id, "name": "Motorcycle"},
                    "rate": {
                        "id": scoped_rate.id,
                        "transport_type_id": motorcycle_id,
                        "effective_from": date(2026, 6, 1),
                        "rate_per_km": Decimal("0.333"),
                    },
                    "total_distance_km": Decimal("20.000"),
                    "amount": Decimal("6.66"),
                    "currency": "EUR",
                    "category": {"id": mileage_category_id, "name": "Mileage"},
                    "vat_treatment": {
                        "id": mileage_treatment.id,
                        "code": "NL_PRIVATE_TRANSPORT_MILEAGE",
                        "label": "NL Private Transport Mileage",
                        "side": VatTreatmentSide.PURCHASE,
                        "effect": VatTreatmentEffect.EXEMPT,
                        "report_box": None,
                        "requires_icp": False,
                        "deductible": False,
                        "active": True,
                    },
                    "vat_rate": {
                        "id": zero_vat_rate.id,
                        "label": "Zero (0%)",
                        "percent": Decimal("0.000"),
                        "active": True,
                    },
                    "expense_projection": {
                        "kind": "MILEAGE",
                        "expense_date": date(2026, 11, 10),
                        "net_amount": Decimal("6.66"),
                        "vat_amount": Decimal("0"),
                        "gross_amount": Decimal("6.66"),
                        "deductible": False,
                        "currency": "EUR",
                        "exchange_rate": Decimal("1"),
                        "base_net_amount": Decimal("6.66"),
                        "base_vat_amount": Decimal("0"),
                        "base_gross_amount": Decimal("6.66"),
                    },
                },
            }
            assert scoped_preview["transport_type"] == {
                "id": str(motorcycle_id),
                "name": "Motorcycle",
            }
            assert scoped_preview["calculation"]["rate_rule_id"] == str(scoped_rate.id)
            assert scoped_preview["calculation"]["rate_per_km"] == "0.333"
            from copy import deepcopy

            from jai.services.agent_mileage import _preview_token

            alternate_company = deepcopy(actual_payload)
            alternate_company["company_id"] = foreign_company_id
            assert _preview_token(alternate_company) != sourced_preview["preview_token"]
            alternate_token = deepcopy(actual_payload)
            alternate_token["token_id"] = uuid.uuid4()
            assert _preview_token(alternate_token) != sourced_preview["preview_token"]

            async with admin_session_maker() as session:
                category = await session.get(ExpenseCategory, mileage_category.id)
                assert category is not None
                category.default_deductible = True
                await session.commit()
            unrelated_category_preview = await call(
                "preview_mileage_expense",
                {
                    "request_id": str(request_id),
                    "source": {"platform": "test-client", "chat_id": "c1"},
                    "mileage": {**base_mileage, "one_way_distance_km": "10.000"},
                },
                50,
            )
            assert unrelated_category_preview.status_code == 200
            unchanged_preview = json.loads(
                unrelated_category_preview.text.split("data: ", 1)[1]
            )["result"]["structuredContent"]
            assert unchanged_preview["preview_token"] == sourced_preview["preview_token"]
            async with admin_session_maker() as session:
                category = await session.get(ExpenseCategory, mileage_category.id)
                assert category is not None
                category.default_deductible = False
                await session.commit()

            for invalid_payload in (
                {
                    "request_id": str(uuid.uuid4()),
                    "mileage": {**base_mileage, "note": "before\u0000after"},
                },
                {
                    "request_id": str(uuid.uuid4()),
                    "source": {"platform": "test", "chat_id": "before\u0000after"},
                    "mileage": base_mileage,
                },
            ):
                nul_text = await call("preview_mileage_expense", invalid_payload, 49)
                assert "MCP_VALIDATION_FAILED" in nul_text.text

            async with admin_session_maker() as session:
                rates = list((await session.scalars(select(MileageRate))).all())
                assert rates
                rate = await session.get(MileageRate, preview["calculation"]["rate_rule_id"])
                assert rate is not None
                original_rate = rate.rate_per_km
                rate.rate_per_km = Decimal("NaN")
                await session.commit()
            invalid_rate = await call(
                "preview_mileage_expense",
                {"request_id": str(uuid.uuid4()), "mileage": base_mileage},
                51,
            )
            assert "MCP_MILEAGE_CONFIGURATION_INVALID" in invalid_rate.text
            async with admin_session_maker() as session:
                rate = await session.get(MileageRate, preview["calculation"]["rate_rule_id"])
                assert rate is not None
                rate.rate_per_km = original_rate
                await session.commit()

            async with admin_session_maker() as session:
                transport_types = list(
                    (await session.scalars(select(MileageTransportType))).all()
                )
                for transport_type in transport_types:
                    transport_type.active = False
                await session.commit()
            no_enabled_options = await call("get_mileage_options", {}, 52)
            assert "MCP_MILEAGE_CONFIGURATION_INVALID" in no_enabled_options.text
            async with admin_session_maker() as session:
                transport_types = list(
                    (await session.scalars(select(MileageTransportType))).all()
                )
                for transport_type in transport_types:
                    transport_type.active = True
                await session.commit()

            motorcycle_preview = await call(
                "preview_mileage_expense",
                {
                    "request_id": str(uuid.uuid4()),
                    "mileage": {
                        **base_mileage,
                        "round_trip": False,
                        "transport_type_id": str(motorcycle_id),
                    },
                },
                40,
            )
            assert motorcycle_preview.status_code == 200
            motorcycle = json.loads(motorcycle_preview.text.split("data: ", 1)[1])["result"][
                "structuredContent"
            ]
            assert motorcycle["transport_type"]["id"] == str(motorcycle_id)
            assert motorcycle["calculation"]["total_distance_km"] == "10.000"

            async with admin_session_maker() as session:
                motorcycle_row = await session.get(MileageTransportType, motorcycle_id)
                assert motorcycle_row is not None
                motorcycle_row.active = False
                await session.commit()
            disabled_type = await call(
                "preview_mileage_expense",
                {
                    "request_id": str(uuid.uuid4()),
                    "mileage": {
                        **base_mileage,
                        "round_trip": False,
                        "transport_type_id": str(motorcycle_id),
                    },
                },
                401,
            )
            assert "MCP_MILEAGE_CONFIGURATION_INVALID" in disabled_type.text
            async with admin_session_maker() as session:
                motorcycle_row = await session.get(MileageTransportType, motorcycle_id)
                assert motorcycle_row is not None
                motorcycle_row.active = True
                await session.commit()

            no_rate = await call(
                "preview_mileage_expense",
                {
                    "request_id": str(uuid.uuid4()),
                    "mileage": {**base_mileage, "trip_date": "2020-01-01"},
                },
                41,
            )
            assert "MCP_MILEAGE_CONFIGURATION_INVALID" in no_rate.text

            overflow = await call(
                "preview_mileage_expense",
                {
                    "request_id": str(uuid.uuid4()),
                    "mileage": {
                        **base_mileage,
                        "one_way_distance_km": "999999999999999.999",
                    },
                },
                42,
            )
            assert "MCP_VALIDATION_FAILED" in overflow.text

            async with admin_session_maker() as session:
                category = await session.get(ExpenseCategory, mileage_category.id)
                assert category is not None
                category.active = False
                await session.commit()
            invalid_category = await call(
                "preview_mileage_expense",
                {"request_id": str(uuid.uuid4()), "mileage": base_mileage},
                43,
            )
            assert "MCP_MILEAGE_CONFIGURATION_INVALID" in invalid_category.text
            async with admin_session_maker() as session:
                category = await session.get(ExpenseCategory, mileage_category.id)
                assert category is not None
                category.active = True
                treatment = await session.get(VatTreatment, mileage_treatment.id)
                assert treatment is not None
                treatment.active = False
                await session.commit()
            invalid_vat = await call(
                "preview_mileage_expense",
                {"request_id": str(uuid.uuid4()), "mileage": base_mileage},
                44,
            )
            assert "MCP_MILEAGE_CONFIGURATION_INVALID" in invalid_vat.text
            async with admin_session_maker() as session:
                treatment = await session.get(VatTreatment, mileage_treatment.id)
                assert treatment is not None
                treatment.active = True
                setting = await session.get(Setting, defaults_setting.id)
                assert setting is not None
                original_defaults = dict(setting.value)
                setting.value = {**original_defaults, "expense_category_id": "invalid-uuid"}
                await session.commit()
            invalid_defaults = await call(
                "preview_mileage_expense",
                {"request_id": str(uuid.uuid4()), "mileage": base_mileage},
                45,
            )
            assert "MCP_MILEAGE_CONFIGURATION_INVALID" in invalid_defaults.text
            invalid_options = await call("get_mileage_options", {}, 46)
            assert "MCP_MILEAGE_CONFIGURATION_INVALID" in invalid_options.text
            async with admin_session_maker() as session:
                setting = await session.get(Setting, defaults_setting.id)
                assert setting is not None
                setting.value = original_defaults
                await session.commit()

            bad_extra = await call(
                "preview_mileage_expense",
                {
                    "request_id": str(uuid.uuid4()),
                    "mileage": {**base_mileage, "amount": "0.01"},
                },
                5,
            )
            assert "MCP_VALIDATION_FAILED" in bad_extra.text

            foreign_types = await call(
                "preview_mileage_expense",
                {
                    "request_id": str(uuid.uuid4()),
                    "mileage": {
                        **base_mileage,
                        "round_trip": False,
                        "transport_type_id": str(foreign_transport_id),
                    },
                },
                6,
            )
            # A random foreign ID is intentionally indistinguishable from missing.
            assert "MCP_TARGET_NOT_FOUND" in foreign_types.text

            missing_types = await call(
                "preview_mileage_expense",
                {
                    "request_id": str(uuid.uuid4()),
                    "mileage": {
                        **base_mileage,
                        "round_trip": False,
                        "transport_type_id": str(uuid.uuid4()),
                    },
                },
                51,
            )
            assert "MCP_TARGET_NOT_FOUND" in missing_types.text

            headers["authorization"] = f"Bearer {old_secret}"
            denied = await call("get_mileage_options", {}, 7)
            assert "MCP_INSUFFICIENT_SCOPE" in denied.text
            headers["authorization"] = f"Bearer {secret}"
            async with admin_session_maker() as session:
                stored_token = await session.get(AgentToken, token.id)
                assert stored_token is not None
                stored_token.revoked_at = datetime.now(UTC)
                await session.commit()
            revoked = await call("get_mileage_options", {}, 8)
            assert "MCP_UNAUTHENTICATED" in revoked.text

    async with admin_session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(MileageTrip)) == 0
        assert await session.scalar(select(func.count()).select_from(Expense)) == 0
        assert await session.scalar(select(func.count()).select_from(AgentOperation)) == 0

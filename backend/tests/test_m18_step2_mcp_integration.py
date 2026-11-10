"""Actual Streamable HTTP coverage for the M18 mileage intake workflow."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import uvicorn
from httpx import AsyncClient, Response
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.types import ASGIApp

from jai.api import mcp as mcp_module
from jai.config import Settings
from jai.models._enums import ExpenseKind, SettingLevel, VatTreatmentEffect, VatTreatmentSide
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
from jai.schemas.agent_tools import AgentMileageWrite
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


@asynccontextmanager
async def _tcp_mcp_client(transport_app: ASGIApp) -> AsyncIterator[AsyncClient]:
    """Serve the real MCP ASGI boundary on an owned ephemeral loopback port."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            transport_app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            lifespan="off",
            timeout_graceful_shutdown=2,
        )
    )
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if server_task.done():
                    await server_task
                    raise RuntimeError("The loopback MCP server stopped before startup.")
                await asyncio.sleep(0.01)
        async with AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False
        ) as client:
            yield client
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(server_task), timeout=5)
        except TimeoutError:
            server.force_exit = True
            await server_task
        finally:
            listener.close()


@pytest.mark.integration
async def test_mileage_options_preview_and_create_work_over_mcp(
    admin_session_maker: async_sessionmaker[AsyncSession],
    runtime_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    company_id, foreign_company_id, owner_id, foreign_owner_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
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
    second_token, second_secret = issue_token(
        AgentTokenCreate(name="M18 second token", scopes=[AgentTokenScope.MILEAGE_CREATE]),
        company_id,
    )
    second_token.creator_id = owner_id
    foreign_token, foreign_secret = issue_token(
        AgentTokenCreate(name="M18 foreign token", scopes=[AgentTokenScope.MILEAGE_CREATE]),
        foreign_company_id,
    )
    foreign_token.creator_id = foreign_owner_id
    async with admin_session_maker() as session:
        session.add_all(
            [
                Company(id=company_id, name="M18 Company", base_currency="EUR"),
                Company(id=foreign_company_id, name="M18 Foreign", base_currency="EUR"),
                User(
                    id=foreign_owner_id,
                    email=f"{foreign_owner_id}@example.test",
                    hashed_password="unused-test-hash",
                    company_id=foreign_company_id,
                    role="owner",
                    mfa_enabled=True,
                    totp_secret="test-secret",
                ),
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
        session.add_all([token, old_token, second_token, foreign_token])
        await session.commit()

    monkeypatch.setattr(mcp_module, "get_session_maker", lambda: runtime_session_maker)
    settings = Settings(base_url="https://m18.example.test", scheduler_enabled=False)
    monkeypatch.setattr(mcp_module, "get_settings", lambda: settings)
    from jai.services import agent_mileage

    monkeypatch.setattr(agent_mileage, "get_settings", lambda: settings)

    transport_app = _fresh_mcp_wire_app()
    async with mcp_module.server.session_manager.run():
        async with _tcp_mcp_client(transport_app) as client:
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
            caplog.set_level(logging.DEBUG, logger="sse_starlette.sse")
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
                "create_mileage_expense",
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

            # These route descriptions and user decisions are controlled test
            # evidence, not live provider queries. The MCP contract receives
            # only the final user-confirmed mileage fields.
            controlled_route_scenarios = [
                {
                    "state": "suggestion",
                    "candidates": [
                        {
                            "origin": "Studio",
                            "destination": "Client A",
                            "mode": "car",
                            "suggested_km": "12.400",
                            "source": "controlled-route-fixture:valid-1",
                        }
                    ],
                    "decision": "user_corrected_distance",
                    "confirmed_km": "11.800",
                },
                {
                    "state": "ambiguous",
                    "candidates": [
                        {
                            "origin": "Studio",
                            "destination": "Client B - North",
                            "mode": "car",
                            "suggested_km": "8.000",
                            "source": "controlled-route-fixture:ambiguous-1",
                        },
                        {
                            "origin": "Studio - West",
                            "destination": "Client B - South",
                            "mode": "car",
                            "suggested_km": "10.500",
                            "source": "controlled-route-fixture:ambiguous-2",
                        },
                    ],
                    "decision": "candidate_2_clarified_and_corrected",
                    "selected_candidate": 2,
                    "confirmed_km": "7.200",
                },
                {
                    "state": "failed",
                    "candidates": [],
                    "failure": "controlled no-route result",
                    "decision": "user_supplied_manual_distance",
                    "manual_endpoints": {
                        "origin": "Home - manual fallback",
                        "destination": "Client C - manual fallback",
                    },
                    "confirmed_km": "4.500",
                },
            ]
            for offset, scenario in enumerate(controlled_route_scenarios, start=1):
                if scenario["state"] == "suggestion":
                    candidate = scenario["candidates"][0]
                    assert {"origin", "destination", "mode", "suggested_km", "source"} <= set(
                        candidate
                    )
                    assert scenario["decision"] == "user_corrected_distance"
                    assert scenario["confirmed_km"] != candidate["suggested_km"]
                elif scenario["state"] == "ambiguous":
                    candidates = scenario["candidates"]
                    assert len(candidates) == 2
                    assert all(
                        {"origin", "destination", "mode", "suggested_km", "source"}
                        <= set(candidate)
                        for candidate in candidates
                    )
                    assert scenario["selected_candidate"] == 2
                    assert scenario["decision"] == "candidate_2_clarified_and_corrected"
                else:
                    assert scenario["state"] == "failed"
                    assert scenario["candidates"] == []
                    assert scenario["failure"]
                    assert scenario["decision"] == "user_supplied_manual_distance"

                confirmed_km = scenario["confirmed_km"]
                if scenario["state"] == "ambiguous":
                    selected_candidate = scenario["candidates"][
                        scenario["selected_candidate"] - 1
                    ]
                    origin = selected_candidate["origin"]
                    destination = selected_candidate["destination"]
                elif scenario["state"] == "failed":
                    origin = scenario["manual_endpoints"]["origin"]
                    destination = scenario["manual_endpoints"]["destination"]
                else:
                    candidate = scenario["candidates"][0]
                    origin = candidate["origin"]
                    destination = candidate["destination"]
                confirmed_mileage = {
                    "trip_date": date(2026, 11, 10).isoformat(),
                    "one_way_distance_km": confirmed_km,
                    "round_trip": False,
                    "origin_address": origin,
                    "destination_address": destination,
                }
                # Validate against the real strict input schema instead of a
                # duplicated field allowlist that can drift from the contract.
                confirmed_mileage = AgentMileageWrite.model_validate(
                    confirmed_mileage
                ).model_dump(mode="json", exclude_unset=True)
                route_preview = await call(
                    "preview_mileage_expense",
                    {
                        "request_id": str(uuid.uuid4()),
                        "mileage": confirmed_mileage,
                    },
                    200 + offset,
                )
                assert route_preview.status_code == 200
                route_result = json.loads(route_preview.text.split("data: ", 1)[1])["result"][
                    "structuredContent"
                ]
                assert route_result["mileage"]["one_way_distance_km"] == confirmed_km
                assert route_result["mileage"]["origin_address"] == origin
                assert route_result["mileage"]["destination_address"] == destination
                assert route_result["calculation"]["total_distance_km"] == confirmed_km

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
            issued_preview_token = preview["preview_token"]
            sse_records = [
                record for record in caplog.records if record.name == "sse_starlette.sse"
            ]
            assert sse_records
            assert not any(issued_preview_token in record.getMessage() for record in sse_records)
            assert any(record.msg != "chunk: %s" for record in sse_records)

            copied_note = await call(
                "preview_mileage_expense",
                {
                    "request_id": str(uuid.uuid4()),
                    "mileage": {
                        **base_mileage,
                        "note": f"prefix{issued_preview_token}suffix",
                    },
                },
                55,
            )
            assert copied_note.status_code == 200
            assert "MCP_VALIDATION_FAILED" in copied_note.text
            assert issued_preview_token not in copied_note.text
            copied_source = await call(
                "preview_mileage_expense",
                {
                    "request_id": str(uuid.uuid4()),
                    "mileage": base_mileage,
                    "source": {
                        "platform": "test-client",
                        "chat_id": f"prefix{issued_preview_token}suffix",
                    },
                },
                56,
            )
            assert copied_source.status_code == 200
            assert "MCP_VALIDATION_FAILED" in copied_source.text
            assert issued_preview_token not in copied_source.text

            resolved_transport_id = uuid.UUID(preview["transport_type"]["id"])
            async with admin_session_maker() as session:
                resolved_transport = await session.get(
                    MileageTransportType, resolved_transport_id
                )
                assert resolved_transport is not None
                original_transport_name = resolved_transport.name
                resolved_transport.name = f"prefix{issued_preview_token}suffix"
                await session.commit()
            signed_payload_count = len(assembled_payloads)
            contaminated_transport = await call(
                "preview_mileage_expense",
                {
                    "request_id": str(uuid.uuid4()),
                    "mileage": base_mileage,
                },
                176,
            )
            assert "MCP_VALIDATION_FAILED" in contaminated_transport.text
            assert issued_preview_token not in contaminated_transport.text
            assert len(assembled_payloads) == signed_payload_count
            async with admin_session_maker() as session:
                resolved_transport = await session.get(
                    MileageTransportType, resolved_transport_id
                )
                assert resolved_transport is not None
                resolved_transport.name = original_transport_name
                await session.commit()

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

            # Confirm creation re-resolves defaults, applicable rate and
            # category snapshots rather than trusting preview-era values.
            default_stale_id = uuid.uuid4()
            default_preview_response = await call(
                "preview_mileage_expense",
                {"request_id": str(default_stale_id), "mileage": base_mileage},
                183,
            )
            default_preview = json.loads(default_preview_response.text.split("data: ", 1)[1])[
                "result"
            ]["structuredContent"]
            async with admin_session_maker() as session:
                setting = await session.get(Setting, defaults_setting.id)
                assert setting is not None
                setting.value = {
                    **original_defaults,
                    "default_transport_type_id": str(motorcycle_id),
                }
                await session.commit()
            default_stale = await call(
                "create_mileage_expense",
                {
                    "request_id": str(default_stale_id),
                    "mileage": base_mileage,
                    "preview_token": default_preview["preview_token"],
                },
                184,
            )
            assert "MCP_MILEAGE_PREVIEW_STALE" in default_stale.text
            async with admin_session_maker() as session:
                setting = await session.get(Setting, defaults_setting.id)
                assert setting is not None
                setting.value = original_defaults
                await session.commit()

            rate_body = {**base_mileage, "transport_type_id": str(motorcycle_id)}
            rate_stale_id = uuid.uuid4()
            rate_preview_response = await call(
                "preview_mileage_expense",
                {"request_id": str(rate_stale_id), "mileage": rate_body},
                185,
            )
            rate_preview = json.loads(rate_preview_response.text.split("data: ", 1)[1])[
                "result"
            ]["structuredContent"]
            async with admin_session_maker() as session:
                rate = await session.get(MileageRate, scoped_rate.id)
                assert rate is not None
                rate.rate_per_km = Decimal("0.444")
                await session.commit()
            rate_stale = await call(
                "create_mileage_expense",
                {
                    "request_id": str(rate_stale_id),
                    "mileage": rate_body,
                    "preview_token": rate_preview["preview_token"],
                },
                186,
            )
            assert "MCP_MILEAGE_PREVIEW_STALE" in rate_stale.text
            async with admin_session_maker() as session:
                rate = await session.get(MileageRate, scoped_rate.id)
                assert rate is not None
                rate.rate_per_km = Decimal("0.333")
                await session.commit()

            category_stale_id = uuid.uuid4()
            category_preview_response = await call(
                "preview_mileage_expense",
                {"request_id": str(category_stale_id), "mileage": base_mileage},
                187,
            )
            category_preview = json.loads(
                category_preview_response.text.split("data: ", 1)[1]
            )["result"]["structuredContent"]
            async with admin_session_maker() as session:
                category = await session.get(ExpenseCategory, mileage_category_id)
                assert category is not None
                category.name = "Category changed after preview"
                await session.commit()
            category_stale = await call(
                "create_mileage_expense",
                {
                    "request_id": str(category_stale_id),
                    "mileage": base_mileage,
                    "preview_token": category_preview["preview_token"],
                },
                188,
            )
            assert "MCP_MILEAGE_PREVIEW_STALE" in category_stale.text
            async with admin_session_maker() as session:
                category = await session.get(ExpenseCategory, mileage_category_id)
                assert category is not None
                category.name = "Mileage"
                await session.commit()

            overflow_id = uuid.uuid4()
            overflow_preview_body = {
                **base_mileage,
                "one_way_distance_km": "500000000000000.001",
                "round_trip": False,
            }
            overflow_preview_response = await call(
                "preview_mileage_expense",
                {"request_id": str(overflow_id), "mileage": overflow_preview_body},
                189,
            )
            overflow_preview = json.loads(
                overflow_preview_response.text.split("data: ", 1)[1]
            )["result"]["structuredContent"]
            overflow_create = await call(
                "create_mileage_expense",
                {
                    "request_id": str(overflow_id),
                    "mileage": {**overflow_preview_body, "round_trip": True},
                    "preview_token": overflow_preview["preview_token"],
                },
                190,
            )
            assert "MCP_VALIDATION_FAILED" in overflow_create.text
            async with admin_session_maker() as session:
                overflow_operation = await session.scalar(
                    select(AgentOperation).where(
                        AgentOperation.request_id == str(overflow_id)
                    )
                )
                assert overflow_operation is not None
                assert overflow_operation.outcome == "failed"
                assert overflow_operation.error_code == "MCP_VALIDATION_FAILED"
                assert await session.scalar(select(func.count()).select_from(MileageTrip)) == 0
                assert await session.scalar(select(func.count()).select_from(Expense)) == 0

            # Confirmed creation is a real MCP write. A stale payload is saved
            # as an expected failure; corrected requests need a fresh ID.
            create_id = uuid.uuid4()
            create_source = {"platform": "test", "chat_id": "m18", "message_id": "1"}
            create_mileage = {
                **base_mileage,
                "round_trip": False,
                "purpose": "Verified client visit",
            }
            credential_note = "Bearer example-agent-secret-987"
            unsafe_preview = await call(
                "preview_mileage_expense",
                {
                    "request_id": str(uuid.uuid4()),
                    "mileage": {**create_mileage, "note": credential_note},
                },
                61,
            )
            assert "MCP_VALIDATION_FAILED" in unsafe_preview.text
            assert credential_note not in unsafe_preview.text
            create_preview_response = await call(
                "preview_mileage_expense",
                {
                    "request_id": str(create_id),
                    "source": create_source,
                    "mileage": create_mileage,
                },
                52,
            )
            assert create_preview_response.status_code == 200
            create_preview = json.loads(create_preview_response.text.split("data: ", 1)[1])[
                "result"
            ]["structuredContent"]
            create_args = {
                "request_id": str(create_id),
                "source": create_source,
                "mileage": create_mileage,
                "preview_token": create_preview["preview_token"],
            }
            source_screen_id = uuid.uuid4()
            source_screen_preview_response = await call(
                "preview_mileage_expense",
                {
                    "request_id": str(source_screen_id),
                    "source": create_source,
                    "mileage": create_mileage,
                },
                177,
            )
            source_screen_preview = json.loads(
                source_screen_preview_response.text.split("data: ", 1)[1]
            )["result"]["structuredContent"]
            unsafe_source_create = await call(
                "create_mileage_expense",
                {
                    "request_id": str(source_screen_id),
                    "source": {"platform": "test", "chat_id": f"Bearer {secret}"},
                    "mileage": create_mileage,
                    "preview_token": source_screen_preview["preview_token"],
                },
                178,
            )
            assert "MCP_VALIDATION_FAILED" in unsafe_source_create.text
            assert secret not in unsafe_source_create.text
            async with admin_session_maker() as session:
                assert await session.scalar(
                    select(AgentOperation.id).where(
                        AgentOperation.request_id == str(source_screen_id)
                    )
                ) is None
            stale_args = {
                **create_args,
                "mileage": {**create_mileage, "purpose": "Changed"},
            }
            tampered = await call("create_mileage_expense", stale_args, 53)
            assert "MCP_MILEAGE_PREVIEW_STALE" in tampered.text
            assert "preview_token" not in tampered.text
            stale_replay = await call("create_mileage_expense", stale_args, 169)
            assert "MCP_MILEAGE_PREVIEW_STALE" in stale_replay.text
            stale_conflict = await call(
                "create_mileage_expense",
                {
                    **create_args,
                    "mileage": {**create_mileage, "purpose": "Different again"},
                },
                170,
            )
            assert "MCP_REQUEST_ID_CONFLICT" in stale_conflict.text

            # A safely recorded stale failure consumes its request ID. Start a
            # corrected attempt with a new ID and fresh preview.
            create_id = uuid.uuid4()
            create_preview_response = await call(
                "preview_mileage_expense",
                {
                    "request_id": str(create_id),
                    "source": create_source,
                    "mileage": create_mileage,
                },
                58,
            )
            create_preview = json.loads(create_preview_response.text.split("data: ", 1)[1])[
                "result"
            ]["structuredContent"]
            create_args = {
                "request_id": str(create_id),
                "source": create_source,
                "mileage": create_mileage,
                "preview_token": create_preview["preview_token"],
            }
            copied_create_source = await call(
                "create_mileage_expense",
                {
                    **create_args,
                    "source": {"platform": "test-client", "chat_id": issued_preview_token},
                },
                57,
            )
            assert "MCP_VALIDATION_FAILED" in copied_create_source.text
            assert issued_preview_token not in copied_create_source.text
            headers["authorization"] = f"Bearer {second_secret}"
            cross_token = await call("create_mileage_expense", create_args, 174)
            assert "MCP_MILEAGE_PREVIEW_STALE" in cross_token.text
            headers["authorization"] = f"Bearer {foreign_secret}"
            cross_company = await call("create_mileage_expense", create_args, 175)
            assert "MCP_MILEAGE_PREVIEW_STALE" in cross_company.text
            headers["authorization"] = f"Bearer {secret}"

            concurrent_responses = await asyncio.gather(
                call("create_mileage_expense", create_args, 54),
                call("create_mileage_expense", create_args, 55),
            )
            assert all(response.status_code == 200 for response in concurrent_responses)
            concurrent_results = [
                json.loads(response.text.split("data: ", 1)[1])["result"]
                for response in concurrent_responses
            ]
            assert all(not result.get("isError") for result in concurrent_results)
            concurrent_payloads = [result["structuredContent"] for result in concurrent_results]
            statuses = {item["operation"]["status"] for item in concurrent_payloads}
            assert statuses == {"created", "replayed"}
            created = next(
                item for item in concurrent_payloads if item["operation"]["status"] == "created"
            )
            replayed = next(
                item
                for item in concurrent_payloads
                if item["operation"]["status"] == "replayed"
            )
            trip_id = created["mileage"]["id"]
            expense_id = created["mileage"]["expense_id"]
            assert created["operation"]["status"] == "created"
            assert created["operation"]["target_id"] == trip_id
            assert created["operation"]["web_url"] == (
                f"https://m18.example.test/expenses/mileage/{trip_id}/edit"
            )
            assert Decimal(created["mileage"]["amount"]) == Decimal(
                create_preview["calculation"]["amount"]
            )
            async with admin_session_maker() as session:
                trip = await session.get(MileageTrip, uuid.UUID(trip_id))
                expense = await session.get(Expense, uuid.UUID(expense_id))
                assert trip is not None and expense is not None
                assert trip.creator_id == owner_id and expense.creator_id == owner_id
                assert trip.expense_id == expense.id
                assert trip.purpose == "Verified client visit"
                assert trip.calculated_amount == expense.net_amount
                assert expense.kind == ExpenseKind.MILEAGE
                assert expense.category_id == mileage_category_id
                assert Decimal(str(expense.vat_amount)) == Decimal("0")

            assert replayed["mileage"] == created["mileage"]
            monkeypatch.setattr(agent_mileage, "get_auth_secret", lambda: "rotated-secret")
            async with admin_session_maker() as session:
                changed_trip = await session.get(MileageTrip, uuid.UUID(trip_id))
                assert changed_trip is not None
                changed_trip.purpose = "Edited after creation"
                setting = await session.get(Setting, defaults_setting.id)
                assert setting is not None
                setting.value = {
                    "default_transport_type_id": str(motorcycle_id),
                    "expense_category_id": str(mileage_category_id),
                }
                category = await session.get(ExpenseCategory, mileage_category_id)
                assert category is not None
                category.name = "Renamed after creation"
                await session.commit()
            edited_replay_response = await call("create_mileage_expense", create_args, 171)
            edited_replay = json.loads(
                edited_replay_response.text.split("data: ", 1)[1]
            )["result"]["structuredContent"]
            assert edited_replay["operation"]["status"] == "replayed"
            assert edited_replay["mileage"] == created["mileage"]
            async with admin_session_maker() as session:
                await session.execute(delete(Expense).where(Expense.id == uuid.UUID(expense_id)))
                await session.commit()
            after_target_deletion = await call("create_mileage_expense", create_args, 57)
            deleted_replay = json.loads(after_target_deletion.text.split("data: ", 1)[1])[
                "result"
            ]["structuredContent"]
            assert deleted_replay["operation"]["status"] == "replayed"
            assert deleted_replay["mileage"] == created["mileage"]
            conflict = await call(
                "create_mileage_expense",
                {**create_args, "preview_token": "m18v1." + "0" * 64},
                56,
            )
            assert "MCP_REQUEST_ID_CONFLICT" in conflict.text
            async with admin_session_maker() as session:
                trip = await session.get(MileageTrip, uuid.UUID(trip_id))
                expense = await session.get(Expense, uuid.UUID(expense_id))
                operation = await session.get(
                    AgentOperation, uuid.UUID(created["operation"]["operation_id"])
                )
                assert trip is None and expense is None and operation is not None
                assert operation.target_type == "mileage"
                assert operation.target_id == uuid.UUID(trip_id)
                assert "preview_token" not in json.dumps(operation.result_snapshot)
                assert operation.result_snapshot["web_url"] == created["operation"]["web_url"]

            from jai.services import mileage as mileage_service

            # Fail once after the Expense insert and once after both business
            # rows have flushed. The operation trace is retained; business
            # rows from the savepoint must roll back in both cases.
            for call_id, patch_target in (
                (62, "_apply_trip_write"),
                (63, "_trip_to_read"),
            ):
                rollback_id = uuid.uuid4()
                rollback_preview_response = await call(
                    "preview_mileage_expense",
                    {
                        "request_id": str(rollback_id),
                        "mileage": create_mileage,
                    },
                    call_id + 100,
                )
                rollback_preview = json.loads(
                    rollback_preview_response.text.split("data: ", 1)[1]
                )["result"]["structuredContent"]
                rollback_args = {
                    "request_id": str(rollback_id),
                    "mileage": create_mileage,
                    "preview_token": rollback_preview["preview_token"],
                }

                def injected_write_failure(*args: object, **kwargs: object) -> None:
                    raise ValueError("injected write boundary failure")

                with monkeypatch.context() as patcher:
                    patcher.setattr(mileage_service, patch_target, injected_write_failure)
                    failed_write = await call(
                        "create_mileage_expense", rollback_args, call_id
                    )
                assert "MCP_MILEAGE_CONFIGURATION_INVALID" in failed_write.text
                async with admin_session_maker() as session:
                    assert await session.scalar(select(func.count()).select_from(MileageTrip)) == 0
                    assert await session.scalar(select(func.count()).select_from(Expense)) == 0

            from jai.services import agent_operations

            snapshot_failure_id = uuid.uuid4()
            snapshot_failure_preview_response = await call(
                "preview_mileage_expense",
                {
                    "request_id": str(snapshot_failure_id),
                    "mileage": create_mileage,
                },
                172,
            )
            snapshot_failure_preview = json.loads(
                snapshot_failure_preview_response.text.split("data: ", 1)[1]
            )["result"]["structuredContent"]
            snapshot_failure_args = {
                "request_id": str(snapshot_failure_id),
                "mileage": create_mileage,
                "preview_token": snapshot_failure_preview["preview_token"],
            }

            def reject_final_snapshot(*args: object, **kwargs: object) -> dict[str, object]:
                raise ValueError("injected final snapshot failure")

            with monkeypatch.context() as patcher:
                patcher.setattr(agent_operations, "_typed_snapshot", reject_final_snapshot)
                snapshot_failure = await call(
                    "create_mileage_expense", snapshot_failure_args, 173
                )
            assert "MCP_VALIDATION_FAILED" in snapshot_failure.text
            async with admin_session_maker() as session:
                assert await session.scalar(select(func.count()).select_from(MileageTrip)) == 0
                assert await session.scalar(select(func.count()).select_from(Expense)) == 0
                assert await session.scalar(
                    select(func.count()).select_from(AgentOperation).where(
                        AgentOperation.request_id == str(snapshot_failure_id)
                    )
                ) == 0

            # Simulate a transport loss after durable commit. The client gets
            # no success payload; the unchanged request recovers the snapshot.
            lost_id = uuid.uuid4()
            lost_preview_response = await call(
                "preview_mileage_expense",
                {"request_id": str(lost_id), "mileage": create_mileage},
                164,
            )
            lost_preview = json.loads(lost_preview_response.text.split("data: ", 1)[1])[
                "result"
            ]["structuredContent"]
            lost_args = {
                "request_id": str(lost_id),
                "mileage": create_mileage,
                "preview_token": lost_preview["preview_token"],
            }
            original_create_service = mcp_module.create_mileage_expense_service

            async def commit_then_lose_response(*args: object, **kwargs: object) -> object:
                await original_create_service(*args, **kwargs)
                raise RuntimeError("simulated lost response")

            with monkeypatch.context() as patcher:
                patcher.setattr(
                    mcp_module, "create_mileage_expense_service", commit_then_lose_response
                )
                lost_response = await call("create_mileage_expense", lost_args, 165)
            assert "MCP_VALIDATION_FAILED" in lost_response.text
            recovered = await call("create_mileage_expense", lost_args, 166)
            recovered_payload = json.loads(recovered.text.split("data: ", 1)[1])["result"][
                "structuredContent"
            ]
            assert recovered_payload["operation"]["status"] == "replayed"
            async with admin_session_maker() as session:
                assert await session.scalar(select(func.count()).select_from(MileageTrip)) == 1
                assert await session.scalar(select(func.count()).select_from(Expense)) == 1

            bad_extra = await call(
                "preview_mileage_expense",
                {
                    "request_id": str(uuid.uuid4()),
                    "mileage": {**base_mileage, "amount": "0.01"},
                },
                5,
            )
            assert "MCP_VALIDATION_FAILED" in bad_extra.text

            malformed_token = await call(
                "create_mileage_expense",
                {
                    "request_id": str(uuid.uuid4()),
                    "mileage": base_mileage,
                    "preview_token": "m18v1.short",
                },
                60,
            )
            assert "MCP_VALIDATION_FAILED" in malformed_token.text
            assert "m18v1.short" not in malformed_token.text

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
            denied_create = await call("create_mileage_expense", create_args, 176)
            assert "MCP_INSUFFICIENT_SCOPE" in denied_create.text
            headers["authorization"] = f"Bearer {secret}"
            race_id = uuid.uuid4()
            race_preview_response = await call(
                "preview_mileage_expense",
                {"request_id": str(race_id), "mileage": create_mileage},
                167,
            )
            race_preview = json.loads(race_preview_response.text.split("data: ", 1)[1])[
                "result"
            ]["structuredContent"]
            race_args = {
                "request_id": str(race_id),
                "mileage": create_mileage,
                "preview_token": race_preview["preview_token"],
            }
            from jai.services import agent_mileage

            original_run_mutation = agent_mileage.run_agent_mutation

            async def revoke_between_auth_and_mutation(*args: object, **kwargs: object) -> object:
                async with admin_session_maker() as session:
                    stored_token = await session.get(AgentToken, token.id)
                    assert stored_token is not None
                    stored_token.revoked_at = datetime.now(UTC)
                    await session.commit()
                return await original_run_mutation(*args, **kwargs)

            monkeypatch.setattr(
                agent_mileage, "run_agent_mutation", revoke_between_auth_and_mutation
            )
            revocation_race = await call("create_mileage_expense", race_args, 168)
            assert "MCP_UNAUTHENTICATED" in revocation_race.text
            revoked = await call("get_mileage_options", {}, 8)
            assert "MCP_UNAUTHENTICATED" in revoked.text
            revoked_replay = await call("create_mileage_expense", create_args, 59)
            assert "MCP_UNAUTHENTICATED" in revoked_replay.text

    async with admin_session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(MileageTrip)) == 1
        assert await session.scalar(select(func.count()).select_from(Expense)) == 1
        assert await session.scalar(select(func.count()).select_from(AgentOperation)) == 11
        stored_operations = list((await session.scalars(select(AgentOperation))).all())
        for operation in stored_operations:
            assert issued_preview_token not in json.dumps(
                {
                    "source_chat_id": operation.source_chat_id,
                    "source_message_id": operation.source_message_id,
                    "snapshot": operation.result_snapshot,
                }
            )

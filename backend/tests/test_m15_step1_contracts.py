"""M15 Step 1 contract and closed MCP transport checks."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

import jai.config as cfg
import jai.main as main
from jai.api import mcp as mcp_api
from jai.api.mcp import _host_from_authority
from jai.main import create_app
from jai.schemas.agent_token import AgentTokenCreate, AgentTokenScope
from jai.schemas.agent_tools import (
    AgentErrorCode,
    AgentWriteResult,
    CatalogListOutput,
    CreateCustomerOutput,
    CreateInvoiceDraftInput,
    DocumentTemplateListOutput,
    InvoiceWriteOutput,
    QuoteWriteOutput,
    SubmitReceiptInput,
    SubmitReceiptOutput,
    VatOptionsOutput,
)
from jai.schemas.content import DocumentTemplateRead
from jai.schemas.invoice import ProductInvoiceOptionRead
from jai.schemas.vat import VatRateRead, VatTreatmentRead


def test_agent_token_contract_requires_known_nonempty_scopes() -> None:
    token = AgentTokenCreate(name="  Hermes  ", scopes=[AgentTokenScope.RECEIPT_INTAKE])
    assert token.name == "Hermes"
    assert token.scopes == [AgentTokenScope.RECEIPT_INTAKE]

    try:
        AgentTokenCreate(name="Hermes", scopes=[])
    except ValidationError:
        pass
    else:
        raise AssertionError("An empty token scope set must be invalid.")


def test_mcp_input_contracts_are_bounded_and_errors_are_stable() -> None:
    request = SubmitReceiptInput(
        request_id=uuid.uuid4(),
        source={"platform": "telegram", "chat_id": "1", "message_id": "2"},
        files=[{"filename": "bill.pdf", "mime_type": "application/pdf", "data_base64": "YQ=="}],
    )
    assert len(request.files) == 1
    assert AgentErrorCode.REQUEST_ID_CONFLICT.value == "MCP_REQUEST_ID_CONFLICT"

    try:
        SubmitReceiptInput(
            request_id=uuid.uuid4(),
            source={"platform": "telegram"},
            files=[
                {"filename": f"{index}.pdf", "mime_type": "application/pdf", "data_base64": "YQ=="}
                for index in range(6)
            ],
        )
    except ValidationError:
        pass
    else:
        raise AssertionError("More than five receipt files must be invalid.")

    invoice = CreateInvoiceDraftInput.model_validate(
        {
            "request_id": str(uuid.uuid4()),
            "invoice": {
                "customer_id": str(uuid.uuid4()),
                "invoice_date": "2026-09-30",
                "tax_mode": "LINE",
                "lines": [
                    {"name": "Repair", "quantity": "1", "unit_price": "10.00"}
                ],
            },
        }
    )
    assert invoice.invoice.invoice_date.isoformat() == "2026-09-30"


def test_read_tool_outputs_preserve_authoritative_catalog_template_and_vat_fields() -> None:
    now = datetime(2026, 9, 30, tzinfo=UTC)
    product = ProductInvoiceOptionRead(
        id=uuid.uuid4(),
        name="Consulting",
        unit_id=uuid.uuid4(),
        unit_name="hour",
        default_vat_rate_id=uuid.uuid4(),
    )
    catalog = CatalogListOutput(items=[product])
    assert catalog.model_dump(mode="json") == {
        "items": [product.model_dump(mode="json")]
    }
    assert set(ProductInvoiceOptionRead.model_fields) == {
        "id", "name", "unit_id", "unit_name", "default_vat_rate_id"
    }

    template = DocumentTemplateRead(
        id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        name="Standard quote",
        applies_to="QUOTE",
        lines=[
            {
                "id": uuid.uuid4(),
                "sort_order": 2,
                "name": "Consulting",
                "description": "Discovery and planning",
                "quantity": Decimal("2.5"),
                "unit_id": product.unit_id,
                "unit_name": "hour",
                "unit_price": Decimal("125.00"),
                "discount_type": "PERCENTAGE",
                "discount_value": Decimal("5"),
                "vat_rate_id": product.default_vat_rate_id,
            }
        ],
        created_at=now,
        updated_at=now,
    )
    template_output = DocumentTemplateListOutput(items=[template])
    serialized_template = template_output.model_dump(mode="json")["items"][0]
    assert serialized_template == template.model_dump(mode="json")
    assert serialized_template["applies_to"] == "QUOTE"
    assert serialized_template["lines"][0]["unit_price"] == "125.00"
    assert serialized_template["lines"][0]["vat_rate_id"] == str(product.default_vat_rate_id)

    rate = VatRateRead(
        id=uuid.uuid4(), label="Reduced", percent=Decimal("9"), active=True,
        created_at=now, updated_at=now,
    )
    treatment = VatTreatmentRead(
        id=uuid.uuid4(), code="EU_B2B", label="EU business customer",
        side="SALES", effect="ZERO_REVERSE", report_box=None,
        requires_icp=True, deductible=None, active=True,
        created_at=now, updated_at=now,
    )
    vat_output = VatOptionsOutput(rates=[rate], treatments=[treatment])
    serialized_vat = vat_output.model_dump(mode="json")
    assert serialized_vat == {
        "rates": [rate.model_dump(mode="json")],
        "treatments": [treatment.model_dump(mode="json")],
    }
    assert serialized_vat["rates"][0]["percent"] == "9"
    assert serialized_vat["treatments"][0]["effect"] == "ZERO_REVERSE"


def test_every_mutating_tool_response_requires_idempotency_result() -> None:
    # Receipt intake, customer creation, and quote/invoice create and update
    # share these response contracts. Keep each write tool explicitly mapped.
    write_responses = {
        "submit_receipt": SubmitReceiptOutput,
        "create_customer": CreateCustomerOutput,
        "create_quote_draft": QuoteWriteOutput,
        "update_quote_draft": QuoteWriteOutput,
        "create_invoice_draft": InvoiceWriteOutput,
        "update_invoice_draft": InvoiceWriteOutput,
    }
    for tool_name, response_model in write_responses.items():
        if response_model is SubmitReceiptOutput:
            assert all(
                response_model.model_fields[field].is_required()
                for field in ("operation_id", "status", "target_id")
            ), tool_name
        else:
            assert response_model.model_fields["operation"].is_required(), tool_name
    assert all(
        AgentWriteResult.model_fields[field].is_required()
        for field in ("operation_id", "status", "target_id")
    )


def test_host_parser_handles_loopback_authorities() -> None:
    assert _host_from_authority("localhost:8000") == "localhost"
    assert _host_from_authority("127.0.0.1:8000") == "127.0.0.1"
    assert _host_from_authority("[::1]:8000") == "[::1]"
    assert _host_from_authority("localhost:65535") == "localhost"
    assert _host_from_authority("[::1]:65535") == "[::1]"
    assert _host_from_authority("localhost:0") == ""
    assert _host_from_authority("[::1]:65536") == ""
    assert _host_from_authority(f"localhost:{'9' * 5000}") == ""
    assert _host_from_authority(f"[::1]:{'9' * 5000}") == ""


async def test_mcp_mount_rejects_anonymous_requests_and_checks_authority() -> None:
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        # The canonical route must pass every HTTP method through the boundary
        # before the SDK can decide whether that method is supported.
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"):
            response = await client.request(method, "/api/v1/mcp")
            assert response.status_code == 401, method
            assert response.json()["error"]["code"] == "MCP_UNAUTHENTICATED", method

        unsafe_host = await client.post(
            "/api/v1/mcp", headers={"host": "attacker.example"}, json={}
        )
        unsafe_origin = await client.post(
            "/api/v1/mcp", headers={"origin": "https://attacker.example"}, json={}
        )
        assert unsafe_host.status_code == 421
        assert unsafe_host.json()["error"]["code"] == "MCP_HOST_REJECTED"
        assert unsafe_origin.status_code == 403
        assert unsafe_origin.json()["error"]["code"] == "MCP_ORIGIN_REJECTED"

        unsafe_method_host = await client.request(
            "OPTIONS", "/api/v1/mcp", headers={"host": "attacker.example"}
        )
        assert unsafe_method_host.status_code == 421
        assert unsafe_method_host.json()["error"]["code"] == "MCP_HOST_REJECTED"

        for invalid_host in (
            "[::1]:65536",
            "localhost:0",
            f"localhost:{'9' * 5000}",
        ):
            rejected_host = await client.post(
                "/api/v1/mcp", headers={"host": invalid_host}, json={}
            )
            assert rejected_host.status_code == 421
            assert rejected_host.json()["error"]["code"] == "MCP_HOST_REJECTED"

        valid_loopback = await client.request(
            "OPTIONS",
            "/api/v1/mcp",
            headers={"host": "[::1]:65535", "origin": "http://[::1]"},
        )
        assert valid_loopback.status_code == 401
        assert valid_loopback.json()["error"]["code"] == "MCP_UNAUTHENTICATED"

        for malformed_origin in (
            "http://[",
            "http://[::1",
            "http://local\nhost",
            " http://localhost",
            "http://localhost\t",
        ):
            malformed = await client.post(
                "/api/v1/mcp", headers={"origin": malformed_origin}, json={}
            )
            assert malformed.status_code == 403
            assert malformed.json()["error"]["code"] == "MCP_ORIGIN_REJECTED"


async def test_mcp_preflight_is_checked_before_dev_cors() -> None:
    app = create_app()
    preflight_headers = {
        "origin": "http://localhost:5173",
        "access-control-request-method": "POST",
        "access-control-request-headers": "authorization,content-type",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        for path in ("/api/v1/mcp", "/api/v1/mcp/", "/api/v1/mcp/nested"):
            allowed_host = await client.options(
                path, headers={**preflight_headers, "host": "localhost"}
            )
            assert allowed_host.status_code == 401, path
            assert allowed_host.json()["error"]["code"] == "MCP_UNAUTHENTICATED"
            assert "access-control-allow-origin" not in allowed_host.headers

            rejected_host = await client.options(
                path, headers={**preflight_headers, "host": "attacker.example"}
            )
            assert rejected_host.status_code == 421, path
            assert rejected_host.json()["error"]["code"] == "MCP_HOST_REJECTED"
            assert "access-control-allow-origin" not in rejected_host.headers

        api_preflight = await client.options(
            "/api/health", headers={**preflight_headers, "host": "localhost"}
        )
        assert api_preflight.status_code == 200
        assert api_preflight.headers["access-control-allow-origin"] == (
            "http://localhost:5173"
        )


async def test_owner_contract_is_mfa_protected_and_mcp_precedes_spa(tmp_path) -> None:
    (tmp_path / "index.html").write_text("<h1>SPA</h1>")
    import jai.config as cfg
    from jai.config import Settings

    original = cfg.get_settings
    cfg.get_settings = lambda: Settings(static_dir=str(tmp_path))  # type: ignore[assignment]
    try:
        app = create_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            token_list = await client.get("/api/v1/agent-tokens")
            mcp_path = await client.get("/api/v1/mcp")
            health = await client.get("/api/health")
            spa = await client.get("/a/spa/route")
            assert token_list.status_code == 401
            assert mcp_path.status_code == 401
            assert health.status_code == 200
            assert spa.status_code == 200
            assert "<h1>SPA</h1>" in spa.text
        schema = app.openapi()
        assert "/api/v1/agent-tokens" in schema["paths"]
        token_schema = schema["components"]["schemas"]["AgentTokenCreated"]
        assert token_schema["properties"]["token"]["writeOnly"] is True
    finally:
        cfg.get_settings = original  # type: ignore[assignment]


async def test_parent_lifespan_runs_real_mcp_session_manager(monkeypatch) -> None:
    async def no_startup_io(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(main, "check_db_migration", no_startup_io)
    monkeypatch.setattr(main, "resolve_auth_secret", no_startup_io)
    monkeypatch.setattr(main, "get_engine", lambda: object())
    monkeypatch.setattr(main, "get_session_maker", lambda: object())
    monkeypatch.setattr(cfg, "get_settings", lambda: cfg.Settings(scheduler_enabled=False))
    app = create_app()
    sdk_headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }
    async with app.router.lifespan_context(app):
        # Exercise the SDK's actual Streamable HTTP protocol while its manager
        # is owned by the parent app lifespan. This internal path is not mounted
        # publicly; public requests still pass through the 401 boundary below.
        async with AsyncClient(
            transport=ASGITransport(app=mcp_api._sdk_app),
            base_url="http://localhost",
        ) as sdk_client:
            initialize = await sdk_client.post(
                "/",
                headers=sdk_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "step1-test", "version": "1"},
                    },
                },
            )
            assert initialize.status_code == 200
            session_id = initialize.headers["mcp-session-id"]
            discovery = await sdk_client.post(
                "/",
                headers={
                    **sdk_headers,
                    "mcp-session-id": session_id,
                    "mcp-protocol-version": "2025-11-25",
                },
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            assert discovery.status_code == 200
            payload = json.loads(discovery.text.split("data: ", 1)[1])
            assert {tool["name"] for tool in payload["result"]["tools"]} == {
                "submit_receipt",
                "get_receipt_status",
                "search_customers",
                "get_customer",
                "create_customer",
                "search_catalog",
                "list_document_templates",
                "list_vat_options",
                "preview_quote",
                "create_quote_draft",
                "get_quote",
                "update_quote_draft",
            }
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://localhost"
        ) as public_client:
            public_discovery = await public_client.post(
                "/api/v1/mcp",
                headers={"cookie": "access-token=owner-session"},
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            assert public_discovery.status_code == 401
            assert public_discovery.json()["error"]["code"] == "MCP_UNAUTHENTICATED"

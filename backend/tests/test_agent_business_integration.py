"""M15 Step6 Agent customer and read-only context integration coverage."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient, Response
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.types import ASGIApp

from jai.api import mcp as mcp_module
from jai.config import Settings
from jai.db import set_rls_company
from jai.models._enums import (
    DocumentTemplateScope,
    InvoiceDocumentKind,
    InvoiceStatus,
    InvoiceTaxMode,
    QuoteStatus,
)
from jai.models.agent_operation import AgentOperation
from jai.models.agent_token import AgentToken
from jai.models.company import Company
from jai.models.content import DocumentTemplate
from jai.models.customer import Customer
from jai.models.document import DocumentChainEvent
from jai.models.invoice import Invoice
from jai.models.product import Product
from jai.models.quote import Quote
from jai.models.user import User
from jai.models.vat import VatRate, VatTreatment
from jai.schemas.agent_token import AgentTokenCreate, AgentTokenScope
from jai.schemas.agent_tools import (
    CreateCustomerInput,
    CreateInvoiceDraftInput,
    CreateQuoteDraftInput,
    PreviewInvoiceInput,
    PreviewQuoteInput,
    SearchCatalogInput,
    SearchCustomersInput,
    UpdateInvoiceDraftInput,
    UpdateQuoteDraftInput,
)
from jai.schemas.customer import CustomerWrite
from jai.schemas.invoice import InvoiceWrite
from jai.schemas.quote import QuoteWrite
from jai.services import agent_business as agent_business_service
from jai.services.agent_business import (
    create_customer_idempotently,
    create_invoice_draft,
    create_quote_draft,
    get_customer_detail,
    list_templates,
    list_vat_options,
    preview_invoice,
    preview_quote,
    read_invoice,
    read_quote,
    search_catalog,
    search_customers,
    update_invoice_draft,
    update_quote_draft,
)
from jai.services.agent_operations import AgentOperationFailure
from jai.services.agent_tokens import AgentPrincipal, issue_token
from jai.services.vat import seed_for_company


def _fresh_mcp_wire_app() -> ASGIApp:
    """Build an isolated SDK transport while reusing registered MCP tools."""
    sdk_app = mcp_module.server.streamable_http_app(
        streamable_http_path="/",
        transport_security=mcp_module.TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )
    app = Starlette(
        routes=[
            Route(
                "/api/v1/mcp",
                mcp_module.ExactMCPEntry(sdk_app),
                methods=["GET", "POST", "DELETE"],
            ),
            Mount("/api/v1/mcp", sdk_app),
        ]
    )
    return mcp_module.ClosedMCPBoundary(app)


@pytest.fixture
async def business_principals(
    admin_session_maker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[AgentPrincipal, AgentPrincipal, uuid.UUID]]:
    company_id, other_company_id, owner_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    scopes = [
        AgentTokenScope.CUSTOMER_READ,
        AgentTokenScope.CUSTOMER_CREATE,
        AgentTokenScope.CATALOG_READ,
        AgentTokenScope.QUOTE_READ,
        AgentTokenScope.QUOTE_WRITE_DRAFT,
        AgentTokenScope.INVOICE_READ,
        AgentTokenScope.INVOICE_WRITE_DRAFT,
    ]
    tokens: list[AgentToken] = []
    principals: list[AgentPrincipal] = []
    for tenant_id in (company_id, other_company_id):
        token, _secret = issue_token(AgentTokenCreate(name="Step6 test", scopes=scopes), tenant_id)
        token.id = uuid.uuid4()
        token.creator_id = owner_id
        tokens.append(token)
        principals.append(AgentPrincipal(token.id, tenant_id, frozenset(scopes)))
    async with admin_session_maker() as session:
        session.add_all(
            [
                Company(id=company_id, name="Step6 company", base_currency="EUR"),
                Company(id=other_company_id, name="Other step6 company", base_currency="EUR"),
                User(
                    id=owner_id,
                    email=f"{owner_id}@example.test",
                    hashed_password="not-a-real-password-hash",
                    company_id=company_id,
                    role="owner",
                    mfa_enabled=True,
                    totp_secret="test-secret",
                ),
            ]
        )
        await session.commit()
        session.add_all(tokens)
        await session.commit()
    yield principals[0], principals[1], owner_id


@pytest.mark.integration
async def test_customer_search_detail_create_replay_and_company_rls(
    business_principals: tuple[AgentPrincipal, AgentPrincipal, uuid.UUID],
    admin_session_maker: async_sessionmaker[AsyncSession],
    runtime_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    principal, other, _owner_id = business_principals
    async with admin_session_maker() as session:
        session.add_all(
            [
                Customer(
                    company_id=principal.company_id,
                    name="Acme BV",
                    email="billing@acme.test",
                ),
                Customer(
                    company_id=principal.company_id,
                    name="Acme Building",
                    company_name="Acme Group",
                ),
                Customer(company_id=other.company_id, name="Acme Other Tenant"),
            ]
        )
        await session.commit()

    candidates = await search_customers(runtime_session_maker, principal, "Acme", 1)
    assert len(candidates.items) == 1
    assert candidates.items[0].name == "Acme Building"
    similar = await search_customers(runtime_session_maker, principal, "Acme", 10)
    assert {row.name for row in similar.items} == {"Acme BV", "Acme Building"}
    exact_candidate = next(row for row in similar.items if row.name == "Acme BV")
    detail = await get_customer_detail(runtime_session_maker, principal, candidates.items[0].id)
    assert detail is not None and detail.customer.name == "Acme Building"
    exact_detail = await get_customer_detail(runtime_session_maker, principal, exact_candidate.id)
    assert exact_detail is not None
    assert exact_detail.customer.email == "billing@acme.test"
    assert await get_customer_detail(runtime_session_maker, principal, uuid.uuid4()) is None
    other_customer = await search_customers(runtime_session_maker, other, "Other", 10)
    assert (
        await get_customer_detail(runtime_session_maker, principal, other_customer.items[0].id)
        is None
    )

    request = CreateCustomerInput(request_id=uuid.uuid4(), customer=CustomerWrite(name="Solo name"))
    first, replay = await asyncio.gather(
        create_customer_idempotently(runtime_session_maker, principal, request),
        create_customer_idempotently(runtime_session_maker, principal, request),
    )
    assert {first.operation.status, replay.operation.status} == {"created", "replayed"}
    assert first.customer.id == replay.customer.id
    assert first.customer.name == "Solo name"
    async with runtime_session_maker() as session, session.begin():
        await set_rls_company(session, principal.company_id)
        rows = list(
            (await session.scalars(select(Customer).where(Customer.name == "Solo name"))).all()
        )
        traces = list(
            (
                await session.scalars(
                    select(AgentOperation).where(
                        AgentOperation.request_id == str(request.request_id)
                    )
                )
            ).all()
        )
    assert len(rows) == 1
    assert len(traces) == 1


@pytest.mark.integration
async def test_customer_create_callback_failure_rolls_back_customer_and_operation(
    business_principals: tuple[AgentPrincipal, AgentPrincipal, uuid.UUID],
    admin_session_maker: async_sessionmaker[AsyncSession],
    runtime_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal, other, _owner_id = business_principals
    original_create = agent_business_service.create_customer

    async def fail_after_create(session: AsyncSession, data: CustomerWrite, company_id: uuid.UUID):
        await original_create(session, data, company_id)
        raise RuntimeError("injected rollback")

    monkeypatch.setattr(agent_business_service, "create_customer", fail_after_create)
    request = CreateCustomerInput(
        request_id=uuid.uuid4(), customer=CustomerWrite(name="Rolled back")
    )
    with pytest.raises(RuntimeError):
        await create_customer_idempotently(runtime_session_maker, principal, request)

    async with admin_session_maker() as session:
        assert (
            await session.scalar(
                select(Customer.id).where(
                    Customer.company_id == principal.company_id,
                    Customer.name == "Rolled back",
                )
            )
            is None
        )
        assert (
            await session.scalar(
                select(AgentOperation.id).where(
                    AgentOperation.company_id == principal.company_id,
                    AgentOperation.request_id == str(request.request_id),
                )
            )
            is None
        )


@pytest.mark.integration
async def test_catalog_and_saved_options_are_bounded_and_company_owned(
    business_principals: tuple[AgentPrincipal, AgentPrincipal, uuid.UUID],
    admin_session_maker: async_sessionmaker[AsyncSession],
    runtime_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    principal, other, _owner_id = business_principals
    async with admin_session_maker() as session:
        session.add_all(
            [
                Product(
                    company_id=principal.company_id,
                    name="Bolt",
                    purchase_cost_excl_vat=Decimal("4.000"),
                ),
                Product(
                    company_id=principal.company_id,
                    name="Bracket",
                    purchase_cost_excl_vat=Decimal("8.000"),
                ),
                Product(
                    company_id=principal.company_id,
                    name="Board",
                    purchase_cost_excl_vat=Decimal("12.000"),
                ),
                Product(company_id=other.company_id, name="Foreign product"),
                DocumentTemplate(
                    company_id=principal.company_id,
                    name="Quote scope",
                    applies_to=DocumentTemplateScope.QUOTE,
                ),
                DocumentTemplate(
                    company_id=other.company_id,
                    name="Other scope",
                    applies_to=DocumentTemplateScope.QUOTE,
                ),
            ]
        )
        await seed_for_company(session, principal.company_id)
        await session.commit()

    products = await search_catalog(runtime_session_maker, principal, "", 2)
    assert [row.name for row in products.items] == ["Board", "Bolt"]
    assert "purchase_cost_excl_vat" not in products.items[0].model_dump()
    templates = await list_templates(runtime_session_maker, principal)
    assert [row.name for row in templates.items] == ["Quote scope"]
    options = await list_vat_options(runtime_session_maker, principal)
    assert options.rates
    assert {rate.percent for rate in options.rates} >= {Decimal("0.000"), Decimal("21.000")}
    assert options.treatments


def test_step6_input_bounds_and_name_validation() -> None:
    assert SearchCustomersInput(query=" Acme ", limit=100).query == "Acme"
    assert SearchCatalogInput(limit=1).limit == 1
    with pytest.raises(ValidationError):
        SearchCustomersInput(query="   ")
    with pytest.raises(ValidationError):
        SearchCatalogInput(limit=101)
    with pytest.raises(ValidationError):
        CustomerWrite(name="   ")
    with pytest.raises(ValidationError):
        CustomerWrite()


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        (
            mcp_module.preview_quote_tool,
            {
                "quote": QuoteWrite.model_construct(
                    customer_id=uuid.uuid4(), quote_date=date.today(),
                    tax_mode=InvoiceTaxMode.LINE, lines=[]
                )
            },
        ),
        (mcp_module.get_quote_tool, {"quote_id": uuid.uuid4()}),
        (
            mcp_module.create_quote_draft_tool,
            {
                "request_id": uuid.uuid4(),
                "quote": QuoteWrite.model_construct(
                    customer_id=uuid.uuid4(), quote_date=date.today(),
                    tax_mode=InvoiceTaxMode.LINE, lines=[]
                ),
            },
        ),
        (
            mcp_module.update_quote_draft_tool,
            {
                "request_id": uuid.uuid4(),
                "quote_id": uuid.uuid4(),
                "quote": QuoteWrite.model_construct(
                    customer_id=uuid.uuid4(), quote_date=date.today(),
                    tax_mode=InvoiceTaxMode.LINE, lines=[]
                ),
            },
        ),
    ],
)
async def test_step7_tools_check_scope_on_every_call(
    tool: object, kwargs: dict[str, object]
) -> None:
    principal = AgentPrincipal(
        uuid.uuid4(), uuid.uuid4(), frozenset({AgentTokenScope.RECEIPT_INTAKE})
    )
    token = mcp_module.agent_principal.set(principal)
    try:
        with pytest.raises(ToolError) as exc:
            await tool(**kwargs)  # type: ignore[operator]
        assert "MCP_INSUFFICIENT_SCOPE" in str(exc.value)
    finally:
        mcp_module.agent_principal.reset(token)


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        (
            mcp_module.preview_invoice_tool,
            {
                "invoice": InvoiceWrite.model_construct(
                    customer_id=uuid.uuid4(), invoice_date=date.today(),
                    tax_mode=InvoiceTaxMode.LINE, lines=[]
                )
            },
        ),
        (mcp_module.get_invoice_tool, {"invoice_id": uuid.uuid4()}),
        (
            mcp_module.create_invoice_draft_tool,
            {
                "request_id": uuid.uuid4(),
                "invoice": InvoiceWrite.model_construct(
                    customer_id=uuid.uuid4(), invoice_date=date.today(),
                    tax_mode=InvoiceTaxMode.LINE, lines=[]
                ),
            },
        ),
        (
            mcp_module.update_invoice_draft_tool,
            {
                "request_id": uuid.uuid4(),
                "invoice_id": uuid.uuid4(),
                "invoice": InvoiceWrite.model_construct(
                    customer_id=uuid.uuid4(), invoice_date=date.today(),
                    tax_mode=InvoiceTaxMode.LINE, lines=[]
                ),
            },
        ),
    ],
)
async def test_step8_tools_check_invoice_scope_on_every_call(
    tool: object, kwargs: dict[str, object]
) -> None:
    principal = AgentPrincipal(
        uuid.uuid4(), uuid.uuid4(), frozenset({AgentTokenScope.RECEIPT_INTAKE})
    )
    token = mcp_module.agent_principal.set(principal)
    try:
        with pytest.raises(ToolError) as exc:
            await tool(**kwargs)  # type: ignore[operator]
        assert "MCP_INSUFFICIENT_SCOPE" in str(exc.value)
    finally:
        mcp_module.agent_principal.reset(token)


def _quote_write(
    customer_id: uuid.UUID, vat_rate_id: uuid.UUID, treatment_id: uuid.UUID
) -> QuoteWrite:
    return QuoteWrite.model_validate(
        {
            "customer_id": customer_id,
            "quote_date": "2026-09-30",
            "tax_mode": "LINE",
            "vat_treatment_id": treatment_id,
            "discount": {"type": "FIXED", "value": "5"},
            "lines": [
                {
                    "name": "Design",
                    "quantity": "2",
                    "unit_price": "100.005",
                    "vat_rate_id": vat_rate_id,
                    "discount": {"type": "PERCENTAGE", "value": "10"},
                },
                {
                    "name": "Support",
                    "quantity": "1",
                    "unit_price": "50.005",
                    "vat_rate_id": vat_rate_id,
                },
            ],
        }
    )


def _invoice_write(
    customer_id: uuid.UUID, vat_rate_id: uuid.UUID, treatment_id: uuid.UUID
) -> InvoiceWrite:
    return InvoiceWrite.model_validate(
        {
            "customer_id": customer_id,
            "invoice_date": "2026-09-30",
            "due_date": "2026-10-30",
            "tax_mode": "LINE",
            "vat_treatment_id": treatment_id,
            "notes": "Invoice note",
            "terms_text": "Payment within thirty days",
            "lines": [
                {
                    "name": "Consulting",
                    "quantity": "2",
                    "unit_price": "100.00",
                    "vat_rate_id": vat_rate_id,
                }
            ],
        }
    )


@pytest.mark.integration
async def test_standard_invoice_tools_share_pricing_atomic_replay_and_locked_draft_gate(
    business_principals: tuple[AgentPrincipal, AgentPrincipal, uuid.UUID],
    admin_session_maker: async_sessionmaker[AsyncSession],
    runtime_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal, _other, _owner_id = business_principals
    customer = Customer(company_id=principal.company_id, name="Invoice customer")
    foreign_customer = Customer(company_id=_other.company_id, name="Foreign invoice customer")
    async with admin_session_maker() as session:
        session.add_all([customer, foreign_customer])
        await session.flush()
        await seed_for_company(session, principal.company_id)
        await seed_for_company(session, _other.company_id)
        rate = await session.scalar(
            select(VatRate).where(
                VatRate.company_id == principal.company_id,
                VatRate.percent == Decimal("21.000"),
            )
        )
        treatment = await session.scalar(
            select(VatTreatment).where(
                VatTreatment.company_id == principal.company_id,
                VatTreatment.code == "NL_DOMESTIC",
            )
        )
        foreign_rate = await session.scalar(
            select(VatRate).where(
                VatRate.company_id == _other.company_id,
                VatRate.percent == Decimal("21.000"),
            )
        )
        assert rate is not None and treatment is not None and foreign_rate is not None
        await session.commit()

    from jai.services import agent_business as business

    monkeypatch.setattr(
        business,
        "get_settings",
        lambda: Settings(base_url="https://invoice-first.example.test", scheduler_enabled=False),
    )
    body = _invoice_write(customer.id, rate.id, treatment.id)
    preview = await preview_invoice(
        runtime_session_maker, principal, PreviewInvoiceInput(invoice=body)
    )
    assert (preview.subtotal, preview.tax_total, preview.total, preview.currency) == (
        Decimal("200.00"), Decimal("42.00"), Decimal("242.00"), "EUR"
    )
    with pytest.raises(ValueError, match="Customer not found"):
        await preview_invoice(
            runtime_session_maker,
            principal,
            PreviewInvoiceInput(
                invoice=body.model_copy(update={"customer_id": foreign_customer.id})
            ),
        )
    cross_company_rate = body.model_copy(
        update={
            "lines": [
                body.lines[0].model_copy(update={"vat_rate_id": foreign_rate.id})
            ]
        }
    )
    with pytest.raises(ValueError, match="VAT rate not found"):
        await preview_invoice(
            runtime_session_maker,
            principal,
            PreviewInvoiceInput(invoice=cross_company_rate),
        )

    request = CreateInvoiceDraftInput(request_id=uuid.uuid4(), invoice=body)
    created, first_replay = await asyncio.gather(
        create_invoice_draft(runtime_session_maker, principal, request),
        create_invoice_draft(runtime_session_maker, principal, request),
    )
    assert {created.operation.status, first_replay.operation.status} == {"created", "replayed"}
    assert created.invoice.document_kind == InvoiceDocumentKind.STANDARD
    assert created.invoice.status == InvoiceStatus.DRAFT
    assert created.invoice.invoice_number is None
    assert created.invoice.sequence_number is None
    assert created.invoice.total_incl_vat == preview.total
    assert created.operation.web_url == (
        f"https://invoice-first.example.test/invoices/{created.invoice.id}/edit"
    )

    changed_body = body.model_copy(update={"notes": "Changed terms", "terms_text": "Net 14"})
    update_request = UpdateInvoiceDraftInput(
        request_id=uuid.uuid4(), invoice_id=created.invoice.id, invoice=changed_body
    )
    updated, update_replay = await asyncio.gather(
        update_invoice_draft(runtime_session_maker, principal, update_request),
        update_invoice_draft(runtime_session_maker, principal, update_request),
    )
    assert {updated.operation.status, update_replay.operation.status} == {"created", "replayed"}
    assert updated.invoice.notes == "Changed terms"
    assert updated.invoice.terms_text == "Net 14"
    assert updated.operation.web_url == created.operation.web_url

    # A later edit and BASE_URL change cannot rewrite the already stored result.
    async with admin_session_maker() as session:
        row = await session.get(Invoice, created.invoice.id)
        assert row is not None
        row.notes = "Later web edit"
        await session.commit()
    monkeypatch.setattr(
        business,
        "get_settings",
        lambda: Settings(base_url="https://invoice-later.example.test", scheduler_enabled=False),
    )
    replay = await update_invoice_draft(runtime_session_maker, principal, update_request)
    assert replay.operation.status == "replayed"
    assert replay.invoice.notes == "Changed terms"
    assert replay.operation.web_url == updated.operation.web_url

    async with admin_session_maker() as session:
        row = await session.get(Invoice, created.invoice.id)
        assert row is not None
        row.status = InvoiceStatus.COMPLETED
        await session.commit()
    with pytest.raises(HTTPException) as rejected_state:
        await update_invoice_draft(
            runtime_session_maker,
            principal,
            UpdateInvoiceDraftInput(
                request_id=uuid.uuid4(), invoice_id=created.invoice.id, invoice=changed_body
            ),
        )
    assert rejected_state.value.detail == {"code": "MCP_DRAFT_REQUIRED"}

    # Non-Standard invoices are rejected for reads and updates; a SENT Standard
    # invoice is rejected for mutation after the normal service locks it.
    async with admin_session_maker() as session:
        row = await session.get(Invoice, created.invoice.id)
        assert row is not None
        row.status = InvoiceStatus.DRAFT
        row.document_kind = InvoiceDocumentKind.ADVANCE
        await session.commit()
    with pytest.raises(AgentOperationFailure):
        await read_invoice(runtime_session_maker, principal, created.invoice.id)
    with pytest.raises(HTTPException) as rejected_kind:
        await update_invoice_draft(
            runtime_session_maker,
            principal,
            UpdateInvoiceDraftInput(
                request_id=uuid.uuid4(), invoice_id=created.invoice.id, invoice=changed_body
            ),
        )
    assert rejected_kind.value.detail == {"code": "MCP_DRAFT_REQUIRED"}

    async with admin_session_maker() as session:
        row = await session.get(Invoice, created.invoice.id)
        assert row is not None
        await session.delete(row)
        await session.commit()
    replay_after_delete = await create_invoice_draft(
        runtime_session_maker, principal, request
    )
    assert replay_after_delete.operation.status == "replayed"
    assert replay_after_delete.invoice.model_dump(mode="json") == created.invoice.model_dump(
        mode="json"
    )
    assert replay_after_delete.operation.web_url == created.operation.web_url
    async with admin_session_maker() as session:
        assert await session.get(Invoice, created.invoice.id) is None


@pytest.mark.integration
async def test_invoice_replay_snapshot_rejection_rolls_back_invoice_chain_and_operation(
    business_principals: tuple[AgentPrincipal, AgentPrincipal, uuid.UUID],
    admin_session_maker: async_sessionmaker[AsyncSession],
    runtime_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal, _other, _owner_id = business_principals
    customer = Customer(company_id=principal.company_id, name="Rollback invoice customer")
    async with admin_session_maker() as session:
        session.add(customer)
        await session.flush()
        await seed_for_company(session, principal.company_id)
        rate = await session.scalar(
            select(VatRate).where(
                VatRate.company_id == principal.company_id,
                VatRate.percent == Decimal("21.000"),
            )
        )
        treatment = await session.scalar(
            select(VatTreatment).where(
                VatTreatment.company_id == principal.company_id,
                VatTreatment.code == "NL_DOMESTIC",
            )
        )
        assert rate is not None and treatment is not None
        await session.commit()

    from jai.services import agent_business as business

    # The mutation succeeds through invoice persistence and event append, then
    # the D9 snapshot filter rejects the bearer-like URL. The outer operation
    # transaction must roll all three records back together.
    monkeypatch.setattr(business, "_invoice_web_url", lambda _invoice_id: "Bearer secret-value")
    request_id = uuid.uuid4()
    with pytest.raises(ValueError, match="bearer credential material"):
        await create_invoice_draft(
            runtime_session_maker,
            principal,
            CreateInvoiceDraftInput(
                request_id=request_id,
                invoice=_invoice_write(customer.id, rate.id, treatment.id),
            ),
        )

    async with admin_session_maker() as session:
        assert (
            await session.scalar(
                select(Invoice.id).where(
                    Invoice.company_id == principal.company_id,
                    Invoice.customer_id == customer.id,
                )
            )
        ) is None
        assert (
            await session.scalar(
                select(DocumentChainEvent.id).where(
                    DocumentChainEvent.company_id == principal.company_id
                )
            )
        ) is None
        assert (
            await session.scalar(
                select(AgentOperation.id).where(
                    AgentOperation.token_id == principal.token_id,
                    AgentOperation.request_id == str(request_id),
                )
            )
        ) is None


@pytest.mark.integration
async def test_standard_invoice_draft_has_no_vat_report_effect(
    db_client: AsyncClient,
    admin_session_maker: async_sessionmaker[AsyncSession],
    runtime_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    from test_quote_payment_integration import _full_auth, _setup_company

    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    customer_response = await db_client.post(
        "/api/v1/customers", json={"name": "Draft report customer"}
    )
    assert customer_response.status_code == 201, customer_response.text
    company_id = uuid.UUID(seeds["company_id"])
    customer_id = uuid.UUID(customer_response.json()["id"])
    async with admin_session_maker() as session:
        owner_id = await session.scalar(select(User.id).where(User.company_id == company_id))
        assert owner_id is not None
        token, _secret = issue_token(
            AgentTokenCreate(
                name="Draft VAT report test",
                scopes=[
                    AgentTokenScope.INVOICE_READ,
                    AgentTokenScope.INVOICE_WRITE_DRAFT,
                ],
            ),
            company_id,
        )
        token.creator_id = owner_id
        session.add(token)
        await session.commit()
    principal = AgentPrincipal(
        token.id,
        company_id,
        frozenset(
            {AgentTokenScope.INVOICE_READ, AgentTokenScope.INVOICE_WRITE_DRAFT}
        ),
    )
    before_response = await db_client.get(
        "/api/v1/reports/vat-return?year=2026&quarter=3"
    )
    assert before_response.status_code == 200, before_response.text
    before = before_response.json()
    await create_invoice_draft(
        runtime_session_maker,
        principal,
        CreateInvoiceDraftInput(
            request_id=uuid.uuid4(),
            invoice=_invoice_write(
                customer_id,
                uuid.UUID(seeds["rates"]["NL standard (21%)"]["id"]),
                uuid.UUID(seeds["treatments"]["NL_DOMESTIC"]["id"]),
            ),
        ),
    )
    after_response = await db_client.get(
        "/api/v1/reports/vat-return?year=2026&quarter=3"
    )
    assert after_response.status_code == 200, after_response.text
    after = after_response.json()
    assert after["event_rows"] == before["event_rows"] == []
    assert after["totals"] == before["totals"]



@pytest.mark.integration
async def test_quote_tools_share_pricing_and_replay_original_snapshot_and_url(
    business_principals: tuple[AgentPrincipal, AgentPrincipal, uuid.UUID],
    admin_session_maker: async_sessionmaker[AsyncSession],
    runtime_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal, other, _owner_id = business_principals
    from jai.config import Settings
    from jai.services import agent_business as business
    from jai.services.vat import seed_for_company

    customer = Customer(company_id=principal.company_id, name="Quote customer")
    other_customer = Customer(company_id=other.company_id, name="Foreign quote customer")
    async with admin_session_maker() as session:
        session.add_all([customer, other_customer])
        await session.flush()
        await seed_for_company(session, principal.company_id)
        await seed_for_company(session, other.company_id)
        rate = await session.scalar(
            select(VatRate).where(
                VatRate.company_id == principal.company_id,
                VatRate.percent == Decimal("21.000"),
            )
        )
        treatment = await session.scalar(
            select(VatTreatment).where(
                VatTreatment.company_id == principal.company_id,
                VatTreatment.code == "NL_DOMESTIC",
            )
        )
        foreign_rate = await session.scalar(
            select(VatRate).where(
                VatRate.company_id == other.company_id,
                VatRate.percent == Decimal("21.000"),
            )
        )
        assert rate is not None and treatment is not None and foreign_rate is not None
        await session.commit()

    monkeypatch.setattr(
        business,
        "get_settings",
        lambda: Settings(base_url="https://first.example.test", scheduler_enabled=False),
    )
    body = _quote_write(customer.id, rate.id, treatment.id)
    with pytest.raises(ValueError, match="Customer not found"):
        await preview_quote(
            runtime_session_maker,
            principal,
            PreviewQuoteInput(quote=body.model_copy(update={"customer_id": other_customer.id})),
        )
    foreign_dictionary_body = body.model_copy(
        update={"lines": [body.lines[0].model_copy(update={"vat_rate_id": foreign_rate.id})]}
    )
    with pytest.raises(ValueError, match="VAT rate not found"):
        await preview_quote(
            runtime_session_maker, principal, PreviewQuoteInput(quote=foreign_dictionary_body)
        )
    preview = await preview_quote(
        runtime_session_maker, principal, PreviewQuoteInput(quote=body)
    )
    assert preview.subtotal == Decimal("250.02")
    assert preview.tax_total == Decimal("47.25")
    assert preview.total == Decimal("272.27")

    request = CreateQuoteDraftInput(request_id=uuid.uuid4(), quote=body)
    created, replay = await asyncio.gather(
        create_quote_draft(runtime_session_maker, principal, request),
        create_quote_draft(runtime_session_maker, principal, request),
    )
    assert {created.operation.status, replay.operation.status} == {"created", "replayed"}
    assert created.quote.status == QuoteStatus.DRAFT
    assert created.quote.quote_number
    assert created.quote.total_incl_vat == preview.total
    assert created.operation.web_url == f"https://first.example.test/quotes/{created.quote.id}/edit"

    updated_body = body.model_copy(update={"notes": "Changed after creation"})
    update_request = UpdateQuoteDraftInput(
        request_id=uuid.uuid4(), quote_id=created.quote.id, quote=updated_body
    )
    updated, update_replay = await asyncio.gather(
        update_quote_draft(runtime_session_maker, principal, update_request),
        update_quote_draft(runtime_session_maker, principal, update_request),
    )
    assert {updated.operation.status, update_replay.operation.status} == {
        "created",
        "replayed",
    }
    assert updated.quote.notes == "Changed after creation"

    async with admin_session_maker() as session:
        row = await session.get(Quote, created.quote.id)
        assert row is not None
        row.notes = "Changed again after the operation"
        await session.delete(row)
        await session.commit()

    monkeypatch.setattr(
        business,
        "get_settings",
        lambda: Settings(base_url="https://changed.example.test", scheduler_enabled=False),
    )
    replay_after_delete = await create_quote_draft(runtime_session_maker, principal, request)
    assert replay_after_delete.operation.status == "replayed"
    assert replay_after_delete.quote.id == created.quote.id
    assert replay_after_delete.quote.notes is None
    assert replay_after_delete.operation.web_url == created.operation.web_url
    update_replay_after_delete = await update_quote_draft(
        runtime_session_maker, principal, update_request
    )
    assert update_replay_after_delete.operation.status == "replayed"
    assert update_replay_after_delete.quote.notes == "Changed after creation"
    assert update_replay_after_delete.operation.web_url == updated.operation.web_url


@pytest.mark.integration
async def test_quote_agent_update_strict_locked_status_gate_and_read_only_expiry(
    business_principals: tuple[AgentPrincipal, AgentPrincipal, uuid.UUID],
    admin_session_maker: async_sessionmaker[AsyncSession],
    runtime_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    principal, _other, _owner_id = business_principals
    from jai.services.vat import seed_for_company

    customer = Customer(company_id=principal.company_id, name="Lifecycle customer")
    async with admin_session_maker() as session:
        session.add(customer)
        await session.flush()
        await seed_for_company(session, principal.company_id)
        rate = await session.scalar(
            select(VatRate).where(
                VatRate.company_id == principal.company_id,
                VatRate.percent == Decimal("21.000"),
            )
        )
        treatment = await session.scalar(
            select(VatTreatment).where(
                VatTreatment.company_id == principal.company_id,
                VatTreatment.code == "NL_DOMESTIC",
            )
        )
        assert rate is not None and treatment is not None
        await session.commit()
    body = _quote_write(customer.id, rate.id, treatment.id)
    created = await create_quote_draft(
        runtime_session_maker,
        principal,
        CreateQuoteDraftInput(request_id=uuid.uuid4(), quote=body),
    )
    for status in (
        QuoteStatus.SENT,
        QuoteStatus.ACCEPTED,
        QuoteStatus.REJECTED,
        QuoteStatus.EXPIRED,
    ):
        async with admin_session_maker() as session:
            row = await session.get(Quote, created.quote.id)
            assert row is not None
            row.status = status
            await session.commit()
        request = UpdateQuoteDraftInput(
            request_id=uuid.uuid4(), quote_id=created.quote.id, quote=body
        )
        with pytest.raises(HTTPException) as exc:
            await update_quote_draft(runtime_session_maker, principal, request)
        assert exc.value.detail == {"code": "MCP_DRAFT_REQUIRED"}
        async with admin_session_maker() as session:
            row = await session.get(Quote, created.quote.id)
            assert row is not None and row.status == status

    async with admin_session_maker() as session:
        row = await session.get(Quote, created.quote.id)
        assert row is not None
        row.status = QuoteStatus.SENT
        row.valid_until = date.fromisoformat("2020-01-01")
        await session.commit()
    read = await read_quote(runtime_session_maker, principal, created.quote.id)
    assert read is not None and read.quote.status == QuoteStatus.SENT
    async with admin_session_maker() as session:
        row = await session.get(Quote, created.quote.id)
        assert row is not None and row.status == QuoteStatus.SENT


@pytest.mark.integration
async def test_quote_tools_execute_over_streamable_http(
    admin_session_maker: async_sessionmaker[AsyncSession],
    runtime_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jai.services.vat import seed_for_company

    company_id, owner_id = uuid.uuid4(), uuid.uuid4()
    token, secret = issue_token(
        AgentTokenCreate(
            name="Quote MCP wire",
            scopes=[
                AgentTokenScope.QUOTE_READ,
                AgentTokenScope.QUOTE_WRITE_DRAFT,
                AgentTokenScope.INVOICE_READ,
                AgentTokenScope.INVOICE_WRITE_DRAFT,
            ],
        ),
        company_id,
    )
    token.creator_id = owner_id
    customer = Customer(company_id=company_id, name="Wire customer")
    async with admin_session_maker() as session:
        session.add_all(
            [
                Company(id=company_id, name="Wire company", base_currency="EUR"),
                User(
                    id=owner_id,
                    email=f"{owner_id}@example.test",
                    hashed_password="not-a-real-password-hash",
                    company_id=company_id,
                    role="owner",
                    mfa_enabled=True,
                    totp_secret="test-secret",
                ),
            ]
        )
        await session.flush()
        session.add(customer)
        await session.flush()
        await seed_for_company(session, company_id)
        rate = await session.scalar(
            select(VatRate).where(
                VatRate.company_id == company_id,
                VatRate.percent == Decimal("21.000"),
            )
        )
        treatment = await session.scalar(
            select(VatTreatment).where(
                VatTreatment.company_id == company_id,
                VatTreatment.code == "NL_DOMESTIC",
            )
        )
        assert rate is not None and treatment is not None
        session.add(token)
        await session.commit()

    monkeypatch.setattr(mcp_module, "get_session_maker", lambda: runtime_session_maker)
    settings = Settings(base_url="https://wire.example.test", scheduler_enabled=False)
    monkeypatch.setattr(mcp_module, "get_settings", lambda: settings)
    monkeypatch.setattr("jai.services.agent_business.get_settings", lambda: settings)
    quote = _quote_write(customer.id, rate.id, treatment.id).model_dump(mode="json")
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
                        "clientInfo": {"name": "quote-wire-test", "version": "1"},
                    },
                },
            )
            assert initialize.status_code == 200
            session_id = initialize.headers["mcp-session-id"]
            headers = {
                "authorization": f"Bearer {secret}",
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
                "mcp-session-id": session_id,
                "mcp-protocol-version": "2025-11-25",
            }

            async def call(tool_name: str, arguments: dict[str, object], call_id: int) -> Response:
                return await client.post(
                    "/api/v1/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": call_id,
                        "method": "tools/call",
                        "params": {"name": tool_name, "arguments": arguments},
                    },
                )

            preview_response = await call("preview_quote", {"quote": quote}, 2)
            assert preview_response.status_code == 200
            preview_result = json.loads(preview_response.text.split("data: ", 1)[1])["result"]
            preview = preview_result.get("structuredContent")
            assert preview is not None and preview["total"] == "272.27"

            created_response = await call(
                "create_quote_draft",
                {"request_id": str(uuid.uuid4()), "quote": quote},
                3,
            )
            assert created_response.status_code == 200
            created_result = json.loads(created_response.text.split("data: ", 1)[1])["result"]
            created = created_result.get("structuredContent")
            assert created is not None
            assert created["quote"]["status"] == "DRAFT"
            assert created["operation"]["web_url"].startswith("https://wire.example.test/quotes/")

            quote_id = created["quote"]["id"]
            read_response = await call("get_quote", {"quote_id": quote_id}, 4)
            assert read_response.status_code == 200
            read_result = json.loads(read_response.text.split("data: ", 1)[1])["result"]
            read_quote_result = read_result.get("structuredContent")
            assert read_quote_result is not None
            assert read_quote_result["quote"]["id"] == quote_id
            assert read_quote_result["web_url"] == created["operation"]["web_url"]

            update_payload = dict(quote)
            update_payload["notes"] = "Updated over MCP"
            update_response = await call(
                "update_quote_draft",
                {
                    "request_id": str(uuid.uuid4()),
                    "quote_id": quote_id,
                    "quote": update_payload,
                },
                5,
            )
            assert update_response.status_code == 200
            update_result = json.loads(update_response.text.split("data: ", 1)[1])["result"]
            updated = update_result.get("structuredContent")
            assert updated is not None and updated["quote"]["notes"] == "Updated over MCP"

            async with admin_session_maker() as session:
                quote_row = await session.get(Quote, uuid.UUID(quote_id))
                assert quote_row is not None
                quote_row.status = QuoteStatus.SENT
                await session.commit()
            rejected_update = await call(
                "update_quote_draft",
                {
                    "request_id": str(uuid.uuid4()),
                    "quote_id": quote_id,
                    "quote": update_payload,
                },
                6,
            )
            assert rejected_update.status_code == 200
            rejected_result = json.loads(rejected_update.text.split("data: ", 1)[1])["result"]
            assert rejected_result["isError"] is True
            assert "MCP_DRAFT_REQUIRED" in rejected_update.text

            bad_update = await call(
                "update_quote_draft",
                {
                    "request_id": str(uuid.uuid4()),
                    "quote_id": quote_id,
                    "quote": quote,
                    "unexpected": "rejected",
                },
                7,
            )
            assert bad_update.status_code == 200
            bad_result = json.loads(bad_update.text.split("data: ", 1)[1])["result"]
            assert bad_result["isError"] is True
            assert "MCP_VALIDATION_FAILED" in bad_update.text

            invoice = _invoice_write(customer.id, rate.id, treatment.id).model_dump(mode="json")
            invoice_preview_response = await call(
                "preview_invoice", {"invoice": invoice}, 8
            )
            assert invoice_preview_response.status_code == 200
            invoice_preview = json.loads(
                invoice_preview_response.text.split("data: ", 1)[1]
            )["result"]["structuredContent"]
            assert invoice_preview["total"] == "242.00"

            invoice_request_id = str(uuid.uuid4())
            create_invoice_response = await call(
                "create_invoice_draft",
                {"request_id": invoice_request_id, "invoice": invoice},
                9,
            )
            assert create_invoice_response.status_code == 200
            created_invoice = json.loads(
                create_invoice_response.text.split("data: ", 1)[1]
            )["result"]["structuredContent"]
            invoice_id = created_invoice["invoice"]["id"]
            assert created_invoice["invoice"]["document_kind"] == "STANDARD"
            assert created_invoice["invoice"]["status"] == "DRAFT"
            assert created_invoice["invoice"]["invoice_number"] is None
            assert created_invoice["operation"]["web_url"] == (
                f"https://wire.example.test/invoices/{invoice_id}/edit"
            )

            invoice_replay_response = await call(
                "create_invoice_draft",
                {"request_id": invoice_request_id, "invoice": invoice},
                10,
            )
            invoice_replay = json.loads(
                invoice_replay_response.text.split("data: ", 1)[1]
            )["result"]["structuredContent"]
            assert invoice_replay["operation"]["status"] == "replayed"
            assert invoice_replay["invoice"] == created_invoice["invoice"]

            invoice_read_response = await call(
                "get_invoice", {"invoice_id": invoice_id}, 11
            )
            invoice_read = json.loads(
                invoice_read_response.text.split("data: ", 1)[1]
            )["result"]["structuredContent"]
            assert invoice_read["invoice"]["id"] == invoice_id
            assert invoice_read["web_url"] == created_invoice["operation"]["web_url"]

            invoice_update = dict(invoice)
            invoice_update["terms_text"] = "Updated by MCP"
            invoice_update_response = await call(
                "update_invoice_draft",
                {
                    "request_id": str(uuid.uuid4()),
                    "invoice_id": invoice_id,
                    "invoice": invoice_update,
                },
                12,
            )
            updated_invoice = json.loads(
                invoice_update_response.text.split("data: ", 1)[1]
            )["result"]["structuredContent"]
            assert updated_invoice["invoice"]["terms_text"] == "Updated by MCP"

            async with admin_session_maker() as session:
                row = await session.get(Invoice, uuid.UUID(invoice_id))
                assert row is not None
                row.document_kind = InvoiceDocumentKind.ADVANCE
                await session.commit()
            rejected_invoice_update = await call(
                "update_invoice_draft",
                {
                    "request_id": str(uuid.uuid4()),
                    "invoice_id": invoice_id,
                    "invoice": invoice_update,
                },
                13,
            )
            assert "MCP_DRAFT_REQUIRED" in rejected_invoice_update.text
            rejected_invoice_get = await call(
                "get_invoice", {"invoice_id": invoice_id}, 14
            )
            assert "MCP_VALIDATION_FAILED" in rejected_invoice_get.text


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        (mcp_module.search_customers, {"query": "Acme"}),
        (mcp_module.get_customer, {"customer_id": uuid.uuid4()}),
        (
            mcp_module.create_customer,
            {"request_id": uuid.uuid4(), "customer": CustomerWrite(name="Acme")},
        ),
        (mcp_module.search_catalog, {}),
        (mcp_module.list_document_templates, {}),
        (mcp_module.list_vat_options, {}),
    ],
)
async def test_each_step6_tool_checks_scope_on_every_call(
    tool: object, kwargs: dict[str, object]
) -> None:
    principal = AgentPrincipal(
        uuid.uuid4(), uuid.uuid4(), frozenset({AgentTokenScope.RECEIPT_INTAKE})
    )
    token = mcp_module.agent_principal.set(principal)
    try:
        with pytest.raises(ToolError) as exc:
            await tool(**kwargs)  # type: ignore[operator]
        assert "MCP_INSUFFICIENT_SCOPE" in str(exc.value)
    finally:
        mcp_module.agent_principal.reset(token)

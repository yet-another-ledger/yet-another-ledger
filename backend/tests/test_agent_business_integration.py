"""M15 Step6 Agent customer and read-only context integration coverage."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jai.api import mcp as mcp_module
from jai.db import set_rls_company
from jai.models._enums import DocumentTemplateScope
from jai.models.agent_operation import AgentOperation
from jai.models.agent_token import AgentToken
from jai.models.company import Company
from jai.models.content import DocumentTemplate
from jai.models.customer import Customer
from jai.models.product import Product
from jai.models.user import User
from jai.schemas.agent_token import AgentTokenCreate, AgentTokenScope
from jai.schemas.agent_tools import CreateCustomerInput, SearchCatalogInput, SearchCustomersInput
from jai.schemas.customer import CustomerWrite
from jai.services import agent_business as agent_business_service
from jai.services.agent_business import (
    create_customer_idempotently,
    get_customer_detail,
    list_templates,
    list_vat_options,
    search_catalog,
    search_customers,
)
from jai.services.agent_tokens import AgentPrincipal, issue_token
from jai.services.vat import seed_for_company


@pytest.fixture
async def business_principals(
    admin_session_maker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[AgentPrincipal, AgentPrincipal, uuid.UUID]]:
    company_id, other_company_id, owner_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    scopes = [
        AgentTokenScope.CUSTOMER_READ,
        AgentTokenScope.CUSTOMER_CREATE,
        AgentTokenScope.CATALOG_READ,
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
    principal, _other, _owner_id = business_principals
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

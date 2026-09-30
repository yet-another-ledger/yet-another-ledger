"""M15 customer and read-only business context tools.

These functions keep tenant context and business work in services while
reusing the existing M3/M4 read and customer creation services.
"""

from __future__ import annotations

import uuid
from typing import Literal, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jai.db import set_rls_company
from jai.schemas.agent_tools import (
    AgentWriteResult,
    CatalogListOutput,
    CreateCustomerInput,
    CreateCustomerOutput,
    CustomerListOutput,
    CustomerOutput,
    DocumentTemplateListOutput,
    VatOptionsOutput,
)
from jai.schemas.customer import CustomerRead
from jai.services.agent_operations import AgentOperationValue, run_agent_mutation
from jai.services.agent_tokens import AgentPrincipal
from jai.services.content import list_document_templates
from jai.services.customer import (
    _load_addresses,
    _to_read,
    create_customer,
    get_customer,
    list_customers,
)
from jai.services.invoice import list_invoice_product_options
from jai.services.vat import list_vat_rates, list_vat_treatments


async def search_customers(
    session_maker: async_sessionmaker[AsyncSession],
    principal: AgentPrincipal,
    query: str,
    limit: int,
) -> CustomerListOutput:
    """Search a bounded customer page using the existing M3 search rules."""
    async with session_maker() as session, session.begin():
        await set_rls_company(session, principal.company_id)
        result = await list_customers(
            session,
            principal.company_id,
            q=query,
            limit=limit,
            offset=0,
            sort_by="name",
        )
        return CustomerListOutput(items=result.items)


async def get_customer_detail(
    session_maker: async_sessionmaker[AsyncSession],
    principal: AgentPrincipal,
    customer_id: uuid.UUID,
) -> CustomerOutput | None:
    """Read one customer under company RLS, with its addresses loaded."""
    async with session_maker() as session, session.begin():
        await set_rls_company(session, principal.company_id)
        row = await get_customer(session, customer_id, principal.company_id)
        if row is None:
            return None
        addresses = await _load_addresses(session, row.id)
        return CustomerOutput(customer=_to_read(row, addresses))


async def create_customer_idempotently(
    session_maker: async_sessionmaker[AsyncSession],
    principal: AgentPrincipal,
    request: CreateCustomerInput,
) -> CreateCustomerOutput:
    """Create a customer atomically with the shared M15 request trace."""

    async def mutate(session: AsyncSession) -> AgentOperationValue:
        customer = await create_customer(session, request.customer, principal.company_id)
        return AgentOperationValue("customer", customer.id, customer)

    async with session_maker() as session:
        response = await run_agent_mutation(
            session,
            principal,
            request_id=str(request.request_id),
            operation="create_customer",
            arguments={"customer": request.customer.model_dump(mode="python")},
            source=(
                request.source.model_dump(mode="python") if request.source is not None else None
            ),
            mutate=mutate,
        )
    if response.outcome != "succeeded" or response.target_id is None:
        # The customer service currently has no expected business failure
        # codes. Fail closed if a future callback records an unsuccessful row.
        raise ValueError("Customer creation did not complete.")
    customer = CustomerRead.model_validate(response.result_snapshot)
    return CreateCustomerOutput(
        customer=customer,
        operation=AgentWriteResult(
            operation_id=response.operation_id,
            status=cast(Literal["created", "replayed"], response.status),
            target_id=response.target_id,
        ),
    )


async def search_catalog(
    session_maker: async_sessionmaker[AsyncSession],
    principal: AgentPrincipal,
    query: str,
    limit: int,
) -> CatalogListOutput:
    """Return only the existing customer-safe invoice product projection."""
    async with session_maker() as session, session.begin():
        await set_rls_company(session, principal.company_id)
        result = await list_invoice_product_options(
            session, principal.company_id, q=query or None, limit=limit
        )
        return CatalogListOutput(items=result.items)


async def list_templates(
    session_maker: async_sessionmaker[AsyncSession],
    principal: AgentPrincipal,
) -> DocumentTemplateListOutput:
    """Return at most 100 company-owned templates for document context."""
    async with session_maker() as session, session.begin():
        await set_rls_company(session, principal.company_id)
        rows = await list_document_templates(
            session,
            principal.company_id,
            limit=100,
        )
        return DocumentTemplateListOutput(items=rows)


async def list_vat_options(
    session_maker: async_sessionmaker[AsyncSession],
    principal: AgentPrincipal,
) -> VatOptionsOutput:
    """Return bounded existing company VAT rates and treatments."""
    async with session_maker() as session, session.begin():
        await set_rls_company(session, principal.company_id)
        rates = await list_vat_rates(session, principal.company_id, limit=100)
        treatments = await list_vat_treatments(session, principal.company_id, limit=100)
        return VatOptionsOutput(rates=rates.items, treatments=treatments.items)

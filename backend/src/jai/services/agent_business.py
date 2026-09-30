"""M15 customer and read-only business context tools.

These functions keep tenant context and business work in services while
reusing the existing M3/M4 read and customer creation services.
"""

from __future__ import annotations

import uuid
from typing import Literal, cast

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jai.config import get_settings
from jai.db import set_rls_company
from jai.models.agent_token import AgentToken
from jai.models.company import Company
from jai.schemas.agent_tools import (
    AgentWriteResult,
    CatalogListOutput,
    CreateCustomerInput,
    CreateCustomerOutput,
    CreateInvoiceDraftInput,
    CreateQuoteDraftInput,
    CustomerListOutput,
    CustomerOutput,
    DocumentPreviewOutput,
    DocumentTemplateListOutput,
    InvoiceOutput,
    InvoiceToolSnapshot,
    InvoiceWriteOutput,
    PreviewInvoiceInput,
    PreviewQuoteInput,
    QuoteOutput,
    QuoteToolSnapshot,
    QuoteWriteOutput,
    UpdateInvoiceDraftInput,
    UpdateQuoteDraftInput,
    VatOptionsOutput,
)
from jai.schemas.customer import CustomerRead
from jai.schemas.invoice import InvoiceCalculationRequest, InvoiceWrite
from jai.schemas.quote import QuoteCalculationRequest, QuoteRead, QuoteWrite
from jai.services.agent_operations import (
    AgentOperationFailure,
    AgentOperationValue,
    run_agent_mutation,
)
from jai.services.agent_tokens import AgentPrincipal
from jai.services.content import list_document_templates
from jai.services.customer import (
    _load_addresses,
    _to_read,
    create_customer,
    get_customer,
    list_customers,
)
from jai.services.invoice import (
    _validate_line_fks as _validate_invoice_line_fks,
)
from jai.services.invoice import (
    create_invoice,
    get_invoice,
    list_invoice_product_options,
    update_invoice,
)
from jai.services.pricing import calculate_invoice, calculate_quote
from jai.services.quote import (
    _validate_line_fks,
    create_quote_in_transaction,
    get_quote,
    update_quote_draft_in_transaction,
)
from jai.services.vat import list_vat_rates, list_vat_treatments


def _quote_web_url(quote_id: uuid.UUID) -> str:
    return f"{get_settings().base_url.rstrip('/')}/quotes/{quote_id}/edit"


def _invoice_web_url(invoice_id: uuid.UUID) -> str:
    return f"{get_settings().base_url.rstrip('/')}/invoices/{invoice_id}/edit"


def _quote_calculation_request(body: QuoteWrite) -> QuoteCalculationRequest:
    # QuoteWrite is the frozen complete input contract. Only the fields used by
    # the shared pricing request cross into the calculation service.
    return QuoteCalculationRequest(
        customer_id=body.customer_id,
        quote_date=body.quote_date,
        valid_until=body.valid_until,
        currency=body.currency,
        tax_mode=body.tax_mode,
        amounts_include_vat=body.amounts_include_vat,
        vat_treatment_id=body.vat_treatment_id,
        document_vat_rate_id=body.document_vat_rate_id,
        discount=body.discount,
        lines=body.lines,
    )


async def preview_quote(
    session_maker: async_sessionmaker[AsyncSession],
    principal: AgentPrincipal,
    request: PreviewQuoteInput,
) -> DocumentPreviewOutput:
    async with session_maker() as session, session.begin():
        await set_rls_company(session, principal.company_id)
        await _validate_line_fks(session, principal.company_id, request.quote)
        calculation = await calculate_quote(
            session,
            company_id=principal.company_id,
            request=_quote_calculation_request(request.quote),
        )
        company = await session.scalar(
            select(Company).where(Company.id == principal.company_id)
        )
        if company is None:
            raise ValueError("Company not found.")
        return DocumentPreviewOutput(
            subtotal=calculation.subtotal_excl_vat,
            tax_total=calculation.vat_total,
            total=calculation.total_incl_vat,
            currency=request.quote.currency or company.base_currency,
        )


async def read_quote(
    session_maker: async_sessionmaker[AsyncSession],
    principal: AgentPrincipal,
    quote_id: uuid.UUID,
) -> QuoteOutput | None:
    async with session_maker() as session, session.begin():
        await set_rls_company(session, principal.company_id)
        quote = await get_quote(
            session, quote_id, principal.company_id, apply_expiry=False
        )
        if quote is None:
            return None
        return QuoteOutput(quote=quote, web_url=_quote_web_url(quote.id))


async def _quote_creator_id(session: AsyncSession, principal: AgentPrincipal) -> uuid.UUID:
    creator_id = await session.scalar(
        select(AgentToken.creator_id)
        .where(AgentToken.id == principal.token_id)
        .with_for_update(read=True)
    )
    if creator_id is None:
        raise AgentOperationFailure("MCP_UNAUTHENTICATED")
    return creator_id


async def _quote_result(
    session: AsyncSession, quote_id: uuid.UUID, company_id: uuid.UUID
) -> QuoteRead:
    result = await get_quote(session, quote_id, company_id, apply_expiry=False)
    if result is None:
        raise AgentOperationFailure("MCP_TARGET_NOT_FOUND")
    return result


async def create_quote_draft(
    session_maker: async_sessionmaker[AsyncSession],
    principal: AgentPrincipal,
    request: CreateQuoteDraftInput,
) -> QuoteWriteOutput:
    async def mutate(session: AsyncSession) -> AgentOperationValue:
        try:
            creator_id = await _quote_creator_id(session, principal)
            company = await session.scalar(
                select(Company).where(Company.id == principal.company_id)
            )
            if company is None:
                raise AgentOperationFailure("MCP_VALIDATION_FAILED")
            quote_row = await create_quote_in_transaction(
                session,
                request.quote,
                principal.company_id,
                company.base_currency,
                creator_id,
            )
        except ValueError:
            raise AgentOperationFailure("MCP_VALIDATION_FAILED") from None
        quote = await _quote_result(session, quote_row.id, principal.company_id)
        return AgentOperationValue(
            "quote",
            quote.id,
            QuoteToolSnapshot(quote=quote, web_url=_quote_web_url(quote.id)),
        )

    async with session_maker() as session:
        try:
            response = await run_agent_mutation(
                session,
                principal,
                request_id=str(request.request_id),
                operation="create_quote_draft",
                arguments={"quote": request.quote.model_dump(mode="python")},
                source=request.source.model_dump(mode="python") if request.source else None,
                mutate=mutate,
            )
        except HTTPException:
            raise
    if response.outcome != "succeeded" or response.target_id is None:
        code = response.error_code or "MCP_VALIDATION_FAILED"
        raise HTTPException(status_code=422, detail={"code": code})
    snapshot = QuoteToolSnapshot.model_validate(response.result_snapshot)
    return QuoteWriteOutput(
        quote=snapshot.quote,
        operation=AgentWriteResult(
            operation_id=response.operation_id,
            status=cast(Literal["created", "replayed"], response.status),
            target_id=response.target_id,
            web_url=snapshot.web_url,
        ),
    )


async def update_quote_draft(
    session_maker: async_sessionmaker[AsyncSession],
    principal: AgentPrincipal,
    request: UpdateQuoteDraftInput,
) -> QuoteWriteOutput:
    async def mutate(session: AsyncSession) -> AgentOperationValue:
        company = await session.scalar(
            select(Company).where(Company.id == principal.company_id)
        )
        if company is None:
            raise AgentOperationFailure("MCP_VALIDATION_FAILED")
        try:
            quote_row = await update_quote_draft_in_transaction(
                session,
                request.quote_id,
                request.quote,
                principal.company_id,
                company.base_currency,
            )
        except ValueError as exc:
            if str(exc) == "MCP_DRAFT_REQUIRED":
                raise AgentOperationFailure("MCP_DRAFT_REQUIRED") from None
            raise AgentOperationFailure("MCP_VALIDATION_FAILED") from None
        if quote_row is None:
            raise AgentOperationFailure("MCP_TARGET_NOT_FOUND")
        quote = await _quote_result(session, quote_row.id, principal.company_id)
        return AgentOperationValue(
            "quote",
            quote.id,
            QuoteToolSnapshot(quote=quote, web_url=_quote_web_url(quote.id)),
        )

    async with session_maker() as session:
        try:
            response = await run_agent_mutation(
                session,
                principal,
                request_id=str(request.request_id),
                operation="update_quote_draft",
                arguments={
                    "quote_id": request.quote_id,
                    "quote": request.quote.model_dump(mode="python"),
                },
                source=request.source.model_dump(mode="python") if request.source else None,
                mutate=mutate,
            )
        except HTTPException:
            raise
    if response.outcome != "succeeded" or response.target_id is None:
        code = response.error_code or "MCP_VALIDATION_FAILED"
        raise HTTPException(status_code=422, detail={"code": code})
    snapshot = QuoteToolSnapshot.model_validate(response.result_snapshot)
    return QuoteWriteOutput(
        quote=snapshot.quote,
        operation=AgentWriteResult(
            operation_id=response.operation_id,
            status=cast(Literal["created", "replayed"], response.status),
            target_id=response.target_id,
            web_url=snapshot.web_url,
        ),
    )


def _invoice_calculation_request(body: InvoiceWrite) -> InvoiceCalculationRequest:
    return InvoiceCalculationRequest(
        customer_id=body.customer_id,
        invoice_date=body.invoice_date,
        due_date=body.due_date,
        currency=body.currency,
        tax_mode=body.tax_mode,
        amounts_include_vat=body.amounts_include_vat,
        vat_treatment_id=body.vat_treatment_id,
        document_vat_rate_id=body.document_vat_rate_id,
        discount=body.discount,
        lines=body.lines,
    )


async def preview_invoice(
    session_maker: async_sessionmaker[AsyncSession],
    principal: AgentPrincipal,
    request: PreviewInvoiceInput,
) -> DocumentPreviewOutput:
    async with session_maker() as session, session.begin():
        await set_rls_company(session, principal.company_id)
        await _validate_invoice_line_fks(session, principal.company_id, request.invoice)
        calculation = await calculate_invoice(
            session,
            company_id=principal.company_id,
            request=_invoice_calculation_request(request.invoice),
        )
        return DocumentPreviewOutput(
            subtotal=calculation.subtotal_excl_vat,
            tax_total=calculation.vat_total,
            total=calculation.total_incl_vat,
            currency=request.invoice.currency
            or await _company_currency(session, principal.company_id),
        )


async def _company_currency(session: AsyncSession, company_id: uuid.UUID) -> str:
    currency = await session.scalar(select(Company.base_currency).where(Company.id == company_id))
    if currency is None:
        raise AgentOperationFailure("MCP_VALIDATION_FAILED")
    return currency


async def read_invoice(
    session_maker: async_sessionmaker[AsyncSession],
    principal: AgentPrincipal,
    invoice_id: uuid.UUID,
) -> InvoiceOutput | None:
    async with session_maker() as session, session.begin():
        await set_rls_company(session, principal.company_id)
        invoice = await get_invoice(session, invoice_id, principal.company_id)
        if invoice is None:
            return None
        if invoice.document_kind.value != "STANDARD":
            raise AgentOperationFailure("MCP_VALIDATION_FAILED")
        return InvoiceOutput(invoice=invoice, web_url=_invoice_web_url(invoice.id))


async def create_invoice_draft(
    session_maker: async_sessionmaker[AsyncSession],
    principal: AgentPrincipal,
    request: CreateInvoiceDraftInput,
) -> InvoiceWriteOutput:
    async def mutate(session: AsyncSession) -> AgentOperationValue:
        try:
            creator_id = await _quote_creator_id(session, principal)
            company = await session.scalar(
                select(Company).where(Company.id == principal.company_id)
            )
            if company is None:
                raise AgentOperationFailure("MCP_VALIDATION_FAILED")
            invoice = await create_invoice(
                session,
                request.invoice,
                principal.company_id,
                company.base_currency,
                creator_id,
                commit=False,
            )
        except ValueError:
            raise AgentOperationFailure("MCP_VALIDATION_FAILED") from None
        if invoice.document_kind.value != "STANDARD":
            raise AgentOperationFailure("MCP_VALIDATION_FAILED")
        return AgentOperationValue(
            "invoice",
            invoice.id,
            InvoiceToolSnapshot(invoice=invoice, web_url=_invoice_web_url(invoice.id)),
        )

    async with session_maker() as session:
        response = await run_agent_mutation(
            session,
            principal,
            request_id=str(request.request_id),
            operation="create_invoice_draft",
            arguments={"invoice": request.invoice.model_dump(mode="python")},
            source=request.source.model_dump(mode="python") if request.source else None,
            mutate=mutate,
        )
    if response.outcome != "succeeded" or response.target_id is None:
        raise HTTPException(
            status_code=422,
            detail={"code": response.error_code or "MCP_VALIDATION_FAILED"},
        )
    snapshot = InvoiceToolSnapshot.model_validate(response.result_snapshot)
    return InvoiceWriteOutput(
        invoice=snapshot.invoice,
        operation=AgentWriteResult(
            operation_id=response.operation_id,
            status=cast(Literal["created", "replayed"], response.status),
            target_id=response.target_id,
            web_url=snapshot.web_url,
        ),
    )


async def update_invoice_draft(
    session_maker: async_sessionmaker[AsyncSession],
    principal: AgentPrincipal,
    request: UpdateInvoiceDraftInput,
) -> InvoiceWriteOutput:
    async def mutate(session: AsyncSession) -> AgentOperationValue:
        company = await session.scalar(
            select(Company).where(Company.id == principal.company_id)
        )
        if company is None:
            raise AgentOperationFailure("MCP_VALIDATION_FAILED")
        try:
            invoice = await update_invoice(
                session,
                request.invoice_id,
                request.invoice,
                principal.company_id,
                company.base_currency,
                actor_user_id=await _quote_creator_id(session, principal),
                commit=False,
                require_standard_draft=True,
            )
        except ValueError as exc:
            if str(exc) == "MCP_DRAFT_REQUIRED":
                raise AgentOperationFailure("MCP_DRAFT_REQUIRED") from None
            raise AgentOperationFailure("MCP_VALIDATION_FAILED") from None
        if invoice is None:
            raise AgentOperationFailure("MCP_TARGET_NOT_FOUND")
        return AgentOperationValue(
            "invoice",
            invoice.id,
            InvoiceToolSnapshot(invoice=invoice, web_url=_invoice_web_url(invoice.id)),
        )

    async with session_maker() as session:
        response = await run_agent_mutation(
            session,
            principal,
            request_id=str(request.request_id),
            operation="update_invoice_draft",
            arguments={
                "invoice_id": request.invoice_id,
                "invoice": request.invoice.model_dump(mode="python"),
            },
            source=request.source.model_dump(mode="python") if request.source else None,
            mutate=mutate,
        )
    if response.outcome != "succeeded" or response.target_id is None:
        raise HTTPException(
            status_code=422,
            detail={"code": response.error_code or "MCP_VALIDATION_FAILED"},
        )
    snapshot = InvoiceToolSnapshot.model_validate(response.result_snapshot)
    return InvoiceWriteOutput(
        invoice=snapshot.invoice,
        operation=AgentWriteResult(
            operation_id=response.operation_id,
            status=cast(Literal["created", "replayed"], response.status),
            target_id=response.target_id,
            web_url=snapshot.web_url,
        ),
    )


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

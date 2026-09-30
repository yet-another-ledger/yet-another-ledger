"""Typed input and output contracts for the frozen M15 MCP tool surface."""

from __future__ import annotations

import uuid
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from jai.schemas.content import DocumentTemplateRead
from jai.schemas.customer import CustomerRead, CustomerWrite
from jai.schemas.invoice import InvoiceRead, InvoiceWrite, ProductInvoiceOptionRead
from jai.schemas.quote import QuoteRead, QuoteWrite
from jai.schemas.vat import VatRateRead, VatTreatmentRead

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
RequestId = Annotated[uuid.UUID, Field(description="Stable UUID for idempotent writes.")]


class AgentSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: NonEmpty
    chat_id: str | None = None
    message_id: str | None = None


class AgentWriteResult(BaseModel):
    operation_id: uuid.UUID
    status: Literal["created", "replayed"]
    target_id: uuid.UUID
    web_url: str | None = None


class AgentErrorCode(StrEnum):
    """Stable machine-readable error vocabulary for MCP calls."""

    UNAUTHENTICATED = "MCP_UNAUTHENTICATED"
    INSUFFICIENT_SCOPE = "MCP_INSUFFICIENT_SCOPE"
    VALIDATION_FAILED = "MCP_VALIDATION_FAILED"
    TARGET_NOT_FOUND = "MCP_TARGET_NOT_FOUND"
    TARGET_AMBIGUOUS = "MCP_TARGET_AMBIGUOUS"
    DRAFT_REQUIRED = "MCP_DRAFT_REQUIRED"
    REQUEST_ID_CONFLICT = "MCP_REQUEST_ID_CONFLICT"
    HOST_REJECTED = "MCP_HOST_REJECTED"
    ORIGIN_REJECTED = "MCP_ORIGIN_REJECTED"


class AgentError(BaseModel):
    code: AgentErrorCode
    message: str


class ReceiptFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: NonEmpty
    mime_type: NonEmpty
    data_base64: NonEmpty


class SubmitReceiptInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: RequestId
    source: AgentSource
    language: Literal["en", "zh"] = "en"
    files: Annotated[list[ReceiptFileInput], Field(min_length=1, max_length=5)]


class SubmitReceiptOutput(AgentWriteResult):
    inbox_id: uuid.UUID
    ai_state: Literal["NOT_RUN", "RUNNING", "READY", "UNAVAILABLE", "FAILED"]
    ai_error_code: str | None = None
    review_url: str


class GetReceiptStatusInput(BaseModel):
    inbox_id: uuid.UUID


class ReceiptStatusOutput(BaseModel):
    inbox_id: uuid.UUID
    status: Literal["PENDING", "CONFIRMED"]
    ai_state: Literal["NOT_RUN", "RUNNING", "READY", "UNAVAILABLE", "FAILED"]
    ai_error_code: str | None = None
    review_url: str


class SearchCustomersInput(BaseModel):
    query: NonEmpty
    limit: int = Field(default=20, ge=1, le=100)


class GetCustomerInput(BaseModel):
    customer_id: uuid.UUID


class CreateCustomerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: RequestId
    source: AgentSource | None = None
    customer: CustomerWrite


class CustomerListOutput(BaseModel):
    items: list[CustomerRead]


class CustomerOutput(BaseModel):
    customer: CustomerRead


class CreateCustomerOutput(BaseModel):
    customer: CustomerRead
    operation: AgentWriteResult


class SearchCatalogInput(BaseModel):
    query: str = ""
    limit: int = Field(default=50, ge=1, le=100)


class EmptyInput(BaseModel):
    """Explicit empty object for bounded list tools without filters."""

    model_config = ConfigDict(extra="forbid")


class CatalogListOutput(BaseModel):
    items: list[ProductInvoiceOptionRead]


class DocumentTemplateListOutput(BaseModel):
    items: list[DocumentTemplateRead]


class VatOptionsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VatOptionsOutput(BaseModel):
    rates: list[VatRateRead]
    treatments: list[VatTreatmentRead]


class PreviewQuoteInput(BaseModel):
    quote: QuoteWrite


class CreateQuoteDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: RequestId
    source: AgentSource | None = None
    quote: QuoteWrite


class GetQuoteInput(BaseModel):
    quote_id: uuid.UUID


class UpdateQuoteDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: RequestId
    source: AgentSource | None = None
    quote_id: uuid.UUID
    quote: QuoteWrite


class PreviewInvoiceInput(BaseModel):
    invoice: InvoiceWrite


class CreateInvoiceDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: RequestId
    source: AgentSource | None = None
    invoice: InvoiceWrite


class GetInvoiceInput(BaseModel):
    invoice_id: uuid.UUID


class UpdateInvoiceDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: RequestId
    source: AgentSource | None = None
    invoice_id: uuid.UUID
    invoice: InvoiceWrite


class DocumentPreviewOutput(BaseModel):
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    currency: str


class QuoteOutput(BaseModel):
    quote: QuoteRead


class QuoteWriteOutput(BaseModel):
    quote: QuoteRead
    operation: AgentWriteResult


class InvoiceOutput(BaseModel):
    invoice: InvoiceRead


class InvoiceWriteOutput(BaseModel):
    invoice: InvoiceRead
    operation: AgentWriteResult

"""Contract for manually maintained external sales drafts."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ExternalSalesSourceKind(StrEnum):
    PLATFORM = "PLATFORM"
    ENERGY = "ENERGY"
    OTHER = "OTHER"


class ExternalSalesStatus(StrEnum):
    PENDING = "PENDING"
    POSTED = "POSTED"
    VOID = "VOID"


class ExternalSalesDocumentKind(StrEnum):
    SALE = "SALE"
    CREDIT = "CREDIT"
    SETTLEMENT_ADJUSTMENT = "SETTLEMENT_ADJUSTMENT"


class ExternalSalesSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    kind: ExternalSalesSourceKind
    default_include_in_pl: bool | None = None
    active: bool = True

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Source name cannot be blank.")
        return value


class ExternalSalesSourceRead(ExternalSalesSourceInput):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class ExternalSalesSourceListRead(BaseModel):
    items: list[ExternalSalesSourceRead]
    total: int
    limit: int
    offset: int


class ExternalSalesTaxInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    treatment_id: uuid.UUID | None = None
    rate_id: uuid.UUID | None = None
    net_amount: Decimal | None = Field(default=None, max_digits=14, decimal_places=3)
    vat_amount: Decimal | None = Field(default=None, max_digits=14, decimal_places=3)


class ExternalSalesTaxRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    treatment_id: uuid.UUID | None
    treatment_code: str | None
    treatment_label: str | None
    rate_id: uuid.UUID | None
    rate_label: str | None
    rate_percent: Decimal | None
    net_amount: Decimal | None
    vat_amount: Decimal | None


class ExternalSalesCalculateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


class ExternalSalesConfirmRequest(ExternalSalesCalculateRequest):
    acknowledge_vat_mismatch: bool = False
    mismatch_ack_reason: str | None = Field(default=None, max_length=2000)


class ExternalSalesCalculation(BaseModel):
    sale_id: uuid.UUID
    expected_version: int
    original_net_total: Decimal
    original_vat_total: Decimal
    derived_vat_total: Decimal
    mismatch: bool
    mismatch_row_indices: list[int]
    warnings: list[str]


class ExternalSalesPostingRead(BaseModel):
    id: uuid.UUID
    version: int
    actor_id: uuid.UUID | None
    created_at: datetime
    snapshot: dict[str, object]


class ExternalSalesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: uuid.UUID | None = None
    original_number: str | None = Field(default=None, max_length=255)
    document_kind: ExternalSalesDocumentKind = ExternalSalesDocumentKind.SALE
    related_external_sale_id: uuid.UUID | None = None
    historical_original_number: str | None = Field(default=None, max_length=255)
    historical_original_explanation: str | None = None
    document_date: date | None = None
    covered_from: date | None = None
    covered_to: date | None = None
    counterparty: str | None = Field(default=None, max_length=255)
    description: str | None = None
    include_in_pl: bool | None = None
    tax_summaries: list[ExternalSalesTaxInput] = Field(default_factory=list, max_length=20)

    @field_validator("counterparty", "description")
    @classmethod
    def trim_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class ExternalSalesFileRead(BaseModel):
    id: uuid.UUID
    filename: str | None
    mime_type: str
    byte_size: int
    sha256: str
    duplicate_warning: bool
    created_at: datetime


class ExternalSalesRead(BaseModel):
    source_id: uuid.UUID | None
    original_number: str | None
    document_kind: ExternalSalesDocumentKind
    related_external_sale_id: uuid.UUID | None
    historical_original_number: str | None
    historical_original_explanation: str | None
    document_date: date | None
    covered_from: date | None
    covered_to: date | None
    counterparty: str | None
    description: str | None
    include_in_pl: bool | None
    id: uuid.UUID
    status: ExternalSalesStatus
    source_name: str | None
    tax_summaries: list[ExternalSalesTaxRead]
    files: list[ExternalSalesFileRead]
    created_at: datetime
    updated_at: datetime
    version: int
    posted_at: datetime | None = Field(...)
    posted_by: uuid.UUID | None = Field(...)
    posted_source_name: str | None = Field(...)
    original_net_total: Decimal | None = Field(...)
    original_vat_total: Decimal | None = Field(...)
    mismatch_ack_reason: str | None = Field(...)
    postings: list[ExternalSalesPostingRead]


class ExternalSalesRevisionTaxInput(ExternalSalesTaxInput):
    # Retained row identity is scoped to this external sale by the service.
    tax_summary_id: uuid.UUID | None = None


class ExternalSalesRevisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)
    document_date: date | None = None
    covered_from: date | None = None
    covered_to: date | None = None
    counterparty: str | None = Field(default=None, max_length=255)
    description: str | None = None
    include_in_pl: bool = False
    tax_summaries: list[ExternalSalesRevisionTaxInput] | None = Field(default=None, max_length=20)
    acknowledge_vat_mismatch: bool = False
    mismatch_ack_reason: str | None = Field(default=None, max_length=2000)

    @field_validator("reason")
    @classmethod
    def require_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A revision reason is required.")
        return value

    @field_validator("counterparty", "description")
    @classmethod
    def trim_revision_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class ExternalSalesVoidInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def require_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A void reason is required.")
        return value


class ExternalSalesRevisionRead(BaseModel):
    id: uuid.UUID
    version: int
    action: str
    reason: str
    actor_id: uuid.UUID | None
    created_at: datetime
    before_snapshot: dict[str, object]
    after_snapshot: dict[str, object]


class ExternalSalesRevisionListRead(BaseModel):
    items: list[ExternalSalesRevisionRead]
    total: int
    offset: int = 0
    limit: int = 50


class ExternalSalesRevisionPreview(BaseModel):
    sale_id: uuid.UUID
    expected_version: int
    old_period: str
    new_period: str
    old_net_total: Decimal
    new_net_total: Decimal
    old_vat_total: Decimal
    new_vat_total: Decimal
    warnings: list[str]


class ExternalSalesListRead(BaseModel):
    items: list[ExternalSalesRead]
    total: int
    limit: int
    offset: int


class ExternalSalesConflict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    detail: ExternalSalesConflictDetail


class ExternalSalesConflictDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    detail: str
    external_sale_id: uuid.UUID | None = None
    current_version: int | None = None

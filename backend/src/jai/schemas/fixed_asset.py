"""Annual fixed-asset use and correction contracts."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from jai.schemas.expense import ExpenseCostPreviewRequest


class FixedAssetResidualEstimateRequest(BaseModel):
    """Minimum asset identity plus a saved or unsaved purchase cost source."""

    expense_id: uuid.UUID | None = None
    cost_inputs: ExpenseCostPreviewRequest | None = None
    product_name: str = Field(min_length=1, max_length=200)
    brand: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=100)
    useful_life_years: int = Field(ge=1, le=100)
    condition: str | None = Field(default=None, max_length=500)
    usage: str | None = Field(default=None, max_length=500)

    @field_validator("product_name", "brand", "model", "condition", "usage")
    @classmethod
    def trim_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("product_name")
    @classmethod
    def require_product_name(cls, value: str) -> str:
        if not value:
            raise ValueError("Product name is required.")
        return value

    @model_validator(mode="after")
    def require_one_cost_source(self) -> FixedAssetResidualEstimateRequest:
        if (self.expense_id is None) == (self.cost_inputs is None):
            raise ValueError("Provide exactly one saved expense or unsaved cost input.")
        return self


class FixedAssetResidualEstimateRead(BaseModel):
    status: Literal["suggested", "insufficient_data"]
    suggested_residual_value: Decimal | None = None
    reason: str
    assumptions: list[str] = Field(default_factory=list)
    acquisition_cost: Decimal
    currency_code: str = Field(min_length=3, max_length=3)


class FixedAssetAnnualUseInput(BaseModel):
    estimated_business_percentage: Decimal = Field(ge=0, le=100, max_digits=6, decimal_places=3)


class FixedAssetAnnualConfirmInput(BaseModel):
    actual_business_percentage: Decimal = Field(ge=0, le=100, max_digits=6, decimal_places=3)
    preview_token: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def trim_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A confirmation reason is required.")
        return value


class FixedAssetAnnualConfirmPreviewInput(BaseModel):
    actual_business_percentage: Decimal = Field(ge=0, le=100, max_digits=6, decimal_places=3)


class FixedAssetAnnualPeriodDelta(BaseModel):
    period: str
    before_business_depreciation: Decimal
    after_business_depreciation: Decimal
    pnl_delta: Decimal


class FixedAssetAnnualConfirmPreview(BaseModel):
    asset_id: uuid.UUID
    year: int
    before_business_percentage: Decimal
    after_business_percentage: Decimal
    period_deltas: list[FixedAssetAnnualPeriodDelta]
    monthly_period_deltas: list[FixedAssetAnnualPeriodDelta] = Field(default_factory=list)
    affected_periods: list[str]
    preview_token: str


class FixedAssetAnnualUseRead(BaseModel):
    asset_id: uuid.UUID
    year: int
    estimated_business_percentage: Decimal
    actual_business_percentage: Decimal | None
    effective_business_percentage: Decimal
    provisional: bool
    version: int
    confirmed_at: datetime | None
    full_depreciation: Decimal
    private_use_reduction: Decimal
    net_pnl_effect: Decimal


class FixedAssetAnnualRevisionInput(BaseModel):
    expected_version: int = Field(ge=1)
    business_percentage: Decimal = Field(ge=0, le=100, max_digits=6, decimal_places=3)
    reason: str = Field(min_length=1, max_length=2000)
    preview_token: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")

    @field_validator("reason")
    @classmethod
    def trim_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A correction reason is required.")
        return value


class FixedAssetAnnualRevisionPreview(BaseModel):
    asset_id: uuid.UUID
    year: int
    expected_version: int
    before: FixedAssetAnnualUseRead
    after_business_percentage: Decimal
    after_net_pnl_effect: Decimal
    pnl_delta: Decimal
    affected_periods: list[str]
    period_deltas: list[FixedAssetAnnualPeriodDelta] = Field(default_factory=list)
    monthly_period_deltas: list[FixedAssetAnnualPeriodDelta] = Field(default_factory=list)
    preview_token: str


class FixedAssetAnnualRevisionRead(BaseModel):
    version: int
    reason: str
    actor_id: uuid.UUID | None
    created_at: datetime
    before_snapshot: dict[str, object]
    after_snapshot: dict[str, object]


class FixedAssetServiceDateRevisionInput(BaseModel):
    expected_version: int = Field(ge=1)
    in_service_date: date
    reason: str = Field(min_length=1, max_length=2000)
    preview_token: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")

    @field_validator("reason")
    @classmethod
    def trim_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A correction reason is required.")
        return value


class FixedAssetServiceDateRevisionPreview(BaseModel):
    asset_id: uuid.UUID
    expected_version: int
    before_in_service_date: date | None
    after_in_service_date: date
    affected_periods: list[str]
    before_pnl_effect: Decimal
    after_pnl_effect: Decimal
    pnl_delta: Decimal
    preview_token: str


class FixedAssetRevisionRead(BaseModel):
    version: int
    reason: str
    actor_id: uuid.UUID | None
    created_at: datetime
    before_snapshot: dict[str, object]
    after_snapshot: dict[str, object]
    affected_periods: list[str]

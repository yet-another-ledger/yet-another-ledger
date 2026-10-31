"""Contract for registered home-battery private-use adjustments."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BatteryAssetPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    acquisition_expense_id: uuid.UUID
    in_service_date: date
    owner_confirms_ordinary_vat_full_deduction: bool


class BatteryConflictCode(StrEnum):
    ACQUISITION_ALREADY_REGISTERED = "BATTERY_ACQUISITION_ALREADY_REGISTERED"
    EXPENSE_INELIGIBLE = "BATTERY_EXPENSE_INELIGIBLE"
    OWNER_CONFIRMATION_REQUIRED = "BATTERY_OWNER_CONFIRMATION_REQUIRED"
    HISTORICAL_ACK_REQUIRED = "BATTERY_HISTORICAL_ACK_REQUIRED"
    VERSION_CONFLICT = "BATTERY_VERSION_CONFLICT"
    YEAR_NOT_APPLICABLE = "BATTERY_YEAR_NOT_APPLICABLE"
    ANNUAL_INPUT_MISSING = "BATTERY_ANNUAL_INPUT_MISSING"
    ANNUAL_CONFIRMED = "BATTERY_ANNUAL_CONFIRMED"
    ANNUAL_ALREADY_CONFIRMED = "BATTERY_ANNUAL_ALREADY_CONFIRMED"
    EXPENSE_PROTECTED = "BATTERY_EXPENSE_PROTECTED"
    EVIDENCE_INVALID = "BATTERY_EVIDENCE_INVALID"


class BatteryAssetPreview(BaseModel):
    acquisition_expense_id: uuid.UUID
    eligible: bool
    conflict_code: BatteryConflictCode | None = None
    in_service_date: date
    applicable_years: list[int] = Field(default_factory=list)
    affected_historical_periods: list[str] = Field(default_factory=list)
    net_basis: Decimal | None = None
    purchase_vat: Decimal | None = None
    purchase_vat_rate_percent: Decimal | None = None
    warnings: list[str] = Field(default_factory=list)


class BatteryConflictDetail(BaseModel):
    code: BatteryConflictCode
    detail: str
    current_version: int | None = None


class BatteryConflictResponse(BaseModel):
    detail: BatteryConflictDetail


class BatteryAssetCreateRequest(BatteryAssetPreviewRequest):
    acknowledge_affected_periods: bool = False


class BatteryAssetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    acquisition_expense_id: uuid.UUID
    acquisition_expense_date: date
    in_service_date: date
    applicable_years: list[int]
    net_basis: Decimal
    purchase_vat: Decimal
    purchase_vat_rate_percent: Decimal
    currency: str
    rule_version: int
    version: int
    ordinary_vat_full_deduction_confirmed: bool
    owner_confirmed_at: datetime
    registered_by: uuid.UUID | None
    historical_acknowledged_periods: list[str]
    historical_acknowledged_at: datetime | None
    created_at: datetime


class BatteryAssetListRead(BaseModel):
    items: list[BatteryAssetRead]
    total: int
    limit: int
    offset: int


class BatteryAnnualUseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int | None = Field(default=None, ge=1)
    private_use_percentage: Decimal | None = Field(
        default=None, ge=0, le=100, max_digits=6, decimal_places=3
    )
    basis_note: str = Field(min_length=1, max_length=4000)

    @field_validator("basis_note")
    @classmethod
    def trim_basis_note(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A calculation basis note is required.")
        return value


class BatteryEvidenceRead(BaseModel):
    id: uuid.UUID
    filename: str | None
    media_type: str
    byte_size: int
    sha256: str
    created_at: datetime


class BatteryMissingCode(StrEnum):
    MISSING = "MISSING"
    PERCENTAGE_MISSING = "PERCENTAGE_MISSING"
    UNCONFIRMED = "UNCONFIRMED"
    RECONFIRMATION_REQUIRED = "RECONFIRMATION_REQUIRED"


class BatteryAnnualMissingInput(BaseModel):
    asset_id: uuid.UUID
    year: int
    code: BatteryMissingCode


class BatteryAnnualUseRead(BaseModel):
    id: uuid.UUID
    asset_id: uuid.UUID
    year: int
    version: int
    private_use_percentage: Decimal | None
    basis_note: str | None
    confirmed: bool
    confirmed_at: datetime | None
    calculation_base: Decimal | None
    calculation_vat: Decimal | None
    asset_version_used: int | None
    evidence: list[BatteryEvidenceRead] = Field(default_factory=list)


class BatteryAnnualCalculateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


class BatteryAnnualCalculation(BaseModel):
    asset_id: uuid.UUID
    year: int
    expected_version: int
    private_use_percentage: Decimal
    calculation_base: Decimal
    calculation_vat: Decimal
    report_quarter: int
    warnings: list[str] = Field(default_factory=list)


class BatteryAnnualConfirmRequest(BatteryAnnualCalculateRequest):
    """Confirm using server-recomputed values for the expected input version."""

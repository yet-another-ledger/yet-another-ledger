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
    REVISION_REQUIRED = "BATTERY_REVISION_REQUIRED"
    REVISION_NO_CHANGES = "BATTERY_REVISION_NO_CHANGES"
    REVISION_PERIOD_ACK_REQUIRED = "BATTERY_REVISION_PERIOD_ACK_REQUIRED"
    PREVIEW_STALE = "BATTERY_PREVIEW_STALE"
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


class BatteryExpenseFinancialSnapshot(BaseModel):
    """Expense values affected by a controlled asset correction."""

    expense_date: date
    currency: str
    exchange_rate: Decimal
    net_amount: Decimal
    vat_amount: Decimal
    gross_amount: Decimal
    base_net_amount: Decimal
    base_vat_amount: Decimal
    base_gross_amount: Decimal
    vat_rate_id: uuid.UUID | None
    vat_rate_percent: Decimal
    vat_rate_label: str


class BatteryAnnualRevisionPreviewItem(BaseModel):
    year: int
    current_version: int | None
    current_confirmed: bool
    current_private_use_percentage: Decimal | None
    current_calculation_base: Decimal | None
    current_calculation_vat: Decimal | None
    proposed_calculation_base: Decimal | None
    proposed_calculation_vat: Decimal | None
    requires_reconfirmation: bool


class BatteryAssetRevisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)
    acquisition_expense_date: date | None = None
    in_service_date: date | None = None
    net_basis: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=3)
    purchase_vat: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=3)
    vat_rate_id: uuid.UUID | None = None
    expected_rate_snapshot: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    expected_annual_versions: dict[int, int] = Field(default_factory=dict)
    acknowledge_affected_periods: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("reason")
    @classmethod
    def require_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A revision reason is required.")
        return value


class BatteryAssetRevisionPreview(BaseModel):
    rate_snapshot: str | None = None
    asset_id: uuid.UUID
    expected_version: int
    before: BatteryAssetRead
    after: BatteryAssetRead
    expense_before: BatteryExpenseFinancialSnapshot
    expense_after: BatteryExpenseFinancialSnapshot
    affected_periods: list[str]
    annual_previews: list[BatteryAnnualRevisionPreviewItem]
    warnings: list[str] = Field(default_factory=list)


class BatteryAssetRevisionRead(BaseModel):
    id: uuid.UUID
    version: int
    reason: str
    actor_id: uuid.UUID | None
    created_at: datetime
    affected_periods: list[str]
    before_snapshot: dict[str, object]
    after_snapshot: dict[str, object]


class BatteryAssetRevisionListRead(BaseModel):
    items: list[BatteryAssetRevisionRead]
    total: int
    offset: int = 0
    limit: int = 50


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


class BatteryAnnualRevisionInput(BatteryAnnualUseInput):
    expected_version: int = Field(ge=1)
    expected_asset_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)
    acknowledge_affected_periods: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("reason")
    @classmethod
    def require_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A revision reason is required.")
        return value


class BatteryAnnualRevisionPreview(BaseModel):
    asset_id: uuid.UUID
    year: int
    expected_version: int
    expected_asset_version: int
    current_private_use_percentage: Decimal | None
    current_calculation_base: Decimal | None
    current_calculation_vat: Decimal | None
    proposed_private_use_percentage: Decimal | None
    proposed_calculation_base: Decimal | None
    proposed_calculation_vat: Decimal | None
    affected_periods: list[str]
    warnings: list[str] = Field(default_factory=list)


class BatteryAnnualRevisionRead(BaseModel):
    id: uuid.UUID
    version: int
    reason: str
    actor_id: uuid.UUID | None
    created_at: datetime
    before_snapshot: dict[str, object]
    after_snapshot: dict[str, object]


class BatteryAnnualRevisionListRead(BaseModel):
    items: list[BatteryAnnualRevisionRead]
    total: int
    offset: int = 0
    limit: int = 50


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
    expected_asset_version: int = Field(ge=1)


class BatteryAnnualCalculation(BaseModel):
    asset_id: uuid.UUID
    year: int
    expected_version: int
    expected_asset_version: int
    private_use_percentage: Decimal
    calculation_base: Decimal
    calculation_vat: Decimal
    report_quarter: int
    warnings: list[str] = Field(default_factory=list)


class BatteryAnnualConfirmRequest(BatteryAnnualCalculateRequest):
    """Confirm using server-recomputed values for the expected input version."""

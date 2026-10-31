"""Company-owned home battery asset and annual-use snapshots."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jai.db import Base


class BatteryAsset(Base):
    __tablename__ = "battery_asset"
    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_battery_asset_company_id"),
        UniqueConstraint(
            "company_id", "acquisition_expense_id", name="uq_battery_asset_acquisition"
        ),
        ForeignKeyConstraint(
            ["company_id", "acquisition_expense_id"],
            ["expense.company_id", "expense.id"],
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company.id", ondelete="RESTRICT"), nullable=False
    )
    acquisition_expense_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    acquisition_expense_date: Mapped[date] = mapped_column(Date, nullable=False)
    in_service_date: Mapped[date] = mapped_column(Date, nullable=False)
    net_basis: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False)
    purchase_vat: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False)
    purchase_vat_rate_percent: Mapped[Decimal] = mapped_column(Numeric(6, 3), nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    ordinary_vat_full_deduction_confirmed: Mapped[bool] = mapped_column(nullable=False)
    owner_confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    registered_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    historical_acknowledged_periods: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    historical_acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    annual_uses: Mapped[list[BatteryAnnualUse]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, order_by="BatteryAnnualUse.year"
    )


class BatteryAnnualUse(Base):
    __tablename__ = "battery_annual_use"
    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_battery_annual_use_company_id"),
        UniqueConstraint("company_id", "asset_id", "year", name="uq_battery_asset_year"),
        ForeignKeyConstraint(
            ["company_id", "asset_id"],
            ["battery_asset.company_id", "battery_asset.id"],
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company.id", ondelete="RESTRICT"), nullable=False
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    private_use_percentage: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    basis_note: Mapped[str | None] = mapped_column(Text)
    confirmed: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    calculation_base: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    calculation_vat: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    asset_version_used: Mapped[int | None] = mapped_column(Integer)
    evidence: Mapped[list[BatteryAnnualEvidence]] = relationship(
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="BatteryAnnualEvidence.created_at",
    )


class BatteryAnnualEvidence(Base):
    __tablename__ = "battery_annual_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "annual_use_id"],
            ["battery_annual_use.company_id", "battery_annual_use.id"],
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company.id", ondelete="RESTRICT"), nullable=False
    )
    annual_use_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    filename: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(Text, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class BatteryAnnualConfirmation(Base):
    __tablename__ = "battery_annual_confirmation"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "annual_use_id", "version", name="uq_battery_confirmation_version"
        ),
        ForeignKeyConstraint(
            ["company_id", "annual_use_id"],
            ["battery_annual_use.company_id", "battery_annual_use.id"],
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company.id", ondelete="RESTRICT"), nullable=False
    )
    annual_use_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL")
    )
    private_use_percentage: Mapped[Decimal] = mapped_column(Numeric(6, 3), nullable=False)
    calculation_base: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False)
    calculation_vat: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False)
    asset_version_used: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

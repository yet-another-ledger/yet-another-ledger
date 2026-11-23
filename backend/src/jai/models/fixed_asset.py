"""Fixed assets recognized from one confirmed purchase Expense."""

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
from sqlalchemy.orm import Mapped, mapped_column

from jai.db import Base


class FixedAsset(Base):
    __tablename__ = "fixed_asset"
    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_fixed_asset_company_id"),
        UniqueConstraint("company_id", "acquisition_expense_id", name="uq_fixed_asset_expense"),
        ForeignKeyConstraint(
            ["company_id", "acquisition_expense_id"],
            ["expense.company_id", "expense.id"],
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    acquisition_expense_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    acquisition_date: Mapped[date] = mapped_column(Date, nullable=False)
    in_service_date: Mapped[date | None] = mapped_column(Date)
    acquisition_cost: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False)
    residual_value: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False)
    useful_life_years: Mapped[int] = mapped_column(Integer, nullable=False)
    business_percentage: Mapped[Decimal] = mapped_column(
        Numeric(6, 3), nullable=False, server_default=text("100")
    )
    use_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'FULL_BUSINESS'")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class FixedAssetAnnualUse(Base):
    """Per-calendar-year business-use estimate and optional actual confirmation."""

    __tablename__ = "fixed_asset_annual_use"
    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_fixed_asset_annual_use_company_id"),
        UniqueConstraint("company_id", "asset_id", "year", name="uq_fixed_asset_use_year"),
        ForeignKeyConstraint(
            ["company_id", "asset_id"],
            ["fixed_asset.company_id", "fixed_asset.id"],
            ondelete="CASCADE",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company.id", ondelete="RESTRICT"), nullable=False
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_business_percentage: Mapped[Decimal] = mapped_column(Numeric(6, 3), nullable=False)
    actual_business_percentage: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class FixedAssetAnnualRevision(Base):
    """Append-only reasoned corrections to annual-use inputs."""

    __tablename__ = "fixed_asset_annual_revision"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "annual_use_id", "version", name="uq_fixed_asset_use_revision_version"
        ),
        ForeignKeyConstraint(
            ["company_id", "annual_use_id"],
            ["fixed_asset_annual_use.company_id", "fixed_asset_annual_use.id"],
            ondelete="CASCADE",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company.id", ondelete="RESTRICT"), nullable=False
    )
    annual_use_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL")
    )
    before_snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    after_snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class FixedAssetRevision(Base):
    """Audited shared-purchase basis and in-service corrections."""

    __tablename__ = "fixed_asset_revision"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "asset_id", "version", name="uq_fixed_asset_revision_version"
        ),
        ForeignKeyConstraint(
            ["company_id", "asset_id"],
            ["fixed_asset.company_id", "fixed_asset.id"],
            ondelete="CASCADE",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company.id", ondelete="RESTRICT"), nullable=False
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL")
    )
    before_snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    after_snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    affected_periods: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

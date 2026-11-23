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
from sqlalchemy.dialects.postgresql import UUID
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
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

"""VAT dictionary service – CRUD + seed seeding for vat_rate / vat_treatment (M4 step 1).

Public API
----------
- ``list_vat_rates``       – all rates for a company
- ``get_vat_rate``         – single rate (company-scoped)
- ``create_vat_rate``      – insert (injects company_id)
- ``update_vat_rate``      – mutate
- ``delete_vat_rate``      – remove

- ``list_vat_treatments``  – all treatments, optional side filter
- ``get_vat_treatment``    – single treatment
- ``create_vat_treatment`` – insert
- ``update_vat_treatment`` – mutate
- ``delete_vat_treatment`` – remove

- ``seed_for_company``     – insert default rates + treatments for a new company
                             (idempotent: skips rows that already exist by code/label)

Design notes
------------
- All queries filter by ``company_id``; front-end never sends it.
- UNIQUE(company_id, label) on vat_rate and UNIQUE(company_id, code) on
  vat_treatment; IntegrityError is translated to HTTPException 409.
- ``report_box`` stays NULL in M4; M10 fills it with the author.
- No money calculations here (red-line 1).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jai.models._enums import VatTreatmentEffect, VatTreatmentSide
from jai.models.vat import VatRate, VatTreatment
from jai.schemas.vat import (
    VatRateListResponse,
    VatRateRead,
    VatRateWrite,
    VatTreatmentListResponse,
    VatTreatmentRead,
    VatTreatmentWrite,
)

# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

_RATE_SEEDS: list[dict[str, object]] = [
    {"label": "NL standard (21%)", "percent": Decimal("21.000")},
    {"label": "NL reduced (9%)", "percent": Decimal("9.000")},
    {"label": "Zero (0%)", "percent": Decimal("0.000")},
]

_TREATMENT_SEEDS: list[dict[str, object]] = [
    # Sales side (4)
    {
        "code": "NL_DOMESTIC",
        "label": "NL Domestic",
        "side": VatTreatmentSide.SALES,
        "effect": VatTreatmentEffect.APPLY_RATE,
        "requires_icp": False,
        "deductible": None,
    },
    {
        "code": "EU_B2B_REVERSE",
        "label": "EU B2B (Reverse Charge)",
        "side": VatTreatmentSide.SALES,
        "effect": VatTreatmentEffect.ZERO_REVERSE,
        "requires_icp": True,
        "deductible": None,
    },
    {
        "code": "EU_B2C",
        "label": "EU B2C",
        "side": VatTreatmentSide.SALES,
        "effect": VatTreatmentEffect.APPLY_RATE,
        "requires_icp": False,
        "deductible": None,
    },
    {
        "code": "EXPORT_NON_EU",
        "label": "Export (Non-EU)",
        "side": VatTreatmentSide.SALES,
        "effect": VatTreatmentEffect.ZERO_EXPORT,
        "requires_icp": False,
        "deductible": None,
    },
    # Purchase side (4)
    {
        "code": "NL_DOMESTIC_PURCH",
        "label": "NL Domestic (Purchase)",
        "side": VatTreatmentSide.PURCHASE,
        "effect": VatTreatmentEffect.APPLY_RATE,
        "requires_icp": False,
        "deductible": True,
    },
    {
        "code": "EU_B2B_REVERSE_PURCH",
        "label": "EU B2B Reverse Charge (Purchase)",
        "side": VatTreatmentSide.PURCHASE,
        "effect": VatTreatmentEffect.ZERO_REVERSE,
        "requires_icp": True,
        "deductible": True,
    },
    {
        "code": "EU_B2C_PURCH",
        "label": "EU B2C (Purchase)",
        "side": VatTreatmentSide.PURCHASE,
        "effect": VatTreatmentEffect.APPLY_RATE,
        "requires_icp": False,
        "deductible": None,
    },
    {
        "code": "IMPORT_NON_EU",
        "label": "Import (Non-EU)",
        "side": VatTreatmentSide.PURCHASE,
        "effect": VatTreatmentEffect.APPLY_RATE,
        "requires_icp": False,
        "deductible": None,
    },
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rate_to_read(r: VatRate) -> VatRateRead:
    return VatRateRead(
        id=r.id,
        label=r.label,
        percent=Decimal(str(r.percent)),
        active=r.active,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


def _treatment_to_read(t: VatTreatment) -> VatTreatmentRead:
    return VatTreatmentRead(
        id=t.id,
        code=t.code,
        label=t.label,
        side=t.side,
        effect=t.effect,
        report_box=t.report_box,
        requires_icp=t.requires_icp,
        deductible=t.deductible,
        active=t.active,
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


async def _check_rate_unique(
    session: AsyncSession,
    company_id: uuid.UUID,
    label: str,
    exclude_id: uuid.UUID | None = None,
) -> None:
    """Raise IntegrityError-like ValueError if label conflicts."""
    stmt = select(VatRate).where(
        VatRate.company_id == company_id,
        VatRate.label == label,
    )
    if exclude_id is not None:
        stmt = stmt.where(VatRate.id != exclude_id)
    result = await session.execute(stmt)
    if result.scalar_one_or_none() is not None:
        raise ValueError(f"VAT rate label '{label}' already exists for this company.")


async def _check_treatment_unique(
    session: AsyncSession,
    company_id: uuid.UUID,
    code: str,
    exclude_id: uuid.UUID | None = None,
) -> None:
    stmt = select(VatTreatment).where(
        VatTreatment.company_id == company_id,
        VatTreatment.code == code,
    )
    if exclude_id is not None:
        stmt = stmt.where(VatTreatment.id != exclude_id)
    result = await session.execute(stmt)
    if result.scalar_one_or_none() is not None:
        raise ValueError(f"VAT treatment code '{code}' already exists for this company.")


# ---------------------------------------------------------------------------
# VAT Rate CRUD
# ---------------------------------------------------------------------------


async def list_vat_rates(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    limit: int | None = None,
) -> VatRateListResponse:
    stmt = (
        select(VatRate)
        .where(VatRate.company_id == company_id)
        .order_by(VatRate.percent.desc(), VatRate.label)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    rows = result.scalars().all()
    return VatRateListResponse(items=[_rate_to_read(r) for r in rows])


async def get_vat_rate(
    session: AsyncSession,
    rate_id: uuid.UUID,
    company_id: uuid.UUID,
) -> VatRate | None:
    stmt = select(VatRate).where(
        VatRate.id == rate_id,
        VatRate.company_id == company_id,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def create_vat_rate(
    session: AsyncSession,
    data: VatRateWrite,
    company_id: uuid.UUID,
) -> VatRateRead:
    await _check_rate_unique(session, company_id, data.label)
    rate = VatRate(
        company_id=company_id,
        label=data.label,
        percent=data.percent,
        active=data.active,
    )
    session.add(rate)
    try:
        await session.flush()
    except IntegrityError as err:
        await session.rollback()
        raise ValueError(
            f"VAT rate label '{data.label}' already exists for this company."
        ) from err
    await session.refresh(rate)
    return _rate_to_read(rate)


async def update_vat_rate(
    session: AsyncSession,
    rate: VatRate,
    data: VatRateWrite,
) -> VatRateRead:
    await _check_rate_unique(session, rate.company_id, data.label, exclude_id=rate.id)
    rate.label = data.label
    rate.percent = data.percent
    rate.active = data.active
    try:
        await session.flush()
    except IntegrityError as err:
        await session.rollback()
        raise ValueError(
            f"VAT rate label '{data.label}' already exists for this company."
        ) from err
    await session.refresh(rate)
    return _rate_to_read(rate)


async def delete_vat_rate(session: AsyncSession, rate: VatRate) -> None:
    await session.delete(rate)
    await session.flush()


# ---------------------------------------------------------------------------
# VAT Treatment CRUD
# ---------------------------------------------------------------------------


async def list_vat_treatments(
    session: AsyncSession,
    company_id: uuid.UUID,
    side: VatTreatmentSide | None = None,
    *,
    limit: int | None = None,
) -> VatTreatmentListResponse:
    stmt = (
        select(VatTreatment)
        .where(VatTreatment.company_id == company_id)
        .order_by(VatTreatment.side, VatTreatment.code)
    )
    if side is not None:
        stmt = stmt.where(VatTreatment.side == side)
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    rows = result.scalars().all()
    return VatTreatmentListResponse(items=[_treatment_to_read(t) for t in rows])


async def get_vat_treatment(
    session: AsyncSession,
    treatment_id: uuid.UUID,
    company_id: uuid.UUID,
) -> VatTreatment | None:
    stmt = select(VatTreatment).where(
        VatTreatment.id == treatment_id,
        VatTreatment.company_id == company_id,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def create_vat_treatment(
    session: AsyncSession,
    data: VatTreatmentWrite,
    company_id: uuid.UUID,
) -> VatTreatmentRead:
    await _check_treatment_unique(session, company_id, data.code)
    treatment = VatTreatment(
        company_id=company_id,
        code=data.code,
        label=data.label,
        side=data.side,
        effect=data.effect,
        requires_icp=data.requires_icp,
        deductible=data.deductible,
        active=data.active,
    )
    session.add(treatment)
    try:
        await session.flush()
    except IntegrityError as err:
        await session.rollback()
        raise ValueError(
            f"VAT treatment code '{data.code}' already exists for this company."
        ) from err
    await session.refresh(treatment)
    return _treatment_to_read(treatment)


async def update_vat_treatment(
    session: AsyncSession,
    treatment: VatTreatment,
    data: VatTreatmentWrite,
) -> VatTreatmentRead:
    await _check_treatment_unique(
        session, treatment.company_id, data.code, exclude_id=treatment.id
    )
    treatment.code = data.code
    treatment.label = data.label
    treatment.side = data.side
    treatment.effect = data.effect
    treatment.requires_icp = data.requires_icp
    treatment.deductible = data.deductible
    treatment.active = data.active
    try:
        await session.flush()
    except IntegrityError as err:
        await session.rollback()
        raise ValueError(
            f"VAT treatment code '{data.code}' already exists for this company."
        ) from err
    await session.refresh(treatment)
    return _treatment_to_read(treatment)


async def delete_vat_treatment(session: AsyncSession, treatment: VatTreatment) -> None:
    await session.delete(treatment)
    await session.flush()


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


async def seed_for_company(session: AsyncSession, company_id: uuid.UUID) -> None:
    """Insert default VAT rates and treatments for *company_id*.

    Idempotent: skips rows that already exist (by label for rates, by code for
    treatments).  Safe to call on every company creation, including the first.
    """
    # Seed VAT rates.
    existing_rate_labels_result = await session.execute(
        select(VatRate.label).where(VatRate.company_id == company_id)
    )
    existing_labels = {row for row in existing_rate_labels_result.scalars().all()}

    for seed in _RATE_SEEDS:
        if seed["label"] not in existing_labels:
            session.add(
                VatRate(
                    company_id=company_id,
                    label=seed["label"],
                    percent=seed["percent"],
                    active=True,
                )
            )

    # Seed VAT treatments.
    existing_codes_result = await session.execute(
        select(VatTreatment.code).where(VatTreatment.company_id == company_id)
    )
    existing_codes = {row for row in existing_codes_result.scalars().all()}

    for seed in _TREATMENT_SEEDS:
        if seed["code"] not in existing_codes:
            session.add(
                VatTreatment(
                    company_id=company_id,
                    code=seed["code"],
                    label=seed["label"],
                    side=seed["side"],
                    effect=seed["effect"],
                    report_box=None,
                    requires_icp=seed["requires_icp"],
                    deductible=seed["deductible"],
                    active=True,
                )
            )

    await session.flush()

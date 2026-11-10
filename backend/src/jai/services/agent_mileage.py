"""M18 Agent mileage option and trusted preview services."""

from __future__ import annotations

import hashlib
import hmac
import re
from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from jai.auth.secret import get_auth_secret
from jai.schemas.agent_tools import (
    MileageCategoryOption,
    MileageOptionsOutput,
    MileagePreviewOutput,
    MileageTransportOption,
    PreviewMileageInput,
)
from jai.services.agent_operations import canonical_argument_digest
from jai.services.agent_tokens import AgentPrincipal
from jai.services.mileage import (
    MileageConfigurationError,
    _lock_mileage_company,
    _MileagePricing,
    get_mileage_defaults,
    list_mileage_transport_types,
    preview_mileage_expense_facts,
)

_PREVIEW_TOKEN = re.compile(r"^m18v1\.[0-9a-f]{64}$")


def _preview_token(payload: dict[str, object]) -> str:
    digest = canonical_argument_digest(payload)
    signature = hmac.new(
        get_auth_secret().encode("utf-8"), b"jai:m18:mileage-preview:v1:" + digest, hashlib.sha256
    ).hexdigest()
    return f"m18v1.{signature}"


def _build_preview_payload(
    principal: AgentPrincipal,
    request: PreviewMileageInput,
    pricing: _MileagePricing,
) -> dict[str, object]:
    """Build the signed payload from the exact facts used by preview output."""
    resolved_type = MileageTransportOption(
        id=pricing.transport_type.id, name=pricing.transport_type.name
    )
    resolved_category = MileageCategoryOption(id=pricing.category.id, name=pricing.category.name)
    return {
        "version": 1,
        "company_id": principal.company_id,
        "token_id": principal.token_id,
        "request_id": request.request_id,
        "source": request.source.model_dump(mode="python") if request.source else None,
        "mileage": request.mileage.model_dump(mode="python"),
        "facts": {
            "transport_type": resolved_type.model_dump(mode="python"),
            "rate": {
                "id": pricing.rate.id,
                "transport_type_id": pricing.rate.transport_type_id,
                "effective_from": pricing.rate.effective_from,
                "rate_per_km": pricing.rate.rate_per_km,
            },
            "total_distance_km": pricing.total_distance_km,
            "amount": pricing.amount,
            "currency": pricing.company.base_currency,
            "category": resolved_category.model_dump(mode="python"),
            "vat_treatment": {
                "id": pricing.treatment.id,
                "code": pricing.treatment.code,
                "label": pricing.treatment.label,
                "side": pricing.treatment.side,
                "effect": pricing.treatment.effect,
                "report_box": pricing.treatment.report_box,
                "requires_icp": pricing.treatment.requires_icp,
                "deductible": pricing.treatment.deductible,
                "active": pricing.treatment.active,
            },
            "vat_rate": {
                "id": pricing.vat_rate.id,
                "label": pricing.vat_rate.label,
                "percent": pricing.vat_rate.percent,
                "active": pricing.vat_rate.active,
            },
            "expense_projection": {
                "kind": "MILEAGE",
                "expense_date": request.mileage.trip_date,
                "net_amount": pricing.amount,
                "vat_amount": Decimal("0"),
                "gross_amount": pricing.amount,
                "deductible": False,
                "currency": pricing.company.base_currency,
                "exchange_rate": Decimal("1"),
                "base_net_amount": pricing.amount,
                "base_vat_amount": Decimal("0"),
                "base_gross_amount": pricing.amount,
            },
        },
    }


def verify_mileage_preview_token(token: str, payload: dict[str, object]) -> bool:
    """Constant-time readiness verifier for the Step 3 create operation."""
    if not _PREVIEW_TOKEN.fullmatch(token):
        return False
    expected = _preview_token(payload)
    return hmac.compare_digest(token, expected)


async def get_mileage_options(
    session: AsyncSession, principal: AgentPrincipal
) -> MileageOptionsOutput:
    """Resolve the live default and enabled transport choices under the company lock."""
    await _lock_mileage_company(session, principal.company_id)
    try:
        defaults = await get_mileage_defaults(
            session, principal.company_id, bypass_cache=True
        )
    except ValidationError as exc:
        raise MileageConfigurationError("Mileage defaults are invalid.") from exc
    types = await list_mileage_transport_types(session, principal.company_id)
    return MileageOptionsOutput(
        default_transport_type_id=defaults.default_transport_type_id,
        transport_types=[
            MileageTransportOption(id=item.id, name=item.name)
            for item in types.items
            if item.active
        ],
    )


async def preview_mileage(
    session: AsyncSession,
    principal: AgentPrincipal,
    request: PreviewMileageInput,
) -> MileagePreviewOutput:
    """Build a canonical, typed preview binding all inputs and Expense facts."""
    try:
        pricing, calculation = await preview_mileage_expense_facts(
            session, principal.company_id, request.mileage
        )
    except ValidationError as exc:
        raise MileageConfigurationError("Mileage defaults are invalid.") from exc
    resolved_type = MileageTransportOption(
        id=pricing.transport_type.id, name=pricing.transport_type.name
    )
    resolved_category = MileageCategoryOption(id=pricing.category.id, name=pricing.category.name)
    payload = _build_preview_payload(principal, request, pricing)
    return MileagePreviewOutput(
        request_id=request.request_id,
        mileage=request.mileage,
        transport_type=resolved_type,
        expense_category=resolved_category,
        calculation=calculation,
        preview_token=_preview_token(payload),
    )

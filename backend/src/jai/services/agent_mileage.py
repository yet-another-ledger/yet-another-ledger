"""M18 Agent mileage option and trusted preview services."""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from decimal import Decimal
from typing import Literal, cast

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jai.auth.secret import get_auth_secret
from jai.config import get_settings
from jai.models.agent_token import AgentToken
from jai.schemas.agent_tools import (
    AgentWriteResult,
    CreateMileageExpenseInput,
    MileageCategoryOption,
    MileageOptionsOutput,
    MileagePreviewOutput,
    MileageToolSnapshot,
    MileageTransportOption,
    MileageWriteOutput,
    PreviewMileageInput,
)
from jai.services.agent_operations import (
    AgentOperationFailure,
    AgentOperationValue,
    _reject_m18_retained_material,
    canonical_argument_digest,
    run_agent_mutation,
)
from jai.services.agent_tokens import AgentPrincipal
from jai.services.mileage import (
    MileageConfigurationError,
    MileageResourceNotFoundError,
    _lock_mileage_company,
    _MileagePricing,
    create_mileage_expense_from_pricing,
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


def _validate_mileage_snapshot_material(
    request: PreviewMileageInput, pricing: _MileagePricing
) -> None:
    """Prevent previews that cannot later be safely retained for replay."""
    _reject_m18_retained_material(
        {
            "mileage": request.mileage.model_dump(mode="python"),
            "source": request.source.model_dump(mode="python") if request.source else None,
            "transport_type_name": pricing.transport_type.name,
        }
    )


def _validate_mileage_request_material(request: PreviewMileageInput) -> None:
    """Reject unsafe retained request text before reserving a create attempt."""
    _reject_m18_retained_material(
        {
            "mileage": request.mileage.model_dump(mode="python"),
            "source": request.source.model_dump(mode="python") if request.source else None,
        }
    )


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
    _validate_mileage_snapshot_material(request, pricing)
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


def _mileage_web_url(trip_id: uuid.UUID) -> str:
    return f"{get_settings().base_url.rstrip('/')}/expenses/mileage/{trip_id}/edit"


async def create_mileage_expense(
    session_maker: async_sessionmaker[AsyncSession],
    principal: AgentPrincipal,
    request: CreateMileageExpenseInput,
) -> MileageWriteOutput:
    """Create one confirmed mileage trip and persist its replay snapshot."""
    preview_request = PreviewMileageInput(
        request_id=request.request_id, source=request.source, mileage=request.mileage
    )
    try:
        _validate_mileage_request_material(preview_request)
    except ValueError:
        raise HTTPException(status_code=422, detail={"code": "MCP_VALIDATION_FAILED"}) from None

    async def mutate(session: AsyncSession) -> AgentOperationValue:
        try:
            # This resolver takes the company schedule lock and bypasses stale
            # process settings caches. Keep this pricing object through insert.
            pricing, _ = await preview_mileage_expense_facts(
                session, principal.company_id, request.mileage
            )
        except ValidationError as exc:
            raise AgentOperationFailure("MCP_MILEAGE_CONFIGURATION_INVALID") from exc
        except MileageResourceNotFoundError:
            raise AgentOperationFailure("MCP_TARGET_NOT_FOUND") from None
        except MileageConfigurationError:
            raise AgentOperationFailure("MCP_MILEAGE_CONFIGURATION_INVALID") from None
        except ValueError:
            # Well-typed trip input can still overflow a computed NUMERIC
            # snapshot (for example after a return-trip multiplier).
            raise AgentOperationFailure("MCP_VALIDATION_FAILED") from None

        try:
            _validate_mileage_snapshot_material(preview_request, pricing)
        except ValueError:
            raise AgentOperationFailure("MCP_VALIDATION_FAILED") from None

        signed_payload = _build_preview_payload(principal, preview_request, pricing)
        if not verify_mileage_preview_token(request.preview_token, signed_payload):
            raise AgentOperationFailure("MCP_MILEAGE_PREVIEW_STALE")

        creator_id = await session.scalar(
            select(AgentToken.creator_id).where(AgentToken.id == principal.token_id)
        )
        if creator_id is None:
            raise AgentOperationFailure("MCP_UNAUTHENTICATED")
        try:
            mileage = await create_mileage_expense_from_pricing(
                session, principal.company_id, request.mileage, creator_id, pricing
            )
        except (ValidationError, ValueError):
            raise AgentOperationFailure("MCP_MILEAGE_CONFIGURATION_INVALID") from None
        return AgentOperationValue(
            "mileage",
            mileage.id,
            MileageToolSnapshot(mileage=mileage, web_url=_mileage_web_url(mileage.id)),
        )

    async with session_maker() as session:
        response = await run_agent_mutation(
            session,
            principal,
            request_id=str(request.request_id),
            operation="create_mileage_expense",
            arguments={
                "mileage": request.mileage.model_dump(mode="python"),
                "preview_token": request.preview_token,
            },
            source=request.source.model_dump(mode="python") if request.source else None,
            mutate=mutate,
        )
    if response.outcome != "succeeded" or response.target_id is None:
        code = response.error_code or "MCP_VALIDATION_FAILED"
        raise HTTPException(status_code=422, detail={"code": code})
    snapshot = MileageToolSnapshot.model_validate(response.result_snapshot)
    return MileageWriteOutput(
        mileage=snapshot.mileage,
        operation=AgentWriteResult(
            operation_id=response.operation_id,
            status=cast(Literal["created", "replayed"], response.status),
            target_id=response.target_id,
            web_url=snapshot.web_url,
        ),
    )

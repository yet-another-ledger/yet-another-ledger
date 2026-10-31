"""Home-battery asset registration and annual private-use operations."""

from __future__ import annotations

import uuid
from typing import NoReturn
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from jai.auth.deps import current_mfa_user
from jai.config import get_settings
from jai.db import get_session
from jai.models.user import User
from jai.schemas.battery import (
    BatteryAnnualCalculateRequest,
    BatteryAnnualCalculation,
    BatteryAnnualConfirmRequest,
    BatteryAnnualRevisionInput,
    BatteryAnnualRevisionListRead,
    BatteryAnnualRevisionPreview,
    BatteryAnnualUseInput,
    BatteryAnnualUseRead,
    BatteryAssetCreateRequest,
    BatteryAssetListRead,
    BatteryAssetPreview,
    BatteryAssetPreviewRequest,
    BatteryAssetRead,
    BatteryAssetRevisionInput,
    BatteryAssetRevisionListRead,
    BatteryAssetRevisionPreview,
    BatteryConflictCode,
    BatteryConflictDetail,
    BatteryConflictResponse,
    BatteryEvidenceRead,
)
from jai.services import battery
from jai.services.battery import BatteryConflict
from jai.services.storage import ReceiptValidationError, get_storage

router = APIRouter(prefix="/api/v1/battery-assets", tags=["battery-assets"])


def _owner(user: User = Depends(current_mfa_user)) -> User:
    if user.role != "owner" or user.company_id is None:
        raise HTTPException(status_code=403, detail="Owner access required.")
    return user


def _company(user: User) -> uuid.UUID:
    if user.company_id is None:
        raise HTTPException(status_code=403, detail="Owner has no company.")
    return user.company_id


def _raise(exc: Exception) -> NoReturn:
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ReceiptValidationError):
        raise HTTPException(
            status_code=422, detail={"code": "BATTERY_EVIDENCE_INVALID", "detail": str(exc)}
        ) from exc
    if isinstance(exc, BatteryConflict):
        status_code = (
            409
            if exc.code
            in {
                "BATTERY_ACQUISITION_ALREADY_REGISTERED",
                "BATTERY_HISTORICAL_ACK_REQUIRED",
                "BATTERY_VERSION_CONFLICT",
                "BATTERY_PREVIEW_STALE",
                "BATTERY_ANNUAL_CONFIRMED",
                "BATTERY_ANNUAL_ALREADY_CONFIRMED",
                "BATTERY_REVISION_REQUIRED",
                "BATTERY_REVISION_PERIOD_ACK_REQUIRED",
                "BATTERY_REVISION_NO_CHANGES",
            }
            else 422
        )
        raise HTTPException(
            status_code=status_code,
            detail=BatteryConflictDetail(
                code=BatteryConflictCode(exc.code),
                detail=str(exc),
                current_version=exc.current_version,
            ).model_dump(),
        ) from exc
    raise exc


@router.post("/preview", response_model=BatteryAssetPreview)
async def preview_asset(
    body: BatteryAssetPreviewRequest,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> BatteryAssetPreview:
    try:
        return await battery.preview_asset(session, _company(user), body)
    except (LookupError, BatteryConflict) as exc:
        _raise(exc)


@router.post(
    "",
    response_model=BatteryAssetRead,
    status_code=201,
    responses={
        409: {"model": BatteryConflictResponse},
        422: {"model": BatteryConflictResponse},
    },
)
async def create_asset(
    body: BatteryAssetCreateRequest,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> BatteryAssetRead:
    try:
        return await battery.register_asset(session, _company(user), user.id, body)
    except (LookupError, BatteryConflict) as exc:
        _raise(exc)


@router.get("", response_model=BatteryAssetListRead)
async def get_assets(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> BatteryAssetListRead:
    return await battery.list_assets(session, _company(user), limit, offset)


@router.get("/{asset_id}", response_model=BatteryAssetRead)
async def get_asset(
    asset_id: uuid.UUID,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> BatteryAssetRead:
    try:
        return await battery.get_asset(session, _company(user), asset_id)
    except LookupError as exc:
        _raise(exc)


@router.post(
    "/{asset_id}/revisions/preview",
    response_model=BatteryAssetRevisionPreview,
    responses={409: {"model": BatteryConflictResponse}, 422: {"model": BatteryConflictResponse}},
)
async def preview_asset_revision(
    asset_id: uuid.UUID,
    body: BatteryAssetRevisionInput,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> BatteryAssetRevisionPreview:
    try:
        return await battery.preview_asset_revision(session, _company(user), asset_id, body)
    except (LookupError, BatteryConflict) as exc:
        _raise(exc)


@router.post(
    "/{asset_id}/revisions",
    response_model=BatteryAssetRead,
    responses={409: {"model": BatteryConflictResponse}, 422: {"model": BatteryConflictResponse}},
)
async def revise_asset(
    asset_id: uuid.UUID,
    body: BatteryAssetRevisionInput,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> BatteryAssetRead:
    try:
        return await battery.revise_asset(session, _company(user), user.id, asset_id, body)
    except (LookupError, BatteryConflict) as exc:
        _raise(exc)


@router.get("/{asset_id}/revisions", response_model=BatteryAssetRevisionListRead)
async def get_asset_revisions(
    asset_id: uuid.UUID,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> BatteryAssetRevisionListRead:
    try:
        return await battery.list_asset_revisions(session, _company(user), asset_id, offset, limit)
    except LookupError as exc:
        _raise(exc)


@router.get(
    "/{asset_id}/annual-use/{year}",
    response_model=BatteryAnnualUseRead | None,
    responses={422: {"model": BatteryConflictResponse}},
)
async def get_annual_use(
    asset_id: uuid.UUID,
    year: int,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> BatteryAnnualUseRead | None:
    try:
        return await battery.get_annual_use(session, _company(user), asset_id, year)
    except (LookupError, BatteryConflict) as exc:
        _raise(exc)


@router.put(
    "/{asset_id}/annual-use/{year}",
    response_model=BatteryAnnualUseRead,
    responses={
        409: {"model": BatteryConflictResponse},
        422: {"model": BatteryConflictResponse},
    },
)
async def put_annual_use(
    asset_id: uuid.UUID,
    year: int,
    body: BatteryAnnualUseInput,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> BatteryAnnualUseRead:
    try:
        result = await battery.put_annual_use(session, _company(user), asset_id, year, body)
        assert result is not None
        return result
    except (LookupError, BatteryConflict) as exc:
        _raise(exc)


@router.post(
    "/{asset_id}/annual-use/{year}/calculate",
    response_model=BatteryAnnualCalculation,
    responses={
        409: {"model": BatteryConflictResponse},
        422: {"model": BatteryConflictResponse},
    },
)
async def calculate_annual_use(
    asset_id: uuid.UUID,
    year: int,
    body: BatteryAnnualCalculateRequest,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> BatteryAnnualCalculation:
    try:
        return await battery.calculate_annual_use(
            session,
            _company(user),
            asset_id,
            year,
            body.expected_version,
            body.expected_asset_version,
        )
    except (LookupError, BatteryConflict) as exc:
        _raise(exc)


@router.post(
    "/{asset_id}/annual-use/{year}/revisions/preview",
    response_model=BatteryAnnualRevisionPreview,
    responses={409: {"model": BatteryConflictResponse}, 422: {"model": BatteryConflictResponse}},
)
async def preview_annual_revision(
    asset_id: uuid.UUID,
    year: int,
    body: BatteryAnnualRevisionInput,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> BatteryAnnualRevisionPreview:
    try:
        return await battery.preview_annual_revision(session, _company(user), asset_id, year, body)
    except (LookupError, BatteryConflict) as exc:
        _raise(exc)


@router.post(
    "/{asset_id}/annual-use/{year}/revisions",
    response_model=BatteryAnnualUseRead,
    responses={409: {"model": BatteryConflictResponse}, 422: {"model": BatteryConflictResponse}},
)
async def revise_annual_use(
    asset_id: uuid.UUID,
    year: int,
    body: BatteryAnnualRevisionInput,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> BatteryAnnualUseRead:
    try:
        result = await battery.revise_annual_use(
            session, _company(user), user.id, asset_id, year, body
        )
        return result
    except (LookupError, BatteryConflict) as exc:
        _raise(exc)


@router.get("/{asset_id}/annual-use/{year}/revisions", response_model=BatteryAnnualRevisionListRead)
async def get_annual_revisions(
    asset_id: uuid.UUID,
    year: int,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> BatteryAnnualRevisionListRead:
    try:
        return await battery.list_annual_revisions(
            session, _company(user), asset_id, year, offset, limit
        )
    except LookupError as exc:
        _raise(exc)


@router.post(
    "/{asset_id}/annual-use/{year}/confirm",
    response_model=BatteryAnnualUseRead,
    responses={
        409: {"model": BatteryConflictResponse},
        422: {"model": BatteryConflictResponse},
    },
)
async def confirm_annual_use(
    asset_id: uuid.UUID,
    year: int,
    body: BatteryAnnualConfirmRequest,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> BatteryAnnualUseRead:
    try:
        result = await battery.confirm_annual_use(
            session, _company(user), asset_id, year, user.id, body
        )
        assert result is not None
        return result
    except (LookupError, BatteryConflict) as exc:
        _raise(exc)


@router.post(
    "/{asset_id}/annual-use/{year}/evidence",
    response_model=BatteryEvidenceRead,
    status_code=201,
    responses={
        409: {"model": BatteryConflictResponse},
        422: {"model": BatteryConflictResponse},
    },
)
async def upload_annual_evidence(
    asset_id: uuid.UUID,
    year: int,
    file: UploadFile = File(...),
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> BatteryEvidenceRead:
    company_id = _company(user)
    try:
        # Resolve ownership/state before reading bytes from the upload stream.
        await battery.validate_annual_evidence_upload(session, company_id, asset_id, year)
        content = await file.read(get_settings().max_receipt_bytes + 1)
        return await battery.add_annual_evidence(
            session,
            company_id,
            asset_id,
            year,
            content,
            file.content_type or "application/octet-stream",
            file.filename,
            get_storage(),
            get_settings().max_receipt_bytes,
        )
    except (LookupError, BatteryConflict, ReceiptValidationError) as exc:
        _raise(exc)


@router.get("/{asset_id}/annual-use/{year}/evidence/{evidence_id}/content")
async def get_annual_evidence_content(
    asset_id: uuid.UUID,
    year: int,
    evidence_id: uuid.UUID,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        content, evidence = await battery.read_annual_evidence(
            session, _company(user), asset_id, year, evidence_id, get_storage()
        )
    except LookupError as exc:
        _raise(exc)
    filename = quote(evidence.filename or "battery-evidence", safe="")
    return Response(
        content,
        media_type=evidence.media_type,
        headers={"Content-Disposition": f"inline; filename*=UTF-8''{filename}"},
    )

"""Expense API routes – CRUD + list + AI extraction (M8 steps 1 + 4).

Endpoints:
  POST   /api/v1/expenses              – create an expense → 201 ExpenseRead
  GET    /api/v1/expenses              – list expenses with filters → 200 ExpenseListResponse
  POST   /api/v1/expenses/ai-extract   – AI receipt extraction → 200 ExpenseAIPrefill
  GET    /api/v1/expenses/{id}         – get a single expense → 200 ExpenseRead
  PUT    /api/v1/expenses/{id}         – update an expense → 200 ExpenseRead
  DELETE /api/v1/expenses/{id}         – delete an expense → 204

Auth: owner-only (mirrors api/payments.py).
company_id is always injected by the service; never taken from the request body
(red-line 2).

Error mapping for ai-extract:
  - Unsupported MIME type → 422
  - AI disabled / no key → 409
  - Model failure / timeout → 502
  - Cross-company attachment → 404
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from jai.auth.deps import current_mfa_user
from jai.db import get_session
from jai.models._enums import ExpenseKind
from jai.models.user import User
from jai.schemas.expense import (
    ExpenseAIPrefill,
    ExpenseCalculateRequest,
    ExpenseCalculateResult,
    ExpenseCostPreviewRequest,
    ExpenseCostPreviewResult,
    ExpenseInput,
    ExpenseListResponse,
    ExpenseRead,
)
from jai.services.expense import (
    ExpenseConflict,
    calculate_expense_cost_preview,
    calculate_expense_preview,
    create_expense,
    delete_expense,
    get_expense,
    get_expense_cost_preview,
    list_expenses,
    update_expense,
)
from jai.services.mileage import MileageExpenseUpdateError
from jai.services.storage import get_storage


class _AiExtractRequest(BaseModel):
    """Request body for POST /expenses/ai-extract."""

    attachment_id: uuid.UUID
    language: str | None = None  # UI locale ("en"/"zh"); explanatory fields follow it


router = APIRouter(prefix="/api/v1", tags=["expenses"])


def _owner_only(user: User) -> None:
    if user.role != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner access required.")


def _require_company_id(user: User) -> uuid.UUID:
    if user.company_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User has no company associated.",
        )
    return user.company_id


# ---------------------------------------------------------------------------
# Create expense
# ---------------------------------------------------------------------------


@router.post(
    "/expenses",
    response_model=ExpenseRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_expense_endpoint(
    body: ExpenseInput,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> ExpenseRead:
    """Create a new expense record."""
    _owner_only(user)
    company_id = _require_company_id(user)
    try:
        return await create_expense(session, company_id, body, creator_id=user.id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


# ---------------------------------------------------------------------------
# List expenses
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Calculate VAT preview – registered BEFORE /expenses/{expense_id} to avoid
# the parameterised route swallowing the literal path "/expenses/calculate".
# ---------------------------------------------------------------------------


@router.post("/expenses/calculate", response_model=ExpenseCalculateResult)
async def calculate_expense_endpoint(
    body: ExpenseCalculateRequest,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> ExpenseCalculateResult:
    """Preview VAT and gross derived from net × VAT rate (not persisted).

    Designed for the expense editor: the frontend calls this when the user
    selects a VAT rate (immediately) or changes the net amount (debounced),
    and uses the returned ``vat_amount`` / ``gross_amount`` to populate the
    form without touching the DB.

    Error responses:
    - ``404`` – ``vat_rate_id`` not found or belongs to a different company.
    - ``422`` – ``net_amount < 0`` or malformed request body.
    """
    _owner_only(user)
    company_id = _require_company_id(user)
    try:
        return await calculate_expense_preview(session, company_id, body)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.post("/expenses/cost-preview", response_model=ExpenseCostPreviewResult)
async def calculate_expense_cost_endpoint(
    body: ExpenseCostPreviewRequest,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> ExpenseCostPreviewResult:
    """Preview the annual cost basis selected for P/L, without persistence."""
    _owner_only(user)
    company_id = _require_company_id(user)
    try:
        return await calculate_expense_cost_preview(session, company_id, body)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.get("/expenses/{expense_id}/cost-preview", response_model=ExpenseCostPreviewResult)
async def get_expense_cost_endpoint(
    expense_id: uuid.UUID,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> ExpenseCostPreviewResult:
    """Read current P/L cost from the stored amounts and VAT snapshots."""
    _owner_only(user)
    company_id = _require_company_id(user)
    try:
        return await get_expense_cost_preview(session, expense_id, company_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# AI extract (step 4) – registered BEFORE /expenses/{expense_id} to avoid
# the parameterised route swallowing the literal path "/expenses/ai-extract".
# ---------------------------------------------------------------------------


@router.post("/expenses/ai-extract", response_model=ExpenseAIPrefill)
async def ai_extract_endpoint(
    body: _AiExtractRequest,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> ExpenseAIPrefill:
    """Run AI receipt extraction on an already-uploaded attachment.

    Returns ``ExpenseAIPrefill`` with pre-filled fields for the user to review.
    **The prefill is never persisted** – the user must confirm by calling
    POST /expenses with the reviewed data.

    Error responses:
    - ``404`` – attachment not found or belongs to a different company.
    - ``409`` – AI is disabled or API key / model not configured.
    - ``422`` – attachment MIME type not supported by the AI pipeline.
    - ``502`` – model call failed or timed out (safe to retry or fall back to manual).
    """
    _owner_only(user)
    company_id = _require_company_id(user)

    from jai.services.ai import extract_from_attachment

    try:
        return await extract_from_attachment(
            session,
            company_id,
            body.attachment_id,
            language=body.language,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        err_str = str(exc)
        # MIME not supported → 422
        if "Unsupported MIME type" in err_str:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=err_str
            ) from exc
        # AI disabled / no key / no model → 409 Conflict
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=err_str) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc


# NOTE: this route MUST be registered before /expenses/{expense_id} so that
# FastAPI matches the literal "/expenses" path before the parameterised one.


@router.get("/expenses", response_model=ExpenseListResponse)
async def list_expenses_endpoint(
    q: str | None = Query(default=None, description="Search supplier, reference, or category."),
    category_id: uuid.UUID | None = Query(default=None),
    vat_treatment_id: uuid.UUID | None = Query(default=None),
    deductible: bool | None = Query(default=None),
    is_draft: bool | None = Query(default=None),
    kind: ExpenseKind | None = Query(default=None),
    date_from: date | None = Query(
        default=None, description="Inclusive lower bound on expense_date."
    ),
    date_to: date | None = Query(
        default=None, description="Inclusive upper bound on expense_date."
    ),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    sort_by: str = Query(default="expense_date", pattern="^(expense_date|created_at)$"),
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> ExpenseListResponse:
    """Return a paginated expenses list filtered by the given parameters."""
    _owner_only(user)
    company_id = _require_company_id(user)
    return await list_expenses(
        session,
        company_id,
        q=q,
        category_id=category_id,
        vat_treatment_id=vat_treatment_id,
        deductible=deductible,
        is_draft=is_draft,
        kind=kind,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
    )


# ---------------------------------------------------------------------------
# Get single expense
# ---------------------------------------------------------------------------


@router.get("/expenses/{expense_id}", response_model=ExpenseRead)
async def get_expense_endpoint(
    expense_id: uuid.UUID,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> ExpenseRead:
    """Fetch a single expense by id (scoped to caller's company)."""
    _owner_only(user)
    company_id = _require_company_id(user)
    try:
        return await get_expense(session, expense_id, company_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Update expense
# ---------------------------------------------------------------------------


@router.put("/expenses/{expense_id}", response_model=ExpenseRead)
async def update_expense_endpoint(
    expense_id: uuid.UUID,
    body: ExpenseInput,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> ExpenseRead:
    """Update an expense (recalculates amounts + snapshots).

    Also used to confirm a recurring-expense draft (set is_draft=false via
    the existing is_draft field on the expense; step 3 adds explicit confirm
    endpoint).
    """
    _owner_only(user)
    company_id = _require_company_id(user)
    try:
        return await update_expense(session, expense_id, company_id, body)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except MileageExpenseUpdateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ExpenseConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "detail": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


# ---------------------------------------------------------------------------
# Delete expense
# ---------------------------------------------------------------------------


@router.delete("/expenses/{expense_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_expense_endpoint(
    expense_id: uuid.UUID,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete an expense (cascade removes attachments; step 2 cleans disk files)."""
    _owner_only(user)
    company_id = _require_company_id(user)
    storage = get_storage()
    try:
        await delete_expense(session, expense_id, company_id, storage=storage)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ExpenseConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "detail": str(exc)},
        ) from exc

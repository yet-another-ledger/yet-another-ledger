"""Recurring expense API routes (M8 step 3).

Endpoints:
  POST   /api/v1/recurring-expenses              – create template → 201
  GET    /api/v1/recurring-expenses              – list templates → 200
  GET    /api/v1/recurring-expenses/{id}         – get single → 200
  PUT    /api/v1/recurring-expenses/{id}         – update (incl. active pause/resume) → 200
  DELETE /api/v1/recurring-expenses/{id}         – delete (keeps generated expenses) → 204
  POST   /api/v1/recurring-expenses/{id}/run-now – trigger due generation → 200

Auth: owner-only (mirrors api/expenses.py).
company_id is always injected by the service; never taken from the request body
(red-line 2).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from jai.auth.deps import current_mfa_user
from jai.db import get_session
from jai.models.user import User
from jai.schemas.expense import ExpenseCostPreviewResult
from jai.schemas.recurring_expense import (
    RecurringExpenseInput,
    RecurringExpenseListResponse,
    RecurringExpenseRead,
    RunNowResponse,
)
from jai.services.recurring_expense import (
    create_recurring_expense,
    delete_recurring_expense,
    generate_due_for_template,
    get_recurring_expense,
    get_recurring_expense_cost_preview,
    list_recurring_expenses,
    update_recurring_expense,
)

router = APIRouter(prefix="/api/v1", tags=["recurring-expenses"])


def _owner_only(user: User) -> None:
    if user.role != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Owner access required."
        )


def _require_company_id(user: User) -> uuid.UUID:
    if user.company_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User has no company associated.",
        )
    return user.company_id


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post(
    "/recurring-expenses",
    response_model=RecurringExpenseRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_recurring_expense_endpoint(
    body: RecurringExpenseInput,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> RecurringExpenseRead:
    """Create a new recurring expense template."""
    _owner_only(user)
    company_id = _require_company_id(user)
    try:
        return await create_recurring_expense(session, company_id, body, creator_id=user.id)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


# NOTE: this route MUST be registered before /recurring-expenses/{id} so that
# FastAPI matches the literal "/recurring-expenses" path before the parameterised one.


@router.get("/recurring-expenses", response_model=RecurringExpenseListResponse)
async def list_recurring_expenses_endpoint(
    active: bool | None = Query(default=None, description="Filter by active flag."),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> RecurringExpenseListResponse:
    """Return a paginated list of recurring expense templates."""
    _owner_only(user)
    company_id = _require_company_id(user)
    return await list_recurring_expenses(
        session,
        company_id,
        active=active,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Get single
# ---------------------------------------------------------------------------


@router.get(
    "/recurring-expenses/{recurring_id}/cost-preview", response_model=ExpenseCostPreviewResult
)
async def get_recurring_expense_cost_endpoint(
    recurring_id: uuid.UUID,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> ExpenseCostPreviewResult:
    """Read the annual cost of one future Expense using the stored template snapshots."""
    _owner_only(user)
    company_id = _require_company_id(user)
    try:
        return await get_recurring_expense_cost_preview(session, recurring_id, company_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/recurring-expenses/{recurring_id}", response_model=RecurringExpenseRead)
async def get_recurring_expense_endpoint(
    recurring_id: uuid.UUID,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> RecurringExpenseRead:
    """Fetch a single recurring expense template by id."""
    _owner_only(user)
    company_id = _require_company_id(user)
    try:
        return await get_recurring_expense(session, recurring_id, company_id)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


@router.put("/recurring-expenses/{recurring_id}", response_model=RecurringExpenseRead)
async def update_recurring_expense_endpoint(
    recurring_id: uuid.UUID,
    body: RecurringExpenseInput,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> RecurringExpenseRead:
    """Update a recurring expense template.

    Set ``active: false`` to pause; ``active: true`` to resume.
    """
    _owner_only(user)
    company_id = _require_company_id(user)
    try:
        return await update_recurring_expense(session, recurring_id, company_id, body)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@router.delete(
    "/recurring-expenses/{recurring_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_recurring_expense_endpoint(
    recurring_id: uuid.UUID,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a recurring expense template.

    Generated expenses are retained as independent accounting records;
    their ``recurring_expense_id`` is set to NULL by the DB FK constraint.
    """
    _owner_only(user)
    company_id = _require_company_id(user)
    try:
        await delete_recurring_expense(session, recurring_id, company_id)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


# ---------------------------------------------------------------------------
# Run-now
# ---------------------------------------------------------------------------


@router.post(
    "/recurring-expenses/{recurring_id}/run-now",
    response_model=RunNowResponse,
)
async def run_now_endpoint(
    recurring_id: uuid.UUID,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> RunNowResponse:
    """Manually trigger due generation for a single recurring expense template.

    Semantics are identical to the scheduled job (idempotent, same cursor-advance
    logic) but scoped to this specific template.  Useful for walkthroughs and
    back-filling missed runs.
    """
    _owner_only(user)
    company_id = _require_company_id(user)
    try:
        return await generate_due_for_template(session, recurring_id, company_id)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

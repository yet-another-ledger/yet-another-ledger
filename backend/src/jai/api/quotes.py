"""Quote API routes – calculate + CRUD + status + convert + reactivate (M6 steps 1–3).

Endpoints:
  POST   /api/v1/quotes/calculate        – pricing preview (no persistence)
  GET    /api/v1/quotes                  – paginated list
  POST   /api/v1/quotes                  – create (allocates quote number)
  GET    /api/v1/quotes/{id}             – get by id
  PUT    /api/v1/quotes/{id}             – update (ACCEPTED blocks)
  DELETE /api/v1/quotes/{id}             – delete (cascade)
  POST   /api/v1/quotes/{id}/status      – status transition
  POST   /api/v1/quotes/{id}/convert     – convert quote → new DRAFT invoice
  POST   /api/v1/quotes/{id}/reactivate  – EXPIRED → SENT + extend valid_until
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from jai.auth.deps import current_mfa_user
from jai.db import get_session, set_rls_company
from jai.models._enums import QuoteStatus
from jai.models.user import User
from jai.schemas.document_chain import DocumentChainRead
from jai.schemas.email_log import DocumentSendRequest, EmailLogListResponse, EmailLogRead
from jai.schemas.invoice import (
    InvoiceCalculationRead,
    InvoiceRead,
    ProjectCancellationCreateRequest,
    ProjectCancellationPreview,
    ProjectCancellationRequest,
    ProjectCancellationResult,
)
from jai.schemas.quote import (
    QuoteCalculationRead,
    QuoteCalculationRequest,
    QuoteListResponse,
    QuoteReactivateWrite,
    QuoteRead,
    QuoteStatusWrite,
    QuoteWrite,
)
from jai.services import company as company_svc
from jai.services.correction_followup import (
    CorrectionFollowupConflictError,
    CorrectionFollowupValidationError,
    create_project_cancellation_drafts,
    preview_project_cancellation,
)
from jai.services.credit import CreditConflictError, CreditValidationError
from jai.services.document_chain import ModeConflictError, get_document_chain
from jai.services.pricing import calculate_quote
from jai.services.quote import (
    ConversionConflictError,
    convert_to_invoice,
    create_quote,
    delete_quote,
    duplicate_quote,
    get_quote,
    list_quotes,
    reactivate,
    transition_status,
    update_quote,
)

router = APIRouter(prefix="/api/v1", tags=["quotes"])


def _correction_error(status_code: int, code: str, exc: Exception) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": str(exc)})


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


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


async def _get_company_currency(
    session: AsyncSession, company_id: uuid.UUID  # noqa: ARG001
) -> str:
    company = await company_svc.get_company(session)
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Company profile not found.",
        )
    return company.base_currency


# ---------------------------------------------------------------------------
# Pricing preview (step 1)
# ---------------------------------------------------------------------------


@router.post(
    "/quotes/calculate",
    response_model=QuoteCalculationRead,
    status_code=status.HTTP_200_OK,
)
async def calculate_quote_endpoint(
    body: QuoteCalculationRequest,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> InvoiceCalculationRead:
    """Preview quote pricing without persisting (red-line 1)."""
    _owner_only(user)
    company_id = _require_company_id(user)
    try:
        return await calculate_quote(session, company_id=company_id, request=body)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


# ---------------------------------------------------------------------------
# CRUD (step 2)
# ---------------------------------------------------------------------------


@router.get("/quotes", response_model=QuoteListResponse)
async def list_quotes_endpoint(
    q: str | None = Query(default=None, description="Search by number/reference/customer name"),
    customer_id: uuid.UUID | None = Query(default=None),
    status_filter: QuoteStatus | None = Query(default=None, alias="status"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    sort_by: Literal["quote_date", "created_at", "quote_number"] = Query(default="quote_date"),
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> QuoteListResponse:
    """Return a paginated list of quotes for the current company."""
    _owner_only(user)
    company_id = _require_company_id(user)
    return await list_quotes(
        session,
        company_id,
        q_param=q,
        customer_id=customer_id,
        status=status_filter,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
    )


@router.post("/quotes", response_model=QuoteRead, status_code=status.HTTP_201_CREATED)
async def create_quote_endpoint(
    body: QuoteWrite,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> QuoteRead:
    """Create a new quote, allocate a quote number, and return the full record."""
    _owner_only(user)
    company_id = _require_company_id(user)
    company_currency = await _get_company_currency(session, company_id)
    try:
        return await create_quote(
            session,
            body=body,
            company_id=company_id,
            company_currency=company_currency,
            creator_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.post(
    "/quotes/{quote_id}/duplicate",
    response_model=QuoteRead,
    status_code=status.HTTP_201_CREATED,
)
async def duplicate_quote_endpoint(
    quote_id: uuid.UUID,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> QuoteRead:
    """Copy saved content into a fresh draft using today's defaults and current VAT."""
    _owner_only(user)
    company_id = _require_company_id(user)
    company_currency = await _get_company_currency(session, company_id)
    try:
        result = await duplicate_quote(
            session, quote_id, company_id, company_currency, creator_id=user.id
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Quote not found.")
    return result


@router.get("/quotes/{quote_id}", response_model=QuoteRead)
async def get_quote_endpoint(
    quote_id: uuid.UUID,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> QuoteRead:
    """Return a quote by ID. Applies read-time expiry check."""
    _owner_only(user)
    company_id = _require_company_id(user)
    result = await get_quote(session, quote_id, company_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found.")
    return result


@router.get("/quotes/{quote_id}/document-chain", response_model=DocumentChainRead)
async def get_quote_document_chain_endpoint(
    quote_id: uuid.UUID,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> DocumentChainRead:
    """Return the read-only authoritative chain rooted at this Quote."""
    _owner_only(user)
    result = await get_document_chain(
        session, company_id=_require_company_id(user), quote_id=quote_id
    )
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found.")
    return result


@router.post(
    "/quotes/{quote_id}/cancellation/preview",
    response_model=ProjectCancellationPreview,
)
async def preview_project_cancellation_endpoint(
    quote_id: uuid.UUID,
    body: ProjectCancellationRequest = Body(default_factory=ProjectCancellationRequest),
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> ProjectCancellationPreview:
    _owner_only(user)
    try:
        return await preview_project_cancellation(
            session,
            company_id=_require_company_id(user),
            quote_id=quote_id,
            request=body,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Quote not found.") from exc
    except (CorrectionFollowupConflictError, CreditConflictError) as exc:
        raise _correction_error(409, getattr(exc, "code", "CANCELLATION_CONFLICT"), exc) from exc
    except CorrectionFollowupValidationError as exc:
        raise _correction_error(422, exc.code, exc) from exc


@router.post(
    "/quotes/{quote_id}/cancellation/create-credit-drafts",
    response_model=ProjectCancellationResult,
    status_code=status.HTTP_201_CREATED,
)
async def create_project_cancellation_drafts_endpoint(
    quote_id: uuid.UUID,
    body: ProjectCancellationCreateRequest,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> ProjectCancellationResult:
    _owner_only(user)
    try:
        return await create_project_cancellation_drafts(
            session,
            company_id=_require_company_id(user),
            quote_id=quote_id,
            request=body,
            creator_id=user.id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Quote not found.") from exc
    except (CorrectionFollowupConflictError, CreditConflictError) as exc:
        raise _correction_error(409, getattr(exc, "code", "CANCELLATION_CONFLICT"), exc) from exc
    except (CorrectionFollowupValidationError, CreditValidationError) as exc:
        raise _correction_error(422, exc.code, exc) from exc


@router.put("/quotes/{quote_id}", response_model=QuoteRead)
async def update_quote_endpoint(
    quote_id: uuid.UUID,
    body: QuoteWrite,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> QuoteRead:
    """Update a quote. Returns 409 if the quote is ACCEPTED."""
    _owner_only(user)
    company_id = _require_company_id(user)
    company_currency = await _get_company_currency(session, company_id)
    try:
        result = await update_quote(
            session,
            quote_id,
            body=body,
            company_id=company_id,
            company_currency=company_currency,
        )
    except ValueError as exc:
        # ACCEPTED-block raises ValueError; surface it as 409 to match spec
        msg = str(exc)
        if "ACCEPTED" in msg:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=msg
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=msg
        ) from exc
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found.")
    return result


@router.delete("/quotes/{quote_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_quote_endpoint(
    quote_id: uuid.UUID,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a quote (cascade removes lines/taxes). Number is not recycled."""
    _owner_only(user)
    company_id = _require_company_id(user)
    try:
        deleted = await delete_quote(session, quote_id, company_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found.")


@router.post("/quotes/{quote_id}/status", response_model=QuoteRead)
async def transition_status_endpoint(
    quote_id: uuid.UUID,
    body: QuoteStatusWrite,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> QuoteRead:
    """Transition quote status per M6 state machine."""
    _owner_only(user)
    company_id = _require_company_id(user)
    try:
        result = await transition_status(session, quote_id, company_id, body)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found.")
    return result


# ---------------------------------------------------------------------------
# Convert / Reactivate (step 3)
# ---------------------------------------------------------------------------


@router.post(
    "/quotes/{quote_id}/convert",
    response_model=InvoiceRead,
    status_code=status.HTTP_201_CREATED,
)
async def convert_quote_endpoint(
    quote_id: uuid.UUID,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> InvoiceRead:
    """Convert a quote to a new DRAFT/UNPAID invoice.

    Valid source statuses: SENT, ACCEPTED, EXPIRED (soft-expiry rule).
    Returns 409 if the quote was already converted.
    """
    _owner_only(user)
    company_id = _require_company_id(user)
    try:
        result = await convert_to_invoice(session, quote_id, company_id, creator_id=user.id)
    except ModeConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except ConversionConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except ValueError as exc:
        msg = str(exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=msg
        ) from exc
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found.")
    return result


@router.post("/quotes/{quote_id}/reactivate", response_model=QuoteRead)
async def reactivate_quote_endpoint(
    quote_id: uuid.UUID,
    body: QuoteReactivateWrite,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> QuoteRead:
    """Reactivate an EXPIRED quote: set status back to SENT and extend valid_until."""
    _owner_only(user)
    company_id = _require_company_id(user)
    try:
        result = await reactivate(session, quote_id, company_id, body)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found.")
    return result


# ---------------------------------------------------------------------------
# Quote email send + log (M9 step 6)
# ---------------------------------------------------------------------------


@router.post(
    "/quotes/{quote_id}/send",
    response_model=EmailLogRead,
    status_code=status.HTTP_200_OK,
)
async def send_quote_email_endpoint(
    quote_id: uuid.UUID,
    body: DocumentSendRequest,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> EmailLogRead:
    """Send a quote email with PDF attachment.

    Renders the PDF immediately (D5 – no cache), resolves the email template
    for the requested locale (D4), and sends via the configured SMTP (D6 –
    synchronous, no auto-retry).  Always writes an EmailLog row (SENT or FAILED).
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from jai.models._enums import EmailRelatedType, SettingLevel
    from jai.models.company import Company
    from jai.models.customer import Customer
    from jai.models.quote import Quote
    from jai.schemas.setting import SETTING_KEY_DOCUMENT_DEFAULTS, DocumentDefaultsSetting
    from jai.services.email import send_document_email
    from jai.services.pdf import render_quote_pdf, resolve_document_locale
    from jai.services.settings import get_setting

    _owner_only(user)
    company_id = _require_company_id(user)

    # -- Load quote scoped to company (cross-company → 404) -------------------
    stmt = (
        select(Quote)
        .where(Quote.id == quote_id, Quote.company_id == company_id)
        .options(selectinload(Quote.lines), selectinload(Quote.taxes))
    )
    result = await session.execute(stmt)
    quote = result.scalar_one_or_none()
    if quote is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Quote not found.",
        )

    # -- Load company ---------------------------------------------------------
    company_result = await session.execute(
        select(Company).where(Company.id == company_id)
    )
    company = company_result.scalar_one_or_none()
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Company not found.",
        )

    # -- Load customer (with addresses) ---------------------------------------
    customer_result = await session.execute(
        select(Customer)
        .where(Customer.id == quote.customer_id)
        .options(selectinload(Customer.addresses))
    )
    customer = customer_result.scalar_one_or_none()
    if customer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Customer not found.",
        )

    # -- Resolve locale via D2 chain ------------------------------------------
    company_default_setting = await get_setting(
        session,
        SETTING_KEY_DOCUMENT_DEFAULTS,
        level=SettingLevel.COMPANY,
        scope_id=company_id,
        value_type=DocumentDefaultsSetting,
    )
    company_default_locale: str | None = (
        company_default_setting.locale if company_default_setting is not None else None
    )
    customer_locale: str | None = getattr(customer, "locale", None)
    resolved_locale = resolve_document_locale(
        body.locale, customer_locale, company_default_locale
    )

    # -- Render PDF immediately (D5) ------------------------------------------
    pdf_bytes, filename = await render_quote_pdf(
        session, quote_id, company_id, locale=resolved_locale
    )

    # -- Normalise CC ---------------------------------------------------------
    cc_list: list[str] | None = None
    if body.cc:
        raw_parts = [p.strip() for p in body.cc.split(",") if p.strip()]
        cc_list = raw_parts if raw_parts else None

    # -- Send email + write log -----------------------------------------------
    log = await send_document_email(
        session=session,
        related_type=EmailRelatedType.QUOTE,
        doc=quote,
        company=company,
        customer=customer,
        to=str(body.to),
        cc=cc_list,
        locale=resolved_locale,
        subject=body.subject,
        body=body.body,
        pdf_bytes=pdf_bytes,
        filename=filename,
        creator_id=user.id,
    )

    await session.commit()
    return EmailLogRead.model_validate(log)


@router.get(
    "/quotes/{quote_id}/emails",
    response_model=EmailLogListResponse,
)
async def list_quote_emails_endpoint(
    quote_id: uuid.UUID,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> EmailLogListResponse:
    """Return the email send log for a quote (newest first)."""
    from sqlalchemy import select

    from jai.models._enums import EmailRelatedType
    from jai.models.email_log import EmailLog
    from jai.models.quote import Quote

    _owner_only(user)
    company_id = _require_company_id(user)
    await set_rls_company(session, company_id)

    # Verify quote belongs to company (cross-company → 404).
    quote_result = await session.execute(
        select(Quote).where(Quote.id == quote_id, Quote.company_id == company_id)
    )
    if quote_result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Quote not found.",
        )

    stmt = (
        select(EmailLog)
        .where(
            EmailLog.company_id == company_id,
            EmailLog.related_type == EmailRelatedType.QUOTE,
            EmailLog.related_id == quote_id,
        )
        .order_by(EmailLog.created_at.desc())
    )
    rows_result = await session.execute(stmt)
    rows = list(rows_result.scalars().all())

    return EmailLogListResponse(
        items=[EmailLogRead.model_validate(r) for r in rows],
        total=len(rows),
    )

"""Expense service – CRUD, list, and attachment management (M8 steps 1 + 2).

Public API
----------
- ``create_expense``        – validate guards, compute amounts/snapshots, persist.
- ``list_expenses``         – filtered/paginated/sorted list for the company.
- ``get_expense``           – fetch by id + company_id (404 guard).
- ``update_expense``        – recompute amounts/snapshots and write back.
- ``delete_expense``        – delete the expense; service deletes attachment disk
                              files before DB delete (D12); DB CASCADE removes rows.
- ``add_attachment``        – validate + save receipt file to storage + persist metadata.
- ``list_attachments``      – return all attachments for an expense (company-scoped).
- ``get_attachment_content``– load attachment content from storage for inline serving.
- ``delete_attachment``     – delete attachment DB row + disk file.

Red-line compliance
-------------------
1. All money logic (gross = net + vat, quantize_to_minor_unit) is here in
   the service.  Schema layer never computes amounts.
2. company_id is always injected by the service; front-end never provides it.
3. No manual cascade deletes – DB ON DELETE CASCADE handles sub-tables.
   Disk-file cleanup for attachments is explicit here (D12).
5. vat_treatment.side == PURCHASE guard – sales-side treatments are rejected.
7. Receipt storage uses only controlled filenames (UUID + whitelisted ext);
   client filename is stored for display only, never used in disk paths.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jai.models._enums import ExpenseKind, PaidBy, VatTreatmentSide
from jai.models.company import Company
from jai.models.dictionary import ExpenseCategory
from jai.models.expense import Expense
from jai.models.expense_attachment import ExpenseAttachment
from jai.models.vat import VatRate, VatTreatment
from jai.schemas.expense import (
    ExpenseAttachmentListResponse,
    ExpenseAttachmentRead,
    ExpenseCalculateRequest,
    ExpenseCalculateResult,
    ExpenseInput,
    ExpenseListItem,
    ExpenseListResponse,
    ExpenseRead,
)
from jai.services.mileage import _lock_mileage_company
from jai.services.money import quantize_to_minor_unit
from jai.services.storage import (
    MIME_TO_EXT,
    Storage,
    validate_receipt,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pure computation helpers (independently unit-testable, no DB)
# ---------------------------------------------------------------------------


def compute_expense_amounts(
    net: Decimal,
    vat: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """Compute (net, vat, gross) quantised to the currency minor unit.

    Both ``net`` and ``vat`` are authority inputs (D8: no validation that
    vat == net × rate).  Each is quantised independently to 2 dp (EUR cents),
    then gross = net + vat is quantised once more.

    Returns
    -------
    (net_q, vat_q, gross_q)
        All three quantised to 2 decimal places (ROUND_HALF_UP).

    Examples
    --------
    >>> from decimal import Decimal
    >>> compute_expense_amounts(Decimal("100"), Decimal("21"))
    (Decimal('100.00'), Decimal('21.00'), Decimal('121.00'))
    >>> # net×rate ≠ vat is accepted (D8)
    >>> compute_expense_amounts(Decimal("99.99"), Decimal("20.99"))
    (Decimal('99.99'), Decimal('20.99'), Decimal('120.98'))
    """
    net_q = quantize_to_minor_unit(net)
    vat_q = quantize_to_minor_unit(vat)
    gross_q = quantize_to_minor_unit(net_q + vat_q)
    return net_q, vat_q, gross_q


def compute_expense_vat_from_rate(
    net: Decimal,
    vat_rate_percent: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """Derive (net_q, vat_q, gross_q) from net × rate%, quantised to minor unit.

    Mirrors ``compute_expense_amounts``' rounding strategy: quantise *net*
    first, derive VAT from the quantised net, then gross = net_q + vat_q
    quantised once more.

    This is a **pure function** (no DB access) designed to be independently
    unit-testable.  The companion service function
    ``calculate_expense_preview`` loads the rate from the DB and calls this.

    Parameters
    ----------
    net:
        Raw net amount (excl. VAT).  Will be quantised to 2 dp as the first
        step.
    vat_rate_percent:
        The VAT rate as a percentage value, e.g. ``Decimal("21")`` for 21%.

    Returns
    -------
    (net_q, vat_q, gross_q)
        All three quantised to 2 decimal places (ROUND_HALF_UP).

    Examples
    --------
    >>> from decimal import Decimal
    >>> compute_expense_vat_from_rate(Decimal("100"), Decimal("21"))
    (Decimal('100.00'), Decimal('21.00'), Decimal('121.00'))
    >>> compute_expense_vat_from_rate(Decimal("0"), Decimal("21"))
    (Decimal('0.00'), Decimal('0.00'), Decimal('0.00'))
    """
    net_q = quantize_to_minor_unit(net)
    vat_q = quantize_to_minor_unit(net_q * vat_rate_percent / Decimal("100"))
    gross_q = quantize_to_minor_unit(net_q + vat_q)
    return net_q, vat_q, gross_q


def resolve_deductible(
    explicit: bool | None,
    category_default: bool | None,
) -> bool:
    """Resolve the effective deductible flag (D9).

    Priority:
    1. Explicit user input (if not None).
    2. ``category.default_deductible`` (if not None).
    3. Default → ``True``.

    Parameters
    ----------
    explicit:
        The value supplied in ``ExpenseInput.deductible`` (or None if omitted).
    category_default:
        The value of ``ExpenseCategory.default_deductible`` for the chosen
        category (or None if the category has no default set).

    Returns
    -------
    bool
        The effective deductible flag.
    """
    if explicit is not None:
        return explicit
    if category_default is not None:
        return category_default
    return True


# ---------------------------------------------------------------------------
# Internal DB helpers
# ---------------------------------------------------------------------------


async def _load_company(
    session: AsyncSession,
    company_id: uuid.UUID,
) -> Company:
    """Load company by id.  Raises ``LookupError`` if not found."""
    stmt = select(Company).where(Company.id == company_id)
    result = await session.execute(stmt)
    company = result.scalar_one_or_none()
    if company is None:
        raise LookupError("Company not found.")
    return company


async def _load_category(
    session: AsyncSession,
    category_id: uuid.UUID,
    company_id: uuid.UUID,
) -> ExpenseCategory:
    """Load expense category scoped to company.  Raises ``LookupError`` on miss."""
    stmt = select(ExpenseCategory).where(
        ExpenseCategory.id == category_id,
        ExpenseCategory.company_id == company_id,
    )
    result = await session.execute(stmt)
    cat = result.scalar_one_or_none()
    if cat is None:
        raise LookupError("Expense category not found or does not belong to this company.")
    return cat


async def _load_vat_treatment(
    session: AsyncSession,
    treatment_id: uuid.UUID,
    company_id: uuid.UUID,
) -> VatTreatment:
    """Load VAT treatment scoped to company.  Raises on miss or SALES side."""
    stmt = select(VatTreatment).where(
        VatTreatment.id == treatment_id,
        VatTreatment.company_id == company_id,
    )
    result = await session.execute(stmt)
    treatment = result.scalar_one_or_none()
    if treatment is None:
        raise LookupError("VAT treatment not found or does not belong to this company.")
    if VatTreatmentSide(treatment.side) != VatTreatmentSide.PURCHASE:
        raise ValueError(
            "VAT treatment is for the SALES side and cannot be used for an expense. "
            "Please select a PURCHASE-side treatment."
        )
    return treatment


async def _load_vat_rate(
    session: AsyncSession,
    rate_id: uuid.UUID,
    company_id: uuid.UUID,
) -> VatRate:
    """Load VAT rate scoped to company.  Raises ``LookupError`` on miss."""
    stmt = select(VatRate).where(
        VatRate.id == rate_id,
        VatRate.company_id == company_id,
    )
    result = await session.execute(stmt)
    rate = result.scalar_one_or_none()
    if rate is None:
        raise LookupError("VAT rate not found or does not belong to this company.")
    return rate


async def _load_expense(
    session: AsyncSession,
    expense_id: uuid.UUID,
    company_id: uuid.UUID,
) -> Expense:
    """Load an expense scoped to the company (red-line 2).

    Raises ``LookupError`` (→ HTTP 404) if the expense does not exist or
    belongs to a different company.
    """
    stmt = select(Expense).where(
        Expense.id == expense_id,
        Expense.company_id == company_id,
    )
    result = await session.execute(stmt)
    exp = result.scalar_one_or_none()
    if exp is None:
        raise LookupError("Expense not found.")
    return exp


def _expense_to_read(exp: Expense, attachment_count: int = 0) -> ExpenseRead:
    """Convert an ORM Expense to ExpenseRead schema."""
    return ExpenseRead(
        id=exp.id,
        kind=ExpenseKind(exp.kind),
        expense_date=exp.expense_date,
        category_id=exp.category_id,
        category_name=exp.category_name,
        supplier_name=exp.supplier_name,
        vat_treatment_id=exp.vat_treatment_id,
        vat_treatment_code=exp.vat_treatment_code,
        vat_treatment_label=exp.vat_treatment_label,
        vat_treatment_effect=exp.vat_treatment_effect,
        vat_rate_id=exp.vat_rate_id,
        vat_rate_percent=Decimal(str(exp.vat_rate_percent)),
        vat_rate_label=exp.vat_rate_label,
        net_amount=Decimal(str(exp.net_amount)),
        vat_amount=Decimal(str(exp.vat_amount)),
        gross_amount=Decimal(str(exp.gross_amount)),
        deductible=exp.deductible,
        currency=exp.currency,
        exchange_rate=Decimal(str(exp.exchange_rate)),
        base_net_amount=Decimal(str(exp.base_net_amount)),
        base_vat_amount=Decimal(str(exp.base_vat_amount)),
        base_gross_amount=Decimal(str(exp.base_gross_amount)),
        reference=exp.reference,
        note=exp.note,
        paid_by=PaidBy(exp.paid_by),
        business_percentage=Decimal(str(exp.business_percentage)),
        depreciation_years=int(exp.depreciation_years),
        is_draft=exp.is_draft,
        recurring_expense_id=getattr(exp, "recurring_expense_id", None),
        attachment_count=attachment_count,
        created_at=exp.created_at,
        updated_at=exp.updated_at,
    )


async def _apply_body(
    session: AsyncSession,
    exp: Expense,
    body: ExpenseInput,
    company_id: uuid.UUID,
) -> None:
    """Apply ExpenseInput fields to an Expense ORM object.

    Resolves all FK lookups, snapshots, amounts, and deductible default.
    Mutates ``exp`` in-place.  Does NOT flush or commit.
    """
    # -- Load company for base_currency (D10) ---------------------------------
    company = await _load_company(session, company_id)

    # -- Validate + snapshot category (D12) -----------------------------------
    category = await _load_category(session, body.category_id, company_id)

    # -- Validate + snapshot VAT treatment (D7, PURCHASE guard) ---------------
    treatment = await _load_vat_treatment(session, body.vat_treatment_id, company_id)

    # -- Validate + snapshot VAT rate (D7) ------------------------------------
    rate = await _load_vat_rate(session, body.vat_rate_id, company_id)

    # -- Compute amounts (D11: quantize_to_minor_unit) -------------------------
    net_q, vat_q, gross_q = compute_expense_amounts(body.net_amount, body.vat_amount)

    # -- Resolve deductible default (D9) ----------------------------------------
    effective_deductible = resolve_deductible(body.deductible, category.default_deductible)

    # -- Apply all fields to ORM object ----------------------------------------
    exp.company_id = company_id
    exp.expense_date = body.expense_date

    exp.category_id = category.id
    exp.category_name = category.name

    exp.supplier_name = body.supplier_name

    exp.vat_treatment_id = treatment.id
    exp.vat_treatment_code = str(treatment.code)
    exp.vat_treatment_label = str(treatment.label)
    exp.vat_treatment_effect = str(treatment.effect)

    exp.vat_rate_id = rate.id
    exp.vat_rate_percent = rate.percent
    exp.vat_rate_label = str(rate.label)

    exp.net_amount = net_q
    exp.vat_amount = vat_q
    exp.gross_amount = gross_q
    exp.deductible = effective_deductible

    # D10: single base currency – currency = company.base_currency, exchange_rate = 1
    exp.currency = company.base_currency
    exp.exchange_rate = Decimal("1")
    exp.base_net_amount = net_q
    exp.base_vat_amount = vat_q
    exp.base_gross_amount = gross_q

    exp.reference = body.reference
    exp.note = body.note

    # -- Bookkeeping fields (M8.5: pure storage, no calculation, D1–D4) -------
    exp.paid_by = body.paid_by.value
    exp.business_percentage = body.business_percentage
    exp.depreciation_years = body.depreciation_years


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------


async def create_expense(
    session: AsyncSession,
    company_id: uuid.UUID,
    body: ExpenseInput,
    creator_id: uuid.UUID | None,
) -> ExpenseRead:
    """Create a new expense record.

    Guards:
    - D12: category must belong to this company.
    - D7 + PURCHASE guard: treatment must belong to this company and be
      side == PURCHASE (sales-side treatment → 422).
    - D7: rate must belong to this company.
    - D10: currency = company.base_currency, exchange_rate = 1.
    - D11: net/vat quantised to minor unit; gross = net + vat.
    - D9: deductible defaults via category.default_deductible → True.
    """
    exp = await create_expense_record(session, company_id, body, creator_id)
    await session.commit()
    await session.refresh(exp)
    # New expense: no attachments yet
    return _expense_to_read(exp, attachment_count=0)


async def create_expense_record(
    session: AsyncSession,
    company_id: uuid.UUID,
    body: ExpenseInput,
    creator_id: uuid.UUID | None,
) -> Expense:
    """Build and flush a confirmed PURCHASE Expense without committing.

    Callers that coordinate the Expense with other rows can use this primitive
    to keep the entire operation inside their transaction.
    """
    exp = Expense()
    exp.kind = ExpenseKind.PURCHASE
    exp.is_draft = False
    exp.creator_id = creator_id
    await _apply_body(session, exp, body, company_id)
    session.add(exp)
    await session.flush()
    return exp


async def _count_attachments(
    session: AsyncSession,
    expense_id: uuid.UUID,
) -> int:
    """Return the number of attachments for a given expense."""
    stmt = select(func.count(ExpenseAttachment.id)).where(
        ExpenseAttachment.expense_id == expense_id
    )
    result = await session.execute(stmt)
    return result.scalar_one()


async def get_expense(
    session: AsyncSession,
    expense_id: uuid.UUID,
    company_id: uuid.UUID,
) -> ExpenseRead:
    """Fetch a single expense by id scoped to company.

    Raises ``LookupError`` (→ HTTP 404) if not found or cross-company.
    """
    exp = await _load_expense(session, expense_id, company_id)
    count = await _count_attachments(session, expense_id)
    return _expense_to_read(exp, count)


async def update_expense(
    session: AsyncSession,
    expense_id: uuid.UUID,
    company_id: uuid.UUID,
    body: ExpenseInput,
) -> ExpenseRead:
    """Edit an expense.  Same guards as create_expense.

    PUT semantics include confirmation: any PUT call sets ``is_draft = False``.
    Drafts produced by recurring-expense generation are confirmed by the first
    PUT (step-1 contract).  Already-confirmed expenses remain ``is_draft=False``.
    ``ExpenseInput`` never carries ``is_draft`` (red-line 1; it is derived here).
    """
    exp = await _load_expense(session, expense_id, company_id)
    if ExpenseKind(exp.kind) == ExpenseKind.MILEAGE:
        # Generated mileage projections are changed only through the trip endpoint,
        # which recomputes all money and snapshot fields atomically.
        from jai.services.mileage import MileageExpenseUpdateError

        raise MileageExpenseUpdateError(
            "Mileage expenses can only be updated through the mileage expense endpoint."
        )
    # PUT always confirms the draft (step-1 contract: PUT sets is_draft → False).
    exp.is_draft = False
    await _apply_body(session, exp, body, company_id)
    await session.commit()
    await session.refresh(exp)
    count = await _count_attachments(session, expense_id)
    return _expense_to_read(exp, count)


async def delete_expense(
    session: AsyncSession,
    expense_id: uuid.UUID,
    company_id: uuid.UUID,
    storage: Storage | None = None,
) -> None:
    """Delete an expense.

    Before deleting the DB row the service fetches all attachment ``storage_key``
    values for this expense and deletes the corresponding disk files via
    ``storage.delete()`` (D12).  Disk deletion failures are logged but do NOT
    abort the DB transaction – orphan files can be cleaned up later, but DB
    consistency takes priority.

    DB ON DELETE CASCADE then removes the ``expense_attachment`` metadata rows
    (red-line 3).
    """
    # Expense DELETE is also the supported root-delete path for Mileage. Take
    # the same company protocol before discovering the kind, so its DB cascade
    # cannot race a rate-recalculation transaction.
    await _lock_mileage_company(session, company_id)
    exp = await _load_expense(session, expense_id, company_id)

    # -- Collect attachment storage keys for disk cleanup (D12) ----------------
    if storage is not None:
        att_stmt = select(ExpenseAttachment.storage_key).where(
            ExpenseAttachment.expense_id == expense_id,
            ExpenseAttachment.company_id == company_id,
        )
        att_result = await session.execute(att_stmt)
        keys = list(att_result.scalars().all())
        for key in keys:
            try:
                storage.delete(key)
            except Exception:
                logger.exception(
                    "delete_expense: failed to delete disk file for key %r "
                    "(expense_id=%s); continuing with DB delete",
                    key,
                    expense_id,
                )

    await session.delete(exp)
    await session.commit()


async def list_expenses(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    q: str | None = None,
    category_id: uuid.UUID | None = None,
    vat_treatment_id: uuid.UUID | None = None,
    deductible: bool | None = None,
    is_draft: bool | None = None,
    kind: ExpenseKind | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "expense_date",
) -> ExpenseListResponse:
    """Return a paginated expenses list for the company.

    Mirrors the filter/pagination/sort contract of ``services.payment.list_payments``.

    Filters
    -------
    q
        ilike search against ``supplier_name``, ``reference``, and ``category_name``.
    category_id
        Exact match on ``expense.category_id``.
    vat_treatment_id
        Exact match on ``expense.vat_treatment_id``.
    deductible
        Exact match on ``expense.deductible``.
    is_draft
        Exact match on ``expense.is_draft``.
    date_from / date_to
        Inclusive range on ``expense_date``.

    Sort
    ----
    sort_by in {"expense_date", "created_at"}; default "expense_date".
    Primary sort DESC (newest first).

    All results are scoped to company_id (red-line 2).
    """
    base = select(Expense).where(Expense.company_id == company_id)

    # -- Filters ---------------------------------------------------------------
    if q:
        like = f"%{q}%"
        base = base.where(
            or_(
                Expense.supplier_name.ilike(like),
                Expense.reference.ilike(like),
                Expense.category_name.ilike(like),
            )
        )
    if category_id is not None:
        base = base.where(Expense.category_id == category_id)
    if vat_treatment_id is not None:
        base = base.where(Expense.vat_treatment_id == vat_treatment_id)
    if deductible is not None:
        base = base.where(Expense.deductible == deductible)
    if is_draft is not None:
        base = base.where(Expense.is_draft == is_draft)
    if kind is not None:
        base = base.where(Expense.kind == kind)
    if date_from is not None:
        base = base.where(Expense.expense_date >= date_from)
    if date_to is not None:
        base = base.where(Expense.expense_date <= date_to)

    # -- Count (total, unaffected by pagination) --------------------------------
    count_stmt = select(func.count()).select_from(base.subquery())
    count_result = await session.execute(count_stmt)
    total = count_result.scalar_one()

    # -- Sort ------------------------------------------------------------------
    sort_primary: object
    sort_secondary: object
    if sort_by == "created_at":
        sort_primary = Expense.created_at.desc()
        sort_secondary = Expense.expense_date.desc()
    else:
        # Default: expense_date (newest first); stable secondary: created_at
        sort_primary = Expense.expense_date.desc()
        sort_secondary = Expense.created_at.desc()

    data_stmt = base.order_by(sort_primary, sort_secondary).limit(limit).offset(offset)
    data_result = await session.execute(data_stmt)
    expenses = list(data_result.scalars().all())

    # -- Fetch attachment counts for all returned expense IDs ------------------
    expense_ids = [exp.id for exp in expenses]
    attachment_counts: dict[uuid.UUID, int] = {}
    if expense_ids:
        count_att_stmt = (
            select(ExpenseAttachment.expense_id, func.count(ExpenseAttachment.id))
            .where(ExpenseAttachment.expense_id.in_(expense_ids))
            .group_by(ExpenseAttachment.expense_id)
        )
        count_att_result = await session.execute(count_att_stmt)
        attachment_counts = {row[0]: row[1] for row in count_att_result.all()}

    items: list[ExpenseListItem] = [
        ExpenseListItem(
            id=exp.id,
            kind=ExpenseKind(exp.kind),
            expense_date=exp.expense_date,
            category_name=exp.category_name,
            supplier_name=exp.supplier_name,
            net_amount=Decimal(str(exp.net_amount)),
            vat_amount=Decimal(str(exp.vat_amount)),
            gross_amount=Decimal(str(exp.gross_amount)),
            deductible=exp.deductible,
            is_draft=exp.is_draft,
            attachment_count=attachment_counts.get(exp.id, 0),
            paid_by=PaidBy(exp.paid_by),
            business_percentage=Decimal(str(exp.business_percentage)),
            depreciation_years=int(exp.depreciation_years),
        )
        for exp in expenses
    ]

    return ExpenseListResponse(items=items, total=total)


async def calculate_expense_preview(
    session: AsyncSession,
    company_id: uuid.UUID,
    request: ExpenseCalculateRequest,
) -> ExpenseCalculateResult:
    """Return a VAT preview (net, vat, gross) without persisting anything.

    Loads the VAT rate from the DB (company-scoped), then delegates to the
    pure function ``compute_expense_vat_from_rate``.

    Parameters
    ----------
    session:
        Async SQLAlchemy session.
    company_id:
        The caller's company id (red-line 2: rate must belong to this company).
    request:
        ``ExpenseCalculateRequest`` carrying ``net_amount`` and ``vat_rate_id``.

    Returns
    -------
    ExpenseCalculateResult
        Quantised (net, vat, gross) plus the rate percent for display.

    Raises
    ------
    LookupError
        If ``vat_rate_id`` does not exist or belongs to a different company
        (caller maps this to HTTP 404).
    """
    rate = await _load_vat_rate(session, request.vat_rate_id, company_id)
    percent = Decimal(str(rate.percent))
    net_q, vat_q, gross_q = compute_expense_vat_from_rate(request.net_amount, percent)
    return ExpenseCalculateResult(
        net_amount=net_q,
        vat_amount=vat_q,
        gross_amount=gross_q,
        vat_rate_percent=percent,
    )


# ---------------------------------------------------------------------------
# Attachment service functions (step 2)
# ---------------------------------------------------------------------------


async def _load_attachment(
    session: AsyncSession,
    attachment_id: uuid.UUID,
    company_id: uuid.UUID,
) -> ExpenseAttachment:
    """Load an attachment scoped to the company.

    Raises ``LookupError`` (→ HTTP 404) if the attachment does not exist or
    belongs to a different company.
    """
    stmt = select(ExpenseAttachment).where(
        ExpenseAttachment.id == attachment_id,
        ExpenseAttachment.company_id == company_id,
    )
    result = await session.execute(stmt)
    att = result.scalar_one_or_none()
    if att is None:
        raise LookupError("Attachment not found.")
    return att


def _attachment_to_read(att: ExpenseAttachment) -> ExpenseAttachmentRead:
    """Convert ORM ExpenseAttachment to the read schema.

    ``storage_key`` is intentionally excluded from the schema (red-line 7 / D2).
    """
    if att.expense_id is None:
        raise ValueError("Pending inbox attachments are not Expense attachments.")
    return ExpenseAttachmentRead(
        id=att.id,
        expense_id=att.expense_id,
        filename=att.filename,
        mime_type=att.mime_type,
        byte_size=att.byte_size,
        created_at=att.created_at,
    )


async def add_attachment(
    session: AsyncSession,
    expense_id: uuid.UUID,
    company_id: uuid.UUID,
    content: bytes,
    mime_type: str,
    filename: str | None,
    creator_id: uuid.UUID | None,
    storage: Storage,
    max_receipt_bytes: int,
) -> ExpenseAttachmentRead:
    """Validate + save a receipt file and persist its metadata.

    Guards (red-line 7 / M8 D2):
    - MIME type must be in the receipt whitelist.
    - File size must not exceed ``max_receipt_bytes``.
    - Magic bytes must match the declared MIME type.
    - The storage key is constructed from UUIDs + whitelisted extension;
      the client-supplied ``filename`` is stored for display only.

    Raises
    ------
    LookupError
        If ``expense_id`` does not exist or belongs to a different company.
    ReceiptValidationError
        If the file fails MIME / size / magic-byte validation.
    """
    # Guard: expense must belong to this company
    _ = await _load_expense(session, expense_id, company_id)

    # Strip MIME type parameters (e.g. "image/png; charset=binary" → "image/png")
    clean_mime = mime_type.split(";")[0].strip().lower()

    # Validate content (raises ReceiptValidationError on failure)
    validate_receipt(content, clean_mime, max_receipt_bytes)

    # Build a controlled storage key from UUIDs + whitelisted extension (red-line 7)
    attachment_id = uuid.uuid4()
    ext = MIME_TO_EXT[clean_mime]
    relative_key = f"{company_id}/{expense_id}/{attachment_id}.{ext}"
    namespace = "receipts"

    # Persist to disk first; if this fails the metadata row is not created
    storage_key = storage.save(namespace, relative_key, content, clean_mime)

    # Persist metadata row
    att = ExpenseAttachment()
    att.id = attachment_id
    att.company_id = company_id
    att.expense_id = expense_id
    att.storage_key = storage_key
    att.filename = filename
    att.mime_type = clean_mime
    att.byte_size = len(content)
    att.creator_id = creator_id

    session.add(att)
    await session.commit()
    await session.refresh(att)
    return _attachment_to_read(att)


async def list_attachments(
    session: AsyncSession,
    expense_id: uuid.UUID,
    company_id: uuid.UUID,
) -> ExpenseAttachmentListResponse:
    """Return all attachments for an expense (company-scoped).

    Raises
    ------
    LookupError
        If ``expense_id`` does not exist or belongs to a different company.
    """
    # Guard: expense must belong to this company
    _ = await _load_expense(session, expense_id, company_id)

    stmt = (
        select(ExpenseAttachment)
        .where(
            ExpenseAttachment.expense_id == expense_id,
            ExpenseAttachment.company_id == company_id,
        )
        .order_by(ExpenseAttachment.created_at.asc())
    )
    result = await session.execute(stmt)
    attachments = list(result.scalars().all())
    return ExpenseAttachmentListResponse(items=[_attachment_to_read(a) for a in attachments])


async def get_attachment_content(
    session: AsyncSession,
    attachment_id: uuid.UUID,
    company_id: uuid.UUID,
    storage: Storage,
) -> tuple[bytes, str]:
    """Return (content_bytes, mime_type) for an attachment.

    The caller is expected to stream the bytes with ``Content-Disposition: inline``
    and ``Content-Type`` set to the returned MIME type.

    Raises
    ------
    LookupError
        If the attachment does not exist or belongs to a different company.
    FileNotFoundError
        If the disk file is missing (storage inconsistency).
    """
    from jai.db import set_rls_company

    await set_rls_company(session, company_id)
    att = await _load_attachment(session, attachment_id, company_id)
    content = storage.open(att.storage_key)
    return content, att.mime_type


async def delete_attachment(
    session: AsyncSession,
    attachment_id: uuid.UUID,
    company_id: uuid.UUID,
    storage: Storage,
) -> None:
    """Delete an attachment: disk file first, then DB row.

    Disk deletion failures are logged but do NOT abort the DB transaction
    (orphan files can be cleaned up later; DB consistency takes priority).

    Raises
    ------
    LookupError
        If the attachment does not exist or belongs to a different company.
    """
    from jai.db import set_rls_company

    await set_rls_company(session, company_id)
    att = await _load_attachment(session, attachment_id, company_id)
    if isinstance(att.receipt_inbox_id, uuid.UUID):
        raise LookupError("Pending inbox attachments can only be removed through the inbox.")
    key = att.storage_key

    # Delete disk file (best-effort; failures logged but not raised)
    try:
        storage.delete(key)
    except Exception:
        logger.exception(
            "delete_attachment: failed to delete disk file for key %r "
            "(attachment_id=%s); continuing with DB delete",
            key,
            attachment_id,
        )

    await session.delete(att)
    await session.commit()

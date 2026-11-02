"""Recurring expense service – CRUD + generation job (M8 step 3).

Public API
----------
- ``compute_next_run``                     – pure function: advance by one period.
- ``create_recurring_expense``             – validate guards, compute amounts/snapshots, persist.
- ``list_recurring_expenses``              – filtered/paginated list for the company.
- ``get_recurring_expense``                – fetch by id + company_id (404 guard).
- ``update_recurring_expense``             – recompute amounts/snapshots and write back.
- ``delete_recurring_expense``             – delete the template (keeps generated expenses).
- ``generate_due_recurring_expenses_all``  – idempotent bulk job (APScheduler / run-now).
- ``generate_due_for_template``            – single-template generation (run-now endpoint).

Red-line compliance
-------------------
1. All money logic is here in the service (amounts reuse ``compute_expense_amounts``
   from services.expense; no duplication).
2. company_id is always injected by the service; front-end never provides it.
3. No manual cascade deletes; the ORM / DB handles the recurring_expense_id SET NULL.
5. vat_treatment.side == PURCHASE guard (same as step 1).
"""

from __future__ import annotations

import calendar
import logging
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jai.models._enums import PaidBy, PlCostBasis, RecurringFrequency
from jai.models.company import Company
from jai.models.dictionary import ExpenseCategory
from jai.models.expense import Expense
from jai.models.recurring_expense import RecurringExpense
from jai.schemas.expense import ExpenseCostPreviewResult
from jai.schemas.recurring_expense import (
    RecurringExpenseInput,
    RecurringExpenseListResponse,
    RecurringExpenseRead,
    RunNowResponse,
)
from jai.services.expense import (
    _load_vat_rate,
    _load_vat_treatment,
    compute_expense_amounts,
    compute_stored_cost_preview,
    resolve_deductible,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure computation – independently unit-testable
# ---------------------------------------------------------------------------


def compute_next_run(frequency: RecurringFrequency, from_date: date) -> date:
    """Advance ``from_date`` by one period and return the new date.

    Period rules:
    - MONTHLY   → same day next month (or last day of that month if the day
                  would overflow, e.g. Jan 31 → Feb 28/29).
    - QUARTERLY → same day 3 months ahead (same overflow rule).
    - YEARLY    → same day next year (same overflow rule; handles Feb 29 leap
                  years by falling back to Feb 28).

    Implementation uses only the standard library (``calendar.monthrange``).
    No external dependencies are introduced.

    Parameters
    ----------
    frequency:
        ``RecurringFrequency.MONTHLY``, ``QUARTERLY``, or ``YEARLY``.
    from_date:
        The date from which to advance.

    Returns
    -------
    date
        The next scheduled date.

    Examples
    --------
    >>> from datetime import date
    >>> from jai.models._enums import RecurringFrequency
    >>> compute_next_run(RecurringFrequency.MONTHLY, date(2026, 1, 31))
    datetime.date(2026, 2, 28)
    >>> compute_next_run(RecurringFrequency.MONTHLY, date(2026, 3, 31))
    datetime.date(2026, 4, 30)
    >>> compute_next_run(RecurringFrequency.QUARTERLY, date(2025, 11, 30))
    datetime.date(2026, 2, 28)
    >>> compute_next_run(RecurringFrequency.YEARLY, date(2024, 2, 29))
    datetime.date(2025, 2, 28)
    """
    if frequency == RecurringFrequency.MONTHLY:
        months_to_add = 1
    elif frequency == RecurringFrequency.QUARTERLY:
        months_to_add = 3
    else:  # YEARLY
        months_to_add = 12

    year = from_date.year
    month = from_date.month + months_to_add

    # Normalise month overflow (e.g. month=13 → year+1, month=1)
    while month > 12:
        month -= 12
        year += 1

    # Clamp day to the last valid day of the target month (month-end retirement)
    last_day = calendar.monthrange(year, month)[1]
    day = min(from_date.day, last_day)

    return date(year, month, day)


# ---------------------------------------------------------------------------
# Internal DB helpers
# ---------------------------------------------------------------------------


async def _load_company(
    session: AsyncSession,
    company_id: uuid.UUID,
) -> Company:
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
    stmt = select(ExpenseCategory).where(
        ExpenseCategory.id == category_id,
        ExpenseCategory.company_id == company_id,
    )
    result = await session.execute(stmt)
    cat = result.scalar_one_or_none()
    if cat is None:
        raise LookupError("Expense category not found or does not belong to this company.")
    return cat


async def _load_recurring_expense(
    session: AsyncSession,
    recurring_id: uuid.UUID,
    company_id: uuid.UUID,
) -> RecurringExpense:
    """Load a recurring expense template scoped to the company."""
    stmt = select(RecurringExpense).where(
        RecurringExpense.id == recurring_id,
        RecurringExpense.company_id == company_id,
    )
    result = await session.execute(stmt)
    rec = result.scalar_one_or_none()
    if rec is None:
        raise LookupError("Recurring expense not found.")
    return rec


async def get_recurring_expense_cost_preview(
    session: AsyncSession,
    recurring_id: uuid.UUID,
    company_id: uuid.UUID,
) -> ExpenseCostPreviewResult:
    """Preview one generated Expense from the template's actual stored snapshots."""
    rec = await _load_recurring_expense(session, recurring_id, company_id)
    return compute_stored_cost_preview(rec)


def _recurring_to_read(rec: RecurringExpense) -> RecurringExpenseRead:
    """Convert an ORM RecurringExpense to RecurringExpenseRead schema."""
    return RecurringExpenseRead(
        id=rec.id,
        name=rec.name,
        category_id=rec.category_id,
        category_name=rec.category_name,
        supplier_name=rec.supplier_name,
        vat_treatment_id=rec.vat_treatment_id,
        vat_treatment_code=rec.vat_treatment_code,
        vat_treatment_label=rec.vat_treatment_label,
        vat_treatment_effect=rec.vat_treatment_effect,
        vat_rate_id=rec.vat_rate_id,
        vat_rate_percent=Decimal(str(rec.vat_rate_percent)),
        vat_rate_label=rec.vat_rate_label,
        net_amount=Decimal(str(rec.net_amount)),
        vat_amount=Decimal(str(rec.vat_amount)),
        gross_amount=Decimal(str(rec.gross_amount)),
        deductible=rec.deductible,
        note=rec.note,
        paid_by=PaidBy(rec.paid_by),
        business_percentage=Decimal(str(rec.business_percentage)),
        depreciation_years=int(rec.depreciation_years),
        include_in_pl=rec.include_in_pl,
        pl_cost_basis=PlCostBasis(rec.pl_cost_basis),
        frequency=rec.frequency,
        start_date=rec.start_date,
        end_date=rec.end_date,
        max_occurrences=rec.max_occurrences,
        occurrences_generated=rec.occurrences_generated,
        next_run_date=rec.next_run_date,
        last_generated_at=rec.last_generated_at,
        active=rec.active,
        created_at=rec.created_at,
        updated_at=rec.updated_at,
    )


async def _apply_body(
    session: AsyncSession,
    rec: RecurringExpense,
    body: RecurringExpenseInput,
    company_id: uuid.UUID,
    *,
    is_create: bool,
) -> None:
    """Apply RecurringExpenseInput fields to a RecurringExpense ORM object.

    Resolves FK lookups, snapshots, amounts, and deductible default.
    Mutates ``rec`` in-place.  Does NOT flush or commit.

    ``is_create`` controls whether ``next_run_date`` is initialised from
    ``start_date``; on update the field is preserved unless ``start_date``
    changes (service always re-derives on create; on update it preserves the
    existing schedule cursor).
    """
    # -- Validate + snapshot category (D12) -----------------------------------
    category = await _load_category(session, body.category_id, company_id)

    # -- Validate + snapshot VAT treatment (D7, PURCHASE guard) ---------------
    # Reuse helpers from services.expense to avoid duplicating logic (red-line 1)
    treatment = await _load_vat_treatment(session, body.vat_treatment_id, company_id)

    # -- Validate + snapshot VAT rate (D7) ------------------------------------
    rate = await _load_vat_rate(session, body.vat_rate_id, company_id)

    # -- Compute amounts (D11: quantize_to_minor_unit) -------------------------
    net_q, vat_q, gross_q = compute_expense_amounts(body.net_amount, body.vat_amount)

    # -- Resolve deductible default (D9) ----------------------------------------
    effective_deductible = resolve_deductible(body.deductible, category.default_deductible)

    # -- Apply all fields to ORM object ----------------------------------------
    rec.company_id = company_id
    rec.name = body.name

    rec.category_id = category.id
    rec.category_name = category.name

    rec.supplier_name = body.supplier_name

    rec.vat_treatment_id = treatment.id
    rec.vat_treatment_code = str(treatment.code)
    rec.vat_treatment_label = str(treatment.label)
    rec.vat_treatment_effect = str(treatment.effect)

    rec.vat_rate_id = rate.id
    rec.vat_rate_percent = rate.percent
    rec.vat_rate_label = str(rate.label)

    rec.net_amount = net_q
    rec.vat_amount = vat_q
    rec.gross_amount = gross_q
    rec.deductible = effective_deductible
    rec.note = body.note

    # -- Bookkeeping fields (M8.5 parity: pure storage, no calculation, D8) --
    rec.paid_by = body.paid_by.value
    rec.business_percentage = body.business_percentage
    rec.depreciation_years = body.depreciation_years
    if "include_in_pl" in body.model_fields_set or is_create:
        rec.include_in_pl = body.include_in_pl
    if "pl_cost_basis" in body.model_fields_set or is_create:
        rec.pl_cost_basis = body.pl_cost_basis.value

    rec.frequency = body.frequency
    rec.start_date = body.start_date
    rec.end_date = body.end_date
    rec.max_occurrences = body.max_occurrences
    rec.active = body.active

    # On create (or if start_date changed) reset next_run_date to start_date.
    # On update we only do so if start_date has changed, which covers the
    # common edit case where a user adjusts the template.
    if is_create:
        rec.next_run_date = body.start_date


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------


async def create_recurring_expense(
    session: AsyncSession,
    company_id: uuid.UUID,
    body: RecurringExpenseInput,
    creator_id: uuid.UUID | None,
) -> RecurringExpenseRead:
    """Create a new recurring expense template."""
    rec = RecurringExpense()
    rec.occurrences_generated = 0
    rec.last_generated_at = None
    rec.creator_id = creator_id
    await _apply_body(session, rec, body, company_id, is_create=True)
    session.add(rec)
    await session.commit()
    await session.refresh(rec)
    return _recurring_to_read(rec)


async def get_recurring_expense(
    session: AsyncSession,
    recurring_id: uuid.UUID,
    company_id: uuid.UUID,
) -> RecurringExpenseRead:
    """Fetch a single recurring expense template by id (company-scoped)."""
    rec = await _load_recurring_expense(session, recurring_id, company_id)
    return _recurring_to_read(rec)


async def update_recurring_expense(
    session: AsyncSession,
    recurring_id: uuid.UUID,
    company_id: uuid.UUID,
    body: RecurringExpenseInput,
) -> RecurringExpenseRead:
    """Update a recurring expense template.

    Updates amounts/snapshots; preserves ``next_run_date`` and
    ``occurrences_generated`` unless ``start_date`` changes (in which case
    ``next_run_date`` is reset to the new ``start_date``).
    Also supports toggling ``active`` to pause/resume.
    """
    rec = await _load_recurring_expense(session, recurring_id, company_id)

    old_start = rec.start_date
    await _apply_body(session, rec, body, company_id, is_create=False)

    # If start_date changed reset next_run_date so the schedule re-aligns.
    if rec.start_date != old_start:
        rec.next_run_date = rec.start_date

    await session.commit()
    await session.refresh(rec)
    return _recurring_to_read(rec)


async def delete_recurring_expense(
    session: AsyncSession,
    recurring_id: uuid.UUID,
    company_id: uuid.UUID,
) -> None:
    """Delete a recurring expense template.

    Generated expenses are NOT deleted; their ``recurring_expense_id``
    column is set to NULL by the DB ON DELETE SET NULL constraint so they
    remain as independent accounting records (D3).
    """
    rec = await _load_recurring_expense(session, recurring_id, company_id)
    await session.delete(rec)
    await session.commit()


async def list_recurring_expenses(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    active: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> RecurringExpenseListResponse:
    """Return a paginated list of recurring expense templates for the company."""
    base = select(RecurringExpense).where(RecurringExpense.company_id == company_id)

    if active is not None:
        base = base.where(RecurringExpense.active == active)

    count_stmt = select(func.count()).select_from(base.subquery())
    count_result = await session.execute(count_stmt)
    total = count_result.scalar_one()

    data_stmt = (
        base.order_by(RecurringExpense.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    data_result = await session.execute(data_stmt)
    templates = list(data_result.scalars().all())

    return RecurringExpenseListResponse(
        items=[_recurring_to_read(r) for r in templates],
        total=total,
    )


# ---------------------------------------------------------------------------
# Generation logic
# ---------------------------------------------------------------------------


def _is_limit_reached(rec: RecurringExpense) -> bool:
    """Return True if the template has reached its generation limit.

    ``end_date`` is checked against ``rec.next_run_date`` (the date of the
    occurrence *about to be generated*), not against the wall-clock today.
    This ensures that in job-delay / run-now catch-up scenarios where
    ``next_run_date <= end_date < today``, the last occurrence is still
    generated (consistent with the ``max_occurrences`` path which also allows
    the final occurrence through before deactivating).
    """
    if rec.end_date is not None and rec.next_run_date > rec.end_date:
        return True
    if rec.max_occurrences is not None and rec.occurrences_generated >= rec.max_occurrences:
        return True
    return False


def _should_deactivate_after_generation(rec: RecurringExpense) -> bool:
    """Return True if the template should be deactivated after this generation.

    Called after incrementing ``occurrences_generated`` and advancing
    ``next_run_date``.
    """
    # max_occurrences reached
    if rec.max_occurrences is not None and rec.occurrences_generated >= rec.max_occurrences:
        return True
    # end_date: next run would be after end_date
    if rec.end_date is not None and rec.next_run_date > rec.end_date:
        return True
    return False


def _clone_expense_from_template(
    rec: RecurringExpense,
    today: date,
    creator_id: uuid.UUID | None,
) -> Expense:
    """Create a draft Expense by cloning the template's snapshot fields."""
    exp = Expense()
    exp.company_id = rec.company_id
    exp.expense_date = today
    exp.category_id = rec.category_id
    exp.category_name = rec.category_name
    exp.supplier_name = rec.supplier_name
    exp.vat_treatment_id = rec.vat_treatment_id
    exp.vat_treatment_code = rec.vat_treatment_code
    exp.vat_treatment_label = rec.vat_treatment_label
    exp.vat_treatment_effect = rec.vat_treatment_effect
    exp.vat_rate_id = rec.vat_rate_id
    exp.vat_rate_percent = rec.vat_rate_percent
    exp.vat_rate_label = rec.vat_rate_label
    exp.net_amount = rec.net_amount
    exp.vat_amount = rec.vat_amount
    exp.gross_amount = rec.gross_amount
    exp.deductible = rec.deductible
    # D10: single base currency – mirrors what _apply_body does on Expense
    # We use a placeholder; the actual company.base_currency is captured in the
    # template's snapshot (we store amounts, not currency here; currency is
    # always the company's base currency so we load it from the template's company).
    # For generation we must load the company; do this in the caller.
    exp.note = rec.note
    # -- Bookkeeping fields (M8.5 D8: copy template values to generated expense) --
    exp.paid_by = rec.paid_by
    exp.business_percentage = rec.business_percentage
    exp.depreciation_years = rec.depreciation_years
    exp.include_in_pl = rec.include_in_pl
    exp.pl_cost_basis = rec.pl_cost_basis
    exp.is_draft = True
    exp.recurring_expense_id = rec.id
    exp.creator_id = creator_id
    return exp


async def _get_company_currency(
    session: AsyncSession,
    company_id: uuid.UUID,
) -> str:
    """Return the base_currency for the given company."""
    stmt = select(Company.base_currency).where(Company.id == company_id)
    result = await session.execute(stmt)
    currency = result.scalar_one_or_none()
    if currency is None:
        raise LookupError(f"Company {company_id} not found during generation.")
    return str(currency)


async def _generate_one(
    session: AsyncSession,
    rec: RecurringExpense,
    today: date,
    *,
    company_currencies: dict[uuid.UUID, str],
) -> bool:
    """Generate a single draft expense from a template and advance the cursor.

    Returns True if an expense was generated, False if the template was already
    up-to-date or deactivated.

    The caller is responsible for flushing / committing.
    """
    if not rec.active:
        return False

    if _is_limit_reached(rec):
        rec.active = False
        return False

    if rec.next_run_date > today:
        return False

    # Load company currency (cached in caller-provided dict)
    if rec.company_id not in company_currencies:
        company_currencies[rec.company_id] = await _get_company_currency(
            session, rec.company_id
        )
    currency = company_currencies[rec.company_id]

    # Clone expense from template
    exp = _clone_expense_from_template(rec, today=rec.next_run_date, creator_id=None)
    exp.currency = currency
    exp.exchange_rate = Decimal("1")
    exp.base_net_amount = rec.net_amount
    exp.base_vat_amount = rec.vat_amount
    exp.base_gross_amount = rec.gross_amount

    session.add(exp)

    # Advance template cursor (idempotent: cursor moves forward in same transaction)
    rec.occurrences_generated += 1
    rec.last_generated_at = datetime.now(tz=UTC)
    freq = RecurringFrequency(rec.frequency)
    rec.next_run_date = compute_next_run(freq, rec.next_run_date)

    # Deactivate if now at limit
    if _should_deactivate_after_generation(rec):
        rec.active = False

    return True


async def generate_due_recurring_expenses_all(
    session: AsyncSession,
) -> int:
    """Idempotent generation of all due recurring expenses across all companies.

    Iterates all ``active`` templates whose ``next_run_date <= today``.
    For each, clones a ``is_draft=True`` expense and advances the template cursor.
    Running twice produces the same result because the cursor is advanced inside
    the same transaction (idempotent by design).

    Does NOT commit – the caller (APScheduler job or run-now endpoint) wraps this
    in a transaction and commits.  Mirrors ``expire_due_quotes_all`` convention.

    Returns
    -------
    int
        Total number of draft expenses generated.
    """
    today = date.today()
    stmt = (
        select(RecurringExpense)
        .where(
            RecurringExpense.active.is_(True),
            RecurringExpense.next_run_date <= today,
        )
        .order_by(RecurringExpense.next_run_date.asc())
    )
    result = await session.execute(stmt)
    templates = list(result.scalars().all())

    company_currencies: dict[uuid.UUID, str] = {}
    total_generated = 0
    for rec in templates:
        generated = await _generate_one(
            session, rec, today, company_currencies=company_currencies
        )
        if generated:
            total_generated += 1

    return total_generated


async def generate_due_for_template(
    session: AsyncSession,
    recurring_id: uuid.UUID,
    company_id: uuid.UUID,
) -> RunNowResponse:
    """Generate due occurrences for a single recurring expense template.

    Used by the ``POST /recurring-expenses/{id}/run-now`` endpoint.
    Unlike the bulk job this operates only on the specified template and
    commits the transaction itself (so the endpoint stays thin).

    Semantics are identical to ``generate_due_recurring_expenses_all`` but
    scoped to one template – idempotent, same cursor-advance logic.

    Returns
    -------
    RunNowResponse
        ``generated`` = count of draft expenses created; ``next_run_date``
        = the template's next_run_date after the run.
    """
    rec = await _load_recurring_expense(session, recurring_id, company_id)
    today = date.today()

    company_currencies: dict[uuid.UUID, str] = {}
    total_generated = 0
    generated = await _generate_one(
        session, rec, today, company_currencies=company_currencies
    )
    if generated:
        total_generated += 1

    await session.commit()
    await session.refresh(rec)
    return RunNowResponse(generated=total_generated, next_run_date=rec.next_run_date)

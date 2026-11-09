"""Read-only Agent expense access through the existing expense services."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jai.db import set_rls_company
from jai.models._enums import ExpenseKind
from jai.schemas.agent_tools import ExpenseListOutput, ExpenseOutput
from jai.schemas.expense import ExpenseRead
from jai.services.agent_tokens import AgentPrincipal
from jai.services.expense import get_expense as get_expense_service
from jai.services.expense import list_expenses as list_expenses_service


async def list_expenses(
    session_maker: async_sessionmaker[AsyncSession],
    principal: AgentPrincipal,
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
) -> ExpenseListOutput:
    """List expenses in one transaction with transaction-local company RLS."""
    async with session_maker() as session, session.begin():
        await set_rls_company(session, principal.company_id)
        result = await list_expenses_service(
            session,
            principal.company_id,
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
        return ExpenseListOutput(items=result.items, total=result.total)


async def get_expense(
    session_maker: async_sessionmaker[AsyncSession],
    principal: AgentPrincipal,
    expense_id: uuid.UUID,
) -> ExpenseOutput | None:
    """Read one full Expense under company RLS; foreign IDs are indistinguishable."""
    async with session_maker() as session, session.begin():
        await set_rls_company(session, principal.company_id)
        try:
            row: ExpenseRead = await get_expense_service(
                session, expense_id, principal.company_id
            )
        except LookupError:
            return None
        return ExpenseOutput(expense=row)

"""Content library service – CRUD for document templates, content blocks,
note templates (M6 step 4).

Public API
----------
- ``create_document_template`` / ``update_document_template``
- ``get_document_template`` / ``list_document_templates`` / ``delete_document_template``
- ``create_content_block`` / ``update_content_block``
- ``get_content_block`` / ``list_content_blocks`` / ``delete_content_block``
- ``create_note_template`` / ``update_note_template``
- ``get_note_template`` / ``list_note_templates`` / ``delete_note_template``

All functions require ``company_id`` (injected by the API layer, never from
the client).  Owner-only access is enforced at the route layer.

Red-line compliance
-------------------
- No money calculation in this module.
- company_id always injected by service; front-end never provides it.
- DB ON DELETE CASCADE handles template_line deletion automatically.
- is_default partial unique enforced at DB level + service-level pre-check.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jai.models._enums import ContentBlockKind, DocumentTemplateScope
from jai.models.content import (
    ContentBlock,
    DocumentTemplate,
    DocumentTemplateLine,
    NoteTemplate,
)
from jai.models.dictionary import Unit
from jai.models.vat import VatRate
from jai.schemas.content import (
    ContentBlockRead,
    ContentBlockWrite,
    DocumentTemplateLineRead,
    DocumentTemplateLineWrite,
    DocumentTemplateRead,
    DocumentTemplateWrite,
    NoteTemplateRead,
    NoteTemplateWrite,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _template_line_to_read(line: DocumentTemplateLine) -> DocumentTemplateLineRead:
    return DocumentTemplateLineRead(
        id=line.id,
        sort_order=line.sort_order,
        name=line.name,
        description=line.description,
        quantity=Decimal(str(line.quantity)),
        unit_id=line.unit_id,
        unit_name=line.unit_name,
        unit_price=Decimal(str(line.unit_price)) if line.unit_price is not None else None,
        discount_type=line.discount_type,
        discount_value=Decimal(str(line.discount_value)),
        vat_rate_id=line.vat_rate_id,
    )


def _template_to_read(tpl: DocumentTemplate) -> DocumentTemplateRead:
    return DocumentTemplateRead(
        id=tpl.id,
        company_id=tpl.company_id,
        name=tpl.name,
        applies_to=DocumentTemplateScope(tpl.applies_to),
        lines=[_template_line_to_read(line) for line in tpl.lines],
        created_at=tpl.created_at,
        updated_at=tpl.updated_at,
    )


def _content_block_to_read(block: ContentBlock) -> ContentBlockRead:
    return ContentBlockRead(
        id=block.id,
        company_id=block.company_id,
        kind=ContentBlockKind(block.kind),
        name=block.name,
        body=block.body,
        is_default=block.is_default,
        created_at=block.created_at,
        updated_at=block.updated_at,
    )


def _note_template_to_read(nt: NoteTemplate) -> NoteTemplateRead:
    return NoteTemplateRead(
        id=nt.id,
        company_id=nt.company_id,
        name=nt.name,
        body=nt.body,
        created_at=nt.created_at,
        updated_at=nt.updated_at,
    )


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


async def _validate_line_fks(
    session: AsyncSession,
    company_id: uuid.UUID,
    lines: list[DocumentTemplateLineWrite],
) -> None:
    """Validate unit_id and vat_rate_id on template lines belong to this company."""
    unit_ids = {line.unit_id for line in lines if line.unit_id is not None}
    vat_rate_ids = {line.vat_rate_id for line in lines if line.vat_rate_id is not None}

    if unit_ids:
        count_stmt = select(func.count()).where(
            Unit.id.in_(unit_ids),
            Unit.company_id == company_id,
        )
        found = (await session.execute(count_stmt)).scalar_one()
        if found != len(unit_ids):
            raise ValueError(
                "One or more unit IDs not found or do not belong to this company."
            )

    if vat_rate_ids:
        count_stmt = select(func.count()).where(
            VatRate.id.in_(vat_rate_ids),
            VatRate.company_id == company_id,
        )
        found = (await session.execute(count_stmt)).scalar_one()
        if found != len(vat_rate_ids):
            raise ValueError(
                "One or more VAT rate IDs not found or do not belong to this company."
            )


async def _clear_existing_default(
    session: AsyncSession,
    company_id: uuid.UUID,
    kind: ContentBlockKind,
    exclude_id: uuid.UUID | None = None,
) -> None:
    """Clear is_default on existing default block of the same kind.

    The partial unique index guards at DB level; this is a pre-flight
    convenience to produce a clear error vs. an IntegrityError.
    """
    stmt = select(ContentBlock).where(
        ContentBlock.company_id == company_id,
        ContentBlock.kind == kind,
        ContentBlock.is_default == True,  # noqa: E712
    )
    if exclude_id is not None:
        stmt = stmt.where(ContentBlock.id != exclude_id)
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing is not None:
        existing.is_default = False
        await session.flush()  # ensure DB sees the clear before next write


# ---------------------------------------------------------------------------
# Document Template CRUD
# ---------------------------------------------------------------------------


async def create_document_template(
    session: AsyncSession,
    body: DocumentTemplateWrite,
    company_id: uuid.UUID,
) -> DocumentTemplateRead:
    """Create a new document template with its lines."""
    await _validate_line_fks(session, company_id, body.lines)

    # Pre-flight: check name uniqueness to avoid IntegrityError on flush
    existing = await session.execute(
        select(DocumentTemplate).where(
            DocumentTemplate.company_id == company_id,
            DocumentTemplate.name == body.name,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise ValueError(f"Document template with name '{body.name}' already exists.")

    tpl = DocumentTemplate()
    tpl.company_id = company_id
    tpl.name = body.name
    tpl.applies_to = body.applies_to
    session.add(tpl)
    await session.flush()  # get tpl.id for FK

    for i, line_in in enumerate(body.lines):
        line_row = DocumentTemplateLine()
        line_row.template_id = tpl.id
        line_row.sort_order = i
        line_row.name = line_in.name
        line_row.description = line_in.description
        line_row.quantity = line_in.quantity
        line_row.unit_id = line_in.unit_id
        line_row.unit_name = line_in.unit_name
        line_row.unit_price = line_in.unit_price
        line_row.discount_type = line_in.discount_type
        line_row.discount_value = line_in.discount_value
        line_row.vat_rate_id = line_in.vat_rate_id
        session.add(line_row)

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ValueError(
            f"Document template with name '{body.name}' already exists."
        ) from exc

    await session.refresh(tpl)
    return _template_to_read(tpl)


async def get_document_template(
    session: AsyncSession,
    template_id: uuid.UUID,
    company_id: uuid.UUID,
) -> DocumentTemplateRead | None:
    """Return a single document template by ID (scoped to company)."""
    stmt = select(DocumentTemplate).where(
        DocumentTemplate.id == template_id,
        DocumentTemplate.company_id == company_id,
    )
    result = await session.execute(stmt)
    tpl = result.scalar_one_or_none()
    if tpl is None:
        return None
    return _template_to_read(tpl)


async def list_document_templates(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    applies_to: DocumentTemplateScope | None = None,
    limit: int | None = None,
) -> list[DocumentTemplateRead]:
    """Return company templates, optionally filtered and bounded."""
    stmt = (
        select(DocumentTemplate)
        .where(DocumentTemplate.company_id == company_id)
        .order_by(DocumentTemplate.name)
    )
    if applies_to is not None:
        # BOTH matches any filter; otherwise exact match on scope or BOTH.
        stmt = stmt.where(
            (DocumentTemplate.applies_to == applies_to)
            | (DocumentTemplate.applies_to == DocumentTemplateScope.BOTH)
        )
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    rows = result.scalars().all()
    return [_template_to_read(tpl) for tpl in rows]


async def update_document_template(
    session: AsyncSession,
    template_id: uuid.UUID,
    body: DocumentTemplateWrite,
    company_id: uuid.UUID,
) -> DocumentTemplateRead | None:
    """Update a document template: replace all fields + lines."""
    stmt = select(DocumentTemplate).where(
        DocumentTemplate.id == template_id,
        DocumentTemplate.company_id == company_id,
    )
    result = await session.execute(stmt)
    tpl = result.scalar_one_or_none()
    if tpl is None:
        return None

    await _validate_line_fks(session, company_id, body.lines)

    tpl.name = body.name
    tpl.applies_to = body.applies_to

    # Delete old lines; cascade handles it, but do explicit delete to avoid
    # ORM selectin load.
    from sqlalchemy import delete

    await session.execute(
        delete(DocumentTemplateLine).where(
            DocumentTemplateLine.template_id == tpl.id
        )
    )

    for i, line_in in enumerate(body.lines):
        line_row = DocumentTemplateLine()
        line_row.template_id = tpl.id
        line_row.sort_order = i
        line_row.name = line_in.name
        line_row.description = line_in.description
        line_row.quantity = line_in.quantity
        line_row.unit_id = line_in.unit_id
        line_row.unit_name = line_in.unit_name
        line_row.unit_price = line_in.unit_price
        line_row.discount_type = line_in.discount_type
        line_row.discount_value = line_in.discount_value
        line_row.vat_rate_id = line_in.vat_rate_id
        session.add(line_row)

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ValueError(
            f"Document template with name '{body.name}' already exists."
        ) from exc

    await session.refresh(tpl)
    return _template_to_read(tpl)


async def delete_document_template(
    session: AsyncSession,
    template_id: uuid.UUID,
    company_id: uuid.UUID,
) -> bool:
    """Delete a document template (cascade removes lines). Returns True if deleted."""
    stmt = select(DocumentTemplate).where(
        DocumentTemplate.id == template_id,
        DocumentTemplate.company_id == company_id,
    )
    result = await session.execute(stmt)
    tpl = result.scalar_one_or_none()
    if tpl is None:
        return False
    await session.delete(tpl)
    await session.commit()
    return True


# ---------------------------------------------------------------------------
# Content Block CRUD
# ---------------------------------------------------------------------------


async def create_content_block(
    session: AsyncSession,
    body: ContentBlockWrite,
    company_id: uuid.UUID,
) -> ContentBlockRead:
    """Create a new content block. Clears existing default of same kind if needed."""
    if body.is_default:
        await _clear_existing_default(session, company_id, body.kind)

    block = ContentBlock()
    block.company_id = company_id
    block.kind = body.kind
    block.name = body.name
    block.body = body.body
    block.is_default = body.is_default
    session.add(block)

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ValueError(
            "Content block with this kind and name already exists, "
            "or another default block of this kind already exists."
        ) from exc

    await session.refresh(block)
    return _content_block_to_read(block)


async def get_content_block(
    session: AsyncSession,
    block_id: uuid.UUID,
    company_id: uuid.UUID,
) -> ContentBlockRead | None:
    """Return a single content block by ID (scoped to company)."""
    stmt = select(ContentBlock).where(
        ContentBlock.id == block_id,
        ContentBlock.company_id == company_id,
    )
    result = await session.execute(stmt)
    block = result.scalar_one_or_none()
    if block is None:
        return None
    return _content_block_to_read(block)


async def list_content_blocks(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    kind: ContentBlockKind | None = None,
) -> list[ContentBlockRead]:
    """Return all content blocks for the company, optionally filtered by kind."""
    stmt = (
        select(ContentBlock)
        .where(ContentBlock.company_id == company_id)
        .order_by(ContentBlock.kind, ContentBlock.name)
    )
    if kind is not None:
        stmt = stmt.where(ContentBlock.kind == kind)
    result = await session.execute(stmt)
    rows = result.scalars().all()
    return [_content_block_to_read(block) for block in rows]


async def update_content_block(
    session: AsyncSession,
    block_id: uuid.UUID,
    body: ContentBlockWrite,
    company_id: uuid.UUID,
) -> ContentBlockRead | None:
    """Update a content block. Clears existing default of same kind if needed."""
    stmt = select(ContentBlock).where(
        ContentBlock.id == block_id,
        ContentBlock.company_id == company_id,
    )
    result = await session.execute(stmt)
    block = result.scalar_one_or_none()
    if block is None:
        return None

    if body.is_default:
        await _clear_existing_default(
            session, company_id, body.kind, exclude_id=block.id
        )

    block.kind = body.kind
    block.name = body.name
    block.body = body.body
    block.is_default = body.is_default

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ValueError(
            "Content block with this kind and name already exists, "
            "or another default block of this kind already exists."
        ) from exc

    await session.refresh(block)
    return _content_block_to_read(block)


async def delete_content_block(
    session: AsyncSession,
    block_id: uuid.UUID,
    company_id: uuid.UUID,
) -> bool:
    """Delete a content block. Returns True if deleted."""
    stmt = select(ContentBlock).where(
        ContentBlock.id == block_id,
        ContentBlock.company_id == company_id,
    )
    result = await session.execute(stmt)
    block = result.scalar_one_or_none()
    if block is None:
        return False
    await session.delete(block)
    await session.commit()
    return True


# ---------------------------------------------------------------------------
# Note Template CRUD
# ---------------------------------------------------------------------------


async def create_note_template(
    session: AsyncSession,
    body: NoteTemplateWrite,
    company_id: uuid.UUID,
) -> NoteTemplateRead:
    """Create a new note template."""
    nt = NoteTemplate()
    nt.company_id = company_id
    nt.name = body.name
    nt.body = body.body
    session.add(nt)

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ValueError(
            f"Note template with name '{body.name}' already exists."
        ) from exc

    await session.refresh(nt)
    return _note_template_to_read(nt)


async def get_note_template(
    session: AsyncSession,
    template_id: uuid.UUID,
    company_id: uuid.UUID,
) -> NoteTemplateRead | None:
    """Return a single note template by ID (scoped to company)."""
    stmt = select(NoteTemplate).where(
        NoteTemplate.id == template_id,
        NoteTemplate.company_id == company_id,
    )
    result = await session.execute(stmt)
    nt = result.scalar_one_or_none()
    if nt is None:
        return None
    return _note_template_to_read(nt)


async def list_note_templates(
    session: AsyncSession,
    company_id: uuid.UUID,
) -> list[NoteTemplateRead]:
    """Return all note templates for the company."""
    stmt = (
        select(NoteTemplate)
        .where(NoteTemplate.company_id == company_id)
        .order_by(NoteTemplate.name)
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()
    return [_note_template_to_read(nt) for nt in rows]


async def update_note_template(
    session: AsyncSession,
    template_id: uuid.UUID,
    body: NoteTemplateWrite,
    company_id: uuid.UUID,
) -> NoteTemplateRead | None:
    """Update a note template."""
    stmt = select(NoteTemplate).where(
        NoteTemplate.id == template_id,
        NoteTemplate.company_id == company_id,
    )
    result = await session.execute(stmt)
    nt = result.scalar_one_or_none()
    if nt is None:
        return None

    nt.name = body.name
    nt.body = body.body

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ValueError(
            f"Note template with name '{body.name}' already exists."
        ) from exc

    await session.refresh(nt)
    return _note_template_to_read(nt)


async def delete_note_template(
    session: AsyncSession,
    template_id: uuid.UUID,
    company_id: uuid.UUID,
) -> bool:
    """Delete a note template. Returns True if deleted."""
    stmt = select(NoteTemplate).where(
        NoteTemplate.id == template_id,
        NoteTemplate.company_id == company_id,
    )
    result = await session.execute(stmt)
    nt = result.scalar_one_or_none()
    if nt is None:
        return False
    await session.delete(nt)
    await session.commit()
    return True

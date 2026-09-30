"""M15 receipt submission orchestration over the shared M14 inbox service."""

from __future__ import annotations

import base64
import binascii
import hashlib
import math
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Literal, cast

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jai.config import get_settings
from jai.models.agent_token import AgentToken
from jai.schemas.agent_tools import (
    ReceiptAIErrorCode,
    ReceiptFileInput,
    ReceiptStatusOutput,
    ReceiptSubmissionSnapshot,
    SubmitReceiptInput,
    SubmitReceiptOutput,
)
from jai.services.agent_operations import AgentOperationValue, run_agent_mutation
from jai.services.agent_tokens import AgentPrincipal
from jai.services.receipt_inbox import create_item_in_transaction, extract_item, get_item
from jai.services.storage import ReceiptValidationError, Storage


class AgentReceiptError(Exception):
    """An expected receipt tool error containing only its stable public code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def decode_receipt_files(
    files: list[ReceiptFileInput], max_bytes: int
) -> list[tuple[bytes, str, str | None]]:
    """Strictly decode bounded canonical Base64 without including values in errors."""
    total = 0
    decoded_files: list[tuple[bytes, str, str | None]] = []
    try:
        for file in files:
            encoded = file.data_base64
            if len(encoded) > math.ceil(max_bytes * 4 / 3) + 4:
                raise ValueError
            decoded = base64.b64decode(encoded, validate=True)
            if (
                not decoded
                or len(decoded) > max_bytes
                or base64.b64encode(decoded).decode("ascii") != encoded
            ):
                raise ValueError
            total += len(decoded)
            if total > max_bytes * 5:
                raise ValueError
            decoded_files.append((decoded, file.mime_type, file.filename))
    except (ValueError, binascii.Error):
        raise AgentReceiptError("MCP_VALIDATION_FAILED") from None
    return decoded_files


def canonical_receipt_arguments(
    request: SubmitReceiptInput,
    files: list[tuple[bytes, str, str | None]],
) -> dict[str, Any]:
    """Hash originals and retain only safe metadata in canonical arguments."""
    return {
        "language": request.language,
        "source": request.source.model_dump(mode="python"),
        "files": [
            {
                "filename": filename,
                "mime_type": mime_type.split(";")[0].strip().lower(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "byte_size": len(data),
            }
            for data, mime_type, filename in files
        ],
    }


def _receipt_mutation(
    principal: AgentPrincipal,
    parts: list[tuple[bytes, str, str | None]],
    storage: Storage,
    written_keys: list[str],
) -> Callable[[AsyncSession], Awaitable[AgentOperationValue]]:
    async def mutate(session: AsyncSession) -> AgentOperationValue:
        # Tenant access is enforced by the transaction's RLS context.
        creator_id = await session.scalar(
            select(AgentToken.creator_id)
            .where(AgentToken.id == principal.token_id)
            .with_for_update(read=True)
        )
        if creator_id is None:
            raise AgentReceiptError("MCP_UNAUTHENTICATED") from None
        inbox, _ = await create_item_in_transaction(
            session,
            principal.company_id,
            creator_id,
            parts,
            get_settings().max_receipt_bytes,
            storage,
            written_keys,
        )
        review_url = f"{get_settings().base_url.rstrip('/')}/receipt-inbox/{inbox.id}"
        return AgentOperationValue(
            "receipt",
            inbox.id,
            ReceiptSubmissionSnapshot(
                inbox_id=inbox.id,
                ai_state="NOT_RUN",
                ai_error_code=None,
                review_url=review_url,
            ),
        )

    return mutate


def _compensate(storage: Storage, keys: list[str]) -> None:
    for key in keys:
        try:
            storage.delete(key)
        except Exception:
            # Storage implementations may include paths or payload context in
            # their exception. Do not serialize any such detail.
            pass


def _review_url(inbox_id: uuid.UUID) -> str:
    return f"{get_settings().base_url.rstrip('/')}/receipt-inbox/{inbox_id}"


async def submit_receipt(
    session_maker: async_sessionmaker[AsyncSession],
    principal: AgentPrincipal,
    request: SubmitReceiptInput,
    storage: Storage,
) -> SubmitReceiptOutput:
    """Atomically persist inbox plus replay snapshot, then attempt M14 AI."""
    max_bytes = get_settings().max_receipt_bytes
    parts = decode_receipt_files(request.files, max_bytes)
    arguments = canonical_receipt_arguments(request, parts)
    written_keys: list[str] = []
    async with session_maker() as session:
        try:
            response = await run_agent_mutation(
                session,
                principal,
                request_id=str(request.request_id),
                operation="submit_receipt",
                arguments=arguments,
                source=request.source.model_dump(mode="python"),
                mutate=_receipt_mutation(principal, parts, storage, written_keys),
            )
        except ReceiptValidationError:
            _compensate(storage, written_keys)
            raise AgentReceiptError("MCP_VALIDATION_FAILED") from None
        except AgentReceiptError:
            _compensate(storage, written_keys)
            raise
        except HTTPException as exc:
            _compensate(storage, written_keys)
            detail = exc.detail
            code = detail.get("code") if isinstance(detail, dict) else None
            if isinstance(code, str) and code in {
                "MCP_UNAUTHENTICATED",
                "MCP_REQUEST_ID_CONFLICT",
                "MCP_VALIDATION_FAILED",
            }:
                raise AgentReceiptError(code) from None
            raise AgentReceiptError("MCP_VALIDATION_FAILED") from None
        except Exception:
            _compensate(storage, written_keys)
            # Database/provider/storage exceptions can carry connection data,
            # local paths, or request context. Expose only a stable error.
            raise AgentReceiptError("MCP_VALIDATION_FAILED") from None
        except BaseException:
            # Cancellation and process-level exceptions must never be turned
            # into a successful result or a public validation failure.
            _compensate(storage, written_keys)
            raise

        if response.outcome == "succeeded" and response.status == "created":
            if response.target_id is None:
                raise AgentReceiptError("MCP_VALIDATION_FAILED") from None
            try:
                await extract_item(
                    session,
                    response.target_id,
                    principal.company_id,
                    storage,
                    request.language,
                )
            except Exception:
                await session.rollback()
                # The durable submission succeeded. M14 status remains
                # recoverable (typically RUNNING) if the follow-up DB failed.

        if response.target_id is None:
            raise AgentReceiptError("MCP_VALIDATION_FAILED") from None
        snapshot = response.result_snapshot
        return SubmitReceiptOutput(
            operation_id=response.operation_id,
            status=cast(Literal["created", "replayed"], response.status),
            target_id=response.target_id,
            inbox_id=uuid.UUID(snapshot["inbox_id"]),
            ai_state=cast(
                Literal["NOT_RUN", "RUNNING", "READY", "UNAVAILABLE", "FAILED"],
                snapshot["ai_state"],
            ),
            ai_error_code=snapshot.get("ai_error_code"),
            review_url=snapshot["review_url"],
        )


async def get_receipt_status(
    session_maker: async_sessionmaker[AsyncSession],
    principal: AgentPrincipal,
    inbox_id: uuid.UUID,
) -> ReceiptStatusOutput:
    """Return current same-company M14 status without files or suggestions."""
    async with session_maker() as session:
        try:
            item = await get_item(session, inbox_id, principal.company_id)
        except LookupError:
            raise AgentReceiptError("MCP_TARGET_NOT_FOUND") from None
    return ReceiptStatusOutput(
        inbox_id=item.id,
        status=item.status.value,
        ai_state=item.ai_state.value,
        ai_error_code=(
            cast(ReceiptAIErrorCode, item.ai_error_code.value)
            if item.ai_error_code
            else None
        ),
        review_url=_review_url(item.id),
    )

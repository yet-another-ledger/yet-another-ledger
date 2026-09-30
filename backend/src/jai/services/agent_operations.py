"""Atomic request replay handling and privacy-limited Agent operation traces."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy import event, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jai.db import set_rls_company
from jai.models.agent_operation import AgentOperation
from jai.models.agent_token import AgentToken
from jai.schemas.agent_tools import (
    AgentSource,
    InvoiceToolSnapshot,
    QuoteToolSnapshot,
    ReceiptSubmissionSnapshot,
)
from jai.schemas.customer import CustomerRead
from jai.schemas.invoice import InvoiceRead
from jai.schemas.quote import QuoteRead
from jai.services.agent_tokens import AgentPrincipal

_SNAPSHOT_MODELS: dict[str, tuple[type[BaseModel], ...]] = {
    "customer": (CustomerRead,),
    # Quote tool responses add the original BASE_URL-derived browser link.
    # Both explicit quote response shapes retain the business target type.
    "quote": (QuoteRead, QuoteToolSnapshot),
    "invoice": (InvoiceRead, InvoiceToolSnapshot),
    # Explicit allowlist for safe receipt submission result fields. The
    # operation envelope (operation ID/status/target ID) is stored separately.
    "receipt": (ReceiptSubmissionSnapshot,),
}
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_BEARER_VALUE = re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_JAI_TOKEN_VALUE = re.compile(
    r"\bjai1\.[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"\.[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"\.[A-Za-z0-9_-]{43}\b",
    re.IGNORECASE,
)
_DATA_URI_TOKEN = r"(?:[a-z0-9!#$&^_.+-]|%[a-f0-9]{2})+"
_DATA_URI_PARAMETER = (
    r"[a-z0-9!#$&^_.+-]+"
    r"(?:\s*=\s*(?:"
    + _DATA_URI_TOKEN
    + r'|"(?:\\.|[^"\\,])*"))?'
)
_FILE_DATA_URI = re.compile(
    # A data URI is identified by its structured header and comma delimiter,
    # not by a file-oriented MIME allowlist. MIME and parameter names are
    # case-insensitive; tolerate spacing around the header separators because
    # snapshots may contain user-provided text. For an omitted media type, the
    # header must begin with a parameter or the comma itself, so prose such as
    # "data: is recorded" is preserved.
    r"(?<![a-z0-9.+-])data:\s*"
    r"(?:"
    r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+"
    r"(?:\s*;\s*" + _DATA_URI_PARAMETER + r")*"
    r"|"
    r"(?:\s*;\s*" + _DATA_URI_PARAMETER + r")*"
    r")\s*,",
    re.IGNORECASE,
)
_BASE64_CANDIDATE = re.compile(r"[A-Za-z0-9+/]+={0,2}")
_RAW_FILE_SIGNATURE = re.compile(r"%PDF-|\x89PNG\r\n\x1a\n|\xff\xd8\xff|RIFF....WEBP")


@dataclass(frozen=True)
class AgentOperationValue:
    """A typed tool result to retain for exact replay after target deletion."""

    target_type: str
    target_id: uuid.UUID
    result: BaseModel


@dataclass(frozen=True)
class AgentOperationResponse:
    """Mutation result plus whether it was created now or replayed."""

    operation_id: uuid.UUID
    status: str
    outcome: str
    target_type: str | None
    target_id: uuid.UUID | None
    result_snapshot: dict[str, Any]
    error_code: str | None


class AgentOperationFailure(Exception):
    """A safe, expected tool failure that should be retained as a trace."""

    def __init__(self, error_code: str) -> None:
        if not _ERROR_CODE.fullmatch(error_code):
            raise ValueError("Agent operation error codes must be stable uppercase identifiers.")
        super().__init__(error_code)
        self.error_code = error_code


def canonical_argument_digest(arguments: Mapping[str, Any]) -> bytes:
    """Hash canonical JSON without retaining arguments in the trace row.

    Callers should pass validated Pydantic inputs via ``model_dump(mode="python")``.
    Python mode preserves dates, UUIDs, Decimals, and enums so the canonicalizer
    can encode each semantic type deterministically instead of hashing a lossy
    or caller-dependent JSON representation.
    """

    encoded = json.dumps(
        _canonical_value(arguments),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).digest()


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Arguments must not contain non-finite numbers.")
        if value.is_zero():
            canonical_decimal = "0"
        else:
            canonical_decimal = format(value, "f")
            if "." in canonical_decimal:
                canonical_decimal = canonical_decimal.rstrip("0").rstrip(".")
        return ["decimal", canonical_decimal]
    if isinstance(value, uuid.UUID):
        return ["uuid", str(value)]
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("Datetime arguments must include a timezone.")
        return ["datetime", value.astimezone(UTC).isoformat()]
    if isinstance(value, date):
        return ["date", value.isoformat()]
    if isinstance(value, Enum):
        return [
            "enum",
            f"{type(value).__module__}.{type(value).__qualname__}",
            _canonical_value(value.value),
        ]
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Argument object keys must be strings.")
        return ["mapping", [[key, _canonical_value(value[key])] for key in sorted(value)]]
    if isinstance(value, (list, tuple)):
        return ["sequence", [_canonical_value(item) for item in value]]
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", value]
    if isinstance(value, float):
        return ["float", value]
    if isinstance(value, str):
        return ["str", value]
    raise ValueError(f"Unsupported argument value type: {type(value).__name__}")


def _safe_source(
    source: Mapping[str, object] | None,
) -> tuple[dict[str, str | None], dict[str, str] | None]:
    """Validate the source contract and retain only its informational IDs."""

    if source is None:
        return (
            {
                "source_platform": None,
                "source_chat_id": None,
                "source_message_id": None,
            },
            None,
        )
    validated = AgentSource.model_validate(source)
    normalized = validated.model_dump(mode="json", exclude_none=True)
    columns = {
        "source_platform": validated.platform,
        "source_chat_id": validated.chat_id,
        "source_message_id": validated.message_id,
    }
    return columns, normalized


def _typed_snapshot(target_type: str, result: BaseModel) -> dict[str, Any]:
    """Serialize only the explicit typed response payload allowed for this target."""

    expected = _SNAPSHOT_MODELS.get(target_type)
    if expected is None:
        raise ValueError("Agent result must use its target's allowlisted response schema.")
    allowed_models = expected if isinstance(expected, tuple) else (expected,)
    if type(result) not in allowed_models:
        raise ValueError("Agent result must use its target's allowlisted response schema.")
    snapshot = result.model_dump(mode="json")
    _reject_forbidden_snapshot_material(snapshot)
    return snapshot


def _assert_target_id_matches(target_type: str, target_id: uuid.UUID, result: BaseModel) -> None:
    """Ensure each allowlisted response identifies the same target as its trace row."""

    id_field = {
        "customer": "id",
        "quote": "id",
        "invoice": "id",
        "receipt": "inbox_id",
    }.get(target_type)
    result_id = None
    if target_type == "quote" and isinstance(result, QuoteToolSnapshot):
        result_id = result.quote.id
    elif target_type == "invoice" and isinstance(result, InvoiceToolSnapshot):
        result_id = result.invoice.id
    elif id_field is not None and not isinstance(result, QuoteToolSnapshot):
        result_id = getattr(result, id_field, None)
    if result_id != target_id:
        raise ValueError("Agent result ID must match its operation target ID.")


def _has_supported_file_signature(value: str) -> bool:
    """Recognize raw or Base64 signatures for M14's supported receipt files."""

    if _RAW_FILE_SIGNATURE.search(value):
        return True

    # Inspect each original token start so preceding free text is never joined
    # into the encoded candidate. Whitespace-only joins support wrapped Base64.
    matches = list(_BASE64_CANDIDATE.finditer(value))
    for index, match in enumerate(matches):
        encoded_prefix = match.group(0)
        previous_end = match.end()
        following_index = index + 1
        while len(encoded_prefix) < 24 and following_index < len(matches):
            following = matches[following_index]
            if not value[previous_end : following.start()].isspace():
                break
            encoded_prefix += following.group(0)
            previous_end = following.end()
            following_index += 1
        encoded_prefix = encoded_prefix[:24]
        encoded_prefix += "=" * (-len(encoded_prefix) % 4)
        try:
            prefix = base64.b64decode(encoded_prefix, validate=True)
        except ValueError:
            continue
        if (
            prefix.startswith(b"%PDF-")
            or prefix.startswith(b"\x89PNG\r\n\x1a\n")
            or prefix.startswith(b"\xff\xd8\xff")
            or (prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP")
        ):
            return True
    return False


def _reject_forbidden_snapshot_material(value: Any) -> None:
    """Reject credentials and recognizable receipt-file payloads in snapshots."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                _reject_forbidden_snapshot_text(key)
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            sensitive_names = (
                "bearer",
                "token",
                "secret",
                "password",
                "credential",
                "apikey",
                "authorization",
            )
            forbidden = normalized_key in sensitive_names or any(
                normalized_key.endswith(name)
                for name in (
                    "bearer",
                    "token",
                    "secret",
                    "password",
                    "credential",
                    "apikey",
                    "authorization",
                )
            )
            if forbidden:
                raise ValueError("Agent result contains forbidden replay snapshot material.")
            _reject_forbidden_snapshot_material(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_forbidden_snapshot_material(item)
    elif isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError("Agent result contains binary replay snapshot material.")
    elif isinstance(value, str):
        _reject_forbidden_snapshot_text(value)


def _reject_forbidden_snapshot_text(value: str) -> None:
    """Apply the same credential and file-content screen to keys and values."""

    if _BEARER_VALUE.search(value) or _JAI_TOKEN_VALUE.search(value):
        raise ValueError("Agent result contains bearer credential material.")
    if _FILE_DATA_URI.search(value) or _has_supported_file_signature(value):
        raise ValueError("Agent result contains recognizable receipt-file content.")


async def run_agent_mutation(
    session: AsyncSession,
    principal: AgentPrincipal,
    *,
    request_id: str,
    operation: str,
    arguments: Mapping[str, Any],
    source: Mapping[str, object] | None = None,
    mutate: Callable[[AsyncSession], Awaitable[AgentOperationValue]],
) -> AgentOperationResponse:
    """Run or replay one mutation and commit its effect and trace atomically.

    The caller supplies the token principal only after Bearer authentication. The
    token is rechecked under a row lock here, so revocation cannot race replay.
    The business callback runs in a savepoint: safe tool failures roll back its
    writes while the outer transaction commits a minimal error trace. Its session
    cannot commit the outer transaction; a deferred database trigger also rejects
    any provisional row that reaches a transaction boundary incomplete.
    """

    if not request_id.strip() or len(request_id) > 200:
        raise HTTPException(status_code=422, detail={"code": "MCP_VALIDATION_FAILED"})
    if not operation.strip() or len(operation) > 100:
        raise HTTPException(status_code=422, detail={"code": "MCP_VALIDATION_FAILED"})
    source_values, canonical_source = _safe_source(source)
    digest = canonical_argument_digest({"arguments": arguments, "source": canonical_source})

    if session.in_transaction():
        raise RuntimeError("Agent mutation requires a session without an active transaction.")

    async with session.begin():
        await set_rls_company(session, principal.company_id)
        token = await session.scalar(
            select(AgentToken)
            .where(
                AgentToken.id == principal.token_id,
                AgentToken.revoked_at.is_(None),
            )
            .with_for_update(read=True)
        )
        if token is None:
            raise HTTPException(status_code=401, detail={"code": "MCP_UNAUTHENTICATED"})

        inserted_id = await session.scalar(
            insert(AgentOperation)
            .values(
                company_id=principal.company_id,
                token_id=principal.token_id,
                request_id=request_id,
                operation=operation,
                arguments_digest=digest,
                **source_values,
                result_snapshot={},
                outcome="succeeded",
                completed_at=None,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    AgentOperation.company_id,
                    AgentOperation.token_id,
                    AgentOperation.request_id,
                ]
            )
            .returning(AgentOperation.id)
        )
        if inserted_id is None:
            prior = await session.scalar(
                select(AgentOperation)
                .where(
                    AgentOperation.token_id == principal.token_id,
                    AgentOperation.request_id == request_id,
                )
                .with_for_update()
            )
            if prior is None:
                raise RuntimeError(
                    "Conflicting Agent operation was not visible after key arbitration."
                )
            if prior.operation != operation or prior.arguments_digest != digest:
                raise HTTPException(status_code=409, detail={"code": "MCP_REQUEST_ID_CONFLICT"})
            return _response(prior, status="replayed")

        row = await session.get(AgentOperation, inserted_id)
        if row is None:
            raise RuntimeError("Inserted Agent operation could not be reloaded.")
        outer_transaction = session.get_transaction()
        if outer_transaction is None:
            raise RuntimeError("Agent operation transaction ended before mutation callback.")

        def reject_callback_commit(sync_session: Any) -> None:
            if sync_session.info.get("jai_agent_mutation_callback"):
                raise RuntimeError("Mutation callbacks cannot commit the Agent transaction.")

        event.listen(session.sync_session, "before_commit", reject_callback_commit)
        try:
            async with session.begin_nested():
                # The guard covers only callback code. Clearing it before the
                # context exits lets SQLAlchemy release the savepoint normally.
                session.sync_session.info["jai_agent_mutation_callback"] = True
                try:
                    value = await mutate(session)
                finally:
                    session.sync_session.info.pop("jai_agent_mutation_callback", None)
        except AgentOperationFailure as error:
            row.outcome = "failed"
            row.error_code = error.error_code
            row.completed_at = datetime.now(UTC)
        else:
            if (
                session.get_transaction() is not outer_transaction
                or not outer_transaction.is_active
            ):
                raise RuntimeError("Mutation callback ended the Agent transaction.")
            if not value.target_type.strip() or len(value.target_type) > 100:
                raise ValueError("Agent operation target type is invalid.")
            _assert_target_id_matches(value.target_type, value.target_id, value.result)
            row.target_type = value.target_type
            row.target_id = value.target_id
            row.result_snapshot = _typed_snapshot(value.target_type, value.result)
            row.completed_at = datetime.now(UTC)
        finally:
            event.remove(session.sync_session, "before_commit", reject_callback_commit)

        await session.flush()
        return _response(row, status="created")


def _response(row: AgentOperation, *, status: str) -> AgentOperationResponse:
    snapshot = dict(row.result_snapshot)
    if row.outcome == "succeeded":
        models = _SNAPSHOT_MODELS.get(row.target_type or "")
        if models is None:
            raise RuntimeError("Saved Agent operation has an unknown result schema.")
        allowed_models = models if isinstance(models, tuple) else (models,)
        for model in allowed_models:
            try:
                snapshot = model.model_validate(snapshot).model_dump(mode="json")
                break
            except ValidationError:
                continue
        else:
            raise RuntimeError("Saved Agent operation has an invalid result snapshot.")
        _reject_forbidden_snapshot_material(snapshot)
    return AgentOperationResponse(
        operation_id=row.id,
        status=status,
        outcome=row.outcome,
        target_type=row.target_type,
        target_id=row.target_id,
        result_snapshot=snapshot,
        error_code=row.error_code,
    )

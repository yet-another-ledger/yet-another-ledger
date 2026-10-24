"""Token-only multipart transport for the M15 local receipt adapter."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError

from jai.api.mcp import agent_principal
from jai.config import get_settings
from jai.db import get_session_maker
from jai.schemas.agent_token import AgentTokenScope
from jai.schemas.agent_tools import ReceiptSubmissionMetadata, SubmitReceiptOutput
from jai.services.agent_receipts import AgentReceiptError, submit_receipt_parts
from jai.services.agent_tokens import require_agent_scope
from jai.services.storage import get_storage

router = APIRouter(prefix="/api/v1/agent", tags=["agent-receipts"])


@router.post("/receipt-inbox", response_model=SubmitReceiptOutput)
async def upload_receipt(
    http_request: Request,
    metadata: Annotated[
        str,
        Form(description="ReceiptSubmissionMetadata JSON: request_id, source, language."),
    ],
    files: Annotated[list[UploadFile], File(description="One to five original receipt files.")],
    _bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))],
) -> SubmitReceiptOutput:
    """Stream originals into the same authenticated intake and replay service.

    The parent transport boundary authenticates and checks receipt:intake
    before multipart parsing, and bounds the body even without Content-Length.
    """
    principal = agent_principal.get()
    require_agent_scope(AgentTokenScope.RECEIPT_INTAKE, principal)
    assert principal is not None
    try:
        form = await http_request.form()
        if len(form.getlist("metadata")) != 1 or set(form) != {"metadata", "files"}:
            raise AgentReceiptError("MCP_VALIDATION_FAILED")
        request = ReceiptSubmissionMetadata.model_validate_json(metadata)
        if not 1 <= len(files) <= 5:
            raise AgentReceiptError("MCP_VALIDATION_FAILED")
        max_bytes = get_settings().max_receipt_bytes
        parts = [
            (
                await file.read(max_bytes + 1),
                file.content_type or "application/octet-stream",
                file.filename,
            )
            for file in files
        ]
        return await submit_receipt_parts(
            get_session_maker(), principal, request, parts, get_storage()
        )
    except ValidationError:
        raise HTTPException(422, detail={"code": "MCP_VALIDATION_FAILED"}) from None
    except AgentReceiptError as exc:
        status = {
            "MCP_UNAUTHENTICATED": 401,
            "MCP_REQUEST_ID_CONFLICT": 409,
        }.get(exc.code, 422)
        raise HTTPException(status, detail={"code": exc.code}) from None

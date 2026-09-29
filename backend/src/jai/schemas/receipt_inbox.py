"""Contract for the pre-Expense receipt inbox workflow."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from jai.schemas.expense import ExpenseAIPrefill, ExpenseRead


class ReceiptInboxStatus(StrEnum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"


class ReceiptInboxAIState(StrEnum):
    NOT_RUN = "NOT_RUN"
    RUNNING = "RUNNING"
    READY = "READY"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"


class ReceiptInboxErrorCode(StrEnum):
    INBOX_NOT_PENDING = "INBOX_NOT_PENDING"
    FILE_LIMIT_REACHED = "FILE_LIMIT_REACHED"
    LAST_FILE = "LAST_FILE"
    ALREADY_CONFIRMED = "ALREADY_CONFIRMED"
    AI_DISABLED = "AI_DISABLED"
    AI_NOT_CONFIGURED = "AI_NOT_CONFIGURED"
    AI_TIMEOUT = "AI_TIMEOUT"
    AI_PROVIDER_ERROR = "AI_PROVIDER_ERROR"
    AI_INVALID_RESPONSE = "AI_INVALID_RESPONSE"


class ReceiptInboxFileRead(BaseModel):
    id: uuid.UUID
    filename: str | None
    mime_type: str
    byte_size: int
    created_at: datetime


class ReceiptInboxRead(BaseModel):
    id: uuid.UUID
    status: ReceiptInboxStatus
    files: list[ReceiptInboxFileRead]
    ai_state: ReceiptInboxAIState
    ai_suggestion: ExpenseAIPrefill | None
    ai_error_code: ReceiptInboxErrorCode | None
    expense_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None


class ReceiptInboxListResponse(BaseModel):
    items: list[ReceiptInboxRead]
    total: int
    limit: int
    offset: int


class ReceiptInboxConfirmationRead(BaseModel):
    inbox: ReceiptInboxRead
    expense: ExpenseRead


class ReceiptInboxExtractRequest(BaseModel):
    language: Literal["en", "zh"] = "en"


class ReceiptInboxConflict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detail: ReceiptInboxErrorCode
    expense_id: uuid.UUID | None = None

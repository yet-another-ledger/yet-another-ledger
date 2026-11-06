"""Contracts for the authenticated PDF materials library."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PdfAppendixSource(StrEnum):
    LIBRARY = "LIBRARY"
    QUOTE = "QUOTE"


class PdfAppendixLimitsRead(BaseModel):
    max_appendix_file_bytes: int
    max_quote_appendices: int
    max_quote_appendix_input_bytes: int
    max_quote_pdf_bytes: int
    max_quote_pdf_pages: int


class PdfAppendixErrorDetail(BaseModel):
    code: str
    message: str
    file_id: uuid.UUID | None = None


class PdfAppendixErrorResponse(BaseModel):
    detail: PdfAppendixErrorDetail


class LegacyDetailErrorResponse(BaseModel):
    """Existing Quote routes return plain string details for legacy errors."""

    detail: str


class PdfAppendixFileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_quote_id: uuid.UUID | None
    title: str
    original_filename: str
    sha256: str
    size_bytes: int
    page_count: int
    is_archived: bool
    created_at: datetime
    updated_at: datetime
    source: PdfAppendixSource

    @model_validator(mode="before")
    @classmethod
    def infer_source(cls, value: object) -> object:
        if isinstance(value, dict):
            value = {**value, "source": "QUOTE" if value.get("owner_quote_id") else "LIBRARY"}
        elif hasattr(value, "owner_quote_id"):
            return {
                name: getattr(value, name)
                for name in (
                    "id",
                    "owner_quote_id",
                    "title",
                    "original_filename",
                    "sha256",
                    "size_bytes",
                    "page_count",
                    "is_archived",
                    "created_at",
                    "updated_at",
                )
            } | {"source": "QUOTE" if value.owner_quote_id else "LIBRARY"}
        return value


class PdfAppendixFileListResponse(BaseModel):
    items: list[PdfAppendixFileRead]
    total: int


class QuoteAppendixRead(BaseModel):
    id: uuid.UUID
    file_id: uuid.UUID
    owner_quote_id: uuid.UUID | None
    source: PdfAppendixSource
    sort_order: int
    title_snapshot: str
    title: str
    original_filename: str
    sha256: str
    size_bytes: int
    page_count: int
    is_archived: bool


class PdfLibraryFileUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    is_archived: bool | None = None

    @model_validator(mode="after")
    def validate_fields(self) -> PdfLibraryFileUpdate:
        if self.title is None and self.is_archived is None:
            raise ValueError("At least one field is required")
        if self.title is not None and not self.title.strip():
            raise ValueError("Title must not be blank")
        return self

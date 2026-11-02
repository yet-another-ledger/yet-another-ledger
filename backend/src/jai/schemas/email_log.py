"""Pydantic schemas for the email-send API (M9 step 6).

Schemas
-------
DocumentSendRequest  – POST /invoices/{id}/send or /quotes/{id}/send body
EmailLogRead         – single email_log row as returned by the API
EmailLogListResponse – list wrapper for GET /invoices/{id}/emails etc.

Security note: ``EmailLogRead`` deliberately excludes ``company_id`` (never
exposed to front-end) and any credential fields.  ``body_snapshot`` is
included so that the author can inspect what was sent; it contains HTML-escaped
user-supplied content (no raw credentials).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

from jai.models._enums import EmailRelatedType, EmailStatus

# ---------------------------------------------------------------------------
# Request body
# ---------------------------------------------------------------------------


class DocumentSendRequest(BaseModel):
    """Body for document and payment-receipt send endpoints.

    ``to`` is mandatory; all other fields are optional overrides.

    ``cc`` may be a single address string (``"a@b.com"``) or a comma-separated
    list (``"a@b.com, c@d.com"``).  It is normalised to a list at the service
    layer.

    ``locale`` when omitted falls through to the D2 resolution chain:
      request override → customer.locale → company default locale → "en".

    ``subject`` / ``body`` when supplied replace the template values but still
    go through the ``render_email_template`` pipeline (placeholder substitution
    + nh3 + nl2br + HTML shell).
    """

    to: EmailStr = Field(description="Primary recipient email address.")
    cc: str | None = Field(
        default=None,
        description=(
            "Optional CC addresses, comma-separated (e.g. 'a@b.com' or 'a@b.com, c@d.com')."
        ),
    )
    locale: Literal["en", "zh"] | None = Field(
        default=None,
        description=(
            "Language for the email and PDF attachment.  "
            "Overrides the D2 resolution chain when supplied."
        ),
    )
    subject: str | None = Field(
        default=None,
        description="Custom email subject.  Uses the stored template when omitted.",
    )
    body: str | None = Field(
        default=None,
        description=(
            "Custom email body (plain text + placeholders).  "
            "Uses the stored template body when omitted.  "
            "Still goes through placeholder rendering + nh3 sanitisation."
        ),
    )


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


SentCopyStatus = Literal["NOT_ATTEMPTED", "SAVED", "FAILED"]


class SentCopyResult(BaseModel):
    sent_copy_status: SentCopyStatus = "NOT_ATTEMPTED"
    sent_copy_error_code: str | None = None
    sent_copy_saved_at: datetime | None = None


class SmtpTestResult(SentCopyResult):
    status: Literal["sent"] = "sent"


class EmailLogRead(SentCopyResult):
    """Single email_log row as returned by the API.

    Deliberately excludes ``company_id`` and any SMTP credential fields.
    ``body_snapshot`` is included for audit purposes; it is HTML-escaped
    user-supplied content (never raw credentials).
    """

    id: uuid.UUID
    related_type: EmailRelatedType
    related_id: uuid.UUID
    to_email: str
    cc: str | None
    subject: str
    body_snapshot: str
    attachment_filename: str | None
    artifact_id: uuid.UUID | None
    locale: str | None
    status: EmailStatus
    error_message: str | None
    creator_id: uuid.UUID | None
    created_at: datetime
    sent_at: datetime | None

    model_config = {"from_attributes": True}


class EmailLogListResponse(BaseModel):
    """Paginated list of email log rows for a document."""

    items: list[EmailLogRead]
    total: int

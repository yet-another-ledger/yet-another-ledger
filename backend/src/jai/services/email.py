"""Email sending service using aiosmtplib + Jinja2 templates.

SMTP configuration is read dynamically from the settings table on each call
(so that changes in the settings UI take effect immediately).  When no SMTP
settings exist in the DB, individual ``SMTP_*`` environment variables serve
as fallback (``jai.config.Settings``).

Templates
---------
Jinja2 HTML templates live in ``jai/templates/email/``.  Autoescape is
**always enabled** (red-line 7 – XSS prevention for user-supplied content).

Email template rendering (M9 step 5)
-------------------------------------
``render_email_template`` converts a plain-text body (with ``{TOKEN}``
placeholders) to a safe HTML email body:

1. nh3 sanitises the raw body text first (strips any accidental HTML/scripts
   the author might have pasted in).
2. Placeholder tokens are replaced with HTML-escaped values (prevent
   injection via data values).
3. Newlines are converted to ``<br>`` tags.
4. The result is wrapped in a minimal HTML shell.

The subject line receives only placeholder substitution (plain text, no HTML
wrapping or nl2br).

Supported placeholder tokens:
  Common:   COMPANY_NAME, CUSTOMER_NAME, DATE, DOCUMENT_NUMBER, TOTAL, CURRENCY
  Invoice:  INVOICE_NUMBER, DUE_DATE, AMOUNT_DUE, DOCUMENT_KIND
  Quote:    QUOTE_NUMBER, VALID_UNTIL
"""
# ruff: noqa: E501

from __future__ import annotations

import html
import logging
import re
import uuid
from datetime import UTC, datetime
from email import policy
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import format_datetime, make_msgid
from pathlib import Path
from typing import TYPE_CHECKING

import aiosmtplib
import nh3
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.ext.asyncio import AsyncSession

from jai.config import get_settings
from jai.models._enums import EmailRelatedType, EmailStatus, SettingLevel
from jai.schemas.email_log import SentCopyResult
from jai.schemas.setting import SETTING_KEY_SMTP, EmailTemplate, SentCopySettings, SmtpSettings
from jai.services.sent_copy import save_sent_copy

if TYPE_CHECKING:
    from jai.models.email_log import EmailLog

logger = logging.getLogger("jai.email")


class MailSendResult(SentCopyResult):
    """Internal SMTP acceptance time, independent of subsequent IMAP latency."""

    smtp_sent_at: datetime


def failed_email_log_values(log: EmailLog) -> dict[str, object]:
    """Capture a failed audit row before rolling back an artifact savepoint."""
    return {
        "company_id": log.company_id,
        "related_type": log.related_type,
        "related_id": log.related_id,
        "to_email": log.to_email,
        "cc": log.cc,
        "subject": log.subject,
        "body_snapshot": log.body_snapshot,
        "attachment_filename": log.attachment_filename,
        "artifact_id": None,
        "locale": log.locale,
        "status": log.status,
        "error_message": log.error_message,
        "creator_id": log.creator_id,
        "sent_at": None,
        "sent_copy_status": "NOT_ATTEMPTED",
        "sent_copy_error_code": None,
        "sent_copy_saved_at": None,
    }


async def persist_failed_email_log(session: AsyncSession, values: dict[str, object]) -> EmailLog:
    """Persist a failed SMTP audit after its unsent artifact was rolled back."""
    from jai.models.email_log import EmailLog

    log = EmailLog(id=uuid.uuid4(), **values)
    session.add(log)
    await session.flush()
    return log


# ---------------------------------------------------------------------------
# Allowed placeholder token names (explicit allowlist — D4)
# ---------------------------------------------------------------------------

#: All tokens that may appear in email template bodies / subjects.
_ALLOWED_TOKENS: frozenset[str] = frozenset(
    {
        # Common
        "COMPANY_NAME",
        "CUSTOMER_NAME",
        "DATE",
        "DOCUMENT_NUMBER",
        "TOTAL",
        "CURRENCY",
        "DOCUMENT_KIND",
        # Invoice-specific
        "INVOICE_NUMBER",
        "DUE_DATE",
        "AMOUNT_DUE",
        # Quote-specific
        "QUOTE_NUMBER",
        "VALID_UNTIL",
        "CREDIT_NOTE_NUMBER",
        "SOURCE_DOCUMENT_NUMBER",
    }
)

#: Regex matching any ``{TOKEN_NAME}`` placeholder.
_PLACEHOLDER_RE = re.compile(r"\{([A-Z_]+)\}")


# ---------------------------------------------------------------------------
# Payment-receipt defaults
# ---------------------------------------------------------------------------

# Receipt mail is deliberately separate from the configurable invoice/quote
# templates.  A receipt has different legal wording and must never fall back
# to an invoice or quote message merely because the user has not customised a
# template in Settings.
_PAYMENT_RECEIPT_EMAIL_TEMPLATES: dict[str, EmailTemplate] = {
    "en": EmailTemplate(
        subject="Payment receipt for {DOCUMENT_NUMBER} from {COMPANY_NAME}",
        body=(
            "Dear {CUSTOMER_NAME},\n\n"
            "Please find the payment receipt for {DOCUMENT_NUMBER} attached.\n\n"
            "Kind regards,\n{COMPANY_NAME}"
        ),
    ),
    "zh": EmailTemplate(
        subject="{COMPANY_NAME} 的收款收据（{DOCUMENT_NUMBER}）",
        body=(
            "尊敬的 {CUSTOMER_NAME}：\n\n"
            "随信附上 {DOCUMENT_NUMBER} 的收款收据。\n\n"
            "此致\n{COMPANY_NAME}"
        ),
    ),
}

# Formal document wording is deliberately kind-aware.  Defaults are defined
# with the typed COMPANY setting; this legacy mapping only declares the stable
# document-kind -> setting-field identity used at send time.
_FORMAL_TEMPLATE_FIELDS: dict[str, str] = {
    "ADVANCE": "advance",
    "FINAL": "final",
    "CREDIT_NOTE": "credit_note",
}

_FORMAL_KIND_LABELS: dict[str, dict[str, str]] = {
    "STANDARD": {"en": "Standard Invoice", "zh": "普通发票"},
    "ADVANCE": {"en": "Advance Invoice", "zh": "预付款发票"},
    "FINAL": {"en": "Final Invoice", "zh": "最终结算发票"},
    "CREDIT_NOTE": {"en": "Credit Note", "zh": "贷项通知单"},
}


def payment_receipt_email_template(locale: str) -> EmailTemplate:
    """Return the typed, built-in payment-receipt email copy for ``locale``.

    Unknown locales deliberately use English, matching document locale
    resolution's final fallback.  Return a copy so this remains a pure default
    factory even though ``EmailTemplate`` is a mutable Pydantic model.
    """
    template = _PAYMENT_RECEIPT_EMAIL_TEMPLATES.get(locale, _PAYMENT_RECEIPT_EMAIL_TEMPLATES["en"])
    return template.model_copy(deep=True)


# ---------------------------------------------------------------------------
# Jinja2 environment (autoescape ON → red-line 7)
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "email"

_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "htm"]),
)


# ---------------------------------------------------------------------------
# SMTP configuration resolution
# ---------------------------------------------------------------------------


async def _get_smtp_config(session: AsyncSession) -> SmtpSettings | None:
    """Load SMTP settings from the DB, falling back to env vars.

    Returns ``None`` if neither source provides a usable configuration
    (specifically, if ``host`` is empty) or if the configuration is
    structurally invalid (e.g. ``from_email`` is not a valid email).
    Invalid configurations are logged and safely degraded to ``None``.
    """
    from jai.services.settings import get_setting

    try:
        cfg = await get_setting(
            session,
            SETTING_KEY_SMTP,
            level=SettingLevel.GLOBAL,
            value_type=SmtpSettings,
        )
    except Exception:
        logger.warning("SMTP_CONFIGURATION_READ_FAILED")
        cfg = None

    if cfg is not None and cfg.host:
        return cfg

    # Env fallback.
    settings = get_settings()
    if settings.smtp_host:
        try:
            return SmtpSettings(
                host=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_username,
                password=settings.smtp_password,
                from_email=settings.smtp_from_email,
                from_name=settings.smtp_from_name,
                use_tls=settings.smtp_use_tls,
                use_ssl=settings.smtp_use_ssl,
                sent_copy=cfg.sent_copy if cfg else SentCopySettings(),
            )
        except Exception:
            logger.warning(
                "SMTP env variables are incomplete or invalid – skipping env fallback",
            )

    return None


def _configured(cfg: SmtpSettings | None) -> bool:
    """Return ``True`` if the configuration has enough data to send."""
    return cfg is not None and bool(cfg.host) and bool(cfg.from_email)


# ---------------------------------------------------------------------------
# Low-level send
# ---------------------------------------------------------------------------


async def _send_mail(
    cfg: SmtpSettings,
    to: str,
    subject: str,
    html_body: str,
    cc: list[str] | None = None,
    attachment_bytes: bytes | None = None,
    attachment_filename: str | None = None,
) -> MailSendResult:
    """Send an HTML email, optionally with CC recipients and a PDF attachment.

    Parameters
    ----------
    cfg:
        Validated SMTP configuration.
    to:
        Primary recipient address.
    subject:
        Email subject (plain text).
    html_body:
        Rendered HTML body.
    cc:
        List of CC recipient addresses.  Each address is also added to the
        SMTP envelope so it is actually delivered (not just header-visible).
    attachment_bytes:
        Raw bytes of a PDF attachment.  ``None`` means no attachment.
    attachment_filename:
        Filename shown to the recipient (e.g. ``"INV-001.pdf"``).  Ignored
        when ``attachment_bytes`` is ``None``.

    Notes
    -----
    Message structure when attachment is present::

        multipart/mixed
          └─ multipart/alternative
               └─ text/html  (body)
          └─ application/pdf  (attachment)

    When no attachment is present the outer mixed wrapper is omitted and we
    fall back to a plain ``multipart/alternative`` (keeps it simple for
    non-document emails such as password-reset and test).
    """
    if attachment_bytes is not None:
        # Full structure: mixed(alternative(html) + pdf)
        outer = MIMEMultipart("mixed")
        alt_part = MIMEMultipart("alternative")
        alt_part.attach(MIMEText(html_body, "html", "utf-8"))
        outer.attach(alt_part)

        pdf_part = MIMEApplication(attachment_bytes, _subtype="pdf")
        fname = attachment_filename or "attachment.pdf"
        pdf_part.add_header("Content-Disposition", "attachment", filename=fname)
        outer.attach(pdf_part)
        msg: MIMEMultipart = outer
    else:
        # Simple structure: alternative(html) – no attachment wrapper needed.
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(html_body, "html", "utf-8"))

    msg["Subject"] = subject
    msg["From"] = f"{cfg.from_name} <{cfg.from_email}>" if cfg.from_name else cfg.from_email
    msg["To"] = to
    if cc:
        msg["Cc"] = ", ".join(cc)

    date = datetime.now(UTC)
    msg["Date"] = format_datetime(date)
    msg["Message-ID"] = make_msgid()
    # Envelope recipients = primary + CC (both must be in the SMTP RCPT TO).
    recipients = [to] + (cc or [])
    utf8_required = not (cfg.from_email + "".join(recipients)).isascii()
    # Raw-byte sendmail bypasses send_message's SMTPUTF8 detection. Keep UTF-8
    # addresses in headers and request UTF-8 envelope encoding explicitly; the
    # library rejects servers without SMTPUTF8 before MAIL/DATA. MIME bodies
    # stay 7-bit encoded, so no BODY=8BITMIME extension is needed.
    raw = msg.as_bytes(policy=policy.SMTP.clone(utf8=utf8_required, cte_type="7bit"))
    mail_options = ["SMTPUTF8"] if utf8_required else []

    if cfg.use_ssl:
        # Implicit TLS (port 465).
        await aiosmtplib.send(
            raw,
            hostname=cfg.host,
            port=cfg.port,
            username=cfg.username or None,
            password=cfg.password or None,
            use_tls=True,
            recipients=recipients,
            sender=cfg.from_email,
            mail_options=mail_options,
        )
    else:
        # STARTTLS (port 587) or plain.
        await aiosmtplib.send(
            raw,
            hostname=cfg.host,
            port=cfg.port,
            username=cfg.username or None,
            password=cfg.password or None,
            start_tls=cfg.use_tls,
            recipients=recipients,
            sender=cfg.from_email,
            mail_options=mail_options,
        )

    accepted_at = datetime.now(UTC)
    copy_result = await save_sent_copy(cfg, raw, date)
    return MailSendResult(**copy_result.model_dump(), smtp_sent_at=accepted_at)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def send_password_reset_email(
    session: AsyncSession,
    to: str,
    reset_url: str,
    locale: str = "en",
) -> None:
    """Send a password-reset email with a link containing the token.

    Parameters
    ----------
    session:
        Database session (used to read SMTP settings).
    to:
        Recipient email address.
    reset_url:
        The full URL the user should click (includes the token).
    locale:
        Language code for the email template (``"en"`` or ``"zh"``).
    """
    cfg = await _get_smtp_config(session)
    if not _configured(cfg):
        logger.warning("SMTP not configured – password-reset email not sent to %s", to)
        return
    assert cfg is not None  # guaranteed by _configured() check above

    template_name = f"reset-password-{locale}.html"
    # Fallback to English if the locale template is missing.
    if not (_TEMPLATES_DIR / template_name).is_file():
        template_name = "reset-password-en.html"

    template = _jinja_env.get_template(template_name)
    html = template.render(reset_url=reset_url)

    subject = "重置您的密码" if locale == "zh" else "Reset your password"
    await _send_mail(cfg, to, subject, html)


async def send_test_email(session: AsyncSession, to: str) -> SentCopyResult:
    """Send a test email to verify SMTP connectivity.

    Raises ``RuntimeError`` if SMTP is not configured or sending fails.
    """
    cfg = await _get_smtp_config(session)
    if not _configured(cfg):
        raise RuntimeError("SMTP is not configured.")
    assert cfg is not None  # guaranteed by _configured() check above

    html = "<p>This is a test email from <strong>Yet Another Ledger</strong>.</p>"
    subject = "YAL – Test Email"
    return await _send_mail(cfg, to, subject, html)


async def is_smtp_configured(session: AsyncSession) -> bool:
    """Return ``True`` if SMTP settings are present (DB or env fallback)."""
    cfg = await _get_smtp_config(session)
    return _configured(cfg)


async def get_configured_smtp_config(session: AsyncSession) -> SmtpSettings | None:
    """Return a usable SMTP configuration without sending or writing a log.

    Receipt sending uses this as an early preflight, before it takes the
    document snapshot locks needed for PDF consistency.  Keeping the result
    lets the subsequent send avoid opening a settings transaction across SMTP
    network I/O.
    """
    cfg = await _get_smtp_config(session)
    return cfg if _configured(cfg) else None


# ---------------------------------------------------------------------------
# Email template rendering (M9 step 5)
# ---------------------------------------------------------------------------


def render_email_template(
    template: EmailTemplate,
    context: dict[str, str],
) -> tuple[str, str]:
    """Render an email template, returning ``(subject, html_body)``.

    The rendering pipeline (red-line 7 + D4 + D7):

    **Body**:
      1. ``nh3.clean`` the raw body text so any accidentally pasted HTML or
         script tags are stripped before further processing.
      2. Replace each ``{TOKEN}`` placeholder with its HTML-escaped value
         (prevents injection through data values).  Tokens not present in
         *context* are replaced with an empty string.  Tokens not in the
         explicit ``_ALLOWED_TOKENS`` allowlist are replaced with empty
         string (not raised, not executed).
      3. Convert remaining newlines to ``<br>`` tags.
      4. Wrap the result in a minimal HTML shell.

    **Subject**:
      Placeholder substitution only (plain text).  Values are treated as
      plain text; no HTML escaping or wrapping is applied.

    Parameters
    ----------
    template:
        The ``EmailTemplate`` instance (subject + body, plain text).
    context:
        Mapping of token name → value.  Missing tokens → empty string.
        Values are caller-supplied strings (e.g. invoice number, company
        name).

    Returns
    -------
    (subject, html_body):
        ``subject`` – plain-text subject with placeholders filled in.
        ``html_body`` – safe HTML body ready for sending.
    """

    # --- Subject (plain text, placeholder substitution only) ---
    def _replace_subject(m: re.Match[str]) -> str:
        token = m.group(1)
        # Unknown tokens → empty string (not crash, not execute).
        return context.get(token, "") if token in _ALLOWED_TOKENS else ""

    rendered_subject = _PLACEHOLDER_RE.sub(_replace_subject, template.subject)

    # --- Body ---
    # Step 1: nh3 clean – strip any HTML/scripts in the stored plain text.
    #   Allow *no* tags so the body is truly plain text after sanitisation.
    clean_body = nh3.clean(template.body, tags=set())

    # Step 2: Replace placeholders with HTML-escaped values.
    def _replace_body(m: re.Match[str]) -> str:
        token = m.group(1)
        if token not in _ALLOWED_TOKENS:
            return ""  # Unknown token → suppress (not raise, not execute)
        raw_value = context.get(token, "")
        return html.escape(raw_value, quote=True)

    escaped_body = _PLACEHOLDER_RE.sub(_replace_body, clean_body)

    # Step 3: nl2br – convert newlines to <br> tags.
    # Also handle Windows-style \r\n.
    br_body = escaped_body.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>\n")

    # Step 4: Minimal HTML shell.
    html_body = (
        "<!DOCTYPE html>\n"
        '<html><head><meta charset="utf-8"></head>\n'
        '<body style="font-family: sans-serif; font-size: 14px; color: #333;">\n'
        f"{br_body}\n"
        "</body></html>"
    )

    return rendered_subject, html_body


# ---------------------------------------------------------------------------
# Document email sending (M9 step 6)
# ---------------------------------------------------------------------------


def _build_template_context(
    doc_type: str,
    doc: object,
    company: object,
    customer: object,
    document_kind: str | None = None,
) -> dict[str, str]:
    """Build the placeholder context dict from document / company / customer objects.

    The returned dict contains string values only (all numeric amounts are
    converted to strings using their stored representation).  ``None`` values
    are replaced with empty strings to avoid printing "None" in emails.

    Common tokens:  COMPANY_NAME, CUSTOMER_NAME, DATE, DOCUMENT_NUMBER,
                    TOTAL, CURRENCY.
    Invoice tokens: INVOICE_NUMBER, DUE_DATE, AMOUNT_DUE.
    Quote tokens:   QUOTE_NUMBER, VALID_UNTIL.
    """

    def _s(v: object) -> str:
        return str(v) if v is not None else ""

    ctx: dict[str, str] = {
        "COMPANY_NAME": _s(getattr(company, "name", None)),
        "CUSTOMER_NAME": _s(getattr(customer, "name", None)),
        "CURRENCY": _s(getattr(doc, "currency", None)),
        "TOTAL": _s(getattr(doc, "total_incl_vat", None)),
        "DOCUMENT_KIND": _FORMAL_KIND_LABELS.get(document_kind or "", {}).get("en", ""),
    }

    if doc_type in {"invoice", "refund"}:
        ctx["DOCUMENT_NUMBER"] = _s(getattr(doc, "invoice_number", None))
        ctx["INVOICE_NUMBER"] = _s(getattr(doc, "invoice_number", None))
        ctx["DATE"] = _s(getattr(doc, "invoice_date", None))
        ctx["DUE_DATE"] = _s(getattr(doc, "due_date", None))
        ctx["AMOUNT_DUE"] = _s(getattr(doc, "due_amount", None))
        ctx["QUOTE_NUMBER"] = ""
        ctx["VALID_UNTIL"] = ""
        credit_number = getattr(doc, "credit_note_number", None)
        if document_kind == "CREDIT_NOTE":
            credit_number = getattr(doc, "invoice_number", None)
        ctx["CREDIT_NOTE_NUMBER"] = _s(credit_number)
        ctx["SOURCE_DOCUMENT_NUMBER"] = _s(getattr(doc, "source_document_number", None))
    elif doc_type == "quote":
        ctx["DOCUMENT_NUMBER"] = _s(getattr(doc, "quote_number", None))
        ctx["QUOTE_NUMBER"] = _s(getattr(doc, "quote_number", None))
        ctx["DATE"] = _s(getattr(doc, "quote_date", None))
        ctx["VALID_UNTIL"] = _s(getattr(doc, "valid_until", None))
        ctx["INVOICE_NUMBER"] = ""
        ctx["DUE_DATE"] = ""
        ctx["AMOUNT_DUE"] = ""
        ctx["CREDIT_NOTE_NUMBER"] = ""
        ctx["SOURCE_DOCUMENT_NUMBER"] = ""
    else:
        ctx["DOCUMENT_NUMBER"] = ""
        ctx["INVOICE_NUMBER"] = ""
        ctx["DATE"] = ""
        ctx["DUE_DATE"] = ""
        ctx["AMOUNT_DUE"] = ""
        ctx["QUOTE_NUMBER"] = ""
        ctx["VALID_UNTIL"] = ""
        ctx["CREDIT_NOTE_NUMBER"] = ""
        ctx["SOURCE_DOCUMENT_NUMBER"] = ""

    return ctx


async def send_document_email(
    session: AsyncSession,
    related_type: EmailRelatedType,
    doc: object,
    company: object,
    customer: object,
    to: str,
    cc: list[str] | None,
    locale: str,
    subject: str | None,
    body: str | None,
    pdf_bytes: bytes,
    filename: str,
    creator_id: object | None = None,
    default_template: EmailTemplate | None = None,
    smtp_config: SmtpSettings | None = None,
    artifact_id: uuid.UUID | None = None,
    document_kind: object | None = None,
) -> EmailLog:
    """Send a document email with PDF attachment and write an EmailLog row.

    The email is **sent synchronously** (D6 – no queue, no auto-retry).
    Regardless of whether sending succeeds or fails, a row is written to
    ``email_log`` with the appropriate status.

    Parameters
    ----------
    session:
        Async DB session (will be flushed but NOT committed here; caller commits).
    related_type:
        ``EmailRelatedType.INVOICE`` or ``EmailRelatedType.QUOTE``.
    doc:
        ORM Invoice or Quote instance (already loaded, with snapshot fields).
    company:
        Company ORM instance.
    customer:
        Customer ORM instance.
    to:
        Primary recipient email address.
    cc:
        Optional list of CC addresses.
    locale:
        Resolved document locale (``"en"`` or ``"zh"``).
    subject:
        Explicit subject override.  When ``None``, the email template for
        ``doc_type × locale`` is resolved and rendered.
    body:
        Explicit body override (plain text + placeholders).  When ``None``,
        the email template body is used.  Either way the body goes through the
        ``render_email_template`` pipeline (placeholder substitution + nh3 +
        nl2br + HTML shell) so it is always safe.
    pdf_bytes:
        Pre-rendered PDF bytes to attach.
    filename:
        Attachment filename shown to the recipient.
    creator_id:
        ``User.id`` of the authenticated user who triggered the send, or
        ``None`` if not available.
    default_template:
        Optional typed built-in default for a specialised document derivative
        such as a payment receipt.  When supplied it replaces the Settings
        invoice/quote template only for this send; explicit ``subject`` and
        ``body`` still pass through the same placeholder and sanitisation
        pipeline.

    Returns
    -------
    EmailLog
        The newly created (and session-flushed) log row.

    Raises
    ------
    HTTPException(400)
        If SMTP is not configured.  The caller should not log a FAILED row
        for a pre-condition failure – we raise before touching email_log so
        the frontend gets a clear actionable error.
    """
    from datetime import UTC, datetime

    from fastapi import HTTPException
    from fastapi import status as http_status

    from jai.models.email_log import EmailLog

    # -- Pre-condition: SMTP must be configured --------------------------------
    cfg = smtp_config if smtp_config is not None else await _get_smtp_config(session)
    if not _configured(cfg):
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=("SMTP is not configured.  Please set up SMTP settings before sending emails."),
        )
    assert cfg is not None  # narrowing for type-checker

    # -- Resolve doc_type string -----------------------------------------------
    doc_type = (
        "invoice"
        if related_type == EmailRelatedType.INVOICE
        else "quote"
        if related_type == EmailRelatedType.QUOTE
        else "refund"
    )
    kind_value = getattr(document_kind, "value", document_kind)
    formal_kind = str(kind_value) if kind_value is not None else "STANDARD"

    # -- Resolve template + render subject/body --------------------------------
    # 1. Load company-level email templates (with built-in fallback).
    from jai.schemas.setting import (
        DEFAULT_EMAIL_TEMPLATES,
        SETTING_KEY_EMAIL_TEMPLATES,
        EmailTemplate,
        EmailTemplatesSetting,
    )
    from jai.services.settings import get_setting

    if default_template is not None:
        stored_template = default_template
    else:
        templates_setting = await get_setting(
            session,
            SETTING_KEY_EMAIL_TEMPLATES,
            level=SettingLevel.COMPANY,
            scope_id=getattr(company, "id", None),
            value_type=EmailTemplatesSetting,
        )
        if templates_setting is None:
            templates_setting = DEFAULT_EMAIL_TEMPLATES

        if doc_type == "invoice" and formal_kind in _FORMAL_TEMPLATE_FIELDS:
            locale_map = getattr(templates_setting, _FORMAL_TEMPLATE_FIELDS[formal_kind])
            stored_template = locale_map.en if locale == "en" else locale_map.zh
        elif doc_type == "refund":
            stored_template = (
                templates_setting.refund.en if locale == "en" else templates_setting.refund.zh
            )
        else:
            locale_map = (
                templates_setting.invoice
                if doc_type in {"invoice", "refund"}
                else templates_setting.quote
            )
            stored_template = locale_map.en if locale == "en" else locale_map.zh

    # 2. If caller supplied an explicit subject or body, build a temporary
    #    EmailTemplate from them (so the same render pipeline is always used).
    if subject is not None or body is not None:
        render_tmpl = EmailTemplate(
            subject=subject if subject is not None else stored_template.subject,
            body=body if body is not None else stored_template.body,
        )
    else:
        render_tmpl = stored_template

    # 3. Build placeholder context and render.
    ctx = _build_template_context(doc_type, doc, company, customer, formal_kind)
    if formal_kind in _FORMAL_KIND_LABELS:
        ctx["DOCUMENT_KIND"] = _FORMAL_KIND_LABELS[formal_kind].get(locale, "")
    rendered_subject, html_body = render_email_template(render_tmpl, ctx)

    # -- Attempt to send -------------------------------------------------------
    doc_id: object = getattr(doc, "id", None)
    company_id: object = getattr(company, "id", None)
    error_msg: str | None = None
    send_status = EmailStatus.SENT
    sent_at: datetime | None = None
    copy_result = SentCopyResult()

    try:
        result = await _send_mail(
            cfg=cfg,
            to=to,
            subject=rendered_subject,
            html_body=html_body,
            cc=cc,
            attachment_bytes=pdf_bytes,
            attachment_filename=filename,
        )
        if isinstance(result, SentCopyResult):
            copy_result = SentCopyResult(**result.model_dump())
        sent_at = result.smtp_sent_at if isinstance(result, MailSendResult) else datetime.now(tz=UTC)
    except Exception:
        send_status = EmailStatus.FAILED
        error_msg = "SMTP_SEND_FAILED"
        logger.warning("Document SMTP send failed", extra={"error_code": error_msg})

    # -- Write EmailLog row (always, success or failure) -----------------------
    log = EmailLog(
        id=__import__("uuid").uuid4(),
        company_id=company_id,
        related_type=related_type,
        related_id=doc_id,
        to_email=to,
        cc=", ".join(cc) if cc else None,
        subject=rendered_subject,
        body_snapshot=html_body,
        attachment_filename=filename,
        # Only a successfully accepted SMTP attachment may claim an artifact.
        artifact_id=artifact_id if send_status == EmailStatus.SENT else None,
        locale=locale,
        status=send_status,
        error_message=error_msg,
        creator_id=creator_id,
        sent_at=sent_at,
        **copy_result.model_dump(),
    )
    session.add(log)
    await session.flush()

    return log

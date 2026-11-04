"""PDF rendering service – M9 step 1 / step 3 / step 4.

Public API
----------
- ``build_invoice_html``          – pure function: assemble Jinja2 context + render template.
- ``build_quote_html``            – pure function: assemble Jinja2 context + render quote template.
- ``build_payment_receipt_html``  – pure function: assemble Jinja2 context + render receipt.
- ``html_to_pdf``                 – thin WeasyPrint wrapper (lazily imported; D8).
- ``render_invoice_pdf``          – DB-backed orchestrator: load invoice, build HTML, render PDF.
- ``render_quote_pdf``            – DB-backed orchestrator: load quote, build HTML, render PDF.
- ``render_payment_receipt_pdf``  – DB-backed orchestrator: load payment, build HTML, render PDF.

Architecture decisions (from M9 D1–D8)
---------------------------------------
D1: WeasyPrint + Jinja2.  Font stack: Noto Sans / Noto Sans CJK SC (Docker runtime).
D5: Immediate rendering on every request – no on-disk cache.
D7: Custom url_fetcher blocks all non-data URIs (SSRF prevention).
    Logo is inlined as a data: URI from binary_asset.content (never a URL).
D8: build_*_html is a pure function with no system-library deps (default pytest).
    html_to_pdf imports WeasyPrint lazily so unit tests run without pango/cairo.

Red-line compliance
-------------------
1. Amounts come from DB snapshots – never recalculated.
2. company_id always injected by caller; cross-company → 404.
7. Jinja2 autoescape ON; url_fetcher blocks http/https/file.
"""
# ruff: noqa: E501

from __future__ import annotations

import base64
import hashlib
import logging
import re
import unicodedata
import urllib.parse
import uuid
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("jai.pdf")

# Semantic output-pipeline version.  Bump it whenever renderer, template, CSS
# or font behaviour can change bytes for identical HTML.
FORMAL_OUTPUT_PIPELINE_VERSION = "m12-formal-output-v2"

# ---------------------------------------------------------------------------
# Template environment (autoescape ON – red-line 7)
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "pdf"
_CSS_PATH = _TEMPLATES_DIR / "base.css"

_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "htm"]),
    keep_trailing_newline=True,
)

# ---------------------------------------------------------------------------
# Jinja2 display filters (display only – red-line 1: no recalculation)
# ---------------------------------------------------------------------------


def _filter_money2(value: Any) -> str:
    """Format a monetary value to exactly 2 decimal places (display only).

    Uses ROUND_HALF_UP – same rounding rule as the pricing pipeline.
    Safe for None / empty: returns "0.00".
    Never recalculates; purely formats the snapshot value for display.
    """
    if value is None:
        return "0.00"
    try:
        d = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return str(d)
    except Exception:
        return "0.00"


def _filter_pct(value: Any) -> str:
    """Format a VAT percentage, stripping insignificant trailing zeros.

    Examples: 21.000 → '21', 9.000 → '9', 21.500 → '21.5', None → '0'.
    """
    if value is None:
        return "0"
    try:
        d = Decimal(str(value)).normalize()
        # normalize() can produce scientific notation for very small/large numbers;
        # convert back to a plain string representation.
        return format(d, "f")
    except Exception:
        return str(value)


_jinja_env.filters["money2"] = _filter_money2
_jinja_env.filters["pct"] = _filter_pct


# ---------------------------------------------------------------------------
# Billing name helper (pure function)
# ---------------------------------------------------------------------------


def resolve_billing_name(customer: Any) -> str:
    """Derive the billing name for client-facing document headers.

    Priority: company_name → contact_name → name.

    ``name`` is the internal nickname (never printed directly on documents);
    it is used only as a guaranteed-non-null fallback.

    Empty strings are treated the same as ``None`` because the service layer
    normalises them to ``None`` before persisting, but callers may pass raw
    ORM objects where the DB value is already ``None``.
    """
    return (
        (customer.company_name or "").strip()
        or (customer.contact_name or "").strip()
        or (customer.name or "").strip()
    )


# ---------------------------------------------------------------------------
# i18n label table (backend-only; not reusing vue-i18n)
# ---------------------------------------------------------------------------

PDF_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "invoice": "Invoice",
        "invoice_number": "Invoice #",
        "reference": "Reference",
        "date": "Invoice Date",
        "due_date": "Due Date",
        "currency": "Currency",
        "bill_to": "BILL TO",
        "vat_id": "VAT ID",
        "coc": "KvK",
        "item": "Item",
        "description": "Description",
        "quantity": "Qty",
        "unit": "Unit",
        "unit_price": "Unit Price",
        "amount": "Amount (excl. VAT)",
        "vat": "VAT",
        "subtotal": "Subtotal",
        "discount": "Discount",
        "vat_total": "VAT",
        "total": "Total (incl. VAT)",
        "amount_due": "Amount Due",
        "notes": "Notes",
        "payment_terms": "Payment Terms",
        "bank_details": "Bank Details",
        "terms": "Terms & Conditions",
        "warranty": "Warranty",
        # Unnumbered draft placeholder (shown instead of a number on DRAFT invoices)
        "draft": "Concept",
        # Trade-name disclosure shown in the per-page footer when legal_name is set
        "trade_name_disclosure": "{trade} is a trade name of {legal}",
        # Quote-specific labels (M9 step 3)
        "quote": "Quote",
        "quote_number": "Quote #",
        "quote_date": "Quote Date",
        "valid_until": "Valid Until",
        "status": "Status",
        # Receipt-specific labels (M9 step 4)
        "receipt": "Receipt",
        "payment_date": "Payment Date",
        "payment_method": "Payment Method",
        "amount_paid": "Amount Paid",
        "amount_paid_total": "Amount Paid (Total)",
        "receipt_amount_due": "Amount Due",
        "paid_status": "Paid Status",
        "payments": "Payments received",
        "already_paid": "Already paid",
        "not_vat_invoice": "NOT A VAT INVOICE",
        "standard_invoice": "Standard Invoice",
        "advance_invoice": "Advance Invoice",
        "final_invoice": "Final Invoice",
        "credit_note": "Credit Note",
        "credit_note_number": "Credit Note Number",
        "supply_or_advance_date": "Supply / Advance Date",
        "advance_applications": "Applied Advance Invoices",
        "source_document": "Corrected Source Document",
        "corrected_lines": "Corrected Lines",
        "refunds": "Refunds",
        "refund_confirmation": "Refund Confirmation",
        "credit_note_reference": "Credit Note",
        "refund_date": "Refund Date",
        "refund_method": "Refund Method",
        "refund_amount": "Refund Amount",
        "refund_reference": "Refund Reference",
        "post_refund_entitlement": "Remaining Refund Entitlement",
        "correction_provenance": "Correction Provenance",
        "replacement_credit_reference": "Replacement for Credit Note",
        "compensation_credit_reference": "Compensates Credit Note",
    },
    "zh": {
        "invoice": "发票",
        "invoice_number": "发票号",
        "reference": "参考号",
        "date": "开票日期",
        "due_date": "到期日期",
        "currency": "币种",
        "bill_to": "账单收件人",
        "vat_id": "增值税号",
        "coc": "工商号",
        "item": "项目",
        "description": "描述",
        "quantity": "数量",
        "unit": "单位",
        "unit_price": "单价",
        "amount": "金额（不含税）",
        "vat": "增值税",
        "subtotal": "小计",
        "discount": "折扣",
        "vat_total": "增值税合计",
        "total": "合计（含税）",
        "amount_due": "应付款",
        "notes": "备注",
        "payment_terms": "付款条款",
        "bank_details": "银行信息",
        "terms": "条款与条件",
        "warranty": "质保说明",
        # Unnumbered draft placeholder (shown instead of a number on DRAFT invoices)
        "draft": "草稿",
        # Trade-name disclosure shown in the per-page footer when legal_name is set
        "trade_name_disclosure": "{trade} 是 {legal} 的商号名称",
        # Quote-specific labels (M9 step 3)
        "quote": "报价",
        "quote_number": "报价号",
        "quote_date": "报价日期",
        "valid_until": "有效期至",
        "status": "状态",
        # Receipt-specific labels (M9 step 4)
        "receipt": "收据",
        "payment_date": "收款日期",
        "payment_method": "收款方式",
        "amount_paid": "本次收款",
        "amount_paid_total": "已收款合计",
        "receipt_amount_due": "未收款",
        "paid_status": "收款状态",
        "payments": "已收款明细",
        "already_paid": "已付款",
        "not_vat_invoice": "非 VAT 发票",
        "standard_invoice": "普通发票",
        "advance_invoice": "预付款发票",
        "final_invoice": "最终结算发票",
        "credit_note": "贷项通知单",
        "credit_note_number": "贷项通知单号",
        "supply_or_advance_date": "供货／预付款日期",
        "advance_applications": "已抵扣的预付款发票",
        "source_document": "更正来源单据",
        "corrected_lines": "更正项目",
        "refunds": "退款",
        "refund_confirmation": "退款确认单",
        "credit_note_reference": "贷项通知单",
        "refund_date": "退款日期",
        "refund_method": "退款方式",
        "refund_amount": "退款金额",
        "refund_reference": "退款参考号",
        "post_refund_entitlement": "退款后剩余额度",
        "correction_provenance": "更正来源",
        "replacement_credit_reference": "替换以下贷项通知单",
        "compensation_credit_reference": "补偿以下贷项通知单",
    },
}

# Fallback locale for unknown locale values
_DEFAULT_LOCALE = "en"


def _get_labels(locale: str) -> dict[str, str]:
    """Return the label dict for *locale*, falling back to English."""
    return PDF_LABELS.get(locale, PDF_LABELS[_DEFAULT_LOCALE])


# ---------------------------------------------------------------------------
# Trade-name disclosure helper (pure function, red-line 7 compliant)
# ---------------------------------------------------------------------------


def _legal_disclosure(company: Any, labels: dict[str, str]) -> str | None:
    """Return the footer trade-name disclosure sentence, or None if unset.

    When the company has a ``legal_name``, the per-page footer leads with this
    sentence (``{trade} is a trade name of {legal}``) instead of the bare trade
    name.  The sentence is assembled in Python with a controlled format string
    and passed to the template as a plain ``{{ variable }}`` substitution (not
    ``| safe``), so Jinja2 autoescape handles any HTML-special characters in
    company.name / legal_name (red-line 7).
    """
    legal_name = getattr(company, "legal_name", None)
    if not legal_name:
        return None
    return labels["trade_name_disclosure"].format(trade=company.name, legal=legal_name)


# ---------------------------------------------------------------------------
# Locale resolution chain (D2 – M9 step 2)
# ---------------------------------------------------------------------------


def resolve_document_locale(
    override: str | None,
    customer_locale: str | None,
    company_default: str | None,
) -> str:
    """Resolve the document language following the D2 priority chain.

    Resolution order (each step falls through to the next when the value is
    ``None`` or not a recognised locale):

    1. ``override``        – explicit locale from the export / send-email request.
    2. ``customer_locale`` – per-customer default (``customer.locale``).
    3. ``company_default`` – company-level default locale setting.
    4. ``"en"``            – hard-coded fallback.

    Parameters
    ----------
    override:
        Locale supplied by the caller (e.g. query-param ``locale=zh``).
        ``None`` means "not supplied".
    customer_locale:
        Value of ``customer.locale`` on the related customer ORM instance.
        ``None`` means the customer has no explicit preference.
    company_default:
        Value of the ``document.default_locale`` company setting (read from
        ``DocumentDefaultsSetting.locale``).  ``None`` if the setting has
        never been written.

    Returns
    -------
    str
        ``"en"`` or ``"zh"``.  Always a recognised value – never ``None``.
    """
    _valid = frozenset(PDF_LABELS)
    for candidate in (override, customer_locale, company_default):
        if candidate and candidate in _valid:
            return candidate
    return _DEFAULT_LOCALE


def _formal_render_fingerprint(html: str) -> str:
    """Hash the formal-output pipeline identity and rendered HTML."""
    return hashlib.sha256(
        FORMAL_OUTPUT_PIPELINE_VERSION.encode("utf-8") + b"\0" + html.encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# RFC 6266 / RFC 5987 compliant Content-Disposition builder
# ---------------------------------------------------------------------------

# Characters unsafe in the quoted-string token of Content-Disposition filename.
# We strip C0/C1 control characters and DEL, and replace " and \ to prevent
# header injection or quote-boundary breakage.
_UNSAFE_CHARS_RE = re.compile(r'[\x00-\x1f\x7f-\x9f"\\]')


def _ascii_fallback(name: str) -> str:
    """Produce a latin-1-safe ASCII fallback filename.

    1. NFKD-normalise so accented chars decompose.
    2. Strip non-ASCII codepoints (covers CJK, emoji, accented residue …).
    3. Replace remaining unsafe chars (control chars, ``"``, ``\\``) with ``_``.
    4. Collapse to a non-empty string.
    """
    normalised = unicodedata.normalize("NFKD", name)
    ascii_only = normalised.encode("ascii", errors="ignore").decode("ascii")
    safe = _UNSAFE_CHARS_RE.sub("_", ascii_only)
    return safe or "invoice"


def build_content_disposition(filename: str) -> str:
    """Return a RFC 6266 / RFC 5987 ``Content-Disposition`` header value.

    Produces::

        attachment; filename="<ascii-safe>"; filename*=UTF-8''<percent-encoded>

    The ``filename=`` token is an ASCII/latin-1 safe fallback for older clients.
    The ``filename*=`` token carries the full UTF-8 filename percent-encoded per
    RFC 5987, and takes precedence in RFC 6266-compliant user-agents.

    The returned string is always latin-1 encodable (safe for ASGI header bytes).
    """
    ascii_name = _ascii_fallback(filename)
    # percent-encode the full original filename for filename* (RFC 5987 §3.2)
    # urllib.parse.quote encodes everything except unreserved chars by default.
    encoded_name = urllib.parse.quote(filename, safe="")
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'


# ---------------------------------------------------------------------------
# SSRF-safe url_fetcher (D7)
# ---------------------------------------------------------------------------


def _safe_url_fetcher(url: str) -> dict[str, Any]:
    """WeasyPrint url_fetcher that only allows data: URIs.

    Any attempt to fetch http/https/file/ftp or any other scheme raises
    ValueError, preventing SSRF and local file reads.  Logo assets must be
    inlined as data: URIs before calling html_to_pdf.
    """
    if url.startswith("data:"):
        # Let WeasyPrint handle data URIs natively via its default fetcher.
        from weasyprint.urls import default_url_fetcher

        return default_url_fetcher(url)  # type: ignore[no-any-return]
    raise ValueError(
        f"URL scheme not allowed in PDF rendering (SSRF prevention): {url!r}"
    )


# ---------------------------------------------------------------------------
# Core rendering functions
# ---------------------------------------------------------------------------


def build_invoice_html(
    invoice: Any,
    company: Any,
    customer: Any,
    locale: str,
    logo_data_uri: str | None,
    *,
    payments: list[Any] | None = None,
    paid_total: Decimal | None = None,
    document_kind_label: str | None = None,
    source_quote: Any | None = None,
    source_invoice: Any | None = None,
    correction_lines: list[Any] | None = None,
    advance_applications: list[Any] | None = None,
    refunds: list[Any] | None = None,
    followup_relations: list[Any] | None = None,
) -> str:
    """Render invoice HTML from ORM objects.

    This is a **pure function** – no I/O, no DB, no WeasyPrint.
    Amounts are read directly from snapshot fields; nothing is recalculated
    (red-line 1).  Jinja2 autoescape is ON (red-line 7).

    Parameters
    ----------
    invoice:
        ``Invoice`` ORM instance (with ``.lines`` and ``.taxes`` preloaded).
    company:
        ``Company`` ORM instance.
    customer:
        ``Customer`` ORM instance (with ``.addresses`` preloaded).
    locale:
        ``"en"`` or ``"zh"``.  Unknown values fall back to ``"en"``.
    logo_data_uri:
        A ``data:<mime>;base64,...`` string, or ``None`` if no logo is set.
        **Must never be a URL** (D7).

    Returns
    -------
    str
        Rendered HTML string (UTF-8).
    """
    from jai.models._enums import AddressType

    labels = _get_labels(locale)
    kind_value = getattr(getattr(invoice, "document_kind", None), "value", getattr(invoice, "document_kind", None))
    is_credit_note = kind_value == "CREDIT_NOTE"

    # -- Resolve billing address ----------------------------------------------
    billing_address: Any = None
    if hasattr(customer, "addresses"):
        for addr in customer.addresses:
            if addr.type == AddressType.BILLING:
                billing_address = addr
                break

    # -- Read CSS inline so WeasyPrint receives a self-contained HTML ---------
    css_text = _CSS_PATH.read_text(encoding="utf-8")

    # -- Build template context -----------------------------------------------
    if paid_total is None:
        paid_total = Decimal(str(invoice.total_incl_vat)) - Decimal(
            str(invoice.due_amount)
        )
    context: dict[str, Any] = {
        "locale": locale if locale in PDF_LABELS else _DEFAULT_LOCALE,
        "labels": labels,
        "legal_disclosure": _legal_disclosure(company, labels),
        "invoice": invoice,
        "company": company,
        "customer": customer,
        "billing_address": billing_address,
        "billing_name": resolve_billing_name(customer),
        "logo_data_uri": logo_data_uri,
        "payments": payments or [],
        "paid_total": paid_total,
        "document_kind_label": document_kind_label or labels["invoice"],
        "is_credit_note": is_credit_note,
        "source_quote": source_quote,
        "source_invoice": source_invoice,
        "correction_lines": correction_lines or [],
        "advance_applications": advance_applications or [],
        "refunds": refunds or [],
        "followup_relations": followup_relations or [],
        "css": css_text,
    }

    template = _jinja_env.get_template("invoice.html")
    return template.render(**context)


def snapshot_presentation(snapshot: Any, company: Any, customer: Any) -> tuple[Any, Any]:
    """Build display-only seller/buyer views from the frozen issue snapshot."""
    from jai.models._enums import AddressType

    if snapshot is None:
        return company, customer
    seller_address = snapshot.seller_address or {}
    buyer_address = snapshot.buyer_address or {}
    seller = SimpleNamespace(
        id=company.id, name=snapshot.seller_name, legal_name=snapshot.seller_legal_name,
        vat_id=snapshot.seller_vat_id, coc_number=snapshot.seller_coc_number,
        email=snapshot.seller_email, phone=snapshot.seller_phone, website=None,
        logo_id=snapshot.logo_id, **seller_address,
    )
    buyer = SimpleNamespace(
        id=customer.id, name=snapshot.buyer_name, company_name=snapshot.buyer_company_name,
        contact_name=snapshot.buyer_contact_name, vat_id=snapshot.buyer_vat_id,
        email=snapshot.buyer_email, phone=snapshot.buyer_phone, locale=snapshot.locale,
        addresses=[SimpleNamespace(type=AddressType.BILLING, **buyer_address)],
    )
    return seller, buyer


def _invoice_kind_label(labels: dict[str, str], kind: Any) -> str:
    value = getattr(kind, "value", kind)
    return {
        "STANDARD": labels["standard_invoice"],
        "ADVANCE": labels["advance_invoice"],
        "FINAL": labels["final_invoice"],
        "CREDIT_NOTE": labels["credit_note"],
    }.get(str(value), labels["invoice"])


def html_to_pdf(html: str) -> bytes:
    """Convert an HTML string to PDF bytes using WeasyPrint.

    WeasyPrint is **lazily imported** inside this function so that default
    pytest unit tests (which test ``build_invoice_html``) don't require
    pango/cairo to be installed (D8).

    The custom ``_safe_url_fetcher`` is wired in to block all non-data URIs
    (D7 – SSRF prevention).

    Parameters
    ----------
    html:
        Fully rendered HTML string (e.g. from ``build_invoice_html``).

    Returns
    -------
    bytes
        Raw PDF bytes starting with ``%PDF-``.
    """
    # Lazy import – keeps the module importable without system PDF libs (D8).
    from weasyprint import HTML

    doc = HTML(string=html, url_fetcher=_safe_url_fetcher)
    pdf_bytes: bytes = doc.write_pdf()
    return pdf_bytes


async def _render_invoice_pdf(
    session: AsyncSession,
    invoice_id: uuid.UUID,
    company_id: uuid.UUID,
    locale: str | None = None,
) -> tuple[bytes, str, str, str]:
    """Load an invoice from the DB, render it as PDF, and return (bytes, filename).

    Steps
    -----
    1. Load Invoice (with lines + taxes via selectinload) scoped to company_id.
    2. Load Company and Customer.
    3. Resolve document locale via ``resolve_document_locale`` (D2 chain).
    4. Inline logo as data URI from binary_asset.content (never a URL).
    5. Call build_invoice_html → html_to_pdf.
    6. Return (pdf_bytes, "<invoice_number>.pdf"); an unnumbered draft
       (invoice_number is None) falls back to "concept.pdf".

    Parameters
    ----------
    locale:
        Explicit locale override (``"en"`` / ``"zh"``).  When ``None``, the
        locale is resolved from the D2 chain: customer.locale →
        company-default setting → ``"en"``.

    Raises
    ------
    HTTPException(404)
        If the invoice doesn't exist or belongs to a different company (red-line 2).
    """
    from fastapi import HTTPException, status
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from jai.db import set_rls_company
    from jai.models.binary_asset import BinaryAsset
    from jai.models.company import Company
    from jai.models.customer import Customer
    from jai.models.document import (
        FinalAdvanceApplication,
        InvoiceCorrection,
        InvoiceCreditBasisLine,
        InvoiceRelation,
    )
    from jai.models.invoice import Invoice
    from jai.models.payment import Payment

    await set_rls_company(session, company_id)

    # -- Load invoice scoped to company (red-line 2) --------------------------
    stmt = (
        select(Invoice)
        .where(Invoice.id == invoice_id, Invoice.company_id == company_id)
        .options(
            selectinload(Invoice.lines),
            selectinload(Invoice.taxes),
            selectinload(Invoice.party_snapshot),
        )
        # Payment writers take ``FOR UPDATE`` on the invoice before changing
        # both payment rows and the invoice due snapshot.  A shared parent lock
        # makes this renderer observe one settlement point-in-time instead of
        # an old due amount combined with a new payment list.
        .with_for_update(read=True)
        # The locked renderer projection is the settlement boundary.  Refresh
        # a previously loaded identity if a caller used the same Session for a
        # lightweight guard before it reached this boundary.
        .execution_options(populate_existing=True)
    )
    result = await session.execute(stmt)
    invoice = result.scalar_one_or_none()
    if invoice is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invoice not found.",
        )

    # -- Load company ---------------------------------------------------------
    company_stmt = select(Company).where(Company.id == company_id)
    company_result = await session.execute(company_stmt)
    company = company_result.scalar_one_or_none()
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found.",
        )

    # -- Load customer (with addresses) ---------------------------------------
    from sqlalchemy.orm import selectinload as _sil


    customer_stmt = (
        select(Customer)
        .where(Customer.id == invoice.customer_id)
        .options(_sil(Customer.addresses))
    )
    customer_result = await session.execute(customer_stmt)
    customer = customer_result.scalar_one_or_none()
    if customer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Customer not found.",
        )

    payment_result = await session.execute(
        select(Payment)
        .where(
            Payment.invoice_id == invoice.id,
            Payment.company_id == company_id,
            Payment.deleted_at.is_(None),
        )
        .order_by(Payment.payment_date, Payment.created_at, Payment.id)
    )
    payments = list(payment_result.scalars().all())
    # Settlement snapshots are authoritative service data. Rendering only
    # presents them; it must not derive cash totals from amounts/due values.
    paid_total = Decimal(str(invoice.incoming_payment_total))

    # Issued legal documents use the issue-time locale snapshot unless the
    # caller explicitly asks for another supported locale.  Drafts keep M9's
    # live D2 chain.
    from jai.models._enums import SettingLevel
    from jai.schemas.setting import SETTING_KEY_DOCUMENT_DEFAULTS, DocumentDefaultsSetting
    from jai.services.settings import get_setting

    company_default_setting = await get_setting(
        session,
        SETTING_KEY_DOCUMENT_DEFAULTS,
        level=SettingLevel.COMPANY,
        scope_id=company_id,
        value_type=DocumentDefaultsSetting,
    )
    company_default_locale: str | None = (
        company_default_setting.locale if company_default_setting is not None else None
    )
    customer_locale: str | None = (
        invoice.party_snapshot.locale
        if invoice.party_snapshot is not None
        else getattr(customer, "locale", None)
    )
    resolved_locale = resolve_document_locale(locale, customer_locale, company_default_locale)

    # -- Inline logo as data: URI (D7 – never a URL) -------------------------
    logo_data_uri: str | None = None
    # A snapshot NULL means no logo at issue; never fall back to a later logo.
    logo_id = (
        invoice.party_snapshot.logo_id
        if invoice.party_snapshot is not None
        else company.logo_id
    )
    if logo_id is not None:
        asset_stmt = select(BinaryAsset).where(BinaryAsset.id == logo_id)
        asset_result = await session.execute(asset_stmt)
        asset = asset_result.scalar_one_or_none()
        if asset is not None:
            b64 = base64.b64encode(asset.content).decode("ascii")
            logo_data_uri = f"data:{asset.mime_type};base64,{b64}"

    source_invoice: Any | None = None
    source_quote: Any | None = None
    correction_lines: list[Any] = []
    applications: list[Any] = []
    refunds: list[Any] = []
    followup_relations: list[Any] = []
    if invoice.quote_id is not None:
        from jai.models.quote import Quote

        source_quote = await session.scalar(
            select(Quote).where(Quote.id == invoice.quote_id, Quote.company_id == company_id)
        )
    kind_value = getattr(invoice.document_kind, "value", invoice.document_kind)
    if kind_value == "CREDIT_NOTE":
        correction = await session.scalar(
            select(InvoiceCorrection)
            .where(InvoiceCorrection.credit_note_id == invoice.id)
            .options(selectinload(InvoiceCorrection.lines))
        )
        if correction is not None:
            source_invoice = await session.scalar(
                select(Invoice).where(Invoice.id == correction.source_invoice_id)
            )
            basis_ids = [line.source_basis_line_id for line in correction.lines]
            basis_by_id = {
                row.id: row
                for row in (await session.execute(
                    select(InvoiceCreditBasisLine).where(
                        InvoiceCreditBasisLine.id.in_(basis_ids)
                    )
                )).scalars()
            }
            correction_lines = [
                SimpleNamespace(
                    sort_order=line.sort_order,
                    name=getattr(basis_by_id.get(line.source_basis_line_id), "name", ""),
                    description=getattr(basis_by_id.get(line.source_basis_line_id), "description", None),
                    quantity=line.quantity,
                    gross_amount=line.gross_amount,
                )
                for line in correction.lines
            ]
        refunds = [
            SimpleNamespace(
                payment_date=refund.payment_date,
                reference=refund.reference,
                amount=refund.amount,
                credit_note_number=invoice.invoice_number,
            )
            for refund in (await session.execute(
                select(Payment).where(
                    Payment.credit_note_id == invoice.id, Payment.company_id == company_id,
                    Payment.deleted_at.is_(None),
                ).order_by(Payment.payment_date, Payment.created_at, Payment.id)
            )).scalars().all()
        ]
    elif kind_value == "FINAL":
        applications = list((await session.execute(
            select(FinalAdvanceApplication)
            .where(FinalAdvanceApplication.final_invoice_id == invoice.id)
            .order_by(FinalAdvanceApplication.sort_order)
        )).scalars().all())

    # Positive follow-ups use the immutable InvoiceRelation provenance rather
    # than a Quote compatibility backlink.  A related Credit's issue number
    # and date are frozen at issue and belong in the retained formal output.
    from sqlalchemy.orm import aliased

    from jai.models._enums import InvoiceRelationType, InvoiceStatus

    related_credit = aliased(Invoice)
    relation_rows = await session.execute(
        select(
            InvoiceRelation.relation_type,
            related_credit.invoice_number,
            related_credit.invoice_date,
        )
        .join(related_credit, related_credit.id == InvoiceRelation.related_credit_note_id)
        .where(
            InvoiceRelation.invoice_id == invoice.id,
            InvoiceRelation.company_id == company_id,
            related_credit.company_id == company_id,
        )
        .order_by(InvoiceRelation.created_at, InvoiceRelation.id)
    )
    labels = _get_labels(resolved_locale)
    for relation_type, credit_number, credit_date in relation_rows:
        relation_value = getattr(relation_type, "value", relation_type)
        if relation_value == InvoiceRelationType.REPLACEMENT_OF.value:
            label = labels["replacement_credit_reference"]
        elif relation_value == InvoiceRelationType.COMPENSATES_CREDIT.value:
            label = labels["compensation_credit_reference"]
        else:
            continue
        followup_relations.append(
            SimpleNamespace(label=label, invoice_number=credit_number, invoice_date=credit_date)
        )

    # The source parent is already held with FOR SHARE.  Settlement writers
    # take it before Credits and Payments, so this issued Credit/refund lookup
    # is a source-bound snapshot, not renderer-side settlement arithmetic.
    if kind_value != "CREDIT_NOTE":
        credit_rows = list((await session.execute(
            select(Invoice.id, Invoice.invoice_number)
            .join(InvoiceCorrection, InvoiceCorrection.credit_note_id == Invoice.id)
            .where(
                InvoiceCorrection.source_invoice_id == invoice.id,
                Invoice.company_id == company_id,
                Invoice.document_kind == "CREDIT_NOTE",
                Invoice.status.in_((InvoiceStatus.SENT, InvoiceStatus.COMPLETED)),
            )
            .order_by(Invoice.invoice_date, Invoice.invoice_number, Invoice.id)
        )).all())
        credit_numbers = {credit_id: credit_number for credit_id, credit_number in credit_rows}
        if credit_numbers:
            refunds = [
                SimpleNamespace(
                    payment_date=refund.payment_date,
                    reference=refund.reference,
                    amount=refund.amount,
                    credit_note_number=credit_numbers[refund.credit_note_id],
                )
                for refund in (await session.execute(
                    select(Payment)
                    .where(
                        Payment.company_id == company_id,
                        Payment.credit_note_id.in_(credit_numbers),
                        Payment.deleted_at.is_(None),
                    )
                    .order_by(Payment.payment_date, Payment.created_at, Payment.id)
                )).scalars().all()
            ]

    presentation_company, presentation_customer = snapshot_presentation(
        invoice.party_snapshot, company, customer
    )
    # -- Build HTML + render PDF ----------------------------------------------
    html = build_invoice_html(
        invoice=invoice,
        company=presentation_company,
        customer=presentation_customer,
        locale=resolved_locale,
        logo_data_uri=logo_data_uri,
        payments=payments,
        paid_total=paid_total,
        document_kind_label=_invoice_kind_label(_get_labels(resolved_locale), invoice.document_kind),
        source_quote=source_quote,
        source_invoice=source_invoice,
        correction_lines=correction_lines,
        advance_applications=applications,
        refunds=refunds,
        followup_relations=followup_relations,
    )
    pdf_bytes = html_to_pdf(html)

    # Unnumbered drafts (invoice_number is None) get an ASCII-safe fallback name.
    filename = f"{invoice.invoice_number}.pdf" if invoice.invoice_number else "concept.pdf"
    return pdf_bytes, filename, resolved_locale, _formal_render_fingerprint(html)


async def render_invoice_pdf(
    session: AsyncSession,
    invoice_id: uuid.UUID,
    company_id: uuid.UUID,
    locale: str | None = None,
) -> tuple[bytes, str]:
    """Render an invoice PDF for previews and legacy callers."""
    pdf_bytes, filename, _, _ = await _render_invoice_pdf(
        session, invoice_id, company_id, locale
    )
    return pdf_bytes, filename


async def render_invoice_pdf_artifact(
    session: AsyncSession,
    invoice_id: uuid.UUID,
    company_id: uuid.UUID,
    locale: str | None = None,
) -> tuple[bytes, str, str, str]:
    """Render a formal PDF with its resolved locale and pipeline fingerprint."""
    return await _render_invoice_pdf(session, invoice_id, company_id, locale)


# ---------------------------------------------------------------------------
# Quote PDF rendering (M9 step 3)
# ---------------------------------------------------------------------------


def build_quote_html(
    quote: Any,
    company: Any,
    customer: Any,
    locale: str,
    logo_data_uri: str | None,
) -> str:
    """Render quote HTML from ORM objects.

    This is a **pure function** – no I/O, no DB, no WeasyPrint.
    Amounts are read directly from snapshot fields; nothing is recalculated
    (red-line 1).  Jinja2 autoescape is ON (red-line 7).

    **Client-facing zero-leakage**: this function renders only the quote's
    own snapshot fields (lines, taxes, totals, content blocks).  Cost/margin/
    estimate data is never passed to the template and must never appear in the
    PDF output (M6.5 guard, extended to M9).

    Parameters
    ----------
    quote:
        ``Quote`` ORM instance (with ``.lines`` and ``.taxes`` preloaded).
    company:
        ``Company`` ORM instance.
    customer:
        ``Customer`` ORM instance (with ``.addresses`` preloaded).
    locale:
        ``"en"`` or ``"zh"``.  Unknown values fall back to ``"en"``.
    logo_data_uri:
        A ``data:<mime>;base64,...`` string, or ``None`` if no logo is set.
        **Must never be a URL** (D7).

    Returns
    -------
    str
        Rendered HTML string (UTF-8).
    """
    from jai.models._enums import AddressType

    labels = _get_labels(locale)

    # -- Resolve billing address ----------------------------------------------
    billing_address: Any = None
    if hasattr(customer, "addresses"):
        for addr in customer.addresses:
            if addr.type == AddressType.BILLING:
                billing_address = addr
                break

    # -- Read CSS inline so WeasyPrint receives a self-contained HTML ---------
    css_text = _CSS_PATH.read_text(encoding="utf-8")

    # -- Build template context (quote-only fields; no cost/margin) -----------
    context: dict[str, Any] = {
        "locale": locale if locale in PDF_LABELS else _DEFAULT_LOCALE,
        "labels": labels,
        "legal_disclosure": _legal_disclosure(company, labels),
        "quote": quote,
        "company": company,
        "customer": customer,
        "billing_address": billing_address,
        "billing_name": resolve_billing_name(customer),
        "logo_data_uri": logo_data_uri,
        "css": css_text,
    }

    template = _jinja_env.get_template("quote.html")
    return template.render(**context)


# ---------------------------------------------------------------------------
# Payment receipt rendering (M9 step 4)
# ---------------------------------------------------------------------------


def build_payment_receipt_html(
    payment: Any,
    invoice: Any | None,
    company: Any,
    customer: Any,
    locale: str,
    logo_data_uri: str | None,
    *,
    quote: Any | None = None,
    paid_total: Decimal | None = None,
    remaining_amount: Decimal | None = None,
) -> str:
    """Render payment receipt HTML from ORM objects.

    This is a **pure function** – no I/O, no DB, no WeasyPrint.
    Amounts are read directly from snapshot fields; nothing is recalculated
    (red-line 1).  Jinja2 autoescape is ON (red-line 7).

    Snapshot fields used:
    - ``payment.amount``              – this payment's amount (snapshot).
    - ``payment.payment_date``        – date of this payment.
    - ``payment.payment_method_name`` – name snapshot (FK SET NULL safe).
    - ``payment.currency``            – currency of this payment.
    - ``payment.reference``           – optional reference (escaped).
    - ``payment.note``                – optional note (escaped).
    - ``invoice.invoice_number``      – related invoice identifier.
    - ``invoice.total_incl_vat``      – invoice total (snapshot).
    - ``invoice.due_amount``          – current amount due (snapshot).
    - ``invoice.paid_status``         – current paid status (snapshot).

    The "amount paid total" (= total − due) is derived from invoice snapshots
    only, **not recalculated** from summing payment rows.

    Parameters
    ----------
    payment:
        ``Payment`` ORM instance.
    invoice:
        ``Invoice`` ORM instance for the related invoice.
    company:
        ``Company`` ORM instance.
    customer:
        ``Customer`` ORM instance (with ``.addresses`` preloaded).
    locale:
        ``"en"`` or ``"zh"``.  Unknown values fall back to ``"en"``.
    logo_data_uri:
        A ``data:<mime>;base64,...`` string, or ``None`` if no logo is set.
        **Must never be a URL** (D7).

    Returns
    -------
    str
        Rendered HTML string (UTF-8).
    """
    from decimal import Decimal

    from jai.models._enums import AddressType

    labels = _get_labels(locale)

    # -- Resolve billing address ----------------------------------------------
    billing_address: Any = None
    if hasattr(customer, "addresses"):
        for addr in customer.addresses:
            if addr.type == AddressType.BILLING:
                billing_address = addr
                break

    # -- Read CSS inline so WeasyPrint receives a self-contained HTML ---------
    css_text = _CSS_PATH.read_text(encoding="utf-8")

    # -- Derive "paid total" from invoice snapshots (red-line 1: no re-sum) --
    # total_incl_vat and due_amount are both snapshot fields on invoice.
    # paid_total = total - due is a simple derived read-only value from the
    # same snapshots; we compute it here in Python (not in the template layer)
    # to keep arithmetic out of Jinja2.
    document = quote if quote is not None else invoice
    if document is None:
        raise ValueError("A payment receipt requires an invoice or quote context.")
    is_quote_origin = quote is not None
    if paid_total is None:
        if invoice is None:
            paid_total = Decimal(str(payment.amount))
        else:
            paid_total = Decimal(str(invoice.total_incl_vat)) - Decimal(
                str(invoice.due_amount)
            )
    if remaining_amount is None:
        remaining_amount = Decimal(str(document.total_incl_vat)) - paid_total
    document_number = (
        quote.quote_number
        if quote is not None
        else invoice.invoice_number if invoice is not None else None
    )

    # -- Build template context (amounts from snapshots, not recalculated) ----
    context: dict[str, Any] = {
        "locale": locale if locale in PDF_LABELS else _DEFAULT_LOCALE,
        "labels": labels,
        "legal_disclosure": _legal_disclosure(company, labels),
        "payment": payment,
        "invoice": invoice,
        "quote": quote,
        "document": document,
        "document_number": document_number,
        "document_number_label": (
            labels["quote_number"] if quote is not None else labels["invoice_number"]
        ),
        "is_quote_origin": is_quote_origin,
        "company": company,
        "customer": customer,
        "billing_address": billing_address,
        "billing_name": resolve_billing_name(customer),
        "logo_data_uri": logo_data_uri,
        "paid_total": paid_total,
        "remaining_amount": remaining_amount,
        "css": css_text,
    }

    template = _jinja_env.get_template("receipt.html")
    return template.render(**context)


async def render_payment_receipt_pdf(
    session: AsyncSession,
    payment_id: uuid.UUID,
    company_id: uuid.UUID,
    locale: str | None = None,
) -> tuple[bytes, str]:
    """Load a payment from the DB, render a receipt PDF, and return (bytes, filename).

    Steps
    -----
    1. Load Payment scoped to company_id (cross-company → 404).
    2. Load related Invoice.
    3. Load Company and Customer (with addresses).
    4. Resolve document locale via ``resolve_document_locale`` (D2 chain).
    5. Inline logo as data URI from binary_asset.content (never a URL).
    6. Call build_payment_receipt_html → html_to_pdf.
    7. Return (pdf_bytes, "receipt-<invoice_number>-<payment_date>.pdf").

    Parameters
    ----------
    locale:
        Explicit locale override (``"en"`` / ``"zh"``).  When ``None``, the
        locale is resolved from the D2 chain: customer.locale →
        company-default setting → ``"en"``.

    Raises
    ------
    HTTPException(404)
        If the payment doesn't exist or belongs to a different company (red-line 2).
    """
    from fastapi import HTTPException, status
    from sqlalchemy import select

    from jai.db import set_rls_company
    from jai.models.binary_asset import BinaryAsset
    from jai.models.company import Company
    from jai.models.customer import Customer
    from jai.models.invoice import Invoice
    from jai.models.payment import Payment
    from jai.models.quote import Quote

    await set_rls_company(session, company_id)

    # The seed is deliberately projection-only: it chooses the immutable
    # payment origin, but is never used to render.  After taking the matching
    # parent lock below we reload the payment and all settlement data, so a
    # concurrent deletion/conversion cannot make a receipt from two snapshots.
    payment_seed_stmt = select(Payment.quote_id, Payment.invoice_id).where(
        Payment.id == payment_id, Payment.company_id == company_id
    )
    payment_seed_result = await session.execute(payment_seed_stmt)
    payment_seed = payment_seed_result.one_or_none()
    if payment_seed is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payment not found.",
        )

    invoice: Invoice | None = None
    quote: Quote | None = None
    if payment_seed.quote_id is not None:
        quote_result = await session.execute(
            select(Quote).where(
                Quote.id == payment_seed.quote_id,
                Quote.company_id == company_id,
            ).with_for_update(read=True)
        )
        quote = quote_result.scalar_one_or_none()
        if quote is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Quote not found.",
            )
    elif payment_seed.invoice_id is not None:
        invoice_result = await session.execute(
            select(Invoice).where(
                Invoice.id == payment_seed.invoice_id,
                Invoice.company_id == company_id,
            ).with_for_update(read=True)
        )
        invoice = invoice_result.scalar_one_or_none()
        if invoice is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Invoice not found.",
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payment document not found.",
        )

    # Reload the current receipt payment only after its parent is locked.  The
    # write protocol locks quote -> invoice for quote-origin payments and
    # invoice for ordinary ones, so this preserves that order and also makes a
    # just-deleted payment a clean 404 rather than a stale receipt.
    payment_result = await session.execute(
        select(Payment)
        .where(Payment.id == payment_id, Payment.company_id == company_id, Payment.deleted_at.is_(None))
        .with_for_update(read=True)
    )
    payment = payment_result.scalar_one_or_none()
    if payment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payment not found.",
        )
    if quote is not None and payment.quote_id != quote.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payment document not found.",
        )
    if invoice is not None and payment.invoice_id != invoice.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payment document not found.",
        )

    # -- Load company ---------------------------------------------------------
    company_stmt = select(Company).where(Company.id == company_id)
    company_result = await session.execute(company_stmt)
    company = company_result.scalar_one_or_none()
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found.",
        )

    # -- Load customer (with addresses) ---------------------------------------
    from sqlalchemy.orm import selectinload as _sil

    if quote is not None:
        document_customer_id = quote.customer_id
    else:
        assert invoice is not None
        document_customer_id = invoice.customer_id
    customer_stmt = (
        select(Customer)
        .where(
            Customer.id == document_customer_id,
            Customer.company_id == company_id,
        )
        .options(_sil(Customer.addresses))
    )
    customer_result = await session.execute(customer_stmt)
    customer = customer_result.scalar_one_or_none()
    if customer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Customer not found.",
        )

    # -- Resolve locale via D2 chain ------------------------------------------
    from jai.models._enums import SettingLevel
    from jai.schemas.setting import SETTING_KEY_DOCUMENT_DEFAULTS, DocumentDefaultsSetting
    from jai.services.settings import get_setting

    company_default_setting = await get_setting(
        session,
        SETTING_KEY_DOCUMENT_DEFAULTS,
        level=SettingLevel.COMPANY,
        scope_id=company_id,
        value_type=DocumentDefaultsSetting,
    )
    company_default_locale: str | None = (
        company_default_setting.locale if company_default_setting is not None else None
    )
    customer_locale: str | None = getattr(customer, "locale", None)
    resolved_locale = resolve_document_locale(locale, customer_locale, company_default_locale)

    # -- Inline logo as data: URI (D7 – never a URL) -------------------------
    logo_data_uri: str | None = None
    if company.logo_id is not None:
        asset_stmt = select(BinaryAsset).where(BinaryAsset.id == company.logo_id)
        asset_result = await session.execute(asset_stmt)
        asset = asset_result.scalar_one_or_none()
        if asset is not None:
            b64 = base64.b64encode(asset.content).decode("ascii")
            logo_data_uri = f"data:{asset.mime_type};base64,{b64}"

    # -- Build HTML + render PDF ----------------------------------------------
    if quote is not None:
        quote_payments_result = await session.execute(
            select(Payment).where(
                Payment.quote_id == quote.id,
                Payment.company_id == company_id,
                Payment.deleted_at.is_(None),
            )
        )
        quote_payments = list(quote_payments_result.scalars().all())
        paid_total = sum(
            (Decimal(str(item.amount)) for item in quote_payments), Decimal("0")
        )
        remaining_amount = Decimal(str(quote.total_incl_vat)) - paid_total
    else:
        assert invoice is not None
        paid_total = Decimal(str(invoice.total_incl_vat)) - Decimal(
            str(invoice.due_amount)
        )
        remaining_amount = Decimal(str(invoice.due_amount))

    html = build_payment_receipt_html(
        payment=payment,
        invoice=invoice,
        company=company,
        customer=customer,
        locale=resolved_locale,
        logo_data_uri=logo_data_uri,
        quote=quote,
        paid_total=paid_total,
        remaining_amount=remaining_amount,
    )
    pdf_bytes = html_to_pdf(html)

    receipt_document_number: str | None
    if quote is not None:
        receipt_document_number = quote.quote_number
    else:
        assert invoice is not None
        receipt_document_number = invoice.invoice_number
    filename = f"receipt-{receipt_document_number}-{payment.payment_date}.pdf"
    return pdf_bytes, filename


async def render_refund_confirmation_pdf(
    session: AsyncSession, payment_id: uuid.UUID, company_id: uuid.UUID,
    locale: str | None = None,
) -> tuple[bytes, str, str, str]:
    """Render a Refund Confirmation from persisted refund/Credit snapshots.

    It is deliberately a separate document, never an invoice or Credit Note.
    Amounts are authoritative service snapshots; the template only formats
    them for presentation.
    """
    from fastapi import HTTPException, status
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from jai.db import set_rls_company
    from jai.models.binary_asset import BinaryAsset
    from jai.models.company import Company
    from jai.models.customer import Customer
    from jai.models.invoice import Invoice
    from jai.services.payment import refund_confirmation_projection

    await set_rls_company(session, company_id)
    try:
        projection = await refund_confirmation_projection(
            session, payment_id=payment_id, company_id=company_id
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Refund not found."
        ) from exc
    refund = projection.refund
    credit = projection.credit
    source = projection.source
    # Fill the relationship on the canonical Credit identity without taking
    # any additional chain lock or running a second settlement query.
    await session.scalar(select(Invoice).where(
        Invoice.id == credit.id, Invoice.company_id == company_id,
    ).options(selectinload(Invoice.party_snapshot)))
    company = await session.scalar(select(Company).where(Company.id == company_id))
    customer = await session.scalar(select(Customer).where(
        Customer.id == credit.customer_id
    ).options(selectinload(Customer.addresses)))
    if company is None or customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Refund party not found.")
    resolved_locale = locale or (credit.party_snapshot.locale if credit.party_snapshot else customer.locale) or "en"
    if resolved_locale not in PDF_LABELS:
        resolved_locale = "en"
    presentation_company, presentation_customer = snapshot_presentation(
        credit.party_snapshot, company, customer
    )
    logo_data_uri: str | None = None
    logo_id = getattr(presentation_company, "logo_id", None)
    if logo_id is not None:
        asset = await session.scalar(select(BinaryAsset).where(BinaryAsset.id == logo_id))
        if asset is not None:
            logo_data_uri = f"data:{asset.mime_type};base64,{base64.b64encode(asset.content).decode('ascii')}"
    billing_address = next(iter(presentation_customer.addresses), None)
    context = {
        "locale": resolved_locale, "labels": _get_labels(resolved_locale),
        "legal_disclosure": _legal_disclosure(presentation_company, _get_labels(resolved_locale)),
        "company": presentation_company, "customer": presentation_customer,
        "billing_name": resolve_billing_name(presentation_customer),
        "billing_address": billing_address, "logo_data_uri": logo_data_uri,
        "refund": refund, "credit": credit, "source": source,
        "remaining_entitlement": projection.collection.remaining_entitlement,
        "css": _CSS_PATH.read_text(encoding="utf-8"),
    }
    html = _jinja_env.get_template("refund_confirmation.html").render(**context)
    filename = f"refund-confirmation-{credit.invoice_number or credit.id}-{refund.payment_date}.pdf"
    return (
        html_to_pdf(html),
        filename,
        resolved_locale,
        _formal_render_fingerprint(html),
    )


async def render_quote_pdf(
    session: AsyncSession,
    quote_id: uuid.UUID,
    company_id: uuid.UUID,
    locale: str | None = None,
) -> tuple[bytes, str]:
    """Load a quote from the DB, render it as PDF, and return (bytes, filename).

    Steps
    -----
    1. Load Quote (with lines + taxes via selectinload) scoped to company_id.
    2. Load Company and Customer.
    3. Resolve document locale via ``resolve_document_locale`` (D2 chain).
    4. Inline logo as data URI from binary_asset.content (never a URL).
    5. Call build_quote_html → html_to_pdf.
    6. Return (pdf_bytes, "<quote_number>.pdf").

    Parameters
    ----------
    locale:
        Explicit locale override (``"en"`` / ``"zh"``).  When ``None``, the
        locale is resolved from the D2 chain: customer.locale →
        company-default setting → ``"en"``.

    Raises
    ------
    HTTPException(404)
        If the quote doesn't exist or belongs to a different company (red-line 2).
    """
    from fastapi import HTTPException, status
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from jai.db import set_rls_company
    from jai.models.binary_asset import BinaryAsset
    from jai.models.company import Company
    from jai.models.customer import Customer
    from jai.models.quote import Quote, QuoteLine

    await set_rls_company(session, company_id)

    # -- Load quote scoped to company (red-line 2) ----------------------------
    stmt = (
        select(Quote)
        .where(Quote.id == quote_id, Quote.company_id == company_id)
        .options(
            selectinload(Quote.lines).selectinload(QuoteLine.line_taxes),
            selectinload(Quote.taxes),
            selectinload(Quote.appendix_memberships),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    result = await session.execute(stmt)
    quote = result.scalar_one_or_none()
    if quote is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Quote not found.",
        )
    if quote.appendix_memberships:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "QUOTE_APPENDIX_OUTPUT_UNAVAILABLE",
                "message": "Quote appendix output is not available yet.",
            },
        )

    # -- Load company ---------------------------------------------------------
    company_stmt = select(Company).where(Company.id == company_id)
    company_result = await session.execute(company_stmt)
    company = company_result.scalar_one_or_none()
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found.",
        )

    # -- Load customer (with addresses) ---------------------------------------
    from sqlalchemy.orm import selectinload as _sil

    customer_stmt = (
        select(Customer)
        .where(Customer.id == quote.customer_id)
        .options(_sil(Customer.addresses))
    )
    customer_result = await session.execute(customer_stmt)
    customer = customer_result.scalar_one_or_none()
    if customer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Customer not found.",
        )

    # -- Resolve locale via D2 chain ------------------------------------------
    from jai.models._enums import SettingLevel
    from jai.schemas.setting import SETTING_KEY_DOCUMENT_DEFAULTS, DocumentDefaultsSetting
    from jai.services.settings import get_setting

    company_default_setting = await get_setting(
        session,
        SETTING_KEY_DOCUMENT_DEFAULTS,
        level=SettingLevel.COMPANY,
        scope_id=company_id,
        value_type=DocumentDefaultsSetting,
    )
    company_default_locale: str | None = (
        company_default_setting.locale if company_default_setting is not None else None
    )
    customer_locale: str | None = getattr(customer, "locale", None)
    resolved_locale = resolve_document_locale(locale, customer_locale, company_default_locale)

    # -- Inline logo as data: URI (D7 – never a URL) -------------------------
    logo_data_uri: str | None = None
    if company.logo_id is not None:
        asset_stmt = select(BinaryAsset).where(BinaryAsset.id == company.logo_id)
        asset_result = await session.execute(asset_stmt)
        asset = asset_result.scalar_one_or_none()
        if asset is not None:
            b64 = base64.b64encode(asset.content).decode("ascii")
            logo_data_uri = f"data:{asset.mime_type};base64,{b64}"

    # -- Build HTML + render PDF ----------------------------------------------
    html = build_quote_html(
        quote=quote,
        company=company,
        customer=customer,
        locale=resolved_locale,
        logo_data_uri=logo_data_uri,
    )
    pdf_bytes = html_to_pdf(html)

    filename = f"{quote.quote_number}.pdf"
    # Release the coherent Quote snapshot lock before any caller can start SMTP.
    await session.commit()
    return pdf_bytes, filename

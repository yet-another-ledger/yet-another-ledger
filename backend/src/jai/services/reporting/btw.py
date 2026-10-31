"""BTW (omzetbelasting) VAT return report service (M10 step 2).

Public API
----------
``compute_vat_return(session, company, year, quarter, tiers)``
    Compute the BTW quarterly return for the given company, year, and quarter.
    Returns a ``VatReturnReport``.

Design decisions (M10.md D-MAP, D-COUNTRY, D-1D, D-PCT, D-BOX5, D6)
----------------------------------------------------------------------
D1  Pure-read: only SELECT queries, no writes, no new tables.
D2  Basis = factuurstelsel (invoice date / expense date), quarterly.
D3  Revenue: status ∈ {SENT, COMPLETED}; Costs: is_draft = false.
D4  All amounts from ``base_*`` columns (EUR, exchange_rate = 1 in v1).
D6  Sum already-persisted to-the-cent amounts; never re-round aggregates.
    Exception: 4b self-assessed VAT (EU_B2B_REVERSE_PURCH) is computed
    fresh from ``base_net_amount × vat_rate_percent / 100`` and rounded
    to cents before accumulation (D-MAP-IMPL).

NL ruleset
----------
The entire D-MAP table is implemented as pure Python functions that
accept row-level snapshots and return box contributions.  The ruleset is
selected by ``company.country_code``; only ``"NL"`` is implemented in v1.
Non-NL companies fall back to the NL ruleset and receive a warning
(D-COUNTRY).

Rate tier matching
------------------
Boxes 1a / 1b / 1e are selected by comparing ``vat_rate_percent`` against
the configured tier thresholds (``VatRateTiers``):
  - rate == hoog → 1a
  - rate == laag → 1b
  - rate == zero → 1e
  - non-zero, not hoog/laag → 1c
For APPLY_RATE effects only; zero-rate effects (ZERO_REVERSE, ZERO_EXPORT)
bypass tier matching and route directly to 3a / 3b / 1e.

Private-use correction (1d, D-1D)
----------------------------------
1d is populated **only** when ``is_last_period_of_year`` is True (Q4).
The value is the sum over **the entire calendar year** (Jan–Dec) of
    Σ (deductible expenses) [ base_vat_amount × (1 − business_percentage/100) ]
This represents the private-use portion of VAT that was fully deducted
throughout the year, now corrected at year-end.

Disclaimer (D-DISCLAIMER)
--------------------------
A fixed English disclaimer string is returned in every response.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from jai.db import set_rls_company
from jai.models._enums import (
    InvoiceDocumentKind,
    InvoiceSettlementStatus,
    InvoiceStatus,
    VatTreatmentEffect,
)
from jai.models.document import (
    FinalAdvanceApplication,
    FinalAdvanceApplicationTax,
    InvoiceCorrection,
    InvoiceCorrectionLine,
    InvoiceCreditBasisLine,
)
from jai.models.expense import Expense
from jai.models.external_sales import ExternalSale
from jai.models.invoice import Invoice
from jai.models.payment import Payment, PaymentTax
from jai.schemas.report import (
    ReportDocumentReference,
    ReportSourceType,
    ReportTaxEventKind,
    ReportTaxEventRow,
    ReportWarning,
    ReportWarningCode,
    VatBoxBaseOnly,
    VatBoxBaseVat,
    VatBoxVatOnly,
    VatReturnBoxes,
    VatReturnReport,
    VatReturnTotals,
)
from jai.schemas.setting import VatRateTiers
from jai.services.money import quantize_to_minor_unit
from jai.services.reporting.events import quarter_label

if TYPE_CHECKING:
    from jai.models.company import Company

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ZERO = Decimal("0")


def _receipt_only_notice_severity(
    document_kind: InvoiceDocumentKind | None,
    status: InvoiceStatus | None,
    settlement_status: InvoiceSettlementStatus | None,
) -> str | None:
    """Classify a quote-origin payment notice without changing its tax event.

    Receipt-only quote deposits are the M11.5 path.  A formal Advance can
    have an associated Quote too, but must never be described as a deposit.
    """
    if document_kind == InvoiceDocumentKind.ADVANCE:
        return None
    if (
        document_kind == InvoiceDocumentKind.STANDARD
        and status in (InvoiceStatus.SENT, InvoiceStatus.COMPLETED)
        and settlement_status == InvoiceSettlementStatus.SETTLED
    ):
        return "info"
    return "warning"


def _event_row_sort_key(row: ReportTaxEventRow) -> tuple[object, ...]:
    """Return the complete canonical key for one public BTW audit event."""
    return (
        row.event_date,
        row.event_kind.value,
        row.document_id or "",
        row.document_kind.value if row.document_kind is not None else "",
        row.document_number or "",
        row.payment_id or "",
        row.source_document_id or "",
        row.source_document_kind.value if row.source_document_kind is not None else "",
        row.source_document_number or "",
        row.vat_treatment_code,
        row.vat_treatment_effect.value,
        row.vat_rate_percent,
        row.requires_icp,
        row.taxable_amount,
        row.vat_amount,
    )

_DISCLAIMER = (
    "This output is for bookkeeping assistance only and does not constitute "
    "tax or accounting advice. Always verify with your accountant and/or the "
    "Belastingdienst before filing your VAT return. "
    "This open-source project accepts no liability for the accuracy of the figures."
)

# VAT treatment effect codes (snapshot values stored on Invoice / Expense).
_EFFECT_APPLY_RATE = "APPLY_RATE"
_EFFECT_ZERO_REVERSE = "ZERO_REVERSE"
_EFFECT_ZERO_EXPORT = "ZERO_EXPORT"


# ---------------------------------------------------------------------------
# Quarter date helpers
# ---------------------------------------------------------------------------


def _quarter_date_range(year: int, quarter: int) -> tuple[date, date]:
    """Return the inclusive [from, to] date range for the given quarter."""
    start_month = (quarter - 1) * 3 + 1
    end_month = start_month + 2
    # End day: last day of end_month.
    import calendar
    end_day = calendar.monthrange(year, end_month)[1]
    return date(year, start_month, 1), date(year, end_month, end_day)


# ---------------------------------------------------------------------------
# Tier resolution
# ---------------------------------------------------------------------------


def _classify_rate(rate_percent: Decimal, tiers: VatRateTiers) -> str:
    """Return the tier label ('hoog', 'laag', 'zero', 'other') for a rate value."""
    if rate_percent == tiers.hoog:
        return "hoog"
    if rate_percent == tiers.laag:
        return "laag"
    if rate_percent == tiers.zero:
        return "zero"
    return "other"


# ---------------------------------------------------------------------------
# Accumulator dataclass (mutable, internal)
# ---------------------------------------------------------------------------


@dataclass
class _BoxAccumulator:
    """Mutable accumulator for BTW box values.  Sums are never re-rounded (D6)."""

    # 1a – hoog tarief
    b1a_base: Decimal = field(default_factory=lambda: _ZERO)
    b1a_vat: Decimal = field(default_factory=lambda: _ZERO)
    # 1b – laag tarief
    b1b_base: Decimal = field(default_factory=lambda: _ZERO)
    b1b_vat: Decimal = field(default_factory=lambda: _ZERO)
    # 1c – other rates (non-zero, non-hoog, non-laag)
    b1c_base: Decimal = field(default_factory=lambda: _ZERO)
    b1c_vat: Decimal = field(default_factory=lambda: _ZERO)
    # 1d – privégebruik (only populated in Q4)
    b1d_vat: Decimal = field(default_factory=lambda: _ZERO)
    # 1e – 0% / not-taxed-at-you (base only)
    b1e_base: Decimal = field(default_factory=lambda: _ZERO)
    # 2a – domestic reverse-charge (recipient side); v1 always 0
    b2a_base: Decimal = field(default_factory=lambda: _ZERO)
    b2a_vat: Decimal = field(default_factory=lambda: _ZERO)
    # 3a – export non-EU (base only)
    b3a_base: Decimal = field(default_factory=lambda: _ZERO)
    # 3b – ICP (EU B2B reverse-charge / intracommunautaire levering, base only)
    b3b_base: Decimal = field(default_factory=lambda: _ZERO)
    # 3c – distance / installation sales EU; v1 always 0
    b3c_base: Decimal = field(default_factory=lambda: _ZERO)
    # 4a – non-EU import (art.23); v1 always 0
    b4a_base: Decimal = field(default_factory=lambda: _ZERO)
    b4a_vat: Decimal = field(default_factory=lambda: _ZERO)
    # 4b – EU intra-community acquisition self-assessment
    b4b_base: Decimal = field(default_factory=lambda: _ZERO)
    b4b_vat: Decimal = field(default_factory=lambda: _ZERO)
    # 5b – input VAT (voorbelasting)
    b5b_vat: Decimal = field(default_factory=lambda: _ZERO)


# ---------------------------------------------------------------------------
# NL ruleset: invoice line-level contribution
# ---------------------------------------------------------------------------


def _apply_invoice_line_nl(
    acc: _BoxAccumulator,
    treatment_effect: str,
    requires_icp: bool,
    treatment_code: str,
    vat_rate_percent: Decimal,
    taxable_base: Decimal,
    vat_amount: Decimal,
    tiers: VatRateTiers,
) -> None:
    """Apply a single invoice VAT line to the accumulator (NL ruleset).

    Parameters
    ----------
    acc:
        Mutable accumulator to update.
    treatment_effect:
        The ``vat_treatment_effect`` snapshot: APPLY_RATE | ZERO_REVERSE | ZERO_EXPORT.
    requires_icp:
        Whether this treatment triggers an ICP (Intrastat) obligation.
    treatment_code:
        The ``vat_treatment_code`` snapshot (used for EU_B2C routing).
    vat_rate_percent:
        The ``vat_rate_percent`` snapshot from InvoiceLine or InvoiceTax.
    taxable_base:
        The ``taxable_amount`` snapshot (already to the cent, D6).
    vat_amount:
        The ``vat_total`` / ``tax_amount`` snapshot (already to the cent, D6).
    tiers:
        VatRateTiers configuration (hoog/laag/zero thresholds).
    """
    if treatment_effect == _EFFECT_ZERO_EXPORT:
        # D-MAP row 5: EXPORT_NON_EU → 3a (base only)
        acc.b3a_base += taxable_base
        return

    if treatment_effect == _EFFECT_ZERO_REVERSE and requires_icp:
        # D-MAP row 6: EU_B2B_REVERSE → 3b (base only, ICP)
        acc.b3b_base += taxable_base
        return

    if treatment_effect == _EFFECT_ZERO_REVERSE and not requires_icp:
        # Catch-all: any other zero-reverse (e.g. domestic 0%-rated services
        # flagged ZERO_REVERSE without ICP).  Route to 1e (base only) as a
        # safe fallback.
        acc.b1e_base += taxable_base
        return

    if treatment_effect == _EFFECT_APPLY_RATE:
        # D-MAP rows 1–4: NL_DOMESTIC and EU_B2C both go through tier matching.
        tier = _classify_rate(vat_rate_percent, tiers)
        if tier == "hoog":
            acc.b1a_base += taxable_base
            acc.b1a_vat += vat_amount
        elif tier == "laag":
            acc.b1b_base += taxable_base
            acc.b1b_vat += vat_amount
        elif tier == "zero":
            # APPLY_RATE at 0% (e.g. NL_DOMESTIC with 0% rate): → 1e
            acc.b1e_base += taxable_base
        else:
            # Non-hoog, non-laag, non-zero → 1c
            acc.b1c_base += taxable_base
            acc.b1c_vat += vat_amount
        return

    # Unknown effect: silently ignore (future-proofing).
    return


# ---------------------------------------------------------------------------
# NL ruleset: expense-level contribution
# ---------------------------------------------------------------------------


def _apply_expense_nl(
    acc: _BoxAccumulator,
    treatment_effect: str,
    treatment_code: str,
    vat_rate_percent: Decimal,
    base_net_amount: Decimal,
    base_vat_amount: Decimal,
    deductible: bool,
) -> None:
    """Apply a single expense to the accumulator (NL ruleset).

    Parameters
    ----------
    acc:
        Mutable accumulator to update.
    treatment_effect:
        The ``vat_treatment_effect`` snapshot.
    treatment_code:
        The ``vat_treatment_code`` snapshot (used to identify EU_B2C_PURCH).
    vat_rate_percent:
        The ``vat_rate_percent`` snapshot.
    base_net_amount:
        The ``base_net_amount`` column (EUR, already to-the-cent).
    base_vat_amount:
        The ``base_vat_amount`` column (EUR, already to-the-cent).
    deductible:
        The ``deductible`` flag on the expense.
    """
    # D-MAP row 10: EU_B2C_PURCH (consumer-side EU foreign VAT) → no NL box.
    # Official: "in andere EU-landen betaalde btw mag u NIET aftrekken in uw
    # Nederlandse btw-aangifte" (guide §5b, Let op!).
    if treatment_code == "EU_B2C_PURCH":
        return

    # D-MAP row 9: IMPORT_NON_EU → v1 not implemented.
    if treatment_code == "IMPORT_NON_EU":
        return

    if treatment_effect == _EFFECT_APPLY_RATE:
        # D-MAP row 7: NL_DOMESTIC_PURCH → 5b (full VAT, if deductible; no business% here)
        if deductible:
            acc.b5b_vat += base_vat_amount
        return

    if treatment_effect == _EFFECT_ZERO_REVERSE:
        # D-MAP row 8: EU_B2B_REVERSE_PURCH → 4b (self-assessed) + 5b (same amount, if deductible)
        # Self-assessed VAT = quantize_to_minor_unit(base_net × rate / 100)  (D-MAP-IMPL)
        self_assessed_vat = quantize_to_minor_unit(
            base_net_amount * vat_rate_percent / Decimal("100")
        )
        acc.b4b_base += base_net_amount
        acc.b4b_vat += self_assessed_vat
        if deductible:
            acc.b5b_vat += self_assessed_vat
        return

    # Fallback: unknown zero-rate expense – ignore.
    return


# ---------------------------------------------------------------------------
# 1d: private-use correction (D-1D)
# ---------------------------------------------------------------------------


def _compute_1d_vat(expenses: list[Expense]) -> Decimal:
    """Compute the 1d private-use correction for the full year.

    Only deductible expenses contribute; business_percentage = 100 contributes 0.

    1d = Σ (deductible expenses) [ base_vat_amount × (1 − business_percentage / 100) ]

    Each per-expense amount is rounded to cents before summing (D6 / D-MAP-IMPL).
    """
    total = _ZERO
    for exp in expenses:
        if not exp.deductible:
            continue
        biz_pct = Decimal(str(exp.business_percentage))
        if biz_pct >= Decimal("100"):
            continue  # 100% business → zero private-use correction
        private_fraction = Decimal("1") - biz_pct / Decimal("100")
        contribution = quantize_to_minor_unit(
            Decimal(str(exp.base_vat_amount)) * private_fraction
        )
        total += contribution
    return total


# ---------------------------------------------------------------------------
# Main async orchestration
# ---------------------------------------------------------------------------


async def compute_vat_return(
    session: AsyncSession,
    company: Company,
    year: int,
    quarter: int,
    tiers: VatRateTiers,
) -> VatReturnReport:
    """Compute the BTW VAT return for *company* for *year* Q*quarter*.

    Parameters
    ----------
    session:
        Read-only async SQLAlchemy session.
    company:
        Company ORM object (needs ``id`` and ``country_code``).
    year:
        Calendar year (e.g. 2026).
    quarter:
        Quarter 1–4.
    tiers:
        VatRateTiers with hoog/laag/zero thresholds.

    Returns
    -------
    VatReturnReport
        Complete VAT return with boxes, totals, warnings, and disclaimer.
    """
    date_from, date_to = _quarter_date_range(year, quarter)
    is_last_period = quarter == 4
    company_id: uuid.UUID = company.id

    # Payment and PaymentTax are RLS-protected.  This is the shared entry
    # point used by both the VAT endpoint and Dashboard, so establish the
    # transaction-local tenant before either report can read cash VAT events.
    await set_rls_company(session, company_id)

    warnings: list[str] = []
    infos: list[str] = []

    # D-COUNTRY: only NL is implemented; all others fall back to NL + warning.
    country_code = getattr(company, "country_code", None) or ""
    if country_code.upper() != "NL":
        warnings.append(
            f"Company country code is '{country_code}' (not 'NL'). "
            "Only the Dutch (NL) BTW ruleset is currently implemented. "
            "The NL ruleset has been applied as a fallback. "
            "Please verify the figures with a local tax advisor."
        )

    acc = _BoxAccumulator()
    # Keep a persisted fact identity alongside each public row until the final
    # sort.  Tax/correction relationships intentionally have no ORM ordering;
    # a row's API-visible fields define its canonical audit order and this
    # immutable source ID resolves otherwise equal public rows without relying
    # on database load order.
    event_rows: list[tuple[ReportTaxEventRow, str]] = []
    correction_warnings: list[ReportWarning] = []

    def apply_tax_event(
        *,
        event_kind: ReportTaxEventKind,
        event_date: date,
        taxable: Decimal,
        vat: Decimal,
        code: str,
        effect: str,
        requires_icp: bool,
        rate: Decimal,
        document: Invoice | None = None,
        payment_id: uuid.UUID | None = None,
        source_document_id: uuid.UUID | None = None,
        source_document_kind: InvoiceDocumentKind | None = None,
        source_document_number: str | None = None,
        source_row_id: uuid.UUID | None = None,
        external_sale_id: uuid.UUID | None = None,
        external_source_name: str | None = None,
        external_original_number: str | None = None,
    ) -> None:
        """Apply and expose one frozen invoice-side tax event exactly once."""
        if taxable == _ZERO and vat == _ZERO:
            return
        document_kind = document.document_kind if document is not None else None
        # The legacy Standard query itself fixes the kind.  A few established
        # pure-report fixtures intentionally model only its financial fields,
        # so keep that compatibility without weakening runtime document kinds.
        if document is not None and not isinstance(document_kind, InvoiceDocumentKind):
            document_kind = InvoiceDocumentKind.STANDARD
        _apply_invoice_line_nl(acc, effect, requires_icp, code, rate, taxable, vat, tiers)
        event_rows.append((ReportTaxEventRow(
            event_kind=event_kind,
            document_id=str(document.id) if document is not None else None,
            document_kind=document_kind,
            document_number=document.invoice_number if document is not None else None,
            event_date=event_date,
            payment_id=str(payment_id) if payment_id is not None else None,
            source_document_id=(
                str(source_document_id) if source_document_id is not None else None
            ),
            source_document_kind=source_document_kind,
            source_document_number=source_document_number,
            source_type=(
                ReportSourceType.EXTERNAL_SALE
                if external_sale_id
                else ReportSourceType.INVOICE
            ),
            external_sale_id=str(external_sale_id) if external_sale_id else None,
            external_source_name=external_source_name,
            external_original_number=external_original_number,
            taxable_amount=taxable,
            vat_amount=vat,
            vat_treatment_code=code,
            vat_treatment_effect=VatTreatmentEffect(effect),
            vat_rate_percent=rate,
            requires_icp=requires_icp,
        ), str(source_row_id) if source_row_id is not None else ""))

    # -----------------------------------------------------------------------
    # 1. Revenue: invoices with status SENT or COMPLETED in [date_from, date_to]
    # -----------------------------------------------------------------------
    revenue_statuses = [InvoiceStatus.SENT, InvoiceStatus.COMPLETED]
    stmt_inv = select(Invoice).where(
        and_(
            Invoice.company_id == company_id,
            # Step 3 keeps the existing invoice-date projection Standard-only.
            # M11.5 quote-origin PaymentTax remains independently included
            # below; Step 8 will add explicit formal tax events.
            Invoice.document_kind == InvoiceDocumentKind.STANDARD,
            Invoice.status.in_(revenue_statuses),
            Invoice.invoice_date >= date_from,
            Invoice.invoice_date <= date_to,
        )
    )
    result_inv = await session.execute(stmt_inv)
    invoices = result_inv.scalars().all()

    # Posted external sales are a distinct source family. Their confirmed
    # original VAT is authoritative and is routed using the frozen data-driven rate.
    external_sales = list(
        (
            await session.scalars(
                select(ExternalSale)
                .options(selectinload(ExternalSale.tax_summaries))
                .where(
                    ExternalSale.status == "POSTED",
                    ExternalSale.document_date >= date_from,
                    ExternalSale.document_date <= date_to,
                )
            )
        ).all()
    )
    for sale in external_sales:
        if sale.document_date is None:
            continue
        for external_tax in sale.tax_summaries:
            if (
                external_tax.net_amount is None
                or external_tax.vat_amount is None
                or external_tax.rate_percent is None
            ):
                continue
            apply_tax_event(
                event_kind=ReportTaxEventKind.DOCUMENT_TAX,
                event_date=sale.document_date,
                taxable=quantize_to_minor_unit(Decimal(str(external_tax.net_amount))),
                vat=quantize_to_minor_unit(Decimal(str(external_tax.vat_amount))),
                code="NL_DOMESTIC",
                effect="APPLY_RATE",
                requires_icp=False,
                rate=Decimal(str(external_tax.rate_percent)),
                source_row_id=external_tax.id,
                external_sale_id=sale.id,
                external_source_name=sale.posted_source_name or "External source",
                external_original_number=sale.original_number or "",
            )

    # Quote-origin advances are recognised on their payment date, whether or
    # not the quote has already been converted. These are locked PaymentTax
    # snapshots; VAT is never inferred from the gross payment amount here.
    advance_result = await session.execute(
        select(
            Payment.id,
            Payment.payment_date,
            PaymentTax,
            Invoice.document_kind,
            Invoice.status,
            Invoice.settlement_status,
        )
        .join(PaymentTax, PaymentTax.payment_id == Payment.id)
        .outerjoin(Invoice, Invoice.id == Payment.invoice_id)
        .where(
            Payment.company_id == company_id,
            Payment.quote_id.is_not(None),
            Payment.deleted_at.is_(None),
            Payment.payment_date >= date_from,
            Payment.payment_date <= date_to,
        )
    )
    advance_info_payment_ids: set[uuid.UUID] = set()
    advance_warning_payment_ids: set[uuid.UUID] = set()
    for (
        payment_id,
        payment_date,
        payment_tax,
        document_kind,
        status,
        settlement_status,
    ) in advance_result.all():
        severity = _receipt_only_notice_severity(document_kind, status, settlement_status)
        if severity == "info":
            advance_info_payment_ids.add(payment_id)
        elif severity == "warning":
            advance_warning_payment_ids.add(payment_id)
        apply_tax_event(
            event_kind=ReportTaxEventKind.RECEIPT_ONLY_PAYMENT_TAX,
            event_date=payment_date,
            payment_id=payment_id,
            code=payment_tax.vat_treatment_code,
            effect=payment_tax.vat_treatment_effect,
            requires_icp=payment_tax.vat_treatment_requires_icp,
            rate=Decimal(str(payment_tax.vat_rate_percent)),
            taxable=quantize_to_minor_unit(Decimal(str(payment_tax.base_taxable_amount))),
            vat=quantize_to_minor_unit(Decimal(str(payment_tax.base_vat_amount))),
            source_row_id=payment_tax.id,
        )
    if advance_info_payment_ids:
        infos.append(
            f"Included {len(advance_info_payment_ids)} receipt-only quote deposit(s) "
            "by payment date. The linked issued Standard is settled; receipt-only "
            "recognition and offset audit are complete with no duplicate VAT."
        )
    if advance_warning_payment_ids:
        warnings.append(
            f"Included {len(advance_warning_payment_ids)} receipt-only quote deposit(s) "
            "by payment date without a fully settled issued Standard. Editing a "
            "filed-period payment may require a correction."
        )

    invoice_ids = [invoice.id for invoice in invoices]
    offsets_by_invoice: dict[uuid.UUID, list[tuple[uuid.UUID, PaymentTax]]] = {}
    offset_info_payment_ids: set[uuid.UUID] = set()
    offset_warning_payment_ids: set[uuid.UUID] = set()
    if invoice_ids:
        offset_result = await session.execute(
            select(Payment.invoice_id, Payment.id, PaymentTax)
            .join(PaymentTax, PaymentTax.payment_id == Payment.id)
            .where(
                Payment.invoice_id.in_(invoice_ids),
                Payment.company_id == company_id,
                Payment.quote_id.is_not(None),
                Payment.deleted_at.is_(None),
            )
        )
        for invoice_id, payment_id, payment_tax in offset_result.all():
            if invoice_id is None:
                continue
            offsets_by_invoice.setdefault(invoice_id, []).append((payment_id, payment_tax))

    # Track EU B2B invoices that need ICP but have no customer vat_id.
    icp_invoices_missing_vat_id: list[str] = []

    for inv in invoices:
        effect = inv.vat_treatment_effect
        requires_icp = inv.vat_treatment_requires_icp
        treatment_code = inv.vat_treatment_code

        if inv.tax_mode.value == "DOCUMENT":
            # DOCUMENT mode: aggregated per tax rate via InvoiceTax rows.
            taxes = inv.taxes  # lazy="selectin" → already loaded
            for tax in taxes:
                rate_pct = Decimal(str(tax.vat_rate_percent))
                taxable = quantize_to_minor_unit(Decimal(str(tax.taxable_amount)))
                vat_amt = quantize_to_minor_unit(Decimal(str(tax.tax_amount)))
                apply_tax_event(
                    event_kind=ReportTaxEventKind.DOCUMENT_TAX,
                    event_date=inv.invoice_date,
                    document=inv,
                    code=treatment_code,
                    effect=effect,
                    requires_icp=requires_icp,
                    rate=rate_pct,
                    taxable=taxable,
                    vat=vat_amt,
                    source_row_id=tax.id,
                )
        else:
            # LINE mode: per-line VAT via InvoiceLineTax rows.
            for line in inv.lines:
                for line_tax in line.line_taxes:
                    rate_pct = Decimal(str(line_tax.vat_rate_percent))
                    taxable = quantize_to_minor_unit(Decimal(str(line_tax.taxable_amount)))
                    vat_amt = quantize_to_minor_unit(Decimal(str(line_tax.tax_amount)))
                    apply_tax_event(
                        event_kind=ReportTaxEventKind.DOCUMENT_TAX,
                        event_date=inv.invoice_date,
                        document=inv,
                        code=treatment_code,
                        effect=effect,
                        requires_icp=requires_icp,
                        rate=rate_pct,
                        taxable=taxable,
                        vat=vat_amt,
                        source_row_id=line_tax.id,
                    )

        # The formal invoice contributes its complete VAT above. Subtract all
        # quote-origin advance snapshots attached to it, including advances
        # recognised in earlier quarters, so the project is filed exactly once.
        for payment_id, payment_tax in offsets_by_invoice.get(inv.id, []):
            severity = _receipt_only_notice_severity(
                inv.document_kind,
                inv.status,
                inv.settlement_status,
            )
            if severity == "info":
                offset_info_payment_ids.add(payment_id)
            elif severity == "warning":
                offset_warning_payment_ids.add(payment_id)
            apply_tax_event(
                event_kind=ReportTaxEventKind.RECEIPT_ONLY_INVOICE_OFFSET,
                event_date=inv.invoice_date,
                document=inv,
                payment_id=payment_id,
                code=payment_tax.vat_treatment_code,
                effect=payment_tax.vat_treatment_effect,
                requires_icp=payment_tax.vat_treatment_requires_icp,
                rate=Decimal(str(payment_tax.vat_rate_percent)),
                taxable=-quantize_to_minor_unit(
                    Decimal(str(payment_tax.base_taxable_amount))
                ),
                vat=-quantize_to_minor_unit(Decimal(str(payment_tax.base_vat_amount))),
                source_row_id=payment_tax.id,
            )

        # D5 / warning: EU B2B invoices going to 3b require a customer vat_id.
        if requires_icp and effect == _EFFECT_ZERO_REVERSE:
            from jai.models.customer import Customer  # avoid circular at module level

            stmt_cust = select(Customer).where(Customer.id == inv.customer_id)
            cust_result = await session.execute(stmt_cust)
            customer = cust_result.scalar_one_or_none()
            if customer is None or not customer.vat_id:
                icp_invoices_missing_vat_id.append(str(inv.invoice_number))

    if icp_invoices_missing_vat_id:
        missing_str = ", ".join(icp_invoices_missing_vat_id[:10])
        extra = (
            f" (and {len(icp_invoices_missing_vat_id) - 10} more)"
            if len(icp_invoices_missing_vat_id) > 10
            else ""
        )
        warnings.append(
            f"The following EU B2B (ICP) invoices are missing a customer VAT ID, "
            f"which is required for the Opgaaf ICP declaration: {missing_str}{extra}."
        )

    # -------------------------------------------------------------------
    # 1b. M12 formal document events.  Receipt-only remains entirely in
    #     the PaymentTax + Standard offset path above.
    # -------------------------------------------------------------------
    formal_result = await session.execute(
        select(Invoice).where(
            and_(
                Invoice.company_id == company_id,
                Invoice.document_kind.in_([
                    InvoiceDocumentKind.ADVANCE, InvoiceDocumentKind.FINAL,
                ]),
                Invoice.status.in_(revenue_statuses),
                Invoice.invoice_date >= date_from,
                Invoice.invoice_date <= date_to,
            )
        )
    )
    formal_invoices = list(formal_result.scalars().all())
    final_ids = [
        row.id
        for row in formal_invoices
        if row.document_kind == InvoiceDocumentKind.FINAL
    ]
    applied: dict[tuple[uuid.UUID, uuid.UUID], tuple[Decimal, Decimal]] = {}
    if final_ids:
        app_result = await session.execute(
            select(FinalAdvanceApplicationTax, FinalAdvanceApplication.final_invoice_id)
            .join(FinalAdvanceApplication)
            .where(FinalAdvanceApplication.final_invoice_id.in_(final_ids))
        )
        for tax, final_id in app_result.all():
            key = (final_id, tax.source_vat_rate_id)
            old_net, old_vat = applied.get(key, (_ZERO, _ZERO))
            applied[key] = (
                old_net + quantize_to_minor_unit(Decimal(str(tax.base_taxable_amount))),
                old_vat + quantize_to_minor_unit(Decimal(str(tax.base_vat_amount))),
            )

    def apply_formal_row(
        invoice: Invoice,
        rate: Decimal,
        taxable: Decimal,
        vat: Decimal,
        *,
        source_id: uuid.UUID | None = None,
        source_kind: InvoiceDocumentKind | None = None,
        source_number: str | None = None,
        treatment_code: str | None = None,
        treatment_effect: str | None = None,
        requires_icp: bool | None = None,
        source_row_id: uuid.UUID | None = None,
    ) -> None:
        if taxable == _ZERO and vat == _ZERO:
            return
        code = treatment_code if treatment_code is not None else invoice.vat_treatment_code
        effect = treatment_effect if treatment_effect is not None else invoice.vat_treatment_effect
        icp = requires_icp if requires_icp is not None else invoice.vat_treatment_requires_icp
        apply_tax_event(
            event_kind=ReportTaxEventKind.DOCUMENT_TAX,
            event_date=invoice.invoice_date,
            document=invoice,
            source_document_id=source_id,
            source_document_kind=source_kind,
            source_document_number=source_number,
            code=code,
            effect=effect,
            requires_icp=icp,
            rate=rate,
            taxable=taxable,
            vat=vat,
            source_row_id=source_row_id,
        )

    for invoice in formal_invoices:
        if invoice.tax_mode.value == "DOCUMENT":
            rows = [(tax.vat_rate_id, Decimal(str(tax.vat_rate_percent)),
                     quantize_to_minor_unit(Decimal(str(tax.taxable_amount))),
                     quantize_to_minor_unit(Decimal(str(tax.tax_amount))), tax.id)
                    for tax in invoice.taxes]
        else:
            rows = [(tax.vat_rate_id, Decimal(str(tax.vat_rate_percent)),
                     quantize_to_minor_unit(Decimal(str(tax.taxable_amount))),
                     quantize_to_minor_unit(Decimal(str(tax.tax_amount))), tax.id)
                    for line in invoice.lines for tax in line.line_taxes]
        for rate_id, rate, taxable, vat, tax_row_id in rows:
            if invoice.document_kind == InvoiceDocumentKind.FINAL:
                app_net, app_vat = applied.get((invoice.id, rate_id), (_ZERO, _ZERO))
                taxable -= app_net
                vat -= app_vat
            apply_formal_row(invoice, rate, taxable, vat, source_row_id=tax_row_id)

    from sqlalchemy.orm import aliased

    source_invoice = aliased(Invoice)
    credit_result = await session.execute(
        select(
            Invoice, InvoiceCorrection, InvoiceCorrectionLine, InvoiceCreditBasisLine,
            source_invoice.invoice_number, source_invoice.invoice_date,
            source_invoice.document_kind,
        )
        .join(InvoiceCorrection, InvoiceCorrection.credit_note_id == Invoice.id)
        .join(InvoiceCorrectionLine, InvoiceCorrectionLine.correction_id == InvoiceCorrection.id)
        .join(
            InvoiceCreditBasisLine,
            InvoiceCreditBasisLine.id == InvoiceCorrectionLine.source_basis_line_id,
        )
        .join(source_invoice, source_invoice.id == InvoiceCorrection.source_invoice_id)
        .where(
            Invoice.company_id == company_id,
            Invoice.document_kind == InvoiceDocumentKind.CREDIT_NOTE,
            Invoice.status.in_(revenue_statuses),
            Invoice.invoice_date >= date_from,
            Invoice.invoice_date <= date_to,
        )
    )
    warnings_by_credit: dict[uuid.UUID, ReportWarning] = {}
    for (
        credit,
        correction,
        line,
        basis,
        source_number,
        source_date,
        source_kind,
    ) in credit_result.all():
        net = -quantize_to_minor_unit(Decimal(str(line.base_net_amount)))
        vat = -quantize_to_minor_unit(Decimal(str(line.base_vat_amount)))
        apply_formal_row(
            credit, Decimal(str(basis.vat_rate_percent or _ZERO)), net, vat,
            source_id=correction.source_invoice_id,
            source_kind=source_kind,
            source_number=source_number,
            treatment_code=basis.vat_treatment_code, treatment_effect=basis.vat_treatment_effect,
            requires_icp=basis.vat_treatment_requires_icp,
            source_row_id=line.id,
        )
        if (
            quarter_label(source_date) != quarter_label(credit.invoice_date)
            and credit.id not in warnings_by_credit
        ):
            warnings_by_credit[credit.id] = ReportWarning(
                code=ReportWarningCode.CREDIT_CROSS_PERIOD,
                message=("This Credit Note is dated in a different VAT period from its source. "
                         "Review the applicable correction process with your accountant."),
                document=ReportDocumentReference(
                    document_id=str(credit.id), document_kind=InvoiceDocumentKind.CREDIT_NOTE,
                    document_number=credit.invoice_number, event_date=credit.invoice_date,
                    source_document_id=str(correction.source_invoice_id),
                    source_document_kind=source_kind,
                    source_document_number=source_number,
                ),
                source=ReportDocumentReference(
                    document_id=str(correction.source_invoice_id), document_kind=source_kind,
                    document_number=source_number, event_date=source_date,
                ),
                event_period=quarter_label(credit.invoice_date),
                source_period=quarter_label(source_date),
                amount=-quantize_to_minor_unit(
                    Decimal(str(correction.issued_base_gross_amount))
                ),
            )

    correction_warnings.extend(warnings_by_credit.values())

    if offset_info_payment_ids:
        infos.append(
            f"Applied final-invoice VAT offsets for {len(offset_info_payment_ids)} "
            "settled receipt-only quote deposit(s). Recognition and offset audit are "
            "complete with no duplicate VAT."
        )
    if offset_warning_payment_ids:
        warnings.append(
            f"Applied final-invoice VAT offsets for {len(offset_warning_payment_ids)} "
            "receipt-only quote deposit(s) without a fully settled issued Standard. "
            "Editing a filed-period payment may require a correction."
        )

    # -----------------------------------------------------------------------
    # 2. Expenses: confirmed expenses in [date_from, date_to]
    # -----------------------------------------------------------------------
    stmt_exp = select(Expense).where(
        and_(
            Expense.company_id == company_id,
            Expense.is_draft.is_(False),
            Expense.expense_date >= date_from,
            Expense.expense_date <= date_to,
        )
    )
    result_exp = await session.execute(stmt_exp)
    expenses_in_period = result_exp.scalars().all()

    for exp in expenses_in_period:
        _apply_expense_nl(
            acc,
            exp.vat_treatment_effect,
            exp.vat_treatment_code,
            Decimal(str(exp.vat_rate_percent)),
            quantize_to_minor_unit(Decimal(str(exp.base_net_amount))),
            quantize_to_minor_unit(Decimal(str(exp.base_vat_amount))),
            exp.deductible,
        )

    # -----------------------------------------------------------------------
    # 3. 1d – private-use correction (D-1D): only in Q4, based on full year
    # -----------------------------------------------------------------------
    if is_last_period:
        # Fetch all confirmed deductible expenses for the full calendar year.
        year_start = date(year, 1, 1)
        year_end = date(year, 12, 31)
        stmt_year_exp = select(Expense).where(
            and_(
                Expense.company_id == company_id,
                Expense.is_draft.is_(False),
                Expense.deductible.is_(True),
                Expense.expense_date >= year_start,
                Expense.expense_date <= year_end,
            )
        )
        result_year_exp = await session.execute(stmt_year_exp)
        year_expenses = list(result_year_exp.scalars().all())
        acc.b1d_vat = _compute_1d_vat(year_expenses)

    # -----------------------------------------------------------------------
    # 4. Build schema objects from accumulator
    # -----------------------------------------------------------------------
    boxes = VatReturnBoxes(
        box_1a=VatBoxBaseVat(base=acc.b1a_base, vat=acc.b1a_vat),
        box_1b=VatBoxBaseVat(base=acc.b1b_base, vat=acc.b1b_vat),
        box_1c=VatBoxBaseVat(base=acc.b1c_base, vat=acc.b1c_vat),
        box_1d=VatBoxVatOnly(vat=acc.b1d_vat),
        box_1e=VatBoxBaseOnly(base=acc.b1e_base),
        box_2a=VatBoxBaseVat(base=acc.b2a_base, vat=acc.b2a_vat),
        box_3a=VatBoxBaseOnly(base=acc.b3a_base),
        box_3b=VatBoxBaseOnly(base=acc.b3b_base),
        box_3c=VatBoxBaseOnly(base=acc.b3c_base),
        box_4a=VatBoxBaseVat(base=acc.b4a_base, vat=acc.b4a_vat),
        box_4b=VatBoxBaseVat(base=acc.b4b_base, vat=acc.b4b_vat),
        box_5b=VatBoxVatOnly(vat=acc.b5b_vat),
    )

    # D-BOX5: output_vat_total = 1a+1b+1c VAT + 1d + 2a+4a+4b self-assessed VAT
    output_vat_total = (
        acc.b1a_vat
        + acc.b1b_vat
        + acc.b1c_vat
        + acc.b1d_vat
        + acc.b2a_vat
        + acc.b4a_vat
        + acc.b4b_vat
    )
    net_payable_or_refundable = output_vat_total - acc.b5b_vat

    totals = VatReturnTotals(
        output_vat_total=VatBoxVatOnly(vat=output_vat_total),
        net_payable_or_refundable=VatBoxVatOnly(vat=net_payable_or_refundable),
    )

    event_rows.sort(
        key=lambda event: (
            # Every public identity, bucket-routing and signed-amount field
            # participates in the canonical order. Decimal values are compared
            # directly, preserving their exact persisted quantized values.
            _event_row_sort_key(event[0]),
            event[1],
        )
    )
    correction_warnings.sort(
        key=lambda warning: (warning.document.event_date, warning.document.document_id)
    )

    return VatReturnReport(
        year=year,
        quarter=quarter,
        date_from=date_from,
        date_to=date_to,
        is_last_period_of_year=is_last_period,
        boxes=boxes,
        totals=totals,
        warnings=warnings,
        infos=infos,
        event_rows=[row for row, _source_row_id in event_rows],
        correction_warnings=correction_warnings,
        disclaimer=_DISCLAIMER,
    )

"""Pydantic schemas for the M10 reporting module.

ProfitLossSeriesItem   – one time-bucket (month or quarter) in the P/L series.
ProfitLossReport       – full P/L report response.
VatReturnReport        – BTW/VAT return summary (step 2, NL ruleset).
IcpLine                – one customer line in the ICP report (step 3).
IcpReport              – full ICP report response.
ExpenseCategoryRow     – one category row in the expense report (step 4).
ExpenseReport          – full expense report response (step 4).

Schema layer never computes amounts (red-line 1).  All monetary fields are
``Decimal`` serialised as strings, matching the established money schema
convention (see payment.py).
"""

from __future__ import annotations

import enum
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from jai.models._enums import InvoiceDocumentKind, VatTreatmentEffect
from jai.schemas.battery import BatteryAnnualMissingInput


class ReportWarningCode(enum.StrEnum):
    """Machine-readable advisory codes emitted by reporting projections."""

    CREDIT_CROSS_PERIOD = "CREDIT_CROSS_PERIOD"


class ReportTaxEventKind(enum.StrEnum):
    """The immutable source family of one signed BTW event row."""

    DOCUMENT_TAX = "DOCUMENT_TAX"
    RECEIPT_ONLY_PAYMENT_TAX = "RECEIPT_ONLY_PAYMENT_TAX"
    RECEIPT_ONLY_INVOICE_OFFSET = "RECEIPT_ONLY_INVOICE_OFFSET"


class ReportSourceType(enum.StrEnum):
    INVOICE = "INVOICE"
    EXTERNAL_SALE = "EXTERNAL_SALE"


class ExternalSaleReportReference(BaseModel):
    source_type: ReportSourceType = ReportSourceType.EXTERNAL_SALE
    external_sale_id: str
    source_name: str
    original_number: str
    event_date: date


class ReportDocumentReference(BaseModel):
    """Frozen document reference carried by a reporting event row."""

    document_id: str
    document_kind: InvoiceDocumentKind
    document_number: str | None = None
    event_date: date
    source_document_id: str | None = None
    source_document_kind: InvoiceDocumentKind | None = None
    source_document_number: str | None = None


class ReportTaxEventRow(BaseModel):
    """One signed BTW event included exactly once in this period's invoice-side totals.

    ``vat_treatment_effect`` and ``vat_rate_percent`` are the immutable inputs
    used by the reporting service to route this row to its Dutch BTW box.
    They are deliberately carried alongside the signed amounts so consumers can
    audit the authoritative projection without inferring tax routing from a
    treatment code or rounded amount.
    """

    event_kind: ReportTaxEventKind
    document_id: str | None = None
    document_kind: InvoiceDocumentKind | None = None
    document_number: str | None = None
    event_date: date
    payment_id: str | None = Field(
        default=None,
        description="Receipt-only payment identity for payment-tax and offset events.",
    )
    source_document_id: str | None = None
    source_document_kind: InvoiceDocumentKind | None = None
    source_document_number: str | None = None
    source_type: ReportSourceType = ReportSourceType.INVOICE
    external_sale_id: str | None = None
    external_source_name: str | None = None
    external_original_number: str | None = None

    taxable_amount: Decimal
    vat_amount: Decimal
    vat_treatment_code: str
    vat_treatment_effect: VatTreatmentEffect = Field(
        description="Frozen treatment effect used to route this event to a BTW box."
    )
    vat_rate_percent: Decimal = Field(
        description="Frozen VAT rate percentage used to route this event to a BTW box."
    )
    requires_icp: bool


class ReportWarning(BaseModel):
    """Stable, advisory correction guidance; never a filing-state assertion."""

    code: ReportWarningCode
    message: str
    document: ReportDocumentReference
    source: ReportDocumentReference
    event_period: str
    source_period: str
    amount: Decimal = Field(
        description="Signed frozen base-currency gross correction amount (EUR)."
    )


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class ProfitLossSeriesItem(BaseModel):
    """One time-bucket in the P/L time series.

    ``period`` is an ISO date string representing the start of the bucket:
    - month granularity  → ``YYYY-MM-01``
    - quarter granularity → ``YYYY-01-01`` / ``YYYY-04-01`` / ``YYYY-07-01`` / ``YYYY-10-01``
    """

    period: str = Field(description="Bucket start date in YYYY-MM-DD format.")
    revenue_net: Decimal = Field(description="Net taxable revenue in this period (EUR).")
    expense_actual: Decimal = Field(
        description="Depreciation-adjusted, business%-prorated expense cost in this period (EUR)."
    )
    profit: Decimal = Field(description="revenue_net − expense_actual for this period.")


# ---------------------------------------------------------------------------
# Top-level report
# ---------------------------------------------------------------------------


class ProfitLossReport(BaseModel):
    """Response schema for GET /api/v1/reports/profit-loss.

    Monetary fields are Decimal (serialised as strings by FastAPI/Pydantic v2
    when ``model_config`` sets ``json_encoders`` or when callers use
    ``model.model_dump(mode='json')``).

    The ``from`` / ``to`` query parameter names are Python reserved words so
    internally they are stored as ``date_from`` / ``date_to`` but serialised
    (and documented in OpenAPI) as ``from`` / ``to``.
    """

    model_config = {"populate_by_name": True}

    date_from: date = Field(serialization_alias="from")
    date_to: date = Field(serialization_alias="to")
    granularity: str = Field(description="'month' or 'quarter'.")
    revenue_net: Decimal = Field(
        description="Total net taxable revenue across the full date range (EUR)."
    )
    expense_actual: Decimal = Field(
        description="Total depreciation-adjusted, business%-prorated expense cost (EUR)."
    )
    profit: Decimal = Field(description="revenue_net − expense_actual across the full range.")
    series: list[ProfitLossSeriesItem] = Field(
        description="Time-bucketed breakdown by month or quarter."
    )
    by_category: None = Field(
        default=None,
        description="Reserved for future per-category breakdown (step 1: always null).",
    )
    external_sales: list[ExternalSaleReportReference] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# BTW / VAT return report (step 2)
# ---------------------------------------------------------------------------


class VatBoxBaseVat(BaseModel):
    """A VAT return box with both taxable base and VAT amount.

    Used for boxes 1a, 1b, 1c, 2a, 4a, 4b.
    """

    base: Decimal = Field(description="Taxable base amount (net, EUR).")
    vat: Decimal = Field(description="VAT / BTW amount (EUR).")


class VatBoxVatOnly(BaseModel):
    """A VAT return box that holds only a VAT amount (no base column).

    Used for box 1d (privégebruik, BTW only) and 5b (voorbelasting).
    """

    vat: Decimal = Field(description="VAT / BTW amount (EUR).")


class VatBoxPrivateUse(BaseModel):
    """Private-use box with an unknown-aware taxable base."""

    base: Decimal | None = Field(
        description="Taxable base; null when a legacy contribution has no reliable base."
    )
    vat: Decimal = Field(description="VAT / BTW amount (EUR).")
    known_battery_base: Decimal = Field(description="Known battery base included in 1d.")
    known_battery_vat: Decimal = Field(description="Known battery VAT included in 1d.")
    missing_inputs: list[BatteryAnnualMissingInput] = Field(default_factory=list)
    is_complete: bool = True


class VatBoxBaseOnly(BaseModel):
    """A VAT return box that holds only a taxable base (no VAT column).

    Used for boxes 1e, 3a, 3b, 3c (0% / ICP / export – no BTW due).
    """

    base: Decimal = Field(description="Taxable base amount (net, EUR).")


class VatReturnBoxes(BaseModel):
    """All BTW return boxes as defined by the NL Belastingdienst form.

    Boxes with ``base + vat``: 1a, 1b, 1c, 2a, 4a, 4b.
    Boxes with ``vat only``: 1d, 5b.
    Boxes with ``base only``: 1e, 3a, 3b, 3c.

    v1 boxes that are always zero but kept for schema completeness:
    1c (other rates), 2a (domestic reverse-charge), 3c (distance selling), 4a (non-EU import).
    """

    box_1a: VatBoxBaseVat = Field(description="Domestic / EU-B2C hoog-tarief (21%) supplies.")
    box_1b: VatBoxBaseVat = Field(description="Domestic / EU-B2C laag-tarief (9%) supplies.")
    box_1c: VatBoxBaseVat = Field(description="Other rates (e.g. 13% forfait); v1 always 0.")
    box_1d: VatBoxPrivateUse = Field(
        description="Privégebruik (private-use correction); only non-zero in Q4 (year-end)."
    )
    box_1e: VatBoxBaseOnly = Field(
        description="0% or reverse-charge supplier-side supplies (net only)."
    )
    box_2a: VatBoxBaseVat = Field(
        description="Domestic reverse-charge (recipient side); v1 always 0."
    )
    box_3a: VatBoxBaseOnly = Field(description="Exports to non-EU countries (net only).")
    box_3b: VatBoxBaseOnly = Field(description="ICP (EU B2B reverse-charge) supplies (net only).")
    box_3c: VatBoxBaseOnly = Field(description="Distance / installation sales EU; v1 always 0.")
    box_4a: VatBoxBaseVat = Field(
        description="Non-EU import self-assessment (art.23); v1 always 0."
    )
    box_4b: VatBoxBaseVat = Field(description="EU intra-community acquisition self-assessment.")
    box_5b: VatBoxVatOnly = Field(description="Input VAT (voorbelasting) deductible.")


class VatReturnTotals(BaseModel):
    """Derived / auxiliary totals (not official box numbers, for display only).

    These mirror the numbers the tax authority's form program computes
    automatically.  Named after the Dutch official wording, not traditional
    5a / 5c labels (see D-BOX5 in M10.md).
    """

    output_vat_total: VatBoxVatOnly = Field(
        description=(
            "Sum of all output VAT (≈ traditional '5a').  "
            "= 1a.vat + 1b.vat + 1c.vat + 1d.vat + 2a.vat + 4a.vat + 4b.vat"
        )
    )
    net_payable_or_refundable: VatBoxVatOnly = Field(
        description=(
            "Net VAT payable (+) or refundable (−) "
            "(Totaal te betalen / terug te vragen; ≈ traditional '5c').  "
            "= output_vat_total.vat − 5b.vat"
        )
    )


# ---------------------------------------------------------------------------
# ICP report (step 3)
# ---------------------------------------------------------------------------


class IcpLine(BaseModel):
    """One customer line in the ICP report.

    Represents one EU-B2B customer's aggregated net amount for the quarter.
    """

    customer_id: str = Field(description="Customer UUID as string.")
    customer_name: str = Field(description="Customer display name (live join, not snapshotted).")
    country_code: str | None = Field(
        default=None,
        description=(
            "ISO 3166-1 alpha-2 country code from the customer's BILLING address. "
            "None if no billing address is on file."
        ),
    )
    vat_id: str | None = Field(
        default=None,
        description="Customer VAT number (live join).  None if not set.",
    )
    net_amount: Decimal = Field(
        description="Sum of base_taxable_amount for all requires_icp invoices in the quarter (EUR)."
    )
    source_documents: list[ReportDocumentReference] = Field(
        default_factory=list,
        description="Issued invoice/Credit event references contributing to this aggregate.",
    )


class IcpReport(BaseModel):
    """Response schema for GET /api/v1/reports/icp.

    The ``total_net`` must equal the BTW 3b net amount for the same quarter
    (D-ICP / guide §3.2 '3b ≡ Opgaaf ICP').
    """

    year: int = Field(description="Calendar year.")
    quarter: int = Field(description="Quarter (1–4).")
    lines: list[IcpLine] = Field(
        description="One line per customer with ICP invoices in the period."
    )
    total_net: Decimal = Field(
        description="Sum of all line net_amounts.  Must equal BTW box 3b for the same quarter."
    )
    warnings: list[str] = Field(
        default_factory=list,
        description=(
            "Advisory messages for customers missing vat_id or billing country_code – "
            "both are required fields for the Opgaaf ICP filing."
        ),
    )
    correction_warnings: list[ReportWarning] = Field(
        default_factory=list,
        description="Dated advisory warnings for cross-period Credit corrections; no filing state.",
    )


class VatReturnReport(BaseModel):
    """Response schema for GET /api/v1/reports/vat-return.

    The ``from`` / ``to`` fields use serialisation aliases so the JSON keys
    match the query-parameter convention used by the P/L report.
    """

    model_config = {"populate_by_name": True}

    year: int = Field(description="Calendar year of the VAT return period.")
    quarter: int = Field(description="Quarter of the VAT return period (1–4).")
    date_from: date = Field(serialization_alias="from", description="First day of the quarter.")
    date_to: date = Field(serialization_alias="to", description="Last day of the quarter.")
    is_last_period_of_year: bool = Field(
        description="True when quarter == 4; triggers 1d private-use calculation."
    )
    boxes: VatReturnBoxes = Field(description="Individual BTW return boxes.")
    totals: VatReturnTotals = Field(description="Derived totals (not official box numbers).")
    warnings: list[str] = Field(
        default_factory=list,
        description=(
            "Advisory messages, e.g. missing VAT-ID on ICP customers, "
            "or non-NL company using the NL ruleset as fallback."
        ),
    )
    infos: list[str] = Field(
        default_factory=list,
        description=(
            "Informational audit messages, including receipt-only quote deposits whose "
            "linked issued Standard invoice is fully settled."
        ),
    )
    event_rows: list[ReportTaxEventRow] = Field(
        default_factory=list,
        description=(
            "All signed invoice-side BTW events included exactly once in this period: "
            "document tax, receipt-only payment tax, and receipt-only invoice offsets."
        ),
    )
    correction_warnings: list[ReportWarning] = Field(
        default_factory=list,
        description="Dated advisory warnings for Credit corrections; no filing state.",
    )
    disclaimer: str = Field(
        description=(
            "Fixed disclaimer: this output is for bookkeeping assistance only; "
            "not tax or accounting advice.  Verify with your accountant / tax authority."
        )
    )


# ---------------------------------------------------------------------------
# Expense report (step 4)
# ---------------------------------------------------------------------------


class ExpenseCategoryRow(BaseModel):
    """One category row in the expense report.

    Aggregates net / VAT / gross amounts and the deductible split for all
    confirmed expenses in the requested date range belonging to this category.

    Grouping key:
    - ``category_id`` non-null  → grouped by the live category FK.
    - ``category_id`` null (category deleted) → grouped by ``category_name``
      snapshot.  Same-name snapshots are merged into one row.
    - Both null → merged into the "Uncategorised" catch-all row.

    Amounts are raw entry amounts (not prorated by business_percentage or
    depreciation_years) – this is the raw expense breakdown, distinct from P/L.
    """

    category_id: str | None = Field(
        default=None,
        description=(
            "Category UUID as string, or null when the category has been deleted "
            "(rows are then grouped by category_name snapshot)."
        ),
    )
    category_name: str = Field(
        description=(
            "Human-readable category name.  For live categories this is the "
            "snapshot captured at expense-entry time.  For deleted categories it "
            "is the preserved snapshot.  'Uncategorised' when both are absent."
        )
    )
    net: Decimal = Field(description="Sum of base_net_amount for this category (EUR).")
    vat: Decimal = Field(description="Sum of base_vat_amount for this category (EUR).")
    gross: Decimal = Field(description="Sum of base_gross_amount for this category (EUR).")
    deductible_net: Decimal = Field(
        description=(
            "Sum of base_net_amount for deductible=true expenses in this category (EUR). "
            "deductible_net + non_deductible_net == net."
        )
    )
    non_deductible_net: Decimal = Field(
        description=(
            "Sum of base_net_amount for deductible=false expenses in this category (EUR). "
            "deductible_net + non_deductible_net == net."
        )
    )


# ---------------------------------------------------------------------------
# Dashboard summary (step 5)
# ---------------------------------------------------------------------------


class DashboardKpi(BaseModel):
    """Key performance indicators for the dashboard.

    ytd_* values are for the full selected year (Jan–Dec).
    Invariant: ytd_* == Σ monthly (both derived from the same P/L call).
    """

    ytd_revenue: Decimal = Field(description="Year-to-date net revenue (EUR).")
    ytd_expense: Decimal = Field(
        description="Year-to-date depreciation-adjusted, business%-prorated expense (EUR)."
    )
    ytd_profit: Decimal = Field(description="ytd_revenue − ytd_expense (EUR).")
    current_quarter_vat_payable: Decimal = Field(
        description=(
            "Net VAT payable (+) or refundable (−) for the current/last quarter "
            "(compute_vat_return.totals.net_payable_or_refundable.vat, EUR)."
        )
    )


class DashboardMonthly(BaseModel):
    """One month in the dashboard time series.

    ``month`` is an ISO date string representing the first day of the month
    (YYYY-MM-01).  Exactly 12 items are always returned for the selected year.
    """

    month: str = Field(description="Month start date in YYYY-MM-01 format.")
    revenue: Decimal = Field(description="Net revenue in this month (EUR).")
    expense: Decimal = Field(
        description="Depreciation-adjusted, business%-prorated expense in this month (EUR)."
    )
    profit: Decimal = Field(description="revenue − expense for this month (EUR).")


class DashboardTopCategory(BaseModel):
    """One entry in the top expense categories list.

    Derived from compute_expense_report.by_category sorted descending by net,
    top 5 (fewer if fewer categories exist).
    """

    category_id: str | None = Field(
        default=None,
        description="Category UUID as string, or null for deleted/uncategorised categories.",
    )
    category_name: str = Field(description="Category display name.")
    net: Decimal = Field(
        description="Total net expense amount in this category for the year (EUR)."
    )


class DashboardSummary(BaseModel):
    """Response schema for GET /api/v1/reports/dashboard.

    Aggregated from P/L, BTW VAT return, and expense report services so that
    all numbers are guaranteed consistent with those sub-reports.
    """

    year: int = Field(description="The selected calendar year.")
    quarter: int = Field(
        description=(
            "The quarter used for current_quarter_vat_payable: "
            "current quarter if year == today's year, else Q4."
        )
    )
    kpi: DashboardKpi = Field(description="Key performance indicators.")
    monthly: list[DashboardMonthly] = Field(
        description="12-month series (Jan–Dec) for the selected year."
    )
    top_expense_categories: list[DashboardTopCategory] = Field(
        description=(
            "Top expense categories by net amount (descending), up to 5 entries. "
            "Fewer entries if fewer than 5 categories have expenses in the year."
        )
    )


class ExpenseReport(BaseModel):
    """Response schema for GET /api/v1/reports/expenses.

    The ``from`` / ``to`` query parameter names are Python reserved words so
    internally they are stored as ``date_from`` / ``date_to`` but serialised
    (and documented in OpenAPI) as ``from`` / ``to``, matching the P/L report
    convention.
    """

    model_config = {"populate_by_name": True}

    date_from: date = Field(
        serialization_alias="from",
        description="Inclusive start date of the report range.",
    )
    date_to: date = Field(
        serialization_alias="to",
        description="Inclusive end date of the report range.",
    )
    by_category: list[ExpenseCategoryRow] = Field(
        description=(
            "Per-category breakdown.  Empty list when no confirmed expenses fall "
            "within the requested date range."
        )
    )
    total_net: Decimal = Field(
        description="Sum of net across all categories (EUR). Equals Σ by_category[*].net."
    )
    total_vat: Decimal = Field(
        description="Sum of vat across all categories (EUR). Equals Σ by_category[*].vat."
    )
    total_gross: Decimal = Field(
        description="Sum of gross across all categories (EUR). Equals Σ by_category[*].gross."
    )
    total_deductible_net: Decimal = Field(
        description=(
            "Sum of deductible_net across all categories (EUR). "
            "total_deductible_net + total_non_deductible_net == total_net."
        )
    )
    total_non_deductible_net: Decimal = Field(
        description=(
            "Sum of non_deductible_net across all categories (EUR). "
            "total_deductible_net + total_non_deductible_net == total_net."
        )
    )

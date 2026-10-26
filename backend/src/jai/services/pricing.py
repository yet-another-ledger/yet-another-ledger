"""Pricing engine – authoritative money calculation for invoices (M5 step 1).

Public API
----------
- ``calculate_invoice``        – full calculation preview (reads DB for lookup,
                                 pure math for computation, never persists)
- ``derive_sales_vat_treatment`` – derive default treatment from customer /
                                   company geography
- ``compute_pricing``          – pure-calculation core (no DB, testable in isolation)

Design notes
------------
All monetary arithmetic uses ``Decimal`` with **line-level rounding to the
currency's minor unit** (EUR = 2 dp, ``ROUND_HALF_UP``; introduced in M7.5).
``quantize_money()`` (3 dp) is retained for intermediate predicates only.

The engine computes:

1. Line subtotal = ``quantize_to_minor_unit(qty × unit_price)`` (先乘后舍)
2. Line/document discount → ``quantize_to_minor_unit``
3. Document discount (fixed → distribute proportionally; percentage → direct);
   last non-zero line absorbs remainder so Σ shares == total_discount exactly.
4. Taxable amount = after discounts (already at minor-unit precision)
5. VAT = ``quantize_to_minor_unit(taxable × rate / 100)``  [excl-VAT input]
       or derived as ``gross − net``                         [incl-VAT input]
6. Line total = taxable + VAT  (no second rounding)

Aggregate invoice amounts = Σ line values (already at minor unit); no further
rounding.  ``unit_price`` is **not** rounded — it retains ≥ 3-dp input
precision so that large-quantity/small-unit-price calculations do not
accumulate truncation error.

Red-line 1 compliance: the schema layer never computes amounts; this module
is the sole authority.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jai.models._enums import DiscountType, InvoiceTaxMode, VatTreatmentEffect, VatTreatmentSide
from jai.models.company import Company
from jai.models.customer import Customer
from jai.models.vat import VatRate, VatTreatment
from jai.schemas.invoice import (
    DiscountInput,
    InvoiceCalculationRead,
    InvoiceCalculationRequest,
    InvoiceLineCalculationRead,
    InvoiceLineInput,
    InvoiceLineTaxRead,
    InvoiceTaxRead,
    VatTreatmentSnapshot,
)
from jai.schemas.quote import QuoteCalculationRequest
from jai.services.money import quantize_money, quantize_to_minor_unit

# ---------------------------------------------------------------------------
# EU country codes (ISO 3166-1 alpha-2) – 27 member states as of 2026
# ---------------------------------------------------------------------------

EU_COUNTRY_CODES: frozenset[str] = frozenset({
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR",
    "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL",
    "PL", "PT", "RO", "SK", "SI", "ES", "SE",
})

# Treatment code constants for derivation logic
_NL_DOMESTIC = "NL_DOMESTIC"
_EU_B2B_REVERSE = "EU_B2B_REVERSE"
_EU_B2C = "EU_B2C"
_EXPORT_NON_EU = "EXPORT_NON_EU"

# ---------------------------------------------------------------------------
# Internal calculation data classes
# ---------------------------------------------------------------------------


@dataclass
class _LineResult:
    """Intermediate result for a single line during pricing computation."""

    index: int
    input_line: InvoiceLineInput
    # Raw subtotal from input perspective
    raw_subtotal: Decimal
    # After line discount
    after_line_discount: Decimal
    line_discount_amount: Decimal
    # Document discount share
    document_discount_share: Decimal
    # Tax fields
    vat_rate_id: uuid.UUID | None
    vat_rate_label: str
    vat_rate_percent: Decimal
    effective_vat_percent: Decimal
    # Final amounts
    taxable_amount: Decimal
    vat_total: Decimal
    total_incl_vat: Decimal
    # Both perspectives
    subtotal_excl_vat: Decimal
    subtotal_incl_vat: Decimal


# ---------------------------------------------------------------------------
# Pure calculation helpers (no DB access)
# ---------------------------------------------------------------------------


def _calc_discount_amount(base: Decimal, discount: DiscountInput) -> Decimal:
    """Calculate discount amount from a ``DiscountInput`` on *base*.

    Returns the discount amount (≥ 0, ≤ base).  ``NONE`` returns 0.
    Raises ``ValueError`` for invalid discount parameters.
    """
    if discount.type == DiscountType.NONE:
        return Decimal("0")
    if discount.type == DiscountType.PERCENTAGE:
        if discount.value < 0:
            raise ValueError("Discount percentage must be ≥ 0.")
        result = quantize_to_minor_unit(base * discount.value / Decimal("100"))
        if result > base:
            raise ValueError("Percentage discount exceeds base amount.")
        return result
    if discount.type == DiscountType.FIXED:
        if discount.value < 0:
            raise ValueError("Fixed discount must be ≥ 0.")
        if discount.value > base:
            raise ValueError("Fixed discount exceeds base amount.")
        return quantize_to_minor_unit(discount.value)
    # Should be unreachable with StrEnum, but guard anyway
    raise ValueError(f"Unknown discount type: {discount.type}")


def _distribute_fixed_discount(
    bases: list[Decimal],
    total_discount: Decimal,
) -> list[Decimal]:
    """Distribute a fixed document discount proportionally across lines.

    Each share (except the last non-zero line) is
    ``quantize_to_minor_unit(total_discount × base_i / Σ bases)``, capped to
    the remaining discount to prevent over-distribution from rounding.  The
    last non-zero base absorbs whatever is left so that
    ``Σ shares == total_discount`` exactly and every share ``≥ 0``.
    """
    total_base = _decimal_sum(iter(bases))
    if is_money_zero_or_negative(total_base) or is_money_zero_or_negative(total_discount):
        return [Decimal("0")] * len(bases)

    shares: list[Decimal] = [Decimal("0")] * len(bases)
    remaining = total_discount
    last_nonzero_idx = -1

    # Identify last non-zero base
    for i, base_i in enumerate(bases):
        if base_i > 0:
            last_nonzero_idx = i

    # Distribute proportionally, capping each share to remaining
    for i, base_i in enumerate(bases):
        if base_i <= 0:
            continue
        if i == last_nonzero_idx:
            # Last non-zero line absorbs whatever remains
            shares[i] = remaining
        else:
            share = quantize_to_minor_unit(total_discount * base_i / total_base)
            # Cap to remaining to prevent over-distribution
            if share > remaining:
                share = remaining
            shares[i] = share
            remaining -= share

    return shares


def is_money_zero_or_negative(value: Decimal) -> bool:
    """Check whether a monetary value is zero or negative (after quantisation)."""
    return quantize_money(value) <= Decimal("0")


# ``sum()`` returns ``Decimal | Literal[0]`` under mypy --strict, which
# causes ``[assignment]`` errors when assigning to ``Decimal`` variables.
# Providing an explicit ``start=Decimal("0")`` makes mypy infer ``Decimal``.
def _decimal_sum(values: Iterator[Decimal]) -> Decimal:
    """Sum an iterator of Decimals with explicit Decimal start (mypy-safe)."""
    return sum(values, Decimal("0"))


# ---------------------------------------------------------------------------
# Pure pricing computation (no DB access)
# ---------------------------------------------------------------------------

# Type alias for the VAT rate lookup: rate_id → (label, percent)
_VatRateMap = dict[uuid.UUID, tuple[str, Decimal]]


def compute_pricing(
    lines: list[InvoiceLineInput],
    *,
    tax_mode: InvoiceTaxMode,
    amounts_include_vat: bool,
    discount: DiscountInput,
    treatment_effect: VatTreatmentEffect,
    vat_rates: _VatRateMap,
    document_vat_rate: tuple[str, Decimal] | None,
    line_rate_overrides: list[tuple[str, Decimal] | None] | None = None,
) -> InvoiceCalculationRead:
    """Pure calculation core – no DB access, no persistence.

    Parameters
    ----------
    lines:
        Line item inputs (already validated).
    tax_mode:
        ``LINE`` (per-line VAT) or ``DOCUMENT`` (single rate for whole invoice).
    amounts_include_vat:
        If ``True``, ``unit_price`` is treated as incl. VAT and excl/VAT are
        reverse-computed.
    discount:
        Document-level discount specification.
    treatment_effect:
        How the VAT treatment affects the effective rate (``APPLY_RATE`` → use
        actual rate; ``ZERO_*`` / ``EXEMPT`` → effective 0%).
    vat_rates:
        Map of ``vat_rate_id`` → ``(label, percent)`` for LINE mode lookups.
    document_vat_rate:
        ``(label, percent)`` for the document-level rate (DOCUMENT mode).

    Returns
    -------
    ``InvoiceCalculationRead`` with all amounts quantised.

    Raises
    ------
    ValueError
        If LINE mode and a line is missing ``vat_rate_id``, or DOCUMENT mode
        and ``document_vat_rate`` is ``None``.
    """
    if tax_mode == InvoiceTaxMode.LINE:
        return _compute_line_mode(
            lines,
            amounts_include_vat=amounts_include_vat,
            discount=discount,
            treatment_effect=treatment_effect,
            vat_rates=vat_rates,
            line_rate_overrides=line_rate_overrides,
        )
    return _compute_document_mode(
        lines,
        amounts_include_vat=amounts_include_vat,
        discount=discount,
        treatment_effect=treatment_effect,
        document_vat_rate=document_vat_rate,
    )


def _effective_rate(
    rate_percent: Decimal,
    treatment_effect: VatTreatmentEffect,
) -> Decimal:
    """Return the effective VAT rate accounting for treatment effect."""
    if treatment_effect == VatTreatmentEffect.APPLY_RATE:
        return rate_percent
    # ZERO_REVERSE, ZERO_EXPORT, EXEMPT → effective 0%
    return Decimal("0")


def _compute_line_mode(
    lines: list[InvoiceLineInput],
    *,
    amounts_include_vat: bool,
    discount: DiscountInput,
    treatment_effect: VatTreatmentEffect,
    vat_rates: _VatRateMap,
    line_rate_overrides: list[tuple[str, Decimal] | None] | None = None,
) -> InvoiceCalculationRead:
    """LINE mode: each line has its own VAT rate; tax is per-line."""
    # --- Phase 1: line subtotals + line discounts ---
    raw_subtotals: list[Decimal] = []
    line_discount_amounts: list[Decimal] = []
    after_line_discounts: list[Decimal] = []

    for line in lines:
        raw = quantize_to_minor_unit(line.quantity * line.unit_price)
        line_disc = _calc_discount_amount(raw, line.discount)
        after = raw - line_disc
        raw_subtotals.append(raw)
        line_discount_amounts.append(line_disc)
        after_line_discounts.append(after)

    # --- Phase 2: document discount ---
    total_after_line = _decimal_sum(iter(after_line_discounts))
    doc_disc_total = _calc_discount_amount(total_after_line, discount)

    # Distribute document discount proportionally
    doc_shares = _distribute_fixed_discount(after_line_discounts, doc_disc_total)

    # --- Phase 3: per-line tax calculation ---
    line_results: list[_LineResult] = []
    line_taxes: list[InvoiceLineTaxRead] = []

    for i, line in enumerate(lines):
        # Resolve VAT rate
        if line.vat_rate_id is None:
            raise ValueError(f"Line {i + 1}: vat_rate_id is required in LINE tax mode.")
        rate_info = (
            line_rate_overrides[i]
            if line_rate_overrides is not None
            and i < len(line_rate_overrides)
            and line_rate_overrides[i] is not None
            else vat_rates.get(line.vat_rate_id)
        )
        if rate_info is None:
            raise ValueError(f"Line {i + 1}: VAT rate {line.vat_rate_id} not found.")
        rate_label, rate_percent = rate_info

        doc_share = doc_shares[i]
        taxable = after_line_discounts[i] - doc_share
        eff_rate = _effective_rate(rate_percent, treatment_effect)

        if not amounts_include_vat:
            # Excl VAT input: taxable is excl, compute VAT then incl
            vat = quantize_to_minor_unit(taxable * eff_rate / Decimal("100"))
            total_incl = taxable + vat
            sub_excl = raw_subtotals[i]
            sub_incl = raw_subtotals[i]  # same as excl before VAT
        else:
            # Incl VAT input: taxable (after discounts) is incl, reverse-compute
            total_incl = taxable
            if eff_rate > 0:
                taxable = quantize_to_minor_unit(
                    total_incl / (Decimal("1") + eff_rate / Decimal("100"))
                )
                vat = total_incl - taxable
            else:
                taxable = total_incl
                vat = Decimal("0")
            # Reverse-compute raw subtotal excl
            raw_incl = raw_subtotals[i]
            if eff_rate > 0:
                sub_excl = quantize_to_minor_unit(
                    raw_incl / (Decimal("1") + eff_rate / Decimal("100"))
                )
            else:
                sub_excl = raw_incl
            sub_incl = raw_incl

        result = _LineResult(
            index=i,
            input_line=line,
            raw_subtotal=raw_subtotals[i],
            after_line_discount=after_line_discounts[i],
            line_discount_amount=line_discount_amounts[i],
            document_discount_share=doc_share,
            vat_rate_id=line.vat_rate_id,
            vat_rate_label=rate_label,
            vat_rate_percent=rate_percent,
            effective_vat_percent=eff_rate,
            taxable_amount=taxable,
            vat_total=vat,
            total_incl_vat=total_incl,
            subtotal_excl_vat=sub_excl,
            subtotal_incl_vat=sub_incl,
        )
        line_results.append(result)

        line_taxes.append(InvoiceLineTaxRead(
            vat_rate_id=line.vat_rate_id,
            vat_rate_label=rate_label,
            vat_rate_percent=rate_percent,
            effective_vat_percent=eff_rate,
            taxable_amount=taxable,
            tax_amount=vat,
        ))

    # --- Phase 4: aggregate ---
    return _build_response(
        line_results=line_results,
        line_taxes=line_taxes,
        document_taxes=[],
        tax_mode=InvoiceTaxMode.LINE,
        amounts_include_vat=amounts_include_vat,
        discount=discount,
    )


def _compute_document_mode(
    lines: list[InvoiceLineInput],
    *,
    amounts_include_vat: bool,
    discount: DiscountInput,
    treatment_effect: VatTreatmentEffect,
    document_vat_rate: tuple[str, Decimal] | None,
) -> InvoiceCalculationRead:
    """DOCUMENT mode: single rate for the whole invoice; tax is per-document."""
    if document_vat_rate is None:
        raise ValueError("document_vat_rate_id is required in DOCUMENT tax mode.")
    rate_label, rate_percent = document_vat_rate
    eff_rate = _effective_rate(rate_percent, treatment_effect)

    # --- Phase 1: line subtotals + line discounts ---
    line_results: list[_LineResult] = []

    for i, line in enumerate(lines):
        raw = quantize_to_minor_unit(line.quantity * line.unit_price)
        line_disc = _calc_discount_amount(raw, line.discount)
        after = raw - line_disc

        # In DOCUMENT mode, lines don't carry VAT individually
        if not amounts_include_vat:
            sub_excl = raw
            sub_incl = raw
            taxable = after
            vat = Decimal("0")
            total_incl = after
        else:
            sub_incl = raw
            if eff_rate > 0:
                sub_excl = quantize_to_minor_unit(
                    raw / (Decimal("1") + eff_rate / Decimal("100"))
                )
            else:
                sub_excl = raw
            taxable = after
            vat = Decimal("0")
            total_incl = after

        line_results.append(_LineResult(
            index=i,
            input_line=line,
            raw_subtotal=raw,
            after_line_discount=after,
            line_discount_amount=line_disc,
            document_discount_share=Decimal("0"),
            vat_rate_id=line.vat_rate_id,
            vat_rate_label="",
            vat_rate_percent=Decimal("0"),
            effective_vat_percent=Decimal("0"),
            taxable_amount=taxable,
            vat_total=vat,
            total_incl_vat=total_incl,
            subtotal_excl_vat=sub_excl,
            subtotal_incl_vat=sub_incl,
        ))

    # --- Phase 2: document-level aggregation + discount + tax ---
    total_after_line = _decimal_sum(lr.after_line_discount for lr in line_results)
    doc_disc_total = _calc_discount_amount(total_after_line, discount)
    doc_taxable = total_after_line - doc_disc_total

    if not amounts_include_vat:
        doc_vat = quantize_to_minor_unit(doc_taxable * eff_rate / Decimal("100"))
        doc_total_incl = doc_taxable + doc_vat
    else:
        doc_total_incl = doc_taxable
        if eff_rate > 0:
            doc_taxable = quantize_to_minor_unit(
                doc_total_incl / (Decimal("1") + eff_rate / Decimal("100"))
            )
            doc_vat = doc_total_incl - doc_taxable
        else:
            doc_vat = Decimal("0")

    # Adjust line totals to include the document discount effect
    # Distribute document discount to lines for display, even though tax is at doc level
    bases = [lr.after_line_discount for lr in line_results]
    doc_shares = _distribute_fixed_discount(bases, doc_disc_total)
    for i, lr in enumerate(line_results):
        lr.document_discount_share = doc_shares[i]
        lr.taxable_amount = lr.after_line_discount - doc_shares[i]
        # Line total_incl_vat stays as after_line_discount (no per-line VAT in DOCUMENT mode)

    document_taxes: list[InvoiceTaxRead] = []
    # Always emit a document tax entry – even when taxable/VAT is 0 (e.g. 100%
    # discount or zero-rate treatment) so the rate snapshot is preserved for
    # PDF rendering and M10 VAT reporting.
    document_taxes.append(InvoiceTaxRead(
        vat_rate_id=uuid.UUID(int=0),  # placeholder; real ID injected by caller
        vat_rate_label=rate_label,
        vat_rate_percent=rate_percent,
        effective_vat_percent=eff_rate,
        taxable_amount=doc_taxable,
        tax_amount=doc_vat,
    ))

    return _build_response(
        line_results=line_results,
        line_taxes=[],
        document_taxes=document_taxes,
        tax_mode=InvoiceTaxMode.DOCUMENT,
        amounts_include_vat=amounts_include_vat,
        discount=discount,
        doc_taxable_override=doc_taxable,
        doc_vat_override=doc_vat,
        doc_total_incl_override=doc_total_incl,
    )


def _build_response(
    *,
    line_results: list[_LineResult],
    line_taxes: list[InvoiceLineTaxRead],
    document_taxes: list[InvoiceTaxRead],
    tax_mode: InvoiceTaxMode,
    amounts_include_vat: bool,
    discount: DiscountInput,
    doc_taxable_override: Decimal | None = None,
    doc_vat_override: Decimal | None = None,
    doc_total_incl_override: Decimal | None = None,
) -> InvoiceCalculationRead:
    """Build ``InvoiceCalculationRead`` from line results."""
    _ZERO = Decimal("0")
    subtotal_excl = _decimal_sum(lr.subtotal_excl_vat for lr in line_results)
    line_disc_total = _decimal_sum(lr.line_discount_amount for lr in line_results)
    doc_disc_total = _decimal_sum(lr.document_discount_share for lr in line_results)

    if tax_mode == InvoiceTaxMode.DOCUMENT and doc_taxable_override is not None:
        taxable_total = doc_taxable_override
        vat_total = doc_vat_override if doc_vat_override is not None else _ZERO
        total_incl = (
            doc_total_incl_override if doc_total_incl_override is not None else _ZERO
        )
    else:
        taxable_total = _decimal_sum(lr.taxable_amount for lr in line_results)
        vat_total = _decimal_sum(lr.vat_total for lr in line_results)
        total_incl = _decimal_sum(lr.total_incl_vat for lr in line_results)

    line_reads = [
        InvoiceLineCalculationRead(
            product_id=lr.input_line.product_id,
            name=lr.input_line.name,
            description=lr.input_line.description,
            quantity=lr.input_line.quantity,
            unit_id=lr.input_line.unit_id,
            unit_name=lr.input_line.unit_name,
            unit_price=lr.input_line.unit_price,
            discount_type=lr.input_line.discount.type,
            discount_value=lr.input_line.discount.value,
            vat_rate_id=(
                lr.vat_rate_id
                if tax_mode == InvoiceTaxMode.LINE
                else lr.input_line.vat_rate_id
            ),
            vat_rate_label=lr.vat_rate_label if tax_mode == InvoiceTaxMode.LINE else None,
            vat_rate_percent=lr.vat_rate_percent if tax_mode == InvoiceTaxMode.LINE else None,
            subtotal_excl_vat=lr.subtotal_excl_vat,
            subtotal_incl_vat=lr.subtotal_incl_vat,
            line_discount_amount=lr.line_discount_amount,
            document_discount_share=lr.document_discount_share,
            taxable_amount=lr.taxable_amount,
            vat_total=lr.vat_total,
            total_incl_vat=lr.total_incl_vat,
        )
        for lr in line_results
    ]

    return InvoiceCalculationRead(
        tax_mode=tax_mode,
        amounts_include_vat=amounts_include_vat,
        discount_type=discount.type,
        discount_value=discount.value,
        vat_treatment_snapshot=None,  # filled by caller (calculate_invoice)
        subtotal_excl_vat=subtotal_excl,
        line_discount_total=line_disc_total,
        document_discount_amount=doc_disc_total,
        taxable_amount=taxable_total,
        vat_total=vat_total,
        total_incl_vat=total_incl,
        lines=line_reads,
        line_taxes=line_taxes,
        document_taxes=document_taxes,
    )


# ---------------------------------------------------------------------------
# DB-aware helpers
# ---------------------------------------------------------------------------


async def derive_sales_vat_treatment(
    session: AsyncSession,
    company_id: uuid.UUID,
    customer_id: uuid.UUID,
) -> VatTreatment | None:
    """Derive the default SALES VAT treatment from customer geography.

    Rules (M5 step 1):
    - Customer billing country is empty or == company country → NL_DOMESTIC
    - EU (≠ company country) + customer has VAT number → EU_B2B_REVERSE
    - EU (≠ company country) + no VAT number → EU_B2C
    - Non-EU → EXPORT_NON_EU
    - Matching active SALES treatment not found → None
    """
    # Fetch company
    company_stmt = select(Company).where(Company.id == company_id)
    company_result = await session.execute(company_stmt)
    company = company_result.scalar_one_or_none()
    if company is None:
        return None

    # Fetch customer with addresses (selectin loaded)
    customer_stmt = select(Customer).where(
        Customer.id == customer_id,
        Customer.company_id == company_id,
    )
    customer_result = await session.execute(customer_stmt)
    customer = customer_result.scalar_one_or_none()
    if customer is None:
        return None

    return await _derive_treatment_from_customer(session, company_id, customer)


async def _derive_treatment_from_customer(
    session: AsyncSession,
    company_id: uuid.UUID,
    customer: Customer,
) -> VatTreatment | None:
    """Internal: derive SALES treatment from an already-loaded customer object.

    Requires the caller to have already validated the customer belongs to the
    given company.  Fetches the company fresh for country_code lookup.
    """
    # Fetch company for country code
    company_stmt = select(Company).where(Company.id == company_id)
    company_result = await session.execute(company_stmt)
    company = company_result.scalar_one_or_none()
    if company is None:
        return None

    # Determine customer billing country (normalise to uppercase)
    billing_country: str | None = None
    for addr in customer.addresses:
        if addr.type == "BILLING":
            billing_country = addr.country_code.upper() if addr.country_code else None
            break

    # Normalise company country; fall back to NL when not set (Dutch-app default)
    raw_company_country = company.country_code
    company_country: str = (raw_company_country.upper() if raw_company_country else "NL")

    # Determine treatment code
    if not billing_country or billing_country == company_country:
        treatment_code = _NL_DOMESTIC
    elif billing_country in EU_COUNTRY_CODES:
        if customer.vat_id:
            treatment_code = _EU_B2B_REVERSE
        else:
            treatment_code = _EU_B2C
    else:
        treatment_code = _EXPORT_NON_EU

    # Look up active SALES treatment by code
    stmt = select(VatTreatment).where(
        VatTreatment.company_id == company_id,
        VatTreatment.code == treatment_code,
        VatTreatment.side == VatTreatmentSide.SALES,
        VatTreatment.active == True,  # noqa: E712
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def calculate_invoice(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    request: InvoiceCalculationRequest,
) -> InvoiceCalculationRead:
    """Full invoice calculation preview – reads DB, calls pure math, never persists.

    Steps:
    1. Validate currency matches company base_currency (M5 single-currency).
    2. Validate customer belongs to this company.
    3. Resolve VAT treatment (provided or derived, SALES side only).
    4. Load VAT rates for line items / document rate.
    5. Call ``compute_pricing`` for pure calculation.
    6. Attach treatment snapshot to response.
    """
    # --- 1. Company & currency validation ---
    company_stmt = select(Company).where(Company.id == company_id)
    company_result = await session.execute(company_stmt)
    company = company_result.scalar_one_or_none()
    if company is None:
        raise ValueError("Company not found.")

    request_currency = request.currency or company.base_currency
    if request_currency != company.base_currency:
        raise ValueError(
            f"M5 only supports invoices in company base currency "
            f"({company.base_currency}); got {request_currency}."
        )

    # --- 2. Validate customer belongs to this company ---
    customer_stmt = select(Customer).where(
        Customer.id == request.customer_id,
        Customer.company_id == company_id,
    )
    customer_result = await session.execute(customer_stmt)
    customer = customer_result.scalar_one_or_none()
    if customer is None:
        raise ValueError("Customer not found or does not belong to this company.")

    # --- 3. Resolve VAT treatment ---
    treatment: VatTreatment | None = None
    if request.vat_treatment_id is not None:
        # Explicit treatment – look up and validate
        stmt = select(VatTreatment).where(
            VatTreatment.id == request.vat_treatment_id,
            VatTreatment.company_id == company_id,
            VatTreatment.side == VatTreatmentSide.SALES,
            VatTreatment.active == True,  # noqa: E712
        )
        treat_result = await session.execute(stmt)
        treatment = treat_result.scalar_one_or_none()
        if treatment is None:
            raise ValueError(
                "VAT treatment not found, inactive, or not a sales treatment."
            )
    else:
        # Derive from customer geography (reuse already-validated customer)
        treatment = await _derive_treatment_from_customer(
            session, company_id, customer
        )
        if treatment is None:
            raise ValueError(
                "Could not derive a default VAT treatment for this customer. "
                "Please select one manually."
            )

    treatment_effect = VatTreatmentEffect(treatment.effect)

    # --- 4. Load VAT rates ---
    vat_rate_ids: set[uuid.UUID] = set()
    document_vat_rate: tuple[str, Decimal] | None = None

    if request.tax_mode == InvoiceTaxMode.LINE:
        for line in request.lines:
            if line.vat_rate_id is None:
                raise ValueError(
                    f"Line '{line.name}': vat_rate_id is required in LINE tax mode."
                )
            vat_rate_ids.add(line.vat_rate_id)
    else:
        # DOCUMENT mode
        if request.document_vat_rate_id is None:
            raise ValueError("document_vat_rate_id is required in DOCUMENT tax mode.")
        vat_rate_ids.add(request.document_vat_rate_id)

    # Batch-load all needed rates
    rates_stmt = select(VatRate).where(
        VatRate.id.in_(vat_rate_ids),
        VatRate.company_id == company_id,
    )
    rates_result = await session.execute(rates_stmt)
    rate_rows = rates_result.scalars().all()
    vat_rates: _VatRateMap = {r.id: (r.label, Decimal(str(r.percent))) for r in rate_rows}

    if request.tax_mode == InvoiceTaxMode.DOCUMENT:
        rate_info = vat_rates.get(request.document_vat_rate_id)  # type: ignore[arg-type]
        if rate_info is None:
            raise ValueError("Document VAT rate not found.")
        document_vat_rate = rate_info

    # Validate all line rate IDs exist
    if request.tax_mode == InvoiceTaxMode.LINE:
        for line in request.lines:
            if line.vat_rate_id not in vat_rates:
                raise ValueError(
                    f"Line '{line.name}': VAT rate not found."
                )

    # --- 5. Compute ---
    calc_result = compute_pricing(
        request.lines,
        tax_mode=request.tax_mode,
        amounts_include_vat=request.amounts_include_vat,
        discount=request.discount,
        treatment_effect=treatment_effect,
        vat_rates=vat_rates,
        document_vat_rate=document_vat_rate,
    )

    # --- 6. Attach treatment snapshot ---
    calc_result.vat_treatment_snapshot = VatTreatmentSnapshot(
        id=treatment.id,
        code=treatment.code,
        label=treatment.label,
        effect=(
            treatment.effect.value
            if isinstance(treatment.effect, VatTreatmentEffect)
            else str(treatment.effect)
        ),
        requires_icp=treatment.requires_icp,
    )

    # Fix document tax vat_rate_id placeholder in DOCUMENT mode
    if request.tax_mode == InvoiceTaxMode.DOCUMENT and calc_result.document_taxes:
        calc_result.document_taxes[0].vat_rate_id = request.document_vat_rate_id  # type: ignore[assignment]

    return calc_result


async def calculate_quote(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    request: QuoteCalculationRequest,
) -> InvoiceCalculationRead:
    """Full quote calculation preview – reads DB, calls pure math, never persists.

    Thin wrapper over ``calculate_invoice``: adapts ``QuoteCalculationRequest``
    (which uses ``quote_date``) to the shared calculation logic.  The date field
    is not used by the pricing engine itself – only stored on the document record –
    so the adaptation is lossless for calculation purposes.
    """
    invoice_req = InvoiceCalculationRequest(
        customer_id=request.customer_id,
        invoice_date=request.quote_date,
        currency=request.currency,
        tax_mode=request.tax_mode,
        amounts_include_vat=request.amounts_include_vat,
        vat_treatment_id=request.vat_treatment_id,
        document_vat_rate_id=request.document_vat_rate_id,
        discount=request.discount,
        lines=request.lines,
    )
    return await calculate_invoice(session, company_id=company_id, request=invoice_req)

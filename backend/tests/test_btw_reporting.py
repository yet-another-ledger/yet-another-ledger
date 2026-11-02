"""Unit tests for M10 step 2 – BTW VAT return reporting (services/reporting/btw.py).

Tests the pure algorithmic layer and the async orchestration function
(using mocked DB sessions for speed).

Coverage map (D-MAP table rows and special cases):
  HAPPY FLOW
  - 1a: NL_DOMESTIC APPLY_RATE hoog tarief (21%)
  - 1b: NL_DOMESTIC APPLY_RATE laag tarief (9%)
  - 1e: NL_DOMESTIC APPLY_RATE zero (0%)
  - 1c: APPLY_RATE other rate (e.g. 13%) → 1c
  - 3a: EXPORT_NON_EU ZERO_EXPORT → 3a
  - 3b: EU_B2B_REVERSE ZERO_REVERSE requires_icp → 3b
  - EU_B2C APPLY_RATE 21% → 1a (same as domestic hoog)
  - 4b: EU_B2B_REVERSE_PURCH ZERO_REVERSE → 4b (self-assessed) + 5b (if deductible)
  - 5b: NL_DOMESTIC_PURCH APPLY_RATE deductible=True → 5b full VAT
  - 1d: private-use correction only in Q4, based on full year
  - output_vat_total includes 4b self-assessed VAT
  - net_payable = output_vat_total - 5b

  CORNER CASES
  - EU_B2C_PURCH → no boxes (official exclusion)
  - IMPORT_NON_EU → no boxes (v1 not implemented)
  - deductible=False NL_DOMESTIC_PURCH → nothing in 5b
  - business_percentage=100 → 1d contribution = 0
  - 1d only in Q4, not in Q1/Q2/Q3
  - 4b net-zero effect: 4b.vat == 5b contribution from that expense
  - Custom tier thresholds (hoog=25, laag=12)
  - Quarter date boundaries (invoice_date at Q boundary edges)
  - Empty quarter → all boxes zero
  - Non-NL company → fallback NL + warning
  - Missing vat_id on EU B2B customer → warning in vat-return
  - DOCUMENT mode invoice uses InvoiceTax rows
  - LINE mode invoice uses InvoiceLineTax rows
  - Multiple invoices: sums accumulated correctly
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from jai.models._enums import (
    InvoiceDocumentKind,
    InvoiceSettlementStatus,
    InvoiceStatus,
)
from jai.schemas.setting import VatRateTiers
from jai.services.reporting.btw import (
    _apply_expense_nl,
    _apply_invoice_line_nl,
    _BoxAccumulator,
    _classify_rate,
    _compute_1d_vat,
    _quarter_date_range,
    _receipt_only_notice_severity,
    compute_vat_return,
)

# ---------------------------------------------------------------------------
# Default tiers used throughout tests (NL 2026 defaults)
# ---------------------------------------------------------------------------

DEFAULT_TIERS = VatRateTiers(hoog=21, laag=9, zero=0)
CUSTOM_TIERS = VatRateTiers(hoog=25, laag=12, zero=0)

_ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# Pure helper: _quarter_date_range
# ---------------------------------------------------------------------------


class TestQuarterDateRange:
    def test_q1(self) -> None:
        assert _quarter_date_range(2026, 1) == (date(2026, 1, 1), date(2026, 3, 31))

    def test_q2(self) -> None:
        assert _quarter_date_range(2026, 2) == (date(2026, 4, 1), date(2026, 6, 30))

    def test_q3(self) -> None:
        assert _quarter_date_range(2026, 3) == (date(2026, 7, 1), date(2026, 9, 30))

    def test_q4(self) -> None:
        assert _quarter_date_range(2026, 4) == (date(2026, 10, 1), date(2026, 12, 31))

    def test_leap_year_q1(self) -> None:
        # 2024 is a leap year – Feb has 29 days, but Q1 ends on March 31
        assert _quarter_date_range(2024, 1) == (date(2024, 1, 1), date(2024, 3, 31))


@pytest.mark.parametrize(
    ("document_kind", "status", "settlement_status", "expected"),
    [
        (None, None, None, "warning"),
        (
            InvoiceDocumentKind.STANDARD,
            InvoiceStatus.DRAFT,
            InvoiceSettlementStatus.OPEN,
            "warning",
        ),
        (
            InvoiceDocumentKind.STANDARD,
            InvoiceStatus.SENT,
            InvoiceSettlementStatus.PARTIALLY_SETTLED,
            "warning",
        ),
        (
            InvoiceDocumentKind.STANDARD,
            InvoiceStatus.COMPLETED,
            InvoiceSettlementStatus.SETTLED,
            "info",
        ),
        (
            InvoiceDocumentKind.ADVANCE,
            InvoiceStatus.SENT,
            InvoiceSettlementStatus.SETTLED,
            None,
        ),
    ],
)
def test_receipt_only_notice_severity(
    document_kind: InvoiceDocumentKind | None,
    status: InvoiceStatus | None,
    settlement_status: InvoiceSettlementStatus | None,
    expected: str | None,
) -> None:
    """Unconverted/DRAFT/unsettled remain warnings; formal Advance is excluded."""
    assert _receipt_only_notice_severity(document_kind, status, settlement_status) == expected


# ---------------------------------------------------------------------------
# Pure helper: _classify_rate
# ---------------------------------------------------------------------------


class TestClassifyRate:
    def test_hoog(self) -> None:
        assert _classify_rate(Decimal("21"), DEFAULT_TIERS) == "hoog"

    def test_laag(self) -> None:
        assert _classify_rate(Decimal("9"), DEFAULT_TIERS) == "laag"

    def test_zero(self) -> None:
        assert _classify_rate(Decimal("0"), DEFAULT_TIERS) == "zero"

    def test_other(self) -> None:
        assert _classify_rate(Decimal("13"), DEFAULT_TIERS) == "other"

    @pytest.mark.parametrize("rate", ["0.001", "0.5", "9.5", "21.5"])
    def test_fractional_rate_does_not_match_integer_tier(self, rate: str) -> None:
        assert _classify_rate(Decimal(rate), DEFAULT_TIERS) == "other"

    @pytest.mark.parametrize(
        ("rate", "box"),
        [("0.5", "other"), ("0.000", "zero"), ("21.000", "hoog"), ("9.000", "laag")],
    )
    def test_decimal_equality_preserves_native_box_amounts(self, rate: str, box: str) -> None:
        acc = _BoxAccumulator()
        _apply_invoice_line_nl(
            acc,
            "APPLY_RATE",
            False,
            "NL_DOMESTIC",
            Decimal(rate),
            Decimal("100.00"),
            Decimal("0.50") if box == "other" else Decimal("0.00"),
            DEFAULT_TIERS,
        )
        if box == "other":
            assert acc.b1c_base == Decimal("100.00")
            assert acc.b1c_vat == Decimal("0.50")
            assert acc.b1e_base == 0
        elif box == "zero":
            assert acc.b1e_base == Decimal("100.00")
            assert acc.b1c_vat == 0
        else:
            assert (acc.b1a_base if box == "hoog" else acc.b1b_base) == Decimal("100.00")

    def test_custom_tiers_hoog(self) -> None:
        assert _classify_rate(Decimal("25"), CUSTOM_TIERS) == "hoog"

    def test_custom_tiers_laag(self) -> None:
        assert _classify_rate(Decimal("12"), CUSTOM_TIERS) == "laag"

    def test_default_21_is_other_in_custom_tiers(self) -> None:
        assert _classify_rate(Decimal("21"), CUSTOM_TIERS) == "other"


# ---------------------------------------------------------------------------
# Pure helper: _apply_invoice_line_nl
# ---------------------------------------------------------------------------


class TestApplyInvoiceLineNL:
    """Test the NL ruleset pure function for invoice lines."""

    def _acc(self) -> _BoxAccumulator:
        return _BoxAccumulator()

    def test_hoog_tarief_goes_to_1a(self) -> None:
        acc = self._acc()
        _apply_invoice_line_nl(
            acc,
            treatment_effect="APPLY_RATE",
            requires_icp=False,
            treatment_code="NL_DOMESTIC",
            vat_rate_percent=Decimal("21"),
            taxable_base=Decimal("100.00"),
            vat_amount=Decimal("21.00"),
            tiers=DEFAULT_TIERS,
        )
        assert acc.b1a_base == Decimal("100.00")
        assert acc.b1a_vat == Decimal("21.00")
        # All others must be zero
        assert acc.b1b_base == _ZERO and acc.b1b_vat == _ZERO
        assert acc.b3a_base == _ZERO and acc.b3b_base == _ZERO

    def test_laag_tarief_goes_to_1b(self) -> None:
        acc = self._acc()
        _apply_invoice_line_nl(
            acc,
            treatment_effect="APPLY_RATE",
            requires_icp=False,
            treatment_code="NL_DOMESTIC",
            vat_rate_percent=Decimal("9"),
            taxable_base=Decimal("200.00"),
            vat_amount=Decimal("18.00"),
            tiers=DEFAULT_TIERS,
        )
        assert acc.b1b_base == Decimal("200.00")
        assert acc.b1b_vat == Decimal("18.00")

    def test_zero_tarief_goes_to_1e(self) -> None:
        acc = self._acc()
        _apply_invoice_line_nl(
            acc,
            treatment_effect="APPLY_RATE",
            requires_icp=False,
            treatment_code="NL_DOMESTIC",
            vat_rate_percent=Decimal("0"),
            taxable_base=Decimal("50.00"),
            vat_amount=Decimal("0.00"),
            tiers=DEFAULT_TIERS,
        )
        assert acc.b1e_base == Decimal("50.00")

    def test_other_rate_goes_to_1c(self) -> None:
        acc = self._acc()
        _apply_invoice_line_nl(
            acc,
            treatment_effect="APPLY_RATE",
            requires_icp=False,
            treatment_code="NL_DOMESTIC",
            vat_rate_percent=Decimal("13"),
            taxable_base=Decimal("80.00"),
            vat_amount=Decimal("10.40"),
            tiers=DEFAULT_TIERS,
        )
        assert acc.b1c_base == Decimal("80.00")
        assert acc.b1c_vat == Decimal("10.40")

    def test_export_non_eu_goes_to_3a(self) -> None:
        acc = self._acc()
        _apply_invoice_line_nl(
            acc,
            treatment_effect="ZERO_EXPORT",
            requires_icp=False,
            treatment_code="EXPORT_NON_EU",
            vat_rate_percent=Decimal("0"),
            taxable_base=Decimal("300.00"),
            vat_amount=Decimal("0.00"),
            tiers=DEFAULT_TIERS,
        )
        assert acc.b3a_base == Decimal("300.00")

    def test_eu_b2b_reverse_icp_goes_to_3b(self) -> None:
        acc = self._acc()
        _apply_invoice_line_nl(
            acc,
            treatment_effect="ZERO_REVERSE",
            requires_icp=True,
            treatment_code="EU_B2B_REVERSE",
            vat_rate_percent=Decimal("0"),
            taxable_base=Decimal("500.00"),
            vat_amount=Decimal("0.00"),
            tiers=DEFAULT_TIERS,
        )
        assert acc.b3b_base == Decimal("500.00")

    def test_eu_b2c_hoog_goes_to_1a(self) -> None:
        """EU_B2C with APPLY_RATE and hoog rate → 1a (D-OSS: no OSS, use NL rate)."""
        acc = self._acc()
        _apply_invoice_line_nl(
            acc,
            treatment_effect="APPLY_RATE",
            requires_icp=False,
            treatment_code="EU_B2C",
            vat_rate_percent=Decimal("21"),
            taxable_base=Decimal("150.00"),
            vat_amount=Decimal("31.50"),
            tiers=DEFAULT_TIERS,
        )
        assert acc.b1a_base == Decimal("150.00")
        assert acc.b1a_vat == Decimal("31.50")

    def test_custom_tiers_routes_correctly(self) -> None:
        """With CUSTOM_TIERS (hoog=25), rate=21 should go to 1c, not 1a."""
        acc = self._acc()
        _apply_invoice_line_nl(
            acc,
            treatment_effect="APPLY_RATE",
            requires_icp=False,
            treatment_code="NL_DOMESTIC",
            vat_rate_percent=Decimal("21"),
            taxable_base=Decimal("100.00"),
            vat_amount=Decimal("21.00"),
            tiers=CUSTOM_TIERS,
        )
        assert acc.b1c_base == Decimal("100.00")
        assert acc.b1a_base == _ZERO

    def test_multiple_lines_accumulate(self) -> None:
        acc = self._acc()
        for _ in range(3):
            _apply_invoice_line_nl(
                acc,
                treatment_effect="APPLY_RATE",
                requires_icp=False,
                treatment_code="NL_DOMESTIC",
                vat_rate_percent=Decimal("21"),
                taxable_base=Decimal("100.00"),
                vat_amount=Decimal("21.00"),
                tiers=DEFAULT_TIERS,
            )
        assert acc.b1a_base == Decimal("300.00")
        assert acc.b1a_vat == Decimal("63.00")


# ---------------------------------------------------------------------------
# Pure helper: _apply_expense_nl
# ---------------------------------------------------------------------------


class TestApplyExpenseNL:
    """Test the NL ruleset pure function for expenses."""

    def _acc(self) -> _BoxAccumulator:
        return _BoxAccumulator()

    def test_nl_domestic_purch_deductible_goes_to_5b(self) -> None:
        acc = self._acc()
        _apply_expense_nl(
            acc,
            treatment_effect="APPLY_RATE",
            treatment_code="NL_DOMESTIC_PURCH",
            vat_rate_percent=Decimal("21"),
            base_net_amount=Decimal("100.00"),
            base_vat_amount=Decimal("21.00"),
            deductible=True,
        )
        assert acc.b5b_vat == Decimal("21.00")
        # No 4b involvement for domestic purchase
        assert acc.b4b_base == _ZERO and acc.b4b_vat == _ZERO

    def test_nl_domestic_purch_not_deductible_nothing(self) -> None:
        acc = self._acc()
        _apply_expense_nl(
            acc,
            treatment_effect="APPLY_RATE",
            treatment_code="NL_DOMESTIC_PURCH",
            vat_rate_percent=Decimal("21"),
            base_net_amount=Decimal("100.00"),
            base_vat_amount=Decimal("21.00"),
            deductible=False,
        )
        assert acc.b5b_vat == _ZERO

    def test_eu_b2b_reverse_purch_deductible_4b_and_5b_net_zero(self) -> None:
        """EU_B2B_REVERSE_PURCH: 4b gets base + self-assessed VAT, 5b gets same VAT.

        Net effect on the btw balance: 4b.vat (output) - 5b (input) = 0.
        """
        acc = self._acc()
        _apply_expense_nl(
            acc,
            treatment_effect="ZERO_REVERSE",
            treatment_code="EU_B2B_REVERSE_PURCH",
            vat_rate_percent=Decimal("21"),
            base_net_amount=Decimal("200.00"),
            base_vat_amount=Decimal("0.00"),  # stored as 0 (reverse-charge line)
            deductible=True,
        )
        # Self-assessed VAT = 200 * 21/100 = 42.00
        expected_self_assessed = Decimal("42.00")
        assert acc.b4b_base == Decimal("200.00")
        assert acc.b4b_vat == expected_self_assessed
        assert acc.b5b_vat == expected_self_assessed
        # Net: 4b.vat contributed to output_vat; 5b same amount as input → net 0

    def test_eu_b2b_reverse_purch_not_deductible_4b_only(self) -> None:
        acc = self._acc()
        _apply_expense_nl(
            acc,
            treatment_effect="ZERO_REVERSE",
            treatment_code="EU_B2B_REVERSE_PURCH",
            vat_rate_percent=Decimal("21"),
            base_net_amount=Decimal("100.00"),
            base_vat_amount=Decimal("0.00"),
            deductible=False,
        )
        assert acc.b4b_base == Decimal("100.00")
        assert acc.b4b_vat == Decimal("21.00")
        # Not deductible → 5b gets nothing
        assert acc.b5b_vat == _ZERO

    def test_eu_b2c_purch_no_boxes(self) -> None:
        """EU_B2C_PURCH: consumer-side EU foreign VAT → does NOT enter NL return."""
        acc = self._acc()
        _apply_expense_nl(
            acc,
            treatment_effect="APPLY_RATE",
            treatment_code="EU_B2C_PURCH",
            vat_rate_percent=Decimal("21"),
            base_net_amount=Decimal("50.00"),
            base_vat_amount=Decimal("10.50"),
            deductible=True,  # even if marked deductible, EU_B2C_PURCH is excluded
        )
        # Nothing should be in any box
        assert acc.b5b_vat == _ZERO
        assert acc.b4b_base == _ZERO

    def test_import_non_eu_no_boxes(self) -> None:
        """IMPORT_NON_EU: v1 not implemented → no boxes."""
        acc = self._acc()
        _apply_expense_nl(
            acc,
            treatment_effect="APPLY_RATE",
            treatment_code="IMPORT_NON_EU",
            vat_rate_percent=Decimal("21"),
            base_net_amount=Decimal("300.00"),
            base_vat_amount=Decimal("63.00"),
            deductible=True,
        )
        assert acc.b5b_vat == _ZERO
        assert acc.b4b_base == _ZERO

    def test_4b_self_assessed_rounding(self) -> None:
        """Self-assessed VAT is rounded to cents before accumulation (D6)."""
        acc = self._acc()
        # 100.01 * 21 / 100 = 21.0021 → rounded to 21.00
        _apply_expense_nl(
            acc,
            treatment_effect="ZERO_REVERSE",
            treatment_code="EU_B2B_REVERSE_PURCH",
            vat_rate_percent=Decimal("21"),
            base_net_amount=Decimal("100.01"),
            base_vat_amount=Decimal("0.00"),
            deductible=True,
        )
        # 100.01 * 21 / 100 = 21.0021, rounded to 21.00
        assert acc.b4b_vat == Decimal("21.00")
        assert acc.b5b_vat == Decimal("21.00")


# ---------------------------------------------------------------------------
# Pure helper: _compute_1d_vat
# ---------------------------------------------------------------------------


class TestCompute1dVat:
    """Test the private-use correction computation."""

    def _make_expense(
        self,
        deductible: bool,
        business_pct: str,
        base_vat: str,
    ) -> MagicMock:
        exp = MagicMock()
        exp.deductible = deductible
        exp.business_percentage = Decimal(business_pct)
        exp.base_vat_amount = Decimal(base_vat)
        return exp

    def test_100_percent_business_contributes_zero(self) -> None:
        exp = self._make_expense(deductible=True, business_pct="100", base_vat="21.00")
        result = _compute_1d_vat([exp])
        assert result == _ZERO

    def test_50_percent_business(self) -> None:
        """50% business use → 50% of VAT is private-use → 1d = 0.5 * 21.00 = 10.50"""
        exp = self._make_expense(deductible=True, business_pct="50", base_vat="21.00")
        result = _compute_1d_vat([exp])
        assert result == Decimal("10.50")

    def test_not_deductible_excluded(self) -> None:
        """Non-deductible expenses don't contribute to 1d."""
        exp = self._make_expense(deductible=False, business_pct="50", base_vat="21.00")
        result = _compute_1d_vat([exp])
        assert result == _ZERO

    def test_empty_list(self) -> None:
        assert _compute_1d_vat([]) == _ZERO

    def test_multiple_expenses(self) -> None:
        """Sum of multiple expenses' private-use portions."""
        exps = [
            self._make_expense(deductible=True, business_pct="80", base_vat="100.00"),  # 20 private
            self._make_expense(deductible=True, business_pct="60", base_vat="50.00"),   # 20 private
            self._make_expense(deductible=False, business_pct="50", base_vat="10.00"),  # excluded
        ]
        result = _compute_1d_vat(exps)
        # 100 * 0.20 + 50 * 0.40 = 20 + 20 = 40
        assert result == Decimal("40.00")

    def test_rounding_per_expense(self) -> None:
        """Each expense contribution is rounded to cents before summing (D6)."""
        # 10.00 * (1 - 33.333/100) = 10 * 0.66667 = 6.6667 → rounds to 6.67
        exp = self._make_expense(deductible=True, business_pct="33.333", base_vat="10.00")
        result = _compute_1d_vat([exp])
        # private_fraction = 1 - 0.33333 = 0.66667
        # 10.00 * 0.66667 = 6.6667 → ROUND_HALF_UP to 6.67
        assert result == Decimal("6.67")


# ---------------------------------------------------------------------------
# Integration: compute_vat_return (mocked DB session)
# ---------------------------------------------------------------------------


def _make_company(country_code: str = "NL") -> MagicMock:
    co = MagicMock()
    co.id = uuid.uuid4()
    co.country_code = country_code
    return co


def _make_invoice(
    invoice_date: date,
    tax_mode: str,
    vat_treatment_effect: str,
    vat_treatment_code: str,
    requires_icp: bool = False,
    customer_id: uuid.UUID | None = None,
    invoice_number: str = "INV-001",
    taxes: list[MagicMock] | None = None,
    lines: list[MagicMock] | None = None,
) -> MagicMock:
    inv = MagicMock()
    inv.id = uuid.uuid4()
    inv.invoice_date = invoice_date
    inv.tax_mode = MagicMock()
    inv.tax_mode.value = tax_mode
    inv.vat_treatment_effect = vat_treatment_effect
    inv.vat_treatment_code = vat_treatment_code
    inv.vat_treatment_requires_icp = requires_icp
    inv.customer_id = customer_id or uuid.uuid4()
    inv.invoice_number = invoice_number
    inv.document_kind = InvoiceDocumentKind.STANDARD
    inv.status = InvoiceStatus.SENT
    inv.settlement_status = InvoiceSettlementStatus.OPEN
    inv.taxes = taxes or []
    inv.lines = lines or []
    return inv


def _make_invoice_tax(
    vat_rate_percent: str,
    taxable_amount: str,
    tax_amount: str,
) -> MagicMock:
    t = MagicMock()
    t.vat_rate_percent = Decimal(vat_rate_percent)
    t.taxable_amount = Decimal(taxable_amount)
    t.tax_amount = Decimal(tax_amount)
    return t


def _make_invoice_line(line_taxes: list[MagicMock]) -> MagicMock:
    line = MagicMock()
    line.line_taxes = line_taxes
    return line


def _make_invoice_line_tax(
    vat_rate_percent: str,
    taxable_amount: str,
    tax_amount: str,
) -> MagicMock:
    lt = MagicMock()
    lt.vat_rate_percent = Decimal(vat_rate_percent)
    lt.taxable_amount = Decimal(taxable_amount)
    lt.tax_amount = Decimal(tax_amount)
    return lt


def _make_payment_tax(
    vat_rate_percent: str,
    base_taxable_amount: str,
    base_vat_amount: str,
) -> MagicMock:
    tax = MagicMock()
    tax.vat_treatment_effect = "APPLY_RATE"
    tax.vat_treatment_requires_icp = False
    tax.vat_treatment_code = "NL_DOMESTIC"
    tax.vat_rate_percent = Decimal(vat_rate_percent)
    tax.base_taxable_amount = Decimal(base_taxable_amount)
    tax.base_vat_amount = Decimal(base_vat_amount)
    return tax


def _make_expense(
    expense_date: date,
    vat_treatment_effect: str,
    vat_treatment_code: str,
    vat_rate_percent: str,
    base_net_amount: str,
    base_vat_amount: str,
    deductible: bool = True,
    business_percentage: str = "100",
    is_draft: bool = False,
) -> MagicMock:
    exp = MagicMock()
    exp.expense_date = expense_date
    exp.vat_treatment_effect = vat_treatment_effect
    exp.vat_treatment_code = vat_treatment_code
    exp.vat_rate_percent = Decimal(vat_rate_percent)
    exp.base_net_amount = Decimal(base_net_amount)
    exp.base_vat_amount = Decimal(base_vat_amount)
    exp.deductible = deductible
    exp.business_percentage = Decimal(business_percentage)
    exp.is_draft = is_draft
    return exp


def _make_customer(vat_id: str | None = "NL123456789B01") -> MagicMock:
    c = MagicMock()
    c.vat_id = vat_id
    return c


def _build_session(
    invoices: list[MagicMock],
    expenses_in_period: list[MagicMock],
    year_expenses: list[MagicMock] | None = None,
    customer: MagicMock | None = None,
    advance_rows: list[tuple[object, ...]] | None = None,
    offset_rows: list[tuple[uuid.UUID, uuid.UUID, MagicMock]] | None = None,
) -> AsyncMock:
    """Build a mock AsyncSession that returns the given data from execute()."""
    session = AsyncMock()

    # We need to return different results depending on query order:
    # 1. Invoices query
    # 2. Quote-payment VAT rows in this period
    # 3. Final-invoice advance offsets (when invoices exist)
    # 4. Customer query (for each ICP invoice) – optional
    # 5. Formal Advance/Final invoices (Step 8)
    # 6. Formal Credit correction rows (Step 8)
    # 7. Expenses in period query
    # 8. Year expenses query (only in Q4)

    invoke_results: list[MagicMock] = []

    # compute_vat_return is also the shared RLS entry point for PaymentTax.
    # The SET LOCAL result is intentionally ignored by the service.
    invoke_results.append(MagicMock())

    # Result 1: invoices
    inv_result = MagicMock()
    inv_result.scalars.return_value.all.return_value = invoices
    invoke_results.append(inv_result)

    advance_result = MagicMock()
    # Payment-date is now exposed in the auditable event-row contract.  The
    # older financial fixtures intentionally did not care about that date, so
    # give their two-column shorthand a stable in-quarter default.
    advance_result.all.return_value = []
    for row in advance_rows or []:
        payment_id, payment_tax, *notice_state = row
        document_kind, status, settlement_status = (
            notice_state + [None, None, None]
        )[:3]
        advance_result.all.return_value.append(
            (
                payment_id,
                date(2026, 1, 1),
                payment_tax,
                document_kind,
                status,
                settlement_status,
            )
        )
    invoke_results.append(advance_result)

    if invoices:
        offset_result = MagicMock()
        offset_result.all.return_value = offset_rows or []
        invoke_results.append(offset_result)

    # Results for customer lookups (one per ICP invoice)
    for inv in invoices:
        if inv.vat_treatment_requires_icp and inv.vat_treatment_effect == "ZERO_REVERSE":
            cust_result = MagicMock()
            cust_result.scalar_one_or_none.return_value = customer
            invoke_results.append(cust_result)

    formal_result = MagicMock()
    formal_result.scalars.return_value.all.return_value = []
    invoke_results.append(formal_result)

    credit_result = MagicMock()
    credit_result.all.return_value = []
    invoke_results.append(credit_result)

    # Result: expenses in period
    exp_period_result = MagicMock()
    exp_period_result.scalars.return_value.all.return_value = expenses_in_period
    invoke_results.append(exp_period_result)

    # Result: year expenses (only consumed in Q4)
    if year_expenses is not None:
        year_exp_result = MagicMock()
        year_exp_result.scalars.return_value.all.return_value = year_expenses
        invoke_results.append(year_exp_result)

    session.execute.side_effect = invoke_results
    external_sales_result = MagicMock()
    external_sales_result.all.return_value = []
    session.scalars.return_value = external_sales_result
    return session


class TestComputeVatReturn:
    """Integration tests for compute_vat_return using a mocked DB session."""

    @pytest.mark.asyncio
    async def test_empty_quarter_all_zeros(self) -> None:
        company = _make_company()
        session = _build_session(invoices=[], expenses_in_period=[])
        report = await compute_vat_return(
            session, company=company, year=2026, quarter=1, tiers=DEFAULT_TIERS
        )
        assert report.boxes.box_1a.base == _ZERO
        assert report.boxes.box_1a.vat == _ZERO
        assert report.boxes.box_1b.base == _ZERO
        assert report.boxes.box_5b.vat == _ZERO
        assert report.totals.output_vat_total.vat == _ZERO
        assert report.totals.net_payable_or_refundable.vat == _ZERO
        assert report.is_last_period_of_year is False
        assert report.year == 2026
        assert report.quarter == 1
        assert report.date_from == date(2026, 1, 1)
        assert report.date_to == date(2026, 3, 31)

    @pytest.mark.asyncio
    async def test_quote_advance_is_recognised_on_payment_date(self) -> None:
        payment_id = uuid.uuid4()
        session = _build_session(
            invoices=[],
            expenses_in_period=[],
            advance_rows=[
                (payment_id, _make_payment_tax("21", "200.00", "42.00"))
            ],
        )
        report = await compute_vat_return(
            session,
            company=_make_company(),
            year=2026,
            quarter=1,
            tiers=DEFAULT_TIERS,
        )
        assert report.boxes.box_1a.base == Decimal("200.00")
        assert report.boxes.box_1a.vat == Decimal("42.00")
        assert any("receipt-only quote deposit" in warning for warning in report.warnings)

    @pytest.mark.asyncio
    async def test_final_invoice_subtracts_all_prior_quote_advances(self) -> None:
        invoice = _make_invoice(
            invoice_date=date(2026, 7, 1),
            tax_mode="DOCUMENT",
            vat_treatment_effect="APPLY_RATE",
            vat_treatment_code="NL_DOMESTIC",
            taxes=[_make_invoice_tax("21", "1000.00", "210.00")],
        )
        payment_id = uuid.uuid4()
        session = _build_session(
            invoices=[invoice],
            expenses_in_period=[],
            offset_rows=[
                (
                    invoice.id,
                    payment_id,
                    _make_payment_tax("21", "500.00", "105.00"),
                )
            ],
        )
        report = await compute_vat_return(
            session,
            company=_make_company(),
            year=2026,
            quarter=3,
            tiers=DEFAULT_TIERS,
        )
        assert report.boxes.box_1a.base == Decimal("500.00")
        assert report.boxes.box_1a.vat == Decimal("105.00")
        assert any("VAT offsets" in warning for warning in report.warnings)

    @pytest.mark.asyncio
    async def test_q2026_017_settled_receipt_only_deposits_are_infos_without_drift(self) -> None:
        """Two deposits plus balance retain their audit events and BTW values exactly."""
        invoice = _make_invoice(
            invoice_date=date(2026, 2, 15),
            tax_mode="DOCUMENT",
            vat_treatment_effect="APPLY_RATE",
            vat_treatment_code="NL_DOMESTIC",
            taxes=[_make_invoice_tax("21", "100.00", "21.00")],
            invoice_number="Q2026-017",
        )
        invoice.status = InvoiceStatus.COMPLETED
        invoice.settlement_status = InvoiceSettlementStatus.SETTLED
        first_id, second_id = uuid.uuid4(), uuid.uuid4()
        first_tax = _make_payment_tax("21", "20.00", "4.20")
        second_tax = _make_payment_tax("21", "50.00", "10.50")
        session = _build_session(
            invoices=[invoice],
            expenses_in_period=[],
            advance_rows=[
                (first_id, first_tax, InvoiceDocumentKind.STANDARD, InvoiceStatus.COMPLETED,
                 InvoiceSettlementStatus.SETTLED),
                (second_id, second_tax, InvoiceDocumentKind.STANDARD, InvoiceStatus.COMPLETED,
                 InvoiceSettlementStatus.SETTLED),
            ],
            offset_rows=[
                (invoice.id, first_id, first_tax),
                (invoice.id, second_id, second_tax),
            ],
        )

        report = await compute_vat_return(
            session, company=_make_company(), year=2026, quarter=1, tiers=DEFAULT_TIERS
        )

        assert report.boxes.box_1a.base == Decimal("100.00")
        assert report.boxes.box_1a.vat == Decimal("21.00")
        assert [row.event_kind.value for row in report.event_rows].count(
            "RECEIPT_ONLY_PAYMENT_TAX"
        ) == 2
        assert [row.event_kind.value for row in report.event_rows].count(
            "RECEIPT_ONLY_INVOICE_OFFSET"
        ) == 2
        assert [row.event_kind.value for row in report.event_rows].count("DOCUMENT_TAX") == 1
        assert len(report.infos) == 2
        assert all("receipt-only quote deposit" in info for info in report.infos)
        assert not any("receipt-only quote deposit" in warning for warning in report.warnings)

    @pytest.mark.asyncio
    async def test_receipt_only_notice_severity_splits_unsettled_and_dedupes_tax_buckets(
        self,
    ) -> None:
        """DRAFT/unsettled quote deposits remain warnings; one payment counts once per notice."""
        settled = _make_invoice(
            date(2026, 2, 15), "DOCUMENT", "APPLY_RATE", "NL_DOMESTIC", taxes=[]
        )
        settled.status = InvoiceStatus.SENT
        settled.settlement_status = InvoiceSettlementStatus.SETTLED
        unsettled = _make_invoice(
            date(2026, 2, 15), "DOCUMENT", "APPLY_RATE", "NL_DOMESTIC", taxes=[]
        )
        unsettled.status = InvoiceStatus.SENT
        unsettled.settlement_status = InvoiceSettlementStatus.PARTIALLY_SETTLED
        settled_id, unsettled_id, formal_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        first_bucket = _make_payment_tax("21", "20.00", "4.20")
        second_bucket = _make_payment_tax("9", "10.00", "0.90")
        session = _build_session(
            invoices=[settled, unsettled],
            expenses_in_period=[],
            advance_rows=[
                (settled_id, first_bucket, InvoiceDocumentKind.STANDARD, InvoiceStatus.SENT,
                 InvoiceSettlementStatus.SETTLED),
                (settled_id, second_bucket, InvoiceDocumentKind.STANDARD, InvoiceStatus.SENT,
                 InvoiceSettlementStatus.SETTLED),
                (unsettled_id, first_bucket, InvoiceDocumentKind.STANDARD, InvoiceStatus.SENT,
                 InvoiceSettlementStatus.PARTIALLY_SETTLED),
                (formal_id, first_bucket, InvoiceDocumentKind.ADVANCE, InvoiceStatus.SENT,
                 InvoiceSettlementStatus.SETTLED),
            ],
            offset_rows=[
                (settled.id, settled_id, first_bucket),
                (settled.id, settled_id, second_bucket),
                (unsettled.id, unsettled_id, first_bucket),
            ],
        )

        report = await compute_vat_return(
            session, company=_make_company(), year=2026, quarter=1, tiers=DEFAULT_TIERS
        )

        assert len(report.infos) == 2
        assert len([info for info in report.infos if "1 settled receipt-only" in info]) == 1
        assert len(report.warnings) == 2
        assert all("1 receipt-only quote deposit" in warning for warning in report.warnings)
        assert all("fully settled issued Standard" in warning for warning in report.warnings)
        assert all("formal" not in notice.lower() for notice in report.infos + report.warnings)

    @pytest.mark.asyncio
    async def test_same_quarter_advance_and_invoice_net_to_full_invoice(self) -> None:
        invoice = _make_invoice(
            invoice_date=date(2026, 2, 15),
            tax_mode="DOCUMENT",
            vat_treatment_effect="APPLY_RATE",
            vat_treatment_code="NL_DOMESTIC",
            taxes=[_make_invoice_tax("21", "1000.00", "210.00")],
        )
        payment_id = uuid.uuid4()
        payment_tax = _make_payment_tax("21", "200.00", "42.00")
        session = _build_session(
            invoices=[invoice],
            expenses_in_period=[],
            advance_rows=[(payment_id, payment_tax)],
            offset_rows=[(invoice.id, payment_id, payment_tax)],
        )
        report = await compute_vat_return(
            session,
            company=_make_company(),
            year=2026,
            quarter=1,
            tiers=DEFAULT_TIERS,
        )
        assert report.boxes.box_1a.base == Decimal("1000.00")
        assert report.boxes.box_1a.vat == Decimal("210.00")

    @pytest.mark.asyncio
    async def test_multi_quarter_advances_and_final_invoice_sum_exactly_once(
        self,
    ) -> None:
        first_id = uuid.uuid4()
        second_id = uuid.uuid4()
        first_tax = _make_payment_tax("21", "200.00", "42.00")
        second_tax = _make_payment_tax("21", "500.00", "105.00")

        q1 = await compute_vat_return(
            _build_session(
                invoices=[], expenses_in_period=[], advance_rows=[(first_id, first_tax)]
            ),
            company=_make_company(),
            year=2026,
            quarter=1,
            tiers=DEFAULT_TIERS,
        )
        q2 = await compute_vat_return(
            _build_session(
                invoices=[],
                expenses_in_period=[],
                advance_rows=[(second_id, second_tax)],
            ),
            company=_make_company(),
            year=2026,
            quarter=2,
            tiers=DEFAULT_TIERS,
        )
        invoice = _make_invoice(
            invoice_date=date(2026, 7, 1),
            tax_mode="DOCUMENT",
            vat_treatment_effect="APPLY_RATE",
            vat_treatment_code="NL_DOMESTIC",
            taxes=[_make_invoice_tax("21", "1000.00", "210.00")],
        )
        q3 = await compute_vat_return(
            _build_session(
                invoices=[invoice],
                expenses_in_period=[],
                offset_rows=[
                    (invoice.id, first_id, first_tax),
                    (invoice.id, second_id, second_tax),
                ],
            ),
            company=_make_company(),
            year=2026,
            quarter=3,
            tiers=DEFAULT_TIERS,
        )

        assert [q.boxes.box_1a.base for q in (q1, q2, q3)] == [
            Decimal("200.00"),
            Decimal("500.00"),
            Decimal("300.00"),
        ]
        assert [q.boxes.box_1a.vat for q in (q1, q2, q3)] == [
            Decimal("42.00"),
            Decimal("105.00"),
            Decimal("63.00"),
        ]
        assert sum((q.boxes.box_1a.base for q in (q1, q2, q3)), Decimal("0")) == Decimal(
            "1000.00"
        )
        assert sum((q.boxes.box_1a.vat for q in (q1, q2, q3)), Decimal("0")) == Decimal(
            "210.00"
        )

    @pytest.mark.asyncio
    async def test_mixed_and_zero_rate_advance_uses_existing_box_mapping(self) -> None:
        payment_id = uuid.uuid4()
        report = await compute_vat_return(
            _build_session(
                invoices=[],
                expenses_in_period=[],
                advance_rows=[
                    (payment_id, _make_payment_tax("21", "100.00", "21.00")),
                    (payment_id, _make_payment_tax("9", "50.00", "4.50")),
                    (payment_id, _make_payment_tax("0", "25.00", "0.00")),
                ],
            ),
            company=_make_company(),
            year=2026,
            quarter=1,
            tiers=DEFAULT_TIERS,
        )
        assert report.boxes.box_1a.base == Decimal("100.00")
        assert report.boxes.box_1a.vat == Decimal("21.00")
        assert report.boxes.box_1b.base == Decimal("50.00")
        assert report.boxes.box_1b.vat == Decimal("4.50")
        assert report.boxes.box_1e.base == Decimal("25.00")

    @pytest.mark.asyncio
    async def test_document_mode_invoice_hoog_tarief(self) -> None:
        inv = _make_invoice(
            invoice_date=date(2026, 2, 15),
            tax_mode="DOCUMENT",
            vat_treatment_effect="APPLY_RATE",
            vat_treatment_code="NL_DOMESTIC",
            taxes=[_make_invoice_tax("21", "1000.00", "210.00")],
        )
        company = _make_company()
        session = _build_session(invoices=[inv], expenses_in_period=[])
        report = await compute_vat_return(
            session, company=company, year=2026, quarter=1, tiers=DEFAULT_TIERS
        )
        assert report.boxes.box_1a.base == Decimal("1000.00")
        assert report.boxes.box_1a.vat == Decimal("210.00")
        assert report.totals.output_vat_total.vat == Decimal("210.00")

    @pytest.mark.asyncio
    async def test_line_mode_invoice_laag_tarief(self) -> None:
        line_tax = _make_invoice_line_tax("9", "200.00", "18.00")
        line = _make_invoice_line([line_tax])
        inv = _make_invoice(
            invoice_date=date(2026, 5, 1),
            tax_mode="LINE",
            vat_treatment_effect="APPLY_RATE",
            vat_treatment_code="NL_DOMESTIC",
            lines=[line],
        )
        company = _make_company()
        session = _build_session(invoices=[inv], expenses_in_period=[])
        report = await compute_vat_return(
            session, company=company, year=2026, quarter=2, tiers=DEFAULT_TIERS
        )
        assert report.boxes.box_1b.base == Decimal("200.00")
        assert report.boxes.box_1b.vat == Decimal("18.00")

    @pytest.mark.asyncio
    async def test_export_non_eu_3a(self) -> None:
        inv = _make_invoice(
            invoice_date=date(2026, 3, 31),
            tax_mode="DOCUMENT",
            vat_treatment_effect="ZERO_EXPORT",
            vat_treatment_code="EXPORT_NON_EU",
            taxes=[_make_invoice_tax("0", "500.00", "0.00")],
        )
        company = _make_company()
        session = _build_session(invoices=[inv], expenses_in_period=[])
        report = await compute_vat_return(
            session, company=company, year=2026, quarter=1, tiers=DEFAULT_TIERS
        )
        assert report.boxes.box_3a.base == Decimal("500.00")
        assert report.boxes.box_1a.base == _ZERO

    @pytest.mark.asyncio
    async def test_eu_b2b_reverse_3b(self) -> None:
        cust_id = uuid.uuid4()
        inv = _make_invoice(
            invoice_date=date(2026, 6, 15),
            tax_mode="DOCUMENT",
            vat_treatment_effect="ZERO_REVERSE",
            vat_treatment_code="EU_B2B_REVERSE",
            requires_icp=True,
            customer_id=cust_id,
            taxes=[_make_invoice_tax("0", "800.00", "0.00")],
        )
        customer = _make_customer(vat_id="DE123456789")
        company = _make_company()
        session = _build_session(
            invoices=[inv], expenses_in_period=[], customer=customer
        )
        report = await compute_vat_return(
            session, company=company, year=2026, quarter=2, tiers=DEFAULT_TIERS
        )
        assert report.boxes.box_3b.base == Decimal("800.00")
        assert len(report.warnings) == 0  # customer has vat_id

    @pytest.mark.asyncio
    async def test_eu_b2b_missing_vat_id_warning(self) -> None:
        cust_id = uuid.uuid4()
        inv = _make_invoice(
            invoice_date=date(2026, 6, 15),
            tax_mode="DOCUMENT",
            vat_treatment_effect="ZERO_REVERSE",
            vat_treatment_code="EU_B2B_REVERSE",
            requires_icp=True,
            customer_id=cust_id,
            invoice_number="INV-ICP-001",
            taxes=[_make_invoice_tax("0", "400.00", "0.00")],
        )
        customer = _make_customer(vat_id=None)  # missing!
        company = _make_company()
        session = _build_session(
            invoices=[inv], expenses_in_period=[], customer=customer
        )
        report = await compute_vat_return(
            session, company=company, year=2026, quarter=2, tiers=DEFAULT_TIERS
        )
        assert any("VAT ID" in w or "vat_id" in w.lower() for w in report.warnings)

    @pytest.mark.asyncio
    async def test_5b_full_vat_deductible(self) -> None:
        exp = _make_expense(
            expense_date=date(2026, 2, 1),
            vat_treatment_effect="APPLY_RATE",
            vat_treatment_code="NL_DOMESTIC_PURCH",
            vat_rate_percent="21",
            base_net_amount="100.00",
            base_vat_amount="21.00",
            deductible=True,
            business_percentage="100",
        )
        company = _make_company()
        session = _build_session(invoices=[], expenses_in_period=[exp])
        report = await compute_vat_return(
            session, company=company, year=2026, quarter=1, tiers=DEFAULT_TIERS
        )
        assert report.boxes.box_5b.vat == Decimal("21.00")

    @pytest.mark.asyncio
    async def test_5b_not_deductible_no_5b(self) -> None:
        exp = _make_expense(
            expense_date=date(2026, 2, 1),
            vat_treatment_effect="APPLY_RATE",
            vat_treatment_code="NL_DOMESTIC_PURCH",
            vat_rate_percent="21",
            base_net_amount="100.00",
            base_vat_amount="21.00",
            deductible=False,
        )
        company = _make_company()
        session = _build_session(invoices=[], expenses_in_period=[exp])
        report = await compute_vat_return(
            session, company=company, year=2026, quarter=1, tiers=DEFAULT_TIERS
        )
        assert report.boxes.box_5b.vat == _ZERO

    @pytest.mark.asyncio
    async def test_4b_eu_b2b_reverse_purch_net_zero(self) -> None:
        """EU intra-community purchase: 4b.vat == 5b contribution → net zero."""
        exp = _make_expense(
            expense_date=date(2026, 4, 10),
            vat_treatment_effect="ZERO_REVERSE",
            vat_treatment_code="EU_B2B_REVERSE_PURCH",
            vat_rate_percent="21",
            base_net_amount="200.00",
            base_vat_amount="0.00",
            deductible=True,
        )
        company = _make_company()
        session = _build_session(invoices=[], expenses_in_period=[exp])
        report = await compute_vat_return(
            session, company=company, year=2026, quarter=2, tiers=DEFAULT_TIERS
        )
        self_assessed = Decimal("42.00")  # 200 * 21%
        assert report.boxes.box_4b.base == Decimal("200.00")
        assert report.boxes.box_4b.vat == self_assessed
        assert report.boxes.box_5b.vat == self_assessed
        # Net payable contribution from 4b: 4b.vat (output) - 5b (input) = 0
        assert report.totals.output_vat_total.vat == self_assessed
        assert report.totals.net_payable_or_refundable.vat == _ZERO

    @pytest.mark.asyncio
    async def test_1d_zero_in_q1(self) -> None:
        """1d (private-use correction) must be zero in Q1."""
        exp = _make_expense(
            expense_date=date(2026, 1, 15),
            vat_treatment_effect="APPLY_RATE",
            vat_treatment_code="NL_DOMESTIC_PURCH",
            vat_rate_percent="21",
            base_net_amount="100.00",
            base_vat_amount="21.00",
            deductible=True,
            business_percentage="50",
        )
        company = _make_company()
        session = _build_session(invoices=[], expenses_in_period=[exp])
        report = await compute_vat_return(
            session, company=company, year=2026, quarter=1, tiers=DEFAULT_TIERS
        )
        assert report.boxes.box_1d.vat == _ZERO
        assert report.is_last_period_of_year is False

    @pytest.mark.asyncio
    async def test_1d_zero_in_q3(self) -> None:
        """1d must also be zero in Q3."""
        exp = _make_expense(
            expense_date=date(2026, 7, 1),
            vat_treatment_effect="APPLY_RATE",
            vat_treatment_code="NL_DOMESTIC_PURCH",
            vat_rate_percent="21",
            base_net_amount="100.00",
            base_vat_amount="21.00",
            deductible=True,
            business_percentage="50",
        )
        company = _make_company()
        session = _build_session(invoices=[], expenses_in_period=[exp])
        report = await compute_vat_return(
            session, company=company, year=2026, quarter=3, tiers=DEFAULT_TIERS
        )
        assert report.boxes.box_1d.vat == _ZERO

    @pytest.mark.asyncio
    async def test_1d_nonzero_in_q4(self) -> None:
        """1d is populated in Q4 from the full-year expenses."""
        # An expense from Q1 with 50% business use → contributes to 1d in Q4
        year_exp = _make_expense(
            expense_date=date(2026, 2, 1),
            vat_treatment_effect="APPLY_RATE",
            vat_treatment_code="NL_DOMESTIC_PURCH",
            vat_rate_percent="21",
            base_net_amount="100.00",
            base_vat_amount="21.00",
            deductible=True,
            business_percentage="50",  # 50% private → 50% of 21 = 10.50
        )
        company = _make_company()
        session = _build_session(
            invoices=[],
            expenses_in_period=[],
            year_expenses=[year_exp],
        )
        report = await compute_vat_return(
            session, company=company, year=2026, quarter=4, tiers=DEFAULT_TIERS
        )
        assert report.is_last_period_of_year is True
        assert report.boxes.box_1d.vat == Decimal("10.50")

    @pytest.mark.asyncio
    async def test_1d_business_100_percent_zero_in_q4(self) -> None:
        """business_percentage=100 → 1d = 0 even in Q4."""
        year_exp = _make_expense(
            expense_date=date(2026, 3, 1),
            vat_treatment_effect="APPLY_RATE",
            vat_treatment_code="NL_DOMESTIC_PURCH",
            vat_rate_percent="21",
            base_net_amount="500.00",
            base_vat_amount="105.00",
            deductible=True,
            business_percentage="100",
        )
        company = _make_company()
        session = _build_session(
            invoices=[],
            expenses_in_period=[],
            year_expenses=[year_exp],
        )
        report = await compute_vat_return(
            session, company=company, year=2026, quarter=4, tiers=DEFAULT_TIERS
        )
        assert report.boxes.box_1d.vat == _ZERO

    @pytest.mark.asyncio
    async def test_output_vat_total_includes_4b_self_assessed(self) -> None:
        """output_vat_total includes 4b.vat (self-assessed VAT) – D-BOX5."""
        # Invoice: 1a = 210.00
        inv = _make_invoice(
            invoice_date=date(2026, 1, 10),
            tax_mode="DOCUMENT",
            vat_treatment_effect="APPLY_RATE",
            vat_treatment_code="NL_DOMESTIC",
            taxes=[_make_invoice_tax("21", "1000.00", "210.00")],
        )
        # Expense: EU_B2B_REVERSE_PURCH → 4b.vat = 42.00 self-assessed
        exp = _make_expense(
            expense_date=date(2026, 1, 20),
            vat_treatment_effect="ZERO_REVERSE",
            vat_treatment_code="EU_B2B_REVERSE_PURCH",
            vat_rate_percent="21",
            base_net_amount="200.00",
            base_vat_amount="0.00",
            deductible=True,
        )
        company = _make_company()
        session = _build_session(invoices=[inv], expenses_in_period=[exp])
        report = await compute_vat_return(
            session, company=company, year=2026, quarter=1, tiers=DEFAULT_TIERS
        )
        # output_vat_total = 1a.vat + 4b.vat = 210 + 42 = 252
        assert report.totals.output_vat_total.vat == Decimal("252.00")
        # 5b = 42 (from deductible 4b)
        assert report.boxes.box_5b.vat == Decimal("42.00")
        # net_payable = 252 - 42 = 210
        assert report.totals.net_payable_or_refundable.vat == Decimal("210.00")

    @pytest.mark.asyncio
    async def test_non_nl_company_gets_warning(self) -> None:
        company = _make_company(country_code="DE")
        session = _build_session(invoices=[], expenses_in_period=[])
        report = await compute_vat_return(
            session, company=company, year=2026, quarter=1, tiers=DEFAULT_TIERS
        )
        assert any("NL" in w for w in report.warnings)

    @pytest.mark.asyncio
    async def test_nl_company_no_fallback_warning(self) -> None:
        company = _make_company(country_code="NL")
        session = _build_session(invoices=[], expenses_in_period=[])
        report = await compute_vat_return(
            session, company=company, year=2026, quarter=1, tiers=DEFAULT_TIERS
        )
        assert not any("fallback" in w.lower() for w in report.warnings)

    @pytest.mark.asyncio
    async def test_quarter_boundary_q1_last_day(self) -> None:
        """Invoice on Q1 last day (March 31) falls in Q1."""
        inv = _make_invoice(
            invoice_date=date(2026, 3, 31),
            tax_mode="DOCUMENT",
            vat_treatment_effect="APPLY_RATE",
            vat_treatment_code="NL_DOMESTIC",
            taxes=[_make_invoice_tax("21", "100.00", "21.00")],
        )
        company = _make_company()
        session = _build_session(invoices=[inv], expenses_in_period=[])
        report = await compute_vat_return(
            session, company=company, year=2026, quarter=1, tiers=DEFAULT_TIERS
        )
        assert report.boxes.box_1a.vat == Decimal("21.00")

    @pytest.mark.asyncio
    async def test_disclaimer_present(self) -> None:
        company = _make_company()
        session = _build_session(invoices=[], expenses_in_period=[])
        report = await compute_vat_return(
            session, company=company, year=2026, quarter=1, tiers=DEFAULT_TIERS
        )
        assert len(report.disclaimer) > 0
        assert "bookkeeping" in report.disclaimer.lower()

    @pytest.mark.asyncio
    async def test_custom_tiers_route_21_to_1c(self) -> None:
        """With CUSTOM_TIERS (hoog=25), a 21% invoice should go to 1c not 1a."""
        inv = _make_invoice(
            invoice_date=date(2026, 1, 5),
            tax_mode="DOCUMENT",
            vat_treatment_effect="APPLY_RATE",
            vat_treatment_code="NL_DOMESTIC",
            taxes=[_make_invoice_tax("21", "100.00", "21.00")],
        )
        company = _make_company()
        session = _build_session(invoices=[inv], expenses_in_period=[])
        report = await compute_vat_return(
            session, company=company, year=2026, quarter=1, tiers=CUSTOM_TIERS
        )
        assert report.boxes.box_1a.base == _ZERO
        assert report.boxes.box_1c.base == Decimal("100.00")
        assert report.boxes.box_1c.vat == Decimal("21.00")

    @pytest.mark.asyncio
    async def test_multiple_invoices_accumulated(self) -> None:
        """Multiple invoices with different rates all accumulate correctly."""
        inv1 = _make_invoice(
            invoice_date=date(2026, 1, 10),
            tax_mode="DOCUMENT",
            vat_treatment_effect="APPLY_RATE",
            vat_treatment_code="NL_DOMESTIC",
            taxes=[_make_invoice_tax("21", "1000.00", "210.00")],
        )
        inv2 = _make_invoice(
            invoice_date=date(2026, 2, 10),
            tax_mode="DOCUMENT",
            vat_treatment_effect="APPLY_RATE",
            vat_treatment_code="NL_DOMESTIC",
            taxes=[_make_invoice_tax("9", "500.00", "45.00")],
        )
        inv3 = _make_invoice(
            invoice_date=date(2026, 3, 10),
            tax_mode="DOCUMENT",
            vat_treatment_effect="ZERO_EXPORT",
            vat_treatment_code="EXPORT_NON_EU",
            taxes=[_make_invoice_tax("0", "200.00", "0.00")],
        )
        company = _make_company()
        session = _build_session(invoices=[inv1, inv2, inv3], expenses_in_period=[])
        report = await compute_vat_return(
            session, company=company, year=2026, quarter=1, tiers=DEFAULT_TIERS
        )
        assert report.boxes.box_1a.base == Decimal("1000.00")
        assert report.boxes.box_1a.vat == Decimal("210.00")
        assert report.boxes.box_1b.base == Decimal("500.00")
        assert report.boxes.box_1b.vat == Decimal("45.00")
        assert report.boxes.box_3a.base == Decimal("200.00")
        assert report.totals.output_vat_total.vat == Decimal("255.00")

    @pytest.mark.asyncio
    async def test_net_payable_formula(self) -> None:
        """net_payable = output_vat_total - 5b."""
        inv = _make_invoice(
            invoice_date=date(2026, 1, 1),
            tax_mode="DOCUMENT",
            vat_treatment_effect="APPLY_RATE",
            vat_treatment_code="NL_DOMESTIC",
            taxes=[_make_invoice_tax("21", "2000.00", "420.00")],
        )
        exp = _make_expense(
            expense_date=date(2026, 1, 15),
            vat_treatment_effect="APPLY_RATE",
            vat_treatment_code="NL_DOMESTIC_PURCH",
            vat_rate_percent="21",
            base_net_amount="500.00",
            base_vat_amount="105.00",
            deductible=True,
        )
        company = _make_company()
        session = _build_session(invoices=[inv], expenses_in_period=[exp])
        report = await compute_vat_return(
            session, company=company, year=2026, quarter=1, tiers=DEFAULT_TIERS
        )
        assert report.totals.output_vat_total.vat == Decimal("420.00")
        assert report.boxes.box_5b.vat == Decimal("105.00")
        assert report.totals.net_payable_or_refundable.vat == Decimal("315.00")

    @pytest.mark.asyncio
    async def test_eu_b2c_purch_not_in_any_box(self) -> None:
        """EU_B2C_PURCH (consumer-side foreign VAT) must not enter any NL box."""
        exp = _make_expense(
            expense_date=date(2026, 2, 1),
            vat_treatment_effect="APPLY_RATE",
            vat_treatment_code="EU_B2C_PURCH",
            vat_rate_percent="21",
            base_net_amount="80.00",
            base_vat_amount="16.80",
            deductible=True,
        )
        company = _make_company()
        session = _build_session(invoices=[], expenses_in_period=[exp])
        report = await compute_vat_return(
            session, company=company, year=2026, quarter=1, tiers=DEFAULT_TIERS
        )
        assert report.boxes.box_5b.vat == _ZERO
        assert report.boxes.box_4b.vat == _ZERO
        assert report.totals.output_vat_total.vat == _ZERO

    @pytest.mark.asyncio
    async def test_import_non_eu_not_in_any_box(self) -> None:
        """IMPORT_NON_EU (v1 not implemented) must not enter any NL box."""
        exp = _make_expense(
            expense_date=date(2026, 3, 15),
            vat_treatment_effect="APPLY_RATE",
            vat_treatment_code="IMPORT_NON_EU",
            vat_rate_percent="21",
            base_net_amount="500.00",
            base_vat_amount="105.00",
            deductible=True,
        )
        company = _make_company()
        session = _build_session(invoices=[], expenses_in_period=[exp])
        report = await compute_vat_return(
            session, company=company, year=2026, quarter=1, tiers=DEFAULT_TIERS
        )
        assert report.boxes.box_5b.vat == _ZERO
        assert report.boxes.box_4a.vat == _ZERO

    @pytest.mark.asyncio
    async def test_q4_is_last_period(self) -> None:
        company = _make_company()
        session = _build_session(
            invoices=[], expenses_in_period=[], year_expenses=[]
        )
        report = await compute_vat_return(
            session, company=company, year=2026, quarter=4, tiers=DEFAULT_TIERS
        )
        assert report.is_last_period_of_year is True
        assert report.date_from == date(2026, 10, 1)
        assert report.date_to == date(2026, 12, 31)

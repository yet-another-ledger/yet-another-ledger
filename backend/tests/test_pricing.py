"""Unit tests for the pricing engine (M5 step 1, updated in M7.5 step 2).

Tests ``services.pricing.compute_pricing`` (pure calculation, no DB) and
helper functions.  All amounts are verified against hand-calculated values
using ``ROUND_HALF_UP`` / **2 decimal places (currency minor unit)** as of
M7.5.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from jai.models._enums import DiscountType, InvoiceTaxMode, VatTreatmentEffect
from jai.schemas.invoice import DiscountInput, InvoiceLineInput
from jai.services.money import quantize_to_minor_unit
from jai.services.pricing import (
    _calc_discount_amount,
    _distribute_fixed_discount,
    compute_pricing,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RATE_21 = Decimal("21.000")
_RATE_9 = Decimal("9.000")
_RATE_0 = Decimal("0.000")


def _make_line(
    name: str = "Item",
    quantity: Decimal = Decimal("1"),
    unit_price: Decimal = Decimal("100.000"),
    discount: DiscountInput | None = None,
    vat_rate_id: uuid.UUID | None = None,
) -> InvoiceLineInput:
    return InvoiceLineInput(
        name=name,
        quantity=quantity,
        unit_price=unit_price,
        discount=discount or DiscountInput(),
        vat_rate_id=vat_rate_id,
    )


_RATE_ID_21 = uuid.uuid4()
_RATE_ID_9 = uuid.uuid4()
_RATE_ID_0 = uuid.uuid4()

_VAT_RATES: dict[uuid.UUID, tuple[str, Decimal]] = {
    _RATE_ID_21: ("21%", _RATE_21),
    _RATE_ID_9: ("9%", _RATE_9),
    _RATE_ID_0: ("0%", _RATE_0),
}


# ---------------------------------------------------------------------------
# _calc_discount_amount
# ---------------------------------------------------------------------------


class TestCalcDiscountAmount:

    def _disc(self, dtype: DiscountType, value: str = "0") -> DiscountInput:
        return DiscountInput(type=dtype, value=Decimal(value))

    def test_none_type_returns_zero(self) -> None:
        assert _calc_discount_amount(Decimal("100"), self._disc(DiscountType.NONE)) == Decimal("0")

    def test_percentage_discount(self) -> None:
        result = _calc_discount_amount(Decimal("200"), self._disc(DiscountType.PERCENTAGE, "10"))
        # 200 × 10% = 20.00
        assert result == Decimal("20.00")

    def test_percentage_discount_rounding(self) -> None:
        """33.33% of 100 → 33.33 (ROUND_HALF_UP to 2 dp)."""
        result = _calc_discount_amount(Decimal("100"), self._disc(DiscountType.PERCENTAGE, "33.33"))
        # 100 × 33.33 / 100 = 33.33 → quantize_to_minor_unit → 33.33
        assert result == Decimal("33.33")

    def test_percentage_100_percent(self) -> None:
        result = _calc_discount_amount(Decimal("100"), self._disc(DiscountType.PERCENTAGE, "100"))
        assert result == Decimal("100.00")

    def test_percentage_exceeds_base_raises(self) -> None:
        with pytest.raises(ValueError, match="exceeds base"):
            _calc_discount_amount(Decimal("100"), self._disc(DiscountType.PERCENTAGE, "150"))

    def test_fixed_discount(self) -> None:
        result = _calc_discount_amount(Decimal("100"), self._disc(DiscountType.FIXED, "25"))
        assert result == Decimal("25.00")

    def test_fixed_discount_exceeds_base_raises(self) -> None:
        with pytest.raises(ValueError, match="exceeds base"):
            _calc_discount_amount(Decimal("10"), self._disc(DiscountType.FIXED, "20"))

    def test_fixed_negative_raises(self) -> None:
        """Pydantic ge=0 on DiscountInput.value catches negative before service."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _calc_discount_amount(Decimal("100"), self._disc(DiscountType.FIXED, "-5"))

    def test_percentage_negative_raises(self) -> None:
        """Pydantic ge=0 on DiscountInput.value catches negative before service."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _calc_discount_amount(Decimal("100"), self._disc(DiscountType.PERCENTAGE, "-10"))


# ---------------------------------------------------------------------------
# _distribute_fixed_discount
# ---------------------------------------------------------------------------


class TestDistributeFixedDiscount:

    def test_single_line_absorbs_all(self) -> None:
        shares = _distribute_fixed_discount([Decimal("100")], Decimal("10"))
        assert shares == [Decimal("10.00")]

    def test_equal_bases_equal_shares(self) -> None:
        shares = _distribute_fixed_discount([Decimal("100"), Decimal("100")], Decimal("10"))
        assert len(shares) == 2
        assert shares[0] == Decimal("5.00")
        assert shares[1] == Decimal("5.00")
        assert sum(shares) == Decimal("10.00")

    def test_proportional_distribution(self) -> None:
        shares = _distribute_fixed_discount([Decimal("300"), Decimal("100")], Decimal("10"))
        assert shares[0] == Decimal("7.50")
        assert shares[1] == Decimal("2.50")
        assert sum(shares) == Decimal("10.00")

    def test_rounding_remainder_absorbed_by_last_nonzero(self) -> None:
        """3 lines of 100 each, discount of 10 → 3.33 + 3.33 + 3.34 (at 2 dp)."""
        shares = _distribute_fixed_discount(
            [Decimal("100"), Decimal("100"), Decimal("100")],
            Decimal("10"),
        )
        assert sum(shares) == Decimal("10.00")
        # Last line absorbs the 0.01 remainder
        assert shares[2] == Decimal("3.34")

    def test_zero_base_gets_zero_share(self) -> None:
        shares = _distribute_fixed_discount([Decimal("0"), Decimal("100")], Decimal("10"))
        assert shares[0] == Decimal("0")
        assert shares[1] == Decimal("10.00")

    def test_all_zero_bases_get_zero(self) -> None:
        shares = _distribute_fixed_discount([Decimal("0"), Decimal("0")], Decimal("10"))
        assert all(s == Decimal("0") for s in shares)

    def test_zero_discount(self) -> None:
        shares = _distribute_fixed_discount([Decimal("100"), Decimal("100")], Decimal("0"))
        assert all(s == Decimal("0") for s in shares)

    def test_sum_equals_discount_exactly(self) -> None:
        """Fuzz: 7 lines with odd values, discount of 1.23."""
        bases = [Decimal(str(v)) for v in [100, 234, 56.7, 999, 12.345, 0.001, 88.88]]
        discount = Decimal("1.23")
        shares = _distribute_fixed_discount(bases, discount)
        assert sum(shares) == discount

    def test_tiny_discount_many_lines_no_negative_shares(self) -> None:
        """10 equal lines, discount 0.05 → no negative shares."""
        shares = _distribute_fixed_discount([Decimal("100")] * 10, Decimal("0.05"))
        assert sum(shares) == Decimal("0.05")
        assert all(s >= Decimal("0") for s in shares)

    def test_all_shares_non_negative_property(self) -> None:
        """Property: all shares are always >= 0 regardless of input."""
        bases = [Decimal(str(v)) for v in [100, 234, 56.7, 0.001, 88.88]]
        discount = Decimal("0.03")
        shares = _distribute_fixed_discount(bases, discount)
        assert sum(shares) == discount
        assert all(s >= Decimal("0") for s in shares)


# ---------------------------------------------------------------------------
# compute_pricing – LINE mode, excl VAT
# ---------------------------------------------------------------------------


class TestComputePricingLineExclVAT:

    def test_single_line_21pct(self) -> None:
        """1 line: 2 × €100, 21% VAT, no discounts.
        Hand-calc: subtotal = quantize(2×100) = 100.00×2... wait, 2×100=200.
        subtotal = 200.00, VAT = quantize(200.00×21/100) = 42.00, total = 242.00.
        """
        lines = [
            _make_line(
                quantity=Decimal("2"),
                unit_price=Decimal("100"),
                vat_rate_id=_RATE_ID_21,
            )
        ]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=False,
            discount=DiscountInput(),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        assert result.subtotal_excl_vat == Decimal("200.00")
        assert result.line_discount_total == Decimal("0")
        assert result.document_discount_amount == Decimal("0")
        assert result.taxable_amount == Decimal("200.00")
        assert result.vat_total == Decimal("42.00")  # 200 × 21%
        assert result.total_incl_vat == Decimal("242.00")
        assert len(result.lines) == 1
        assert len(result.line_taxes) == 1

    def test_multi_rate_lines(self) -> None:
        """2 lines: 1×€100 @ 21% + 1×€100 @ 9%.
        21%: vat=21.00; 9%: vat=9.00; total=230.00.
        """
        lines = [
            _make_line(name="A", unit_price=Decimal("100"), vat_rate_id=_RATE_ID_21),
            _make_line(name="B", unit_price=Decimal("100"), vat_rate_id=_RATE_ID_9),
        ]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=False,
            discount=DiscountInput(),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        assert result.subtotal_excl_vat == Decimal("200.00")
        assert result.vat_total == Decimal("30.00")  # 21 + 9
        assert result.total_incl_vat == Decimal("230.00")

    def test_zero_rate_line(self) -> None:
        """1 line @ 0% VAT."""
        lines = [_make_line(unit_price=Decimal("50"), vat_rate_id=_RATE_ID_0)]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=False,
            discount=DiscountInput(),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        assert result.vat_total == Decimal("0")
        assert result.total_incl_vat == Decimal("50.00")

    def test_line_percentage_discount(self) -> None:
        """1 line: €200 with 10% line discount, 21% VAT.
        subtotal=200.00, disc=20.00, taxable=180.00, vat=37.80, total=217.80.
        """
        lines = [_make_line(
            unit_price=Decimal("200"),
            discount=DiscountInput(type=DiscountType.PERCENTAGE, value=Decimal("10")),
            vat_rate_id=_RATE_ID_21,
        )]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=False,
            discount=DiscountInput(),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        # Line discount: 200 × 10% = 20.00
        # Taxable: 180.00, VAT: 180.00×21/100 = 37.80
        assert result.line_discount_total == Decimal("20.00")
        assert result.taxable_amount == Decimal("180.00")
        assert result.vat_total == Decimal("37.80")
        assert result.total_incl_vat == Decimal("217.80")

    def test_line_fixed_discount(self) -> None:
        """1 line: €100 with €15 fixed discount, 21% VAT.
        taxable=85.00, vat=85.00×21/100=17.85.
        """
        lines = [_make_line(
            unit_price=Decimal("100"),
            discount=DiscountInput(type=DiscountType.FIXED, value=Decimal("15")),
            vat_rate_id=_RATE_ID_21,
        )]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=False,
            discount=DiscountInput(),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        assert result.line_discount_total == Decimal("15.00")
        assert result.taxable_amount == Decimal("85.00")
        assert result.vat_total == Decimal("17.85")

    def test_document_percentage_discount(self) -> None:
        """2 lines of €100 each, 10% document discount, 21% VAT.
        subtotal=200.00, doc_disc=20.00, taxable per line=90.00.
        vat per line = 90.00×21/100 = 18.90; total vat = 37.80.
        """
        lines = [
            _make_line(name="A", unit_price=Decimal("100"), vat_rate_id=_RATE_ID_21),
            _make_line(name="B", unit_price=Decimal("100"), vat_rate_id=_RATE_ID_21),
        ]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=False,
            discount=DiscountInput(type=DiscountType.PERCENTAGE, value=Decimal("10")),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        # Subtotal: 200, doc discount: 20.00, each line taxable: 90.00
        # vat per line: 18.90, total vat: 37.80
        assert result.document_discount_amount == Decimal("20.00")
        assert result.taxable_amount == Decimal("180.00")
        assert result.vat_total == Decimal("37.80")

    def test_document_fixed_discount_proportional(self) -> None:
        """3 lines of 300/200/100, €30 fixed document discount, 21% VAT.
        subtotal=600.00, doc_disc=30.00, taxable=570.00.
        vat: line shares are 15.00, 10.00, 5.00 (proportional).
        Line taxable: 285.00, 190.00, 95.00.
        vat per line: 59.85, 39.90, 19.95.
        total vat = 59.85 + 39.90 + 19.95 = 119.70.
        """
        lines = [
            _make_line(name="A", unit_price=Decimal("300"), vat_rate_id=_RATE_ID_21),
            _make_line(name="B", unit_price=Decimal("200"), vat_rate_id=_RATE_ID_21),
            _make_line(name="C", unit_price=Decimal("100"), vat_rate_id=_RATE_ID_21),
        ]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=False,
            discount=DiscountInput(type=DiscountType.FIXED, value=Decimal("30")),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        assert result.document_discount_amount == Decimal("30.00")
        # Total taxable = 600 - 30 = 570
        assert result.taxable_amount == Decimal("570.00")
        # VAT = 59.85 + 39.90 + 19.95 = 119.70
        assert result.vat_total == Decimal("119.70")
        # Verify per-line shares sum to discount
        line_shares = [line.document_discount_share for line in result.lines]
        assert sum(line_shares) == Decimal("30.00")

    def test_line_and_document_discounts_combined(self) -> None:
        """1 line: €100, 10% line disc + €5 fixed doc disc, 21% VAT.
        line disc=10.00, after=90.00; doc_disc=5.00; taxable=85.00; vat=17.85.
        """
        lines = [_make_line(
            unit_price=Decimal("100"),
            discount=DiscountInput(type=DiscountType.PERCENTAGE, value=Decimal("10")),
            vat_rate_id=_RATE_ID_21,
        )]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=False,
            discount=DiscountInput(type=DiscountType.FIXED, value=Decimal("5")),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        # Line disc: 10, after: 90. Doc disc: 5. Taxable: 85.
        assert result.line_discount_total == Decimal("10.00")
        assert result.document_discount_amount == Decimal("5.00")
        assert result.taxable_amount == Decimal("85.00")
        assert result.vat_total == Decimal("17.85")

    def test_missing_vat_rate_id_raises(self) -> None:
        """LINE mode requires vat_rate_id on every line."""
        lines = [_make_line(unit_price=Decimal("100"))]
        with pytest.raises(ValueError, match="vat_rate_id is required"):
            compute_pricing(
                lines,
                tax_mode=InvoiceTaxMode.LINE,
                amounts_include_vat=False,
                discount=DiscountInput(),
                treatment_effect=VatTreatmentEffect.APPLY_RATE,
                vat_rates=_VAT_RATES,
                document_vat_rate=None,
            )

    def test_unknown_vat_rate_id_raises(self) -> None:
        """LINE mode: unknown vat_rate_id should raise."""
        lines = [_make_line(unit_price=Decimal("100"), vat_rate_id=uuid.uuid4())]
        with pytest.raises(ValueError, match="not found"):
            compute_pricing(
                lines,
                tax_mode=InvoiceTaxMode.LINE,
                amounts_include_vat=False,
                discount=DiscountInput(),
                treatment_effect=VatTreatmentEffect.APPLY_RATE,
                vat_rates=_VAT_RATES,
                document_vat_rate=None,
            )


# ---------------------------------------------------------------------------
# compute_pricing – LINE mode, incl VAT (reverse)
# ---------------------------------------------------------------------------


class TestComputePricingLineInclVAT:

    def test_single_line_21pct_reverse(self) -> None:
        """1 line: 1 × €121 (incl 21%), reverse → €100 excl + €21 VAT.
        gross = 121.00; net = quantize(121.00/1.21) = quantize(100.0000) = 100.00;
        vat = 121.00 - 100.00 = 21.00.
        """
        lines = [_make_line(unit_price=Decimal("121"), vat_rate_id=_RATE_ID_21)]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=True,
            discount=DiscountInput(),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        assert result.total_incl_vat == Decimal("121.00")
        assert result.taxable_amount == Decimal("100.00")
        assert result.vat_total == Decimal("21.00")
        assert result.lines[0].subtotal_incl_vat == Decimal("121.00")
        assert result.lines[0].subtotal_excl_vat == Decimal("100.00")

    def test_incl_vat_with_line_discount(self) -> None:
        """1 line: €121 incl, 10% line disc, 21% VAT → after disc = 108.90.
        gross = quantize(121.00) = 121.00; disc = 12.10; after_disc = 108.90.
        net = quantize(108.90/1.21) = quantize(90.0000) = 90.00; vat = 18.90.
        """
        lines = [_make_line(
            unit_price=Decimal("121"),
            discount=DiscountInput(type=DiscountType.PERCENTAGE, value=Decimal("10")),
            vat_rate_id=_RATE_ID_21,
        )]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=True,
            discount=DiscountInput(),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        # Line disc: 12.10, after: 108.90
        assert result.line_discount_total == Decimal("12.10")
        # Reverse: 108.90 / 1.21 = 90.000 → 90.00
        assert result.taxable_amount == Decimal("90.00")
        assert result.vat_total == Decimal("18.90")
        assert result.total_incl_vat == Decimal("108.90")

    def test_incl_vat_with_doc_discount(self) -> None:
        """2 lines of €121 each, 10% doc disc, 21% VAT.
        Each line subtotal = 121.00; total_after_line = 242.00.
        doc_disc = 24.20; each line share = 12.10; line gross_after_disc = 108.90.
        net per line = 90.00; vat per line = 18.90; total vat = 37.80.
        """
        lines = [
            _make_line(name="A", unit_price=Decimal("121"), vat_rate_id=_RATE_ID_21),
            _make_line(name="B", unit_price=Decimal("121"), vat_rate_id=_RATE_ID_21),
        ]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=True,
            discount=DiscountInput(type=DiscountType.PERCENTAGE, value=Decimal("10")),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        # Total incl: 242, doc disc: 24.20, after: 217.80
        assert result.document_discount_amount == Decimal("24.20")
        assert result.total_incl_vat == Decimal("217.80")
        # Reverse: each line 108.90/1.21 = 90.00; total net = 180.00
        assert result.taxable_amount == Decimal("180.00")
        assert result.vat_total == Decimal("37.80")


# ---------------------------------------------------------------------------
# compute_pricing – DOCUMENT mode
# ---------------------------------------------------------------------------


class TestComputePricingDocumentMode:

    def test_single_rate_excl(self) -> None:
        """2 lines, DOCUMENT mode, 21% rate, excl VAT.
        subtotal = 100+200=300.00; vat = 300.00×21/100=63.00; total=363.00.
        """
        lines = [
            _make_line(name="A", unit_price=Decimal("100")),
            _make_line(name="B", unit_price=Decimal("200")),
        ]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.DOCUMENT,
            amounts_include_vat=False,
            discount=DiscountInput(),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=("21%", _RATE_21),
        )
        assert result.subtotal_excl_vat == Decimal("300.00")
        assert result.taxable_amount == Decimal("300.00")
        assert result.vat_total == Decimal("63.00")  # 300 × 21%
        assert result.total_incl_vat == Decimal("363.00")
        assert len(result.document_taxes) == 1
        assert len(result.line_taxes) == 0

    def test_document_mode_with_discount(self) -> None:
        """DOCUMENT mode, €10 doc disc on €300, 21%.
        taxable=290.00, vat=290.00×21/100=60.90.
        """
        lines = [
            _make_line(name="A", unit_price=Decimal("100")),
            _make_line(name="B", unit_price=Decimal("200")),
        ]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.DOCUMENT,
            amounts_include_vat=False,
            discount=DiscountInput(type=DiscountType.FIXED, value=Decimal("10")),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=("21%", _RATE_21),
        )
        assert result.document_discount_amount == Decimal("10.00")
        assert result.taxable_amount == Decimal("290.00")
        assert result.vat_total == Decimal("60.90")  # 290 × 21%

    def test_document_mode_incl_vat(self) -> None:
        """DOCUMENT mode, incl VAT input, 21% → reverse.
        total_incl = 121+242=363.00; net = 363.00/1.21=300.00; vat=63.00.
        """
        lines = [
            _make_line(name="A", unit_price=Decimal("121")),
            _make_line(name="B", unit_price=Decimal("242")),
        ]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.DOCUMENT,
            amounts_include_vat=True,
            discount=DiscountInput(),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=("21%", _RATE_21),
        )
        # Total incl: 363, reverse: 363 / 1.21 = 300
        assert result.total_incl_vat == Decimal("363.00")
        assert result.taxable_amount == Decimal("300.00")
        assert result.vat_total == Decimal("63.00")

    def test_document_mode_missing_rate_raises(self) -> None:
        """DOCUMENT mode requires document_vat_rate."""
        lines = [_make_line(unit_price=Decimal("100"))]
        with pytest.raises(ValueError, match="document_vat_rate_id is required"):
            compute_pricing(
                lines,
                tax_mode=InvoiceTaxMode.DOCUMENT,
                amounts_include_vat=False,
                discount=DiscountInput(),
                treatment_effect=VatTreatmentEffect.APPLY_RATE,
                vat_rates=_VAT_RATES,
                document_vat_rate=None,
            )

    def test_document_mode_100pct_discount_returns_tax_entry(self) -> None:
        """100% document discount → taxable=0, VAT=0, but tax entry still present."""
        lines = [_make_line(unit_price=Decimal("100"))]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.DOCUMENT,
            amounts_include_vat=False,
            discount=DiscountInput(type=DiscountType.PERCENTAGE, value=Decimal("100")),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=("21%", _RATE_21),
        )
        assert len(result.document_taxes) == 1
        assert result.document_taxes[0].taxable_amount == Decimal("0.00")
        assert result.document_taxes[0].tax_amount == Decimal("0.00")
        assert result.document_taxes[0].vat_rate_percent == _RATE_21
        assert result.document_taxes[0].effective_vat_percent == _RATE_21


# ---------------------------------------------------------------------------
# compute_pricing – VAT treatment effects
# ---------------------------------------------------------------------------


class TestVatTreatmentEffects:

    def test_zero_reverse_effect(self) -> None:
        """ZERO_REVERSE: effective rate = 0, VAT = 0."""
        lines = [_make_line(unit_price=Decimal("100"), vat_rate_id=_RATE_ID_21)]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=False,
            discount=DiscountInput(),
            treatment_effect=VatTreatmentEffect.ZERO_REVERSE,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        assert result.vat_total == Decimal("0")
        assert result.total_incl_vat == Decimal("100.00")
        assert result.taxable_amount == Decimal("100.00")
        # Line tax still records the original rate for reporting
        assert result.line_taxes[0].vat_rate_percent == _RATE_21
        assert result.line_taxes[0].effective_vat_percent == Decimal("0")

    def test_exempt_effect(self) -> None:
        lines = [_make_line(unit_price=Decimal("100"), vat_rate_id=_RATE_ID_21)]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=False,
            discount=DiscountInput(),
            treatment_effect=VatTreatmentEffect.EXEMPT,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        assert result.vat_total == Decimal("0")
        assert result.total_incl_vat == Decimal("100.00")

    def test_zero_export_effect(self) -> None:
        lines = [_make_line(unit_price=Decimal("100"), vat_rate_id=_RATE_ID_21)]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=False,
            discount=DiscountInput(),
            treatment_effect=VatTreatmentEffect.ZERO_EXPORT,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        assert result.vat_total == Decimal("0")

    def test_zero_treatment_incl_vat(self) -> None:
        """Incl VAT + zero treatment: no reverse, total = taxable."""
        lines = [_make_line(unit_price=Decimal("100"), vat_rate_id=_RATE_ID_21)]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=True,
            discount=DiscountInput(),
            treatment_effect=VatTreatmentEffect.ZERO_REVERSE,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        assert result.taxable_amount == Decimal("100.00")
        assert result.vat_total == Decimal("0")
        assert result.total_incl_vat == Decimal("100.00")
        assert result.lines[0].subtotal_excl_vat == Decimal("100.00")

    def test_document_mode_zero_treatment(self) -> None:
        """DOCUMENT mode + zero treatment: VAT = 0 at doc level."""
        lines = [_make_line(unit_price=Decimal("100"))]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.DOCUMENT,
            amounts_include_vat=False,
            discount=DiscountInput(),
            treatment_effect=VatTreatmentEffect.ZERO_REVERSE,
            vat_rates=_VAT_RATES,
            document_vat_rate=("21%", _RATE_21),
        )
        assert result.vat_total == Decimal("0")
        assert result.total_incl_vat == Decimal("100.00")


# ---------------------------------------------------------------------------
# Rounding edge cases
# ---------------------------------------------------------------------------


class TestRoundingEdgeCases:

    def test_quantity_fractional(self) -> None:
        """1.5 × €66.667 = 100.0005 → quantize_to_minor_unit → 100.00 (ROUND_HALF_UP ties)."""
        lines = [
            _make_line(
                quantity=Decimal("1.5"),
                unit_price=Decimal("66.667"),
                vat_rate_id=_RATE_ID_21,
            )
        ]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=False,
            discount=DiscountInput(),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        # 1.5 × 66.667 = 100.0005 → quantize_to_minor_unit (ROUND_HALF_UP) → 100.00
        # (0.0005 rounds up at 4th decimal, but we're rounding at 2nd: 100.0005 → 100.00)
        # Actually: 100.0005 at 2 dp → 100.00 (the digit after 2nd dp is 0, so no round-up)
        # Wait: 100.0005 → looking at 3rd decimal = 0, so 2nd dp stays 0 → 100.00
        assert result.subtotal_excl_vat == Decimal("100.00")
        expected_vat = quantize_to_minor_unit(
            Decimal("100.00") * Decimal("21") / Decimal("100")
        )
        assert result.vat_total == expected_vat

    def test_small_amounts_rounding(self) -> None:
        """0.001 × €0.001 = 0.000001 → quantized to 0.00."""
        lines = [
            _make_line(
                quantity=Decimal("0.001"),
                unit_price=Decimal("0.001"),
                vat_rate_id=_RATE_ID_21,
            )
        ]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=False,
            discount=DiscountInput(),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        assert result.subtotal_excl_vat == Decimal("0.00")
        assert result.vat_total == Decimal("0.00")

    def test_many_lines_sum_consistency(self) -> None:
        """100 lines of €1.009 each. Each line subtotal quantised to 1.01.
        Each line VAT: quantize_to_minor_unit(1.01 × 21/100) = quantize(0.2121) = 0.21.
        Sum of 100 lines subtotal: 101.00; vat sum: 21.00.
        """
        lines = [_make_line(unit_price=Decimal("1.009"), vat_rate_id=_RATE_ID_21)] * 100
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=False,
            discount=DiscountInput(),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        # Each line: 1 × 1.009 = 1.009 → quantize to 2dp → 1.01
        # 100 lines: 101.00
        assert result.subtotal_excl_vat == Decimal("101.00")
        assert result.taxable_amount == Decimal("101.00")
        # Each line VAT: quantize_to_minor_unit(1.01 × 21/100) = quantize(0.2121) = 0.21
        per_line_vat = quantize_to_minor_unit(Decimal("1.01") * Decimal("21") / Decimal("100"))
        assert result.vat_total == per_line_vat * 100

    def test_doc_discount_distribution_remainder(self) -> None:
        """5 lines of €1 each, €1 doc discount → each gets 0.20."""
        lines = [
            _make_line(name=f"Item {i}", unit_price=Decimal("1"), vat_rate_id=_RATE_ID_21)
            for i in range(5)
        ]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=False,
            discount=DiscountInput(type=DiscountType.FIXED, value=Decimal("1")),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        assert result.document_discount_amount == Decimal("1.00")
        assert result.taxable_amount == Decimal("4.00")

    def test_incl_vat_odd_number_reverse(self) -> None:
        """€100 incl at 9% → net = quantize(100/1.09), VAT = 100 - net.
        100/1.09 = 91.743119... → 91.74; vat = 8.26.
        """
        lines = [_make_line(unit_price=Decimal("100"), vat_rate_id=_RATE_ID_9)]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=True,
            discount=DiscountInput(),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        excl = quantize_to_minor_unit(Decimal("100") / (1 + Decimal("9") / Decimal("100")))
        vat = Decimal("100") - excl
        assert result.taxable_amount == excl
        assert result.vat_total == vat
        assert result.total_incl_vat == Decimal("100.00")

    def test_tiny_doc_discount_multi_rate_no_negative_shares(self) -> None:
        """10 lines (alternating 21% / 9%), €0.05 fixed doc discount.

        Ensures no line gets a negative document_discount_share and each
        line's taxable_amount stays non-negative.
        """
        lines = [
            _make_line(
                name=f"Item {i}",
                unit_price=Decimal("100"),
                vat_rate_id=_RATE_ID_21 if i % 2 == 0 else _RATE_ID_9,
            )
            for i in range(10)
        ]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=False,
            discount=DiscountInput(type=DiscountType.FIXED, value=Decimal("0.05")),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        assert result.document_discount_amount == Decimal("0.05")
        for line in result.lines:
            assert line.document_discount_share >= Decimal("0")
            assert line.taxable_amount >= Decimal("0")


# ---------------------------------------------------------------------------
# M7.5 不变量测试 (D1：行级三级自洽)
# ---------------------------------------------------------------------------


class TestMinorUnitInvariants:

    def test_f2026_009_shape_line_level_amounts(self) -> None:
        """F2026-009 形态：unit_price=3194.352, qty=1, 21%, 不含税录入。
        手算：
        - 行小计 = quantize_to_minor_unit(1 × 3194.352) = quantize(3194.352) = 3194.35
          (第3位为2 < 5，舍去)
        - net = 3194.35 (无折扣)
        - VAT = quantize_to_minor_unit(3194.35 × 21/100) = quantize(670.8135) = 670.81
          (第3位为3 < 5，舍去)
        - total = 3194.35 + 670.81 = 3865.16
        """
        lines = [
            _make_line(
                unit_price=Decimal("3194.352"),
                quantity=Decimal("1"),
                vat_rate_id=_RATE_ID_21,
            )
        ]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=False,
            discount=DiscountInput(),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        line = result.lines[0]
        assert result.taxable_amount == Decimal("3194.35"), "net should be 3194.35"
        assert result.vat_total == Decimal("670.81"), "vat should be 670.81"
        assert result.total_incl_vat == Decimal("3865.16"), "total should be 3865.16"
        # 行级不变量：net + VAT = total
        assert line.taxable_amount + result.vat_total == result.total_incl_vat

    def test_line_net_plus_vat_equals_total_invariant(self) -> None:
        """每行不变量：net + VAT = total（分级）。"""
        lines = [
            _make_line(
                unit_price=Decimal("3194.352"), quantity=Decimal("1"), vat_rate_id=_RATE_ID_21
            ),
            _make_line(
                unit_price=Decimal("55.555"), quantity=Decimal("3"), vat_rate_id=_RATE_ID_21
            ),
        ]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=False,
            discount=DiscountInput(),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        for i, line in enumerate(result.lines):
            # 每行 net + vat = total
            net, vat, total = line.taxable_amount, line.vat_total, line.total_incl_vat
            assert net + vat == total, (
                f"Line {i}: net ({net}) + vat ({vat}) != total ({total})"
            )
        # 单据级
        assert result.taxable_amount + result.vat_total == result.total_incl_vat

    def test_multi_line_same_rate_no_group_allocation(self) -> None:
        """多行同率：各行各自到分后相加，不做组级分摊。
        2 lines @ 21%: unit_price=100.005, qty=1 each.
        行小计 = quantize(100.005) = 100.01 each (5 rounds up at 3rd decimal, but here
        we're going to 2dp: 100.005 → 3rd digit=0, wait:
        100.005 → looking at 3rd decimal place (0.001s): digit is 0... no.
        Actually: 100.005 at 2dp → look at digit at position 3 (thousandths) = 5 → rounds up.
        So: 100.005 → 100.01 (ROUND_HALF_UP: .005 rounds .00 up to .01).

        Wait: 100.005 is 100 + 0.005.
        At 2dp (hundredths), we look at the thousandths digit: 5 → rounds up.
        So 100.005 → 100.01.

        vat each: quantize(100.01 × 21/100) = quantize(21.0021) = 21.00.
        total each: 100.01 + 21.00 = 121.01.
        sum: subtotal=200.02, vat=42.00, total=242.02.
        """
        lines = [
            _make_line(
                unit_price=Decimal("100.005"), quantity=Decimal("1"), vat_rate_id=_RATE_ID_21
            ),
            _make_line(
                unit_price=Decimal("100.005"), quantity=Decimal("1"), vat_rate_id=_RATE_ID_21
            ),
        ]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=False,
            discount=DiscountInput(),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        # Each line: subtotal=100.01, vat=21.00, total=121.01
        for line in result.lines:
            assert line.taxable_amount == Decimal("100.01")
            assert line.vat_total == Decimal("21.00")
            assert line.total_incl_vat == Decimal("121.01")
        # 不变量: taxable+vat=total 在单据级
        assert result.taxable_amount + result.vat_total == result.total_incl_vat

    def test_multi_rate_group_and_document_self_consistency(self) -> None:
        """多税率：21% + 9% 行，每组自洽，Σ组 = 单据。
        Line A: unit_price=100.00, 21%; net=100.00, vat=21.00, total=121.00.
        Line B: unit_price=100.00, 9%; net=100.00, vat=9.00, total=109.00.
        Document: taxable=200.00, vat=30.00, total=230.00.
        """
        lines = [
            _make_line(name="A", unit_price=Decimal("100"), vat_rate_id=_RATE_ID_21),
            _make_line(name="B", unit_price=Decimal("100"), vat_rate_id=_RATE_ID_9),
        ]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=False,
            discount=DiscountInput(),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        line_a = result.lines[0]
        line_b = result.lines[1]
        # 每行不变量
        assert line_a.taxable_amount + line_a.vat_total == line_a.total_incl_vat
        assert line_b.taxable_amount + line_b.vat_total == line_b.total_incl_vat
        # 单据级不变量
        assert result.taxable_amount + result.vat_total == result.total_incl_vat
        # Σ行 = 单据
        total_net = sum(ln.taxable_amount for ln in result.lines)
        total_vat = sum(ln.vat_total for ln in result.lines)
        total_gross = sum(ln.total_incl_vat for ln in result.lines)
        assert total_net == result.taxable_amount
        assert total_vat == result.vat_total
        assert total_gross == result.total_incl_vat

    def test_incl_vat_input_gross_anchor_invariant(self) -> None:
        """含税录入方向：gross 为锚到分，net/VAT 派生且 net+VAT=gross。
        unit_price=121.005 (incl 21%).
        gross = quantize(121.005) = 121.01 (5 at thousandths → rounds up).
        net = quantize(121.01/1.21) = quantize(100.00826...) = 100.01.
        vat = 121.01 - 100.01 = 21.00.
        net+vat = 121.01 = gross ✓.
        """
        lines = [
            _make_line(
                unit_price=Decimal("121.005"), quantity=Decimal("1"), vat_rate_id=_RATE_ID_21
            )
        ]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=True,
            discount=DiscountInput(),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        gross = result.total_incl_vat
        net = result.taxable_amount
        vat = result.vat_total
        # gross = 121.005 → 121.01
        assert gross == Decimal("121.01")
        # net + vat = gross (no rounding error)
        assert net + vat == gross

    def test_fixed_doc_discount_distribution_sum_invariant(self) -> None:
        """整单 fixed 折扣分摊到分且 Σ分摊 = 折扣额。
        3 lines: 300/200/100, €30 fixed discount.
        Shares (proportional, 2dp): 15.00, 10.00, 5.00. Sum=30.00.
        """
        lines = [
            _make_line(name="A", unit_price=Decimal("300"), vat_rate_id=_RATE_ID_21),
            _make_line(name="B", unit_price=Decimal("200"), vat_rate_id=_RATE_ID_21),
            _make_line(name="C", unit_price=Decimal("100"), vat_rate_id=_RATE_ID_21),
        ]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=False,
            discount=DiscountInput(type=DiscountType.FIXED, value=Decimal("30")),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        doc_disc = result.document_discount_amount
        shares_sum = sum(line.document_discount_share for line in result.lines)
        # Σ分摊 = 折扣额
        assert shares_sum == doc_disc
        # 所有分摊都是分值（2dp）
        for line in result.lines:
            share = line.document_discount_share
            assert share == quantize_to_minor_unit(share)

    def test_all_amounts_are_minor_unit_precision(self) -> None:
        """验证所有行级和单据级金额都在分级精度（最多2位小数）。"""
        lines = [
            _make_line(
                unit_price=Decimal("3194.352"), quantity=Decimal("1"), vat_rate_id=_RATE_ID_21
            ),
            _make_line(
                unit_price=Decimal("55.333"), quantity=Decimal("2"), vat_rate_id=_RATE_ID_9
            ),
        ]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=False,
            discount=DiscountInput(type=DiscountType.FIXED, value=Decimal("10")),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates=_VAT_RATES,
            document_vat_rate=None,
        )
        # 单据级
        for val in [result.taxable_amount, result.vat_total, result.total_incl_vat,
                    result.line_discount_total, result.document_discount_amount]:
            assert val == quantize_to_minor_unit(val), f"{val} is not at 2dp precision"
        # 行级
        for line in result.lines:
            for val in [line.taxable_amount, line.vat_total, line.total_incl_vat,
                        line.line_discount_amount, line.document_discount_share]:
                assert val == quantize_to_minor_unit(val), f"Line amount {val} not at 2dp"

    def test_revision_frozen_rate_overrides_allow_same_id_with_distinct_snapshots(self) -> None:
        lines = [
            _make_line(unit_price=Decimal("100"), vat_rate_id=_RATE_ID_21),
            _make_line(unit_price=Decimal("100"), vat_rate_id=_RATE_ID_21),
        ]
        result = compute_pricing(
            lines,
            tax_mode=InvoiceTaxMode.LINE,
            amounts_include_vat=False,
            discount=DiscountInput(),
            treatment_effect=VatTreatmentEffect.APPLY_RATE,
            vat_rates={_RATE_ID_21: ("Current", Decimal("99"))},
            document_vat_rate=None,
            line_rate_overrides=[("Frozen A", Decimal("21")), ("Frozen B", Decimal("9"))],
        )
        assert [tax.vat_rate_label for tax in result.line_taxes] == ["Frozen A", "Frozen B"]
        assert [tax.vat_rate_percent for tax in result.line_taxes] == [Decimal("21"), Decimal("9")]
        assert result.vat_total == Decimal("30.00")

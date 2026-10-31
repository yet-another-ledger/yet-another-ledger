"""Pure calculation checks for M16 battery annual private-use VAT."""

from decimal import Decimal

import pytest

from jai.services.battery import compute_battery_adjustment


@pytest.mark.parametrize(
    ("net", "rate", "private", "expected_base", "expected_vat"),
    [
        ("10000", "21", "45", "900.00", "189.00"),
        ("10000", "21", "0", "0.00", "0.00"),
        ("10000", "21", "100", "2000.00", "420.00"),
        ("1", "21", "45", "0.09", "0.02"),
    ],
)
def test_battery_adjustment_rounds_base_and_vat_after_unrounded_calculation(
    net: str, rate: str, private: str, expected_base: str, expected_vat: str
) -> None:
    assert compute_battery_adjustment(Decimal(net), Decimal(rate), Decimal(private)) == (
        Decimal(expected_base),
        Decimal(expected_vat),
    )


def test_battery_vat_uses_unrounded_base_before_cent_rounding() -> None:
    base, vat = compute_battery_adjustment(Decimal("0.041"), Decimal("21"), Decimal("100"))
    assert base == Decimal("0.01")
    # Unrounded 0.0082 * 21% = 0.001722, rounded to €0.00.
    assert vat == Decimal("0.00")


def test_annual_assets_round_individually_before_aggregation() -> None:
    per_asset = [
        compute_battery_adjustment(Decimal("0.025"), Decimal("21"), Decimal("100"))
        for _ in range(2)
    ]
    assert tuple(sum(values, Decimal("0.00")) for values in zip(*per_asset, strict=True)) == (
        Decimal("0.02"),
        Decimal("0.00"),
    )
    combined_base = compute_battery_adjustment(
        Decimal("0.05"), Decimal("21"), Decimal("100")
    )[0]
    assert combined_base == Decimal("0.01")

"""Closed-patch contract checks for the SI2 revision preview."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from jai.models._enums import DiscountType, InvoiceTaxMode
from jai.schemas.invoice_revision import (
    InvoiceRevisionCalculateRequest,
    InvoiceRevisionChanges,
    InvoiceRevisionCreate,
)


def test_revision_patch_distinguishes_omission_from_nullable_clears() -> None:
    omitted = InvoiceRevisionChanges.model_validate({})
    clear = InvoiceRevisionChanges.model_validate(
        {"due_date": None, "reference_number": None, "document_vat_rate_id": None}
    )
    assert omitted.model_fields_set == set()
    assert clear.model_fields_set == {"due_date", "reference_number", "document_vat_rate_id"}


@pytest.mark.parametrize(
    "payload",
    [
        {"customer_id": str(uuid.uuid4())},
        {"status": "DRAFT"},
        {"total_incl_vat": "0"},
        {"invoice_date": None},
        {"tax_mode": None},
    ],
)
def test_revision_patch_rejects_protected_or_invalid_fields(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        InvoiceRevisionChanges.model_validate(payload)


@pytest.mark.parametrize(
    "line",
    [
        {"name": "Service", "quantity": "1", "unit_price": "2", "client_total": "2"},
        {
            "name": "Service",
            "quantity": "1",
            "unit_price": "2",
            "discount": {"type": DiscountType.NONE.value, "value": "0", "calculated": "0"},
        },
    ],
)
def test_revision_line_and_discount_are_closed(line: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        InvoiceRevisionChanges.model_validate({"lines": [line]})


def test_calculate_request_keeps_expected_version_and_exact_decimal_inputs() -> None:
    updated = datetime(2026, 10, 26, tzinfo=UTC)
    request = InvoiceRevisionCalculateRequest.model_validate(
        {
            "expected_updated_at": updated.isoformat(),
            "changes": {
                "invoice_date": date(2026, 10, 1).isoformat(),
                "tax_mode": InvoiceTaxMode.LINE.value,
                "lines": [
                    {
                        "line_id": str(uuid.uuid4()),
                        "name": "Precise",
                        "quantity": "1.250",
                        "unit_price": "1.2345",
                        "discount": {"type": DiscountType.NONE.value, "value": "0"},
                    }
                ],
            },
        }
    )
    assert request.expected_updated_at == updated
    assert request.changes.lines is not None
    assert request.changes.lines[0].quantity == Decimal("1.250")
    assert request.changes.lines[0].unit_price == Decimal("1.2345")


@pytest.mark.parametrize("confirmation", [False, 1, "true"])
def test_revision_save_requires_explicit_boolean_confirmation(confirmation: object) -> None:
    with pytest.raises(ValidationError):
        InvoiceRevisionCreate.model_validate(
            {
                "expected_updated_at": datetime(2026, 10, 26, tzinfo=UTC).isoformat(),
                "changes": {},
                "reason": "Valid reason",
                "confirm_not_delivered": confirmation,
            }
        )


@pytest.mark.parametrize("reason", ["", "  \n "])
def test_revision_save_requires_non_whitespace_reason(reason: str) -> None:
    with pytest.raises(ValidationError):
        InvoiceRevisionCreate.model_validate(
            {
                "expected_updated_at": datetime(2026, 10, 26, tzinfo=UTC).isoformat(),
                "changes": {},
                "reason": reason,
                "confirm_not_delivered": True,
            }
        )

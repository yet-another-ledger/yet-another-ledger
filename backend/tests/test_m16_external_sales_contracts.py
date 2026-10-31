"""Contract and draft-boundary checks for M16 Step 2."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from jai.main import app
from jai.schemas.external_sales import ExternalSalesInput, ExternalSalesTaxInput
from jai.schemas.setting import VatRateTiers
from jai.services import external_sales


def test_external_sales_openapi_exposes_only_safe_file_metadata() -> None:
    schema = app.openapi()
    paths = schema["paths"]
    expected = {
        ("/api/v1/external-sales-sources", "get"),
        ("/api/v1/external-sales-sources", "post"),
        ("/api/v1/external-sales-sources/{source_id}", "put"),
        ("/api/v1/external-sales", "get"),
        ("/api/v1/external-sales", "post"),
        ("/api/v1/external-sales/{sale_id}", "get"),
        ("/api/v1/external-sales/{sale_id}", "put"),
        ("/api/v1/external-sales/{sale_id}", "delete"),
        ("/api/v1/external-sales/{sale_id}/files", "post"),
        ("/api/v1/external-sales/{sale_id}/files/{file_id}", "get"),
        ("/api/v1/external-sales/{sale_id}/files/{file_id}", "delete"),
        ("/api/v1/external-sales/{sale_id}/calculate", "post"),
        ("/api/v1/external-sales/{sale_id}/confirm", "post"),
    }
    assert expected <= {(path, method) for path, item in paths.items() for method in item}
    props = schema["components"]["schemas"]["ExternalSalesFileRead"]["properties"]
    assert "storage_key" not in props
    assert "sha256" in props and "duplicate_warning" in props
    assert "treatment_id" in schema["components"]["schemas"]["ExternalSalesTaxInput"]["properties"]
    assert "rate_id" in schema["components"]["schemas"]["ExternalSalesTaxInput"]["properties"]
    assert (
        "expected_version"
        in schema["components"]["schemas"]["ExternalSalesCalculateRequest"]["properties"]
    )
    calculation = schema["components"]["schemas"]["ExternalSalesCalculation"]["properties"]
    assert {"mismatch", "mismatch_row_indices", "original_vat_total"} <= set(calculation)
    conflict = schema["components"]["schemas"]["ExternalSalesConflictDetail"]["properties"]
    assert {"code", "external_sale_id", "current_version"} <= set(conflict)


def test_pending_contract_allows_incomplete_entries_and_preserves_original_number() -> None:
    original = "  EXT-2026/17  "
    body = ExternalSalesInput.model_validate(
        {"original_number": original, "tax_summaries": [{"net_amount": "12.345"}]}
    )
    assert body.original_number == original
    assert body.source_id is None
    assert body.document_date is None
    assert body.tax_summaries[0].net_amount == Decimal("12.345")


def test_tax_summary_accepts_typed_dictionary_ids_and_rejects_extra_authority_fields() -> None:
    treatment_id, rate_id = uuid.uuid4(), uuid.uuid4()
    row = ExternalSalesTaxInput.model_validate(
        {"treatment_id": str(treatment_id), "rate_id": str(rate_id), "vat_amount": "2.10"}
    )
    assert row.treatment_id == treatment_id
    assert row.rate_id == rate_id
    with pytest.raises(ValidationError):
        ExternalSalesTaxInput.model_validate({"rate_percent": "21", "net_amount": "10"})


def test_posting_calculation_rounds_each_rate_row_and_preserves_original_vat() -> None:
    """Opposing row differences remain visible even when their totals cancel."""
    sale = SimpleNamespace(
        id=uuid.uuid4(),
        status="PENDING",
        version=7,
        source_id=uuid.uuid4(),
        source=object(),
        original_number="EXT-7",
        document_date=date(2026, 10, 1),
        include_in_pl=True,
        covered_from=None,
        covered_to=None,
        files=[object()],
        tax_summaries=[
            SimpleNamespace(
                treatment_code="NL_DOMESTIC",
                rate_percent=Decimal("10"),
                net_amount=Decimal("10.005"),
                vat_amount=Decimal("1.01"),
            ),
            SimpleNamespace(
                treatment_code="NL_DOMESTIC",
                rate_percent=Decimal("5"),
                net_amount=Decimal("20.00"),
                vat_amount=Decimal("0.99"),
            ),
        ],
    )

    preview = external_sales._calculate_locked(sale, expected_version=7)

    assert preview.original_net_total == Decimal("30.01")
    assert preview.original_vat_total == Decimal("2.00")
    assert preview.derived_vat_total == Decimal("2.00")
    assert preview.mismatch is True
    assert preview.mismatch_row_indices == [0, 1]
    assert len(preview.warnings) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["preflight", "append"])
async def test_external_sales_upload_state_guard_precedes_file_validation(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    # Step 2's database permits only PENDING; exercise the service guard without
    # adding the later posting schema or bypassing current database constraints.
    session = AsyncMock()
    monkeypatch.setattr(external_sales, "set_rls_company", AsyncMock())
    monkeypatch.setattr(
        external_sales,
        "_owned_sale",
        AsyncMock(
            return_value=SimpleNamespace(
                status="POSTED", files=[], id=uuid.uuid4(), version=2
            )
        ),
    )
    normalize = Mock(side_effect=AssertionError("Do not validate files in a forbidden state"))
    monkeypatch.setattr(external_sales, "_normalize_uploads", normalize)
    company_id, sale_id, creator_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with pytest.raises(external_sales.ExternalSalesConflict, match="EXTERNAL_SALE_NOT_PENDING"):
        if command == "preflight":
            await external_sales.check_append_capacity(session, company_id, sale_id, 6)
        else:
            await external_sales.append_files(
                session,
                company_id,
                creator_id,
                sale_id,
                [(b"bad", "text/plain", None)],
                64,
                Mock(),
            )
    normalize.assert_not_called()


@pytest.mark.parametrize(
    ("rate", "vat", "zero_tier", "mismatch", "invalid"),
    [
        ("0", "0", 0, False, False),
        ("0", "0.01", 0, True, True),
        ("10", "1.25", 0, True, False),
        ("3", "3.00", 3, False, True),
        ("21", "2.10", 21, False, False),
    ],
)
def test_posting_zero_rate_requires_zero_original_vat(
    rate: str, vat: str, zero_tier: int, mismatch: bool, invalid: bool
) -> None:
    sale = SimpleNamespace(
        id=uuid.uuid4(),
        status="PENDING",
        version=1,
        source_id=uuid.uuid4(),
        source=object(),
        original_number="ZERO-CHECK",
        document_date=date(2026, 3, 31),
        include_in_pl=True,
        covered_from=None,
        covered_to=None,
        files=[object()],
        tax_summaries=[
            SimpleNamespace(
                treatment_code="NL_DOMESTIC",
                rate_percent=Decimal(rate),
                net_amount=Decimal("10.00"),
                vat_amount=Decimal(vat),
            )
        ],
    )
    if invalid:
        with pytest.raises(external_sales.ReceiptValidationError, match="zero VAT rate"):
            external_sales._calculate_locked(sale, 1, VatRateTiers(zero=zero_tier))
    else:
        calculation = external_sales._calculate_locked(sale, 1, VatRateTiers(zero=zero_tier))
        assert calculation.mismatch is mismatch
        assert calculation.original_vat_total == Decimal(vat)
    assert sale.tax_summaries[0].vat_amount == Decimal(vat)

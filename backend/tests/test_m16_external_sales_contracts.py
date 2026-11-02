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
from jai.services import ai as ai_service
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
        ("/api/v1/external-sales/{sale_id}/extract", "post"),
        ("/api/v1/external-sales/{sale_id}/calculate", "post"),
        ("/api/v1/external-sales/{sale_id}/confirm", "post"),
        ("/api/v1/external-sales/{sale_id}/revisions/preview", "post"),
        ("/api/v1/external-sales/{sale_id}/revisions", "post"),
        ("/api/v1/external-sales/{sale_id}/revisions", "get"),
        ("/api/v1/external-sales/{sale_id}/void", "post"),
    }
    assert expected <= {(path, method) for path, item in paths.items() for method in item}
    revision_history = paths["/api/v1/external-sales/{sale_id}/revisions"]["get"]
    assert {"offset", "limit"} <= {
        parameter["name"] for parameter in revision_history["parameters"]
    }
    assert (
        schema["components"]["schemas"]["ExternalSalesRevisionListRead"]["properties"].keys()
        >= {"items", "total", "offset", "limit"}
    )
    preview_responses = paths["/api/v1/external-sales/{sale_id}/revisions/preview"]["post"][
        "responses"
    ]
    assert "409" in preview_responses
    assert "ExternalSalesConflict" in str(preview_responses["409"])
    props = schema["components"]["schemas"]["ExternalSalesFileRead"]["properties"]
    assert "storage_key" not in props
    assert "sha256" in props and "duplicate_warning" in props
    sale_props = schema["components"]["schemas"]["ExternalSalesRead"]["properties"]
    assert {"ai_suggestion", "ai_state", "ai_error_code"} <= set(sale_props)
    suggestion_props = schema["components"]["schemas"]["ExternalSalesAISuggestion"]["properties"]
    assert {
        "observed_net_total",
        "observed_vat_total",
        "tax_summaries",
        "confidence",
        "review_notes",
    } <= set(suggestion_props)
    assert not {"treatment_id", "include_in_pl", "tax_eligible"} & set(suggestion_props)
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


def test_external_sales_revision_contract_requires_nonblank_reason_and_version() -> None:
    from jai.schemas.external_sales import ExternalSalesRevisionInput, ExternalSalesVoidInput

    with pytest.raises(ValidationError):
        ExternalSalesRevisionInput.model_validate({"expected_version": 1, "reason": "  "})
    with pytest.raises(ValidationError):
        ExternalSalesVoidInput.model_validate({"expected_version": 1, "reason": ""})
    with pytest.raises(ValidationError):
        ExternalSalesRevisionInput.model_validate({"expected_version": 0, "reason": "correct date"})


def test_external_credit_sign_is_applied_once_and_settlement_can_be_signed() -> None:
    with pytest.raises(external_sales.ReceiptValidationError, match="negative signed rows"):
        external_sales.validate_signed_external_amounts(
            "CREDIT", Decimal("-10.00"), Decimal("2.10")
        )
    external_sales.validate_signed_external_amounts("CREDIT", Decimal("-10.00"), Decimal("-2.10"))
    external_sales.validate_signed_external_amounts(
        "SETTLEMENT_ADJUSTMENT", Decimal("-3.25"), Decimal("-0.68")
    )
    with pytest.raises(external_sales.ReceiptValidationError, match="cannot be negative"):
        external_sales.validate_signed_external_amounts("SALE", Decimal("-10.00"), Decimal("-2.10"))


def test_external_sale_ai_suggestion_keeps_partial_multirate_observations_only() -> None:
    data = external_sales._safe_observed_suggestion(
        {
            "counterparty": "Energy Co",
            "observed_net_total": "10.125",
            "tax_summaries": [
                {"rate_percent": "21", "net_amount": "5", "vat_amount": "1.05"},
                {"rate_percent": "9", "net_amount": "5.125", "vat_amount": "0.46"},
            ],
            "review_notes": ["Mixed statement: verify exported electricity only."],
            "include_in_pl": True,
            "raw_provider_response": "must not persist",
        },
        truncated=True,
    )
    assert data["counterparty"] == "Energy Co"
    assert data["observed_net_total"] == "10.125"
    assert len(data["tax_summaries"]) == 2
    assert data["review_notes"][-1].startswith("PDF page limit reached")
    assert "include_in_pl" not in data and "raw_provider_response" not in data


def test_external_sale_ai_parser_rejects_empty_or_unusable_responses() -> None:
    with pytest.raises(ValueError):
        external_sales._safe_observed_suggestion(
            {"confidence": "high", "review_notes": ["Check"]}, False
        )
    partial = external_sales._safe_observed_suggestion(
        {"document_date": "malformed", "counterparty": "Export platform"}, False
    )
    assert partial["counterparty"] == "Export platform"
    assert "document_date" not in partial


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"document_date": "2026-10-30", "observed_net_total": "unknown"},
         {"document_date": "2026-10-30"}),
        ({"document_date": "2026-10-30", "observed_vat_total": "NaN"},
         {"document_date": "2026-10-30"}),
        ({"document_date": "invalid", "observed_net_total": "12.345"},
         {"observed_net_total": "12.345"}),
        ({"document_date": "2026-02-30", "observed_vat_total": "0"},
         {"observed_vat_total": "0"}),
        ({"document_date": "2026-10-30", "observed_net_total": {},
          "observed_vat_total": "123456789012345"}, {"document_date": "2026-10-30"}),
    ],
)
def test_external_sale_ai_keeps_independent_valid_header_observations(
    raw: dict[str, object], expected: dict[str, object]
) -> None:
    assert external_sales._safe_observed_suggestion(raw, False) == expected


def test_energy_statement_does_not_prefill_ambiguous_household_or_net_settlement_amounts() -> None:
    suggestion = external_sales._safe_observed_suggestion(
        {
            "counterparty": "Energy Co",
            "observed_net_total": "500.00",
            "observed_vat_total": "105.00",
            "tax_summaries": [{"rate_percent": "21", "net_amount": "500", "vat_amount": "105"}],
            "explicit_electricity_sale_component": False,
        },
        False,
        require_explicit_energy_sale=True,
    )
    assert suggestion["counterparty"] == "Energy Co"
    assert suggestion["explicit_electricity_sale_component"] is False
    assert "observed_net_total" not in suggestion
    assert "observed_vat_total" not in suggestion
    assert "tax_summaries" not in suggestion
    assert "No separately itemized" in suggestion["review_notes"][-1]


@pytest.mark.asyncio
async def test_external_sale_ai_prompt_is_energy_safe_and_parses_partial_provider_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jai.schemas.setting import AiSettings

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        ai_service,
        "_get_ai_config",
        AsyncMock(
            return_value=AiSettings(
                enabled=True, base_url="https://ai.invalid", api_key="x", model="m"
            )
        ),
    )
    monkeypatch.setattr(ai_service, "_attachment_to_images", lambda _mime, _raw: [b"png"])

    async def fake_call(**kwargs: object) -> str:
        captured.update(kwargs)
        return (
            '{"counterparty":"Energy Co",'
            '"explicit_electricity_sale_component":false,"raw":"private"}'
        )

    monkeypatch.setattr(ai_service, "_call_chat_completions", fake_call)
    parsed, truncated = await ai_service.extract_external_sales_from_files(
        AsyncMock(), uuid.uuid4(), [("image/png", b"receipt")], energy_source=True
    )
    prompt = str(captured["messages"])
    assert "fixed charges" in prompt and "net settlement/payable" in prompt
    assert parsed == {"counterparty": "Energy Co", "explicit_electricity_sale_component": False}
    assert truncated is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settings", "expected_code"),
    [
        ({"enabled": False, "api_key": "secret", "model": "model"}, "AI_DISABLED"),
        ({"enabled": True, "api_key": "", "model": "model"}, "AI_NOT_CONFIGURED"),
        ({"enabled": True, "api_key": "secret", "model": ""}, "AI_NOT_CONFIGURED"),
    ],
)
async def test_external_sale_ai_disabled_or_missing_config_stays_on_manual_path(
    monkeypatch: pytest.MonkeyPatch, settings: dict[str, object], expected_code: str
) -> None:
    from jai.schemas.setting import AiSettings

    monkeypatch.setattr(
        ai_service,
        "_get_ai_config",
        AsyncMock(return_value=AiSettings(**settings)),
    )
    with pytest.raises(ai_service.AIUnavailableError) as raised:
        await ai_service.extract_external_sales_from_files(
            AsyncMock(), uuid.uuid4(), [("image/png", b"original")]
        )
    assert raised.value.code == expected_code


def test_posting_calculation_rounds_each_rate_row_and_preserves_original_vat() -> None:
    """Opposing row differences remain visible even when their totals cancel."""
    sale = SimpleNamespace(
        id=uuid.uuid4(),
        status="PENDING",
        document_kind="SALE",
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
    # Invalid states are rejected before file validation; posted/voided rows may
    # append supporting evidence under Step 4.
    session = AsyncMock()
    monkeypatch.setattr(external_sales, "set_rls_company", AsyncMock())
    monkeypatch.setattr(
        external_sales,
        "_owned_sale",
        AsyncMock(
                return_value=SimpleNamespace(status="INVALID", files=[], id=uuid.uuid4(), version=2)
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
        document_kind="SALE",
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

def test_revision_tax_calculation_preserves_decimal_originals_and_signed_rounding() -> None:
    from jai.models.external_sales import ExternalSaleTax

    taxes = [
        ExternalSaleTax(
            treatment_code="NL_DOMESTIC",
            rate_percent=Decimal("10"),
            net_amount=Decimal("10.005"),
            vat_amount=Decimal("1.005"),
        ),
        ExternalSaleTax(
            treatment_code="NL_DOMESTIC",
            rate_percent=Decimal("5"),
            net_amount=Decimal("20.005"),
            vat_amount=Decimal("0.995"),
        ),
    ]
    net, vat, mismatch, warnings = external_sales._calculate_revision_taxes(
        taxes, "SALE", VatRateTiers()
    )
    assert net == Decimal("30.02") and vat == Decimal("2.01")
    assert mismatch and len(warnings) == 1
    taxes[0].net_amount, taxes[0].vat_amount = Decimal("-10.005"), Decimal("-1.005")
    net, vat, mismatch, _ = external_sales._calculate_revision_taxes(
        taxes[:1], "CREDIT", VatRateTiers()
    )
    assert net == Decimal("-10.01") and vat == Decimal("-1.01") and mismatch
    with pytest.raises(external_sales.ReceiptValidationError, match="negative signed"):
        external_sales._calculate_revision_taxes(taxes[1:], "CREDIT", VatRateTiers())
    taxes[0].rate_percent = Decimal("0")
    with pytest.raises(external_sales.ReceiptValidationError, match="zero VAT"):
        external_sales._calculate_revision_taxes(taxes[:1], "CREDIT", VatRateTiers())


def test_revision_null_pl_is_invalid_but_omission_is_a_patch() -> None:
    from jai.schemas.external_sales import ExternalSalesRevisionInput

    body = {"expected_version": 2, "reason": "Review P/L"}
    assert "include_in_pl" not in ExternalSalesRevisionInput.model_validate(body).model_fields_set
    for value in (True, False):
        assert (
            ExternalSalesRevisionInput.model_validate(
                {**body, "include_in_pl": value}
            ).include_in_pl
            is value
        )
    with pytest.raises(ValidationError):
        ExternalSalesRevisionInput.model_validate({**body, "include_in_pl": None})


def test_revision_tax_identity_preserves_reorder_and_rejects_ambiguous_or_foreign_rows() -> None:
    from jai.models.external_sales import ExternalSaleTax
    from jai.schemas.external_sales import ExternalSalesRevisionTaxInput

    treatment_id, rate_id = uuid.uuid4(), uuid.uuid4()
    a, b = [
        ExternalSaleTax(
            id=uuid.uuid4(),
            treatment_id=treatment_id,
            rate_id=rate_id,
            treatment_code="NL_DOMESTIC",
            treatment_label="Original",
            rate_label="Frozen",
            rate_percent=Decimal(percent),
            net_amount=Decimal("100.00"),
            vat_amount=Decimal("21.00"),
        )
        for percent in ("21", "9")
    ]
    raw = dict(treatment_id=treatment_id, rate_id=rate_id, net_amount="100", vat_amount="21")

    def item(**extra: object) -> ExternalSalesRevisionTaxInput:
        return ExternalSalesRevisionTaxInput.model_validate({**raw, **extra})

    assert external_sales._match_revision_taxes(
        [a, b], [item(tax_summary_id=b.id), item(tax_summary_id=a.id)]
    ) == [b, a]
    for inputs in (
        [item()],
        [item(tax_summary_id=uuid.uuid4())],
        [item(tax_summary_id=a.id), item(tax_summary_id=a.id)],
    ):
        with pytest.raises(external_sales.ReceiptValidationError):
            external_sales._match_revision_taxes([a, b], inputs)

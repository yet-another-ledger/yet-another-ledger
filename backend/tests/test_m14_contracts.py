"""M14 Step 1 OpenAPI and SQLAlchemy ownership contract checks."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jai.main import app
from jai.models.expense_attachment import ExpenseAttachment
from jai.schemas.expense import ExpenseInput
from jai.schemas.receipt_inbox import (
    ReceiptInboxAIState,
    ReceiptInboxErrorCode,
    ReceiptInboxStatus,
)
from jai.services.receipt_inbox import (
    _has_presentable_suggestion,
)


def test_receipt_inbox_openapi_contract_is_present_and_has_no_storage_key() -> None:
    schema = app.openapi()
    paths = schema["paths"]
    expected = {
        ("/api/v1/receipt-inbox", "post"),
        ("/api/v1/receipt-inbox", "get"),
        ("/api/v1/receipt-inbox/{inbox_id}", "get"),
        ("/api/v1/receipt-inbox/{inbox_id}/files", "post"),
        ("/api/v1/receipt-inbox/{inbox_id}/files/{file_id}", "delete"),
        ("/api/v1/receipt-inbox/{inbox_id}/extract", "post"),
        ("/api/v1/receipt-inbox/{inbox_id}/confirm", "post"),
        ("/api/v1/receipt-inbox/{inbox_id}", "delete"),
    }
    assert expected <= {(path, method) for path, item in paths.items() for method in item}
    schemas = schema["components"]["schemas"]
    assert "storage_key" not in schemas["ReceiptInboxFileRead"]["properties"]
    assert schemas["ExpenseAttachmentRead"]["properties"]["expense_id"]["type"] == "string"
    assert schemas["ReceiptInboxRead"]["properties"]["status"]["$ref"].endswith(
        "/ReceiptInboxStatus"
    )


def test_receipt_inbox_enums_match_frozen_codes() -> None:
    assert {value.value for value in ReceiptInboxStatus} == {"PENDING", "CONFIRMED"}
    assert {value.value for value in ReceiptInboxAIState} == {
        "NOT_RUN",
        "RUNNING",
        "READY",
        "UNAVAILABLE",
        "FAILED",
    }
    assert {value.value for value in ReceiptInboxErrorCode} == {
        "INBOX_NOT_PENDING",
        "FILE_LIMIT_REACHED",
        "LAST_FILE",
        "ALREADY_CONFIRMED",
        "AI_DISABLED",
        "AI_NOT_CONFIGURED",
        "AI_TIMEOUT",
        "AI_PROVIDER_ERROR",
        "AI_INVALID_RESPONSE",
    }


@pytest.mark.parametrize(
    ("suggestion", "category_matches", "rate_matches", "expected"),
    [
        ({"suggested_category_name": "Unknown"}, False, False, False),
        ({"suggested_category_name": "  "}, False, False, False),
        ({"supplier_name": "  ", "summary": "\t"}, False, False, False),
        ({"vat_rate_percent": "21"}, False, False, False),
        ({"vat_rate_percent": "0"}, False, True, True),
        ({"suggested_category_name": "Office"}, True, False, True),
        ({"supplier_name": " Shop "}, False, False, True),
        ({"net_amount": "0"}, False, False, True),
    ],
)
def test_only_presentable_suggestions_count_as_ready(
    suggestion: dict[str, object],
    category_matches: bool,
    rate_matches: bool,
    expected: bool,
) -> None:
    assert _has_presentable_suggestion(
        suggestion, category_matches=category_matches, rate_matches=rate_matches
    ) is expected


def test_attachment_model_requires_exactly_one_parent() -> None:
    constraint = next(
        item
        for item in ExpenseAttachment.__table__.constraints
        if item.name == "ck_expense_attachment_exactly_one_parent"
    )
    assert "expense_id IS NOT NULL" in str(constraint.sqltext)
    assert "receipt_inbox_id IS NOT NULL" in str(constraint.sqltext)
    assert ExpenseAttachment.__table__.c.expense_id.nullable
    assert ExpenseAttachment.__table__.c.receipt_inbox_id.nullable


def test_confirmation_uses_complete_expense_input_contract() -> None:
    with pytest.raises(ValidationError) as error:
        ExpenseInput.model_validate({})
    assert {tuple(item["loc"]) for item in error.value.errors()} == {
        ("expense_date",),
        ("category_id",),
        ("vat_treatment_id",),
        ("vat_rate_id",),
        ("net_amount",),
        ("vat_amount",),
    }

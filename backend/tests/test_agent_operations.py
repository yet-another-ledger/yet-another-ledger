"""Unit tests for canonical arguments and privacy-limited operation summaries."""

import base64
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError
from pydantic.fields import PydanticUndefined

from jai.models._enums import DiscountType, InvoiceStatus, InvoiceTaxMode, QuoteStatus
from jai.schemas.agent_tools import ReceiptSubmissionSnapshot
from jai.schemas.customer import CustomerRead
from jai.schemas.invoice import InvoiceLineInput, InvoiceRead, InvoiceWrite
from jai.schemas.quote import QuoteRead, QuoteWrite
from jai.services.agent_operations import (
    _assert_target_id_matches,
    _safe_source,
    _typed_snapshot,
    canonical_argument_digest,
)


def test_canonical_argument_digest_ignores_object_order_and_decimal_scale() -> None:
    first = {"customer": {"amount": Decimal("12.500"), "name": "Sensitive"}}
    second = {"customer": {"name": "Sensitive", "amount": Decimal("12.5")}}
    assert canonical_argument_digest(first) == canonical_argument_digest(second)
    assert canonical_argument_digest({"amount": Decimal("1234567890123456789012345678.500")}) == (
        canonical_argument_digest({"amount": Decimal("1234567890123456789012345678.5")})
    )
    assert canonical_argument_digest({"amount": Decimal("1234567890123456789012345678.5")}) != (
        canonical_argument_digest({"amount": Decimal("1234567890123456789012345678.4")})
    )


def test_canonical_argument_digest_preserves_semantic_types() -> None:
    assert canonical_argument_digest({"value": Decimal("12.5")}) != (
        canonical_argument_digest({"value": "12.5"})
    )
    identifier = uuid.uuid4()
    assert canonical_argument_digest({"value": identifier}) != (
        canonical_argument_digest({"value": str(identifier)})
    )
    timestamp = datetime(2026, 9, 30, tzinfo=UTC)
    assert canonical_argument_digest({"value": timestamp}) != (
        canonical_argument_digest({"value": timestamp.isoformat()})
    )
    assert canonical_argument_digest({"value": date(2026, 9, 30)}) != (
        canonical_argument_digest({"value": "2026-09-30"})
    )
    assert canonical_argument_digest({"value": InvoiceTaxMode.LINE}) != (
        canonical_argument_digest({"value": "LINE"})
    )


@pytest.mark.parametrize(
    ("model", "date_field"),
    [(QuoteWrite, "quote_date"), (InvoiceWrite, "invoice_date")],
)
def test_quote_and_invoice_write_python_inputs_have_stable_date_digests(
    model: type[QuoteWrite] | type[InvoiceWrite], date_field: str,
) -> None:
    payload: dict[str, Any] = {
        "customer_id": uuid.uuid4(),
        date_field: date(2026, 9, 30),
        "tax_mode": InvoiceTaxMode.LINE,
        "lines": [
            InvoiceLineInput(
                name="Consulting",
                quantity=Decimal("1.000"),
                unit_price=Decimal("95.00"),
            )
        ],
    }
    write = model.model_validate(payload)
    python_arguments = write.model_dump(mode="python")

    assert isinstance(python_arguments[date_field], date)
    assert isinstance(python_arguments["tax_mode"], InvoiceTaxMode)
    assert canonical_argument_digest(python_arguments) == canonical_argument_digest(
        write.model_dump(mode="python")
    )
    changed_payload = dict(python_arguments)
    changed_payload[date_field] = date(2026, 10, 1)
    changed_date = model.model_validate(changed_payload)
    assert canonical_argument_digest(python_arguments) != canonical_argument_digest(
        changed_date.model_dump(mode="python")
    )


def test_canonical_argument_digest_rejects_nonfinite_values() -> None:
    with pytest.raises(ValueError):
        canonical_argument_digest({"amount": Decimal("NaN")})


def test_source_fields_are_validated_and_part_of_the_digest() -> None:
    _, source_a = _safe_source(
        {"platform": "telegram", "chat_id": "chat-a", "message_id": "message-1"}
    )
    _, source_b = _safe_source(
        {"platform": "telegram", "chat_id": "chat-b", "message_id": "message-1"}
    )
    assert source_a is not None and source_b is not None
    assert canonical_argument_digest({"arguments": {"name": "A"}, "source": source_a}) != (
        canonical_argument_digest({"arguments": {"name": "A"}, "source": source_b})
    )
    with pytest.raises(ValidationError):
        _safe_source({"platform": "telegram", "bearer": "must-not-be-accepted"})


def test_typed_customer_snapshot_retains_the_complete_read_result() -> None:
    target_id = uuid.uuid4()
    customer = CustomerRead(
        id=target_id,
        name="Frozen",
        email="contact@example.test",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    snapshot = _typed_snapshot("customer", customer)
    assert snapshot["id"] == str(target_id)
    assert snapshot["email"] == "contact@example.test"
    with pytest.raises(ValueError):
        _typed_snapshot("invoice", customer)
    with pytest.raises(ValueError, match="receipt-file"):
        _typed_snapshot(
            "customer",
            CustomerRead(
                id=target_id,
                name="Unsafe extra",
                extra={"receipt_data_base64": "JVBERi0xLjQK"},
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            ),
        )
    with pytest.raises(ValueError, match="bearer"):
        _typed_snapshot(
            "customer",
            CustomerRead(
                id=target_id,
                name="Unsafe bearer",
                extra={"notes": "Bearer secret-value"},
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            ),
        )
    ai_like_user_text = CustomerRead(
        id=target_id,
        name="AI-like user text",
        extra={
            "raw_ai_output": {"name": "inferred"},
            "ai_response": "The supplier name may be Example BV.",
            "response": "I inferred this from the receipt.",
        },
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    assert _typed_snapshot("customer", ai_like_user_text)["extra"] == ai_like_user_text.extra


@pytest.mark.parametrize(
    ("target_type", "result", "id_field"),
    [
        ("customer", CustomerRead.model_construct(id=uuid.UUID(int=1)), "id"),
        ("quote", QuoteRead.model_construct(id=uuid.UUID(int=1)), "id"),
        ("invoice", InvoiceRead.model_construct(id=uuid.UUID(int=1)), "id"),
        (
            "receipt",
            ReceiptSubmissionSnapshot.model_construct(inbox_id=uuid.UUID(int=1)),
            "inbox_id",
        ),
    ],
)
def test_all_typed_result_ids_must_match_operation_target(
    target_type: str, result: BaseModel, id_field: str,
) -> None:
    target_id = getattr(result, id_field)
    _assert_target_id_matches(target_type, target_id, result)
    with pytest.raises(ValueError, match="match its operation target ID"):
        _assert_target_id_matches(target_type, uuid.uuid4(), result)


@pytest.mark.parametrize(
    "extra",
    [
        {"token": "jai1.company.selector.secret"},
        {"nested": [{"api_key": "sensitive"}]},
        {"nested": {"authorization": "sensitive"}},
        {"metadata": {"client_secret": "sensitive"}},
    ],
)
def test_typed_customer_snapshot_rejects_nested_sensitive_extra_keys(
    extra: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="forbidden"):
        _typed_snapshot(
            "customer",
            CustomerRead(
                id=uuid.uuid4(),
                name="Unsafe extra",
                extra=extra,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            ),
        )


@pytest.mark.parametrize(
    "notes",
    [
        "Contact sent this Bearer secret-value during the call",
        "Reference jai1.00000000-0000-0000-0000-000000000001."
        "00000000-0000-0000-0000-000000000002."
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA in the note",
    ],
)
def test_typed_customer_snapshot_rejects_credentials_embedded_in_notes(notes: str) -> None:
    with pytest.raises(ValueError, match="credential"):
        _typed_snapshot(
            "customer",
            CustomerRead(
                id=uuid.uuid4(),
                name="Unsafe note",
                # CustomerRead has no notes field; preserve the free-text test
                # through its allowed arbitrary `extra` content.
                extra={"notes": notes, "ordinary": "approved"},
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            ),
        )


def test_typed_customer_snapshot_retains_ordinary_extra_fields() -> None:
    customer = CustomerRead(
        id=uuid.uuid4(),
        name="Safe extra",
        extra={"notes": "call after 10:00", "preferred_contact": "email"},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    snapshot = _typed_snapshot("customer", customer)
    assert snapshot["extra"] == {
        "notes": "call after 10:00",
        "preferred_contact": "email",
    }


def test_typed_customer_snapshot_allows_keys_containing_sensitive_words() -> None:
    customer = CustomerRead(
        id=uuid.uuid4(),
        name="Safe extra keys",
        extra={"secretary_name": "Anna", "token_discount": "early customer"},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    assert _typed_snapshot("customer", customer)["extra"] == customer.extra


@pytest.mark.parametrize(
    "unsafe_key",
    [
        "Bearer secret-value",
        "jai1.00000000-0000-0000-0000-000000000001."
        "00000000-0000-0000-0000-000000000002."
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "data:application/pdf;base64,JVBERi0xLjQK",
        "nested",
        base64.b64encode(b"%PDF-1.7\nfile").decode("ascii"),
    ],
)
def test_typed_customer_snapshot_checks_arbitrary_json_keys_recursively(
    unsafe_key: str,
) -> None:
    if unsafe_key == "nested":
        extra = {unsafe_key: {"Bearer secret-value": "ordinary"}}
    else:
        extra = {"outer": [{unsafe_key: "ordinary"}]}
    with pytest.raises(ValueError):
        _typed_snapshot(
            "customer",
            CustomerRead(
                id=uuid.uuid4(),
                name="Unsafe key",
                extra=extra,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            ),
        )


def test_receipt_submission_snapshot_is_registered_and_limited_to_safe_fields() -> None:
    inbox_id = uuid.uuid4()
    result = ReceiptSubmissionSnapshot(
        inbox_id=inbox_id,
        ai_state="FAILED",
        ai_error_code="AI_DISABLED",
        review_url="https://jai.example.test/receipt-inbox",
    )
    snapshot = _typed_snapshot("receipt", result)
    assert snapshot == {
        "inbox_id": str(inbox_id),
        "ai_state": "FAILED",
        "ai_error_code": "AI_DISABLED",
        "review_url": "https://jai.example.test/receipt-inbox",
    }
    with pytest.raises(ValueError, match="target's allowlisted response schema"):
        _typed_snapshot(
            "receipt",
            CustomerRead(
                id=inbox_id,
                name="wrong result type",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            ),
        )
    with pytest.raises(ValidationError):
        ReceiptSubmissionSnapshot.model_validate(
            {
                "inbox_id": inbox_id,
                "ai_state": "FAILED",
                "ai_error_code": "provider returned 400 with private customer data",
                "review_url": "https://jai.example.test/receipt-inbox",
            }
        )
    with pytest.raises(ValidationError):
        ReceiptSubmissionSnapshot.model_validate(
            {
                "inbox_id": inbox_id,
                "ai_state": "FAILED",
                "ai_error_code": "INBOX_NOT_PENDING",
                "review_url": "https://jai.example.test/receipt-inbox",
            }
        )
    with pytest.raises(ValidationError):
        ReceiptSubmissionSnapshot.model_validate(
            {
                "inbox_id": inbox_id,
                "ai_state": "FAILED",
                "ai_error_code": "AI_DISABLED",
                "review_url": "https://jai.example.test/receipt-inbox",
                "raw_provider_response": {"text": "must not be persisted"},
            }
        )
    with pytest.raises(ValueError, match="credential"):
        _typed_snapshot(
            "receipt",
            ReceiptSubmissionSnapshot(
                inbox_id=inbox_id,
                ai_state="FAILED",
                ai_error_code="AI_DISABLED",
                review_url="Bearer secret-value",
            ),
        )


@pytest.mark.parametrize(
    "content",
    [
        "data:application/pdf;base64,JVBERi0xLjQK",
        "data:text/plain;base64,YWJjZA==",
        "data:;base64,YWJjZA==",
        "data:,plain%20text",
        "DATA:TEXT/PLAIN;BASE64,YWJjZA==",
        'data:text/plain;charset="UTF-8";base64,YWJjZA==',
        "data:text/plain;charset=%22UTF-8%22;base64,YWJjZA==",
        "DATA: \tTEXT/PLAIN ; \tBASE64 , YWJjZA==",
        "data: ; charset = UTF-8 ; BASE64 , YWJjZA==",
        "Customer attachment: data:image/png;base64,iVBORw0KGgo=",
        "data:application/octet-stream;base64,JVBERi0xLjQK",
        base64.b64encode(b"%PDF-1.7\nbody").decode("ascii"),
        base64.b64encode(b"\x89PNG\r\n\x1a\nrest!!!").decode("ascii"),
        base64.b64encode(b"\xff\xd8\xff\xe0\x00\x10JFIF").decode("ascii"),
        base64.b64encode(b"RIFF\x00\x00\x00\x00WEBPVP8 ").decode("ascii"),
        "%PDF-1.7\nraw bytes accidentally decoded as text",
        "\x89PNG\r\n\x1a\nraw bytes accidentally decoded as text",
        "\xff\xd8\xff\xe0raw bytes accidentally decoded as text",
        "RIFF\x00\x00\x00\x00WEBPraw bytes accidentally decoded as text",
    ],
)
def test_typed_customer_snapshot_rejects_recognizable_file_content_under_neutral_keys(
    content: str,
) -> None:
    with pytest.raises(ValueError, match="receipt-file"):
        _typed_snapshot(
            "customer",
            CustomerRead(
                id=uuid.uuid4(),
                name="Neutral key file payload",
                extra={"response": {"note": content}},
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            ),
        )


@pytest.mark.parametrize(
    "content",
    [
        "data: is recorded in the customer profile",
        "The data: text field is optional",
        "Please keep the phrase data: for the audit note",
    ],
)
def test_typed_customer_snapshot_preserves_ordinary_data_colon_text(content: str) -> None:
    customer = CustomerRead(
        id=uuid.uuid4(),
        name="Ordinary data text",
        extra={"notes": content},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    assert _typed_snapshot("customer", customer)["extra"]["notes"] == content


@pytest.mark.parametrize(
    "file_bytes",
    [
        b"%PDF-1.7\noriginal file body",
        b"\x89PNG\r\n\x1a\noriginal image body",
        b"\xff\xd8\xff\xe0\x00\x10JFIF original image body",
        b"RIFF\x00\x00\x00\x00WEBPVP8 original image body",
    ],
    ids=["pdf", "png", "jpeg", "webp"],
)
@pytest.mark.parametrize("separator", [" ", "\n"], ids=["space", "line-wrap"])
def test_typed_customer_snapshot_rejects_file_signatures_after_free_text(
    file_bytes: bytes, separator: str,
) -> None:
    encoded = base64.b64encode(file_bytes).decode("ascii")
    folded = separator.join(encoded[offset : offset + 8] for offset in range(0, len(encoded), 8))
    customer = CustomerRead(
        id=uuid.uuid4(),
        name="Nested file signature",
        extra={"details": {"notes": f"Original vendor attachment {separator}{folded}"}},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    with pytest.raises(ValueError, match="receipt-file"):
        _typed_snapshot("customer", customer)


def test_typed_customer_snapshot_preserves_ordinary_whitespace_text() -> None:
    customer = CustomerRead(
        id=uuid.uuid4(),
        name="Ordinary whitespace text",
        extra={"details": {"notes": "Original vendor attachment with no encoded file"}},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    assert "Original vendor attachment" in _typed_snapshot("customer", customer)["extra"][
        "details"
    ]["notes"]


def _required_payload(model: type[BaseModel]) -> dict[str, Any]:
    """Build a valid minimal schema payload for snapshot round-trip tests."""

    values: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        if field.default is not PydanticUndefined:
            continue
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            values[name] = _required_payload(annotation)
        elif isinstance(annotation, type) and issubclass(annotation, Enum):
            values[name] = next(iter(annotation))
        elif annotation is uuid.UUID:
            values[name] = uuid.uuid4()
        elif annotation is datetime:
            values[name] = datetime.now(UTC)
        elif annotation is date:
            values[name] = date.today()
        elif annotation is Decimal:
            values[name] = Decimal("12.345")
        elif annotation is str:
            values[name] = "snapshot-value"
        elif annotation is int:
            values[name] = 1
        elif annotation is bool:
            values[name] = False
        else:
            raise AssertionError(f"No required test value for {model.__name__}.{name}")
    return values


@pytest.mark.parametrize(
    ("target_type", "schema", "enum_marker"),
    [
        ("quote", QuoteRead, QuoteStatus.DRAFT),
        ("invoice", InvoiceRead, InvoiceStatus.DRAFT),
    ],
)
def test_document_typed_snapshots_preserve_notes_and_authoritative_totals(
    target_type: str,
    schema: type[BaseModel],
    enum_marker: Enum,
) -> None:
    payload = _required_payload(schema)
    payload.update(
        {
            "status": enum_marker,
            "tax_mode": InvoiceTaxMode.LINE,
            "discount_type": DiscountType.NONE,
            "notes": "retain this note for replay",
            "total_incl_vat": Decimal("148.140"),
        }
    )
    original = schema.model_validate(payload)

    snapshot = _typed_snapshot(target_type, original)
    replayed = schema.model_validate(snapshot)

    assert replayed.model_dump(mode="json") == snapshot
    assert snapshot["notes"] == "retain this note for replay"
    assert snapshot["total_incl_vat"] == "148.140"

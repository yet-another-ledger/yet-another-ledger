"""Pure action projection parity guards for M12 Step 10."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from jai.models._enums import InvoiceDocumentKind, InvoiceStatus, QuoteSettlementMode
from jai.services.document_actions import (
    credit_note_eligibility,
    followup_eligibility,
    invoice_revision_eligibility,
)


def _invoice(
    kind: InvoiceDocumentKind, status: InvoiceStatus, *, credited: str = "0", payable: str = "100"
) -> SimpleNamespace:
    return SimpleNamespace(
        document_kind=kind,
        status=status,
        credited_total=Decimal(credited),
        payable_before_payments=Decimal(payable),
    )


@pytest.mark.parametrize(
    ("kind", "status", "credited", "final_draft", "available", "reason"),
    [
        (InvoiceDocumentKind.STANDARD, InvoiceStatus.SENT, "0", False, True, None),
        (
            InvoiceDocumentKind.STANDARD,
            InvoiceStatus.DRAFT,
            "0",
            False,
            False,
            "CREDIT_SOURCE_NOT_ISSUED",
        ),
        (
            InvoiceDocumentKind.CREDIT_NOTE,
            InvoiceStatus.SENT,
            "0",
            False,
            False,
            "CREDIT_OF_CREDIT",
        ),
        (InvoiceDocumentKind.ADVANCE, InvoiceStatus.SENT, "0", True, False, "FINAL_DRAFT_FREEZE"),
        (
            InvoiceDocumentKind.FINAL,
            InvoiceStatus.SENT,
            "100",
            False,
            False,
            "CREDIT_NO_REMAINING_BASIS",
        ),
    ],
)
def test_credit_projection_uses_command_stable_codes(
    kind, status, credited, final_draft, available, reason
) -> None:
    result = credit_note_eligibility(
        _invoice(kind, status, credited=credited), final_draft_exists=final_draft
    )
    assert (result.available, result.reason_code) == (available, reason)


def test_draft_credit_followup_uses_locked_command_code() -> None:
    replacement, compensation = followup_eligibility(
        _invoice(InvoiceDocumentKind.CREDIT_NOTE, InvoiceStatus.DRAFT),
        _invoice(InvoiceDocumentKind.STANDARD, InvoiceStatus.SENT),
        mode=QuoteSettlementMode.DIRECT_INVOICE,
        final_exists=False,
        open_advance_draft_exists=False,
        existing_followup=False,
    )
    assert (replacement.reason_code, compensation.reason_code) == (
        "CREDIT_NOT_ISSUED",
        "CREDIT_NOT_ISSUED",
    )


@pytest.mark.parametrize(
    ("overrides", "available", "reason"),
    [
        ({}, True, None),
        ({"document_kind": InvoiceDocumentKind.ADVANCE}, False, "REVISION_KIND_UNSUPPORTED"),
        ({"status": InvoiceStatus.DRAFT}, False, "REVISION_STATUS_UNSUPPORTED"),
        (
            {"settlement_mode": QuoteSettlementMode.RECEIPT_ONLY},
            False,
            "REVISION_SETTLEMENT_UNSUPPORTED",
        ),
        ({"has_effective_payment": True}, False, "REVISION_HAS_PAYMENT"),
        ({"has_successful_email": True}, False, "REVISION_ALREADY_DELIVERED"),
        ({"has_dependent_chain": True}, False, "REVISION_HAS_DEPENDENCIES"),
        ({"has_receipt_only_provenance": True}, False, "REVISION_RECEIPT_PROVENANCE"),
    ],
)
def test_invoice_revision_predicate_uses_actual_guard_facts(overrides, available, reason) -> None:
    values = {
        "document_kind": InvoiceDocumentKind.STANDARD,
        "status": InvoiceStatus.SENT,
        "settlement_mode": QuoteSettlementMode.DIRECT_INVOICE,
        "has_effective_payment": False,
        "has_successful_email": False,
        "has_dependent_chain": False,
        "has_receipt_only_provenance": False,
    }
    values.update(overrides)
    result = invoice_revision_eligibility(**values)
    assert (result.available, result.reason_code) == (available, reason)

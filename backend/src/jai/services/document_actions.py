"""Pure M12 document-action eligibility shared by reads and commands.

The projection deliberately receives an already loaded chain.  Keeping these
predicates free of sessions is what prevents a document-chain GET from taking
the command path's row locks (or growing its query count with every Credit).
Commands call the same functions only after acquiring their canonical locks.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from jai.models._enums import (
    InvoiceDocumentKind,
    InvoiceStatus,
    QuoteSettlementMode,
)
from jai.models.invoice import Invoice

_ZERO = Decimal("0")
_ISSUED = {InvoiceStatus.SENT, InvoiceStatus.COMPLETED}


@dataclass(frozen=True)
class ActionEligibility:
    available: bool
    reason_code: str | None = None


def invoice_revision_eligibility(
    *,
    document_kind: InvoiceDocumentKind,
    status: InvoiceStatus,
    settlement_mode: QuoteSettlementMode | None,
    has_effective_payment: bool,
    has_successful_email: bool,
    has_dependent_chain: bool,
    has_receipt_only_provenance: bool,
) -> ActionEligibility:
    """Pure SI2 read/command predicate; callers supply actual persisted facts."""
    if document_kind != InvoiceDocumentKind.STANDARD:
        return ActionEligibility(False, "REVISION_KIND_UNSUPPORTED")
    if status != InvoiceStatus.SENT:
        return ActionEligibility(False, "REVISION_STATUS_UNSUPPORTED")
    if has_receipt_only_provenance:
        return ActionEligibility(False, "REVISION_RECEIPT_PROVENANCE")
    if settlement_mode not in (None, QuoteSettlementMode.DIRECT_INVOICE):
        return ActionEligibility(False, "REVISION_SETTLEMENT_UNSUPPORTED")
    if has_effective_payment:
        return ActionEligibility(False, "REVISION_HAS_PAYMENT")
    if has_successful_email:
        return ActionEligibility(False, "REVISION_ALREADY_DELIVERED")
    if has_dependent_chain:
        return ActionEligibility(False, "REVISION_HAS_DEPENDENCIES")
    return ActionEligibility(True)


def credit_note_eligibility(source: Invoice, *, final_draft_exists: bool) -> ActionEligibility:
    """The structural portion of the Credit calculate/create command guard."""
    kind = InvoiceDocumentKind(source.document_kind)
    if kind == InvoiceDocumentKind.CREDIT_NOTE:
        return ActionEligibility(False, "CREDIT_OF_CREDIT")
    if InvoiceStatus(source.status) not in _ISSUED:
        return ActionEligibility(False, "CREDIT_SOURCE_NOT_ISSUED")
    if kind == InvoiceDocumentKind.ADVANCE and final_draft_exists:
        return ActionEligibility(False, "FINAL_DRAFT_FREEZE")
    # This cache is maintained by the backend settlement service.  It is only
    # a read projection; calculate/create replays immutable basis lines under
    # locks to detect a concurrent/stale remainder exactly.
    if Decimal(str(source.credited_total)) >= Decimal(str(source.payable_before_payments)):
        return ActionEligibility(False, "CREDIT_NO_REMAINING_BASIS")
    return ActionEligibility(True)


def cancellation_eligibility(
    *,
    mode: QuoteSettlementMode,
    final_draft_exists: bool,
    has_remaining_formal_charge: bool,
) -> ActionEligibility:
    """Shared structural cancellation guard for GET and locked commands.

    ``has_remaining_formal_charge`` is calculated from each source's persisted
    immutable-basis cache by the caller.  It intentionally includes a linked
    supplemental Standard, whose remaining charge is as real as an Advance or
    Final correction source.
    """
    if mode != QuoteSettlementMode.FORMAL_ADVANCE:
        return ActionEligibility(False, "FORMAL_CHAIN_REQUIRED")
    if final_draft_exists:
        return ActionEligibility(False, "FINAL_DRAFT_FREEZE")
    if not has_remaining_formal_charge:
        return ActionEligibility(False, "NO_REMAINING_FORMAL_CHARGE")
    return ActionEligibility(True)


def advance_replacement_capacity_eligibility(
    capacity: Mapping[uuid.UUID, tuple[Decimal, Decimal]],
    required: Mapping[uuid.UUID, tuple[Decimal, Decimal]],
) -> bool:
    """Pure bucket check used by projection and the Quote-locked command."""
    return all(
        (available := capacity.get(rate_id)) is not None
        and net <= available[0]
        and vat <= available[1]
        for rate_id, (net, vat) in required.items()
    )


def followup_eligibility(
    credit: Invoice,
    source: Invoice | None,
    *,
    mode: QuoteSettlementMode | None,
    final_exists: bool,
    open_advance_draft_exists: bool,
    existing_followup: bool,
    advance_capacity_confirmed: bool = True,
) -> tuple[ActionEligibility, ActionEligibility]:
    """Return replacement and compensation eligibility for one issued Credit.

    Amount/capacity revalidation remains command-only because it requires the
    newly copied immutable snapshot.  The chain-state conditions and their
    user-facing reason are nevertheless exactly the same in both callers.
    """
    if InvoiceStatus(credit.status) not in _ISSUED:
        # The locked command has always used this public code.  Keep GET
        # projection exact so callers never need a reason-code translation.
        unavailable = ActionEligibility(False, "CREDIT_NOT_ISSUED")
        return unavailable, unavailable
    if source is None:
        unavailable = ActionEligibility(False, "FOLLOWUP_CHAIN_CONFLICT")
        return unavailable, unavailable
    if existing_followup:
        unavailable = ActionEligibility(False, "FOLLOWUP_ALREADY_EXISTS")
        return unavailable, unavailable

    source_kind = InvoiceDocumentKind(source.document_kind)
    direct_standard = source_kind == InvoiceDocumentKind.STANDARD and (
        mode is None or mode == QuoteSettlementMode.DIRECT_INVOICE
    )
    pre_final_advance = (
        source_kind == InvoiceDocumentKind.ADVANCE and mode is not None and not final_exists
    )

    if source_kind == InvoiceDocumentKind.ADVANCE and mode is not None and final_exists:
        # The command reports the Final freeze distinctly from the structural
        # replacement-kind rule; keep the read projection identical.
        replacement = ActionEligibility(False, "FINAL_DRAFT_FREEZE")
    elif direct_standard:
        replacement = ActionEligibility(True)
    elif pre_final_advance:
        if open_advance_draft_exists:
            replacement = ActionEligibility(False, "ADVANCE_DRAFT_EXISTS")
        elif not advance_capacity_confirmed:
            replacement = ActionEligibility(False, "ADVANCE_REPLACEMENT_CAPACITY")
        else:
            replacement = ActionEligibility(True)
    else:
        replacement = ActionEligibility(False, "REPLACEMENT_NOT_ELIGIBLE")

    # A compensating standard is always possible for an issued Credit; only a
    # pre-Final Advance compensation creates an Advance DRAFT and shares D5.
    compensation = ActionEligibility(
        not (pre_final_advance and open_advance_draft_exists),
        "ADVANCE_DRAFT_EXISTS" if pre_final_advance and open_advance_draft_exists else None,
    )
    return replacement, compensation

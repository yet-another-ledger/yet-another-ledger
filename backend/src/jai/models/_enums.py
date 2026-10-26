"""Enum definitions shared across ORM models.

This module will grow as business entities are introduced (M1+).
"""

from __future__ import annotations

import enum


class SettingLevel(enum.StrEnum):
    """Hierarchy level for the three-layer settings system.

    ``USER`` > ``COMPANY`` > ``GLOBAL`` — lookup falls back from the most
    specific level to the most general.
    """

    GLOBAL = "GLOBAL"
    COMPANY = "COMPANY"
    USER = "USER"


class AddressType(enum.StrEnum):
    """Address type for customer addresses (billing or shipping)."""

    BILLING = "BILLING"
    SHIPPING = "SHIPPING"


class VatTreatmentSide(enum.StrEnum):
    """Which side of a transaction a VAT treatment applies to."""

    SALES = "SALES"
    PURCHASE = "PURCHASE"


class VatTreatmentEffect(enum.StrEnum):
    """How a VAT treatment affects the tax amount on a line item."""

    APPLY_RATE = "APPLY_RATE"
    ZERO_REVERSE = "ZERO_REVERSE"
    ZERO_EXPORT = "ZERO_EXPORT"
    EXEMPT = "EXEMPT"


class InvoiceStatus(enum.StrEnum):
    """Lifecycle status of an invoice."""

    DRAFT = "DRAFT"
    SENT = "SENT"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class InvoicePaidStatus(enum.StrEnum):
    """Payment status of an invoice."""

    UNPAID = "UNPAID"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class InvoiceDocumentKind(enum.StrEnum):
    """Formal document kind for the M12 invoice family."""

    STANDARD = "STANDARD"
    ADVANCE = "ADVANCE"
    FINAL = "FINAL"
    CREDIT_NOTE = "CREDIT_NOTE"


class QuoteSettlementMode(enum.StrEnum):
    """The immutable billing branch selected for an accepted quote."""

    UNSET = "UNSET"
    DIRECT_INVOICE = "DIRECT_INVOICE"
    RECEIPT_ONLY = "RECEIPT_ONLY"
    FORMAL_ADVANCE = "FORMAL_ADVANCE"


class DocumentChainEventType(enum.StrEnum):
    """Append-only, safe-to-display lifecycle facts for a document chain."""

    MODE_LOCKED = "MODE_LOCKED"
    INVOICE_CREATED = "INVOICE_CREATED"
    QUOTE_PAYMENT_CREATED = "QUOTE_PAYMENT_CREATED"
    QUOTE_PAYMENT_UPDATED = "QUOTE_PAYMENT_UPDATED"
    QUOTE_PAYMENT_DELETED = "QUOTE_PAYMENT_DELETED"
    INVOICE_DELETED = "INVOICE_DELETED"
    INVOICE_UPDATED = "INVOICE_UPDATED"
    INVOICE_ISSUED = "INVOICE_ISSUED"
    INVOICE_STATUS_CHANGED = "INVOICE_STATUS_CHANGED"
    INVOICE_PAYMENT_CREATED = "INVOICE_PAYMENT_CREATED"
    INVOICE_PAYMENT_UPDATED = "INVOICE_PAYMENT_UPDATED"
    INVOICE_PAYMENT_DELETED = "INVOICE_PAYMENT_DELETED"
    REFUND_CREATED = "REFUND_CREATED"
    REFUND_UPDATED = "REFUND_UPDATED"
    REFUND_DELETED = "REFUND_DELETED"
    REPLACEMENT_CREATED = "REPLACEMENT_CREATED"
    COMPENSATING_INVOICE_CREATED = "COMPENSATING_INVOICE_CREATED"
    PROJECT_CANCELLATION_CREDIT_CREATED = "PROJECT_CANCELLATION_CREDIT_CREATED"
    INVOICE_REVISED = "INVOICE_REVISED"


class InvoiceRelationType(enum.StrEnum):
    """Typed provenance between a positive follow-up and an issued Credit."""

    REPLACEMENT_OF = "REPLACEMENT_OF"
    COMPENSATES_CREDIT = "COMPENSATES_CREDIT"


class PaymentDirection(enum.StrEnum):
    """Cash direction.  Refund support is wired in a later M12 step."""

    INCOMING = "INCOMING"
    REFUND = "REFUND"


class InvoiceSettlementStatus(enum.StrEnum):
    """Settlement state independent from the document lifecycle."""

    OPEN = "OPEN"
    PARTIALLY_SETTLED = "PARTIALLY_SETTLED"
    SETTLED = "SETTLED"
    REFUND_DUE = "REFUND_DUE"


class InvoiceCreditStatus(enum.StrEnum):
    """Credit coverage state independent from lifecycle and settlement."""

    NOT_CREDITED = "NOT_CREDITED"
    PARTIALLY_CREDITED = "PARTIALLY_CREDITED"
    CREDITED = "CREDITED"


class AdvanceInputMode(enum.StrEnum):
    """Intent accepted by the later Advance calculation/create commands."""

    GROSS_AMOUNT = "GROSS_AMOUNT"
    PERCENTAGE = "PERCENTAGE"


class CreditLineInputMode(enum.StrEnum):
    """Intent accepted by the later source-bound Credit line commands."""

    QUANTITY = "QUANTITY"
    GROSS_AMOUNT = "GROSS_AMOUNT"


class PartySnapshotProvenance(enum.StrEnum):
    """Whether an issue-party snapshot was captured natively or migrated."""

    NATIVE_ISSUE = "NATIVE_ISSUE"
    MIGRATED_CURRENT_STATE = "MIGRATED_CURRENT_STATE"


class InvoiceTaxMode(enum.StrEnum):
    """Whether VAT is calculated per-line or per-document."""

    LINE = "LINE"
    DOCUMENT = "DOCUMENT"


class DiscountType(enum.StrEnum):
    """Type of discount applied to a line or document."""

    NONE = "NONE"
    PERCENTAGE = "PERCENTAGE"
    FIXED = "FIXED"


class NumberSequenceScope(enum.StrEnum):
    """Whether a sequence counter is scoped to the company or a specific customer."""

    COMPANY = "COMPANY"
    CUSTOMER = "CUSTOMER"


class QuoteStatus(enum.StrEnum):
    """Lifecycle status of a quote."""

    DRAFT = "DRAFT"
    SENT = "SENT"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class ContentBlockKind(enum.StrEnum):
    """Category of a standard content block."""

    WARRANTY = "WARRANTY"
    TERMS = "TERMS"
    BANK = "BANK"
    PAYMENT_TERMS = "PAYMENT_TERMS"


class DocumentTemplateScope(enum.StrEnum):
    """Which document types a document template applies to."""

    QUOTE = "QUOTE"
    INVOICE = "INVOICE"
    BOTH = "BOTH"


class RecurringFrequency(enum.StrEnum):
    """How often a recurring expense template fires.

    Used by ``models.recurring_expense.RecurringExpense.frequency``.
    """

    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    YEARLY = "YEARLY"


class PaidBy(enum.StrEnum):
    """Payment source indicator for an expense (D2 / M8.5).

    Indicates whether the expense was paid from the private account or the
    business account.  **Pure bookkeeping – does not affect any calculation.**
    Default: ``BUSINESS``.
    """

    PRIVATE = "PRIVATE"
    BUSINESS = "BUSINESS"


class ExpenseKind(enum.StrEnum):
    """Whether an expense is a normal purchase or a mileage projection."""

    PURCHASE = "PURCHASE"
    MILEAGE = "MILEAGE"


class MileageTripOwnership(enum.StrEnum):
    """Transport ownership boundary for a mileage trip."""

    PRIVATE = "PRIVATE"


class EmailRelatedType(enum.StrEnum):
    """Which document type an email_log row relates to (M9 step 6).

    Uses a generic ``(related_type, related_id)`` pair instead of multiple
    nullable FKs (red-line 6).
    """

    INVOICE = "INVOICE"
    QUOTE = "QUOTE"
    REFUND = "REFUND"


class DocumentArtifactKind(enum.StrEnum):
    """The retained PDF's legal/document purpose."""

    FORMAL_DOCUMENT = "FORMAL_DOCUMENT"
    REFUND_CONFIRMATION = "REFUND_CONFIRMATION"


class DocumentArtifactReason(enum.StrEnum):
    """The user-visible action which first retained the exact PDF bytes."""

    DOWNLOAD = "DOWNLOAD"
    SEND = "SEND"
    UPLOAD = "UPLOAD"


class DocumentArtifactValidationStatus(enum.StrEnum):
    """Overall advisory result for a proposed formal-artifact upload."""

    MATCH = "MATCH"
    WARNING = "WARNING"
    INCONCLUSIVE = "INCONCLUSIVE"


class DocumentArtifactValidationConfidence(enum.StrEnum):
    """Confidence attached to an advisory formal-artifact validation result."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class DocumentArtifactValidationField(enum.StrEnum):
    """A persisted issued-document fact checked by advisory validation."""

    DOCUMENT_NUMBER = "DOCUMENT_NUMBER"
    DOCUMENT_KIND = "DOCUMENT_KIND"
    DOCUMENT_DATE = "DOCUMENT_DATE"
    SUPPLY_OR_ADVANCE_DATE = "SUPPLY_OR_ADVANCE_DATE"
    SELLER = "SELLER"
    BUYER = "BUYER"
    CURRENCY = "CURRENCY"
    TOTAL_EXCL_VAT = "TOTAL_EXCL_VAT"
    VAT_TOTAL = "VAT_TOTAL"
    TOTAL_INCL_VAT = "TOTAL_INCL_VAT"


class DocumentArtifactValidationCheckStatus(enum.StrEnum):
    """Advisory outcome for one formal-artifact validation field."""

    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    NOT_FOUND = "NOT_FOUND"


class EmailStatus(enum.StrEnum):
    """Send-attempt status for an email_log row (M9 step 6).

    ``SENT``   – aiosmtplib accepted the message without error.
    ``FAILED`` – sending raised an exception; ``error_message`` has details.
    """

    SENT = "SENT"
    FAILED = "FAILED"

"""M12 Step 8 reporting projections over frozen formal-document facts."""

# ruff: noqa: E501, E702

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from jai.models._enums import (
    InvoiceDocumentKind,
    InvoiceSettlementStatus,
    InvoiceStatus,
    VatTreatmentEffect,
)
from jai.schemas.report import ReportTaxEventKind, ReportTaxEventRow, ReportWarningCode
from jai.schemas.setting import VatRateTiers
from jai.services.reporting.btw import compute_vat_return
from jai.services.reporting.icp import compute_icp
from jai.services.reporting.pl import compute_profit_loss


def _result_scalars(items: list[object]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = items
    return result


def _empty_scalar_rows(session: AsyncMock) -> None:
    result = MagicMock()
    result.all.return_value = []
    session.scalars.return_value = result


def _tax(rate: str, net: str, vat: str, rate_id: uuid.UUID | None = None) -> MagicMock:
    row = MagicMock()
    row.id = uuid.uuid4()
    row.vat_rate_id = rate_id or uuid.uuid4()
    row.vat_rate_percent = Decimal(rate)
    row.taxable_amount = Decimal(net)
    row.tax_amount = Decimal(vat)
    return row


def _invoice(
    kind: InvoiceDocumentKind,
    event_date: date,
    taxes: list[MagicMock],
    *,
    net: str = "0.00",
    customer_id: uuid.UUID | None = None,
) -> MagicMock:
    row = MagicMock()
    row.id = uuid.uuid4()
    row.document_kind = kind
    row.invoice_number = f"{kind.value}-{row.id.hex[:6]}"
    row.invoice_date = event_date
    row.customer_id = customer_id or uuid.uuid4()
    row.tax_mode.value = "DOCUMENT"
    row.taxes = taxes
    row.lines = []
    row.vat_treatment_code = "NL_DOMESTIC"
    row.vat_treatment_effect = "APPLY_RATE"
    row.vat_treatment_requires_icp = False
    row.base_taxable_amount = Decimal(net)
    return row


def _company() -> MagicMock:
    company = MagicMock()
    company.id = uuid.uuid4()
    company.country_code = "NL"
    return company


@pytest.mark.asyncio
async def test_formal_advance_and_final_use_invoice_dates_and_final_residual() -> None:
    """50/50 formal VAT has two document-dated 50% events, never payment VAT."""
    company = _company()
    rate_id = uuid.uuid4()
    advance = _invoice(
        InvoiceDocumentKind.ADVANCE, date(2026, 3, 20), [_tax("21", "500", "105", rate_id)]
    )
    final = _invoice(
        InvoiceDocumentKind.FINAL, date(2026, 4, 10), [_tax("21", "1000", "210", rate_id)]
    )
    application_tax = MagicMock()
    application_tax.source_vat_rate_id = rate_id
    application_tax.base_taxable_amount = Decimal("500")
    application_tax.base_vat_amount = Decimal("105")
    session = AsyncMock()
    _empty_scalar_rows(session)
    ignored = MagicMock()
    payment_rows = MagicMock(); payment_rows.all.return_value = []
    formal = _result_scalars([advance, final])
    applications = MagicMock(); applications.all.return_value = [(application_tax, final.id)]
    credits = MagicMock(); credits.all.return_value = []
    expenses = _result_scalars([])
    session.execute.side_effect = [ignored, _result_scalars([]), payment_rows, formal, applications, credits, expenses]

    q1 = await compute_vat_return(session, company, 2026, 1, VatRateTiers())
    # The fixture is intentionally invoked for Q1 only by the service mock;
    # 50% formal Advance is a dated invoice fact.
    assert q1.boxes.box_1a.base == Decimal("1000.00")
    assert q1.boxes.box_1a.vat == Decimal("210.00")
    assert [row.document_kind for row in q1.event_rows] == ["ADVANCE", "FINAL"]
    assert [row.taxable_amount for row in q1.event_rows] == [Decimal("500.00"), Decimal("500.00")]


@pytest.mark.asyncio
async def test_cross_period_credit_emits_negative_icp_and_advisory_only() -> None:
    """A cross-border Credit is negative ICP and has no filing-status claim."""
    company = _company()
    customer_id = uuid.uuid4()
    credit = _invoice(InvoiceDocumentKind.CREDIT_NOTE, date(2026, 4, 5), [], customer_id=customer_id)
    correction = MagicMock(); correction.source_invoice_id = uuid.uuid4(); correction.issued_base_gross_amount = Decimal("100")
    line = MagicMock(); line.base_net_amount = Decimal("100"); line.base_gross_amount = Decimal("100")
    basis = MagicMock(); basis.vat_treatment_requires_icp = True
    source_date = date(2026, 3, 10)
    customer = MagicMock(); customer.id = customer_id; customer.name = "EU Buyer"; customer.vat_id = "DE123"
    address = MagicMock(); address.customer_id = customer_id; address.country_code = "DE"
    session = AsyncMock()
    _empty_scalar_rows(session)
    credit_rows = MagicMock()
    credit_rows.all.return_value = [
        (
            credit,
            correction,
            line,
            basis,
            "INV-1",
            source_date,
            InvoiceDocumentKind.STANDARD,
        )
    ]
    session.execute.side_effect = [
        _result_scalars([]), credit_rows, _result_scalars([customer]), _result_scalars([address]),
    ]

    report = await compute_icp(session, company, 2026, 2)
    assert report.total_net == Decimal("-100.00")
    assert report.lines[0].source_documents[0].document_kind == "CREDIT_NOTE"
    assert report.correction_warnings[0].code == "CREDIT_CROSS_PERIOD"
    assert report.correction_warnings[0].source.document_kind == InvoiceDocumentKind.STANDARD
    assert report.correction_warnings[0].amount == Decimal("-100.00")
    assert "filed" not in report.correction_warnings[0].message.lower()


@pytest.mark.asyncio
async def test_vat_event_rows_include_standard_receipt_payment_and_offset_once() -> None:
    """Receipt-only payment tax and its converted-invoice offset remain auditable events."""
    company = _company()
    standard = _invoice(
        InvoiceDocumentKind.STANDARD,
        date(2026, 3, 10),
        [_tax("21", "100", "21")],
    )
    payment_id = uuid.uuid4()
    payment_tax = MagicMock()
    payment_tax.vat_treatment_code = "NL_DOMESTIC"
    payment_tax.vat_treatment_effect = "APPLY_RATE"
    payment_tax.vat_treatment_requires_icp = False
    payment_tax.vat_rate_percent = Decimal("21")
    payment_tax.base_taxable_amount = Decimal("40")
    payment_tax.base_vat_amount = Decimal("8.40")
    payment_rows = MagicMock()
    payment_rows.all.return_value = [(
        payment_id,
        date(2026, 3, 5),
        payment_tax,
        InvoiceDocumentKind.STANDARD,
        InvoiceStatus.SENT,
        InvoiceSettlementStatus.OPEN,
    )]
    offset_rows = MagicMock()
    offset_rows.all.return_value = [(standard.id, payment_id, payment_tax)]
    session = AsyncMock()
    _empty_scalar_rows(session)
    session.execute.side_effect = [
        MagicMock(),
        _result_scalars([standard]),
        payment_rows,
        offset_rows,
        _result_scalars([]),
        MagicMock(all=MagicMock(return_value=[])),
        _result_scalars([]),
    ]

    report = await compute_vat_return(session, company, 2026, 1, VatRateTiers())

    assert [(row.event_kind, row.taxable_amount) for row in report.event_rows] == [
        (ReportTaxEventKind.RECEIPT_ONLY_PAYMENT_TAX, Decimal("40.00")),
        (ReportTaxEventKind.DOCUMENT_TAX, Decimal("100.00")),
        (ReportTaxEventKind.RECEIPT_ONLY_INVOICE_OFFSET, Decimal("-40.00")),
    ]
    assert report.boxes.box_1a.base == Decimal("100.00")
    assert report.boxes.box_1a.vat == Decimal("21.00")
    offset = report.event_rows[2]
    assert offset.document_kind == InvoiceDocumentKind.STANDARD
    assert offset.payment_id == str(payment_id)
    assert offset.vat_treatment_effect == VatTreatmentEffect.APPLY_RATE
    assert offset.vat_rate_percent == Decimal("21")
    payment = report.event_rows[0]
    assert payment.document_kind is None
    assert payment.payment_id == str(payment_id)
    # The payment-tax positive and converted-Standard offset have opposite
    # signed amounts but replay the same frozen routing identity.
    assert payment.vat_treatment_effect == offset.vat_treatment_effect
    assert payment.vat_rate_percent == offset.vat_rate_percent


@pytest.mark.asyncio
async def test_event_rows_are_canonical_for_reversed_equal_amount_cent_tail_buckets() -> None:
    """A tax relationship's load order cannot affect public event ordering."""
    company = _company()
    twenty_one = _tax("21", "0.01", "0.00")
    zero = _tax("0", "0.01", "0.00")
    standard = _invoice(
        InvoiceDocumentKind.STANDARD, date(2026, 3, 10), [twenty_one, zero]
    )

    def session_for(taxes: list[MagicMock]) -> AsyncMock:
        standard.taxes = taxes
        session = AsyncMock()
        _empty_scalar_rows(session)
        empty_rows = MagicMock()
        empty_rows.all.return_value = []
        session.execute.side_effect = [
            MagicMock(),
            _result_scalars([standard]),
            empty_rows,
            empty_rows,
            _result_scalars([]),
            empty_rows,
            _result_scalars([]),
        ]
        return session

    first = await compute_vat_return(
        session_for([twenty_one, zero]), company, 2026, 1, VatRateTiers()
    )
    reversed_rows = await compute_vat_return(
        session_for([zero, twenty_one]), company, 2026, 1, VatRateTiers()
    )
    repeated = await compute_vat_return(
        session_for([zero, twenty_one]), company, 2026, 1, VatRateTiers()
    )

    assert first.event_rows == reversed_rows.event_rows == repeated.event_rows
    assert [row.vat_rate_percent for row in first.event_rows] == [Decimal("0"), Decimal("21")]


def test_event_row_sort_key_covers_public_credit_source_and_receipt_ties() -> None:
    """Every publicly distinguishable audit field breaks an event-row tie."""
    from jai.services.reporting.btw import _event_row_sort_key

    row = ReportTaxEventRow(
        event_kind=ReportTaxEventKind.RECEIPT_ONLY_INVOICE_OFFSET,
        document_id="document-a",
        document_kind=InvoiceDocumentKind.CREDIT_NOTE,
        document_number="CR-1",
        event_date=date(2026, 3, 10),
        payment_id="receipt-a",
        source_document_id="source-a",
        source_document_kind=InvoiceDocumentKind.STANDARD,
        source_document_number="INV-1",
        taxable_amount=Decimal("-0.01"),
        vat_amount=Decimal("0.00"),
        vat_treatment_code="NL_DOMESTIC",
        vat_treatment_effect=VatTreatmentEffect.APPLY_RATE,
        vat_rate_percent=Decimal("21"),
        requires_icp=False,
    )
    changes = {
        "event_kind": ReportTaxEventKind.RECEIPT_ONLY_PAYMENT_TAX,
        "document_id": "document-b",
        "document_kind": InvoiceDocumentKind.FINAL,
        "document_number": "CR-2",
        "event_date": date(2026, 3, 11),
        "payment_id": "receipt-b",
        "source_document_id": "source-b",
        "source_document_kind": InvoiceDocumentKind.ADVANCE,
        "source_document_number": "INV-2",
        "taxable_amount": Decimal("-0.02"),
        "vat_amount": Decimal("-0.01"),
        "vat_treatment_code": "EU_B2B_REVERSE",
        "vat_treatment_effect": VatTreatmentEffect.ZERO_REVERSE,
        "vat_rate_percent": Decimal("0"),
        "requires_icp": True,
    }

    for field, value in changes.items():
        assert _event_row_sort_key(row) != _event_row_sort_key(row.model_copy(update={field: value}))


@pytest.mark.asyncio
async def test_multiline_credit_has_one_base_gross_warning_and_real_source_kind() -> None:
    """Credit tax buckets may split, but correction guidance is per issued Credit."""
    company = _company()
    credit = _invoice(InvoiceDocumentKind.CREDIT_NOTE, date(2026, 4, 5), [])
    correction = MagicMock()
    correction.source_invoice_id = uuid.uuid4()
    correction.issued_base_gross_amount = Decimal("61.29")
    source_date = date(2026, 3, 10)
    basis = MagicMock()
    basis.vat_rate_percent = Decimal("21")
    basis.vat_treatment_code = "NL_DOMESTIC"
    basis.vat_treatment_effect = "APPLY_RATE"
    basis.vat_treatment_requires_icp = False
    first = MagicMock(); first.base_net_amount = Decimal("30"); first.base_vat_amount = Decimal("6.30")
    second = MagicMock(); second.base_net_amount = Decimal("25"); second.base_vat_amount = Decimal("0")
    credit_rows = MagicMock()
    credit_rows.all.return_value = [
        (credit, correction, first, basis, "INV-1", source_date, InvoiceDocumentKind.FINAL),
        (credit, correction, second, basis, "INV-1", source_date, InvoiceDocumentKind.FINAL),
    ]
    session = AsyncMock()
    _empty_scalar_rows(session)
    session.execute.side_effect = [
        MagicMock(),
        _result_scalars([]),
        MagicMock(all=MagicMock(return_value=[])),
        _result_scalars([]),
        credit_rows,
        _result_scalars([]),
    ]

    report = await compute_vat_return(session, company, 2026, 2, VatRateTiers())

    assert len(report.event_rows) == 2
    assert len(report.correction_warnings) == 1
    warning = report.correction_warnings[0]
    assert warning.code == ReportWarningCode.CREDIT_CROSS_PERIOD
    assert warning.amount == Decimal("-61.29")
    assert warning.source.document_kind == InvoiceDocumentKind.FINAL
    assert all(row.source_document_kind == InvoiceDocumentKind.FINAL for row in report.event_rows)
    # Credit rows replay the source treatment/rate snapshots used for their
    # negative tax events; they never infer a rate from rounded amounts.
    assert {row.vat_treatment_effect for row in report.event_rows} == {
        VatTreatmentEffect.APPLY_RATE
    }
    assert {row.vat_rate_percent for row in report.event_rows} == {Decimal("21")}


@pytest.mark.asyncio
async def test_icp_multiline_credit_deduplicates_source_and_uses_same_warning_amount() -> None:
    """One EU-B2B Credit remains one ICP source reference and one warning."""
    company = _company()
    customer_id = uuid.uuid4()
    credit = _invoice(InvoiceDocumentKind.CREDIT_NOTE, date(2026, 4, 5), [], customer_id=customer_id)
    correction = MagicMock()
    correction.source_invoice_id = uuid.uuid4()
    correction.issued_base_gross_amount = Decimal("60")
    source_date = date(2026, 3, 10)
    basis = MagicMock(); basis.vat_treatment_requires_icp = True
    first = MagicMock(); first.base_net_amount = Decimal("40")
    second = MagicMock(); second.base_net_amount = Decimal("20")
    credit_rows = MagicMock()
    credit_rows.all.return_value = [
        (credit, correction, first, basis, "INV-1", source_date, InvoiceDocumentKind.STANDARD),
        (credit, correction, second, basis, "INV-1", source_date, InvoiceDocumentKind.STANDARD),
    ]
    customer = MagicMock(); customer.id = customer_id; customer.name = "EU Buyer"; customer.vat_id = "DE123"
    address = MagicMock(); address.customer_id = customer_id; address.country_code = "DE"
    session = AsyncMock()
    session.execute.side_effect = [
        _result_scalars([]), credit_rows, _result_scalars([customer]), _result_scalars([address]),
    ]

    report = await compute_icp(session, company, 2026, 2)

    assert report.total_net == Decimal("-60.00")
    assert len(report.lines[0].source_documents) == 1
    assert report.lines[0].source_documents[0].source_document_kind == InvoiceDocumentKind.STANDARD
    assert len(report.correction_warnings) == 1
    assert report.correction_warnings[0].amount == Decimal("-60.00")


@pytest.mark.asyncio
async def test_profit_loss_uses_full_final_and_frozen_advance_credit_rule() -> None:
    """Final is full project net; only frozen affects_revenue Credits reverse it."""
    company_id = uuid.uuid4()
    final = _invoice(InvoiceDocumentKind.FINAL, date(2026, 5, 1), [], net="1000")
    credit = _invoice(InvoiceDocumentKind.CREDIT_NOTE, date(2026, 5, 3), [], customer_id=final.customer_id)
    correction = MagicMock(); correction.issued_base_net_amount = Decimal("200")
    session = AsyncMock()
    _empty_scalar_rows(session)
    credit_rows = MagicMock(); credit_rows.all.return_value = [(credit, correction)]
    session.execute.side_effect = [MagicMock(), _result_scalars([final]), credit_rows, _result_scalars([])]

    report = await compute_profit_loss(
        session, company_id, date(2026, 5, 1), date(2026, 5, 31), "month"
    )
    assert report.revenue_net == Decimal("800.00")
    assert report.series[0].revenue_net == Decimal("800.00")

"""Tests for M9 step 6: document email send + email_log.

Unit tests (no DB):
- _send_mail MIME structure with attachment + Cc
- _send_mail without attachment (backward compat)
- send_document_email: SENT path writes EmailLog row
- send_document_email: SMTP error writes FAILED + safe error message
- send_document_email: SMTP unconfigured raises HTTPException 400
- email log does not contain SMTP credentials/passwords
- attachment filename preserved in log
- _build_template_context invoice vs quote fields

Integration tests (require PostgreSQL):
- POST /invoices/{id}/send → EmailLog row + GET /invoices/{id}/emails returns it
- POST /quotes/{id}/send → EmailLog row
- Cross-company 404 on send
- Cross-company 404 on emails list
- SMTP not configured → 400 (no log row written)
"""

from __future__ import annotations

import uuid
from email import message_from_bytes
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pyotp
import pytest
from httpx import AsyncClient

from jai.models._enums import EmailRelatedType, EmailStatus
from jai.schemas.setting import SmtpSettings
from jai.services.email import (
    _build_template_context,
    _send_mail,
    payment_receipt_email_template,
)

# ---------------------------------------------------------------------------
# Helper: build a minimal SmtpSettings for unit tests
# ---------------------------------------------------------------------------


def _smtp() -> SmtpSettings:
    return SmtpSettings(
        host="smtp.example.com",
        port=587,
        username="user",
        password="s3cr3t",
        from_email="sender@example.com",
        from_name="Test Sender",
        use_tls=True,
        use_ssl=False,
    )


def test_payment_receipt_defaults_are_localized_and_independent() -> None:
    """Receipt mail defaults are distinct from invoice/quote Settings templates."""
    en = payment_receipt_email_template("en")
    zh = payment_receipt_email_template("zh")

    assert en.subject == "Payment receipt for {DOCUMENT_NUMBER} from {COMPANY_NAME}"
    assert "payment receipt" in en.body.lower()
    assert zh.subject == "{COMPANY_NAME} 的收款收据（{DOCUMENT_NUMBER}）"
    assert "收款收据" in zh.body
    assert payment_receipt_email_template("unexpected") == en


# ---------------------------------------------------------------------------
# Unit tests – _send_mail MIME structure
# ---------------------------------------------------------------------------


class TestSendMail:
    """Unit tests for the low-level MIME assembly in ``_send_mail``."""

    @pytest.mark.asyncio
    async def test_sends_with_attachment_and_cc(self) -> None:
        """MIME is mixed/alternative + pdf attachment; Cc header + recipients include cc."""
        pdf_bytes = b"%PDF-1.4 fake"
        captured: dict[str, Any] = {}

        async def fake_send(msg: Any, **kwargs: Any) -> None:
            captured["msg"] = msg
            captured["recipients"] = kwargs.get("recipients", [])

        with patch("aiosmtplib.send", side_effect=fake_send):
            await _send_mail(
                _smtp(),
                to="to@example.com",
                subject="Test Subject",
                html_body="<p>Hello</p>",
                cc=["cc1@example.com", "cc2@example.com"],
                attachment_bytes=pdf_bytes,
                attachment_filename="INV-001.pdf",
            )

        msg = captured["msg"]
        raw = msg
        parsed = message_from_bytes(raw)

        # Top-level is multipart/mixed
        assert parsed.get_content_type() == "multipart/mixed"

        parts = list(parsed.walk())
        # Expect: mixed → alternative → html + pdf attachment
        types = [p.get_content_type() for p in parts]
        assert "multipart/alternative" in types
        assert "text/html" in types
        assert "application/pdf" in types

        # Verify attachment filename
        pdf_part = next(p for p in parts if p.get_content_type() == "application/pdf")
        disposition = pdf_part.get("Content-Disposition", "")
        assert "INV-001.pdf" in disposition

        # Cc header present
        assert "cc1@example.com" in parsed.get("Cc", "")
        assert "cc2@example.com" in parsed.get("Cc", "")

        # Envelope recipients include cc addresses
        recipients = captured["recipients"]
        assert "to@example.com" in recipients
        assert "cc1@example.com" in recipients
        assert "cc2@example.com" in recipients

    @pytest.mark.asyncio
    async def test_sends_without_attachment_is_alternative(self) -> None:
        """Without attachment the outer wrapper is multipart/alternative (backward compat)."""
        captured: dict[str, Any] = {}

        async def fake_send(msg: Any, **kwargs: Any) -> None:
            captured["msg"] = msg
            captured["recipients"] = kwargs.get("recipients", [])

        with patch("aiosmtplib.send", side_effect=fake_send):
            await _send_mail(
                _smtp(),
                to="to@example.com",
                subject="Test",
                html_body="<p>body</p>",
            )

        msg = captured["msg"]
        raw = msg
        parsed = message_from_bytes(raw)

        # No attachment → plain alternative, not mixed
        assert parsed.get_content_type() == "multipart/alternative"

        types = [p.get_content_type() for p in parsed.walk()]
        assert "application/pdf" not in types

        # Envelope recipients = [to] only
        assert captured["recipients"] == ["to@example.com"]

    @pytest.mark.asyncio
    async def test_no_cc_means_no_cc_header(self) -> None:
        """When cc is None, the Cc header is not set."""
        captured: dict[str, Any] = {}

        async def fake_send(msg: Any, **kwargs: Any) -> None:
            captured["msg"] = msg

        with patch("aiosmtplib.send", side_effect=fake_send):
            await _send_mail(_smtp(), to="to@example.com", subject="S", html_body="<p>B</p>")

        parsed = message_from_bytes(captured["msg"])
        assert parsed.get("Cc") is None

    @pytest.mark.asyncio
    async def test_ssl_path_uses_use_tls_true(self) -> None:
        """use_ssl=True triggers aiosmtplib.send with use_tls=True (implicit TLS)."""
        cfg = _smtp()
        cfg.use_ssl = True

        calls: list[dict[str, Any]] = []

        async def fake_send(msg: Any, **kwargs: Any) -> None:
            calls.append(kwargs)

        with patch("aiosmtplib.send", side_effect=fake_send):
            await _send_mail(cfg, "x@y.com", "S", "<p>B</p>")

        assert len(calls) == 1
        assert calls[0].get("use_tls") is True


# ---------------------------------------------------------------------------
# Unit tests – send_document_email service
# ---------------------------------------------------------------------------


class TestSendDocumentEmail:
    """Unit tests for ``send_document_email``."""

    def _make_doc(self, doc_type: str = "invoice") -> Any:
        """Return a minimal mock document object."""
        doc = MagicMock()
        doc.id = uuid.uuid4()
        doc.currency = "EUR"
        doc.total_incl_vat = "1000.000"
        if doc_type == "invoice":
            doc.invoice_number = "INV-001"
            doc.invoice_date = "2026-06-14"
            doc.due_date = "2026-07-14"
            doc.due_amount = "1000.000"
        else:
            doc.quote_number = "QUO-001"
            doc.quote_date = "2026-06-14"
            doc.valid_until = "2026-07-14"
        return doc

    def _make_company(self) -> Any:
        company = MagicMock()
        company.id = uuid.uuid4()
        company.name = "Acme BV"
        return company

    def _make_customer(self) -> Any:
        customer = MagicMock()
        customer.name = "Bob Ltd"
        return customer

    def _make_session(self) -> AsyncMock:
        session = AsyncMock()
        session.add = MagicMock()
        session.flush = AsyncMock()
        return session

    @pytest.mark.asyncio
    async def test_success_path_writes_sent_log(self) -> None:
        """Successful send → EmailLog with status=SENT and sent_at set."""
        from jai.services.email import send_document_email

        doc = self._make_doc("invoice")
        company = self._make_company()
        customer = self._make_customer()
        session = self._make_session()
        artifact_id = uuid.uuid4()

        with (
            patch(
                "jai.services.email._get_smtp_config",
                return_value=_smtp(),
            ),
            patch(
                "jai.services.email._send_mail",
                new_callable=AsyncMock,
            ),
            patch("jai.services.settings.get_setting", return_value=None),
        ):
            log = await send_document_email(
                session=session,
                related_type=EmailRelatedType.INVOICE,
                doc=doc,
                company=company,
                customer=customer,
                to="recipient@example.com",
                cc=None,
                locale="en",
                subject=None,
                body=None,
                pdf_bytes=b"%PDF",
                filename="INV-001.pdf",
                creator_id=uuid.uuid4(),
                artifact_id=artifact_id,
            )

        assert log.status == EmailStatus.SENT
        assert log.sent_at is not None
        assert log.error_message is None
        assert log.to_email == "recipient@example.com"
        assert log.attachment_filename == "INV-001.pdf"
        assert log.locale == "en"
        assert log.related_type == EmailRelatedType.INVOICE
        assert log.artifact_id == artifact_id
        assert session.add.called
        assert session.flush.called

    @pytest.mark.asyncio
    async def test_smtp_error_writes_failed_log(self) -> None:
        """SMTP exception → EmailLog with status=FAILED + safe error_message."""
        from jai.services.email import send_document_email

        doc = self._make_doc("invoice")
        company = self._make_company()
        customer = self._make_customer()
        session = self._make_session()
        artifact_id = uuid.uuid4()

        async def _fail(*args: Any, **kwargs: Any) -> None:
            raise ConnectionError("Connection refused")

        with (
            patch("jai.services.email._get_smtp_config", return_value=_smtp()),
            patch("jai.services.email._send_mail", side_effect=_fail),
            patch("jai.services.settings.get_setting", return_value=None),
        ):
            log = await send_document_email(
                session=session,
                related_type=EmailRelatedType.INVOICE,
                doc=doc,
                company=company,
                customer=customer,
                to="x@y.com",
                cc=None,
                locale="en",
                subject=None,
                body=None,
                pdf_bytes=b"%PDF",
                filename="INV-001.pdf",
                artifact_id=artifact_id,
            )

        assert log.status == EmailStatus.FAILED
        assert log.sent_at is None
        assert log.error_message is not None
        assert log.error_message == "SMTP_SEND_FAILED"
        assert log.artifact_id is None

    @pytest.mark.asyncio
    async def test_error_message_does_not_contain_smtp_password(self) -> None:
        """Sanitised error_message must not expose the SMTP password."""
        from jai.services.email import send_document_email

        cfg = _smtp()
        cfg.password = "MY_SECRET_PASS"

        doc = self._make_doc("invoice")
        company = self._make_company()
        customer = self._make_customer()
        session = self._make_session()

        async def _fail(*args: Any, **kwargs: Any) -> None:
            # Simulate an exception that accidentally includes the password.
            raise RuntimeError("Auth error with password=MY_SECRET_PASS host=smtp.example.com")

        with (
            patch("jai.services.email._get_smtp_config", return_value=cfg),
            patch("jai.services.email._send_mail", side_effect=_fail),
            patch("jai.services.settings.get_setting", return_value=None),
        ):
            log = await send_document_email(
                session=session,
                related_type=EmailRelatedType.INVOICE,
                doc=doc,
                company=company,
                customer=customer,
                to="x@y.com",
                cc=None,
                locale="en",
                subject=None,
                body=None,
                pdf_bytes=b"%PDF",
                filename="test.pdf",
            )

        assert log.status == EmailStatus.FAILED
        assert log.error_message is not None
        # The SMTP password must not appear in the log.
        assert "MY_SECRET_PASS" not in (log.error_message or "")
        assert log.error_message == "SMTP_SEND_FAILED"

    @pytest.mark.asyncio
    async def test_smtp_not_configured_raises_400(self) -> None:
        """SMTP not configured → HTTPException 400, no EmailLog row written."""
        from fastapi import HTTPException

        from jai.services.email import send_document_email

        doc = self._make_doc("invoice")
        company = self._make_company()
        customer = self._make_customer()
        session = self._make_session()

        with patch("jai.services.email._get_smtp_config", return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                await send_document_email(
                    session=session,
                    related_type=EmailRelatedType.INVOICE,
                    doc=doc,
                    company=company,
                    customer=customer,
                    to="x@y.com",
                    cc=None,
                    locale="en",
                    subject=None,
                    body=None,
                    pdf_bytes=b"%PDF",
                    filename="test.pdf",
                )

        assert exc_info.value.status_code == 400
        # No EmailLog row must have been added.
        session.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_log_body_snapshot_does_not_contain_smtp_credentials(self) -> None:
        """body_snapshot stored in the log must not contain SMTP credentials."""
        from jai.services.email import send_document_email

        doc = self._make_doc("invoice")
        company = self._make_company()
        customer = self._make_customer()
        session = self._make_session()

        with (
            patch("jai.services.email._get_smtp_config", return_value=_smtp()),
            patch("jai.services.email._send_mail", new_callable=AsyncMock),
            patch("jai.services.settings.get_setting", return_value=None),
        ):
            log = await send_document_email(
                session=session,
                related_type=EmailRelatedType.INVOICE,
                doc=doc,
                company=company,
                customer=customer,
                to="recipient@example.com",
                cc=None,
                locale="en",
                subject=None,
                body=None,
                pdf_bytes=b"%PDF",
                filename="INV-001.pdf",
            )

        # The SMTP password must not appear in any log field.
        for field_val in [
            log.body_snapshot or "",
            log.subject or "",
            log.error_message or "",
            log.to_email or "",
        ]:
            assert "s3cr3t" not in field_val

    @pytest.mark.asyncio
    async def test_quote_template_context(self) -> None:
        """Quote log has quote-specific fields set; invoice-specific ones absent."""
        from jai.services.email import send_document_email

        doc = self._make_doc("quote")
        company = self._make_company()
        customer = self._make_customer()
        session = self._make_session()

        with (
            patch("jai.services.email._get_smtp_config", return_value=_smtp()),
            patch("jai.services.email._send_mail", new_callable=AsyncMock),
            patch("jai.services.settings.get_setting", return_value=None),
        ):
            log = await send_document_email(
                session=session,
                related_type=EmailRelatedType.QUOTE,
                doc=doc,
                company=company,
                customer=customer,
                to="x@y.com",
                cc=None,
                locale="en",
                subject=None,
                body=None,
                pdf_bytes=b"%PDF",
                filename="QUO-001.pdf",
            )

        assert log.related_type == EmailRelatedType.QUOTE
        assert "QUO-001" in log.subject

    @pytest.mark.asyncio
    async def test_cc_list_stored_in_log(self) -> None:
        """CC addresses are stored comma-separated in the log."""
        from jai.services.email import send_document_email

        doc = self._make_doc("invoice")
        company = self._make_company()
        customer = self._make_customer()
        session = self._make_session()

        with (
            patch("jai.services.email._get_smtp_config", return_value=_smtp()),
            patch("jai.services.email._send_mail", new_callable=AsyncMock),
            patch("jai.services.settings.get_setting", return_value=None),
        ):
            log = await send_document_email(
                session=session,
                related_type=EmailRelatedType.INVOICE,
                doc=doc,
                company=company,
                customer=customer,
                to="to@example.com",
                cc=["cc1@example.com", "cc2@example.com"],
                locale="en",
                subject=None,
                body=None,
                pdf_bytes=b"%PDF",
                filename="INV-001.pdf",
            )

        assert log.cc is not None
        assert "cc1@example.com" in log.cc
        assert "cc2@example.com" in log.cc


# ---------------------------------------------------------------------------
# Unit tests – _build_template_context
# ---------------------------------------------------------------------------


class TestBuildTemplateContext:
    """Unit tests for the placeholder context builder."""

    def _make_invoice(self) -> Any:
        doc = MagicMock()
        doc.invoice_number = "INV-099"
        doc.invoice_date = "2026-06-14"
        doc.due_date = "2026-07-14"
        doc.due_amount = "500.000"
        doc.currency = "EUR"
        doc.total_incl_vat = "500.000"
        return doc

    def _make_quote(self) -> Any:
        doc = MagicMock()
        doc.quote_number = "QUO-099"
        doc.quote_date = "2026-06-14"
        doc.valid_until = "2026-07-14"
        doc.currency = "EUR"
        doc.total_incl_vat = "500.000"
        return doc

    def _make_company(self) -> Any:
        company = MagicMock()
        company.name = "Acme BV"
        return company

    def _make_customer(self) -> Any:
        customer = MagicMock()
        customer.name = "Bob Ltd"
        return customer

    def test_invoice_context_has_invoice_fields(self) -> None:
        """Invoice context includes INVOICE_NUMBER, DUE_DATE, AMOUNT_DUE."""
        ctx = _build_template_context(
            "invoice", self._make_invoice(), self._make_company(), self._make_customer()
        )
        assert ctx["INVOICE_NUMBER"] == "INV-099"
        assert ctx["DUE_DATE"] == "2026-07-14"
        assert ctx["AMOUNT_DUE"] == "500.000"
        assert ctx["QUOTE_NUMBER"] == ""  # quote fields empty for invoice

    def test_quote_context_has_quote_fields(self) -> None:
        """Quote context includes QUOTE_NUMBER, VALID_UNTIL; invoice fields are empty."""
        ctx = _build_template_context(
            "quote", self._make_quote(), self._make_company(), self._make_customer()
        )
        assert ctx["QUOTE_NUMBER"] == "QUO-099"
        assert ctx["VALID_UNTIL"] == "2026-07-14"
        assert ctx["INVOICE_NUMBER"] == ""  # invoice fields empty for quote

    def test_common_fields_always_present(self) -> None:
        """COMPANY_NAME, CUSTOMER_NAME, CURRENCY, TOTAL are always set."""
        for doc_type, doc in [
            ("invoice", self._make_invoice()),
            ("quote", self._make_quote()),
        ]:
            ctx = _build_template_context(
                doc_type, doc, self._make_company(), self._make_customer()
            )
            assert ctx["COMPANY_NAME"] == "Acme BV"
            assert ctx["CUSTOMER_NAME"] == "Bob Ltd"
            assert ctx["CURRENCY"] == "EUR"
            assert ctx["TOTAL"] == "500.000"

    def test_none_values_become_empty_string(self) -> None:
        """None attribute values are converted to '' not 'None'."""
        doc = MagicMock()
        doc.invoice_number = None
        doc.invoice_date = None
        doc.due_date = None
        doc.due_amount = None
        doc.currency = None
        doc.total_incl_vat = None
        company = MagicMock()
        company.name = None
        customer = MagicMock()
        customer.name = None

        ctx = _build_template_context("invoice", doc, company, customer)
        for v in ctx.values():
            assert v != "None", f"Found 'None' string in context: {ctx}"


# ---------------------------------------------------------------------------
# Integration tests – send + email log API endpoints
# ---------------------------------------------------------------------------


async def _full_auth(client: AsyncClient) -> str:
    """Register → login → MFA setup → MFA verify. Returns TOTP secret."""
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "owner@example.com", "password": "testpassword1"},
    )
    assert resp.status_code == 201
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": "testpassword1"},
    )
    assert resp.status_code == 200
    resp = await client.post("/api/v1/auth/mfa/setup")
    assert resp.status_code == 200
    secret = resp.json()["secret"]
    code = pyotp.TOTP(secret).now()
    resp = await client.post("/api/v1/auth/mfa/verify", json={"code": code})
    assert resp.status_code == 204
    return secret


async def _setup_company(client: AsyncClient) -> str:
    """Create the singleton company, return company id."""
    resp = await client.put(
        "/api/v1/company",
        json={"name": "Test Co BV", "base_currency": "EUR"},
    )
    assert resp.status_code == 200
    return resp.json()["id"]


async def _setup_vat(client: AsyncClient) -> dict[str, str]:
    """Create a minimal VAT rate + treatment and return their IDs."""
    resp = await client.post(
        "/api/v1/vat-rates",
        json={"label": "Standard 21%", "percent": "21.000", "active": True},
    )
    assert resp.status_code == 201, resp.text
    rate_id = resp.json()["id"]

    resp = await client.post(
        "/api/v1/vat-treatments",
        json={
            "label": "Standard sales",
            "code": "STD_SALES",
            "side": "SALES",
            "effect": "APPLY_RATE",
            "requires_icp": False,
        },
    )
    assert resp.status_code == 201, resp.text
    treatment_id = resp.json()["id"]

    return {"rate_id": rate_id, "treatment_id": treatment_id}


async def _create_customer(client: AsyncClient) -> str:
    """Create a minimal customer, return customer id."""
    resp = await client.post(
        "/api/v1/customers",
        json={"name": "Test Customer", "email": "customer@example.com"},
    )
    assert resp.status_code == 201
    return resp.json()["id"]


async def _create_invoice(
    client: AsyncClient,
    customer_id: str,
    treatment_id: str,
    rate_id: str,
) -> str:
    """Create a minimal invoice and return its id."""
    payload = {
        "customer_id": customer_id,
        "invoice_date": "2026-06-14",
        "due_date": "2026-07-14",
        "currency": "EUR",
        "tax_mode": "LINE",
        "amounts_include_vat": False,
        "vat_treatment_id": treatment_id,
        "lines": [
            {
                "name": "Test service",
                "description": "Test line",
                "quantity": "1",
                "unit_price": "100",
                "vat_rate_id": rate_id,
                "discount_type": "NONE",
                "discount_value": "0",
            }
        ],
    }
    resp = await client.post("/api/v1/invoices", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _issue_invoice(client: AsyncClient, invoice_id: str) -> None:
    """Issue a draft invoice (DRAFT -> SENT) so it receives a legal number.

    Required before an invoice can be emailed (issue-gated guard, D8): an
    unnumbered draft is rejected with a 400.
    """
    resp = await client.post(
        f"/api/v1/invoices/{invoice_id}/status",
        json={"status": "SENT"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["invoice_number"] is not None


async def _create_quote(
    client: AsyncClient,
    customer_id: str,
    treatment_id: str,
    rate_id: str,
) -> str:
    """Create a minimal quote and return its id."""
    payload = {
        "customer_id": customer_id,
        "quote_date": "2026-06-14",
        "valid_until": "2026-07-14",
        "currency": "EUR",
        "tax_mode": "LINE",
        "amounts_include_vat": False,
        "vat_treatment_id": treatment_id,
        "lines": [
            {
                "name": "Test service",
                "description": "Test line",
                "quantity": "1",
                "unit_price": "100",
                "vat_rate_id": rate_id,
                "discount_type": "NONE",
                "discount_value": "0",
            }
        ],
    }
    resp = await client.post("/api/v1/quotes", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.integration
class TestEmailSendIntegration:
    """Integration tests for the email send + log API endpoints."""

    @pytest.mark.asyncio
    async def test_send_invoice_creates_log_and_list_returns_it(
        self, db_client: AsyncClient
    ) -> None:
        """POST /invoices/{id}/send → 200 + log row; GET /invoices/{id}/emails lists it."""
        await _full_auth(db_client)
        await _setup_company(db_client)
        vat = await _setup_vat(db_client)
        customer_id = await _create_customer(db_client)
        invoice_id = await _create_invoice(
            db_client, customer_id, vat["treatment_id"], vat["rate_id"]
        )
        await _issue_invoice(db_client, invoice_id)

        # Mock SMTP so no real email is sent but the service sees a configured state.
        captured: dict[str, bytes] = {}

        async def _capture(**kwargs: Any) -> None:
            captured["attachment"] = kwargs["attachment_bytes"]

        with (
            patch(
                "jai.services.email._get_smtp_config",
                return_value=_smtp(),
            ),
            patch("jai.services.email._send_mail", side_effect=_capture),
        ):
            resp = await db_client.post(
                f"/api/v1/invoices/{invoice_id}/send",
                json={"to": "customer@example.com", "cc": "cc@example.com"},
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "SENT"
        assert data["to_email"] == "customer@example.com"
        assert data["cc"] == "cc@example.com"
        assert data["related_type"] == "INVOICE"
        assert data["related_id"] == invoice_id
        assert data["attachment_filename"] is not None
        assert data["artifact_id"] is not None
        artifacts = await db_client.get(f"/api/v1/invoices/{invoice_id}/artifacts")
        assert artifacts.status_code == 200
        assert len(artifacts.json()["items"]) == 1
        artifact = await db_client.get(
            f"/api/v1/invoices/{invoice_id}/artifacts/{data['artifact_id']}"
        )
        assert artifact.status_code == 200
        assert artifact.content == captured["attachment"]
        # No sensitive fields exposed.
        assert "company_id" not in data

        # GET /emails returns the log.
        list_resp = await db_client.get(f"/api/v1/invoices/{invoice_id}/emails")
        assert list_resp.status_code == 200
        items = list_resp.json()["items"]
        assert len(items) == 1
        assert items[0]["id"] == data["id"]
        assert items[0]["status"] == "SENT"

    @pytest.mark.asyncio
    async def test_send_quote_creates_log(self, db_client: AsyncClient) -> None:
        """POST /quotes/{id}/send → 200 + QUOTE log row."""
        await _full_auth(db_client)
        await _setup_company(db_client)
        vat = await _setup_vat(db_client)
        customer_id = await _create_customer(db_client)
        quote_id = await _create_quote(db_client, customer_id, vat["treatment_id"], vat["rate_id"])

        with (
            patch("jai.services.email._get_smtp_config", return_value=_smtp()),
            patch("jai.services.email._send_mail", new_callable=AsyncMock),
        ):
            resp = await db_client.post(
                f"/api/v1/quotes/{quote_id}/send",
                json={"to": "customer@example.com"},
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "SENT"
        assert data["related_type"] == "QUOTE"

    @pytest.mark.asyncio
    async def test_send_failed_smtp_writes_failed_log(self, db_client: AsyncClient) -> None:
        """SMTP exception during send → 200 response with FAILED status in log."""
        await _full_auth(db_client)
        await _setup_company(db_client)
        vat = await _setup_vat(db_client)
        customer_id = await _create_customer(db_client)
        invoice_id = await _create_invoice(
            db_client, customer_id, vat["treatment_id"], vat["rate_id"]
        )
        await _issue_invoice(db_client, invoice_id)

        async def _fail(*a: Any, **kw: Any) -> None:
            raise ConnectionError("Connection refused to SMTP")

        with (
            patch("jai.services.email._get_smtp_config", return_value=_smtp()),
            patch("jai.services.email._send_mail", side_effect=_fail),
        ):
            resp = await db_client.post(
                f"/api/v1/invoices/{invoice_id}/send",
                json={"to": "x@y.com"},
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "FAILED"
        assert data["artifact_id"] is None
        assert data["error_message"] is not None
        assert data["error_message"] == "SMTP_SEND_FAILED"
        # Log row created and listable.
        list_resp = await db_client.get(f"/api/v1/invoices/{invoice_id}/emails")
        assert list_resp.json()["total"] == 1

    @pytest.mark.asyncio
    async def test_smtp_not_configured_returns_400(self, db_client: AsyncClient) -> None:
        """SMTP not configured → 400; no log row written."""
        await _full_auth(db_client)
        await _setup_company(db_client)
        vat = await _setup_vat(db_client)
        customer_id = await _create_customer(db_client)
        invoice_id = await _create_invoice(
            db_client, customer_id, vat["treatment_id"], vat["rate_id"]
        )
        await _issue_invoice(db_client, invoice_id)

        with patch("jai.services.email._get_smtp_config", return_value=None):
            resp = await db_client.post(
                f"/api/v1/invoices/{invoice_id}/send",
                json={"to": "x@y.com"},
            )

        assert resp.status_code == 400
        assert "SMTP" in resp.json()["detail"]

        # No log row should have been created.
        list_resp = await db_client.get(f"/api/v1/invoices/{invoice_id}/emails")
        assert list_resp.json()["total"] == 0

    @pytest.mark.asyncio
    async def test_send_invoice_cross_company_404(self, db_client: AsyncClient) -> None:
        """POST /invoices/{id}/send with a foreign invoice id → 404."""
        await _full_auth(db_client)
        await _setup_company(db_client)

        fake_id = str(uuid.uuid4())
        resp = await db_client.post(
            f"/api/v1/invoices/{fake_id}/send",
            json={"to": "x@y.com"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_list_emails_cross_company_404(self, db_client: AsyncClient) -> None:
        """GET /invoices/{id}/emails with a foreign invoice id → 404."""
        await _full_auth(db_client)
        await _setup_company(db_client)

        fake_id = str(uuid.uuid4())
        resp = await db_client.get(f"/api/v1/invoices/{fake_id}/emails")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_send_requires_auth(self, client: AsyncClient) -> None:
        """POST /invoices/{id}/send without auth → 401."""
        fake_id = str(uuid.uuid4())
        resp = await client.post(
            f"/api/v1/invoices/{fake_id}/send",
            json={"to": "x@y.com"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_emails_list_requires_auth(self, client: AsyncClient) -> None:
        """GET /invoices/{id}/emails without auth → 401."""
        fake_id = str(uuid.uuid4())
        resp = await client.get(f"/api/v1/invoices/{fake_id}/emails")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_send_invoice_locale_override(self, db_client: AsyncClient) -> None:
        """locale override in request → log row has that locale."""
        await _full_auth(db_client)
        await _setup_company(db_client)
        vat = await _setup_vat(db_client)
        customer_id = await _create_customer(db_client)
        invoice_id = await _create_invoice(
            db_client, customer_id, vat["treatment_id"], vat["rate_id"]
        )
        await _issue_invoice(db_client, invoice_id)

        with (
            patch("jai.services.email._get_smtp_config", return_value=_smtp()),
            patch("jai.services.email._send_mail", new_callable=AsyncMock),
        ):
            resp = await db_client.post(
                f"/api/v1/invoices/{invoice_id}/send",
                json={"to": "x@y.com", "locale": "zh"},
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["locale"] == "zh"

    @pytest.mark.asyncio
    async def test_send_unissued_draft_invoice_returns_400(self, db_client: AsyncClient) -> None:
        """POST /invoices/{id}/send on an unnumbered DRAFT → 400 (issue-gated, D8).

        The draft is rejected before any PDF render / SMTP attempt, and no
        EmailLog row is written.  After issuing it (DRAFT -> SENT) the send
        succeeds (happy-path regression).
        """
        await _full_auth(db_client)
        await _setup_company(db_client)
        vat = await _setup_vat(db_client)
        customer_id = await _create_customer(db_client)
        invoice_id = await _create_invoice(
            db_client, customer_id, vat["treatment_id"], vat["rate_id"]
        )

        # Draft (no number) → blocked, even with SMTP configured + mocked.
        with (
            patch("jai.services.email._get_smtp_config", return_value=_smtp()),
            patch("jai.services.email._send_mail", new_callable=AsyncMock),
        ):
            resp = await db_client.post(
                f"/api/v1/invoices/{invoice_id}/send",
                json={"to": "customer@example.com"},
            )
        assert resp.status_code == 400, resp.text
        assert "issue" in resp.json()["detail"].lower()

        # No log row written for the rejected draft.
        list_resp = await db_client.get(f"/api/v1/invoices/{invoice_id}/emails")
        assert list_resp.json()["total"] == 0

        # Issue it → send now works.
        await _issue_invoice(db_client, invoice_id)
        with (
            patch("jai.services.email._get_smtp_config", return_value=_smtp()),
            patch("jai.services.email._send_mail", new_callable=AsyncMock),
        ):
            resp2 = await db_client.post(
                f"/api/v1/invoices/{invoice_id}/send",
                json={"to": "customer@example.com"},
            )
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["status"] == "SENT"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_si1_quote_actual_transport_saves_copy_and_lists_result(db_client):
    from unittest.mock import MagicMock

    from jai.schemas.setting import SentCopySettings

    await _full_auth(db_client)
    await _setup_company(db_client)
    vat = await _setup_vat(db_client)
    customer = await _create_customer(db_client)
    quote = await _create_quote(db_client, customer, vat["treatment_id"], vat["rate_id"])
    cfg = _smtp()
    cfg.sent_copy = SentCopySettings(enabled=True, host="imap.example.test")
    imap = MagicMock()
    imap.login.return_value = ("OK", [])
    imap.capability.return_value = ("OK", [b"IMAP4rev1"])
    imap.list.return_value = ("OK", [b'(\\Sent) "/" Sent'])
    imap.append.return_value = ("OK", [])
    with (
        patch("jai.services.email._get_smtp_config", return_value=cfg),
        patch("aiosmtplib.send", new_callable=AsyncMock) as smtp,
        patch("jai.services.sent_copy.imaplib.IMAP4_SSL", return_value=imap),
    ):
        response = await db_client.post(
            f"/api/v1/quotes/{quote}/send", json={"to": "to@example.com", "cc": "cc@example.com"}
        )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "SENT" and data["sent_copy_status"] == "SAVED"
    assert data["sent_copy_saved_at"] and data["sent_copy_error_code"] is None
    assert smtp.call_args.args[0] == imap.append.call_args.args[3]
    items = (await db_client.get(f"/api/v1/quotes/{quote}/emails")).json()["items"]
    assert items[0]["sent_copy_status"] == "SAVED"

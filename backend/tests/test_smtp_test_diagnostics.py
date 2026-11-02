"""Test-email diagnostics without real SMTP, IMAP, or database access."""

from __future__ import annotations

import io
import logging
import socket
import ssl
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiosmtplib import errors as smtp_errors
from fastapi import HTTPException

from jai.api import settings as settings_api
from jai.schemas.email_log import SentCopyResult
from jai.services import email


@pytest.fixture
def formatted_log(monkeypatch):
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(logging.Formatter())
    emit = MagicMock(wraps=handler.emit)
    monkeypatch.setattr(handler, "emit", emit)
    monkeypatch.setattr(email.logger, "handlers", [handler])
    monkeypatch.setattr(email.logger, "propagate", False)
    monkeypatch.setattr(email.logger, "level", logging.WARNING)
    monkeypatch.setattr(email.logger, "disabled", False)
    try:
        yield output, emit
    finally:
        handler.close()


@pytest.mark.parametrize("failure,code", [
    (smtp_errors.SMTPAuthenticationError(535, "secret-password"), "SMTP_AUTH_FAILED"),
    (smtp_errors.SMTPSenderRefused(550, "secret-password", "private@example.com"),
     "SMTP_SENDER_REJECTED"),
    (smtp_errors.SMTPRecipientRefused(550, "secret-password", "private@example.com"),
     "SMTP_RECIPIENT_REJECTED"),
    (smtp_errors.SMTPRecipientsRefused([
        smtp_errors.SMTPRecipientRefused(550, "secret-password", "private@example.com"),
    ]), "SMTP_RECIPIENT_REJECTED"),
    (smtp_errors.SMTPConnectTimeoutError("secret-password"), "SMTP_TIMEOUT"),
    (smtp_errors.SMTPReadTimeoutError("secret-password"), "SMTP_TIMEOUT"),
    (TimeoutError("secret-password"), "SMTP_TIMEOUT"),
    (ssl.SSLCertVerificationError("secret-password"), "SMTP_TLS_FAILED"),
    (smtp_errors.SMTPConnectError("secret-password"), "SMTP_CONNECTION_FAILED"),
    (smtp_errors.SMTPServerDisconnected("secret-password"), "SMTP_CONNECTION_FAILED"),
    (socket.gaierror("secret-password"), "SMTP_CONNECTION_FAILED"),
    (smtp_errors.SMTPNotSupported("secret-password"), "SMTP_FEATURE_UNSUPPORTED"),
    (smtp_errors.SMTPDataError(554, "secret-password"), "SMTP_SERVER_REJECTED"),
    (ValueError("secret-password"), "SMTP_MESSAGE_INVALID"),
    (RuntimeError("secret-password"), "SMTP_SEND_FAILED"),
])
async def test_fixed_code_in_default_log_and_generic_api_error(
    failure, code, formatted_log, monkeypatch,
):
    send = AsyncMock(side_effect=failure)
    monkeypatch.setattr(email, "send_test_email", send)
    user = SimpleNamespace(role="owner", email="owner@example.com")
    session = object()
    with pytest.raises(HTTPException) as caught:
        await settings_api.test_smtp_settings(user, session)
    assert caught.value.status_code == 400
    assert caught.value.detail == "SMTP_SEND_FAILED"
    send.assert_awaited_once_with(session, user.email)
    output, emit = formatted_log
    assert output.getvalue() == f"SMTP test send failed: {code}\n"
    record = emit.call_args.args[0]
    assert record.smtp_error_code == code
    assert record.exc_info is None
    assert "secret-password" not in output.getvalue()
    assert "private@example.com" not in output.getvalue()


def test_wrapped_tls_failure_preserves_category_without_logging_cause(formatted_log):
    error = smtp_errors.SMTPConnectError("secret-wrapper")
    error.__cause__ = ssl.SSLCertVerificationError("secret-certificate")
    email.log_smtp_test_failure(error)
    output, _ = formatted_log
    assert output.getvalue() == "SMTP test send failed: SMTP_TLS_FAILED\n"


@pytest.mark.parametrize("copy_status", ["SAVED", "FAILED"])
async def test_smtp_success_stays_success_without_failure_log(
    copy_status, formatted_log, monkeypatch,
):
    result = SentCopyResult(
        sent_copy_status=copy_status,
        sent_copy_error_code="IMAP_AUTH_FAILED" if copy_status == "FAILED" else None,
    )
    send = AsyncMock(return_value=result)
    monkeypatch.setattr(email, "send_test_email", send)
    user = SimpleNamespace(role="owner", email="owner@example.com")
    response = await settings_api.test_smtp_settings(user, object())
    assert response.status == "sent"
    assert response.sent_copy_status == copy_status
    send.assert_awaited_once()
    output, emit = formatted_log
    assert output.getvalue() == ""
    emit.assert_not_called()

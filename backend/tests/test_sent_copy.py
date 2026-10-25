"""SI1 transport invariants with no real SMTP/IMAP network connections."""

from __future__ import annotations

import ssl
import uuid
from datetime import UTC, datetime
from email import message_from_bytes
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from jai.models._enums import EmailRelatedType, EmailStatus
from jai.schemas.setting import SentCopySettings, SmtpSettings
from jai.services import email, sent_copy


def config(**overrides):
    return SmtpSettings(
        host="smtp.test",
        username="secret-user",
        password="secret-password",
        from_email="from@example.com",
        sent_copy=SentCopySettings(enabled=True, host="imap.test", **overrides),
    )


def client(folders=None):
    mock = MagicMock()
    mock.capability.return_value = ("OK", [b"IMAP4rev1"])
    mock.list.return_value = ("OK", folders if folders is not None else [b'(\\Sent) "/" "Sent"'])
    mock.login.return_value = ("OK", [])
    mock.starttls.return_value = ("OK", [])
    mock.append.return_value = ("OK", [])
    return mock


@pytest.mark.parametrize(
    ("folders", "folder", "expected"),
    [
        ([b'(\\Sent) "/" "Sent Items"'], None, '"Sent Items"'),
        ([b'() "/" Sent'], None, '"Sent"'),
        ([b'(\\NoSelect \\Sent) "/" Parent', b'() "/" Sent'], None, '"Sent"'),
        ([b'(\\Sent) "/" Other', b'() "/" "My Sent"'], "My Sent", '"My Sent"'),
        ([b'() "/" "&XfJT0ZAB-"'], "已发送", '"&XfJT0ZAB-"'),
        ([(b'() "/" {7}', b"My Sent")], "My Sent", '"My Sent"'),
        ([b'() "/" "A\\"B\\\\C"'], 'A"B\\C', '"A\\"B\\\\C"'),
    ],
)
def test_folder_resolution_and_quoting(folders, folder, expected):
    assert sent_copy._folder(client(folders), folder).decode() == expected


@pytest.mark.parametrize(
    ("folders", "folder", "code"),
    [
        ([], None, "IMAP_FOLDER_MISSING"),
        ([b'(\\NoSelect \\Sent) "/" Sent'], None, "IMAP_FOLDER_MISSING"),
        ([b'(\\Sent) "/" One', b'(\\Sent) "/" Two'], None, "IMAP_FOLDER_AMBIGUOUS"),
        ([b'() "/" Sent'], "Missing", "IMAP_FOLDER_MISSING"),
        ([b'() "/" Sent', b'() "/" Sent'], None, "IMAP_FOLDER_AMBIGUOUS"),
        ([b"bad-secret-server-response"], None, "IMAP_FOLDER_INVALID"),
    ],
)
def test_folder_failures(folders, folder, code):
    with pytest.raises(sent_copy.CopyFailure, match=code):
        sent_copy._folder(client(folders), folder)


@pytest.mark.parametrize("security", ["SSL_TLS", "STARTTLS"])
def test_secure_transport_and_cleanup(security):
    imap = client()
    now = datetime.now(UTC)
    with (
        patch.object(sent_copy.imaplib, "IMAP4_SSL", return_value=imap) as implicit,
        patch.object(sent_copy.imaplib, "IMAP4", return_value=imap) as explicit,
    ):
        result = sent_copy._append(config(security=security, port=1143), b"full-mail\r\n", now)
    assert result.sent_copy_status == "SAVED"
    assert result.sent_copy_saved_at is not None
    constructor = implicit if security == "SSL_TLS" else explicit
    assert constructor.call_args.args == ("imap.test", 1143)
    assert constructor.call_args.kwargs["timeout"] == 15
    context = (
        implicit.call_args.kwargs["ssl_context"]
        if security == "SSL_TLS"
        else imap.starttls.call_args.kwargs["ssl_context"]
    )
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    if security == "STARTTLS":
        assert imap.method_calls[0][0] == "starttls"
        imap.starttls.assert_called_once()
    imap.append.assert_called_once_with(
        '"Sent"', r"\Seen", sent_copy.imaplib.Time2Internaldate(now), b"full-mail\r\n"
    )
    imap.logout.assert_called_once()
    imap.create.assert_not_called()


@pytest.mark.parametrize(
    ("method", "failure", "code"),
    [
        ("login", RuntimeError("secret-password"), "IMAP_AUTH_FAILED"),
        ("login", ("NO", [b"secret-password"]), "IMAP_AUTH_FAILED"),
        ("list", ("NO", [b"secret-password"]), "IMAP_FOLDER_FAILED"),
        ("append", ("NO", [b"secret-password"]), "IMAP_APPEND_FAILED"),
        ("append", TimeoutError("secret-password"), "IMAP_TIMEOUT"),
        ("append", RuntimeError("secret-password"), "IMAP_APPEND_FAILED"),
        ("starttls", ssl.SSLError("secret-password"), "IMAP_TLS_FAILED"),
        ("starttls", ("NO", [b"secret-password"]), "IMAP_TLS_FAILED"),
    ],
)
def test_safe_failure_codes_and_no_retry(method, failure, code, caplog):
    imap = client()
    if isinstance(failure, Exception):
        getattr(imap, method).side_effect = failure
    else:
        getattr(imap, method).return_value = failure
    with patch.object(sent_copy.imaplib, "IMAP4", return_value=imap):
        result = sent_copy._append(config(security="STARTTLS"), b"SECRET MIME", datetime.now(UTC))
    assert result.sent_copy_status == "FAILED"
    assert result.sent_copy_error_code == code
    assert imap.append.call_count <= 1
    imap.logout.assert_called_once()
    assert "secret-password" not in caplog.text and "SECRET MIME" not in caplog.text


def test_acknowledged_append_survives_cleanup_failure():
    imap = client()
    imap.logout.side_effect = RuntimeError("secret")
    imap.shutdown.side_effect = RuntimeError("secret")
    with patch.object(sent_copy.imaplib, "IMAP4_SSL", return_value=imap):
        result = sent_copy._append(config(), b"mail", datetime.now(UTC))
    assert result.sent_copy_status == "SAVED"
    imap.shutdown.assert_called_once()


@pytest.mark.parametrize(
    "exception,code",
    [
        (TimeoutError("secret"), "IMAP_TIMEOUT"),
        (ssl.SSLError("secret"), "IMAP_TLS_FAILED"),
        (OSError("secret"), "IMAP_CONNECTION_FAILED"),
    ],
)
def test_connection_failures(exception, code):
    with patch.object(sent_copy.imaplib, "IMAP4_SSL", side_effect=exception):
        assert sent_copy._append(config(), b"mail", datetime.now(UTC)).sent_copy_error_code == code


@pytest.mark.asyncio
async def test_same_complete_smtp_bytes_and_append():
    imap = client()
    with (
        patch("aiosmtplib.send", new_callable=AsyncMock) as smtp,
        patch.object(sent_copy.imaplib, "IMAP4_SSL", return_value=imap),
    ):
        result = await email._send_mail(
            config(),
            "to@example.com",
            "Subject",
            "<p>Body 中文</p>",
            ["cc@example.com"],
            b"%PDF-EXACT",
            "中文.pdf",
        )
    assert result.sent_copy_status == "SAVED"
    raw = smtp.call_args.args[0]
    assert isinstance(raw, bytes) and raw == imap.append.call_args.args[3]
    assert b"\n" not in raw.replace(b"\r\n", b"")
    parsed = message_from_bytes(raw)
    assert parsed["Date"] and parsed["Message-ID"]
    assert parsed["To"] == "to@example.com" and parsed["Cc"] == "cc@example.com"
    assert smtp.call_args.kwargs["sender"] == "from@example.com"
    assert smtp.call_args.kwargs["recipients"] == ["to@example.com", "cc@example.com"]
    assert (
        next(p for p in parsed.walk() if p.get_content_type() == "application/pdf").get_payload(
            decode=True
        )
        == b"%PDF-EXACT"
    )
    assert (
        "Body 中文"
        in next(p for p in parsed.walk() if p.get_content_type() == "text/html")
        .get_payload(decode=True)
        .decode()
    )


@pytest.mark.asyncio
async def test_smtp_failure_never_attempts_copy():
    with (
        patch("aiosmtplib.send", side_effect=RuntimeError("secret")),
        patch("jai.services.email.save_sent_copy", new_callable=AsyncMock) as copy,
    ):
        with pytest.raises(RuntimeError):
            await email._send_mail(config(), "to@example.com", "S", "B")
    copy.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_and_missing_credentials():
    cfg = config()
    cfg.sent_copy.enabled = False
    with patch.object(sent_copy.imaplib, "IMAP4_SSL") as constructor:
        assert (
            await sent_copy.save_sent_copy(cfg, b"mail", datetime.now(UTC))
        ).sent_copy_status == "NOT_ATTEMPTED"
        cfg.sent_copy.enabled = True
        cfg.password = ""
        assert (
            await sent_copy.save_sent_copy(cfg, b"mail", datetime.now(UTC))
        ).sent_copy_error_code == "IMAP_CONFIGURATION_FAILED"
    constructor.assert_not_called()


@pytest.mark.parametrize(
    "values",
    [
        dict(enabled=True),
        dict(port=0),
        dict(port=65536),
        dict(security="PLAIN"),
        dict(folder="Sent\r\nINJECT"),
    ],
)
def test_settings_validation(values):
    with pytest.raises(ValidationError):
        SentCopySettings(**values)


def test_legacy_settings_defaults_and_normalization():
    assert not SmtpSettings().sent_copy.enabled
    assert SentCopySettings(folder="  ").folder is None
    assert SentCopySettings(folder=" My Sent ").folder == "My Sent"
    assert SentCopySettings(security="STARTTLS", port=2993).port == 2993


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["STANDARD", "ADVANCE", "FINAL", "CREDIT_NOTE", "quote", "receipt", "refund"]
)
async def test_all_business_kinds_keep_smtp_success_on_copy_failure(kind):
    session = AsyncMock()
    session.add = MagicMock()
    doc = MagicMock(id=uuid.uuid4(), kind=kind)
    doc.kind = kind
    company = MagicMock(id=uuid.uuid4())
    customer = MagicMock()
    imap = client()
    imap.append.side_effect = TimeoutError("secret-password")
    related = EmailRelatedType.QUOTE if kind == "quote" else EmailRelatedType.INVOICE
    if kind == "receipt":
        related = EmailRelatedType.INVOICE
    if kind == "refund":
        related = EmailRelatedType.REFUND
    artifact = uuid.uuid4()
    with (
        patch("jai.services.email._get_smtp_config", return_value=config()),
        patch("jai.services.settings.get_setting", return_value=None),
        patch("aiosmtplib.send", new_callable=AsyncMock) as smtp,
        patch.object(sent_copy.imaplib, "IMAP4_SSL", return_value=imap),
    ):
        log = await email.send_document_email(
            session=session,
            related_type=related,
            doc=doc,
            company=company,
            customer=customer,
            to="to@example.com",
            cc=["cc@example.com"],
            locale="en",
            subject="subject",
            body="body",
            pdf_bytes=b"%PDF",
            filename="doc.pdf",
            artifact_id=artifact,
            document_kind=kind if kind in {"STANDARD", "ADVANCE", "FINAL", "CREDIT_NOTE"} else None,
        )
    smtp.assert_called_once()
    assert log.status == EmailStatus.SENT and log.sent_at is not None
    assert log.artifact_id == artifact and log.error_message is None
    assert log.sent_copy_status == "FAILED" and log.sent_copy_error_code == "IMAP_TIMEOUT"


@pytest.mark.asyncio
async def test_system_senders_use_shared_path_and_reset_has_no_business_log(caplog):
    session = AsyncMock()
    session.add = MagicMock()
    imap = client()
    imap.append.side_effect = TimeoutError("secret-password")
    with (
        patch("jai.services.email._get_smtp_config", return_value=config()),
        patch("aiosmtplib.send", new_callable=AsyncMock) as smtp,
        patch.object(sent_copy.imaplib, "IMAP4_SSL", return_value=imap),
    ):
        result = await email.send_test_email(session, "to@example.com")
        await email.send_password_reset_email(
            session, "to@example.com", "https://example.com/reset?token=SECRET"
        )
    assert result.sent_copy_status == "FAILED"
    assert smtp.call_count == 2 and imap.append.call_count == 2
    session.add.assert_not_called()
    assert "SECRET" not in caplog.text and "secret-password" not in caplog.text
    assert all(record.sent_copy_error_code == "IMAP_TIMEOUT" for record in caplog.records)


@pytest.mark.asyncio
async def test_environment_fallback_preserves_saved_imap():
    settings = MagicMock(
        smtp_host="env.smtp",
        smtp_port=587,
        smtp_username="env-user",
        smtp_password="env-password",
        smtp_from_email="from@example.com",
        smtp_from_name="",
        smtp_use_tls=True,
        smtp_use_ssl=False,
    )
    stored = config()
    stored.host = ""
    with (
        patch("jai.services.settings.get_setting", return_value=stored),
        patch("jai.services.email.get_settings", return_value=settings),
    ):
        effective = await email._get_smtp_config(AsyncMock())
    assert effective.host == "env.smtp"
    assert effective.username == "env-user" and effective.password == "env-password"
    assert effective.sent_copy == stored.sent_copy


@pytest.mark.asyncio
async def test_worker_dispatch_failure_is_still_only_copy_failure():
    with patch("jai.services.sent_copy.asyncio.to_thread", side_effect=RuntimeError("secret")):
        result = await sent_copy.save_sent_copy(config(), b"SECRET", datetime.now(UTC))
    assert result.sent_copy_status == "FAILED" and result.sent_copy_error_code == "IMAP_COPY_FAILED"


def test_failed_log_reconstruction_includes_not_attempted():
    log = MagicMock()
    values = email.failed_email_log_values(log)
    assert values["sent_copy_status"] == "NOT_ATTEMPTED"
    assert values["sent_copy_error_code"] is None and values["sent_copy_saved_at"] is None
    assert values["artifact_id"] is None and values["sent_at"] is None


def test_nonexistent_mailbox_cannot_be_created_by_append():
    imap = client([b'(\\NonExistent \\Sent) "/" Sent'])
    with pytest.raises(sent_copy.CopyFailure, match="IMAP_FOLDER_MISSING"):
        sent_copy._folder(imap, None)
    imap.append.assert_not_called()


@pytest.mark.asyncio
async def test_smtp_acceptance_timestamp_precedes_copy_wait():
    from jai.schemas.email_log import SentCopyResult
    copy_started = None

    async def copy(*args):
        nonlocal copy_started
        copy_started = datetime.now(UTC)
        return SentCopyResult(sent_copy_status='FAILED', sent_copy_error_code='IMAP_TIMEOUT')

    with patch('aiosmtplib.send', new_callable=AsyncMock), \
         patch('jai.services.email.save_sent_copy', side_effect=copy):
        result = await email._send_mail(config(), 'to@example.com', 'Subject', 'body')
    assert result.smtp_sent_at <= copy_started
    assert result.sent_copy_status == 'FAILED'


@pytest.mark.parametrize("count", [0, 1, 2])
def test_special_use_return_requested_before_fallback(count):
    imap = object.__new__(sent_copy.imaplib.IMAP4)
    imap.debug = 0
    imap.capability = MagicMock(return_value=("OK", [b"IMAP4rev1 SPECIAL-USE"]))
    ordinary = [b'() "/" "SentMail"', b'() "/" "Sent"']
    extended = [b'() "/" "Sent"'] + [
        f'(\\Sent) "/" "Special{i}"'.encode() for i in range(count)
    ]

    def command(*args):
        imap.untagged_responses = {"LIST": extended if "SPECIAL-USE" in str(args) else ordinary}
        return "OK", [b"done"]

    with patch.object(imap, "_simple_command", side_effect=command) as wire:
        if count == 2:
            with pytest.raises(sent_copy.CopyFailure, match="IMAP_FOLDER_AMBIGUOUS"):
                sent_copy._folder(imap, None)
        else:
            assert sent_copy._folder(imap, None) == (b'"Special0"' if count else b'"Sent"')
    wire.assert_called_once_with("LIST", '""', '* RETURN (SPECIAL-USE)')


class ScriptedIMAP(sent_copy.imaplib.IMAP4):
    """Real stdlib commands/parser with only the server wire held in memory."""

    def open(self, host="", port=143, timeout=None):
        self.host, self.port = host, port
        self.replies = [b"* OK scripted IMAP server\r\n"]
        self.commands = []
        self.authenticated = False
        self.special_count = 1
        self.include_fallback = True
        self.post_auth_capabilities = b"IMAP4rev1 SPECIAL-USE"
        self.capability_failure = None
        self.closed = False

    def readline(self):
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def send(self, wire):
        tag, command, *_ = wire.rstrip(b"\r\n").split(b" ")
        self.commands.append(wire.split(b" ", 1)[1].rstrip(b"\r\n"))
        if command == b"CAPABILITY":
            if self.authenticated and self.capability_failure:
                if isinstance(self.capability_failure, Exception):
                    self.replies.append(self.capability_failure)
                    return
                self.replies += [tag + b" " + self.capability_failure + b" secret-response\r\n"]
                return
            caps = self.post_auth_capabilities if self.authenticated else b"IMAP4rev1 AUTH=PLAIN"
            self.replies += [b"* CAPABILITY " + caps + b"\r\n", tag + b" OK caps\r\n"]
        elif command == b"LOGIN":
            self.authenticated = True
            self.replies += [tag + b" OK [CAPABILITY " + self.post_auth_capabilities
                             + b"] logged in\r\n"]
        elif command == b"LIST":
            for index in range(self.special_count):
                flag = b"\\Sent" if b"RETURN (SPECIAL-USE)" in wire else b""
                self.replies += [b"* LIST (" + flag + b') "/" "Special'
                                 + str(index).encode() + b'"\r\n']
            if self.include_fallback:
                self.replies += [b'* LIST () "/" "Sent"\r\n']
            self.replies += [tag + b" OK list complete\r\n"]
        elif command == b"LOGOUT":
            self.replies += [b"* BYE logged out\r\n", tag + b" OK logout\r\n"]
        else:
            raise AssertionError(wire)

    def shutdown(self):
        self.closed = True


@pytest.mark.parametrize("security", ["SSL_TLS", "STARTTLS"])
@pytest.mark.parametrize("count,include_fallback,expected", [
    (1, True, '"Special0"'),
    (1, False, '"Special0"'),
    (2, True, "IMAP_FOLDER_AMBIGUOUS"),
    (2, False, "IMAP_FOLDER_AMBIGUOUS"),
    (0, True, '"Sent"'),
    (0, False, "IMAP_FOLDER_MISSING"),
])
def test_discovery_uses_real_post_login_capabilities(security, count, include_fallback, expected):
    imap = ScriptedIMAP()
    imap.special_count = count
    imap.include_fallback = include_fallback
    with (
        patch.object(sent_copy.imaplib, "IMAP4_SSL", return_value=imap),
        patch.object(sent_copy.imaplib, "IMAP4", return_value=imap),
        patch.object(imap, "starttls", return_value=("OK", [])),
        patch.object(imap, "append", return_value=("OK", [])) as append,
    ):
        result = sent_copy._append(config(security=security), b"mail", datetime.now(UTC))
    # Real login() and capability() both leave the initial cached tuple stale.
    assert imap.capabilities == ("IMAP4REV1", "AUTH=PLAIN")
    assert [command.split(b" ")[0] for command in imap.commands] == [
        b"CAPABILITY", b"LOGIN", b"CAPABILITY", b"LIST", b"LOGOUT",
    ]
    assert imap.commands[3] == b'LIST "" * RETURN (SPECIAL-USE)'
    if expected.startswith("IMAP_"):
        assert result.sent_copy_status == "FAILED" and result.sent_copy_error_code == expected
        append.assert_not_called()
    else:
        assert result.sent_copy_status == "SAVED"
        append.assert_called_once()
        assert append.call_args.args[0] == expected
    assert imap.closed


@pytest.mark.parametrize("failure,code", [
    (b"NO", "IMAP_FOLDER_FAILED"),
    (b"BAD", "IMAP_FOLDER_FAILED"),
    (TimeoutError("secret-response"), "IMAP_TIMEOUT"),
])
def test_real_post_login_capability_failure_is_safe_and_not_retried(failure, code, caplog):
    imap = ScriptedIMAP()
    imap.capability_failure = failure
    with (
        patch.object(sent_copy.imaplib, "IMAP4_SSL", return_value=imap),
        patch.object(imap, "append") as append,
    ):
        result = sent_copy._append(config(), b"SECRET MIME", datetime.now(UTC))
    assert result.sent_copy_status == "FAILED" and result.sent_copy_error_code == code
    assert [command.split(b" ")[0] for command in imap.commands] == [
        b"CAPABILITY", b"LOGIN", b"CAPABILITY", b"LOGOUT",
    ]
    append.assert_not_called()
    assert imap.closed
    assert "secret-response" not in caplog.text and "SECRET MIME" not in caplog.text


def test_real_post_login_without_special_use_uses_ordinary_list():
    imap = ScriptedIMAP()
    imap.post_auth_capabilities = b"IMAP4rev1"
    with (
        patch.object(sent_copy.imaplib, "IMAP4_SSL", return_value=imap),
        patch.object(imap, "append", return_value=("OK", [])) as append,
    ):
        result = sent_copy._append(config(), b"mail", datetime.now(UTC))
    assert result.sent_copy_status == "SAVED"
    assert imap.commands[3] == b'LIST "" *'
    assert append.call_args.args[0] == '"Sent"'
    assert imap.closed


@pytest.mark.parametrize("advertised", [[], [None], [b""], ["secret-response"]])
def test_unusable_capability_response_fails_closed(advertised):
    imap = client()
    imap.capability.return_value = ("OK", advertised)
    with patch.object(sent_copy.imaplib, "IMAP4_SSL", return_value=imap):
        result = sent_copy._append(config(), b"mail", datetime.now(UTC))
    assert result.sent_copy_error_code == "IMAP_FOLDER_FAILED"
    imap.capability.assert_called_once()
    imap.list.assert_not_called()
    imap.append.assert_not_called()
    imap.logout.assert_called_once()


def test_explicit_folder_does_not_require_capability_refresh():
    imap = client()
    imap.capability.side_effect = AssertionError("explicit folder needs no discovery")
    with patch.object(sent_copy.imaplib, "IMAP4_SSL", return_value=imap):
        result = sent_copy._append(config(folder="Sent"), b"mail", datetime.now(UTC))
    assert result.sent_copy_status == "SAVED"
    imap.capability.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["from", "to", "cc"])
@pytest.mark.parametrize("address", ["user@bücher.de", "büro@example.com"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("supported", [False, True])
async def test_raw_smtp_international_envelopes(field, address, enabled, supported):
    import aiosmtplib
    from aiosmtplib.response import SMTPResponse

    cfg = config()
    cfg.sent_copy.enabled = enabled
    if field == "from":
        cfg.from_email = address
    to = address if field == "to" else "to@example.com"
    cc = [address if field == "cc" else "cc@example.com"]
    imap = client()
    commands = []
    payloads = []

    async def enter(smtp):
        return smtp

    async def exit(smtp, *args):
        return None

    async def command(smtp, *args, **kwargs):
        commands.append(args)
        return SMTPResponse(250, "OK")

    async def data(smtp, raw, **kwargs):
        payloads.append(raw)
        return SMTPResponse(250, "OK")

    # Keep real send -> sendmail -> mail/rcpt, including encoding and checks.
    # Only connection lifecycle, server EHLO and network I/O are replaced.
    with (
        patch.object(aiosmtplib.SMTP, "__aenter__", enter),
        patch.object(aiosmtplib.SMTP, "__aexit__", exit),
        patch.object(aiosmtplib.SMTP, "_ehlo_or_helo_if_needed", new_callable=AsyncMock),
        patch.object(
            aiosmtplib.SMTP, "supports_extension",
            side_effect=lambda ext: supported and ext.lower() in {"smtputf8", "8bitmime"},
        ),
        patch.object(aiosmtplib.SMTP, "execute_command", command),
        patch.object(aiosmtplib.SMTP, "data", data),
        patch.object(sent_copy.imaplib, "IMAP4_SSL", return_value=imap),
    ):
        if supported:
            result = await email._send_mail(
                cfg, to, "Subject 中文", "Body 中文", cc, b"%PDF", "doc.pdf"
            )
            assert result.sent_copy_status == ("SAVED" if enabled else "NOT_ATTEMPTED")
        else:
            with pytest.raises(aiosmtplib.SMTPNotSupported):
                await email._send_mail(cfg, to, "Subject", "Body", cc)
            assert not commands and not payloads
    if supported:
        assert commands[0] == (b"MAIL", b"FROM:<" + cfg.from_email.encode() + b">", b"SMTPUTF8")
        assert commands[1:3] == [
            (b"RCPT", b"TO:<" + recipient.encode() + b">") for recipient in [to, *cc]
        ]
        assert len(payloads) == 1
        parsed = message_from_bytes(payloads[0], policy=email.policy.SMTPUTF8)
        assert address in str(parsed[{"from": "From", "to": "To", "cc": "Cc"}[field]])
        assert payloads[0].count(b"Message-ID:") == 1
        if enabled:
            assert payloads[0] == imap.append.call_args.args[3]
    if not supported or not enabled:
        imap.append.assert_not_called()

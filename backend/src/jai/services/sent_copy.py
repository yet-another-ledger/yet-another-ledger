"""Bounded, best-effort IMAP APPEND. Never retries or stores MIME bytes."""

from __future__ import annotations

import asyncio
import base64
import imaplib
import logging
import re
import ssl
from collections.abc import Sequence
from datetime import UTC, datetime

from jai.schemas.email_log import SentCopyResult
from jai.schemas.setting import SmtpSettings

logger = logging.getLogger("jai.email")
_TIMEOUT = 15
_LIST = re.compile(rb'^\(([^)]*)\)\s+(?:NIL|"(?:[^"\\]|\\.)*")\s+(.+)$')


class CopyFailure(Exception):
    """Only a stable internal code, never a server response."""


def _utf7(value: str) -> bytes:
    result = bytearray()
    pending = ""

    def flush() -> None:
        nonlocal pending
        if pending:
            result.extend(
                b"&"
                + base64.b64encode(pending.encode("utf-16-be")).rstrip(b"=").replace(b"/", b",")
                + b"-"
            )
            pending = ""

    for char in value:
        if " " <= char <= "~":
            flush()
            result.extend(b"&-" if char == "&" else char.encode("ascii"))
        else:
            pending += char
    flush()
    return bytes(result)


def _quote(value: bytes) -> bytes:
    if any(c in value for c in (b"\r", b"\n", b"\x00")):
        raise CopyFailure("IMAP_FOLDER_INVALID")
    return b'"' + value.replace(b"\\", b"\\\\").replace(b'"', b'\\"') + b'"'


def _mailboxes(
    data: Sequence[bytes | tuple[bytes, bytes] | None],
) -> list[tuple[set[bytes], bytes]]:
    result = []
    for entry in data:
        if entry is None or entry == b"":
            continue
        header = entry[0] if isinstance(entry, tuple) else entry
        match = _LIST.match(header)
        if not match:
            raise CopyFailure("IMAP_FOLDER_INVALID")
        flags = set(match[1].lower().split())
        name = match[2]
        if isinstance(entry, tuple):
            name = entry[1]
        elif name.startswith(b'"') and name.endswith(b'"'):
            name = re.sub(rb"\\(.)", rb"\1", name[1:-1])
        if not flags.intersection({b"\\noselect", b"\\nonexistent"}):
            result.append((flags, name))
    return result


def _folder(client: imaplib.IMAP4, configured: str | None) -> bytes:
    capabilities: set[bytes] = set()
    if not configured:
        # LOGIN does not refresh imaplib's cached capabilities, even when its
        # response advertises new ones. Read the post-authentication response
        # directly: capability() also leaves that cached attribute unchanged.
        status, advertised = client.capability()
        if status != "OK" or not advertised or any(not isinstance(c, bytes) for c in advertised):
            raise CopyFailure("IMAP_FOLDER_FAILED")
        capabilities = set(b" ".join(advertised).upper().split())
        if not capabilities:
            raise CopyFailure("IMAP_FOLDER_FAILED")
    # LIST all existing folders so explicit names cannot accidentally create one.
    if b"SPECIAL-USE" in capabilities:
        # RFC 6154: ordinary LIST may omit special-use flags. RETURN preserves
        # all folders (including the literal Sent fallback) and requests flags.
        status, data = client.list('""', '* RETURN (SPECIAL-USE)')
    else:
        status, data = client.list()
    if status != "OK":
        raise CopyFailure("IMAP_FOLDER_FAILED")
    mailboxes = _mailboxes(data)
    if configured:
        matches = [name for _, name in mailboxes if name == _utf7(configured)]
    else:
        matches = [name for flags, name in mailboxes if b"\\sent" in flags]
        if not matches:
            matches = [name for _, name in mailboxes if name == b"Sent"]
    if len(matches) != 1:
        raise CopyFailure("IMAP_FOLDER_AMBIGUOUS" if len(matches) > 1 else "IMAP_FOLDER_MISSING")
    return _quote(matches[0])


def _append(cfg: SmtpSettings, raw: bytes, date: datetime) -> SentCopyResult:
    settings = cfg.sent_copy
    client: imaplib.IMAP4 | None = None
    stage = "IMAP_CONNECTION_FAILED"
    try:
        if not cfg.username or not cfg.password or not settings.host:
            raise CopyFailure("IMAP_CONFIGURATION_FAILED")
        context = ssl.create_default_context()
        if settings.security == "SSL_TLS":
            client = imaplib.IMAP4_SSL(
                settings.host, settings.port, ssl_context=context, timeout=_TIMEOUT
            )
        else:
            client = imaplib.IMAP4(settings.host, settings.port, timeout=_TIMEOUT)
            stage = "IMAP_TLS_FAILED"
            if client.starttls(ssl_context=context)[0] != "OK":
                raise CopyFailure(stage)
        stage = "IMAP_AUTH_FAILED"
        if client.login(cfg.username, cfg.password)[0] != "OK":
            raise CopyFailure(stage)
        stage = "IMAP_FOLDER_FAILED"
        folder = _folder(client, settings.folder)
        stage = "IMAP_APPEND_FAILED"
        if (
            client.append(folder.decode("ascii"), r"\Seen", imaplib.Time2Internaldate(date), raw)[0]
            != "OK"
        ):
            raise CopyFailure(stage)
        return SentCopyResult(sent_copy_status="SAVED", sent_copy_saved_at=datetime.now(UTC))
    except Exception as exc:
        code = (
            str(exc)
            if isinstance(exc, CopyFailure)
            else (
                "IMAP_TIMEOUT"
                if isinstance(exc, TimeoutError)
                else "IMAP_TLS_FAILED"
                if isinstance(exc, ssl.SSLError)
                else stage
            )
        )
        logger.warning("Sent copy failed: %s", code, extra={"sent_copy_error_code": code})
        return SentCopyResult(sent_copy_status="FAILED", sent_copy_error_code=code)
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                # LOGOUT failure cannot change an acknowledged APPEND result.
                try:
                    client.shutdown()
                except Exception:
                    pass


async def save_sent_copy(cfg: SmtpSettings, raw: bytes, date: datetime) -> SentCopyResult:
    if not cfg.sent_copy.enabled:
        return SentCopyResult()
    try:
        return await asyncio.to_thread(_append, cfg, raw, date)
    except Exception:
        logger.warning(
            "Sent copy failed: %s",
            "IMAP_COPY_FAILED",
            extra={"sent_copy_error_code": "IMAP_COPY_FAILED"},
        )
        return SentCopyResult(sent_copy_status="FAILED", sent_copy_error_code="IMAP_COPY_FAILED")

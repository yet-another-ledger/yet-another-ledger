#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["mcp==2.2.0", "httpx==0.28.1"]
# ///
"""Local stdio MCP adapter: stream approved receipt attachments to JAI.

No file bytes, Base64, bearer token, or arbitrary server error enters a tool
response. Configure the cache root explicitly; never approve HOME or '/'.
"""

from __future__ import annotations

import os
import stat
import sys
import uuid
from contextlib import ExitStack
from functools import partial
from pathlib import Path
from typing import Annotated, Any, BinaryIO, Literal
from urllib.parse import urlsplit

import anyio
import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator


class AdapterSettings(BaseModel):
    cache_root: Path
    base_url: str
    token: SecretStr
    max_file_bytes: int = Field(default=10 * 1024 * 1024, ge=1, le=100 * 1024 * 1024)

    @field_validator("base_url")
    @classmethod
    def loopback_only(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
        ):
            raise ValueError("An explicit loopback JAI base URL is required.")
        return value.rstrip("/")

    @field_validator("cache_root")
    @classmethod
    def bounded_root(cls, value: Path) -> Path:
        root = value.expanduser().resolve(strict=True)
        if (
            not value.expanduser().is_absolute()
            or not root.is_dir()
            or root
            in {
                Path("/"),
                Path.home(),
                Path("/tmp"),
                Path("/home"),
            }
        ):
            raise ValueError("Approve a specific existing attachment-cache directory.")
        return root


class LocalSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["telegram"]
    chat_id: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,19}$")]
    message_id: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,19}$")]


class LocalFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Annotated[str, Field(min_length=1, max_length=4096)]
    filename: Annotated[str, Field(min_length=1, max_length=512)]
    mime_type: Literal["application/pdf", "image/png", "image/jpeg", "image/webp"]


class LocalSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID
    source: LocalSource
    files: Annotated[list[LocalFile], Field(min_length=1, max_length=5)]
    language: Literal["en", "zh"] = "en"


class SubmissionResult(BaseModel):
    """The frozen M15 submission result, without originals or AI suggestions."""

    model_config = ConfigDict(extra="forbid")

    operation_id: uuid.UUID
    status: Literal["created", "replayed"]
    target_id: uuid.UUID
    web_url: str | None = None
    inbox_id: uuid.UUID
    ai_state: Literal["NOT_RUN", "RUNNING", "READY", "UNAVAILABLE", "FAILED"]
    ai_error_code: (
        Literal[
            "AI_DISABLED",
            "AI_NOT_CONFIGURED",
            "AI_TIMEOUT",
            "AI_PROVIDER_ERROR",
            "AI_INVALID_RESPONSE",
        ]
        | None
    ) = None
    review_url: str


def open_attachment(root: Path, path: str, max_bytes: int) -> BinaryIO:
    """Open through directory descriptors, denying symlinks and traversal.

    O_NOFOLLOW at every component prevents validation/open races. Reject hard
    links and special files too, so an approved directory cannot point at a
    secret elsewhere or block on a FIFO/device. Only the opened fd is uploaded.
    """
    file_fd: int | None = None
    try:
        candidate = Path(path)
        if not candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError
        components = candidate.relative_to(root).parts
        if not components:
            raise ValueError
        with ExitStack() as stack:
            directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, directory_fd)
            for component in components[:-1]:
                directory_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                stack.callback(os.close, directory_fd)
            file_fd = os.open(
                components[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory_fd,
            )
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError
            if not 1 <= info.st_size <= max_bytes:
                raise ValueError
            opened = os.fdopen(file_fd, "rb")
            file_fd = None
            return opened
    except (OSError, ValueError):
        raise ToolError("JAI_ATTACHMENT_REJECTED") from None
    finally:
        if file_fd is not None:
            os.close(file_fd)


_SAFE_REMOTE_CODES = {
    "MCP_UNAUTHENTICATED",
    "MCP_INSUFFICIENT_SCOPE",
    "MCP_VALIDATION_FAILED",
    "MCP_REQUEST_ID_CONFLICT",
}


def stream_submission(
    settings: AdapterSettings,
    request: LocalSubmission,
    transport: httpx.BaseTransport | None = None,
) -> SubmissionResult:
    """Upload in bounded chunks; never return raw response bodies or exceptions."""
    with ExitStack() as stack:
        uploads = [
            (
                "files",
                (
                    file.filename,
                    stack.enter_context(
                        open_attachment(settings.cache_root, file.path, settings.max_file_bytes)
                    ),
                    file.mime_type,
                ),
            )
            for file in request.files
        ]
        metadata = request.model_dump_json(exclude={"files"})
        try:
            with (
                httpx.Client(
                    transport=transport,
                    timeout=httpx.Timeout(180, connect=5),
                    trust_env=False,
                    follow_redirects=False,
                ) as client,
                client.stream(
                    "POST",
                    settings.base_url + "/api/v1/agent/receipt-inbox",
                    headers={"Authorization": "Bearer " + settings.token.get_secret_value()},
                    data={"metadata": metadata},
                    files=uploads,
                ) as response,
            ):
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes(chunk_size=4096):
                    size += len(chunk)
                    if size > 16 * 1024:
                        raise ToolError("JAI_SUBMISSION_OUTCOME_UNKNOWN")
                    chunks.append(chunk)
                body = b"".join(chunks)
                if response.status_code != 200:
                    if response.status_code == 401:
                        raise ToolError("MCP_UNAUTHENTICATED")
                    if response.status_code == 403:
                        raise ToolError("MCP_INSUFFICIENT_SCOPE")
                    if response.status_code == 409:
                        raise ToolError("MCP_REQUEST_ID_CONFLICT")
                    if response.status_code in {400, 413, 422}:
                        raise ToolError("MCP_VALIDATION_FAILED")
                    raise ToolError("JAI_SUBMISSION_OUTCOME_UNKNOWN")
                try:
                    return SubmissionResult.model_validate_json(body)
                except ValidationError:
                    raise ToolError("JAI_SUBMISSION_OUTCOME_UNKNOWN") from None
        except httpx.HTTPError:
            # A timeout can occur after the durable commit. The model must
            # not claim definite failure or retry with a newly generated ID.
            raise ToolError("JAI_SUBMISSION_OUTCOME_UNKNOWN") from None


def create_server(settings: AdapterSettings) -> MCPServer:
    class SafeAdapter(MCPServer):
        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any],
            context: Any = None,
        ) -> Any:
            try:
                if name != "submit_receipt":
                    raise ToolError("MCP_VALIDATION_FAILED")
                LocalSubmission.model_validate(arguments)
                return await super().call_tool(name, arguments, context)
            except ToolError as exc:
                cause = exc.__cause__
                codes = _SAFE_REMOTE_CODES | {
                    "JAI_ATTACHMENT_REJECTED",
                    "JAI_SUBMISSION_OUTCOME_UNKNOWN",
                }
                code = str(exc) if str(exc) in codes else str(cause)
                raise ToolError(code if code in codes else "MCP_VALIDATION_FAILED") from None
            except Exception:
                raise ToolError("MCP_VALIDATION_FAILED") from None

    server = SafeAdapter("JAI local receipt upload")

    @server.tool(name="submit_receipt", structured_output=True)
    async def submit_receipt(
        request_id: uuid.UUID,
        source: LocalSource,
        files: Annotated[list[LocalFile], Field(min_length=1, max_length=5)],
        language: Literal["en", "zh"] = "en",
    ) -> SubmissionResult:
        """Stream current original Telegram attachment paths into JAI web review.

        Use request_id/source from the installed request_id.py helper. Give
        original cache paths, names and MIME types; never Base64 or file text.
        JAI_SUBMISSION_OUTCOME_UNKNOWN requires an identical retry with the
        same ID, source and originals; never claim success without a result.
        """
        request = LocalSubmission(
            request_id=request_id,
            source=source,
            files=files,
            language=language,
        )
        return await anyio.to_thread.run_sync(partial(stream_submission, settings, request))

    return server


def main() -> int:
    try:
        settings = AdapterSettings(
            cache_root=Path(os.environ["JAI_ATTACHMENT_CACHE_ROOT"]),
            base_url=os.environ["JAI_BASE_URL"],
            token=SecretStr(os.environ["JAI_MCP_TOKEN"]),
            max_file_bytes=int(os.environ.get("JAI_MAX_RECEIPT_BYTES", str(10 * 1024 * 1024))),
        )
        if not settings.token.get_secret_value():
            raise ValueError
    except (KeyError, ValueError, OSError):
        print("JAI adapter configuration is missing or invalid.", file=sys.stderr)
        return 2
    create_server(settings).run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

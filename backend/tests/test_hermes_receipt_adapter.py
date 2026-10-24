"""Local adapter safety and byte-preservation tests using temporary files."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import uuid
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import SecretStr, ValidationError

ADAPTER = Path(__file__).resolve().parents[2] / "integrations/hermes/jai-ledger/receipt_adapter.py"
spec = importlib.util.spec_from_file_location("jai_receipt_adapter", ADAPTER)
assert spec is not None and spec.loader is not None
adapter = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = adapter
spec.loader.exec_module(adapter)


def settings(root: Path) -> Any:
    return adapter.AdapterSettings(
        cache_root=root,
        base_url="http://127.0.0.1:12345",
        token=SecretStr("private-token"),
        max_file_bytes=256 * 1024,
    )


def submission(path: Path) -> Any:
    return adapter.LocalSubmission(
        request_id=uuid.uuid4(),
        source={"platform": "telegram", "chat_id": "123", "message_id": "42"},
        language="zh",
        files=[{"path": str(path), "filename": "original.pdf", "mime_type": "application/pdf"}],
    )


def result() -> dict[str, Any]:
    inbox_id = uuid.uuid4()
    return {
        "operation_id": str(uuid.uuid4()),
        "status": "created",
        "target_id": str(inbox_id),
        "inbox_id": str(inbox_id),
        "ai_state": "NOT_RUN",
        "ai_error_code": None,
        "web_url": None,
        "review_url": f"https://example.test/receipt-inbox/{inbox_id}",
    }


@pytest.mark.parametrize(
    "mime,prefix", [("application/pdf", b"%PDF-1.7\n"), ("image/png", b"\x89PNG\r\n\x1a\n")]
)
def test_streams_original_bytes_without_base64_or_paths_in_metadata(
    tmp_path: Path,
    mime: str,
    prefix: bytes,
) -> None:
    original = prefix + bytes(range(256)) * 415
    file = tmp_path / "current-attachment"
    file.write_bytes(original)
    request = submission(file)
    request.files[0].mime_type = mime
    expected_result = result()

    def respond(http_request: httpx.Request) -> httpx.Response:
        assert http_request.headers["authorization"] == "Bearer private-token"
        multipart = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {http_request.headers['content-type']}\r\n\r\n".encode()
            + http_request.read()
        )
        parts = {
            part.get_param("name", header="content-disposition"): part
            for part in multipart.iter_parts()
        }
        assert parts["files"].get_payload(decode=True) == original
        metadata = json.loads(parts["metadata"].get_payload(decode=True))
        assert metadata == request.model_dump(mode="json", exclude={"files"})
        assert "path" not in str(metadata) and "data_base64" not in str(metadata)
        return httpx.Response(200, json=expected_result)

    response = adapter.stream_submission(settings(tmp_path), request, httpx.MockTransport(respond))
    assert response.model_dump(mode="json") == expected_result
    assert "private-token" not in str(response)
    assert file.read_bytes() == original


@pytest.mark.parametrize(
    "kind",
    [
        "outside",
        "traversal",
        "symlink",
        "directory-symlink",
        "hardlink",
        "fifo",
        "directory",
        "empty",
        "large",
        "relative",
    ],
)
def test_adapter_denies_unsafe_attachment_paths(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"private secret")
    candidate = root / "file.pdf"
    if kind == "outside":
        candidate = outside
    elif kind == "traversal":
        candidate = root / ".." / "secret.pdf"
    elif kind == "symlink":
        candidate.symlink_to(outside)
    elif kind == "directory-symlink":
        (root / "link").symlink_to(tmp_path, target_is_directory=True)
        candidate = root / "link" / "secret.pdf"
    elif kind == "hardlink":
        os.link(outside, candidate)
    elif kind == "fifo":
        os.mkfifo(candidate)
    elif kind == "directory":
        candidate.mkdir()
    elif kind == "empty":
        candidate.touch()
    elif kind == "large":
        candidate.write_bytes(b"x" * 9)
    else:
        candidate = Path("relative.pdf")
    with pytest.raises(ToolError, match="^JAI_ATTACHMENT_REJECTED$"):
        adapter.open_attachment(root, str(candidate), 8)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test",
        "http://localhost:8000",
        "http://127.0.0.1@evil.test",
        "http://127.0.0.1:8000/api",
        "http://127.0.0.1:8000?token=secret",
    ],
)
def test_adapter_url_rejects_remote_hosts_and_embedded_credentials(
    tmp_path: Path, url: str
) -> None:
    with pytest.raises(ValidationError):
        adapter.AdapterSettings(cache_root=tmp_path, base_url=url, token=SecretStr("secret"))


@pytest.mark.parametrize("root", ["/", "/tmp", "/home", str(Path.home())])
def test_adapter_rejects_broad_roots(root: str) -> None:
    with pytest.raises(ValidationError):
        settings(Path(root))


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "MCP_UNAUTHENTICATED"),
        (403, "MCP_INSUFFICIENT_SCOPE"),
        (409, "MCP_REQUEST_ID_CONFLICT"),
        (422, "MCP_VALIDATION_FAILED"),
        (500, "JAI_SUBMISSION_OUTCOME_UNKNOWN"),
        (307, "JAI_SUBMISSION_OUTCOME_UNKNOWN"),
    ],
)
def test_remote_errors_never_echo_bodies_or_follow_redirects(
    tmp_path: Path,
    status: int,
    code: str,
) -> None:
    file = tmp_path / "a.pdf"
    file.write_bytes(b"%PDF-1.7\n")
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status, text="private-token raw file bytes", headers={"location": "https://evil.test"}
        )

    with pytest.raises(ToolError, match=f"^{code}$"):
        adapter.stream_submission(
            settings(tmp_path), submission(file), httpx.MockTransport(respond)
        )
    assert calls == 1


@pytest.mark.parametrize("failure", ["timeout", "invalid-result", "large-result"])
def test_uncertain_submission_is_not_reported_as_success(tmp_path: Path, failure: str) -> None:
    file = tmp_path / "a.pdf"
    file.write_bytes(b"%PDF-1.7\n")

    def respond(_request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("secret credentials and body")
        return httpx.Response(200, text="secret" if failure == "invalid-result" else "x" * 20000)

    with pytest.raises(ToolError, match="^JAI_SUBMISSION_OUTCOME_UNKNOWN$"):
        adapter.stream_submission(
            settings(tmp_path), submission(file), httpx.MockTransport(respond)
        )


async def test_local_tool_schema_and_errors_do_not_require_model_file_bytes(tmp_path: Path) -> None:
    server = adapter.create_server(settings(tmp_path))
    tools = await server.list_tools()
    assert [tool.name for tool in tools] == ["submit_receipt"]
    schema = str(tools[0].input_schema)
    assert "path" in schema and "data_base64" not in schema
    with pytest.raises(ToolError, match="^MCP_VALIDATION_FAILED$"):
        await server.call_tool("submit_receipt", {"files": "secret"})
    with pytest.raises(ToolError, match="^JAI_ATTACHMENT_REJECTED$"):
        await server.call_tool(
            "submit_receipt", submission(tmp_path / "missing.pdf").model_dump(mode="json")
        )


async def test_real_stdio_discovery_and_path_denial(tmp_path: Path) -> None:
    """Launch only our adapter, never the live Hermes gateway or profile."""
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ADAPTER)],
        env={
            "JAI_ATTACHMENT_CACHE_ROOT": str(tmp_path),
            "JAI_BASE_URL": "http://127.0.0.1:12345",
            "JAI_MCP_TOKEN": "temporary-test-token",
        },
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
        await client.initialize()
        tools = await client.list_tools()
        assert [tool.name for tool in tools.tools] == ["submit_receipt"]
        denied = await client.call_tool(
            "submit_receipt",
            submission(tmp_path / "missing.pdf").model_dump(mode="json"),
        )
        assert denied.is_error
        assert "JAI_ATTACHMENT_REJECTED" in str(denied.content)
        assert "temporary-test-token" not in str(denied)

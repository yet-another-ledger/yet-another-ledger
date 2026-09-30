"""Closed-by-default Streamable HTTP MCP mount for M15."""

from __future__ import annotations

import json

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def _valid_authority_port(value: str) -> bool:
    """Accept only bounded decimal TCP ports in the usable range."""
    # A legal port has at most five decimal digits. Check the length before
    # int() so hostile, arbitrarily long numeric authorities cannot raise.
    return (
        value.isascii()
        and value.isdigit()
        and len(value) <= 5
        and 1 <= int(value) <= 65535
    )


def _host_from_authority(value: str) -> str:
    """Parse a simple loopback Host/Origin authority without DNS lookups."""
    if not value or any(char in value for char in " ,/@\\"):
        return ""
    if value.startswith("["):
        end = value.find("]")
        if end < 0 or value[: end + 1].lower() != "[::1]":
            return ""
        suffix = value[end + 1 :]
        if suffix and not (
            suffix.startswith(":") and _valid_authority_port(suffix[1:])
        ):
            return ""
        return "[::1]"
    hostname, separator, port = value.partition(":")
    if separator and not _valid_authority_port(port):
        return ""
    return hostname.lower()


async def _reject(send: Send, status_code: int, code: str, message: str) -> None:
    body = json.dumps({"error": {"code": code, "message": message}}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                *(
                    [(b"www-authenticate", b"Bearer")]
                    if status_code == 401
                    else []
                ),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class ClosedMCPBoundary:
    """Protect MCP paths before parent middleware can answer the request.

    Step 2 replaces the unconditional 401 with token authentication and scope
    checks. No cookie or protocol discovery exception is admitted here. This
    middleware is installed once on the parent app, outside development CORS.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (
            path == "/api/v1/mcp" or path.startswith("/api/v1/mcp/")
        ):
            await self.app(scope, receive, send)
            return
        header_values: dict[bytes, list[str]] = {}
        for key, value in scope["headers"]:
            header_values.setdefault(key.lower(), []).append(value.decode("latin-1"))
        host_values = header_values.get(b"host", [])
        if len(host_values) != 1:
            await _reject(send, 421, "MCP_HOST_REJECTED", "MCP Host is not allowed.")
            return
        host = _host_from_authority(host_values[0])
        if host not in _ALLOWED_HOSTS:
            await _reject(send, 421, "MCP_HOST_REJECTED", "MCP Host is not allowed.")
            return
        origin_values = header_values.get(b"origin", [])
        if len(origin_values) > 1:
            await _reject(send, 403, "MCP_ORIGIN_REJECTED", "MCP Origin is not allowed.")
            return
        origin = origin_values[0] if origin_values else None
        if origin is not None:
            # urllib.parse.urlsplit strips ASCII tabs/newlines and leading
            # C0 controls before parsing. Validate the actual header value
            # first so normalization cannot turn malformed input into an
            # allowed loopback origin.
            if (
                origin != origin.strip()
                or any(ord(char) < 0x20 or ord(char) == 0x7F for char in origin)
            ):
                await _reject(
                    send, 403, "MCP_ORIGIN_REJECTED", "MCP Origin is not allowed."
                )
                return
            from urllib.parse import urlsplit

            try:
                parsed = urlsplit(origin)
                valid_port = parsed.port is None or 1 <= parsed.port <= 65535
            except ValueError:
                parsed = None
                valid_port = False
            if parsed is None:
                await _reject(
                    send, 403, "MCP_ORIGIN_REJECTED", "MCP Origin is not allowed."
                )
                return
            origin_host = _host_from_authority(parsed.netloc)
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
                or not valid_port
                or origin_host not in _ALLOWED_HOSTS
            ):
                await _reject(send, 403, "MCP_ORIGIN_REJECTED", "MCP Origin is not allowed.")
                return
        # Step 1 intentionally rejects even initialize/discovery and ignores
        # browser cookies. Step 2 installs the verifier at this boundary.
        await _reject(send, 401, "MCP_UNAUTHENTICATED", "A valid Bearer token is required.")


class ExactMCPEntry:
    """Serve the no-trailing-slash canonical endpoint without a redirect."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        child_scope = dict(scope)
        child_scope["root_path"] = f'{scope.get("root_path", "").rstrip("/")}/api/v1/mcp'
        child_scope["path"] = "/"
        child_scope["raw_path"] = b"/"
        await self.app(child_scope, receive, send)


server = MCPServer("Yet Another Ledger")
_sdk_app = server.streamable_http_app(
    streamable_http_path="/",
    # The parent app's path-selective ClosedMCPBoundary is the one Host/Origin
    # policy. Disable the SDK's parallel DNS-rebinding check so Step 2 has a
    # single boundary to update for token verification.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
app = _sdk_app
exact_app = ExactMCPEntry(app)

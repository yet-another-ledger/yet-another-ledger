"""Closed-by-default Streamable HTTP MCP mount for M15."""

from __future__ import annotations

import json
import uuid
from collections.abc import MutableMapping
from contextvars import ContextVar
from typing import Annotated, Any, Literal

from fastapi import HTTPException
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import jsonrpc_message_adapter
from pydantic import BaseModel, Field, ValidationError
from starlette.types import ASGIApp, Receive, Scope, Send

from jai.config import get_settings
from jai.db import get_session_maker
from jai.schemas.agent_token import AgentTokenScope
from jai.schemas.agent_tools import (
    AgentSource,
    CatalogListOutput,
    CreateCustomerInput,
    CreateCustomerOutput,
    CustomerListOutput,
    CustomerOutput,
    DocumentTemplateListOutput,
    EmptyInput,
    GetCustomerInput,
    GetReceiptStatusInput,
    NonEmpty,
    ReceiptFileInput,
    ReceiptStatusOutput,
    SearchCatalogInput,
    SearchCustomersInput,
    SubmitReceiptInput,
    SubmitReceiptOutput,
    VatOptionsOutput,
)
from jai.schemas.customer import CustomerWrite
from jai.services.agent_business import (
    create_customer_idempotently,
    get_customer_detail,
    list_templates,
)
from jai.services.agent_business import (
    list_vat_options as list_vat_options_service,
)
from jai.services.agent_business import (
    search_catalog as search_catalog_service,
)
from jai.services.agent_business import (
    search_customers as search_customers_service,
)
from jai.services.agent_receipts import (
    AgentReceiptError,
)
from jai.services.agent_receipts import (
    get_receipt_status as get_receipt_status_service,
)
from jai.services.agent_receipts import (
    submit_receipt as submit_receipt_service,
)
from jai.services.agent_tokens import AgentPrincipal, authenticate_token, require_agent_scope
from jai.services.storage import get_storage

_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
agent_principal: ContextVar[AgentPrincipal | None] = ContextVar("agent_principal", default=None)


def _body_limit() -> int:
    """Bound the complete JSON body before the MCP SDK parses it."""
    max_decoded = get_settings().max_receipt_bytes * 5
    return (max_decoded * 4 + 2) // 3 + 16 * 1024


def require_scope(scope: AgentTokenScope) -> None:
    """Authorize a registered MCP handler against the current HTTP request."""
    from jai.services.agent_tokens import require_agent_scope

    require_agent_scope(scope, agent_principal.get())


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

    Every MCP HTTP request must authenticate a Bearer token. No cookie or
    protocol discovery exception is admitted here. This
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
        auth_values = header_values.get(b"authorization", [])
        if len(auth_values) != 1:
            await _reject(send, 401, "MCP_UNAUTHENTICATED", "A valid Bearer token is required.")
            return
        scheme, separator, token_value = auth_values[0].partition(" ")
        if (
            scheme.lower() != "bearer"
            or not separator
            or not token_value
            or token_value.strip() != token_value
        ):
            await _reject(send, 401, "MCP_UNAUTHENTICATED", "A valid Bearer token is required.")
            return
        try:
            async with get_session_maker()() as session:
                principal = await authenticate_token(session, token_value)
        except Exception:
            # Fail closed without logging request credentials or database details.
            await _reject(send, 401, "MCP_UNAUTHENTICATED", "A valid Bearer token is required.")
            return
        if principal is None:
            await _reject(send, 401, "MCP_UNAUTHENTICATED", "A valid Bearer token is required.")
            return
        if scope.get("method") == "POST":
            body_limit = _body_limit()
            content_lengths = header_values.get(b"content-length", [])
            if len(content_lengths) > 1 or (
                content_lengths
                and (
                    len(content_lengths[0]) > 12
                    or not content_lengths[0].isascii()
                    or not content_lengths[0].isdigit()
                    or int(content_lengths[0]) > body_limit
                )
            ):
                await _reject(send, 413, "MCP_VALIDATION_FAILED", "MCP request body is too large.")
                return
            chunks: list[bytes] = []
            received = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                received += len(chunk)
                if received > body_limit:
                    await _reject(
                        send,
                        413,
                        "MCP_VALIDATION_FAILED",
                        "MCP request body is too large.",
                    )
                    return
                chunks.append(chunk)
                if not message.get("more_body", False):
                    break
            body = b"".join(chunks)
            try:
                jsonrpc_message_adapter.validate_python(json.loads(body), by_name=False)
            except Exception:
                # The SDK's own envelope error includes Pydantic input_value
                # details. Reject here, while the bounded raw body is still at
                # the authenticated transport boundary, without echo or log.
                await _reject(
                    send,
                    400,
                    "MCP_VALIDATION_FAILED",
                    "MCP request envelope is invalid.",
                )
                return
            original_receive = receive
            sent = False

            async def replay_receive() -> MutableMapping[str, Any]:
                nonlocal sent
                if sent:
                    return await original_receive()
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}

            receive = replay_receive
        token = agent_principal.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            agent_principal.reset(token)


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


_SAFE_TOOL_ERRORS = frozenset(
    {
        "MCP_UNAUTHENTICATED",
        "MCP_INSUFFICIENT_SCOPE",
        "MCP_VALIDATION_FAILED",
        "MCP_TARGET_NOT_FOUND",
        "MCP_TARGET_AMBIGUOUS",
        "MCP_DRAFT_REQUIRED",
        "MCP_REQUEST_ID_CONFLICT",
    }
)
_TOOL_INPUT_MODELS: dict[str, type[BaseModel]] = {
    "submit_receipt": SubmitReceiptInput,
    "get_receipt_status": GetReceiptStatusInput,
    "search_customers": SearchCustomersInput,
    "get_customer": GetCustomerInput,
    "create_customer": CreateCustomerInput,
    "search_catalog": SearchCatalogInput,
    "list_document_templates": EmptyInput,
    "list_vat_options": EmptyInput,
}


class SafeMCPServer(MCPServer):
    """Map SDK argument validation text to a fixed error before it can echo input."""

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Any = None
    ) -> Any:
        input_model = _TOOL_INPUT_MODELS.get(name)
        if input_model is not None:
            try:
                # Enforce the frozen root model (including extra='forbid')
                # before the SDK's function argument model can ignore extras.
                input_model.model_validate(arguments)
            except ValidationError:
                raise ToolError("MCP_VALIDATION_FAILED") from None
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as exc:
            cause = exc.__cause__
            safe_code = str(cause) if isinstance(cause, ToolError) else None
            if safe_code in _SAFE_TOOL_ERRORS:
                raise ToolError(safe_code) from None
            raise ToolError("MCP_VALIDATION_FAILED") from None


server = SafeMCPServer("Yet Another Ledger")
_sdk_app = server.streamable_http_app(
    streamable_http_path="/",
    # The parent app's path-selective ClosedMCPBoundary is the one Host/Origin
    # policy. Disable the SDK's parallel DNS-rebinding check so Step 2 has a
    # single boundary to update for token verification.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
app = _sdk_app
exact_app = ExactMCPEntry(app)


def _tool_error(code: str) -> ToolError:
    return ToolError(code)


def _authorized_principal(scope: AgentTokenScope) -> AgentPrincipal:
    principal = agent_principal.get()
    try:
        require_agent_scope(scope, principal)
    except HTTPException as exc:
        detail = exc.detail
        code = detail.get("code") if isinstance(detail, dict) else None
        raise _tool_error(code or "MCP_UNAUTHENTICATED") from exc
    if principal is None:
        raise _tool_error("MCP_UNAUTHENTICATED")
    return principal


@server.tool(
    name="submit_receipt",
    description="Submit 1–5 original receipt files for web review.",
    structured_output=True,
)
async def submit_receipt(
    request_id: uuid.UUID,
    source: AgentSource,
    files: Annotated[list[ReceiptFileInput], Field(min_length=1, max_length=5)],
    language: Literal["en", "zh"] = "en",
) -> SubmitReceiptOutput:
    """Validate and durably add an original receipt batch to the M14 inbox."""
    principal = _authorized_principal(AgentTokenScope.RECEIPT_INTAKE)
    try:
        request = SubmitReceiptInput(
            request_id=request_id,
            source=source,
            files=files,
            language=language,
        )
    except Exception:
        raise _tool_error("MCP_VALIDATION_FAILED") from None
    try:
        return await submit_receipt_service(
            get_session_maker(), principal, request, get_storage()
        )
    except AgentReceiptError as exc:
        raise _tool_error(exc.code) from None


@server.tool(
    name="get_receipt_status",
    description="Read the current status of an inbox receipt.",
    structured_output=True,
)
async def get_receipt_status(inbox_id: uuid.UUID) -> ReceiptStatusOutput:
    principal = _authorized_principal(AgentTokenScope.RECEIPT_INTAKE)
    try:
        return await get_receipt_status_service(
            get_session_maker(), principal, inbox_id
        )
    except AgentReceiptError as exc:
        raise _tool_error(exc.code) from None


@server.tool(
    name="search_customers",
    description="Search company customers by name, email, or legal name.",
    structured_output=True,
)
async def search_customers(
    query: NonEmpty,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> CustomerListOutput:
    principal = _authorized_principal(AgentTokenScope.CUSTOMER_READ)
    try:
        request = SearchCustomersInput(query=query, limit=limit)
        return await search_customers_service(
            get_session_maker(), principal, request.query, request.limit
        )
    except Exception:
        raise _tool_error("MCP_VALIDATION_FAILED") from None


@server.tool(
    name="get_customer",
    description="Read one company customer and its saved addresses.",
    structured_output=True,
)
async def get_customer(customer_id: uuid.UUID) -> CustomerOutput:
    principal = _authorized_principal(AgentTokenScope.CUSTOMER_READ)
    try:
        request = GetCustomerInput(customer_id=customer_id)
        result = await get_customer_detail(get_session_maker(), principal, request.customer_id)
    except Exception:
        raise _tool_error("MCP_VALIDATION_FAILED") from None
    if result is None:
        raise _tool_error("MCP_TARGET_NOT_FOUND")
    return result


@server.tool(
    name="create_customer",
    description="Create a customer after checking candidates with the user.",
    structured_output=True,
)
async def create_customer(
    request_id: uuid.UUID,
    customer: CustomerWrite,
    source: AgentSource | None = None,
) -> CreateCustomerOutput:
    principal = _authorized_principal(AgentTokenScope.CUSTOMER_CREATE)
    try:
        request = CreateCustomerInput(request_id=request_id, customer=customer, source=source)
    except ValidationError:
        raise _tool_error("MCP_VALIDATION_FAILED") from None
    try:
        return await create_customer_idempotently(get_session_maker(), principal, request)
    except HTTPException as exc:
        detail = exc.detail
        code = detail.get("code") if isinstance(detail, dict) else None
        if isinstance(code, str) and code in _SAFE_TOOL_ERRORS:
            raise _tool_error(code) from None
        raise _tool_error("MCP_VALIDATION_FAILED") from None
    except Exception:
        raise _tool_error("MCP_VALIDATION_FAILED") from None


@server.tool(
    name="search_catalog",
    description="Search saved active product options for document lines.",
    structured_output=True,
)
async def search_catalog(
    query: str = "",
    limit: Annotated[int, Field(ge=1, le=100)] = 50,
) -> CatalogListOutput:
    principal = _authorized_principal(AgentTokenScope.CATALOG_READ)
    try:
        request = SearchCatalogInput(query=query, limit=limit)
        return await search_catalog_service(
            get_session_maker(), principal, request.query, request.limit
        )
    except Exception:
        raise _tool_error("MCP_VALIDATION_FAILED") from None


@server.tool(
    name="list_document_templates",
    description="List saved document line templates.",
    structured_output=True,
)
async def list_document_templates() -> DocumentTemplateListOutput:
    principal = _authorized_principal(AgentTokenScope.CATALOG_READ)
    try:
        return await list_templates(get_session_maker(), principal)
    except Exception:
        raise _tool_error("MCP_VALIDATION_FAILED") from None


@server.tool(
    name="list_vat_options",
    description="List saved VAT rates and treatments for this company.",
    structured_output=True,
)
async def list_vat_options() -> VatOptionsOutput:
    principal = _authorized_principal(AgentTokenScope.CATALOG_READ)
    try:
        return await list_vat_options_service(get_session_maker(), principal)
    except Exception:
        raise _tool_error("MCP_VALIDATION_FAILED") from None

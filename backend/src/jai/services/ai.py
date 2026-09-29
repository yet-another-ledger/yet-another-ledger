"""AI receipt extraction service (M8 step 4).

Architecture
------------
- Uses OpenAI-compatible Chat Completions API via ``httpx.AsyncClient``.
- All configuration (base_url, api_key, model, prompt) comes from the database
  setting ``SETTING_KEY_AI`` with env-variable fallback (D1, D6).
- The ``httpx.AsyncClient`` is injectable so unit tests can supply a mock
  transport without touching the network (D6 – never hit real endpoints in tests).
- PDF receipts are rasterised with ``pypdfium2`` (pure-wheel, no poppler) and
  encoded as PNG images for the multimodal ``image_url`` parts (D16).
- The legacy Expense endpoint returns ``ExpenseAIPrefill`` without persisting it.
  Receipt inbox callers persist only the parsed, validated prefill plus a safe
  state/error code; provider responses and source bytes are never persisted.

Security
--------
- ``api_key`` is **never** written to logs or included in any response body.
  Search ``api_key`` in this file: it only appears in the Authorization header
  construction (not logged) and in ``AiSettings`` field access.
- Receipt image bytes are treated as untrusted input; model output is parsed
  defensively (all fields nullable, strict type / range coercion).
- Red-line 7: user-supplied image data flows into base64 strings embedded in
  the prompt; it is never rendered or executed server-side.

Prompt design (D15)
-------------------
The effective prompt sent to the model is built by concatenating (in order):

    DEFAULT_RECEIPT_PROMPT
    + (optional) AiSettings.receipt_prompt   ← appended, never replaces default
    + date_context                           ← runtime injection
    + _language_context(language)            ← runtime injection (UI locale)
    + _OUTPUT_CONTRACT                       ← always last

``AiSettings.receipt_prompt`` is an **additive** override: it is appended after
the default extraction instruction so that users can refine or extend the
prompt without losing the built-in Dutch BTW context.  Leaving it empty results
in the default behaviour only.
The ``_OUTPUT_CONTRACT`` footer (which defines the JSON output schema) is
**always appended last by the service** and cannot be altered by the user.  This
guarantees that the JSON parser can always handle the model's output regardless
of prompt customisation.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from jai.config import get_settings
from jai.models._enums import SettingLevel
from jai.schemas.expense import ExpenseAIPrefill
from jai.schemas.setting import (
    SETTING_KEY_AI,
    AiSettings,
    AiTestResult,
)
from jai.services.storage import get_storage

logger = logging.getLogger("jai.ai")

_DEFAULT_COMPLETION_MAX_TOKENS = 512
RECEIPT_EXTRACTION_MAX_TOKENS = 1_024
ARTIFACT_VALIDATION_MAX_TOKENS = 4_096

# ---------------------------------------------------------------------------
# Probe image (built-in minimal PNG, used for connectivity/multimodal testing)
# ---------------------------------------------------------------------------
# This 64×64 RGB PNG is embedded as static bytes so that the test probe never
# needs to read from disk and never exposes user receipt data (D14).
# Many vision gateways reject images smaller than ~8×8 pixels (xAI enforces an
# 8×8 minimum); the original 1×1 probe was returned as HTTP 400 by such
# providers, which was mistakenly interpreted as "model does not support
# multimodal input".  This 64×64 image (black border + diagonal lines, not a
# solid colour) passes the size checks of all tested providers.
# Generated offline; embedded as hex so the runtime has NO Pillow dependency.
_PROBE_PNG_BYTES: bytes = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000040000000400802000000250be6"
    "890000012f4944415478dad5dacb8e83301044517cffff9f338b4851260f62ec"
    "7e54b101dbdd251f40ac380ef363dc4fb7dbcd72f76370dffd18c3f409f07802"
    "a6061e57a6069e078e065ec67606dea7bc0c7c9c3532f06dc1c5c0c99a8581f3"
    "657d033f2bc40dcc14291b98ac9335305faa69e052b5a081ab0d6a06167aa40c"
    "acb5e91858ee1431b0d3ac6060b3bfddc07e44af819094460351415d0602b35a"
    "0cc4c6d51b084f2c3690115a692029b7cc405e748d81d4f40203d97728db40c1"
    "6b9a6aa0e65b91672802e419ea004986524086a11a106e6800c41a7a00818636"
    "4094a11310626806ec1bfa019b0609c08e4105b06c1002ac19b4000b0639c055"
    "8322e0924114306fd0054c1aa401330675c04f8301e0dce0013831d800be199c"
    "001f0d668077831fe0c5600938ccfff7fe77fc013e7fba7ca70f55d000000000"
    "49454e44ae426082"
)

# ---------------------------------------------------------------------------
# Prompt constants (D15)
# ---------------------------------------------------------------------------

DEFAULT_RECEIPT_PROMPT: str = (
    "You are an expert receipt and invoice analyser, specialised in Dutch (NL) and "
    "EU business documents. Extract the following fields from the receipt image(s) "
    "provided. The document may be a BTW (VAT) invoice, a till receipt, a PDF "
    "statement, or a scanned document.\n\n"
    "Fields to extract:\n"
    "- expense_date: the invoice or receipt date (ISO 8601 format: YYYY-MM-DD)\n"
    "- supplier_name: the name of the supplier / vendor\n"
    "- net_amount: the net (excl. BTW/VAT) total amount as a decimal number\n"
    "- vat_amount: the total BTW/VAT amount as a decimal number\n"
    "- vat_rate_percent: the VAT rate percentage (e.g. 21, 9, or 0)\n"
    "- suggested_category_name: a short expense category (e.g. 'Office supplies', "
    "'Travel', 'Software', 'Utilities', 'Meals', 'Professional services')\n"
    "- confidence: your overall confidence level: 'high', 'medium', or 'low'\n"
    "- raw_model_note: any caveat or uncertainty about the extraction\n"
    "- summary: a brief one-line description of what was purchased / what this expense is for "
    "(e.g. 'Lunch meeting with client', 'A4 printer paper', 'Train ticket Amsterdam-Rotterdam')\n\n"
    "Return ONLY a JSON object. Omit keys you cannot determine with reasonable "
    "confidence."
)

# This footer is ALWAYS appended last to the effective prompt (after
# DEFAULT_RECEIPT_PROMPT, any additive receipt_prompt, date_context, and
# _language_context).  It pins the output format so the JSON parser can always
# handle the model's reply regardless of what the user writes as extra
# instructions.
_OUTPUT_CONTRACT: str = (
    "\n\n---\nIMPORTANT: Reply with EXACTLY one JSON object using these keys "
    "(omit any key you cannot determine):\n"
    '{"expense_date": "YYYY-MM-DD", "supplier_name": "...", '
    '"net_amount": 0.00, "vat_amount": 0.00, "vat_rate_percent": 21, '
    '"suggested_category_name": "...", "confidence": "high|medium|low", '
    '"raw_model_note": "...", "summary": "..."}\n'
    "Do NOT wrap in markdown code fences. Do NOT include any text outside the "
    "JSON object."
)


def _language_context(language: str | None) -> str:
    """Return a runtime prompt block that steers explanatory-field language.

    - ``summary`` and ``raw_model_note`` follow the UI locale (``language``).
    - Supplier names, product/item names, and brand names are kept verbatim.
    - ``suggested_category_name`` stays in English (it is matched against a
      fixed category list; translating it would break the matching logic).
    """
    lang_name = (
        "Simplified Chinese (简体中文)"
        if (language or "").lower().startswith("zh")
        else "English"
    )
    return (
        "\n\nLANGUAGE OF EXPLANATORY TEXT:\n"
        f"- Write the explanatory fields `summary` and `raw_model_note` in {lang_name}.\n"
        "- Do NOT translate proper nouns: keep supplier names, product/item names and "
        "brand names exactly as printed on the document.\n"
        "- Keep `suggested_category_name` in English (it is matched against a fixed "
        "category list, not shown as prose)."
    )


# ---------------------------------------------------------------------------
# Pydantic models for httpx request / response (mypy-friendly, D6)
# ---------------------------------------------------------------------------


class _ImageUrlContent(BaseModel):
    type: str = "image_url"
    image_url: dict[str, str]


class _TextContent(BaseModel):
    type: str = "text"
    text: str


class _ChatMessage(BaseModel):
    role: str
    content: list[dict[str, Any]]


class _ChatRequest(BaseModel):
    model: str
    messages: list[dict[str, Any]]
    max_tokens: int = 512
    response_format: dict[str, str] | None = None


class _ChatChoice(BaseModel):
    message: dict[str, Any]


class _ChatResponse(BaseModel):
    choices: list[_ChatChoice]


class ChatCompletionTruncatedError(RuntimeError):
    """The provider stopped because the requested completion budget was exhausted."""


class AIUnavailableError(ValueError):
    """A safe, stable reason why extraction is not configured or enabled."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code)


class AIExtractionError(RuntimeError):
    """A safe provider failure code without private response details."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        messages = {
            "AI_TIMEOUT": (
                "AI model request timed out. The receipt may be too complex or the "
                "service is temporarily unavailable."
            ),
            "AI_PROVIDER_ERROR": (
                "The AI service could not process the receipt. Check the AI settings "
                "or retry later."
            ),
            "AI_INVALID_RESPONSE": (
                "AI returned an invalid response. Please retry or enter the details manually."
            ),
        }
        super().__init__(message or messages.get(code, "AI extraction failed."))


# ---------------------------------------------------------------------------
# Config resolution (mirrors services/email.py::_get_smtp_config)
# ---------------------------------------------------------------------------


async def _get_ai_config(session: AsyncSession) -> AiSettings:
    """Load AI settings from the DB, falling back to environment variables.

    Mirrors ``services/email.py::_get_smtp_config`` with field-level fallback:
    1. Try DB ``SETTING_KEY_AI`` at GLOBAL level.
    2. For each critical field (``base_url``, ``api_key``, ``model``) use the
       DB value when non-empty, otherwise fall back to ``config.ai_*``.
    3. ``enabled`` and ``receipt_prompt`` use the DB value when a row exists,
       otherwise fall back to env / default.

    This matches the SMTP pattern: a DB row with a blank ``api_key`` does not
    shadow the ``AI_API_KEY`` env variable.  The ``api_key`` field is **never**
    logged.
    """
    from jai.services.settings import get_setting

    try:
        cfg = await get_setting(
            session,
            SETTING_KEY_AI,
            level=SettingLevel.GLOBAL,
            value_type=AiSettings,
        )
    except Exception:
        logger.warning("Failed to read AI settings from DB", exc_info=True)
        cfg = None

    # Env fallback (field-level, mirroring _get_smtp_config).
    settings = get_settings()

    if cfg is None:
        # No DB row at all – pure env fallback.
        return AiSettings(
            enabled=settings.ai_enabled,
            base_url=settings.ai_base_url,
            api_key=settings.ai_api_key,
            model=settings.ai_model,
            receipt_prompt="",
        )

    # DB row exists: use DB value per field when non-empty, else fall back to env.
    return AiSettings(
        enabled=cfg.enabled,
        base_url=cfg.base_url if cfg.base_url else settings.ai_base_url,
        api_key=cfg.api_key if cfg.api_key else settings.ai_api_key,
        model=cfg.model if cfg.model else settings.ai_model,
        receipt_prompt=cfg.receipt_prompt,
    )


# ---------------------------------------------------------------------------
# PDF rasterisation (D16)
# ---------------------------------------------------------------------------


def _attachment_to_images(mime_type: str, raw_bytes: bytes) -> list[bytes]:
    """Convert a receipt file to a list of PNG image byte strings.

    Supported MIME types
    --------------------
    - ``image/*`` → returned as-is (single-element list).
    - ``application/pdf`` → each page is rasterised with ``pypdfium2`` (up to
      ``config.ai_pdf_max_pages`` pages) and encoded as PNG via Pillow.

    Raises
    ------
    ValueError
        For unsupported MIME types (caller maps this to HTTP 422).
    """
    settings = get_settings()
    mime_type = mime_type.split(";")[0].strip().lower()

    if mime_type.startswith("image/"):
        return [raw_bytes]

    if mime_type == "application/pdf":
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(raw_bytes)
        try:
            return rasterize_pdf_pages(
                raw_bytes, list(range(min(len(doc), settings.ai_pdf_max_pages)))
            )
        finally:
            doc.close()

    raise ValueError(
        f"Unsupported MIME type for AI extraction: {mime_type!r}. "
        "Only image/* and application/pdf are supported."
    )


def rasterize_pdf_pages(raw_bytes: bytes, page_indexes: list[int]) -> list[bytes]:
    """Rasterise selected zero-based PDF pages to PNG bytes in memory.

    The caller owns page selection.  Keeping it separate allows formal-artifact
    validation to include both the first and final pages without changing the
    receipt extractor's historical first-N behaviour.
    """
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(raw_bytes)
    try:
        page_count = len(document)
        if not page_indexes or any(index < 0 or index >= page_count for index in page_indexes):
            raise ValueError("PDF page selection is invalid.")
        scale = get_settings().ai_pdf_render_scale
        images: list[bytes] = []
        for index in page_indexes:
            page = document[index]
            bitmap = page.render(scale=scale)
            pil_image = bitmap.to_pil()
            if pil_image.mode not in ("RGB", "L"):
                pil_image = pil_image.convert("RGB")
            buffer = io.BytesIO()
            pil_image.save(buffer, format="PNG", optimize=False)
            images.append(buffer.getvalue())
        return images
    finally:
        document.close()


# ---------------------------------------------------------------------------
# httpx Chat Completions call (injectable client for testing)
# ---------------------------------------------------------------------------


def _build_messages(
    image_bytes_list: list[bytes],
    prompt_text: str,
    mime_hint: str = "image/png",
) -> list[dict[str, Any]]:
    """Build the Chat Completions messages payload.

    Each image byte string becomes one ``image_url`` content part.  The
    text prompt is the final content part in the user message.

    ``mime_hint`` should be the MIME type of the *original* attachment (e.g.
    ``image/png``, ``image/jpeg``).  For PDF-derived images the bytes are
    always PNG-encoded (from pypdfium2 → Pillow), so if the original MIME
    is ``application/pdf`` we always use ``image/png`` in the data URL.
    All other image MIME types are used as-is.
    """
    content: list[dict[str, Any]] = []

    # For PDF-derived images the rasterised bytes are always PNG regardless
    # of page count.  For native images use the declared MIME type.
    resolved_mime = (
        "image/png"
        if mime_hint == "application/pdf" or not mime_hint.startswith("image/")
        else mime_hint
    )

    for img_bytes in image_bytes_list:
        b64 = base64.b64encode(img_bytes).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{resolved_mime};base64,{b64}"},
            }
        )

    content.append({"type": "text", "text": prompt_text})

    return [{"role": "user", "content": content}]


def _build_group_messages(
    files: list[tuple[str, list[bytes]]], prompt_text: str
) -> list[dict[str, Any]]:
    """Build one multimodal request, preserving each source image's MIME."""
    content: list[dict[str, Any]] = []
    for mime_type, pages in files:
        resolved_mime = "image/png" if mime_type == "application/pdf" else mime_type
        for image_bytes in pages:
            encoded = base64.b64encode(image_bytes).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{resolved_mime};base64,{encoded}"},
            })
    content.append({"type": "text", "text": prompt_text})
    return [{"role": "user", "content": content}]


async def _call_chat_completions(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    client: httpx.AsyncClient | None = None,
    *,
    request_json_mode: bool = True,
    max_tokens: int = _DEFAULT_COMPLETION_MAX_TOKENS,
) -> str:
    """POST to {base_url}/chat/completions and return the response text.

    The ``client`` parameter is injectable for testing.  When ``None`` a
    temporary ``httpx.AsyncClient`` is created for the duration of the call.

    When ``request_json_mode=True`` (the default for real extraction calls),
    ``response_format={"type": "json_object"}`` is included in the request.
    If the endpoint responds with HTTP 400 (common when the model or gateway
    does not support json mode), the call is retried **without**
    ``response_format``.  Parsing is always defensive so this is safe (D15 –
    do not hard-depend on json mode).

    When ``request_json_mode=False`` (used for the connectivity probe),
    ``response_format`` is never sent, so endpoints that only lack json-mode
    support are not mistakenly reported as non-multimodal.

    Raises
    ------
    httpx.HTTPStatusError
        For 4xx / 5xx responses (after any retry).
    httpx.TimeoutException
        For connection / read timeouts.
    httpx.ConnectError
        For network failures.
    RuntimeError
        If the response has an unexpected shape.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    url = f"{base_url.rstrip('/')}/chat/completions"

    async def _do_post(_client: httpx.AsyncClient, body: dict[str, Any]) -> object:
        resp = await _client.post(url, json=body, headers=headers)
        resp.raise_for_status()
        return resp.json()

    base_body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    body_with_json_mode = {**base_body, "response_format": {"type": "json_object"}}

    if client is not None:
        if request_json_mode:
            try:
                data = await _do_post(client, body_with_json_mode)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 400:
                    # Endpoint doesn't support json mode – retry without it.
                    data = await _do_post(client, base_body)
                else:
                    raise
        else:
            data = await _do_post(client, base_body)
    else:
        async with httpx.AsyncClient(timeout=60.0) as _client:
            if request_json_mode:
                try:
                    data = await _do_post(_client, body_with_json_mode)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 400:
                        data = await _do_post(_client, base_body)
                    else:
                        raise
            else:
                data = await _do_post(_client, base_body)

    # OpenAI-compatible gateways occasionally return successful HTTP responses
    # with a provider-specific error envelope.  Validate every node before
    # accessing it, and never include that envelope in the exception: it can
    # contain model output or provider diagnostics that must not reach logs.
    if not isinstance(data, dict):
        raise RuntimeError("AI response has an invalid chat completion envelope.")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("AI response has an invalid chat completion envelope.")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise RuntimeError("AI response has an invalid chat completion envelope.")
    if choice.get("finish_reason") == "length":
        # Do not parse or report partial content: it may be incomplete JSON and
        # must never appear in an exception or log message.
        raise ChatCompletionTruncatedError("AI response was truncated.")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise RuntimeError("AI response has an invalid chat completion envelope.")
    content = message.get("content")
    if not isinstance(content, str):
        raise RuntimeError("AI response has an invalid chat completion envelope.")
    return content


# ---------------------------------------------------------------------------
# Robust JSON parsing (D15 – defensive, all fields nullable)
# ---------------------------------------------------------------------------


def _strip_code_fence(text: str) -> str:
    """Remove markdown code fences from model output.

    Models sometimes wrap JSON in ```json ... ``` blocks despite instructions
    not to.  This strips the wrapper if present.
    """
    text = text.strip()
    # Remove ```json...``` or ```...``` fences
    fence_re = re.compile(r"^```(?:json)?\s*([\s\S]*?)\s*```$", re.MULTILINE)
    match = fence_re.match(text)
    if match:
        return match.group(1).strip()
    return text


def _safe_decimal(value: object, *, min_val: Decimal | None = None) -> Decimal | None:
    """Convert *value* to Decimal with optional range guard.

    Returns ``None`` on any conversion error or range violation (red-line 7:
    do not blindly trust model output).
    """
    if value is None:
        return None
    try:
        d = Decimal(str(value))
        if min_val is not None and d < min_val:
            return None
        return d
    except (InvalidOperation, ValueError, TypeError):
        return None


def _safe_date(value: object) -> date | None:
    """Convert *value* to a ``date`` (ISO 8601 string 'YYYY-MM-DD').

    Returns ``None`` on failure (red-line 7).
    """
    if value is None:
        return None
    try:
        text = str(value).strip()
        # Accept full datetime strings; take date part only.
        return date.fromisoformat(text[:10])
    except (ValueError, TypeError):
        return None


def _parse_model_output(raw_text: str) -> dict[str, Any]:
    """Parse and validate the raw model output into a field dict.

    Strategy:
    1. Strip code fences.
    2. ``json.loads`` the result.
    3. Unknown keys are silently ignored (only whitelisted keys survive).

    Returns an empty dict on any failure – callers must handle all-None prefill.
    """
    try:
        cleaned = _strip_code_fence(raw_text)
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        logger.debug("AI: could not parse JSON from model output")
        return {}

    if not isinstance(data, dict):
        return {}

    # Whitelist: only keys we expect (red-line 7 – ignore unknown model fields)
    allowed_keys = {
        "expense_date",
        "supplier_name",
        "net_amount",
        "vat_amount",
        "vat_rate_percent",
        "suggested_category_name",
        "confidence",
        "raw_model_note",
        "summary",
    }
    return {k: v for k, v in data.items() if k in allowed_keys}


# ---------------------------------------------------------------------------
# Category matching (best-effort by name)
# ---------------------------------------------------------------------------


async def _match_category(
    session: AsyncSession,
    company_id: uuid.UUID,
    name: str | None,
) -> uuid.UUID | None:
    """Best-effort case-insensitive match of ``name`` against expense categories.

    Returns the category UUID if found, otherwise ``None``.
    """
    if not name:
        return None

    from sqlalchemy import func, select

    from jai.models.dictionary import ExpenseCategory

    stmt = select(ExpenseCategory).where(
        ExpenseCategory.company_id == company_id,
        func.lower(ExpenseCategory.name) == name.strip().lower(),
    )
    result = await session.execute(stmt)
    cat = result.scalar_one_or_none()
    return cat.id if cat is not None else None


# ---------------------------------------------------------------------------
# Public API: extract_from_attachment / extract_from_files
# ---------------------------------------------------------------------------


async def extract_from_attachment(
    session: AsyncSession,
    company_id: uuid.UUID,
    attachment_id: uuid.UUID,
    client: httpx.AsyncClient | None = None,
    language: str | None = None,
) -> ExpenseAIPrefill:
    """Run AI receipt extraction on an already-uploaded attachment.

    Steps
    -----
    1. Load the attachment (404 on cross-company access).
    2. Check AI is enabled + key present (raises ``ValueError`` → 409).
    3. Read file bytes from storage.
    4. Rasterise to images (``_attachment_to_images``; 422 on unsupported MIME).
    5. Build Chat Completions request + call model.
    6. Parse output defensively → ``ExpenseAIPrefill`` (all nullable).
    7. Best-effort category name matching.
    8. Return the prefill. The legacy endpoint does not persist it; inbox
       callers persist only this validated model, never provider output.

    Raises
    ------
    LookupError
        If attachment doesn't exist or belongs to a different company (→ 404).
    ValueError
        If AI is disabled or ``api_key`` is empty (→ 409).
    ValueError
        If the MIME type is unsupported (→ 422).
    RuntimeError
        If the model call fails (→ 502).
    """
    from sqlalchemy import select

    from jai.models.expense_attachment import ExpenseAttachment

    # 1. Load attachment – scoped to company (cross-company → 404)
    stmt = select(ExpenseAttachment).where(
        ExpenseAttachment.id == attachment_id,
        ExpenseAttachment.company_id == company_id,
    )
    result = await session.execute(stmt)
    attachment = result.scalar_one_or_none()
    if attachment is None:
        raise LookupError(
            f"Attachment {attachment_id} not found or does not belong to this company."
        )
    if isinstance(attachment.receipt_inbox_id, uuid.UUID):
        raise LookupError(
            "Pending inbox attachments can only be extracted through the inbox workflow."
        )

    # 2. Check AI config
    ai_cfg = await _get_ai_config(session)
    if not ai_cfg.enabled:
        raise ValueError(
            "AI receipt extraction is disabled. Enable it in Settings → AI to use "
            "this feature."
        )
    if not ai_cfg.api_key:
        raise ValueError("AI API key is not configured. Add your key in Settings → AI.")
    if not ai_cfg.model:
        raise ValueError("AI model is not configured. Set the model name in Settings → AI.")

    # Keep the legacy entry point and its guards while sharing the exact same
    # parser/prompt/model implementation as whole inbox groups.
    return await extract_from_files(
        session, company_id,
        [(attachment.mime_type, get_storage().open(attachment.storage_key))],
        client=client, language=language,
    )


async def extract_from_files(
    session: AsyncSession,
    company_id: uuid.UUID,
    files: list[tuple[str, bytes]],
    client: httpx.AsyncClient | None = None,
    language: str | None = None,
    strict_response: bool = False,
) -> ExpenseAIPrefill:
    """Extract one suggestion from a complete ordered group of files."""
    if not files:
        raise ValueError("At least one receipt file is required.")
    ai_cfg = await _get_ai_config(session)
    if not ai_cfg.enabled:
        raise AIUnavailableError("AI_DISABLED", "AI receipt extraction is disabled.")
    if not ai_cfg.api_key or not ai_cfg.model or not ai_cfg.base_url:
        raise AIUnavailableError("AI_NOT_CONFIGURED", "AI is not configured.")

    # Build prompt and call model.
    date_context = (
        f"\n\nFor reference, today's date is {date.today().isoformat()}. "
        "A receipt date ON OR BEFORE today is normal — do NOT flag it as future. "
        "Only a date strictly AFTER today is unusual and may be flagged."
    )
    extra = (ai_cfg.receipt_prompt or "").strip()
    prompt_text = (
        DEFAULT_RECEIPT_PROMPT
        + (("\n\n" + extra) if extra else "")
        + date_context
        + _language_context(language)
        + _OUTPUT_CONTRACT
    )

    group_images: list[tuple[str, list[bytes]]] = []
    for mime, raw_bytes in files:
        normalized_mime = mime.split(";")[0].strip().lower()
        group_images.append((normalized_mime, _attachment_to_images(normalized_mime, raw_bytes)))
    messages = _build_group_messages(group_images, prompt_text)

    try:
        raw_response = await _call_chat_completions(
            base_url=ai_cfg.base_url,
            api_key=ai_cfg.api_key,
            model=ai_cfg.model,
            messages=messages,
            client=client,
            max_tokens=RECEIPT_EXTRACTION_MAX_TOKENS,
        )
    except httpx.TimeoutException as exc:
        raise AIExtractionError(
            "AI_TIMEOUT",
            "AI model request timed out. The receipt may be too complex or the "
            "service is temporarily unavailable.",
        ) from exc
    except httpx.ConnectError as exc:
        raise AIExtractionError(
            "AI_PROVIDER_ERROR",
            "Could not connect to the AI service. Check the base URL in Settings → AI.",
        ) from exc
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        logger.warning("AI provider returned HTTP %d", status_code)
        raise AIExtractionError(
            "AI_PROVIDER_ERROR",
            f"AI service returned HTTP {status_code}. Check your API key and model configuration.",
        ) from exc
    except ChatCompletionTruncatedError as exc:
        raise AIExtractionError("AI_INVALID_RESPONSE") from exc
    except RuntimeError as exc:
        raise AIExtractionError("AI_INVALID_RESPONSE") from exc
    except Exception as exc:
        raise AIExtractionError("AI_PROVIDER_ERROR") from exc

    # 6. Parse output defensively
    if strict_response:
        try:
            valid_json = json.loads(_strip_code_fence(raw_response))
        except (json.JSONDecodeError, ValueError):
            raise AIExtractionError("AI_INVALID_RESPONSE") from None
        if not isinstance(valid_json, dict):
            raise AIExtractionError("AI_INVALID_RESPONSE")
    parsed = _parse_model_output(raw_response)

    # 7. Best-effort category matching
    suggested_name = parsed.get("suggested_category_name")
    cat_name: str | None = str(suggested_name) if suggested_name else None
    cat_id = await _match_category(session, company_id, cat_name)

    # 8. Build prefill (all fields nullable; range-validate amounts)
    return ExpenseAIPrefill(
        expense_date=_safe_date(parsed.get("expense_date")),
        supplier_name=str(parsed["supplier_name"]) if parsed.get("supplier_name") else None,
        net_amount=_safe_decimal(parsed.get("net_amount"), min_val=Decimal("0")),
        vat_amount=_safe_decimal(parsed.get("vat_amount"), min_val=Decimal("0")),
        vat_rate_percent=_safe_decimal(parsed.get("vat_rate_percent"), min_val=Decimal("0")),
        suggested_category_name=cat_name,
        suggested_category_id=cat_id,
        raw_model_note=str(parsed["raw_model_note"]) if parsed.get("raw_model_note") else None,
        confidence=str(parsed["confidence"]) if parsed.get("confidence") else None,
        summary=str(parsed["summary"]) if parsed.get("summary") else None,
    )


# ---------------------------------------------------------------------------
# Public API: test_ai_config
# ---------------------------------------------------------------------------


async def test_ai_config(
    cfg: AiSettings,
    client: httpx.AsyncClient | None = None,
) -> AiTestResult:
    """Send a connectivity and multimodal probe using the given config.

    Uses the built-in ``_PROBE_PNG_BYTES`` (a 1×1 white PNG) so that no user
    receipt data is ever sent during a test (D14).

    Returns
    -------
    AiTestResult
        ``ok=True, multimodal=True`` on success.
        ``ok=False`` on auth/connection failure.
        ``ok=True, multimodal=False`` if the endpoint responds but rejects images.
    """
    if not cfg.api_key:
        return AiTestResult(
            ok=False, multimodal=False, detail="API key is not configured."
        )
    if not cfg.model:
        return AiTestResult(
            ok=False, multimodal=False, detail="Model name is not configured."
        )

    b64 = base64.b64encode(_PROBE_PNG_BYTES).decode("ascii")
    probe_messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                },
                {
                    "type": "text",
                    "text": (
                        "This is a connectivity test. Reply with exactly: "
                        '{"status": "ok"}'
                    ),
                },
            ],
        }
    ]

    try:
        await _call_chat_completions(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            model=cfg.model,
            messages=probe_messages,
            client=client,
            request_json_mode=False,  # Probe only checks connectivity + multimodal
        )
        return AiTestResult(
            ok=True,
            multimodal=True,
            detail="Connection successful and model accepted image input.",
        )

    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        # Try to get error detail from response body
        try:
            err_body = exc.response.json()
            err_msg = (
                err_body.get("error", {}).get("message", "")
                or str(err_body)
            )
        except Exception:
            err_msg = exc.response.text[:200]

        if status_code in (401, 403):
            return AiTestResult(
                ok=False,
                multimodal=False,
                detail=f"Authentication failed (HTTP {status_code}): {err_msg}",
            )
        if status_code == 404:
            return AiTestResult(
                ok=False,
                multimodal=False,
                detail=f"Model not found (HTTP 404): {err_msg or cfg.model!r}",
            )
        # 400 might mean the model doesn't support image inputs, but could also
        # indicate other provider-side rejections (e.g. image too small).
        # Report the provider's own message verbatim so the user can diagnose.
        if status_code == 400:
            return AiTestResult(
                ok=True,
                multimodal=False,
                detail=(
                    f"Endpoint returned HTTP 400 — the model may have rejected the "
                    f"request (see provider message): {err_msg}"
                ),
            )
        return AiTestResult(
            ok=False,
            multimodal=False,
            detail=f"HTTP {status_code}: {err_msg}",
        )

    except httpx.ConnectError as exc:
        return AiTestResult(
            ok=False,
            multimodal=False,
            detail=f"Connection failed: {exc}",
        )

    except httpx.TimeoutException:
        return AiTestResult(
            ok=False,
            multimodal=False,
            detail="Request timed out.",
        )

    except Exception as exc:
        return AiTestResult(
            ok=False,
            multimodal=False,
            detail=f"Unexpected error: {exc}",
        )

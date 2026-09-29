"""Unit tests for services/ai.py – M8 step 4.

All HTTP calls are mocked via respx; **no real network requests are made**.

Coverage:
- DEFAULT_RECEIPT_PROMPT always included; receipt_prompt is ADDITIVE (appended)
- _language_context: language="zh" → Simplified Chinese; "en"/None → English
- _attachment_to_images: image/* passthrough, PDF rasterisation (pypdfium2 real)
- _attachment_to_images: max_pages limit obeyed
- _attachment_to_images: unsupported MIME raises ValueError
- _parse_model_output: happy path JSON, code fence stripping, missing fields,
  extra keys (whitelisted), malformed JSON
- _safe_decimal: valid, invalid, below-min-val coercion
- _safe_date: valid ISO, invalid, datetime-prefix
- extract_from_attachment: (all mocked via respx)
  - happy path: fields mapped, category matched by name
  - model returns code-fenced JSON → still parsed
  - model omits fields → nulls in prefill
  - model adds extra keys → ignored
  - AI disabled → ValueError (409)
  - AI key empty → ValueError (409)
  - AI model empty → ValueError (409)
  - Connection error → RuntimeError (502)
  - Timeout → RuntimeError (502)
  - HTTP 4xx → RuntimeError (502)
  - Parse failure (empty response) → all-null prefill
- test_ai_config:
  - success → {ok: True, multimodal: True}
  - 401 auth failure → {ok: False, multimodal: False, detail contains 401}
  - 400 (model rejects image) → {ok: True, multimodal: False}
  - 404 model not found → {ok: False, multimodal: False}
  - ConnectError → {ok: False, multimodal: False}
  - Timeout → {ok: False, multimodal: False}
  - No key → {ok: False, multimodal: False}
  - No model → {ok: False, multimodal: False}
- AiSettingsRead: api_key_set bool, base_url/receipt_prompt returned as-is
- api_key never in response bodies (ExpenseAIPrefill / AiTestResult)
- PUT /settings/ai: omit api_key → keep, "" → clear, value → replace
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from jai.schemas.setting import AiSettings, AiTestResult
from jai.services.ai import (
    _OUTPUT_CONTRACT,
    _PROBE_PNG_BYTES,
    DEFAULT_RECEIPT_PROMPT,
    _attachment_to_images,
    _call_chat_completions,
    _language_context,
    _parse_model_output,
    _safe_date,
    _safe_decimal,
    _strip_code_fence,
)
from jai.services.ai import (
    test_ai_config as run_ai_config_test,
)

# ---------------------------------------------------------------------------
# Helpers: build fixture bytes
# ---------------------------------------------------------------------------


def _make_minimal_png() -> bytes:
    """Return the same probe PNG used in services/ai.py (single source of truth)."""
    return _PROBE_PNG_BYTES


def _make_minimal_jpeg() -> bytes:
    """Minimal valid JPEG (SOI + EOI markers, 2 bytes)."""
    # A proper but tiny JPEG that passes magic-byte check
    # SOI marker + APP0 + SOF0 + SOS + EOI – smallest valid single-pixel JPEG
    return bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000"  # SOI + APP0
        "ffdb004300080606070605080707070909080a0c"
        "140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20"
        "242e2720222c231c1c2837292c30313434341f27"
        "39413d2d41302d343232ffc0000b080001000101"
        "011100ffc40014000100000000000000000000000"
        "00000000affc40014100000000000000000000000"
        "000000000ffda0008010100003f00f50000ffd9"
    )


def _make_test_pdf_1page() -> bytes:
    """Return a minimal valid 1-page PDF."""
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n"
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100]\n"
        b"   /Contents 4 0 R /Resources << >> >>\nendobj\n"
        b"4 0 obj\n<< /Length 2 >>\nstream\n  \nendstream\nendobj\n"
        b"xref\n0 5\n0000000000 65535 f \n"
        b"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n1\n%%EOF"
    )


def _make_test_pdf_2pages() -> bytes:
    """Return a minimal valid 2-page PDF."""
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>\nendobj\n"
        b"3 0 obj\n"
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100]\n"
        b"   /Contents 4 0 R /Resources << >> >>\nendobj\n"
        b"4 0 obj\n<< /Length 2 >>\nstream\n  \nendstream\nendobj\n"
        b"5 0 obj\n"
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100]\n"
        b"   /Contents 6 0 R /Resources << >> >>\nendobj\n"
        b"6 0 obj\n<< /Length 2 >>\nstream\n  \nendstream\nendobj\n"
        b"xref\n0 7\n0000000000 65535 f \n"
        b"trailer\n<< /Size 7 /Root 1 0 R >>\nstartxref\n1\n%%EOF"
    )


# ---------------------------------------------------------------------------
# Prompt constant tests
# ---------------------------------------------------------------------------


def test_default_receipt_prompt_not_empty() -> None:
    assert len(DEFAULT_RECEIPT_PROMPT) > 50
    assert "BTW" in DEFAULT_RECEIPT_PROMPT or "VAT" in DEFAULT_RECEIPT_PROMPT


def test_output_contract_not_empty() -> None:
    assert len(_OUTPUT_CONTRACT) > 20
    assert "JSON" in _OUTPUT_CONTRACT


_MISSING_FINISH_REASON = object()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "finish_reason",
    [_MISSING_FINISH_REASON, None, "stop", "tool_calls"],
    ids=["missing", "explicit-null", "stop", "other-provider-value"],
)
async def test_chat_completion_accepts_normal_or_missing_finish_reason(
    finish_reason: object,
) -> None:
    choice: dict[str, object] = {"message": {"content": "{}"}}
    if finish_reason is not _MISSING_FINISH_REASON:
        choice["finish_reason"] = finish_reason
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"choices": [choice]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _call_chat_completions(
            base_url="https://example.test",
            api_key="test-key",
            model="vision",
            messages=[{"role": "user", "content": []}],
            client=client,
        )

    assert result == "{}"
    assert len(captured) == 1


def test_output_contract_in_default_prompt_combination() -> None:
    """_OUTPUT_CONTRACT is always last; DEFAULT_RECEIPT_PROMPT is always first."""
    from datetime import date as _date

    date_context = (
        f"\n\nFor reference, today's date is {_date.today().isoformat()}. "
        "A receipt date ON OR BEFORE today is normal — do NOT flag it as future. "
        "Only a date strictly AFTER today is unusual and may be flagged."
    )
    # No extra receipt_prompt: default → date_context → language_context → contract
    effective = (
        DEFAULT_RECEIPT_PROMPT
        + date_context
        + _language_context(None)
        + _OUTPUT_CONTRACT
    )
    assert effective.endswith(_OUTPUT_CONTRACT)
    assert effective.startswith(DEFAULT_RECEIPT_PROMPT)
    assert _date.today().isoformat() in effective


def test_custom_prompt_is_additive_appended_after_default() -> None:
    """receipt_prompt is APPENDED after DEFAULT; default is never replaced."""
    from datetime import date as _date

    extra = "Pay special attention to the line items on this receipt."
    date_context = (
        f"\n\nFor reference, today's date is {_date.today().isoformat()}. "
        "A receipt date ON OR BEFORE today is normal — do NOT flag it as future. "
        "Only a date strictly AFTER today is unusual and may be flagged."
    )
    effective = (
        DEFAULT_RECEIPT_PROMPT
        + "\n\n"
        + extra
        + date_context
        + _language_context(None)
        + _OUTPUT_CONTRACT
    )
    # Default must appear BEFORE the extra instruction
    assert effective.startswith(DEFAULT_RECEIPT_PROMPT)
    assert DEFAULT_RECEIPT_PROMPT in effective
    assert extra in effective
    assert effective.index(DEFAULT_RECEIPT_PROMPT) < effective.index(extra)
    assert effective.endswith(_OUTPUT_CONTRACT)


def test_empty_extra_prompt_no_double_newline() -> None:
    """When receipt_prompt is empty/blank, no extra newlines are inserted."""
    from datetime import date as _date

    date_context = (
        f"\n\nFor reference, today's date is {_date.today().isoformat()}. "
        "A receipt date ON OR BEFORE today is normal — do NOT flag it as future. "
        "Only a date strictly AFTER today is unusual and may be flagged."
    )
    extra = ""  # empty → should not add "\n\n" in the middle
    effective = (
        DEFAULT_RECEIPT_PROMPT
        + (("\n\n" + extra) if extra else "")
        + date_context
        + _language_context(None)
        + _OUTPUT_CONTRACT
    )
    # Prompt transitions cleanly from DEFAULT to date_context
    assert DEFAULT_RECEIPT_PROMPT + date_context in effective


# ---------------------------------------------------------------------------
# _language_context tests
# ---------------------------------------------------------------------------


def test_language_context_zh() -> None:
    """language='zh' → Simplified Chinese; proper-noun and category guards present."""
    ctx = _language_context("zh")
    assert "Simplified Chinese" in ctx
    assert "简体中文" in ctx
    assert "suggested_category_name" in ctx
    assert "English" in ctx  # category stays English
    assert "proper nouns" in ctx or "supplier names" in ctx


def test_language_context_zh_tw_prefix() -> None:
    """language='zh-TW' still starts with 'zh' → maps to Simplified Chinese block."""
    ctx = _language_context("zh-TW")
    assert "Simplified Chinese" in ctx


def test_language_context_en() -> None:
    """language='en' → English explanatory fields."""
    ctx = _language_context("en")
    assert "English" in ctx
    assert "Simplified Chinese" not in ctx


def test_language_context_none_defaults_to_english() -> None:
    """language=None → English (safe default)."""
    ctx = _language_context(None)
    assert "English" in ctx
    assert "Simplified Chinese" not in ctx


def test_language_context_category_always_english() -> None:
    """Regardless of language, suggested_category_name stays English."""
    for lang in ("zh", "en", None, "fr"):
        ctx = _language_context(lang)
        assert "suggested_category_name" in ctx
        assert "English" in ctx


def test_language_context_no_translate_proper_nouns() -> None:
    """Both zh and en contexts carry the do-not-translate guard for proper nouns."""
    for lang in ("zh", "en", None):
        ctx = _language_context(lang)
        # The guard phrase appears in both variants
        assert "supplier names" in ctx


# ---------------------------------------------------------------------------
# Probe PNG
# ---------------------------------------------------------------------------


def test_probe_png_has_valid_signature() -> None:
    """Regression guard: probe image must be PNG and at least 8×8 pixels.

    The original 1×1 probe was rejected by vision gateways (e.g. xAI enforces
    an 8×8 minimum) with HTTP 400, which was mis-classified as "model does not
    support multimodal input".  This test prevents that regression.
    """
    import struct

    assert _PROBE_PNG_BYTES[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(_PROBE_PNG_BYTES) > 30
    # IHDR is always the first chunk; width/height are at bytes 16–23 (big-endian)
    width, height = struct.unpack(">II", _PROBE_PNG_BYTES[16:24])
    assert width >= 8, f"Probe PNG width {width} is too small (must be ≥ 8)"
    assert height >= 8, f"Probe PNG height {height} is too small (must be ≥ 8)"
    # The current probe is 64×64
    assert width == 64 and height == 64, f"Expected 64×64 probe, got {width}×{height}"


# ---------------------------------------------------------------------------
# _strip_code_fence
# ---------------------------------------------------------------------------


def test_strip_code_fence_bare_json() -> None:
    raw = '{"a": 1}'
    assert _strip_code_fence(raw) == '{"a": 1}'


def test_strip_code_fence_with_json_fence() -> None:
    raw = '```json\n{"a": 1}\n```'
    assert _strip_code_fence(raw) == '{"a": 1}'


def test_strip_code_fence_with_plain_fence() -> None:
    raw = "```\n{\"a\": 1}\n```"
    assert _strip_code_fence(raw) == '{"a": 1}'


def test_strip_code_fence_no_fence() -> None:
    raw = "some text"
    assert _strip_code_fence(raw) == "some text"


# ---------------------------------------------------------------------------
# _safe_decimal
# ---------------------------------------------------------------------------


def test_safe_decimal_valid() -> None:
    assert _safe_decimal("21.00") == Decimal("21.00")


def test_safe_decimal_int_string() -> None:
    assert _safe_decimal(21) == Decimal("21")


def test_safe_decimal_none() -> None:
    assert _safe_decimal(None) is None


def test_safe_decimal_invalid_string() -> None:
    assert _safe_decimal("not-a-number") is None


def test_safe_decimal_below_min() -> None:
    assert _safe_decimal("-5.00", min_val=Decimal("0")) is None


def test_safe_decimal_zero_ok_with_min_zero() -> None:
    assert _safe_decimal("0", min_val=Decimal("0")) == Decimal("0")


# ---------------------------------------------------------------------------
# _safe_date
# ---------------------------------------------------------------------------


def test_safe_date_valid_iso() -> None:
    assert _safe_date("2024-01-15") == date(2024, 1, 15)


def test_safe_date_datetime_string() -> None:
    """Accept datetime strings; take date part only."""
    assert _safe_date("2024-01-15T12:34:56") == date(2024, 1, 15)


def test_safe_date_none() -> None:
    assert _safe_date(None) is None


def test_safe_date_invalid() -> None:
    assert _safe_date("not-a-date") is None


def test_safe_date_wrong_format() -> None:
    assert _safe_date("15/01/2024") is None


# ---------------------------------------------------------------------------
# _parse_model_output
# ---------------------------------------------------------------------------


def test_parse_model_output_happy_path() -> None:
    raw = '{"expense_date": "2024-03-01", "supplier_name": "IKEA", "net_amount": 100.00}'
    result = _parse_model_output(raw)
    assert result["expense_date"] == "2024-03-01"
    assert result["supplier_name"] == "IKEA"
    assert result["net_amount"] == 100.00


def test_parse_model_output_code_fence() -> None:
    raw = '```json\n{"supplier_name": "Test"}\n```'
    result = _parse_model_output(raw)
    assert result["supplier_name"] == "Test"


def test_parse_model_output_missing_fields() -> None:
    """Missing fields result in empty dict – caller handles None."""
    raw = '{"supplier_name": "ACME"}'
    result = _parse_model_output(raw)
    assert "expense_date" not in result
    assert result["supplier_name"] == "ACME"


def test_parse_model_output_extra_keys_ignored() -> None:
    """Keys not in the whitelist are silently dropped."""
    raw = '{"supplier_name": "X", "secret_field": "leak", "api_key": "sk-..."}'
    result = _parse_model_output(raw)
    assert "secret_field" not in result
    assert "api_key" not in result
    assert result["supplier_name"] == "X"


def test_parse_model_output_malformed_json_does_not_log_raw_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="jai.ai")
    private_raw_content = "private-receipt-raw-model-output sk-secret prompt-data-url"
    result = _parse_model_output(private_raw_content + " {")
    assert result == {}
    assert "AI: could not parse JSON from model output" in caplog.text
    assert private_raw_content not in caplog.text


def test_parse_model_output_empty_string() -> None:
    result = _parse_model_output("")
    assert result == {}


def test_parse_model_output_non_dict_json() -> None:
    result = _parse_model_output("[1, 2, 3]")
    assert result == {}


# ---------------------------------------------------------------------------
# _attachment_to_images – image passthrough
# ---------------------------------------------------------------------------


def test_attachment_to_images_png_passthrough() -> None:
    png = _make_minimal_png()
    result = _attachment_to_images("image/png", png)
    assert len(result) == 1
    assert result[0] == png


def test_attachment_to_images_jpeg_passthrough() -> None:
    # Use a minimal bytes that start with JPEG magic
    jpeg_magic = b"\xff\xd8\xff" + b"\x00" * 10
    result = _attachment_to_images("image/jpeg", jpeg_magic)
    assert len(result) == 1
    assert result[0] == jpeg_magic


def test_attachment_to_images_webp_passthrough() -> None:
    webp_magic = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 10
    result = _attachment_to_images("image/webp", webp_magic)
    assert len(result) == 1
    assert result[0] == webp_magic


def test_attachment_to_images_unsupported_mime_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported MIME type"):
        _attachment_to_images("text/csv", b"a,b,c")


def test_attachment_to_images_unsupported_mime_text_plain() -> None:
    with pytest.raises(ValueError, match="Unsupported MIME type"):
        _attachment_to_images("text/plain", b"hello")


# ---------------------------------------------------------------------------
# _attachment_to_images – PDF rasterisation (pypdfium2 real render)
# ---------------------------------------------------------------------------


def test_attachment_to_images_pdf_single_page() -> None:
    """Single-page PDF produces one PNG image."""
    pdf_bytes = _make_test_pdf_1page()
    result = _attachment_to_images("application/pdf", pdf_bytes)
    assert len(result) == 1
    # Result should be valid PNG
    assert result[0][:8] == b"\x89PNG\r\n\x1a\n"


def test_attachment_to_images_pdf_max_pages_limit() -> None:
    """2-page PDF with ai_pdf_max_pages=1 returns only 1 page."""
    pdf_bytes = _make_test_pdf_2pages()
    from unittest.mock import patch as mpatch

    from jai.config import get_settings

    settings = get_settings()
    with mpatch.object(settings, "ai_pdf_max_pages", 1):
        with mpatch.object(type(settings), "ai_pdf_max_pages", 1, create=True):
            # Directly patch via module-level settings
            with mpatch("jai.services.ai.get_settings") as mock_gs:
                fake_settings = MagicMock()
                fake_settings.ai_pdf_max_pages = 1
                fake_settings.ai_pdf_render_scale = 1.0
                mock_gs.return_value = fake_settings
                result = _attachment_to_images("application/pdf", pdf_bytes)
    assert len(result) == 1
    assert result[0][:8] == b"\x89PNG\r\n\x1a\n"


def test_attachment_to_images_pdf_2pages_both_rendered() -> None:
    """2-page PDF with sufficient max produces 2 PNG images."""
    pdf_bytes = _make_test_pdf_2pages()
    with patch("jai.services.ai.get_settings") as mock_gs:
        fake = MagicMock()
        fake.ai_pdf_max_pages = 5
        fake.ai_pdf_render_scale = 1.0
        mock_gs.return_value = fake
        result = _attachment_to_images("application/pdf", pdf_bytes)
    assert len(result) == 2
    for img in result:
        assert img[:8] == b"\x89PNG\r\n\x1a\n"


def test_attachment_to_images_pdf_mime_with_params() -> None:
    """MIME type with parameters (e.g. charset) still works."""
    pdf_bytes = _make_test_pdf_1page()
    with patch("jai.services.ai.get_settings") as mock_gs:
        fake = MagicMock()
        fake.ai_pdf_max_pages = 3
        fake.ai_pdf_render_scale = 1.0
        mock_gs.return_value = fake
        result = _attachment_to_images("application/pdf; charset=utf-8", pdf_bytes)
    assert len(result) == 1


# ---------------------------------------------------------------------------
# test_ai_config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ai_config_success() -> None:
    """Successful probe returns ok=True, multimodal=True."""
    cfg = AiSettings(enabled=True, base_url="http://fake-ai", api_key="sk-test", model="gpt-v")
    fake_response = {"choices": [{"message": {"content": '{"status": "ok"}'}}]}

    with respx.mock(base_url="http://fake-ai") as mock:
        mock.post("/chat/completions").mock(
            return_value=httpx.Response(200, json=fake_response)
        )
        async with httpx.AsyncClient() as client:
            result = await run_ai_config_test(cfg, client=client)

    assert result.ok is True
    assert result.multimodal is True
    assert "api_key" not in result.detail.lower() or "sk-test" not in result.detail


@pytest.mark.asyncio
async def test_ai_config_auth_failure_401() -> None:
    cfg = AiSettings(enabled=True, base_url="http://fake-ai", api_key="bad-key", model="gpt-v")
    error_body = {"error": {"message": "Invalid API key."}}

    with respx.mock(base_url="http://fake-ai") as mock:
        mock.post("/chat/completions").mock(
            return_value=httpx.Response(401, json=error_body)
        )
        async with httpx.AsyncClient() as client:
            result = await run_ai_config_test(cfg, client=client)

    assert result.ok is False
    assert result.multimodal is False
    assert "401" in result.detail or "auth" in result.detail.lower()
    # Key must not appear in detail
    assert "bad-key" not in result.detail


@pytest.mark.asyncio
async def test_ai_config_auth_failure_403() -> None:
    cfg = AiSettings(enabled=True, base_url="http://fake-ai", api_key="bad-key", model="gpt-v")

    with respx.mock(base_url="http://fake-ai") as mock:
        mock.post("/chat/completions").mock(
            return_value=httpx.Response(403, json={"error": {"message": "Forbidden"}})
        )
        async with httpx.AsyncClient() as client:
            result = await run_ai_config_test(cfg, client=client)

    assert result.ok is False
    assert result.multimodal is False


@pytest.mark.asyncio
async def test_ai_config_model_rejects_image_400() -> None:
    """HTTP 400 → model doesn't support multimodal, but endpoint is reachable."""
    cfg = AiSettings(enabled=True, base_url="http://fake-ai", api_key="sk", model="text-only")
    error_body = {"error": {"message": "model does not support image inputs"}}

    with respx.mock(base_url="http://fake-ai") as mock:
        mock.post("/chat/completions").mock(
            return_value=httpx.Response(400, json=error_body)
        )
        async with httpx.AsyncClient() as client:
            result = await run_ai_config_test(cfg, client=client)

    assert result.ok is True
    assert result.multimodal is False
    assert "400" in result.detail or "multimodal" in result.detail.lower()


@pytest.mark.asyncio
async def test_ai_config_model_not_found_404() -> None:
    cfg = AiSettings(enabled=True, base_url="http://fake-ai", api_key="sk", model="no-such-model")

    with respx.mock(base_url="http://fake-ai") as mock:
        mock.post("/chat/completions").mock(
            return_value=httpx.Response(404, json={"error": {"message": "Model not found"}})
        )
        async with httpx.AsyncClient() as client:
            result = await run_ai_config_test(cfg, client=client)

    assert result.ok is False
    assert result.multimodal is False
    assert "404" in result.detail or "not found" in result.detail.lower()


@pytest.mark.asyncio
async def test_ai_config_connect_error() -> None:
    cfg = AiSettings(enabled=True, base_url="http://fake-ai", api_key="sk", model="m")

    with respx.mock(base_url="http://fake-ai") as mock:
        mock.post("/chat/completions").mock(side_effect=httpx.ConnectError("refused"))
        async with httpx.AsyncClient() as client:
            result = await run_ai_config_test(cfg, client=client)

    assert result.ok is False
    assert result.multimodal is False
    assert "Connection" in result.detail or "refused" in result.detail


@pytest.mark.asyncio
async def test_ai_config_timeout() -> None:
    cfg = AiSettings(enabled=True, base_url="http://fake-ai", api_key="sk", model="m")

    with respx.mock(base_url="http://fake-ai") as mock:
        mock.post("/chat/completions").mock(
            side_effect=httpx.ReadTimeout("timed out", request=None)  # type: ignore[arg-type]
        )
        async with httpx.AsyncClient() as client:
            result = await run_ai_config_test(cfg, client=client)

    assert result.ok is False
    assert result.multimodal is False
    assert "timed out" in result.detail.lower() or "timeout" in result.detail.lower()


@pytest.mark.asyncio
async def test_ai_config_no_api_key() -> None:
    cfg = AiSettings(enabled=True, base_url="http://fake-ai", api_key="", model="m")
    result = await run_ai_config_test(cfg)
    assert result.ok is False
    assert result.multimodal is False
    assert "key" in result.detail.lower() or "api" in result.detail.lower()


@pytest.mark.asyncio
async def test_ai_config_no_model() -> None:
    cfg = AiSettings(enabled=True, base_url="http://fake-ai", api_key="sk", model="")
    result = await run_ai_config_test(cfg)
    assert result.ok is False
    assert result.multimodal is False
    assert "model" in result.detail.lower()


# ---------------------------------------------------------------------------
# AiSettingsRead – api_key masking
# ---------------------------------------------------------------------------


def test_ai_settings_read_masks_key() -> None:
    """api_key_set=True when key is present; raw key never in read model."""
    from jai.schemas.setting import AiSettingsRead

    read = AiSettingsRead(
        enabled=True,
        base_url="https://custom.api/v1",
        api_key_set=True,
        model="gpt-4o",
        receipt_prompt="Custom prompt",
    )
    # Serialise to dict and verify key not in values
    data = read.model_dump()
    assert "api_key" not in data
    assert data["api_key_set"] is True
    assert data["base_url"] == "https://custom.api/v1"
    assert data["receipt_prompt"] == "Custom prompt"


def test_ai_settings_read_key_not_set() -> None:
    from jai.schemas.setting import AiSettingsRead

    read = AiSettingsRead(enabled=False, api_key_set=False)
    data = read.model_dump()
    assert data["api_key_set"] is False


# ---------------------------------------------------------------------------
# AiSettings update – api_key semantics (None/""/"value")
# ---------------------------------------------------------------------------


def test_ai_settings_update_key_omitted_means_none() -> None:
    from jai.schemas.setting import AiSettingsUpdate

    body = AiSettingsUpdate(enabled=True)
    assert body.api_key is None  # omitted → None (keep existing)


def test_ai_settings_update_key_empty_clears() -> None:
    from jai.schemas.setting import AiSettingsUpdate

    body = AiSettingsUpdate(api_key="")
    assert body.api_key == ""  # explicit empty → clear


def test_ai_settings_update_key_value_replaces() -> None:
    from jai.schemas.setting import AiSettingsUpdate

    body = AiSettingsUpdate(api_key="sk-new-key")
    assert body.api_key == "sk-new-key"


# ---------------------------------------------------------------------------
# ExpenseAIPrefill – no credentials in schema
# ---------------------------------------------------------------------------


def test_expense_ai_prefill_no_credential_fields() -> None:
    """ExpenseAIPrefill must not contain api_key, token, secret, or similar."""
    from jai.schemas.expense import ExpenseAIPrefill

    prefill = ExpenseAIPrefill(
        expense_date=date(2024, 3, 1),
        supplier_name="ACME",
        net_amount=Decimal("100.00"),
    )
    data = prefill.model_dump()
    forbidden = {"api_key", "api_key_set", "token", "secret", "password", "key"}
    for field in forbidden:
        assert field not in data, f"Credential field {field!r} found in ExpenseAIPrefill"


def test_expense_ai_prefill_all_nullable() -> None:
    from jai.schemas.expense import ExpenseAIPrefill

    empty = ExpenseAIPrefill()
    data = empty.model_dump()
    for v in data.values():
        assert v is None


# ---------------------------------------------------------------------------
# AiTestResult – no credential fields
# ---------------------------------------------------------------------------


def test_ai_test_result_no_credential_fields() -> None:
    result = AiTestResult(ok=True, multimodal=True, detail="ok")
    data = result.model_dump()
    forbidden = {"api_key", "api_key_set", "token", "secret", "password"}
    for field in forbidden:
        assert field not in data


# ---------------------------------------------------------------------------
# extract_from_attachment – service-layer unit tests with mocked DB + storage
# ---------------------------------------------------------------------------


def _make_fake_attachment(
    attachment_id: str | None = None,
    mime_type: str = "image/png",
    storage_key: str = "receipts/c/e/a.png",
    company_id: str | None = None,
) -> MagicMock:
    import uuid as _uuid

    att = MagicMock()
    att.id = _uuid.UUID(attachment_id or str(_uuid.uuid4()))
    att.mime_type = mime_type
    att.storage_key = storage_key
    att.company_id = _uuid.UUID(company_id or str(_uuid.uuid4()))
    return att


@pytest.mark.asyncio
async def test_extract_from_attachment_ai_disabled() -> None:
    """AI disabled → raises ValueError (maps to 409)."""
    import uuid

    from jai.services.ai import extract_from_attachment

    session = AsyncMock()
    company_id = uuid.uuid4()
    att_id = uuid.uuid4()

    # Fake DB: attachment found
    fake_att = _make_fake_attachment(str(att_id), company_id=str(company_id))
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: fake_att))

    # Patch _get_ai_config to return disabled
    with patch("jai.services.ai._get_ai_config", return_value=AsyncMock(
        return_value=AiSettings(enabled=False)
    )):
        with patch("jai.services.ai._get_ai_config", new=AsyncMock(
            return_value=AiSettings(enabled=False)
        )):
            with pytest.raises(ValueError, match="disabled"):
                await extract_from_attachment(session, company_id, att_id)


@pytest.mark.asyncio
async def test_extract_from_attachment_no_api_key() -> None:
    """AI enabled but no key → raises ValueError (maps to 409)."""
    import uuid

    from jai.services.ai import extract_from_attachment

    session = AsyncMock()
    company_id = uuid.uuid4()
    att_id = uuid.uuid4()

    fake_att = _make_fake_attachment(str(att_id), company_id=str(company_id))
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: fake_att))

    with patch("jai.services.ai._get_ai_config", new=AsyncMock(
        return_value=AiSettings(enabled=True, api_key="", model="gpt-v")
    )):
        with pytest.raises(ValueError, match="API key"):
            await extract_from_attachment(session, company_id, att_id)


@pytest.mark.asyncio
async def test_extract_from_attachment_no_model() -> None:
    """AI enabled + key but no model → raises ValueError (maps to 409)."""
    import uuid

    from jai.services.ai import extract_from_attachment

    session = AsyncMock()
    company_id = uuid.uuid4()
    att_id = uuid.uuid4()

    fake_att = _make_fake_attachment(str(att_id), company_id=str(company_id))
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: fake_att))

    with patch("jai.services.ai._get_ai_config", new=AsyncMock(
        return_value=AiSettings(enabled=True, api_key="sk-test", model="")
    )):
        with pytest.raises(ValueError, match="model"):
            await extract_from_attachment(session, company_id, att_id)


@pytest.mark.asyncio
async def test_extract_from_attachment_cross_company_404() -> None:
    """Attachment not found / cross-company → LookupError (maps to 404)."""
    import uuid

    from jai.services.ai import extract_from_attachment

    session = AsyncMock()
    company_id = uuid.uuid4()
    att_id = uuid.uuid4()

    # DB returns None (not found)
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: None))

    with pytest.raises(LookupError):
        await extract_from_attachment(session, company_id, att_id)


@pytest.mark.asyncio
async def test_extract_from_attachment_unsupported_mime_422() -> None:
    """text/csv MIME → raises ValueError with 'Unsupported MIME' (maps to 422)."""
    import uuid

    from jai.services.ai import extract_from_attachment

    session = AsyncMock()
    company_id = uuid.uuid4()
    att_id = uuid.uuid4()

    fake_att = _make_fake_attachment(str(att_id), mime_type="text/csv", company_id=str(company_id))
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: fake_att))

    cfg = AiSettings(enabled=True, api_key="sk-test", model="gpt-v")
    fake_storage = MagicMock()
    fake_storage.open.return_value = b"a,b,c\n1,2,3"

    with patch("jai.services.ai._get_ai_config", new=AsyncMock(return_value=cfg)):
        with patch("jai.services.ai.get_storage", return_value=fake_storage):
            with pytest.raises(ValueError, match="Unsupported MIME type"):
                await extract_from_attachment(session, company_id, att_id)


@pytest.mark.asyncio
async def test_extract_from_attachment_happy_path_png() -> None:
    """Happy path: PNG attachment → model returns JSON → ExpenseAIPrefill populated."""
    import uuid

    from jai.services.ai import extract_from_attachment

    session = AsyncMock()
    company_id = uuid.uuid4()
    att_id = uuid.uuid4()

    png_bytes = _make_minimal_png()
    fake_att = _make_fake_attachment(
        str(att_id), mime_type="image/png", company_id=str(company_id)
    )

    # First execute → attachment lookup; subsequent executes → category match
    att_result = MagicMock(scalar_one_or_none=lambda: fake_att)
    cat_result = MagicMock(scalar_one_or_none=lambda: None)  # no category match
    session.execute = AsyncMock(side_effect=[att_result, cat_result])

    cfg = AiSettings(
        enabled=True, api_key="sk-test", model="gpt-v",
        base_url="http://fake-ai"
    )
    fake_storage = MagicMock()
    fake_storage.open.return_value = png_bytes

    model_json = (
        '{"expense_date": "2024-03-15", "supplier_name": "IKEA BV", '
        '"net_amount": 100.00, "vat_amount": 21.00, "vat_rate_percent": 21, '
        '"suggested_category_name": "Office supplies", "confidence": "high"}'
    )
    fake_response = {"choices": [{"message": {"content": model_json}}]}

    with patch("jai.services.ai._get_ai_config", new=AsyncMock(return_value=cfg)):
        with patch("jai.services.ai.get_storage", return_value=fake_storage):
            with respx.mock(base_url="http://fake-ai") as mock:
                mock.post("/chat/completions").mock(
                    return_value=httpx.Response(200, json=fake_response)
                )
                async with httpx.AsyncClient() as client:
                    prefill = await extract_from_attachment(
                        session, company_id, att_id, client=client
                    )

    assert prefill.expense_date == date(2024, 3, 15)
    assert prefill.supplier_name == "IKEA BV"
    assert prefill.net_amount == Decimal("100")
    assert prefill.vat_amount == Decimal("21")
    assert prefill.vat_rate_percent == Decimal("21")
    assert prefill.suggested_category_name == "Office supplies"
    assert prefill.confidence == "high"
    # No credentials in result
    data = prefill.model_dump()
    assert "api_key" not in data
    assert "sk-test" not in str(data)


@pytest.mark.asyncio
async def test_extract_from_files_preserves_mixed_image_mimes_and_filters_fields() -> None:
    """A mixed receipt group keeps each image MIME and persists only prefill fields."""
    import uuid

    from jai.services.ai import extract_from_files

    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=lambda: None)
    )
    cfg = AiSettings(
        enabled=True, api_key="sk-group-test", model="vision", base_url="http://group-ai"
    )
    # Inputs can be tiny opaque test bytes: only the MIME/message construction matters here.
    response = {"choices": [{"message": {"content": (
        '{"summary":"fuel","vat_treatment_id":"attacker",'
        '"vat_rate_id":"attacker","confidence":"medium"}'
    )}}]}
    requested: list[dict[str, object]] = []

    def capture(request: httpx.Request) -> httpx.Response:
        import json

        requested.append(json.loads(request.content))
        return httpx.Response(200, json=response)

    with patch("jai.services.ai._get_ai_config", new=AsyncMock(return_value=cfg)):
        with respx.mock(base_url="http://group-ai") as mock:
            mock.post("/chat/completions").mock(side_effect=capture)
            async with httpx.AsyncClient() as client:
                result = await extract_from_files(
                    session,
                    uuid.uuid4(),
                    [("image/jpeg", b"jpeg"), ("image/webp", b"webp"), ("image/png", b"png")],
                    client=client,
                )

    content = requested[0]["messages"][0]["content"]
    urls = [part["image_url"]["url"] for part in content if part["type"] == "image_url"]
    assert [url.split(";", 1)[0] for url in urls] == [
        "data:image/jpeg", "data:image/webp", "data:image/png"
    ]
    assert result.summary == "fuel"
    assert not hasattr(result, "vat_treatment_id")
    assert not hasattr(result, "vat_rate_id")


@pytest.mark.asyncio
async def test_extract_from_attachment_language_zh_in_prompt() -> None:
    """language='zh' → sent prompt contains Simplified Chinese context + category guard."""
    user_content = await _run_extract_capture_prompt(language="zh", base_url="http://fake-zh")
    assert "Simplified Chinese" in user_content or "简体中文" in user_content
    assert "suggested_category_name" in user_content
    # DEFAULT_RECEIPT_PROMPT always present
    assert "BTW" in user_content or "VAT" in user_content


async def _run_extract_capture_prompt(
    language: str | None,
    extra_receipt_prompt: str = "",
    base_url: str = "http://fake-ai2",
) -> str:
    """Helper: run extract_from_attachment with a minimal PNG and return the prompt text."""
    import uuid as _uuid

    from jai.services.ai import extract_from_attachment

    session = AsyncMock()
    company_id = _uuid.uuid4()
    att_id = _uuid.uuid4()

    png_bytes = _make_minimal_png()
    fake_att = _make_fake_attachment(
        str(att_id), mime_type="image/png", company_id=str(company_id)
    )
    att_result = MagicMock(scalar_one_or_none=lambda: fake_att)
    cat_result = MagicMock(scalar_one_or_none=lambda: None)
    session.execute = AsyncMock(side_effect=[att_result, cat_result])

    cfg = AiSettings(
        enabled=True, api_key="sk", model="m", base_url=base_url,
        receipt_prompt=extra_receipt_prompt,
    )
    fake_storage = MagicMock()
    fake_storage.open.return_value = png_bytes

    fake_response = {"choices": [{"message": {"content": '{"confidence": "low"}'}}]}
    captured: list[str] = []

    def capture_request(request: httpx.Request) -> httpx.Response:
        import json as _json
        body = _json.loads(request.content)
        messages = body.get("messages", [])
        for m in messages:
            if m.get("role") == "user":
                content = m["content"]
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            captured.append(part.get("text", ""))
                else:
                    captured.append(str(content))
        return httpx.Response(200, json=fake_response)

    with patch("jai.services.ai._get_ai_config", new=AsyncMock(return_value=cfg)):
        with patch("jai.services.ai.get_storage", return_value=fake_storage):
            with respx.mock(base_url=base_url) as mock:
                mock.post("/chat/completions").mock(side_effect=capture_request)
                async with httpx.AsyncClient() as client:
                    await extract_from_attachment(
                        session, company_id, att_id, client=client, language=language
                    )
    return " ".join(captured)


@pytest.mark.asyncio
async def test_extract_from_attachment_language_en_in_prompt() -> None:
    """language='en' or None → prompt contains English context, not Simplified Chinese."""
    for lang in ("en", None):
        user_content = await _run_extract_capture_prompt(language=lang, base_url="http://fake-en")
        assert "Simplified Chinese" not in user_content
        assert "English" in user_content


@pytest.mark.asyncio
async def test_extract_from_attachment_additive_receipt_prompt() -> None:
    """Non-empty receipt_prompt is APPENDED after DEFAULT; DEFAULT always present."""
    extra_instruction = "EXTRA_INSTRUCTION_MARKER_XYZ"
    user_content = await _run_extract_capture_prompt(
        language=None,
        extra_receipt_prompt=extra_instruction,
        base_url="http://fake-ai3",
    )

    # Both DEFAULT and extra instruction must be present
    assert "BTW" in user_content or "VAT" in user_content  # DEFAULT marker
    assert extra_instruction in user_content
    # DEFAULT appears BEFORE the extra instruction
    default_idx = user_content.find("BTW") if "BTW" in user_content else user_content.find("VAT")
    extra_idx = user_content.find(extra_instruction)
    assert default_idx < extra_idx


@pytest.mark.asyncio
async def test_extract_from_attachment_code_fenced_response() -> None:
    """Model wraps JSON in code fence → still parsed correctly."""
    import uuid

    from jai.services.ai import extract_from_attachment

    session = AsyncMock()
    company_id = uuid.uuid4()
    att_id = uuid.uuid4()

    png_bytes = _make_minimal_png()
    fake_att = _make_fake_attachment(
        str(att_id), mime_type="image/png", company_id=str(company_id)
    )
    att_result = MagicMock(scalar_one_or_none=lambda: fake_att)
    cat_result = MagicMock(scalar_one_or_none=lambda: None)
    session.execute = AsyncMock(side_effect=[att_result, cat_result])

    cfg = AiSettings(enabled=True, api_key="sk", model="m", base_url="http://fake")
    fake_storage = MagicMock()
    fake_storage.open.return_value = png_bytes

    # Model wraps in code fence
    fenced = '```json\n{"supplier_name": "Fence Corp", "net_amount": 50.00}\n```'
    fake_response = {"choices": [{"message": {"content": fenced}}]}

    with patch("jai.services.ai._get_ai_config", new=AsyncMock(return_value=cfg)):
        with patch("jai.services.ai.get_storage", return_value=fake_storage):
            with respx.mock(base_url="http://fake") as mock:
                mock.post("/chat/completions").mock(
                    return_value=httpx.Response(200, json=fake_response)
                )
                async with httpx.AsyncClient() as client:
                    prefill = await extract_from_attachment(
                        session, company_id, att_id, client=client
                    )

    assert prefill.supplier_name == "Fence Corp"
    assert prefill.net_amount == Decimal("50")


@pytest.mark.asyncio
async def test_extract_from_attachment_missing_fields_all_null() -> None:
    """Model returns empty JSON {} → all prefill fields are None."""
    import uuid

    from jai.services.ai import extract_from_attachment

    session = AsyncMock()
    company_id = uuid.uuid4()
    att_id = uuid.uuid4()

    fake_att = _make_fake_attachment(
        str(att_id), mime_type="image/png", company_id=str(company_id)
    )
    att_result = MagicMock(scalar_one_or_none=lambda: fake_att)
    cat_result = MagicMock(scalar_one_or_none=lambda: None)
    session.execute = AsyncMock(side_effect=[att_result, cat_result])

    cfg = AiSettings(enabled=True, api_key="sk", model="m", base_url="http://fake")
    fake_storage = MagicMock()
    fake_storage.open.return_value = _make_minimal_png()

    fake_response = {"choices": [{"message": {"content": "{}"}}]}

    with patch("jai.services.ai._get_ai_config", new=AsyncMock(return_value=cfg)):
        with patch("jai.services.ai.get_storage", return_value=fake_storage):
            with respx.mock(base_url="http://fake") as mock:
                mock.post("/chat/completions").mock(
                    return_value=httpx.Response(200, json=fake_response)
                )
                async with httpx.AsyncClient() as client:
                    prefill = await extract_from_attachment(
                        session, company_id, att_id, client=client
                    )

    assert prefill.expense_date is None
    assert prefill.supplier_name is None
    assert prefill.net_amount is None
    assert prefill.vat_amount is None


@pytest.mark.asyncio
async def test_extract_from_attachment_extra_keys_ignored() -> None:
    """Extra JSON keys (including api_key) are silently ignored."""
    import uuid

    from jai.services.ai import extract_from_attachment

    session = AsyncMock()
    company_id = uuid.uuid4()
    att_id = uuid.uuid4()

    fake_att = _make_fake_attachment(
        str(att_id), mime_type="image/png", company_id=str(company_id)
    )
    att_result = MagicMock(scalar_one_or_none=lambda: fake_att)
    cat_result = MagicMock(scalar_one_or_none=lambda: None)
    session.execute = AsyncMock(side_effect=[att_result, cat_result])

    cfg = AiSettings(enabled=True, api_key="sk", model="m", base_url="http://fake")
    fake_storage = MagicMock()
    fake_storage.open.return_value = _make_minimal_png()

    # Model leaks extra keys
    model_output = (
        '{"supplier_name": "X", "api_key": "leaked-key", '
        '"secret": "pw", "net_amount": 10.00}'
    )
    fake_response = {"choices": [{"message": {"content": model_output}}]}

    with patch("jai.services.ai._get_ai_config", new=AsyncMock(return_value=cfg)):
        with patch("jai.services.ai.get_storage", return_value=fake_storage):
            with respx.mock(base_url="http://fake") as mock:
                mock.post("/chat/completions").mock(
                    return_value=httpx.Response(200, json=fake_response)
                )
                async with httpx.AsyncClient() as client:
                    prefill = await extract_from_attachment(
                        session, company_id, att_id, client=client
                    )

    data = prefill.model_dump()
    assert "api_key" not in data
    assert "secret" not in data
    # Legitimate fields still present
    assert prefill.supplier_name == "X"


@pytest.mark.asyncio
async def test_extract_from_attachment_connection_error_502() -> None:
    """Connection failure → RuntimeError (maps to 502)."""
    import uuid

    from jai.services.ai import extract_from_attachment

    session = AsyncMock()
    company_id = uuid.uuid4()
    att_id = uuid.uuid4()

    fake_att = _make_fake_attachment(
        str(att_id), mime_type="image/png", company_id=str(company_id)
    )
    att_result = MagicMock(scalar_one_or_none=lambda: fake_att)
    session.execute = AsyncMock(return_value=att_result)

    cfg = AiSettings(enabled=True, api_key="sk", model="m", base_url="http://fake")
    fake_storage = MagicMock()
    fake_storage.open.return_value = _make_minimal_png()

    with patch("jai.services.ai._get_ai_config", new=AsyncMock(return_value=cfg)):
        with patch("jai.services.ai.get_storage", return_value=fake_storage):
            with respx.mock(base_url="http://fake") as mock:
                mock.post("/chat/completions").mock(
                    side_effect=httpx.ConnectError("refused")
                )
                async with httpx.AsyncClient() as client:
                    with pytest.raises(RuntimeError, match="connect"):
                        await extract_from_attachment(
                            session, company_id, att_id, client=client
                        )


@pytest.mark.asyncio
async def test_extract_from_attachment_timeout_502() -> None:
    """Timeout → RuntimeError (maps to 502)."""
    import uuid

    from jai.services.ai import extract_from_attachment

    session = AsyncMock()
    company_id = uuid.uuid4()
    att_id = uuid.uuid4()

    fake_att = _make_fake_attachment(
        str(att_id), mime_type="image/png", company_id=str(company_id)
    )
    att_result = MagicMock(scalar_one_or_none=lambda: fake_att)
    session.execute = AsyncMock(return_value=att_result)

    cfg = AiSettings(enabled=True, api_key="sk", model="m", base_url="http://fake")
    fake_storage = MagicMock()
    fake_storage.open.return_value = _make_minimal_png()

    with patch("jai.services.ai._get_ai_config", new=AsyncMock(return_value=cfg)):
        with patch("jai.services.ai.get_storage", return_value=fake_storage):
            with respx.mock(base_url="http://fake") as mock:
                mock.post("/chat/completions").mock(
                    side_effect=httpx.ReadTimeout("timed out", request=None)  # type: ignore[arg-type]
                )
                async with httpx.AsyncClient() as client:
                    with pytest.raises(RuntimeError, match="timed out"):
                        await extract_from_attachment(
                            session, company_id, att_id, client=client
                        )


@pytest.mark.asyncio
async def test_extract_from_attachment_http_error_502() -> None:
    """HTTP 500 from model → RuntimeError (maps to 502)."""
    import uuid

    from jai.services.ai import extract_from_attachment

    session = AsyncMock()
    company_id = uuid.uuid4()
    att_id = uuid.uuid4()

    fake_att = _make_fake_attachment(
        str(att_id), mime_type="image/png", company_id=str(company_id)
    )
    att_result = MagicMock(scalar_one_or_none=lambda: fake_att)
    session.execute = AsyncMock(return_value=att_result)

    cfg = AiSettings(enabled=True, api_key="sk", model="m", base_url="http://fake")
    fake_storage = MagicMock()
    fake_storage.open.return_value = _make_minimal_png()

    with patch("jai.services.ai._get_ai_config", new=AsyncMock(return_value=cfg)):
        with patch("jai.services.ai.get_storage", return_value=fake_storage):
            with respx.mock(base_url="http://fake") as mock:
                mock.post("/chat/completions").mock(
                    return_value=httpx.Response(500, json={"error": "internal"})
                )
                async with httpx.AsyncClient() as client:
                    with pytest.raises(RuntimeError, match="500"):
                        await extract_from_attachment(
                            session, company_id, att_id, client=client
                        )


@pytest.mark.asyncio
async def test_extract_from_attachment_pdf_rasterised_as_images() -> None:
    """PDF attachment → pypdfium2 renders pages → each page becomes image_url in request."""
    import uuid

    from jai.services.ai import extract_from_attachment

    session = AsyncMock()
    company_id = uuid.uuid4()
    att_id = uuid.uuid4()

    pdf_bytes = _make_test_pdf_1page()
    fake_att = _make_fake_attachment(
        str(att_id), mime_type="application/pdf", company_id=str(company_id)
    )
    att_result = MagicMock(scalar_one_or_none=lambda: fake_att)
    cat_result = MagicMock(scalar_one_or_none=lambda: None)
    session.execute = AsyncMock(side_effect=[att_result, cat_result])

    cfg = AiSettings(
        enabled=True, api_key="sk", model="gpt-v", base_url="http://fake"
    )
    fake_storage = MagicMock()
    fake_storage.open.return_value = pdf_bytes

    captured_request: list[httpx.Request] = []

    def capture_handler(request: httpx.Request) -> httpx.Response:
        captured_request.append(request)
        fake_response = {"choices": [{"message": {"content": '{"supplier_name": "PDF Corp"}'}}]}
        return httpx.Response(200, json=fake_response)

    with patch("jai.services.ai._get_ai_config", new=AsyncMock(return_value=cfg)):
        with patch("jai.services.ai.get_storage", return_value=fake_storage):
            with patch("jai.services.ai.get_settings") as mock_gs:
                fake_settings = MagicMock()
                fake_settings.ai_pdf_max_pages = 3
                fake_settings.ai_pdf_render_scale = 1.0
                mock_gs.return_value = fake_settings
                with respx.mock(base_url="http://fake") as mock:
                    mock.post("/chat/completions").mock(side_effect=capture_handler)
                    async with httpx.AsyncClient() as client:
                        prefill = await extract_from_attachment(
                            session, company_id, att_id, client=client
                        )

    assert prefill.supplier_name == "PDF Corp"
    # Verify the request contained an image_url part
    assert captured_request
    import json as _json
    body = _json.loads(captured_request[0].content)
    messages = body["messages"]
    user_msg = messages[0]
    content_parts = user_msg["content"]
    image_parts = [p for p in content_parts if p.get("type") == "image_url"]
    assert len(image_parts) >= 1
    # Image should be base64-encoded PNG
    img_url = image_parts[0]["image_url"]["url"]
    assert img_url.startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_extract_from_attachment_pdf_max_pages_limits_image_parts() -> None:
    """2-page PDF with max_pages=1 → only 1 image_url part sent to model."""
    import uuid

    from jai.services.ai import extract_from_attachment

    session = AsyncMock()
    company_id = uuid.uuid4()
    att_id = uuid.uuid4()

    pdf_bytes = _make_test_pdf_2pages()
    fake_att = _make_fake_attachment(
        str(att_id), mime_type="application/pdf", company_id=str(company_id)
    )
    att_result = MagicMock(scalar_one_or_none=lambda: fake_att)
    cat_result = MagicMock(scalar_one_or_none=lambda: None)
    session.execute = AsyncMock(side_effect=[att_result, cat_result])

    cfg = AiSettings(enabled=True, api_key="sk", model="m", base_url="http://fake")
    fake_storage = MagicMock()
    fake_storage.open.return_value = pdf_bytes

    captured_request: list[httpx.Request] = []

    def capture_handler(request: httpx.Request) -> httpx.Response:
        captured_request.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    with patch("jai.services.ai._get_ai_config", new=AsyncMock(return_value=cfg)):
        with patch("jai.services.ai.get_storage", return_value=fake_storage):
            with patch("jai.services.ai.get_settings") as mock_gs:
                fake_settings = MagicMock()
                fake_settings.ai_pdf_max_pages = 1  # limit to 1
                fake_settings.ai_pdf_render_scale = 1.0
                mock_gs.return_value = fake_settings
                with respx.mock(base_url="http://fake") as mock:
                    mock.post("/chat/completions").mock(side_effect=capture_handler)
                    async with httpx.AsyncClient() as client:
                        await extract_from_attachment(
                            session, company_id, att_id, client=client
                        )

    import json as _json
    body = _json.loads(captured_request[0].content)
    content_parts = body["messages"][0]["content"]
    image_parts = [p for p in content_parts if p.get("type") == "image_url"]
    assert len(image_parts) == 1  # max_pages=1 → only 1 image


@pytest.mark.asyncio
async def test_extract_from_attachment_custom_prompt_with_output_contract() -> None:
    """receipt_prompt is APPENDED after DEFAULT (additive); _OUTPUT_CONTRACT always last."""
    import json as _json
    import uuid

    from jai.services.ai import extract_from_attachment

    session = AsyncMock()
    company_id = uuid.uuid4()
    att_id = uuid.uuid4()

    fake_att = _make_fake_attachment(
        str(att_id), mime_type="image/png", company_id=str(company_id)
    )
    att_result = MagicMock(scalar_one_or_none=lambda: fake_att)
    cat_result = MagicMock(scalar_one_or_none=lambda: None)
    session.execute = AsyncMock(side_effect=[att_result, cat_result])

    custom_prompt = "Extract only the total amount from this Dutch receipt."
    cfg = AiSettings(
        enabled=True, api_key="sk", model="m", base_url="http://fake",
        receipt_prompt=custom_prompt,
    )
    fake_storage = MagicMock()
    fake_storage.open.return_value = _make_minimal_png()

    captured_request: list[httpx.Request] = []

    def capture_handler(request: httpx.Request) -> httpx.Response:
        captured_request.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    with patch("jai.services.ai._get_ai_config", new=AsyncMock(return_value=cfg)):
        with patch("jai.services.ai.get_storage", return_value=fake_storage):
            with respx.mock(base_url="http://fake") as mock:
                mock.post("/chat/completions").mock(side_effect=capture_handler)
                async with httpx.AsyncClient() as client:
                    await extract_from_attachment(
                        session, company_id, att_id, client=client
                    )

    body = _json.loads(captured_request[0].content)
    user_text_parts = [
        p["text"]
        for p in body["messages"][0]["content"]
        if p.get("type") == "text"
    ]
    assert user_text_parts
    sent_text = user_text_parts[0]

    # Both DEFAULT and extra prompt must appear (additive, not replacement)
    assert DEFAULT_RECEIPT_PROMPT in sent_text
    assert custom_prompt in sent_text
    # DEFAULT appears BEFORE the extra instruction
    assert sent_text.index(DEFAULT_RECEIPT_PROMPT) < sent_text.index(custom_prompt)
    # Output contract still appended last
    assert _OUTPUT_CONTRACT in sent_text
    assert sent_text.endswith(_OUTPUT_CONTRACT)
    # Date context injected
    from datetime import date as _date
    assert _date.today().isoformat() in sent_text
    assert "today's date is" in sent_text


@pytest.mark.asyncio
async def test_extract_from_attachment_default_prompt_with_output_contract() -> None:
    """Empty receipt_prompt → DEFAULT_RECEIPT_PROMPT + _OUTPUT_CONTRACT sent."""
    import json as _json
    import uuid

    from jai.services.ai import extract_from_attachment

    session = AsyncMock()
    company_id = uuid.uuid4()
    att_id = uuid.uuid4()

    fake_att = _make_fake_attachment(
        str(att_id), mime_type="image/png", company_id=str(company_id)
    )
    att_result = MagicMock(scalar_one_or_none=lambda: fake_att)
    cat_result = MagicMock(scalar_one_or_none=lambda: None)
    session.execute = AsyncMock(side_effect=[att_result, cat_result])

    cfg = AiSettings(
        enabled=True, api_key="sk", model="m", base_url="http://fake",
        receipt_prompt="",  # empty → fallback to DEFAULT
    )
    fake_storage = MagicMock()
    fake_storage.open.return_value = _make_minimal_png()

    captured_request: list[httpx.Request] = []

    def capture_handler(request: httpx.Request) -> httpx.Response:
        captured_request.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    with patch("jai.services.ai._get_ai_config", new=AsyncMock(return_value=cfg)):
        with patch("jai.services.ai.get_storage", return_value=fake_storage):
            with respx.mock(base_url="http://fake") as mock:
                mock.post("/chat/completions").mock(side_effect=capture_handler)
                async with httpx.AsyncClient() as client:
                    await extract_from_attachment(
                        session, company_id, att_id, client=client
                    )

    body = _json.loads(captured_request[0].content)
    user_text_parts = [
        p["text"]
        for p in body["messages"][0]["content"]
        if p.get("type") == "text"
    ]
    sent_text = user_text_parts[0]
    assert DEFAULT_RECEIPT_PROMPT in sent_text
    assert _OUTPUT_CONTRACT in sent_text
    # Date context injected between default prompt and contract
    from datetime import date as _date
    assert _date.today().isoformat() in sent_text
    assert "today's date is" in sent_text


# ---------------------------------------------------------------------------
# F1 – _get_ai_config: field-level DB→env fallback (fixer tests, M8 step 4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_ai_config_pure_env_no_db_row() -> None:
    """When no DB row exists, all fields come from env config (pure env scenario)."""
    from jai.services.ai import _get_ai_config

    session = AsyncMock()

    # get_setting is imported inside _get_ai_config; patch where it's defined
    with patch("jai.services.settings.get_setting", new=AsyncMock(return_value=None)):
        with patch("jai.services.ai.get_settings") as mock_gs:
            fake_settings = MagicMock()
            fake_settings.ai_enabled = True
            fake_settings.ai_base_url = "https://env.example.com/v1"
            fake_settings.ai_api_key = "env-key"
            fake_settings.ai_model = "env-model"
            mock_gs.return_value = fake_settings
            cfg = await _get_ai_config(session)

    assert cfg.api_key == "env-key"
    assert cfg.base_url == "https://env.example.com/v1"
    assert cfg.model == "env-model"
    assert cfg.enabled is True


@pytest.mark.asyncio
async def test_get_ai_config_db_row_with_empty_api_key_falls_back_to_env() -> None:
    """DB row exists but api_key is empty → api_key falls back to env (F1 fix)."""
    from jai.services.ai import _get_ai_config

    session = AsyncMock()
    # DB row has enabled=True, base_url/model set, but api_key empty
    db_cfg = AiSettings(
        enabled=True,
        base_url="https://db.example.com/v1",
        api_key="",  # empty → should fall back to env
        model="db-model",
        receipt_prompt="",
    )

    with patch("jai.services.settings.get_setting", new=AsyncMock(return_value=db_cfg)):
        with patch("jai.services.ai.get_settings") as mock_gs:
            fake_settings = MagicMock()
            fake_settings.ai_enabled = False
            fake_settings.ai_base_url = "https://env.example.com/v1"
            fake_settings.ai_api_key = "env-fallback-key"
            fake_settings.ai_model = "env-model"
            mock_gs.return_value = fake_settings
            cfg = await _get_ai_config(session)

    # api_key was empty in DB → must come from env
    assert cfg.api_key == "env-fallback-key"
    # base_url and model are set in DB → use DB values
    assert cfg.base_url == "https://db.example.com/v1"
    assert cfg.model == "db-model"
    # enabled comes from DB row
    assert cfg.enabled is True


@pytest.mark.asyncio
async def test_get_ai_config_db_row_with_all_fields_uses_db() -> None:
    """DB row with all fields populated → all fields from DB, env not consulted."""
    from jai.services.ai import _get_ai_config

    session = AsyncMock()
    db_cfg = AiSettings(
        enabled=True,
        base_url="https://db.example.com/v1",
        api_key="db-key",
        model="db-model",
        receipt_prompt="Custom prompt",
    )

    with patch("jai.services.settings.get_setting", new=AsyncMock(return_value=db_cfg)):
        with patch("jai.services.ai.get_settings") as mock_gs:
            fake_settings = MagicMock()
            fake_settings.ai_enabled = False
            fake_settings.ai_base_url = "https://env.example.com/v1"
            fake_settings.ai_api_key = "env-key"
            fake_settings.ai_model = "env-model"
            mock_gs.return_value = fake_settings
            cfg = await _get_ai_config(session)

    # All fields from DB
    assert cfg.api_key == "db-key"
    assert cfg.base_url == "https://db.example.com/v1"
    assert cfg.model == "db-model"
    assert cfg.receipt_prompt == "Custom prompt"


@pytest.mark.asyncio
async def test_get_ai_config_db_row_empty_base_url_falls_back_to_env() -> None:
    """DB row has empty base_url → base_url falls back to env."""
    from jai.services.ai import _get_ai_config

    session = AsyncMock()
    db_cfg = AiSettings(
        enabled=True,
        base_url="",  # empty → should fall back to env
        api_key="db-key",
        model="db-model",
        receipt_prompt="",
    )

    with patch("jai.services.settings.get_setting", new=AsyncMock(return_value=db_cfg)):
        with patch("jai.services.ai.get_settings") as mock_gs:
            fake_settings = MagicMock()
            fake_settings.ai_enabled = False
            fake_settings.ai_base_url = "https://env-fallback.com/v1"
            fake_settings.ai_api_key = ""
            fake_settings.ai_model = "env-model"
            mock_gs.return_value = fake_settings
            cfg = await _get_ai_config(session)

    # base_url from env fallback
    assert cfg.base_url == "https://env-fallback.com/v1"
    # api_key from DB
    assert cfg.api_key == "db-key"


# ---------------------------------------------------------------------------
# F2 – response_format: probe has no json mode; extraction retries on 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_request_does_not_contain_response_format() -> None:
    """Connectivity probe must NOT include response_format in the request body (F2)."""
    import json as _json

    cfg = AiSettings(enabled=True, base_url="http://fake-ai", api_key="sk", model="m")
    captured: list[httpx.Request] = []

    def capture_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"status": "ok"}'}}]},
        )

    with respx.mock(base_url="http://fake-ai") as mock:
        mock.post("/chat/completions").mock(side_effect=capture_handler)
        async with httpx.AsyncClient() as client:
            await run_ai_config_test(cfg, client=client)

    assert captured, "No request captured"
    body = _json.loads(captured[0].content)
    assert "response_format" not in body, (
        "Probe request must not contain response_format; "
        "it would misclassify json-mode-unsupported endpoints as non-multimodal"
    )
    assert body["max_tokens"] == 512


@pytest.mark.asyncio
async def test_probe_400_json_mode_only_still_multimodal_false() -> None:
    """Without response_format in probe, a 400 means truly no multimodal support."""
    cfg = AiSettings(enabled=True, base_url="http://fake-ai", api_key="sk", model="text-only")

    with respx.mock(base_url="http://fake-ai") as mock:
        mock.post("/chat/completions").mock(
            return_value=httpx.Response(
                400, json={"error": {"message": "model does not support image inputs"}}
            )
        )
        async with httpx.AsyncClient() as client:
            result = await run_ai_config_test(cfg, client=client)

    # Still correctly reported as multimodal=False (400 without response_format
    # means the image itself was rejected)
    assert result.ok is True
    assert result.multimodal is False


@pytest.mark.asyncio
async def test_extraction_retries_without_response_format_on_400() -> None:
    """extract_from_attachment: if endpoint returns 400 to json-mode request,
    retry without response_format and succeed (F2 fix)."""
    import json as _json
    import uuid

    from jai.services.ai import extract_from_attachment

    session = AsyncMock()
    company_id = uuid.uuid4()
    att_id = uuid.uuid4()

    png_bytes = _make_minimal_png()
    fake_att = _make_fake_attachment(
        str(att_id), mime_type="image/png", company_id=str(company_id)
    )
    att_result = MagicMock(scalar_one_or_none=lambda: fake_att)
    cat_result = MagicMock(scalar_one_or_none=lambda: None)
    session.execute = AsyncMock(side_effect=[att_result, cat_result])

    cfg = AiSettings(enabled=True, api_key="sk-test", model="gpt-v", base_url="http://fake-ai")
    fake_storage = MagicMock()
    fake_storage.open.return_value = png_bytes

    model_json = '{"supplier_name": "RetryShop", "net_amount": 42.00}'
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = _json.loads(request.content)
        if "response_format" in body:
            # Simulate endpoint that rejects json mode
            return httpx.Response(400, json={"error": {"message": "json mode not supported"}})
        # Without response_format → success
        return httpx.Response(
            200, json={"choices": [{"message": {"content": model_json}}]}
        )

    with patch("jai.services.ai._get_ai_config", new=AsyncMock(return_value=cfg)):
        with patch("jai.services.ai.get_storage", return_value=fake_storage):
            with respx.mock(base_url="http://fake-ai") as mock:
                mock.post("/chat/completions").mock(side_effect=handler)
                async with httpx.AsyncClient() as client:
                    prefill = await extract_from_attachment(
                        session, company_id, att_id, client=client
                    )

    # Should have retried (2 calls total: first with response_format → 400, then without)
    assert len(calls) == 2
    first_body = _json.loads(calls[0].content)
    second_body = _json.loads(calls[1].content)
    assert "response_format" in first_body
    assert "response_format" not in second_body
    assert first_body["max_tokens"] == 1024
    assert second_body["max_tokens"] == 1024
    # Result is correctly populated from the retry
    assert prefill.supplier_name == "RetryShop"
    assert prefill.net_amount == Decimal("42")


@pytest.mark.asyncio
async def test_extraction_request_contains_response_format_on_success() -> None:
    """Real extraction call includes response_format when endpoint supports it."""
    import json as _json
    import uuid

    from jai.services.ai import extract_from_attachment

    session = AsyncMock()
    company_id = uuid.uuid4()
    att_id = uuid.uuid4()

    png_bytes = _make_minimal_png()
    fake_att = _make_fake_attachment(
        str(att_id), mime_type="image/png", company_id=str(company_id)
    )
    att_result = MagicMock(scalar_one_or_none=lambda: fake_att)
    cat_result = MagicMock(scalar_one_or_none=lambda: None)
    session.execute = AsyncMock(side_effect=[att_result, cat_result])

    cfg = AiSettings(enabled=True, api_key="sk-test", model="gpt-v", base_url="http://fake-ai")
    fake_storage = MagicMock()
    fake_storage.open.return_value = png_bytes

    captured: list[httpx.Request] = []

    def capture_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"supplier_name": "OKShop"}'}}]}
        )

    with patch("jai.services.ai._get_ai_config", new=AsyncMock(return_value=cfg)):
        with patch("jai.services.ai.get_storage", return_value=fake_storage):
            with respx.mock(base_url="http://fake-ai") as mock:
                mock.post("/chat/completions").mock(side_effect=capture_handler)
                async with httpx.AsyncClient() as client:
                    prefill = await extract_from_attachment(
                        session, company_id, att_id, client=client
                    )

    assert len(captured) == 1  # No retry needed
    body = _json.loads(captured[0].content)
    # response_format should be present in normal extraction
    assert "response_format" in body
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == 1024
    assert prefill.supplier_name == "OKShop"


# ---------------------------------------------------------------------------
# Summary field – parsing and prefill (Task B)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_summary_present() -> None:
    """Model returns a summary → prefill.summary equals that value."""
    import uuid

    from jai.services.ai import extract_from_attachment

    session = AsyncMock()
    company_id = uuid.uuid4()
    att_id = uuid.uuid4()

    png_bytes = _make_minimal_png()
    fake_att = _make_fake_attachment(
        str(att_id), mime_type="image/png", company_id=str(company_id)
    )
    att_result = MagicMock(scalar_one_or_none=lambda: fake_att)
    cat_result = MagicMock(scalar_one_or_none=lambda: None)
    session.execute = AsyncMock(side_effect=[att_result, cat_result])

    cfg = AiSettings(enabled=True, api_key="sk", model="m", base_url="http://fake")
    fake_storage = MagicMock()
    fake_storage.open.return_value = png_bytes

    model_json = (
        '{"supplier_name": "Office Depot", "net_amount": 45.00, '
        '"summary": "A4 printer paper, 5 reams"}'
    )
    fake_response = {"choices": [{"message": {"content": model_json}}]}

    with patch("jai.services.ai._get_ai_config", new=AsyncMock(return_value=cfg)):
        with patch("jai.services.ai.get_storage", return_value=fake_storage):
            with respx.mock(base_url="http://fake") as mock:
                mock.post("/chat/completions").mock(
                    return_value=httpx.Response(200, json=fake_response)
                )
                async with httpx.AsyncClient() as client:
                    prefill = await extract_from_attachment(
                        session, company_id, att_id, client=client
                    )

    assert prefill.summary == "A4 printer paper, 5 reams"
    assert prefill.supplier_name == "Office Depot"


@pytest.mark.asyncio
async def test_extract_summary_absent() -> None:
    """Model omits summary → prefill.summary is None."""
    import uuid

    from jai.services.ai import extract_from_attachment

    session = AsyncMock()
    company_id = uuid.uuid4()
    att_id = uuid.uuid4()

    png_bytes = _make_minimal_png()
    fake_att = _make_fake_attachment(
        str(att_id), mime_type="image/png", company_id=str(company_id)
    )
    att_result = MagicMock(scalar_one_or_none=lambda: fake_att)
    cat_result = MagicMock(scalar_one_or_none=lambda: None)
    session.execute = AsyncMock(side_effect=[att_result, cat_result])

    cfg = AiSettings(enabled=True, api_key="sk", model="m", base_url="http://fake")
    fake_storage = MagicMock()
    fake_storage.open.return_value = png_bytes

    model_json = '{"supplier_name": "No Summary Shop", "net_amount": 10.00}'
    fake_response = {"choices": [{"message": {"content": model_json}}]}

    with patch("jai.services.ai._get_ai_config", new=AsyncMock(return_value=cfg)):
        with patch("jai.services.ai.get_storage", return_value=fake_storage):
            with respx.mock(base_url="http://fake") as mock:
                mock.post("/chat/completions").mock(
                    return_value=httpx.Response(200, json=fake_response)
                )
                async with httpx.AsyncClient() as client:
                    prefill = await extract_from_attachment(
                        session, company_id, att_id, client=client
                    )

    assert prefill.summary is None


@pytest.mark.asyncio
async def test_extract_summary_falsy_value() -> None:
    """Model returns summary as empty string (falsy) → prefill.summary is None."""
    import uuid

    from jai.services.ai import extract_from_attachment

    session = AsyncMock()
    company_id = uuid.uuid4()
    att_id = uuid.uuid4()

    png_bytes = _make_minimal_png()
    fake_att = _make_fake_attachment(
        str(att_id), mime_type="image/png", company_id=str(company_id)
    )
    att_result = MagicMock(scalar_one_or_none=lambda: fake_att)
    cat_result = MagicMock(scalar_one_or_none=lambda: None)
    session.execute = AsyncMock(side_effect=[att_result, cat_result])

    cfg = AiSettings(enabled=True, api_key="sk", model="m", base_url="http://fake")
    fake_storage = MagicMock()
    fake_storage.open.return_value = png_bytes

    # Model returns empty string for summary (falsy) → should be None
    model_json = '{"supplier_name": "Falsy Corp", "summary": ""}'
    fake_response = {"choices": [{"message": {"content": model_json}}]}

    with patch("jai.services.ai._get_ai_config", new=AsyncMock(return_value=cfg)):
        with patch("jai.services.ai.get_storage", return_value=fake_storage):
            with respx.mock(base_url="http://fake") as mock:
                mock.post("/chat/completions").mock(
                    return_value=httpx.Response(200, json=fake_response)
                )
                async with httpx.AsyncClient() as client:
                    prefill = await extract_from_attachment(
                        session, company_id, att_id, client=client
                    )

    assert prefill.summary is None


# ---------------------------------------------------------------------------
# Date context in prompt – today's date always injected (Task A)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_prompt_contains_today_date() -> None:
    """The prompt sent to the model always contains today's date."""
    import json as _json
    import uuid
    from datetime import date as _date

    from jai.services.ai import extract_from_attachment

    session = AsyncMock()
    company_id = uuid.uuid4()
    att_id = uuid.uuid4()

    fake_att = _make_fake_attachment(
        str(att_id), mime_type="image/png", company_id=str(company_id)
    )
    att_result = MagicMock(scalar_one_or_none=lambda: fake_att)
    cat_result = MagicMock(scalar_one_or_none=lambda: None)
    session.execute = AsyncMock(side_effect=[att_result, cat_result])

    cfg = AiSettings(enabled=True, api_key="sk", model="m", base_url="http://fake")
    fake_storage = MagicMock()
    fake_storage.open.return_value = _make_minimal_png()

    captured_request: list[httpx.Request] = []

    def capture_handler(request: httpx.Request) -> httpx.Response:
        captured_request.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    with patch("jai.services.ai._get_ai_config", new=AsyncMock(return_value=cfg)):
        with patch("jai.services.ai.get_storage", return_value=fake_storage):
            with respx.mock(base_url="http://fake") as mock:
                mock.post("/chat/completions").mock(side_effect=capture_handler)
                async with httpx.AsyncClient() as client:
                    await extract_from_attachment(
                        session, company_id, att_id, client=client
                    )

    body = _json.loads(captured_request[0].content)
    user_text_parts = [
        p["text"]
        for p in body["messages"][0]["content"]
        if p.get("type") == "text"
    ]
    assert user_text_parts
    sent_text = user_text_parts[0]
    assert "today's date is" in sent_text
    assert _date.today().isoformat() in sent_text

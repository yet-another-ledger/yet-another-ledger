"""Controlled provider tests for M20 residual estimates and additive product fields."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from jai.schemas.fixed_asset import FixedAssetResidualEstimateRequest
from jai.schemas.setting import AiSettings
from jai.services.ai import AIExtractionError, estimate_fixed_asset_residual


def _request() -> FixedAssetResidualEstimateRequest:
    return FixedAssetResidualEstimateRequest.model_validate(
        {
            "cost_inputs": {
                "net_amount": "800.00",
                "vat_amount": "168.00",
                "vat_treatment_id": str(uuid.uuid4()),
                "deductible": True,
                "pl_cost_basis": "NET",
                "include_in_pl": True,
                "business_percentage": "100",
                "depreciation_years": 1,
            },
            "product_name": "Battery",
            "brand": "Example",
            "model": "B-1",
            "useful_life_years": 5,
            "condition": "Good",
            "usage": "Daily",
        }
    )


def _patch_company(company_id: uuid.UUID) -> tuple[object, object]:
    return (
        patch("jai.db.set_rls_company", new=AsyncMock()),
        patch(
            "jai.services.company.get_company",
            new=AsyncMock(return_value=MagicMock(id=company_id, base_currency="EUR")),
        ),
    )


@pytest.mark.parametrize(
    "protocol,endpoint",
    [
        ("chat_completions", "/chat/completions"),
        ("responses", "/responses"),
    ],
)
@pytest.mark.asyncio
async def test_residual_estimate_filters_provider_input_and_returns_valid_suggestion(
    protocol: str,
    endpoint: str,
) -> None:
    cfg = AiSettings(
        enabled=True,
        api_key="sk-controlled",
        model="vision-test",
        base_url="http://estimate.test",
        api_protocol=protocol,
    )
    captured: list[dict[str, object]] = []
    cost_preview = AsyncMock(return_value=MagicMock(cost_basis_amount=Decimal("800.00")))
    company_id = uuid.uuid4()
    rls_patch, company_patch = _patch_company(company_id)

    def reply(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        body = {
            "status": "suggested",
            "suggested_residual_value": "125.00",
            "reason": "Comparable equipment may retain value.",
            "assumptions": ["Good condition", "Five years of use"],
        }
        if protocol == "responses":
            return httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [{"type": "output_text", "text": json.dumps(body)}],
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(body)}}]})

    with (
        patch("jai.services.ai._get_ai_config", new=AsyncMock(return_value=cfg)),
        rls_patch,
        company_patch,
        patch(
            "jai.services.expense.calculate_expense_cost_preview",
            new=cost_preview,
        ),
        respx.mock(base_url="http://estimate.test") as router,
    ):
        router.post(endpoint).mock(side_effect=reply)
        async with httpx.AsyncClient() as client:
            result = await estimate_fixed_asset_residual(
                AsyncMock(), company_id, _request(), client
            )

    assert result.status == "suggested"
    assert result.suggested_residual_value == Decimal("125.00")
    assert result.acquisition_cost == Decimal("800.00")
    assert result.currency_code == "EUR"
    assert cost_preview.await_args.args[2].pl_cost_basis == "AUTO"
    assert result.assumptions == ["Good condition", "Five years of use"]
    provider_body = captured[0]
    serialized = json.dumps(provider_body)
    for expected in ("Battery", "Example", "B-1", "800.00", "Good", "Daily"):
        assert expected in serialized
    for forbidden in ("168.00", "vat_treatment_id", "sk-controlled", "receipt", "attachment"):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_insufficient_data_and_invalid_amount_are_handled_without_booking() -> None:
    cfg = AiSettings(
        enabled=True,
        api_key="key",
        model="m",
        base_url="http://estimate.test",
        api_protocol="chat_completions",
    )
    insufficient = {
        "status": "insufficient_data",
        "suggested_residual_value": None,
        "reason": "Product identity is not specific enough.",
        "assumptions": [],
    }
    company_id = uuid.uuid4()
    rls_patch, company_patch = _patch_company(company_id)
    with (
        patch("jai.services.ai._get_ai_config", new=AsyncMock(return_value=cfg)),
        rls_patch,
        company_patch,
        patch(
            "jai.services.expense.calculate_expense_cost_preview",
            new=AsyncMock(return_value=MagicMock(cost_basis_amount=Decimal("800.00"))),
        ),
        respx.mock(base_url="http://estimate.test") as router,
    ):
        router.post("/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"content": json.dumps(insufficient)}}]}
            )
        )
        async with httpx.AsyncClient() as client:
            result = await estimate_fixed_asset_residual(
                AsyncMock(), company_id, _request(), client
            )
    assert result.status == "insufficient_data"
    assert result.suggested_residual_value is None

    invalid = {
        "status": "suggested",
        "suggested_residual_value": "800.01",
        "reason": "Invalid",
        "assumptions": [],
    }
    rls_patch, company_patch = _patch_company(company_id)
    with (
        patch("jai.services.ai._get_ai_config", new=AsyncMock(return_value=cfg)),
        rls_patch,
        company_patch,
        patch(
            "jai.services.expense.calculate_expense_cost_preview",
            new=AsyncMock(return_value=MagicMock(cost_basis_amount=Decimal("800.00"))),
        ),
        respx.mock(base_url="http://estimate.test") as router,
    ):
        router.post("/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"content": json.dumps(invalid)}}]}
            )
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(AIExtractionError):
                await estimate_fixed_asset_residual(AsyncMock(), company_id, _request(), client)


@pytest.mark.parametrize("protocol,endpoint", [
    ("chat_completions", "/chat/completions"),
    ("responses", "/responses"),
])
@pytest.mark.parametrize("numeric_token", [
    "968.00000000000001",
    "1e99999999999999999999",
])
@pytest.mark.asyncio
async def test_residual_estimate_parses_json_numbers_as_decimal_before_cost_bound(
    protocol: str, endpoint: str, numeric_token: str,
) -> None:
    cfg = AiSettings(
        enabled=True, api_key="key", model="m", base_url="http://estimate.test",
        api_protocol=protocol,
    )
    company_id = uuid.uuid4()
    rls_patch, company_patch = _patch_company(company_id)
    raw = (
        '{"status":"suggested","suggested_residual_value":'
        f'{numeric_token},"reason":"Too high","assumptions":[]}}'
    )
    with (
        patch("jai.services.ai._get_ai_config", new=AsyncMock(return_value=cfg)),
        rls_patch,
        company_patch,
        patch(
            "jai.services.expense.calculate_expense_cost_preview",
            new=AsyncMock(return_value=MagicMock(cost_basis_amount=Decimal("968.00"))),
        ),
        respx.mock(base_url="http://estimate.test") as router,
    ):
        if protocol == "responses":
            provider_response = {
                "output": [{
                    "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": raw}],
                }],
            }
        else:
            provider_response = {"choices": [{"message": {"content": raw}}]}
        router.post(endpoint).mock(return_value=httpx.Response(200, json=provider_response))
        async with httpx.AsyncClient() as client:
            with pytest.raises(AIExtractionError) as exc_info:
                await estimate_fixed_asset_residual(AsyncMock(), company_id, _request(), client)
            assert exc_info.value.code == "AI_INVALID_RESPONSE"


@pytest.mark.asyncio
async def test_residual_estimate_timeout_returns_safe_error() -> None:
    cfg = AiSettings(
        enabled=True,
        api_key="key",
        model="m",
        base_url="http://estimate.test",
        api_protocol="chat_completions",
    )
    company_id = uuid.uuid4()
    rls_patch, company_patch = _patch_company(company_id)
    with (
        patch("jai.services.ai._get_ai_config", new=AsyncMock(return_value=cfg)),
        rls_patch,
        company_patch,
        patch(
            "jai.services.expense.calculate_expense_cost_preview",
            new=AsyncMock(
                return_value=MagicMock(cost_basis_amount=Decimal("800.00")),
            ),
        ),
        respx.mock(base_url="http://estimate.test") as router,
    ):
        router.post("/chat/completions").mock(side_effect=httpx.ReadTimeout("timed out"))
        async with httpx.AsyncClient() as client:
            with pytest.raises(AIExtractionError, match="timed out"):
                await estimate_fixed_asset_residual(AsyncMock(), company_id, _request(), client)


def test_estimate_request_requires_exactly_one_cost_source() -> None:
    payload = _request().model_dump()
    with pytest.raises(ValueError):
        FixedAssetResidualEstimateRequest.model_validate(
            {**payload, "expense_id": str(uuid.uuid4())}
        )
    with pytest.raises(ValueError):
        FixedAssetResidualEstimateRequest.model_validate(
            {
                "product_name": "Tool",
                "useful_life_years": 5,
            }
        )


@pytest.mark.asyncio
async def test_draft_capitalization_basis_uses_auto_irrecoverable_vat_not_legacy_net() -> None:
    from jai.services.expense import calculate_expense_cost_preview

    inputs = _request().cost_inputs.model_copy(update={"deductible": False})
    session = AsyncMock()
    treatment = SimpleNamespace(effect="APPLY_RATE", code="NL_DOMESTIC_PURCH")
    with (
        patch("jai.services.expense._load_vat_treatment", new=AsyncMock(return_value=treatment)),
        patch("jai.services.expense._load_category", new=AsyncMock(return_value=None)),
    ):
        auto = await calculate_expense_cost_preview(
            session, uuid.uuid4(), inputs.model_copy(update={"pl_cost_basis": "AUTO"})
        )
        legacy_net = await calculate_expense_cost_preview(
            session, uuid.uuid4(), inputs.model_copy(update={"pl_cost_basis": "NET"})
        )

    assert auto.cost_basis_amount == Decimal("968.00")
    assert legacy_net.cost_basis_amount == Decimal("800.00")

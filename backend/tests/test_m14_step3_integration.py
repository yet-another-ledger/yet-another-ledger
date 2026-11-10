"""Isolated PostgreSQL checks for M14 Step 3 stale extraction protection."""

from __future__ import annotations

import asyncio
import logging
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jai.db import set_rls_company
from jai.models._enums import VatTreatmentEffect, VatTreatmentSide
from jai.models.dictionary import ExpenseCategory
from jai.models.vat import VatRate, VatTreatment
from jai.schemas.expense import ExpenseAIPrefill
from jai.services import ai as ai_service
from jai.services.ai import _resolve_treatment_suggestion
from jai.services.receipt_inbox import append_files, create_item, extract_item, get_item
from jai.services.storage import Storage

pytestmark = pytest.mark.integration


class _MemoryStorage(Storage):
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def save(self, namespace: str, key: str, content: bytes, content_type: str) -> str:
        del content_type
        stored = f"{namespace}/{key}"
        self.data[stored] = content
        return stored

    def open(self, key: str) -> bytes:
        return self.data[key]

    def delete(self, key: str) -> None:
        self.data.pop(key, None)

    def exists(self, key: str) -> bool:
        return key in self.data


@pytest.mark.asyncio
async def test_ai_treatment_resolution_rejects_ineligible_real_dictionary_rows(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Exercise eligibility filters against persisted company dictionaries."""
    valid_company, other_company, empty_company = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with db_session_maker() as session:
        for company_id, name in (
            (valid_company, "M19 dictionary"),
            (other_company, "M19 other"),
            (empty_company, "M19 empty"),
        ):
            await session.execute(
                text("INSERT INTO company (id, name, base_currency) VALUES (:id, :name, 'EUR')"),
                {"id": company_id, "name": name},
            )
        session.add_all(
            [
                VatTreatment(
                    company_id=valid_company,
                    code="NL_DOMESTIC_PURCH",
                    label="Domestic purchase",
                    side=VatTreatmentSide.PURCHASE,
                    effect=VatTreatmentEffect.APPLY_RATE,
                    active=True,
                ),
                VatTreatment(
                    company_id=valid_company,
                    code="EU_B2B_REVERSE_PURCH",
                    label="Disabled reverse",
                    side=VatTreatmentSide.PURCHASE,
                    effect=VatTreatmentEffect.ZERO_REVERSE,
                    active=False,
                ),
                VatTreatment(
                    company_id=valid_company,
                    code="EU_B2C_PURCH",
                    label="Sales side only",
                    side=VatTreatmentSide.SALES,
                    effect=VatTreatmentEffect.APPLY_RATE,
                    active=True,
                ),
                VatTreatment(
                    company_id=other_company,
                    code="EU_B2B_REVERSE_PURCH",
                    label="Changed effect",
                    side=VatTreatmentSide.PURCHASE,
                    effect=VatTreatmentEffect.APPLY_RATE,
                    active=True,
                ),
                VatTreatment(
                    company_id=other_company,
                    code="NL_DOMESTIC_PURCH",
                    label="Other company's domestic",
                    side=VatTreatmentSide.PURCHASE,
                    effect=VatTreatmentEffect.APPLY_RATE,
                    active=True,
                ),
            ]
        )
        await session.commit()
        await set_rls_company(session, valid_company)

        valid = await _resolve_treatment_suggestion(
            session,
            valid_company,
            {"code": "NL_DOMESTIC_PURCH", "reason": "Invoice shows Dutch VAT"},
            positive_supplier_vat=True,
        )
        disabled = await _resolve_treatment_suggestion(
            session,
            valid_company,
            {"code": "EU_B2B_REVERSE_PURCH", "reason": "Invoice states reverse charge"},
            positive_supplier_vat=False,
        )
        sales_side = await _resolve_treatment_suggestion(
            session,
            valid_company,
            {"code": "EU_B2C_PURCH", "reason": "Invoice shows foreign VAT"},
            positive_supplier_vat=True,
        )
        changed_effect = await _resolve_treatment_suggestion(
            session,
            other_company,
            {"code": "EU_B2B_REVERSE_PURCH", "reason": "Invoice states reverse charge"},
            positive_supplier_vat=False,
        )
        not_found = await _resolve_treatment_suggestion(
            session,
            empty_company,
            {"code": "NL_DOMESTIC_PURCH", "reason": "Invoice shows Dutch VAT"},
            positive_supplier_vat=True,
        )

    assert valid is not None and valid["code"] == "NL_DOMESTIC_PURCH"
    assert disabled is None
    assert sales_side is None
    assert changed_effect is None
    assert not_found is None


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_point", ["expense", "receipt_inbox"])
@pytest.mark.parametrize("outcome", ["success", "partial", "provider_failure"])
async def test_m19_public_http_entry_points_with_mocked_provider(
    db_client: object,
    monkeypatch: pytest.MonkeyPatch,
    entry_point: str,
    outcome: str,
) -> None:
    """Exercise real FastAPI routes through provider parsing and dictionary lookup."""
    import json

    import httpx
    import pyotp
    import respx
    from httpx import AsyncClient

    from jai.services import ai as ai_module
    from jai.services.storage import Storage

    class MemoryStorage(Storage):
        def __init__(self) -> None:
            self.files: dict[str, bytes] = {}

        def save(self, namespace: str, key: str, content: bytes, content_type: str) -> str:
            del content_type
            storage_key = f"{namespace}/{key}"
            self.files[storage_key] = content
            return storage_key

        def open(self, key: str) -> bytes:
            return self.files[key]

        def delete(self, key: str) -> None:
            self.files.pop(key, None)

        def exists(self, key: str) -> bool:
            return key in self.files

    assert isinstance(db_client, AsyncClient)
    email = f"m19.{entry_point}.{outcome}@example.com"
    registered = await db_client.post(
        "/api/v1/auth/register", json={"email": email, "password": "testpassword1"}
    )
    assert registered.status_code == 201, registered.text
    assert (await db_client.post(
        "/api/v1/auth/login", json={"email": email, "password": "testpassword1"}
    )).status_code == 200
    setup = await db_client.post("/api/v1/auth/mfa/setup")
    assert setup.status_code == 200
    verified = await db_client.post(
        "/api/v1/auth/mfa/verify", json={"code": pyotp.TOTP(setup.json()["secret"]).now()}
    )
    assert verified.status_code == 204

    company = await db_client.put(
        "/api/v1/company",
        json={"name": "M19 HTTP Test", "base_currency": "EUR", "country_code": "NL"},
    )
    assert company.status_code == 200
    treatments_response = await db_client.get("/api/v1/vat-treatments?side=PURCHASE")
    treatments = {row["code"]: row for row in treatments_response.json()["items"]}
    treatment = treatments["NL_DOMESTIC_PURCH"]
    storage = MemoryStorage()
    monkeypatch.setattr("jai.api.attachments.get_storage", lambda: storage)
    monkeypatch.setattr("jai.api.receipt_inbox.get_storage", lambda: storage)
    monkeypatch.setattr(ai_module, "get_storage", lambda: storage)
    monkeypatch.setattr(
        ai_module,
        "_get_ai_config",
        lambda _session: _async_ai_settings(),
    )

    model_result: dict[str, object] = {"net_amount": "100", "vat_amount": "21"}
    if outcome == "success":
        model_result["vat_treatment_suggestion"] = {
            "code": "NL_DOMESTIC_PURCH",
            "reason": "The Dutch receipt shows Dutch VAT.",
        }
    provider_error = outcome == "provider_failure"

    def provider(request: httpx.Request) -> httpx.Response:
        del request
        if provider_error:
            return httpx.Response(503, json={"error": "synthetic provider failure"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(model_result)}}]},
        )

    from jai.services.ai import _PROBE_PNG_BYTES

    with respx.mock(base_url="http://m19-provider") as mock:
        mock.post("/chat/completions").mock(side_effect=provider)
        if entry_point == "expense":
            category_response = await db_client.post(
                "/api/v1/expense-categories", json={"name": "Office"}
            )
            assert category_response.status_code == 201
            rates_response = await db_client.get("/api/v1/vat-rates")
            rate_id = rates_response.json()["items"][0]["id"]
            expense_response = await db_client.post(
                "/api/v1/expenses",
                json={
                    "expense_date": "2026-06-13",
                    "category_id": category_response.json()["id"],
                    "vat_treatment_id": treatment["id"],
                    "vat_rate_id": rate_id,
                    "net_amount": "100.00",
                    "vat_amount": "21.00",
                },
            )
            assert expense_response.status_code == 201, expense_response.text
            attachment = await db_client.post(
                f"/api/v1/expenses/{expense_response.json()['id']}/attachments",
                files={"file": ("receipt.png", _PROBE_PNG_BYTES, "image/png")},
            )
            assert attachment.status_code == 201, attachment.text
            response = await db_client.post(
                "/api/v1/expenses/ai-extract",
                json={"attachment_id": attachment.json()["id"], "language": "en"},
            )
            if provider_error:
                assert response.status_code == 502
                assert "synthetic provider failure" not in response.text
            else:
                assert response.status_code == 200, response.text
                suggestion = response.json()
                assert Decimal(str(suggestion["net_amount"])) == Decimal("100")
                if outcome == "success":
                    assert suggestion["suggested_vat_treatment"]["id"] == treatment["id"]
                else:
                    assert suggestion["suggested_vat_treatment"] is None
        else:
            response = await db_client.post(
                "/api/v1/receipt-inbox?language=en",
                files={"files": ("receipt.png", _PROBE_PNG_BYTES, "image/png")},
            )
            assert response.status_code == 201, response.text
            result = response.json()
            if provider_error:
                assert result["ai_state"] == "FAILED"
                assert result["ai_error_code"] == "AI_PROVIDER_ERROR"
            else:
                assert result["ai_state"] == "READY"
                assert Decimal(str(result["ai_suggestion"]["net_amount"])) == Decimal("100")
                if outcome == "success":
                    suggestion = result["ai_suggestion"]["suggested_vat_treatment"]
                    assert suggestion["id"] == treatment["id"]
                else:
                    assert result["ai_suggestion"]["suggested_vat_treatment"] is None


async def _async_ai_settings() -> object:
    from jai.schemas.setting import AiSettings

    return AiSettings(
        enabled=True,
        api_key="sk-m19-test",
        model="vision-test",
        base_url="http://m19-provider",
    )


@pytest.mark.asyncio
async def test_old_group_result_cannot_replace_new_file_revision(
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    import httpx
    from sqlalchemy import text

    company_id, creator_id = uuid.uuid4(), uuid.uuid4()
    async with db_session_maker() as setup:
        await setup.execute(
            text("INSERT INTO company (id, name, base_currency) VALUES (:id, 'M14 Step 3', 'EUR')"),
            {"id": company_id},
        )
        await setup.execute(
            text(
                'INSERT INTO "user" (id, email, hashed_password, company_id) '
                "VALUES (:id, :email, 'test-hash', :company_id)"
            ),
            {"id": creator_id, "email": f"{creator_id}@example.test", "company_id": company_id},
        )
        await setup.commit()

    async with db_session_maker() as setup:
        setup.add(ExpenseCategory(company_id=company_id, name="Office", active=False))
        treatment = VatTreatment(
            company_id=company_id,
            code="EU_B2B_REVERSE_PURCH",
            label="EU reverse charge",
            side=VatTreatmentSide.PURCHASE,
            effect=VatTreatmentEffect.ZERO_REVERSE,
            active=True,
        )
        setup.add(treatment)
        setup.add(
            VatRate(company_id=company_id, label="Standard", percent=Decimal("21"), active=True)
        )
        setup.add(
            VatRate(
                company_id=company_id,
                label="Inactive reduced",
                percent=Decimal("19"),
                active=False,
            )
        )
        await setup.commit()
        treatment_id = treatment.id

    storage = _MemoryStorage()
    png = b"\x89PNG\r\n\x1a\nreceipt"
    async with db_session_maker() as session:
        item = await create_item(
            session, company_id, creator_id, [(png, "image/png", "one.png")], 1024, storage
        )

    # Historical JSONB has no treatment field; it remains readable and keeps
    # the legacy fields when decoded through the public read schema.
    async with db_session_maker() as legacy_session:
        legacy_item = await create_item(
            legacy_session, company_id, creator_id, [(png, "image/png", "legacy.png")],
            1024, storage,
        )
        await legacy_session.execute(
            text("UPDATE receipt_inbox SET ai_suggestion = CAST(:payload AS jsonb) WHERE id = :id"),
            {
                "payload": json.dumps({"net_amount": "12.50", "summary": "Old receipt"}),
                "id": legacy_item.id,
            },
        )
        await legacy_session.commit()
    async with db_session_maker() as legacy_read:
        legacy = await get_item(legacy_read, legacy_item.id, company_id)
        assert legacy.ai_suggestion is not None
        assert legacy.ai_suggestion.net_amount == Decimal("12.50")
        assert legacy.ai_suggestion.summary == "Old receipt"
        assert legacy.ai_suggestion.suggested_vat_treatment is None

    old_started = asyncio.Event()
    release_old = asyncio.Event()
    calls = 0
    provider_payloads = [
        {
            "summary": "stale",
            "vat_treatment_suggestion": {
                "code": "EU_B2B_REVERSE_PURCH", "reason": "Old extraction evidence",
            },
        },
        {
            "summary": "current",
            "vat_treatment_suggestion": {
                "code": "EU_B2B_REVERSE_PURCH", "reason": "Current extraction evidence",
            },
        },
        {
            "vat_treatment_suggestion": {
                "code": "EU_B2B_REVERSE_PURCH", "reason": "Invoice states reverse charge",
            },
        },
        {
            "net_amount": "100",
            "vat_treatment_suggestion": {
                "code": "EU_B2B_REVERSE_PURCH", "reason": "Invoice states reverse charge",
            },
        },
    ]

    async def mock_provider(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        assert request.url.path == "/chat/completions"
        index = calls
        calls += 1
        if index == 0:
            old_started.set()
            await asyncio.wait_for(release_old.wait(), timeout=5)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(provider_payloads[index])}}]},
        )

    monkeypatch.setattr(
        ai_service, "_get_ai_config", lambda _session: _async_ai_settings()
    )
    original_extract = ai_service.extract_from_files
    mocked_http = httpx.AsyncClient(transport=httpx.MockTransport(mock_provider))

    async def extract_via_mock_http(*args: object, **kwargs: object) -> ExpenseAIPrefill:
        return await original_extract(*args, **kwargs, client=mocked_http)

    monkeypatch.setattr(ai_service, "extract_from_files", extract_via_mock_http)

    async def run_old() -> None:
        async with db_session_maker() as session:
            await extract_item(session, item.id, company_id, storage)

    old_task = asyncio.create_task(run_old())
    await asyncio.wait_for(old_started.wait(), timeout=5)

    async with db_session_maker() as session:
        await append_files(
            session, item.id, company_id, creator_id,
            [(png, "image/png", "two.png")], 1024, storage,
        )
        await extract_item(session, item.id, company_id, storage)

    release_old.set()
    await old_task

    async with db_session_maker() as verify:
        current = await get_item(verify, item.id, company_id)
        assert current.ai_suggestion is not None
        assert current.ai_suggestion.summary == "current"
        assert current.ai_suggestion.suggested_vat_treatment is not None
        assert current.ai_suggestion.suggested_vat_treatment.reason == "Current extraction evidence"
        assert current.ai_state.value == "READY"
        assert len(current.files) == 2

    # Type-only model output traverses the same parser, real dictionary lookup,
    # JSONB persistence and presentability check, and is still READY.
    async with db_session_maker() as session:
        type_only_item = await create_item(
            session, company_id, creator_id, [(png, "image/png", "type-only.png")],
            1024, storage,
        )
        type_only = await extract_item(session, type_only_item.id, company_id, storage)
        assert type_only.ai_state.value == "READY"
        assert type_only.ai_suggestion is not None
        assert type_only.ai_suggestion.net_amount is None
        assert type_only.ai_suggestion.suggested_vat_treatment is not None
        assert type_only.ai_suggestion.suggested_vat_treatment.id == treatment_id

    # Simulate a dictionary entry becoming ineligible between extraction
    # resolution and the inbox persistence recheck. The stale suggestion is
    # removed while valid amount data is kept and remains presentable.
    from jai.services import vat as vat_service

    resolve_calls = 0
    original_resolver = vat_service.resolve_ai_purchase_treatment

    async def invalidate_on_recheck(*args: object, **kwargs: object) -> object:
        nonlocal resolve_calls
        resolve_calls += 1
        if resolve_calls == 2:
            return None
        return await original_resolver(*args, **kwargs)

    monkeypatch.setattr(vat_service, "resolve_ai_purchase_treatment", invalidate_on_recheck)
    async with db_session_maker() as session:
        changed_item = await create_item(
            session, company_id, creator_id, [(png, "image/png", "changed-dictionary.png")],
            1024, storage,
        )
        changed = await extract_item(session, changed_item.id, company_id, storage)
        assert changed.ai_state.value == "READY"
        assert changed.ai_suggestion is not None
        assert changed.ai_suggestion.net_amount == Decimal("100")
        assert changed.ai_suggestion.suggested_vat_treatment is None

    async def unavailable(*args: object, **kwargs: object) -> ExpenseAIPrefill:
        del args, kwargs
        raise ai_service.AIUnavailableError("AI_NOT_CONFIGURED")

    monkeypatch.setattr(ai_service, "extract_from_files", unavailable)
    async with db_session_maker() as session:
        another = await create_item(
            session, company_id, creator_id, [(png, "image/png", "offline.png")], 1024, storage
        )
        saved = await extract_item(session, another.id, company_id, storage)
        assert saved.ai_state.value == "UNAVAILABLE"
        assert saved.ai_error_code is not None
        assert saved.ai_error_code.value == "AI_NOT_CONFIGURED"
        assert saved.ai_suggestion is None

    async def empty_prefill(*args: object, **kwargs: object) -> ExpenseAIPrefill:
        del args, kwargs
        return ExpenseAIPrefill(confidence="low", raw_model_note="unusable")

    monkeypatch.setattr(ai_service, "extract_from_files", empty_prefill)
    async with db_session_maker() as session:
        empty_item = await create_item(
            session, company_id, creator_id, [(png, "image/png", "empty.json.png")], 1024, storage
        )
        empty_result = await extract_item(session, empty_item.id, company_id, storage)
        assert empty_result.ai_state.value == "FAILED"
        assert empty_result.ai_error_code is not None
        assert empty_result.ai_error_code.value == "AI_INVALID_RESPONSE"
        assert empty_result.ai_suggestion is None

    async def check_unusable(prefill: ExpenseAIPrefill, filename: str) -> None:
        async def response(*args: object, **kwargs: object) -> ExpenseAIPrefill:
            del args, kwargs
            return prefill

        monkeypatch.setattr(ai_service, "extract_from_files", response)
        async with db_session_maker() as session:
            candidate = await create_item(
                session, company_id, creator_id, [(png, "image/png", filename)], 1024, storage
            )
            result = await extract_item(session, candidate.id, company_id, storage)
            assert result.ai_state.value == "FAILED"
            assert result.ai_error_code is not None
            assert result.ai_error_code.value == "AI_INVALID_RESPONSE"
            assert result.ai_suggestion is None

    await check_unusable(ExpenseAIPrefill(suggested_category_name="Unknown"), "unknown.png")
    await check_unusable(ExpenseAIPrefill(supplier_name="  "), "whitespace.png")
    await check_unusable(ExpenseAIPrefill(vat_rate_percent=Decimal("17")), "unknown-rate.png")

    async def usable_prefill(*args: object, **kwargs: object) -> ExpenseAIPrefill:
        del args, kwargs
        return ExpenseAIPrefill(
            suggested_vat_treatment={
                "id": treatment_id,
                "code": "EU_B2B_REVERSE_PURCH",
                "reason": "Invoice explicitly states reverse charge",
            }
        )

    monkeypatch.setattr(ai_service, "extract_from_files", usable_prefill)
    async with db_session_maker() as session:
        valid_item = await create_item(
            session, company_id, creator_id, [(png, "image/png", "known.png")], 1024, storage
        )
        valid_result = await extract_item(session, valid_item.id, company_id, storage)
        assert valid_result.ai_state.value == "READY"
        assert valid_result.ai_suggestion is not None
        assert valid_result.ai_suggestion.suggested_vat_treatment is not None

    async def inactive_rate_prefill(*args: object, **kwargs: object) -> ExpenseAIPrefill:
        del args, kwargs
        return ExpenseAIPrefill(vat_rate_percent=Decimal("19"))

    monkeypatch.setattr(ai_service, "extract_from_files", inactive_rate_prefill)
    async with db_session_maker() as session:
        inactive_rate_item = await create_item(
            session,
            company_id,
            creator_id,
            [(png, "image/png", "inactive-rate.png")],
            1024,
            storage,
        )
        inactive_rate_result = await extract_item(
            session, inactive_rate_item.id, company_id, storage
        )
        assert inactive_rate_result.ai_state.value == "READY"
        assert inactive_rate_result.ai_suggestion is not None
        assert inactive_rate_result.ai_suggestion.vat_rate_percent == Decimal("19")

    async def provider_error(*args: object, **kwargs: object) -> ExpenseAIPrefill:
        del args, kwargs
        raise ai_service.AIExtractionError("AI_TIMEOUT")

    monkeypatch.setattr(ai_service, "extract_from_files", provider_error)
    async with db_session_maker() as session:
        failed = await extract_item(session, another.id, company_id, storage)
        assert failed.ai_state.value == "FAILED"
        assert failed.ai_error_code is not None
        assert failed.ai_error_code.value == "AI_TIMEOUT"
        assert failed.ai_suggestion is None
        assert "AI_TIMEOUT" in str(failed.ai_error_code)

    confirmed = asyncio.Event()
    release_confirmed = asyncio.Event()

    async def finishing(*args: object, **kwargs: object) -> ExpenseAIPrefill:
        del args, kwargs
        confirmed.set()
        await release_confirmed.wait()
        return ExpenseAIPrefill(summary="must not be saved")

    monkeypatch.setattr(ai_service, "extract_from_files", finishing)
    async with db_session_maker() as session:
        final_item = await create_item(
            session, company_id, creator_id, [(png, "image/png", "confirmed.png")], 1024, storage
        )

    async def in_flight() -> None:
        async with db_session_maker() as session:
            await extract_item(session, final_item.id, company_id, storage)

    extraction = asyncio.create_task(in_flight())
    await confirmed.wait()
    async with db_session_maker() as session:
        await set_rls_company(session, company_id)
        await session.execute(
            text("UPDATE receipt_inbox SET status = 'CONFIRMED' WHERE id = :id"),
            {"id": final_item.id},
        )
        await session.commit()
    release_confirmed.set()
    await extraction

    async with db_session_maker() as verify:
        completed = await get_item(verify, final_item.id, company_id)
        assert completed.status.value == "CONFIRMED"
        assert completed.ai_suggestion is None

    await mocked_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_source", ["storage", "provider"])
async def test_extraction_failure_log_excludes_private_data(
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_source: str,
) -> None:
    company_id, creator_id = uuid.uuid4(), uuid.uuid4()
    async with db_session_maker() as setup:
        await setup.execute(
            text("INSERT INTO company (id, name, base_currency) VALUES (:id, 'M14 log', 'EUR')"),
            {"id": company_id},
        )
        await setup.execute(
            text(
                'INSERT INTO "user" (id, email, hashed_password, company_id) '
                "VALUES (:id, :email, 'test-hash', :company_id)"
            ),
            {"id": creator_id, "email": f"{creator_id}@example.test", "company_id": company_id},
        )
        await setup.commit()

    storage = _MemoryStorage()
    async with db_session_maker() as session:
        item = await create_item(
            session, company_id, creator_id,
            [(b"\x89PNG\r\n\x1a\nsecret receipt bytes", "image/png", "receipt.png")],
            1024, storage,
        )
    storage_key = next(iter(storage.data))
    private_text = (
        f"{storage_key} /private/receipt.png provider-response-secret "
        "secret receipt bytes"
    )

    if failure_source == "storage":
        def missing_file(key: str) -> bytes:
            raise FileNotFoundError(f"Storage key not found: {key}; {private_text}")

        monkeypatch.setattr(storage, "open", missing_file)
    else:
        async def broken_provider(*args: object, **kwargs: object) -> ExpenseAIPrefill:
            del args, kwargs
            raise RuntimeError(private_text)

        monkeypatch.setattr(ai_service, "extract_from_files", broken_provider)

    with caplog.at_level(logging.WARNING, logger="jai.services.receipt_inbox"):
        async with db_session_maker() as session:
            failed = await extract_item(session, item.id, company_id, storage)

    assert failed.ai_state.value == "FAILED"
    assert failed.ai_error_code is not None
    assert failed.ai_error_code.value == "AI_PROVIDER_ERROR"
    assert failed.ai_suggestion is None
    records = [
        record for record in caplog.records
        if record.name == "jai.services.receipt_inbox"
    ]
    assert len(records) == 1
    assert records[0].getMessage() == "Receipt inbox AI extraction failed: AI_PROVIDER_ERROR"
    assert records[0].exc_info is None
    for secret in (
        storage_key,
        "/private/receipt.png",
        "provider-response-secret",
        "secret receipt bytes",
    ):
        assert secret not in caplog.text

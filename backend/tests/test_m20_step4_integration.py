"""M20 Step 4 registration during purchase creation and receipt confirmation."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pyotp
import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jai.db import set_rls_company
from jai.main import app
from jai.models.expense import ExpenseSubmission
from jai.schemas.expense import ExpenseInput, ExpenseUpdateInput, ReceiptInboxExpenseInput
from jai.services.expense import _submission_hash
from jai.services.storage import LocalStorage, set_storage


@pytest.fixture(autouse=True)
def _isolated_storage(tmp_path: Path):
    set_storage(LocalStorage(str(tmp_path)))
    yield
    set_storage(None)


async def _auth(client: AsyncClient) -> uuid.UUID:
    token = uuid.uuid4().hex
    created = await client.post(
        "/api/v1/auth/register",
        json={"email": f"m20-step4-{token}@example.com", "password": "testpassword1"},
    )
    assert created.status_code == 201, created.text
    logged = await client.post(
        "/api/v1/auth/login",
        json={"email": created.json()["email"], "password": "testpassword1"},
    )
    assert logged.status_code == 200, logged.text
    setup = await client.post("/api/v1/auth/mfa/setup")
    assert setup.status_code == 200, setup.text
    assert (await client.post(
        "/api/v1/auth/mfa/verify", json={"code": pyotp.TOTP(setup.json()["secret"]).now()}
    )).status_code == 204
    company = await client.put(
        "/api/v1/company", json={"name": "M20 Step 4", "base_currency": "EUR", "country_code": "NL"}
    )
    assert company.status_code == 200, company.text
    user = await client.get("/api/v1/users/me")
    assert user.status_code == 200, user.text
    return uuid.UUID(user.json()["company_id"])


async def _purchase_body(client: AsyncClient) -> dict[str, object]:
    category = await client.post("/api/v1/expense-categories", json={"name": "Equipment"})
    rates = await client.get("/api/v1/vat-rates")
    treatments = await client.get("/api/v1/vat-treatments?side=PURCHASE")
    rate = next(row for row in rates.json()["items"] if row["label"] == "NL standard (21%)")
    treatment = next(
        row for row in treatments.json()["items"] if row["code"] == "NL_DOMESTIC_PURCH"
    )
    return {
        "expense_date": "2026-11-01",
        "category_id": category.json()["id"],
        "vat_treatment_id": treatment["id"],
        "vat_rate_id": rate["id"],
        "net_amount": "1000.00",
        "vat_amount": "210.00",
        "depreciation_years": 5,
    }


def test_optional_asset_contract_is_additive_and_uses_d1_defaults() -> None:
    base: dict[str, object] = {
        "expense_date": "2026-11-01",
        "category_id": str(uuid.uuid4()),
        "vat_treatment_id": str(uuid.uuid4()),
        "vat_rate_id": str(uuid.uuid4()),
        "net_amount": "100.00",
        "vat_amount": "21.00",
    }
    # Existing callers can still omit both additions.
    ExpenseInput.model_validate(base)
    # Historical clients' additional fields remain ignored across all three
    # purchase inputs, including fields the backend derives itself.
    for input_model in (ExpenseInput, ExpenseUpdateInput, ReceiptInboxExpenseInput):
        parsed = input_model.model_validate({**base, "is_draft": False, "legacy_flag": "ignored"})
        assert "is_draft" not in parsed.model_dump()
        assert "legacy_flag" not in parsed.model_dump()
    with pytest.raises(ValidationError):
        ExpenseInput.model_validate({**base, "fixed_asset": {"asset_name": "Equipment"}})
    asset = ExpenseInput.model_validate({
        **base,
        "fixed_asset": {"asset_name": " Equipment "},
        "client_submission_id": str(uuid.uuid4()),
    }).fixed_asset
    assert asset is not None
    assert asset.asset_name == "Equipment"
    assert asset.useful_life_years == 5
    assert asset.residual_value == 0
    assert asset.business_percentage == 100
    assert asset.use_type == "FULL_BUSINESS"
    with pytest.raises(ValidationError):
        ExpenseInput.model_validate({
            **base,
            "fixed_asset": {"asset_name": "Mixed", "use_type": "MIXED_USE"},
            "client_submission_id": str(uuid.uuid4()),
        })
    with pytest.raises(ValidationError):
        ExpenseUpdateInput.model_validate(
            {**base, "fixed_asset": {"asset_name": "No update registration"}}
        )
    with pytest.raises(ValidationError):
        ExpenseUpdateInput.model_validate({**base, "client_submission_id": str(uuid.uuid4())})
    ReceiptInboxExpenseInput.model_validate({**base, "fixed_asset": {"asset_name": "Inbox"}})
    with pytest.raises(ValidationError):
        ReceiptInboxExpenseInput.model_validate({**base, "client_submission_id": str(uuid.uuid4())})


def test_expense_submission_conflict_is_declared_in_openapi() -> None:
    operation = app.openapi()["paths"]["/api/v1/expenses"]["post"]
    conflict = operation["responses"]["409"]
    assert conflict["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ExpenseConflictRead"
    )
    assert app.openapi()["components"]["schemas"]["ExpenseConflictRead"]["properties"] == {
        "detail": {"title": "Detail", "type": "string"}
    }


@pytest.mark.parametrize(
    ("base_value", "equivalent_value"),
    [
        ("0", "-0.00"),
        ("0.000", "-0"),
        ("1000.00", "1000"),
        ("210.000", "210.0"),
    ],
)
def test_submission_hash_canonicalizes_decimal_zero_sign_and_scale(
    base_value: str, equivalent_value: str
) -> None:
    common = {
        "expense_date": "2026-11-01",
        "category_id": str(uuid.uuid4()),
        "vat_treatment_id": str(uuid.uuid4()),
        "vat_rate_id": str(uuid.uuid4()),
        "net_amount": base_value,
        "vat_amount": "0.00",
        "client_submission_id": str(uuid.uuid4()),
        "fixed_asset": {"asset_name": "Equipment", "residual_value": "-0.00"},
    }
    alternate = {
        **common,
        "net_amount": equivalent_value,
        "vat_amount": "-0",
        "fixed_asset": {"asset_name": "Equipment", "residual_value": "0"},
    }
    assert _submission_hash(ExpenseInput.model_validate(common)) == _submission_hash(
        ExpenseInput.model_validate(alternate)
    )


@pytest.mark.integration
async def test_new_expense_asset_replay_and_conflicting_reuse(
    db_client: AsyncClient,
    runtime_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    company_id = await _auth(db_client)
    body = await _purchase_body(db_client)
    submission_id = str(uuid.uuid4())
    payload = {
        **body,
        "business_percentage": "64.1239",
        "client_submission_id": submission_id,
        "fixed_asset": {
            "asset_name": "Test equipment",
            "in_service_date": None,
            "useful_life_years": 5,
            "residual_value": "125.00",
            "business_percentage": "100",
            "use_type": "FULL_BUSINESS",
        },
    }
    first, concurrent = await asyncio.gather(
        db_client.post("/api/v1/expenses", json=payload),
        db_client.post("/api/v1/expenses", json=payload),
    )
    assert first.status_code == 201, first.text
    assert concurrent.status_code == 201, concurrent.text
    assert first.json() == concurrent.json()
    assert first.json()["business_percentage"] == "64.124"
    assert concurrent.json()["id"] == first.json()["id"]
    assert first.json()["linked_fixed_asset_id"]
    reopened = await db_client.get(f"/api/v1/expenses/{first.json()['id']}")
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["linked_fixed_asset_id"] == first.json()["linked_fixed_asset_id"]
    invalid_update = {
        **body,
        "fixed_asset": payload["fixed_asset"],
        "client_submission_id": submission_id,
    }
    rejected_update = await db_client.put(
        f"/api/v1/expenses/{first.json()['id']}", json=invalid_update
    )
    assert rejected_update.status_code == 422, rejected_update.text
    replay = await db_client.post("/api/v1/expenses", json=payload)
    assert replay.status_code == 201, replay.text
    assert replay.json() == first.json()
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["linked_fixed_asset_id"] == first.json()["linked_fixed_asset_id"]
    equivalent = {
        **payload,
        "net_amount": "1000",
        "vat_amount": "210.0",
        "fixed_asset": {**payload["fixed_asset"], "residual_value": "125"},
    }
    equivalent_replay = await db_client.post("/api/v1/expenses", json=equivalent)
    assert equivalent_replay.status_code == 201, equivalent_replay.text
    assert equivalent_replay.json() == first.json()
    assert equivalent_replay.json()["id"] == first.json()["id"]
    metadata_update = await db_client.put(
        f"/api/v1/expenses/{first.json()['id']}",
        json={**body, "business_percentage": "64.124", "note": "metadata only"},
    )
    assert metadata_update.status_code == 200, metadata_update.text
    assert metadata_update.json()["linked_fixed_asset_id"] == first.json()["linked_fixed_asset_id"]
    changed = {**payload, "note": "different payload"}
    conflict = await db_client.post("/api/v1/expenses", json=changed)
    assert conflict.status_code == 409, conflict.text
    invalid_asset = {
        **body,
        "client_submission_id": str(uuid.uuid4()),
        "fixed_asset": {
            "asset_name": "Too much residual",
            "in_service_date": None,
            "useful_life_years": 5,
            "residual_value": "5000.00",
            "business_percentage": "100",
            "use_type": "FULL_BUSINESS",
        },
    }
    failed = await db_client.post("/api/v1/expenses", json=invalid_asset)
    assert failed.status_code == 422, failed.text
    assert (await db_client.get("/api/v1/expenses")).json()["total"] == 1
    assets = await db_client.get("/api/v1/fixed-assets")
    assert sum(row["acquisition_expense_id"] == first.json()["id"] for row in assets.json()) == 1
    assert assets.json()[0]["pending_in_service"] is True
    assert assets.json()[0]["residual_value"] == "125.000"
    async with runtime_session_maker() as session:
        await set_rls_company(session, company_id)
        assert await session.scalar(select(func.count()).select_from(ExpenseSubmission)) == 1
        await set_rls_company(session, uuid.uuid4())
        assert await session.scalar(select(func.count()).select_from(ExpenseSubmission)) == 0


@pytest.mark.integration
async def test_expense_without_asset_keeps_legacy_path(db_client: AsyncClient) -> None:
    await _auth(db_client)
    response = await db_client.post("/api/v1/expenses", json=await _purchase_body(db_client))
    assert response.status_code == 201, response.text
    assert response.json()["linked_fixed_asset_id"] is None
    assert (await db_client.get("/api/v1/fixed-assets")).json() == []


@pytest.mark.integration
async def test_new_historical_purchase_is_not_a_conversion_and_future_date_is_rejected(
    db_client: AsyncClient,
) -> None:
    await _auth(db_client)
    body = await _purchase_body(db_client)
    old_payload = {
        **body,
        "expense_date": "2025-12-12",
        "client_submission_id": str(uuid.uuid4()),
        "fixed_asset": {"asset_name": "Backdated equipment"},
    }
    created = await db_client.post("/api/v1/expenses", json=old_payload)
    assert created.status_code == 201, created.text
    assert created.json()["linked_fixed_asset_id"]
    assert (await db_client.get("/api/v1/fixed-assets")).json()[0]["pending_in_service"] is True

    future_payload = {
        **body,
        "client_submission_id": str(uuid.uuid4()),
        "fixed_asset": {"asset_name": "Future equipment", "in_service_date": "2026-12-09"},
    }
    rejected = await db_client.post("/api/v1/expenses", json=future_payload)
    assert rejected.status_code == 422, rejected.text
    assert (await db_client.get("/api/v1/expenses")).json()["total"] == 1


@pytest.mark.integration
async def test_receipt_confirmation_registers_asset_and_transfers_original(
    db_client: AsyncClient,
) -> None:
    await _auth(db_client)
    from jai.services.ai import _PROBE_PNG_BYTES

    created = await db_client.post(
        "/api/v1/receipt-inbox?language=en",
        files=[
            ("files", ("receipt-a.png", _PROBE_PNG_BYTES, "image/png")),
            ("files", ("receipt-b.png", _PROBE_PNG_BYTES, "image/png")),
        ],
    )
    assert created.status_code == 201, created.text
    inbox = created.json()
    body = await _purchase_body(db_client)
    body.update({
        "fixed_asset": {
            "asset_name": "Inbox equipment",
            "in_service_date": "2026-11-01",
            "useful_life_years": 5,
            "residual_value": "0.00",
            "business_percentage": "65.000",
            "use_type": "MIXED_USE",
        },
    })
    # A failed registration must roll back the purchase and leave both original files
    # attached to the pending review group so the owner can correct and retry.
    invalid = {**body, "fixed_asset": {**body["fixed_asset"], "residual_value": "5000.00"}}
    failed = await db_client.post(
        f"/api/v1/receipt-inbox/{inbox['id']}/confirm?expected_file_revision={inbox['file_revision']}",
        json=invalid,
    )
    assert failed.status_code == 422, failed.text
    assert (await db_client.get("/api/v1/expenses")).json()["total"] == 0
    still_pending = await db_client.get(f"/api/v1/receipt-inbox/{inbox['id']}")
    assert still_pending.json()["status"] == "PENDING"
    assert len(still_pending.json()["files"]) == 2
    # Mutating the file group invalidates the revision the form originally reviewed.
    removed = await db_client.delete(
        f"/api/v1/receipt-inbox/{inbox['id']}/files/{inbox['files'][0]['id']}"
    )
    assert removed.status_code == 200, removed.text
    current = removed.json()
    stale = await db_client.post(
        f"/api/v1/receipt-inbox/{inbox['id']}/confirm?expected_file_revision={inbox['file_revision']}",
        json=body,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == "FILE_GROUP_CHANGED"
    assert (await db_client.get("/api/v1/expenses")).json()["total"] == 0
    assert (await db_client.get("/api/v1/fixed-assets")).json() == []
    after_stale = await db_client.get(f"/api/v1/receipt-inbox/{inbox['id']}")
    assert after_stale.json()["status"] == "PENDING"
    assert len(after_stale.json()["files"]) == 1
    assert after_stale.json()["files"][0]["id"] == current["files"][0]["id"]
    missing = await db_client.post(
        f"/api/v1/receipt-inbox/{inbox['id']}/confirm",
        json=body,
    )
    assert missing.status_code == 409, missing.text
    assert missing.json()["detail"] == "FILE_GROUP_CHANGED"
    assert (await db_client.get("/api/v1/expenses")).json()["total"] == 0
    assert (await db_client.get("/api/v1/fixed-assets")).json() == []
    pending = await db_client.get(f"/api/v1/receipt-inbox/{inbox['id']}")
    assert pending.json()["status"] == "PENDING"
    assert len(pending.json()["files"]) == 1
    assert pending.json()["files"][0]["id"] == current["files"][0]["id"]
    confirmed = await db_client.post(
        f"/api/v1/receipt-inbox/{inbox['id']}/confirm?expected_file_revision={current['file_revision']}",
        json=body,
    )
    assert confirmed.status_code == 201, confirmed.text
    expense = confirmed.json()["expense"]
    assert expense["linked_fixed_asset_id"]
    assert expense["attachment_count"] == 1
    attachments = await db_client.get(f"/api/v1/expenses/{expense['id']}/attachments")
    assert attachments.status_code == 200, attachments.text
    assert len(attachments.json()["items"]) == 1
    assert attachments.json()["items"][0]["id"] == current["files"][0]["id"]
    asset = await db_client.get(f"/api/v1/fixed-assets/{expense['linked_fixed_asset_id']}")
    assert asset.status_code == 200, asset.text
    assert asset.json()["use_type"] == "MIXED_USE"
    already_confirmed = await db_client.post(
        f"/api/v1/receipt-inbox/{inbox['id']}/confirm",
        json=body,
    )
    assert already_confirmed.status_code == 409
    assert already_confirmed.json() == {
        "detail": "ALREADY_CONFIRMED",
        "expense_id": expense["id"],
    }

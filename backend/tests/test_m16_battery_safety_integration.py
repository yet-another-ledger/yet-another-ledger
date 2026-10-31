"""Acquisition serialization and evidence ownership failure regressions."""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from test_m16_battery_integration import MemoryStorage, _owner_company, _purchase_dictionaries

import jai.api.attachments as attachment_api
import jai.api.battery as battery_api
import jai.api.expenses as expenses_api
import jai.services.battery as battery_service
import jai.services.expense as expense_service
from jai.models.battery import BatteryAnnualEvidence
from jai.models.expense import Expense

pytestmark = pytest.mark.integration


async def _purchase(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, dict[str, Any], dict[str, Any], MemoryStorage]:
    storage = MemoryStorage()
    for api in (attachment_api, expenses_api, battery_api):
        monkeypatch.setattr(api, "get_storage", lambda: storage)
    for api in (attachment_api, battery_api):
        monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(max_receipt_bytes=128))
    await _owner_company(client, "battery-safety@example.com")
    category, treatment, rate = await _purchase_dictionaries(client)
    today = date.today().isoformat()
    body = {
        "expense_date": today, "category_id": category, "vat_treatment_id": treatment,
        "vat_rate_id": rate, "net_amount": "10000", "vat_amount": "2100", "deductible": True,
    }
    purchase = await client.post("/api/v1/expenses", json=body)
    assert purchase.status_code == 201, purchase.text
    expense_id = purchase.json()["id"]
    attached = await client.post(
        f"/api/v1/expenses/{expense_id}/attachments",
        files={"file": ("purchase.pdf", b"%PDF-acquisition", "application/pdf")},
    )
    assert attached.status_code == 201, attached.text
    registration = {
        "acquisition_expense_id": expense_id, "in_service_date": today,
        "owner_confirms_ordinary_vat_full_deduction": True, "acknowledge_affected_periods": True,
    }
    return expense_id, body, registration, storage


@pytest.mark.asyncio
@pytest.mark.parametrize("winner,loser", [("register", "delete"), ("delete", "register"),
                                         ("register", "edit"), ("edit", "register")])
async def test_acquisition_commands_serialize_and_recheck_after_winner(
    db_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, winner: str, loser: str,
) -> None:
    expense_id, body, registration, storage = await _purchase(db_client, monkeypatch)
    original_bytes = dict(storage.data)
    original_load = expense_service._load_expense
    held, attempted, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    first = True

    async def controlled_load(*args: Any, **kwargs: Any) -> Expense:
        nonlocal first
        if not kwargs.get("for_update"):
            return await original_load(*args, **kwargs)
        is_first = first
        first = False
        if not is_first:
            attempted.set()
        result = await original_load(*args, **kwargs)
        if is_first:
            held.set()
            await asyncio.wait_for(release.wait(), 10)
        return result

    monkeypatch.setattr(expense_service, "_load_expense", controlled_load)
    monkeypatch.setattr(battery_service, "_load_expense", controlled_load)

    async def command(name: str) -> Any:
        if name == "register":
            return await db_client.post("/api/v1/battery-assets", json=registration)
        if name == "delete":
            return await db_client.delete(f"/api/v1/expenses/{expense_id}")
        return await db_client.put(f"/api/v1/expenses/{expense_id}",
                                   json={**body, "deductible": False})

    winning = asyncio.create_task(command(winner))
    losing: asyncio.Task[Any] | None = None
    try:
        await asyncio.wait_for(held.wait(), 10)
        losing = asyncio.create_task(command(loser))
        await asyncio.wait_for(attempted.wait(), 10)
        # The second request has entered the lock helper and cannot finish while
        # the winning acquisition transaction is paused under its row lock.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(losing), 0.1)
        assert storage.data == original_bytes
    finally:
        release.set()
        results = await asyncio.wait_for(
            asyncio.gather(winning, *([losing] if losing is not None else [])), 10
        )
    assert len(results) == 2
    won, lost = results
    assert won.status_code == {"register": 201, "delete": 204, "edit": 200}[winner], won.text
    if winner == "delete":
        assert lost.status_code == 404, lost.text
        assert storage.data == {}
        assert (await db_client.get(f"/api/v1/expenses/{expense_id}")).status_code == 404
        assert (await db_client.get("/api/v1/battery-assets")).json()["items"] == []
    else:
        assert lost.status_code == (422 if winner == "edit" else 409), lost.text
        code = "BATTERY_EXPENSE_INELIGIBLE" if winner == "edit" else "BATTERY_EXPENSE_PROTECTED"
        assert lost.json()["detail"]["code"] == code
        assert storage.data == original_bytes
        assert (await db_client.get(f"/api/v1/expenses/{expense_id}/attachments")).json()["items"]


@pytest.mark.asyncio
async def test_failed_acquisition_delete_preserves_metadata_and_bytes(
    db_client: AsyncClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    expense_id, _, _, storage = await _purchase(db_client, monkeypatch)
    original_bytes = dict(storage.data)
    original_commit = AsyncSession.commit

    async def fail_delete(session: AsyncSession) -> None:
        if any(isinstance(row, Expense) for row in session.deleted):
            raise RuntimeError("injected precommit acquisition failure")
        await original_commit(session)

    monkeypatch.setattr(AsyncSession, "commit", fail_delete)
    with pytest.raises(RuntimeError, match="injected precommit"):
        await db_client.delete(f"/api/v1/expenses/{expense_id}")
    assert storage.data == original_bytes
    assert (await db_client.get(f"/api/v1/expenses/{expense_id}")).status_code == 200
    attachments = await db_client.get(f"/api/v1/expenses/{expense_id}/attachments")
    assert len(attachments.json()["items"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("annual_command", ["confirm", "revise"])
async def test_asset_revision_serializes_against_annual_confirm_and_revision(
    db_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    annual_command: str,
) -> None:
    expense_id, _, registration, _ = await _purchase(db_client, monkeypatch)
    registered = await db_client.post("/api/v1/battery-assets", json=registration)
    assert registered.status_code == 201, registered.text
    asset = registered.json()
    asset_id = asset["id"]
    annual_year = asset["applicable_years"][0]
    annual_path = f"/api/v1/battery-assets/{asset_id}/annual-use/{annual_year}"
    annual = await db_client.put(
        annual_path, json={"private_use_percentage": "45", "basis_note": "EMS estimate"}
    )
    assert annual.status_code == 200, annual.text
    asset_body = {
        "expected_version": asset["version"],
        "expected_annual_versions": {annual_year: annual.json()["version"]},
        "reason": "Corrected battery purchase basis",
        "net_basis": "11000.00",
    }
    preview = await db_client.post(
        f"/api/v1/battery-assets/{asset_id}/revisions/preview", json=asset_body
    )
    assert preview.status_code == 200, preview.text
    asset_body["acknowledge_affected_periods"] = preview.json()["affected_periods"]
    original_rows = battery_service._asset_revision_rows
    original_annual_command = getattr(battery_service, f"{annual_command}_annual_use")
    asset_locked, annual_waiting, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def hold_asset_lock(*args: Any, **kwargs: Any) -> Any:
        result = await original_rows(*args, **kwargs)
        if kwargs.get("lock"):
            asset_locked.set()
            await asyncio.wait_for(release.wait(), 10)
        return result

    async def signal_annual_command(*args: Any, **kwargs: Any) -> Any:
        annual_waiting.set()
        return await original_annual_command(*args, **kwargs)

    monkeypatch.setattr(battery_service, "_asset_revision_rows", hold_asset_lock)
    monkeypatch.setattr(
        battery_service, f"{annual_command}_annual_use", signal_annual_command
    )
    annual_body = {
        "expected_version": annual.json()["version"],
        "expected_asset_version": asset["version"],
        "private_use_percentage": "45",
        "basis_note": "Updated EMS estimate",
    }
    if annual_command == "revise":
        annual_body.update(
            reason="Corrected annual meter allocation",
            acknowledge_affected_periods=[f"{annual_year}-Q4"],
        )

    async def change_asset() -> Any:
        return await db_client.post(
            f"/api/v1/battery-assets/{asset_id}/revisions", json=asset_body
        )

    async def change_annual() -> Any:
        if annual_command == "confirm":
            return await db_client.post(
                f"{annual_path}/confirm",
                json={
                    "expected_version": annual.json()["version"],
                    "expected_asset_version": asset["version"],
                },
            )
        return await db_client.post(f"{annual_path}/revisions", json=annual_body)

    asset_task = asyncio.create_task(change_asset())
    annual_task: asyncio.Task[Any] | None = None
    try:
        await asyncio.wait_for(asset_locked.wait(), 10)
        annual_task = asyncio.create_task(change_annual())
        await asyncio.wait_for(annual_waiting.wait(), 10)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(annual_task), 0.1)
    finally:
        release.set()
        results = await asyncio.wait_for(
            asyncio.gather(asset_task, *([annual_task] if annual_task is not None else [])), 10
        )
    assert len(results) == 2
    asset_result, annual_result = results
    assert asset_result.status_code == 200, asset_result.text
    assert annual_result.status_code == 409, annual_result.text
    assert annual_result.json()["detail"]["code"] == "BATTERY_VERSION_CONFLICT"
    current = await db_client.get(annual_path)
    assert current.status_code == 200
    assert current.json()["version"] == annual.json()["version"] + 1
    assert current.json()["confirmed"] is False
    assert current.json()["calculation_base"] is None
    stale_calculation = await db_client.post(
        f"{annual_path}/calculate",
        json={
            "expected_version": annual.json()["version"],
            "expected_asset_version": asset["version"],
        },
    )
    stale_confirmation = await db_client.post(
        f"{annual_path}/confirm",
        json={
            "expected_version": annual.json()["version"],
            "expected_asset_version": asset["version"],
        },
    )
    assert stale_calculation.status_code == 409
    assert stale_confirmation.status_code == 409
    stale_asset_body = {
        "expected_version": asset_result.json()["version"],
        "expected_annual_versions": {annual_year: annual.json()["version"]},
        "reason": "Basis correction based on an outdated annual version",
        "net_basis": "12000.00",
    }
    stale_asset_preview = await db_client.post(
        f"/api/v1/battery-assets/{asset_id}/revisions/preview", json=stale_asset_body
    )
    assert stale_asset_preview.status_code == 200, stale_asset_preview.text
    stale_asset_body["acknowledge_affected_periods"] = stale_asset_preview.json()[
        "affected_periods"
    ]
    rejected_asset_revision = await db_client.post(
        f"/api/v1/battery-assets/{asset_id}/revisions", json=stale_asset_body
    )
    assert rejected_asset_revision.status_code == 409
    assert Decimal(
        (await db_client.get(f"/api/v1/battery-assets/{asset_id}")).json()["net_basis"]
    ) == Decimal("11000.000")
    assert (await db_client.get(f"/api/v1/expenses/{expense_id}")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["precommit", "postcommit_refresh"])
async def test_evidence_compensation_respects_committed_ownership(
    db_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    _, _, registration, storage = await _purchase(db_client, monkeypatch)
    registered = await db_client.post("/api/v1/battery-assets", json=registration)
    assert registered.status_code == 201, registered.text
    asset_id = registered.json()["id"]
    year = date.today().year
    path = f"/api/v1/battery-assets/{asset_id}/annual-use/{year}"
    annual = await db_client.put(path, json={"private_use_percentage": "45", "basis_note": "EMS"})
    assert annual.status_code == 200
    original_bytes = dict(storage.data)
    original_commit, original_refresh = AsyncSession.commit, AsyncSession.refresh
    committed = False

    async def evidence_commit(session: AsyncSession) -> None:
        nonlocal committed
        is_evidence = any(isinstance(row, BatteryAnnualEvidence) for row in session.new)
        if is_evidence and failure == "precommit":
            raise RuntimeError("injected precommit evidence failure")
        await original_commit(session)
        if is_evidence:
            committed = True

    async def evidence_refresh(session: AsyncSession, instance: object, *args: Any,
                               **kwargs: Any) -> None:
        if isinstance(instance, BatteryAnnualEvidence) and failure == "postcommit_refresh":
            assert committed
            raise RuntimeError("injected postcommit evidence read failure")
        await original_refresh(session, instance, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "commit", evidence_commit)
    monkeypatch.setattr(AsyncSession, "refresh", evidence_refresh)
    with pytest.raises(RuntimeError, match="injected"):
        await db_client.post(f"{path}/evidence",
                             files={"file": ("ems.pdf", b"%PDF-ems-evidence", "application/pdf")})
    retained = await db_client.get(path)
    assert retained.status_code == 200
    evidence = retained.json()["evidence"]
    if failure == "precommit":
        assert committed is False
        assert evidence == [] and storage.data == original_bytes
    else:
        assert committed is True
        assert len(evidence) == 1 and len(storage.data) == len(original_bytes) + 1
        content = await db_client.get(f"{path}/evidence/{evidence[0]['id']}/content")
        assert content.status_code == 200 and content.content == b"%PDF-ems-evidence"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["percent", "label", "active"])
async def test_asset_revision_rejects_changed_preview_rate_without_partial_writes(
    db_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    expense_id, _, registration, _ = await _purchase(db_client, monkeypatch)
    registered = await db_client.post("/api/v1/battery-assets", json=registration)
    assert registered.status_code == 201, registered.text
    asset = registered.json()
    path = f"/api/v1/battery-assets/{asset['id']}"
    year = asset["applicable_years"][0]
    annual_path = f"{path}/annual-use/{year}"
    annual = await db_client.put(
        annual_path, json={"private_use_percentage": "45", "basis_note": "Original EMS basis"}
    )
    assert annual.status_code == 200, annual.text
    annual_before = (await db_client.get(annual_path)).json()
    expense_before = (await db_client.get(f"/api/v1/expenses/{expense_id}")).json()
    rates = (await db_client.get("/api/v1/vat-rates")).json()["items"]
    rate = next(row for row in rates if Decimal(row["percent"]) == Decimal("9"))
    body = {
        "expected_version": asset["version"],
        "expected_annual_versions": {str(year): annual.json()["version"]},
        "reason": "Correct supplier VAT selection", "vat_rate_id": rate["id"],
    }
    preview = await db_client.post(f"{path}/revisions/preview", json=body)
    assert preview.status_code == 200, preview.text
    assert Decimal(
        preview.json()["annual_previews"][0]["proposed_calculation_vat"]
    ) == Decimal("81")
    body["acknowledge_affected_periods"] = preview.json()["affected_periods"]
    # Omitting the snapshot cannot bypass re-preview; no client amount is accepted.
    missing = await db_client.post(f"{path}/revisions", json=body)
    assert missing.status_code == 409, missing.text
    assert missing.json()["detail"]["code"] == "BATTERY_PREVIEW_STALE"
    body["expected_rate_snapshot"] = preview.json()["rate_snapshot"]
    edit = {"label": rate["label"], "percent": rate["percent"], "active": True}
    edit[change] = {"percent": "15", "label": "Corrected dictionary label", "active": False}[change]
    changed = await db_client.put(f"/api/v1/vat-rates/{rate['id']}", json=edit)
    assert changed.status_code == 200, changed.text
    rejected = await db_client.post(f"{path}/revisions", json=body)
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"]["code"] == "BATTERY_PREVIEW_STALE"
    assert (await db_client.get(path)).json() == asset
    assert (await db_client.get(annual_path)).json() == annual_before
    assert (await db_client.get(f"/api/v1/expenses/{expense_id}")).json() == expense_before
    assert (await db_client.get(f"{path}/revisions")).json()["total"] == 0
    if change == "active":
        invalid = await db_client.post(f"{path}/revisions/preview", json=body)
        assert invalid.status_code == 422, invalid.text
        edit["active"] = True
        restored = await db_client.put(f"/api/v1/vat-rates/{rate['id']}", json=edit)
        assert restored.status_code == 200, restored.text
    fresh = await db_client.post(f"{path}/revisions/preview", json=body)
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["rate_snapshot"] != preview.json()["rate_snapshot"]
    body["expected_rate_snapshot"] = fresh.json()["rate_snapshot"]
    body["acknowledge_affected_periods"] = fresh.json()["affected_periods"]
    saved = await db_client.post(f"{path}/revisions", json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["purchase_vat_rate_percent"] == fresh.json()["after"][
        "purchase_vat_rate_percent"
    ]
    history = (await db_client.get(f"{path}/revisions")).json()
    assert history["total"] == 1
    assert history["items"][0]["after_snapshot"]["annual_previews"] == fresh.json()[
        "annual_previews"
    ]

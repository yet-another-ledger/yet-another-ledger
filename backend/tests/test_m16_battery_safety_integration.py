"""Acquisition serialization and evidence ownership failure regressions."""

from __future__ import annotations

import asyncio
from datetime import date
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

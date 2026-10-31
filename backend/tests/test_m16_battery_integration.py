"""M16 Step 6 runtime-role API and report integration coverage."""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import jai.api.battery as battery_api
from jai.db import set_rls_company
from jai.models.company import Company
from jai.models.user import User
from jai.services.storage import Storage

pytestmark = pytest.mark.integration


class MemoryStorage(Storage):
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.open_calls = 0

    def save(self, namespace: str, key: str, content: bytes, content_type: str) -> str:
        del content_type
        storage_key = f"{namespace}/{key}"
        self.data[storage_key] = content
        return storage_key

    def open(self, key: str) -> bytes:
        self.open_calls += 1
        return self.data[key]

    def delete(self, key: str) -> None:
        self.data.pop(key, None)

    def exists(self, key: str) -> bool:
        return key in self.data


async def _owner_company(client: AsyncClient, email: str) -> None:
    registered = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": "testpassword1"}
    )
    assert registered.status_code == 201
    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "testpassword1"}
    )
    assert login.status_code == 200
    setup = await client.post("/api/v1/auth/mfa/setup")
    assert setup.status_code == 200
    verified = await client.post(
        "/api/v1/auth/mfa/verify", json={"code": pyotp.TOTP(setup.json()["secret"]).now()}
    )
    assert verified.status_code == 204
    company = await client.put(
        "/api/v1/company",
        json={"name": "Battery integration", "base_currency": "EUR", "country_code": "NL"},
    )
    assert company.status_code == 200


async def _purchase_dictionaries(client: AsyncClient) -> tuple[str, str, str]:
    categories = await client.get("/api/v1/expense-categories")
    treatments = await client.get("/api/v1/vat-treatments?side=PURCHASE")
    rates = await client.get("/api/v1/vat-rates")
    assert categories.status_code == treatments.status_code == rates.status_code == 200
    category = categories.json()["items"][0]
    treatment = next(
        row for row in treatments.json()["items"] if row["code"] == "NL_DOMESTIC_PURCH"
    )
    rate = next(row for row in rates.json()["items"] if Decimal(row["percent"]) == Decimal("21"))
    return category["id"], treatment["id"], rate["id"]


@pytest.mark.asyncio
async def test_battery_registration_annual_confirmation_report_and_expense_guards(
    db_client: AsyncClient,
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = MemoryStorage()
    monkeypatch.setattr(battery_api, "get_storage", lambda: storage)
    monkeypatch.setattr(battery_api, "get_settings", lambda: SimpleNamespace(max_receipt_bytes=64))
    await _owner_company(db_client, "battery-step6@example.com")
    category_id, treatment_id, rate_id = await _purchase_dictionaries(db_client)
    expense_body = {
        "expense_date": "2024-12-20",
        "category_id": category_id,
        "supplier_name": "Battery supplier",
        "vat_treatment_id": treatment_id,
        "vat_rate_id": rate_id,
        "net_amount": "10000.00",
        "vat_amount": "2100.00",
        "deductible": True,
        "business_percentage": "55",
        "depreciation_years": 5,
        "include_in_pl": True,
        "pl_cost_basis": "AUTO",
    }
    created_expense = await db_client.post("/api/v1/expenses", json=expense_body)
    assert created_expense.status_code == 201, created_expense.text
    expense_id = created_expense.json()["id"]
    pl_before_registration = await db_client.get(
        "/api/v1/reports/profit-loss?from=2024-01-01&to=2024-12-31&granularity=quarter"
    )
    assert Decimal(pl_before_registration.json()["expense_actual"]) == Decimal("1100.00")

    preview_body = {
        "acquisition_expense_id": expense_id,
        "in_service_date": "2025-02-01",
        "owner_confirms_ordinary_vat_full_deduction": True,
    }
    preview = await db_client.post("/api/v1/battery-assets/preview", json=preview_body)
    assert preview.status_code == 200, preview.text
    assert preview.json()["eligible"] is True
    assert preview.json()["applicable_years"] == [2025, 2026, 2027, 2028, 2029]
    assert "2024-Q4" in preview.json()["affected_historical_periods"]
    unacknowledged = await db_client.post("/api/v1/battery-assets", json=preview_body)
    assert unacknowledged.status_code == 409
    assert unacknowledged.json()["detail"]["code"] == "BATTERY_HISTORICAL_ACK_REQUIRED"
    registered = await db_client.post(
        "/api/v1/battery-assets",
        json={**preview_body, "acknowledge_affected_periods": True},
    )
    assert registered.status_code == 201, registered.text
    asset = registered.json()
    assert asset["ordinary_vat_full_deduction_confirmed"] is True
    assert "2024-Q4" in asset["historical_acknowledged_periods"]
    asset_id = asset["id"]
    listed_expense = await db_client.get(f"/api/v1/expenses/{expense_id}")
    assert listed_expense.json()["include_in_pl"] is False
    duplicate = await db_client.post(
        "/api/v1/battery-assets",
        json={**preview_body, "acknowledge_affected_periods": True},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "BATTERY_ACQUISITION_ALREADY_REGISTERED"

    protected = await db_client.put(
        f"/api/v1/expenses/{expense_id}",
        json={**expense_body, "net_amount": "10001.00"},
    )
    assert protected.status_code == 409
    async with db_session_maker() as session:
        owner = (
            await session.execute(select(User).where(User.email == "battery-step6@example.com"))
        ).scalar_one()
        assert owner.company_id is not None
        await set_rls_company(session, owner.company_id)
        await session.execute(
            text("UPDATE vat_rate SET active=false, label='Retired 21', percent=15 WHERE id=:id"),
            {"id": rate_id},
        )
        await session.execute(
            text("UPDATE vat_treatment SET active=false, label='Retired purchase' WHERE id=:id"),
            {"id": treatment_id},
        )
        await session.commit()
    editable = await db_client.put(
        f"/api/v1/expenses/{expense_id}", json={**expense_body, "note": "Updated memo"}
    )
    assert editable.status_code == 200, editable.text
    assert editable.json()["include_in_pl"] is False
    assert Decimal(editable.json()["vat_rate_percent"]) == Decimal("21")
    assert editable.json()["vat_rate_label"] != "Retired 21"
    assert editable.json()["vat_treatment_label"] != "Retired purchase"
    deleted = await db_client.delete(f"/api/v1/expenses/{expense_id}")
    assert deleted.status_code == 409

    missing_2026 = await db_client.get(f"/api/v1/battery-assets/{asset_id}/annual-use/2026")
    assert missing_2026.status_code == 200 and missing_2026.json() is None
    untouched_annual = await db_client.get(f"/api/v1/battery-assets/{asset_id}/annual-use/2026")
    assert untouched_annual.json() is None
    empty_report = await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=4")
    missing_box = empty_report.json()["boxes"]["box_1d"]
    assert empty_report.status_code == 200
    assert missing_box["is_complete"] is False
    assert missing_box["missing_inputs"] == [
        {"asset_id": asset_id, "year": 2026, "code": "MISSING"}
    ]

    annual_input = await db_client.put(
        f"/api/v1/battery-assets/{asset_id}/annual-use/2025",
        json={"private_use_percentage": "45", "basis_note": "EMS annual statement"},
    )
    assert annual_input.status_code == 200, annual_input.text
    invalid_percentage = await db_client.put(
        f"/api/v1/battery-assets/{asset_id}/annual-use/2025",
        json={
            "expected_version": annual_input.json()["version"],
            "private_use_percentage": "101",
            "basis_note": "Invalid",
        },
    )
    assert invalid_percentage.status_code == 422
    invalid_evidence = await db_client.post(
        f"/api/v1/battery-assets/{asset_id}/annual-use/2025/evidence",
        files={"file": ("notes.txt", b"not a PDF", "text/plain")},
    )
    assert invalid_evidence.status_code == 422
    evidence = await db_client.post(
        f"/api/v1/battery-assets/{asset_id}/annual-use/2025/evidence",
        files={"file": ("ems.pdf", b"%PDF-evidence", "application/pdf")},
    )
    assert evidence.status_code == 201, evidence.text
    calculation = await db_client.post(
        f"/api/v1/battery-assets/{asset_id}/annual-use/2025/calculate",
        json={
            "expected_version": annual_input.json()["version"],
            "expected_asset_version": asset["version"],
        },
    )
    assert calculation.status_code == 200, calculation.text
    assert Decimal(calculation.json()["calculation_base"]) == Decimal("900.00")
    assert Decimal(calculation.json()["calculation_vat"]) == Decimal("189.00")
    confirmed = await db_client.post(
        f"/api/v1/battery-assets/{asset_id}/annual-use/2025/confirm",
        json={
            "expected_version": annual_input.json()["version"],
            "expected_asset_version": asset["version"],
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["confirmed"] is True
    assert len(confirmed.json()["evidence"]) == 1
    confirmation_conflict = await db_client.put(
        f"/api/v1/battery-assets/{asset_id}/annual-use/2025",
        json={
            "expected_version": confirmed.json()["version"],
            "private_use_percentage": "40",
            "basis_note": "Would overwrite confirmed snapshot",
        },
    )
    assert confirmation_conflict.status_code == 409
    year_one_report = await db_client.get("/api/v1/reports/vat-return?year=2025&quarter=4")
    box_1d = year_one_report.json()["boxes"]["box_1d"]
    assert Decimal(box_1d["base"]) == Decimal("900.00")
    assert Decimal(box_1d["vat"]) == Decimal("189.00")

    annual_revision_body = {
        "expected_version": confirmed.json()["version"],
        "expected_asset_version": asset["version"],
        "private_use_percentage": "40",
        "basis_note": "Corrected EMS allocation",
        "reason": "The original EMS export used the wrong household meter",
        "acknowledge_affected_periods": ["2025-Q4"],
    }
    annual_revision_preview = await db_client.post(
        f"/api/v1/battery-assets/{asset_id}/annual-use/2025/revisions/preview",
        json=annual_revision_body,
    )
    assert annual_revision_preview.status_code == 200, annual_revision_preview.text
    assert Decimal(annual_revision_preview.json()["current_calculation_base"]) == Decimal("900.00")
    assert Decimal(annual_revision_preview.json()["proposed_calculation_base"]) == Decimal("800.00")
    annual_revision_unacknowledged = await db_client.post(
        f"/api/v1/battery-assets/{asset_id}/annual-use/2025/revisions",
        json={**annual_revision_body, "acknowledge_affected_periods": []},
    )
    assert annual_revision_unacknowledged.status_code == 409
    revised_annual = await db_client.post(
        f"/api/v1/battery-assets/{asset_id}/annual-use/2025/revisions",
        json=annual_revision_body,
    )
    assert revised_annual.status_code == 200, revised_annual.text
    assert revised_annual.json()["confirmed"] is False
    assert revised_annual.json()["evidence"] == confirmed.json()["evidence"]
    annual_history = await db_client.get(
        f"/api/v1/battery-assets/{asset_id}/annual-use/2025/revisions"
    )
    assert annual_history.status_code == 200 and annual_history.json()["total"] == 1
    assert annual_history.json()["items"][0]["reason"] == annual_revision_body["reason"]
    assert annual_history.json()["items"][0]["actor_id"] is not None
    assert annual_history.json()["items"][0]["created_at"]
    stale_confirm = await db_client.post(
        f"/api/v1/battery-assets/{asset_id}/annual-use/2025/confirm",
        json={
            "expected_version": confirmed.json()["version"],
            "expected_asset_version": asset["version"],
        },
    )
    assert stale_confirm.status_code == 409
    incomplete_after_revision = await db_client.get(
        "/api/v1/reports/vat-return?year=2025&quarter=4"
    )
    revised_box = incomplete_after_revision.json()["boxes"]["box_1d"]
    assert revised_box["is_complete"] is False
    assert Decimal(revised_box["known_battery_vat"]) == Decimal("0.00")
    annual_reconfirmed = await db_client.post(
        f"/api/v1/battery-assets/{asset_id}/annual-use/2025/confirm",
        json={
            "expected_version": revised_annual.json()["version"],
            "expected_asset_version": asset["version"],
        },
    )
    assert annual_reconfirmed.status_code == 200, annual_reconfirmed.text
    revised_report = await db_client.get("/api/v1/reports/vat-return?year=2025&quarter=4")
    assert Decimal(revised_report.json()["boxes"]["box_1d"]["base"]) == Decimal("800.00")
    assert Decimal(revised_report.json()["boxes"]["box_1d"]["vat"]) == Decimal("168.00")

    asset_revision_body = {
        "expected_version": asset["version"],
        "reason": "Corrected supplier invoice basis and service date",
        "acquisition_expense_date": "2025-01-15",
        "in_service_date": "2026-02-01",
        "net_basis": "11000.00",
        "purchase_vat": "2310.00",
    }
    asset_revision_preview = await db_client.post(
        f"/api/v1/battery-assets/{asset_id}/revisions/preview",
        json=asset_revision_body,
    )
    assert asset_revision_preview.status_code == 200, asset_revision_preview.text
    assert asset_revision_preview.json()["expense_before"]["expense_date"] == "2024-12-20"
    assert asset_revision_preview.json()["expense_after"]["expense_date"] == "2025-01-15"
    assert Decimal(asset_revision_preview.json()["expense_after"]["base_net_amount"]) == Decimal(
        "11000.00"
    )
    assert asset_revision_preview.json()["expense_before"]["currency"] == "EUR"
    assert asset_revision_preview.json()["expense_after"]["currency"] == "EUR"
    assert Decimal(asset_revision_preview.json()["expense_before"]["exchange_rate"]) == Decimal(
        "1"
    )
    assert Decimal(asset_revision_preview.json()["expense_after"]["exchange_rate"]) == Decimal(
        "1"
    )
    assert Decimal(asset_revision_preview.json()["expense_after"]["vat_rate_percent"]) == Decimal(
        "21"
    )
    rates = await db_client.get("/api/v1/vat-rates")
    alternative_rate = next(
        row for row in rates.json()["items"] if Decimal(row["percent"]) == Decimal("9")
    )
    rate_revision_body = {**asset_revision_body, "vat_rate_id": alternative_rate["id"]}
    rate_preview = await db_client.post(
        f"/api/v1/battery-assets/{asset_id}/revisions/preview", json=rate_revision_body
    )
    assert rate_preview.status_code == 200, rate_preview.text
    rate_revision_body["expected_rate_snapshot"] = rate_preview.json()["rate_snapshot"]
    assert Decimal(rate_preview.json()["expense_after"]["vat_rate_percent"]) == Decimal("9")
    affected_periods = asset_revision_preview.json()["affected_periods"]
    assert "2024-Q4" in affected_periods and "2025-Q1" in affected_periods
    assert "2025-Q4" in affected_periods and "2030-Q4" in affected_periods
    asset_revision_missing_ack = await db_client.post(
        f"/api/v1/battery-assets/{asset_id}/revisions",
        json=asset_revision_body,
    )
    assert asset_revision_missing_ack.status_code == 409
    concurrent_revisions = await asyncio.gather(
        *(
            db_client.post(
                f"/api/v1/battery-assets/{asset_id}/revisions",
                json={
                    **rate_revision_body,
                        "expected_annual_versions": {"2025": 2},
                    "acknowledge_affected_periods": affected_periods,
                },
            )
            for _ in range(2)
        )
    )
    assert sorted(response.status_code for response in concurrent_revisions) == [200, 409]
    revised_asset = next(
        response.json() for response in concurrent_revisions if response.status_code == 200
    )
    assert revised_asset["version"] == asset["version"] + 1
    updated_expense = await db_client.get(f"/api/v1/expenses/{expense_id}")
    assert updated_expense.status_code == 200
    assert updated_expense.json()["expense_date"] == "2025-01-15"
    assert Decimal(updated_expense.json()["base_net_amount"]) == Decimal("11000.00")
    assert Decimal(updated_expense.json()["net_amount"]) == Decimal("11000.00")
    assert updated_expense.json()["vat_rate_id"] == alternative_rate["id"]
    assert Decimal(updated_expense.json()["vat_rate_percent"]) == Decimal("9")
    assert updated_expense.json()["include_in_pl"] is False
    asset_history = await db_client.get(f"/api/v1/battery-assets/{asset_id}/revisions")
    assert asset_history.status_code == 200 and asset_history.json()["total"] == 1
    assert asset_history.json()["items"][0]["actor_id"] is not None
    assert asset_history.json()["items"][0]["created_at"]
    preserved_annual = next(
        row
        for row in asset_history.json()["items"][0]["before_snapshot"]["annual_before"]
        if row["year"] == 2025
    )
    invalidated_annual = next(
        row
        for row in asset_history.json()["items"][0]["after_snapshot"]["annual_after"]
        if row["year"] == 2025
    )
    assert Decimal(preserved_annual["calculation_base"]) == Decimal("800.00")
    assert Decimal(preserved_annual["calculation_vat"]) == Decimal("168.00")
    assert invalidated_annual["confirmed"] is False
    assert invalidated_annual["calculation_base"] is None
    historical_annual = await db_client.get(f"/api/v1/battery-assets/{asset_id}/annual-use/2025")
    assert historical_annual.status_code == 200
    assert historical_annual.json()["confirmed"] is False
    assert historical_annual.json()["evidence"] == confirmed.json()["evidence"]
    old_year_report = await db_client.get("/api/v1/reports/vat-return?year=2025&quarter=4")
    assert Decimal(old_year_report.json()["boxes"]["box_1d"]["vat"]) == Decimal("0.00")
    new_year_report = await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=4")
    assert new_year_report.json()["boxes"]["box_1d"]["is_complete"] is False
    new_year_missing = new_year_report.json()["boxes"]["box_1d"]["missing_inputs"]
    assert {item["year"] for item in new_year_missing} >= {2026}
    acquisition_year = await db_client.get("/api/v1/reports/vat-return?year=2024&quarter=4")
    assert Decimal(acquisition_year.json()["boxes"]["box_1d"]["vat"]) == Decimal("0.00")
    assert Decimal(acquisition_year.json()["boxes"]["box_5b"]["vat"]) == Decimal("0.00")
    assert acquisition_year.json()["boxes"]["box_1d"]["missing_inputs"] == []
    corrected_acquisition_period = await db_client.get(
        "/api/v1/reports/vat-return?year=2025&quarter=1"
    )
    assert Decimal(
        corrected_acquisition_period.json()["boxes"]["box_5b"]["vat"]
    ) == Decimal("2310.00")
    pl = await db_client.get(
        "/api/v1/reports/profit-loss?from=2024-01-01&to=2024-12-31&granularity=quarter"
    )
    assert pl.status_code == 200
    assert Decimal(pl.json()["expense_actual"]) == Decimal("0")

    fifth_year = await db_client.put(
        f"/api/v1/battery-assets/{asset_id}/annual-use/2030",
        json={"private_use_percentage": "45", "basis_note": "Fifth-year EMS statement"},
    )
    assert fifth_year.status_code == 200
    fifth_confirmed = await db_client.post(
        f"/api/v1/battery-assets/{asset_id}/annual-use/2030/confirm",
        json={
            "expected_version": fifth_year.json()["version"],
            "expected_asset_version": revised_asset["version"],
        },
    )
    assert fifth_confirmed.status_code == 200
    fifth_report = await db_client.get("/api/v1/reports/vat-return?year=2030&quarter=4")
    assert Decimal(fifth_report.json()["boxes"]["box_1d"]["vat"]) == Decimal("89.10")
    non_q4 = await db_client.get("/api/v1/reports/vat-return?year=2030&quarter=2")
    assert Decimal(non_q4.json()["boxes"]["box_1d"]["vat"]) == Decimal("0.00")
    sixth_year = await db_client.get("/api/v1/reports/vat-return?year=2031&quarter=4")
    sixth_box = sixth_year.json()["boxes"]["box_1d"]
    assert Decimal(sixth_box["vat"]) == Decimal("0.00")
    assert sixth_box["missing_inputs"] == []

    zero_input = await db_client.put(
        f"/api/v1/battery-assets/{asset_id}/annual-use/2026",
        json={"private_use_percentage": "0", "basis_note": "EMS indicates no private use"},
    )
    assert zero_input.status_code == 200
    zero_confirmed = await db_client.post(
        f"/api/v1/battery-assets/{asset_id}/annual-use/2026/confirm",
        json={
            "expected_version": zero_input.json()["version"],
            "expected_asset_version": revised_asset["version"],
        },
    )
    assert zero_confirmed.status_code == 200
    complete_report = await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=4")
    complete_box = complete_report.json()["boxes"]["box_1d"]
    assert complete_box["is_complete"] is True
    assert complete_box["missing_inputs"] == []
    assert Decimal(complete_box["base"]) == Decimal("0.00")

    concurrent_body = {"private_use_percentage": "30", "basis_note": "Concurrent input"}
    concurrent_writes = await asyncio.gather(
        *(
            db_client.put(
                f"/api/v1/battery-assets/{asset_id}/annual-use/2027",
                json={"expected_version": None, **concurrent_body},
            )
            for _ in range(2)
        )
    )
    assert sorted(response.status_code for response in concurrent_writes) == [200, 409]
    winner = next(response.json() for response in concurrent_writes if response.status_code == 200)
    input_revision = {
        "expected_version": winner["version"],
        "expected_asset_version": revised_asset["version"],
        **concurrent_body,
        "reason": "Updated the annual meter estimate",
        "acknowledge_affected_periods": ["2027-Q4"],
    }
    revision_preview = await db_client.post(
        f"/api/v1/battery-assets/{asset_id}/annual-use/2027/revisions/preview",
        json=input_revision,
    )
    assert revision_preview.status_code == 200, revision_preview.text
    next_version = await db_client.post(
        f"/api/v1/battery-assets/{asset_id}/annual-use/2027/revisions",
        json=input_revision,
    )
    assert next_version.status_code == 200, next_version.text
    stale = await db_client.post(
        f"/api/v1/battery-assets/{asset_id}/annual-use/2027/revisions",
        json=input_revision,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "BATTERY_VERSION_CONFLICT"
    paged_history = await db_client.get(
        f"/api/v1/battery-assets/{asset_id}/annual-use/2027/revisions?limit=100"
    )
    assert paged_history.status_code == 200
    assert paged_history.json()["limit"] == 100
    assert paged_history.json()["total"] == 1

    content = await db_client.get(
        f"/api/v1/battery-assets/{asset_id}/annual-use/2025/evidence/{evidence.json()['id']}/content"
    )
    assert content.status_code == 200 and content.content == b"%PDF-evidence"

    async with db_session_maker() as session:
        owner = (
            await session.execute(select(User).where(User.email == "battery-step6@example.com"))
        ).scalar_one()
        assert owner.company_id is not None
        await set_rls_company(session, owner.company_id)
        await session.execute(text("UPDATE vat_rate SET active=true WHERE id=:id"), {"id": rate_id})
        await session.execute(
            text("UPDATE vat_treatment SET active=true WHERE id=:id"), {"id": treatment_id}
        )
        await session.commit()
    today = date.today().isoformat()
    raced_expense = await db_client.post(
        "/api/v1/expenses",
        json={
            **expense_body,
            "expense_date": today,
            "net_amount": "1000",
            "vat_amount": "150",
        },
    )
    assert raced_expense.status_code == 201
    concurrent_registration = await asyncio.gather(
        *(
            db_client.post(
                "/api/v1/battery-assets",
                json={
                    "acquisition_expense_id": raced_expense.json()["id"],
                    "in_service_date": today,
                    "owner_confirms_ordinary_vat_full_deduction": True,
                    "acknowledge_affected_periods": True,
                },
            )
            for _ in range(2)
        )
    )
    assert sorted(response.status_code for response in concurrent_registration) == [201, 409]
    successful_registration = next(
        response for response in concurrent_registration if response.status_code == 201
    )
    failed_registration = next(
        response for response in concurrent_registration if response.status_code == 409
    )
    assert failed_registration.json()["detail"]["code"] == "BATTERY_ACQUISITION_ALREADY_REGISTERED"
    legacy_expense = await db_client.post(
        "/api/v1/expenses",
        json={
            **expense_body,
            "expense_date": today,
            "net_amount": "1000",
            "vat_amount": "150",
            "supplier_name": "Legacy private-use expense",
        },
    )
    assert legacy_expense.status_code == 201
    current_q4 = await db_client.get(
        f"/api/v1/reports/vat-return?year={date.today().year}&quarter=4"
    )
    legacy_box = current_q4.json()["boxes"]["box_1d"]
    assert legacy_box["base"] is None
    assert Decimal(legacy_box["vat"]) == Decimal("67.50")
    assert Decimal(legacy_box["known_battery_base"]) == Decimal("0.00")
    assert Decimal(legacy_box["known_battery_vat"]) == Decimal("0.00")
    assert legacy_box["is_complete"] is False
    assert any(
        item["asset_id"] == successful_registration.json()["id"]
        for item in legacy_box["missing_inputs"]
    )
    assert Decimal(current_q4.json()["boxes"]["box_5b"]["vat"]) == Decimal("300.00")

    # The app intentionally allows one registered owner. Reassociate that
    # owner to a separately seeded tenant, then exercise the real runtime-role
    # request path against the foreign evidence ID.
    async with db_session_maker() as session:
        second_company = Company(name="Battery integration B", base_currency="EUR")
        session.add(second_company)
        await session.flush()
        owner = (
            await session.execute(select(User).where(User.email == "battery-step6@example.com"))
        ).scalar_one()
        owner.company_id = second_company.id
        await session.commit()
    calls_before = storage.open_calls
    foreign_file = await db_client.get(
        f"/api/v1/battery-assets/{asset_id}/annual-use/2025/evidence/{evidence.json()['id']}/content"
    )
    assert foreign_file.status_code == 404
    assert storage.open_calls == calls_before


@pytest.mark.asyncio
async def test_vat_report_aggregates_each_battery_rounded_snapshot_with_unknown_legacy_base(
    db_client: AsyncClient,
) -> None:
    await _owner_company(db_client, "battery-aggregation@example.com")
    category_id, treatment_id, rate_id = await _purchase_dictionaries(db_client)
    year = date.today().year - 1
    in_service_date = date(year, 1, 5)

    for index in range(2):
        purchase = await db_client.post(
            "/api/v1/expenses",
            json={
                "expense_date": in_service_date.isoformat(),
                "category_id": category_id,
                "supplier_name": f"Small battery {index}",
                "vat_treatment_id": treatment_id,
                "vat_rate_id": rate_id,
                "net_amount": "0.25",
                "vat_amount": "0.05",
                "deductible": True,
                "business_percentage": "50",
                "depreciation_years": 1,
                "include_in_pl": True,
                "pl_cost_basis": "AUTO",
            },
        )
        assert purchase.status_code == 201, purchase.text
        registered = await db_client.post(
            "/api/v1/battery-assets",
            json={
                "acquisition_expense_id": purchase.json()["id"],
                "in_service_date": in_service_date.isoformat(),
                "owner_confirms_ordinary_vat_full_deduction": True,
                "acknowledge_affected_periods": True,
            },
        )
        assert registered.status_code == 201, registered.text
        asset_id = registered.json()["id"]
        annual = await db_client.put(
            f"/api/v1/battery-assets/{asset_id}/annual-use/{year}",
            json={"private_use_percentage": "50", "basis_note": "Meter shows half private use"},
        )
        assert annual.status_code == 200, annual.text
        confirmed = await db_client.post(
            f"/api/v1/battery-assets/{asset_id}/annual-use/{year}/confirm",
            json={
                "expected_version": annual.json()["version"],
                "expected_asset_version": registered.json()["version"],
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        assert Decimal(confirmed.json()["calculation_base"]) == Decimal("0.03")
        assert Decimal(confirmed.json()["calculation_vat"]) == Decimal("0.01")

    legacy = await db_client.post(
        "/api/v1/expenses",
        json={
            "expense_date": in_service_date.isoformat(),
            "category_id": category_id,
            "supplier_name": "Legacy expense with unknown 1d base",
            "vat_treatment_id": treatment_id,
            "vat_rate_id": rate_id,
            "net_amount": "100",
            "vat_amount": "21",
            "deductible": True,
            "business_percentage": "50",
            "depreciation_years": 1,
            "include_in_pl": True,
            "pl_cost_basis": "AUTO",
        },
    )
    assert legacy.status_code == 201, legacy.text
    report = await db_client.get(f"/api/v1/reports/vat-return?year={year}&quarter=4")
    assert report.status_code == 200, report.text
    box_1d = report.json()["boxes"]["box_1d"]
    assert box_1d["base"] is None
    assert Decimal(box_1d["vat"]) == Decimal("10.52")
    assert Decimal(box_1d["known_battery_base"]) == Decimal("0.06")
    assert Decimal(box_1d["known_battery_vat"]) == Decimal("0.02")
    assert box_1d["missing_inputs"] == []
    assert box_1d["is_complete"] is False
    assert any(
        "base cannot be determined reliably" in warning for warning in report.json()["warnings"]
    )

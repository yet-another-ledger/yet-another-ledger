"""M20 Step 2 conversion regressions against isolated PostgreSQL fixtures."""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jai.db import set_rls_company
from jai.models.company import Company
from jai.models.fixed_asset import FixedAsset
from jai.services import expense as expense_service
from jai.services import fixed_asset as fixed_asset_service
from jai.services import fixed_asset_preview as fixed_asset_preview_service
from jai.services.reporting import dashboard as dashboard_service
from jai.services.reporting import pl as pl_service
from jai.services.storage import LocalStorage, set_storage


@pytest.fixture(autouse=True)
def _isolated_storage(tmp_path: Path):
    set_storage(LocalStorage(str(tmp_path)))
    yield
    set_storage(None)


async def _auth(client: AsyncClient, email: str = "asset-owner@example.com") -> None:
    await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "testpassword1"},
    )
    await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "testpassword1"},
    )
    response = await client.post("/api/v1/auth/mfa/setup")
    code = pyotp.TOTP(response.json()["secret"]).now()
    await client.post("/api/v1/auth/mfa/verify", json={"code": code})
    response = await client.put(
        "/api/v1/company",
        json={"name": "Asset Test Co", "base_currency": "EUR", "country_code": "NL"},
    )
    assert response.status_code == 200


@pytest.mark.integration
async def test_conversion_replaces_pl_once_and_protects_saved_purchase(
    db_client: AsyncClient,
    admin_session_maker: async_sessionmaker[AsyncSession],
    runtime_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FrozenDate(date):
        @classmethod
        def today(cls) -> date:
            return date(2026, 2, 3)

    monkeypatch.setattr(fixed_asset_preview_service, "date", FrozenDate)
    monkeypatch.setattr(fixed_asset_service, "date", FrozenDate)
    monkeypatch.setattr(expense_service, "date", FrozenDate)
    monkeypatch.setattr(pl_service, "date", FrozenDate)
    monkeypatch.setattr(dashboard_service, "date", FrozenDate)
    await _auth(db_client)
    categories = await db_client.post("/api/v1/expense-categories", json={"name": "Equipment"})
    assert categories.status_code == 201
    rates = await db_client.get("/api/v1/vat-rates")
    treatments = await db_client.get("/api/v1/vat-treatments?side=PURCHASE")
    rate = next(row for row in rates.json()["items"] if row["label"] == "NL standard (21%)")
    treatment = next(
        row for row in treatments.json()["items"] if row["code"] == "NL_DOMESTIC_PURCH"
    )
    purchase_response = await db_client.post(
        "/api/v1/expenses",
        json={
            "expense_date": "2026-01-01",
            "category_id": categories.json()["id"],
            "vat_treatment_id": treatment["id"],
            "vat_rate_id": rate["id"],
            "net_amount": "10000.00",
            "vat_amount": "2100.00",
            "depreciation_years": 5,
            "include_in_pl": True,
            "pl_cost_basis": "AUTO",
        },
    )
    assert purchase_response.status_code == 201, purchase_response.text
    original = purchase_response.json()
    original_cost = await db_client.get(f"/api/v1/expenses/{original['id']}/cost-preview")
    assert original_cost.status_code == 200
    assert Decimal(original_cost.json()["annual_cost"]) == Decimal("2000.00")
    old_pl_before = await db_client.get(
        "/api/v1/reports/profit-loss?from=2026-01-01&to=2026-01-30&granularity=month"
    )
    assert old_pl_before.status_code == 200
    assert Decimal(old_pl_before.json()["expense_actual"]) == Decimal("2000.00")
    attachment = await db_client.post(
        f"/api/v1/expenses/{original['id']}/attachments",
        files={"file": ("receipt.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 200, "image/png")},
    )
    assert attachment.status_code == 201, attachment.text
    terms = {
        "asset_name": "Fluke tester",
        "in_service_date": "2026-01-16",
        "useful_life_years": 5,
        "residual_value": "1000.00",
        "business_percentage": "100",
        "use_type": "FULL_BUSINESS",
    }
    preview_response = await db_client.post(
        f"/api/v1/fixed-assets/expenses/{original['id']}/conversion-preview", json=terms
    )
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["conversion_blocked"] is False
    preview_2026 = next(row for row in preview["yearly_deltas"] if row["year"] == 2026)
    assert Decimal(preview_2026["legacy_expense_amount"]) == Decimal("2000.00")
    assert Decimal(preview_2026["fixed_asset_depreciation"]) == Decimal("77.42")
    assert Decimal(preview_2026["difference"]) == Decimal("-1922.58")
    vat_before = await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=1")
    assert vat_before.status_code == 200
    assert Decimal(vat_before.json()["boxes"]["box_5b"]["vat"]) == Decimal("2100.00")
    confirm = {
        **terms,
        "preview_token": preview["preview_token"],
    }
    missing_token = {key: value for key, value in confirm.items() if key != "preview_token"}
    assert (
        await db_client.post(
            f"/api/v1/fixed-assets/expenses/{original['id']}/convert", json=missing_token
        )
    ).status_code == 422
    mismatched_terms = {**confirm, "asset_name": "Different reviewed asset"}
    assert (
        await db_client.post(
            f"/api/v1/fixed-assets/expenses/{original['id']}/convert", json=mismatched_terms
        )
    ).status_code == 409
    updated_source = await db_client.put(
        f"/api/v1/expenses/{original['id']}",
        json={
            "expense_date": "2026-01-01",
            "category_id": categories.json()["id"],
            "vat_treatment_id": treatment["id"],
            "vat_rate_id": rate["id"],
            "net_amount": "10000.00",
            "vat_amount": "2100.00",
            "depreciation_years": 5,
            "note": "Reviewed source receipt",
        },
    )
    assert updated_source.status_code == 200, updated_source.text
    stale = await db_client.post(
        f"/api/v1/fixed-assets/expenses/{original['id']}/convert", json=confirm
    )
    assert stale.status_code == 409
    preview_response = await db_client.post(
        f"/api/v1/fixed-assets/expenses/{original['id']}/conversion-preview", json=terms
    )
    assert preview_response.status_code == 200
    confirm["preview_token"] = preview_response.json()["preview_token"]
    # Force the single asset INSERT to fail after the transaction starts, then
    # verify no link or source mutation survives the rolled-back transaction.
    async with admin_session_maker() as admin:
        await admin.execute(
            text("""
            CREATE FUNCTION jai_test_fixed_asset_reject() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic rollback check'; END $$
        """)
        )
        await admin.execute(
            text("""
            CREATE TRIGGER jai_test_fixed_asset_reject BEFORE INSERT ON fixed_asset
            FOR EACH ROW EXECUTE FUNCTION jai_test_fixed_asset_reject()
        """)
        )
        await admin.commit()
    with pytest.raises(Exception, match="synthetic rollback check"):
        await db_client.post(
            f"/api/v1/fixed-assets/expenses/{original['id']}/convert", json=confirm
        )
    async with admin_session_maker() as admin:
        await admin.execute(text("DROP TRIGGER jai_test_fixed_asset_reject ON fixed_asset"))
        await admin.execute(text("DROP FUNCTION jai_test_fixed_asset_reject()"))
        await admin.commit()
    assert (await db_client.get("/api/v1/fixed-assets")).json() == []
    rolled_back_source = (await db_client.get(f"/api/v1/expenses/{original['id']}")).json()
    assert rolled_back_source["include_in_pl"] is True

    converted_responses = await asyncio.gather(
        *[
            db_client.post(f"/api/v1/fixed-assets/expenses/{original['id']}/convert", json=confirm)
            for _ in range(2)
        ]
    )
    converted, concurrent_retry = converted_responses
    assert converted.status_code == 201, converted.text
    assert concurrent_retry.status_code == 201, concurrent_retry.text
    assert concurrent_retry.json()["id"] == converted.json()["id"]
    current_cost = await db_client.get(f"/api/v1/expenses/{original['id']}/cost-preview")
    assert current_cost.status_code == 200
    assert current_cost.json()["included_in_pl"] is True
    assert Decimal(current_cost.json()["annual_cost"]) == Decimal("2000.00")
    assert current_cost.json()["linked_fixed_asset_id"] == converted.json()["id"]
    assert Decimal(current_cost.json()["effective_annual_cost"]) == Decimal("77.42")
    duplicate_preview = await db_client.post(
        f"/api/v1/fixed-assets/expenses/{original['id']}/conversion-preview", json=terms
    )
    assert duplicate_preview.status_code == 409
    assert Decimal(converted.json()["acquisition_cost"]) == Decimal("10000.00")
    assert Decimal(
        converted.json()["yearly_deltas"][0]["planned_fixed_asset_depreciation"]
    ) == sum(
        (
            Decimal(row["business_amount"])
            for row in preview_response.json()["monthly_schedule"]
            if row["period_end"].startswith("2026-")
        ),
        Decimal("0"),
    )

    # Identical retry is idempotent and preserves the single source purchase.
    repeated = await db_client.post(
        f"/api/v1/fixed-assets/expenses/{original['id']}/convert", json=confirm
    )
    assert repeated.status_code == 201
    assert repeated.json()["id"] == converted.json()["id"]
    unfiltered_register = await db_client.get("/api/v1/fixed-assets")
    filtered_register = await db_client.get("/api/v1/fixed-assets?year=2026")
    unfiltered_detail = await db_client.get(f"/api/v1/fixed-assets/{converted.json()['id']}")
    filtered_detail = await db_client.get(
        f"/api/v1/fixed-assets/{converted.json()['id']}?year=2026"
    )
    for projection in (
        converted.json(), repeated.json(), unfiltered_register.json()[0],
        filtered_register.json()[0], unfiltered_detail.json(), filtered_detail.json(),
    ):
        assert projection["yearly_deltas"][0]["year"] == 2026
        assert Decimal(projection["yearly_deltas"][0]["legacy_expense_amount"]) == 0
        assert Decimal(projection["yearly_deltas"][0]["fixed_asset_depreciation"]) == Decimal(
            "77.42"
        )
        assert Decimal(projection["yearly_deltas"][0]["difference"]) == Decimal("77.42")
    saved = (await db_client.get(f"/api/v1/expenses/{original['id']}")).json()
    assert saved["vat_amount"] == original["vat_amount"]
    assert saved["include_in_pl"] == original["include_in_pl"]
    battery_request = {
        "acquisition_expense_id": original["id"],
        "in_service_date": "2026-01-16",
        "owner_confirms_ordinary_vat_full_deduction": True,
    }
    battery_preview = await db_client.post("/api/v1/battery-assets/preview", json=battery_request)
    assert battery_preview.status_code == 200, battery_preview.text
    assert battery_preview.json()["eligible"] is False
    assert battery_preview.json()["conflict_code"] == "BATTERY_FIXED_ASSET_LINKED"
    battery_confirm = await db_client.post(
        "/api/v1/battery-assets",
        json={**battery_request, "acknowledge_affected_periods": True},
    )
    assert battery_confirm.status_code == 409, battery_confirm.text
    metadata_purchase = await db_client.post(
        "/api/v1/expenses",
        json={
            "expense_date": "2026-04-01",
            "category_id": categories.json()["id"],
            "vat_treatment_id": treatment["id"],
            "vat_rate_id": rate["id"],
            "net_amount": "10000.00",
            "vat_amount": "2100.00",
            "depreciation_years": 5,
            "include_in_pl": False,
            "pl_cost_basis": "AUTO",
        },
    )
    assert metadata_purchase.status_code == 201
    metadata_update = await db_client.put(
        f"/api/v1/expenses/{metadata_purchase.json()['id']}",
        json={
            "expense_date": "2026-04-01",
            "category_id": categories.json()["id"],
            "vat_treatment_id": treatment["id"],
            "vat_rate_id": rate["id"],
            "net_amount": "10000.00",
            "vat_amount": "2100.00",
            "business_percentage": "100",
            "depreciation_years": 5,
            "note": "Updated metadata with legacy omitted fields",
        },
    )
    assert metadata_update.status_code == 200, metadata_update.text
    assert metadata_update.json()["include_in_pl"] is False
    assert metadata_update.json()["pl_cost_basis"] == "AUTO"
    saved_attachments = await db_client.get(f"/api/v1/expenses/{original['id']}/attachments")
    assert saved_attachments.status_code == 200
    assert saved_attachments.json()["items"][0]["id"] == attachment.json()["id"]
    receipt = await db_client.get(f"/api/v1/attachments/{attachment.json()['id']}/content")
    assert receipt.status_code == 200
    assert receipt.content == b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
    protected_attachment = await db_client.delete(
        f"/api/v1/attachments/{attachment.json()['id']}"
    )
    assert protected_attachment.status_code == 409
    receipt_after_delete_attempt = await db_client.get(
        f"/api/v1/attachments/{attachment.json()['id']}/content"
    )
    assert receipt_after_delete_attempt.status_code == 200
    assert receipt_after_delete_attempt.content == receipt.content
    vat_after = await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=1")
    assert vat_after.status_code == 200
    assert vat_after.json()["boxes"]["box_5b"] == vat_before.json()["boxes"]["box_5b"]

    blocked_update = await db_client.put(
        f"/api/v1/expenses/{original['id']}",
        json={
            "expense_date": "2026-01-01",
            "category_id": categories.json()["id"],
            "vat_treatment_id": treatment["id"],
            "vat_rate_id": rate["id"],
            "net_amount": "11000.00",
            "vat_amount": "2310.00",
        },
    )
    assert blocked_update.status_code == 409
    blocked_delete = await db_client.delete(f"/api/v1/expenses/{original['id']}")
    assert blocked_delete.status_code == 409

    terminal_projection = await db_client.get("/api/v1/fixed-assets?year=2031")
    assert terminal_projection.status_code == 200, terminal_projection.text
    terminal_year = terminal_projection.json()[0]["yearly_deltas"][0]
    assert terminal_year["year"] == 2031
    assert Decimal(terminal_year["planned_closing_book_value"]) == Decimal("1000.00")
    assert Decimal(terminal_year["opening_book_value"]) - Decimal(
        terminal_year["closing_book_value"]
    ) == Decimal(terminal_year["realized_asset_depreciation"])
    assert Decimal(terminal_year["planned_opening_book_value"]) - Decimal(
        terminal_year["planned_closing_book_value"]
    ) == Decimal(terminal_year["asset_depreciation"])
    assert Decimal(terminal_year["business_use_reduction"]) == 0
    assert Decimal(terminal_year["planned_business_use_reduction"]) == 0

    old_pl_after = await db_client.get(
        "/api/v1/reports/profit-loss?from=2026-01-01&to=2026-01-30&granularity=month"
    )
    assert old_pl_after.status_code == 200
    assert Decimal(old_pl_after.json()["expense_actual"]) == 0
    january_cutoff = await db_client.get(
        "/api/v1/reports/profit-loss?from=2026-01-01&to=2026-01-31&granularity=month"
    )
    february_cutoff = await db_client.get(
        "/api/v1/reports/profit-loss?from=2026-01-01&to=2026-02-28&granularity=month"
    )
    assert january_cutoff.status_code == february_cutoff.status_code == 200
    assert Decimal(january_cutoff.json()["expense_actual"]) == Decimal("77.42")
    assert Decimal(february_cutoff.json()["expense_actual"]) == Decimal("77.42")
    report = await db_client.get(
        "/api/v1/reports/profit-loss?from=2026-01-01&to=2026-12-31&granularity=month"
    )
    assert report.status_code == 200, report.text
    body = report.json()
    current_register = await db_client.get("/api/v1/fixed-assets?year=2026")
    assert current_register.status_code == 200
    assert Decimal(body["fixed_asset_depreciation"]) == Decimal(
        current_register.json()[0]["yearly_deltas"][0]["fixed_asset_depreciation"]
    )
    assert body["expense_actual"] == body["fixed_asset_depreciation"]
    january = next(row for row in body["series"] if row["period"].startswith("2026-01"))
    february = next(row for row in body["series"] if row["period"].startswith("2026-02"))
    assert Decimal(january["fixed_asset_depreciation"]) > 0
    assert Decimal(february["fixed_asset_depreciation"]) == 0
    dashboard = await db_client.get("/api/v1/reports/dashboard?year=2026")
    assert dashboard.status_code == 200
    assert Decimal(dashboard.json()["kpi"]["ytd_fixed_asset_depreciation"]) == Decimal(
        body["fixed_asset_depreciation"]
    )
    dashboard_months = {row["month"][:7]: row for row in dashboard.json()["monthly"]}
    assert dashboard_months["2026-01"]["fixed_asset_depreciation"] == january[
        "fixed_asset_depreciation"
    ]
    assert Decimal(dashboard_months["2026-02"]["fixed_asset_depreciation"]) == 0

    # Historical three-place purchase amounts stay saved as entered, while a
    # newly registered asset locks its reportable basis to EUR cents.
    legacy_precision_purchase = await db_client.post(
        "/api/v1/expenses",
        json={
            "expense_date": "2026-01-01",
            "category_id": categories.json()["id"],
            "vat_treatment_id": treatment["id"],
            "vat_rate_id": rate["id"],
            "net_amount": "100.005",
            "vat_amount": "21.001",
            "depreciation_years": 5,
            "include_in_pl": True,
        },
    )
    assert legacy_precision_purchase.status_code == 201, legacy_precision_purchase.text
    legacy_id = legacy_precision_purchase.json()["id"]
    # Recreate the precision of a row saved before M7.5; current Expense writes
    # correctly round newly entered document amounts to cents.
    async with admin_session_maker() as admin:
        await admin.execute(
            text("""
                UPDATE expense SET
                    net_amount = 100.005, vat_amount = 21.001, gross_amount = 121.006,
                    base_net_amount = 100.005, base_vat_amount = 21.001,
                    base_gross_amount = 121.006
                WHERE id = :expense_id
            """),
            {"expense_id": legacy_id},
        )
        await admin.commit()
    saved_legacy = await db_client.get(f"/api/v1/expenses/{legacy_id}")
    assert saved_legacy.status_code == 200
    assert Decimal(saved_legacy.json()["net_amount"]) == Decimal("100.005")
    assert Decimal(saved_legacy.json()["vat_amount"]) == Decimal("21.001")
    legacy_terms = {
        "asset_name": "Three-place historical purchase",
        "in_service_date": "2026-01-16",
        "useful_life_years": 5,
        "residual_value": "0.00",
        "business_percentage": "100",
        "use_type": "FULL_BUSINESS",
    }
    legacy_preview = await db_client.post(
        f"/api/v1/fixed-assets/expenses/{legacy_id}/conversion-preview", json=legacy_terms
    )
    assert legacy_preview.status_code == 200, legacy_preview.text
    assert Decimal(legacy_preview.json()["acquisition_cost"]) == Decimal("100.01")
    legacy_conversion = await db_client.post(
        f"/api/v1/fixed-assets/expenses/{legacy_id}/convert",
        json={**legacy_terms, "preview_token": legacy_preview.json()["preview_token"]},
    )
    assert legacy_conversion.status_code == 201, legacy_conversion.text
    legacy_asset = legacy_conversion.json()
    assert Decimal(legacy_asset["acquisition_cost"]) == Decimal("100.01")
    legacy_register = await db_client.get("/api/v1/fixed-assets?year=2026")
    assert legacy_register.status_code == 200
    legacy_register_asset = next(
        row for row in legacy_register.json() if row["id"] == legacy_asset["id"]
    )
    assert Decimal(legacy_register_asset["acquisition_cost"]) == Decimal("100.01")
    first_year = legacy_register_asset["yearly_deltas"][0]
    schedule = fixed_asset_preview_service.build_monthly_fixed_asset_schedule(
        Decimal(legacy_asset["acquisition_cost"]),
        Decimal(legacy_asset["residual_value"]),
        legacy_asset["useful_life_years"],
        date.fromisoformat(legacy_asset["in_service_date"]),
        Decimal(legacy_asset["business_percentage"]),
    )
    legacy_pl = await db_client.get(
        "/api/v1/reports/profit-loss?from=2026-01-01&to=2026-12-31&granularity=month"
    )
    assert legacy_pl.status_code == 200, legacy_pl.text
    register_total = sum(
        (Decimal(row["yearly_deltas"][0]["fixed_asset_depreciation"])
         for row in legacy_register.json()),
        Decimal("0"),
    )
    assert Decimal(legacy_pl.json()["fixed_asset_depreciation"]) == register_total
    assert Decimal(legacy_pl.json()["expense_actual"]) == register_total
    legacy_dashboard = await db_client.get("/api/v1/reports/dashboard?year=2026")
    assert legacy_dashboard.status_code == 200, legacy_dashboard.text
    assert Decimal(
        legacy_dashboard.json()["kpi"]["ytd_fixed_asset_depreciation"]
    ) == register_total
    assert Decimal(first_year["fixed_asset_depreciation"]) == sum(
        (
            row.business_amount
            for row in schedule
            if row.period_end.year == 2026 and row.period_end <= date(2026, 2, 3)
        ),
        Decimal("0"),
    )
    assert all(
        Decimal(row["fixed_asset_depreciation"]) >= 0
        for row in legacy_pl.json()["series"]
    )
    assert all(row.amount >= 0 and row.closing_book_value >= 0 for row in schedule)
    assert all(row.business_amount == row.amount for row in schedule)
    assert schedule[-1].cumulative_amount == Decimal("100.01")
    assert schedule[-1].closing_book_value == Decimal("0.00")

    prior_purchase = await db_client.post(
        "/api/v1/expenses",
        json={
            "expense_date": "2024-01-01",
            "category_id": categories.json()["id"],
            "vat_treatment_id": treatment["id"],
            "vat_rate_id": rate["id"],
            "net_amount": "10000.00",
            "vat_amount": "2100.00",
            "depreciation_years": 5,
        },
    )
    assert prior_purchase.status_code == 201
    old_terms = {**terms, "asset_name": "Historical tester", "in_service_date": "2024-01-01"}
    old_preview = await db_client.post(
        f"/api/v1/fixed-assets/expenses/{prior_purchase.json()['id']}/conversion-preview",
        json=old_terms,
    )
    assert old_preview.status_code == 200
    assert old_preview.json()["conversion_blocked"] is True
    blocked = await db_client.post(
        f"/api/v1/fixed-assets/expenses/{prior_purchase.json()['id']}/convert",
        json={
            **old_terms,
            "preview_token": old_preview.json()["preview_token"],
        },
    )
    assert blocked.status_code == 409

    pending_purchase = await db_client.post(
        "/api/v1/expenses",
        json={
            "expense_date": "2026-06-01",
            "category_id": categories.json()["id"],
            "vat_treatment_id": treatment["id"],
            "vat_rate_id": rate["id"],
            "net_amount": "700.00",
            "vat_amount": "147.00",
            "depreciation_years": 5,
        },
    )
    assert pending_purchase.status_code == 201
    pending_terms = {
        **terms,
        "asset_name": "Pending storage unit",
        "in_service_date": None,
        "residual_value": "0.00",
    }
    pending_preview = await db_client.post(
        f"/api/v1/fixed-assets/expenses/{pending_purchase.json()['id']}/conversion-preview",
        json=pending_terms,
    )
    assert pending_preview.status_code == 200, pending_preview.text
    pending_conversion = await db_client.post(
        f"/api/v1/fixed-assets/expenses/{pending_purchase.json()['id']}/convert",
        json={**pending_terms, "preview_token": pending_preview.json()["preview_token"]},
    )
    assert pending_conversion.status_code == 201, pending_conversion.text
    assert (await db_client.get("/api/v1/fixed-assets?year=2025")).json() == []
    pending_register = await db_client.get("/api/v1/fixed-assets?year=2027")
    assert pending_register.status_code == 200
    pending_asset = next(
        row for row in pending_register.json() if row["id"] == pending_conversion.json()["id"]
    )
    assert pending_asset["pending_in_service"] is True
    pending_year = pending_asset["yearly_deltas"][0]
    assert pending_year["year"] == 2027
    assert Decimal(pending_year["fixed_asset_depreciation"]) == 0
    assert Decimal(pending_year["opening_book_value"]) == Decimal("700.00")
    assert Decimal(pending_year["closing_book_value"]) == Decimal("700.00")
    assert Decimal(pending_year["planned_closing_book_value"]) == Decimal("700.00")
    pending_details_before_acquisition = await db_client.get(
        f"/api/v1/fixed-assets/{pending_conversion.json()['id']}?year=2025"
    )
    assert pending_details_before_acquisition.status_code == 404
    invalid_year = await db_client.get("/api/v1/fixed-assets?year=10000")
    assert invalid_year.status_code == 422

    race_purchase = await db_client.post(
        "/api/v1/expenses",
        json={
            "expense_date": "2026-07-01",
            "category_id": categories.json()["id"],
            "vat_treatment_id": treatment["id"],
            "vat_rate_id": rate["id"],
            "net_amount": "800.00",
            "vat_amount": "168.00",
            "depreciation_years": 5,
        },
    )
    assert race_purchase.status_code == 201
    race_attachment = await db_client.post(
        f"/api/v1/expenses/{race_purchase.json()['id']}/attachments",
        files={"file": ("race.png", b"\x89PNG\r\n\x1a\n" + b"R" * 200, "image/png")},
    )
    assert race_attachment.status_code == 201
    race_terms = {
        **terms,
        "asset_name": "Concurrent attachment tester",
        "in_service_date": "2026-07-01",
        "residual_value": "0.00",
    }
    race_preview = await db_client.post(
        f"/api/v1/fixed-assets/expenses/{race_purchase.json()['id']}/conversion-preview",
        json=race_terms,
    )
    assert race_preview.status_code == 200
    race_confirm = {
        **race_terms,
        "preview_token": race_preview.json()["preview_token"],
    }
    race_convert, race_delete = await asyncio.gather(
        db_client.post(
            f"/api/v1/fixed-assets/expenses/{race_purchase.json()['id']}/convert",
            json=race_confirm,
        ),
        db_client.delete(f"/api/v1/attachments/{race_attachment.json()['id']}"),
    )
    assert race_convert.status_code == 201, race_convert.text
    assert race_delete.status_code in {204, 409}, race_delete.text
    race_receipt = await db_client.get(
        f"/api/v1/attachments/{race_attachment.json()['id']}/content"
    )
    if race_delete.status_code == 409:
        assert race_receipt.status_code == 200
        assert race_receipt.content == b"\x89PNG\r\n\x1a\n" + b"R" * 200
    else:
        assert race_receipt.status_code == 404

    # Establish a second tenant and query as the actual application role with
    # its RLS context. The other company's asset remains invisible.
    other_company_id = uuid.uuid4()
    async with admin_session_maker() as admin:
        admin.add(Company(id=other_company_id, name="Other Co", base_currency="EUR"))
        await admin.commit()
    async with runtime_session_maker() as runtime:
        await set_rls_company(runtime, other_company_id)
        hidden_assets = (await runtime.scalars(select(FixedAsset))).all()
        assert hidden_assets == []
        assert await fixed_asset_service.list_fixed_assets(runtime, other_company_id) == []
        with pytest.raises(LookupError, match="Fixed asset not found"):
            await fixed_asset_service.get_fixed_asset(
                runtime, uuid.UUID(converted.json()["id"]), other_company_id
            )

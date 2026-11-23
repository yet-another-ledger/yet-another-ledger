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
async def test_fixed_asset_annual_use_routes_reject_invalid_years_before_service(
    db_client: AsyncClient,
) -> None:
    await _auth(db_client)
    categories = await db_client.post("/api/v1/expense-categories", json={"name": "Equipment"})
    rates = await db_client.get("/api/v1/vat-rates")
    treatments = await db_client.get("/api/v1/vat-treatments?side=PURCHASE")
    rate = next(row for row in rates.json()["items"] if row["label"] == "NL standard (21%)")
    treatment = next(
        row for row in treatments.json()["items"] if row["code"] == "NL_DOMESTIC_PURCH"
    )
    purchase = await db_client.post(
        "/api/v1/expenses",
        json={
            "expense_date": "2026-01-01",
            "category_id": categories.json()["id"],
            "vat_treatment_id": treatment["id"],
            "vat_rate_id": rate["id"],
            "net_amount": "1000.00",
            "vat_amount": "210.00",
            "include_in_pl": True,
            "pl_cost_basis": "AUTO",
        },
    )
    assert purchase.status_code == 201, purchase.text
    terms = {
        "asset_name": "Mixed use test asset",
        "in_service_date": "2026-01-01",
        "useful_life_years": 5,
        "residual_value": "0.00",
        "business_percentage": "50.000",
        "use_type": "MIXED_USE",
    }
    preview = await db_client.post(
        f"/api/v1/fixed-assets/expenses/{purchase.json()['id']}/conversion-preview", json=terms
    )
    assert preview.status_code == 200, preview.text
    created = await db_client.post(
        f"/api/v1/fixed-assets/expenses/{purchase.json()['id']}/convert",
        json={**terms, "preview_token": preview.json()["preview_token"]},
    )
    assert created.status_code == 201, created.text
    asset_id = created.json()["id"]
    valid_body = {"estimated_business_percentage": "55.000"}
    revision_body = {
        "expected_version": 1,
        "business_percentage": "50.000",
        "reason": "Validation request",
        "preview_token": "0" * 64,
    }
    confirm_body = {
        "actual_business_percentage": "50.000",
        "reason": "Validation request",
        "preview_token": "0" * 64,
    }
    for invalid_year in (0, 10000):
        path = f"/api/v1/fixed-assets/{asset_id}/annual-use/{invalid_year}"
        assert (await db_client.get(path)).status_code == 422
        assert (await db_client.put(path, json=valid_body)).status_code == 422
        assert (await db_client.post(f"{path}/confirm", json=confirm_body)).status_code == 422
        confirm_preview = await db_client.post(
            f"{path}/confirm/preview", json={"actual_business_percentage": "50"}
        )
        assert confirm_preview.status_code == 422
        assert (await db_client.get(f"{path}/revisions")).status_code == 422
        revision_preview = await db_client.post(
            f"{path}/revisions/preview", json=revision_body
        )
        assert revision_preview.status_code == 422
        assert (await db_client.post(f"{path}/revisions", json=revision_body)).status_code == 422

    valid = await db_client.put(
        f"/api/v1/fixed-assets/{asset_id}/annual-use/2026", json=valid_body
    )
    assert valid.status_code == 200, valid.text
    assert valid.json()["year"] == 2026
    assert Decimal(valid.json()["estimated_business_percentage"]) == Decimal("55.000")


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
    assert Decimal(converted.json()["yearly_deltas"][0]["planned_fixed_asset_depreciation"]) == sum(
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
        converted.json(),
        repeated.json(),
        unfiltered_register.json()[0],
        filtered_register.json()[0],
        unfiltered_detail.json(),
        filtered_detail.json(),
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
    assert battery_preview.json()["eligible"] is True
    battery_confirm = await db_client.post(
        "/api/v1/battery-assets",
        json={**battery_request, "acknowledge_affected_periods": True},
    )
    assert battery_confirm.status_code == 201, battery_confirm.text
    linked_asset = await db_client.get(f"/api/v1/fixed-assets/{converted.json()['id']}")
    assert linked_asset.json()["battery_asset_id"] == battery_confirm.json()["id"]
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
    protected_attachment = await db_client.delete(f"/api/v1/attachments/{attachment.json()['id']}")
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
    assert (
        dashboard_months["2026-01"]["fixed_asset_depreciation"]
        == january["fixed_asset_depreciation"]
    )
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
        (
            Decimal(row["yearly_deltas"][0]["fixed_asset_depreciation"])
            for row in legacy_register.json()
        ),
        Decimal("0"),
    )
    assert Decimal(legacy_pl.json()["fixed_asset_depreciation"]) == register_total
    assert Decimal(legacy_pl.json()["expense_actual"]) == register_total
    legacy_dashboard = await db_client.get("/api/v1/reports/dashboard?year=2026")
    assert legacy_dashboard.status_code == 200, legacy_dashboard.text
    assert Decimal(legacy_dashboard.json()["kpi"]["ytd_fixed_asset_depreciation"]) == register_total
    assert Decimal(first_year["fixed_asset_depreciation"]) == sum(
        (
            row.business_amount
            for row in schedule
            if row.period_end.year == 2026 and row.period_end <= date(2026, 2, 3)
        ),
        Decimal("0"),
    )
    assert all(Decimal(row["fixed_asset_depreciation"]) >= 0 for row in legacy_pl.json()["series"])
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


@pytest.mark.integration
async def test_mixed_use_confirmation_replaces_quarters_and_audits_correction(
    db_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FrozenDate(date):
        current = date(2026, 11, 23)

        @classmethod
        def today(cls) -> date:
            return cls(cls.current.year, cls.current.month, cls.current.day)

    monkeypatch.setattr(fixed_asset_preview_service, "date", FrozenDate)
    monkeypatch.setattr(fixed_asset_service, "date", FrozenDate)
    monkeypatch.setattr(pl_service, "date", FrozenDate)
    await _auth(db_client, email="mixed-asset-owner@example.com")
    categories = await db_client.post("/api/v1/expense-categories", json={"name": "Equipment"})
    rates = await db_client.get("/api/v1/vat-rates")
    treatments = await db_client.get("/api/v1/vat-treatments?side=PURCHASE")
    rate = next(row for row in rates.json()["items"] if row["label"] == "NL standard (21%)")
    treatment = next(
        row for row in treatments.json()["items"] if row["code"] == "NL_DOMESTIC_PURCH"
    )
    purchase = await db_client.post(
        "/api/v1/expenses",
        json={
            "expense_date": "2026-01-01",
            "category_id": categories.json()["id"],
            "vat_treatment_id": treatment["id"],
            "vat_rate_id": rate["id"],
            "net_amount": "1000.00",
            "vat_amount": "210.00",
            "depreciation_years": 5,
            "include_in_pl": True,
            "pl_cost_basis": "AUTO",
        },
    )
    assert purchase.status_code == 201, purchase.text
    terms = {
        "asset_name": "Mixed-use equipment",
        "in_service_date": "2026-01-01",
        "useful_life_years": 5,
        "residual_value": "0",
        "business_percentage": "50",
        "use_type": "MIXED_USE",
    }
    preview = await db_client.post(
        f"/api/v1/fixed-assets/expenses/{purchase.json()['id']}/conversion-preview", json=terms
    )
    assert preview.status_code == 200, preview.text
    created = await db_client.post(
        f"/api/v1/fixed-assets/expenses/{purchase.json()['id']}/convert",
        json={**terms, "preview_token": preview.json()["preview_token"]},
    )
    assert created.status_code == 201, created.text
    asset_id = created.json()["id"]
    assert Decimal(created.json()["yearly_deltas"][0]["business_use_reduction"]) == Decimal(
        "83.33"
    )
    assert Decimal(
        created.json()["yearly_deltas"][0]["planned_business_use_reduction"]
    ) == Decimal("100.00")
    q1_url = "/api/v1/reports/profit-loss?from=2026-01-01&to=2026-03-31&granularity=quarter"
    estimated = await db_client.get(q1_url)
    assert estimated.status_code == 200
    assert estimated.json()["fixed_asset_depreciation_provisional"] is True
    old_q1 = Decimal(estimated.json()["fixed_asset_depreciation"])
    confirm_preview = await db_client.post(
        f"/api/v1/fixed-assets/{asset_id}/annual-use/2026/confirm/preview",
        json={"actual_business_percentage": "80"},
    )
    assert confirm_preview.status_code == 200, confirm_preview.text
    assert confirm_preview.json()["period_deltas"]
    changed_estimate = await db_client.put(
        f"/api/v1/fixed-assets/{asset_id}/annual-use/2026",
        json={"estimated_business_percentage": "70"},
    )
    assert changed_estimate.status_code == 200, changed_estimate.text
    stale_confirm = await db_client.post(
        f"/api/v1/fixed-assets/{asset_id}/annual-use/2026/confirm",
        json={
            "actual_business_percentage": "80",
            "preview_token": confirm_preview.json()["preview_token"],
            "reason": "Confirmed from the annual business-use records",
        },
    )
    assert stale_confirm.status_code == 409, stale_confirm.text
    confirm_preview = await db_client.post(
        f"/api/v1/fixed-assets/{asset_id}/annual-use/2026/confirm/preview",
        json={"actual_business_percentage": "80"},
    )
    assert confirm_preview.status_code == 200, confirm_preview.text
    confirmed = await db_client.post(
        f"/api/v1/fixed-assets/{asset_id}/annual-use/2026/confirm",
        json={
            "actual_business_percentage": "80",
            "preview_token": confirm_preview.json()["preview_token"],
            "reason": "Confirmed from the annual business-use records",
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["provisional"] is False
    inherited = await db_client.get(f"/api/v1/fixed-assets/{asset_id}/annual-use/2027")
    assert inherited.status_code == 200, inherited.text
    assert Decimal(inherited.json()["effective_business_percentage"]) == Decimal("80")
    assert inherited.json()["provisional"] is True
    estimated_2027 = await db_client.put(
        f"/api/v1/fixed-assets/{asset_id}/annual-use/2027",
        json={"estimated_business_percentage": "30"},
    )
    assert estimated_2027.status_code == 200, estimated_2027.text
    inherited_2028 = await db_client.get(f"/api/v1/fixed-assets/{asset_id}/annual-use/2028")
    assert inherited_2028.status_code == 200, inherited_2028.text
    assert Decimal(inherited_2028.json()["effective_business_percentage"]) == Decimal("30")
    assert inherited_2028.json()["provisional"] is True
    FrozenDate.current = date(2028, 11, 23)
    direct_preview = await db_client.post(
        f"/api/v1/fixed-assets/{asset_id}/annual-use/2028/confirm/preview",
        json={"actual_business_percentage": "60"},
    )
    assert direct_preview.status_code == 200, direct_preview.text
    assert Decimal(direct_preview.json()["before_business_percentage"]) == Decimal("30")
    direct_confirm = await db_client.post(
        f"/api/v1/fixed-assets/{asset_id}/annual-use/2028/confirm",
        json={
            "actual_business_percentage": "60",
            "preview_token": direct_preview.json()["preview_token"],
            "reason": "Confirmed from the 2028 business-use records",
        },
    )
    assert direct_confirm.status_code == 200, direct_confirm.text
    assert Decimal(direct_confirm.json()["actual_business_percentage"]) == Decimal("60")
    direct_history = await db_client.get(
        f"/api/v1/fixed-assets/{asset_id}/annual-use/2028/revisions"
    )
    assert direct_history.status_code == 200
    assert direct_history.json()[0]["reason"] == "Confirmed from the 2028 business-use records"
    FrozenDate.current = date(2026, 11, 23)
    actual = await db_client.get(q1_url)
    assert actual.json()["fixed_asset_depreciation_provisional"] is False
    assert Decimal(actual.json()["fixed_asset_depreciation"]) > old_q1
    revision_input = {
        "expected_version": confirmed.json()["version"],
        "business_percentage": "60",
        "reason": "Corrected from the measured business-use log",
        "preview_token": "0" * 64,
    }
    revision_preview = await db_client.post(
        f"/api/v1/fixed-assets/{asset_id}/annual-use/2026/revisions/preview",
        json=revision_input,
    )
    assert revision_preview.status_code == 200, revision_preview.text
    FrozenDate.current = date(2026, 11, 24)
    cutoff_stale_revision = await db_client.post(
        f"/api/v1/fixed-assets/{asset_id}/annual-use/2026/revisions",
        json={**revision_input, "preview_token": revision_preview.json()["preview_token"]},
    )
    assert cutoff_stale_revision.status_code == 409, cutoff_stale_revision.text
    revision_preview = await db_client.post(
        f"/api/v1/fixed-assets/{asset_id}/annual-use/2026/revisions/preview",
        json=revision_input,
    )
    assert revision_preview.status_code == 200, revision_preview.text
    assert Decimal(revision_preview.json()["pnl_delta"]) < 0
    changed_future_estimate = await db_client.put(
        f"/api/v1/fixed-assets/{asset_id}/annual-use/2027",
        json={"estimated_business_percentage": "20"},
    )
    assert changed_future_estimate.status_code == 200, changed_future_estimate.text
    stale_revision = await db_client.post(
        f"/api/v1/fixed-assets/{asset_id}/annual-use/2026/revisions",
        json={**revision_input, "preview_token": revision_preview.json()["preview_token"]},
    )
    assert stale_revision.status_code == 409, stale_revision.text
    revision_preview = await db_client.post(
        f"/api/v1/fixed-assets/{asset_id}/annual-use/2026/revisions/preview",
        json=revision_input,
    )
    assert revision_preview.status_code == 200, revision_preview.text
    revised = await db_client.post(
        f"/api/v1/fixed-assets/{asset_id}/annual-use/2026/revisions",
        json={**revision_input, "preview_token": revision_preview.json()["preview_token"]},
    )
    assert revised.status_code == 200, revised.text
    history = await db_client.get(f"/api/v1/fixed-assets/{asset_id}/annual-use/2026/revisions")
    assert history.status_code == 200
    assert history.json()[0]["reason"] == "Confirmed from the annual business-use records"
    assert history.json()[0]["actor_id"] is not None
    assert history.json()[0]["after_snapshot"]["affected_periods"]
    assert history.json()[1]["reason"] == revision_input["reason"]
    assert history.json()[0]["actor_id"]
    after = await db_client.get(q1_url)
    assert Decimal(after.json()["fixed_asset_depreciation"]) < Decimal(
        actual.json()["fixed_asset_depreciation"]
    )

    pending_purchase = await db_client.post(
        "/api/v1/expenses",
        json={
            "expense_date": "2026-02-01",
            "category_id": categories.json()["id"],
            "vat_treatment_id": treatment["id"],
            "vat_rate_id": rate["id"],
            "net_amount": "500.00",
            "vat_amount": "105.00",
            "depreciation_years": 1,
            "include_in_pl": True,
            "pl_cost_basis": "AUTO",
        },
    )
    assert pending_purchase.status_code == 201, pending_purchase.text
    pending_terms = {
        "asset_name": "Pending equipment",
        "in_service_date": None,
        "useful_life_years": 5,
        "residual_value": "0",
        "business_percentage": "100",
        "use_type": "FULL_BUSINESS",
    }
    pending_preview = await db_client.post(
        f"/api/v1/fixed-assets/expenses/{pending_purchase.json()['id']}/conversion-preview",
        json=pending_terms,
    )
    assert pending_preview.status_code == 200, pending_preview.text
    pending_created = await db_client.post(
        f"/api/v1/fixed-assets/expenses/{pending_purchase.json()['id']}/convert",
        json={**pending_terms, "preview_token": pending_preview.json()["preview_token"]},
    )
    assert pending_created.status_code == 201, pending_created.text
    pending_id = pending_created.json()["id"]
    assert pending_created.json()["pending_in_service"] is True
    FrozenDate.current = date(2026, 11, 29)
    service_correction = {
        "expected_version": pending_created.json()["version"],
        "in_service_date": "2026-11-01",
        "reason": "Verified installation date from the commissioning record",
        "preview_token": "0" * 64,
    }
    date_preview = await db_client.post(
        f"/api/v1/fixed-assets/{pending_id}/service-date/revisions/preview",
        json=service_correction,
    )
    assert date_preview.status_code == 200, date_preview.text
    assert Decimal(date_preview.json()["after_pnl_effect"]) == 0
    FrozenDate.current = date(2026, 11, 30)
    stale_activation = await db_client.post(
        f"/api/v1/fixed-assets/{pending_id}/service-date/revisions",
        json={**service_correction, "preview_token": date_preview.json()["preview_token"]},
    )
    assert stale_activation.status_code == 409, stale_activation.text
    pending_after_stale = await db_client.get(f"/api/v1/fixed-assets/{pending_id}")
    assert pending_after_stale.json()["in_service_date"] is None
    date_preview = await db_client.post(
        f"/api/v1/fixed-assets/{pending_id}/service-date/revisions/preview",
        json=service_correction,
    )
    assert date_preview.status_code == 200, date_preview.text
    assert Decimal(date_preview.json()["after_pnl_effect"]) > 0
    activated = await db_client.post(
        f"/api/v1/fixed-assets/{pending_id}/service-date/revisions",
        json={**service_correction, "preview_token": date_preview.json()["preview_token"]},
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["in_service_date"] == "2026-11-01"
    activation_history = await db_client.get(f"/api/v1/fixed-assets/{pending_id}/revisions")
    assert activation_history.status_code == 200
    assert activation_history.json()[0]["reason"] == service_correction["reason"]


@pytest.mark.integration
async def test_battery_first_fixed_asset_conversion_and_shared_audited_date_revision(
    db_client: AsyncClient,
) -> None:
    await _auth(db_client, email="battery-first-fixed-asset@example.com")
    categories = await db_client.post("/api/v1/expense-categories", json={"name": "Equipment"})
    rates = await db_client.get("/api/v1/vat-rates")
    treatments = await db_client.get("/api/v1/vat-treatments?side=PURCHASE")
    rate = next(row for row in rates.json()["items"] if row["label"] == "NL standard (21%)")
    treatment = next(
        row for row in treatments.json()["items"] if row["code"] == "NL_DOMESTIC_PURCH"
    )
    purchase = await db_client.post(
        "/api/v1/expenses",
        json={
            "expense_date": "2026-01-01",
            "category_id": categories.json()["id"],
            "vat_treatment_id": treatment["id"],
            "vat_rate_id": rate["id"],
            "net_amount": "1000.00",
            "vat_amount": "210.00",
            "depreciation_years": 5,
            "include_in_pl": True,
            "pl_cost_basis": "AUTO",
        },
    )
    assert purchase.status_code == 201, purchase.text
    battery_body = {
        "acquisition_expense_id": purchase.json()["id"],
        "in_service_date": "2026-01-01",
        "owner_confirms_ordinary_vat_full_deduction": True,
        "acknowledge_affected_periods": True,
    }
    registered_battery = await db_client.post("/api/v1/battery-assets", json=battery_body)
    assert registered_battery.status_code == 201, registered_battery.text
    assert (await db_client.get(f"/api/v1/expenses/{purchase.json()['id']}")).json()[
        "include_in_pl"
    ] is False
    terms = {
        "asset_name": "Battery fixed asset",
        "in_service_date": None,
        "useful_life_years": 5,
        "residual_value": "0",
        "business_percentage": "50",
        "use_type": "MIXED_USE",
    }
    preview = await db_client.post(
        f"/api/v1/fixed-assets/expenses/{purchase.json()['id']}/conversion-preview",
        json=terms,
    )
    assert preview.status_code == 200, preview.text
    created = await db_client.post(
        f"/api/v1/fixed-assets/expenses/{purchase.json()['id']}/convert",
        json={**terms, "preview_token": preview.json()["preview_token"]},
    )
    assert created.status_code == 201, created.text
    asset_id = created.json()["id"]
    assert created.json()["in_service_date"] == "2026-01-01"
    assert created.json()["battery_asset_id"] == registered_battery.json()["id"]
    converted_purchase = await db_client.get(f"/api/v1/expenses/{purchase.json()['id']}")
    assert converted_purchase.json()["include_in_pl"] is False

    battery_id = registered_battery.json()["id"]
    zero_use = await db_client.put(
        f"/api/v1/fixed-assets/{asset_id}/annual-use/2026",
        json={"estimated_business_percentage": "0"},
    )
    assert zero_use.status_code == 200, zero_use.text
    correction = {
        "expected_version": registered_battery.json()["version"],
        "in_service_date": "2026-02-01",
        "net_basis": "1200.00",
        "purchase_vat": "252.00",
        "reason": "Corrected to the signed commissioning date",
        "expected_annual_versions": {},
        "acknowledge_affected_periods": [],
    }
    correction["expected_annual_versions"] = {"2026": zero_use.json()["version"]}
    battery_preview = await db_client.post(
        f"/api/v1/battery-assets/{battery_id}/revisions/preview", json=correction
    )
    assert battery_preview.status_code == 200, battery_preview.text
    assert battery_preview.json()["fixed_asset_pnl_previews"] == []
    correction["acknowledge_affected_periods"] = battery_preview.json()["affected_periods"]
    battery_preview = await db_client.post(
        f"/api/v1/battery-assets/{battery_id}/revisions/preview", json=correction
    )
    assert battery_preview.status_code == 200, battery_preview.text
    correction["expected_fixed_asset_snapshot"] = battery_preview.json()["fixed_asset_snapshot"]
    correction.update({"net_basis": "1300.00", "purchase_vat": "273.00"})
    edited_preview = await db_client.post(
        f"/api/v1/battery-assets/{battery_id}/revisions", json=correction
    )
    assert edited_preview.status_code == 409, edited_preview.text
    unchanged_purchase = await db_client.get(f"/api/v1/expenses/{purchase.json()['id']}")
    assert Decimal(unchanged_purchase.json()["base_net_amount"]) == Decimal("1000.000")
    assert Decimal(unchanged_purchase.json()["base_vat_amount"]) == Decimal("210.000")
    unchanged_asset = await db_client.get(f"/api/v1/battery-assets/{battery_id}")
    assert Decimal(unchanged_asset.json()["net_basis"]) == Decimal("1000.000")
    correction.update({"net_basis": "1200.00", "purchase_vat": "252.00"})
    battery_preview = await db_client.post(
        f"/api/v1/battery-assets/{battery_id}/revisions/preview", json=correction
    )
    assert battery_preview.status_code == 200, battery_preview.text
    correction["expected_fixed_asset_snapshot"] = battery_preview.json()["fixed_asset_snapshot"]
    updated_estimate = await db_client.put(
        f"/api/v1/fixed-assets/{asset_id}/annual-use/2026",
        json={"estimated_business_percentage": "80"},
    )
    assert updated_estimate.status_code == 200, updated_estimate.text
    correction["expected_annual_versions"] = {"2026": updated_estimate.json()["version"]}
    stale = await db_client.post(f"/api/v1/battery-assets/{battery_id}/revisions", json=correction)
    assert stale.status_code == 409, stale.text
    battery_preview = await db_client.post(
        f"/api/v1/battery-assets/{battery_id}/revisions/preview", json=correction
    )
    assert battery_preview.status_code == 200, battery_preview.text
    correction["expected_fixed_asset_snapshot"] = battery_preview.json()["fixed_asset_snapshot"]
    correction["expected_annual_versions"] = {
        str(item["year"]): item["current_version"]
        for item in battery_preview.json()["annual_previews"]
        if item["current_version"] is not None
    }
    revised_battery = await db_client.post(
        f"/api/v1/battery-assets/{battery_id}/revisions", json=correction
    )
    assert revised_battery.status_code == 200, revised_battery.text
    assert Decimal(revised_battery.json()["net_basis"]) == Decimal("1200.000")
    revised_purchase = await db_client.get(f"/api/v1/expenses/{purchase.json()['id']}")
    assert revised_purchase.status_code == 200
    assert Decimal(revised_purchase.json()["base_net_amount"]) == Decimal("1200.000")
    assert Decimal(revised_purchase.json()["base_vat_amount"]) == Decimal("252.000")
    fixed_after = await db_client.get(f"/api/v1/fixed-assets/{asset_id}")
    assert fixed_after.json()["in_service_date"] == "2026-02-01"
    fixed_history = await db_client.get(f"/api/v1/fixed-assets/{asset_id}/revisions")
    assert fixed_history.status_code == 200
    assert fixed_history.json()[0]["reason"] == correction["reason"]
    fixed_audit = fixed_history.json()[0]
    assert fixed_audit["affected_periods"] == [
        item["period"] for item in fixed_audit["after_snapshot"]["period_deltas"]
    ]
    assert "2026-Q1" in fixed_audit["affected_periods"]
    assert "2026-Q2" in fixed_audit["affected_periods"]
    assert "2026-Q3" in fixed_audit["affected_periods"]
    assert "2026-Q4" in fixed_audit["affected_periods"]
    pnl_deltas = {
        item["period"]: Decimal(item["pnl_delta"])
        for item in fixed_audit["after_snapshot"]["period_deltas"]
    }
    assert pnl_deltas["2026-Q2"] != 0
    assert pnl_deltas["2026-Q3"] != 0
    battery_history = await db_client.get(f"/api/v1/battery-assets/{battery_id}/revisions")
    assert battery_history.status_code == 200
    battery_audit = battery_history.json()["items"][0]
    assert battery_audit["affected_periods"] != fixed_audit["affected_periods"]
    assert battery_audit["after_snapshot"]["pnl_affected_periods"] == fixed_audit[
        "affected_periods"
    ]


@pytest.mark.integration
async def test_shared_basis_revision_reconciles_every_changed_month(
    db_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FrozenDate(date):
        @classmethod
        def today(cls) -> date:
            return date(2026, 11, 23)

    from jai.services import battery as battery_service

    for module in (battery_service, fixed_asset_preview_service, fixed_asset_service, pl_service):
        monkeypatch.setattr(module, "date", FrozenDate)
    await _auth(db_client, email="monthly-revision@example.com")
    category = await db_client.post("/api/v1/expense-categories", json={"name": "Battery"})
    rates = await db_client.get("/api/v1/vat-rates")
    treatments = await db_client.get("/api/v1/vat-treatments?side=PURCHASE")
    rate = next(row for row in rates.json()["items"] if row["label"] == "NL standard (21%)")
    treatment = next(
        row for row in treatments.json()["items"] if row["code"] == "NL_DOMESTIC_PURCH"
    )
    purchase = await db_client.post(
        "/api/v1/expenses",
        json={
            "expense_date": "2026-01-01",
            "category_id": category.json()["id"],
            "vat_treatment_id": treatment["id"],
            "vat_rate_id": rate["id"],
            "net_amount": "1000.00",
            "vat_amount": "210.00",
            "depreciation_years": 5,
            "include_in_pl": True,
            "pl_cost_basis": "AUTO",
        },
    )
    assert purchase.status_code == 201, purchase.text
    battery = await db_client.post(
        "/api/v1/battery-assets",
        json={
            "acquisition_expense_id": purchase.json()["id"],
            "in_service_date": "2026-01-01",
            "owner_confirms_ordinary_vat_full_deduction": True,
            "acknowledge_affected_periods": True,
        },
    )
    assert battery.status_code == 201, battery.text
    terms = {
        "asset_name": "Battery fixed asset",
        "in_service_date": None,
        "useful_life_years": 5,
        "residual_value": "0",
        "business_percentage": "100",
        "use_type": "MIXED_USE",
    }
    root = f"/api/v1/fixed-assets/expenses/{purchase.json()['id']}"
    conversion_preview = await db_client.post(f"{root}/conversion-preview", json=terms)
    assert conversion_preview.status_code == 200, conversion_preview.text
    fixed_asset = await db_client.post(
        f"{root}/convert",
        json={**terms, "preview_token": conversion_preview.json()["preview_token"]},
    )
    assert fixed_asset.status_code == 201, fixed_asset.text
    battery_id = battery.json()["id"]
    def monthly_values(response):
        assert response.status_code == 200, response.text
        return {
            row["period"][:7]: Decimal(row["fixed_asset_depreciation"])
            for row in response.json()["series"]
        }

    before_response = await db_client.get(
        "/api/v1/reports/profit-loss?from=2026-01-01&to=2026-12-31&granularity=month"
    )
    before = monthly_values(before_response)
    assert [before[f"2026-{month:02d}"] for month in range(1, 4)] == [
        Decimal("16.67"), Decimal("16.66"), Decimal("16.67")
    ]
    request = {
        "expected_version": battery.json()["version"],
        "in_service_date": "2026-02-01",
        "net_basis": "1500.00",
        "purchase_vat": "315.00",
        "reason": "Corrected commissioning date and source basis",
        "expected_annual_versions": {},
        "acknowledge_affected_periods": [],
    }
    preview_response = await db_client.post(
        f"/api/v1/battery-assets/{battery_id}/revisions/preview", json=request
    )
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    preview_months = {
        row["period"]: row for row in preview["fixed_asset_pnl_monthly_previews"]
    }
    assert preview_months["2026-01"]["before_depreciation"] == "16.67"
    assert preview_months["2026-01"]["after_depreciation"] == "0.00"
    assert preview_months["2026-02"]["before_depreciation"] == "16.66"
    assert preview_months["2026-02"]["after_depreciation"] == "25.00"
    assert preview_months["2026-03"]["before_depreciation"] == "16.67"
    assert preview_months["2026-03"]["after_depreciation"] == "25.00"
    assert "2026-Q1" not in preview["pnl_affected_periods"]
    assert "2026-Q2" in preview["pnl_affected_periods"]
    assert "2026-Q1" in preview["affected_periods"]
    assert preview["pnl_affected_months"] == sorted(preview_months)
    assert sum(before[f"2026-{month:02d}"] for month in range(1, 4)) == Decimal("50.00")
    assert set(preview["pnl_affected_months"]) == {
        period
        for period, value in before.items()
        if period in preview_months
        and value != Decimal(preview_months[period]["after_depreciation"])
    }
    request["acknowledge_affected_periods"] = preview["affected_periods"]
    request["expected_fixed_asset_snapshot"] = preview["fixed_asset_snapshot"]
    confirmed = await db_client.post(
        f"/api/v1/battery-assets/{battery_id}/revisions", json=request
    )
    assert confirmed.status_code == 200, confirmed.text
    after_response = await db_client.get(
        "/api/v1/reports/profit-loss?from=2026-01-01&to=2026-12-31&granularity=month"
    )
    after = monthly_values(after_response)
    assert [after[f"2026-{month:02d}"] for month in range(1, 4)] == [
        Decimal("0.00"), Decimal("25.00"), Decimal("25.00")
    ]
    assert sum(before[f"2026-{month:02d}"] for month in range(1, 4)) == sum(
        after[f"2026-{month:02d}"] for month in range(1, 4)
    ) == Decimal("50.00")
    changed_months = {
        period for period in before.keys() & after.keys() if before[period] != after[period]
    }
    assert changed_months == set(preview_months)
    preview_deltas = {
        row["period"]: row for row in preview["fixed_asset_pnl_monthly_previews"]
    }
    assert {
        period: {
            "before": Decimal(preview_deltas[period]["before_depreciation"]),
            "after": Decimal(preview_deltas[period]["after_depreciation"]),
        }
        for period in changed_months
    } == {
        period: {"before": before[period], "after": after[period]}
        for period in changed_months
    }
    for endpoint in (
        f"/api/v1/fixed-assets/{fixed_asset.json()['id']}/revisions",
        f"/api/v1/battery-assets/{battery_id}/revisions",
    ):
        history = await db_client.get(endpoint)
        assert history.status_code == 200, history.text
        item = history.json()[0] if isinstance(history.json(), list) else history.json()["items"][0]
        after_snapshot = item["after_snapshot"]
        if endpoint.startswith("/api/v1/fixed-assets/"):
            audit_periods = after_snapshot["affected_months"]
            audit_rows = after_snapshot["monthly_period_deltas"]
        else:
            audit_periods = after_snapshot["pnl_affected_months"]
            audit_rows = after_snapshot["fixed_asset_pnl_monthly_previews"]
        assert set(audit_periods) == changed_months
        audit_deltas = {row["period"]: row for row in audit_rows}
        assert set(audit_deltas) == changed_months
        for period in changed_months:
            assert Decimal(audit_deltas[period]["before_depreciation"]) == before[period]
            assert Decimal(audit_deltas[period]["after_depreciation"]) == after[period]

    annual_input = await db_client.put(
        f"/api/v1/battery-assets/{battery_id}/annual-use/2026",
        json={"private_use_percentage": "50", "basis_note": "Synthetic EMS evidence"},
    )
    assert annual_input.status_code == 200, annual_input.text
    annual_confirmed = await db_client.post(
        f"/api/v1/battery-assets/{battery_id}/annual-use/2026/confirm",
        json={
            "expected_version": annual_input.json()["version"],
            "expected_asset_version": confirmed.json()["version"],
        },
    )
    assert annual_confirmed.status_code == 200, annual_confirmed.text
    vat_return = await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=4")
    assert vat_return.status_code == 200, vat_return.text
    assert Decimal(vat_return.json()["boxes"]["box_1d"]["base"]) == Decimal("150.00")
    assert Decimal(vat_return.json()["boxes"]["box_1d"]["vat"]) == Decimal("31.50")

    current_battery = (await db_client.get(f"/api/v1/battery-assets/{battery_id}")).json()
    current_asset = (await db_client.get(f"/api/v1/fixed-assets/{fixed_asset.json()['id']}")).json()
    current_purchase = (await db_client.get(f"/api/v1/expenses/{purchase.json()['id']}")).json()
    invalid = await db_client.post(
        f"/api/v1/battery-assets/{battery_id}/revisions/preview",
        json={
            "expected_version": current_battery["version"],
            "in_service_date": "9996-03-02",
            "reason": "Out of supported schedule range",
        },
    )
    assert 400 <= invalid.status_code < 500, invalid.text
    revised_fixed = await db_client.get(f"/api/v1/fixed-assets/{fixed_asset.json()['id']}")
    assert revised_fixed.json() == current_asset
    assert (await db_client.get(f"/api/v1/battery-assets/{battery_id}")).json() == current_battery
    revised_purchase = await db_client.get(f"/api/v1/expenses/{purchase.json()['id']}")
    assert revised_purchase.json() == current_purchase


@pytest.mark.integration
async def test_battery_fixed_asset_registration_orders_share_source_reporting(
    db_client: AsyncClient,
) -> None:
    await _auth(db_client, email="registration-order-reporting@example.com")
    rates = await db_client.get("/api/v1/vat-rates")
    treatments = await db_client.get("/api/v1/vat-treatments?side=PURCHASE")
    rate = next(row for row in rates.json()["items"] if row["label"] == "NL standard (21%)")
    treatment = next(
        row for row in treatments.json()["items"] if row["code"] == "NL_DOMESTIC_PURCH"
    )

    async def create_purchase(name: str, *, include: bool = True) -> dict:
        category = await db_client.post("/api/v1/expense-categories", json={"name": name})
        response = await db_client.post(
            "/api/v1/expenses",
            json={
                "expense_date": "2026-01-01",
                "category_id": category.json()["id"],
                "vat_treatment_id": treatment["id"],
                "vat_rate_id": rate["id"],
                "net_amount": "1000.00",
                "vat_amount": "210.00",
                "depreciation_years": 5,
                "include_in_pl": include,
                "pl_cost_basis": "AUTO",
            },
        )
        assert response.status_code == 201, response.text
        purchase = response.json()
        receipt = await db_client.post(
            f"/api/v1/expenses/{purchase['id']}/attachments",
            files={"file": ("receipt.pdf", b"%PDF-1.4\nsynthetic receipt", "application/pdf")},
        )
        assert receipt.status_code == 201, receipt.text
        purchase["receipt_id"] = receipt.json()["id"]
        return purchase

    async def register_battery(purchase: dict) -> dict:
        response = await db_client.post(
            "/api/v1/battery-assets",
            json={
                "acquisition_expense_id": purchase["id"],
                "in_service_date": "2026-01-01",
                "owner_confirms_ordinary_vat_full_deduction": True,
                "acknowledge_affected_periods": True,
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    async def register_fixed_asset(
        purchase: dict, *, name: str = "Linked battery", business_percentage: str = "100"
    ) -> dict:
        terms = {
            "asset_name": name,
            "in_service_date": "2026-01-01",
            "useful_life_years": 5,
            "residual_value": "0",
            "business_percentage": business_percentage,
            "use_type": "MIXED_USE" if business_percentage != "100" else "FULL_BUSINESS",
        }
        path = f"/api/v1/fixed-assets/expenses/{purchase['id']}"
        preview = await db_client.post(f"{path}/conversion-preview", json=terms)
        assert preview.status_code == 200, preview.text
        response = await db_client.post(
            f"{path}/convert",
            json={**terms, "preview_token": preview.json()["preview_token"]},
        )
        assert response.status_code == 201, response.text
        return response.json()

    battery_only_purchase = await create_purchase("Battery only")
    await register_battery(battery_only_purchase)
    fixed_first_purchase = await create_purchase("Fixed asset first")
    fixed_first_asset = await register_fixed_asset(fixed_first_purchase)
    fixed_first_battery = await register_battery(fixed_first_purchase)
    battery_first_purchase = await create_purchase("Battery first")
    battery_first_battery = await register_battery(battery_first_purchase)
    assert (await db_client.get(f"/api/v1/expenses/{battery_first_purchase['id']}"))\
        .json()["include_in_pl"] is False
    battery_first_asset = await register_fixed_asset(battery_first_purchase)
    ordinary_asset_purchase = await create_purchase("Ordinary included fixed asset")
    ordinary_asset = await register_fixed_asset(
        ordinary_asset_purchase, name="Ordinary included fixed asset"
    )
    excluded_asset_purchase = await create_purchase("Excluded zero-use fixed asset", include=False)
    excluded_asset = await register_fixed_asset(
        excluded_asset_purchase, name="Excluded zero-use fixed asset", business_percentage="0"
    )

    for purchase, source_flag in (
        (fixed_first_purchase, False),
        (battery_first_purchase, False),
        (battery_only_purchase, False),
        (ordinary_asset_purchase, True),
        (excluded_asset_purchase, False),
    ):
        saved = await db_client.get(f"/api/v1/expenses/{purchase['id']}")
        assert saved.status_code == 200
        assert saved.json()["include_in_pl"] is source_flag
        assert Decimal(saved.json()["base_vat_amount"]) == Decimal("210.000")
        receipt_list = await db_client.get(f"/api/v1/expenses/{purchase['id']}/attachments")
        assert receipt_list.status_code == 200, receipt_list.text
        assert [row["id"] for row in receipt_list.json()["items"]] == [purchase["receipt_id"]]
    fixed_first_saved = await db_client.get(f"/api/v1/expenses/{fixed_first_purchase['id']}")
    protected_change = await db_client.put(
        f"/api/v1/expenses/{fixed_first_purchase['id']}",
        json={**fixed_first_saved.json(), "include_in_pl": True},
    )
    assert 400 <= protected_change.status_code < 500
    assert (await db_client.get(f"/api/v1/expenses/{fixed_first_purchase['id']}"))\
        .json()["include_in_pl"] is False
    report_url = "/api/v1/reports/expenses?from=2026-01-01&to=2026-12-31"
    raw = await db_client.get(report_url)
    included = await db_client.get(f"{report_url}&include_in_pl=true")
    excluded = await db_client.get(f"{report_url}&include_in_pl=false")
    assert raw.status_code == included.status_code == excluded.status_code == 200
    assert len(raw.json()["by_category"]) == 5
    assert [row["category_name"] for row in included.json()["by_category"]] == [
        "Ordinary included fixed asset"
    ]
    assert {row["category_name"] for row in excluded.json()["by_category"]} == {
        "Battery only", "Fixed asset first", "Battery first", "Excluded zero-use fixed asset",
    }
    vat_report = await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=1")
    assert vat_report.status_code == 200, vat_report.text
    assert Decimal(vat_report.json()["boxes"]["box_5b"]["vat"]) == Decimal("1050.00")
    dashboard = await db_client.get("/api/v1/reports/dashboard?year=2026")
    assert dashboard.status_code == 200, dashboard.text
    assert {row["category_name"] for row in dashboard.json()["top_expense_categories"]} == {
        "Ordinary included fixed asset",
    }
    assert "Excluded zero-use fixed asset" not in {
        row["category_name"] for row in dashboard.json()["top_expense_categories"]
    }
    assert Decimal(dashboard.json()["kpi"]["ytd_fixed_asset_depreciation"]) > 0
    register = await db_client.get("/api/v1/fixed-assets?year=2026")
    assert register.status_code == 200, register.text
    asset_amounts = {
        row["id"]: Decimal(row["yearly_deltas"][0]["fixed_asset_depreciation"])
        for row in register.json()
    }
    assert asset_amounts == {
        fixed_first_asset["id"]: Decimal("166.67"),
        battery_first_asset["id"]: Decimal("166.67"),
        ordinary_asset["id"]: Decimal("166.67"),
        excluded_asset["id"]: Decimal("0.00"),
    }
    assert fixed_first_asset["id"] != battery_first_asset["id"]
    assert fixed_first_battery["id"] != battery_first_battery["id"]


@pytest.mark.integration
async def test_annual_use_preview_and_history_preserve_monthly_changes_when_quarters_net_zero(
    db_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FrozenDate(date):
        current = date(2026, 10, 1)

        @classmethod
        def today(cls) -> date:
            return cls.current

    monkeypatch.setattr(fixed_asset_service, "date", FrozenDate)
    monkeypatch.setattr(pl_service, "date", FrozenDate)
    await _auth(db_client, email="annual-month-delta@example.com")
    category = await db_client.post("/api/v1/expense-categories", json={"name": "Annual delta"})
    rates = await db_client.get("/api/v1/vat-rates")
    treatments = await db_client.get("/api/v1/vat-treatments?side=PURCHASE")
    rate = next(row for row in rates.json()["items"] if row["label"] == "NL standard (21%)")
    treatment = next(
        row for row in treatments.json()["items"] if row["code"] == "NL_DOMESTIC_PURCH"
    )
    purchase = await db_client.post("/api/v1/expenses", json={
        "expense_date": "2026-01-01", "category_id": category.json()["id"],
        "vat_treatment_id": treatment["id"], "vat_rate_id": rate["id"],
        "net_amount": "1000.00", "vat_amount": "210.00", "depreciation_years": 5,
        "include_in_pl": True, "pl_cost_basis": "AUTO",
    })
    assert purchase.status_code == 201, purchase.text
    terms = {"asset_name": "Annual month test", "in_service_date": "2026-01-01",
             "useful_life_years": 5, "residual_value": "0", "business_percentage": "49.999",
             "use_type": "MIXED_USE"}
    path = f"/api/v1/fixed-assets/expenses/{purchase.json()['id']}"
    converted = await db_client.post(f"{path}/conversion-preview", json=terms)
    assert converted.status_code == 200, converted.text
    asset_response = await db_client.post(f"{path}/convert", json={
        **terms, "preview_token": converted.json()["preview_token"]})
    assert asset_response.status_code == 201, asset_response.text
    asset_id = asset_response.json()["id"]
    month_url = "/api/v1/reports/profit-loss?from=2026-01-01&to=2026-09-30&granularity=month"
    quarter_url = "/api/v1/reports/profit-loss?from=2026-01-01&to=2026-09-30&granularity=quarter"
    before_months = (await db_client.get(month_url)).json()
    before_quarters = (await db_client.get(quarter_url)).json()
    preview = await db_client.post(
        f"/api/v1/fixed-assets/{asset_id}/annual-use/2026/confirm/preview",
        json={"actual_business_percentage": "50.000"},
    )
    assert preview.status_code == 200, preview.text
    monthly_deltas = preview.json()["monthly_period_deltas"]
    assert len(monthly_deltas) == 6
    assert {row["period"] for row in monthly_deltas} == {
        "2026-01", "2026-03", "2026-04", "2026-06", "2026-07", "2026-09"
    }
    assert all(row["pnl_delta"] != "0.00" for row in monthly_deltas)
    assert [row["period"] for row in preview.json()["period_deltas"]] == []
    FrozenDate.current = date(2026, 10, 2)
    stale = await db_client.post(
        f"/api/v1/fixed-assets/{asset_id}/annual-use/2026/confirm",
        json={
            "actual_business_percentage": "50.000",
            "preview_token": preview.json()["preview_token"],
            "reason": "Confirm measured annual business use",
        },
    )
    assert stale.status_code == 409, stale.text
    FrozenDate.current = date(2026, 10, 1)
    confirmed = await db_client.post(
        f"/api/v1/fixed-assets/{asset_id}/annual-use/2026/confirm",
        json={
            "actual_business_percentage": "50.000",
            "preview_token": preview.json()["preview_token"],
            "reason": "Confirm measured annual business use",
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    after_months = (await db_client.get(month_url)).json()
    after_quarters = (await db_client.get(quarter_url)).json()
    before = {
        row["period"][:7]: Decimal(row["fixed_asset_depreciation"])
        for row in before_months["series"]
    }
    after = {
        row["period"][:7]: Decimal(row["fixed_asset_depreciation"])
        for row in after_months["series"]
    }
    changed = {period for period in before if before[period] != after[period]}
    assert changed == {row["period"] for row in monthly_deltas}
    assert {period: (before[period], after[period]) for period in changed} == {
        "2026-01": (Decimal("8.33"), Decimal("8.34")),
        "2026-03": (Decimal("8.34"), Decimal("8.33")),
        "2026-04": (Decimal("8.33"), Decimal("8.34")),
        "2026-06": (Decimal("8.34"), Decimal("8.33")),
        "2026-07": (Decimal("8.33"), Decimal("8.34")),
        "2026-09": (Decimal("8.34"), Decimal("8.33")),
    }
    before_q = {
        row["period"]: Decimal(row["fixed_asset_depreciation"])
        for row in before_quarters["series"]
    }
    after_q = {
        row["period"]: Decimal(row["fixed_asset_depreciation"])
        for row in after_quarters["series"]
    }
    assert before_q == after_q
    assert list(before_q.values()) == [Decimal("25.00"), Decimal("25.00"), Decimal("25.00")]
    history = await db_client.get(f"/api/v1/fixed-assets/{asset_id}/annual-use/2026/revisions")
    assert history.status_code == 200, history.text
    audit_months = history.json()[0]["after_snapshot"]["monthly_period_deltas"]
    assert audit_months == monthly_deltas

    revision_preview = await db_client.post(
        f"/api/v1/fixed-assets/{asset_id}/annual-use/2026/revisions/preview",
        json={"expected_version": confirmed.json()["version"], "business_percentage": "49.999",
              "reason": "Correct measured annual business use", "preview_token": "0" * 64},
    )
    assert revision_preview.status_code == 200, revision_preview.text
    reverse_months = revision_preview.json()["monthly_period_deltas"]
    assert {row["period"] for row in reverse_months} == {row["period"] for row in monthly_deltas}
    assert [row["period"] for row in revision_preview.json()["period_deltas"]] == []
    reverse = await db_client.post(
        f"/api/v1/fixed-assets/{asset_id}/annual-use/2026/revisions",
        json={"expected_version": confirmed.json()["version"], "business_percentage": "49.999",
              "reason": "Correct measured annual business use",
              "preview_token": revision_preview.json()["preview_token"]},
    )
    assert reverse.status_code == 200, reverse.text
    final_months = (await db_client.get(month_url)).json()
    final_quarters = (await db_client.get(quarter_url)).json()
    final_by_month = {
        row["period"][:7]: Decimal(row["fixed_asset_depreciation"])
        for row in final_months["series"]
    }
    assert final_by_month == before
    final_q = {
        row["period"]: Decimal(row["fixed_asset_depreciation"])
        for row in final_quarters["series"]
    }
    assert final_q == before_q
    history = await db_client.get(f"/api/v1/fixed-assets/{asset_id}/annual-use/2026/revisions")
    assert history.status_code == 200, history.text
    assert history.json()[1]["after_snapshot"]["monthly_period_deltas"] == reverse_months

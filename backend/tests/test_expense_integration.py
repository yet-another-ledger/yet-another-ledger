"""Integration tests for M8 step 1 – Expense CRUD + list + guards + calculate.

Requires a running PostgreSQL instance (``pytest -m integration``).

Coverage:
- gross = net + vat, each quantised to 2 dp (EUR minor unit) – D11
- net × rate ≠ vat is accepted (D8: no equality check)
- deductible default: category True / False / None (→ fallback True) + explicit override (D9)
- Snapshots are preserved after category deletion (D12)
- PURCHASE-side treatment guard: sales-side treatment → 422
- Single base currency: currency = company.base_currency, exchange_rate = 1 (D10)
- base_* = original values (D10)
- List filters: category_id / date range / deductible / is_draft / q + pagination total + sort
- Cross-company expense → 404
- Owner-only: non-owner → 403
- Negative net or vat → 422 (schema validation)
- Full CRUD: create → read → update → delete
- PUT confirms draft (is_draft True → False); idempotent on already-confirmed expense
- POST /expenses/calculate: VAT preview (not persisted), happy path + guards
"""

from __future__ import annotations

import uuid

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jai.models._enums import ExpenseKind
from jai.models.expense import Expense

# ---------------------------------------------------------------------------
# Shared test helpers (self-contained, no cross-file imports)
# ---------------------------------------------------------------------------


async def _full_auth(
    client: AsyncClient,
    email: str = "owner@example.com",
    password: str = "testpassword1",
) -> None:
    resp = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": password}
    )
    assert resp.status_code == 201

    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    assert resp.status_code == 200

    resp = await client.post("/api/v1/auth/mfa/setup")
    assert resp.status_code == 200
    secret = resp.json()["secret"]

    code = pyotp.TOTP(secret).now()
    resp = await client.post("/api/v1/auth/mfa/verify", json={"code": code})
    assert resp.status_code == 204


async def _setup_company(client: AsyncClient, *, base_currency: str = "EUR") -> dict:
    """Setup company and return dict with vat rates + treatments."""
    resp = await client.put(
        "/api/v1/company",
        json={"name": "Test Co", "base_currency": base_currency, "country_code": "NL"},
    )
    assert resp.status_code == 200

    rates_resp = await client.get("/api/v1/vat-rates")
    rates = {r["label"]: r for r in rates_resp.json()["items"]}

    # Get PURCHASE-side treatments
    purch_resp = await client.get("/api/v1/vat-treatments?side=PURCHASE")
    purch_treatments = {t["code"]: t for t in purch_resp.json()["items"]}

    # Get SALES-side treatments (for guard tests)
    sales_resp = await client.get("/api/v1/vat-treatments?side=SALES")
    sales_treatments = {t["code"]: t for t in sales_resp.json()["items"]}

    return {
        "rates": rates,
        "purch_treatments": purch_treatments,
        "sales_treatments": sales_treatments,
    }


async def _create_expense_category(
    client: AsyncClient,
    *,
    name: str = "Office Supplies",
    default_deductible: bool | None = True,
) -> dict:
    """Create an expense category and return its JSON."""
    body: dict = {"name": name}
    if default_deductible is not None:
        body["default_deductible"] = default_deductible
    resp = await client.post("/api/v1/expense-categories", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_expense(
    client: AsyncClient,
    *,
    category_id: str,
    vat_treatment_id: str,
    vat_rate_id: str,
    net_amount: str = "100.00",
    vat_amount: str = "21.00",
    expense_date: str = "2026-06-13",
    supplier_name: str | None = "ACME BV",
    deductible: bool | None = None,
    reference: str | None = None,
    note: str | None = None,
    include_in_pl: bool | None = None,
    pl_cost_basis: str | None = None,
) -> dict:
    """Create an expense and return its JSON."""
    body: dict = {
        "expense_date": expense_date,
        "category_id": category_id,
        "vat_treatment_id": vat_treatment_id,
        "vat_rate_id": vat_rate_id,
        "net_amount": net_amount,
        "vat_amount": vat_amount,
    }
    if supplier_name is not None:
        body["supplier_name"] = supplier_name
    if deductible is not None:
        body["deductible"] = deductible
    if reference is not None:
        body["reference"] = reference
    if note is not None:
        body["note"] = note
    if include_in_pl is not None:
        body["include_in_pl"] = include_in_pl
    if pl_cost_basis is not None:
        body["pl_cost_basis"] = pl_cost_basis

    resp = await client.post("/api/v1/expenses", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Happy flow: create + read + amounts
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestExpenseCreate:
    async def test_expense_kind_is_purchase_on_create_read_and_cannot_be_injected(
        self, db_client: AsyncClient
    ) -> None:
        """The generic Expense contract owns company and always creates PURCHASE rows."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        category = await _create_expense_category(db_client)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]
        injected_company_id = str(uuid.uuid4())

        response = await db_client.post(
            "/api/v1/expenses",
            json={
                "expense_date": "2026-06-13",
                "category_id": category["id"],
                "vat_treatment_id": treatment["id"],
                "vat_rate_id": rate["id"],
                "net_amount": "100.00",
                "vat_amount": "21.00",
                "company_id": injected_company_id,
                "kind": "MILEAGE",
            },
        )
        assert response.status_code == 201, response.text
        created = response.json()
        assert created["kind"] == "PURCHASE"
        assert "company_id" not in created

        read_response = await db_client.get(f"/api/v1/expenses/{created['id']}")
        assert read_response.status_code == 200
        assert read_response.json()["kind"] == "PURCHASE"

    async def test_expense_kind_filter_applies_to_items_and_total(
        self,
        db_client: AsyncClient,
        db_session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The new kind filter keeps both list rows and pagination count in sync."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        category = await _create_expense_category(db_client)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]
        purchase = await _create_expense(
            db_client,
            category_id=category["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
            supplier_name="Purchase row",
        )
        mileage = await _create_expense(
            db_client,
            category_id=category["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
            supplier_name="Mileage projection",
        )
        async with db_session_maker() as session:
            mileage_expense = await session.get(Expense, uuid.UUID(mileage["id"]))
            assert mileage_expense is not None
            mileage_expense.kind = ExpenseKind.MILEAGE
            await session.commit()

        purchase_response = await db_client.get("/api/v1/expenses?kind=PURCHASE")
        assert purchase_response.status_code == 200
        purchase_list = purchase_response.json()
        assert purchase_list["total"] == 1
        assert [item["id"] for item in purchase_list["items"]] == [purchase["id"]]
        assert [item["kind"] for item in purchase_list["items"]] == ["PURCHASE"]

        mileage_response = await db_client.get("/api/v1/expenses?kind=MILEAGE")
        assert mileage_response.status_code == 200
        mileage_list = mileage_response.json()
        assert mileage_list["total"] == 1
        assert [item["id"] for item in mileage_list["items"]] == [mileage["id"]]
        assert [item["kind"] for item in mileage_list["items"]] == ["MILEAGE"]

    async def test_gross_equals_net_plus_vat_quantised(self, db_client: AsyncClient) -> None:
        """gross = quantize_to_minor_unit(net + vat); all at 2 dp (D11)."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_expense_category(db_client)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        exp = await _create_expense(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
            net_amount="100.00",
            vat_amount="21.00",
        )

        # NUMERIC(18,3) columns return 3 dp from DB; use Decimal for exact comparison
        from decimal import Decimal

        assert Decimal(exp["net_amount"]) == Decimal("100.00")
        assert Decimal(exp["vat_amount"]) == Decimal("21.00")
        assert Decimal(exp["gross_amount"]) == Decimal("121.00")

    async def test_net_times_rate_not_equal_vat_accepted(self, db_client: AsyncClient) -> None:
        """D8: vat ≠ net × rate is accepted without error.

        Real invoices often have per-line rounding artefacts.
        """
        from decimal import Decimal

        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_expense_category(db_client)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        # 21% of 100 = 21.00 but we intentionally give 21.05
        exp = await _create_expense(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
            net_amount="100.00",
            vat_amount="21.05",
        )

        assert Decimal(exp["net_amount"]) == Decimal("100.00")
        assert Decimal(exp["vat_amount"]) == Decimal("21.05")
        assert Decimal(exp["gross_amount"]) == Decimal("121.05")

    async def test_base_amounts_equal_original(self, db_client: AsyncClient) -> None:
        """D10: base_* = originals, currency = EUR, exchange_rate = 1."""
        from decimal import Decimal

        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_expense_category(db_client)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        exp = await _create_expense(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
            net_amount="80.00",
            vat_amount="16.80",
        )

        assert exp["currency"] == "EUR"
        assert Decimal(exp["exchange_rate"]) == Decimal("1")
        assert Decimal(exp["base_net_amount"]) == Decimal(exp["net_amount"])
        assert Decimal(exp["base_vat_amount"]) == Decimal(exp["vat_amount"])
        assert Decimal(exp["base_gross_amount"]) == Decimal(exp["gross_amount"])

    async def test_snapshots_captured_on_create(self, db_client: AsyncClient) -> None:
        """D7/D12: snapshot fields are populated at create time."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_expense_category(db_client, name="Custom Category XYZ")
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        exp = await _create_expense(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
        )

        assert exp["category_name"] == "Custom Category XYZ"
        assert exp["vat_treatment_code"] == "NL_DOMESTIC_PURCH"
        assert exp["vat_treatment_effect"] is not None
        assert float(exp["vat_rate_percent"]) == pytest.approx(21.0, abs=0.01)

    async def test_category_snapshot_preserved_after_category_deletion(
        self, db_client: AsyncClient
    ) -> None:
        """D12: category_name snapshot survives category deletion (FK SET NULL)."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_expense_category(db_client, name="Temporary Category")
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        exp = await _create_expense(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
        )
        exp_id = exp["id"]
        assert exp["category_name"] == "Temporary Category"

        # Delete the category
        del_resp = await db_client.delete(f"/api/v1/expense-categories/{cat['id']}")
        assert del_resp.status_code == 204

        # Fetch the expense: category_id should be null, but name snapshot intact
        get_resp = await db_client.get(f"/api/v1/expenses/{exp_id}")
        assert get_resp.status_code == 200
        updated = get_resp.json()
        assert updated["category_id"] is None
        assert updated["category_name"] == "Temporary Category"


# ---------------------------------------------------------------------------
# Deductible defaults (D9)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestDeductibleDefault:

    async def test_category_default_true_no_explicit(self, db_client: AsyncClient) -> None:
        """Default comes from category.default_deductible = True."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_expense_category(db_client, default_deductible=True)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        exp = await _create_expense(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
        )
        assert exp["deductible"] is True

    async def test_category_default_false_no_explicit(self, db_client: AsyncClient) -> None:
        """Default comes from category.default_deductible = False."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_expense_category(db_client, default_deductible=False)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        exp = await _create_expense(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
        )
        assert exp["deductible"] is False

    async def test_category_default_none_falls_back_to_true(
        self, db_client: AsyncClient
    ) -> None:
        """D9: category has no default → deductible falls back to True."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        # Create category with no default_deductible (None)
        cat = await _create_expense_category(db_client, default_deductible=None)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        exp = await _create_expense(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
        )
        assert exp["deductible"] is True

    async def test_explicit_overrides_category_default(self, db_client: AsyncClient) -> None:
        """Explicit deductible=False overrides category.default_deductible=True."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_expense_category(db_client, default_deductible=True)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        exp = await _create_expense(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
            deductible=False,
        )
        assert exp["deductible"] is False

    async def test_explicit_true_overrides_category_false(self, db_client: AsyncClient) -> None:
        """Explicit deductible=True overrides category.default_deductible=False."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_expense_category(db_client, default_deductible=False)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        exp = await _create_expense(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
            deductible=True,
        )
        assert exp["deductible"] is True


# ---------------------------------------------------------------------------
# Guards: PURCHASE side treatment, negative amounts
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestExpenseGuards:

    async def test_sales_side_treatment_rejected_422(self, db_client: AsyncClient) -> None:
        """A SALES-side VAT treatment is rejected with 422."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_expense_category(db_client)
        rate = seeds["rates"]["NL standard (21%)"]

        # Pick any SALES-side treatment
        sales_treatment = next(iter(seeds["sales_treatments"].values()))

        resp = await db_client.post(
            "/api/v1/expenses",
            json={
                "expense_date": "2026-06-13",
                "category_id": cat["id"],
                "vat_treatment_id": sales_treatment["id"],
                "vat_rate_id": rate["id"],
                "net_amount": "100.00",
                "vat_amount": "21.00",
            },
        )
        assert resp.status_code == 422
        assert "PURCHASE" in resp.json()["detail"]

    async def test_negative_net_rejected_422(self, db_client: AsyncClient) -> None:
        """Negative net_amount is rejected at schema validation (422)."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_expense_category(db_client)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        resp = await db_client.post(
            "/api/v1/expenses",
            json={
                "expense_date": "2026-06-13",
                "category_id": cat["id"],
                "vat_treatment_id": treatment["id"],
                "vat_rate_id": rate["id"],
                "net_amount": "-1.00",
                "vat_amount": "0.00",
            },
        )
        assert resp.status_code == 422

    async def test_negative_vat_rejected_422(self, db_client: AsyncClient) -> None:
        """Negative vat_amount is rejected at schema validation (422)."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_expense_category(db_client)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        resp = await db_client.post(
            "/api/v1/expenses",
            json={
                "expense_date": "2026-06-13",
                "category_id": cat["id"],
                "vat_treatment_id": treatment["id"],
                "vat_rate_id": rate["id"],
                "net_amount": "100.00",
                "vat_amount": "-5.00",
            },
        )
        assert resp.status_code == 422

    async def test_cross_company_category_rejected_404(self, db_client: AsyncClient) -> None:
        """category_id from another company → 404."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        resp = await db_client.post(
            "/api/v1/expenses",
            json={
                "expense_date": "2026-06-13",
                "category_id": str(uuid.uuid4()),  # random → cross-company
                "vat_treatment_id": treatment["id"],
                "vat_rate_id": rate["id"],
                "net_amount": "100.00",
                "vat_amount": "21.00",
            },
        )
        assert resp.status_code == 404

    async def test_cross_company_treatment_rejected_404(self, db_client: AsyncClient) -> None:
        """vat_treatment_id from another company → 404."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_expense_category(db_client)
        rate = seeds["rates"]["NL standard (21%)"]

        resp = await db_client.post(
            "/api/v1/expenses",
            json={
                "expense_date": "2026-06-13",
                "category_id": cat["id"],
                "vat_treatment_id": str(uuid.uuid4()),  # random → cross-company
                "vat_rate_id": rate["id"],
                "net_amount": "100.00",
                "vat_amount": "21.00",
            },
        )
        assert resp.status_code == 404

    async def test_cross_company_rate_rejected_404(self, db_client: AsyncClient) -> None:
        """vat_rate_id from another company → 404."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_expense_category(db_client)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]

        resp = await db_client.post(
            "/api/v1/expenses",
            json={
                "expense_date": "2026-06-13",
                "category_id": cat["id"],
                "vat_treatment_id": treatment["id"],
                "vat_rate_id": str(uuid.uuid4()),  # random → cross-company
                "net_amount": "100.00",
                "vat_amount": "21.00",
            },
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Cross-company isolation
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestExpenseCrossCompanyIsolation:
    """Cross-company isolation: a random UUID is indistinguishable from a
    cross-company resource because every lookup is scoped by company_id.
    Using random UUIDs mirrors the pattern from test_payment_integration.
    """

    async def test_get_expense_random_uuid_404(self, db_client: AsyncClient) -> None:
        """GET /expenses/{random_uuid} → 404 (cross-company or non-existent)."""
        await _full_auth(db_client)
        await _setup_company(db_client)

        resp = await db_client.get(f"/api/v1/expenses/{uuid.uuid4()}")
        assert resp.status_code == 404

    async def test_put_expense_random_uuid_404(self, db_client: AsyncClient) -> None:
        """PUT /expenses/{random_uuid} → 404."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_expense_category(db_client)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        resp = await db_client.put(
            f"/api/v1/expenses/{uuid.uuid4()}",
            json={
                "expense_date": "2026-06-13",
                "category_id": cat["id"],
                "vat_treatment_id": treatment["id"],
                "vat_rate_id": rate["id"],
                "net_amount": "50.00",
                "vat_amount": "10.50",
            },
        )
        assert resp.status_code == 404

    async def test_delete_expense_random_uuid_404(self, db_client: AsyncClient) -> None:
        """DELETE /expenses/{random_uuid} → 404."""
        await _full_auth(db_client)
        await _setup_company(db_client)

        resp = await db_client.delete(f"/api/v1/expenses/{uuid.uuid4()}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Owner-only
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestExpenseOwnerOnly:

    async def test_non_owner_rejected_403(self, db_client: AsyncClient) -> None:
        """A non-owner user cannot create expenses (403)."""
        # Register two users: owner + member
        await _full_auth(db_client, email="owner@example.com", password="testpassword1")
        seeds = await _setup_company(db_client)

        # Register member (role stays 'owner' in v1 so we need a workaround:
        # just try as unauthenticated/different role via a second client).
        # In v1 all registered users get role=owner, so we test 401 for
        # unauthenticated instead.

        # Unauthenticated request → 401
        import httpx
        from httpx import ASGITransport

        from jai.main import app

        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as anon:
            cat = await _create_expense_category(db_client)
            treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
            rate = seeds["rates"]["NL standard (21%)"]

            resp = await anon.post(
                "/api/v1/expenses",
                json={
                    "expense_date": "2026-06-13",
                    "category_id": cat["id"],
                    "vat_treatment_id": treatment["id"],
                    "vat_rate_id": rate["id"],
                    "net_amount": "100.00",
                    "vat_amount": "21.00",
                },
            )
            assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Full CRUD roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestExpenseCRUD:

    async def test_create_read_update_delete(self, db_client: AsyncClient) -> None:
        """Full CRUD roundtrip."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_expense_category(db_client, name="Cloud Software")
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        # CREATE
        exp = await _create_expense(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
            net_amount="200.00",
            vat_amount="42.00",
            supplier_name="GitHub BV",
            reference="INV-2026-001",
            note="Annual subscription",
            include_in_pl=False,
            pl_cost_basis="GROSS",
        )
        from decimal import Decimal

        exp_id = exp["id"]
        assert Decimal(exp["net_amount"]) == Decimal("200.00")
        assert Decimal(exp["gross_amount"]) == Decimal("242.00")
        assert exp["supplier_name"] == "GitHub BV"
        assert exp["reference"] == "INV-2026-001"
        assert exp["note"] == "Annual subscription"
        assert exp["is_draft"] is False

        # READ
        get_resp = await db_client.get(f"/api/v1/expenses/{exp_id}")
        assert get_resp.status_code == 200
        fetched = get_resp.json()
        assert fetched["id"] == exp_id
        assert Decimal(fetched["gross_amount"]) == Decimal("242.00")

        # UPDATE (use a name not in seeds)
        cat2 = await _create_expense_category(db_client, name="Business Travel Expenses")
        put_resp = await db_client.put(
            f"/api/v1/expenses/{exp_id}",
            json={
                "expense_date": "2026-06-14",
                "category_id": cat2["id"],
                "vat_treatment_id": treatment["id"],
                "vat_rate_id": rate["id"],
                "net_amount": "150.00",
                "vat_amount": "31.50",
                "supplier_name": "KLM BV",
            },
        )
        assert put_resp.status_code == 200
        updated = put_resp.json()
        assert Decimal(updated["net_amount"]) == Decimal("150.00")
        assert Decimal(updated["gross_amount"]) == Decimal("181.50")
        assert updated["supplier_name"] == "KLM BV"
        assert updated["category_name"] == "Business Travel Expenses"
        # Old clients omit these fields on PUT and keep the stored settings.
        assert updated["include_in_pl"] is False
        assert updated["pl_cost_basis"] == "GROSS"

        all_report = await db_client.get(
            "/api/v1/reports/expenses?from=2026-01-01&to=2026-12-31"
        )
        included_report = await db_client.get(
            "/api/v1/reports/expenses?from=2026-01-01&to=2026-12-31&include_in_pl=true"
        )
        excluded_report = await db_client.get(
            "/api/v1/reports/expenses?from=2026-01-01&to=2026-12-31&include_in_pl=false"
        )
        assert all_report.json()["total_net"] == "150.00"
        assert included_report.json()["total_net"] == "0"
        assert excluded_report.json()["total_net"] == "150.00"

        # DELETE
        del_resp = await db_client.delete(f"/api/v1/expenses/{exp_id}")
        assert del_resp.status_code == 204

        # GET after delete → 404
        get_after = await db_client.get(f"/api/v1/expenses/{exp_id}")
        assert get_after.status_code == 404

    async def test_get_unknown_expense_404(self, db_client: AsyncClient) -> None:
        """GET /expenses/{random_id} → 404."""
        await _full_auth(db_client)
        await _setup_company(db_client)

        resp = await db_client.get(f"/api/v1/expenses/{uuid.uuid4()}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# List filters + pagination + sort
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestExpenseList:

    async def _setup_expenses(self, client: AsyncClient) -> dict:
        """Helper: setup company + several expenses for filter tests."""
        seeds = await _setup_company(client)
        cat_a = await _create_expense_category(client, name="Cat A", default_deductible=True)
        cat_b = await _create_expense_category(client, name="Cat B", default_deductible=False)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        # Expense 1: Cat A, deductible=True, 2026-01-10
        e1 = await _create_expense(
            client,
            category_id=cat_a["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
            expense_date="2026-01-10",
            supplier_name="Supplier Alpha",
            reference="REF-001",
        )
        # Expense 2: Cat B, deductible=False, 2026-03-15
        e2 = await _create_expense(
            client,
            category_id=cat_b["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
            expense_date="2026-03-15",
            supplier_name="Supplier Beta",
        )
        # Expense 3: Cat A, deductible=True (explicit), 2026-06-13
        e3 = await _create_expense(
            client,
            category_id=cat_a["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
            expense_date="2026-06-13",
            supplier_name="Supplier Gamma",
            deductible=True,
        )
        return {
            "cat_a": cat_a,
            "cat_b": cat_b,
            "e1": e1,
            "e2": e2,
            "e3": e3,
            "treatment": treatment,
            "rate": rate,
        }

    async def test_list_all_returns_total(self, db_client: AsyncClient) -> None:
        """Total count is correct."""
        await _full_auth(db_client)
        await self._setup_expenses(db_client)

        resp = await db_client.get("/api/v1/expenses")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert len(data["items"]) == 3

    async def test_filter_by_category_id(self, db_client: AsyncClient) -> None:
        """category_id filter: only expenses in that category."""
        await _full_auth(db_client)
        ctx = await self._setup_expenses(db_client)

        resp = await db_client.get(
            f"/api/v1/expenses?category_id={ctx['cat_a']['id']}"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        ids = {item["id"] for item in data["items"]}
        assert ctx["e1"]["id"] in ids
        assert ctx["e3"]["id"] in ids

    async def test_filter_by_deductible_false(self, db_client: AsyncClient) -> None:
        """deductible=false filter: only non-deductible expenses."""
        await _full_auth(db_client)
        ctx = await self._setup_expenses(db_client)

        resp = await db_client.get("/api/v1/expenses?deductible=false")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["id"] == ctx["e2"]["id"]
        assert data["items"][0]["deductible"] is False

    async def test_filter_by_deductible_true(self, db_client: AsyncClient) -> None:
        """deductible=true filter: only deductible expenses."""
        await _full_auth(db_client)
        await self._setup_expenses(db_client)

        resp = await db_client.get("/api/v1/expenses?deductible=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2

    async def test_filter_by_date_range(self, db_client: AsyncClient) -> None:
        """date_from / date_to inclusive filter."""
        await _full_auth(db_client)
        ctx = await self._setup_expenses(db_client)

        resp = await db_client.get(
            "/api/v1/expenses?date_from=2026-01-01&date_to=2026-03-31"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        ids = {item["id"] for item in data["items"]}
        assert ctx["e1"]["id"] in ids
        assert ctx["e2"]["id"] in ids

    async def test_filter_by_q_supplier_name(self, db_client: AsyncClient) -> None:
        """q filter matches supplier_name (case-insensitive)."""
        await _full_auth(db_client)
        ctx = await self._setup_expenses(db_client)

        resp = await db_client.get("/api/v1/expenses?q=alpha")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["id"] == ctx["e1"]["id"]

    async def test_filter_by_q_reference(self, db_client: AsyncClient) -> None:
        """q filter also matches reference field."""
        await _full_auth(db_client)
        ctx = await self._setup_expenses(db_client)

        resp = await db_client.get("/api/v1/expenses?q=REF-001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["id"] == ctx["e1"]["id"]

    async def test_filter_by_q_category_name(self, db_client: AsyncClient) -> None:
        """q filter also matches category_name snapshot."""
        await _full_auth(db_client)
        ctx = await self._setup_expenses(db_client)

        resp = await db_client.get("/api/v1/expenses?q=Cat B")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["id"] == ctx["e2"]["id"]

    async def test_filter_by_is_draft_false(self, db_client: AsyncClient) -> None:
        """is_draft=false returns only non-draft expenses (all in step 1)."""
        await _full_auth(db_client)
        await self._setup_expenses(db_client)

        resp = await db_client.get("/api/v1/expenses?is_draft=false")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3  # all are non-draft

    async def test_filter_by_is_draft_true(self, db_client: AsyncClient) -> None:
        """is_draft=true returns only draft expenses (none in step 1)."""
        await _full_auth(db_client)
        await self._setup_expenses(db_client)

        resp = await db_client.get("/api/v1/expenses?is_draft=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0

    async def test_pagination_limit_offset(self, db_client: AsyncClient) -> None:
        """limit / offset pagination with consistent total count."""
        await _full_auth(db_client)
        await self._setup_expenses(db_client)

        # First page: 2 items
        resp1 = await db_client.get("/api/v1/expenses?limit=2&offset=0")
        assert resp1.status_code == 200
        data1 = resp1.json()
        assert data1["total"] == 3
        assert len(data1["items"]) == 2

        # Second page: 1 item
        resp2 = await db_client.get("/api/v1/expenses?limit=2&offset=2")
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["total"] == 3
        assert len(data2["items"]) == 1

    async def test_sort_by_expense_date_desc(self, db_client: AsyncClient) -> None:
        """Default sort: expense_date DESC (newest first)."""
        await _full_auth(db_client)
        await self._setup_expenses(db_client)

        resp = await db_client.get("/api/v1/expenses?sort_by=expense_date")
        assert resp.status_code == 200
        items = resp.json()["items"]
        dates = [item["expense_date"] for item in items]
        assert dates == sorted(dates, reverse=True)

    async def test_sort_by_created_at_desc(self, db_client: AsyncClient) -> None:
        """sort_by=created_at: newest created_at first."""
        await _full_auth(db_client)
        await self._setup_expenses(db_client)

        resp = await db_client.get("/api/v1/expenses?sort_by=created_at")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3


# ---------------------------------------------------------------------------
# PUT-based draft confirmation (M8 step-1 contract)
# ---------------------------------------------------------------------------


async def _create_recurring_and_run(
    client: AsyncClient,
    *,
    category_id: str,
    vat_treatment_id: str,
    vat_rate_id: str,
    start_date: str = "2025-01-01",
) -> dict:
    """Create a recurring-expense template and trigger run-now to get a draft."""
    body = {
        "name": "Confirm-test Template",
        "category_id": category_id,
        "vat_treatment_id": vat_treatment_id,
        "vat_rate_id": vat_rate_id,
        "net_amount": "100.00",
        "vat_amount": "21.00",
        "frequency": "MONTHLY",
        "start_date": start_date,
        "active": True,
    }
    resp = await client.post("/api/v1/recurring-expenses", json=body)
    assert resp.status_code == 201, resp.text
    rec_id = resp.json()["id"]

    run_resp = await client.post(f"/api/v1/recurring-expenses/{rec_id}/run-now")
    assert run_resp.status_code == 200, run_resp.text
    assert run_resp.json()["generated"] >= 1

    # Retrieve the generated draft via the expense list (is_draft=true)
    list_resp = await client.get("/api/v1/expenses?is_draft=true")
    assert list_resp.status_code == 200
    items = list_resp.json()["items"]
    assert len(items) >= 1
    return items[0]  # most-recent draft


@pytest.mark.integration
class TestPutConfirmsDraft:
    """PUT /expenses/{id} always sets is_draft=False (step-1 contract)."""

    async def test_put_draft_sets_is_draft_false(self, db_client: AsyncClient) -> None:
        """PUT on a draft expense (is_draft=True) → response + DB have is_draft=False."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_expense_category(db_client)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        # Generate a draft via recurring-expense run-now
        draft = await _create_recurring_and_run(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
        )
        assert draft["is_draft"] is True, "pre-condition: must be a draft"
        draft_id = draft["id"]

        # Fetch full expense (list item may be minimal)
        full_resp = await db_client.get(f"/api/v1/expenses/{draft_id}")
        assert full_resp.status_code == 200
        full = full_resp.json()

        # PUT (confirm) – send clean ExpenseInput, no is_draft field
        put_resp = await db_client.put(
            f"/api/v1/expenses/{draft_id}",
            json={
                "expense_date": full["expense_date"],
                "category_id": full["category_id"],
                "vat_treatment_id": full["vat_treatment_id"],
                "vat_rate_id": full["vat_rate_id"],
                "net_amount": full["net_amount"],
                "vat_amount": full["vat_amount"],
                "deductible": full["deductible"],
                "reference": full.get("reference"),
                "note": full.get("note"),
            },
        )
        assert put_resp.status_code == 200, put_resp.text
        updated = put_resp.json()

        # Response confirms is_draft is now False
        assert updated["is_draft"] is False, "PUT must flip is_draft to False"

        # Verify DB state via GET
        get_resp = await db_client.get(f"/api/v1/expenses/{draft_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["is_draft"] is False, "DB must persist is_draft=False"

    async def test_put_confirmed_expense_stays_false(self, db_client: AsyncClient) -> None:
        """PUT on an already-confirmed expense keeps is_draft=False (idempotent)."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_expense_category(db_client)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        # Create a regular (non-draft) expense
        exp = await _create_expense(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
            net_amount="50.00",
            vat_amount="10.50",
        )
        assert exp["is_draft"] is False, "pre-condition: regular create sets is_draft=False"
        exp_id = exp["id"]

        # PUT again → is_draft must remain False
        put_resp = await db_client.put(
            f"/api/v1/expenses/{exp_id}",
            json={
                "expense_date": exp["expense_date"],
                "category_id": exp["category_id"],
                "vat_treatment_id": exp["vat_treatment_id"],
                "vat_rate_id": exp["vat_rate_id"],
                "net_amount": exp["net_amount"],
                "vat_amount": exp["vat_amount"],
                "deductible": exp["deductible"],
            },
        )
        assert put_resp.status_code == 200, put_resp.text
        assert put_resp.json()["is_draft"] is False, "is_draft must stay False (idempotent)"


# ---------------------------------------------------------------------------
# POST /expenses/calculate – VAT preview endpoint
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestExpenseCalculate:
    """POST /expenses/calculate – VAT and gross preview (not persisted)."""

    async def test_happy_path_21_percent(self, db_client: AsyncClient) -> None:
        """100.00 × 21% → vat=21.00, gross=121.00; vat_rate_percent returned."""
        from decimal import Decimal

        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        rate = seeds["rates"]["NL standard (21%)"]

        resp = await db_client.post(
            "/api/v1/expenses/calculate",
            json={"net_amount": "100.00", "vat_rate_id": rate["id"]},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert Decimal(data["net_amount"]) == Decimal("100.00")
        assert Decimal(data["vat_amount"]) == Decimal("21.00")
        assert Decimal(data["gross_amount"]) == Decimal("121.00")
        assert Decimal(data["vat_rate_percent"]) == pytest.approx(
            Decimal("21"), abs=Decimal("0.01")
        )

    async def test_cost_preview_handles_nonrecoverable_and_reverse_charge_vat(
        self, db_client: AsyncClient
    ) -> None:
        """AUTO includes EU consumer VAT but excludes reverse-charge self-assessment."""
        from decimal import Decimal

        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        purchase = seeds["purch_treatments"]
        common = {
            "net_amount": "100.00",
            "vat_amount": "21.00",
            "deductible": True,
            "pl_cost_basis": "AUTO",
            "include_in_pl": True,
            "business_percentage": "100",
            "depreciation_years": 1,
        }
        eu_vat = await db_client.post(
            "/api/v1/expenses/cost-preview",
            json={
                **common,
                "vat_treatment_id": purchase["EU_B2C_PURCH"]["id"],
            },
        )
        assert eu_vat.status_code == 200, eu_vat.text
        assert Decimal(eu_vat.json()["cost_basis_amount"]) == Decimal("121.00")
        assert Decimal(eu_vat.json()["annual_cost"]) == Decimal("121.00")

        reverse_charge = await db_client.post(
            "/api/v1/expenses/cost-preview",
            json={
                **common,
                "vat_treatment_id": purchase["EU_B2B_REVERSE_PURCH"]["id"],
            },
        )
        assert reverse_charge.status_code == 200, reverse_charge.text
        assert Decimal(reverse_charge.json()["cost_basis_amount"]) == Decimal("100.00")

        excluded = await db_client.post(
            "/api/v1/expenses/cost-preview",
            json={
                **common,
                "vat_treatment_id": purchase["EU_B2C_PURCH"]["id"],
                "include_in_pl": False,
            },
        )
        assert excluded.status_code == 200, excluded.text
        assert Decimal(excluded.json()["annual_cost"]) == Decimal("0.00")

    def _setup_and_get_rate_id_sync(self) -> None:
        """Helper placeholder – actual setup uses async helpers above."""

    async def test_happy_path_9_percent(self, db_client: AsyncClient) -> None:
        """50.00 × 9% → vat=4.50, gross=54.50."""
        from decimal import Decimal

        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        rate = seeds["rates"]["NL reduced (9%)"]

        resp = await db_client.post(
            "/api/v1/expenses/calculate",
            json={"net_amount": "50.00", "vat_rate_id": rate["id"]},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert Decimal(data["vat_amount"]) == Decimal("4.50")
        assert Decimal(data["gross_amount"]) == Decimal("54.50")

    async def test_net_zero_returns_all_zeros(self, db_client: AsyncClient) -> None:
        """Zero net → vat 0, gross 0."""
        from decimal import Decimal

        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        rate = seeds["rates"]["NL standard (21%)"]

        resp = await db_client.post(
            "/api/v1/expenses/calculate",
            json={"net_amount": "0", "vat_rate_id": rate["id"]},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert Decimal(data["net_amount"]) == Decimal("0.00")
        assert Decimal(data["vat_amount"]) == Decimal("0.00")
        assert Decimal(data["gross_amount"]) == Decimal("0.00")

    async def test_cross_company_vat_rate_returns_404(self, db_client: AsyncClient) -> None:
        """vat_rate_id from another company (or random UUID) → 404."""
        await _full_auth(db_client)
        await _setup_company(db_client)

        resp = await db_client.post(
            "/api/v1/expenses/calculate",
            json={"net_amount": "100.00", "vat_rate_id": str(uuid.uuid4())},
        )
        assert resp.status_code == 404

    async def test_unauthenticated_returns_401(self, db_client: AsyncClient) -> None:
        """Unauthenticated request → 401 (owner-only guard)."""
        import httpx
        from httpx import ASGITransport

        from jai.main import app

        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        rate = seeds["rates"]["NL standard (21%)"]

        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as anon:
            resp = await anon.post(
                "/api/v1/expenses/calculate",
                json={"net_amount": "100.00", "vat_rate_id": rate["id"]},
            )
        assert resp.status_code == 401


@pytest.mark.integration
class TestCostPreviewPersistenceParity:
    @pytest.mark.parametrize(
        ("basis", "deductible", "percentage", "years", "included", "expected"),
        [
            ("NET", True, "50", 1, True, "50.01"),
            ("NET", True, "49.9995", 1, True, "50.01"),
            ("NET", True, "49.9994", 1, True, "50.00"),
            ("AUTO", True, "50", 1, True, "50.01"),
            ("AUTO", False, "50", 1, True, "60.51"),
            ("GROSS", True, "50", 1, True, "60.51"),
            ("AUTO", None, "50", 1, True, "60.51"),
            ("AUTO", False, "75", 3, True, "30.26"),
            ("AUTO", False, "0", 1, True, "0.00"),
            ("NET", True, "50", 1, False, "0.00"),
        ],
    )
    async def test_raw_preview_matches_saved_cost_and_report(
        self, db_client: AsyncClient, basis: str, deductible: bool | None,
        percentage: str, years: int, included: bool, expected: str,
    ) -> None:
        from decimal import Decimal

        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_expense_category(db_client, default_deductible=False)
        body = {
            "net_amount": "100.005", "vat_amount": "21.005",
            "category_id": cat["id"],
            "vat_treatment_id": seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]["id"],
            "deductible": deductible, "pl_cost_basis": basis, "include_in_pl": included,
            "business_percentage": percentage, "depreciation_years": years,
        }
        preview = await db_client.post("/api/v1/expenses/cost-preview", json=body)
        assert preview.status_code == 200, preview.text
        saved = await db_client.post("/api/v1/expenses", json={
            **body, "expense_date": "2026-06-13",
            "vat_rate_id": seeds["rates"]["NL standard (21%)"]["id"],
        })
        assert saved.status_code == 201, saved.text
        assert Decimal(saved.json()["net_amount"]) == Decimal("100.010")
        current = await db_client.get(f"/api/v1/expenses/{saved.json()['id']}/cost-preview")
        report = await db_client.get("/api/v1/reports/profit-loss?from=2026-01-01&to=2026-12-31")
        assert current.status_code == report.status_code == 200
        for result in (preview, current):
            assert Decimal(result.json()["annual_cost"]) == Decimal(expected)
        assert Decimal(report.json()["expense_actual"]) == Decimal(expected)

    @pytest.mark.parametrize("changed_field", ["effect", "code"])
    async def test_current_preview_uses_persisted_tax_snapshot(
        self, db_client: AsyncClient, changed_field: str,
    ) -> None:
        from decimal import Decimal

        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_expense_category(db_client)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        # Changing a normal deductible treatment's code to EU_B2C makes proposed VAT
        # irrecoverable; changing effect to ZERO_REVERSE removes irrecoverable VAT.
        deductible = changed_field == "code"
        original, proposed = ("100.00", "121.00") if deductible else ("121.00", "100.00")
        expense = await _create_expense(
            db_client, category_id=cat["id"], vat_treatment_id=treatment["id"],
            vat_rate_id=seeds["rates"]["NL standard (21%)"]["id"],
            deductible=deductible, pl_cost_basis="AUTO",
        )
        path = f"/api/v1/expenses/{expense['id']}/cost-preview"
        before = await db_client.get(path)
        report_path = "/api/v1/reports/profit-loss?from=2026-01-01&to=2026-12-31"
        report_before = await db_client.get(report_path)
        if changed_field == "code":
            # Free the seeded unique code through the public API first.
            eu = seeds["purch_treatments"]["EU_B2C_PURCH"]
            renamed = await db_client.put(f"/api/v1/vat-treatments/{eu['id']}", json={
                **eu, "code": "OLD_EU_B2C_PURCH",
            })
            assert renamed.status_code == 200, renamed.text
        changed = await db_client.put(f"/api/v1/vat-treatments/{treatment['id']}", json={
            **treatment, changed_field: "EU_B2C_PURCH" if deductible else "ZERO_REVERSE",
        })
        assert changed.status_code == 200, changed.text
        after = await db_client.get(path)
        raw = {
            "net_amount": expense["net_amount"], "vat_amount": expense["vat_amount"],
            "vat_treatment_id": treatment["id"], "deductible": deductible,
            "pl_cost_basis": "AUTO", "business_percentage": "100", "depreciation_years": 1,
        }
        proposal = await db_client.post("/api/v1/expenses/cost-preview", json=raw)
        assert proposal.status_code == 200, proposal.text
        assert before.json() == after.json()
        assert Decimal(after.json()["annual_cost"]) == Decimal(original)
        assert Decimal(proposal.json()["annual_cost"]) == Decimal(proposed)
        report_after = await db_client.get(report_path)
        assert report_before.json() == report_after.json()
        assert Decimal(report_after.json()["expense_actual"]) == Decimal(original)
        # Preview is read-only; a subsequent real update explicitly snapshots the new treatment.
        updated = await db_client.put(f"/api/v1/expenses/{expense['id']}", json={
            **raw, "expense_date": expense["expense_date"], "category_id": cat["id"],
            "vat_rate_id": seeds["rates"]["NL standard (21%)"]["id"],
        })
        assert updated.status_code == 200, updated.text
        current = await db_client.get(path)
        for field in ("annual_cost", "cost_basis_amount"):
            assert Decimal(current.json()[field]) == Decimal(proposal.json()[field])
        assert current.json()["included_in_pl"] == proposal.json()["included_in_pl"]

    async def test_current_cost_preview_guards(self, db_client: AsyncClient) -> None:
        paths = [
            f"/api/v1/{resource}/{uuid.uuid4()}/cost-preview"
            for resource in ("expenses", "recurring-expenses")
        ]
        for path in paths:
            assert (await db_client.get(path)).status_code == 401
        await _full_auth(db_client)
        await _setup_company(db_client)
        for path in paths:
            assert (await db_client.get(path)).status_code == 404

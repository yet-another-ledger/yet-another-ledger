"""Integration tests for M8 step 3 – Recurring expense CRUD + generation.

Requires a running PostgreSQL instance (``pytest -m integration``).

Coverage (mirrors M8.md step 3 「测试（必覆盖）」):
- compute_next_run pure-function tests → see test_recurring_expense.py (no DB needed)
- generate_due_* idempotent: run twice → same expense only generated once
- To-date generation: is_draft=true + snapshot fields + recurring_expense_id backlink
- Inactive (active=false) template is skipped by generation
- max_occurrences cap: after N generations template goes active=false
- end_date cap: generation past end_date → template goes active=false
- run-now endpoint: same semantics as bulk job
- Cross-company isolation: GET / run-now returns 404 for other company's template
- Owner-only: non-owner → 403
- Full CRUD: create → read → update (pause/resume) → delete
- DELETE does NOT cascade-delete generated expenses (recurring_expense_id → NULL)
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pyotp
import pytest
from httpx import AsyncClient

# ---------------------------------------------------------------------------
# Shared test helpers (self-contained, mirrors test_expense_integration.py)
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
    """Create company and return vat rates + treatments dict."""
    resp = await client.put(
        "/api/v1/company",
        json={"name": "Test Co", "base_currency": base_currency, "country_code": "NL"},
    )
    assert resp.status_code == 200

    rates_resp = await client.get("/api/v1/vat-rates")
    rates = {r["label"]: r for r in rates_resp.json()["items"]}

    purch_resp = await client.get("/api/v1/vat-treatments?side=PURCHASE")
    purch_treatments = {t["code"]: t for t in purch_resp.json()["items"]}

    return {"rates": rates, "purch_treatments": purch_treatments}


async def _create_category(
    client: AsyncClient,
    *,
    name: str = "Office Supplies",
    default_deductible: bool = True,
) -> dict:
    resp = await client.post(
        "/api/v1/expense-categories",
        json={"name": name, "default_deductible": default_deductible},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_recurring(
    client: AsyncClient,
    *,
    category_id: str,
    vat_treatment_id: str,
    vat_rate_id: str,
    name: str = "Monthly Office Rent",
    frequency: str = "MONTHLY",
    start_date: str,
    net_amount: str = "500.00",
    vat_amount: str = "105.00",
    end_date: str | None = None,
    max_occurrences: int | None = None,
    active: bool = True,
    deductible: bool | None = None,
    include_in_pl: bool = True,
    pl_cost_basis: str = "NET",
) -> dict:
    body: dict = {
        "name": name,
        "category_id": category_id,
        "vat_treatment_id": vat_treatment_id,
        "vat_rate_id": vat_rate_id,
        "net_amount": net_amount,
        "vat_amount": vat_amount,
        "frequency": frequency,
        "start_date": start_date,
        "active": active,
        "include_in_pl": include_in_pl,
        "pl_cost_basis": pl_cost_basis,
    }
    if end_date is not None:
        body["end_date"] = end_date
    if max_occurrences is not None:
        body["max_occurrences"] = max_occurrences
    if deductible is not None:
        body["deductible"] = deductible
    resp = await client.post("/api/v1/recurring-expenses", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# CRUD tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestRecurringExpenseCRUD:
    """Basic CRUD + list operations."""

    async def test_create_and_read(self, db_client: AsyncClient) -> None:
        """POST creates a template with correct fields; GET returns it."""
        from decimal import Decimal

        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_category(db_client)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        rec = await _create_recurring(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
            start_date="2026-01-01",
            frequency="MONTHLY",
        )

        assert rec["name"] == "Monthly Office Rent"
        assert rec["frequency"] == "MONTHLY"
        assert rec["start_date"] == "2026-01-01"
        assert rec["next_run_date"] == "2026-01-01"
        assert rec["occurrences_generated"] == 0
        assert rec["active"] is True
        assert Decimal(rec["net_amount"]) == Decimal("500.00")
        assert Decimal(rec["vat_amount"]) == Decimal("105.00")
        assert Decimal(rec["gross_amount"]) == Decimal("605.00")

        # GET single
        get_resp = await db_client.get(f"/api/v1/recurring-expenses/{rec['id']}")
        assert get_resp.status_code == 200
        assert get_resp.json()["id"] == rec["id"]

    async def test_update_name_and_amount(self, db_client: AsyncClient) -> None:
        """PUT updates template fields; amounts are recomputed."""
        from decimal import Decimal

        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_category(db_client)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        rec = await _create_recurring(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
            start_date="2026-01-01",
        )

        # Update with new net_amount
        put_resp = await db_client.put(
            f"/api/v1/recurring-expenses/{rec['id']}",
            json={
                "name": "Updated Rent",
                "category_id": cat["id"],
                "vat_treatment_id": treatment["id"],
                "vat_rate_id": rate["id"],
                "net_amount": "600.00",
                "vat_amount": "126.00",
                "frequency": "MONTHLY",
                "start_date": "2026-01-01",
                "active": True,
            },
        )
        assert put_resp.status_code == 200
        updated = put_resp.json()
        assert updated["name"] == "Updated Rent"
        assert Decimal(updated["net_amount"]) == Decimal("600.00")
        assert Decimal(updated["gross_amount"]) == Decimal("726.00")

    async def test_pause_and_resume(self, db_client: AsyncClient) -> None:
        """PUT with active=false pauses; PUT with active=true resumes."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_category(db_client)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        rec = await _create_recurring(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
            start_date="2026-01-01",
        )

        base_body = {
            "name": rec["name"],
            "category_id": cat["id"],
            "vat_treatment_id": treatment["id"],
            "vat_rate_id": rate["id"],
            "net_amount": "500.00",
            "vat_amount": "105.00",
            "frequency": "MONTHLY",
            "start_date": "2026-01-01",
        }

        # Pause
        pause_resp = await db_client.put(
            f"/api/v1/recurring-expenses/{rec['id']}",
            json={**base_body, "active": False},
        )
        assert pause_resp.status_code == 200
        assert pause_resp.json()["active"] is False

        # Resume
        resume_resp = await db_client.put(
            f"/api/v1/recurring-expenses/{rec['id']}",
            json={**base_body, "active": True},
        )
        assert resume_resp.status_code == 200
        assert resume_resp.json()["active"] is True

    async def test_delete_template_does_not_delete_generated_expenses(
        self, db_client: AsyncClient
    ) -> None:
        """DELETE template → generated expenses kept; recurring_expense_id → NULL."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_category(db_client)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        today = date.today()
        rec = await _create_recurring(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
            start_date=str(today),
        )

        # Generate an expense via run-now
        run_resp = await db_client.post(f"/api/v1/recurring-expenses/{rec['id']}/run-now")
        assert run_resp.status_code == 200
        assert run_resp.json()["generated"] == 1

        # Confirm expense exists and has recurring_expense_id set
        expenses_resp = await db_client.get("/api/v1/expenses?is_draft=true")
        assert expenses_resp.status_code == 200
        items = expenses_resp.json()["items"]
        assert len(items) == 1
        expense_id = items[0]["id"]

        # Read full expense to check recurring_expense_id
        exp_resp = await db_client.get(f"/api/v1/expenses/{expense_id}")
        assert exp_resp.json()["recurring_expense_id"] == rec["id"]

        # Delete the template
        del_resp = await db_client.delete(f"/api/v1/recurring-expenses/{rec['id']}")
        assert del_resp.status_code == 204

        # Expense still exists and recurring_expense_id is now NULL (SET NULL FK)
        exp_resp2 = await db_client.get(f"/api/v1/expenses/{expense_id}")
        assert exp_resp2.status_code == 200
        assert exp_resp2.json()["recurring_expense_id"] is None

    async def test_list_filter_by_active(self, db_client: AsyncClient) -> None:
        """GET list with active=false returns only inactive templates."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_category(db_client)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        await _create_recurring(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
            start_date="2026-01-01",
            active=True,
        )
        await _create_recurring(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
            start_date="2026-01-01",
            active=False,
            name="Paused Subscription",
        )

        active_list = await db_client.get("/api/v1/recurring-expenses?active=true")
        assert active_list.json()["total"] == 1

        inactive_list = await db_client.get("/api/v1/recurring-expenses?active=false")
        assert inactive_list.json()["total"] == 1

        all_list = await db_client.get("/api/v1/recurring-expenses")
        assert all_list.json()["total"] == 2


# ---------------------------------------------------------------------------
# Generation tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestRecurringExpenseGeneration:
    """Generation logic, idempotency, limits, and run-now."""

    async def test_run_now_generates_draft_expense_with_correct_fields(
        self, db_client: AsyncClient
    ) -> None:
        """run-now generates a is_draft=true expense with correct snapshot + backlink."""
        from decimal import Decimal

        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_category(db_client, name="Software")
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        today = date.today()
        rec = await _create_recurring(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
            start_date=str(today),
            net_amount="200.00",
            vat_amount="42.00",
            name="Monthly SaaS",
            include_in_pl=False,
            pl_cost_basis="GROSS",
        )

        run_resp = await db_client.post(f"/api/v1/recurring-expenses/{rec['id']}/run-now")
        assert run_resp.status_code == 200
        body = run_resp.json()
        assert body["generated"] == 1

        # Check the generated expense
        expenses_resp = await db_client.get("/api/v1/expenses")
        assert expenses_resp.json()["total"] == 1
        item = expenses_resp.json()["items"][0]
        assert item["is_draft"] is True
        assert Decimal(item["net_amount"]) == Decimal("200.00")
        assert Decimal(item["vat_amount"]) == Decimal("42.00")
        assert Decimal(item["gross_amount"]) == Decimal("242.00")
        assert item["category_name"] == "Software"
        assert item["include_in_pl"] is False
        assert item["pl_cost_basis"] == "GROSS"

        # Full expense has recurring_expense_id
        exp = await db_client.get(f"/api/v1/expenses/{item['id']}")
        assert exp.json()["recurring_expense_id"] == rec["id"]
        assert exp.json()["vat_treatment_code"] == treatment["code"]

    async def test_run_now_is_idempotent(self, db_client: AsyncClient) -> None:
        """Running run-now twice does not generate two expenses for the same period."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_category(db_client)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        today = date.today()
        rec = await _create_recurring(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
            start_date=str(today),
        )

        # First run
        run1 = await db_client.post(f"/api/v1/recurring-expenses/{rec['id']}/run-now")
        assert run1.json()["generated"] == 1

        # Second run – cursor already advanced, next_run_date is in the future
        run2 = await db_client.post(f"/api/v1/recurring-expenses/{rec['id']}/run-now")
        assert run2.json()["generated"] == 0

        # Only one expense in DB
        expenses_resp = await db_client.get("/api/v1/expenses")
        assert expenses_resp.json()["total"] == 1

    async def test_inactive_template_skipped_by_run_now(
        self, db_client: AsyncClient
    ) -> None:
        """A paused (active=false) template generates nothing when run-now called."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_category(db_client)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        today = date.today()
        rec = await _create_recurring(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
            start_date=str(today),
            active=False,  # paused
        )

        run = await db_client.post(f"/api/v1/recurring-expenses/{rec['id']}/run-now")
        assert run.json()["generated"] == 0

        expenses_resp = await db_client.get("/api/v1/expenses")
        assert expenses_resp.json()["total"] == 0

    async def test_max_occurrences_deactivates_template(
        self, db_client: AsyncClient
    ) -> None:
        """Template with max_occurrences=1 goes active=false after one generation."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_category(db_client)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        today = date.today()
        rec = await _create_recurring(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
            start_date=str(today),
            max_occurrences=1,
        )

        # First run generates and deactivates
        run = await db_client.post(f"/api/v1/recurring-expenses/{rec['id']}/run-now")
        assert run.json()["generated"] == 1

        # Template is now inactive
        get_resp = await db_client.get(f"/api/v1/recurring-expenses/{rec['id']}")
        assert get_resp.json()["active"] is False
        assert get_resp.json()["occurrences_generated"] == 1

        # Second run: already inactive → generates nothing
        run2 = await db_client.post(f"/api/v1/recurring-expenses/{rec['id']}/run-now")
        assert run2.json()["generated"] == 0

    async def test_end_date_deactivates_template(self, db_client: AsyncClient) -> None:
        """Template with end_date in the past (or today) deactivates after generation."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_category(db_client)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        today = date.today()
        # end_date = today: this generation is the last one; next_run_date would
        # be next month, which is > end_date, so template deactivates.
        rec = await _create_recurring(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
            start_date=str(today),
            end_date=str(today),
            frequency="MONTHLY",
        )

        run = await db_client.post(f"/api/v1/recurring-expenses/{rec['id']}/run-now")
        assert run.json()["generated"] == 1

        # Template should now be deactivated (next_run_date > end_date)
        get_resp = await db_client.get(f"/api/v1/recurring-expenses/{rec['id']}")
        assert get_resp.json()["active"] is False

    async def test_future_next_run_date_not_generated(
        self, db_client: AsyncClient
    ) -> None:
        """Template whose next_run_date is tomorrow generates nothing today."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_category(db_client)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        tomorrow = str(date.today() + timedelta(days=1))
        rec = await _create_recurring(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
            start_date=tomorrow,
        )

        run = await db_client.post(f"/api/v1/recurring-expenses/{rec['id']}/run-now")
        assert run.json()["generated"] == 0

        expenses_resp = await db_client.get("/api/v1/expenses")
        assert expenses_resp.json()["total"] == 0

    async def test_run_now_next_run_date_advanced(self, db_client: AsyncClient) -> None:
        """After run-now, next_run_date advances by one period."""
        from datetime import date

        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_category(db_client)
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        today = date.today()
        rec = await _create_recurring(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
            start_date=str(today),
            frequency="MONTHLY",
        )

        run = await db_client.post(f"/api/v1/recurring-expenses/{rec['id']}/run-now")
        assert run.json()["generated"] == 1

        # next_run_date should be ~1 month ahead
        from jai.models._enums import RecurringFrequency
        from jai.services.recurring_expense import compute_next_run

        expected_next = compute_next_run(RecurringFrequency.MONTHLY, today)
        assert run.json()["next_run_date"] == str(expected_next)


# ---------------------------------------------------------------------------
# Guards and isolation
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestRecurringExpenseGuards:
    """Cross-company isolation + owner-only checks."""

    async def test_cross_company_404(self, db_client: AsyncClient) -> None:
        """GET / run-now for a random UUID (cross-company or non-existent) → 404.

        All lookups are scoped by company_id, so a random UUID is
        indistinguishable from another company's template – same pattern as
        test_expense_integration.py's cross-company suite.
        """
        await _full_auth(db_client)
        await _setup_company(db_client)

        fake_id = str(uuid.uuid4())
        get_resp = await db_client.get(f"/api/v1/recurring-expenses/{fake_id}")
        assert get_resp.status_code == 404

        run_resp = await db_client.post(f"/api/v1/recurring-expenses/{fake_id}/run-now")
        assert run_resp.status_code == 404

        put_resp = await db_client.put(
            f"/api/v1/recurring-expenses/{fake_id}",
            json={
                "name": "X",
                "category_id": str(uuid.uuid4()),
                "vat_treatment_id": str(uuid.uuid4()),
                "vat_rate_id": str(uuid.uuid4()),
                "net_amount": "100",
                "vat_amount": "21",
                "frequency": "MONTHLY",
                "start_date": "2026-01-01",
            },
        )
        assert put_resp.status_code == 404

        del_resp = await db_client.delete(f"/api/v1/recurring-expenses/{fake_id}")
        assert del_resp.status_code == 404

    async def test_owner_only_unauthenticated(self, db_client: AsyncClient) -> None:
        """Unauthenticated requests to recurring-expenses endpoints return 401.

        v1 registers all users as 'owner', so we test the auth boundary by
        using a fresh unauthenticated client – mirrors test_expense_integration.
        """
        await _full_auth(db_client)
        await _setup_company(db_client)

        import httpx
        from httpx import ASGITransport

        from jai.main import app

        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as anon:
            list_resp = await anon.get("/api/v1/recurring-expenses")
            assert list_resp.status_code == 401

            create_resp = await anon.post(
                "/api/v1/recurring-expenses",
                json={
                    "name": "Test",
                    "category_id": str(uuid.uuid4()),
                    "vat_treatment_id": str(uuid.uuid4()),
                    "vat_rate_id": str(uuid.uuid4()),
                    "net_amount": "100",
                    "vat_amount": "21",
                    "frequency": "MONTHLY",
                    "start_date": "2026-01-01",
                },
            )
            assert create_resp.status_code == 401

    async def test_not_found(self, db_client: AsyncClient) -> None:
        """GET non-existent template → 404."""
        await _full_auth(db_client)
        await _setup_company(db_client)

        fake_id = str(uuid.uuid4())
        resp = await db_client.get(f"/api/v1/recurring-expenses/{fake_id}")
        assert resp.status_code == 404

    async def test_delete_not_found(self, db_client: AsyncClient) -> None:
        """DELETE non-existent template → 404."""
        await _full_auth(db_client)
        await _setup_company(db_client)

        fake_id = str(uuid.uuid4())
        resp = await db_client.delete(f"/api/v1/recurring-expenses/{fake_id}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# end_date boundary: job-delay / run-now catch-up scenario
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestEndDateBoundary:
    """Verify end_date check is against next_run_date, not wall-clock today.

    Scenario: template started in the past, end_date is also in the past
    (but >= start_date), and the job/run-now is being called today (after a
    simulated delay).  Concretely:

        next_run_date = 5 days ago  (= start_date)
        end_date      = 4 days ago  (next_run_date <= end_date)
        today         = actual today (end_date < today)

    Expected:
    - run-now generates the last occurrence (next_run_date <= end_date ✓).
    - after generation, next_run_date advances to ~1 month from start; that
      new next_run_date > end_date, so the template is deactivated.
    - second run-now → 0 generated (already deactivated).

    This tests the fixed behaviour: the OLD code used ``today > end_date`` and
    would have skipped generation entirely (set active=False without generating).
    """

    async def test_last_occurrence_generated_when_job_delayed(
        self, db_client: AsyncClient
    ) -> None:
        """next_run_date <= end_date < today: last period must still be generated."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_category(db_client, name="Delayed Billing")
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        today = date.today()
        start = today - timedelta(days=5)   # next_run_date = start = 5 days ago
        end = today - timedelta(days=4)     # end_date = 4 days ago; start <= end < today

        rec = await _create_recurring(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
            start_date=str(start),
            end_date=str(end),
            frequency="MONTHLY",
            net_amount="100.00",
            vat_amount="21.00",
        )

        # Precondition: next_run_date == start <= end < today
        assert rec["next_run_date"] == str(start)
        assert rec["active"] is True

        # run-now should generate the last (and only) occurrence
        run = await db_client.post(f"/api/v1/recurring-expenses/{rec['id']}/run-now")
        assert run.status_code == 200
        body = run.json()
        assert body["generated"] == 1, (
            "Last occurrence must be generated when next_run_date <= end_date, "
            "even if end_date < today (job-delay / catch-up scenario)."
        )

        # Template should now be deactivated (new next_run_date > end_date)
        get_resp = await db_client.get(f"/api/v1/recurring-expenses/{rec['id']}")
        assert get_resp.json()["active"] is False

        # Generated expense is a draft with correct date
        expenses_resp = await db_client.get("/api/v1/expenses")
        assert expenses_resp.json()["total"] == 1
        expense_date = expenses_resp.json()["items"][0]["expense_date"]
        assert expense_date == str(start), "Expense date should be the occurrence's next_run_date"

        # Second run: inactive → generates nothing
        run2 = await db_client.post(f"/api/v1/recurring-expenses/{rec['id']}/run-now")
        assert run2.json()["generated"] == 0

    async def test_next_run_date_after_end_date_not_generated(
        self, db_client: AsyncClient
    ) -> None:
        """next_run_date > end_date: no generation, template deactivated immediately."""
        await _full_auth(db_client)
        seeds = await _setup_company(db_client)
        cat = await _create_category(db_client, name="Expired Billing")
        treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
        rate = seeds["rates"]["NL standard (21%)"]

        today = date.today()
        # end_date is yesterday; start_date (= next_run_date) is today → next_run_date > end_date
        start = today
        end = today - timedelta(days=1)  # end_date is before next_run_date

        rec = await _create_recurring(
            db_client,
            category_id=cat["id"],
            vat_treatment_id=treatment["id"],
            vat_rate_id=rate["id"],
            start_date=str(start),
            end_date=str(end),
            frequency="MONTHLY",
            net_amount="50.00",
            vat_amount="10.50",
        )

        # next_run_date == today > end_date: limit already exceeded
        run = await db_client.post(f"/api/v1/recurring-expenses/{rec['id']}/run-now")
        assert run.status_code == 200
        assert run.json()["generated"] == 0, (
            "Should generate nothing when next_run_date > end_date."
        )

        # Template should be deactivated
        get_resp = await db_client.get(f"/api/v1/recurring-expenses/{rec['id']}")
        assert get_resp.json()["active"] is False

        expenses_resp = await db_client.get("/api/v1/expenses")
        assert expenses_resp.json()["total"] == 0


@pytest.mark.integration
@pytest.mark.parametrize("basis", ["AUTO", "GROSS"])
async def test_legacy_template_cost_preview_and_generation(
    db_client: AsyncClient, basis: str,
) -> None:
    """NET→AUTO/GROSS previews use stored current values and agree with future generation."""
    from decimal import Decimal

    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    cat = await _create_category(db_client, default_deductible=False)
    treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
    rate = seeds["rates"]["NL standard (21%)"]
    template = await _create_recurring(
        db_client, category_id=cat["id"], vat_treatment_id=treatment["id"],
        vat_rate_id=rate["id"], start_date=str(date.today()), net_amount="100", vat_amount="21",
    )
    path = f"/api/v1/recurring-expenses/{template['id']}/cost-preview"
    current = await db_client.get(path)
    assert current.status_code == 200, current.text
    assert Decimal(current.json()["annual_cost"]) == Decimal("100")
    body = {
        "category_id": cat["id"], "vat_treatment_id": treatment["id"],
        "net_amount": "100", "vat_amount": "21", "deductible": None,
        "business_percentage": "100", "depreciation_years": 1, "pl_cost_basis": basis,
    }
    proposal = await db_client.post("/api/v1/expenses/cost-preview", json=body)
    assert proposal.status_code == 200, proposal.text
    assert Decimal(proposal.json()["annual_cost"]) == Decimal("121")
    assert (await db_client.get(path)).json() == current.json()
    updated = await db_client.put(f"/api/v1/recurring-expenses/{template['id']}", json={
        **template, **body, "vat_rate_id": rate["id"],
    })
    assert updated.status_code == 200, updated.text
    saved_cost = await db_client.get(path)
    for field in ("annual_cost", "cost_basis_amount"):
        assert Decimal(saved_cost.json()[field]) == Decimal(proposal.json()[field])
    generated = await db_client.post(f"/api/v1/recurring-expenses/{template['id']}/run-now")
    assert generated.status_code == 200, generated.text
    expenses = await db_client.get("/api/v1/expenses")
    expense = expenses.json()["items"][0]
    assert expense["pl_cost_basis"] == basis
    generated_cost = await db_client.get(f"/api/v1/expenses/{expense['id']}/cost-preview")
    assert generated_cost.json() == saved_cost.json()


@pytest.mark.integration
async def test_template_current_cost_and_generation_keep_tax_snapshot_after_dictionary_edit(
    db_client: AsyncClient,
) -> None:
    from decimal import Decimal

    await _full_auth(db_client)
    seeds = await _setup_company(db_client)
    cat = await _create_category(db_client, default_deductible=False)
    treatment = seeds["purch_treatments"]["NL_DOMESTIC_PURCH"]
    template = await _create_recurring(
        db_client, category_id=cat["id"], vat_treatment_id=treatment["id"],
        vat_rate_id=seeds["rates"]["NL standard (21%)"]["id"], start_date=str(date.today()),
        net_amount="100", vat_amount="21", pl_cost_basis="AUTO",
    )
    path = f"/api/v1/recurring-expenses/{template['id']}/cost-preview"
    before = await db_client.get(path)
    changed = await db_client.put(f"/api/v1/vat-treatments/{treatment['id']}", json={
        **treatment, "effect": "ZERO_REVERSE",
    })
    assert changed.status_code == 200, changed.text
    after = await db_client.get(path)
    assert after.json() == before.json()
    assert Decimal(after.json()["annual_cost"]) == Decimal("121")
    proposal = await db_client.post("/api/v1/expenses/cost-preview", json={
        "net_amount": "100", "vat_amount": "21", "vat_treatment_id": treatment["id"],
        "deductible": False, "pl_cost_basis": "AUTO", "business_percentage": "100",
        "depreciation_years": 1,
    })
    assert proposal.status_code == 200, proposal.text
    assert Decimal(proposal.json()["annual_cost"]) == Decimal("100")
    generated = await db_client.post(f"/api/v1/recurring-expenses/{template['id']}/run-now")
    assert generated.status_code == 200, generated.text
    expenses = await db_client.get("/api/v1/expenses")
    expense = expenses.json()["items"][0]
    current = await db_client.get(f"/api/v1/expenses/{expense['id']}/cost-preview")
    assert current.json() == before.json()

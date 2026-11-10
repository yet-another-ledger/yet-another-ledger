"""M11 step-2 integration coverage for mileage CRUD and Expense projection."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jai.models._enums import ExpenseKind, SettingLevel
from jai.models.company import Company
from jai.models.expense import Expense
from jai.models.mileage import MileageRateAdjustment, MileageTrip
from jai.models.setting import Setting
from jai.models.user import User
from jai.schemas.mileage import MileageDefaultsRead, MileageExpenseWrite
from jai.schemas.setting import SETTING_KEY_MILEAGE_DEFAULTS
from jai.services import mileage as mileage_service
from jai.services import vat as vat_service
from jai.services.settings import get_setting


async def _authenticate(client: AsyncClient, email: str = "mileage-crud@example.com") -> None:
    password = "testpassword1"
    assert (
        await client.post("/api/v1/auth/register", json={"email": email, "password": password})
    ).status_code == 201
    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    assert login.status_code == 200
    setup = await client.post("/api/v1/auth/mfa/setup")
    assert setup.status_code == 200
    verify = await client.post(
        "/api/v1/auth/mfa/verify", json={"code": pyotp.TOTP(setup.json()["secret"]).now()}
    )
    assert verify.status_code == 204


async def _setup_company(client: AsyncClient) -> dict[str, object]:
    company_response = await client.put(
        "/api/v1/company",
        json={"name": "Mileage CRUD Co", "base_currency": "EUR", "country_code": "NL"},
    )
    assert company_response.status_code == 200
    defaults = (await client.get("/api/v1/settings/mileage-defaults")).json()
    types = (await client.get("/api/v1/mileage-transport-types")).json()["items"]
    rates = (await client.get("/api/v1/mileage-rates")).json()["items"]
    return {
        "company": company_response.json(),
        "defaults": defaults,
        "types": {item["name"]: item for item in types},
        "rates": rates,
    }


async def _create_trip(client: AsyncClient, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "trip_date": "2026-06-15",
        "one_way_distance_km": "12.500",
        "round_trip": True,
        "origin_address": "Amsterdam",
        "destination_address": "Utrecht",
        "purpose": "Customer visit",
        "note": "Parking not included",
    }
    body.update(overrides)
    response = await client.post("/api/v1/mileage-expenses", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def _create_taxable_purchase(client: AsyncClient) -> dict[str, object]:
    category = await client.post(
        "/api/v1/expense-categories", json={"name": "Taxable baseline", "default_deductible": True}
    )
    assert category.status_code == 201
    rates = (await client.get("/api/v1/vat-rates")).json()["items"]
    treatments = (await client.get("/api/v1/vat-treatments?side=PURCHASE")).json()["items"]
    standard_rate = next(rate for rate in rates if Decimal(rate["percent"]) == Decimal("21"))
    domestic_purchase = next(
        treatment for treatment in treatments if treatment["code"] == "NL_DOMESTIC_PURCH"
    )
    purchase = await client.post(
        "/api/v1/expenses",
        json={
            "expense_date": "2026-06-13",
            "category_id": category.json()["id"],
            "vat_treatment_id": domestic_purchase["id"],
            "vat_rate_id": standard_rate["id"],
            "net_amount": "100.00",
            "vat_amount": "21.00",
        },
    )
    assert purchase.status_code == 201, purchase.text
    return purchase.json()


@pytest.mark.integration
class TestMileageCrudProjection:
    async def test_transaction_primitive_returns_usable_result_and_outer_rollback_removes_both_rows(
        self,
        db_client: AsyncClient,
        db_session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _authenticate(db_client)
        setup = await _setup_company(db_client)
        company_id = uuid.UUID(setup["company"]["id"])  # type: ignore[index]

        async with db_session_maker() as session:
            result = await mileage_service.create_mileage_expense_in_transaction(
                session,
                company_id,
                MileageExpenseWrite(
                    trip_date=date(2026, 6, 15),
                    one_way_distance_km=Decimal("12.500"),
                    round_trip=True,
                    purpose="Uncommitted visit",
                ),
                creator_id=None,
            )
            assert session.in_transaction()
            assert result.id is not None
            assert result.expense_id is not None
            assert result.one_way_distance_km == Decimal("12.500")
            assert result.total_distance_km == Decimal("25.000")
            assert result.amount == Decimal("6.25")
            assert result.purpose == "Uncommitted visit"
            await session.rollback()

        async with db_session_maker() as session:
            assert await session.get(MileageTrip, result.id) is None
            assert await session.get(Expense, result.expense_id) is None

    async def test_creation_bypasses_stale_settings_cache_without_caching_uncommitted_defaults(
        self,
        db_client: AsyncClient,
        db_session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _authenticate(db_client)
        setup = await _setup_company(db_client)
        company_id = uuid.UUID(setup["company"]["id"])  # type: ignore[index]
        original_defaults = setup["defaults"]
        alternate = await db_client.post(
            "/api/v1/mileage-transport-types", json={"name": "Fresh default", "active": True}
        )
        assert alternate.status_code == 201, alternate.text
        alternate_id = uuid.UUID(alternate.json()["id"])

        async with db_session_maker() as session:
            updated = MileageDefaultsRead(
                expense_category_id=uuid.UUID(original_defaults["expense_category_id"]),  # type: ignore[index]
                default_transport_type_id=alternate_id,
            )
            # The default is already warm in the process cache from setup.
            # Simulate a concurrent committed writer without invalidating that
            # local cache entry, then make the change transaction-local here.
            await session.execute(
                update(Setting)
                .where(
                    Setting.level == SettingLevel.COMPANY,
                    Setting.scope_id == company_id,
                    Setting.key == SETTING_KEY_MILEAGE_DEFAULTS,
                )
                .values(value=updated.model_dump(mode="json"))
            )
            result = await mileage_service.create_mileage_expense_in_transaction(
                session,
                company_id,
                MileageExpenseWrite(
                    trip_date=date(2026, 6, 15), one_way_distance_km=Decimal("1.000")
                ),
                creator_id=None,
                bypass_settings_cache=True,
            )
            assert result.transport_type_id == alternate_id
            cached = await get_setting(
                session,
                SETTING_KEY_MILEAGE_DEFAULTS,
                level=SettingLevel.COMPANY,
                scope_id=company_id,
                value_type=MileageDefaultsRead,
            )
            assert cached is not None
            assert cached.default_transport_type_id != alternate_id
            await session.rollback()

    async def test_owner_create_keeps_database_normalized_decimal_wire_scale(
        self, db_client: AsyncClient
    ) -> None:
        await _authenticate(db_client)
        await _setup_company(db_client)
        response = await db_client.post(
            "/api/v1/mileage-expenses",
            json={"trip_date": "2026-06-15", "one_way_distance_km": "1.2"},
        )
        assert response.status_code == 201, response.text
        assert response.json()["one_way_distance_km"] == "1.200"

    async def test_put_audits_immutable_scope_when_same_rule_moves_to_general_or_another_type(
        self,
        db_client: AsyncClient,
        db_session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _authenticate(db_client)
        await _setup_company(db_client)
        scope_a_response = await db_client.post(
            "/api/v1/mileage-transport-types", json={"name": "Scope A", "active": True}
        )
        scope_b_response = await db_client.post(
            "/api/v1/mileage-transport-types", json={"name": "Scope B", "active": True}
        )
        assert scope_a_response.status_code == scope_b_response.status_code == 201
        scope_a = scope_a_response.json()
        scope_b = scope_b_response.json()

        async def create_scoped_trip() -> tuple[dict[str, object], dict[str, object]]:
            rate_response = await db_client.post(
                "/api/v1/mileage-rates",
                json={
                    "transport_type_id": scope_a["id"],
                    "effective_from": "2026-02-01",
                    "rate_per_km": "0.300",
                },
            )
            assert rate_response.status_code == 201, rate_response.text
            trip = await _create_trip(
                db_client,
                transport_type_id=scope_a["id"],
                one_way_distance_km="10.000",
                round_trip=False,
            )
            return rate_response.json(), trip

        # A same-UUID rule moving from type-specific to general must compare
        # the old persisted Scope A against the new general scope.
        general_rate, general_trip = await create_scoped_trip()
        move_to_general = await db_client.put(
            f"/api/v1/mileage-rates/{general_rate['id']}",
            json={
                "effective_from": "2026-02-01",
                "rate_per_km": "0.300",
            },
        )
        assert move_to_general.status_code == 200, move_to_general.text
        general_update = await db_client.put(
            f"/api/v1/mileage-expenses/{general_trip['id']}",
            json={
                "trip_date": "2026-06-15",
                "transport_type_id": scope_a["id"],
                "one_way_distance_km": "10.000",
                "round_trip": False,
            },
        )
        assert general_update.status_code == 200, general_update.text

        # A different same-UUID rule moving Scope A -> Scope B records both
        # immutable scopes when the trip's selected type follows it to B.
        type_rate, type_trip = await create_scoped_trip()
        move_to_type_b = await db_client.put(
            f"/api/v1/mileage-rates/{type_rate['id']}",
            json={
                "transport_type_id": scope_b["id"],
                "effective_from": "2026-02-01",
                "rate_per_km": "0.300",
            },
        )
        assert move_to_type_b.status_code == 200, move_to_type_b.text
        type_update = await db_client.put(
            f"/api/v1/mileage-expenses/{type_trip['id']}",
            json={
                "trip_date": "2026-06-15",
                "transport_type_id": scope_b["id"],
                "one_way_distance_km": "10.000",
                "round_trip": False,
            },
        )
        assert type_update.status_code == 200, type_update.text

        async with db_session_maker() as session:
            adjustments = list(
                (
                    await session.execute(
                        select(MileageRateAdjustment)
                        .where(
                            MileageRateAdjustment.trip_id.in_(
                                [uuid.UUID(general_trip["id"]), uuid.UUID(type_trip["id"])]
                            )
                        )
                        .order_by(MileageRateAdjustment.trip_id, MileageRateAdjustment.created_at)
                    )
                ).scalars()
            )
        assert len(adjustments) == 2
        by_trip = {adjustment.trip_id: adjustment for adjustment in adjustments}
        general_adjustment = by_trip[uuid.UUID(general_trip["id"])]
        assert (
            general_adjustment.old_rate_rule_id
            == general_adjustment.new_rate_rule_id
            == uuid.UUID(general_rate["id"])
        )
        assert general_adjustment.old_rate_transport_type_id == uuid.UUID(scope_a["id"])
        assert general_adjustment.old_rate_transport_type_name == "Scope A"
        assert general_adjustment.new_rate_transport_type_id is None
        assert general_adjustment.new_rate_transport_type_name is None
        type_adjustment = by_trip[uuid.UUID(type_trip["id"])]
        assert (
            type_adjustment.old_rate_rule_id
            == type_adjustment.new_rate_rule_id
            == uuid.UUID(type_rate["id"])
        )
        assert type_adjustment.old_rate_transport_type_id == uuid.UUID(scope_a["id"])
        assert type_adjustment.old_rate_transport_type_name == "Scope A"
        assert type_adjustment.new_rate_transport_type_id == uuid.UUID(scope_b["id"])
        assert type_adjustment.new_rate_transport_type_name == "Scope B"

    async def test_put_audits_complete_rate_scope_and_ignores_distance_only_change(
        self,
        db_client: AsyncClient,
        db_session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _authenticate(db_client)
        setup = await _setup_company(db_client)
        car = setup["types"]["Car"]  # type: ignore[index]
        electric_response = await db_client.post(
            "/api/v1/mileage-transport-types", json={"name": "Audit electric", "active": True}
        )
        assert electric_response.status_code == 201
        electric = electric_response.json()
        rate_response = await db_client.post(
            "/api/v1/mileage-rates",
            json={
                "transport_type_id": electric["id"],
                "effective_from": "2026-01-01",
                "rate_per_km": "0.300",
            },
        )
        assert rate_response.status_code == 201
        rate = rate_response.json()
        trip = await _create_trip(
            db_client,
            transport_type_id=electric["id"],
            one_way_distance_km="10.000",
            round_trip=False,
        )

        # A rate rule can be edited in place.  Its stable UUID must not hide
        # the 0.300 -> 0.400 adjustment when the trip is subsequently saved.
        assert (
            await db_client.put(
                f"/api/v1/mileage-rates/{rate['id']}",
                json={
                    "transport_type_id": electric["id"],
                    "effective_from": "2026-01-01",
                    "rate_per_km": "0.400",
                },
            )
        ).status_code == 200
        updated = await db_client.put(
            f"/api/v1/mileage-expenses/{trip['id']}",
            json={
                "trip_date": "2026-06-15",
                "transport_type_id": electric["id"],
                "one_way_distance_km": "10.000",
                "round_trip": False,
            },
        )
        assert updated.status_code == 200, updated.text
        assert Decimal(updated.json()["amount"]) == Decimal("4.00")

        # Switching to a general rule records both the old type-specific and
        # new general scopes.  A later distance-only change has no rate audit.
        switched = await db_client.put(
            f"/api/v1/mileage-expenses/{trip['id']}",
            json={
                "trip_date": "2026-06-15",
                "transport_type_id": car["id"],
                "one_way_distance_km": "10.000",
                "round_trip": False,
            },
        )
        assert switched.status_code == 200, switched.text
        distance_only = await db_client.put(
            f"/api/v1/mileage-expenses/{trip['id']}",
            json={
                "trip_date": "2026-06-15",
                "transport_type_id": car["id"],
                "one_way_distance_km": "11.000",
                "round_trip": False,
            },
        )
        assert distance_only.status_code == 200

        async with db_session_maker() as session:
            adjustments = list(
                (
                    await session.execute(
                        select(MileageRateAdjustment)
                        .where(MileageRateAdjustment.trip_id == uuid.UUID(trip["id"]))
                        .order_by(MileageRateAdjustment.created_at, MileageRateAdjustment.id)
                    )
                ).scalars()
            )
            projection = await session.get(Expense, uuid.UUID(trip["expense_id"]))
        assert projection is not None
        assert Decimal(str(projection.net_amount)) == Decimal("2.75")
        assert len(adjustments) == 2
        same_rule, general = adjustments
        assert same_rule.old_rate_rule_id == same_rule.new_rate_rule_id == uuid.UUID(rate["id"])
        assert same_rule.old_rate_transport_type_id == uuid.UUID(electric["id"])
        assert same_rule.new_rate_transport_type_id == uuid.UUID(electric["id"])
        assert same_rule.old_rate_transport_type_name == "Audit electric"
        assert same_rule.new_rate_transport_type_name == "Audit electric"
        assert Decimal(str(same_rule.old_rate_per_km)) == Decimal("0.300")
        assert Decimal(str(same_rule.new_rate_per_km)) == Decimal("0.400")
        assert general.old_rate_transport_type_id == uuid.UUID(electric["id"])
        assert general.old_rate_transport_type_name == "Audit electric"
        assert general.new_rate_transport_type_id is None
        assert general.new_rate_transport_type_name is None
        assert Decimal(str(general.new_rate_per_km)) == Decimal("0.250")

    async def test_controlled_configuration_and_range_errors_leave_no_rows(
        self, db_client: AsyncClient
    ) -> None:
        await _authenticate(db_client)
        setup = await _setup_company(db_client)
        overflow = await db_client.post(
            "/api/v1/mileage-expenses",
            json={
                "trip_date": "2026-06-15",
                "one_way_distance_km": "999999999999999.999",
                "round_trip": True,
            },
        )
        assert overflow.status_code == 422
        assert "Total distance" in overflow.json()["detail"]
        assert (await db_client.get("/api/v1/mileage-expenses")).json()["total"] == 0
        assert (await db_client.get("/api/v1/expenses?kind=MILEAGE")).json()["total"] == 0

        defaults = setup["defaults"]  # type: ignore[assignment]
        category = await db_client.get(
            f"/api/v1/expense-categories/{defaults['expense_category_id']}"
        )
        assert category.status_code == 200
        inactive_category = await db_client.put(
            f"/api/v1/expense-categories/{defaults['expense_category_id']}",
            json={**category.json(), "active": False},
        )
        assert inactive_category.status_code == 200
        missing_default = await db_client.post(
            "/api/v1/mileage-expenses",
            json={"trip_date": "2026-06-15", "one_way_distance_km": "1.000"},
        )
        assert missing_default.status_code == 409
        assert "Mileage expense category" in missing_default.json()["detail"]
        assert (await db_client.get("/api/v1/mileage-expenses")).json()["total"] == 0
        assert (await db_client.get("/api/v1/expenses?kind=MILEAGE")).json()["total"] == 0

    async def test_defaults_guard_tenant_isolation_and_complete_list_contract(
        self,
        db_client: AsyncClient,
        db_session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _authenticate(db_client, "mileage-company-a@example.com")
        first = await _setup_company(db_client)
        first_car = first["types"]["Car"]  # type: ignore[index]
        first_trip = await _create_trip(
            db_client,
            trip_date="2026-06-01",
            transport_type_id=first_car["id"],
            purpose="A-only",
        )
        first_rate = first["rates"][0]  # type: ignore[index]

        # The active configured default cannot be made inactive or removed.
        blocked_inactive = await db_client.put(
            f"/api/v1/mileage-transport-types/{first_car['id']}",
            json={"name": "Car", "active": False},
        )
        blocked_delete = await db_client.delete(
            f"/api/v1/mileage-transport-types/{first_car['id']}")
        assert blocked_inactive.status_code == blocked_delete.status_code == 409
        assert "default transport type" in blocked_inactive.json()["detail"]

        # The application deliberately permits one registered owner.  Move
        # that authenticated owner's company association to a separately
        # seeded company so the HTTP paths exercise genuine cross-tenant IDs.
        async with db_session_maker() as session:
            second_company = Company(name="Mileage CRUD Co B", base_currency="EUR")
            session.add(second_company)
            await session.flush()
            await vat_service.seed_for_company(session, second_company.id)
            await mileage_service.seed_for_company(session, second_company.id)
            owner = (
                await session.execute(
                    select(User).where(User.email == "mileage-company-a@example.com")
                )
            ).scalar_one()
            owner.company_id = second_company.id
            await session.commit()
        second = await _setup_company(db_client)
        second_defaults = second["defaults"]  # type: ignore[assignment]
        assert second_defaults != first["defaults"]
        assert (
            await db_client.get(f"/api/v1/mileage-transport-types/{first_car['id']}")
        ).status_code == 404
        assert (await db_client.put(
            f"/api/v1/mileage-transport-types/{first_car['id']}",
            json={"name": "Cross tenant", "active": True},
        )).status_code == 404
        assert (
            await db_client.delete(f"/api/v1/mileage-transport-types/{first_car['id']}")
        ).status_code == 404
        assert (
            await db_client.get(f"/api/v1/mileage-rates/{first_rate['id']}")
        ).status_code == 404
        assert (await db_client.put(
            f"/api/v1/mileage-rates/{first_rate['id']}",
            json={"effective_from": "2026-01-01", "rate_per_km": "0.300"},
        )).status_code == 404
        assert (
            await db_client.delete(f"/api/v1/mileage-rates/{first_rate['id']}")
        ).status_code == 404
        assert (
            await db_client.get(f"/api/v1/mileage-expenses/{first_trip['id']}")
        ).status_code == 404
        assert (await db_client.put(
            f"/api/v1/mileage-expenses/{first_trip['id']}",
            json={"trip_date": "2026-06-01", "one_way_distance_km": "1.000"},
        )).status_code == 404
        assert (
            await db_client.delete(f"/api/v1/mileage-expenses/{first_trip['id']}")
        ).status_code == 404
        assert (await db_client.put(
            "/api/v1/settings/mileage-defaults",
            json=first["defaults"],
        )).status_code == 404
        filtered = await db_client.get(
            f"/api/v1/mileage-expenses?transport_type_id={first_car['id']}"
        )
        assert filtered.status_code == 200
        assert filtered.json() == {"items": [], "total": 0}

        second_car = second["types"]["Car"]  # type: ignore[index]
        first_list = await _create_trip(
            db_client, trip_date="2026-06-01", transport_type_id=second_car["id"], purpose="first"
        )
        second_list = await _create_trip(
            db_client, trip_date="2026-06-02", transport_type_id=second_car["id"], purpose="second"
        )
        third_list = await _create_trip(
            db_client, trip_date="2026-06-03", transport_type_id=second_car["id"], purpose="third"
        )
        inclusive = await db_client.get(
            f"/api/v1/mileage-expenses?transport_type_id={second_car['id']}&date_from=2026-06-01&date_to=2026-06-02"
        )
        assert inclusive.status_code == 200
        assert {item["id"] for item in inclusive.json()["items"]} == {
            first_list["id"],
            second_list["id"],
        }
        page = await db_client.get("/api/v1/mileage-expenses?limit=1&offset=1&sort_by=created_at")
        assert page.status_code == 200
        assert page.json()["total"] == 3
        assert page.json()["items"][0]["id"] == second_list["id"]
        date_sorted = await db_client.get("/api/v1/mileage-expenses?sort_by=trip_date")
        assert [item["id"] for item in date_sorted.json()["items"]] == [
            third_list["id"],
            second_list["id"],
            first_list["id"],
        ]

    async def test_trip_commit_failure_rolls_back_flushed_expense_and_mileage_delete(
        self,
        db_client: AsyncClient,
        db_session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _authenticate(db_client)
        setup = await _setup_company(db_client)
        company_id = uuid.UUID(setup["company"]["id"])  # type: ignore[index]
        original_apply = mileage_service._apply_trip_write

        def invalid_trip_write(*args: object) -> None:
            original_apply(*args)  # type: ignore[arg-type]
            args[0].transport_type_name = None  # type: ignore[attr-defined]

        monkeypatch.setattr(mileage_service, "_apply_trip_write", invalid_trip_write)
        async with db_session_maker() as session:
            with pytest.raises(IntegrityError):
                await mileage_service.create_mileage_expense(
                    session,
                    company_id,
                    MileageExpenseWrite(
                        trip_date=date(2026, 6, 15), one_way_distance_km=Decimal("1.000")
                    ),
                    creator_id=None,
                )
            assert (
                await session.execute(
                    select(func.count()).select_from(Expense).where(
                        Expense.company_id == company_id, Expense.kind == ExpenseKind.MILEAGE
                    )
                )
            ).scalar_one() == 0
            assert (
                await session.execute(
                    select(func.count()).select_from(MileageTrip).where(
                        MileageTrip.company_id == company_id
                    )
                )
            ).scalar_one() == 0

        monkeypatch.setattr(mileage_service, "_apply_trip_write", original_apply)
        trip = await _create_trip(db_client, one_way_distance_km="1.000")
        assert (await db_client.delete(f"/api/v1/mileage-expenses/{trip['id']}")).status_code == 204
        assert (await db_client.get(f"/api/v1/mileage-expenses/{trip['id']}")).status_code == 404
        assert (await db_client.get(f"/api/v1/expenses/{trip['expense_id']}")).status_code == 404

    async def test_calculate_crud_projection_and_generic_update_guard(
        self, db_client: AsyncClient
    ) -> None:
        await _authenticate(db_client)
        setup = await _setup_company(db_client)
        car = setup["types"]["Car"]  # type: ignore[index]

        calculate = await db_client.post(
            "/api/v1/mileage-expenses/calculate",
            json={
                "trip_date": "2026-06-15",
                "transport_type_id": car["id"],
                "one_way_distance_km": "12.500",
                "round_trip": True,
                "company_id": str(uuid.uuid4()),
            },
        )
        assert calculate.status_code == 200, calculate.text
        assert Decimal(calculate.json()["total_distance_km"]) == Decimal("25.000")
        assert Decimal(calculate.json()["amount"]) == Decimal("6.25")

        trip = await _create_trip(db_client, transport_type_id=car["id"])
        assert trip["expense_category_id"] == setup["defaults"]["expense_category_id"]  # type: ignore[index]
        assert trip["transport_type_name"] == "Car"
        assert Decimal(trip["amount"]) == Decimal("6.25")
        assert trip["origin_address"] == "Amsterdam"

        projection = await db_client.get(f"/api/v1/expenses/{trip['expense_id']}")
        assert projection.status_code == 200
        expense = projection.json()
        assert expense["kind"] == "MILEAGE"
        assert expense["currency"] == "EUR"
        assert Decimal(expense["net_amount"]) == Decimal("6.25")
        assert Decimal(expense["vat_amount"]) == Decimal("0")
        assert expense["deductible"] is False
        assert expense["paid_by"] == "PRIVATE"
        assert Decimal(expense["business_percentage"]) == Decimal("100")
        assert expense["depreciation_years"] == 1
        assert expense["vat_treatment_code"] == "NL_PRIVATE_TRANSPORT_MILEAGE"
        assert Decimal(expense["vat_rate_percent"]) == Decimal("0")

        alternative_category = await db_client.post(
            "/api/v1/expense-categories",
            json={"name": "Alternative mileage", "default_deductible": False},
        )
        assert alternative_category.status_code == 201
        changed_defaults = await db_client.put(
            "/api/v1/settings/mileage-defaults",
            json={
                "expense_category_id": alternative_category.json()["id"],
                "default_transport_type_id": car["id"],
            },
        )
        assert changed_defaults.status_code == 200

        generic_update = await db_client.put(
            f"/api/v1/expenses/{trip['expense_id']}",
            json={
                "expense_date": expense["expense_date"],
                "category_id": expense["category_id"],
                "vat_treatment_id": expense["vat_treatment_id"],
                "vat_rate_id": expense["vat_rate_id"],
                "net_amount": expense["net_amount"],
                "vat_amount": expense["vat_amount"],
            },
        )
        assert generic_update.status_code == 409

        updated = await db_client.put(
            f"/api/v1/mileage-expenses/{trip['id']}",
            json={
                "trip_date": "2025-06-15",
                "transport_type_id": car["id"],
                "one_way_distance_km": "10.000",
                "round_trip": False,
                "purpose": None,
                "origin_address": None,
                "destination_address": None,
                "note": None,
            },
        )
        assert updated.status_code == 200, updated.text
        assert Decimal(updated.json()["amount"]) == Decimal("2.30")
        assert updated.json()["purpose"] is None
        refreshed_projection = await db_client.get(f"/api/v1/expenses/{trip['expense_id']}")
        original_defaults = setup["defaults"]  # type: ignore[assignment]
        assert (
            refreshed_projection.json()["category_id"] == original_defaults["expense_category_id"]
        )

        listed = await db_client.get("/api/v1/mileage-expenses?q=Car&date_from=2025-01-01")
        assert listed.status_code == 200
        assert listed.json()["total"] == 1
        assert listed.json()["items"][0]["id"] == trip["id"]
        assert (await db_client.get(f"/api/v1/mileage-expenses/{trip['id']}")).status_code == 200

        purchase_rows = await db_client.get("/api/v1/expenses?kind=PURCHASE")
        mileage_rows = await db_client.get("/api/v1/expenses?kind=MILEAGE")
        assert purchase_rows.json()["total"] == 0
        assert mileage_rows.json()["total"] == 1

    async def test_type_rate_crud_historical_snapshot_and_expense_root_cascade(
        self,
        db_client: AsyncClient,
        db_session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _authenticate(db_client)
        await _setup_company(db_client)
        custom = await db_client.post(
            "/api/v1/mileage-transport-types", json={"name": "Electric car", "active": True}
        )
        assert custom.status_code == 201
        custom_type = custom.json()
        duplicate = await db_client.post(
            "/api/v1/mileage-transport-types", json={"name": "Electric car", "active": True}
        )
        assert duplicate.status_code == 409

        rate = await db_client.post(
            "/api/v1/mileage-rates",
            json={
                "transport_type_id": custom_type["id"],
                "effective_from": "2026-01-01",
                "rate_per_km": "0.300",
            },
        )
        assert rate.status_code == 201
        read_rate = await db_client.get(f"/api/v1/mileage-rates/{rate.json()['id']}")
        assert read_rate.status_code == 200
        updated_rate = await db_client.put(
            f"/api/v1/mileage-rates/{rate.json()['id']}",
            json={
                "transport_type_id": custom_type["id"],
                "effective_from": "2026-01-01",
                "rate_per_km": "0.300",
            },
        )
        assert updated_rate.status_code == 200
        duplicate_rate = await db_client.post(
            "/api/v1/mileage-rates",
            json={
                "transport_type_id": custom_type["id"],
                "effective_from": "2026-01-01",
                "rate_per_km": "0.310",
            },
        )
        assert duplicate_rate.status_code == 409

        trip = await _create_trip(
            db_client, transport_type_id=custom_type["id"], one_way_distance_km="10.000"
        )
        assert Decimal(trip["amount"]) == Decimal("6.00")

        deactivated = await db_client.put(
            f"/api/v1/mileage-transport-types/{custom_type['id']}",
            json={"name": "Electric car", "active": False},
        )
        assert deactivated.status_code == 200
        inactive_create = await db_client.post(
            "/api/v1/mileage-expenses",
            json={
                "trip_date": "2026-06-16",
                "transport_type_id": custom_type["id"],
                "one_way_distance_km": "1.000",
                "round_trip": False,
            },
        )
        assert inactive_create.status_code == 409
        assert (await db_client.get("/api/v1/mileage-expenses")).json()["total"] == 1
        assert (
            await db_client.get(f"/api/v1/mileage-transport-types/{uuid.uuid4()}")
        ).status_code == 404

        deleted_rate = await db_client.delete(f"/api/v1/mileage-rates/{rate.json()['id']}")
        assert deleted_rate.status_code == 204
        after_rate_delete = await db_client.get(f"/api/v1/mileage-expenses/{trip['id']}")
        assert after_rate_delete.status_code == 200
        assert after_rate_delete.json()["rate_rule_id"] is None
        assert Decimal(after_rate_delete.json()["rate_per_km"]) == Decimal("0.300")

        # Type deletion uses FK SET NULL on the trip and preserves its name/rate snapshots.
        deleted_type = await db_client.delete(
            f"/api/v1/mileage-transport-types/{custom_type['id']}"
        )
        assert deleted_type.status_code == 204, deleted_type.text
        historical = await db_client.get(f"/api/v1/mileage-expenses/{trip['id']}")
        assert historical.status_code == 200
        assert historical.json()["transport_type_id"] is None
        assert historical.json()["transport_type_name"] == "Electric car"
        assert Decimal(historical.json()["rate_per_km"]) == Decimal("0.300")

        # A root Expense delete must cascade the trip and any audit rows in the database.
        async with db_session_maker() as session:
            projection = await session.get(Expense, uuid.UUID(trip["expense_id"]))
            assert projection is not None
            session.add(
                MileageRateAdjustment(
                    company_id=projection.company_id,
                    trip_id=uuid.UUID(trip["id"]),
                    old_rate_effective_from=date(2026, 1, 1),
                    new_rate_effective_from=date(2026, 1, 1),
                    old_rate_per_km=Decimal("0.300"),
                    new_rate_per_km=Decimal("0.300"),
                    old_amount=Decimal("6.00"),
                    new_amount=Decimal("6.00"),
                )
            )
            await session.commit()
        assert (await db_client.delete(f"/api/v1/expenses/{trip['expense_id']}")).status_code == 204
        async with db_session_maker() as session:
            assert await session.get(MileageTrip, uuid.UUID(trip["id"])) is None
            assert (
                await session.execute(
                    select(MileageRateAdjustment).where(
                        MileageRateAdjustment.trip_id == uuid.UUID(trip["id"])
                    )
                )
            ).scalar_one_or_none() is None

    async def test_reports_include_mileage_but_btw_input_vat_stays_zero(
        self,
        db_client: AsyncClient,
        db_session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _authenticate(db_client)
        await _setup_company(db_client)
        await _create_taxable_purchase(db_client)
        before_mileage = await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=2")
        assert before_mileage.status_code == 200
        before_boxes = before_mileage.json()["boxes"]
        assert Decimal(before_boxes["box_5b"]["vat"]) == Decimal("21.00")
        trip = await _create_trip(db_client, one_way_distance_km="10.000")
        # The expense's category snapshot is mutable display data. P/L must
        # continue identifying this projection by Expense.kind.
        async with db_session_maker() as session:
            projection = await session.get(Expense, uuid.UUID(trip["expense_id"]))
            assert projection is not None
            projection.category_name = "Renamed travel category"
            await session.commit()

        expense_report = await db_client.get(
            "/api/v1/reports/expenses?from=2026-01-01&to=2026-12-31"
        )
        assert expense_report.status_code == 200
        mileage_row = next(
            row
            for row in expense_report.json()["by_category"]
            if row["category_name"] == "Renamed travel category"
        )
        assert Decimal(mileage_row["net"]) == Decimal("5.00")

        profit_loss = await db_client.get(
            "/api/v1/reports/profit-loss?from=2026-01-01&to=2026-12-31"
        )
        assert profit_loss.status_code == 200
        assert Decimal(profit_loss.json()["expense_actual"]) == Decimal("105.00")
        assert Decimal(profit_loss.json()["mileage_expense_actual"]) == Decimal("5.00")
        assert Decimal(profit_loss.json()["other_expense_actual"]) == Decimal("100.00")
        assert Decimal(profit_loss.json()["profit_before_mileage"]) == Decimal("-100.00")
        assert Decimal(profit_loss.json()["profit_after_mileage"]) == Decimal("-105.00")
        assert profit_loss.json()["profit"] == profit_loss.json()["profit_after_mileage"]
        dashboard = await db_client.get("/api/v1/reports/dashboard?year=2026")
        assert dashboard.status_code == 200
        assert Decimal(dashboard.json()["kpi"]["ytd_expense"]) == Decimal("105.00")
        assert Decimal(dashboard.json()["kpi"]["ytd_profit"]) == Decimal("-105.00")
        assert dashboard.json()["top_expense_categories"][0]["category_name"] == "Taxable baseline"

        btw = await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=2")
        assert btw.status_code == 200
        assert btw.json()["boxes"] == before_boxes

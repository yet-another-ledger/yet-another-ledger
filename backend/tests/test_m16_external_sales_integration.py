"""Runtime-role coverage for M16 Step 2 external sales drafts and originals."""

from __future__ import annotations

import asyncio
import uuid
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload
from starlette.datastructures import UploadFile

import jai.api.external_sales as external_sales_api
from jai.db import set_rls_company
from jai.models.external_sales import ExternalSale
from jai.services import external_sales
from jai.services.storage import Storage

pytestmark = pytest.mark.integration


class MemoryStorage(Storage):
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.fail_on_call: int | None = None
        self.calls = 0

    def save(self, namespace: str, key: str, content: bytes, content_type: str) -> str:
        del content_type
        storage_key = f"{namespace}/{key}"
        self.data[storage_key] = content
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise OSError("forced storage write failure")
        return storage_key

    def open(self, key: str) -> bytes:
        return self.data[key]

    def delete(self, key: str) -> None:
        self.data.pop(key, None)

    def exists(self, key: str) -> bool:
        return key in self.data


async def _owner_company(client: AsyncClient) -> tuple[dict, dict, dict, dict, dict]:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "m16-sales@example.com", "password": "testpassword1"},
    )
    assert response.status_code == 201
    response = await client.post(
        "/api/v1/auth/login", json={"email": "m16-sales@example.com", "password": "testpassword1"}
    )
    assert response.status_code == 200
    response = await client.post("/api/v1/auth/mfa/setup")
    assert response.status_code == 200
    response = await client.post(
        "/api/v1/auth/mfa/verify", json={"code": pyotp.TOTP(response.json()["secret"]).now()}
    )
    assert response.status_code == 204
    response = await client.put(
        "/api/v1/company",
        json={"name": "M16 External Sales", "base_currency": "EUR", "country_code": "NL"},
    )
    assert response.status_code == 200
    source_dictionary = await client.get("/api/v1/vat-treatments?side=SALES")
    rate_dictionary = await client.get("/api/v1/vat-rates")
    treatments = [
        row
        for row in source_dictionary.json()["items"]
        if row["active"] and row["code"] == "NL_DOMESTIC"
    ]
    eu_b2c = next(row for row in source_dictionary.json()["items"] if row["code"] == "EU_B2C")
    rates = [row for row in rate_dictionary.json()["items"] if row["active"]]
    assert treatments and len(rates) >= 2
    return response.json(), treatments[0], rates[0], rates[1], eu_b2c


@pytest.mark.asyncio
async def test_external_sales_runtime_drafts_defaults_duplicates_files_and_report_isolation(
    db_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = MemoryStorage()
    monkeypatch.setattr(external_sales_api, "get_storage", lambda: storage)
    monkeypatch.setattr(
        external_sales_api, "get_settings", lambda: SimpleNamespace(max_receipt_bytes=64)
    )
    company, treatment, rate_a, rate_b, eu_b2c = await _owner_company(db_client)
    assert company["country_code"] == "NL"

    platform = await db_client.post(
        "/api/v1/external-sales-sources", json={"name": "Market", "kind": "PLATFORM"}
    )
    energy = await db_client.post(
        "/api/v1/external-sales-sources", json={"name": "Energy", "kind": "ENERGY"}
    )
    other = await db_client.post(
        "/api/v1/external-sales-sources", json={"name": "Other", "kind": "OTHER"}
    )
    assert (platform.status_code, energy.status_code, other.status_code) == (201, 201, 201)
    platform_id, other_id = platform.json()["id"], other.json()["id"]
    assert platform.json()["default_include_in_pl"] is True
    assert energy.json()["default_include_in_pl"] is False
    source_page = await db_client.get("/api/v1/external-sales-sources?limit=2&offset=0")
    assert source_page.json()["total"] == 3
    assert len(source_page.json()["items"]) == 2

    pl_before = await db_client.get("/api/v1/reports/profit-loss?from=2026-01-01&to=2026-12-31")
    vat_before = await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=1")
    payload = {
        "source_id": platform_id,
        "original_number": "  EXT-2026/17  ",
        "document_date": "2026-02-15",
        "tax_summaries": [
            {
                "treatment_id": treatment["id"],
                "rate_id": rate_a["id"],
                "net_amount": "100.005",
                "vat_amount": "21.005",
            },
            {
                "treatment_id": treatment["id"],
                "rate_id": rate_b["id"],
                "net_amount": "50.00",
                "vat_amount": "4.50",
            },
        ],
    }
    created = await db_client.post("/api/v1/external-sales", json=payload)
    assert created.status_code == 201, created.text
    sale = created.json()
    assert sale["original_number"] == payload["original_number"]
    assert sale["include_in_pl"] is True
    assert sale["status"] == "PENDING"
    assert len(sale["tax_summaries"]) == 2
    assert sale["tax_summaries"][0]["rate_id"] == rate_a["id"]
    assert sale["tax_summaries"][0]["net_amount"] == "100.01"
    unsupported = await db_client.post(
        "/api/v1/external-sales",
        json={
            **payload,
            "original_number": "EU-B2C-OUT-OF-SCOPE",
            "tax_summaries": [
                {
                    "treatment_id": eu_b2c["id"],
                    "rate_id": rate_a["id"],
                    "net_amount": "10.00",
                    "vat_amount": "2.10",
                }
            ],
        },
    )
    assert unsupported.status_code == 422
    duplicate = await db_client.post(
        "/api/v1/external-sales", json={**payload, "original_number": "EXT-2026/17"}
    )
    assert duplicate.status_code == 409
    same_number_other_source = await db_client.post(
        "/api/v1/external-sales", json={**payload, "source_id": other_id}
    )
    assert same_number_other_source.status_code == 201
    incomplete = await db_client.post(
        "/api/v1/external-sales", json={"description": "Can remain incomplete"}
    )
    assert incomplete.status_code == 201
    assert incomplete.json()["source_id"] is None
    assert incomplete.json()["original_number"] is None

    source_disabled = await db_client.put(
        f"/api/v1/external-sales-sources/{platform_id}",
        json={"name": "Market", "kind": "PLATFORM", "default_include_in_pl": True, "active": False},
    )
    assert source_disabled.status_code == 200, source_disabled.text
    still_readable = await db_client.get(f"/api/v1/external-sales/{sale['id']}")
    assert still_readable.status_code == 200
    assert still_readable.json()["source_name"] == "Market"
    rejected_inactive = await db_client.post(
        "/api/v1/external-sales", json={"source_id": platform_id, "original_number": "NEXT"}
    )
    assert rejected_inactive.status_code == 404
    edited = await db_client.put(
        f"/api/v1/external-sales/{sale['id']}", json={**payload, "include_in_pl": False}
    )
    assert edited.status_code == 200
    assert edited.json()["include_in_pl"] is False
    assert (
        await db_client.get("/api/v1/reports/profit-loss?from=2026-01-01&to=2026-12-31")
    ).json() == pl_before.json()
    assert (
        await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=1")
    ).json() == vat_before.json()

    storage.fail_on_call = 2
    with pytest.raises(OSError, match="forced storage write failure"):
        await db_client.post(
            f"/api/v1/external-sales/{sale['id']}/files",
            files=[
                ("files", ("one.pdf", b"%PDF-one", "application/pdf")),
                ("files", ("two.pdf", b"%PDF-two", "application/pdf")),
            ],
        )
    assert storage.data == {}
    assert (await db_client.get(f"/api/v1/external-sales/{sale['id']}")).json()["files"] == []

    async def fail_commit(_session: AsyncSession) -> None:
        raise RuntimeError("forced external originals metadata commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="forced external originals metadata commit failure"):
            await db_client.post(
                f"/api/v1/external-sales/{sale['id']}/files",
                files=[("files", ("commit.pdf", b"%PDF-commit", "application/pdf"))],
            )
    assert storage.data == {}
    storage.fail_on_call = None
    storage.calls = 0

    pdf = b"%PDF-1.7\noriginal-bytes\n"
    upload = await db_client.post(
        f"/api/v1/external-sales/{sale['id']}/files",
        files=[("files", ("../../odd\r\nname.pdf", pdf, "application/pdf"))],
    )
    assert upload.status_code == 200, upload.text
    file_one = upload.json()["files"][0]
    assert file_one["filename"] == "../../odd%0D%0Aname.pdf"
    assert all("odd" not in key for key in storage.data)
    download = await db_client.get(f"/api/v1/external-sales/{sale['id']}/files/{file_one['id']}")
    assert download.status_code == 200 and download.content == pdf
    duplicate_upload = await db_client.post(
        f"/api/v1/external-sales/{sale['id']}/files",
        files=[("files", ("copy.pdf", pdf, "application/pdf"))],
    )
    assert duplicate_upload.status_code == 200
    assert duplicate_upload.json()["files"][1]["duplicate_warning"] is True
    invalid_magic = await db_client.post(
        f"/api/v1/external-sales/{sale['id']}/files",
        files=[("files", ("spoof.jpg", pdf, "image/jpeg"))],
    )
    assert invalid_magic.status_code == 422
    oversized = await db_client.post(
        f"/api/v1/external-sales/{sale['id']}/files",
        files=[("files", ("large.pdf", b"%PDF-" + b"x" * 100, "application/pdf"))],
    )
    assert oversized.status_code == 422
    too_many = await db_client.post(
        f"/api/v1/external-sales/{sale['id']}/files",
        files=[("files", (f"{i}.pdf", pdf, "application/pdf")) for i in range(4)],
    )
    assert too_many.status_code == 409
    remove = await db_client.delete(f"/api/v1/external-sales/{sale['id']}/files/{file_one['id']}")
    assert remove.status_code == 200
    assert (
        remove.json()["files"]
        == (await db_client.get(f"/api/v1/external-sales/{sale['id']}")).json()["files"]
    )
    assert len(remove.json()["files"]) == 1
    assert len(storage.data) == 1
    discarded = await db_client.delete(f"/api/v1/external-sales/{sale['id']}")
    assert discarded.status_code == 204
    assert storage.data == {}
    assert (await db_client.get(f"/api/v1/external-sales/{sale['id']}")).status_code == 404


@pytest.mark.asyncio
async def test_external_sales_runtime_rls_and_cross_company_child_ownership(
    db_session_maker: async_sessionmaker[AsyncSession],
    runtime_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    company_a, company_b = uuid.uuid4(), uuid.uuid4()
    source_id, sale_id = uuid.uuid4(), uuid.uuid4()
    async with db_session_maker() as admin:
        await admin.execute(
            text(
                "INSERT INTO company (id,name,base_currency) VALUES "
                "(:a,'M16 RLS A','EUR'),(:b,'M16 RLS B','EUR')"
            ),
            {"a": company_a, "b": company_b},
        )
        await admin.commit()

    async with runtime_session_maker() as runtime:
        await set_rls_company(runtime, company_a)
        await runtime.execute(
            text(
                "INSERT INTO external_sales_source "
                "(id,company_id,name,kind) VALUES (:id,:company_id,'Tenant A','OTHER')"
            ),
            {"id": source_id, "company_id": company_a},
        )
        await runtime.execute(
            text(
                "INSERT INTO external_sale (id,company_id,source_id,original_number) "
                "VALUES (:id,:company_id,:source_id,'A-1')"
            ),
            {"id": sale_id, "company_id": company_a, "source_id": source_id},
        )
        await runtime.execute(
            text(
                "INSERT INTO external_sale_tax (company_id,external_sale_id,net_amount) "
                "VALUES (:company_id,:sale_id,10)"
            ),
            {"company_id": company_a, "sale_id": sale_id},
        )
        await runtime.execute(
            text(
                "INSERT INTO external_sale_file "
                "(company_id,external_sale_id,storage_key,filename,mime_type,byte_size,sha256) "
                "VALUES (:company_id,:sale_id,'external-sales/safe-key','a.pdf',"
                "'application/pdf',3,:sha)"
            ),
            {"company_id": company_a, "sale_id": sale_id, "sha": "a" * 64},
        )
        await runtime.commit()

        await set_rls_company(runtime, company_b)
        for table in (
            "external_sales_source",
            "external_sale",
            "external_sale_tax",
            "external_sale_file",
            "external_sale_posting",
        ):
            assert await runtime.scalar(text(f"SELECT count(*) FROM {table}")) == 0
        with pytest.raises(DBAPIError):
            await runtime.execute(
                text(
                    "INSERT INTO external_sales_source "
                    "(company_id,name,kind) VALUES (:company_id,'Spoof','OTHER')"
                ),
                {"company_id": company_a},
            )
        await runtime.rollback()

        await set_rls_company(runtime, company_a)
        await runtime.execute(
            text(
                "INSERT INTO external_sale_posting "
                "(company_id,external_sale_id,version,snapshot) "
                "VALUES (:company_id,:sale_id,2,'{}'::jsonb)"
            ),
            {"company_id": company_a, "sale_id": sale_id},
        )
        await runtime.commit()
        with pytest.raises(DBAPIError):
            await runtime.execute(
                text(
                    "UPDATE external_sale_posting SET snapshot='{}'::jsonb "
                    "WHERE external_sale_id=:id"
                ),
                {"id": sale_id},
            )
        await runtime.rollback()
        with pytest.raises(DBAPIError):
            await runtime.execute(
                text("DELETE FROM external_sale_posting WHERE external_sale_id=:id"),
                {"id": sale_id},
            )
        await runtime.rollback()

        await set_rls_company(runtime, company_b)
        with pytest.raises(DBAPIError):
            await runtime.execute(
                text(
                    "INSERT INTO external_sale_file "
                    "(company_id,external_sale_id,storage_key,filename,mime_type,byte_size,sha256) "
                    "VALUES (:company_id,:sale_id,'spoof','spoof.pdf','application/pdf',3,:sha)"
                ),
                {"company_id": company_b, "sale_id": sale_id, "sha": "b" * 64},
            )
        await runtime.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("tax_selection", ["both", "treatment", "rate", "empty"])
async def test_external_sales_duplicate_update_is_409_and_rolls_back_tax_rows(
    db_client: AsyncClient, tax_selection: str
) -> None:
    company, treatment, rate, _, _ = await _owner_company(db_client)
    source = (
        await db_client.post(
            "/api/v1/external-sales-sources", json={"name": "Duplicate updates", "kind": "OTHER"}
        )
    ).json()
    taxes = (
        []
        if tax_selection == "empty"
        else [
            {
                "treatment_id": treatment["id"] if tax_selection in {"both", "treatment"} else None,
                "rate_id": rate["id"] if tax_selection in {"both", "rate"} else None,
                "net_amount": "10",
                "vat_amount": "2.10",
            }
        ]
    )
    payload = {"source_id": source["id"], "tax_summaries": taxes}
    assert (
        await db_client.post("/api/v1/external-sales", json={**payload, "original_number": "A"})
    ).status_code == 201
    sale = (
        await db_client.post("/api/v1/external-sales", json={**payload, "original_number": "B"})
    ).json()
    url = f"/api/v1/external-sales/{sale['id']}"
    updated = await db_client.put(url, json={**payload, "original_number": "C"})
    assert updated.status_code == 200
    before = (await db_client.get(url)).json()
    duplicate = await db_client.put(url, json={**payload, "original_number": "  A  "})
    assert duplicate.status_code == 409
    assert duplicate.json() == {
        "detail": {
            "code": "DUPLICATE_SOURCE_NUMBER",
            "detail": "DUPLICATE_SOURCE_NUMBER",
            "external_sale_id": None,
            "current_version": None,
        }
    }
    after = (await db_client.get(url)).json()
    assert after == before


@pytest.mark.asyncio
async def test_external_sales_remove_file_updates_full_capacity_response(
    db_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = MemoryStorage()
    monkeypatch.setattr(external_sales_api, "get_storage", lambda: storage)
    await _owner_company(db_client)
    sale = (await db_client.post("/api/v1/external-sales", json={})).json()
    url = f"/api/v1/external-sales/{sale['id']}"
    files = [("files", (f"{i}.pdf", b"%PDF-safe", "application/pdf")) for i in range(5)]
    full = await db_client.post(f"{url}/files", files=files)
    assert full.status_code == 200 and len(full.json()["files"]) == 5
    originals = full.json()["files"]
    removed = await db_client.delete(f"{url}/files/{originals[0]['id']}")
    assert removed.status_code == 200
    assert len(removed.json()["files"]) == 4
    assert removed.json()["files"] == (await db_client.get(url)).json()["files"]
    assert (await db_client.post(f"{url}/files", files=files[:1])).status_code == 200


@pytest.mark.asyncio
async def test_external_sale_preview_acknowledged_posting_and_reports_once(
    db_client: AsyncClient,
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = MemoryStorage()
    monkeypatch.setattr(external_sales_api, "get_storage", lambda: storage)
    company, treatment, rate, _, _ = await _owner_company(db_client)
    source = (
        await db_client.post(
            "/api/v1/external-sales-sources", json={"name": "Test platform", "kind": "PLATFORM"}
        )
    ).json()
    rate_pct = Decimal(rate["percent"])
    net = Decimal("100.01")
    derived = (net * rate_pct / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    original_vat = derived + Decimal("0.01")
    second_net = Decimal("10.00")
    second_derived = (second_net * rate_pct / Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    second_vat = second_derived - Decimal("0.01")
    original_vat_total = original_vat + second_vat
    net_total = net + second_net
    created = await db_client.post(
        "/api/v1/external-sales",
        json={
            "source_id": source["id"],
            "original_number": "  PLATFORM-42  ",
            "document_date": "2026-02-15",
            "include_in_pl": True,
            "tax_summaries": [
                {
                    "treatment_id": treatment["id"],
                    "rate_id": rate["id"],
                    "net_amount": str(net),
                    "vat_amount": str(original_vat),
                },
                {
                    "treatment_id": treatment["id"],
                    "rate_id": rate["id"],
                    "net_amount": str(second_net),
                    "vat_amount": str(second_vat),
                },
            ],
        },
    )
    assert created.status_code == 201, created.text
    sale = created.json()
    sale_url = f"/api/v1/external-sales/{sale['id']}"
    no_original = await db_client.post(
        f"{sale_url}/calculate", json={"expected_version": sale["version"]}
    )
    assert no_original.status_code == 422
    uploaded = await db_client.post(
        f"{sale_url}/files",
        files=[("files", ("original.pdf", b"%PDF-original", "application/pdf"))],
    )
    assert uploaded.status_code == 200, uploaded.text
    sale = uploaded.json()
    assert sale["version"] > 1

    before_pl = (
        await db_client.get("/api/v1/reports/profit-loss?from=2026-01-01&to=2026-12-31")
    ).json()
    preview = await db_client.post(
        f"{sale_url}/calculate", json={"expected_version": sale["version"]}
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["mismatch"] is True
    assert Decimal(preview.json()["original_vat_total"]) == original_vat_total
    assert preview.json()["mismatch_row_indices"] == [0, 1]
    stale_version = sale["version"]
    appended = await db_client.post(
        f"{sale_url}/files",
        files=[("files", ("second.pdf", b"%PDF-second", "application/pdf"))],
    )
    assert appended.status_code == 200
    stale_confirm = await db_client.post(
        f"{sale_url}/confirm",
        json={"expected_version": stale_version},
    )
    assert stale_confirm.status_code == 409
    assert stale_confirm.json()["detail"]["code"] == "EXTERNAL_SALE_VERSION_CONFLICT"
    assert stale_confirm.json()["detail"]["external_sale_id"] == sale["id"]
    assert stale_confirm.json()["detail"]["current_version"] == appended.json()["version"]
    sale = appended.json()
    preview = await db_client.post(
        f"{sale_url}/calculate", json={"expected_version": sale["version"]}
    )
    assert preview.status_code == 200
    no_ack = await db_client.post(f"{sale_url}/confirm", json={"expected_version": sale["version"]})
    assert no_ack.status_code == 422

    async def fail_posting_commit(_session: AsyncSession) -> None:
        raise RuntimeError("forced external sale posting commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", fail_posting_commit)
        with pytest.raises(RuntimeError, match="forced external sale posting commit failure"):
            await db_client.post(
                f"{sale_url}/confirm",
                json={
                    "expected_version": sale["version"],
                    "acknowledge_vat_mismatch": True,
                    "mismatch_ack_reason": "Commit rollback check.",
                },
            )
    after_failed_commit = (await db_client.get(sale_url)).json()
    assert after_failed_commit["status"] == "PENDING"
    assert after_failed_commit["postings"] == []
    confirm_payload = {
        "expected_version": sale["version"],
        "acknowledge_vat_mismatch": True,
        "mismatch_ack_reason": "The original document shows the retained VAT amount.",
    }
    confirmation, concurrent_confirmation = await asyncio.gather(
        db_client.post(f"{sale_url}/confirm", json=confirm_payload),
        db_client.post(f"{sale_url}/confirm", json=confirm_payload),
    )
    statuses = sorted([confirmation.status_code, concurrent_confirmation.status_code])
    assert statuses == [200, 409], (confirmation.text, concurrent_confirmation.text)
    if confirmation.status_code == 409:
        confirmation, concurrent_confirmation = concurrent_confirmation, confirmation
    assert concurrent_confirmation.json()["detail"]["code"] == "EXTERNAL_SALE_ALREADY_POSTED"
    posted = confirmation.json()
    assert posted["status"] == "POSTED"
    assert Decimal(posted["original_net_total"]) == net_total
    assert Decimal(posted["original_vat_total"]) == original_vat_total
    assert posted["mismatch_ack_reason"]
    assert len(posted["postings"]) == 1
    assert (await db_client.put(sale_url, json={})).status_code == 409
    assert (await db_client.delete(sale_url)).status_code == 409
    retained_file = posted["files"][0]
    assert (await db_client.delete(f"{sale_url}/files/{retained_file['id']}")).status_code == 409
    retained_download = await db_client.get(f"{sale_url}/files/{retained_file['id']}")
    assert retained_download.status_code == 200
    assert retained_download.content == b"%PDF-original"

    conflict = await db_client.post(
        f"{sale_url}/confirm",
        json={
            "expected_version": sale["version"],
            "acknowledge_vat_mismatch": True,
            "mismatch_ack_reason": "retry",
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "EXTERNAL_SALE_ALREADY_POSTED"
    assert conflict.json()["detail"]["external_sale_id"] == sale["id"]

    for failure in (FileNotFoundError("offline"), None):
        broken_open = Mock(side_effect=failure, return_value=b"corrupted original")
        with monkeypatch.context() as patch:
            patch.setattr(storage, "open", broken_open)
            for command in ("calculate", "confirm"):
                for expected_version in (sale["version"], posted["version"]):
                    retry = await db_client.post(
                        f"{sale_url}/{command}", json={"expected_version": expected_version}
                    )
                    assert retry.status_code == 409, retry.text
                    assert retry.json()["detail"] == {
                        "code": "EXTERNAL_SALE_ALREADY_POSTED",
                        "detail": "EXTERNAL_SALE_ALREADY_POSTED",
                        "external_sale_id": posted["id"],
                        "current_version": posted["version"],
                    }
            broken_open.assert_not_called()

    after_pl = (
        await db_client.get("/api/v1/reports/profit-loss?from=2026-01-01&to=2026-12-31")
    ).json()
    assert Decimal(after_pl["revenue_net"]) - Decimal(before_pl["revenue_net"]) == net_total
    assert after_pl["external_sales"][0]["external_sale_id"] == sale["id"]
    vat = (await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=1")).json()
    assert (
        Decimal(vat["boxes"]["box_1a"]["vat"] if rate_pct == 21 else vat["boxes"]["box_1b"]["vat"])
        == original_vat_total
    )
    event = next(row for row in vat["event_rows"] if row["external_sale_id"] == sale["id"])
    assert event["source_type"] == "EXTERNAL_SALE"
    assert event["document_id"] is None and event["document_kind"] is None
    assert event["external_original_number"] == "  PLATFORM-42  "
    assert (await db_client.get("/api/v1/reports/dashboard?year=2026")).json()["kpi"][
        "ytd_revenue"
    ] == after_pl["revenue_net"]

    async with db_session_maker() as admin:
        await set_rls_company(admin, uuid.UUID(company["id"]))
        count = await admin.scalar(
            text("SELECT count(*) FROM external_sale_posting WHERE external_sale_id=:id"),
            {"id": uuid.UUID(sale["id"])},
        )
        assert count == 1

    energy = (
        await db_client.post(
            "/api/v1/external-sales-sources", json={"name": "Energy company", "kind": "ENERGY"}
        )
    ).json()
    energy_net = Decimal("25.00")
    energy_vat = (energy_net * rate_pct / Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    energy_sale = (
        await db_client.post(
            "/api/v1/external-sales",
            json={
                "source_id": energy["id"],
                "original_number": "ENERGY-1",
                "document_date": "2026-02-20",
                "tax_summaries": [
                    {
                        "treatment_id": treatment["id"],
                        "rate_id": rate["id"],
                        "net_amount": str(energy_net),
                        "vat_amount": str(energy_vat),
                    }
                ],
            },
        )
    ).json()
    assert energy_sale["include_in_pl"] is False
    energy_url = f"/api/v1/external-sales/{energy_sale['id']}"
    energy_sale = (
        await db_client.post(
            f"{energy_url}/files",
            files=[("files", ("energy.pdf", b"%PDF-energy", "application/pdf"))],
        )
    ).json()
    assert (
        await db_client.post(
            f"{energy_url}/calculate", json={"expected_version": energy_sale["version"]}
        )
    ).status_code == 200
    energy_post = await db_client.post(
        f"{energy_url}/confirm", json={"expected_version": energy_sale["version"]}
    )
    assert energy_post.status_code == 200, energy_post.text
    pl_after_energy = (
        await db_client.get("/api/v1/reports/profit-loss?from=2026-01-01&to=2026-12-31")
    ).json()
    assert pl_after_energy["revenue_net"] == after_pl["revenue_net"]
    vat_after_energy = (
        await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=1")
    ).json()
    assert (
        Decimal(
            vat_after_energy["boxes"]["box_1a"]["vat"]
            if rate_pct == 21
            else vat_after_energy["boxes"]["box_1b"]["vat"]
        )
        == original_vat_total + energy_vat
    )
    assert (await db_client.get("/api/v1/reports/dashboard?year=2026")).json()["kpi"][
        "ytd_revenue"
    ] == after_pl["revenue_net"]


@pytest.mark.asyncio
async def test_external_sales_remove_file_cleanup_failure_keeps_successful_metadata_result(
    db_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    storage = MemoryStorage()
    monkeypatch.setattr(external_sales_api, "get_storage", lambda: storage)
    await _owner_company(db_client)
    sale = (await db_client.post("/api/v1/external-sales", json={})).json()
    url = f"/api/v1/external-sales/{sale['id']}"
    uploaded = await db_client.post(
        f"{url}/files", files=[("files", ("safe.pdf", b"%PDF-safe", "application/pdf"))]
    )
    assert uploaded.status_code == 200
    file_id = uploaded.json()["files"][0]["id"]

    def fail_delete(_key: str) -> None:
        raise OSError("forced original cleanup failure")

    with monkeypatch.context() as patch:
        patch.setattr(storage, "delete", fail_delete)
        cleaned = await db_client.delete(f"{url}/files/{file_id}")
    assert cleaned.status_code == 200
    assert cleaned.json()["files"] == []
    assert cleaned.json()["files"] == (await db_client.get(url)).json()["files"]
    assert "Could not remove external sale original" in caplog.text
    assert "forced original cleanup failure" in caplog.text
    assert len(storage.data) == 1  # Failed cleanup is logged; committed metadata stays removed.
    assert (await db_client.delete(f"{url}/files/{file_id}")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["missing", "cross_company"])
@pytest.mark.parametrize("file_count", [1, 6])
@pytest.mark.parametrize("valid_file", [False, True])
async def test_external_sales_upload_ownership_precedes_read_and_file_validation(
    db_client: AsyncClient,
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    file_count: int,
    valid_file: bool,
) -> None:
    await _owner_company(db_client)
    sale_id = uuid.uuid4()
    if target == "cross_company":
        async with db_session_maker() as session:
            company_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO company (id,name,base_currency) VALUES (:id,'Other tenant','EUR')"
                ),
                {"id": company_id},
            )
            await set_rls_company(session, company_id)
            await session.execute(
                text("INSERT INTO external_sale (id,company_id) VALUES (:id,:company)"),
                {"id": sale_id, "company": company_id},
            )
            await session.commit()
    read = AsyncMock(side_effect=AssertionError("Must reject before reading file bytes"))
    monkeypatch.setattr(UploadFile, "read", read)
    storage = MemoryStorage()
    monkeypatch.setattr(external_sales_api, "get_storage", lambda: storage)
    response = await db_client.post(
        f"/api/v1/external-sales/{sale_id}/files",
        files=[
            (
                "files",
                (f"{i}.pdf", b"%PDF-safe", "application/pdf")
                if valid_file
                else (f"{i}.txt", b"invalid", "text/plain"),
            )
            for i in range(file_count)
        ],
    )
    assert response.status_code == 404
    read.assert_not_awaited()
    assert storage.calls == 0


@pytest.mark.asyncio
async def test_external_sales_upload_preflight_capacity_and_locked_recheck(
    db_client: AsyncClient,
    db_session_maker: async_sessionmaker[AsyncSession],
    runtime_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = MemoryStorage()
    monkeypatch.setattr(external_sales_api, "get_storage", lambda: storage)
    company, _, _, _, _ = await _owner_company(db_client)
    sale = (await db_client.post("/api/v1/external-sales", json={})).json()
    sale_id, company_id = uuid.UUID(sale["id"]), uuid.UUID(company["id"])
    url = f"/api/v1/external-sales/{sale_id}/files"
    parts = [(b"%PDF-safe", "application/pdf", "safe.pdf")]
    async with db_session_maker() as session:
        await set_rls_company(session, company_id)
        creator_id = await session.scalar(
            text("SELECT creator_id FROM external_sale WHERE id=:id"), {"id": sale_id}
        )
    assert creator_id is not None
    uploaded = await db_client.post(
        url, files=[("files", (f"{i}.pdf", b"%PDF-safe", "application/pdf")) for i in range(4)]
    )
    assert uploaded.status_code == 200
    async with runtime_session_maker() as first, runtime_session_maker() as second:
        await external_sales.check_append_capacity(first, company_id, sale_id, 1)
        cached = await first.scalar(
            select(ExternalSale)
            .options(selectinload(ExternalSale.files))
            .where(ExternalSale.id == sale_id)
        )
        assert cached is not None and len(cached.files) == 4
        await external_sales.append_files(
            second, company_id, creator_id, sale_id, parts, 64, storage
        )
        with pytest.raises(external_sales.ExternalSalesConflict, match="FILE_LIMIT_REACHED"):
            await external_sales.append_files(
                first, company_id, creator_id, sale_id, parts, 64, storage
            )
    assert storage.calls == 5
    read = AsyncMock(side_effect=AssertionError("Full entries must reject before file reads"))
    with monkeypatch.context() as patch:
        patch.setattr(UploadFile, "read", read)
        full = await db_client.post(url, files=[("files", ("bad.txt", b"bad", "text/plain"))])
    assert full.status_code == 409
    assert full.json()["detail"]["code"] == "FILE_LIMIT_REACHED"
    assert full.json()["detail"]["external_sale_id"] == str(sale_id)
    assert full.json()["detail"]["current_version"] == 3
    read.assert_not_awaited()
    with monkeypatch.context() as patch:
        patch.setattr(UploadFile, "read", read)
        invalid_count = await db_client.post(
            url, files=[("files", ("bad.txt", b"bad", "text/plain")) for _ in range(6)]
        )
    assert invalid_count.status_code == 422
    read.assert_not_awaited()
    # Direct service callers also reject invisible targets before validating their bytes.
    async with runtime_session_maker() as session:
        with pytest.raises(LookupError):
            await external_sales.append_files(
                session,
                company_id,
                creator_id,
                uuid.uuid4(),
                [(b"bad", "text/plain", None)],
                64,
                storage,
            )


@pytest.mark.asyncio
async def test_external_sale_zero_rate_validation_preserves_draft_and_reconciles_reports(
    db_client: AsyncClient,
    runtime_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject unsupported originals before posting; corrected selection retains its VAT."""
    import jai.services.reporting.dashboard as dashboard

    monkeypatch.setattr(dashboard, "_select_quarter", lambda _year: 1)
    storage = MemoryStorage()
    monkeypatch.setattr(external_sales_api, "get_storage", lambda: storage)
    company, treatment, _, _, _ = await _owner_company(db_client)
    rates = (await db_client.get("/api/v1/vat-rates")).json()["items"]
    zero = next(rate for rate in rates if Decimal(rate["percent"]) == 0)
    high = next(rate for rate in rates if Decimal(rate["percent"]) == 21)
    source = (
        await db_client.post(
            "/api/v1/external-sales-sources", json={"name": "Rate review", "kind": "PLATFORM"}
        )
    ).json()
    payload = {
        "source_id": source["id"],
        "original_number": "ZERO-ORIGINAL",
        "document_date": "2026-03-31",
        "include_in_pl": True,
        "tax_summaries": [
            {
                "treatment_id": treatment["id"],
                "rate_id": zero["id"],
                "net_amount": "100.00",
                "vat_amount": "21.01",
            }
        ],
    }
    draft = (await db_client.post("/api/v1/external-sales", json=payload)).json()
    url = f"/api/v1/external-sales/{draft['id']}"
    draft = (
        await db_client.post(
            f"{url}/files", files=[("files", ("original.pdf", b"%PDF-zero", "application/pdf"))]
        )
    ).json()
    originals = dict(storage.data)
    for command in ("calculate", "confirm"):
        body = {"expected_version": draft["version"]}
        if command == "confirm":
            body.update(acknowledge_vat_mismatch=True, mismatch_ack_reason="Original checked.")
        rejected = await db_client.post(f"{url}/{command}", json=body)
        assert rejected.status_code == 422, rejected.text
        assert "zero VAT rate" in rejected.json()["detail"]
        assert "review the original document" in rejected.json()["detail"]
    assert (await db_client.get(url)).json() == draft
    assert storage.data == originals
    async with runtime_session_maker() as runtime:
        assert await runtime.scalar(text("SELECT current_user")) == "jai_app"
        await set_rls_company(runtime, uuid.UUID(company["id"]))
        assert await runtime.scalar(text("SELECT count(*) FROM external_sale_posting")) == 0
    vat_url = "/api/v1/reports/vat-return?year=2026&quarter=1"
    pl_url = "/api/v1/reports/profit-loss?from=2026-01-01&to=2026-03-31"
    dashboard_url = "/api/v1/reports/dashboard?year=2026"
    before_vat = (await db_client.get(vat_url)).json()
    assert before_vat["event_rows"] == []
    assert Decimal(before_vat["totals"]["output_vat_total"]["vat"]) == 0
    assert Decimal((await db_client.get(pl_url)).json()["revenue_net"]) == 0
    assert (
        Decimal((await db_client.get(dashboard_url)).json()["kpi"]["current_quarter_vat_payable"])
        == 0
    )

    # Correct the selected rate, never the original amount. A real mismatch still needs a reason.
    payload["tax_summaries"][0]["rate_id"] = high["id"]
    updated = await db_client.put(url, json=payload)
    assert updated.status_code == 200, updated.text
    draft = (await db_client.get(url)).json()
    for failure in (FileNotFoundError("offline"), None):
        with monkeypatch.context() as patch:
            patch.setattr(storage, "open", Mock(side_effect=failure, return_value=b"corrupt"))
            for command in ("calculate", "confirm"):
                unavailable = await db_client.post(
                    f"{url}/{command}", json={"expected_version": draft["version"]}
                )
                assert unavailable.status_code == 422, unavailable.text
                assert "original file" in unavailable.json()["detail"]
    assert (await db_client.get(url)).json() == draft
    preview = await db_client.post(f"{url}/calculate", json={"expected_version": draft["version"]})
    assert preview.status_code == 200 and preview.json()["mismatch"] is True
    assert Decimal(preview.json()["original_vat_total"]) == Decimal("21.01")
    assert (
        await db_client.post(f"{url}/confirm", json={"expected_version": draft["version"]})
    ).status_code == 422
    posted = await db_client.post(
        f"{url}/confirm",
        json={
            "expected_version": draft["version"],
            "acknowledge_vat_mismatch": True,
            "mismatch_ack_reason": "Verified 21% selection against the retained original.",
        },
    )
    assert posted.status_code == 200, posted.text

    # A supported 0%/zero-VAT original still posts to 1e (base only).
    payload["original_number"] = "ZERO-VALID"
    payload["tax_summaries"][0].update(rate_id=zero["id"], net_amount="25.00", vat_amount="0.00")
    zero_sale = (await db_client.post("/api/v1/external-sales", json=payload)).json()
    zero_url = f"/api/v1/external-sales/{zero_sale['id']}"
    zero_sale = (
        await db_client.post(
            f"{zero_url}/files", files=[("files", ("zero.pdf", b"%PDF-valid", "application/pdf"))]
        )
    ).json()
    assert (
        await db_client.post(
            f"{zero_url}/calculate", json={"expected_version": zero_sale["version"]}
        )
    ).status_code == 200
    assert (
        await db_client.post(f"{zero_url}/confirm", json={"expected_version": zero_sale["version"]})
    ).status_code == 200
    vat = (await db_client.get(vat_url)).json()
    events_vat = sum(Decimal(row["vat_amount"]) for row in vat["event_rows"])
    assert events_vat == Decimal("21.01")
    assert Decimal(vat["boxes"]["box_1a"]["vat"]) == events_vat
    assert Decimal(vat["boxes"]["box_1e"]["base"]) == Decimal("25.00")
    assert Decimal(vat["totals"]["output_vat_total"]["vat"]) == events_vat
    pl = (await db_client.get(pl_url)).json()
    assert Decimal(pl["revenue_net"]) == Decimal("125.00")
    summary = (await db_client.get(dashboard_url)).json()
    assert summary["kpi"]["ytd_revenue"] == pl["revenue_net"]
    assert (
        summary["kpi"]["current_quarter_vat_payable"]
        == vat["totals"]["net_payable_or_refundable"]["vat"]
    )
    q2 = (await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=2")).json()
    assert q2["event_rows"] == []

    saved = (await db_client.get(url)).json()
    renamed_rate = await db_client.put(
        f"/api/v1/vat-rates/{high['id']}",
        json={"label": "Later dictionary rate", "percent": "9", "active": False},
    )
    assert renamed_rate.status_code == 200, renamed_rate.text
    assert (await db_client.get(url)).json() == saved
    assert (await db_client.get(vat_url)).json() == vat
    assert (await db_client.get(pl_url)).json() == pl
    assert (await db_client.get(dashboard_url)).json() == summary


@pytest.mark.asyncio
@pytest.mark.parametrize("storage_failure", ["missing", "corrupt"])
async def test_external_sale_posting_conflicts_precede_storage_access(
    db_client: AsyncClient,
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    storage_failure: str,
) -> None:
    storage = MemoryStorage()
    monkeypatch.setattr(external_sales_api, "get_storage", lambda: storage)
    await _owner_company(db_client)
    # No complete business inputs needed: state/version must precede both input and storage checks.
    draft = (await db_client.post("/api/v1/external-sales", json={})).json()
    url = f"/api/v1/external-sales/{draft['id']}"
    stale_version = draft["version"]
    draft = (
        await db_client.post(
            f"{url}/files", files=[("files", ("original.pdf", b"%PDF-safe", "application/pdf"))]
        )
    ).json()
    broken_open = (
        Mock(side_effect=FileNotFoundError("offline"))
        if storage_failure == "missing"
        else Mock(return_value=b"corrupt")
    )
    monkeypatch.setattr(storage, "open", broken_open)
    for command in ("calculate", "confirm"):
        conflict = await db_client.post(
            f"{url}/{command}", json={"expected_version": stale_version}
        )
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["detail"] == {
            "code": "EXTERNAL_SALE_VERSION_CONFLICT",
            "detail": "EXTERNAL_SALE_VERSION_CONFLICT",
            "external_sale_id": draft["id"],
            "current_version": draft["version"],
        }
    broken_open.assert_not_called()

    # A different tenant's existing identity remains 404, even with stale/invalid fields.
    async with db_session_maker() as admin:
        other_id, other_sale_id = uuid.uuid4(), uuid.uuid4()
        await admin.execute(
            text("INSERT INTO company (id,name,base_currency) VALUES (:id,'Other','EUR')"),
            {"id": other_id},
        )
        await set_rls_company(admin, other_id)
        await admin.execute(
            text("INSERT INTO external_sale (id,company_id) VALUES (:id,:company)"),
            {"id": other_sale_id, "company": other_id},
        )
        await admin.commit()
    for command in ("calculate", "confirm"):
        assert (
            await db_client.post(
                f"/api/v1/external-sales/{other_sale_id}/{command}", json={"expected_version": 99}
            )
        ).status_code == 404
    broken_open.assert_not_called()


@pytest.mark.asyncio
async def test_fractional_external_rate_and_configured_zero_tier_report_without_vat_loss(
    db_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jai.services.reporting.dashboard as dashboard

    monkeypatch.setattr(dashboard, "_select_quarter", lambda _year: 1)
    storage = MemoryStorage()
    monkeypatch.setattr(external_sales_api, "get_storage", lambda: storage)
    _, treatment, _, _, _ = await _owner_company(db_client)
    source = (
        await db_client.post(
            "/api/v1/external-sales-sources", json={"name": "Decimal rates", "kind": "PLATFORM"}
        )
    ).json()
    configured = await db_client.put(
        "/api/v1/settings/vat-rate-tiers", json={"hoog": 21, "laag": 9, "zero": 3}
    )
    assert configured.status_code == 200, configured.text
    for percent, original_vat in [("0.5", "0.51"), ("3", "3.00")]:
        rate = await db_client.post(
            "/api/v1/vat-rates", json={"label": f"Custom {percent}", "percent": percent}
        )
        assert rate.status_code == 201, rate.text
        created = await db_client.post(
            "/api/v1/external-sales",
            json={
                "source_id": source["id"],
                "original_number": f"RATE-{percent}",
                "document_date": "2026-03-31",
                "include_in_pl": True,
                "tax_summaries": [
                    {
                        "treatment_id": treatment["id"],
                        "rate_id": rate.json()["id"],
                        "net_amount": "100.00",
                        "vat_amount": original_vat,
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        url = f"/api/v1/external-sales/{created.json()['id']}"
        draft = (
            await db_client.post(
                f"{url}/files", files=[("files", ("rate.pdf", b"%PDF-rate", "application/pdf"))]
            )
        ).json()
        preview = await db_client.post(
            f"{url}/calculate", json={"expected_version": draft["version"]}
        )
        confirmation = await db_client.post(
            f"{url}/confirm",
            json={
                "expected_version": draft["version"],
                "acknowledge_vat_mismatch": True,
                "mismatch_ack_reason": "Reviewed the original fractional-rate VAT.",
            },
        )
        if percent == "3":
            assert preview.status_code == confirmation.status_code == 422
            assert "configured zero VAT rate tier" in preview.json()["detail"]
            assert "configured zero VAT rate tier" in confirmation.json()["detail"]
            assert (await db_client.get(url)).json() == draft
        else:
            assert preview.status_code == confirmation.status_code == 200, confirmation.text
            assert preview.json()["mismatch"] is True
            assert Decimal(preview.json()["derived_vat_total"]) == Decimal("0.50")
            assert Decimal(confirmation.json()["original_vat_total"]) == Decimal("0.51")
            assert confirmation.json()["mismatch_ack_reason"]
    vat = (await db_client.get("/api/v1/reports/vat-return?year=2026&quarter=1")).json()
    assert len(vat["event_rows"]) == 1
    assert Decimal(vat["event_rows"][0]["vat_rate_percent"]) == Decimal("0.5")
    assert Decimal(vat["event_rows"][0]["vat_amount"]) == Decimal("0.51")
    assert Decimal(vat["boxes"]["box_1c"]["base"]) == Decimal("100.00")
    assert Decimal(vat["boxes"]["box_1c"]["vat"]) == Decimal("0.51")
    assert Decimal(vat["boxes"]["box_1e"]["base"]) == 0
    assert Decimal(vat["totals"]["output_vat_total"]["vat"]) == Decimal("0.51")
    summary = (await db_client.get("/api/v1/reports/dashboard?year=2026")).json()
    assert Decimal(summary["kpi"]["current_quarter_vat_payable"]) == Decimal("0.51")
    assert Decimal(summary["kpi"]["ytd_revenue"]) == Decimal("100.00")

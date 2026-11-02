"""SI1 regression: ordered arrays, existing pricing, PDF output and conversion."""

from decimal import Decimal
from typing import Any

import pypdfium2
import pytest
from httpx import AsyncClient
from test_estimate_generate_quote_integration import _full_auth, _setup_company

from jai.services import pdf

pytestmark = pytest.mark.integration


def _assert_names(document: dict[str, Any], names: list[str]) -> None:
    assert [line["name"] for line in document["lines"]] == names
    assert [line["sort_order"] for line in document["lines"]] == list(range(len(names)))


def _pdf_text(content: bytes) -> str:
    document = pypdfium2.PdfDocument(content)
    try:
        parts = []
        for page in document:
            try:
                text_page = page.get_textpage()
                try:
                    parts.append(text_page.get_text_range())
                finally:
                    text_page.close()
            finally:
                page.close()
        return "\n".join(parts)
    finally:
        document.close()


@pytest.mark.parametrize("kind", ["invoices", "quotes"])
async def test_order_save_reload_pricing_pdf_and_quote_conversion(
    db_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    await _full_auth(db_client)
    seed = await _setup_company(db_client)
    rates = seed["rates"]
    payload = {
        "customer_id": seed["nl_customer"]["id"],
        "invoice_date" if kind == "invoices" else "quote_date": "2026-10-25",
        "tax_mode": "LINE",
        "discount": {"type": "FIXED", "value": "0.01"},
        "lines": [
            {"name": name, "quantity": "1", "unit_price": "0.333", "vat_rate_id": rate}
            for name, rate in [
                ("OrderAlpha", rates["NL standard (21%)"]["id"]),
                ("OrderBeta", rates["NL reduced (9%)"]["id"]),
                ("OrderGamma", rates["NL standard (21%)"]["id"]),
            ]
        ],
    }
    response = await db_client.post(f"/api/v1/{kind}", json=payload)
    assert response.status_code == 201, response.text
    document_id = response.json()["id"]
    _assert_names(response.json(), ["OrderAlpha", "OrderBeta", "OrderGamma"])
    payload["lines"] = [payload["lines"][2], payload["lines"][0], payload["lines"][1]]
    for expected in (
        ["OrderGamma", "OrderAlpha", "OrderBeta"], ["OrderBeta", "OrderAlpha", "OrderGamma"]
    ):
        if expected[0] == "OrderBeta":
            payload["lines"] = list(reversed(payload["lines"]))
        preview = await db_client.post(f"/api/v1/{kind}/calculate", json=payload)
        assert preview.status_code == 200, preview.text
        saved = await db_client.put(f"/api/v1/{kind}/{document_id}", json=payload)
        assert saved.status_code == 200, saved.text
        read = await db_client.get(f"/api/v1/{kind}/{document_id}")
        assert read.status_code == 200
        _assert_names(read.json(), expected)
        # Compare the existing engine for this order, including final-line remainder.
        for field in (
            "subtotal_excl_vat", "document_discount_amount", "vat_total", "total_incl_vat"
        ):
            assert Decimal(read.json()[field]) == Decimal(preview.json()[field])
        for stored, calculated in zip(read.json()["lines"], preview.json()["lines"], strict=True):
            for field in (
                "document_discount_share", "taxable_amount", "vat_total", "total_incl_vat"
            ):
                assert Decimal(stored[field]) == Decimal(calculated[field])
        assert sum(
            Decimal(line["document_discount_share"]) for line in read.json()["lines"]
        ) == Decimal("0.01")

    rendered_html: list[str] = []
    original_render = pdf.html_to_pdf

    def capture_html(html: str) -> bytes:
        rendered_html.append(html)
        return original_render(html)

    monkeypatch.setattr(pdf, "html_to_pdf", capture_html)
    output = await db_client.get(
        f"/api/v1/{kind}/{document_id}/pdf?locale=en&preview=true"
    )
    assert output.status_code == 200, output.text
    assert output.content.startswith(b"%PDF")
    for text in (rendered_html[-1], _pdf_text(output.content)):
        assert text.index("OrderBeta") < text.index("OrderAlpha") < text.index("OrderGamma")

    invalid = {**payload, "lines": [{**payload["lines"][0], "quantity": "-1"}]}
    failed = await db_client.put(f"/api/v1/{kind}/{document_id}", json=invalid)
    assert failed.status_code == 422
    _assert_names((await db_client.get(f"/api/v1/{kind}/{document_id}")).json(), expected)

    if kind == "quotes":
        sent = await db_client.post(
            f"/api/v1/quotes/{document_id}/status", json={"status": "SENT"}
        )
        assert sent.status_code == 200, sent.text
        converted = await db_client.post(f"/api/v1/quotes/{document_id}/convert")
        assert converted.status_code == 201, converted.text
        _assert_names(converted.json(), expected)
        converted_pdf = await db_client.get(
            f"/api/v1/invoices/{converted.json()['id']}/pdf?locale=en&preview=true"
        )
        assert converted_pdf.status_code == 200, converted_pdf.text
        text = _pdf_text(converted_pdf.content)
        assert text.index("OrderBeta") < text.index("OrderAlpha") < text.index("OrderGamma")


async def test_estimate_order_preserves_membership_and_generated_public_groups(
    db_client: AsyncClient,
) -> None:
    await _full_auth(db_client)
    seed = await _setup_company(db_client)
    rate = seed["rates"]["NL standard (21%)"]["id"]
    payload = {
        "name": "Internal ordering", "customer_id": seed["nl_customer"]["id"],
        "groups": [
            {"ref": ref, "public_description": name, "vat_rate_id": rate, "sort_order": i}
            for i, (ref, name) in enumerate([("a", "PublicFirst"), ("b", "PublicSecond")])
        ],
        "lines": [
            {"name": name, "unit_cost_excl_vat": price, "quantity": "1",
             "margin_rate": "0.2", "group_ref": ref}
            for name, price, ref in [
                ("InternalAlpha", "0.333", "a"), ("InternalBeta", "10", "b"),
                ("InternalGamma", "5", "a"),
            ]
        ],
    }
    created = await db_client.post("/api/v1/estimates", json=payload)
    assert created.status_code == 201, created.text
    estimate_id = created.json()["id"]
    payload["lines"] = list(reversed(payload["lines"]))
    saved = await db_client.put(f"/api/v1/estimates/{estimate_id}", json=payload)
    assert saved.status_code == 200, saved.text
    reloaded = (await db_client.get(f"/api/v1/estimates/{estimate_id}")).json()
    _assert_names(reloaded, ["InternalGamma", "InternalBeta", "InternalAlpha"])
    group_names = {g["id"]: g["public_description"] for g in reloaded["groups"]}
    assert list(group_names.values()) == ["PublicFirst", "PublicSecond"]
    assert [group_names[line["group_id"]] for line in reloaded["lines"]] == [
        "PublicFirst", "PublicSecond", "PublicFirst"
    ]
    for field in ("total_excl_vat", "total_margin"):
        assert Decimal(reloaded[field]) == Decimal(created.json()[field])
    generated = await db_client.post(f"/api/v1/estimates/{estimate_id}/generate-quote")
    assert generated.status_code == 201, generated.text
    _assert_names(generated.json(), ["PublicFirst", "PublicSecond"])
    assert "Internal" not in str(generated.json()["lines"])

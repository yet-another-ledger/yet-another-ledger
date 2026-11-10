"""Focused Step 2 contract and preview-signature coverage."""

from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import date
from decimal import Decimal

from jai.schemas.agent_token import AgentTokenScope
from jai.schemas.agent_tools import AgentMileageSource, AgentMileageWrite, PreviewMileageInput
from jai.services import agent_mileage
from jai.services.agent_mileage import _preview_token, verify_mileage_preview_token


def _payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": 1,
        "company_id": uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        "token_id": uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        "request_id": uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc"),
        "source": {"platform": "test", "chat_id": None, "message_id": None},
        "mileage": {
            "trip_date": date(2026, 11, 10),
            "transport_type_id": None,
            "one_way_distance_km": Decimal("10.000"),
            "round_trip": True,
            "origin_address": None,
            "destination_address": None,
            "purpose": "meeting",
            "note": None,
        },
        "facts": {
            "transport_type": {"id": uuid.uuid4(), "name": "Car"},
            "rate": {
                "id": uuid.uuid4(),
                "transport_type_id": None,
                "effective_from": date(2026, 1, 1),
                "rate_per_km": Decimal("0.250"),
            },
            "total_distance_km": Decimal("20.000"),
            "amount": Decimal("5.00"),
            "currency": "EUR",
            "category": {"id": uuid.uuid4(), "name": "Mileage"},
            "vat_treatment": {
                "id": uuid.uuid4(),
                "code": "NL_PRIVATE_TRANSPORT_MILEAGE",
                "label": "Private transport mileage",
                "side": "PURCHASE",
                "effect": "EXEMPT",
            },
            "vat_rate": {
                "id": uuid.uuid4(),
                "label": "Zero",
                "percent": Decimal("0.000"),
                "active": True,
            },
        },
    }
    payload.update(changes)
    return payload


def test_mileage_create_is_explicit_typed_scope_and_input_is_closed() -> None:
    assert AgentTokenScope("mileage:create") is AgentTokenScope.MILEAGE_CREATE
    try:
        PreviewMileageInput.model_validate(
            {
                "request_id": str(uuid.uuid4()),
                "mileage": {
                    "trip_date": "2026-11-10",
                    "one_way_distance_km": "10.000",
                    "unexpected": True,
                },
            }
        )
    except ValueError:
        pass
    else:
        raise AssertionError("Nested unknown mileage input must be rejected")

    try:
        PreviewMileageInput.model_validate(
            {
                "request_id": str(uuid.uuid4()),
                "mileage": {
                    "trip_date": "2026-11-10",
                    "one_way_distance_km": "10.000",
                },
                "unexpected": True,
            }
        )
    except ValueError:
        pass
    else:
        raise AssertionError("MCP root unknown fields must be rejected")


def test_mileage_and_source_text_reject_nul_without_changing_shared_contracts() -> None:
    mileage_fields = ("origin_address", "destination_address", "purpose", "note")
    for field in mileage_fields:
        try:
            AgentMileageWrite.model_validate(
                {
                    "trip_date": "2026-11-10",
                    "one_way_distance_km": "10.000",
                    field: "before\u0000after",
                }
            )
        except ValueError:
            pass
        else:
            raise AssertionError(f"Mileage field {field} accepted NUL")

    for field in ("platform", "chat_id", "message_id"):
        try:
            AgentMileageSource.model_validate(
                {"platform": "test", field: "before\u0000after"}
            )
        except ValueError:
            pass
        else:
            raise AssertionError(f"Source field {field} accepted NUL")

    # Other M15 MCP tools continue to use the unchanged shared source schema.
    from jai.schemas.agent_tools import AgentSource

    assert AgentSource.model_validate({"platform": "test", "chat_id": "a\u0000b"})


def test_mileage_preview_hmac_is_domain_version_and_canonical_value_bound() -> None:
    payload = _payload()
    first = _preview_token(payload)
    assert first.startswith("m18v1.")
    assert verify_mileage_preview_token(first, payload)
    assert not verify_mileage_preview_token("m18v1." + "0" * 64, payload)
    assert not verify_mileage_preview_token("invalid", payload)
    equivalent = deepcopy(payload)
    # Change only numeric exponent/scale.
    equivalent["mileage"]["one_way_distance_km"] = Decimal("10.0")
    equivalent["facts"]["rate"]["rate_per_km"] = Decimal("0.25")
    assert _preview_token(equivalent) == first
    assert verify_mileage_preview_token(first, equivalent)

    changes: list[tuple[tuple[str, ...], object]] = [
        (("company_id",), uuid.uuid4()),
        (("token_id",), uuid.uuid4()),
        (("request_id",), uuid.uuid4()),
        (("source", "platform"), "other"),
        (("mileage", "trip_date"), date(2026, 11, 11)),
        (("mileage", "transport_type_id"), uuid.uuid4()),
        (("mileage", "one_way_distance_km"), Decimal("10.001")),
        (("mileage", "round_trip"), False),
        (("mileage", "origin_address"), "A"),
        (("mileage", "destination_address"), "B"),
        (("mileage", "purpose"), "changed"),
        (("mileage", "note"), "changed"),
        (("facts", "transport_type", "id"), uuid.uuid4()),
        (("facts", "transport_type", "name"), "Motorcycle"),
        (("facts", "rate", "id"), uuid.uuid4()),
        (("facts", "rate", "transport_type_id"), uuid.uuid4()),
        (("facts", "rate", "effective_from"), date(2026, 2, 1)),
        (("facts", "rate", "rate_per_km"), Decimal("0.251")),
        (("facts", "total_distance_km",), Decimal("20.001")),
        (("facts", "amount"), Decimal("5.01")),
        (("facts", "currency"), "USD"),
        (("facts", "category", "id"), uuid.uuid4()),
        (("facts", "category", "name"), "Travel"),
        (("facts", "vat_treatment", "id"), uuid.uuid4()),
        (("facts", "vat_treatment", "code"), "OTHER"),
        (("facts", "vat_treatment", "label"), "Other"),
        (("facts", "vat_treatment", "side"), "SALES"),
        (("facts", "vat_treatment", "effect"), "APPLY_RATE"),
        (("facts", "vat_rate", "id"), uuid.uuid4()),
        (("facts", "vat_rate", "label"), "Other"),
        (("facts", "vat_rate", "percent"), Decimal("9.000")),
        (("facts", "vat_rate", "active"), False),
    ]
    for path, value in changes:
        altered = deepcopy(payload)
        target = altered
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value
        assert _preview_token(altered) != first


def test_mileage_preview_verifier_rejects_rotated_signing_secret(
    monkeypatch,
) -> None:
    payload = _payload()
    monkeypatch.setattr(agent_mileage, "get_auth_secret", lambda: "before-rotation")
    token = _preview_token(payload)
    assert verify_mileage_preview_token(token, payload)
    monkeypatch.setattr(agent_mileage, "get_auth_secret", lambda: "after-rotation")
    assert not verify_mileage_preview_token(token, payload)

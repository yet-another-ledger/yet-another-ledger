"""Coverage for the checked-in Hermes-side deterministic request-ID helper."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

HELPER_PATH = (
    Path(__file__).resolve().parents[2] / "integrations" / "hermes" / "jai-ledger" / "request_id.py"
)
SPEC = importlib.util.spec_from_file_location("jai_hermes_request_id", HELPER_PATH)
assert SPEC is not None and SPEC.loader is not None
request_ids = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(request_ids)


def test_message_retry_is_stable_and_operation_suffix_changes_id() -> None:
    kwargs = {
        "chat_id": "123456789",
        "user_id": "123456789",
        "message_id": "41",
    }
    first = request_ids.telegram_request_id(**kwargs, operation="submit_receipt")
    retry = request_ids.telegram_request_id(**kwargs, operation="submit_receipt")

    assert first == retry
    assert first != request_ids.telegram_request_id(**kwargs, operation="create_quote_draft")


def test_album_updates_emit_identical_full_request_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_TYPE": "dm",
        "HERMES_SESSION_CHAT_ID": "123456789",
        "HERMES_SESSION_USER_ID": "123456789",
        "HERMES_SESSION_MEDIA_GROUP_ID": "album-abc-123",
        "HERMES_SESSION_MEDIA_GROUP_ANCHOR_MESSAGE_ID": "41",
        "HERMES_SESSION_MEDIA_GROUP_COMPLETE": "true",
        "HERMES_SESSION_MEDIA_GROUP_MESSAGE_IDS": '["41","42"]',
    }.items():
        monkeypatch.setenv(name, value)

    # Telegram delivers separate updates with different current message IDs.
    monkeypatch.setenv("HERMES_SESSION_MESSAGE_ID", "41")
    first = request_ids.request_metadata_from_hermes_env("submit_receipt")
    monkeypatch.setenv("HERMES_SESSION_MESSAGE_ID", "42")
    retry = request_ids.request_metadata_from_hermes_env("submit_receipt")

    def canonical_submit_arguments(metadata: dict[str, object]) -> dict[str, object]:
        return {
            "request_id": metadata["request_id"],
            "source": metadata["source"],
            "language": "en",
            "files": [
                {
                    "filename": "receipt-41.jpg",
                    "mime_type": "image/jpeg",
                    "content_base64": "cGhvdG8tNDE=",
                },
                {
                    "filename": "receipt-42.pdf",
                    "mime_type": "application/pdf",
                    "content_base64": "cGRmLTQy",
                },
            ],
        }

    # Compare every canonical argument, not only the UUID. Two different real
    # update IDs for one group must produce the same source and ordered files.
    assert canonical_submit_arguments(first) == canonical_submit_arguments(retry)
    assert first["source"] == {
        "platform": "telegram",
        "chat_id": "123456789",
        "message_id": "41",
    }
    assert first["media_group"] == {
        "id": "album-abc-123",
        "anchor_message_id": "41",
        "ordered_message_ids": ["41", "42"],
    }
    assert first["request_id"] == str(
        request_ids.telegram_request_id(
            chat_id="123456789",
            user_id="123456789",
            message_id="41",
            operation="submit_receipt",
            media_group_id="album-abc-123",
            media_group_anchor_message_id="41",
        )
    )


def test_album_metadata_fails_closed_without_complete_trusted_aggregator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_TYPE": "dm",
        "HERMES_SESSION_CHAT_ID": "123456789",
        "HERMES_SESSION_USER_ID": "123456789",
        "HERMES_SESSION_MESSAGE_ID": "41",
        "HERMES_SESSION_MEDIA_GROUP_ID": "album-abc-123",
        "HERMES_SESSION_MEDIA_GROUP_ANCHOR_MESSAGE_ID": "41",
        "HERMES_SESSION_MEDIA_GROUP_COMPLETE": "false",
        "HERMES_SESSION_MEDIA_GROUP_MESSAGE_IDS": '["41","42"]',
    }.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match="complete album"):
        request_ids.request_metadata_from_hermes_env("submit_receipt")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("chat_id", "123abc"),
        ("chat_id", "987654321"),
        ("user_id", "-123"),
        ("user_id", "0123"),
        ("message_id", "0"),
        ("message_id", "1.5"),
        ("operation", "SubmitReceipt"),
        ("media_group_id", ""),
    ],
)
def test_rejects_invalid_identity_or_operation(field: str, value: str) -> None:
    kwargs = {
        "chat_id": "123456789",
        "user_id": "123456789",
        "message_id": "41",
        "operation": "submit_receipt",
        "media_group_id": None,
    }
    kwargs[field] = value

    with pytest.raises(ValueError):
        request_ids.telegram_request_id(**kwargs)


def test_environment_requires_real_private_telegram_coordinates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_TYPE": "dm",
        "HERMES_SESSION_CHAT_ID": "123456789",
        "HERMES_SESSION_USER_ID": "123456789",
        "HERMES_SESSION_MESSAGE_ID": "41",
    }.items():
        monkeypatch.setenv(name, value)

    original = request_ids.request_metadata_from_hermes_env("submit_receipt")
    assert original == request_ids.request_metadata_from_hermes_env("submit_receipt")

    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", "group")
    with pytest.raises(ValueError, match="private-chat"):
        request_ids.request_metadata_from_hermes_env("submit_receipt")

    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", "private")
    with pytest.raises(ValueError, match="private-chat"):
        request_ids.request_metadata_from_hermes_env("submit_receipt")


def test_cli_missing_arguments_returns_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", [str(HELPER_PATH)])

    assert request_ids.main() == 2

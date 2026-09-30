#!/usr/bin/env python3
"""Print stable JAI request metadata from trusted Hermes session metadata.

The Telegram gateway supplies chat/user/message identity to subprocesses through
HERMES_SESSION_* variables. Album support requires all four
HERMES_SESSION_MEDIA_GROUP_* values documented below; stock Hermes does not
currently bind them.
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid

_NAMESPACE = uuid.UUID("f0a73e90-9705-5bfa-b521-5ad87969e803")
_OPERATION = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_POSITIVE_DECIMAL = re.compile(r"[1-9][0-9]{0,19}\Z")


def telegram_request_id(
    *,
    chat_id: str,
    user_id: str,
    message_id: str,
    operation: str,
    media_group_id: str | None = None,
    media_group_anchor_message_id: str | None = None,
) -> uuid.UUID:
    """Derive the same UUID for a retried message and operation."""
    if not _POSITIVE_DECIMAL.fullmatch(chat_id):
        raise ValueError("Telegram private chat ID must be a positive numeric ID.")
    if not _POSITIVE_DECIMAL.fullmatch(user_id):
        raise ValueError("Telegram user ID must be a positive numeric ID.")
    if chat_id != user_id:
        raise ValueError("Telegram private chat and sender IDs must match.")
    if not _POSITIVE_DECIMAL.fullmatch(message_id):
        raise ValueError("Telegram message ID must be a positive numeric ID.")
    if not _OPERATION.fullmatch(operation):
        raise ValueError("Operation must be a lowercase identifier.")
    if media_group_id is not None and (
        not media_group_id or len(media_group_id) > 128 or not media_group_id.isascii()
    ):
        raise ValueError("Telegram media group ID must be non-empty ASCII metadata.")
    if media_group_id is not None and (
        media_group_anchor_message_id is None
        or not _POSITIVE_DECIMAL.fullmatch(media_group_anchor_message_id)
    ):
        raise ValueError("A trusted stable album anchor message ID is required.")
    if media_group_id is None and media_group_anchor_message_id is not None:
        raise ValueError("An album anchor requires a media group ID.")

    # Namespace and length-prefix each field so delimiters inside provider IDs
    # cannot create ambiguous keys. In an album, only the actual group ID is a
    # valid shared identity; callers must not substitute the first message ID.
    event_kind = "media-group" if media_group_id is not None else "message"
    event_id = media_group_id if media_group_id is not None else message_id
    fields = ("jai-ledger", "telegram", chat_id, user_id, event_kind, event_id, operation)
    name = "".join(f"{len(field)}:{field}" for field in fields)
    return uuid.uuid5(_NAMESPACE, name)


def request_metadata_from_hermes_env(operation: str) -> dict[str, object]:
    """Read trusted identity and emit the request ID with its exact source."""
    platform = os.environ.get("HERMES_SESSION_PLATFORM", "")
    chat_type = os.environ.get("HERMES_SESSION_CHAT_TYPE", "")
    # Hermes normalizes Telegram's Bot API `private` type to SessionSource `dm`.
    if platform.lower() != "telegram" or chat_type.lower() != "dm":
        raise ValueError("A trusted Telegram private-chat session is required.")
    chat_id = os.environ.get("HERMES_SESSION_CHAT_ID", "")
    user_id = os.environ.get("HERMES_SESSION_USER_ID", "")
    message_id = os.environ.get("HERMES_SESSION_MESSAGE_ID", "")
    media_group_id = os.environ.get("HERMES_SESSION_MEDIA_GROUP_ID") or None
    result: dict[str, object] = {
        "source": {"platform": "telegram", "chat_id": chat_id, "message_id": message_id}
    }
    if media_group_id is not None:
        # These are custom trusted-aggregator exports, not stock Hermes fields.
        anchor_id = os.environ.get("HERMES_SESSION_MEDIA_GROUP_ANCHOR_MESSAGE_ID", "")
        complete = os.environ.get("HERMES_SESSION_MEDIA_GROUP_COMPLETE", "")
        raw_message_ids = os.environ.get("HERMES_SESSION_MEDIA_GROUP_MESSAGE_IDS", "")
        if complete.lower() != "true":
            raise ValueError("Trusted aggregator must confirm the complete album attachment set.")
        try:
            message_ids = json.loads(raw_message_ids)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "Trusted aggregator must provide ordered album message IDs as JSON."
            ) from exc
        if (
            not isinstance(message_ids, list)
            or not message_ids
            or any(
                not isinstance(item, str) or not _POSITIVE_DECIMAL.fullmatch(item)
                for item in message_ids
            )
            or len(set(message_ids)) != len(message_ids)
            or message_id not in message_ids
            or anchor_id not in message_ids
        ):
            raise ValueError("Trusted aggregator album message IDs are invalid or incomplete.")
        result["source"] = {
            "platform": "telegram",
            "chat_id": chat_id,
            "message_id": anchor_id,
        }
        result["media_group"] = {
            "id": media_group_id,
            "anchor_message_id": anchor_id,
            "ordered_message_ids": message_ids,
        }
    request_id = telegram_request_id(
        chat_id=chat_id,
        user_id=user_id,
        message_id=message_id,
        operation=operation,
        media_group_id=media_group_id,
        media_group_anchor_message_id=(
            os.environ.get("HERMES_SESSION_MEDIA_GROUP_ANCHOR_MESSAGE_ID")
            if media_group_id is not None
            else None
        ),
    )
    result["request_id"] = str(request_id)
    return result


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: request_id.py <operation>", file=sys.stderr)
        return 2
    try:
        print(json.dumps(request_metadata_from_hermes_env(sys.argv[1]), separators=(",", ":")))
    except ValueError as exc:
        print(f"Cannot derive request ID: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

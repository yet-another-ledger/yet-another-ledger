"""Three-layer settings service with typed access and in-process cache.

Public API
----------
- ``get_setting`` – retrieve a typed setting with ``user → company → global``
  fallback.  The caller provides the starting *level* and *scope_id* manually.
- ``get_effective_setting`` – convenience wrapper that resolves the correct
  scope IDs for each layer from a logged-in ``User`` object (``USER(user.id)
  → COMPANY(user.company_id) → GLOBAL``).
- ``set_setting`` – persist a Pydantic model as the setting value (upsert).

Design (red-line 5)
-------------------
All values go through Pydantic validation.  No raw ``'YES'/'NO'`` string
checks in calling code — the typed schemas guarantee the correct Python
types (``bool``, ``int``, nested models, …).

Cache
-----
An in-process ``dict`` cache stores *already-parsed* Pydantic objects keyed
by ``(level, scope_id, key)``.  Writes invalidate the affected entry so
subsequent reads always reflect the latest value.
"""

from __future__ import annotations

import uuid
from typing import Any, overload

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from jai.models._enums import SettingLevel
from jai.models.setting import Setting

# Avoid circular import: ``User`` is only used as a type hint for the
# convenience wrapper.  Import at runtime inside the function body.
from jai.models.user import User

# Fallback order: most specific first.
_FALLBACK_ORDER: list[SettingLevel] = [
    SettingLevel.USER,
    SettingLevel.COMPANY,
    SettingLevel.GLOBAL,
]

# In-process cache: (level, scope_id, key) → parsed Pydantic object.
# ``None`` is never cached (absent settings return ``None``).
_cache: dict[tuple[SettingLevel, uuid.UUID | None, str], Any] = {}


def _cache_key(
    level: SettingLevel,
    scope_id: uuid.UUID | None,
    key: str,
) -> tuple[SettingLevel, uuid.UUID | None, str]:
    return (level, scope_id, key)


@overload
async def get_setting[T: BaseModel](
    session: AsyncSession,
    key: str,
    *,
    level: SettingLevel,
    scope_id: uuid.UUID | None = None,
    value_type: type[T],
    _scope_map: dict[SettingLevel, uuid.UUID | None] | None = None,
    bypass_cache: bool = False,
) -> T | None: ...


@overload
async def get_setting(
    session: AsyncSession,
    key: str,
    *,
    level: SettingLevel,
    scope_id: uuid.UUID | None = None,
    value_type: None = None,
    _scope_map: dict[SettingLevel, uuid.UUID | None] | None = None,
    bypass_cache: bool = False,
) -> dict[str, Any] | None: ...


async def get_setting[T: BaseModel](
    session: AsyncSession,
    key: str,
    *,
    level: SettingLevel,
    scope_id: uuid.UUID | None = None,
    value_type: type[T] | None = None,
    _scope_map: dict[SettingLevel, uuid.UUID | None] | None = None,
    bypass_cache: bool = False,
) -> T | dict[str, Any] | None:
    """Retrieve a typed setting, with level-based fallback.

    When *level* is ``USER`` or ``COMPANY``, the function walks the fallback
    chain (``USER → COMPANY → GLOBAL``) until a value is found.  When
    *level* is ``GLOBAL``, only the global slot is checked.

    Parameters
    ----------
    session:
        Database session.
    key:
        Setting key (e.g. ``"onboarding.completed"``).
    level:
        Most-specific level to start the lookup from.
    scope_id:
        Entity UUID for the *starting* level; ``None`` for ``GLOBAL``.
    value_type:
        Pydantic model class to parse the raw JSONB dict into.  If ``None``,
        the raw dict is returned.
    _scope_map:
        Optional explicit mapping ``{SettingLevel: scope_id}`` used during
        fallback.  When provided, each probed level uses its mapped scope ID.
        When absent, ``scope_id`` is used only for the starting level and
        ``None`` for all deeper levels (i.e. falls straight to GLOBAL).
    bypass_cache:
        When true, read every candidate from the database and do not cache the
        result. This is useful when a caller needs transaction-fresh settings
        and must not expose an uncommitted value to other sessions.

    Returns
    -------
    Parsed Pydantic model instance or ``None`` if no setting found.
    """
    # Determine which levels to probe (in fallback order).
    start_idx = _FALLBACK_ORDER.index(level)
    levels_to_check = _FALLBACK_ORDER[start_idx:]

    for lvl in levels_to_check:
        # Resolve scope_id for this level.
        if _scope_map is not None and lvl in _scope_map:
            sid = _scope_map[lvl]
        elif lvl == level:
            sid = scope_id
        else:
            sid = None

        if not bypass_cache:
            cache_hit = _cache.get(_cache_key(lvl, sid, key))
            if cache_hit is not None:
                return cache_hit  # type: ignore[no-any-return]

        row = await _fetch_row(session, lvl, sid, key)
        if row is not None:
            raw: dict[str, Any] = row
            parsed = value_type.model_validate(raw) if value_type is not None else raw
            if not bypass_cache:
                _cache[_cache_key(lvl, sid, key)] = parsed
            return parsed

    return None


@overload
async def get_effective_setting[T: BaseModel](
    session: AsyncSession,
    key: str,
    *,
    user: User,
    value_type: type[T],
) -> T | None: ...


@overload
async def get_effective_setting(
    session: AsyncSession,
    key: str,
    *,
    user: User,
    value_type: None = None,
) -> dict[str, Any] | None: ...


async def get_effective_setting[T: BaseModel](
    session: AsyncSession,
    key: str,
    *,
    user: User,
    value_type: type[T] | None = None,
) -> T | dict[str, Any] | None:
    """Convenience wrapper that resolves scope IDs from a ``User`` object.

    Probes ``USER(user.id) → COMPANY(user.company_id) → GLOBAL`` and returns
    the first hit.  This is the preferred entry point for API routes that
    have access to the authenticated user.

    Parameters
    ----------
    session:
        Database session.
    key:
        Setting key.
    user:
        The authenticated ``User`` ORM instance (must have ``company_id``
        populated for COMPANY-level lookup).
    value_type:
        Pydantic model class to parse the raw JSONB dict into.

    Returns
    -------
    Parsed Pydantic model instance or ``None`` if no setting found.
    """
    scope_map: dict[SettingLevel, uuid.UUID | None] = {
        SettingLevel.USER: user.id,
        SettingLevel.COMPANY: user.company_id,
        SettingLevel.GLOBAL: None,
    }
    return await get_setting(
        session,
        key,
        level=SettingLevel.USER,
        value_type=value_type,
        _scope_map=scope_map,
    )


async def set_setting(
    session: AsyncSession,
    key: str,
    value: BaseModel,
    *,
    level: SettingLevel,
    scope_id: uuid.UUID | None = None,
) -> None:
    """Persist a typed setting value (upsert).

    Uses PostgreSQL ``INSERT … ON CONFLICT … DO UPDATE`` so that the unique
    constraint ``(level, scope_id, key)`` is never violated.

    The in-process cache entry is invalidated immediately so that subsequent
    ``get_setting`` calls will load the fresh value.

    Parameters
    ----------
    session:
        Database session.
    key:
        Setting key.
    value:
        A Pydantic ``BaseModel`` instance; its JSON-mode ``model_dump()`` is
        stored as JSONB so UUIDs and other typed values remain serializable.
    level:
        Setting hierarchy level.
    scope_id:
        Entity UUID for non-global levels.

    Raises
    ------
    ValueError
        If ``level``/``scope_id`` combination is invalid.
    """
    _validate_level_scope(level, scope_id)
    raw = value.model_dump(mode="json")
    stmt = pg_insert(Setting).values(
        level=level,
        scope_id=scope_id,
        key=key,
        value=raw,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["level", "scope_id", "key"],
        set_={"value": stmt.excluded.value, "updated_at": func.now()},
    )
    await session.execute(stmt)
    await session.flush()

    # Invalidate cache for this slot.
    _cache.pop(_cache_key(level, scope_id, key), None)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_level_scope(level: SettingLevel, scope_id: uuid.UUID | None) -> None:
    """Ensure ``level``/``scope_id`` combination is valid.

    Rules (mirrors the DB CHECK constraint ``chk_setting_level_scope``):
    - ``GLOBAL``  → ``scope_id`` must be ``None``.
    - ``COMPANY`` / ``USER`` → ``scope_id`` must not be ``None``.
    """
    if level == SettingLevel.GLOBAL:
        if scope_id is not None:
            msg = f"GLOBAL settings must have scope_id=None, got {scope_id}"
            raise ValueError(msg)
    elif scope_id is None:
        msg = f"{level.value} settings require a scope_id"
        raise ValueError(msg)


async def _fetch_row(
    session: AsyncSession,
    level: SettingLevel,
    scope_id: uuid.UUID | None,
    key: str,
) -> dict[str, Any] | None:
    """Load a single setting row's ``value`` column."""
    stmt = select(Setting.value).where(
        Setting.level == level,
        Setting.scope_id == scope_id if scope_id is not None else Setting.scope_id.is_(None),  # noqa: E501
        Setting.key == key,
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    return row

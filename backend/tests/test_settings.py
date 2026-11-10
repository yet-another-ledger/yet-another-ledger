"""Tests for ``jai.services.settings`` – typed access, fallback, and caching.

All tests use mocks — no running PostgreSQL required.  The service logic
(fallback priority, cache, type parsing, upsert) is verified without
touching a real database.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from jai.models._enums import SettingLevel
from jai.schemas.setting import (
    SETTING_KEY_ONBOARDING_COMPLETED,
    SETTING_KEY_USER_PREFERENCES,
    OnboardingState,
    UserPreferences,
)
from jai.services.settings import (
    _cache,
    _cache_key,
    get_effective_setting,
    get_setting,
    set_setting,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_cache() -> Generator[None, None, None]:
    """Clear the settings cache before and after each test."""
    _cache.clear()
    yield
    _cache.clear()


def _make_fetch_mock(
    return_map: dict[tuple[SettingLevel, object], dict[str, object]],
) -> AsyncMock:
    """Build an async mock for ``_fetch_row`` that returns values from a map.

    *return_map* keys are ``(level, scope_id)`` tuples; values are dicts
    (the JSONB payload).
    """

    async def _fetch(
        session: object,  # noqa: ARG001
        level: SettingLevel,
        scope_id: object,  # noqa: ARG001
        key: str,  # noqa: ARG001
    ) -> dict[str, object] | None:
        return return_map.get((level, scope_id))

    return AsyncMock(side_effect=_fetch)


def _make_user_mock(
    user_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
) -> AsyncMock:
    """Build a mock User object with id and company_id."""
    user = AsyncMock()
    user.id = user_id or uuid.uuid4()
    user.company_id = company_id
    return user


# ---------------------------------------------------------------------------
# Basic typed read
# ---------------------------------------------------------------------------


class TestGetSetting:
    """Tests for ``get_setting``."""

    async def test_returns_parsed_value(self) -> None:
        """get_setting parses JSONB dict via Pydantic model."""
        with patch(
            "jai.services.settings._fetch_row",
            new_callable=lambda: _make_fetch_mock(
                {(SettingLevel.GLOBAL, None): {"completed": True}}
            ),
        ):
            result = await get_setting(
                AsyncMock(),
                SETTING_KEY_ONBOARDING_COMPLETED,
                level=SettingLevel.GLOBAL,
                value_type=OnboardingState,
            )

        assert result is not None
        assert isinstance(result, OnboardingState)
        assert result.completed is True

    async def test_returns_none_when_missing(self) -> None:
        with patch(
            "jai.services.settings._fetch_row",
            new_callable=lambda: _make_fetch_mock({}),
        ):
            result = await get_setting(
                AsyncMock(),
                "nonexistent",
                level=SettingLevel.GLOBAL,
                value_type=OnboardingState,
            )

        assert result is None

    async def test_returns_raw_dict_without_type(self) -> None:
        with patch(
            "jai.services.settings._fetch_row",
            new_callable=lambda: _make_fetch_mock(
                {(SettingLevel.GLOBAL, None): {"completed": False}}
            ),
        ):
            result = await get_setting(
                AsyncMock(),
                SETTING_KEY_ONBOARDING_COMPLETED,
                level=SettingLevel.GLOBAL,
            )

        assert isinstance(result, dict)
        assert result["completed"] is False

    async def test_default_value_parsed(self) -> None:
        """Pydantic default (completed=False) is applied when key absent."""
        with patch(
            "jai.services.settings._fetch_row",
            new_callable=lambda: _make_fetch_mock(
                {(SettingLevel.GLOBAL, None): {}}
            ),
        ):
            result = await get_setting(
                AsyncMock(),
                SETTING_KEY_ONBOARDING_COMPLETED,
                level=SettingLevel.GLOBAL,
                value_type=OnboardingState,
            )

        assert result is not None
        assert result.completed is False

    async def test_bypass_cache_uses_typed_database_fallback_without_changing_cache(self) -> None:
        company_id = uuid.uuid4()
        company_key = _cache_key(
            SettingLevel.COMPANY, company_id, SETTING_KEY_ONBOARDING_COMPLETED
        )
        global_key = _cache_key(
            SettingLevel.GLOBAL, None, SETTING_KEY_ONBOARDING_COMPLETED
        )
        cached_company = OnboardingState(completed=False)
        cached_global = OnboardingState(completed=False)
        _cache[company_key] = cached_company
        _cache[global_key] = cached_global
        fetch = _make_fetch_mock(
            {(SettingLevel.GLOBAL, None): {"completed": True}}
        )

        with patch("jai.services.settings._fetch_row", fetch):
            result = await get_setting(
                AsyncMock(),
                SETTING_KEY_ONBOARDING_COMPLETED,
                level=SettingLevel.COMPANY,
                scope_id=company_id,
                value_type=OnboardingState,
                bypass_cache=True,
            )

        assert result == OnboardingState(completed=True)
        assert [call.args[1:3] for call in fetch.await_args_list] == [
            (SettingLevel.COMPANY, company_id),
            (SettingLevel.GLOBAL, None),
        ]
        assert _cache[company_key] is cached_company
        assert _cache[global_key] is cached_global

    async def test_bypass_cache_rejects_invalid_typed_database_value_without_fallback(self) -> None:
        company_id = uuid.uuid4()
        company_key = _cache_key(
            SettingLevel.COMPANY, company_id, SETTING_KEY_ONBOARDING_COMPLETED
        )
        cached_company = OnboardingState(completed=False)
        _cache[company_key] = cached_company
        fetch = _make_fetch_mock(
            {
                (SettingLevel.COMPANY, company_id): {"completed": "invalid"},
                (SettingLevel.GLOBAL, None): {"completed": True},
            }
        )

        with (
            patch("jai.services.settings._fetch_row", fetch),
            pytest.raises(ValidationError),
        ):
            await get_setting(
                AsyncMock(),
                SETTING_KEY_ONBOARDING_COMPLETED,
                level=SettingLevel.COMPANY,
                scope_id=company_id,
                value_type=OnboardingState,
                bypass_cache=True,
            )

        assert [call.args[1:3] for call in fetch.await_args_list] == [
            (SettingLevel.COMPANY, company_id)
        ]
        assert _cache[company_key] is cached_company


# ---------------------------------------------------------------------------
# Fallback priority: USER > COMPANY > GLOBAL
# ---------------------------------------------------------------------------


class TestFallback:
    """Verify the three-layer fallback chain."""

    async def test_user_overrides_company_and_global(self) -> None:
        user_id = uuid.uuid4()
        with patch(
            "jai.services.settings._fetch_row",
            new_callable=lambda: _make_fetch_mock(
                {(SettingLevel.USER, user_id): {"completed": False}}
            ),
        ):
            result = await get_setting(
                AsyncMock(),
                SETTING_KEY_ONBOARDING_COMPLETED,
                level=SettingLevel.USER,
                scope_id=user_id,
                value_type=OnboardingState,
            )

        assert result is not None
        assert result.completed is False  # USER-level value

    async def test_falls_back_to_global(self) -> None:
        user_id = uuid.uuid4()
        with patch(
            "jai.services.settings._fetch_row",
            new_callable=lambda: _make_fetch_mock(
                {(SettingLevel.GLOBAL, None): {"completed": True}}
            ),
        ):
            result = await get_setting(
                AsyncMock(),
                SETTING_KEY_ONBOARDING_COMPLETED,
                level=SettingLevel.USER,
                scope_id=user_id,
                value_type=OnboardingState,
            )

        assert result is not None
        assert result.completed is True  # GLOBAL-level fallback

    async def test_global_only_checks_global(self) -> None:
        """When level=GLOBAL, only the GLOBAL slot is checked."""
        user_id = uuid.uuid4()
        with patch(
            "jai.services.settings._fetch_row",
            new_callable=lambda: _make_fetch_mock(
                {(SettingLevel.USER, user_id): {"completed": True}}
            ),
        ):
            result = await get_setting(
                AsyncMock(),
                SETTING_KEY_ONBOARDING_COMPLETED,
                level=SettingLevel.GLOBAL,
                value_type=OnboardingState,
            )

        assert result is None  # USER entry not checked

    async def test_all_levels_set_user_wins_no_scope_map(self) -> None:
        """Without _scope_map, fallback from USER skips to GLOBAL."""
        user_id = uuid.uuid4()
        with patch(
            "jai.services.settings._fetch_row",
            new_callable=lambda: _make_fetch_mock(
                {
                    (SettingLevel.GLOBAL, None): {"completed": True},
                    (SettingLevel.USER, user_id): {"completed": False},
                }
            ),
        ):
            result = await get_setting(
                AsyncMock(),
                SETTING_KEY_ONBOARDING_COMPLETED,
                level=SettingLevel.USER,
                scope_id=user_id,
                value_type=OnboardingState,
            )

        assert result is not None
        assert result.completed is False  # USER wins

    async def test_scope_map_enables_company_fallback(self) -> None:
        """With _scope_map, USER → COMPANY(company_id) → GLOBAL works."""
        user_id = uuid.uuid4()
        company_id = uuid.uuid4()
        scope_map = {
            SettingLevel.USER: user_id,
            SettingLevel.COMPANY: company_id,
            SettingLevel.GLOBAL: None,
        }
        with patch(
            "jai.services.settings._fetch_row",
            new_callable=lambda: _make_fetch_mock(
                {(SettingLevel.COMPANY, company_id): {"completed": True}}
            ),
        ):
            result = await get_setting(
                AsyncMock(),
                SETTING_KEY_ONBOARDING_COMPLETED,
                level=SettingLevel.USER,
                value_type=OnboardingState,
                _scope_map=scope_map,
            )

        assert result is not None
        assert result.completed is True  # COMPANY-level hit

    async def test_scope_map_user_overrides_company(self) -> None:
        """With _scope_map, USER value takes priority over COMPANY."""
        user_id = uuid.uuid4()
        company_id = uuid.uuid4()
        scope_map = {
            SettingLevel.USER: user_id,
            SettingLevel.COMPANY: company_id,
            SettingLevel.GLOBAL: None,
        }
        with patch(
            "jai.services.settings._fetch_row",
            new_callable=lambda: _make_fetch_mock(
                {
                    (SettingLevel.COMPANY, company_id): {"completed": True},
                    (SettingLevel.USER, user_id): {"completed": False},
                }
            ),
        ):
            result = await get_setting(
                AsyncMock(),
                SETTING_KEY_ONBOARDING_COMPLETED,
                level=SettingLevel.USER,
                value_type=OnboardingState,
                _scope_map=scope_map,
            )

        assert result is not None
        assert result.completed is False  # USER wins over COMPANY

    async def test_scope_map_full_fallback_to_global(self) -> None:
        """With _scope_map, falls USER → COMPANY → GLOBAL when USER and COMPANY absent."""
        user_id = uuid.uuid4()
        company_id = uuid.uuid4()
        scope_map = {
            SettingLevel.USER: user_id,
            SettingLevel.COMPANY: company_id,
            SettingLevel.GLOBAL: None,
        }
        with patch(
            "jai.services.settings._fetch_row",
            new_callable=lambda: _make_fetch_mock(
                {(SettingLevel.GLOBAL, None): {"completed": True}}
            ),
        ):
            result = await get_setting(
                AsyncMock(),
                SETTING_KEY_ONBOARDING_COMPLETED,
                level=SettingLevel.USER,
                value_type=OnboardingState,
                _scope_map=scope_map,
            )

        assert result is not None
        assert result.completed is True  # GLOBAL fallback


# ---------------------------------------------------------------------------
# get_effective_setting – convenience wrapper
# ---------------------------------------------------------------------------


class TestGetEffectiveSetting:
    """Verify the convenience wrapper resolves scope from a User object."""

    async def test_returns_user_level_value(self) -> None:
        user_id = uuid.uuid4()
        company_id = uuid.uuid4()
        user = _make_user_mock(user_id=user_id, company_id=company_id)

        with patch(
            "jai.services.settings._fetch_row",
            new_callable=lambda: _make_fetch_mock(
                {(SettingLevel.USER, user_id): {"theme": "dark"}}
            ),
        ):
            result = await get_effective_setting(
                AsyncMock(),
                SETTING_KEY_USER_PREFERENCES,
                user=user,
                value_type=UserPreferences,
            )

        assert result is not None
        assert result.theme == "dark"

    async def test_falls_back_to_company(self) -> None:
        user_id = uuid.uuid4()
        company_id = uuid.uuid4()
        user = _make_user_mock(user_id=user_id, company_id=company_id)

        with patch(
            "jai.services.settings._fetch_row",
            new_callable=lambda: _make_fetch_mock(
                {(SettingLevel.COMPANY, company_id): {"theme": "light"}}
            ),
        ):
            result = await get_effective_setting(
                AsyncMock(),
                SETTING_KEY_USER_PREFERENCES,
                user=user,
                value_type=UserPreferences,
            )

        assert result is not None
        assert result.theme == "light"

    async def test_falls_back_to_global(self) -> None:
        user_id = uuid.uuid4()
        company_id = uuid.uuid4()
        user = _make_user_mock(user_id=user_id, company_id=company_id)

        with patch(
            "jai.services.settings._fetch_row",
            new_callable=lambda: _make_fetch_mock(
                {(SettingLevel.GLOBAL, None): {"theme": "system"}}
            ),
        ):
            result = await get_effective_setting(
                AsyncMock(),
                SETTING_KEY_USER_PREFERENCES,
                user=user,
                value_type=UserPreferences,
            )

        assert result is not None
        assert result.theme == "system"

    async def test_returns_none_when_all_absent(self) -> None:
        user_id = uuid.uuid4()
        company_id = uuid.uuid4()
        user = _make_user_mock(user_id=user_id, company_id=company_id)

        with patch(
            "jai.services.settings._fetch_row",
            new_callable=lambda: _make_fetch_mock({}),
        ):
            result = await get_effective_setting(
                AsyncMock(),
                SETTING_KEY_USER_PREFERENCES,
                user=user,
                value_type=UserPreferences,
            )

        assert result is None

    async def test_user_overrides_company_and_global(self) -> None:
        user_id = uuid.uuid4()
        company_id = uuid.uuid4()
        user = _make_user_mock(user_id=user_id, company_id=company_id)

        with patch(
            "jai.services.settings._fetch_row",
            new_callable=lambda: _make_fetch_mock(
                {
                    (SettingLevel.GLOBAL, None): {"theme": "system"},
                    (SettingLevel.COMPANY, company_id): {"theme": "light"},
                    (SettingLevel.USER, user_id): {"theme": "dark"},
                }
            ),
        ):
            result = await get_effective_setting(
                AsyncMock(),
                SETTING_KEY_USER_PREFERENCES,
                user=user,
                value_type=UserPreferences,
            )

        assert result is not None
        assert result.theme == "dark"  # USER wins

    async def test_company_null_falls_to_global(self) -> None:
        """When user has no company_id, COMPANY layer is skipped."""
        user_id = uuid.uuid4()
        user = _make_user_mock(user_id=user_id, company_id=None)

        with patch(
            "jai.services.settings._fetch_row",
            new_callable=lambda: _make_fetch_mock(
                {(SettingLevel.GLOBAL, None): {"theme": "light"}}
            ),
        ):
            result = await get_effective_setting(
                AsyncMock(),
                SETTING_KEY_USER_PREFERENCES,
                user=user,
                value_type=UserPreferences,
            )

        assert result is not None
        assert result.theme == "light"  # Fell to GLOBAL (no COMPANY scope)

    async def test_raw_dict_without_type(self) -> None:
        """Without value_type, raw dict is returned."""
        user_id = uuid.uuid4()
        user = _make_user_mock(user_id=user_id, company_id=None)

        with patch(
            "jai.services.settings._fetch_row",
            new_callable=lambda: _make_fetch_mock(
                {(SettingLevel.USER, user_id): {"theme": "dark"}}
            ),
        ):
            result = await get_effective_setting(
                AsyncMock(),
                SETTING_KEY_USER_PREFERENCES,
                user=user,
            )

        assert isinstance(result, dict)
        assert result["theme"] == "dark"


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


class TestCache:
    """Verify in-process cache hit and write-time invalidation."""

    async def test_cache_hit_avoids_db_query(self) -> None:
        """Second get_setting uses cache without calling _fetch_row again."""
        call_count = 0

        async def _fetch(session: object, level: SettingLevel, scope_id: object, key: str) -> dict:  # type: ignore[type-arg]  # noqa: ARG001
            nonlocal call_count
            call_count += 1
            return {"completed": True}

        with patch("jai.services.settings._fetch_row", side_effect=_fetch):
            session = AsyncMock()
            r1 = await get_setting(
                session,
                SETTING_KEY_ONBOARDING_COMPLETED,
                level=SettingLevel.GLOBAL,
                value_type=OnboardingState,
            )
            assert call_count == 1
            assert r1 is not None

            r2 = await get_setting(
                session,
                SETTING_KEY_ONBOARDING_COMPLETED,
                level=SettingLevel.GLOBAL,
                value_type=OnboardingState,
            )
            assert call_count == 1  # no additional fetch
            assert r2 is not None

    async def test_cache_invalidated_on_write(self) -> None:
        """set_setting invalidates cache; next get_setting re-fetches."""
        fetch_count = 0

        async def _fetch(session: object, level: SettingLevel, scope_id: object, key: str) -> dict:  # type: ignore[type-arg]  # noqa: ARG001
            nonlocal fetch_count
            fetch_count += 1
            return {"completed": True}

        with patch("jai.services.settings._fetch_row", side_effect=_fetch):
            session = AsyncMock()
            await get_setting(
                session,
                SETTING_KEY_ONBOARDING_COMPLETED,
                level=SettingLevel.GLOBAL,
                value_type=OnboardingState,
            )
            assert fetch_count == 1

            # Write invalidates cache
            await set_setting(
                session,
                SETTING_KEY_ONBOARDING_COMPLETED,
                OnboardingState(completed=True),
                level=SettingLevel.GLOBAL,
            )

            # Re-fetch
            await get_setting(
                session,
                SETTING_KEY_ONBOARDING_COMPLETED,
                level=SettingLevel.GLOBAL,
                value_type=OnboardingState,
            )
            assert fetch_count == 2

    async def test_cache_hit_with_scope_map(self) -> None:
        """Cache works correctly when _scope_map is used."""
        user_id = uuid.uuid4()
        company_id = uuid.uuid4()
        scope_map = {
            SettingLevel.USER: user_id,
            SettingLevel.COMPANY: company_id,
            SettingLevel.GLOBAL: None,
        }
        call_count = 0

        async def _fetch(  # noqa: ARG001
            session: object,
            level: SettingLevel,
            scope_id: object,
            key: str,  # noqa: ARG001
        ) -> dict[str, str] | None:
            nonlocal call_count
            call_count += 1
            if level == SettingLevel.USER:
                return None  # USER miss
            if level == SettingLevel.COMPANY:
                return {"theme": "light"}
            return {"theme": "system"}

        with patch("jai.services.settings._fetch_row", side_effect=_fetch):
            session = AsyncMock()
            r1 = await get_setting(
                session,
                SETTING_KEY_USER_PREFERENCES,
                level=SettingLevel.USER,
                value_type=UserPreferences,
                _scope_map=scope_map,
            )
            assert call_count == 2  # USER miss, COMPANY hit
            assert r1 is not None

            r2 = await get_setting(
                session,
                SETTING_KEY_USER_PREFERENCES,
                level=SettingLevel.USER,
                value_type=UserPreferences,
                _scope_map=scope_map,
            )
            # Second call: USER miss (not cached because None), but COMPANY
            # cache should be hit.  However, USER-level query still fires
            # because it returns None (None is never cached).
            # So call_count should be 2 + 1 (USER probe) = 3.
            assert call_count == 3
            assert r2 is not None


# ---------------------------------------------------------------------------
# set_setting
# ---------------------------------------------------------------------------


class TestSetSetting:
    """Tests for ``set_setting``."""

    async def test_calls_execute_and_flush(self) -> None:
        session = AsyncMock()

        await set_setting(
            session,
            SETTING_KEY_ONBOARDING_COMPLETED,
            OnboardingState(completed=True),
            level=SettingLevel.GLOBAL,
        )

        session.execute.assert_called_once()
        session.flush.assert_called_once()

    async def test_upsert_invalidates_cache(self) -> None:
        session = AsyncMock()

        # Pre-populate cache
        _cache[(SettingLevel.GLOBAL, None, SETTING_KEY_ONBOARDING_COMPLETED)] = OnboardingState(
            completed=False
        )

        await set_setting(
            session,
            SETTING_KEY_ONBOARDING_COMPLETED,
            OnboardingState(completed=True),
            level=SettingLevel.GLOBAL,
        )

        assert (SettingLevel.GLOBAL, None, SETTING_KEY_ONBOARDING_COMPLETED) not in _cache


# ---------------------------------------------------------------------------
# level / scope_id validation
# ---------------------------------------------------------------------------


class TestLevelScopeValidation:
    """Verify _validate_level_scope rejects invalid combinations."""

    async def test_global_rejects_scope_id(self) -> None:
        """GLOBAL level must have scope_id=None."""
        with pytest.raises(ValueError, match="GLOBAL"):
            await set_setting(
                AsyncMock(),
                SETTING_KEY_ONBOARDING_COMPLETED,
                OnboardingState(completed=True),
                level=SettingLevel.GLOBAL,
                scope_id=uuid.uuid4(),
            )

    async def test_company_requires_scope_id(self) -> None:
        """COMPANY level must have a non-None scope_id."""
        with pytest.raises(ValueError, match="COMPANY"):
            await set_setting(
                AsyncMock(),
                SETTING_KEY_ONBOARDING_COMPLETED,
                OnboardingState(completed=True),
                level=SettingLevel.COMPANY,
                scope_id=None,
            )

    async def test_user_requires_scope_id(self) -> None:
        """USER level must have a non-None scope_id."""
        with pytest.raises(ValueError, match="USER"):
            await set_setting(
                AsyncMock(),
                SETTING_KEY_ONBOARDING_COMPLETED,
                OnboardingState(completed=True),
                level=SettingLevel.USER,
                scope_id=None,
            )

    async def test_global_with_none_is_ok(self) -> None:
        """GLOBAL + scope_id=None should not raise."""
        session = AsyncMock()
        await set_setting(
            session,
            SETTING_KEY_ONBOARDING_COMPLETED,
            OnboardingState(completed=True),
            level=SettingLevel.GLOBAL,
        )
        session.execute.assert_called_once()

    async def test_user_with_scope_id_is_ok(self) -> None:
        """USER + scope_id=<uuid> should not raise."""
        session = AsyncMock()
        await set_setting(
            session,
            SETTING_KEY_ONBOARDING_COMPLETED,
            OnboardingState(completed=True),
            level=SettingLevel.USER,
            scope_id=uuid.uuid4(),
        )
        session.execute.assert_called_once()

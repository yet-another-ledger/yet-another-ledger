"""PostgreSQL integration coverage for atomic Agent operation replay."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jai.models._enums import InvoiceTaxMode
from jai.models.agent_operation import AgentOperation
from jai.models.agent_token import AgentToken
from jai.models.company import Company
from jai.models.customer import Customer
from jai.models.receipt_inbox import ReceiptInbox
from jai.models.user import User
from jai.schemas.agent_token import AgentTokenCreate, AgentTokenScope
from jai.schemas.agent_tools import ReceiptSubmissionSnapshot
from jai.schemas.customer import CustomerRead
from jai.schemas.invoice import InvoiceLineInput, InvoiceWrite
from jai.schemas.quote import QuoteWrite
from jai.services.agent_operations import (
    AgentOperationFailure,
    AgentOperationValue,
    canonical_argument_digest,
    run_agent_mutation,
)
from jai.services.agent_tokens import AgentPrincipal, issue_token


@pytest.fixture
async def operation_principals(
    admin_session_maker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[AgentPrincipal, AgentPrincipal, AgentToken]]:
    company_id, other_company_id, user_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    body = AgentTokenCreate(name="Operation test", scopes=[AgentTokenScope.CUSTOMER_CREATE])
    token, _secret = issue_token(body, company_id)
    token.creator_id = user_id
    other_token, _other_secret = issue_token(body, other_company_id)
    other_token.creator_id = user_id
    async with admin_session_maker() as session:
        session.add_all(
            [
                Company(id=company_id, name="Operation company", base_currency="EUR"),
                Company(id=other_company_id, name="Other operation company", base_currency="EUR"),
                User(
                    id=user_id,
                    email=f"{user_id}@example.test",
                    hashed_password="not-a-real-password-hash",
                    company_id=company_id,
                    role="owner",
                    mfa_enabled=True,
                    totp_secret="test-secret",
                ),
            ]
        )
        await session.commit()
        session.add_all([token, other_token])
        await session.commit()
    yield (
        AgentPrincipal(token.id, company_id, frozenset({AgentTokenScope.CUSTOMER_CREATE})),
        AgentPrincipal(
            other_token.id,
            other_company_id,
            frozenset({AgentTokenScope.CUSTOMER_CREATE}),
        ),
        token,
    )


@pytest.mark.integration
async def test_operation_migration_grants_runtime_role_and_enforces_company_rls(
    operation_principals: tuple[AgentPrincipal, AgentPrincipal, AgentToken],
    runtime_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    principal, _other, token = operation_principals
    async with runtime_session_maker() as session:
        privileges = await session.scalar(
            text(
                "SELECT has_table_privilege(current_user, 'agent_operation', "
                "'SELECT, INSERT, UPDATE')"
            )
        )
        assert privileges is True
        delete_privilege = await session.scalar(
            text("SELECT has_table_privilege(current_user, 'agent_operation', 'DELETE')")
        )
        assert delete_privilege is False
        # These are effective privilege checks (including inherited and PUBLIC
        # grants); the app role still needs UPDATE for provisional completion.
        update_column_privilege = await session.scalar(
            text(
                "SELECT has_column_privilege(current_user, 'agent_operation', "
                "'request_id', 'UPDATE')"
            )
        )
        assert update_column_privilege is True
        insert_privilege = await session.scalar(
            text("SELECT has_table_privilege(current_user, 'agent_operation', 'INSERT')")
        )
        assert insert_privilege is True
        visible_without_context = await session.scalar(text("SELECT count(*) FROM agent_operation"))
        assert visible_without_context == 0
        await session.execute(
            text("SELECT set_config('jai.company_id', :company, true)"),
            {"company": str(principal.company_id)},
        )
        # RLS also permits a correctly tenant-scoped insert through the runtime role.
        session.add(
            AgentOperation(
                company_id=principal.company_id,
                token_id=token.id,
                request_id="raw-rls-check",
                operation="test.create",
                arguments_digest=canonical_argument_digest({"x": 1}),
                target_type="customer",
                target_id=uuid.uuid4(),
                outcome="succeeded",
                result_snapshot={
                    "id": str(uuid.uuid4()),
                    "name": "RLS check",
                    "created_at": datetime.now(UTC).isoformat(),
                    "updated_at": datetime.now(UTC).isoformat(),
                },
                completed_at=datetime.now(UTC),
            )
        )
        await session.flush()
        await session.execute(
            text("SELECT set_config('jai.company_id', :company, true)"),
            {"company": str(uuid.uuid4())},
        )
        assert await session.scalar(text("SELECT count(*) FROM agent_operation")) == 0
        # Deferred checks run under the transaction's current RLS context, so
        # restore the owning company before committing its inserted row.
        await session.execute(
            text("SELECT set_config('jai.company_id', :company, true)"),
            {"company": str(principal.company_id)},
        )
        await session.commit()

    # The raw SQL path available to runtime code cannot erase the idempotency
    # reservation or its audit trail.
    async with runtime_session_maker() as session:
        await session.execute(
            text("SELECT set_config('jai.company_id', :company, true)"),
            {"company": str(principal.company_id)},
        )
        with pytest.raises(DBAPIError):
            await session.execute(
                text("DELETE FROM agent_operation WHERE request_id = 'raw-rls-check'")
            )
        await session.rollback()


@pytest.mark.integration
async def test_deferred_constraint_rejects_deleted_provisional_operation(
    operation_principals: tuple[AgentPrincipal, AgentPrincipal, AgentToken],
    admin_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    principal, _other, token = operation_principals
    request_id = "deleted-provisional-row"
    async with admin_session_maker() as session:
        await session.execute(
            text(
                "INSERT INTO agent_operation "
                "(company_id, token_id, request_id, operation, arguments_digest, outcome) "
                "VALUES (:company_id, :token_id, :request_id, "
                "'customer.create', :digest, 'succeeded')"
            ),
            {
                "company_id": principal.company_id,
                "token_id": token.id,
                "request_id": request_id,
                "digest": canonical_argument_digest({"name": "temporary"}),
            },
        )
        await session.execute(
            text("DELETE FROM agent_operation WHERE request_id = :request_id"),
            {"request_id": request_id},
        )
        with pytest.raises(IntegrityError, match="reservation was deleted"):
            await session.commit()
        await session.rollback()

    async with admin_session_maker() as session:
        assert (
            await session.scalar(
                select(AgentOperation.id).where(AgentOperation.request_id == request_id)
            )
            is None
        )


@pytest.mark.integration
async def test_sequential_replay_conflict_and_deleted_target_do_not_repeat_mutation(
    operation_principals: tuple[AgentPrincipal, AgentPrincipal, AgentToken],
    runtime_session_maker: async_sessionmaker[AsyncSession],
    admin_session_maker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    principal, _other, _token = operation_principals
    calls = 0
    target_id = uuid.uuid4()

    async def mutate(session: AsyncSession) -> AgentOperationValue:
        nonlocal calls
        calls += 1
        customer = Customer(
            id=target_id,
            company_id=principal.company_id,
            name="Frozen Customer",
            email="private@example.test",
        )
        session.add(customer)
        await session.flush()
        return AgentOperationValue(
            "customer",
            target_id,
            CustomerRead(
                id=target_id,
                name="Frozen Customer",
                email="private@example.test",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            ),
        )

    async with runtime_session_maker() as session:
        created = await run_agent_mutation(
            session,
            principal,
            request_id="stable-1",
            operation="customer.create",
            arguments={"name": "Private Customer", "decimal": Decimal("4.00")},
            source={
                "platform": "telegram",
                "chat_id": "chat-17",
                "message_id": "msg-23",
            },
            mutate=mutate,
        )
        async with admin_session_maker() as admin_session:
            customer = await admin_session.get(Customer, target_id)
            assert customer is not None
            await admin_session.delete(customer)
            await admin_session.commit()

        async with admin_session_maker() as admin_session:
            assert await admin_session.get(Customer, target_id) is None

        replayed = await run_agent_mutation(
            session,
            principal,
            request_id="stable-1",
            operation="customer.create",
            arguments={"decimal": Decimal("4"), "name": "Private Customer"},
            source={
                "platform": "telegram",
                "chat_id": "chat-17",
                "message_id": "msg-23",
            },
            mutate=mutate,
        )
        assert created.status == "created"
        assert replayed.status == "replayed"
        assert replayed.target_id == target_id
        assert replayed.result_snapshot["name"] == "Frozen Customer"
        assert replayed.result_snapshot["email"] == "private@example.test"
        assert calls == 1

        with pytest.raises(HTTPException) as changed:
            await run_agent_mutation(
                session,
                principal,
                request_id="stable-1",
                operation="customer.create",
                arguments={"name": "Different Customer"},
                mutate=mutate,
            )
        assert changed.value.status_code == 409
        with pytest.raises(HTTPException) as changed_operation:
            await run_agent_mutation(
                session,
                principal,
                request_id="stable-1",
                operation="quote.create",
                arguments={"name": "Private Customer", "decimal": Decimal("4.00")},
                mutate=mutate,
            )
        assert changed_operation.value.status_code == 409
        with pytest.raises(HTTPException) as changed_type:
            await run_agent_mutation(
                session,
                principal,
                request_id="stable-1",
                operation="customer.create",
                arguments={"decimal": "4", "name": "Private Customer"},
                source={
                    "platform": "telegram",
                    "chat_id": "chat-17",
                    "message_id": "msg-23",
                },
                mutate=mutate,
            )
        assert changed_type.value.status_code == 409

    async with admin_session_maker() as session:
        row = await session.scalar(
            select(AgentOperation).where(AgentOperation.request_id == "stable-1")
        )
        assert row is not None
        assert row.arguments_digest == canonical_argument_digest(
            {
                "arguments": {"name": "Private Customer", "decimal": Decimal("4.00")},
                "source": {
                    "platform": "telegram",
                    "chat_id": "chat-17",
                    "message_id": "msg-23",
                },
            }
        )
        assert row.source_platform == "telegram"
        assert row.source_chat_id == "chat-17"
        assert row.source_message_id == "msg-23"
        assert row.result_snapshot["name"] == "Frozen Customer"
        assert row.result_snapshot["email"] == "private@example.test"
        assert "bearer" not in str(row.result_snapshot)
        assert "Private Customer" not in caplog.text
        assert "private@example.test" not in caplog.text
        assert "secret" not in caplog.text


@pytest.mark.integration
async def test_receipt_result_snapshot_replays_after_inbox_target_deletion(
    operation_principals: tuple[AgentPrincipal, AgentPrincipal, AgentToken],
    runtime_session_maker: async_sessionmaker[AsyncSession],
    admin_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    principal, _other, _token = operation_principals
    inbox_id = uuid.uuid4()
    calls = 0

    async def submit(session: AsyncSession) -> AgentOperationValue:
        nonlocal calls
        calls += 1
        session.add(ReceiptInbox(id=inbox_id, company_id=principal.company_id))
        await session.flush()
        return AgentOperationValue(
            "receipt",
            inbox_id,
            ReceiptSubmissionSnapshot(
                inbox_id=inbox_id,
                ai_state="FAILED",
                ai_error_code="AI_DISABLED",
                review_url="https://jai.example.test/receipt-inbox/" + str(inbox_id),
            ),
        )

    async with runtime_session_maker() as session:
        created = await run_agent_mutation(
            session,
            principal,
            request_id="receipt-replay-1",
            operation="submit_receipt",
            arguments={"language": "en", "file_count": 1},
            mutate=submit,
        )
        assert created.status == "created"
        async with admin_session_maker() as admin_session:
            target = await admin_session.get(ReceiptInbox, inbox_id)
            assert target is not None
            await admin_session.delete(target)
            await admin_session.commit()

        replayed = await run_agent_mutation(
            session,
            principal,
            request_id="receipt-replay-1",
            operation="submit_receipt",
            arguments={"file_count": 1, "language": "en"},
            mutate=submit,
        )

    assert replayed.status == "replayed"
    assert replayed.operation_id == created.operation_id
    assert replayed.target_type == "receipt"
    assert replayed.target_id == inbox_id
    assert replayed.result_snapshot == {
        "inbox_id": str(inbox_id),
        "ai_state": "FAILED",
        "ai_error_code": "AI_DISABLED",
        "review_url": "https://jai.example.test/receipt-inbox/" + str(inbox_id),
    }
    assert calls == 1
    async with admin_session_maker() as admin_session:
        assert await admin_session.get(ReceiptInbox, inbox_id) is None


@pytest.mark.integration
async def test_runtime_cannot_rewrite_completed_operation_or_idempotency_key(
    operation_principals: tuple[AgentPrincipal, AgentPrincipal, AgentToken],
    runtime_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    principal, _other, _token = operation_principals
    calls = 0
    target_id = uuid.uuid4()

    async def mutate(_session: AsyncSession) -> AgentOperationValue:
        nonlocal calls
        calls += 1
        return AgentOperationValue(
            "customer",
            target_id,
            CustomerRead(
                id=target_id,
                name="Immutable customer",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            ),
        )

    async with runtime_session_maker() as session:
        created = await run_agent_mutation(
            session,
            principal,
            request_id="immutable-key",
            operation="customer.create",
            arguments={"name": "Immutable customer"},
            mutate=mutate,
        )
        assert created.status == "created"

    # Exercise the SQL path available to runtime code. Changing the idempotency
    # key and audit/result fields must fail even though UPDATE is granted for
    # the provisional reservation -> completion transition.
    async with runtime_session_maker() as session:
        await session.execute(
            text("SELECT set_config('jai.company_id', :company, true)"),
            {"company": str(principal.company_id)},
        )
        with pytest.raises(DBAPIError, match="completed agent_operation rows are immutable"):
            await session.execute(
                text(
                    "UPDATE agent_operation SET request_id = 'rewritten-key', "
                    "arguments_digest = :digest, target_id = :target_id, "
                    "source_chat_id = 'rewritten-chat', result_snapshot = '{}'::jsonb, "
                    "outcome = 'failed', error_code = 'ERASED' "
                    "WHERE request_id = 'immutable-key'"
                ),
                {
                    "digest": canonical_argument_digest({"name": "rewritten"}),
                    "target_id": uuid.uuid4(),
                },
            )
        await session.rollback()

    async with runtime_session_maker() as session:
        replayed = await run_agent_mutation(
            session,
            principal,
            request_id="immutable-key",
            operation="customer.create",
            arguments={"name": "Immutable customer"},
            mutate=mutate,
        )
        assert replayed.status == "replayed"
        assert replayed.operation_id == created.operation_id
        assert replayed.target_id == target_id
        assert replayed.result_snapshot["name"] == "Immutable customer"
        assert calls == 1


@pytest.mark.integration
@pytest.mark.parametrize(
    ("write_model", "date_field", "request_id"),
    [
        (QuoteWrite, "quote_date", "quote-date-replay"),
        (InvoiceWrite, "invoice_date", "invoice-date-replay"),
    ],
)
async def test_validated_document_write_python_dates_replay_and_conflict(
    write_model: type[QuoteWrite] | type[InvoiceWrite],
    date_field: str,
    request_id: str,
    operation_principals: tuple[AgentPrincipal, AgentPrincipal, AgentToken],
    runtime_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    principal, _other, _token = operation_principals
    validated_input = write_model(
        customer_id=uuid.uuid4(),
        **{date_field: date(2026, 9, 30)},
        tax_mode=InvoiceTaxMode.LINE,
        lines=[
            InvoiceLineInput(
                name="Consulting",
                quantity=Decimal("1"),
                unit_price=Decimal("95"),
            )
        ],
    )
    args = {"write": validated_input.model_dump(mode="python")}
    target_id = uuid.uuid4()
    calls = 0

    async def mutate(_session: AsyncSession) -> AgentOperationValue:
        nonlocal calls
        calls += 1
        return AgentOperationValue(
            "customer",
            target_id,
            CustomerRead(
                id=target_id,
                name="Date replay",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            ),
        )

    async with runtime_session_maker() as session:
        created = await run_agent_mutation(
            session,
            principal,
            request_id=request_id,
            operation="test.validated_write",
            arguments=args,
            mutate=mutate,
        )
    async with runtime_session_maker() as session:
        replayed = await run_agent_mutation(
            session,
            principal,
            request_id=request_id,
            operation="test.validated_write",
            arguments={"write": validated_input.model_dump(mode="python")},
            mutate=mutate,
        )
        assert replayed.status == "replayed"
        assert replayed.operation_id == created.operation_id
        changed_payload = validated_input.model_dump(mode="python")
        changed_payload[date_field] = date(2026, 10, 1)
        changed_input = write_model.model_validate(changed_payload)
        with pytest.raises(HTTPException) as changed:
            await run_agent_mutation(
                session,
                principal,
                request_id=request_id,
                operation="test.validated_write",
                arguments={"write": changed_input.model_dump(mode="python")},
                mutate=mutate,
            )
        assert changed.value.status_code == 409
    assert calls == 1


@pytest.mark.integration
async def test_target_id_mismatch_rolls_back_mutation_and_operation(
    operation_principals: tuple[AgentPrincipal, AgentPrincipal, AgentToken],
    runtime_session_maker: async_sessionmaker[AsyncSession],
    admin_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    principal, _other, _token = operation_principals
    target_id = uuid.uuid4()
    mismatched_id = uuid.uuid4()

    async def mutate(session: AsyncSession) -> AgentOperationValue:
        session.add(
            Customer(
                id=target_id,
                company_id=principal.company_id,
                name="Must roll back",
            )
        )
        await session.flush()
        return AgentOperationValue(
            "customer",
            target_id,
            CustomerRead(
                id=mismatched_id,
                name="Wrong response target",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            ),
        )

    async with runtime_session_maker() as session:
        with pytest.raises(ValueError, match="match its operation target ID"):
            await run_agent_mutation(
                session,
                principal,
                request_id="mismatched-target-rollback",
                operation="customer.create",
                arguments={"name": "Must roll back"},
                mutate=mutate,
            )

    async with admin_session_maker() as session:
        assert await session.get(Customer, target_id) is None
        row = await session.scalar(
            select(AgentOperation).where(
                AgentOperation.request_id == "mismatched-target-rollback"
            )
        )
        assert row is None


@pytest.mark.integration
async def test_concurrent_duplicate_executes_mutation_once(
    operation_principals: tuple[AgentPrincipal, AgentPrincipal, AgentToken],
    runtime_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    principal, _other, _token = operation_principals
    calls = 0
    target_id = uuid.uuid4()

    async def mutate(_session: AsyncSession) -> AgentOperationValue:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return AgentOperationValue(
            "customer",
            target_id,
            CustomerRead(
                id=target_id,
                name="Concurrent",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            ),
        )

    async def request() -> str:
        async with runtime_session_maker() as session:
            response = await run_agent_mutation(
                session,
                principal,
                request_id="concurrent-1",
                operation="customer.create",
                arguments={"name": "Concurrent"},
                mutate=mutate,
            )
            return response.status

    statuses = await asyncio.gather(request(), request())
    assert sorted(statuses) == ["created", "replayed"]
    assert calls == 1


@pytest.mark.integration
async def test_rollback_safe_error_cross_company_and_revoked_replay(
    operation_principals: tuple[AgentPrincipal, AgentPrincipal, AgentToken],
    runtime_session_maker: async_sessionmaker[AsyncSession],
    admin_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    principal, _other, token = operation_principals
    async with runtime_session_maker() as session:

        async def crash(session: AsyncSession) -> AgentOperationValue:
            await session.execute(
                text("UPDATE agent_token SET name = 'changed by failed mutation' WHERE id = :id"),
                {"id": token.id},
            )
            raise RuntimeError("injected crash with sensitive text")

        with pytest.raises(RuntimeError):
            await run_agent_mutation(
                session,
                principal,
                request_id="rollback-1",
                operation="customer.create",
                arguments={"name": "No record"},
                mutate=crash,
            )

        async def expected_failure(_session: AsyncSession) -> AgentOperationValue:
            raise AgentOperationFailure("MCP_TARGET_NOT_FOUND")

        failed = await run_agent_mutation(
            session,
            principal,
            request_id="safe-error-1",
            operation="customer.create",
            arguments={"name": "Missing"},
            mutate=expected_failure,
        )
        assert failed.outcome == "failed"
        assert failed.error_code == "MCP_TARGET_NOT_FOUND"

        forged_cross_company = AgentPrincipal(
            principal.token_id,
            uuid.uuid4(),
            principal.scopes,
        )
        with pytest.raises(HTTPException) as cross_company:
            await run_agent_mutation(
                session,
                forged_cross_company,
                request_id="rollback-1",
                operation="customer.create",
                arguments={"name": "No record"},
                mutate=expected_failure,
            )
        assert cross_company.value.status_code == 401

    async with admin_session_maker() as session:
        name = await session.scalar(select(AgentToken.name).where(AgentToken.id == token.id))
        assert name == "Operation test"
        assert (
            await session.scalar(
                select(AgentOperation.id).where(AgentOperation.request_id == "rollback-1")
            )
            is None
        )
        saved = await session.get(AgentToken, token.id)
        assert saved is not None
        saved.revoked_at = datetime.now(UTC)
        await session.commit()

    async with runtime_session_maker() as session:
        with pytest.raises(HTTPException) as revoked:
            await run_agent_mutation(
                session,
                principal,
                request_id="safe-error-1",
                operation="customer.create",
                arguments={"name": "Missing"},
                mutate=expected_failure,
            )
        assert revoked.value.status_code == 401


@pytest.mark.integration
async def test_callback_commit_cannot_persist_provisional_operation(
    operation_principals: tuple[AgentPrincipal, AgentPrincipal, AgentToken],
    runtime_session_maker: async_sessionmaker[AsyncSession],
    admin_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    principal, _other, _token = operation_principals

    async with runtime_session_maker() as session:

        async def erroneous_commit(_session: AsyncSession) -> AgentOperationValue:
            await session.commit()
            return AgentOperationValue(
                "customer",
                uuid.uuid4(),
                CustomerRead(
                    id=uuid.uuid4(),
                    name="Must not persist",
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                ),
            )

        with pytest.raises(RuntimeError, match="cannot commit"):
            await run_agent_mutation(
                session,
                principal,
                request_id="callback-commit-1",
                operation="customer.create",
                arguments={"name": "Must not persist"},
                mutate=erroneous_commit,
            )

    async with admin_session_maker() as session:
        assert (
            await session.scalar(
                select(AgentOperation.id).where(AgentOperation.request_id == "callback-commit-1")
            )
            is None
        )


@pytest.mark.integration
async def test_callback_raw_delete_cannot_erase_operation_reservation(
    operation_principals: tuple[AgentPrincipal, AgentPrincipal, AgentToken],
    runtime_session_maker: async_sessionmaker[AsyncSession],
    admin_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    principal, _other, _token = operation_principals

    async with runtime_session_maker() as session:

        async def erroneous_delete(callback_session: AsyncSession) -> AgentOperationValue:
            await callback_session.execute(
                text("DELETE FROM agent_operation WHERE request_id = 'callback-delete-1'")
            )
            raise AssertionError("runtime DELETE unexpectedly succeeded")

        with pytest.raises(DBAPIError):
            await run_agent_mutation(
                session,
                principal,
                request_id="callback-delete-1",
                operation="customer.create",
                arguments={"name": "Must not delete"},
                mutate=erroneous_delete,
            )

    async with admin_session_maker() as session:
        assert (
            await session.scalar(
                select(AgentOperation.id).where(AgentOperation.request_id == "callback-delete-1")
            )
            is None
        )


@pytest.mark.integration
async def test_changed_source_ids_conflict_even_when_arguments_match(
    operation_principals: tuple[AgentPrincipal, AgentPrincipal, AgentToken],
    runtime_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    principal, _other, _token = operation_principals
    target_id = uuid.uuid4()

    async def mutate(_session: AsyncSession) -> AgentOperationValue:
        return AgentOperationValue(
            "customer",
            target_id,
            CustomerRead(
                id=target_id,
                name="Source-bound",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            ),
        )

    async with runtime_session_maker() as session:
        kwargs = dict(
            request_id="source-conflict-1",
            operation="customer.create",
            arguments={"name": "Source-bound"},
            mutate=mutate,
        )
        await run_agent_mutation(
            session,
            principal,
            **kwargs,
            source={"platform": "telegram", "chat_id": "chat-a", "message_id": "msg-a"},
        )
        with pytest.raises(HTTPException) as changed_source:
            await run_agent_mutation(
                session,
                principal,
                **kwargs,
                source={"platform": "telegram", "chat_id": "chat-b", "message_id": "msg-a"},
            )
        assert changed_source.value.status_code == 409

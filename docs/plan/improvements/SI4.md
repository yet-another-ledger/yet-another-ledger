# SI4 · Read expenses through MCP

> 🌐 **English** · [中文](SI4_zh.md)

> **Status (2026-11-10)** 🟢 Implementation, independent review and author-delegated real TCP subagent acceptance are complete. The two read tools and `expense:read` are delivered in 0.13.0. Historical M15 acceptance is preserved. Human UI/Telegram/Hermes checks remain distinguished from this automated evidence.

> **Planned follow-on (2026-11-10)** [M18 Agent mileage intake](../milestones/M18.md) has an agreed design awaiting implementation. It reuses the mileage and Agent foundations for client-neutral options, previews and confirmed creation under a separate `mileage:create` permission.

## Contract and boundary

- Add `list_expenses` and `get_expense`, both requiring new `expense:read`. The complete current surface becomes 18 tools and nine scopes. Existing tokens retain exactly their original grants. Owners create a replacement token to obtain the new scope.
- `list_expenses` mirrors the existing owner API filters `q`, `category_id`, `vat_treatment_id`, `deductible`, `is_draft`, `kind`, inclusive `date_from`/`date_to`, `limit` (default 50, 1–500), `offset` (default 0, nonnegative) and `sort_by` (`expense_date` or `created_at`, default `expense_date`). Reuse existing expense ordering and return existing typed list items plus total. Both drafts and confirmed expenses follow these existing filters.
- `get_expense` accepts UUID `expense_id` and returns `expense` containing existing `ExpenseRead` fields. Missing and foreign-company IDs produce the same `MCP_TARGET_NOT_FOUND`. Inputs reject unknown fields and invalid types/bounds through existing safe validation. Permission, revoked-token and Host/Origin guards remain enforced on every request.
- Use existing expense services and transaction-local RLS under the non-superuser runtime role. Introduce no parallel pricing or tenant filter logic. Responses contain typed business data and attachment counts, with no file bytes, disk/storage paths or raw AI-provider payloads. Reads need no request ID or source metadata and create no operation/replay record. Normal token last-used tracking still applies.
- Delivery covers read access only. Expense mutations, posting/confirmation, attachment download and report tools stay outside this change.
- Add migration `0057` after `0056` to widen the existing token-scope CHECK constraint. Preserve published migrations, existing token rows and operation history. Downgrade must fail transactionally if any stored token still contains `expense:read`, preserving data and the wider constraint. Operators restoring an older release use a pre-upgrade backup when this guard applies.

## Atomic implementation step

### Step 1 · Expense read contract, authorization and clients

- Define schemas first, then extend the scope enum, guarded MCP registration and service delegation. Update the token UI/types and matching EN/ZH labels, regenerate OpenAPI TypeScript, and add the migration plus focused regressions.
- Update current full-workflow Hermes configs/skills and deployment documentation to 18 tools/nine scopes and describe read-only expense access. Receipt-only and unrelated configurations keep their own scope. Preserve M15's frozen original 16-tool/eight-scope history, with a current-extension pointer. Update paired README/roadmap/SI4 docs. Prepare consistent project version 0.13.0 without dependency churn.
- Tests cover real runtime-role reads, list/detail consistency, filters/pagination, empty list, nonexistent/foreign IDs, malformed input and safe errors, insufficient scope, revoked credentials including established sessions, compatibility of old tokens, and absence of business mutations/operation rows. Verify migration upgrade with existing tokens, unknown-scope rejection, downgrade guard and safe downgrade when no new-scope rows exist. UI coverage verifies the ninth permission.
- Required gates are Ruff, strict Mypy, default pytest, integration pytest against a fresh owned PostgreSQL container, regenerated types with no drift, frontend tests/build and Docker build. Gate evidence may combine local verification and final exact-commit CI, with unrun checks explicitly identified.
- Independent review receives this original design, roadmap/M15 context and full step diff. Review priorities include scope-to-tool mapping, RLS, migration compatibility, safe failure behavior, API/schema consistency, existing token behavior and matching bilingual configuration. Fixer receives relevant review and implementation reports. The orchestrator owns all Git writes.

## Independent acceptance and release

After Step 1 passes review, a fresh subagent exercises the actual Streamable HTTP MCP server on an owned temporary loopback port and disposable PostgreSQL database with synthetic fixtures for two companies. Exercise discovery, filtered/paginated lists, details, missing/foreign IDs, malformed input, missing scope and revocation. Compare business row/operation counts before and after. Record commands, expected/actual results and clean up owned resources. Existing production/development/walkthrough environments and live Hermes stay outside the test.

The author delegated this acceptance to a subagent. Distinguish actual MCP client acceptance from human UI/Telegram checks. Commit/push follow successful review and independent acceptance. Release follows all required exact-commit CI gates. Publish 0.13.0 for linux/amd64 and linux/arm64, verify registry tag digests and the pulled artifact, and report its multi-architecture SHA-256. Keep all process briefs and release evidence local in ignored `review-notes/`.

## Acceptance conclusion (2026-11-10)

- One atomic implementation and two repair rounds are complete. The final fresh reviewer found no actionable issues. Root input validation, revoked-token downgrade documentation and the existing receipt discovery regression were verified.
- Local gates passed 1,705 default backend tests, 1,303 full PostgreSQL integration tests, Ruff, strict Mypy, seven Node and 431 frontend component tests, frontend build, generated types and Docker build. Independent review also completed 26 relevant PostgreSQL tests and protocol probes after repairs.
- A fresh subagent passed 19/19 scenarios using dedicated PostgreSQL, a temporary loopback TCP server and two synthetic companies. Coverage includes discovery, filtering/pagination, detail, empty lists, real foreign/missing IDs, invalid arguments, insufficient scope and established-session revocation. Expense snapshots and Agent operation counts matched before/after.
- Company-isolation verification follows the existing Expense service boundary and transaction-local company context under the runtime role, consistent with roadmap §3.3.
- The author delegated this automated acceptance and authorized commit/push and release. Human UI, Telegram and live Hermes checks were not performed and are distinguished from automated passes.
- CI and image publication outcomes are recorded in GitHub Actions and the v0.13.0 Release. Detailed local briefs, repairs, acceptance evidence and optional human steps remain in ignored `review-notes/SI4-expense-read-20261109/`.

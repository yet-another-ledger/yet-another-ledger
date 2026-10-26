# SI2 · Revising Undelivered Standard Invoices
> 🌐 **English** · [中文](SI2_zh.md)

> **Status**: Design agreed on 2026-10-26; all five steps completed automated implementation, full local gates and independent blind review on 2026-10-27. Author walkthrough and acceptance remain pending. SI2 is a separately numbered small improvement, not a milestone. It follows the milestone implementation-plan format. M15 remains paused; SI2 neither depends on nor resumes its development branch.

## Goals & Scope

**Goal**: Allow an issued Standard Invoice to be revised before customer delivery and before payment, retaining its status and number with a complete revision history.

**IN**:
- Independently created Standard Invoices and Standards converted directly from Quotes without advance receipts.
- Revision of dates, reference, notes, content blocks, ordered lines and monetary inputs.
- Backend eligibility, revision calculation preview, atomic save and revision history.
- Preservation of the original invoice number, issue identity and retained PDF/email history.
- Clear access to existing correction workflows once the Invoice has been delivered.

**OUT / deferred**:
- `SENT -> DRAFT`, any new lifecycle status, or reopening an issued document for deletion/cancellation.
- Revision of Advances, Finals, Credit Notes or Standards involving receipt-only deposits, refunds, credits, replacement/compensation relations or formal applications.
- In-place financial revision after customer delivery.
- Generic database auditing, restoring old revisions, historical email resending or IMAP backfill.
- Filing-state tracking, filed-period locks or automatic tax-return corrections.
- A new Terms & Conditions template system or a Dictionary navigation change.

**T&C decision**: Reuse **Content Library -> Content Blocks -> Terms & Conditions**. Multiple named blocks and an optional default already exist and can be selected in Quote/Invoice editors. Applying a block copies its text; later template edits/deletion do not rewrite saved documents. No implementation step is needed for this second request.

**Related docs / existing foundations**: [roadmap](../roadmap.md), [M5](../milestones/M5.md) pricing and Invoice CRUD, [M6](../milestones/M6.md) content snapshots, [M7.5](../milestones/M7.5.md) rounding, [M9](../milestones/M9.md) PDF/email, [M12](../milestones/M12.md) document chains and credit basis, [M13](../milestones/M13.md) historical PDF retention, and [SI1](SI1.md) ordered lines and Sent-copy behavior.

## Execution Model

- Manual mode remains the default under `AGENTS.md`. This plan itself does not authorize orchestration, implementation commits or pushes; follow the author's instructions for each implementation round.
- Each step is bounded and independently deployable, with its contract defined first and the [roadmap §5](../roadmap.md#5-atomic-step-template--definition-of-done) gates passing. Blind-review points support independent review or a later explicitly requested orchestration workflow.
- Write Chinese implementation briefs only to ignored local `review-notes/SI2-step<n>-impl.md`. At issue closure, write `review-notes/SI2-report.md` containing automated evidence and the complete manual walkthrough. Never track these files.
- Use automated gates per step and one author walkthrough at issue closure. Automated verification uses isolated databases and mail substitutes, never production data, containers or customer mailboxes.
- SI2 is an explicit, bounded exception to the earlier issued-document edit prohibition. Do not rewrite completed milestones' historical acceptance records or extend the exception to other document kinds.

## Frozen Product & Architecture Decisions

- [x] **D1 · Preserve issued identity**: revision keeps `SENT`, invoice/sequence/customer-sequence numbers, first `issued_at`, issuing user, and the frozen seller/buyer identity snapshot. It never reissues the document.
- [x] **D2 · Delivery boundary**: any successful Invoice email log blocks revision. In the absence of such a log, Save still requires explicit confirmation that the Invoice has not been delivered through another channel. A PDF download alone is not proof of delivery. SI2 adds no automated detection of external delivery.
- [x] **D3 · No effective payments**: inspect actual non-deleted Payment records, not only `paid_status` or cached totals. Partial payment also blocks revision.
- [x] **D4 · No dependent correction/settlement chain**: an existing Credit draft or issued Credit, refund, replacement/compensation relation, Advance/Final application or receipt-only payment provenance blocks revision. Do not delete or invalidate dependent records to make a revision eligible.
- [x] **D5 · Fixed customer and currency**: dates, reference, notes, content blocks, lines, quantities, prices, discounts and tax selections may change. Customer, currency, numbering, lifecycle and provenance cannot change through revision.
- [x] **D6 · No implicit master-data refresh**: date/text-only revisions preserve all monetary/tax snapshots. Financial changes use the existing backend pricing core. Unchanged tax selections retain their frozen snapshots rather than drifting with later dictionary edits; catalog changes do not overwrite entered unit prices.
- [x] **D7 · Consistent current credit basis**: line revisions also maintain the current source basis used by later Credits. Do not call the ordinary draft-update path and assume a subsequent issue transition will repair snapshots: the existing issue foundation returns early when the party snapshot already exists.
- [x] **D8 · Transactional audit**: every actual revision records actor, timestamp, required reason, undelivered confirmation and complete before/after business snapshots in the same transaction as the change. Failures leave no partial revision.
- [x] **D9 · Preserve historical output**: never rewrite/delete existing PDF artifacts, email logs or attachment associations. Live preview and subsequent Download/Send use current content under the existing M12/M13 presentation-fingerprint rules; identical presentations may still reuse existing exact bytes.
- [x] **D10 · No automatic email**: calculation, revision Save and history viewing never send mail or append a Sent copy.
- [x] **D11 · One current reporting fact**: update the existing Invoice; a revision is not another revenue/VAT event. Date changes can move the Invoice between current report periods. Show the old/new periods for a cross-period date change without claiming to know whether either period has been filed.
- [x] **D12 · Corrections after delivery**: reduce a delivered Invoice with a partial Credit, increase it with an additional ordinary Invoice identifying the original, or redo it through full Credit plus replacement. Reuse existing workflows; no new supplemental-invoice business model.
- [x] **D13 · No deletion escape**: an issued numbered Invoice remains non-deletable. Revision eligibility grants no new cancellation, deletion or status-transition permission.

## Contract (Define First)

### Revision changes and preview

Define `InvoiceRevisionChanges` as a closed field whitelist:

- Omitted fields retain their current values; explicit `null` clears only nullable fields.
- Header fields: `invoice_date`, `due_date`, `supply_or_advance_date`, `reference_number`, `notes`, `warranty_text`, `terms_text`, `bank_text`, `payment_terms_text`.
- Pricing fields: `tax_mode`, `amounts_include_vat`, `vat_treatment_id`, `document_vat_rate_id`, document `discount`, and `lines`.
- When supplied, `lines` is the complete target ordered array, with at least one line. Each row carries the existing raw line inputs and an optional `line_id`: retained rows identify their original line; new rows omit it; original rows absent from the array are deleted.
- Reject duplicate, unknown or foreign-Invoice line IDs. Reject protected fields, including customer, currency, number, status, provenance and client-calculated amounts. Do not accept arbitrary ORM/read-model payloads.

`POST /api/v1/invoices/{invoice_id}/revisions/calculate`:

- Request `InvoiceRevisionCalculateRequest { expected_updated_at, changes }`.
- Response `200 InvoiceCalculationRead`, reusing the current calculation-result structure and request-array ordering.
- Read the original Invoice consistently, validate the requested version, merge the changes and calculate without persistence, audit/event/artifact creation, numbering or email.
- A preview does not reserve eligibility. Save rechecks all conditions after acquiring locks.

### Save and history

`POST /api/v1/invoices/{invoice_id}/revisions`:

- Request `InvoiceRevisionCreate { expected_updated_at, changes, reason, confirm_not_delivered }`.
- `reason` must contain non-whitespace text; `confirm_not_delivered` must be explicitly `true`.
- Response `200 InvoiceRead`, including current backend-authoritative revision eligibility.
- After locking, compare `expected_updated_at` to the persisted Invoice version. A stale request returns `409` without applying any part of it.
- An otherwise valid request with no actual change returns the current Invoice without a revision/event or an `updated_at` change.

`GET /api/v1/invoices/{invoice_id}/revisions`:

- Paginated `InvoiceRevisionListRead { items, total }` using existing list conventions, ordered by the database-generated revision order, newest first.
- Each `InvoiceRevisionRead` exposes identity, stable order, actor, timestamp, reason, undelivered confirmation and complete typed before/after business snapshots.
- No update/delete/restore endpoint for revisions.

Extend Invoice detail with `revision_eligibility { available, reason_code }`. Reuse one backend eligibility predicate for read projections and locked commands. Do not introduce per-row eligibility queries into Invoice lists.

### Authorization, errors and compatibility

- Use existing authenticated MFA owner guards and company RLS. Non-owner -> `403`; missing/cross-company Invoice -> `404` without history disclosure.
- State, effective-payment, delivery, related-chain and stale-version conflicts -> `409` with stable reason codes. Invalid field/date/amount/reference/reason/confirmation -> `422`.
- Keep ordinary draft PUT, calculation and status endpoints compatible. They must not permit `SENT` changes that bypass the revision audit.
- Regenerate committed OpenAPI TypeScript types whenever the contract changes. Routes remain thin; eligibility, merge, pricing and persistence live in `services/`.

## Data Models / Migrations

### New revision table

Add `invoice_revision` with:

- UUID `id`, `company_id`, `invoice_id`, nullable `actor_user_id`, timezone-aware `created_at`.
- A database-generated, unique stable order value; never allocate it with `max+1`.
- Non-empty `reason` stored as `text`, and the explicit undelivered confirmation.
- `before_snapshot` and `after_snapshot` JSONB, validated by a dedicated, versioned Pydantic business-snapshot schema. Include header/raw fields, ordered lines, tax snapshots, calculated amounts and corresponding credit basis.
- Serialize Decimal as strings and dates as ISO values. Exclude credentials, raw request envelopes, ORM state and arbitrary context.
- Company RLS, Invoice/company ownership validation and append-only runtime permissions. Follow existing FK/ORM ownership conventions; no hand-written cascade deletes.

### Existing event trail

- Add `DocumentChainEventType.INVOICE_REVISED`.
- Define closed safe event metadata referencing the revision ID. Complete text and before/after content belong in the revision table, not generic event metadata.
- Persist the revision, current Invoice changes and event atomically.

### Upgrade and legacy rules

- Add migration(s) on the current linear `main` head, separate from paused M15 work.
- Do not modify/recalculate legacy Invoices, payments, numbers, PDF artifacts, email logs or report data.
- Do not invent earlier revisions. The first new revision snapshots the then-persisted state.
- Test fresh creation, production-shaped upgrade and isolated downgrade/upgrade with the runtime role and company isolation.

## Atomic Steps Checklist

### Step 1 · Audit foundation and read-only revision eligibility

- **Status**: Automated implementation, full local gates and independent blind review complete (2026-10-26); author walkthrough pending.
- **Goal**: establish revision storage, history reads and reliable eligibility without enabling revision writes.
- **Contract**: define snapshot/history/eligibility schemas and stable error codes; expose history reads and detail eligibility; regenerate TS types.
- **Backend**: add models, migration and history service/API. Extend the existing action-eligibility mechanism with a shared pure predicate supplied with actual payment, successful-mail and related-chain facts. Detail reads remain non-mutating.
- **Frontend**: type preparation only; no clickable revision action yet.
- **Migration**: revision table, indexes, RLS/runtime privileges/ownership checks and revision event type.
- **Tests**: standalone/direct-conversion Standards; excluded kinds/modes; partial payments; successful versus failed mail; Credit drafts; cross-company/non-owner access; empty/paginated/stably ordered history; migration compatibility and runtime-role protections.
- **Blind-review points**: no writes in eligibility queries; downloads are not delivery evidence; unissued dependent Credits are not overlooked; no historical reconstruction or Invoice-list N+1 queries.
- **DoD**: roadmap §5; write local `review-notes/SI2-step1-impl.md`.

### Step 2 · Revision preview with preserved tax snapshots

- **Status**: Automated implementation, full local gates and independent blind review complete (2026-10-26); author walkthrough pending.
- **Goal**: preview revisions with the same merge and calculation rules later used by Save.
- **Contract**: implement `InvoiceRevisionChanges`, original-line identity and `/revisions/calculate`; regenerate types.
- **Backend**: consistently read the original and merge whitelisted changes. Determine actual field changes: preserve amounts for header/text-only edits, otherwise invoke the existing pricing core.
- **Tax resolution**: for a retained line with the same rate selection, use that line's frozen rate. New lines and explicit changed selections resolve the selected valid dictionary record. Apply equivalent snapshot-preservation rules to treatment and document-level rate. Support different frozen percentages for the same rate ID across lines; a single current dictionary value cannot override them.
- **Pricing**: do not refresh unit prices from the catalog. Preserve M7.5 rounding and SI1 ordering behavior. Put any snapshot-input adaptation inside pricing services; add no second arithmetic engine.
- **Frontend**: generated types only; the editor entry arrives in Step 4.
- **Migration**: none.
- **Tests**: date/text edits preserve amounts; add/delete/edit/reorder lines; quantity/price/discount/mixed VAT/inclusive input; changed dictionary records versus unchanged snapshots; deliberate tax changes; invalid/duplicate line IDs; stale versions; preview leaves every business table unchanged.
- **Blind-review points**: no ordinary draft preview silently refreshing VAT; old/new snapshots can coexist; omission and clearing differ correctly; money math stays in backend services.
- **DoD**: roadmap §5; write local `review-notes/SI2-step2-impl.md`.

### Step 3 · Atomic revision save and concurrency protection

- **Status**: Automated implementation, full local gates and independent blind review complete (2026-10-26); author walkthrough pending.
- **Goal**: commit one fully audited revision with consistent amounts, receivables and future credit basis.
- **Contract**: implement `POST /revisions` with reason, undelivered confirmation and version checking; regenerate types.
- **Backend**: follow existing Quote -> Invoice lock order, then reread the Invoice/version, payments, successful email and dependent documents. Reuse Step 2's merge/pricing implementation.
- **Persistence**: date/header-text changes do not rebuild amounts or lines. Line changes maintain relevant rows, tax snapshots and current credit basis through existing FK/ORM ownership. Update receivable/settlement caches, save before/after snapshots and append the revision event in the same transaction.
- **Identity**: retain `SENT`, all allocated numbers, first issue identity/time/actor and provenance. Do not rerun issue initialization or replace the party snapshot.
- **Concurrency**: revision, payment, Credit creation and sending use compatible parent locks. Prevent a revision from interleaving between email rendering and send-result commit; customer attachment, email content and log must describe the same version. Recheck rather than trust an unlocked discovery read.
- **Failure flow**: reject stale/repeated requests with `409`; valid no-op creates no history; any failure rolls back all revision state. Ordinary PUT continues to reject `SENT` edits. No automatic email or retries.
- **Frontend**: generated types only; keep the revision entry hidden until Step 4 completes the workflow.
- **Migration**: none; use Step 1's storage.
- **Tests**: before/after values; amount/receivable/basis conservation; payment and Credit after revision; repeat submission; concurrent revisions/payments/SMTP/Credit creation; injected mid-save failures; history immutability; alternate endpoints cannot bypass audit.
- **Blind-review points**: no check/write race, stale ORM projection, temporary DRAFT, issue-snapshot refresh, orphaned/stale basis or hand-written cascade cleanup.
- **DoD**: roadmap §5; write local `review-notes/SI2-step3-impl.md`.

### Step 4 · Revision editor and history workflow

- **Status**: Automated implementation, full local gates and independent blind review complete (2026-10-27); author walkthrough pending.
- **Goal**: complete revision in the existing Invoice workspace and distinguish it clearly from post-delivery corrections.
- **Contract**: consume generated eligibility, preview, save and history types; no new calculation logic in Vue.
- **Backend**: reuse the completed eligibility, calculation, save and history services; no additional business endpoint or calculation algorithm.
- **Frontend**: show Revise when backend eligibility allows it; reuse the editor and line ordering while locking customer, currency and numbering. Use the revision preview and ignore superseded calculation responses.
- **Confirmation**: show the main changes, require a reason and explicit undelivered confirmation. For date changes crossing report periods, show old/new periods without asserting filing knowledge.
- **Save/cancel**: disable duplicate submissions; preserve local input on failure/conflict and ask the user to reload current data instead of auto-overwriting/retrying. Reuse the existing unsaved-change guard on cancellation/navigation.
- **History**: show actor, timestamp, reason, field before/after values and ordered lines as read-only escaped text, never executable HTML.
- **Delivered documents**: explain the boundary and expose existing Credit/replacement entry points. For an increase, use ordinary Invoice creation and identify the original via existing reference/notes fields; add no new supplemental-document model.
- **T&C**: reuse existing content-block selection in the revision form. Applying a template is still a text copy; saved documents do not track later template changes. Add no Dictionary page or duplicate template store.
- **Migration**: none.
- **Tests**: eligibility/read-only fields; reason/confirmation; explicit clears; no-op/cancel; loading/failure/conflict; rapid/stale previews; consecutive revisions/history; unsafe text rendering; EN/ZH; T&C selection and snapshot independence.
- **Blind-review points**: backend remains authoritative; revision never changes status or sends mail; errors retain user input; history text is escaped and no template lifecycle leaks into existing documents.
- **DoD**: roadmap §5; write local `review-notes/SI2-step4-impl.md`.

### Step 5 · Output/reporting regression, documentation and acceptance closure

- **Status**: Automated implementation, full local gates and independent blind review complete (2026-10-27); author walkthrough pending.
- **Goal**: finish cross-module verification and provide one complete author walkthrough.
- **Output regression**: existing artifact bytes/hashes/associations remain identical. Current preview and later Download/Send use revised content under M12/M13 fingerprints, including uploaded-artifact behavior and reuse for an unchanged presentation.
- **Business regression**: payment, partial/full Credit and replacement after a revision use the correct current basis. Excluded Advance/Final/other chains retain their prior behavior.
- **Reporting regression**: the Invoice is counted once; changed amounts/dates project correctly into P/L, BTW, ICP and Dashboard. Revision audit records never become extra accounting events.
- **Execute**: Ruff, strict mypy, default/PostgreSQL integration suites, migration/runtime-role RLS checks, OpenAPI->TS freshness, frontend tests/build, EN/ZH symmetry and Docker build.
- **Documentation**: synchronize this plan and its Chinese mirror and paired roadmap status. Document the limited exception to the issued-document edit prohibition without rewriting completed milestones' historical acceptance. M15 stays paused.
- **Reports**: write local `review-notes/SI2-step5-impl.md` and `review-notes/SI2-report.md`, including exact automated evidence, all manual steps below and unperformed checks.
- **Blind-review points**: evidence matches the final implementation; no post-delivery overwrite, extra Invoice kind, new T&C system or M15 work has slipped in.
- **DoD**: every roadmap §5 gate passes; mark SI2 complete only after author acceptance.

## 🟢 Deployment Self-Test Points (Issue Acceptance)

Use development Compose by default:

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

1. Create a Standard Invoice and mark it Sent. Record number, totals and first issue metadata; do not deliver it or record payment.
2. Revise only date, notes and due date. Verify `SENT`, number, issue identity and amounts remain intact; history shows reason and before/after values.
3. Revise unit price, quantity, discount, line order and tax selection. Check backend preview, saved totals, receivable and current PDF.
4. Retain a downloaded PDF before revising content. Verify that original artifact remains byte-identical and downloadable while current preview reflects the revision.
5. Open the same Invoice in two browser views. Save a revision in one; the other's stale Save must conflict without discarding its local input.
6. Successfully email a separate Invoice to a test recipient. Verify revision becomes unavailable. Inspect the confirmation wording for delivery through other channels; do not use real customer recipients.
7. Record a partial payment on another Invoice and verify revision is rejected. Also check a Credit draft and other excluded relations.
8. Create a partial Credit against a revised Invoice and verify its basis. Exercise full Credit plus replacement for a delivered Invoice needing complete rework.
9. Check the current Invoice contributes once to reports. Verify cross-period date changes agree with the period warning and current report data.
10. Create two T&C blocks in Content Library and apply them to different Quotes/Invoices. Edit the template afterward and confirm saved document texts remain unchanged. This verifies existing behavior rather than a new template feature.

The issue report must distinguish author-exercised checks from automated-only coverage; never record an unperformed check as passed.

## Acceptance Conclusion

- Implementation completion date: 2026-10-27 (automated implementation of Steps 1–5).
- Automated gates: local Ruff, strict mypy, 1,411 default backend tests, all 1,150 PostgreSQL integration tests (three disjoint file shards on isolated databases, including migration/runtime-role RLS), OpenAPI→TS freshness, 26 Node tests, 234 component tests, frontend build, EN/ZH 1,586-key symmetry and Docker build passed. All independent review findings are closed.
- Remote CI: not run; no push was performed. Local checks do not claim a remote GitHub Actions result.
- Author walkthrough: pending; none of the ten deployment points has been recorded as author-passed in this run. An isolated Compose/mail-substitute walkthrough is provided in the local issue report.
- Paths accepted on automated evidence rather than manual walkthrough: none accepted by the author yet; automated coverage is not author acceptance.
- Known carryovers/deferred items: author walkthrough and remote CI remain outstanding; M15 stays paused. No unresolved implementation/review findings; the existing Vite chunk-size warning is non-blocking.

All five atomic steps are implemented and independently reviewed. SI2 remains unaccepted until the author walkthrough is settled. Completed milestones' historical acceptance is unchanged; M15 remains paused.

# SI1 · Document Ordering, Quote Duplication and Sent Copies
> 🌐 **English** · [中文](SI1_zh.md)

> **Status**: Design agreed on 2026-10-25; implementation not started. SI1 is a small-improvement issue, not a milestone. Its plan uses the milestone implementation format and lives beside `milestones/` in `improvements/`. M15 remains paused; SI1 neither depends on nor resumes its development branch.

## Goals & Scope

**Goal**: Improve document editing, quote reuse and email traceability through three bounded additions.

**IN**:
- Drag-to-reorder lines in Invoice, Quote and Estimate editors.
- Duplicate an existing Quote into an independent new draft.
- Save every newly sent email, including its attachments, in the provider's server-side Sent folder when enabled.

**OUT / deferred**:
- Estimate group reordering, drag-to-change-group and regrouping the editor layout.
- A new rounding algorithm, a dedicated ordering API, or separate display/calculation orders.
- Quote revision chains, version comparisons or automatic replacement of older quotes.
- Sent-copy retry queues, automatic retries, manual copy-only retry and historical email backfill.
- Product-manual PDF upload/storage and checkboxes for choosing additional quote attachments. Keep this as a candidate for a separately designed future milestone; reserve no schema or milestone number now.

**Related docs / existing foundations**: [roadmap](../roadmap.md), [M5](../milestones/M5.md) pricing and Invoice CRUD, [M6](../milestones/M6.md) Quote CRUD and numbering, [M6.5](../milestones/M6.5.md) Estimate grouping, [M7.5](../milestones/M7.5.md) rounding, [M9](../milestones/M9.md) PDF/email, and [M12](../milestones/M12.md)/[M13](../milestones/M13.md) formal document artifacts.

## Execution Model

- Manual mode remains the default under `AGENTS.md`. This plan does not authorize orchestration, implementation commits or pushes; follow the author's instructions for each implementation round.
- Each step is a bounded, independently deployable change, with its contract defined first and the [roadmap §5](../roadmap.md#5-atomic-step-template--definition-of-done) gates passing. Review points below also support a later explicitly requested blind-review workflow.
- Write Chinese implementation briefs as local ignored `review-notes/SI1-step<n>-impl.md`. At issue completion, write `review-notes/SI1-report.md` with the integrated manual walkthrough. Never track those files.
- Use automated gates per step and one author walkthrough at issue closure. Do not touch production data, containers, files or mailboxes for automated verification; use isolated databases and mail substitutes.

## Frozen Product & Architecture Decisions

- [x] **D1 · Reuse ordinary save and calculation**: dragging changes the frontend line array; ordinary Save persists its order through existing `sort_order`. Recalculate preview after reordering. Keep the existing rule that the last eligible line absorbs document-discount rounding remainder. The author accepts small rounding differences rather than expanding this issue; this is not a new guaranteed numerical bound.
- [x] **D2 · Preserve existing editing boundaries**: enable dragging only where the current editor permits line edits. Already delivered email attachments and retained formal PDF bytes are not rewritten. A newly rendered editable document follows its newly saved order.
- [x] **D3 · Estimate sorting is internal**: preserve group membership, group order and group-to-Quote aggregation. Estimate has no new PDF endpoint; generated Quote groups retain their existing ordering, and the Quote editor can reorder its public lines.
- [x] **D4 · A duplicate is a new Quote**: allocate a new number, set `DRAFT`, use today's quote date and the company's current default validity period. Copy content and raw price inputs, then calculate through existing services using current VAT configuration.
- [x] **D5 · No copied business history**: do not copy payments, settlement mode/locks, conversion links, document-chain history, email logs or artifacts. The source and duplicate remain independent; no version-management model is added.
- [x] **D6 · Sent copying is opt-in and applies to all new mail**: default off; when on, include quotes, every supported invoice kind, payment receipts, refund confirmations, test emails and password resets. Reuse the effective SMTP username/password for IMAP; do not add separate credentials.
- [x] **D7 · Separate SMTP and IMAP outcomes**: only attempt the copy after SMTP accepts the message. IMAP failure does not turn the send into a failure, roll back its business/artifact result, or trigger another SMTP send. No automatic retry or later copy-only recovery is included.
- [x] **D8 · Copy the complete message**: use the same serialized MIME message for SMTP and IMAP, including body, To, CC and every attachment. Keep Date and Message-ID stable between stages. Hold the bytes only for the current request; add no persistent raw-mail store.
- [x] **D9 · Resolve an existing server folder**: use an explicitly configured folder when supplied. Otherwise discover the unique selectable `\Sent` mailbox; if none exists, try an existing selectable `Sent`. Multiple matches or no usable folder require explicit configuration. Do not silently choose an arbitrary mailbox or create one.
- [x] **D10 · Secure and bounded IMAP access**: support SSL/TLS (default port 993) and STARTTLS (normally 143), verify certificates, and provide no plaintext mode. Changing security mode does not silently replace an explicitly entered port. Bound network waits and close connections reliably. If the provider already saves SMTP mail automatically, leave the switch off to avoid duplicate copies; SI1 does not promise provider-independent deduplication.

## Contract (Define First)

### Document ordering

- Reuse existing create/update/calculation endpoints and request line arrays. Array order remains authoritative for persistence and output.
- Add no ordering endpoint or schema field. Stable client row keys are local UI data and must not enter business payloads.

### Quote duplication

- `POST /api/v1/quotes/{quote_id}/duplicate`, no request body → `201 QuoteRead`.
- Use the existing authenticated MFA owner guards, company isolation and numbering mechanism. Missing/inaccessible source → `404`; ordinary invalid references/configuration follow existing Quote validation errors.
- Preserve customer, reference number, currency, tax mode, VAT treatment/rate selections, discount inputs, notes/content text and ordered raw line inputs. Do not reread catalog prices to overwrite copied unit prices.
- All source Quote statuses may be duplicated, including accepted or converted quotes. The new record always starts with fresh identifiers, `DRAFT`, unset settlement mode, no conversion/history links and newly calculated amounts.
- Lists copy the saved source. In an existing editor, reuse the current save-and-continue/cancel guard for unsaved changes. Do not show Duplicate for an unsaved new Quote. On success navigate to the new editor.

### SMTP settings and Sent-copy results

Extend `GET/PUT /api/v1/settings/smtp` with typed nested `sent_copy` configuration:

| Field | Default / meaning |
| --- | --- |
| `enabled` | `false` |
| `host` | Empty; required when enabled |
| `port` | `993`; valid TCP port |
| `security` | `SSL_TLS`; alternative `STARTTLS` |
| `folder` | `null` for discovery; blank input normalizes to `null` |

- Omission of `sent_copy` in an older PUT preserves the stored value; older stored settings without it read as disabled. Preserve existing password keep/update/clear semantics and masking.
- Resolve IMAP credentials from the effective SMTP configuration, including the current environment-variable fallback, while retaining the saved Sent-copy configuration even when SMTP falls back to environment variables. Missing usable credentials produce a safe configuration failure, never a second credential form.
- Add `sent_copy_status: NOT_ATTEMPTED | SAVED | FAILED`, nullable `sent_copy_error_code` and nullable `sent_copy_saved_at` to `EmailLogRead`. SMTP failure or disabled copying → `NOT_ATTEMPTED`; successful APPEND → `SAVED`; a failed/uncertain copy attempt → `FAILED`. Keep `status`/`sent_at` strictly about SMTP.
- `POST /api/v1/settings/smtp/test` retains `202` and `status="sent"` after SMTP success, adding the same copy-result fields through a typed response. IMAP failure returns a warning result rather than a failed SMTP test. SMTP failure retains the existing error response, with safe error details.
- Password-reset responses retain their current external semantics. Record copy failure through safe structured runtime logging rather than adding reset-email rows to the business email-log model.
- Generate TypeScript types after contract changes. Business routes remain thin; sending and copy logic stay in `services/`.

## Data Models / Migrations

- Sent-copy settings stay in the existing GLOBAL SMTP JSONB setting; no new settings table or credential columns.
- Add `email_log.sent_copy_status` (non-null, default `NOT_ATTEMPTED`, constrained to the typed values), `sent_copy_error_code` (nullable text) and `sent_copy_saved_at` (nullable timezone-aware timestamp).
- Existing rows receive `NOT_ATTEMPTED` and null details; do not infer historical copy success or change prior SMTP status, artifacts or document values.
- Extend the ORM, serializers and failed-email-log reconstruction helpers consistently. Keep existing company ownership, RLS and FK behavior; no new business table or raw-message storage.
- Add a migration on the current linear main-branch head. Test fresh and production-shaped upgrade and isolated downgrade/upgrade. Do not use the paused M15 migration chain.

## Atomic Steps Checklist

### Step 1 · Drag-to-reorder document lines

- **Goal**: Invoice, Quote and Estimate editors can reorder lines, save them and recover the same order on reload and subsequent output.
- **Contract**: no API changes; use ordered line arrays. Expected generated-type drift is zero.
- **Frontend**: use the Vue 3 `vuedraggable` component with a shared handle-based ordering container. Keep normal text selection/input separate from dragging. Add stable keys to Invoice/Quote rows and reuse Estimate keys; cover loaded records, templates and new rows. Disable dragging while saving/loading or when current line-edit rules forbid it.
- **Preview**: capture keys with each calculation request, bind returned amounts to those keys, and ignore superseded results. Reordering triggers ordinary backend preview; no money calculations in Vue.
- **Backend/PDF**: reuse existing array-to-`sort_order`, ORM read ordering and PDF rendering; change only an ordering defect demonstrated by the regression tests. Keep pricing and Estimate group aggregation unchanged.
- **Migration**: none.
- **Tests**: create/edit, first/last moves, duplicate-content rows, add/delete then reorder, preserved input state, rapid moves and stale preview responses, failed save, reload, and read-only states. Check persisted order and Invoice/Quote HTML/PDF order, including a sorted Quote converted to an Invoice. Estimate membership/group order and generated public groups remain unchanged.
- **Money regression**: fixed document discounts, mixed VAT rates and remainder cases match the existing engine for the same reordered inputs; do not assert order-invariant totals. Existing delivered/retained artifact bytes remain unchanged.
- **Blind-review points**: stable keys cover every row source; no leaked UI key, frontend calculation, dedicated sorting API, new edit permission or group behavior.
- **DoD**: roadmap §5; write local `review-notes/SI1-step1-impl.md` after implementation.

### Step 2 · Complete Quote Duplicate workflow

- **Goal**: create and open an independent duplicate from the list or an existing editor.
- **Contract**: define the new route and `QuoteRead` result first; regenerate TS types.
- **Backend**: implement duplication in the Quote service using a consistent source read and the existing create/numbering/pricing services. Copy the agreed raw fields and order; reset dates, state and associations. Preserve source data and use existing tenant protections rather than introducing a separate scoping convention.
- **Frontend**: add list/editor actions, loading and failure states, save-and-continue/cancel handling, success navigation and EN/ZH copy. Block repeated clicks while the request is active.
- **Migration**: none.
- **Tests**: every source status, converted quotes, fresh/concurrent numbering, current date/default validity, field/order copying, original unit prices after catalog changes, recalculation after VAT configuration changes, cleared history/links, independent edits/deletion, invalid references, missing source and authorization/company boundaries.
- **Blind-review points**: no copied ORM identities/history, catalog repricing, source mutation, version-chain model or change to the accepted-quote edit guard.
- **DoD**: roadmap §5; write local `review-notes/SI1-step2-impl.md`.

### Step 3 · Save complete outgoing mail to server-side Sent

- **Goal**: every successful outgoing send attempts a provider-side copy when enabled and reports the two outcomes accurately.
- **Contract/migration**: first define the settings/result schemas, compatibility rules and log migration above; regenerate types before frontend integration.
- **Backend**: build the message once, add Date/Message-ID, serialize with SMTP line endings, and pass the same bytes to SMTP and IMAP. Use standard-library `imaplib` through a worker thread with bounded socket waits; verify TLS, authenticate with effective SMTP credentials, resolve/encode the folder correctly, APPEND with `\Seen` and an appropriate internal date, then close the connection.
- **Failure flow**: separate SMTP exceptions from IMAP exceptions. An accepted SMTP send remains `SENT` even if copying fails; retain its PDF artifact and business result. A connection-cleanup failure must not overwrite an already acknowledged APPEND success. Treat an uncertain APPEND outcome as a copy failure without automatic retry. Record only stable safe error codes, never raw protocol responses, credentials or MIME content.
- **Coverage of senders**: wire the shared path for business mail, tests and password resets. Persist business-copy results, return test-copy results, and log password-reset-copy failures safely. Add no artificial business-log association for system mail.
- **Frontend**: extend the existing SMTP form with the switch, IMAP host/port/security/folder and explanatory copy. Show SMTP and Sent test outcomes separately. Update the send dialog and email-log panel to distinguish send failure from "sent, but saving to Sent failed"; include EN/ZH and loading/error states. Explain that a provider which already auto-saves sent mail should use the switch off.
- **Tests**: disabled/default behavior; older settings/PUT compatibility; password masking/preservation; environment fallback; SSL/TLS and STARTTLS; configuration/auth/TLS/timeout failures; explicit, discovered, missing and ambiguous folders; spaces/non-ASCII folder names; equal body/CC/attachments/Message-ID across stages. Use substitutes for network calls, including all sender entry points.
- **Failure regression**: SMTP failure never calls APPEND; IMAP failure does not resend SMTP, change SMTP success, roll back a retained PDF or leak secrets; historical logs survive migration; known-successful APPEND survives cleanup failure.
- **Blind-review points**: no sender bypasses the shared path; no IMAP exception enters the SMTP-failed branch; settings preserve credentials and environment fallback; no queue, history backfill, arbitrary folder creation or raw-mail disk store.
- **DoD**: roadmap §5; write local `review-notes/SI1-step3-impl.md`.

### Step 4 · Regression closure, documentation and issue report

- **Goal**: finish verification and give the author one complete issue-level walkthrough.
- **Execute**: Ruff, strict mypy, default and PostgreSQL integration suites, migration checks, runtime-role isolation, OpenAPI→TS freshness, frontend tests/build, EN/ZH key symmetry and Docker build.
- **Recheck**: document editing/PDF, Estimate→Quote→Invoice, payments and formal retained artifacts, all mail types and backward-compatible SMTP settings.
- **Documentation**: maintain this English plan and its Chinese mirror, paired roadmap entries, and SMTP/IMAP setup/troubleshooting details. Record the selected folder behavior, provider-side storage of attachments and the auto-save duplicate-copy switch guidance. Do not resume M15 or add the product-manual library.
- **Reports**: write local `review-notes/SI1-step4-impl.md` and `review-notes/SI1-report.md`, with automated evidence and the complete walkthrough below. Keep all review notes untracked.
- **Blind-review points**: evidence matches the final worktree; all three agreed capabilities are present; no deferred feature is included; bilingual documents agree.
- **DoD**: every roadmap §5 gate passes. Record SI1 as complete only after author acceptance; retain its separate small-improvement classification.

## 🟢 Deployment Self-Test Points (Issue Acceptance)

Use the default development Compose startup:

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

1. Create/edit each of the three document types, drag lines, save and reload. Verify the order survives and Estimate group memberships/group order do not change. Include a new line moved next to related existing lines.
2. Download and send the reordered Invoice/Quote; verify PDF order. Existing delivered attachments and retained formal PDFs remain unchanged. Read-only documents do not offer dragging.
3. Duplicate a Quote and verify its new number, today's date, default validity, copied content/order and independent state. Edit the duplicate and confirm the source remains unchanged. Exercise the unsaved-edit save/cancel guard.
4. Configure IMAP and send a test email and a business email with CC/PDF. Open the provider's webmail Sent folder, inspect body/recipients/CC and download the attachment to verify it matches the sent document. Use a test account to check password-reset copying.
5. Temporarily misconfigure IMAP while leaving SMTP valid. Send to a test recipient and verify delivery succeeds, the UI reports only copy failure, the business email log remains `SENT`, and no automatic resend occurs. Restore settings and confirm new mail copies successfully; the failed old copy is not backfilled.
6. Disable Sent copying and verify ordinary sending still works. If the provider already saves SMTP mail, leave the switch off and verify its native behavior instead of enabling a second copy path.

The final report must state which mail types were manually exercised and which rely on automated coverage; do not record an unperformed provider check as passed.

## Acceptance Conclusion (Fill at Closure)

- Completion date: Not implemented.
- Automated gates: Pending implementation; this design is not test evidence.
- Author walkthrough: Pending.
- Known carryover / deferred items: product-manual PDF management and attachment selection need a future separate milestone; Sent-copy retries/backfill and Quote version management remain out of scope. M15 stays paused.

## Protocol References

- [RFC 9051 §6.3.12 — IMAP APPEND](https://www.rfc-editor.org/rfc/rfc9051.html#section-6.3.12): store the complete message in a server-side mailbox.
- [RFC 6154 — Special-use mailboxes](https://www.rfc-editor.org/rfc/rfc6154.html): discover `\Sent`; some providers already place sent messages there automatically.
- [Python 3.12 imaplib](https://docs.python.org/3.12/library/imaplib.html): SSL/TLS, STARTTLS and APPEND client APIs.

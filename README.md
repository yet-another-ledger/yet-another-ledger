# Yet Another Ledger

> 🌐 **English** · [中文](README_zh.md)

Self-hosted invoicing and small-business administration for freelancers and small businesses. Invoices, quotes, expenses, payments, PDF + email, and Dutch **BTW (VAT) return** summaries — run with Docker Compose (a single app container plus PostgreSQL).

Built with **FastAPI + Vue 3 + PostgreSQL**.

> **Status — pre-1.0 (`v0.13.0`).** Open source and self-hosted. Container images are published to the GitHub Container Registry (GHCR). Expect rough edges and possible breaking changes before `v1.0`.

## Features

- **Invoices** — Standard, Advance, Final and source-bound Credit Notes; backend pricing, concurrency-safe numbering, independent lifecycle/settlement/credit states, and an auditable document chain.
- **Quotes** — reusable content blocks/templates, auto-expiry, and an immutable choice between direct invoicing, receipt-only deposits, or formal staged Advance/Final invoicing.
- **Quote PDF appendices (M17)** — keep reusable static PDFs in the Content Library or upload files to one saved Quote, choose and order them manually, and preview, download or email a single combined PDF. The author completed the recorded walkthrough scope on 2026-11-06.
- **Editing and reuse** — audited revisions for eligible undelivered, unpaid Standard Invoices, retaining SENT, the original number, unchanged tax/identity snapshots and historical PDFs; drag-to-reorder lines in Invoice, Quote and Estimate editors; duplicate a Quote into an independent draft with a fresh number and dates.
- **Cost estimation → quote** — internal margin-based costing that never leaks cost/margin to the customer.
- **Payments & refunds** — partial payments, Quote deposits, automatic settlement states, Credit-linked Refunds, and Refund Confirmations.
- **Expenses** — receipt inbox for review before posting, AI extraction from image/PDF originals, recurring expenses, bookkeeping fields (paid-by / business-use % / depreciation years), independent P/L inclusion, and NET/GROSS/AUTO cost handling.
- **External sales** — retain platform and energy originals with their original numbers, review optional AI suggestions, post multi-rate VAT summaries, and audit revisions, credits, settlement differences and voids; revenue can be excluded from P/L while remaining in VAT.
- **Home batteries** — register eligible ordinary-VAT acquisitions once, confirm annual private-use inputs for the five-year Q4 adjustment, retain evidence and revision history, and flag missing or unconfirmed inputs. Battery Assets navigation is hidden by default and can be enabled in Settings → Preference.
- **Customers & catalog** — addresses, VAT IDs, per-customer currency & document language; product/material catalog.
- **Documents** — EN/ZH PDFs and email for invoices, quotes, receipts and Refund Confirmations, with issue-time party snapshots, exact downloaded/sent artifact retention, and immutable historical Invoice PDF backfill with optional advisory AI comparison.
- **Reports** — Profit & Loss, **Dutch BTW VAT-return summary**, ICP listing, expense report, and an ECharts dashboard, including exactly-once Advance/Final/Credit projections and separate mileage costs and profit before/after mileage.
- **Platform** — TOTP two-factor auth, typed three-tier settings, `Decimal` money math, bilingual UI (English / 中文), single-container app deployed via Docker Compose.
- **Agent / MCP (M15)** — owner-managed revocable scoped tokens, receipt intake, customer lookup/create, and preview/create/update for Quote and Standard Invoice drafts, with durable replay and audit. Codex and Hermes clients are supported. Receipt confirmation and document send/issue remain web actions.

## Tech stack

| Layer | Choice |
| --- | --- |
| Backend | FastAPI · SQLAlchemy 2.0 (async) · Alembic · fastapi-users · PostgreSQL (asyncpg) · Python 3.12 (uv) |
| Frontend | Vue 3 + TypeScript · Pinia · Vue Router · Vite · Naive UI · ECharts |
| Packaging | Single app container (frontend build + backend + uvicorn) + PostgreSQL, run with Docker Compose |

## Quick start

Pull the published image and run it with Docker Compose (app + PostgreSQL).

**Prerequisites:** Docker + Docker Compose, and `git`.

```bash
# 1. Clone (for docker-compose.yml + .env.example)
git clone https://github.com/yet-another-ledger/yet-another-ledger.git
cd yet-another-ledger

# 2. Configure (the template already sets COOKIE_SECURE=false for local HTTP)
cp .env.example .env

# 3. Pre-create the receipt-storage folder owned by your user
#    (the app container runs as uid:gid 1000:1000 by default; set PUID/PGID
#    in .env if your user differs)
mkdir -p data/storage

# 4. Pull images and start (runs DB migrations, then app + PostgreSQL)
docker compose up -d
```

Open **http://localhost:8000**, register the first (owner) account, and set up TOTP two-factor authentication. The app is published on `127.0.0.1` only; put it behind a TLS reverse proxy for remote access.

To stop: `docker compose down` (add `-v` to also drop the database volume).

> `:latest` tracks the newest non-prerelease release; pin this release by setting `JAI_IMAGE=ghcr.io/yet-another-ledger/yet-another-ledger:0.13.0` in `.env`. To build the image yourself instead of pulling, run `docker build -t ghcr.io/yet-another-ledger/yet-another-ledger:latest .` before `docker compose up -d`.

## Configuration

Compose reads `.env` automatically. The most useful variables:

| Variable | Default | Notes |
| --- | --- | --- |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `jai` | Database credentials (shared by app + Postgres). |
| `APP_HOST_PORT` | `8000` | Host port the app is published on (loopback). |
| `COOKIE_SECURE` | `false` in `.env.example` | Must be `true` in production behind HTTPS. |
| `BASE_URL` | `http://localhost:8000` | Public URL used for absolute links in emails. |
| `STORAGE_DIR` | `./data/storage` | Host folder bind-mounted for receipts/attachments. |
| `PUID` / `PGID` | `1000` | Host uid:gid the app runs as (owns the storage folder). |
| `AUTH_SECRET` | auto | Auto-generated and persisted on first boot; set only to pin an external secret. |

### SMTP and server-side Sent copies

Configure SMTP under Settings after first login for document mail, payment receipts, refund confirmations, test mail and password resets. Saved SMTP settings with a host take precedence; otherwise the existing `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL`, `SMTP_FROM_NAME`, `SMTP_USE_TLS` and `SMTP_USE_SSL` environment fallback applies. Sent copying retains its saved settings when SMTP uses that fallback, and reuses the **effective SMTP username/password**; there is no separate IMAP login form. A provider may require an app password and permission for both SMTP and IMAP.

Sent copying is off by default. Enable it in the SMTP panel, enter the provider's IMAP host, and choose **SSL/TLS, usually port 993**, or **STARTTLS, usually port 143**. Certificates are verified; plaintext is unavailable. Changing security does not change an explicitly entered port: set the correct port yourself.

Use an existing folder's exact server name, including any hierarchy prefix, or leave Folder blank to discover the unique selectable special-use `\Sent` folder. If none is advertised, the app tries an existing selectable folder named `Sent`. Multiple special-use matches, no usable folder, or an incorrect explicit name require an explicit valid folder; the app never creates one or chooses arbitrarily. The copy stores the complete email **on the provider's server**, including body, To/CC and all attachments; verify it in webmail and download an attachment after the first test. If the provider already saves SMTP mail automatically, **leave the switch off** to avoid duplicate copies.

SMTP and Sent-copy results are separate. SMTP acceptance remains a successful send even if copying fails: the test result/send warning and business Email log show the copy outcome independently. `SAVED` means the server acknowledged APPEND; `FAILED` includes uncertain outcomes; `NOT_ATTEMPTED` means copying was disabled or SMTP failed. Historical log rows show `NOT_ATTEMPTED`; this does not infer whether an old mail exists in the provider's Sent folder. Password-reset responses keep their existing behavior; copy failures appear only as safe runtime log codes, not business mail rows.

For a copy failure, check the stable error code: `IMAP_CONFIGURATION_FAILED` means missing host/usable shared credentials; connection/TLS/timeout codes call for checking host, port, security, certificate and reachability; `IMAP_AUTH_FAILED` calls for checking IMAP access and the effective SMTP credentials; folder missing/ambiguous/invalid/failed codes call for checking the exact folder and discovery support; `IMAP_APPEND_FAILED` calls for checking write permission or quota. Other copy failures use `IMAP_COPY_FAILED`. Errors expose safe codes rather than raw provider responses, credentials or message content. There is **no retry or historical backfill**: restoring settings affects new mail only. Do not resend a delivered business email just to obtain a copy, as that sends another email to its recipients.

SMTP test failures keep the generic `SMTP_SEND_FAILED` API response. Server logs add fixed categories such as `SMTP_AUTH_FAILED`, `SMTP_SENDER_REJECTED`, `SMTP_RECIPIENT_REJECTED`, `SMTP_TIMEOUT`, `SMTP_TLS_FAILED` and `SMTP_CONNECTION_FAILED`, without logging provider text or credentials. Use the category to check the corresponding configuration before another manual test.

### Quote PDF appendices (M17)

Open **Content Library → PDF Materials** to upload company-private reusable static PDFs, search by title, preview/download originals, edit titles, and archive or restore library entries. In a saved Quote, use **PDF appendices** to select shared files or upload Quote-specific files, reorder the list, and save it with the Quote. New Quotes must first use **Save and continue** to create the Quote before a Quote-specific upload is available. A Quote-specific upload is retained for that Quote even if its pending selection is later abandoned; reopen the Quote and select it from that Quote's own-file list if needed. Removing an item from the saved selection does not delete its original.

Quote preview, download, and email use the same assembled PDF: the quotation first, followed by selected originals in saved order. Original static page content, dimensions, and orientation are retained; originals are not translated, rasterized, or rewritten. Replacing a library file means uploading another record; archive/restore and title changes do not rewrite existing Quote selections. Accepted Quotes remain viewable and downloadable, but their selection is read-only. Duplicated Quotes start without appendices, and conversion does not copy appendices to an Invoice.

The default typed server limits are 10 MiB per upload, 10 files per Quote, 15 MiB combined selected inputs, 15 MiB assembled output, and 200 total pages including the quotation. Deployments can override the corresponding typed settings (`MAX_APPENDIX_FILE_BYTES`, `MAX_QUOTE_APPENDICES`, `MAX_QUOTE_APPENDIX_INPUT_BYTES`, `MAX_QUOTE_PDF_BYTES`, `MAX_QUOTE_PDF_PAGES`). Only static, unencrypted PDFs without signatures or fillable forms are accepted. Unsupported active content and unsafe links are rejected or removed. A successful response is newly serialized, so combined PDFs need not be byte-identical between separate requests. Email encoding increases attachment size, and a mail provider may impose a smaller limit.

Appendix originals and their metadata are stored transactionally in PostgreSQL (`BYTEA`), not in `STORAGE_DIR`. Include the database in backup and restore procedures; a storage-volume-only backup does not cover them. The existing formal Invoice/Refund `DocumentArtifact` history is unchanged: M17 does not retain exact historical outgoing Quote PDFs or freeze the quotation body.

M17 introduced migrations `0053` and `0054` in v0.11.0. Existing Quotes start with an empty appendix list when upgrading from v0.10.0. Before any upgrade, back up PostgreSQL and the receipt storage. If `.env` explicitly sets `APP_VERSION`, update it to `0.13.0` for this release.

## Agent access and M15 upgrade

v0.13.0 exposes Streamable HTTP MCP at `/api/v1/mcp` inside the existing app image. In **Settings → Agent access**, the MFA-authenticated owner creates a named token, selects its scopes, copies the secret once into the client's private environment, and can revoke it later. Configure the client's HTTP endpoint as `http://127.0.0.1:<JAI-port>/api/v1/mcp` with `Authorization: Bearer <token>` using its private secret configuration. Keep the endpoint on the same host/private network and set JAI's `BASE_URL` separately for browser review links.

Available scopes are `receipt:intake`, `customer:read`, `customer:create`, `catalog:read`, `quote:read`, `quote:write_draft`, `invoice:read`, `invoice:write_draft` and `expense:read`. Existing tokens retain their original grants; create a replacement token to grant `expense:read`. Receipt-only access uses `receipt:intake`. The current complete workflow exposes 18 tools across nine scopes, including read-only Expense list/detail. The historical M15 scope remains 16 tools/eight scopes; see [M15](docs/plan/milestones/M15.md) and [SI4](docs/plan/improvements/SI4.md). Every call checks authorization, including replay. Each mutation requires a caller-generated stable `request_id`; retry identical arguments with that same ID, and use a new ID for a new operation. Generic CLI clients omit Telegram `source` metadata.

Hermes setup, role skills and full MCP allowlists are documented in the [client guide](docs/plan/milestones/M15-hermes-telegram.md). The skills are files installed in the selected Hermes profile. Original Telegram files use the [local streaming adapter](docs/plan/milestones/M15-local-receipt-adapter.md) when remote Base64 transfer is unreliable. This optional stdio adapter runs beside Hermes and needs an approved attachment-cache root, the local JAI URL and the token. Both the MCP endpoint and upload route are included in the same JAI image. After replacing a skill, start a fresh conversation to load its current rules.

Back up PostgreSQL and storage together before upgrading from v0.12.0. The normal Compose migration service applies `0057` after the already released `0055` and `0056`, widening the allowed Agent token scopes. Downgrade is blocked while any stored token row contains `expense:read`, including revoked tokens; revocation preserves the scope. If that guard applies when returning to an older release, restore the matching pre-upgrade PostgreSQL and storage backups together. Databases from old M15 previews can have conflicting `0044`/`0045` or `0053`/`0054` histories. Migration refuses ambiguous histories when Agent tables exist. Preserve a backup and reconcile separately, or prepare a fresh isolated test database. Never blindly stamp such a database. The [M15 plan](docs/plan/milestones/M15.md) records the acceptance scope and skipped/deferred checks.

## Development

Run the full dev stack (app + Postgres, with the dev override) from source:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

Backend lives in `backend/` (`uv run ...`), frontend in `frontend/` (`npm run ...`). Conventions, red lines, and the contract-first workflow are documented in [`AGENTS.md`](AGENTS.md).

## Roadmap

M0–M17 are accepted within their recorded scopes. **M15 was accepted on 2026-11-09 and is included in v0.12.0**, following historical real receipt intake, automated Codex MCP acceptance and isolated real Hermes CLI validation. Author-skipped and deferred checks remain recorded in the [M15 acceptance conclusion](docs/plan/milestones/M15.md) and [roadmap](docs/plan/roadmap.md).

The author accepted [M16](docs/plan/milestones/M16.md) on 2026-11-01 on its recorded representative walkthrough scope. External sales bookkeeping, VAT/P&L separation, annual battery private-use adjustment and the incorporated SI3 mileage/profit breakdown are delivered; skipped/unretested paths and known carryovers remain explicitly recorded.

The [M17 Quote PDF appendices design](docs/plan/milestones/M17.md) was agreed and recorded on 2026-11-04: a shared library and Quote-specific uploads, manual ordered selection, and one merged output for preview/download/email. All six implementation steps passed local gates and independent review. The author walkthrough was completed on 2026-11-06 within the recorded scope. The walkthrough scope and omissions are recorded in the M17 plan. M15 was separately accepted within its recorded scope on 2026-11-09.

SI1 (document ordering, Quote duplication and server-side Sent copies) was accepted on 2026-10-25 on a representative walkthrough scope; unperformed mail variants and failure paths remain explicitly recorded with automated coverage. See the [SI1 plan and acceptance](docs/plan/improvements/SI1.md).

[M18 Agent mileage intake](docs/plan/milestones/M18.md) has an agreed design dated 2026-11-10, with four atomic task cards awaiting implementation. It plans client-neutral transport options, user-confirmed previews and idempotent creation under a new explicit `mileage:create` permission. Route suggestions come from the Agent's own available search/map capabilities. This capability is pending delivery in a future release.

## Documentation

Docs are English-first with a synchronized Chinese mirror (`*_zh.md`). Start at [`docs/plan/roadmap.md`](docs/plan/roadmap.md); the Dutch VAT/BTW filing basis is in [`docs/insight/btw-aangifte-2026-guide.md`](docs/insight/btw-aangifte-2026-guide.md).

## License

[MIT](LICENSE).

# Yet Another Ledger

> 🌐 **English** · [中文](README_zh.md)

Self-hosted invoicing and small-business administration for freelancers and small businesses. Invoices, quotes, expenses, payments, PDF + email, and Dutch **BTW (VAT) return** summaries — run with Docker Compose (a single app container plus PostgreSQL).

Built with **FastAPI + Vue 3 + PostgreSQL**.

> **Status — pre-1.0 (`v0.9.0`).** Open source and self-hosted. Container images are published to the GitHub Container Registry (GHCR). Expect rough edges and possible breaking changes before `v1.0`.

## Features

- **Invoices** — Standard, Advance, Final and source-bound Credit Notes; backend pricing, concurrency-safe numbering, independent lifecycle/settlement/credit states, and an auditable document chain.
- **Quotes** — reusable content blocks/templates, auto-expiry, and an immutable choice between direct invoicing, receipt-only deposits, or formal staged Advance/Final invoicing.
- **Editing and reuse** — audited revisions for eligible undelivered, unpaid Standard Invoices, retaining SENT, the original number, unchanged tax/identity snapshots and historical PDFs; drag-to-reorder lines in Invoice, Quote and Estimate editors; duplicate a Quote into an independent draft with a fresh number and dates.
- **Cost estimation → quote** — internal margin-based costing that never leaks cost/margin to the customer.
- **Payments & refunds** — partial payments, Quote deposits, automatic settlement states, Credit-linked Refunds, and Refund Confirmations.
- **Expenses** — receipt inbox for review before posting, AI extraction from image/PDF originals, recurring expenses, bookkeeping fields (paid-by / business-use % / depreciation years).
- **Customers & catalog** — addresses, VAT IDs, per-customer currency & document language; product/material catalog.
- **Documents** — EN/ZH PDFs and email for invoices, quotes, receipts and Refund Confirmations, with issue-time party snapshots, exact downloaded/sent artifact retention, and immutable historical Invoice PDF backfill with optional advisory AI comparison.
- **Reports** — Profit & Loss, **Dutch BTW VAT-return summary**, ICP listing, expense report, and an ECharts dashboard, including exactly-once Advance/Final/Credit projections.
- **Platform** — TOTP two-factor auth, typed three-tier settings, `Decimal` money math, bilingual UI (English / 中文), single-container app deployed via Docker Compose.

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

> `:latest` tracks the newest non-prerelease release; pin this release by setting `JAI_IMAGE=ghcr.io/yet-another-ledger/yet-another-ledger:0.9.0` in `.env`. To build the image yourself instead of pulling, run `docker build -t ghcr.io/yet-another-ledger/yet-another-ledger:latest .` before `docker compose up -d`.

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

## Development

Run the full dev stack (app + Postgres, with the dev override) from source:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

Backend lives in `backend/` (`uv run ...`), frontend in `frontend/` (`npm run ...`). Conventions, red lines, and the contract-first workflow are documented in [`AGENTS.md`](AGENTS.md).

## Roadmap

M0–M14 are complete. **M15 remains in development and was paused by the author on 2026-10-24 to prioritize other work**; it has not been accepted. Its implementation and walkthrough fixes are preserved on `wip/m15-agent-accounting-paused`, outside `main`. M15 covers scoped Agent/MCP receipt intake and customer, quote, and Standard Invoice draft tools; the existing M14 inbox and AI extraction remain independent. No milestone is currently active; the author selects the next work, and M15 must not resume automatically. See the [M15 status and resume checkpoint](docs/plan/milestones/M15.md), [roadmap](docs/plan/roadmap.md), and [M14](docs/plan/milestones/M14.md).

The [M16 design](docs/plan/milestones/M16.md) was agreed on 2026-10-30; implementation has not started. Nine atomic steps cover external sales bookkeeping, VAT/P&L separation, annual battery private-use adjustment and the incorporated SI3 mileage/profit breakdown. These are planned capabilities, not shipped `main` features, and do not resume M15.

SI1 (document ordering, Quote duplication and server-side Sent copies) was accepted on 2026-10-25 on a representative walkthrough scope; unperformed mail variants and failure paths remain explicitly recorded with automated coverage. See the [SI1 plan and acceptance](docs/plan/improvements/SI1.md).

## Documentation

Docs are English-first with a synchronized Chinese mirror (`*_zh.md`). Start at [`docs/plan/roadmap.md`](docs/plan/roadmap.md); the Dutch VAT/BTW filing basis is in [`docs/insight/btw-aangifte-2026-guide.md`](docs/insight/btw-aangifte-2026-guide.md).

## License

[MIT](LICENSE).

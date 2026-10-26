# Yet Another Ledger · v1 Master Roadmap

> 🌐 **English** · [中文](roadmap_zh.md)

> **Current scheduling (2026-10-26)**: M15 remains in development but is paused, with its work preserved on `wip/m15-agent-accounting-paused`, outside `main`. No milestone is currently active. SI1 was accepted on the recorded representative walkthrough scope on 2026-10-25. The author agreed the [SI2 implementation plan](improvements/SI2.md) on 2026-10-26: five atomic steps for audited revision of undelivered, unpaid Standard Invoices; orchestrated implementation is in progress, with author acceptance pending. Small improvements are separately numbered and do not resume M15.

> **What this is**: A **buildable overview** of the agreed v1 scope (P0–P7) in **M0–M13**, plus the planned receipt-inbox and agent-accounting follow-ons **M14–M15**. Milestones follow the principles of atomicity, frontend/backend parallelism, and deployable verification.
>
> **What this is not**: A line-by-line construction guide. This document stays at the level of "milestones + methodology + constraints"; the **atomic step list** for each milestone is written out JIT in `docs/plan/milestones/M<x>.md` (template: `milestones/_TEMPLATE.md`).
>
> **Authoritative sources**: Direction, scope, domain model, and decision records are governed by **this roadmap + each `milestones/M<x>.md` (frozen per-milestone decisions)**, with separately numbered small-improvement plans in **`improvements/SI<n>.md`**; Dutch VAT/BTW filing conventions are governed by `docs/insight/btw-aangifte-2026-guide.md` (structured guide to the official Tax Authority filing instructions). (Early upstream "analysis documents" were removed from the repository on 2026-06-15 — their content has been absorbed into the above documents; any remaining "analysis doc §x" references in older milestone files are historical artefacts only.)
>
> **Architecture reference**: `~/workspace/trading-journal` (isomorphic, live in production). Skeleton, conventions, Dockerfile, and CI all align with it; the only differences are "swap SQLite for PostgreSQL + invoice domain".

---

## 0. Agent pre-flight checklist (read these 5 items every time before starting)

1. **Read the constraints first**: [§2 Global Constraints] are the hard guardrails — stop and ask before violating any of them.
2. **Contract first**: Define/update the API contract (Pydantic schema) before writing backend implementation or frontend UI. See [§1.1].
3. **Money calculations only in the backend**: The frontend only collects raw input; all amounts are authoritatively calculated by backend `services/pricing`. See [§2] item 1.
4. **One step = one atomic change + pass DoD**: Atomic, independently deployable, with tests; once CI is green, merge to `main` (solo development — no mandatory PR). Template at [§5].
5. **Regenerate TS types whenever the contract changes**: `npm run codegen`, and ensure the CI drift check passes. See [§1.1] and [§5].

---

## 1. Development Methodology (Four Pillars)

### 1.1 Contract-first → Frontend and backend in parallel

The **first action** for any feature is to lock the API contract: write Pydantic request/response models + route signatures in the backend (stub data is fine initially), and FastAPI produces OpenAPI automatically. Once the contract is locked:

- **Backend track**: Implement `models → schemas → services → api` with real logic + tests.
- **Frontend track**: `npm run codegen` pulls the OpenAPI into `frontend/src/api/schema.d.ts` (**committed to the repo**); the frontend writes stores/views against those types — mock first, then wire up the real API.

Both tracks run in parallel, kept in sync by the single "types + OpenAPI" contract. The CI `codegen-freshness` gate enforces that `schema.d.ts` matches the backend at all times.

### 1.2 Walking Skeleton + vertical thin slices → every milestone is deployable

- **M0 is not "pile up infrastructure"** — it threads the thinnest possible end-to-end path: single container up, FastAPI serving the frontend, health check, one working page, Postgres connected, migrations running, CI green. **Day one you can open a browser and click something.**
- Every subsequent milestone adds **one vertically visible feature** (DB → API → UI end-to-end), not "finish all backend, then all frontend". Each milestone ends with an explicit **deployment smoke test** (see [§4]).

### 1.3 Atomic steps + uniform Definition of Done → maintainable and reviewable

Every atomic step is a small, self-contained change that always follows the reference layering (`models / schemas / services / api`, money logic in `services`) and passes the uniform DoD (see [§5]). **Solo development — no mandatory PR**: self-test + CI green is enough to merge to `main` (branch/PR still available for intentional human review). Benefit: every step is small and isomorphic, easy to read back later.

### 1.4 In-repo Markdown plan documents → vibe-coding friendly

All planning lives as Markdown under `docs/plan/`, version-controlled alongside code and readable by both agents and humans. This document is the overview; milestone details are written JIT into `milestones/M<x>.md`. Structure at [§7].

---

## 2. Global Constraints (Guardrails · Hard Lines)

> Derived from analysis doc §7.3 pitfall list + decision records. **Every agent must read these before starting; stop and ask before violating any.**

1. **Money calculations in the backend**: The frontend only sends `{item_id?, name, description, quantity, unit_price, discount, tax_category_id, ...}` and other raw inputs; backend `services/pricing` is responsible for line subtotals → discounts → tax computation (inclusive/exclusive/compound/fixed) → document totals → base-currency conversion, all produced authoritatively and persisted. All amounts must use **`Decimal`** (DB `NUMERIC`, scale≈3); **rounding rules and positions are fixed** (per-line vs. aggregate rounding mode is fixed).
2. **Multi-tenancy via Postgres RLS, not manual scoping**: Even for v1 single-tenant, converge data access and reserve `company_id` on core tables — no scattered `where company=` clauses. See [§3.3].
3. **No hand-written cascade deletes**: Use DB foreign keys + ORM cascade (`SQLAlchemy relationship(cascade=...)` / `ondelete="CASCADE"`) to eliminate orphaned data.
4. **Concurrency-safe numbering**: No `max+1`; use DB sequences / unique constraints + retry; support **custom starting number and gap skipping** (for migrating from legacy systems).
5. **No stringly-typed settings**: Three-tier settings with typed access (Pydantic/enum) + caching; no `'YES'/'NO'` scattered everywhere.
6. **Normalised tax table structure**: No wide table with a pile of nullable FKs; use normalised polymorphism or separate "document-level tax table / line-level tax table".
7. **Sanitise user input before rendering**: Filter before entering PDF/HTML (XSS/SSRF), following InvoiceShelf's recent sanitiser approach.
8. **Lock exchange rate as a snapshot**: Foreign currency locks the EUR tax base at **invoice date** (VAT compliance); payment date uses a separate cash/FX rate; historical records never drift (analysis doc §7.4.5).
9. **No in-app self-update**: Upgrades go through redeploying the container image; no inbound network entry point that could control the container.
10. **Use `text` for `description`**: Never repeat the 255-character limit mistake.
11. **OpenAPI → TS type generation**: Follow the reference project's approach — consistent frontend/backend types, no contract drift; CI enforces drift detection.
12. **VAT is data-driven, not hardcoded enums**: Tax rates and VAT treatment categories are **user-editable records** (NL defaults 21/9/0 are seeds only); the "category → filing box" mapping is country-specific and decoupled from the rate table (analysis doc §7.4.2).

---

## 3. Tech Stack and Skeleton (aligned with the reference project)

### 3.1 Tech stack

| Layer | Choice |
| --- | --- |
| Backend | **FastAPI** + **SQLAlchemy 2.0 (async)** + **Alembic** + **fastapi-users** + **pydantic-settings**; package manager **uv**; Python 3.12 |
| Database | **PostgreSQL** (used from day one, no SQLite; asyncpg driver) + **row-level security RLS** |
| Quality gates | **ruff** (line 100; E/F/I/B/UP/ASYNC) + **mypy --strict** + **pytest** (asyncio auto) |
| Frontend | **Vue 3 + TypeScript** + Pinia + Vue Router + Vite + **Naive UI** + **ECharts**; **openapi-typescript** generates `schema.d.ts` |
| Deployment | **Single container**: three-stage Dockerfile (frontend `vite build` → backend `uv` install deps → runtime serves `static/` + uvicorn); entrypoint runs `alembic upgrade head` |
| CI | GitHub Actions: backend-quality / codegen-freshness / frontend-build / docker-build; tag triggers multi-arch image publish |

> **The only material difference from the reference project**: The reference uses `sqlite+aiosqlite`; this project uses `postgresql+asyncpg` from M0 (laying the groundwork for RLS). Everything else — skeleton, Dockerfile layers, CI four-gate, codegen flow — is **copied directly**.

### 3.2 Directory skeleton (Python package = `jai`)

```
backend/src/jai/
  main.py            # FastAPI app assembly, route mounting, static serving + SPA fallback
  config.py          # pydantic-settings: DATABASE_URL / SMTP / STATIC_DIR / ...
  db.py              # async engine / session / Base; RLS session context (later)
  auth/              # fastapi-users: users / backend / deps
  models/            # SQLAlchemy ORM (incl. _enums.py, Money/Decimal conventions)
  schemas/           # Pydantic request/response (separate from models; no money logic)
  services/          # Business logic: pricing / numbering / fx / reports ... (money logic here)
  api/               # Routes (/api/v1/*): thin controllers, call services
backend/alembic/     # migrations
frontend/src/        # api(schema.d.ts) / stores / views / components / router / ...
Dockerfile           # three-stage single container
.github/workflows/   # ci.yml / release.yml
docs/                # insight (analysis) + plan (this roadmap + milestones)
```

### 3.3 "Multi-tenant-friendly schema + single-tenant simple logic" baseline

- v1 implements **single company, single user**, but the schema is not hardcoded as a singleton: "company/business entity" is built as a proper table (v1 has one row), with core business tables carrying an ownership reference.
- Whether `company_id` is added now or via a later Alembic migration is **deferred to each milestone's table design** (default preference: core business tables carry `company_id` from M2 onward, reserving a slot for RLS).
- RLS / multi-user RBAC / multi-company switching UI and similar **application-layer complexity are explicitly out of scope for v1** — only schema hooks are reserved.

---

## 4. Milestone Map M0–M15 (including inserted M2.5 · Settings UX refactor / M6.5 · Cost accounting / M11.5 · Quote deposits)

> Dependencies: **M0 → M1 → M2 → (M2.5) → {M3, M4}**, then two parallelisable tracks:
> ① **Document track**: M5 → M6 → **M6.5** → M7; ② **Expense track**: M8 (depends only on M2 base currency + M4 dictionaries, optionally M3 customers, **independent of invoices/quotes/payments**). The two tracks converge at M9 → M10, then continue through **M11 (private-transport business mileage) → M11.5 (quote deposits and final-invoice settlement) → M12 (advance/final invoices and credit notes) → M13 (Document Artifact backfill) → M14 (receipt inbox and review) → M15 (agent receipt intake and business drafts)**.
> Parallelisable: M3‖M4; document track‖expense track (M8 can run alongside M5/M6/M7). **M2.5 is a pure frontend UX fix, non-blocking** — it slots after M2 and can run in parallel with M3/M4; it blocks nothing. The "🟢 deployment smoke test" at the end of each entry is that milestone's acceptance signal.

### M0 · Foundation skeleton (walking skeleton) | corresponds to P0
- **Goal**: Thinnest possible end-to-end path running in a single container.
- **Key content**: Build `jai` backend skeleton + Vue frontend skeleton mirroring the reference project; integrate **PostgreSQL + asyncpg**; Alembic baseline migration; three-stage Dockerfile + entrypoint; CI four gates; `/api/health`; **Money/Decimal currency primitive types** (`NUMERIC` convention + rounding utilities); i18n scaffold (EN/ZH skeleton); `openapi-typescript` codegen wired up.
- **Two running modes (both must work in M0; base `docker-compose.yml` contains `app` + `postgres`; DB does not publish to the host; app publishes only to `127.0.0.1:${APP_HOST_PORT:-8000}` for local reverse-proxying, container-internal port fixed at 8000; local development overlays `docker-compose.dev.yml` which binds Postgres only to `127.0.0.1`, port adjustable via dev-only `POSTGRES_DEV_PORT`)**:
  - **Development mode**: Frontend `npm run dev` (Vite) | Backend `uv run uvicorn jai.main:app --reload` | Database **`docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d postgres` (only the Postgres service)**.
  - **Deployment mode**: `docker compose up -d` (starts **single-container app = built frontend + backend**, plus **Postgres**; production pulls from GHCR, local integration uses dev override `up --build`); migration service automatically runs `alembic upgrade head`.
- **Tables/placeholders**: Alembic starting point; stub RLS session hook location.
- **🟢 Deployment smoke test**: Local integration `docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build` starts app + Postgres; production `docker compose up -d` pulls from GHCR; browser opens `http://localhost:${APP_HOST_PORT:-8000}` and sees a (blank) placeholder page + `/api/health` returns ok; CI all green.

### M1 · Authentication + email foundation | corresponds to P1
- **Goal**: A private application where users can register/log in, and emails can be sent.
- **Key content**: fastapi-users username+password (**Argon2**); **MFA (TOTP) — essential for accounting software**; login/logout UI; protected routes + empty shell dashboard.
- **Email foundation (done in this milestone, reused directly by M9)**: SMTP sending via a Python package; **password-reset email**; **settings table basics** (single table + `level` for the three-tier settings, initially used for SMTP config) + **SMTP settings page** (frontend form to fill and save).
- **Tables/placeholders**: `user` table with `company_id` + `role` fields from the start (RBAC added later; v1 owner has full access); **settings table (key-value + level) lands here**; M2 extends it with company/user tiers.
- **🟢 Deployment smoke test**: Register → login → bind TOTP → reach the empty dashboard; fill in SMTP on the settings page → trigger password reset → receive the email; after logging out, protected pages redirect to login.

### M2 · Company profile + three-tier settings (complete) | corresponds to P1
- **Goal**: Ability to configure "your business entity".
- **Key content**: Singleton **business profile** (name/logo/VAT number/address/base currency/numbering rules); on top of **the settings table landed in M1**, complete **three-tier semantics** (global/company/user, scope id, typed access + caching, fallback order user→company→global) + company/user-level settings UI pages.
- **Tables/placeholders**: `company` table lands (v1: one row); settings table `level`/scope designed to be multi-company-friendly.
- **🟢 Deployment smoke test**: Edit company letterhead/logo/base currency and persist; change a user-level preference and verify fallback takes effect.

### M2.5 · Unified settings entry + settings UX refactor (Affine-style expandable panel) | new in this roadmap (UX fix, no new backend features)
- **Goal**: Consolidate the currently scattered, semantically confusing settings entry points into "a single gear icon in the top-right → a unified expandable settings panel", aligned with the Affine settings UX.
- **Dependencies**: M2 (company profile + three-tier settings already in place). **Pure frontend refactor** — does not touch backend contracts or money logic; **non-blocking**, can run in parallel with M3/M4.
- **Current pain points (why we're doing this)**:
  - The top-right "avatar" icon opens Preference, while a separate "Settings" entry next to it only contains SMTP;
  - After entering Preference, the top-right icon changes to a no-icon Company/other state — **entries scattered everywhere, icon and content semantics inconsistent**.
- **Boundary principle (define and acknowledge before coding)**: The gear panel **contains only "Preference" settings + system configuration** (SMTP, theme…); **business entity identity** (letterhead / logo / VAT number / address / base currency / numbering rules) belongs to Company, placed under the **Company icon's Company settings**, **not in the gear panel**. The two have non-overlapping responsibilities.
- **Key content**:
  - **Single entry point**: Top-right retains only **one gear icon** as the total settings entry; clearly distinguished from the **dedicated Company icon (business identity area)** in the top-right — the Company one does not merge into settings.
  - **Expandable panel (not a plain dropdown)**: Clicking the gear opens an Affine-style **Expandable Menu / settings panel** — categories on the left, details on the right, editable in place.
  - **Settings categories consolidated** (all into this one panel):
    1. **Preference settings**: User/company-level preference toggles and defaults (language, default values…), **excluding business entity identity** (that's Company's domain);
    2. **Email settings**: SMTP etc., fill and save in place (reuses M1/M2 existing endpoints);
    3. **Theme**: Dark mode, default theme, and other scattered items **all consolidated here**;
    4. **Future expansion slot**: Later settings such as AI are entered from this panel (**category slot reserved**, no concrete functionality in this milestone).
- **Out of scope**: No new backend fields/endpoints (contract alignment only if needed, no money logic); custom themes and multi-company switching are not in this milestone.
- **Follow-up (scheduled · account-level language persistence)**: The main M2.5 body makes language session-scoped (retaining the old 🌐 behaviour — resets on refresh); a follow-up adds language persistence **like theme, following the account** — adds a `locale` field to `UserPreferences` at the USER level, reuses `GET/PUT /settings/me`, and the frontend mirrors the `useTheme` pattern of "localStorage cache + server authoritative". This is **the only intentional small extension** to the "zero backend" boundary above (one field, no money logic, requires regenerating `schema.d.ts`); details in `milestones/M2.5.md` step 4.
- **🟢 Deployment smoke test**: Top-right retains only **one gear + one Company icon**; clicking the gear opens the expandable panel; within the panel, complete "company preference / SMTP / theme switch" for all three setting categories and persist; the "avatar turns into Preference, icon disappears" confusion never recurs.

### M3 · Customers | corresponds to P2 (parallelisable with M4)
- **Goal**: Ability to manage customer records.
- **Key content**: Customer CRUD + list; billing/shipping **addresses**; per-customer default currency (locked once a transaction exists); **country + VAT number** (groundwork for M10's ICP / reverse-charge determination); long-tail fields via **JSONB**.
- **Tables/placeholders**: `customer.company_id`; address uses `type` (BILLING/SHIPPING).
- **🟢 Deployment smoke test**: Create/edit/delete customers, fill address and VAT number, search the list.

### M4 · Dictionaries / master data | corresponds to P2 (parallelisable with M3)
- **Goal**: Dictionaries needed by documents are ready.
- **Key content**: **Tax types/VAT treatment categories** (data-driven; NL 21/9/0/exempt/reverse/EU-B2B/export as seeds, see analysis doc §7.4.2); payment methods; expense categories (aligned with NL/EU conventions); currencies + exchange rate basics (manual entry, provider interface stubbed out); **product/materials catalogue (data foundation for M6.5 cost accounting)** — entries carry "purchase cost (excl. VAT) + category + unit + category-default Margin Rate", manual CRUD + supplier Excel paste/import; margins and categories are always **data-driven and editable** (guardrail 12), not hardcoded enums.
- **Tables/placeholders**: Tax category table **decoupled** from "filing box mapping"; abstract exchange-rate provider interface; cost field in the product catalogue is the "current price" — historical snapshots are locked by each estimate that uses it (see M6.5).
- **🟢 Deployment smoke test**: Maintain tax rates/payment methods/expense categories, seed data visible and editable; create a few products/materials with default margins, import a batch of supplier prices via Excel.

### M5 · Pricing engine + invoice core | corresponds to P3 (the "heart" of v1)
- **Goal**: Ability to issue an invoice with amounts authoritatively calculated by the backend.
- **Key content**: **`services/pricing` authoritative calculation** (line subtotals → line/document discounts → per-document/per-line tax → inclusive/exclusive → totals → base-currency conversion, rounding fixed); `POST /invoices/calculate` preview endpoint; invoice CRUD + list; **numbering** (templated + custom starting number + gap skipping + concurrency-safe); **dual status** (lifecycle status + payment status); line items (free-form `description` as `text`, quantity required, unit optional, optional catalogue reference).
- **Tables/placeholders**: Normalised tax tables (document-level/line-level separate tables or proper polymorphism); `unique_hash` field reserved (public link not enabled in v1).
- **🟢 Deployment smoke test**: Create an invoice, add multiple lines, toggle per-document/per-line tax, observe backend-calculated subtotals/tax/totals; change the starting number and verify it takes effect.

### M6 · Quotes + conversion + content templates | corresponds to P3
- **Goal**: Complete quote workflow + document content reuse.
- **Key content**: Quote CRUD (isomorphic with invoices); **simplified status** draft/sent/accepted/rejected/expired (removed: viewed); **auto-expire on due date** (backend timer, APScheduler); **Convert quote → invoice**; **document content templates** (one-click fill for common trade quotes/invoices); **standard content blocks** (warranty/T&C/bank info/payment terms, company-level defaults overridable per document); document **Notes** (free-form notes + reusable templates).
- **🟢 Deployment smoke test**: Create a quote, apply a content template, mark accepted, one-click convert to invoice; expired quotes automatically set to expired by the timer.

### M6.5 · Cost accounting / quote assistance (internal estimation → quote) | new in this roadmap (core author workflow, replacing the existing Excel)
- **Goal**: Use an **internal** "cost → selling price" worksheet to assist quote pricing (blue-collar / renewable energy installation context).
- **Dependencies**: M4 **product/materials catalogue** + M5 **pricing engine** (reuse its VAT/total layer, **no re-computation of tax**) + M6 **quote entity**. Slots after M6 on the document track, before M7.
- **Key content**:
  - **Estimate entity + unified line model**: Each line `Total = Price × Amount`, `Margin Amount = Total × Margin Rate`, `line selling price (excl. VAT) = Total + Margin Amount`; **labor / shipping / travel / overhead are all ordinary lines with Margin Rate = 0** (no separate line types); travel and overhead typically appear as **one document-level line each**.
  - **Three rolling summaries**: Total Margin (equipment profit only) / **Total Excl. VAT** (Σ line selling prices) / Total Incl. VAT (**passed to M5 engine to add 21% VAT**; costing does not compute tax itself).
  - **`services/costing` authoritative calculation**: `Decimal` + fixed rounding (per-line vs. aggregate mode nailed down when this milestone is detailed); **money logic must have unit tests** (guardrail 1).
  - **Estimation → quote linkage (non-one-to-one)**: Estimate lines can be **grouped**; each group generates **one quote line** — carrying only the **public description (brand / kWh and other publicly shareable parameters) + excl. VAT price (= that group's Σ line selling prices)**; the quote line then goes through M5 to produce the incl. VAT total.
- **Tables/placeholders**: Estimate lines **snapshot** the cost at the time (decoupled from M4 catalogue — later price changes in the catalogue do not affect historical estimates); estimate and quote use a "group → quote line" weak association, not forced one-to-one.
- **Guardrails specific to this milestone (extensions of guardrails 7/8 — acknowledge before writing code)**:
  1. **Zero customer-facing leakage**: Estimate cost / Margin / hourly-rate fields are **never** serialised into quotes / PDFs / public links (extension of guardrail 7).
  2. **Cost snapshot**: Updating catalogue prices **does not back-fill** historical estimates; history never drifts (same as guardrail 8).
- **🟢 Deployment smoke test**: Create an estimate, add several equipment lines (with margins) + labour/freight lines (margin 0), observe the backend-calculated excl. VAT selling price and Total Margin; select a group to generate one **public quote line** (confirm no cost/margin/hourly rate is visible on the customer side); that quote goes through M5 to produce the 21% incl. VAT total.

### M7 · Payments | corresponds to P4 (parallelisable with M8)
- **Goal**: Invoices can receive payments, and status transitions automatically.
- **Key content**: Payment entity (linked to invoice / standalone); **partial/multiple payments** (deposit/progress/final); payment methods; **exchange rate locked at payment time** (EUR rate on payment date); automatic recalculation of `due_amount` and `paid_status`.
- **🟢 Deployment smoke test**: Apply two separate payments to one invoice and observe UNPAID → PARTIALLY_PAID → PAID automatic transitions.

### M7.5 · Currency rounding correction (round to the minor unit) | new in this roadmap (M5 money rounding fix, spans document track / expense track)
- **Goal**: Make all **customer-facing / reconciliation / filing monetary amounts** consistently land on the currency's minor unit (v1 = EUR = 2 decimal places / cents), so that "frontend display = amount due = backend reconciliation = supplier invoice / bank statement" are all self-consistent; **unit prices and intermediate calculations retain ≥3 decimal places** unchanged.
- **Background**: The M7 payment walkthrough revealed that invoice incl. VAT totals were stored at 3 decimal places (e.g. `F2026-009 = 3865.166`), the UI displayed `3865.17`, and payments rounded to cents could never balance. Root cause: M5 only quantised the **amount due** to 3 places and never rounded to cents (guardrail 1's "rounding position is fixed" is exactly this).
- **Dependencies / position**: Modifies **M5 `services/pricing`** (+ M6.5 customer-facing costing and M8 expense when they land); positioned **after M7, before M8** (M8 expenses follow the same convention — supplier invoices and bank statements are already at cent precision).
- **Key decisions (frozen · see `milestones/M7.5.md`)**: **Round at line level** (sub-variant of Method B — `quantity × unit_price` result rounded to cents immediately; each line's net/VAT/total rounded to cents; document total = sum of lines; multi-line same-rate: no group-level allocation; `F2026-009` → `3865.16`); only `unit_price` retains ≥3 places; rounding direction follows `amounts_include_vat`; applies to all document-level output amounts (invoice/quote/customer-facing estimate/expense); **no column type change / no contract change / no migration** (`NUMERIC(18,3)` can hold 2 decimal places); **no rounding to whole euros** (VAT must be filed at cent precision); introduces `currency_minor_unit` (EUR=2 hardcoded + extension point); **no historical recomputation** (project not yet live, all test data — recreate old documents as needed); `services/costing` and `services/payment` zero changes.
- **🟢 Deployment smoke test**: After recomputation, an `F2026-009`-style invoice total lands at cents and can be fully paid (reaching `COMPLETED`) using the displayed amount; a multi-line multi-rate invoice has each VAT group rounded to cents with excl+vat=total self-consistent, matching supplier invoices/bank statements.

### M8 · Expenses | corresponds to P4 (independent track: depends only on M2+M4, parallelisable with the entire document track M5/M6/M7)
- **Goal**: Ability to record expenses and auto-fill receipts intelligently.
- **Dependencies**: M2 (base currency) + M4 (expense categories/payment methods/currencies); optionally M3 customers. **Independent of invoices/quotes/payments** — available to start in parallel with the document track as soon as M4 is done.
- **Key content**: Expense CRUD + categories; **receipt upload** (image/PDF, local storage + storage abstraction); **recurring expenses** (fixed costs automatically generated on a schedule); **⭐ AI receipt smart fill** (receipt image → vision language model → auto-fill net amount/VAT amount/rate/supplier/date/category; external dependency: Claude or other vision model APIs); per-expense flag for "deductible".
- **(Follow-on) ⭐ AI supplier price list recognition → import to product catalogue**: **Reuses this milestone's vision model pipeline** to take supplier price lists (image / PDF / Excel) → auto-recognise → load into **M4's product/materials catalogue** (feeds M6.5 cost accounting); scheduled after M8 AI infrastructure is in place.
- **🟢 Deployment smoke test**: Upload a receipt photo, AI auto-fills the expense fields, save and categorise; create a recurring expense and observe automatic generation.

### M8.5 · Expense bookkeeping fields (aligned with author's NL bookkeeping Excel) | new in this roadmap (M8 data model completion, no new money logic)
- **Goal**: Add three **record-only** fields to expenses to make the data model reflect the author's Dutch sole-trader bookkeeping Excel — **payment source** (private/business account), **business use percentage**, **depreciation years**.
- **Dependencies / position**: Additive field extension on top of the **M8** expense track; positioned **after M8, before M9**. **No new money logic** — current-year actual expense / deductible VAT by year / quarterly aggregation / BTW boxes and other derived values are all deferred to **M10** (they depend on "which year is being filed", a reporting engine concern).
- **Key content**: Add `paid_by` (PRIVATE/BUSINESS indicator), `business_percentage` (0–100), `depreciation_years` (≥1) columns to `expense` (+ `recurring_expense` parity); editor/list UI + i18n; contract change → `npm run codegen`. `deductible` semantics unchanged (= can VAT be reclaimed); personal-header non-deductible VAT handled at entry time (Net = Gross, VAT = 0), no new modelling. See `milestones/M8.5.md` (D1–D9 frozen).
- **🟢 Deployment smoke test**: New expenses can fill private/business, business%, depreciation years and persist; M8-era old expenses migrated with defaults (Business/100/1); range validation (%∈[0,100], years≥1); three fields visible in list; CI green + `schema.d.ts` no drift.

### M9 · Output: PDF (email foundation already in M1) | corresponds to P5
- **Goal**: Ability to deliver documents to customers.
- **Key content**: **PDF generation** (one template set, Jinja2 + WeasyPrint candidate; user input sanitised) + download (no public link, manual distribution); **reuse M1's SMTP foundation**, add document **email body templates/placeholders** + **Email log** (no read receipts) + send PDF as attachment; payment receipt PDF (low priority).
- **Tables/placeholders**: PDF template reserves a CSS interface (custom templates not done in v1).
- **🟢 Deployment smoke test**: Download an invoice PDF (letterhead/line items/tax/totals correct); use the SMTP configured in M1 to send an invoice email + PDF attachment, and see the log entry in Email log.

### M10 · Reports / dashboard | corresponds to P6
- **Goal**: VAT filing and business performance made visible.
- **Key content**: **P/L** profit and loss; **⭐ VAT filing summary** (quarterly + VAT category aggregation into BTW boxes 1a/1b/1e/2a/3a/3b/4a/4b/5a/5b/5c + generate **ICP list**; conventions from analysis doc §7.4, exact numeric conventions finalised at implementation time); expense report; **Dashboard** (ECharts charts).
- **⚠️ Outstanding item from M4 (mandatory)**: M4 created the `vat_treatment.report_box` column but **left it empty**. This milestone must **jointly confirm with the author** the `(treatment × rate) → BTW box` mapping against the official Belastingdienst website before populating it — **agents must not act unilaterally**. See `milestones/M4.md` "JIT review confirmed" and memory `vat-model-two-axis`.
- **🟢 Deployment smoke test**: Select a quarter, export VAT summary and ICP; Dashboard displays revenue/expense/profit charts.

### M11 · Mileage expenses (private transport used for business) | expense-track extension
- **Goal**: Record privately owned or privately rented transport used for business by entering the trip date and one-way kilometres (optionally return), with the backend creating the correct Expense from an effective-dated rate.
- **Key content**: Expense page gains Purchase/Mileage tabs; company-editable transport-type dictionary; general effective-dated mileage rates with optional per-type overrides; 2024/2025 €0.23 and 2026 €0.25 editable seeds; optional origin/destination/purpose/note; backend-only Decimal distance×rate calculation; Mileage category + Expense projection into existing P/L/Dashboard/Expense Report with €0 VAT; explicit preview/confirm/audit for retrospective rate corrections. Existing expenses and Travel category remain untouched. See `milestones/M11.md` (D1–D18 frozen 2026-08-19).
- **Boundary / follow-on**: M11 only creates claims for private transport. Company vehicles use actual-cost accounting and are deferred; the trip model leaves an additive extension point. Google Places/Routes address autocomplete and route distance are a later optional follow-on—M11 performs no external map calls and manual kilometres remain authoritative.
- **🟢 Deployment smoke test**: Configure general/type-specific rates; create a 12.5 km one-way return Car trip dated in 2026 and see 25 km → €6.25; verify Purchase/Mileage tabs and existing reports; preview then confirm one retrospective rate correction and inspect its audit record; BTW totals remain unchanged.

### M11.5 · Quote deposits and final-invoice settlement | document-track extension
- **Goal**: Record one or more deposits on an accepted quote before its final invoice exists, atomically carry them to the converted invoice, and recognise VAT at the payment date without double-filing it when the final invoice is issued.
- **Key content**: Quote-origin payments with immutable quote provenance; deterministic mixed-rate VAT snapshots; quote→DRAFT-invoice transfer and payment-state recomputation; guarded final-invoice edits and lifecycle transitions; quote-stage non-VAT payment receipts; final invoice PDF payment breakdown; BTW advance recognition and final-invoice offsets; full quote/invoice/global-payments frontend workflow. The 2026-08-28 walkthrough refinement adds locale-single-language receipt warnings and source-audited receipt email. Only `NL_DOMESTIC` advances are supported. See `milestones/M11.5.md` (D1–D15 frozen 2026-08-27).
- **Boundary / follow-on**: M11.5 itself has no formal advance invoice, standalone customer credit, refund/negative/overpayment, unrelated-invoice reassignment, percentage deposit calculator, cross-border/reverse-charge/export advance handling, historical tax-snapshot backfill, or filed-period correction workflow. M12 now owns the separately modelled formal Advance/Final, source-bound Credit Note and Credit-linked Refund paths; receipt-only Quote-deposit refund/reapplication remains deferred.
- **🟢 Deployment smoke test**: Accept an €8,000 domestic quote; record €1,600 and €4,000 deposits; download EN/ZH quote receipts with the matching single-language non-VAT warning; send a localized receipt and inspect its source-document audit log; convert once to a DRAFT invoice showing €5,600 paid and €2,400 due; verify edit/delete/reconvert/issue guards; issue the complete invoice and pay the €2,400 balance; confirm cross-quarter BTW totals recognise the deposits early and the project exactly once overall.

### M12 · Invoice lifecycle expansion: Advance/Final Invoices and Credit Notes | document-track expansion
- **Status**: 🟢 Completed, accepted and frozen on 2026-09-02. All orchestrated steps and blind reviews converged; the author completed a risk-based test-environment walkthrough and accepted the remaining low-use paths on automated evidence. The M13 Document Artifact follow-on was subsequently completed and accepted on 2026-09-05. See `milestones/M12.md`.
- **Goal**: Extend the current invoice model into an auditable family covering standard one-off invoices, staged formal advance/final invoicing, the existing private-customer receipt-only deposit path, and linked credit notes for correcting or reversing issued invoices.
- **Key content**: Quote-locked `DIRECT_INVOICE`/`RECEIPT_ONLY`/`FORMAL_ADVANCE` modes; `STANDARD`/`ADVANCE`/`FINAL`/`CREDIT_NOTE` kinds; gross/percentage Advances and one guarded Final; normalized application and immutable remaining-credit basis; full/partial source-bound Credits; separate Credit numbering; positive Credit-linked Refund ledger; replacement/compensation/project-cancellation flows; independent lifecycle/settlement/credit status; exactly-once BTW/ICP/P&L/Dashboard semantics; issue-time party snapshots; retained exact Download/Send PDF artifacts; unified list and complete document-chain timeline.
- **Frozen boundary**: Formal Advance/Final supports `NL_DOMESTIC` only; Credit inherits every existing source VAT treatment. Standalone/unallocated credit, receipt-only Quote-deposit refund/reapplication, overpayment, selected-line Advances, multi-currency/FX, Dutch locale and app-managed VAT filing locks remain deferred. Corrections emit structured warnings only.
- **Atomic delivery / acceptance**: Eleven implementation steps cover foundation/migration, mode+chain audit, Advance, Final, Credit, replacement/cancellation, Refund settlement, reports, output/artifacts, frontend and closure. Automated coverage spans the full 18-point matrix. The author's risk-based walkthrough exercised Direct, receipt-only, 50/50, 20/50/30 mixed 21%/0%, cancellation/Credit/Refund and report reconciliation; rarely used 9%, FX and ICP paths were accepted on automated evidence rather than repeated manually.

### M13 · Document Artifact Backfill | document-output follow-on
- **Status**: 🟢 Completed, accepted and frozen on 2026-09-05. All four orchestrated steps, blind-review/rework loops and automated closure gates converged; the author passed the Standard-Invoice Happy Path and accepted the unperformed negative and Advance/Final/Credit Note manual paths on automated evidence. See `milestones/M13.md`.
- **Goal**: Let an issued formal Invoice with no retained artifact accept one exact historical PDF from an old JAI deployment, legacy invoicing system or external tool.
- **Key content**: zero-artifact-only PDF upload for all four formal Invoice kinds; immutable exact bytes + `UPLOAD` provenance; reuse as the current locale/fingerprint's canonical Download/Send artifact; future settlement/locale/renderer changes may still create new artifacts; optional explicit AI field comparison with a fixed prompt and non-blocking EN/ZH checklist; race-safe upload/Download/Send first action; compact existing-workflow UI.
- **Frozen boundary**: no upload once any artifact exists; no replace/delete/primary selector, cleanup CLI, artifact/AI audit trail, AI-result persistence, Refund Confirmation upload, OCR import or accounting mutation. Existing multi-artifact history is preserved. The former generic wrap-up placeholder is retired; displaced operational ideas live in §4.aa.
- **Atomic delivery / acceptance**: four steps delivered contract/enum/file validation, exact upload+canonical output reuse, optional advisory AI comparison, and frontend/regression closure. Final automation passed Ruff, mypy, 1243 default and 1074 PostgreSQL integration tests, OpenAPI codegen freshness, 148 frontend tests, EN/ZH key parity, frontend and Docker builds, and the CJK PDF smoke. The walkthrough also produced and verified a purpose-bounded AI output-budget fix: receipt extraction 1024 tokens, artifact comparison 4096, and capability probing 512.
- **🟢 Deployment smoke test**: Upload a legacy PDF to an issued zero-artifact Invoice; verify optional AI warnings never block confirmation; ordinary Download/Send reuse the exact uploaded bytes; change settlement state and verify a new artifact appears while the original remains byte-identical and downloadable.

### M14 · Receipt inbox and review | expense-track follow-on
- **Status**: 🟢 Completed and accepted on 2026-09-29 after the author's PDF-focused walkthrough. The complete contract, frozen decisions, six atomic steps and accepted coverage limits are in `milestones/M14.md`.
- **Goal**: Upload a receipt before creating an Expense, retain the original image/PDF and AI extraction suggestions, then review and confirm it into a complete Expense later. Eliminate placeholder Expense entries used only to enable receipt upload.
- **Key content / boundary**: One company-scoped pending item represents one purchase and holds up to five originals; upload/file changes automatically attempt whole-group AI extraction, with persisted suggestions and manual retry. AI failure never blocks manual review. Confirmation atomically creates one non-draft Expense through existing backend money rules and transfers the original attachments; processed history remains. Pending items stay out of expense, P/L and VAT reports and are discarded only by explicit user action. M15 transport, credentials and delivery deduplication remain separate.
- **Atomic delivery / automated evidence**: six steps delivered contract/migration, durable intake/files, AI suggestions, exactly-once confirmation, web UX and regression closure. The PDF walkthrough repair passed 1,269 default and 1,091 isolated PostgreSQL integration tests (2,360 combined), including the M8/M10 regressions, migrations, runtime-role RLS, confirmation races and cleanup; 160 frontend tests, Ruff, strict mypy, frontend and Docker builds also passed. The later layout-only change passed 14 focused frontend tests and a production build. API types did not change. The detailed walkthrough report remains local in ignored `review-notes/`.
- **🟢 Deployment smoke test**: The author accepted PDF upload, AI prefill/retry, manual correction and confirmation, download, discard and processed history after deleting an Expense. Browser-only repeat/concurrent confirmation and image upload were not manually exercised; concurrent confirmation and image paths have automated coverage. Multi-page scans are handled as single PDF files. See `milestones/M14.md` for non-blocking observations and operational cleanup carryover.

### M15 · Agent receipt intake and business drafts | depends on M14
- **Status**: ⏸️ In development, paused by the author on 2026-10-24 to work on other features. The implementation, adapter and walkthrough fixes are preserved on `wip/m15-agent-accounting-paused`, outside `main`; automated implementation is complete but author acceptance is not. The walkthrough stopped at 3/6, preparing the existing-customer quote test. Read the checkpoint in `milestones/M15.md` before resuming, and resume only at the author's explicit request.
- **Goal**: Let an authorized agent, initially Hermes via Telegram, submit purchase documents to the M14 inbox, resolve or create customers, and prepare editable `DRAFT` quotes and Standard Invoices through a small MCP surface usable by other MCP-capable agents.
- **Key content / boundary**: Same-host private-network Streamable HTTP MCP, owner-managed revocable/scoped application tokens, Hermes sender/chat allowlists, original attachment transfer with a mandatory streaming-adapter fallback if remote MCP fails the real image/PDF gate, durable write idempotency and traceability. Reuse existing intake, master-data and backend pricing services; missing or ambiguous facts trigger clarification. Receipt accounting confirmation and document send/issue remain web-only. No direct database access, automatic Expense/VAT posting, or Agent operations on other invoice kinds.
- **🟢 Deployment smoke test**: Pending author acceptance. Telegram image/PDF each produce one pending inbox item with original-byte SHA-256 verified and a phone-openable review link; repeated delivery creates no duplicate. Use an existing and a new customer to create a draft quote, then create/edit an unnumbered Standard Invoice draft; backend totals match the web app. Revoke the token and try unauthorized sender/scope requests; none can confirm an Expense or send/issue a document. Full walkthrough in `milestones/M15.md`.
- **Automated closure**: historical backend/frontend gates and isolated Docker build evidence remain in the local ignored `review-notes/M15-report.md`. The milestone is unaccepted and paused; check the resume checkpoint and author exceptions before continuing, without automatically repeating skipped manual checks.
- **Walkthrough repair (2026-10-01)**: the real PDF remote-transfer failure triggered the frozen local adapter/upload route, which was implemented and installed. The author subsequently confirmed real PDF/valid-image intake, replay and the single-Expense path. Work is now paused; see the resume checkpoint in `milestones/M15.md` for remaining and author-skipped checks. Adapter instructions are preserved on the development branch.

---

## 4.si Small improvements (separate from milestone numbering)

- **SI1 · Document ordering, Quote duplication and Sent copies**: design agreed on 2026-10-25; all four implementation steps, automated gates and independent blind reviews are complete, and the author accepted the recorded representative walkthrough scope on 2026-10-25. The [implementation plan](improvements/SI1.md) follows the milestone format with four atomic steps, contracts, migration notes, review points and acceptance checks. Reuse existing line-order persistence and rounding; duplicate Quotes into new independently numbered drafts; optionally save all new outgoing messages and attachments to the provider's Sent mailbox through IMAP. M15 remains paused.
- **SI2 · Revising undelivered Standard Invoices**: design agreed on 2026-10-26; orchestrated implementation is in progress, with author acceptance pending. The [implementation plan](improvements/SI2.md) defines five atomic steps covering audit/eligibility, snapshot-preserving preview, transactional save, editor/history and regression closure. Retain SENT and the original number; allow revision only before delivery/payment and without dependent correction/settlement chains. Delivered financial changes use existing correction workflows. Reuse existing T&C content blocks; no new Dictionary template feature. M15 remains paused.
- **Deferred**: product-manual PDF management and checkboxes for choosing extra quote attachments need a separately designed future milestone. SI1 reserves no schema or milestone number for it.

---

## 4.x Beyond the roadmap (vNext) · External bank feed integration (memo only, not in M0–M15)

> **Status**: Explicitly **not doing** in the current roadmap through M15; this note is kept here so it's easy to pick up later. **No DB schema is reserved now** — any future columns/tables are additive migrations, existing data is unaffected, cost is acceptable, no need to pre-allocate.

- **What it is**: Connect an **external transaction provider** and automatically pull transaction feeds from partner banks via API, eliminating manual entry. **Provider-agnostic**: Plaid (used behind YNAB), GoCardless Bank Account Data (formerly Nordigen, EU open banking, AIS free tier), Tink / TrueLayer, etc. — selection deferred.
- **Two use cases**:
  - **Payment reconciliation**: Read feed Descriptions (use an LLM to recognise Invoice Numbers etc.) → attempt to match existing invoices → link / suggest a Payment (connects to M7).
  - **Expense import**: Turn feed entries directly into expenses, pre-filling known fields (amount / date / counterparty); at bookkeeping time, use **M8's AI receipt fill** to complete net amount / VAT rate / category (connects to M8).
- **Sync semantics (key product direction, YNAB-style)**:
  - **Sync only from first connection onwards**; it is fine if **historical data is not back-filled** — consolidate the current state at connection time.
  - **Matching is not required**: Reconciliation is a best-effort convenience feature, not a prerequisite for accounting correctness.
  - **Bank feed is not the single source of truth**: Small businesses have **cash income/expenses that don't go through bank accounts**, so **full real-time sync is not required** on either side; the feed is just one convenient input source. Specific reconciliation / deduplication logic deferred.
- **Implementation principles (when actually built)**: **Polling not webhooks** (self-hosted, no inbound entry points, guardrail 9); provider behind an **abstract interface** (same as M4's exchange-rate provider); credentials use M2 typed settings + at-rest encryption. Low sync frequency (once or twice a day is sufficient), pay-as-you-go cost-friendly.

---

## 4.y Beyond the roadmap (vNext) · PDF document/letterhead template customisation (memo only, not in M0–M15)

> **Status**: Raised by the author in the 2026-06-14 M9 walkthrough, explicitly **deferred**. Elaborates on M9's OUT item "custom / multiple PDF template sets → deferred (one template family + CSS interface)". **No schema reserved now** — everything will be additive.
>
> **Partially shipped ahead (2026-06-29)**: the minimal subset — a company `legal_name` field + an auto-rendered "trade name of" disclosure sentence in the per-page footer of every invoice/quote/receipt PDF — the footer leads with `{trade} is a trade name of {legal}` instead of the bare trade name (locale-aware EN/ZH, rendered only when `legal_name` is set; empty/whitespace treated as unset) — was pulled forward and implemented (migration 0025, label `trade_name_disclosure`; orchestrator single-step, blind-reviewed with zero findings). The **full template editor** (freely repositionable `{{ }}` placeholder blocks) remains vNext as described below.

- **What it is**: Make invoice/quote PDF templates **editable in Settings**, **modelled on M9's email templates** — plain text + `{{ }}` placeholders (e.g. `{{COMPANY_NAME}}` / `{{EMAIL}}` / `{{ADDRESS}}` / `{{LEGAL_NAME}}` …), with these blocks **freely repositionable**. **Primarily targets the letterhead** (company identity block), e.g. optionally adding "Trade name of <legal name>", sometimes included, sometimes not.
- **Reusable foundation already available**: Typed settings + three-tier locale resolution chain + settings panel (gear expandable panel) + placeholder engine — all of it is the M9 email template infrastructure (`doc_type × locale`).
- **The big concern is security (guardrail 7)**: User input entering PDF = XSS/SSRF surface; reuse / strengthen sanitisation — reference the two bugs fixed 2026-06-14: `{{ css | safe }}` font escaping and SVG `<style>` inline class sanitisation; body uses "plain text + explicit placeholders + escaping" rather than arbitrary HTML/CSS. Plus template editor UI + preview + built-in defaults when values are absent.
- **Granularity**: Configured per document type (invoice / quote) × language (EN / ZH), following the email template structure.

## 4.z Beyond the roadmap (vNext) · Customer address free-text block (memo only, not in M0–M15)

> **Status**: Raised by the author in the 2026-06-14 M9 walkthrough, explicitly **deferred**.

- **What it is**: Below the customer's **structured address** (street / house number / postcode / province / city / country), add a **free-form text area**.
- **Use case**: **Bilingual customers** — structured fields are filled with Latin/English address; the free-text area holds the **same address repeated in another script** (e.g. Chinese).
- **Implementation sketch (when built)**: Add one additive text column to the `address` (or customer) model (guardrail 10: `text`) + schema + customer editor UI + invoice/quote PDF renders the free-text below the structured address block (preserve line breaks, autoescape). Bounded scope, but spans model + UI + PDF.

## 4.aa Beyond the roadmap · Operational hardening and maintenance backlog (memo only)

> **Status**: The author retired the former unsliced M13 “Wrap-up / pre-GA health check” placeholder on 2026-09-02 and repurposed M13 for the bounded Document Artifact Backfill. The items below are **not acceptance conditions for M13** and have no scheduled milestone. Promote an item into a separately designed milestone only when it has concrete value and scope.

- **Operational backlog**: backup/restore scripts and a full restore drill (`pg_dump` plus uploaded-receipt volume), expanded deployment/operator documentation, and any future security/performance hardening with a specific finding rather than generic polish.
- **Maintenance backlog**: the M8.5 expense frontend display refinements, M10 Dashboard dead `_MONTH_LABELS`/unused `dashboard.loadError`, optional report PDF/CSV export, and broad EN/ZH review beyond the per-change symmetry gates already enforced by every milestone.
- **Continuing rule**: i18n symmetry, security regression coverage, performance/query-count checks where relevant, Docker build and documentation parity remain part of ordinary Definition of Done; moving generic cleanup here does not weaken those gates.
- **Migration baselining remains DROPPED (decided 2026-06-19)**: `v0.1.0` went live for self-use on 2026-06-17, so the production database carries real data and its `alembic_version` follows the live chain. Keep the accumulated linear Alembic migrations; fresh databases replay them, and all future migrations are additive.
- **Release `latest` handling remains DONE (2026-06-17)**: `release.yml` uses `flavor: latest=auto`; only stable semver tags move `:latest`, while prereleases do not.

---

## 5. Atomic step template + Definition of Done

> When detailing a milestone, every atomic step is broken down using this template (see `milestones/_TEMPLATE.md`). **Solo development — no mandatory PR**: self-test + CI green is enough to merge to `main`; open a branch/PR when intentional human review is wanted.

**An atomic step contains:**
- **Goal**: One sentence.
- **Contract**: API schema additions/changes for this step (OpenAPI fragment) — **defined first**.
- **Backend tasks** / **Frontend tasks**: Two columns, parallelisable (both against the contract above).
- **Migration**: Alembic (if tables are created or columns changed).
- **Tests**: Backend pytest (services calculation logic must be tested); frontend basic validation where necessary.

**Definition of Done (every step must pass all of these):**
- [ ] `ruff check` + `mypy --strict` pass
- [ ] `pytest` passes (pricing/numbering/fx/reports and other **money logic must have unit tests**)
- [ ] If the contract changed: `npm run codegen` regenerates `schema.d.ts`, CI drift check passes
- [ ] `frontend` build passes (`vue-tsc + vite build`)
- [ ] **Deployment smoke**: `docker build` passes; the milestone's "🟢 deployment smoke test" can be manually exercised
- [ ] **CI four gates green** (green = merge to `main`; solo development, no mandatory PR)
- [ ] Complies with [§2 Global Constraints], no violations
- [ ] Self-check: naming/layering consistent with the reference project, `description` uses `text`, money logic in `services`

---

## 6. Deployment and smoke-test loop

- **Development mode**: Frontend/backend separated — frontend `npm run dev` (Vite, proxy `/api`), backend `uv run uvicorn jai.main:app --reload`, database via `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d postgres` (only the Postgres service; DB port bound to `127.0.0.1` only, host port adjustable via dev-only `POSTGRES_DEV_PORT`).
- **Deployment mode / milestone acceptance**: Production `docker compose up -d` (**single-container app + Postgres**, pulls from GHCR; migration service automatically runs `alembic upgrade head`); local integration uses `docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build` → browser follows the milestone's "🟢 deployment smoke test".
- **CI**: Four gates run on every push; `main`/PR additionally runs docker-build; `v*` tags trigger multi-arch image publishing. Green = merge to `main` (solo development, no mandatory PR).
- At the end of each milestone, record one line of acceptance conclusion + known remainders in `docs/plan/milestones/M<x>.md`.

---

## 7. Plan document structure & JIT refinement flow

```
docs/
  insight/btw-aangifte-2026-guide.md # Dutch VAT/BTW filing conventions (authoritative, structured guide to official Tax Authority instructions)
  plan/
    roadmap.md                       # This document: overview + methodology + constraints + M0–M15
    milestones/
      _TEMPLATE.md                   # Milestone refinement template
      M0.md, M1.md, ...              # Produced JIT before starting work (atomic step lists)
    improvements/
      SI1.md                         # Separately numbered small-improvement implementation plans
```

**JIT refinement flow (when entering each milestone)**:
1. Tell the agent: "Read `docs/plan/roadmap.md` §2 constraints + the target milestone's entry, then read the corresponding sections of the analysis documents, then use `milestones/_TEMPLATE.md` to break M<x> into atomic steps."
2. Review the atomic step list for that milestone (backfill "to be refined" product decisions from the analysis docs as needed, e.g. whether units are optional, exact VAT numeric conventions, etc.).
3. Implement step by step, each step passing [§5] DoD.
4. Milestone wrap-up: walk through "🟢 deployment smoke test" and record acceptance conclusion.

Small improvements use the same implementation-plan structure and quality gates, with their own `SI<n>` identifiers under `improvements/`; they do not change milestone numbering or resume a paused milestone. English plans ship with matching `_zh.md` mirrors.

---

## Progress tracker

| Milestone | Content | Status |
| --- | --- | --- |
| M0 | Foundation skeleton | 🟢 Done |
| M1 | Authentication + email foundation | 🟢 Done (2026-06-05; steps 1–4 + deployment smoke tests 1–7 all passed) |
| M2 | Company profile + three-tier settings (complete) | 🟢 Done (2026-06-08; steps 1–4 + deployment smoke tests 1–6 all passed) |
| M2.5 | Unified settings entry + settings UX refactor (Affine-style expandable panel) | 🟢 Done (2026-06-08 steps 1–3 + smoke tests 1–6 + SMTP radio follow-up; 2026-06-09 step 4 account-level language persistence wrap-up · all passed) |
| M3 | Customers | 🟢 Done (2026-06-09; steps 1–4 + deployment smoke tests 1–8 all passed) |
| M4 | Dictionaries / master data (+ product/materials catalogue) | 🟢 Done (2026-06-10; steps 1–5 + deployment smoke tests 1–8 all passed) |
| M5 | Pricing engine + invoice core | 🟢 Done (2026-06-11; steps 1–4 + deployment smoke tests 1–11 all passed) |
| M6 | Quotes + conversion + content templates | 🟢 Done (2026-06-11; steps 1–6; smoke tests 1–9 manual pass, 10–11 integration test coverage, 12 pending CI) |
| M6.5 | Cost accounting / quote assistance (internal estimation → quote) | 🟢 Done (2026-06-12; steps 1–5 + deployment smoke tests 1–8 all passed; 987 tests green) |
| M7 | Payments | 🟢 Done (2026-06-13; orchestrator 5 steps, each blind-reviewed + rework converged; ruff/mypy/unit tests 404/integration 641/codegen no drift/build all green; manual walkthrough smoke tests 1–8 passed, #9 single-currency UI pending FX frontend, isolation/cascade covered by integration tests). Payment sub-cent edge case (3-decimal total can never balance at cents) deferred to M7.5 |
| M7.5 | Currency rounding correction (round to minor unit) | 🟢 Done (2026-06-13; orchestrator 3 steps blind-reviewed converged; step 2 one docstring fixup, steps 1/3 zero findings; **round at line level**, `F2026-009`→`3865.16`; ruff/mypy/unit tests 426/integration 644 (+3 F2026-009 full-payment regression) all green; zero migration/zero contract/no codegen; payment/costing/estimate service code zero changes; author manual walkthrough smoke tests 1–3 passed, no findings) |
| M8 | Expenses (incl. AI receipt fill + AI supplier price list recognition, parallelisable with document track) | 🟢 Done (2026-06-14; orchestrator 5 steps blind-reviewed converged [expense+split storage / storage receipts / recurring expenses / AI receipt fill / frontend wrap-up]; final walkthrough refinements collapsed into one wrap-up commit [deductible follows category / receipt bind-mount uid1000 / AI probe 64×64 / inject current date / summary written to note follows UI language / auto-compute VAT backend endpoint / prompt "permanent default + custom append"]; ruff/mypy/unit tests 599/integration/build/no drift all green; smoke tests 1–5 manual pass, 7 integration covered, 8 passed, 6 recurring expense author not using yet (does not affect acceptance), 9 remote CI pending confirmation. AI uses OpenAI-compatible Chat Completions (`httpx` hand-built, no SDK; base_url/model/key/prompt user-configured + multimodal test; PDF rasterised via pypdfium2 to image for unified image_url). **Follow-on not done**: AI supplier price list → M4 catalogue) |
| M8.5 | Expense bookkeeping fields (payment source / business% / depreciation years, aligned with NL bookkeeping Excel) | 🟢 Done (2026-06-14; orchestrator 2 steps blind-reviewed converged, both steps **zero findings / zero rework**; 3 **record-only** additive fields [`paid_by` / `business_percentage` / `depreciation_years`], `expense` + `recurring_expense` parity, migration 0021 NOT NULL+server_default auto-backfill; **no new money logic** — current-year amortisation / deductible VAT by year / quarterly / BTW all deferred to M10; ruff/mypy/unit tests 626[+27]/integration slice 79[+19]/build/no codegen drift all green; author manual walkthrough: backend/migration/contract/recurring parity passed, old data migration backfill skipped due to deleted data+not yet live, **frontend display issues found in walkthrough moved to the unscheduled §4.aa maintenance backlog**; D1–D9 jointly confirmed against author's Excel column by column) |
| M9 | PDF (email foundation in M1) | 🟢 Done (2026-06-14; orchestrator 8 steps blind-reviewed converged; steps 1/4/5 each 1 rework round [Content-Disposition RFC6266 filename / receipt label key / integration missing company setup], steps 2/3/6/7 zero findings, one commit per step; WeasyPrint+Jinja, invoice/quote/receipt PDF by locale download [resolution chain override>customer>company>en], company-editable email templates+placeholder engine, email_log sending [attachment+cc, SENT/FAILED redacted], migrations 0022 customer.locale / 0023 email_log, frontend download+send dialog+Email log; ruff/mypy/default 760/integration 785/build/i18n EN-ZH symmetric 1001/docker build+in-container Chinese PDF rendering all green. **Author manual walkthrough passed**; walkthrough raised and fixed [each Opus blind-reviewed zero findings, separate commits]: ① PDF in-app preview [`0f75310`, same commit includes invoice/quote layout: remove Description column+Item bold with description below+all 2-decimal money2/pct+`css\|safe` font fix] ② SVG logo `<style>` inline class sanitisation [`4cfd369`, class-styled logos no longer all-black, **logo must be re-uploaded**] ③ multi-page per-page footer [`b9090ae`]; after fixes default 802/PDF integration 138 all green. **Deferred**: full PDF letterhead template customisation→§4.y, customer address free-text block→§4.z, public link/unique_hash, read receipts, receipt email/multi-payment summary receipt, PDF cache/queue, NL language PDF, VAT reports→M10, gradient SVG logo not supported) |
| M10 | Reports / dashboard (incl. VAT filing) | 🟢 Done (2026-06-15; orchestrator 5 steps blind-reviewed converged [P/L → ⭐BTW filing summary → ICP → expense report → Dashboard], one commit per step `89ab353`→`273ed75`; ruff/mypy/unit tests 966+integration 788/codegen no drift/build/docker build all green. **Tax law decisions 2026-06-15 jointly confirmed with author against official guide `docs/insight/btw-aangifte-2026-guide.md` (Opus read all 41 pages) and frozen**: NL ruleset selected by `company.country_code` + other-country fallback+banner, hoog/laag/zero rate bands persisted as defaults 21/9/0, 5b full deduction+private use via 1d (year-end computed by business%), 5a/net payable as auxiliary totals (official only names 5b, not 5c), EU intra-community purchases=4b (not art.23 import), non-EU import/domestic reverse charge/OSS/herziening/KOR all N/A v1, reports carry disclaimer. **Author imported 2026 Q1–Q2 data and manual walkthrough passed**; walkthrough raised and fixed [each Opus blind-reviewed zero findings, separate commits]: ① Expense date picker off-by-one [`1a5a94a`] ② outbound document letterhead leaking customer alias → derived billing_name [`853f07c`] ③ P/L month/quarter granularity replaced with MTD/QTD/YTD period presets + highlight derived from interval [`df2ba13`]. See `milestones/M10.md` acceptance conclusion. **Deferred**: multi-currency ICP/3b column split deferred to FX; Dashboard dead constants/unused keys moved to the unscheduled §4.aa maintenance backlog) |
| M11 | Mileage expenses (private transport used for business) | 🟢 Done (2026-08-21; orchestrator steps 1–5 blind-review convergence; full automated gates green; author walkthrough accepted; two walkthrough UX fixes clean-reviewed; see `milestones/M11.md`) |
| M11.5 | Quote deposits and final-invoice settlement | 🟢 Done (2026-08-28; recovery orchestrator review of the 2026-08-27 base implementation and milestone cross-step review converged with no findings; orchestrated receipt-email refinement added localized single-language warnings and source-audited email, converged after three fixup/re-review rounds, and passed Ruff/mypy/default 1067/integration 872/migrations 14/codegen no drift/build/i18n 1236/Docker; author walkthrough accepted with no findings; historical base-milestone per-step commits could not be reconstructed) |
| M12 | Advance/Final Invoices and Credit Notes | 🟢 Done (2026-09-02; orchestrated Steps 1–11 and blind reviews converged; Ruff/mypy/default 1125/integration 1065, migrations, codegen, frontend Node/Vitest 122 + build + EN/ZH 1423, Docker/CJK PDF all green; risk-based author walkthrough plus final read-only test-data audit passed; five walkthrough refinements fixed) |
| M13 | Document Artifact Backfill | 🟢 Done (2026-09-05; orchestrated Steps 1–4 and blind reviews converged; AI output-budget walkthrough fix re-reviewed with no findings; Ruff/mypy/default 1243/integration 1074, migrations, codegen, frontend 148 + build + EN/ZH 1458, Docker/CJK PDF all green; author Standard-Invoice Happy Path passed, remaining manual paths accepted on automated evidence) |
| M14 | Receipt inbox and review | 🟢 Done (2026-09-29; author PDF walkthrough accepted; browser concurrency and images accepted on automated evidence; non-blocking carryovers in `milestones/M14.md`) |
| M15 | Agent receipt intake and business drafts (MCP; Hermes/Telegram first) | ⏸️ In development, paused by the author on 2026-10-24; implementation preserved on `wip/m15-agent-accounting-paused`, outside main; walkthrough stopped at 3/6 preparation, not accepted; see `milestones/M15.md` |

Separately numbered improvements:

| Improvement | Content | Status |
| --- | --- | --- |
| SI1 | Document ordering, Quote duplication and server-side Sent copies | 🟢 Done (2026-10-25; four steps and independent blind reviews complete; Quote action refinement accepted; representative author walkthrough accepted with skipped paths recorded); see [implementation plan](improvements/SI1.md) |
| SI2 | Audited revision of undelivered, unpaid Standard Invoices | 🟡 In progress (Steps 1–2 automated gates and independent review complete; Steps 3–5 and author acceptance pending); see [implementation plan](improvements/SI2.md) |

> Legend: ⬜ Not started | 🟡 In progress | 🟢 Done (deployment smoke test passed) | ⏸️ In development but paused (not the active milestone)

# M15 · Local receipt adapter

> 🌐 **English** · [中文](M15-local-receipt-adapter_zh.md)

> **Status (2026-10-24)**: ⏸️ M15 remains in development and is paused at the author's request. Its implementation and this guide are preserved on `wip/m15-agent-accounting-paused`, outside `main`. The procedures below are for a future resume, not an instruction to deploy now. Parts of receipt intake have been walked through, but acceptance is incomplete; see the [M15 resume checkpoint](M15.md) for progress and author-skipped checks.

## Why this path is needed

On 2026-10-01, the author tested a real 106,388-byte Telegram PDF. Hermes could read it and load `jai-receipts`, but could not reliably place the complete Base64 into remote MCP arguments. No submission succeeded. This triggers the fallback already frozen in [M15 Step 5](M15.md). Repeat real image/PDF, stored-original SHA-256, replay and phone-link checks after installation; automated checks do not replace acceptance.

The [adapter](../../../integrations/hermes/jai-ledger/receipt_adapter.py) is a local stdio MCP subprocess managed by Hermes. It takes original attachment paths, filenames and MIME types and streams multipart to `POST /api/v1/agent/receipt-inbox`. No new container or Hermes source change is required. The endpoint checks the same bearer token and `receipt:intake` scope before parsing, bounds the request body even without Content-Length, and calls the same M14 validation, RLS and atomic intake/replay service as remote MCP. File bytes and Base64 stay out of model arguments and results.

## Install one action at a time

These commands match the author's **`accountant`** profile and confirmed **development port 8000**. Run repository-relative commands from the project root. Do not operate the production app, database or default Hermes gateway.

1. Rebuild the development app to load the new endpoint. There is no new migration:

   ```sh
   docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build app
   ```

2. Ask Hermes to report only the current original attachment's full cache path and directory, without encoding, submitting or copying the file. Confirm the actual host-visible path. The locally inspected Hermes source uses profile-relative `cache/documents` and `cache/images`, with possible legacy `document_cache`/`image_cache` directories. Approve only a specific attachment-cache root containing the required originals, such as the profile's `cache` when both types really live there. Never approve `/`, `/tmp`, HOME, the entire profile, or a directory containing credentials. Do not guess container/host path translations or invent paths.

3. Install the adapter and updated English skill. Keep the already-installed request-ID helper; only the skill copy's helper path is adapted:

   ```sh
   install -m 700 integrations/hermes/jai-ledger/receipt_adapter.py \
     "$HOME/.hermes/profiles/accountant/helpers/receipt_adapter.py"
   sed 's@/profiles/jai-ledger/@/profiles/accountant/@g' \
     integrations/hermes/jai-ledger/SKILL.md \
     > "$HOME/.hermes/profiles/accountant/skills/jai-receipts/SKILL.md"
   ```

4. Preserve token and Telegram settings in the profile's private `.env`, adding these non-secret values with **actual absolute paths**:

   ```dotenv
   JAI_BASE_URL=http://127.0.0.1:8000
   JAI_ADAPTER_SCRIPT=/actual/home/.hermes/profiles/accountant/helpers/receipt_adapter.py
   JAI_ATTACHMENT_CACHE_ROOT=/verified/attachment/cache/root
   ```

   The adapter needs Python 3.12+ and `uv`. Its script pins MCP 2.2.0 and httpx 0.28.1 in a separate script environment; Hermes' Python environment is not modified. First launch may install these dependencies. Its default per-file limit is 10 MiB, matching JAI. If JAI's `MAX_RECEIPT_BYTES` is changed, set matching `JAI_MAX_RECEIPT_BYTES` in the adapter environment and explicitly forward it in the config's `env`.

5. Merge [config-receipts-local.yaml](../../../integrations/hermes/jai-ledger/config-receipts-local.yaml) into this profile's `config.yaml`, keeping one `mcp_servers` key and preserving provider/model settings. Remote `jai` exposes `get_receipt_status`; local `jai_receipts` exposes `submit_receipt`. Remove remote Base64 `submit_receipt` from the active inclusion list. For a full workflow, preserve the other 15 authorized remote tools and add the same local entry. The inspected Hermes config loader recursively expands `${VAR}` in `env` too; explicitly forward only the required JAI values. Never paste the token into YAML, shell arguments or chat.

6. Verify and apply only to this profile:

   ```sh
   hermes -p accountant mcp test jai_receipts
   hermes -p accountant gateway restart
   ```

   The local diagnostic should discover one tool. Remote `mcp test jai` may discover all 16 server tools even when the client inclusion list registers fewer tools.

7. Load the updated existing `jai-receipts` skill. If `/jai-receipts` is unknown, ask in natural language to read it using `skill_view`; absent menu entries or visible loading notices alone do not establish failure. Start a fresh conversation if the old rules remain in context. Send one image as a Telegram file/document and one PDF, with explicit submission instructions. Hermes should run the operation-specific request-ID helper, call `mcp_jai_receipts_submit_receipt` with short original-path arguments, and then call remote `mcp_jai_get_receipt_status`. The helper can still need terminal approval under Hermes' configured policy; do not disable global approvals just for this walkthrough.

8. Follow the original [deployment self-tests](M15-hermes-telegram.md): stored originals must match source SHA-256, intended purchases must yield one pending item each, identical same-event retries must replay, current AI status must be accurate, and the returned review link must open on the phone. JAI's browser-link `BASE_URL` remains separate from the adapter's loopback `JAI_BASE_URL`. Only the owner confirms in the web app.

## Errors and boundaries

- `JAI_ATTACHMENT_REJECTED`: check approved root, actual path, file type and size. The adapter rejects traversal, symlinks at every path component, hard links, devices/FIFOs, directories, empty and oversized files. Do not broaden roots or copy arbitrary files to bypass refusal.
- `JAI_SUBMISSION_OUTCOME_UNKNOWN`: the response was unusable/lost, possibly after durable commit. Say the result is unknown and retry only the exact same request ID, source, filenames, MIME types and original bytes; do not mint a fresh ID or assert definite failure. Definite auth/validation/conflict errors retain the MCP error codes.
- Stable IDs still require actual trusted session/message metadata. The long-lived stdio server cannot inherit a future Telegram turn's environment; use the existing per-turn helper and pass its output unchanged. Never synthesize missing source coordinates.
- Albums remain conditional on a trusted complete aggregator; the adapter does not add aggregation or weaken the check.
- This removes model Base64 copying. Overall time still includes Telegram download, model tool selection, configured terminal approval and JAI's existing AI extraction; no fixed response time is promised.
- No agent confirmation, expense posting, draft lifecycle operation or extra accounting permission is added.

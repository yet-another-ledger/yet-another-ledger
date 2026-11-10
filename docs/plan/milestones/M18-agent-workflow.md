# M18 · Client-neutral Agent mileage workflow

> 🌐 **English** · [中文](M18-agent-workflow_zh.md)

> **Version requirement:** JAI v0.14.0 or later, migration `0058`, and `mileage:create`. Start a fresh client session after updating the allowlist/workflow; existing tokens do not gain this scope automatically.

This guide describes the shared workflow for any client connected to JAI's authenticated Streamable HTTP MCP endpoint. It complements the [M18 design and acceptance record](M18.md); it does not require Hermes, Telegram, or a particular route-search provider.

## Permission and effect

An owner creates a token with the explicit `mileage:create` permission. That permission grants only `get_mileage_options`, `preview_mileage_expense`, and `create_mileage_expense`. Options and preview are reads; a successful create records one formal mileage trip and its linked Expense. Explain that accounting effect before asking for confirmation. Existing tokens do not acquire the permission automatically. Every call, including a replay, checks that the token is still valid and authorized.

This is a narrow exception to the older blanket rule against Agent Expense posting: only the dedicated mileage workflow may create a formal mileage Expense, and only after the user confirms the complete JAI preview. Receipt confirmation/posting and direct or general Expense creation/editing remain unavailable to the Agent. Owner workflows in the web app are unchanged.

## Collect a complete trip

Ask for a definite trip date and positive one-way distance. Clarify whether the trip was one-way or return; never rely on a client or schema default. Collect any known origin, destination, purpose, and note. These text fields are optional in JAI, but must be shown when supplied.

Call `get_mileage_options` and present the enabled transport types. Match the user's wording to one unambiguous enabled type. Ask when names are ambiguous; do not guess. If the user leaves transport unspecified, JAI resolves the company default and the preview must show which type was selected. Do not infer an ID from a name or use a disabled type.

One create call records one trip with one transport type. If outbound and return legs have unequal distances or need separate facts, collect and preview them as separate one-way trips. Treat each leg as a separate user-confirmed operation, with its own initiating event, request ID, preview and confirmation. Do not bundle multiple legs into one mileage record.

## Route evidence and distance confirmation

If the client has an available search or map tool, it may request a route suggestion. Show the endpoints, travel mode, suggested one-way kilometres, and source to the user. A route suggestion is evidence for discussion, not the accounting input: ask the user to confirm or correct the kilometres. Send only that user-confirmed one-way distance to JAI. Do not add a route provider, coordinates, or route lookup to JAI.

If search returns multiple plausible routes or unclear endpoints, explain the ambiguity and ask the user to select/correct the route or supply the distance. If search is unavailable, errors, or yields no reliable route, say that no reliable suggestion is available and ask the user for kilometres. Never invent a distance or imply that a controlled test fixture was a real provider lookup. If the user cannot supply a distance, stop before preview/create.

## Preview, confirmation, and creation

Before preview, generate one stable UUID for this booking attempt. Send the same `request_id`, optional `source`, and complete `mileage` object to preview and create. Include the full known `MileageExpenseWrite` fields; do not send extra fields. JAI validates the input and returns the normalized mileage input, resolved transport and Expense category, calculation, and `preview_token`.

Present all supplied trip fields, the resolved transport and category, one-way/return choice, total kilometres, effective rate and date, amount, currency, and any route evidence and user correction. Financial values come only from JAI. Tell the user that confirmation will create one formal mileage Expense linked to the trip. Wait for explicit confirmation of this exact preview. A request to edit any field requires a new preview.

After confirmation call `create_mileage_expense` with the exact request ID, source, mileage body and preview token from that attempt. Do not create without an explicit confirmation. Report the actual response envelope (`operation`) and `mileage` result: created versus replayed status, trip ID, linked Expense ID, amount/currency, and the exact returned `operation.web_url`. Do not reconstruct or normalize the URL.

## Retries and stale previews

- For a timeout or lost response after create, retry the identical tool call with the same request ID, source, mileage body and preview token. A successful replay returns the original result; do this before starting another booking.
- Reusing an ID with different arguments is a conflict. Never change fields, source, or token under that ID.
- If preview/create reports a stale preview, changed configuration, or a failed attempt, explain what needs to be refreshed. Start a new attempt with a new initiating event and new UUID, obtain a fresh preview, present it, and get renewed explicit confirmation before create. Do not reuse an ID for a corrected or renewed attempt.
- In Telegram, derive metadata only from the trusted initiating event using the installed helper with operation name `create_mileage_expense`; pass its full `request_id` and `source` unchanged. A second leg or renewed attempt needs a distinct trusted initiating event. Never copy, guess, or edit Telegram source IDs to force a new request ID.
- Other clients generate a UUID before preview and keep it with the attempt state across conversation turns. Keep source metadata unchanged when the client uses it. A separate trip or new attempt gets a new UUID; an exact network retry keeps the original UUID and full payload.

Revoked credentials cannot create or replay. A missing permission should be reported as an access configuration issue for the owner; do not try another token, route, or tool to bypass it.

## Boundaries

JAI remains authoritative for validation, current settings, rates, Decimal calculations, VAT projection, saved trip/Expense values, and replay results. The client does not calculate or adjust money. The Agent cannot use these tools to confirm receipt-inbox items, create general Expenses, edit mileage, manage settings, or perform other Expense lifecycle operations. Existing zero-VAT mileage projection and reporting behavior is retained.

For deployment and integrated author acceptance, follow the [M18 milestone walkthrough](M18.md#deployment-self-tests-and-acceptance). Automated protocol fixtures for route suggestions must be reported as controlled scenarios; real search/map use is a separate client walkthrough result.

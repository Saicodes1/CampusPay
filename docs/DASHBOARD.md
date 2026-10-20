# Student dashboard

Open **http://127.0.0.1:8080/** after `docker compose up -d --build --wait`. Use Alice Student or Bob Student with `campuspay-demo-only` (or your configured demo password). The API documentation remains at `/docs`. This dashboard uses only simulated funds.

## Student flows

* **Add funds:** Overview → Add demo funds → enter an amount or choose $10/$25/$50 → confirm. There is no card input or actual funding provider.
* **Transfer:** Overview → Send money → choose a seeded student and amount → Review transfer → confirm. Dollars are converted to integer cents without floating-point rounding.
* **Purchase:** Campus payments → Review purchase → confirm the merchant, item and price → Pay. The demo merchant captures immediately. The backend validates product price, available balance and idempotency and commits the intent, ledger and outbox together.
* **Merchant code:** A merchant creates an intent through `/api/intents`. The student chooses Enter payment code and pastes its UUID or `campuspay:intent:UUID`, reviews it, then authorizes. This reserves funds without spending them. The merchant captures separately through `/api/intents/{id}/capture`. Until capture, Overview lists the hold and allows cancellation/release. Refresh after the merchant changes the state.
* **Activity:** View incoming/outgoing amounts, counterparty and date. Search/filter applies to loaded entries. Load older activity explicitly for more history. The student view never returns another student's full ledger.
* **Account:** View your seeded student details and sign out. Merchant and admin credentials cannot use the student endpoints.

## Retry and session behavior

Authentication is kept in JavaScript memory, not cookies, localStorage or sessionStorage. Requests omit browser-managed credentials. A refresh/reload therefore requires sign-in; credentials are cleared on sign-out.

Before sending a mutation, the dashboard records its actor, path, body and stable UUID idempotency key in sessionStorage. A network failure or retryable server response leaves this metadata in place, shows a pending-payment banner and blocks other new mutations. **Check payment** reuses the exact request. A matching success clears the pending metadata; a definitive business rejection clears it and shows the error. Requests are scoped to the signed-in student's storage key. Signing in as Bob will not submit an unresolved Alice request.

The metadata deliberately contains no password or Authorization header. Session storage is not a durable financial record: closing the tab, clearing data, or storage restrictions may lose it. After losing an uncertain request key, check account activity before creating another payment. The server's ledger and idempotency table remain the durable source of truth.

## Browser security and limits

The dashboard and API share an origin; no development proxy, npm server, CORS wildcard or separately hosted frontend is needed. Origin-bearing writes must match the request's scheme/host/port exactly. Cross-origin and `null` origins remain blocked; headerless non-browser API clients continue to work. Use the same host spelling throughout a session (`127.0.0.1` and `localhost` are different browser origins).

Local assets load under a self-only Content Security Policy; rendering uses text nodes rather than interpolating server data into HTML. Native dialogs provide keyboard focus trapping and Escape handling; visible labels and status/error announcements are included. Layout adapts to narrow screens without a desktop-width sidebar. No third-party scripts, fonts, tracking, fabricated charts or decorative transaction data are used.

The fixed catalog contains demo items only. No inventory, fulfillment, real student directory, merchant onboarding, camera scanner, profile editing or actual university identity integration is implied. Manual payment-code authorization and instant catalog purchase are distinct, documented flows. Merchant refunds still use the merchant API; a separate merchant dashboard is outside this change.

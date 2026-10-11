# WP A5: Dealer-facing notifications, commercial e2e journeys, commercial handbook pass

Lane A (commercial), round 5. Placeholder prefix `§A5-n`.

Everything from rounds 1–4 is on main: invoices, credit notes, returns, collections, the dealer portal backend (C3), push (C1b) and alerts (B4). The dealer portal **frontend** is being built in parallel this round (WP C4) at `/dealer/*`.

## Part 1: Dealer-facing notifications

These are the proposal's mobile notifications for dealers: "Order dispatched", "Courier name & tracking ID", "Delivery expected date", "Payment reminder", "Approval request / New order request".

Use the `NotificationType` members C3b declared. Raise each one in the same transaction as the event. The recipients are the dealer users of that customer who hold the matching dealer right. Add a resolver `user_ids_for_customer(customer_id, *, permission)` next to C1a's vendor resolver.

| Event | Right | Link |
|---|---|---|
| Sales order confirmed (DRAFT→CONFIRMED) for a dealer customer | order | `/dealer/orders/{id}` |
| Dispatch dispatched: "Order dispatched", with courier/transporter name, tracking ID, LR and expected delivery date in the body | order | `/dealer/orders/{order_id}` |
| Dispatch delivered | order | |
| Tracking event added on an in-flight dispatch, grouped per dispatch | order | |
| Invoice issued (A2) | account | `/dealer/invoices/{id}` |
| Credit note issued (A3) | account | |
| Return received / credited (A3) | return | |
| Payment reminder: when A4's daily sweep reminds the internal assignee, also send a gentle reminder to dealer users with `account`, at most once per `DEALER_PAYMENT_REMINDER_DAYS` (7, `§A5-n`), with the overdue total and the oldest due date | account | `/dealer/account` |

Agree the `/dealer/...` paths with C4's routes. The `test_notification_action_urls.py` guard from B4 must pass after integration, so use exactly these paths and confirm them against `specs/C4a-dealer-app-frontend.md`.

**Tests:** one integration test per event, covering the recipients, the link, and that no other customer's users are notified. Add a unit test for the resolver.

## Part 2: Commercial e2e journeys (`tests/e2e/specs`, Playwright)

Read `tests/e2e` and the README first, including how `auth.setup.ts` and `lib/session.mjs` work and how journeys assert writes back through the API. Add `commercial-flows.spec.ts`, API-driven like `order-to-receipt.spec.ts`. Use a per-run suffix, one shared page, and assert every write back through the API.

1. SO → confirm → dispatch → **issue invoice** (preview, then issue). Assert the AR journal, the outstanding balance, the SO reaching INVOICED, and the PDF job completing.
2. Customer receipt → allocate to the invoice. Assert PAID, then INVOICED → close the SO.
3. **Sales return** of part of a dispatch → receive (QC hold) → QC accept → close → **credit note**. Assert the stock state, the credited amount, the CREDITED/PAID status, and that the GST summary nets the credit.
4. Overdue invoice → collections sweep (call the service or task through a test endpoint only if one exists; otherwise drive it through the API surfaces) → follow-up shows on the worklist → promise → resolve.
5. Brand created → item tagged → item list filtered by brand.

Also add one **UI** journey to `ui-write-paths.spec.ts`: raise an invoice from the dispatch page through the form, then see it in the sales-invoices list.

The stack must be running for these. **Do NOT start or stop the user's Docker stack.** Write the specs, make them type-check and lint clean, and run them **only if** the lane's API and web servers are already running. Otherwise state "not executed; runs in final integration". The final integration round runs every journey.

## Part 3: Commercial handbook pass

`docs/handbook/content.json` and `tests/e2e/scripts/capture-screens.mjs`:
- Make every commercial screen added in rounds 1–4 present and accurate: Masters (Brands, Manufacturers), Sales invoices, Credit notes, Sales returns, Collections, the Dealer performance report, the dashboard tiles, and the customer adjustment.
- Remove the stale claims the readers found, such as "has no screen", "customer charges are journals" and "payments due alerts" (now real).
- Update the Finance and Sales chapter intros.
- Do not run the screenshot capture; it needs the running stack. Final integration will.

## Docs

`assumptions.md` (`§A5-n`), `api.md` if needed, handbook.

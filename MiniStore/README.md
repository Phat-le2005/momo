# FinShop LIVE — Independent Live-Commerce Platform (`MiniStore/`)

Independent live-commerce system (`Next.js + TypeScript + Tailwind CSS` frontend on port `3100`, `FastAPI + PostgreSQL + SQLAlchemy + Alembic` backend on port `8100`, standalone worker `python -m app.worker`) designed to operate completely independently of `FinResolve/`.

## Prompt 10.1 Hardening Highlights

1. **Authentication & Session Boundaries**
   - **Customer sessions**: `GET /api/v1/store/bootstrap` returns public storefront metadata only (zero session tokens or PII). Isolated guest sessions are provisioned via `POST /api/v1/store/customers/guest` with cryptographically random, expiring, revocable tokens (`POST /api/v1/store/customers/logout`).
   - **Merchant Center**: Password-authenticated login (`POST /api/v1/merchant/auth/login`) verified against salted `scrypt` hashes in `merchant_accounts`, issuing expiring, revocable `merchant_sessions` (`POST /api/v1/merchant/auth/logout`).
   - **Service Read Connectors (`/api/v1/service/*`)**: Scoped per-credential authentication (`service_credentials`) bound to `(merchant_id, commerce_session_id, allowed_scopes)` with required `X-World-Epoch` validation and cursor-based pagination on `/api/v1/service/events`.
   - **Payment Boundary**: `POST /api/v1/test-payments/mock/notify` requires server-side `X-Mock-Provider-Secret`; browser storefront uses customer-authenticated `POST /api/v1/store/orders/{order_id}/mock-pay` so provider secrets never reach client code.
2. **Payment, Inventory, Checkout & Refund Safety**
   - Durable `provider_notifications` inbox with unique `(provider, notification_id)` and `payload_hash` conflict detection (`409`).
   - Explicit `RECONCILIATION_REQUIRED` handling for late payments on cancelled/expired orders, pre-sync refunded payments, and expired `UNKNOWN` payments.
   - Canonical SHA-256 `request_fingerprint` validation on checkout `idempotency_key` replay (`409` on conflicting reuse).
   - Separate `flash_sale_reserved_qty` vs `flash_sale_sold_qty` so unpaid cancellation/expiry restores Flash Sale quota.
   - Idempotency-first refund replay (works even after payment becomes `REFUNDED`).
3. **Two-Phase Outbox & Worker Separation**
   - Core business worker (`sync_confirmed_payments_to_orders` + `expire_stale_reservations`) runs on an independent loop/session from FinResolve outbox delivery (`dispatch_pending_outbox_events`).
   - Outbox releases DB locks before external HTTP calls, requires both `X-Demo-Session` and `X-Service-Token`, and classifies permanent auth/contract errors (`DEAD_LETTER`), stale world (`REVIEW_REQUIRED`), and transient errors (exponential backoff).

## Product Visual Assets Provenance

- 12 product studio photographs (`MiniStore/frontend/public/products/*.jpg`: `fash-jacket`, `fash-tshirt`, `fash-tote`, `tech-earbuds`, `tech-powerbank`, `tech-cable`, `cosm-serum`, `cosm-sunscreen`, `cosm-lipstick`, `home-tumbler`, `home-blender`, `home-lamp`) were generated specifically for FinShop LIVE test data with clean neutral studio lighting and zero watermarks.
- 2 food product illustrations (`food-coffee.svg`, `food-cashew.svg`) and `store-avatar.svg` are custom local vector assets.

## Environment Variables (Local Test Defaults)

- `COMMERCE_DATABASE_URL`: PostgreSQL connection string (`finshop_commerce`).
- `COMMERCE_SOURCE_MODE`: `MOCK` (only `LOCAL_MOCK` payment is supported in Prompt 10.1; VNPAY Sandbox is deferred to Prompt 11).
- `COMMERCE_TEST_MERCHANT_USERNAME`: default `merchant_owner`.
- `COMMERCE_TEST_MERCHANT_PASSWORD`: local default `FinShopLocal!2026` (must be overridden in non-local envs).
- `COMMERCE_MOCK_PROVIDER_SECRET`: server-side secret for `/api/v1/test-payments/mock/notify`.
- `COMMERCE_SERVICE_API_KEY`: scoped service connector key for `/api/v1/service/*`.

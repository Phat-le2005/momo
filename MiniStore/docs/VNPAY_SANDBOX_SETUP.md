# VNPAY Sandbox Integration & Public HTTPS Gateway Setup (Windows PowerShell)

This guide explains how to expose the **restricted FinShop LIVE Public Gateway** over HTTPS so you can register for VNPAY Sandbox merchant credentials (`https://sandbox.vnpayment.vn/devreg/`) and test genuine VNPAY Sandbox transactions safely.

> **Security Note**: Never expose port `8100` (Commerce API), `8000` (FinResolve API), `5433`/`5434`/`5544` (PostgreSQL), or `/merchant` directly to the internet. Only expose the **Restricted Gateway** on port `8200`, which strictly blocks `/docs`, `/merchant`, `/api/v1/merchant/*`, `/api/v1/service/*`, and `/api/v1/test-payments/*`.

---

## 1. Required Environment Variables (`MiniStore/backend/.env`)

```dotenv
# Keep LOCAL_MOCK working alongside VNPAY Sandbox
COMMERCE_SOURCE_MODE=MOCK

# VNPAY Sandbox (API 2.1.0) — disabled by default until credentials are set
COMMERCE_VNPAY_ENABLED=false
COMMERCE_VNPAY_TMN_CODE=
COMMERCE_VNPAY_HASH_SECRET=
COMMERCE_VNPAY_VERSION=2.1.0
COMMERCE_VNPAY_PAYMENT_URL=https://sandbox.vnpayment.vn/paymentv2/vpcpay.html
COMMERCE_VNPAY_API_URL=https://sandbox.vnpayment.vn/merchant_webapi/api/transaction
COMMERCE_VNPAY_RETURN_URL=https://<YOUR-PUBLIC-TUNNEL-HOST>/payment/vnpay/return
COMMERCE_VNPAY_IPN_PUBLIC_URL=https://<YOUR-PUBLIC-TUNNEL-HOST>/api/v1/payments/vnpay/ipn
COMMERCE_VNPAY_REFUND_ENABLED=false
COMMERCE_VNPAY_TIMEOUT_SECONDS=10
COMMERCE_VNPAY_PAYMENT_EXPIRY_MINUTES=15

# Restricted Public Gateway
COMMERCE_PUBLIC_GATEWAY_ENABLED=true
COMMERCE_PUBLIC_BASE_URL=https://<YOUR-PUBLIC-TUNNEL-HOST>
```

---

## 2. Step-by-Step Windows PowerShell Startup

### Terminal 1 — Commerce API (`127.0.0.1:8100`)
```powershell
cd d:\momo\MiniStore\backend
$env:COMMERCE_DATABASE_URL = "postgresql+psycopg://finshop:finshop@127.0.0.1:5544/finshop_commerce"
$env:COMMERCE_PUBLIC_GATEWAY_ENABLED = "true"
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8100
```

### Terminal 2 — Commerce Background Worker (Order Sync + QueryDr + Outbox)
```powershell
cd d:\momo\MiniStore\backend
$env:COMMERCE_DATABASE_URL = "postgresql+psycopg://finshop:finshop@127.0.0.1:5544/finshop_commerce"
.\.venv\Scripts\python.exe -m app.worker
```

### Terminal 3 — FinShop LIVE Storefront (`127.0.0.1:3100`)
When accessing via the public tunnel, set `NEXT_PUBLIC_COMMERCE_API_BASE_URL=""` so browser requests go through the same-origin restricted gateway instead of `localhost:8100`:
```powershell
cd d:\momo\MiniStore\frontend
$env:NEXT_PUBLIC_COMMERCE_API_BASE_URL = ""
npm run dev -- --hostname 127.0.0.1 --port 3100
```

### Terminal 4 — Restricted Public Gateway (`127.0.0.1:8200`)
```powershell
cd d:\momo\MiniStore\backend
$env:COMMERCE_PUBLIC_GATEWAY_ENABLED = "true"
$env:COMMERCE_GATEWAY_PORT = "8200"
.\.venv\Scripts\python.exe -m app.gateway
```

### Terminal 5 — HTTPS Tunnel (Cloudflare Tunnel — Free, No Credit Card)
```powershell
# Install once if needed: winget install --id Cloudflare.cloudflared
cloudflared tunnel --url http://127.0.0.1:8200
```
*(Alternatively with ngrok: `ngrok http 8200`)*

Copy the generated HTTPS URL (e.g., `https://<random-subdomain>.trycloudflare.com` or your named Cloudflare Tunnel hostname).

---

## 3. VNPAY Sandbox Merchant Registration (`https://sandbox.vnpayment.vn/devreg/`)

Use your actual running HTTPS tunnel URL in the VNPAY Sandbox registration form:

- **Website URL**: `https://<YOUR-PUBLIC-TUNNEL-HOST>`
- **IPN URL**: `https://<YOUR-PUBLIC-TUNNEL-HOST>/api/v1/payments/vnpay/ipn`
- **Return URL** (configured in `.env`): `https://<YOUR-PUBLIC-TUNNEL-HOST>/payment/vnpay/return`

Once VNPAY emails your `vnp_TmnCode` and `vnp_HashSecret`:
1. Set `COMMERCE_VNPAY_ENABLED=true`, `COMMERCE_VNPAY_TMN_CODE`, `COMMERCE_VNPAY_HASH_SECRET`, `COMMERCE_VNPAY_RETURN_URL`, and `COMMERCE_VNPAY_IPN_PUBLIC_URL` in `MiniStore/backend/.env` (never commit `.env`!).
2. Restart Terminal 1 (API) and Terminal 2 (Worker).
3. VNPAY Sandbox will automatically appear as an available payment method in FinShop LIVE Checkout.
4. Keep `COMMERCE_VNPAY_REFUND_ENABLED=false` until your Sandbox account explicitly permits API refunds.

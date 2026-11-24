# FinShop LIVE — Independent Mini Store & Live Commerce (`MiniStore/`)

Hệ thống thương mại điện tử & Live Shopping độc lập hoàn toàn với `FinResolve/` (cơ sở dữ liệu PostgreSQL riêng, migration riêng, backend cổng `8100`, frontend cổng `3100`).

## Cấu trúc
- `MiniStore/backend/` — FastAPI + SQLAlchemy 2.0 + Alembic + PostgreSQL (`python -m app.worker` cho đồng bộ đơn, hết hạn giữ chỗ tồn kho và Event Outbox).
- `MiniStore/frontend/` — Next.js 16 + TypeScript + Tailwind CSS (Storefront mobile-first `/`, `/live`, `/products/[id]`, `/cart`, `/checkout`, `/orders`, `/profile` và Kênh Người Bán `/merchant`).

## Khởi chạy cục bộ
```powershell
# 1. Backend API (cổng 8100)
cd MiniStore/backend
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8100

# 2. Background Worker
cd MiniStore/backend
python -m app.worker

# 3. Frontend (cổng 3100)
cd MiniStore/frontend
npm run dev
```

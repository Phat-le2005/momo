# FinResolve AI — bộ tài liệu đưa thẳng cho Codex / Antigravity

> Đây là **bộ context, hợp đồng và prompts**, **chưa phải mã nguồn sản phẩm đã triển khai**. Bản đầy đủ được tạo từ tài liệu Word tổng quan người dùng cung cấp.

## Cách dùng (chỉ cần 5 bước)
1. Giải nén ZIP. Mở **thư mục `FinResolve/`** bên trong bằng VS Code, Codex hoặc Antigravity.
2. Tạo Git repository tại thư mục này (`git init`). Không cần tự tạo thêm file tài liệu.
3. Gửi nội dung `START_HERE.md` cho coding agent hoặc dán `prompts/00_bootstrap.md`.
4. Chỉ giao **Prompt 00** cho đến khi bootstrap hoàn thành và chạy test, rồi giao **Prompt 01**. Làm tiếp theo thứ tự trong `docs/DELIVERY_PLAN.md`.
5. Mỗi prompt phải sửa mã nguồn thật, chạy test và báo file đã đổi. Không yêu cầu agent chỉ giải thích kiến trúc.

## Trong ZIP có gì?
- `references/FinResolve_AI_Native_v1.2.docx`: đúng bản Word được cung cấp.
- `references/FinResolve_AI_Native_v1.2_fulltext.md`: bản chuyển toàn văn Markdown để AI dễ tra cứu.
- `docs/PRODUCT_OVERVIEW.md`: tóm tắt sản phẩm và cách hiểu **Product-first** mới nhất.
- `docs/PRODUCT_CONTRACT.md`: schema, API, mô hình dữ liệu và invariant.
- `docs/DELIVERY_PLAN.md`: lịch code đến **03/12/2026**.
- `docs/QA_MATRIX.md`: tình huống kiểm thử an toàn và E2E.
- `AGENTS.md`: quy tắc bắt buộc cho coding agent.
- `prompts/00_bootstrap.md` → `prompts/09_release.md`: các bước lập trình tuần tự.
- `ALL_PROMPTS.md`: bản tổng hợp prompt để đọc nhanh.

## Quy tắc
- Lõi điều phối AI/Agent/Policy/Verifier do đội tự viết; có thể dùng API model và thư viện hạ tầng chuẩn.
- Sản phẩm dùng dữ liệu giả lập công khai, không tự hoàn tiền thật, không quảng bá tích hợp MoMo thật khi chưa được cấp quyền.
- Chốt một luồng E2E hoạt động trước, sau đó làm Risk/Taskforce/Mega Live.
- Đội 3 người chia Git branch/worktree, không để nhiều agent sửa chung một thư mục cùng lúc.

**Gợi ý:** `START_HERE.md` là prompt đầu tiên đã chuẩn bị để copy nguyên văn.

---

# Chạy mã nguồn (Prompt 00 — bootstrap)

> Trạng thái: **khung chạy được** (health, `/api/v1`, error envelope, Alembic baseline, trang `/demo` tiếng Việt). Chưa có Case/Evidence/AI/Policy — các tính năng này thuộc Prompt 01+. Mọi dữ liệu là **MOCK**; không có giao dịch tiền thật.

## Cấu trúc

```text
apps/web/          Next.js 16 + TypeScript + Tailwind 4  (/demo, /cases, /risks, /live)
services/api/      Python 3.12 + FastAPI + Pydantic v2 + SQLAlchemy 2 + Alembic
  app/core/        config (env), error envelope, request-id middleware
  app/db/          Declarative Base, engine/session, DB health probe
  app/api/         /healthz, /readyz, /api/v1 router
  alembic/         migrations (0001_baseline = schema rỗng)
  tests/           pytest
infra/             docker-compose.yml (PostgreSQL + api + web)
scripts/           dev.ps1 (Windows), dev.sh (wraps Makefile)
```

## Yêu cầu

Python 3.12+, Node 20.9+ (đã thử với Node 22), PostgreSQL 16+ **hoặc** Docker Desktop.

## Cách 1 — Docker Compose (một lệnh)

```bash
cp infra/.env.example infra/.env        # tùy chọn; có giá trị mặc định chỉ dùng local
docker compose -f infra/docker-compose.yml up --build
```

- Web: http://localhost:3000/demo · API: http://localhost:8000/healthz · OpenAPI UI: http://localhost:8000/docs
- PostgreSQL được map ra cổng host **5433** (tránh trùng PostgreSQL cài sẵn ở 5432). Dữ liệu nằm trong volume `finresolve_pgdata`.
- Container `api` tự chạy `alembic upgrade head` trước khi khởi động.

## Cách 2 — Chạy trực tiếp (không Docker)

Windows PowerShell:

```powershell
.\scripts\dev.ps1 setup                       # tạo services/api/.venv, cài backend + frontend
Copy-Item services\api\.env.example services\api\.env   # sửa DATABASE_URL theo PostgreSQL của bạn
Copy-Item apps\web\.env.example apps\web\.env.local
.\scripts\dev.ps1 migrate                     # alembic upgrade head
.\scripts\dev.ps1 api                         # http://localhost:8000
.\scripts\dev.ps1 web                         # (terminal khác) http://localhost:3000/demo
```

Linux/macOS: `make setup`, `make migrate`, `make api`, `make web`.

Tạo database local (ví dụ, dùng tài khoản quản trị PostgreSQL của bạn):

```sql
CREATE ROLE finresolve LOGIN PASSWORD 'change-me-local-only';
CREATE DATABASE finresolve OWNER finresolve;
```

Nếu CSDL chưa sẵn sàng, `/healthz` vẫn trả 200 với `"status": "degraded"`, còn `/readyz` trả 503 kèm error envelope — trang `/demo` hiển thị "API suy giảm".

## Kiểm thử & kiểm tra chất lượng

```powershell
.\scripts\dev.ps1 check      # ruff + ruff format --check + mypy --strict + pytest + eslint + tsc + next build
.\scripts\dev.ps1 test       # chỉ pytest
```

Tương đương thủ công:

```bash
cd services/api && .venv/Scripts/python -m pytest   # (Linux: .venv/bin/python)
cd services/api && .venv/Scripts/ruff check . && .venv/Scripts/mypy
cd apps/web && npm run lint && npm run typecheck && npm run build
```

Pytest dùng SQLite in-memory cho health probe nên không cần PostgreSQL; test nghiệp vụ từ Prompt 01 nên chạy trên PostgreSQL thật.

## Chế độ nguồn dữ liệu và mô hình

| Biến | Giá trị | Ý nghĩa |
|---|---|---|
| `SOURCE_MODE` | `MOCK` (duy nhất được hỗ trợ) | Dữ liệu mô phỏng. `MOMO_SANDBOX` bị từ chối khi khởi động cho tới khi có quyền + adapter. |
| `MODEL_PROVIDER`, `MODEL_NAME`, `MODEL_API_KEY` | để trống | `model_mode = DEMO_RULES` — luật xác định, UI ghi rõ **không phải AI sinh**. |
| | đủ cả 3 | `model_mode = LLM_API` (adapter thực hiện ở Prompt 02). |
| `MODEL_TIMEOUT_SECONDS`, `MODEL_BUDGET_TOKENS` | 20 / 20000 | Giới hạn cho adapter sau này. |
| `WEB_ORIGIN` | `http://localhost:3000` | Danh sách origin CORS cụ thể, phân tách bằng dấu phẩy; `*` bị từ chối. |

## Hợp đồng API hiện có

### 1. Hệ thống & Giám sát
- `GET /healthz` → `{status: ok|degraded, service, version, environment, source_mode, model_mode, database: {status}, checked_at}`.
- `GET /readyz` → như trên, hoặc **503** nếu CSDL không truy cập được.
- `GET /api/v1` → metadata (`api_version`, `source_mode`, `model_mode`, `synthetic_data_only`).
- `GET /api/v1/openapi.json` → OpenAPI 3.1 schema.

### 2. Demo Session & Cách ly dữ liệu (Prompt 01)
- `POST /api/v1/demo/sessions` → `{session_id, token, status, max_cases, case_count, expires_at, created_at}`
  - Tự động sinh `token` (dsk_...) và gán quota số lượng Case (`max_cases`, mặc định 20).
  - Tự động nạp dữ liệu mẫu giả lập (`seed_fixtures: true`) cho phiên: đơn hàng chờ nhưng giao dịch thành công (`payment_sync`), đơn đã hoàn tiền (`refund_risk`), đơn thanh toán đang chờ (`unrelated_delayed`).
  - Mọi thao tác nghiệp vụ yêu cầu header `X-Demo-Session: <token>` (hoặc `Authorization: Bearer <token>`). Máy chủ chỉ tin cậy phiên được xác thực trên server, không cho phép client tự gửi `session_id`/`tenant_id` tùy tiện trong body.

### 3. Case Center & Tiếp nhận khiếu nại (Prompt 01)
- `POST /api/v1/cases` → Tiếp nhận khiếu nại tiếng Việt, khởi tạo Case trạng thái `NEW`, `version: 1`.
  - **Bất biến nghiệp vụ:** Tin nhắn của khách hàng được lưu trữ dưới dạng bằng chứng loại `CLAIM` (lời khai chưa được kiểm chứng), không bao giờ tự ý nâng cấp thành xác nhận đã thanh toán.
  - Ghi nhận `CaseEvent` loại `CASE_CREATED` (append-only audit log).
  - Hỗ trợ chống trùng lặp qua header `X-Idempotency-Key` (hoặc field `idempotency_key` trong JSON). Khi gửi lại cùng key, hệ thống trả về đúng Case đã tạo (HTTP 200) mà không trừ trùng quota hay nhân bản dữ liệu.
- `GET /api/v1/cases/{case_id}` → Chi tiết Case và danh sách bằng chứng (`Evidence`). Được bảo vệ cách ly tuyệt đối giữa các guest session: session khác không thể đọc Case (trả về 404 để chống dò quét ID).
- `GET /api/v1/cases/{case_id}/events` → Dấu vết quyết định / dòng thời gian kiểm toán (`CaseEvent`).
- `GET /api/v1/cases/{case_id}/status` → Trạng thái nhẹ và phiên bản (`state`, `version`, `updated_at`).

### 4. Quy tắc chuyển trạng thái Case (`transition_case`)
- Đồ thị trạng thái: `NEW -> INVESTIGATING -> WAITING_EVIDENCE / WAITING_APPROVAL / EXECUTING -> VERIFYING -> RESOLVED`.
- Bất biến bắt buộc: Không thể chuyển sang `RESOLVED` nếu thiếu bản ghi `Verification` có `verdict == PASSED`. Mọi nỗ lực nhảy cóc từ `NEW -> RESOLVED` hoặc `VERIFYING -> RESOLVED` khi chưa xác minh đều bị từ chối với mã lỗi 409 `CONFLICT`.
- Mỗi lần đổi trạng thái: tăng `case.version += 1` (khóa lạc quan) và ghi append-only `CaseEvent(type='STATE_TRANSITION')`.

## Hướng dẫn `curl` mẫu đã kiểm thử (Prompt 01)

```bash
# 1. Tạo guest session
SESSION_RESP=$(curl -s -X POST http://127.0.0.1:8000/api/v1/demo/sessions \
  -H "Content-Type: application/json" \
  -d '{"ttl_minutes": 60, "max_cases": 20, "seed_fixtures": true}')
TOKEN=$(echo $SESSION_RESP | grep -o '"token":"[^"]*' | cut -d'"' -f4)

# 2. Tiếp nhận khiếu nại tiếng Việt (CLAIM) kèm khóa chống trùng lặp
CASE_RESP=$(curl -s -X POST http://127.0.0.1:8000/api/v1/cases \
  -H "Content-Type: application/json; charset=utf-8" \
  -H "X-Demo-Session: $TOKEN" \
  -H "X-Idempotency-Key: client-req-001" \
  -d '{"text": "Khách hàng thông báo đã chuyển khoản MoMo 250.000đ mã MOMO_TXN_888 nhưng đơn chưa thanh toán", "type": "PAYMENT_DISPUTE"}')
CASE_ID=$(echo $CASE_RESP | grep -o '"id":"[^"]*' | cut -d'"' -f4)

# 3. Đọc chi tiết Case
curl -s http://127.0.0.1:8000/api/v1/cases/$CASE_ID \
  -H "X-Demo-Session: $TOKEN"

# 4. Đọc dòng thời gian sự kiện (Audit trail)
curl -s http://127.0.0.1:8000/api/v1/cases/$CASE_ID/events \
  -H "X-Demo-Session: $TOKEN"

# 5. Đọc trạng thái nhẹ
curl -s http://127.0.0.1:8000/api/v1/cases/$CASE_ID/status \
  -H "X-Demo-Session: $TOKEN"
```

## Hạn chế đã biết

- Trên máy dựng bootstrap không có Docker nên `docker compose config/up` chưa được chạy; Dockerfile và compose file đã được kiểm tra cú pháp và cấu trúc tĩnh.
- Prompt 01 đã hoàn thành đầy đủ: Schema, Migrations, Sessions, Case Intake, State Machine, Audit Log, Idempotency, Quota và Seed Fixtures.
- Chưa có Bounded Planner / ModelAdapter (thuộc Prompt 02: AI Core).
- Giao diện Web hiện đang ở dạng khung (sẽ kết nối API ở Prompt 07).

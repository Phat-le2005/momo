# BÁO CÁO KỸ THUẬT VÀ TỔNG QUAN HỆ THỐNG
# DỰ ÁN: FINRESOLVE AI (v1.2 MVP)

> **Thông điệp cốt lõi:** *"From anomalous business events to evidence-grounded, verified resolution."*  
> **Phiên bản tài liệu:** 1.2  
> **Ngày cập nhật:** 22/11/2026  
> **Mục tiêu:** Cung cấp tài liệu kỹ thuật tổng quan, kiến trúc mô hình, hướng dẫn triển khai thực tế và lộ trình nâng cấp hệ thống.

---

## MỤC LỤC
1. [Tổng quan dự án & Bối cảnh bài toán](#1-tổng-quan-dự-án--bối-cảnh-bài-toán)
2. [Triết lý thiết kế & Các bất biến an toàn (Safety Invariants)](#2-triết-lý-thiết-kế--các-bất-biến-an-toàn-safety-invariants)
3. [Mô hình kiến trúc hệ thống & Luồng xử lý](#3-mô-hình-kiến-trúc-hệ-thống--luồng-xử-lý)
4. [Mô tả chi tiết các phân hệ cốt lõi](#4-mô-tả-chi-tiết-các-phân-hệ-cốt-lõi)
5. [Hướng dẫn cài đặt & Triển khai (Deployment Guide)](#5-hướng-dẫn-cài-đặt--triển-khai-deployment-guide)
6. [Hiện trạng tiến độ & Bộ kiểm thử đã hoàn thành](#6-hiện-trạng-tiến-độ--bộ-kiểm-thử-đã-hoàn-thành)
7. [Lộ trình chuyển đổi từ MOCK sang PRODUCTION thực tế](#7-lộ-trình-chuyển-đổi-từ-mock-sang-production-thực-tế)

---

## 1. TỔNG QUAN DỰ ÁN & BỐI CẢNH BÀI TOÁN

### 1.1. FinResolve AI là gì?
**FinResolve AI** là nền tảng **AI-Native Agentic AI** chuyên biệt phục vụ việc **điều tra ngoại lệ vận hành và kiểm soát rủi ro trong Thanh toán & Thương mại điện tử (Payment & Commerce)**. 

Hệ thống được thiết kế nhằm tự động hóa quy trình tiếp nhận khiếu nại của khách hàng (bằng tiếng Việt tự nhiên hoặc tài liệu), chủ động điều tra dữ liệu đa nguồn (Cổng thanh toán, Hệ thống đơn hàng OMS, Kho bãi WMS), đánh giá rủi ro, lập đề xuất hành động qua lớp chính sách an toàn, và **bắt buộc xác minh độc lập** trước khi hoàn tất vụ việc.

### 1.2. Bối cảnh & Bài toán doanh nghiệp
Trong thương mại điện tử hiện đại, đặc biệt là các phiên bán hàng dồn dập (**Mega Live**) hoặc mô hình bán hàng đa kênh (Omnichannel):
* **Sự cố thông tin (Information Failure):** Webhook thanh toán bị rớt, độ trễ đồng bộ mạng, ngân hàng báo trừ tiền nhưng đơn hàng trên hệ thống vẫn ở trạng thái "Chờ thanh toán".
* **Sự cố quyết định (Decision Failure):** Nhân viên hỗ trợ (CSKH) dễ bị quá tải, thao tác nhầm hoặc tin theo ảnh chụp biên lai giả mạo của người mua, dẫn đến việc duyệt hoàn tiền trùng lặp (Double Refund) hoặc vượt quá số tiền khách thực trả.
* **Sự cố thực thi (Execution Failure):** Cổng thanh toán phản hồi timeout (không rõ thành công hay thất bại), hệ thống tự ý gọi lệnh ghi lại gây trừ tiền/hoàn tiền nhiều lần.

---

## 2. TRIẾT LÝ THIẾT KẾ & CÁC BẤT BIẾN AN TOÀN (SAFETY INVARIANTS)

Hệ thống tuân thủ nghiêm ngặt các nguyên tắc thiết kế phòng vệ tài chính:

> **LLM là công cụ suy luận ngôn ngữ, KHÔNG PHẢI nguồn sự thật (Source of Truth).**  
> AI được sử dụng để hiểu tiếng Việt, trích xuất thực thể và đề xuất hướng xử lý. Toàn bộ logic kiểm tra số dư, đối soát giao dịch, cấp quyền phê duyệt và ghi dữ liệu do code xác định (Deterministic Code) và CSDL SQL đảm nhiệm.

1. **CLAIM vs OBSERVED_FACT:** Mọi tin nhắn, khiếu nại, ảnh hóa đơn của khách chỉ được coi là **lời khai chưa kiểm chứng (`CLAIM`)**. Hệ thống không bao giờ tự ý nâng cấp một lời khai thành sự thật thanh toán (`OBSERVED_FACT`) nếu chưa đối soát thành công với bản ghi kế toán từ Payment Provider.
2. **Verified Resolution (Không có kết luận mù quáng):** Trạng thái Case không thể chuyển sang `RESOLVED` nếu thiếu bản ghi `Verification` đạt kết quả `PASSED` (được đọc lại độc lập từ CSDL).
3. **Idempotency & Concurrency Safety:** Mọi thao tác ghi nhạy cảm đều hỗ trợ khóa chống trùng lặp (`X-Idempotency-Key`), kiểm tra khóa lạc quan (`version += 1`) và khóa bi quan (`SELECT FOR UPDATE`) tại thời điểm ghi.
4. **An toàn hoàn tiền:** Số tiền hoàn khả dụng luôn được tính theo công thức:  
   $$\text{Số tiền được hoàn} = \text{Số tiền hợp lệ} - \text{Đã hoàn} - \text{Đang chờ xử lý}$$  
   Triệt tiêu hoàn toàn nguy cơ số dư hoàn tiền bị âm.
5. **Môi trường giả lập (Synthetic MOCK):** MVP sử dụng dữ liệu mô phỏng an toàn, không gọi API tiền thật, không liên kết tài khoản ngân hàng thật khi chưa có ủy quyền pháp lý.

---

## 3. MÔ HÌNH KIẾN TRÚC HỆ THỐNG & LUỒNG XỬ LÝ

### 3.1. Sơ đồ kiến trúc tổng thể

```mermaid
flowchart TD
    subgraph ClientLayer ["1. TẦNG TRUY CẬP (Client Layer)"]
        WebUI["Web Application (Next.js 16 + Tailwind 4)"]
        JudgeUser["Người dùng / Giám khảo Demo (Guest Sandbox)"]
    end

    subgraph GatewayLayer ["2. TẦNG CỔNG & PHIÊN (Gateway & Session)"]
        SessionMgr["Demo Session Manager (Token: dsk_..., Quota, TTL)"]
        IdempFilter["Idempotency Filter (X-Idempotency-Key)"]
        ApiRouter["FastAPI REST Router (/api/v1)"]
    end

    subgraph AICore ["3. BỘ NÃO AI (AI Core & Bounded Planner)"]
        NLP["Vietnamese Understanding & Anti-Injection Guard"]
        Adapter["ModelAdapter (LLM API hoặc Fallback DEMO_RULES)"]
        Planner["Bounded Planner (Max Steps, Budget Tokens)"]
    end

    subgraph Connectors ["4. CÔNG CỤ ĐỌC AN TOÀN (Safe Read-Only Connectors)"]
        ToolOrder["get_order"]
        ToolPayment["find_payment_candidates"]
        ToolBalance["get_customer_balance"]
        ToolStock["check_inventory_levels"]
    end

    subgraph PolicyRisk ["5. CHÍNH SÁCH & RỦI RO (Deterministic Safety Gates)"]
        RiskEngine["Risk Engine (Anti-Duplicate, Balance Guard, Anomaly)"]
        PolicyEngine["Policy Engine (Preconditions, Role Permissions)"]
        ApprovalMgr["HMAC Signed Approval Manager (Tokens: Role-based)"]
    end

    subgraph ExecutionLayer ["6. THỰC THI & XÁC MINH (Execution & Verification)"]
        StateMachine["Case State Machine (Optimistic Locking)"]
        Executor["Safe Action Executor (Pessimistic Locks SELECT FOR UPDATE)"]
        Verifier["Durable Read-Back Verifier (Tách rời kết nối DB độc lập)"]
        AuditLog["Append-only Audit Trail (CaseEvents)"]
    end

    subgraph Storage ["7. CƠ SỞ DỮ LIỆU (PostgreSQL 16)"]
        DB[(PostgreSQL Domain & Simulator Tables)]
    end

    JudgeUser --> WebUI
    WebUI --> SessionMgr
    SessionMgr --> IdempFilter --> ApiRouter
    ApiRouter --> NLP --> Adapter --> Planner
    Planner <--> Connectors
    Connectors <--> DB
    Planner --> PolicyEngine
    PolicyEngine --> RiskEngine
    PolicyEngine --> ApprovalMgr
    ApprovalMgr --> Executor
    Executor --> StateMachine
    Executor --> DB
    Executor --> Verifier
    Verifier <--> DB
    Verifier --> StateMachine
    StateMachine --> AuditLog
    AuditLog --> DB
```

---

### 3.2. Sơ đồ chuỗi điều tra và giải quyết một vụ việc (Sequence Diagram)

```mermaid
sequenceDiagram
    autonumber
    actor Customer as Khách hàng / Khiếu nại
    participant API as API Gateway (/cases)
    participant AI as AI Core & Bounded Planner
    participant Tools as Read-Only Connectors
    participant Policy as Policy & Risk Engine
    participant Exec as Action Executor
    participant DB as PostgreSQL Ledger
    participant Verifier as Post-Commit Verifier

    Customer->>API: Gửi khiếu nại tiếng Việt (Ví dụ: "Đã chuyển MoMo 250k nhưng đơn treo")
    API->>DB: Tạo Case (State: NEW, lưu text dưới dạng CLAIM)
    API->>AI: Kích hoạt pipeline điều tra (run-pipeline)
    AI->>AI: Bóc tách thực thể (Mã đơn, Mã GD, Số tiền) + Chặn Prompt Injection
    AI->>Tools: Gọi find_payment_candidates(order_id)
    Tools->>DB: Đọc giao dịch (Không thay đổi dữ liệu)
    DB-->>Tools: Trả về trạng thái giao dịch: SUCCESS, 250.000đ
    Tools-->>AI: Bằng chứng OBSERVED_FACT
    AI->>Policy: Đề xuất hành động SYNC_PAYMENT_STATUS
    Policy->>Policy: Kiểm tra tiền điều kiện (Preconditions) & Rủi ro
    alt Cần duyệt phân quyền (ví dụ: Hoàn tiền)
        Policy-->>API: Yêu cầu phê duyệt (WAITING_APPROVAL) kèm HMAC Token
        API->>Policy: Gửi token phê duyệt hợp lệ
    end
    Policy->>Exec: Ủy quyền thực thi (EXECUTING)
    Exec->>DB: Khóa hàng SELECT FOR UPDATE, cập nhật đơn hàng sang PAID
    Exec->>Verifier: Kích hoạt xác minh độc lập
    Verifier->>DB: Mở phiên DB mới, đọc lại trực tiếp trạng thái đơn hàng
    alt Đơn hàng thực tế đã chuyển sang PAID
        Verifier-->>API: Kết quả PASSED ➔ Chuyển Case sang RESOLVED
    else Sai lệch hoặc Timeout
        Verifier-->>API: Kết quả FAILED ➔ Chuyển Case sang UNKNOWN_OUTCOME
    end
```

---

### 3.3. Đồ thị máy trạng thái vụ việc (State Machine)

```mermaid
stateDiagram-v2
    [*] --> NEW : Tiếp nhận khiếu nại (CLAIM)
    NEW --> INVESTIGATING : AI bắt đầu điều tra bằng chứng
    INVESTIGATING --> WAITING_EVIDENCE : Dữ liệu mơ hồ / Thiếu mã giao dịch
    INVESTIGATING --> WAITING_APPROVAL : Đề xuất hành động nhạy cảm (Cần duyệt)
    INVESTIGATING --> EXECUTING : Hành động rủi ro thấp (Tự động thực thi)
    WAITING_EVIDENCE --> INVESTIGATING : Bổ sung thêm bằng chứng mới
    WAITING_APPROVAL --> EXECUTING : Nhận được chữ ký phê duyệt hợp lệ
    WAITING_APPROVAL --> REJECTED : Người quản trị từ chối
    EXECUTING --> VERIFYING : Ghi nhận lệnh vào CSDL thành công
    VERIFYING --> RESOLVED : Verifier xác minh hậu điều kiện PASSED
    VERIFYING --> UNKNOWN_OUTCOME : Xảy ra Timeout / Mất kết nối
    RESOLVED --> [*]
    REJECTED --> [*]
    UNKNOWN_OUTCOME --> [*]
```

---

## 4. MÔ TẢ CHI TIẾT CÁC PHÂN HỆ CỐT LÕI

| Phân hệ | Thư mục mã nguồn | Chức năng chi tiết |
| :--- | :--- | :--- |
| **Phiên Demo & Cô lập** | `app/core/session.py` | Quản lý vòng đời Guest Session, sinh token `dsk_...`, giới hạn quota số case, ngăn chặn truy cập trái phép chéo giữa các phiên. |
| **Hiểu ngôn ngữ tự nhiên** | `app/ai_core/understanding.py` | Sử dụng biểu thức chính quy (Regex) và luật ngôn ngữ tối ưu bóc tách mã đơn `ORD-...`, mã MoMo `MOMO_TXN_...`, số tiền VND (`250k`, `250.000đ`). Phát hiện và vô hiệu hóa các câu lệnh Prompt Injection. |
| **Bộ chuyển đổi mô hình (Adapter)** | `app/ai_core/model_adapter.py` | Trừu tượng hóa việc giao tiếp với LLM API (timeout, token budget). Nếu không cấu hình API Key, tự động chuyển sang chế độ `DEMO_RULES` xác định rõ ràng, không giả mạo kết quả AI. |
| **Bounded Planner** | `app/ai_core/runtime.py` | Điều phối AI suy luận trong giới hạn bước (`max_steps`), tuần tự gọi các công cụ đọc an toàn và tạo đề xuất có cấu trúc JSON Schema. |
| **Động cơ Chính sách** | `app/policy/engine.py` | Kiểm tra tính hợp lệ của đề xuất: đơn có tồn tại không, trạng thái có đúng tiền điều kiện không, người thực hiện có quyền hạn tương ứng hay không. |
| **Quản lý Phê duyệt** | `app/policy/approval.py` | Ký và xác thực chữ ký HMAC đa yếu tố theo từng vai trò (`SUPPORT`, `OPS`, `FINANCE`, `MANAGER`), ngăn chặn việc dùng lại token cũ hoặc sửa tham số đề xuất. |
| **Động cơ Rủi ro** | `app/risk/engine.py` | Kiểm soát an toàn hoàn tiền: chống duplicate refund, phát hiện tái sử dụng mã giao dịch cũ, kiểm tra hạn mức còn lại không cho phép âm. |
| **Thực thi & Xác minh** | `app/policy/executor.py` | Thực thi an toàn với giao dịch CSDL ACID, khóa bi quan `SELECT FOR UPDATE`, và xác minh 2 pha độc lập (Two-Phase Read-Back Verification). |
| **Giao diện người dùng** | `apps/web/` | Nền tảng Next.js 16 App Router, Tailwind CSS v4, cung cấp giao diện `/demo`, `/cases`, `/risks`, `/live`. |

---

## 5. HƯỚNG DẪN CÀI ĐẶT & TRIỂN KHAI (DEPLOYMENT GUIDE)

### 5.1. Yêu cầu môi trường
* **Python:** 3.12+ (đã có môi trường ảo tại `services/api/.venv`)
* **Node.js:** 20.9+ hoặc 22+ (đã cài `node_modules` tại `apps/web/`)
* **Hệ cơ sở dữ liệu:** PostgreSQL 16+ hoặc Docker Desktop.

---

### 5.2. Cách 1: Triển khai toàn bộ bằng Docker Compose (Khuyên dùng)
Đây là cách đóng gói chuẩn, tự động dựng cả Cơ sở dữ liệu, Backend và Frontend trong các container riêng biệt:

```bash
# 1. Di chuyển vào thư mục dự án
cd D:/momo/FinResolve

# 2. Tạo file cấu hình từ mẫu (nếu chưa có)
cp infra/.env.example infra/.env

# 3. Khởi chạy toàn bộ hệ thống
docker compose -f infra/docker-compose.yml up --build
```

* **Frontend Web:** `http://localhost:3000/demo`
* **API Backend:** `http://localhost:8000/healthz`
* **Tài liệu OpenAPI UI:** `http://localhost:8000/docs`
* **Cơ sở dữ liệu PostgreSQL:** Ánh xạ ra cổng host **5433** (user: `finresolve`, pass: `change-me-local-only`, database: `finresolve`).

---

### 5.3. Cách 2: Triển khai trực tiếp (Local Development trên Windows)

#### Bước 1: Khởi động cơ sở dữ liệu PostgreSQL
Đảm bảo PostgreSQL đang chạy trên máy (mặc định cổng `5432`). Tạo tài khoản và CSDL:
```sql
CREATE ROLE finresolve LOGIN PASSWORD 'change-me-local-only';
CREATE DATABASE finresolve OWNER finresolve;
CREATE DATABASE finresolve_test OWNER finresolve;
```

#### Bước 2: Cấu hình biến môi trường
Kiểm tra file `services/api/.env`:
```ini
APP_ENV=local
DATABASE_URL=postgresql+psycopg://finresolve:change-me-local-only@127.0.0.1:5432/finresolve
WEB_ORIGIN=http://localhost:3000
SOURCE_MODE=MOCK
DEMO_APPROVAL_SECRET=finresolve_super_secret_local_hmac_key_v1
```

Kiểm tra file `apps/web/.env.local`:
```ini
NEXT_PUBLIC_API_URL=http://localhost:8000
```

#### Bước 3: Chạy Database Migration
```powershell
cd D:\momo\FinResolve\services\api
.venv\Scripts\alembic upgrade head
```

#### Bước 4: Chạy Backend API (Cổng 8000)
```powershell
cd D:\momo\FinResolve\services\api
.venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

#### Bước 5: Chạy Frontend Web (Cổng 3000 - Terminal khác)
```powershell
cd D:\momo\FinResolve\apps\web
npm run dev
```
Truy cập trình duyệt tại: `http://localhost:3000/demo`.

---

### 5.4. Chạy kiểm thử tự động (Quality & Test Suite)
Để chạy toàn bộ kiểm tra cú pháp, typing và unit test:
```powershell
# Chạy bằng script tích hợp
cd D:\momo\FinResolve
.\scripts\dev.ps1 check

# Hoặc chỉ chạy kiểm thử backend pytest:
cd D:\momo\FinResolve\services\api
$env:DATABASE_URL="postgresql+psycopg://finresolve:change-me-local-only@127.0.0.1:5432/finresolve"
.venv\Scripts\pytest -v
```

---

## 6. HIỆN TRẠNG TIẾN ĐỘ & BỘ KIỂM THỬ ĐÃ HOÀN THÀNH

Theo lộ trình phát triển đến ngày **03/12/2026** (`docs/DELIVERY_PLAN.md`), tiến độ các Prompt như sau:

| Mốc triển khai | Nội dung kỹ thuật | Tình trạng | Commit tương ứng |
| :--- | :--- | :---: | :--- |
| **Prompt 00** | Khung sườn kiến trúc, Docker Compose, FastAPI, Next.js, Health checks | ✅ Đạt | `4139c06 triển khai db` |
| **Prompt 01** | CSDL Schema, Session Token `dsk_...`, Idempotency, Quota, Cases API, Audit Log | ✅ Đạt | `1869edf triển khai db` |
| **Prompt 02** | AI Core, Model Adapter, Hiểu tiếng Việt, Bounded Planner, Read Tools | ✅ Đạt | `b040a2d Base AI core` |
| **Prompt 03** | Policy Engine, Chữ ký số HMAC Approval, Safe Executor, 2-Phase Verification | ✅ Đạt | `fc3b682 base policy` |
| **Prompt 04** | Risk Center, An toàn hoàn tiền (Refund Safety), Chống duplicate, Balance check | ✅ Đạt | `34effa9 base Risk` |
| **Prompt 05** | **Incident Taskforce:** Gom nhóm sự cố chung & điều phối vai trò Finance/Ops | ⏳ **Kế tiếp** | Chuẩn bị triển khai |
| **Prompt 06** | **Mega Live Simulator:** Trình sinh dữ liệu 50–100 đơn đồng thời | ⏳ Chờ | Sau Prompt 05 |
| **Prompt 07** | **Frontend Integration:** Nối toàn bộ API hoàn chỉnh lên giao diện Web | ⏳ Chờ | Sau Prompt 06 |
| **Prompt 08** | E2E Security & Regression Testing theo QA Matrix | ⏳ Chờ | Sau Prompt 07 |
| **Prompt 09** | Đóng gói phát hành, Freeze code và nộp bài | ⏳ Chờ | Trước 03/12/2026 |

---

## 7. LỘ TRÌNH CHUYỂN ĐỔI TỪ MOCK SANG PRODUCTION THỰC TẾ

Khi đưa hệ thống vào thương mại hóa hoặc đấu nối trực tiếp với đối tác tài chính thật, cần thực hiện các thay đổi kỹ thuật sau:

```mermaid
flowchart LR
    subgraph MockCurrent ["Hiện tại (MOCK)"]
        S1["sim_payments / sim_orders"]
        S2["Đồng bộ qua FastAPI Thread"]
        S3["Token Demo tĩnh"]
        S4["Gọi trực tiếp LLM API"]
    end

    subgraph ProdTarget ["Thực tế (PRODUCTION)"]
        T1["MoMo Open API Gateway (Query / Refund)"]
        T2["Async Queue (Celery / Kafka / RabbitMQ)"]
        T3["Enterprise IAM (OAuth2 / OIDC / SSO)"]
        T4["Tiered AI Pipeline (Rules -> SLM -> LLM)"]
    end

    S1 ==>|Đấu nối API đối tác| T1
    S2 ==>|Chuyển kiến trúc nền| T2
    S3 ==>|Bảo mật phân quyền| T3
    S4 ==>|Tối ưu chi phí & độ trễ| T4
```

1. **Đấu nối MoMo Open API Gateway thật:**
   * Thay thế `app/connectors/read_tools.py` bằng lời gọi API `POST /v2/gateway/api/query` có ký số HMAC-SHA256 với cặp khóa bí mật (`secretKey`, `accessKey`).
   * Thay thế lệnh hoàn tiền mock trong `app/policy/executor.py` bằng API `POST /v2/gateway/api/refund`, xử lý chặt chẽ phản hồi bất đồng bộ (mã 9000, 1000) và các tình huống Timeout.
   * Xây dựng Endpoint nhận Webhook (IPN - Instant Payment Notification) có xác thực chữ ký để cập nhật tức thời khi giao dịch biến động.
2. **Đấu nối hệ thống OMS / ERP / Sàn TMĐT:**
   * Viết các Adapter chuẩn hóa kết nối với TikTok Shop, Shopee, Sapo, KiotViet, Haravan, Shopify để lấy thông tin đơn hàng và tồn kho thật.
   * Giới hạn phạm vi quyền (API Scopes): FinResolve chỉ có quyền READ dữ liệu và chỉ GHI trạng thái cập nhật thanh toán hoặc tạm giữ đơn theo chính sách được ký kết.
3. **Kiến trúc Bất đồng bộ (Async Event-Driven Architecture):**
   * Đưa vào **Redis / RabbitMQ / Kafka** kết hợp worker **Celery / ARQ / Temporal**.
   * Khi khách gửi khiếu nại, API tiếp nhận trả về ngay trong 100ms; việc điều tra AI, đối soát MoMo chạy ngầm trong nền và đẩy kết quả tức thì lên Web thông qua **Server-Sent Events (SSE)** hoặc **WebSockets**.
4. **Bảo mật & Định danh Doanh nghiệp (Enterprise IAM / RBAC / SSO):**
   * Thay thế token demo bằng chuẩn **OAuth2 / OIDC** liên kết tài khoản nội bộ (Google Workspace, Microsoft Entra ID).
   * Phân quyền ma trận vai trò (RBAC):
     * *CSKH (Support):* Chỉ được tra cứu và tạo đề xuất.
     * *Vận hành (Ops Lead):* Được duyệt đồng bộ trạng thái đơn hàng.
     * *Kế toán (Finance Manager):* Duyệt hoàn tiền dưới hạn mức (ví dụ < 5.000.000đ).
     * *Giám đốc tài chính (CFO):* Duyệt các khoản hoàn tiền lớn hoặc ca nghi ngờ rủi ro.
5. **Mô hình AI đa tầng tối ưu chi phí (Tiered AI Pipeline):**
   * *Tầng 1 (Rule-based Fast Path - 50ms, chi phí 0đ):* Tự động giải quyết 70% các ca đơn giản (có mã giao dịch rõ ràng, đối soát khớp hoàn toàn với MoMo).
   * *Tầng 2 (Small Language Model - SLM cục bộ):* Sửa lỗi chính tả tiếng Việt, trích xuất thực thể.
   * *Tầng 3 (Large Multimodal LLM):* Chỉ gọi khi có mâu thuẫn phức tạp, khách hàng gửi ảnh biên lai cần OCR phân tích giả mạo, hoặc tình huống rủi ro gian lận bất thường.

---
*Tài liệu được trích xuất tự động và lưu trữ tại thư mục dự án.*

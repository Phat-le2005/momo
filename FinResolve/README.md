# FinResolve AI

**FinResolve AI** là hệ thống ứng dụng AI giúp tiếp nhận, điều tra và xử lý các sự cố thanh toán – đơn hàng trong thương mại điện tử (như khách đã chuyển tiền nhưng đơn chưa cập nhật, yêu cầu hoàn tiền trùng lặp, hay nghẽn đồng bộ đơn hàng khi có lượng truy cập lớn).

Thay vì để AI tự ý sửa dữ liệu hay quyết định cảm tính, FinResolve AI kết hợp **AI điều tra bằng chứng** với **cơ chế kiểm tra chính sách, phê duyệt và xác minh kết quả độc lập**, giúp xử lý sự cố nhanh hơn mà vẫn đảm bảo an toàn tài chính.

---

## Dự án dùng để làm gì?

Trong thực tế bán hàng online (đặc biệt các đợt Flash Sale / Mega Live), đội vận hành thường gặp các tình huống:

- Khách báo đã thanh toán qua ví điện tử nhưng hệ thống đơn hàng vẫn báo **chờ thanh toán**.
- Khách gửi nhiều yêu cầu **hoàn tiền lặp** hoặc vượt quá số tiền có thể hoàn.
- Lỗi kỹ thuật làm **hàng loạt đơn hàng cùng bị treo đồng bộ** trong cùng một khung giờ.
- Lượng đơn dồn về cùng lúc gây **tranh chấp tồn kho** cho sản phẩm hot.

**FinResolve AI** được xây dựng để:
- Hỗ trợ đội **Chăm sóc khách hàng, Vận hành và Tài chính/Đối soát** rút ngắn thời gian tra cứu và xử lý khiếu nại.
- Làm **môi trường trải nghiệm trực tiếp (Self-Serve Demo)** cho người dùng thử quy trình xử lý sự cố thanh toán có AI hỗ trợ mà không cần đăng ký tài khoản hay kết nối tiền thật.

---

## Các chức năng nổi bật

### 1. Trung tâm Điều hành & Thử nghiệm Tự hành — AI Mission Control (`/demo`)
- **AI Supervisor tự động giám sát theo sự kiện (Event-Driven Autopilot)**: Tự động phát hiện khiếu nại, hóa đơn đáng ngờ, yêu cầu hoàn tiền trùng hoặc cụm đơn hàng nghẽn đồng bộ để điều tra ngay mà không cần người dùng bấm nút điều tra thủ công cho từng hồ sơ. Khi hệ thống rảnh, AI không gọi LLM lặp vô hạn và không tiêu tốn token.
- **Phòng thử nghiệm tình huống đối kháng (Challenge Lab)**: Cho phép tạo nhanh các tình huống kiểm thử chỉ với 1–2 thao tác:
  - **Hóa đơn đáng ngờ**: Biên lai báo thành công nhưng không có trong sổ cái, sửa số tiền trên hóa đơn, dùng biên lai của đơn hàng khác hoặc giao dịch đã hoàn tiền.
  - **Request tăng đột biến**: Mô phỏng đợt lưu lượng tăng cao trong giới hạn an toàn, đo lường số request được nhận, bị chặn trùng lặp hoặc bị giới hạn tốc độ (Rate Limit) và để AI phân tích số liệu tổng hợp.
  - **Hoàn tiền trùng**: Phân biệt giữa yêu cầu hoàn tiền lặp/vượt số dư bị chặn (`HOLD_ACTION`) và yêu cầu hoàn tiền một phần hợp lệ.
  - **Nghẽn đơn hàng**: Mô phỏng cụm đơn hàng đã thanh toán nhưng lỗi đồng bộ tại cùng một worker, tự động gom `Incident`, kích hoạt `Taskforce` và đưa đề xuất vào hộp thư chờ duyệt.
  - **Tấn công Prompt Injection**: Kiểm thử khả năng kháng các câu lệnh giả mạo quyền hệ thống trong nội dung khiếu nại.
- **Dòng thời gian hoạt động Agent & Hộp thư phê duyệt của con người**: Hiển thị trực tiếp các công cụ đọc dữ liệu AI vừa gọi, bằng chứng thu thập được và danh sách các đề xuất (`ActionProposal`) đang chờ con người phê duyệt trước khi thực thi và kiểm chứng hậu điều kiện (`Verifier`).

### 2. Trung tâm xử lý hồ sơ — Case Center (`/cases`)
- **Điều tra dựa trên bằng chứng**: Phân tách rõ giữa *lời khai của khách hàng (`CLAIM`)*, *dữ liệu đối chiếu thực tế từ hệ thống (`OBSERVED_FACT`)* và *các điểm chưa rõ (`UNKNOWN`)*.
- **AI chỉ đề xuất, không tự ý ghi nhận giao dịch**: Sau khi tra cứu dữ liệu, hệ thống chỉ tạo đề xuất hành động (`ActionProposal`) kèm lý do và dấu vết từng bước (`Decision Trace`).
- **Kiểm soát an toàn nhiều lớp**: Mọi thao tác thay đổi trạng thái đơn hàng hoặc hoàn tiền đều phải đi qua kiểm tra chính sách (**Policy**), bước **Phê duyệt** có xác nhận rõ ràng, và **Kiểm chứng độc lập (Verifier)** đọc lại dữ liệu sau khi thực thi trước khi đóng hồ sơ (`RESOLVED`).

### 3. Trung tâm quản lý rủi ro — Risk Center (`/risks`)
- Tự động phát hiện các dấu hiệu rủi ro khi hoàn tiền như hoàn trùng, vượt hạn mức còn lại hoặc đang có giao dịch hoàn tiền chờ xử lý.
- Phân loại mức phản hồi rõ ràng (`OBSERVE`, `REVIEW`, `HOLD_ACTION`) kèm giải thích bằng tiếng Việt và dẫn chứng cụ thể, tránh khóa tài khoản hay quy kết gian lận một cách máy móc.

### 4. Giám sát sự cố hàng loạt — Mega Live & Incident Taskforce (`/live`)
- Tự động gom nhóm các hồ sơ bị ảnh hưởng bởi **cùng một nguyên nhân kỹ thuật** thành một Sự cố chung (`Incident`), đồng thời loại trừ các hồ sơ chỉ có nội dung khiếu nại giống nhau nhưng khác nguyên nhân.
- Phân công nhóm vai trò phối hợp (**Finance, Operations, Risk, Support**) để tổng hợp bức tranh sự cố nhanh chóng, nhưng vẫn đảm bảo **mỗi đơn hàng/hồ sơ con phải được kiểm chứng độc lập** trước khi hoàn tất.
- Hiển thị tách biệt kết quả kiểm tra an toàn và tiến độ xử lý thực tế để người vận hành nắm đúng trạng thái hệ thống.

---

## Lưu ý về dữ liệu mô phỏng

Toàn bộ đơn hàng, giao dịch thanh toán, hoàn tiền và tồn kho trong FinResolve AI hiện chạy trên **dữ liệu mô phỏng (`MOCK`)**. Hệ thống không kết nối tài khoản ngân hàng, không thực hiện thanh toán hay hoàn tiền thật.

---

## Khởi chạy nhanh (API + Standalone Supervisor Worker + Frontend)

### 1. Docker Compose (khuyến nghị)
`infra/docker-compose.yml` chạy đầy đủ 4 dịch vụ: `db` (PostgreSQL), `api` (FastAPI + Alembic migrations), `worker` (Standalone Autonomous Supervisor: `python -m app.supervisor.worker`), và `web` (Next.js):

```bash
cp infra/.env.example infra/.env
# Đặt DEMO_APPROVAL_SECRET ngẫu nhiên >= 32 ký tự trong infra/.env
docker compose -f infra/docker-compose.yml up --build
```

### 2. Chạy cục bộ (Linux / macOS / Windows PowerShell)
Mở 3 terminal riêng biệt (giữ `ENABLE_EMBEDDED_SUPERVISOR_WORKER=false` để tránh nhân đôi worker):

```powershell
# Terminal 1 — Backend API (tự chạy Alembic migration trước)
cd FinResolve/backend
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8000

# Terminal 2 — Standalone Autonomous Supervisor Worker
cd FinResolve/backend
python -m app.supervisor.worker

# Terminal 3 — Frontend (Next.js)
cd FinResolve/frontend
npm run dev
```

### 3. FinShop LIVE — Cửa hàng & Live Commerce độc lập (`MiniStore/`)
`infra/docker-compose.yml` cũng tích hợp đầy đủ 4 dịch vụ độc lập của FinShop LIVE (`commerce-db` cổng `5434`, `commerce-api` cổng `8100`, `commerce-worker`, và `commerce-web` cổng `3100`). Chạy cục bộ:

```powershell
# Commerce Backend API (cổng 8100)
cd MiniStore/backend
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8100

# Commerce Background Worker (đồng bộ đơn, hết hạn giữ chỗ, outbox)
cd MiniStore/backend
python -m app.worker

# Commerce Frontend — FinShop LIVE Storefront & Merchant Center (cổng 3100)
cd MiniStore/frontend
npm run dev
```

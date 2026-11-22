# FINRESOLVE AI — PRODUCT OVERVIEW (đọc trước khi code)

**Nguồn:** Tóm lược từ `references/FinResolve_AI_Native_v1.2.docx` (báo cáo tổng quan do chủ dự án cung cấp, cập nhật 22/11/2026). Đọc bản Word hoặc bản chuyển Markdown ở `references/FinResolve_AI_Native_v1.2_fulltext.md` nếu cần chi tiết.

> **Ưu tiên thực thi được thống nhất sau báo cáo v1.2:** đây là cuộc thi **làm sản phẩm**, nên MVP ưu tiên **chạy thực, chính xác, tốc độ phản hồi tốt, dễ dùng, tự demo được**. Không thực hiện nghiên cứu/benchmark nhiều baseline quy mô lớn khi các chức năng cốt lõi chưa hoàn thành. Đây là điều chỉnh ưu tiên triển khai, **không thay đổi tầm nhìn sản phẩm**.

## 1. Sản phẩm là gì?

**FinResolve AI** là một nền tảng **AI-Native Agentic AI cho xử lý ngoại lệ và rủi ro vận hành trong Payment & Commerce**. Hệ thống quan sát các sự kiện kinh doanh trong phạm vi được cấp quyền, tiếp nhận khiếu nại tiếng Việt (và ảnh/tài liệu khi có), chủ động điều tra dữ liệu từ nhiều nguồn, đánh giá rủi ro, đề xuất kế hoạch, phối hợp Agent chuyên môn và **xác minh** kết quả cuối cùng.

**Thông điệp:** *From anomalous business events to evidence-grounded, verified resolution.*

FinResolve không phải công cụ đánh giá gian lận cấp ngân hàng, không thay thế core payment/ERP, không có quyền tự ý hoàn tiền thật hoặc đóng băng tài khoản. Mục tiêu giai đoạn đầu là SME bán hàng trực tuyến/đa kênh và hoạt động livestream có nhiều ngoại lệ.

## 2. Bài toán người dùng

Thanh toán, đơn hàng, kho, voucher, hoàn tiền, CSKH nằm trên nhiều hệ thống khác nhau. Lỗi đồng bộ, webhook mất/trễ/trùng, đơn thiếu mã, hoàn tiền lặp, tồn kho tranh chấp, điều kiện khuyến mãi thay đổi có thể khiến nhân viên phải tra cứu, chuyển phòng ban và ra quyết định thủ công. Trong Mega Live, nhiều sai lệch có thể phát sinh đồng thời.

Ba kiểu thất bại cần xử lý:
1. **Information failure:** dữ liệu thiếu, mâu thuẫn hoặc không đủ tin cậy.
2. **Decision failure:** phương án không phù hợp quyền, chính sách hoặc trạng thái.
3. **Execution failure:** công cụ báo thành công nhưng hậu điều kiện chưa đạt; duplicate, race hoặc timeout.

## 3. AI-Native và Agentic AI trong sản phẩm

- **Business Perception:** nhận business event, claim, ảnh, dấu hiệu bất thường. Rule/code xử lý tín hiệu xác định trước; LLM được dùng khi cần hiểu ngôn ngữ và ngữ cảnh.
- **Active Evidence Investigation:** AI phân tích điều chưa biết, chủ động chọn read tool, hỏi thêm hoặc thay giả thuyết theo bằng chứng mới.
- **Goal-driven / bounded planning:** kế hoạch có JSON schema, mục tiêu và điều kiện dừng; không vòng lặp vô hạn.
- **Adaptive Risk Response:** chuyển tín hiệu thành OBSERVE / REVIEW / HOLD_ACTION (chỉ với hành động thuộc quyền hệ thống).
- **Incident Correlation + Adaptive Taskforce:** liên hệ các Case có bằng chứng chung, điều phối Finance/Risk/Operations/Support dạng vai trò trong một runtime; không tạo agent với quyền mới.
- **Verified Resolution:** mọi action ghi qua policy, precondition, approval, idempotency, read-back; chỉ RESOLVED nếu postcondition được kiểm chứng.

**LLM là công cụ suy luận, không phải nguồn sự thật thanh toán hay bộ máy phân quyền.** SQL, source records, chữ ký, giới hạn refund, tồn kho và transaction do code xử lý.

## 4. Đối tượng nghiệp vụ

- **Case:** một vấn đề riêng, có goal, owner/session, state, evidence, nguồn, trạng thái cuối và điều kiện thành công.
- **Incident:** một nguyên nhân/sự cố có thể ảnh hưởng nhiều Case; điều tra nguyên nhân chung không tự hoàn tất từng Case.
- **Taskforce:** nhóm nhiệm vụ tạm thời của các vai trò chuyên môn được chọn từ registry có quyền sẵn; chấm dứt khi không còn nhiệm vụ nhưng giữ audit.
- **Evidence:** CLAIM (tin/ảnh khách gửi), OBSERVED_FACT (nguồn đã kiểm tra), HYPOTHESIS, UNKNOWN. Không nâng claim thành payment confirmed.

## 5. Ví dụ sản phẩm

**A. Payment exception / shop nhỏ:** khách nói đã trả tiền, đơn chưa cập nhật. AI trích xuất ngữ nghĩa, truy vấn payment record đã xác thực, kiểm tra đơn và kho; đề xuất sync có điều kiện; backend thực thi; Verifier đọc lại. Nếu payment chưa xác minh → WAITING_EVIDENCE.

**B. Refund risk / doanh nghiệp vừa:** các request hoàn tiền gần nhau; code kiểm lịch sử refund, hạn mức, pending/duplicate, approval. AI hiểu lý do, đề xuất bằng chứng cần tìm. Trường hợp mơ hồ → REVIEW, không kết luận gian lận.

**C. Mega Live / nhiều Case:** một phiên bán hàng mô phỏng 50–100 đơn cho MVP, phát sinh sự kiện mất/trễ/trùng, vấn đề voucher và stock; AI nhóm Case có liên hệ thật thành Incident; Taskforce điều tra nguyên nhân chung, từng Case vẫn phải được verify riêng.

## 6. Phân hệ dự kiến

- **Web UI:** AI Case Center, Risk Center, Mega Live / Self-Serve Demo; timeline Evidence → AI Plan → Tool → Policy → Verification.
- **Event & Intake Gateway:** webhook, complaint, file, validation, dedup, session scope.
- **Evidence + Context Engine:** dữ kiện và provenance; không gửi cả database vào prompt.
- **LLM Model Adapter + Bounded Planner:** xử lý tiếng Việt, phân tích ảnh nếu có, lựa chọn read tool và proposal JSON.
- **Agent Runtime + Taskforce:** tự xây state machine/orchestrator và giao việc.
- **Risk Signals / Policy / Approval:** deterministic safety gates.
- **MCP / HTTP Tool Gateway:** kết nối typed business tools và adapters (đội tự định nghĩa hợp đồng nghiệp vụ).
- **Action Executor + Verification:** transaction, concurrency control, idempotency, postcondition.
- **Mega Live Simulator:** world state giả lập, observable API tách oracle, seeded scenarios; không được dựng dashboard giả.

## 7. MVP Product-first: thứ tự ưu tiên đã chốt

### P0 — phải chạy trước khi mở rộng
- One-click guest online sandbox, không đòi đăng nhập, API credentials hay setup của giám khảo.
- **Một Case E2E thực**: intake → evidence → AI read/plan → tool → policy → action → verification → timeline.
- Hai kịch bản cực kỳ rõ: payment-sync hợp lệ và refund-risk bị chặn/đòi xét duyệt; 3 scenario buttons có thể gồm Mega Live bản giản lược.
- Một AI model API có `ModelAdapter`, JSON schema và timeout; không có key thì fallback demo được gắn nhãn rõ `DEMO_RULES`, không giả vờ generative AI.
- Kiểm thử an toàn: no fake paid, no double refund, no stale approval, no false RESOLVED, session isolation.
- Trang demo công khai có trạng thái chạy/tải/error và data reset.

### P1 — chỉ sau khi P0 ổn định
- Adaptive Incident Taskforce 2–3 vai trò và Incident Correlation đơn giản có bằng chứng chung.
- Mega Live 50–100 đơn, sự cố chung và ca giống bề ngoài nhưng khác nguyên nhân.
- Vision OCR ảnh và MoMo payment sandbox có quyền tích hợp: **không để chặn tiến độ**.

### P2 — sau cuộc thi
- SaaS multi-tenant đầy đủ, nhiều ERP connector, fraud ML riêng, real-money refund/production, model local và platform quy mô lớn.

## 8. Stack và giới hạn

- **Frontend:** Next.js, TypeScript, Tailwind.
- **Backend:** Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy/Alembic, PostgreSQL.
- **LLM:** API được cấp quyền, thay được qua `ModelAdapter`; không hard-code một phiên bản model chưa xác minh.
- **Tools:** HTTPX; MCP SDK chỉ giao thức khi cần; logic Agent Core/Planner/Taskforce/Policy/Verifier **tự viết**.
- **Async:** background DB worker đơn giản; Redis chỉ khi có nhu cầu. Docker Compose local và hosting ổn định cho demo.

## 9. Quy tắc an toàn bất biến

1. Screenshot, redirect, customer message và model output **không chứng minh đã nhận tiền**.
2. Không gọi API tiền thật hoặc refund production trong demo; phân biệt rõ `MOCK` và `MOMO_SANDBOX`.
3. Refund tính cả tiền đã hoàn và đang chờ, chống duplicate/over-limit, cấp quyền khi phê duyệt.
4. Timeout từ payment provider là UNKNOWN: không retry lệnh ghi mù quáng.
5. Policy kiểm tra ngay tại thời điểm commit; action trên state cũ hoặc quá quyền phải bị từ chối.
6. Incident khắc phục không kéo theo Case con RESOLVED nếu chưa được verify.
7. Risk signal không đồng nghĩa gian lận; tránh cáo buộc, block nhầm, tạm giữ ngoài quyền.
8. Mỗi phiên Guest cô lập, giới hạn quota/upload; dữ liệu synthetic, không secrets.

## 10. Hướng dẫn coding agent

Tham khảo `docs/PRODUCT_CONTRACT.md` cho schema/API đã đóng băng, `docs/DELIVERY_PLAN.md` cho mốc triển khai, `docs/QA_MATRIX.md` cho ca kiểm thử. Tài liệu báo cáo gốc ở `references/`. Nếu có khác biệt giữa báo cáo định hướng dài hạn và **P0/P1 Product-first**, hãy thực hiện P0/P1 và giải thích quyết định; không tự mở scope.

**Mục tiêu đến ngày 03/12/2026:** một sản phẩm vận hành được, chính xác, nhanh, an toàn, có tính AI-native và giám khảo có thể tự dùng thử trên web — không phải một bản nghiên cứu hoàn hảo hay một dashboard giả.

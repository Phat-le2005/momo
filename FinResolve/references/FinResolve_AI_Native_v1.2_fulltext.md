# FinResolve AI — toàn văn chuyển từ bản Word v1.2

> Nguồn: `FinResolve_AI_Native_v1.2.docx` do người dùng cung cấp. Bản chuyển sang Markdown phục vụ coding agent đọc và tra cứu, không thay thế bản Word gốc.

FINRESOLVE AI

BÁO CÁO TỔNG QUAN
DỰ ÁN

AI-NATIVE BUSINESS RISK & OPERATIONS

Evidence-Grounded Agentic Operations · Adaptive Risk Response · Verified Resolution

| ĐỊNH VỊ Nền tảng AI-Native giúp doanh nghiệp nhỏ và vừa chủ động nhận diện rủi ro vận hành, điều tra ngoại lệ đa nguồn, huy động nhóm Agent thích ứng và giải quyết công việc bằng hành động có kiểm soát, có bằng chứng và được xác minh. |
| --- |

| PHIÊN BẢN | TRẠNG THÁI | PHẠM VI |
| --- | --- | --- |
| v1.2 – đề xuất hợp nhất | Đặc tả / kế hoạch triển khai; chưa khẳng định có sản phẩm chạy | Sản phẩm FinResolve + MoMo Sandbox + Mega Live Simulator |

Ngày cập nhật: 22/11/2026  |  Đối tượng: Đội thi MoMo Tech Contest 2026, cố vấn và người phát triển

Tài liệu này kế thừa bản đặc tả sản phẩm v1.1 và hai tài liệu tích hợp/đánh giá. Những nội dung AI-Native chủ động quan sát, phạm vi P0/P1/P2 và mốc sprint là đề xuất hợp nhất mới, chưa phải kết quả thử nghiệm.


## TÓM TẮT ĐIỀU HÀNH

FinResolve AI hướng đến xử lý một nhóm vấn đề tốn kém nhưng thường bị bỏ quên trong thương mại điện tử: trạng thái thanh toán, đơn hàng, kho, khuyến mãi và hoàn tiền không đồng nhất; khiếu nại thiếu thông tin; hoạt động xử lý bị chia cắt giữa nhiều ứng dụng và bộ phận. Trong phiên Mega Live, các lỗi có thể đồng thời xuất hiện và lan rộng trước khi nhân viên kịp phát hiện.

Cách tiếp cận AI-Native của FinResolve là quan sát sự kiện doanh nghiệp liên tục trong phạm vi cho phép, phát hiện tín hiệu bất thường bằng code, sau đó dùng AI để hiểu dữ liệu phi cấu trúc, xây giả thuyết, chủ động chọn bằng chứng cần xác minh và đề xuất kế hoạch xử lý. Hệ thống có thể liên kết những Case có khả năng chung nguyên nhân thành một Incident và cấu hình Taskforce tạm thời gồm các vai trò Finance, Operations, Risk hoặc Support.

FinResolve không cho LLM quyết định sự thật giao dịch hoặc tự quyền thực hiện lệnh tài chính. Mọi tác động phải đi qua Policy Engine, Business Action Contract, phê duyệt phù hợp, kiểm tra trạng thái và Verifier. Thành công chỉ được công nhận khi kết quả thực tế đã được xác minh; nếu thiếu bằng chứng hoặc API timeout, hệ thống phải duy trì trạng thái chưa xác định và chuyển người phụ trách.

| LỜI HỨA SẢN PHẨM Từ sự kiện bất thường đến kết quả được xác minh: phát hiện sớm – điều tra dựa trên bằng chứng – phối hợp thích ứng – hành động đúng quyền – đo được hiệu quả. |
| --- |

| Đối tượng / kết quả | Bản MVP cần chứng minh |
| --- | --- |
| Người bán SME | Một người quản lý thấy rõ nguyên nhân, rủi ro, trách nhiệm và kết quả xử lý qua một màn hình. |
| AI là cốt lõi | Đầu vào tiếng Việt/ảnh, thiếu thông tin, AI thay đổi bước điều tra khi có bằng chứng mới. |
| Giá trị an toàn | Chặn được thao tác không hợp lệ: refund trùng, vượt giới hạn, xác nhận tiền thiếu bằng chứng, oversell. |
| Khả năng thích ứng | Nhóm nhiều Case cùng nguyên nhân và tái sử dụng bằng chứng; xác minh từng Case. |
| Tính kiểm chứng | Simulator có Oracle ẩn, stress/fault injection, benchmark đối chứng. |


### Trạng thái và nguyên tắc công bố

• Hiện tại: tài liệu thiết kế và kế hoạch thử nghiệm; mọi chỉ số hiệu quả nêu sau đây là mục tiêu đo, không phải kết quả thực tế.

• MoMo Sandbox chỉ được coi là nguồn dữ liệu thử nghiệm nếu đã có testing credentials hợp lệ; nếu không, dùng MockMoMoAdapter có nhãn mô phỏng.

• Không khẳng định phát hiện được mọi hành vi gian lận, không kết luận tội phạm chỉ từ ảnh hoặc nội dung khiếu nại, không vận hành tiền thật trong bản demo.


## 1. Bài toán, bối cảnh và khách hàng mục tiêu


### 1.1. Vấn đề kinh doanh

Một đơn hàng đi qua nhiều hệ thống: kênh bán hàng, cổng thanh toán, OMS/ERP, tồn kho, vận chuyển và chăm sóc khách hàng. Trong luồng bình thường, công cụ tự động truyền thống xử lý tương đối hiệu quả. Chi phí lớn phát sinh ở ngoại lệ: sự kiện trễ/trùng/mất, mã giao dịch thiếu, hoàn tiền lặp, tồn kho thay đổi đồng thời, chính sách khuyến mãi mâu thuẫn, và phê duyệt không còn hợp lệ khi thực thi.

Ba dạng thất bại cốt lõi: (i) thất bại thông tin – bằng chứng thiếu hoặc mâu thuẫn; (ii) thất bại quyết định – phương án sai chính sách, mục tiêu, quyền; (iii) thất bại thực thi – API trả thành công nhưng trạng thái thực tế chưa đạt, hoặc thao tác bị lặp.


### 1.2. Nhóm người dùng ưu tiên

| Nhân vật | Nhu cầu thực tế | Giá trị kỳ vọng |
| --- | --- | --- |
| Chủ shop / quản lý SME | Giám sát nhiều ứng dụng; duyệt ngoại lệ, không có phòng Risk chuyên biệt | Ít công việc kiểm tra lặp, tránh thiệt hại có thể ngăn ngừa |
| CSKH | Hiểu khiếu nại, tìm đúng giao dịch và chuyển đúng bộ phận | Không hứa với khách điều chưa được xác minh |
| Vận hành / kho | Quản lý đơn, voucher, tồn kho, sự kiện trễ/trùng | Không oversell, giảm lỗi đồng bộ và thao tác lặp |
| Kế toán / tài chính | Xem thanh toán, refund, hạn mức và công nợ | Có bằng chứng, quyền và lịch sử rõ ràng |


### 1.3. Phân khúc thị trường

Ưu tiên e-commerce / livestream / shop đa kênh có tần suất ngoại lệ đủ lớn để tạo giá trị. Micro-business có ít Case có thể chỉ dùng trợ lý một Agent; doanh nghiệp vừa cần phối hợp nhiều vai trò; enterprise là hướng mở rộng sau, không phải cam kết MVP. Cần phỏng vấn người bán thực tế để xác nhận nhu cầu và mô hình chi trả.


## 2. Tầm nhìn, định vị và tính AI-Native

Định vị cuối: FinResolve AI – AI-Native Business Risk & Operations Platform; sản phẩm đầu tiên tập trung ngoại lệ Payment & Commerce. AI-Native ở đây nghĩa là năng lực hiểu ngữ cảnh, đặt giả thuyết và chọn hướng xử lý là yếu tố thiết kế trung tâm, không phải một chatbot được gắn thêm lên dashboard.

| Khía cạnh | Thiết kế AI-Native | Giới hạn không được vượt |
| --- | --- | --- |
| Perception | Nhận sự kiện, văn bản, ảnh và tín hiệu rủi ro | Không để LLM xác nhận giao dịch thành công |
| Active investigation | AI chọn nguồn cần kiểm tra để giảm bất định | Không tự suy diễn dữ kiện không có nguồn |
| Goal-driven planning | Tạo next step theo objective và trạng thái mới | Kế hoạch JSON được validate và giới hạn lượt |
| Adaptive collaboration | Chọn vai trò Taskforce theo Incident | Không tự cấp quyền hay tạo tool ghi mới |
| Verified feedback | Đọc hậu điều kiện và cập nhật Case | Không coi tool trả success là đã resolved |

AI-Native không đồng nghĩa biến mọi quy trình thành LLM. Đối soát số tiền, khóa kho, kiểm hạn mức, chống ghi trùng, xác minh chữ ký và quyền phải do code và nguồn dữ liệu có thẩm quyền xử lý. AI được gọi khi có bài toán ngữ nghĩa, thiếu dữ liệu, giả thuyết nhiều nhánh hoặc cần phối hợp tùy tình huống.


## 3. Giá trị sản phẩm và những năng lực cốt lõi

| Năng lực | Diễn giải kết quả mang lại |
| --- | --- |
| Business Perception | Quan sát sự kiện đơn hàng, thanh toán, refund, voucher, tồn kho và tin nhắn trong phạm vi cấp quyền; tạo Risk Signals. |
| Evidence-Grounded Reasoning | Tách CLAIM, OBSERVED FACT, HYPOTHESIS, UNKNOWN; mỗi dữ kiện quan trọng có nguồn, thời điểm, độ mới. |
| Active Evidence Investigation | Chọn câu hỏi/tool đọc tiếp theo khi có nhiều giả thuyết; biết dừng khi thiếu bằng chứng. |
| Adaptive Risk Response | Chuyển tín hiệu rủi ro thành OBSERVE / REVIEW / HOLD_ACTION trên hành động thuộc quyền kiểm soát. |
| Incident Correlation | Nhóm nhiều Case có khả năng chung nguyên nhân dựa vào thời gian, worker, mã lỗi, SKU và bằng chứng, không chỉ similarity. |
| Adaptive Incident Taskforce | Tạo cấu hình nhiệm vụ tạm thời cho Finance/Risk/Operations/Support trên runtime chung; không spawn một process/model mỗi Case. |
| Authorized Execution | Action Gateway kiểm input, quyền, phiên bản, hạn mức, approval digest và idempotency. |
| Verified Resolution | Chỉ đóng Case khi hậu điều kiện đọc lại có chứng cứ; UNKNOWN/ESCALATE khi kết quả chưa chắc chắn. |
| Self-Serve Sandbox | Giám khảo chạy scenario, nhập khiếu nại, xem Decision Trace và kết quả đánh giá; không cần cấu hình API. |


## 4. Ba kịch bản nghiệp vụ tiêu biểu


### 4.1. Shop nhỏ: khiếu nại đa phương thức và đơn chưa cập nhật

Khách gửi “đã chuyển khoản nhưng chưa có đơn” kèm ảnh chứng từ. Vision/LLM trích xuất reference và nêu thiếu sót; Finance read tool đối chiếu nguồn có thẩm quyền. Nếu payment đã xác thực nhưng order chưa đồng bộ, Operations kiểm tra phiên bản và đề xuất đồng bộ. Nếu hết hàng, Agent chuyển sang các phương án chờ/thay/hoàn tiền (cần duyệt theo mức rủi ro). Ảnh bill chỉ là dữ liệu do khách cung cấp, không phải chứng cứ tiền đã đến.


### 4.2. Doanh nghiệp vừa: refund bất thường

Một nhóm yêu cầu hoàn tiền xuất hiện trong phiên bán hàng. Risk Signal Engine kiểm giao dịch gốc, tổng tiền đã hoàn/đang chờ, yêu cầu trùng, giới hạn theo thời gian, phê duyệt có còn hiệu lực. AI đọc lý do khách hàng, điều khoản phức tạp và đề xuất bước xác minh; Finance/Risk phối hợp. Trường hợp chưa đủ bằng chứng chuyển REVIEW, không cáo buộc gian lận.


### 4.3. Mega Live / vận hành quy mô lớn: nhiều Incident đồng thời

Simulator tạo 100–500 đơn và các sự kiện: webhook trễ, voucher gần hết quota, SKU cuối bị tranh giữ, hủy đơn đến muộn. FinResolve phát hiện cụm Case có bằng chứng liên quan, đề xuất Taskforce xử lý nguyên nhân chung, nhưng Action Gateway và Verification kiểm tra từng đơn độc lập. Không gộp nhầm Incident có biểu hiện giống nhau nhưng nguyên nhân khác.

| TÍNH NĂNG THỰC TẾ Các rủi ro ở trên là ví dụ để thử nghiệm năng lực xử lý, không phải danh sách “mọi chiêu trò gian lận” đã được FinResolve phát hiện. Giá trị lõi là quyết định có căn cứ và ngăn thao tác không an toàn, kể cả khi nguồn gây lỗi là con người hoặc hệ thống. |
| --- |


## 5. Luồng hoạt động và cấu trúc Case / Incident

| Vòng lặp chính Business Event / Complaint → Risk Signal / Case Intake → Context & Evidence → AI Hypothesis / Plan → Read Tools / Ask User → Observe & Replan → Action Proposal → Policy / Approval → Transactional Execution → Verification → Resolved hoặc Escalated. |
| --- |

Hệ thống AI-Native có thể tạo Case chủ động từ tín hiệu rủi ro được xác định bởi quy tắc hoặc bất thường hệ thống; không phụ thuộc vào việc khách hàng đã gửi khiếu nại. Agent phải có stop condition, ngân sách gọi tool và replan, giới hạn token và thời gian xử lý.


### 5.1. Case và Incident không phải một

| Đối tượng | Ý nghĩa | Quy tắc |
| --- | --- | --- |
| Case | Một vấn đề gắn đơn/khách với objective, owner, state, evidence, version và success criteria | Có thể đóng khi postcondition đạt |
| Incident | Giả thuyết / sự kiện hệ thống ảnh hưởng nhiều Case | Có thể giải quyết nguyên nhân nhưng không tự đóng Case con |
| Taskforce | Cấu hình Agent/Role/Tool được lập tạm cho Incident | Hết nhiệm vụ thì giải thể runtime, vẫn giữ lịch sử |


### 5.2. Trạng thái tối thiểu

Case: NEW → INVESTIGATING → WAITING_EVIDENCE / WAITING_APPROVAL → EXECUTING → VERIFYING → RESOLVED; nhánh ESCALATED, FAILED hoặc UNKNOWN_OUTCOME theo tình huống. Payment, Order, Refund và Inventory phải có state machine riêng; không dồn chung vào một status.


## 6. Kiến trúc và các thành phần chức năng

| QUYẾT ĐỊNH KIẾN TRÚC Modular monolith Python/FastAPI + PostgreSQL + worker; web Next.js. Một runtime chung chứa các vai trò Agent. Chỉ tách dịch vụ hoặc thêm queue chuyên dụng khi có kết quả đo chứng minh cần thiết. |
| --- |

| Module | Chức năng bắt buộc | AI / Code |
| --- | --- | --- |
| Event & Intake Gateway | Nhận webhook, khiếu nại, tải file; auth, dedupe, giới hạn dữ liệu | Code |
| Perception / Risk Signals | Trích xuất nội dung; detect anomaly; phân mức tín hiệu và mức độ tin cậy | Code + Vision/LLM khi cần |
| Evidence / Context Engine | Lưu facts, claims, hypotheses, unknowns, refs, freshness; dựng context có kiểm soát | Code |
| Bounded Planner | Tạo hypothesis / next read / proposal JSON, có stop và replan | LLM + schema |
| Agent Runtime | Quản lý objective, Agent state, tool budget, task assignments | Đội tự viết |
| Incident Correlator | Đề xuất Case cùng nguyên nhân, tính confidence từ bằng chứng | Rule + AI tùy case |
| Taskforce Manager | Chọn role từ registry, phân quyền sẵn có và chia việc | Đội tự viết |
| MCP / Tool Gateway | Expose tools và kiểm input; gọi adapter đúng scope | SDK giao thức + code đội |
| Policy / Approval | Kiểm hạn mức, quyền, phiên bản, risk level, approval digest | Code bắt buộc |
| Action Executor | Transaction, outbox, idempotency, timeout/UNKNOWN | Code bắt buộc |
| Verifier / Audit | Read-back postcondition, event history, provenance, trace | Code bắt buộc |
| Simulator / Oracle | Tạo world state, fault injection, ground truth riêng, chấm đối chứng | Code đội |


### 6.1. Ranh giới tự phát triển

Đội sở hữu logic Agent Core, Tool Contract, Taskforce, Risk/Policy, Action/Verification và Simulator. LLM API dùng cho suy luận; SDK MCP chỉ hỗ trợ chuẩn giao tiếp. Không dùng framework Agent bên thứ ba để thay thế orchestrator hoặc vòng thực thi do đội tự viết.


### 6.2. Inter-Agent Communication

Agent không chat tự do như các nhân vật. Handoff qua Case Event, task assignment, payload JSON có schema, evidence_refs, correlation_id, version và tenant scope. Tất cả role dùng chung nguồn dữ liệu có kiểm soát; không cho một Agent tự mở quyền đọc chéo doanh nghiệp.


## 7. Chiến lược AI, model và công cụ


### 7.1. Vai trò AI là cốt lõi nhưng có giới hạn

LLM/Vision xử lý tiếng Việt không cấu trúc, ảnh/tài liệu, hiểu lý do yêu cầu, lựa chọn câu hỏi tiếp theo, xây giả thuyết, lập kế hoạch có cấu trúc và tóm tắt có dẫn nguồn. Code chịu trách nhiệm xác minh trạng thái thanh toán, kiểm chữ ký, so khớp tiền, khóa tồn kho, hạn mức refund, tính quota, authorization và kết luận postcondition.


### 7.2. Quy tắc gọi model

• Không gọi LLM trên mọi IPN hoặc mọi đơn; batch/rule engine xử lý sự kiện thường lệ trước.

• Một model multimodal/API ổn định làm chính, model mạnh hơn hoặc model local làm thử nghiệm phụ khi cần; chọn theo kiểm thử tiếng Việt, JSON schema, độ trễ, giá và chính sách dữ liệu.

• Planner trả JSON được Pydantic validate; mọi tool call kiểm allowlist và input schema trước khi thực thi.

• Context là dữ liệu cần thiết, không phải toàn DB; giới hạn 6–8 lượt tool/Case và tối đa 2 lần replan là ngưỡng giả định để benchmark, không phải đảm bảo hiệu quả.

• Không hiển thị CoT nội bộ; Decision Trace ghi Objective, Evidence, Chosen Step, Policy Result, Actual Tool Output và Verification.


### 7.3. MCP / Business Tool Contract

| Tool ví dụ | Input / tiền điều kiện | Quyền và hậu điều kiện |
| --- | --- | --- |
| find_payment_candidates | order_id + payment reference (nếu có) | Read-only; trả danh sách nguồn, timestamp, confidence mapping |
| get_order / get_inventory | tenant_id + order_id/SKU | Read-only, kèm version/freshness |
| propose_refund | Case + verified payment + amount + evidence refs | Proposal-only; không chuyển tiền |
| execute_approved_action | action digest + approval + expected_version + idempotency_key | Policy gateway chặn trước ghi; verify lại trạng thái |
| verify_case_state | Case goal + current source records | Đọc hậu điều kiện và trả verdict grounded |


## 8. Risk Intelligence & Adaptive Incident Taskforce


### 8.1. Phân loại tín hiệu rủi ro

ERROR = lỗi hệ thống; ANOMALY = chỉ dấu khác thường; SUSPECTED_ABUSE = nghi lạm dụng; CONFIRMED_POLICY_VIOLATION = vi phạm quy tắc đã kiểm chứng. Tín hiệu rủi ro không đồng nghĩa với kết luận gian lận. Response: OBSERVE, REVIEW, HOLD_ACTION; HOLD chỉ được áp vào hành động, tài nguyên mà doanh nghiệp có quyền kiểm soát, trong phạm vi hẹp và có hạn sử dụng.


### 8.2. Quy tắc an toàn ưu tiên

• Không xác nhận tiền chỉ vì ảnh, URL redirect hoặc lời khai; phải có provider record được kiểm chứng.

• Không refund nhiều hơn phần tiền hợp lệ còn lại; tính cả phần đã hoàn, đang chờ và hạn mức cộng dồn.

• Không dùng phê duyệt cho đơn A để ghi đơn B; policy, amount và order version phải khớp tại lúc commit.

• Không cấp 1 đơn vị tồn cho 2 đơn; ngăn oversell bằng transaction/invariant.

• Không lặp lại hành động ghi khi kết quả external API đang UNKNOWN; query/reconcile rồi mới quyết định.

• Không nhóm Case chỉ vì khách dùng câu chữ giống nhau; false merge nguy hiểm hơn false split.


### 8.3. Tạo Taskforce mà không tăng chi phí vô hạn

Khi nhiều Case xuất hiện có bằng chứng quan hệ (cùng worker, time window, provider event, SKU, mã lỗi), Incident Correlator đề xuất Incident. Taskforce Manager gán role từ registry đã được duyệt, có thể chỉ là ba cấu hình vai trò trên một Agent Runtime và cùng model. Hành động hàng loạt vẫn kiểm policy + precondition + version riêng cho từng Case. Kết thúc Taskforce không xóa các Case chưa được xác minh.


## 9. Tích hợp phần mềm doanh nghiệp và MoMo


### 9.1. Kiến trúc Bring Your Own Tools

Connector Adapter chuyển API/webhook của phần mềm bán hàng, ERP, kế toán, vận chuyển, CRM, kênh tin nhắn thành các Business Tools có schema, phạm vi quyền và nguồn dữ liệu. Mỗi connector phải mapping mã đơn, tenant, timestamp, trạng thái và điều kiện gọi; OpenAPI tự mô tả endpoint nhưng không tự đảm bảo hiểu nghiệp vụ.


### 9.2. Tích hợp thanh toán MoMo

Tài liệu MoMo cung cấp môi trường Sandbox riêng, testing credentials theo merchant, API tạo thanh toán, IPN, query và refund theo quyền được cấp. FinResolve là web app độc lập: backend merchant nhận IPN, kiểm chữ ký và đối chiếu order/amount, ghi event rồi đẩy điều tra vào worker. LLM không chạy trong IPN handler. MoMo Test App là ví test, không phải MoMo Mini App. Không khẳng định API truy cập giao dịch của mọi tài khoản MoMo.

| Môi trường | Vai trò | Nguyên tắc công bố |
| --- | --- | --- |
| MOCK | Mô phỏng payment/ERP/inventory do đội tự viết | Gắn nhãn giả lập; nguồn của simulator |
| MOMO_SANDBOX | Giao dịch test với credential được cấp | Chỉ dữ liệu merchant test; không phải tiền thật |
| PRODUCTION | Kết nối thương mại tương lai | Ngoài phạm vi MVP; yêu cầu quyền, bảo mật, UAT |

Nếu sau 48 giờ chưa có testing credentials, chuyển MockMoMoAdapter. Không ghi khóa lên frontend, prompt hoặc repo. Không tự động hoàn tiền test nếu chưa có quyền; thay bằng refund proposal và mô phỏng executor có nhãn.


## 10. Mega Live Simulator: sản phẩm thử nghiệm độc lập

Simulator không phải bản thân FinResolve. Nó là thế giới thương mại giả lập gồm đơn hàng, thanh toán, voucher, tồn kho, giao vận, lịch ảo và các lỗi có thể tiêm vào. World State/Oracle chứa đáp án bí mật; Observable API chỉ lộ dữ liệu và sự kiện mà Agent được phép thấy. Tránh fake demo nơi dashboard thay đổi nhưng backend không có tác động thật.

| Bộ phận simulator | Vai trò |
| --- | --- |
| Scenario Registry / Seed | Phiên bản hóa cấu hình, cho phép tái lập thí nghiệm. |
| World State / Virtual Clock | Lưu sự thật nền và tiến trình thời gian mô phỏng. |
| Event Generator + Fault Injector | Tạo lost/delayed/duplicate/out-of-order events, stale snapshot, timeout, race. |
| Observable API | Expose dữ liệu theo role, không lộ nhãn nguyên nhân gốc. |
| Action Executor | Thực thi action do FinResolve đề xuất trên DB giả lập sau kiểm quyền. |
| Evaluation Oracle | Chấm trạng thái cuối, false block, unsafe action, latency, chi phí; không được đọc bởi Agent. |


### 10.1. Ba chế độ thử

Guided Demo: kịch bản dễ hiểu, có nút reset. Randomized Stress Test: thay seed/khối lượng/lỗi. Hidden Evaluation: bộ tình huống chưa đưa vào prompt hoặc dữ liệu công khai để kiểm tra khả năng khái quát.


### 10.2. Bài toán Mega Live nên chấm

• Payment match thiếu mã hoặc status UNKNOWN;

• Refund trùng/vượt hạn mức/approval hết hạn;

• Voucher quota và thời điểm hết hạn;

• Oversell do tồn kho tranh chấp;

• Incident chung nguyên nhân và các Case nhiễu dễ gộp nhầm;

• Prompt injection trong lời khiếu nại hoặc dữ liệu tool.


## 11. Đánh giá khoa học và tiêu chí thành công


### 11.1. Thiết kế đối chứng

| Baseline | Ý nghĩa |
| --- | --- |
| B0 – Rule-based workflow | Kiểm tra với công cụ truyền thống, cùng nguồn dữ liệu và quyền. |
| B1 – Single Agent | Một Agent điều tra/plan với cùng số công cụ. |
| B2 – + Risk Signals | Kiểm tra giá trị của rủi ro vận hành. |
| B3 – + Incident Correlation / Taskforce | Đo giá trị điều phối và chia sẻ bằng chứng nhóm Case. |

Kết quả được đo trên tập scenario cùng nhãn ground truth và cùng điều kiện. Pilot đề xuất [GT]: 40–60 Case, có case bình thường và case dễ nhầm, gồm refund, payment mismatch, voucher/stock và incident. Chưa đủ mẫu thì không tuyên bố ý nghĩa thống kê. Bộ 120 Case là mục tiêu mở rộng khi kịp thời gian.


### 11.2. Chỉ số

| Chỉ số | Ý nghĩa |
| --- | --- |
| Verified Resolution Rate | Case kết thúc và đạt postcondition hợp lệ. |
| Unsafe Effect Rate | Tỷ lệ hành động bị thực thi sai chính sách/điều kiện. |
| False Block / False Escalation | Đo thiệt hại do cảnh báo/chặn nhầm. |
| Incident Precision / Recall / F1 | Độ chính xác gom Case cùng nguyên nhân; báo false merge riêng. |
| Human review workload | Số lần và thời gian con người phải xử lý. |
| Time-to-detect / p50–p95 Time-to-verified | Tốc độ phát hiện và giải quyết có xác minh. |
| LLM token / tool calls / cost per verified Case | Tính kinh tế vận hành. |

| QUY TẮC BÁO CÁO Chỉ đưa ra kết quả thực đo. Synthetic benchmark chứng minh khả năng trên môi trường giả lập, không tự chứng minh tỷ lệ gian lận thật bị ngăn, hiệu quả ROI trên SME hay năng lực production. |
| --- |


## 12. Đặc tả MVP 2026: phạm vi và trải nghiệm

| Mức | Tính năng | Điều kiện chấp nhận |
| --- | --- | --- |
| P0 – bắt buộc | Case intake tiếng Việt; Evidence/Context; Planner JSON; read tools; Policy/Approval; MockAdapter; Verifier; audit | Một Case E2E chạy được, có verify và xử lý lỗi |
| P0 – bắt buộc | Risk rules cho refund trùng/vượt hạn mức; 3 one-click sandbox scenarios; guest isolation | Ca hợp lệ không bị chặn nhầm, ca không hợp lệ bị kiểm soát |
| P1 – nên có | Incident Correlation và Taskforce 2–3 role; Oracle/benchmark 40–60 Case | Phân biệt Case cùng/khác nguyên nhân; có report đối chứng |
| P1 – nếu được | MoMo Sandbox read/query/checkout test, ảnh đầu vào | Có credentials và kiểm thử hợp lệ |
| P2 – sau cuộc thi | Mở rộng connector, local LLM, fraud ML, cross-tenant SaaS, streaming và tự động refund production | Có dữ liệu, quyền và đánh giá bảo mật |


### 12.1. Self-Serve Sandbox cho giám khảo

Giao diện web cho phép chạy ngay ba scenario không cần nhập credentials: (A) sai lệch thanh toán; (B) refund đáng ngờ cần kiểm tra; (C) Mega Live với Incident voucher/stock. Có vùng nhập khiếu nại tự do và tải ảnh mẫu nếu triển khai kịp. Hiển thị timeline: Claim → Evidence → AI Plan → Tool Call → Policy → Postcondition; nêu rõ MOCK/SANDBOX và trạng thái thực tế.

Guest session phải tách dữ liệu, giới hạn upload, số yêu cầu AI, thời gian sống và dung lượng; tránh sử dụng ảnh chứng từ người thật trên trang công khai. Hướng dẫn tự thử trong một trang A4, demo online liên tục đến 11/12/2026 theo thể lệ.


## 13. Công nghệ đề xuất và sơ đồ repository

| Tầng | Công nghệ tham khảo | Lý do |
| --- | --- | --- |
| Frontend | Next.js + TypeScript + Tailwind | Case timeline, approval, simulator và trạng thái realtime. |
| API | Python + FastAPI + Pydantic | Nhanh, JSON schema validation, dễ test. |
| Database | PostgreSQL + SQLAlchemy + Alembic | Nguồn sự thật, version, transaction, outbox, audit. |
| Async worker | DB queue; Redis tùy tải | Retry có kiểm soát, không cần Kafka. |
| LLM/Vision | Model API hỗ trợ tiếng Việt, ảnh và structured output | Suy luận, trích xuất, planning; có model adapter để thay thế. |
| MCP / Integration | MCP Python SDK + HTTPX + adapter tự viết | Chuẩn hóa tool và kết nối hệ thống. |
| Testing | Pytest + scenario seed + Oracle độc lập | Đánh giá repeatable và guardrails. |
| Deployment | Docker Compose + VPS/hosting ổn định | Dễ deploy, giảm cold start, backup/restart. |

Repository đề xuất: apps/web; services/api/{cases,evidence,ai_core,risk,incidents,policy,execution,verification,connectors,db,workers}; mcp_servers/commerce; simulator/{scenarios,world_state,faults,oracle}; tests/{unit,integration,benchmark}; infra/.


## 14. Chi phí, mô hình vận hành và ngân sách token


### 14.1. Chi phí model tính theo sử dụng thực tế

Chi phí AI/Case = Σ[(input tokens/1.000.000 × giá input) + (output tokens/1.000.000 × giá output)] theo từng lần gọi. Hình ảnh và reasoning tokens có thể làm tăng đơn giá hoặc số token tính phí; cần lấy giá tại thời điểm chọn model và đo qua logs. Giả định tham khảo của đặc tả gốc là 18.000 input + 3.000 output tokens/Case. Đây là giả định, không phải mức tiêu thụ đo được.


### 14.2. Ngân sách MVP

| Hạng mục | Dự trù [GT] | Ghi chú |
| --- | --- | --- |
| LLM API + thử nghiệm | 200.000 – 600.000 VNĐ | Token cap, rate limit, lưu sử dụng theo Case. |
| Hosting, database, storage | 300.000 – 800.000 VNĐ | Dùng máy/credit sẵn có để tối ưu. |
| Dự phòng | 200.000 – 500.000 VNĐ | Debug, retry, vận hành link demo. |
| Tổng tiền mặt tham khảo | 700.000 – 1.900.000 VNĐ | Không tính thời gian nhân sự, GPU, tài khoản API thương mại. |

Đây là khoảng dự trù nội bộ để lập kế hoạch, không phải báo giá thị trường hoặc cam kết chi phí. Nếu nhiều lượt benchmark, ảnh độ phân giải cao, hoặc chạy local model trên GPU thuê, cần điều chỉnh.


### 14.3. Cách tiết kiệm

• Không 1 event = 1 LLM; phân tích batch/SQL trước, gọi AI khi ngoại lệ không thể kết luận bằng luật.

• Một runtime, nhiều role cấu hình thay vì một tiến trình/model cho mỗi Agent.

• Ngữ cảnh rút gọn theo evidence_refs, không gửi toàn DB hoặc mọi message vào mỗi prompt.

• Bộ benchmark tái lập có caching an toàn và giới hạn retry; bật rate limiting cho guest demo.


## 15. Kế hoạch triển khai và phân công ba thành viên

Lưu ý thời gian: theo lịch chính thức, hạn nộp là 23:59:59 ngày 03/12/2026; tính từ 22/11/2026, đội có ít hơn hai tuần. Lộ trình 14 ngày trong đặc tả v1.1 là khung tham khảo, phải nén thành sprint đến 03/12. Sau hạn nộp không được sửa source, slides hoặc deployed demo đã nộp; cần giữ link online đến 11/12.

| Mốc | Backend / Security | AI / Evaluation | Frontend / DevOps |
| --- | --- | --- | --- |
| 22–24/11 | Schema, Case, evidence, policy, mock payment/order | Model adapter, planner JSON, 1 scenario | Case UI, one-click skeleton |
| 25–27/11 | Tool Gateway, executor, postcondition, refund rules | Active evidence loop, test errors, incident grouping tối thiểu | Timeline, approval, role UI, API integration |
| 28–30/11 | Incident state, taskforce registry, hard constraints | Simulator seed/oracle, B0/B1 benchmark | Mega Live UI, sandbox isolation, retry UX |
| 01–02/12 | E2E, cleanup, security, secrets, regression | Benchmark, metrics, report và demo cases | Deploy staging/production demo, video, slides, A4 guide |
| 03/12 | Freeze, smoke test, backup; NỘP TRƯỚC HẠN | Đảm bảo kết quả và log được tái lập | Đường link hoạt động ổn định, xác nhận bài nộp |


### 15.1. Go / No-Go gates

• Đến 26–27/11: tối thiểu một Case E2E có hậu điều kiện. Nếu chưa đạt, bỏ Taskforce trước.

• Đến 30/11: refund risk rule + Policy + Verifier ổn định. Nếu không, giảm bớt Risk loại phụ nhưng không cắt an toàn.

• Đến 01/12: giám khảo có thể vào link và chạy kịch bản trong 1–2 thao tác; nếu không, ưu tiên guest demo hơn chức năng mới.

• Tài khoản MoMo chưa có quyền trong 48 giờ: chuyển Mock Adapter, không để chặn tiến độ.


## 16. Rủi ro và nguyên tắc kiểm soát

| Rủi ro | Hậu quả | Đối sách |
| --- | --- | --- |
| AI bịa / hiểu sai bằng chứng | Chốt thanh toán hoặc kết luận sai | Source provenance, read-back, unknowns, reviewer |
| Prompt injection / tool poisoning | Vượt chính sách, gọi tool sai | Tách data/instructions, allowlist, schema, policy |
| False block / false accusation | Thiệt hại kinh doanh và uy tín | Observe/review/hold hẹp; người quản lý kiểm tra |
| False merge Incident | Batch tác động sai nhiều Case | Evidence-based correlation; mỗi Case kiểm riêng |
| TOCTOU / approval replay | Ghi theo trạng thái cũ hoặc sai đơn | Version, digest, expiry, tenant + order bound |
| Duplicate event / refund retry | Tác động nghiệp vụ nhiều lần | Idempotency, dedupe, query unknown outcome |
| Concurrency tồn kho | Oversell, sai reservation | DB transaction/unique constraints, locks |
| Leak Oracle / dữ liệu thật | Kết quả test giả hoặc lộ thông tin | Process/DB role tách, synthetic data |
| Mất kết nối API / quota | Case treo, demo lỗi | Timeout, backoff, circuit break, fallback mock |
| Demo public bị abuse | Tốn token, upload dữ liệu độc hại | Rate limit, upload cap, expiration, sandbox isolation |
| Scope creep / thiếu nhân lực | Không có sản phẩm hoàn chỉnh | Chỉ 1 E2E P0, cắt P1 khi trễ |


## 17. Chiến lược sản phẩm, thương mại hóa và cuộc thi


### 17.1. Đường đến sản phẩm thật

Giai đoạn 1: case intelligence + verified actions cho một loại doanh nghiệp thương mại. Giai đoạn 2: connector được cấp quyền với hệ thống bán hàng/kho/thanh toán và workflow chuyên sâu. Giai đoạn 3: quản lý đa tenant, phân quyền tổ chức, bảo mật, audit nâng cao, thanh toán đúng thẩm quyền và Incident Taskforce quy mô lớn. Không đưa ra ROI hoặc hiệu quả chống gian lận khi chưa có pilot khách hàng.


### 17.2. Điểm khác biệt cần chứng minh

Không cạnh tranh bằng số Agent, ảnh dashboard hoặc một bộ prompt. Điểm mạnh phải nằm ở (i) Active Evidence Investigation, (ii) quyết định trên nguồn có thẩm quyền và trạng thái biến động, (iii) Adaptive Incident Correlation/Taskforce giảm điều tra lặp và (iv) Verified Execution có kiểm soát. Kết quả so với B0 và B1 phải được trình bày công khai, cả trường hợp thất bại.


### 17.3. Demo ba phút

| Thời điểm | Nội dung |
| --- | --- |
| 0:00–0:25 | Đặt bài toán: Mega Live gây ngoại lệ thanh toán/refund/stock, SME khó điều phối. |
| 0:25–1:00 | Giám khảo bấm scenario; AI đọc khiếu nại và chọn chứng cứ cần tìm. |
| 1:00–1:40 | Rủi ro refund hoặc dữ liệu mâu thuẫn; Policy chặn hành động sai / xin duyệt. |
| 1:40–2:20 | Nhiều Case xuất hiện; Incident Correlation và Taskforce điều tra chung. |
| 2:20–2:45 | Verifier hiển thị Case được giải quyết, Case còn UNKNOWN, và audit. |
| 2:45–3:00 | Benchmark cùng seed: đúng/sai, tốc độ, token, demo và giới hạn. |


### 17.4. Tuân thủ cuộc thi

Theo thể lệ chính thức, đội 2–3 sinh viên; nộp trước 23:59:59 03/12/2026; demo phải truy cập được tới hết TechDay 11/12/2026; bài nộp sau hạn không được cập nhật. Tài sản có sẵn và phần mềm dùng từ trước phải khai báo README; tuân thủ license và quyền dùng dữ liệu. Ba tiêu chí chấm là sáng tạo, chất lượng/hoàn thiện và tiềm năng tác động, không có trọng số công khai trong thể lệ.


## 18. Quyết định chốt và những điểm còn cần xác minh

| CHỐT ĐỊNH HƯỚNG FinResolve AI là một nền tảng AI-Native xử lý và phòng ngừa rủi ro vận hành trong Payment & Commerce. Lõi do đội tự phát triển; LLM giúp hiểu, lập giả thuyết và lập kế hoạch; backend và policy giữ quyền quyết định và thực thi; Simulator kiểm chứng giá trị thật sự so với cách làm truyền thống. |
| --- |

| Câu hỏi mở | Cách xác minh |
| --- | --- |
| Có đủ testing credentials MoMo? | Kiểm tra M4B/sandbox quyền merchant; fallback mock theo kế hoạch. |
| Model đọc tiếng Việt, ảnh bill và JSON có ổn định? | Test tập ảnh/tin nhắn giả lập đa dạng, đo extraction accuracy. |
| AI giúp hơn B0 workflow không? | Chạy cùng seed trên 40–60 Case, phân tích lỗi cụ thể. |
| Incident Taskforce giảm chi phí không? | So B1/B2/B3: tool call, token, time, false merge, review workload. |
| Shop có sẵn sàng sử dụng/trả phí? | Phỏng vấn 5–8 người bán, kiểm workflow hiện tại, đo pain point. |
| Demo có chạy ổn khi giám khảo tự thử? | Guest sandbox, tài khoản demo, uptime monitoring, smoke test. |


## NGUỒN THAM KHẢO VÀ XUẤT XỨ

Tài liệu nội bộ làm căn cứ:

• [F1] 01_FinResolve_AI_Dac_Ta_San_Pham.docx – bản đặc tả sản phẩm gốc, 20/11/2026.

• [F2] FinResolve_AI_Dac_Ta_San_Pham_v1_1_Adaptive_Risk.docx – bổ sung Adaptive Risk Response và Incident Taskforce.

• [F3] 02_MoMo_Test_App_Sandbox_Tich_Hop_FinResolve.docx – luồng tích hợp Payment Sandbox, quyền và fallback.

• [F4] 03_Mega_Live_Simulator_Dac_Ta_Danh_Gia.docx – fault injection, oracle, baseline, simulator.

Nguồn công khai đối chiếu (xem điều khoản/phiên bản API chính thức khi triển khai):

https://techday.momo.vn/

https://techday.momo.vn/assets/docs/the-le-cuoc-thi.pdf

https://developers.momo.vn/v3/docs/payment/onboarding/integration-process/

https://developers.momo.vn/v3/docs/payment/api/result-handling/notification/

https://developers.momo.vn/v3/docs/payment/api/payment-api/refund/

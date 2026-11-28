# Nghiệm thu VNPAY Sandbox

| Gate | Kết quả | Bằng chứng |
|---|---|---|
| `PAY_IPN_RETURN_READY` | PASS | Lần nghiệm thu trước đã xác minh PAY và hai IPN Sandbox có chữ ký hợp lệ cho thành công và hủy. Lượt RC kiểm tra Return giữ PENDING trước IPN, chữ ký sai trả 97, IPN hợp lệ trả 00, gửi lại trả 02 và worker xác nhận đơn. |
| `FINRESOLVE_OBSERVATION_READY` | PASS | Lần trước Guardian đọc provenance Sandbox qua đường chỉ đọc. Lượt RC kiểm tra scope, phân biệt fixture với bằng chứng Sandbox, và trả capability unknown cho preview không có binding. |
| `QUERYDR_OFFLINE_READY` | PASS | Test PostgreSQL kiểm tra chữ ký, tương quan giao dịch, trạng thái terminal, kết quả lỗi và giới hạn lần thử. QueryDR bị tắt ở cả API và worker của Limited RC. |
| `QUERYDR_LIVE_VALIDATED` | NO | ISOLATED BUT UNRESOLVED. Các client đối chứng trước đây nhận mã 99 thiếu chữ ký. Lượt RC gửi 0 QueryDR ra provider. |
| `REFUND_ENABLED` | FALSE | Cấu hình runtime của API và worker đều tắt refund. |
| `VNPAY_LIMITED_SANDBOX_RC_READY` | PASS | Phù hợp trình diễn có kiểm soát cho PAY, signed IPN, Return và Guardian. Khi thiếu IPN, hệ thống chuyển sang đối soát thay vì xác nhận từ browser. |
| `VNPAY_SANDBOX_TASK_COMPLETE` | BLOCKED | Chưa có QueryDR live đã xác minh chữ ký và persistence qua MiniStore. |

## Lượt kiểm thử hiện tại

| Kiểm thử | Kết quả |
|---|---|
| MiniStore backend trên PostgreSQL | 52 passed |
| FinResolve backend trên PostgreSQL | 234 passed, gồm regression cho hợp đồng legacy của Guardian |
| FinResolve frontend unit | 15 passed |
| MiniStore frontend unit | 4 passed |
| FinResolve Guardian Playwright | 4 passed, gồm flow browser và fixture xử lý anomaly |
| MiniStore Return qua gateway | PASS cho pending, hạn polling, cô lập dữ liệu khi chuyển trang và dừng polling ở unknown |
| Gateway và luồng IPN fixture | PASS cho 6 trường hợp bị chặn, mã 97/00/02, đồng bộ order qua worker và log không chứa trường trong callback đã ký |
| Guardian ↔ Commerce runtime | PASS cho configured, QueryDR unavailable, fixture không được tính là bằng chứng live, preview unknown và cờ legacy vẫn là boolean |
| Competition Baseline đang chạy | 9/9 service healthy, LOCAL_MOCK khả dụng và VNPAY không khả dụng |
| Ruff, format và mypy hai backend | PASS |
| FinResolve frontend lint và typecheck | PASS |
| Compose RC cô lập | 10/10 service healthy, volume riêng, gateway và giao diện vận hành chỉ bind loopback, connector inactive |

Gateway tách quota IPN khỏi storefront. Các giới hạn storefront hiện vẫn gộp theo peer khi nhiều client đi qua cùng reverse proxy. QueryDR chưa có chứng cứ live mới. Phạm vi nghiệm thu này dành cho nhánh review riêng. Xem [bằng chứng tổng hợp](evidence.json) và [báo cáo QueryDR](QUERYDR_ROOT_CAUSE.md).

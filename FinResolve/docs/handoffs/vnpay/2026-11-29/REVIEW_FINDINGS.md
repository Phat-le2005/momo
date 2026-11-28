# Review findings

| Finding | Bằng chứng | Xử lý |
| --- | --- | --- |
| Gateway trước audit dựng URL upstream từ path đã decode | Regressions qua MockTransport kiểm tra biến thể encoded và phương thức ở public routes | Kiểm tra path canonical trước khi tạo upstream URL, giữ raw query riêng và không dùng forwarded headers từ request |
| Gateway dùng chung ingress và IPN quota chịu request vô danh | Storefront traffic và IPN đi qua một origin. Bộ giới hạn cũ tính IPN trước HMAC | Tách admission class ở gateway và quota IPN sau HMAC, kiểm tra merchant |
| HTTPX ghi URL ở mức INFO khi gateway chạy độc lập | Test chuyển tiếp IPN với query giả và root logger ở mức INFO | Giữ log theo nhóm route và tắt log request INFO của HTTPX |
| Signed Return có thể về trước IPN | Test Return success đọc PENDING và không hoàn tất đơn | Trình duyệt đọc có giới hạn và hiển thị hướng đối soát khi trạng thái chưa xác định |
| Hết thời hạn giữ chỗ trong khi VNPAY chưa có bằng chứng cuối | Regression tạo checkout, hết hạn reservation và phát lại IPN đến muộn | Giải phóng giữ chỗ, đánh dấu đối soát. IPN đến muộn không phục hồi đơn đã hủy |
| Signed QueryDR báo 00/00 nhưng thiếu bằng chứng thành công có thể làm FAILED thành UNKNOWN | Fixture có chữ ký hợp lệ và thiếu field xác nhận | Bảo toàn terminal payment, ghi bằng chứng mâu thuẫn |
| Guardian dùng nhãn tĩnh cho môi trường đã cấu hình VNPAY | API trả khả năng cấu hình, UI từng dùng thông báo VNPAY luôn tắt | Tách cấu hình, bằng chứng đã ký, trạng thái chưa biết và QueryDR không khả dụng |

Rate limit tại Commerce vẫn dùng peer của gateway cho auth/checkout. Khoảng cách quota giữa người dùng cùng origin cần quan sát trên buổi demo tải cao. Gateway không dùng IP từ header do người dùng cung cấp.

# QueryDR — Isolated but unresolved

Client MiniStore trước đây đã gọi bốn probe cho giao dịch Sandbox có IPN thành công. Trong vòng điều tra riêng, bốn probe chỉ đọc dùng client độc lập, hàm trong ZIP Python chính thức và một biến thể JSON theo mẫu PHP. Tất cả nhận HTTP 200, mã 99, thiếu chữ ký trong các probe mới. Mẫu Python tái hiện trên cả giao dịch thành công và giao dịch đã hủy. Chưa đủ căn cứ để phân biệt lỗi backend, cấu hình merchant và chính sách IP.

| Probe trước RC | Thay đổi được kiểm tra | Kết quả |
|---|---|---|
| E1 | Client độc lập theo đặc tả, ID dạng chuỗi và mô tả MiniStore | HTTP 200, mã 99, thiếu chữ ký |
| E2 | Hàm QueryDR và serialization từ ZIP Python chính thức | HTTP 200, mã 99, thiếu chữ ký |
| E3 | Cùng mẫu Python với giao dịch đã hủy | HTTP 200, mã 99, thiếu chữ ký |
| E4 | Client độc lập với ID JSON dạng số và mô tả theo mẫu PHP | HTTP 200, mã 99, thiếu chữ ký |

Đã đối chiếu PAY gốc với persisted identity, thời điểm tạo giao dịch và JSON cuối của HTTPX. Canonical bytes khớp trên vector độc lập và request đã chuẩn bị. Endpoint thực tế xác minh TLS. Mã 99 đã có ở upstream response trước bước parse hoặc state transition của ứng dụng.

Sửa sai lệch dữ liệu IP ở manual QueryDR bằng cách dùng cấu hình server chung cho API và worker. Chữ ký thiếu và chữ ký sai đều bị từ chối. Limited RC không gửi QueryDR. Các hardening tests của nhánh này dùng fixture và mock transport.

Nguồn protocol là POST 2.1.0. Mẫu Python xác định bằng SHA-256 của ZIP chính thức, hàm được gọi qua [wrapper](../../../../../MiniStore/backend/scripts/querydr_official_sample.py) và [client độc lập](../../../../../MiniStore/backend/scripts/querydr_reference.py). [Đặc tả QueryDR](https://sandbox.vnpayment.vn/apis/docs/truy-van-hoan-tien/querydr%26refund.html) · [ZIP Python](https://sandbox.vnpayment.vn/apis/files/vnpay_python.zip)

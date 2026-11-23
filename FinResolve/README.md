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

### 1. Trang trải nghiệm nhanh (`/demo`)
- Cho phép tạo phiên dùng thử tách biệt ngay trên trình duyệt, không cần đăng nhập.
- Cung cấp sẵn **3 kịch bản thực tế** để bấm chạy thử ngay:
  - **Thanh toán chưa cập nhật**: Giao dịch đã thành công nhưng đơn hàng vẫn chờ đồng bộ.
  - **Yêu cầu hoàn tiền bất thường**: Phân biệt giữa yêu cầu hoàn tiền một phần hợp lệ và yêu cầu hoàn tiền trùng lặp.
  - **Mega Live (50–100 đơn hàng)**: Mô phỏng phiên bán hàng cao điểm có xen kẽ nhiều lỗi thực tế (nghẽn đồng bộ hàng loạt, trùng IPN, hoàn tiền lặp, tranh chấp tồn kho).
- Hỗ trợ nhập tự do nội dung khiếu nại bằng tiếng Việt để tạo hồ sơ xử lý (Case) mới và nút reset phiên mô phỏng bất cứ lúc nào.

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

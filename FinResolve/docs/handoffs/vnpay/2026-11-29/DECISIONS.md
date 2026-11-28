# Release decisions

- PAY và IPN Sandbox là đường tài chính đã kiểm chứng live trước audit. Return chỉ tạo dấu hiệu trình duyệt và đọc trạng thái máy chủ. Giữ một authoritative write path qua IPN hoặc QueryDR hợp lệ khi capability QueryDR được bật trong tương lai.
- Limited RC tắt QueryDR tự động và truy vấn tay trước khi gửi HTTP. Tắt refund. QueryDR offline vẫn có fixture HMAC, lease, retry và state safety. Một lần chạy demo có thể tiếp tục khi chỉ dùng đường IPN đã kiểm chứng.
- Với giao dịch chưa chốt khi hết hạn giữ chỗ, bảo toàn trạng thái không chắc chắn và gắn đối soát. Nếu IPN hợp lệ đến muộn, mức tiền có thể được xác minh, còn đơn đã hủy phải được xử lý theo quy trình đối soát.
- Guardian chỉ quan sát. Cấu hình VNPAY không chứng minh QueryDR thành công. Bằng chứng fixture không được dùng để gắn nhãn đã xác minh live.
- Review công khai dùng evidence rút gọn. Chi tiết đối soát, log, cấu hình triển khai riêng và snapshot trước đó được bảo quản local.

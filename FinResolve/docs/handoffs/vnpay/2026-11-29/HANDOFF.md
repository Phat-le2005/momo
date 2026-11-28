# VNPAY Sandbox — Limited release candidate

Phạm vi đề xuất là trình diễn có kiểm soát Hosted PAY, callback IPN đã ký, Return chỉ đọc và FinResolve quan sát giao dịch Sandbox. Competition Release giữ Feature Freeze và tiếp tục dùng LOCAL_MOCK. Nhánh này phục vụ review riêng.

**Limited RC sẵn sàng ở phạm vi trên.** QueryDR live vẫn ở trạng thái `ISOLATED BUT UNRESOLVED`. Gate tổng thể `VNPAY_SANDBOX_TASK_COMPLETE` vẫn `BLOCKED`.

## Vận hành

1. Dùng bộ volume riêng cho project Sandbox. Tạo file credentials riêng ngoài working tree bằng [script khởi tạo](../../../../infra/init_vnpay_sandbox_env.py). Cung cấp merchant ID và HashSecret ngoài terminal history, log và Git. Không dùng credentials production.
2. Thêm hai biến `VNPAY_SANDBOX_SHOP_HOST` và `VNPAY_SANDBOX_IPN_HOST` vào cấu hình riêng của môi trường. Mỗi biến nhận một hostname, không có scheme hoặc path. Nếu tách hostname vào file thứ hai, truyền cả hai `--env-file` khi gọi Compose. `VNPAY_SANDBOX_IPN_HOST` phải khớp với đăng ký provider.
3. Chạy Compose từ root repo trong shell Linux/WSL có quyền đọc cấu hình riêng. Giá trị `SANDBOX_ENV` là đường dẫn file đó.

   ```sh
   docker compose --parallel 2 -p finshop-sandbox --env-file "$SANDBOX_ENV" \
     -f FinResolve/infra/docker-compose.yml \
     -f FinResolve/infra/docker-compose.vnpay-sandbox.yml config --quiet
   docker compose --parallel 2 -p finshop-sandbox --env-file "$SANDBOX_ENV" \
     -f FinResolve/infra/docker-compose.yml \
     -f FinResolve/infra/docker-compose.vnpay-sandbox.yml up -d --build --wait
   ```

4. Chỉ định hai public origin qua gateway loopback. Connector phải giữ nguyên header `Host`. Chuyển tiếp IPN tại đúng hostname và path `/api/v1/payments/vnpay/ipn`, method GET. Dùng TLS hợp lệ và bỏ cache trên callback. FinResolve API, Commerce API trực tiếp, PostgreSQL và các route merchant/service/reset ở mạng riêng.
5. Limited RC tắt QueryDR outbound và refund. Nếu Return về trước IPN, kiểm tra trạng thái tại lịch sử đơn hàng và Guardian. Trường hợp quá hạn mà chưa có IPN hợp lệ được đưa vào đối soát. Sau demo, dừng connector theo quy trình môi trường và giữ volume.

Đọc [findings](REVIEW_FINDINGS.md), [quyết định](DECISIONS.md), [nguyên nhân QueryDR](QUERYDR_ROOT_CAUSE.md) và [acceptance](ACCEPTANCE_MATRIX.md).

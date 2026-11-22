import type { Metadata } from "next";
import { ApiHealthPanel } from "@/components/ApiHealth";
import { MockBadge } from "@/components/MockBadge";

export const metadata: Metadata = { title: "Demo tự phục vụ — FinResolve AI" };

type Scenario = {
  id: "payment_sync" | "refund_risk" | "mega_live";
  title: string;
  summary: string;
  expected: string;
};

const SCENARIOS: Scenario[] = [
  {
    id: "payment_sync",
    title: "Đã thanh toán nhưng đơn chưa cập nhật",
    summary:
      "Nhà cung cấp thanh toán (mô phỏng) ghi nhận giao dịch thành công, nhưng đơn hàng vẫn ở trạng thái chờ.",
    expected:
      "AI đọc bằng chứng, đề xuất đồng bộ đơn; chính sách kiểm tra, thực thi và đọc lại trạng thái trước khi đánh dấu đã giải quyết.",
  },
  {
    id: "refund_risk",
    title: "Yêu cầu hoàn tiền trùng lặp",
    summary: "Một đơn đã hoàn tiền (hoặc đang chờ hoàn) tiếp tục nhận khiếu nại đòi hoàn tiền.",
    expected:
      "Hệ thống phát hiện trùng lặp/vượt hạn mức và chuyển sang xem xét (REVIEW) — không kết luận gian lận.",
  },
  {
    id: "mega_live",
    title: "Mega Live: nhiều sự cố cùng lúc",
    summary: "Phiên livestream mô phỏng 50–100 đơn với một nhóm sự kiện bị trễ/mất cùng nguyên nhân.",
    expected:
      "AI gom nhóm Case có bằng chứng chung thành Incident; mỗi Case vẫn được xác minh riêng.",
  },
];

export default function DemoPage() {
  return (
    <div className="space-y-10">
      <section className="space-y-4">
        <div className="flex flex-wrap items-center gap-3">
          <MockBadge />
          <span className="text-sm text-slate-600">Môi trường thử nghiệm công khai · không cần đăng nhập</span>
        </div>
        <h1 className="text-3xl font-bold tracking-tight sm:text-4xl">
          Từ sự kiện bất thường đến giải quyết đã được xác minh
        </h1>
        <p className="max-w-3xl text-lg text-slate-700">
          FinResolve AI điều tra ngoại lệ thanh toán và thương mại dựa trên bằng chứng, đề xuất hành động
          an toàn qua lớp chính sách, rồi đọc lại trạng thái thật để xác minh kết quả. Tin nhắn, ảnh chụp
          màn hình và kết quả của AI chỉ được coi là <strong>lời khai (CLAIM)</strong>, không phải bằng
          chứng đã thanh toán.
        </p>
      </section>

      <ApiHealthPanel />

      <section aria-labelledby="scenarios-heading" className="space-y-4">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 id="scenarios-heading" className="text-xl font-semibold">
            Kịch bản một chạm
          </h2>
          <p className="text-sm text-slate-600">
            Đang xây dựng — các nút sẽ được kích hoạt khi luồng xử lý thật sẵn sàng.
          </p>
        </div>
        <ul className="grid gap-4 md:grid-cols-3">
          {SCENARIOS.map((s) => (
            <li
              key={s.id}
              className="flex flex-col rounded-xl border border-slate-200 bg-white p-5 shadow-sm"
            >
              <div className="mb-2 flex items-center justify-between gap-2">
                <code className="rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-600">{s.id}</code>
                <MockBadge compact />
              </div>
              <h3 className="text-base font-semibold">{s.title}</h3>
              <p className="mt-2 text-sm text-slate-700">{s.summary}</p>
              <p className="mt-2 text-sm text-slate-500">
                <span className="font-medium text-slate-600">Kỳ vọng: </span>
                {s.expected}
              </p>
              <button
                type="button"
                disabled
                aria-disabled="true"
                className="mt-4 cursor-not-allowed rounded-lg bg-slate-200 px-4 py-2 text-sm font-medium text-slate-500"
                title="Chưa khả dụng trong bản khung (bootstrap)"
              >
                Chạy kịch bản (sắp có)
              </button>
            </li>
          ))}
        </ul>
      </section>

      <section className="rounded-xl border border-amber-200 bg-amber-50 p-5 text-sm text-amber-900">
        <h2 className="mb-1 font-semibold">Nguyên tắc an toàn</h2>
        <ul className="list-disc space-y-1 pl-5">
          <li>Không có giao dịch hay hoàn tiền thật; mọi dữ liệu là mô phỏng.</li>
          <li>Chỉ đánh dấu &quot;Đã giải quyết&quot; khi hậu điều kiện được kiểm chứng từ nguồn dữ liệu.</li>
          <li>Kết quả không xác định (timeout) được giữ nguyên là KHÔNG XÁC ĐỊNH, không thử lại mù quáng.</li>
          <li>Tín hiệu rủi ro không đồng nghĩa với gian lận.</li>
        </ul>
      </section>
    </div>
  );
}

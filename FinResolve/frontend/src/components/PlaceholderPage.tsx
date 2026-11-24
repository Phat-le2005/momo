import { MockBadge } from "./MockBadge";

type Props = { title: string; description: string };

export function PlaceholderPage({ title, description }: Props) {
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-bold">{title}</h1>
        <MockBadge compact />
      </div>
      <p className="max-w-3xl text-slate-700">{description}</p>
      <div
        role="status"
        className="rounded-xl border border-dashed border-slate-300 bg-white p-6 text-sm text-slate-600"
      >
        Trang này chưa có dữ liệu — tính năng đang được xây dựng. Không có kết quả nào được hiển thị
        cho tới khi luồng xử lý thật hoạt động.
      </div>
    </div>
  );
}

import type { ReactNode } from "react";

/** Neutral, outcome-aware labels; never imply UNKNOWN means success. */
const successful = new Set([
  "PASS",
  "COMPLETE",
  "PASSED",
  "RESOLVED",
  "COMPLETED",
  "PAID",
  "APPROVED",
  "MONITORING",
  "BLOCKED_SAFELY",
]);
const unsafe = new Set([
  "FAIL",
  "FAILED",
  "HOLD_ACTION",
  "UNKNOWN_OUTCOME",
  "INTERRUPTED",
  "DEGRADED",
  "BUDGET_EXHAUSTED",
]);
const caution = new Set([
  "INCOMPLETE",
  "UNKNOWN",
  "PENDING",
  "PARTIAL",
  "PARTIALLY_RESOLVED",
  "REVIEW",
  "ESCALATED",
  "WAITING_APPROVAL",
  "WAITING_EVIDENCE",
  "INVESTIGATING",
  "RUNNING",
  "CANCELLING",
  "QUEUED",
  "PAUSED",
  "SUPERSEDED",
]);

const STATUS_LABELS_VI: Record<string, string> = {
  MONITORING: "Đang giám sát (MONITORING)",
  INVESTIGATING: "Đang điều tra (INVESTIGATING)",
  WAITING_APPROVAL: "Chờ phê duyệt (WAITING_APPROVAL)",
  WAITING_EVIDENCE: "Chờ bằng chứng (WAITING_EVIDENCE)",
  PAUSED: "Tạm dừng (PAUSED)",
  DEGRADED: "Cảnh báo (DEGRADED)",
  STOPPED: "Đã dừng (STOPPED)",
  BLOCKED_SAFELY: "Đã chặn an toàn (BLOCKED_SAFELY)",
  COMPLETED: "Hoàn tất (COMPLETED)",
  PARTIALLY_RESOLVED: "Đã xác minh một phần (PARTIALLY_RESOLVED)",
  RESOLVED: "Đã giải quyết & xác minh (RESOLVED)",
  ESCALATED: "Chuyển cấp xử lý (ESCALATED)",
  QUEUED: "Đang chờ worker (QUEUED)",
  RUNNING: "AI đang điều tra (RUNNING)",
  CANCELLING: "Đang dừng hợp tác (CANCELLING)",
  CANCELLED: "Đã dừng (CANCELLED)",
  SUPERSEDED: "Đã thay thế (SUPERSEDED)",
  BUDGET_EXHAUSTED: "Hết ngân sách LLM (BUDGET_EXHAUSTED)",
  HOLD_ACTION: "Chặn hành động (HOLD_ACTION)",
  REVIEW: "Cần xem xét (REVIEW)",
  OBSERVE: "Theo dõi (OBSERVE)",
};

export function StatusPill({
  value,
  status,
}: {
  value?: string | null;
  status?: string | null;
}) {
  const text = value || status || "UNKNOWN";
  const cls = successful.has(text)
    ? "bg-emerald-50 border-emerald-300 text-emerald-800"
    : unsafe.has(text)
      ? "bg-rose-50 border-rose-300 text-rose-800"
      : caution.has(text)
        ? "bg-amber-50 border-amber-300 text-amber-800"
        : "bg-slate-100 border-slate-200 text-slate-700";
  const display = STATUS_LABELS_VI[text] ?? text;
  return (
    <span
      className={`inline-flex max-w-full items-center rounded-full border px-2.5 py-1 text-xs font-semibold ${cls}`}
    >
      {display}
    </span>
  );
}

export function Panel({
  title,
  subtitle,
  right,
  children,
}: {
  title: string;
  subtitle?: string;
  right?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-slate-900">{title}</h2>
          {subtitle && <p className="mt-0.5 text-xs text-slate-500">{subtitle}</p>}
        </div>
        {right && <div>{right}</div>}
      </div>
      {children}
    </section>
  );
}

export function Timestamp({ value }: { value?: string | null }) {
  if (!value) return <span>Chưa có</span>;
  const date = new Date(value);
  return (
    <time dateTime={value}>
      {Number.isNaN(date.getTime()) ? value : date.toLocaleString("vi-VN")}
    </time>
  );
}

export const primaryButton =
  "rounded-lg bg-indigo-700 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-800 focus-visible:outline-2 disabled:cursor-not-allowed disabled:bg-slate-300 disabled:text-slate-500";
export const secondaryButton =
  "rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-semibold text-slate-800 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50";

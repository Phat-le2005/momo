import type { ReactNode } from "react";

/** Neutral, outcome-aware labels; never imply UNKNOWN means success. */
const successful = new Set(["PASS", "COMPLETE", "PASSED", "RESOLVED", "COMPLETED", "PAID", "APPROVED"]);
const unsafe = new Set(["FAIL", "FAILED", "HOLD_ACTION", "UNKNOWN_OUTCOME", "INTERRUPTED"]);
const caution = new Set(["INCOMPLETE", "UNKNOWN", "PENDING", "PARTIAL", "REVIEW", "ESCALATED", "WAITING_APPROVAL", "SUPERSEDED"]);

export function StatusPill({ value }: { value: string | null | undefined }) {
  const text = value || "UNKNOWN";
  const cls = successful.has(text) ? "bg-emerald-50 border-emerald-300 text-emerald-800"
    : unsafe.has(text) ? "bg-rose-50 border-rose-300 text-rose-800"
    : caution.has(text) ? "bg-amber-50 border-amber-300 text-amber-800"
    : "bg-slate-100 border-slate-200 text-slate-700";
  return <span className={`inline-flex max-w-full items-center rounded-full border px-2.5 py-1 text-xs font-semibold ${cls}`}>{text}</span>;
}
export function Panel({ title, children }: { title: string; children: ReactNode }) {
  return <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
    <h2 className="mb-4 text-lg font-semibold text-slate-900">{title}</h2>{children}
  </section>;
}
export function Timestamp({ value }: { value?: string | null }) {
  if (!value) return <span>Chưa có</span>;
  const date = new Date(value);
  return <time dateTime={value}>{Number.isNaN(date.getTime()) ? value : date.toLocaleString("vi-VN")}</time>;
}
export const primaryButton = "rounded-lg bg-indigo-700 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-800 focus-visible:outline-2 disabled:cursor-not-allowed disabled:bg-slate-300 disabled:text-slate-500";
export const secondaryButton = "rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-semibold text-slate-800 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50";

"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { GuardianBadge } from "@/components/GuardianBadge";
import { useExperience } from "@/hooks/useExperience";
import {
  formatMetricOrEmpty,
  formatPercentOrEmpty,
  translateBusinessEventVi,
  type ExperienceAnomaly,
} from "@/lib/experience-api";

type WorkspaceView = "guardian" | "customer" | "sandbox";
type SidebarSection =
  | "overview"
  | "business"
  | "guardian"
  | "incidents"
  | "approvals"
  | "sandbox";

function formatShortTime(iso: string | null | undefined): string {
  if (!iso) return "Chưa có dữ liệu";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString("vi-VN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function severityPill(severity: string): string {
  switch (severity.toUpperCase()) {
    case "CRITICAL":
    case "HIGH":
      return "bg-[#FEF2F2] text-[#B91C1C] border-[#FECACA]";
    case "MEDIUM":
      return "bg-[#FFFBEB] text-[#B45309] border-[#FDE68A]";
    default:
      return "bg-[#EFF6FF] text-[#1D4ED8] border-[#BFDBFE]";
  }
}

function lifecyclePill(state: string): string {
  switch (state.toUpperCase()) {
    case "RECOVERED":
      return "bg-[#ECFDF5] text-[#047857] border-[#A7F3D0]";
    case "INVESTIGATING":
    case "RECOVERY_PENDING":
      return "bg-[#EFF6FF] text-[#1D4ED8] border-[#BFDBFE]";
    case "ESCALATED":
      return "bg-[#FFFBEB] text-[#B45309] border-[#FDE68A]";
    default:
      return "bg-[#F8FAFC] text-[#475569] border-[#E2E8F0]";
  }
}

function connectivityBadge(status: string): { label: string; cls: string; dot: string } {
  if (status === "CONNECTED") {
    return {
      label: "Đã kết nối",
      cls: "bg-[#ECFDF5] text-[#047857] border-[#A7F3D0]",
      dot: "bg-[#10B981]",
    };
  }
  if (status === "UNCONFIGURED") {
    return {
      label: "Chưa cấu hình",
      cls: "bg-[#F8FAFC] text-[#475569] border-[#E2E8F0]",
      dot: "bg-[#94A3B8]",
    };
  }
  return {
    label: "Mất kết nối",
    cls: "bg-[#FEF2F2] text-[#B91C1C] border-[#FECACA]",
    dot: "bg-[#EF4444]",
  };
}

export function ExperienceCommandCenter() {
  const {
    loading,
    error,
    bootstrap,
    overview,
    scanning,
    resetting,
    toasts,
    dismissToast,
    refreshNow,
    scanNow,
    resetSandbox,
    retryBootstrap,
  } = useExperience();

  const [view, setView] = useState<WorkspaceView>("guardian");
  const [section, setSection] = useState<SidebarSection>("overview");
  const [selectedAnomaly, setSelectedAnomaly] = useState<ExperienceAnomaly | null>(null);
  const [expandedTraces, setExpandedTraces] = useState<Record<string, boolean>>({});
  const [confirmResetOpen, setConfirmResetOpen] = useState(false);

  const toggleTrace = (id: string) => {
    setExpandedTraces((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  const handleSidebarClick = (target: SidebarSection) => {
    setSection(target);
    if (target === "sandbox") {
      setView("sandbox");
    } else if (view !== "guardian") {
      setView("guardian");
    }
  };

  const guardian = overview?.guardian;
  const kpis = overview?.kpis;
  const health = overview?.business_health;
  const connStatus = guardian?.connectivity_status ?? bootstrap?.sandbox.connectivity_status ?? "UNCONFIGURED";
  const conn = connectivityBadge(connStatus);
  const visualState = guardian?.visual_state ?? "DISCONNECTED";
  const modelMode = guardian?.model_mode ?? bootstrap?.guardian.model_mode ?? "DEMO_RULES";
  const modelName = guardian?.model_name ?? bootstrap?.guardian.model_name ?? "deterministic-demo";
  const storefrontUrl = bootstrap?.sandbox.storefront_url ?? "http://localhost:3100";
  const envName = bootstrap?.sandbox.environment_name ?? "FinShop LIVE Sandbox (MOCK)";

  const navItems = useMemo(
    () => [
      { id: "overview" as const, label: "Tổng quan", badge: null },
      {
        id: "business" as const,
        label: "Hoạt động kinh doanh",
        badge: overview?.recent_business_events.length ?? 0,
      },
      {
        id: "guardian" as const,
        label: "AI Guardian",
        badge: overview?.timeline.length ?? 0,
      },
      {
        id: "incidents" as const,
        label: "Sự cố",
        badge: kpis?.active_anomalies ?? 0,
      },
      {
        id: "approvals" as const,
        label: "Phê duyệt & Rủi ro",
        badge: kpis?.pending_approvals ?? 0,
      },
      { id: "sandbox" as const, label: "Thử thách hệ thống", badge: null },
    ],
    [kpis?.active_anomalies, kpis?.pending_approvals, overview?.recent_business_events.length, overview?.timeline.length],
  );

  return (
    <div className="min-h-screen overflow-x-hidden bg-[#F8FAFC] text-[#0F172A]">
      {/* Toast Notifications */}
      {toasts.length > 0 && (
        <div
          aria-live="polite"
          className="fixed bottom-4 right-4 z-50 flex w-full max-w-sm flex-col gap-2"
        >
          {toasts.map((t) => (
            <div
              key={t.id}
              className={`rounded-xl border bg-white p-3.5 shadow-lg ${
                t.tone === "success"
                  ? "border-[#A7F3D0]"
                  : t.tone === "warning"
                    ? "border-[#FDE68A]"
                    : "border-[#BFDBFE]"
              }`}
            >
              <div className="flex items-start justify-between gap-3">
                <div>
                  <p className="text-sm font-semibold text-[#0F172A]">{t.title}</p>
                  <p className="mt-0.5 text-xs text-[#64748B]">{t.detail}</p>
                </div>
                <button
                  type="button"
                  onClick={() => dismissToast(t.id)}
                  className="rounded-md px-1.5 py-0.5 text-xs text-[#64748B] hover:bg-[#F1F5F9] hover:text-[#0F172A]"
                  aria-label="Đóng thông báo"
                >
                  ✕
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Reset Confirmation Modal */}
      {confirmResetOpen && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="reset-dialog-title"
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4 backdrop-blur-xs"
        >
          <div className="w-full max-w-md rounded-2xl border border-[#E2E8F0] bg-white p-6 shadow-xl">
            <h2 id="reset-dialog-title" className="text-base font-bold text-[#0F172A]">
              Xác nhận làm mới phiên trải nghiệm?
            </h2>
            <p className="mt-2 text-sm leading-relaxed text-[#64748B]">
              Hệ thống sẽ tăng <span className="font-mono text-xs">world_epoch</span> cho riêng
              phiên hiện tại của bạn. Dữ liệu của các phiên khác hoàn toàn được cô lập và không bị
              ảnh hưởng.
            </p>
            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setConfirmResetOpen(false)}
                className="rounded-lg border border-[#E2E8F0] bg-white px-4 py-2 text-sm font-medium text-[#475569] hover:bg-[#F8FAFC]"
              >
                Hủy bỏ
              </button>
              <button
                type="button"
                disabled={resetting}
                onClick={async () => {
                  setConfirmResetOpen(false);
                  await resetSandbox();
                }}
                className="rounded-lg bg-[#2563EB] px-4 py-2 text-sm font-semibold text-white hover:bg-[#1D4ED8] disabled:opacity-60"
              >
                {resetting ? "Đang làm mới..." : "Xác nhận Reset"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Incident Detail Drawer */}
      {selectedAnomaly && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="Chi tiết sự cố"
          className="fixed inset-0 z-40 flex justify-end bg-slate-900/30 backdrop-blur-xs"
          onClick={() => setSelectedAnomaly(null)}
        >
          <div
            className="h-full w-full max-w-lg overflow-y-auto border-l border-[#E2E8F0] bg-white p-6 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-start justify-between gap-3 border-b border-[#E2E8F0] pb-4">
              <div>
                <p className="text-xs font-semibold uppercase tracking-wider text-[#2563EB]">
                  Chi tiết bất thường
                </p>
                <h3 className="mt-1 text-lg font-bold text-[#0F172A]">
                  {selectedAnomaly.anomaly_type}
                </h3>
                <p className="mt-0.5 font-mono text-xs text-[#64748B]">
                  {selectedAnomaly.id} · {selectedAnomaly.detector_code}
                </p>
              </div>
              <button
                type="button"
                onClick={() => setSelectedAnomaly(null)}
                className="rounded-lg border border-[#E2E8F0] px-2.5 py-1 text-xs font-medium text-[#64748B] hover:bg-[#F8FAFC]"
              >
                Đóng
              </button>
            </div>

            <div className="mt-4 flex flex-wrap gap-2">
              <span
                className={`rounded-full border px-2.5 py-0.5 text-xs font-semibold ${severityPill(
                  selectedAnomaly.severity,
                )}`}
              >
                Mức độ: {selectedAnomaly.severity}
              </span>
              <span
                className={`rounded-full border px-2.5 py-0.5 text-xs font-semibold ${lifecyclePill(
                  selectedAnomaly.lifecycle_state,
                )}`}
              >
                Trạng thái: {selectedAnomaly.lifecycle_state}
              </span>
              {selectedAnomaly.verification_verdict && (
                <span className="rounded-full border border-[#A7F3D0] bg-[#ECFDF5] px-2.5 py-0.5 text-xs font-semibold text-[#047857]">
                  Kiểm chứng: {selectedAnomaly.verification_verdict}
                </span>
              )}
            </div>

            <div className="mt-5 space-y-4">
              <div className="rounded-xl border border-[#E2E8F0] bg-[#F8FAFC] p-4">
                <p className="text-xs font-semibold uppercase tracking-wide text-[#64748B]">
                  Nhận định từ hệ thống
                </p>
                <p className="mt-1.5 text-sm leading-relaxed text-[#0F172A]">
                  {selectedAnomaly.rationale || "Đang tổng hợp lý giải từ bằng chứng quan sát."}
                </p>
              </div>

              <div className="grid grid-cols-2 gap-3 text-xs">
                <div className="rounded-xl border border-[#E2E8F0] p-3">
                  <p className="text-[#64748B]">Đơn hàng ảnh hưởng</p>
                  <p className="mt-1 font-mono font-semibold text-[#0F172A]">
                    {selectedAnomaly.affected_order_id ?? "Không gắn đơn cụ thể"}
                  </p>
                </div>
                <div className="rounded-xl border border-[#E2E8F0] p-3">
                  <p className="text-[#64748B]">Giao dịch thanh toán</p>
                  <p className="mt-1 font-mono font-semibold text-[#0F172A]">
                    {selectedAnomaly.affected_payment_id ?? "Chưa có"}
                  </p>
                </div>
                <div className="rounded-xl border border-[#E2E8F0] p-3">
                  <p className="text-[#64748B]">Phát hiện lúc</p>
                  <p className="mt-1 font-medium text-[#0F172A]">
                    {formatShortTime(selectedAnomaly.first_observed_at)}
                  </p>
                </div>
                <div className="rounded-xl border border-[#E2E8F0] p-3">
                  <p className="text-[#64748B]">Nguồn phục hồi</p>
                  <p className="mt-1 font-medium text-[#0F172A]">
                    {selectedAnomaly.recovery_source ?? "Chưa thực thi"}
                  </p>
                </div>
              </div>

              <div>
                <p className="text-xs font-semibold uppercase tracking-wide text-[#64748B]">
                  Bằng chứng tham chiếu ({selectedAnomaly.evidence_refs.length})
                </p>
                {selectedAnomaly.evidence_refs.length === 0 ? (
                  <p className="mt-1.5 text-xs text-[#64748B]">Chưa có mã bằng chứng đính kèm.</p>
                ) : (
                  <ul className="mt-2 space-y-1.5">
                    {selectedAnomaly.evidence_refs.map((ref) => (
                      <li
                        key={ref}
                        className="rounded-lg border border-[#E2E8F0] bg-[#F8FAFC] px-3 py-1.5 font-mono text-xs text-[#334155]"
                      >
                        {ref}
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              {selectedAnomaly.linked_case_id && (
                <div className="pt-2">
                  <Link
                    href={`/cases?caseId=${encodeURIComponent(selectedAnomaly.linked_case_id)}`}
                    className="inline-flex w-full items-center justify-center rounded-xl bg-[#2563EB] px-4 py-2.5 text-sm font-semibold text-white hover:bg-[#1D4ED8]"
                  >
                    Mở hồ sơ điều tra {selectedAnomaly.linked_case_id} →
                  </Link>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      <div className="flex min-h-screen">
        {/* Desktop Sidebar (232px) */}
        <aside className="hidden w-[232px] shrink-0 flex-col border-r border-[#E2E8F0] bg-white lg:flex">
          <div className="flex items-center gap-2.5 border-b border-[#E2E8F0] px-5 py-4">
            <Link href="/" className="flex items-center gap-2.5">
              <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-[#2563EB] text-sm font-bold text-white shadow-xs">
                FR
              </span>
              <div>
                <p className="text-sm font-bold tracking-tight text-[#0F172A]">FinResolve AI</p>
                <p className="text-[11px] text-[#64748B]">Guardian Command</p>
              </div>
            </Link>
          </div>

          <nav aria-label="Điều hướng Command Center" className="flex-1 space-y-1 p-3">
            <p className="px-2.5 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-wider text-[#94A3B8]">
              Không gian giám sát
            </p>
            {navItems.map((item) => {
              const active =
                (item.id === "sandbox" && view === "sandbox") ||
                (view === "guardian" && section === item.id);
              return (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => handleSidebarClick(item.id)}
                  className={`flex w-full items-center justify-between rounded-xl px-3 py-2 text-left text-sm font-medium transition ${
                    active
                      ? "bg-[#EFF6FF] text-[#1D4ED8]"
                      : "text-[#475569] hover:bg-[#F8FAFC] hover:text-[#0F172A]"
                  }`}
                >
                  <span>{item.label}</span>
                  {item.badge !== null && item.badge > 0 && (
                    <span
                      className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${
                        active ? "bg-[#2563EB] text-white" : "bg-[#F1F5F9] text-[#475569]"
                      }`}
                    >
                      {item.badge}
                    </span>
                  )}
                </button>
              );
            })}

            <div className="pt-5">
              <p className="px-2.5 pb-1 text-[11px] font-semibold uppercase tracking-wider text-[#94A3B8]">
                Công cụ chuyên sâu
              </p>
              <div className="space-y-1">
                {[
                  { href: "/cases", label: "Hồ sơ điều tra (Cases)" },
                  { href: "/risks", label: "Trung tâm rủi ro" },
                  { href: "/live", label: "Sự cố & Taskforce" },
                  { href: "/demo", label: "Mission Control" },
                  { href: "/judge", label: "Judge Lab" },
                ].map((link) => (
                  <Link
                    key={link.href}
                    href={link.href}
                    className="block rounded-xl px-3 py-2 text-sm text-[#64748B] hover:bg-[#F8FAFC] hover:text-[#0F172A]"
                  >
                    {link.label}
                  </Link>
                ))}
              </div>
            </div>
          </nav>

          <div className="border-t border-[#E2E8F0] p-4 text-xs text-[#64748B]">
            <p className="font-semibold text-[#0F172A]">Chế độ an toàn</p>
            <p className="mt-1 leading-relaxed">
              LOCAL_MOCK Sandbox · VNPAY đang tắt có chủ đích.
            </p>
            {overview && (
              <p className="mt-2 font-mono text-[11px] text-[#94A3B8]">
                {overview.session_id} · Epoch #{overview.world_epoch}
              </p>
            )}
          </div>
        </aside>

        {/* Main Column */}
        <div className="flex min-w-0 flex-1 flex-col">
          {/* Top Command Header */}
          <header className="sticky top-0 z-30 border-b border-[#E2E8F0] bg-white/95 backdrop-blur-md">
            <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-3 sm:px-6">
              <div className="flex flex-wrap items-center gap-3">
                <Link href="/" className="font-bold text-[#2563EB] lg:hidden">
                  FinResolve AI
                </Link>
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <h1 className="text-sm font-bold text-[#0F172A] sm:text-base">{envName}</h1>
                    <span
                      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-semibold ${conn.cls}`}
                    >
                      <span className={`h-1.5 w-1.5 rounded-full ${conn.dot}`} />
                      {conn.label}
                    </span>
                    <span
                      className={`rounded-full border px-2.5 py-0.5 text-xs font-semibold ${
                        modelMode === "LLM_API"
                          ? "border-[#BFDBFE] bg-[#EFF6FF] text-[#1D4ED8]"
                          : "border-[#E2E8F0] bg-[#F8FAFC] text-[#475569]"
                      }`}
                      title={`Model: ${modelName}`}
                    >
                      {modelMode === "LLM_API" ? `Live AI · ${modelName}` : "Chế độ DEMO_RULES"}
                    </span>
                  </div>
                  <p className="mt-0.5 text-xs text-[#64748B]">
                    Cập nhật lúc {formatShortTime(overview?.updated_at)} · Worker:{" "}
                    <span
                      className={
                        guardian?.worker_healthy
                          ? "font-medium text-[#047857]"
                          : "font-medium text-[#B45309]"
                      }
                    >
                      {guardian?.worker_healthy ? "Đang hoạt động" : "Tạm dừng / chưa phản hồi"}
                    </span>
                  </p>
                </div>
              </div>

              <div className="flex flex-wrap items-center gap-2">
                {/* 3-View Switcher */}
                <div
                  role="tablist"
                  aria-label="Chuyển góc nhìn trải nghiệm"
                  className="inline-flex rounded-xl border border-[#E2E8F0] bg-[#F8FAFC] p-1 text-xs font-semibold"
                >
                  {(
                    [
                      { id: "guardian", label: "Guardian" },
                      { id: "customer", label: "Khách hàng" },
                      { id: "sandbox", label: "Sandbox" },
                    ] as const
                  ).map((tab) => (
                    <button
                      key={tab.id}
                      role="tab"
                      type="button"
                      aria-selected={view === tab.id}
                      onClick={() => setView(tab.id)}
                      className={`rounded-lg px-3 py-1.5 transition ${
                        view === tab.id
                          ? "bg-white text-[#2563EB] shadow-xs"
                          : "text-[#64748B] hover:text-[#0F172A]"
                      }`}
                    >
                      {tab.label}
                    </button>
                  ))}
                </div>

                <button
                  type="button"
                  disabled={scanning || loading}
                  onClick={() => void scanNow()}
                  className="rounded-xl bg-[#2563EB] px-3.5 py-2 text-xs font-semibold text-white shadow-xs transition hover:bg-[#1D4ED8] disabled:opacity-60"
                >
                  {scanning ? "Đang quét..." : "Quét ngay"}
                </button>
                <button
                  type="button"
                  disabled={resetting || loading}
                  onClick={() => setConfirmResetOpen(true)}
                  className="rounded-xl border border-[#E2E8F0] bg-white px-3 py-2 text-xs font-semibold text-[#475569] transition hover:bg-[#F8FAFC] disabled:opacity-60"
                >
                  Reset phiên
                </button>
              </div>
            </div>
          </header>

          {/* Main Content */}
          <main className="mx-auto w-full max-w-7xl flex-1 space-y-6 px-4 py-6 sm:px-6">
            {/* Error Banner */}
            {error && (
              <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[#FECACA] bg-[#FEF2F2] px-4 py-3 text-sm text-[#991B1B]">
                <div>
                  <p className="font-semibold">Không thể đồng bộ dữ liệu thời gian thực</p>
                  <p className="text-xs text-[#B91C1C]">{error}</p>
                </div>
                <button
                  type="button"
                  onClick={() => void (bootstrap ? refreshNow() : retryBootstrap())}
                  className="rounded-lg bg-white px-3 py-1.5 text-xs font-semibold text-[#B91C1C] shadow-xs hover:bg-[#FEE2E2]"
                >
                  Thử lại ngay
                </button>
              </div>
            )}

            {/* Loading Skeleton */}
            {loading && !overview ? (
              <div className="space-y-6">
                <div className="h-24 animate-pulse rounded-2xl border border-[#E2E8F0] bg-white" />
                <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
                  {Array.from({ length: 6 }).map((_, idx) => (
                    <div
                      key={idx}
                      className="h-24 animate-pulse rounded-2xl border border-[#E2E8F0] bg-white"
                    />
                  ))}
                </div>
                <div className="grid gap-6 lg:grid-cols-2">
                  <div className="h-72 animate-pulse rounded-2xl border border-[#E2E8F0] bg-white" />
                  <div className="h-72 animate-pulse rounded-2xl border border-[#E2E8F0] bg-white" />
                </div>
              </div>
            ) : view === "customer" ? (
              /* ==================== CUSTOMER VIEW ==================== */
              <div className="space-y-6">
                <section className="rounded-2xl border border-[#BFDBFE] bg-gradient-to-br from-[#EFF6FF] via-white to-white p-6 shadow-xs">
                  <div className="flex flex-wrap items-center justify-between gap-4">
                    <div className="max-w-2xl">
                      <span className="inline-flex rounded-full bg-[#DBEAFE] px-3 py-1 text-xs font-semibold text-[#1D4ED8]">
                        Góc nhìn khách hàng · FinShop LIVE
                      </span>
                      <h2 className="mt-3 text-xl font-bold text-[#0F172A]">
                        Mua sắm thực tế trên FinShop và xem AI Guardian bảo vệ phía sau
                      </h2>
                      <p className="mt-2 text-sm leading-relaxed text-[#475569]">
                        Mở cửa hàng FinShop LIVE ở tab mới, thực hiện đặt hàng bằng{" "}
                        <span className="font-semibold text-[#0F172A]">LOCAL_MOCK</span> hoặc gửi
                        khiếu nại đơn hàng. Phiên Guardian này vẫn giữ nguyên trạng thái và tự động
                        ghi nhận mọi sự kiện phát sinh.
                      </p>
                    </div>
                    <div className="flex flex-wrap gap-2.5">
                      <a
                        href={storefrontUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-2 rounded-xl bg-[#2563EB] px-5 py-3 text-sm font-semibold text-white shadow-sm transition hover:bg-[#1D4ED8]"
                      >
                        Mở FinShop LIVE ↗
                      </a>
                      <button
                        type="button"
                        onClick={() => setView("guardian")}
                        className="rounded-xl border border-[#E2E8F0] bg-white px-4 py-3 text-sm font-semibold text-[#0F172A] hover:bg-[#F8FAFC]"
                      >
                        Quay lại Guardian View
                      </button>
                    </div>
                  </div>

                  <div className="mt-6 grid gap-4 md:grid-cols-3">
                    {[
                      {
                        step: "Bước 1",
                        title: "Tạo đơn hàng trên FinShop",
                        desc: "Chọn sản phẩm bất kỳ và thanh toán bằng phương thức LOCAL_MOCK an toàn.",
                      },
                      {
                        step: "Bước 2",
                        title: "Quan sát sự kiện phát sinh",
                        desc: "Mỗi giao dịch, độ trễ đồng bộ hoặc khiếu nại đều được chuyển về luồng quan sát.",
                      },
                      {
                        step: "Bước 3",
                        title: "Theo dõi AI Guardian xử lý",
                        desc: "Quay lại tab Guardian hoặc nhấn Quét ngay để xem AI điều tra và xác minh.",
                      },
                    ].map((card) => (
                      <div
                        key={card.step}
                        className="rounded-xl border border-[#E2E8F0] bg-white p-4"
                      >
                        <p className="text-xs font-bold uppercase tracking-wider text-[#2563EB]">
                          {card.step}
                        </p>
                        <p className="mt-1 text-sm font-semibold text-[#0F172A]">{card.title}</p>
                        <p className="mt-1 text-xs leading-relaxed text-[#64748B]">{card.desc}</p>
                      </div>
                    ))}
                  </div>
                </section>

                <section className="rounded-2xl border border-[#E2E8F0] bg-white p-5 shadow-xs">
                  <div className="flex items-center justify-between gap-2">
                    <div>
                      <h3 className="text-sm font-bold text-[#0F172A]">
                        Sự kiện kinh doanh mới nhất từ FinShop
                      </h3>
                      <p className="text-xs text-[#64748B]">
                        Dữ liệu thực thuộc phiên {overview?.session_id ?? "hiện tại"}
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={() => void scanNow()}
                      disabled={scanning}
                      className="rounded-lg border border-[#E2E8F0] px-3 py-1.5 text-xs font-semibold text-[#2563EB] hover:bg-[#EFF6FF]"
                    >
                      {scanning ? "Đang đồng bộ..." : "Đồng bộ sự kiện"}
                    </button>
                  </div>
                  {!overview || overview.recent_business_events.length === 0 ? (
                    <div className="mt-4 rounded-xl border border-dashed border-[#CBD5E1] bg-[#F8FAFC] p-6 text-center text-sm text-[#64748B]">
                      Chưa ghi nhận sự kiện mua hàng nào trong Epoch này. Hãy mở FinShop LIVE và
                      tạo một đơn hàng mẫu.
                    </div>
                  ) : (
                    <ul className="mt-4 divide-y divide-[#F1F5F9]">
                      {overview.recent_business_events.map((ev) => (
                        <li
                          key={ev.event_id}
                          className="flex flex-wrap items-center justify-between gap-2 py-3 text-sm"
                        >
                          <div>
                            <p className="font-medium text-[#0F172A]">
                              {translateBusinessEventVi(ev.event_type)}
                            </p>
                            <p className="font-mono text-xs text-[#64748B]">
                              {ev.order_id ? `Đơn: ${ev.order_id}` : ev.event_id}
                              {ev.payment_id ? ` · TT: ${ev.payment_id}` : ""}
                            </p>
                          </div>
                          <span className="text-xs text-[#64748B]">
                            {formatShortTime(ev.occurred_at)}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </section>
              </div>
            ) : view === "sandbox" ? (
              /* ==================== SANDBOX PLAYGROUND VIEW ==================== */
              <div className="space-y-6">
                <section className="rounded-2xl border border-[#E2E8F0] bg-white p-6 shadow-xs">
                  <span className="inline-flex rounded-full border border-[#BFDBFE] bg-[#EFF6FF] px-3 py-1 text-xs font-semibold text-[#1D4ED8]">
                    Sandbox Playground · Cô lập theo phiên
                  </span>
                  <h2 className="mt-3 text-xl font-bold text-[#0F172A]">
                    Thử thách hệ thống an toàn trong môi trường LOCAL_MOCK
                  </h2>
                  <p className="mt-2 max-w-3xl text-sm leading-relaxed text-[#64748B]">
                    Bạn có thể kích hoạt lượt quan sát, làm mới phạm vi phiên (World Epoch) hoặc
                    chuyển sang Judge Lab / Mission Control để kiểm thử kịch bản mà không làm lộ
                    khóa quản trị Chaos hay ground truth về phía trình duyệt.
                  </p>

                  <div className="mt-6 grid gap-4 md:grid-cols-3">
                    <div className="rounded-xl border border-[#E2E8F0] bg-[#F8FAFC] p-4">
                      <h3 className="text-sm font-bold text-[#0F172A]">
                        1. Kích hoạt quét Monitor ngay
                      </h3>
                      <p className="mt-1 text-xs leading-relaxed text-[#64748B]">
                        Chạy ngay 1 chu kỳ quan sát read-only từ FinShop để cập nhật telemetry và
                        phát hiện bất thường mới.
                      </p>
                      <button
                        type="button"
                        disabled={scanning}
                        onClick={() => void scanNow()}
                        className="mt-4 w-full rounded-lg bg-[#2563EB] px-3 py-2 text-xs font-semibold text-white hover:bg-[#1D4ED8] disabled:opacity-60"
                      >
                        {scanning ? "Đang quét..." : "Chạy lượt quan sát"}
                      </button>
                    </div>

                    <div className="rounded-xl border border-[#E2E8F0] bg-[#F8FAFC] p-4">
                      <h3 className="text-sm font-bold text-[#0F172A]">
                        2. Kiểm thử mù (Judge Lab)
                      </h3>
                      <p className="mt-1 text-xs leading-relaxed text-[#64748B]">
                        Mở phòng kiểm thử đánh giá độc lập để kiểm chứng năng lực phát hiện và phục
                        hồi không phụ thuộc kịch bản định sẵn.
                      </p>
                      <Link
                        href="/judge"
                        className="mt-4 inline-flex w-full items-center justify-center rounded-lg border border-[#2563EB] bg-white px-3 py-2 text-xs font-semibold text-[#2563EB] hover:bg-[#EFF6FF]"
                      >
                        Mở Judge Lab →
                      </Link>
                    </div>

                    <div className="rounded-xl border border-[#E2E8F0] bg-[#F8FAFC] p-4">
                      <h3 className="text-sm font-bold text-[#0F172A]">
                        3. Làm mới phiên hiện tại
                      </h3>
                      <p className="mt-1 text-xs leading-relaxed text-[#64748B]">
                        Tăng World Epoch để bắt đầu một chu kỳ quan sát sạch mới chỉ trong phiên của
                        riêng bạn.
                      </p>
                      <button
                        type="button"
                        disabled={resetting}
                        onClick={() => setConfirmResetOpen(true)}
                        className="mt-4 w-full rounded-lg border border-[#CBD5E1] bg-white px-3 py-2 text-xs font-semibold text-[#334155] hover:bg-[#F1F5F9]"
                      >
                        Reset World Epoch
                      </button>
                    </div>
                  </div>

                  <div className="mt-5 rounded-xl border border-[#FDE68A] bg-[#FFFBEB] p-4 text-xs leading-relaxed text-[#92400E]">
                    <p className="font-semibold">Nguyên tắc bảo mật Sandbox</p>
                    <p className="mt-1">
                      Khóa Chaos Admin và khóa ký HMAC chỉ tồn tại ở backend. Các thao tác tiêm lỗi
                      cấp quản trị không được mở công khai trên trình duyệt khách; hệ thống chỉ thực
                      thi qua kênh xác thực máy chủ và luôn giữ VNPAY ở trạng thái tắt.
                    </p>
                  </div>
                </section>
              </div>
            ) : (
              /* ==================== GUARDIAN VIEW (DEFAULT) ==================== */
              <>
                {/* Hero Status Strip with Guardian Visual Identity */}
                <section className="flex flex-wrap items-center justify-between gap-4 rounded-2xl border border-[#E2E8F0] bg-white p-5 shadow-xs">
                  <GuardianBadge state={visualState} />
                  <div className="flex flex-wrap items-center gap-6 text-xs">
                    <div>
                      <p className="text-[#64748B]">Hàng đợi điều tra</p>
                      <p className="mt-0.5 text-base font-bold text-[#0F172A]">
                        {guardian?.queued_jobs ?? 0} tác vụ
                      </p>
                    </div>
                    <div>
                      <p className="text-[#64748B]">Lần quét gần nhất</p>
                      <p className="mt-0.5 text-base font-bold text-[#0F172A]">
                        {formatShortTime(guardian?.last_scan_at)}
                      </p>
                    </div>
                    <div>
                      <p className="text-[#64748B]">Trạng thái Baseline</p>
                      <p className="mt-0.5 text-sm font-semibold text-[#2563EB]">
                        {health?.baseline_status === "ESTABLISHED"
                          ? "Đã thiết lập"
                          : "Đang học mẫu thực"}
                      </p>
                    </div>
                  </div>
                </section>

                {/* 6 Real KPI Cards */}
                <section aria-label="Chỉ số vận hành thực">
                  <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
                    {[
                      {
                        label: "Đơn hàng quan sát",
                        value: formatMetricOrEmpty(kpis?.orders_observed),
                        accent: "text-[#0F172A]",
                      },
                      {
                        label: "Thanh toán quan sát",
                        value: formatMetricOrEmpty(kpis?.payments_observed),
                        accent: "text-[#0F172A]",
                      },
                      {
                        label: "Bất thường đang mở",
                        value: formatMetricOrEmpty(kpis?.active_anomalies ?? 0),
                        accent:
                          (kpis?.active_anomalies ?? 0) > 0
                            ? "text-[#D97706]"
                            : "text-[#0F172A]",
                      },
                      {
                        label: "Phục hồi xác minh",
                        value: formatMetricOrEmpty(kpis?.verified_recoveries ?? 0),
                        accent:
                          (kpis?.verified_recoveries ?? 0) > 0
                            ? "text-[#059669]"
                            : "text-[#0F172A]",
                      },
                      {
                        label: "Chờ phê duyệt",
                        value: formatMetricOrEmpty(kpis?.pending_approvals ?? 0),
                        accent:
                          (kpis?.pending_approvals ?? 0) > 0
                            ? "text-[#2563EB]"
                            : "text-[#0F172A]",
                      },
                      {
                        label: "Tổng hồ sơ (Cases)",
                        value: formatMetricOrEmpty(kpis?.total_cases ?? 0),
                        accent: "text-[#0F172A]",
                      },
                    ].map((kpi) => (
                      <div
                        key={kpi.label}
                        className="rounded-2xl border border-[#E2E8F0] bg-white p-4 shadow-xs"
                      >
                        <p className="text-xs font-medium text-[#64748B]">{kpi.label}</p>
                        <p className={`mt-2 text-2xl font-bold tracking-tight ${kpi.accent}`}>
                          {kpi.value}
                        </p>
                      </div>
                    ))}
                  </div>
                </section>

                {/* Business Health + AI Guardian Live Activity */}
                <div className="grid gap-6 lg:grid-cols-12">
                  {/* Business Health Panel */}
                  {(section === "overview" || section === "business") && (
                    <section className="rounded-2xl border border-[#E2E8F0] bg-white p-5 shadow-xs lg:col-span-5">
                      <div className="flex items-start justify-between gap-2">
                        <div>
                          <h2 className="text-sm font-bold text-[#0F172A]">
                            Sức khỏe vận hành (Business Health)
                          </h2>
                          <p className="mt-0.5 text-xs text-[#64748B]">
                            {health?.baseline_message_vi ??
                              "Guardian đang thiết lập trạng thái hoạt động bình thường."}
                          </p>
                        </div>
                        <span
                          className={`shrink-0 rounded-full border px-2.5 py-0.5 text-[11px] font-semibold ${
                            health?.telemetry_available
                              ? "border-[#A7F3D0] bg-[#ECFDF5] text-[#047857]"
                              : "border-[#E2E8F0] bg-[#F8FAFC] text-[#64748B]"
                          }`}
                        >
                          {health?.telemetry_available ? "Có telemetry" : "Chưa có mẫu"}
                        </span>
                      </div>

                      <div className="mt-4 grid grid-cols-2 gap-3">
                        {[
                          {
                            label: "Lưu lượng (RPS)",
                            value: formatMetricOrEmpty(health?.rps, " req/s", 2),
                          },
                          {
                            label: "Độ trễ P95",
                            value: formatMetricOrEmpty(health?.p95_latency_ms, " ms", 0),
                          },
                          {
                            label: "Tỉ lệ lỗi",
                            value: formatPercentOrEmpty(health?.error_rate),
                          },
                          {
                            label: "Thanh toán lệch đồng bộ",
                            value: formatMetricOrEmpty(health?.unsynced_payments),
                          },
                          {
                            label: "Độ trễ Outbox",
                            value: formatMetricOrEmpty(health?.outbox_lag),
                          },
                          {
                            label: "Cửa sổ quan sát",
                            value: health?.window_id ?? "Chưa có dữ liệu",
                          },
                        ].map((m) => (
                          <div
                            key={m.label}
                            className="rounded-xl border border-[#F1F5F9] bg-[#F8FAFC] p-3"
                          >
                            <p className="text-[11px] font-medium text-[#64748B]">{m.label}</p>
                            <p className="mt-1 truncate text-sm font-bold text-[#0F172A]">
                              {m.value}
                            </p>
                          </div>
                        ))}
                      </div>

                      {/* Live Business Events */}
                      <div className="mt-5 border-t border-[#F1F5F9] pt-4">
                        <div className="flex items-center justify-between">
                          <h3 className="text-xs font-bold uppercase tracking-wider text-[#64748B]">
                            Sự kiện kinh doanh gần đây
                          </h3>
                          <button
                            type="button"
                            onClick={() => setView("customer")}
                            className="text-xs font-semibold text-[#2563EB] hover:underline"
                          >
                            Tạo đơn thử →
                          </button>
                        </div>
                        {!overview || overview.recent_business_events.length === 0 ? (
                          <p className="mt-3 rounded-xl border border-dashed border-[#CBD5E1] bg-[#F8FAFC] p-4 text-center text-xs text-[#64748B]">
                            Chưa có sự kiện mới từ FinShop trong phiên này.
                          </p>
                        ) : (
                          <ul className="mt-3 space-y-2">
                            {overview.recent_business_events.slice(0, 6).map((ev) => (
                              <li
                                key={ev.event_id}
                                className="flex items-center justify-between gap-2 rounded-xl border border-[#F1F5F9] px-3 py-2 text-xs"
                              >
                                <div className="min-w-0">
                                  <p className="truncate font-medium text-[#0F172A]">
                                    {translateBusinessEventVi(ev.event_type)}
                                  </p>
                                  <p className="truncate font-mono text-[11px] text-[#64748B]">
                                    {ev.order_id ?? ev.event_id}
                                  </p>
                                </div>
                                <span className="shrink-0 text-[11px] text-[#94A3B8]">
                                  {formatShortTime(ev.occurred_at)}
                                </span>
                              </li>
                            ))}
                          </ul>
                        )}
                      </div>
                    </section>
                  )}

                  {/* AI Guardian Live Activity Timeline */}
                  {(section === "overview" || section === "guardian") && (
                    <section
                      className={`rounded-2xl border border-[#E2E8F0] bg-white p-5 shadow-xs ${
                        section === "guardian" ? "lg:col-span-12" : "lg:col-span-7"
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <div>
                          <h2 className="text-sm font-bold text-[#0F172A]">
                            Hoạt động AI Guardian trực tiếp
                          </h2>
                          <p className="mt-0.5 text-xs text-[#64748B]">
                            Diễn giải bằng tiếng Việt rõ ràng kèm Technical Trace có thể mở rộng.
                          </p>
                        </div>
                        <span className="rounded-full bg-[#EFF6FF] px-2.5 py-0.5 text-xs font-semibold text-[#1D4ED8]">
                          {overview?.timeline.length ?? 0} bước
                        </span>
                      </div>

                      {!overview || overview.timeline.length === 0 ? (
                        <div className="mt-4 rounded-xl border border-dashed border-[#CBD5E1] bg-[#F8FAFC] p-8 text-center">
                          <p className="text-sm font-semibold text-[#0F172A]">
                            AI Guardian đang ở trạng thái sẵn sàng quan sát
                          </p>
                          <p className="mt-1 text-xs text-[#64748B]">
                            Khi phát hiện bất thường từ FinShop hoặc khi bạn nhấn &ldquo;Quét
                            ngay&rdquo;, từng bước điều tra và kiểm chứng sẽ hiển thị tại đây.
                          </p>
                        </div>
                      ) : (
                        <ol className="mt-4 space-y-3">
                          {overview.timeline.slice(0, 12).map((item) => {
                            const isOpen = !!expandedTraces[item.id];
                            return (
                              <li
                                key={item.id}
                                className="rounded-xl border border-[#E2E8F0] bg-[#F8FAFC]/70 p-3.5"
                              >
                                <div className="flex flex-wrap items-start justify-between gap-2">
                                  <div className="min-w-0 flex-1">
                                    <p className="text-sm font-semibold text-[#0F172A]">
                                      {item.label_vi}
                                    </p>
                                    {item.detail_vi && (
                                      <p className="mt-0.5 text-xs leading-relaxed text-[#475569]">
                                        {item.detail_vi}
                                      </p>
                                    )}
                                  </div>
                                  <span className="shrink-0 text-[11px] font-medium text-[#64748B]">
                                    {formatShortTime(item.timestamp)}
                                  </span>
                                </div>

                                <div className="mt-2 flex flex-wrap items-center gap-3 text-xs">
                                  {item.case_id && (
                                    <Link
                                      href={`/cases?caseId=${encodeURIComponent(item.case_id)}`}
                                      className="font-mono font-semibold text-[#2563EB] hover:underline"
                                    >
                                      Hồ sơ {item.case_id}
                                    </Link>
                                  )}
                                  <button
                                    type="button"
                                    onClick={() => toggleTrace(item.id)}
                                    className="font-medium text-[#64748B] hover:text-[#0F172A]"
                                  >
                                    {isOpen ? "Ẩn Technical Trace ▲" : "Xem Technical Trace ▼"}
                                  </button>
                                </div>

                                {isOpen && (
                                  <pre className="mt-2.5 overflow-x-auto rounded-lg border border-[#E2E8F0] bg-white p-2.5 font-mono text-[11px] text-[#334155]">
                                    {JSON.stringify(
                                      {
                                        kind: item.kind,
                                        job_id: item.job_id,
                                        trace: item.technical_trace,
                                      },
                                      null,
                                      2,
                                    )}
                                  </pre>
                                )}
                              </li>
                            );
                          })}
                        </ol>
                      )}
                    </section>
                  )}
                </div>

                {/* Anomalies & Incidents + Approvals */}
                <div className="grid gap-6 lg:grid-cols-12">
                  {(section === "overview" || section === "incidents") && (
                    <section
                      className={`rounded-2xl border border-[#E2E8F0] bg-white p-5 shadow-xs ${
                        section === "incidents" ? "lg:col-span-12" : "lg:col-span-7"
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <div>
                          <h2 className="text-sm font-bold text-[#0F172A]">
                            Bất thường & Sự cố phát hiện
                          </h2>
                          <p className="mt-0.5 text-xs text-[#64748B]">
                            Nhấp vào từng dòng để xem bằng chứng và kết quả kiểm chứng chi tiết.
                          </p>
                        </div>
                        <Link
                          href="/cases"
                          className="text-xs font-semibold text-[#2563EB] hover:underline"
                        >
                          Tất cả hồ sơ →
                        </Link>
                      </div>

                      {!overview || overview.anomalies.length === 0 ? (
                        <div className="mt-4 rounded-xl border border-dashed border-[#CBD5E1] bg-[#F8FAFC] p-6 text-center text-xs text-[#64748B]">
                          Không có bất thường nào được ghi nhận trong phiên hiện tại.
                        </div>
                      ) : (
                        <div className="mt-4 space-y-2.5">
                          {overview.anomalies.map((a) => (
                            <button
                              key={a.id}
                              type="button"
                              onClick={() => setSelectedAnomaly(a)}
                              className="flex w-full flex-wrap items-center justify-between gap-3 rounded-xl border border-[#E2E8F0] bg-white p-3.5 text-left transition hover:border-[#93C5FD] hover:bg-[#EFF6FF]/40"
                            >
                              <div className="min-w-0 flex-1">
                                <div className="flex flex-wrap items-center gap-2">
                                  <span className="text-sm font-semibold text-[#0F172A]">
                                    {a.anomaly_type}
                                  </span>
                                  <span
                                    className={`rounded-full border px-2 py-0.5 text-[11px] font-semibold ${severityPill(
                                      a.severity,
                                    )}`}
                                  >
                                    {a.severity}
                                  </span>
                                  <span
                                    className={`rounded-full border px-2 py-0.5 text-[11px] font-semibold ${lifecyclePill(
                                      a.lifecycle_state,
                                    )}`}
                                  >
                                    {a.lifecycle_state}
                                  </span>
                                </div>
                                <p className="mt-1 line-clamp-1 text-xs text-[#64748B]">
                                  {a.rationale}
                                </p>
                              </div>
                              <div className="text-right text-xs text-[#64748B]">
                                <p className="font-mono">{a.affected_order_id ?? a.detector_code}</p>
                                <p className="mt-0.5 text-[11px]">
                                  {formatShortTime(a.last_observed_at)}
                                </p>
                              </div>
                            </button>
                          ))}
                        </div>
                      )}
                    </section>
                  )}

                  {(section === "overview" || section === "approvals") && (
                    <section
                      className={`rounded-2xl border border-[#E2E8F0] bg-white p-5 shadow-xs ${
                        section === "approvals" ? "lg:col-span-12" : "lg:col-span-5"
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <div>
                          <h2 className="text-sm font-bold text-[#0F172A]">
                            Chờ phê duyệt & Lệnh phục hồi
                          </h2>
                          <p className="mt-0.5 text-xs text-[#64748B]">
                            Mọi hành động rủi ro cao đều đi qua Policy Engine & phê duyệt người dùng.
                          </p>
                        </div>
                        <Link
                          href="/risks"
                          className="text-xs font-semibold text-[#2563EB] hover:underline"
                        >
                          Rủi ro →
                        </Link>
                      </div>

                      {overview && overview.approvals.length > 0 ? (
                        <ul className="mt-4 space-y-2.5">
                          {overview.approvals.map((p) => (
                            <li
                              key={p.proposal_id}
                              className="rounded-xl border border-[#FDE68A] bg-[#FFFBEB]/60 p-3.5 text-xs"
                            >
                              <div className="flex items-center justify-between gap-2">
                                <span className="font-bold text-[#92400E]">{p.action_type}</span>
                                <span className="rounded-full bg-white px-2 py-0.5 font-semibold text-[#B45309]">
                                  {p.risk_level}
                                </span>
                              </div>
                              <p className="mt-1 font-mono text-[11px] text-[#78350F]">
                                Case: {p.case_id}
                              </p>
                              <Link
                                href={`/cases?caseId=${encodeURIComponent(p.case_id)}`}
                                className="mt-2 inline-block font-semibold text-[#2563EB] hover:underline"
                              >
                                Xem & Phê duyệt tại Hồ sơ →
                              </Link>
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <div className="mt-4 rounded-xl border border-dashed border-[#CBD5E1] bg-[#F8FAFC] p-5 text-center text-xs text-[#64748B]">
                          Không có đề xuất nào đang chờ bạn phê duyệt.
                        </div>
                      )}

                      {overview && overview.recovery_commands.length > 0 && (
                        <div className="mt-5 border-t border-[#F1F5F9] pt-4">
                          <h3 className="text-xs font-bold uppercase tracking-wider text-[#64748B]">
                            Lệnh phục hồi có chữ ký ({overview.recovery_commands.length})
                          </h3>
                          <ul className="mt-2.5 space-y-2">
                            {overview.recovery_commands.slice(0, 5).map((cmd) => (
                              <li
                                key={cmd.id}
                                className="flex items-center justify-between gap-2 rounded-xl border border-[#F1F5F9] bg-[#F8FAFC] px-3 py-2 text-xs"
                              >
                                <div className="min-w-0">
                                  <p className="truncate font-semibold text-[#0F172A]">
                                    {cmd.command}
                                  </p>
                                  <p className="font-mono text-[11px] text-[#64748B]">
                                    {cmd.target_order_id ?? cmd.id} · {cmd.policy_decision}
                                  </p>
                                </div>
                                <span className="shrink-0 rounded-full border border-[#E2E8F0] bg-white px-2 py-0.5 text-[11px] font-semibold text-[#334155]">
                                  {cmd.state}
                                </span>
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </section>
                  )}
                </div>
              </>
            )}
          </main>
        </div>
      </div>
    </div>
  );
}

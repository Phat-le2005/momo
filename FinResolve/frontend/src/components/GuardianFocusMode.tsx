"use client";

import { useState } from "react";
import { GuardianBadge, type GuardianVisualState } from "@/components/GuardianBadge";
import { LivingBusinessNetwork } from "@/components/LivingBusinessNetwork";
import {
  formatMetricOrEmpty,
  translateBusinessEventVi,
  type ExperienceAnomaly,
  type ExperienceOverviewResponse,
} from "@/lib/experience-api";

export interface GuardianFocusModeProps {
  overview: ExperienceOverviewResponse | null;
  visualState: GuardianVisualState;
  storefrontUrl: string;
  isReadOnlyPreview: boolean;
  scanning: boolean;
  onScanNow: () => Promise<void>;
  onSelectAnomaly: (anomaly: ExperienceAnomaly) => void;
  onOpenOperatorModal: () => void;
  onExit: () => void;
}

function humanizeStoryLabel(kind: string, fallback: string, trace: Record<string, unknown>): string {
  const tool = String(trace?.tool_name ?? "").toLowerCase();
  if (tool === "get_payment_record" || tool === "find_payment_candidates") {
    return "Đang xác minh giao dịch thanh toán của đơn hàng.";
  }
  if (tool === "get_order") {
    return "Đang kiểm tra trạng thái và phiên bản hiện tại của đơn hàng.";
  }
  if (tool === "get_policy") {
    return "Đang đối chiếu chính sách xử lý an toàn từ Policy RAG.";
  }
  if (tool === "get_refund_record") {
    return "Đang kiểm tra lịch sử hoàn tiền để tránh hoàn trùng.";
  }
  return fallback || kind;
}

/**
 * Guardian Live Experience — Dedicated Full-Screen Focus Mode optimized for judge presentations.
 * Hides navigation chrome, centers the Living Business Network, live MiniStore stream,
 * AI Investigation storytelling, and verified before/after recovery results.
 */
export function GuardianFocusMode({
  overview,
  visualState,
  storefrontUrl,
  isReadOnlyPreview,
  scanning,
  onScanNow,
  onSelectAnomaly,
  onOpenOperatorModal,
  onExit,
}: GuardianFocusModeProps) {
  const [sandboxDrawerOpen, setSandboxDrawerOpen] = useState(false);

  const verifiedList = (overview?.anomalies ?? []).filter(
    (a) => a.lifecycle_state === "RECOVERED" && a.verification_verdict === "VERIFIED_RECOVERY",
  );
  const activeList = (overview?.anomalies ?? []).filter((a) =>
    ["OBSERVED", "INVESTIGATING", "RECOVERY_PENDING"].includes(a.lifecycle_state),
  );

  return (
    <div
      role="region"
      aria-label="Guardian Live Experience — Chế độ trình diễn tập trung"
      className="fixed inset-0 z-50 flex flex-col overflow-y-auto bg-[#F8FAFF] text-[#0B1739]"
    >
      {/* Presentation Top Bar */}
      <header className="sticky top-0 z-20 flex flex-wrap items-center justify-between gap-3 border-b border-[#DBEAFE] bg-white/95 px-5 py-3 backdrop-blur-md">
        <div className="flex flex-wrap items-center gap-3">
          <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-gradient-to-br from-[#2563EB] to-[#0284C7] text-xs font-bold text-white">
            FR
          </span>
          <div>
            <p className="text-sm font-bold tracking-tight text-[#0B1739]">
              Guardian Live Experience
            </p>
            <p className="text-[11px] text-[#64748B]">
              Chế độ trình diễn toàn màn hình · Dữ liệu thật từ Backend (LOCAL_MOCK)
            </p>
          </div>
          <GuardianBadge state={visualState} />
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <a
            href={storefrontUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="rounded-xl border border-[#BFDBFE] bg-[#EAF3FF] px-3.5 py-2 text-xs font-semibold text-[#1D4ED8] transition hover:bg-[#DBEAFE]"
          >
            Mở FinShop LIVE ↗
          </a>
          <button
            type="button"
            onClick={() => setSandboxDrawerOpen((v) => !v)}
            className="rounded-xl border border-[#E2E8F0] bg-white px-3 py-2 text-xs font-semibold text-[#334155] transition hover:bg-[#F8FAFF]"
          >
            {sandboxDrawerOpen ? "Ẩn bảng Sandbox" : "Điều khiển Sandbox"}
          </button>
          {isReadOnlyPreview ? (
            <button
              type="button"
              onClick={onOpenOperatorModal}
              className="rounded-xl border border-[#FDE68A] bg-[#FFFBEB] px-3 py-2 text-xs font-semibold text-[#B45309] hover:bg-[#FEF3C7]"
            >
              Kích hoạt Operator
            </button>
          ) : (
            <button
              type="button"
              disabled={scanning}
              onClick={() => void onScanNow()}
              className="rounded-xl bg-[#2563EB] px-3.5 py-2 text-xs font-semibold text-white transition hover:bg-[#1D4ED8] disabled:opacity-50"
            >
              {scanning ? "Đang quét..." : "Quét ngay"}
            </button>
          )}
          <button
            type="button"
            onClick={onExit}
            className="rounded-xl border border-[#CBD5E1] bg-white px-3.5 py-2 text-xs font-semibold text-[#0B1739] transition hover:bg-[#F1F5F9]"
          >
            Thoát Focus Mode ✕
          </button>
        </div>
      </header>

      {/* Collapsible Authorized Sandbox Controls */}
      {sandboxDrawerOpen && (
        <div className="border-b border-[#DBEAFE] bg-[#EAF3FF]/60 px-6 py-3">
          <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-4 text-xs">
            <div className="flex flex-wrap items-center gap-5">
              <span>
                <strong className="text-[#0B1739]">Phiên:</strong>{" "}
                <span className="font-mono">{overview?.session_id ?? "—"}</span>
              </span>
              <span>
                <strong className="text-[#0B1739]">World Epoch:</strong> #
                {overview?.world_epoch ?? 1}
              </span>
              <span>
                <strong className="text-[#0B1739]">RPS:</strong>{" "}
                {formatMetricOrEmpty(overview?.business_health.rps, " req/s", 2)}
              </span>
              <span>
                <strong className="text-[#0B1739]">P95:</strong>{" "}
                {formatMetricOrEmpty(overview?.business_health.p95_latency_ms, " ms", 0)}
              </span>
              <span>
                <strong className="text-[#0B1739]">Outbox Lag:</strong>{" "}
                {formatMetricOrEmpty(overview?.business_health.outbox_lag)}
              </span>
            </div>
            <span className="font-medium text-[#1D4ED8]">
              Khóa Chaos Admin được bảo vệ hoàn toàn tại Backend · VNPAY đang tắt
            </span>
          </div>
        </div>
      )}

      {/* Main Presentation Stage */}
      <div className="mx-auto grid w-full max-w-7xl flex-1 gap-6 px-5 py-6 lg:grid-cols-12">
        {/* Left 7 cols: Living Business Network + MiniStore Live Stream */}
        <div className="space-y-5 lg:col-span-7">
          <LivingBusinessNetwork
            overview={overview}
            visualState={visualState}
            onSelectAnomaly={onSelectAnomaly}
          />

          {/* Compact Live MiniStore Stream */}
          <section className="rounded-2xl border border-[#E2E8F0] bg-white p-4 shadow-xs">
            <div className="flex items-center justify-between gap-2">
              <h3 className="text-xs font-bold uppercase tracking-wider text-[#64748B]">
                Hoạt động MiniStore thời gian thực
              </h3>
              <span className="font-mono text-xs text-[#2563EB]">
                Đơn: {formatMetricOrEmpty(overview?.kpis.orders_observed)} · TT:{" "}
                {formatMetricOrEmpty(overview?.kpis.payments_observed)}
              </span>
            </div>
            {!overview || overview.recent_business_events.length === 0 ? (
              <p className="mt-2.5 text-xs text-[#64748B]">
                Chưa có sự kiện mới từ FinShop. Hãy mở FinShop LIVE để tạo đơn hàng thực tế.
              </p>
            ) : (
              <div className="mt-3 grid gap-2 sm:grid-cols-2">
                {overview.recent_business_events.slice(0, 4).map((ev) => (
                  <div
                    key={ev.event_id}
                    className="rounded-xl border border-[#F1F5F9] bg-[#F8FAFF] px-3 py-2 text-xs"
                  >
                    <p className="truncate font-semibold text-[#0B1739]">
                      {translateBusinessEventVi(ev.event_type)}
                    </p>
                    <p className="mt-0.5 truncate font-mono text-[11px] text-[#64748B]">
                      {ev.order_id ?? ev.event_id}
                    </p>
                  </div>
                ))}
              </div>
            )}
          </section>
        </div>

        {/* Right 5 cols: Before/After Verification + AI Investigation Storytelling */}
        <div className="space-y-5 lg:col-span-5">
          {/* Before & After Verified Outcome Card */}
          <section className="rounded-3xl border border-[#DBEAFE] bg-white p-5 shadow-xs">
            <h3 className="text-sm font-bold text-[#0B1739]">Kết quả Trước &amp; Sau xử lý</h3>
            <div className="mt-3 grid grid-cols-2 gap-3">
              <div className="rounded-2xl border border-[#FEF2F2] bg-[#FEF2F2]/60 p-3.5">
                <p className="text-[11px] font-semibold text-[#B91C1C]">Bất thường đang mở</p>
                <p className="mt-1 text-2xl font-bold text-[#B91C1C]">{activeList.length}</p>
              </div>
              <div className="rounded-2xl border border-[#A7F3D0] bg-[#ECFDF5]/70 p-3.5">
                <p className="text-[11px] font-semibold text-[#047857]">Đã xác minh phục hồi</p>
                <p className="mt-1 text-2xl font-bold text-[#047857]">{verifiedList.length}</p>
              </div>
            </div>
            {verifiedList[0] && (
              <button
                type="button"
                onClick={() => onSelectAnomaly(verifiedList[0])}
                className="mt-3 w-full rounded-xl border border-[#A7F3D0] bg-[#ECFDF5] px-3.5 py-2.5 text-left text-xs text-[#065F46] transition hover:bg-[#D1FAE5]"
              >
                <span className="font-bold">Phục hồi gần nhất:</span>{" "}
                {verifiedList[0].affected_order_id ?? verifiedList[0].anomaly_type} →{" "}
                <span className="font-mono font-bold">VERIFIED_RECOVERY</span>
              </button>
            )}
          </section>

          {/* Live AI Investigation Story Feed */}
          <section className="rounded-3xl border border-[#E2E8F0] bg-white p-5 shadow-xs">
            <div className="flex items-center justify-between gap-2">
              <h3 className="text-sm font-bold text-[#0B1739]">Diễn biến điều tra AI Guardian</h3>
              <span className="rounded-full bg-[#EAF3FF] px-2.5 py-0.5 text-[11px] font-semibold text-[#1D4ED8]">
                {overview?.timeline.length ?? 0} sự kiện
              </span>
            </div>
            {!overview || overview.timeline.length === 0 ? (
              <p className="mt-4 rounded-2xl border border-dashed border-[#CBD5E1] bg-[#F8FAFF] p-5 text-center text-xs text-[#64748B]">
                AI Guardian đang sẵn sàng. Mọi bước thu thập bằng chứng và xác minh từ backend sẽ
                hiển thị trực tiếp tại đây.
              </p>
            ) : (
              <ol className="mt-4 space-y-2.5">
                {overview.timeline.slice(0, 7).map((item) => (
                  <li
                    key={item.id}
                    className="rounded-2xl border border-[#EFF6FF] bg-[#F8FAFF] p-3 text-xs"
                  >
                    <p className="font-semibold text-[#0B1739]">
                      {humanizeStoryLabel(item.kind, item.label_vi, item.technical_trace)}
                    </p>
                    {item.detail_vi && (
                      <p className="mt-0.5 line-clamp-2 text-[#475569]">{item.detail_vi}</p>
                    )}
                  </li>
                ))}
              </ol>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}

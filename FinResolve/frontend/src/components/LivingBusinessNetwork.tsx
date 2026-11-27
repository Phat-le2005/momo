"use client";

import { useMemo, useState } from "react";
import type { GuardianVisualState } from "@/components/GuardianBadge";
import { GuardianCore } from "@/components/GuardianCore";
import type {
  ExperienceAnomaly,
  ExperienceOverviewResponse,
} from "@/lib/experience-api";

type NetworkNodeId =
  | "customer"
  | "orders"
  | "payment"
  | "refund"
  | "inventory"
  | "outbox"
  | "policy";

type NodeRuntimeStatus = "normal" | "active_signal" | "investigating" | "anomaly" | "verified";

interface NetworkNodeMeta {
  id: NetworkNodeId;
  labelVi: string;
  labelEn: string;
  x: number;
  y: number;
  status: NodeRuntimeStatus;
  metricText: string;
  detailVi: string;
  linkedAnomaly: ExperienceAnomaly | null;
}

const NODE_COORDS: Record<NetworkNodeId, { x: number; y: number; labelVi: string; labelEn: string }> = {
  customer: { x: 115, y: 78, labelVi: "Khách hàng", labelEn: "Customer" },
  orders: { x: 340, y: 46, labelVi: "Đơn hàng", labelEn: "Orders" },
  payment: { x: 565, y: 78, labelVi: "Thanh toán", labelEn: "Payment" },
  refund: { x: 605, y: 225, labelVi: "Hoàn tiền", labelEn: "Refund" },
  inventory: { x: 515, y: 345, labelVi: "Kho hàng", labelEn: "Inventory" },
  outbox: { x: 165, y: 345, labelVi: "Sự kiện Outbox", labelEn: "Event Outbox" },
  policy: { x: 75, y: 225, labelVi: "Policy Engine", labelEn: "Policy" },
};

const CENTER_X = 340;
const CENTER_Y = 205;

function statusStyles(status: NodeRuntimeStatus): {
  stroke: string;
  pillBg: string;
  pillText: string;
  dot: string;
  statusLabelVi: string;
} {
  switch (status) {
    case "anomaly":
      return {
        stroke: "#EF4444",
        pillBg: "bg-[#FEF2F2] border-[#FECACA]",
        pillText: "text-[#B91C1C]",
        dot: "bg-[#EF4444]",
        statusLabelVi: "Bất thường",
      };
    case "investigating":
      return {
        stroke: "#0284C7",
        pillBg: "bg-[#E0F2FE] border-[#7DD3FC]",
        pillText: "text-[#0369A1]",
        dot: "bg-[#0284C7]",
        statusLabelVi: "Đang đối chiếu",
      };
    case "verified":
      return {
        stroke: "#10B981",
        pillBg: "bg-[#ECFDF5] border-[#A7F3D0]",
        pillText: "text-[#047857]",
        dot: "bg-[#10B981]",
        statusLabelVi: "Đã xác minh",
      };
    case "active_signal":
      return {
        stroke: "#2563EB",
        pillBg: "bg-[#EAF3FF] border-[#BFDBFE]",
        pillText: "text-[#1D4ED8]",
        dot: "bg-[#2563EB]",
        statusLabelVi: "Có tín hiệu",
      };
    default:
      return {
        stroke: "#CBD5E1",
        pillBg: "bg-white border-[#E2E8F0]",
        pillText: "text-[#334155]",
        dot: "bg-[#94A3B8]",
        statusLabelVi: "Ổn định",
      };
  }
}

export interface LivingBusinessNetworkProps {
  overview: ExperienceOverviewResponse | null;
  visualState: GuardianVisualState;
  onSelectAnomaly?: (anomaly: ExperienceAnomaly) => void;
  compact?: boolean;
}

/**
 * Living Business Network — Hero interactive topology map of FinResolve AI.
 * Strictly driven by real backend observations, events, anomalies, and tool reads.
 * Includes accessible table toggle for screen readers and structured inspection.
 */
export function LivingBusinessNetwork({
  overview,
  visualState,
  onSelectAnomaly,
  compact = false,
}: LivingBusinessNetworkProps) {
  const [displayMode, setDisplayMode] = useState<"map" | "table">("map");
  const [hoveredNode, setHoveredNode] = useState<NetworkNodeId | null>(null);

  const nodes = useMemo<NetworkNodeMeta[]>(() => {
    const anomalies = overview?.anomalies ?? [];
    const activeAnoms = anomalies.filter((a) =>
      ["OBSERVED", "INVESTIGATING", "RECOVERY_PENDING"].includes(a.lifecycle_state),
    );
    const verifiedAnoms = anomalies.filter(
      (a) => a.lifecycle_state === "RECOVERED" && a.verification_verdict === "VERIFIED_RECOVERY",
    );
    const events = overview?.recent_business_events ?? [];
    const timeline = overview?.timeline ?? [];
    const cmds = overview?.recovery_commands ?? [];

    // Inspect real tool reads from timeline (never fabricate LLM thoughts)
    const recentToolNames = new Set(
      timeline
        .slice(0, 12)
        .map((t) => String((t.technical_trace?.tool_name as string | undefined) ?? "").toLowerCase())
        .filter(Boolean),
    );

    const findAnomalyFor = (pred: (a: ExperienceAnomaly) => boolean): ExperienceAnomaly | null =>
      activeAnoms.find(pred) ?? null;

    const orderAnom = findAnomalyFor(
      (a) => Boolean(a.affected_order_id) || a.detector_code === "D01" || a.detector_code === "D03",
    );
    const paymentAnom = findAnomalyFor(
      (a) => Boolean(a.affected_payment_id) || a.detector_code === "D01" || a.detector_code === "D02",
    );
    const refundAnom = findAnomalyFor(
      (a) => a.anomaly_type.includes("REFUND") || a.detector_code === "D04",
    );
    const inventoryAnom = findAnomalyFor(
      (a) => a.anomaly_type.includes("INVENTORY") || a.detector_code === "D05",
    );
    const outboxAnom = findAnomalyFor(
      (a) => a.anomaly_type.includes("OUTBOX") || a.detector_code === "D06",
    );
    const customerAnom = findAnomalyFor(
      (a) => a.anomaly_type.includes("RECEIPT") || a.anomaly_type.includes("COMPLAINT"),
    );

    const hasOrderEvent = events.some((e) => e.event_type.includes("ORDER"));
    const hasPayEvent = events.some((e) => e.event_type.includes("PAYMENT"));
    const hasRefundEvent = events.some((e) => e.event_type.includes("REFUND"));
    const hasCustEvent = events.some((e) => e.event_type.includes("COMPLAINT") || e.event_type.includes("RECEIPT"));

    const resolveStatus = (
      anom: ExperienceAnomaly | null,
      isInvestigatingTool: boolean,
      hasSignal: boolean,
      isVerified: boolean,
    ): NodeRuntimeStatus => {
      if (anom) return "anomaly";
      if (isInvestigatingTool && visualState === "INVESTIGATING") return "investigating";
      if (isVerified) return "verified";
      if (hasSignal) return "active_signal";
      return "normal";
    };

    const ordersObs = overview?.kpis.orders_observed;
    const paymentsObs = overview?.kpis.payments_observed;
    const outboxLag = overview?.business_health.outbox_lag;
    const verifiedCount = verifiedAnoms.length;

    return [
      {
        id: "customer",
        ...NODE_COORDS.customer,
        status: resolveStatus(
          customerAnom,
          recentToolNames.has("get_case_evidence"),
          hasCustEvent || events.length > 0,
          false,
        ),
        metricText: hasCustEvent ? "Có khiếu nại / biên lai" : `${events.length} sự kiện gần đây`,
        detailVi: customerAnom
          ? customerAnom.rationale
          : "Ghi nhận hành vi mua hàng, khiếu nại và biên lai từ FinShop LIVE.",
        linkedAnomaly: customerAnom,
      },
      {
        id: "orders",
        ...NODE_COORDS.orders,
        status: resolveStatus(
          orderAnom,
          recentToolNames.has("get_order"),
          hasOrderEvent,
          verifiedCount > 0 && !orderAnom,
        ),
        metricText: ordersObs !== null && ordersObs !== undefined ? `${ordersObs} đơn quan sát` : "Chưa có mẫu",
        detailVi: orderAnom
          ? orderAnom.rationale
          : "Theo dõi vòng đời đơn hàng và phiên bản đồng bộ lạc quan (optimistic version).",
        linkedAnomaly: orderAnom,
      },
      {
        id: "payment",
        ...NODE_COORDS.payment,
        status: resolveStatus(
          paymentAnom,
          recentToolNames.has("get_payment_record") || recentToolNames.has("find_payment_candidates"),
          hasPayEvent,
          verifiedCount > 0 && !paymentAnom,
        ),
        metricText:
          paymentsObs !== null && paymentsObs !== undefined
            ? `${paymentsObs} giao dịch`
            : "Chưa có mẫu",
        detailVi: paymentAnom
          ? paymentAnom.rationale
          : "Đối chiếu trạng thái thanh toán xác thực LOCAL_MOCK với trạng thái đơn hàng.",
        linkedAnomaly: paymentAnom,
      },
      {
        id: "refund",
        ...NODE_COORDS.refund,
        status: resolveStatus(
          refundAnom,
          recentToolNames.has("get_refund_record"),
          hasRefundEvent,
          false,
        ),
        metricText: hasRefundEvent ? "Có yêu cầu hoàn tiền" : "Không có tồn đọng",
        detailVi: refundAnom
          ? refundAnom.rationale
          : "Kiểm soát hoàn tiền trùng lặp và hạn mức hoàn trả an toàn.",
        linkedAnomaly: refundAnom,
      },
      {
        id: "inventory",
        ...NODE_COORDS.inventory,
        status: resolveStatus(inventoryAnom, false, hasOrderEvent, false),
        metricText: inventoryAnom ? "Cảnh báo tồn kho" : "Đồng bộ ổn định",
        detailVi: inventoryAnom
          ? inventoryAnom.rationale
          : "Giám sát giữ chỗ tồn kho và phát hiện bất thường trừ kho.",
        linkedAnomaly: inventoryAnom,
      },
      {
        id: "outbox",
        ...NODE_COORDS.outbox,
        status: resolveStatus(
          outboxAnom,
          false,
          typeof outboxLag === "number" && outboxLag > 0,
          false,
        ),
        metricText:
          outboxLag !== null && outboxLag !== undefined ? `Lag: ${outboxLag}` : "Chưa có telemetry",
        detailVi: outboxAnom
          ? outboxAnom.rationale
          : "Theo dõi độ trễ hàng đợi sự kiện bất đồng bộ giữa Commerce và Monitor.",
        linkedAnomaly: outboxAnom,
      },
      {
        id: "policy",
        ...NODE_COORDS.policy,
        status: resolveStatus(
          null,
          recentToolNames.has("get_policy") || visualState === "RECOVERING",
          cmds.length > 0,
          cmds.some((c) => c.state === "VERIFIED"),
        ),
        metricText: `${cmds.length} lệnh đã thẩm định`,
        detailVi:
          "Thẩm định điều kiện an toàn, ký HMAC-SHA256 và chặn mọi hành động thiếu bằng chứng.",
        linkedAnomaly: null,
      },
    ];
  }, [overview, visualState]);

  const effectiveCoreState: GuardianVisualState =
    visualState === "MONITORING" && nodes.some((n) => n.status === "anomaly")
      ? "ANOMALY_DETECTED"
      : visualState;

  const activeHoverNode = hoveredNode ? nodes.find((n) => n.id === hoveredNode) ?? null : null;

  return (
    <section
      aria-label="Bản đồ mạng lưới vận hành trực tiếp (Living Business Network)"
      className="relative overflow-hidden rounded-3xl border border-[#DBEAFE] bg-white shadow-sm shadow-blue-950/5"
    >
      {/* Header bar */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[#EFF6FF] bg-gradient-to-r from-[#F8FAFF] via-white to-[#F8FAFF] px-5 py-3.5">
        <div>
          <div className="flex items-center gap-2">
            <span className="h-2 w-2 rounded-full bg-[#2563EB]" />
            <h2 className="text-sm font-bold tracking-tight text-[#0B1739]">
              Living Business Network
            </h2>
            <span className="rounded-full border border-[#DBEAFE] bg-[#EAF3FF] px-2 py-0.5 text-[11px] font-semibold text-[#1D4ED8]">
              Dữ liệu thời gian thực
            </span>
          </div>
          <p className="mt-0.5 text-xs text-[#64748B]">
            Nhấn vào nút đang báo bất thường để mở chi tiết sự cố và bằng chứng điều tra.
          </p>
        </div>

        <div
          role="tablist"
          aria-label="Chế độ hiển thị mạng lưới"
          className="inline-flex rounded-xl border border-[#E2E8F0] bg-[#F8FAFF] p-0.5 text-xs font-semibold"
        >
          <button
            type="button"
            role="tab"
            aria-selected={displayMode === "map"}
            onClick={() => setDisplayMode("map")}
            className={`rounded-lg px-3 py-1 transition ${
              displayMode === "map"
                ? "bg-white text-[#2563EB] shadow-xs"
                : "text-[#64748B] hover:text-[#0B1739]"
            }`}
          >
            Bản đồ mạng
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={displayMode === "table"}
            onClick={() => setDisplayMode("table")}
            className={`rounded-lg px-3 py-1 transition ${
              displayMode === "table"
                ? "bg-white text-[#2563EB] shadow-xs"
                : "text-[#64748B] hover:text-[#0B1739]"
            }`}
          >
            Bảng dữ liệu
          </button>
        </div>
      </div>

      {displayMode === "table" ? (
        /* Accessible Tabular Alternative */
        <div className="overflow-x-auto p-4">
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="border-b border-[#E2E8F0] text-[11px] font-semibold uppercase tracking-wider text-[#64748B]">
                <th className="py-2.5 pr-3">Thành phần nghiệp vụ</th>
                <th className="py-2.5 pr-3">Trạng thái</th>
                <th className="py-2.5 pr-3">Quan sát thực tế</th>
                <th className="py-2.5">Chi tiết</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#F1F5F9]">
              {nodes.map((node) => {
                const st = statusStyles(node.status);
                return (
                  <tr key={node.id} className="hover:bg-[#F8FAFF]">
                    <td className="py-3 pr-3 font-semibold text-[#0B1739]">
                      {node.labelVi}{" "}
                      <span className="font-normal text-[#94A3B8]">({node.labelEn})</span>
                    </td>
                    <td className="py-3 pr-3">
                      <span
                        className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-semibold ${st.pillBg} ${st.pillText}`}
                      >
                        <span className={`h-1.5 w-1.5 rounded-full ${st.dot}`} />
                        {st.statusLabelVi}
                      </span>
                    </td>
                    <td className="py-3 pr-3 font-mono text-[#334155]">{node.metricText}</td>
                    <td className="py-3 text-[#475569]">
                      <div className="flex items-center justify-between gap-2">
                        <span>{node.detailVi}</span>
                        {node.linkedAnomaly && onSelectAnomaly && (
                          <button
                            type="button"
                            onClick={() => onSelectAnomaly(node.linkedAnomaly!)}
                            className="shrink-0 rounded-lg bg-[#FEF2F2] px-2.5 py-1 text-[11px] font-semibold text-[#B91C1C] hover:bg-[#FEE2E2]"
                          >
                            Mở sự cố →
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        /* Interactive Living SVG + HTML Node Map */
        <div className={`fr-grid-surface relative px-3 py-4 sm:px-5 ${compact ? "min-h-[340px]" : "min-h-[410px]"}`}>
          <div className="relative mx-auto w-full max-w-[680px]" style={{ aspectRatio: "680 / 400" }}>
            {/* Connection Lines SVG Layer */}
            <svg
              viewBox="0 0 680 400"
              className="pointer-events-none absolute inset-0 h-full w-full"
              aria-hidden="true"
            >
              {nodes.map((node) => {
                const st = statusStyles(node.status);
                const isAnimated =
                  node.status === "anomaly" ||
                  node.status === "investigating" ||
                  node.status === "active_signal" ||
                  node.status === "verified";
                const isFast = node.status === "anomaly" || node.status === "investigating";
                return (
                  <g key={node.id}>
                    <line
                      x1={CENTER_X}
                      y1={CENTER_Y}
                      x2={node.x}
                      y2={node.y}
                      stroke={st.stroke}
                      strokeOpacity={node.status === "normal" ? 0.45 : 0.32}
                      strokeWidth={node.status === "anomaly" ? 2.6 : 1.75}
                    />
                    {isAnimated && (
                      <line
                        x1={node.x}
                        y1={node.y}
                        x2={CENTER_X}
                        y2={CENTER_Y}
                        stroke={st.stroke}
                        strokeWidth={node.status === "anomaly" ? 2.6 : 2}
                        strokeLinecap="round"
                        className={isFast ? "fr-signal-line-fast" : "fr-signal-line"}
                      />
                    )}
                  </g>
                );
              })}
            </svg>

            {/* Center Guardian Core */}
            <div
              className="absolute z-10 -translate-x-1/2 -translate-y-1/2"
              style={{ left: "50%", top: "51.25%" }}
            >
              <GuardianCore state={effectiveCoreState} size={compact ? "sm" : "md"} />
            </div>

            {/* 7 Orbiting Business Nodes */}
            {nodes.map((node) => {
              const st = statusStyles(node.status);
              const leftPct = `${(node.x / 680) * 100}%`;
              const topPct = `${(node.y / 400) * 100}%`;
              const clickable = Boolean(node.linkedAnomaly && onSelectAnomaly);

              return (
                <button
                  key={node.id}
                  type="button"
                  onMouseEnter={() => setHoveredNode(node.id)}
                  onMouseLeave={() => setHoveredNode(null)}
                  onFocus={() => setHoveredNode(node.id)}
                  onBlur={() => setHoveredNode(null)}
                  onClick={() => {
                    if (node.linkedAnomaly && onSelectAnomaly) {
                      onSelectAnomaly(node.linkedAnomaly);
                    }
                  }}
                  style={{ left: leftPct, top: topPct }}
                  className={`group absolute z-20 -translate-x-1/2 -translate-y-1/2 rounded-2xl border px-3 py-2 text-left shadow-sm transition-all duration-200 ${
                    st.pillBg
                  } ${
                    clickable
                      ? "cursor-pointer ring-2 ring-[#EF4444]/30 hover:scale-105 hover:shadow-md"
                      : "cursor-default hover:border-[#93C5FD] hover:shadow"
                  }`}
                  aria-label={`${node.labelVi}: ${st.statusLabelVi}, ${node.metricText}`}
                >
                  <div className="flex items-center gap-1.5">
                    <span
                      className={`h-2 w-2 shrink-0 rounded-full ${st.dot} ${
                        node.status === "anomaly" || node.status === "investigating"
                          ? "fr-pulse-soft"
                          : ""
                      }`}
                    />
                    <span className="whitespace-nowrap text-[11px] font-bold text-[#0B1739] sm:text-xs">
                      {node.labelVi}
                    </span>
                  </div>
                  <p className={`mt-0.5 whitespace-nowrap font-mono text-[10px] font-medium ${st.pillText}`}>
                    {node.metricText}
                  </p>
                </button>
              );
            })}
          </div>

          {/* Contextual Bottom Inspector Bar */}
          <div className="mt-3 flex flex-wrap items-center justify-between gap-2 rounded-2xl border border-[#E2E8F0] bg-white/95 px-4 py-2.5 text-xs">
            {activeHoverNode ? (
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-bold text-[#0B1739]">{activeHoverNode.labelVi}:</span>
                <span className="text-[#475569]">{activeHoverNode.detailVi}</span>
              </div>
            ) : (
              <div className="flex flex-wrap items-center gap-4 text-[11px] text-[#64748B]">
                <span className="inline-flex items-center gap-1.5">
                  <span className="h-2 w-2 rounded-full bg-[#2563EB]" /> Tín hiệu thực
                </span>
                <span className="inline-flex items-center gap-1.5">
                  <span className="h-2 w-2 rounded-full bg-[#0284C7]" /> AI đang đối chiếu
                </span>
                <span className="inline-flex items-center gap-1.5">
                  <span className="h-2 w-2 rounded-full bg-[#EF4444]" /> Bất thường (nhấn để xem)
                </span>
                <span className="inline-flex items-center gap-1.5">
                  <span className="h-2 w-2 rounded-full bg-[#10B981]" /> Đã xác minh phục hồi
                </span>
              </div>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

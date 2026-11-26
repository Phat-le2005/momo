/** Pure, dependency-free helpers for Unified Demo Experience polling, formatting, and notifications. */

export type GuardianVisualStateValue =
  | "MONITORING"
  | "INVESTIGATING"
  | "RECOVERING"
  | "WAITING_APPROVAL"
  | "VERIFIED"
  | "DISCONNECTED";

export type OverviewToastInput = {
  session_id: string;
  world_epoch: number;
  updated_at: string;
  kpis: {
    total_anomalies_detected: number;
    verified_recoveries: number;
  };
};

export type ExperienceToast = {
  id: string;
  tone: "info" | "success" | "warning";
  title: string;
  detail: string;
};

/** Adaptive poll interval:
 * - Active investigation / recovery: 4s
 * - Calm monitoring: 10s
 * - Consecutive network errors: exponential backoff capped at 30s
 */
export function computeAdaptivePollIntervalMs(
  visualState: GuardianVisualStateValue | null,
  queuedJobs: number,
  consecutiveErrors: number,
): number {
  if (consecutiveErrors > 0) {
    return Math.min(30_000, 4_000 * 2 ** Math.min(consecutiveErrors, 4));
  }
  if (
    queuedJobs > 0 ||
    visualState === "INVESTIGATING" ||
    visualState === "RECOVERING"
  ) {
    return 4_000;
  }
  return 10_000;
}

/** Monotonic request sequence guard to prevent out-of-order stale responses from overwriting UI state. */
export function isStaleResponse(requestSeq: number, latestAppliedSeq: number): boolean {
  return requestSeq < latestAppliedSeq;
}

export function formatMetricOrEmpty(
  value: number | null | undefined,
  suffix = "",
  decimals = 0,
): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "Chưa có dữ liệu";
  }
  const formatted =
    decimals > 0 ? value.toFixed(decimals) : Math.round(value).toLocaleString("vi-VN");
  return suffix ? `${formatted}${suffix}` : formatted;
}

export function formatPercentOrEmpty(rate: number | null | undefined): string {
  if (rate === null || rate === undefined || Number.isNaN(rate)) {
    return "Chưa có dữ liệu";
  }
  return `${(rate * 100).toFixed(1)}%`;
}

const BUSINESS_EVENT_LABELS_VI: Record<string, string> = {
  ORDER_CREATED: "Khách tạo đơn hàng mới",
  PAYMENT_CAPTURED: "Thanh toán đã được ghi nhận",
  PAYMENT_PAID: "Thanh toán thành công",
  PAYMENT_FAILED: "Thanh toán thất bại",
  PAYMENT_SYNC_DELAYED: "Độ trễ đồng bộ thanh toán",
  REFUND_REQUESTED: "Khách gửi yêu cầu hoàn tiền",
  DISPUTE_SUBMITTED: "Khách gửi khiếu nại giao dịch",
  INVENTORY_UPDATED: "Cập nhật tồn kho sản phẩm",
};

export function translateBusinessEventVi(eventType: string): string {
  return BUSINESS_EVENT_LABELS_VI[eventType] ?? eventType;
}

export function detectOverviewToasts(
  prev: OverviewToastInput | null,
  next: OverviewToastInput,
): ExperienceToast[] {
  if (!prev || prev.session_id !== next.session_id || prev.world_epoch !== next.world_epoch) {
    return [];
  }
  const toasts: ExperienceToast[] = [];
  if (next.kpis.total_anomalies_detected > prev.kpis.total_anomalies_detected) {
    const diff = next.kpis.total_anomalies_detected - prev.kpis.total_anomalies_detected;
    toasts.push({
      id: `anom-${next.updated_at}`,
      tone: "warning",
      title: `Phát hiện ${diff} bất thường mới`,
      detail: "AI Guardian đã ghi nhận tín hiệu mới và bắt đầu kiểm tra bằng chứng.",
    });
  }
  if (next.kpis.verified_recoveries > prev.kpis.verified_recoveries) {
    const diff = next.kpis.verified_recoveries - prev.kpis.verified_recoveries;
    toasts.push({
      id: `rec-${next.updated_at}`,
      tone: "success",
      title: `Đã xác minh ${diff} phục hồi thành công`,
      detail: "Independent Verifier xác nhận trạng thái nghiệp vụ đã trở lại ổn định.",
    });
  }
  return toasts;
}

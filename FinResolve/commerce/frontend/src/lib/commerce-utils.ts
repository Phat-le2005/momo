/** Pure formatting and state helpers for FinShop LIVE (unit-tested). */

export function formatVnd(amount: number): string {
  const safe = Number.isFinite(amount) ? Math.round(amount) : 0;
  return `${safe.toLocaleString("vi-VN")}đ`;
}

export function formatDiscountBadge(originalVnd: number, effectiveVnd: number): string | null {
  if (originalVnd <= 0 || effectiveVnd >= originalVnd) return null;
  const pct = Math.round(((originalVnd - effectiveVnd) / originalVnd) * 100);
  return pct > 0 ? `-${pct}%` : null;
}

export function formatCountdown(endsAtIso: string | null | undefined, nowMs: number): string {
  if (!endsAtIso) return "00:00:00";
  const diff = Math.max(0, Math.floor((Date.parse(endsAtIso) - nowMs) / 1000));
  if (!Number.isFinite(diff) || diff <= 0) return "Đã kết thúc";
  const hrs = Math.floor(diff / 3600);
  const mins = Math.floor((diff % 3600) / 60);
  const secs = diff % 60;
  return [hrs, mins, secs].map((n) => String(n).padStart(2, "0")).join(":");
}

export function orderStatusLabelVi(status: string): string {
  const map: Record<string, string> = {
    PENDING_PAYMENT: "Chờ thanh toán",
    CONFIRMED: "Đã xác nhận",
    PROCESSING: "Đang đóng gói",
    SHIPPED: "Đang giao hàng",
    DELIVERED: "Đã giao thành công",
    CANCELLED: "Đã hủy",
  };
  return map[status] ?? status;
}

export function paymentStatusLabelVi(status: string): string {
  const map: Record<string, string> = {
    INITIATED: "Khởi tạo",
    PENDING: "Đang chờ xác nhận",
    SUCCEEDED: "Đã thanh toán (MOCK)",
    FAILED: "Thanh toán thất bại",
    UNKNOWN: "Chưa xác định (UNKNOWN)",
    PARTIALLY_REFUNDED: "Đã hoàn tiền một phần",
    REFUNDED: "Đã hoàn tiền toàn phần",
  };
  return map[status] ?? status;
}

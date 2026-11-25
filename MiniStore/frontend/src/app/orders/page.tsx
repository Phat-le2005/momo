"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { StoreShell } from "@/components/StoreShell";
import {
  type CustomerIdentityDTO,
  type OrderDTO,
  commerceFetch,
  ensureCustomerBootstrap,
} from "@/lib/commerce-api";
import {
  formatVnd,
  orderStatusLabelVi,
  paymentStatusLabelVi,
  reconciliationLabelVi,
} from "@/lib/commerce-utils";

export default function OrdersPage() {
  const [customer, setCustomer] = useState<CustomerIdentityDTO | null>(null);
  const [orders, setOrders] = useState<OrderDTO[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeComplaintOrder, setActiveComplaintOrder] = useState<string | null>(null);
  const [complaintText, setComplaintText] = useState("");
  const [receiptRef, setReceiptRef] = useState("");
  const [activeRefundOrder, setActiveRefundOrder] = useState<OrderDTO | null>(null);
  const [refundAmount, setRefundAmount] = useState("");
  const [refundReason, setRefundReason] = useState("");
  const [message, setMessage] = useState<string | null>(null);

  async function refreshOrders() {
    const list = await commerceFetch<OrderDTO[]>("/api/v1/store/orders");
    setOrders(list);
  }

  useEffect(() => {
    let mounted = true;
    async function init() {
      try {
        const { activeCustomer } = await ensureCustomerBootstrap();
        if (!mounted) return;
        setCustomer(activeCustomer);
        await refreshOrders();
      } catch (err) {
        if (mounted) setMessage(err instanceof Error ? err.message : "Không tải được đơn hàng");
      } finally {
        if (mounted) setLoading(false);
      }
    }
    void init();
    return () => {
      mounted = false;
    };
  }, []);

  async function handleCancel(orderId: string) {
    try {
      await commerceFetch(`/api/v1/store/orders/${orderId}/cancel`, { method: "POST" });
      setMessage("Đã hủy đơn hàng & hoàn trả giữ chỗ tồn kho.");
      await refreshOrders();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Không thể hủy đơn");
    }
  }

  async function handleSubmitComplaint(e: React.FormEvent) {
    e.preventDefault();
    if (!activeComplaintOrder) return;
    try {
      await commerceFetch(`/api/v1/store/orders/${activeComplaintOrder}/complaints`, {
        method: "POST",
        body: JSON.stringify({
          category: "PAYMENT_OR_ORDER",
          description: complaintText,
          claimed_receipt_ref: receiptRef.trim() || null,
        }),
      });
      setActiveComplaintOrder(null);
      setComplaintText("");
      setReceiptRef("");
      setMessage("Đã gửi khiếu nại (lưu dưới dạng lời khai CLAIM, không tự xác nhận thanh toán).");
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Không gửi được khiếu nại");
    }
  }

  async function handleSubmitRefund(e: React.FormEvent) {
    e.preventDefault();
    if (!activeRefundOrder) return;
    try {
      await commerceFetch(`/api/v1/store/orders/${activeRefundOrder.id}/refund-requests`, {
        method: "POST",
        body: JSON.stringify({
          requested_amount_vnd: Number(refundAmount),
          reason: refundReason,
        }),
      });
      setActiveRefundOrder(null);
      setRefundAmount("");
      setRefundReason("");
      setMessage("Đã gửi yêu cầu hoàn tiền tới Kênh Người Bán để xem xét.");
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Không gửi được yêu cầu hoàn tiền");
    }
  }

  return (
    <StoreShell customerName={customer?.display_name}>
      <h1 className="mb-4 text-lg font-extrabold text-slate-900">Lịch sử đơn hàng</h1>

      {message && (
        <div className="mb-4 rounded-xl border border-slate-200 bg-white px-4 py-2.5 text-xs font-semibold text-slate-800 shadow-2xs">
          {message}
        </div>
      )}

      {loading ? (
        <div className="h-48 animate-pulse rounded-2xl bg-slate-200" />
      ) : orders.length === 0 ? (
        <div className="rounded-2xl border border-slate-200 bg-white p-10 text-center text-sm text-slate-500">
          Bạn chưa có đơn hàng nào.{" "}
          <Link href="/" className="font-bold text-[#fe2c55] hover:underline">
            Mua sắm ngay →
          </Link>
        </div>
      ) : (
        <div className="space-y-4">
          {orders.map((ord) => (
            <article
              key={ord.id}
              className="rounded-2xl border border-slate-200 bg-white p-4 shadow-2xs"
            >
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 pb-3">
                <div>
                  <span className="text-sm font-black text-slate-900">{ord.order_code}</span>
                  <span className="ml-2 text-xs text-slate-500">
                    {new Date(ord.created_at).toLocaleString("vi-VN")}
                  </span>
                </div>
                <div className="flex flex-wrap gap-1.5 text-[11px] font-bold">
                  <span className="rounded-full bg-slate-100 px-2.5 py-0.5 text-slate-800">
                    Đơn hàng: {orderStatusLabelVi(ord.status)}
                  </span>
                  <span className="rounded-full bg-rose-50 px-2.5 py-0.5 text-[#fe2c55]">
                    Thanh toán: {paymentStatusLabelVi(ord.payment_status)}
                  </span>
                  {reconciliationLabelVi(ord.reconciliation_status) && (
                    <span className="rounded-full bg-amber-100 px-2.5 py-0.5 text-amber-900">
                      {reconciliationLabelVi(ord.reconciliation_status)}
                    </span>
                  )}
                </div>
              </div>

              {ord.reconciliation_status === "RECONCILIATION_REQUIRED" && ord.reconciliation_reason && (
                <div className="mt-2.5 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] font-semibold text-amber-900">
                  Cần đối soát: {ord.reconciliation_reason}
                </div>
              )}

              <div className="mt-3 space-y-2">
                {ord.items.map((it) => (
                  <div key={it.id} className="flex items-center justify-between gap-3 text-xs">
                    <div className="flex items-center gap-2.5">
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img
                        src={it.image_url_snapshot || "/products/fash-tshirt.jpg"}
                        alt={it.product_title_snapshot}
                        className="h-12 w-12 rounded-lg bg-slate-50 object-cover"
                      />
                      <div>
                        <p className="font-bold text-slate-900">{it.product_title_snapshot}</p>
                        <p className="text-slate-500">
                          {it.variant_title_snapshot} × {it.quantity}
                        </p>
                      </div>
                    </div>
                    <span className="font-bold text-slate-900">{formatVnd(it.line_total_vnd)}</span>
                  </div>
                ))}
              </div>

              <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-slate-100 pt-3">
                <div className="text-sm">
                  Tổng tiền:{" "}
                  <strong className="text-base font-black text-[#fe2c55]">
                    {formatVnd(ord.total_vnd)}
                  </strong>
                </div>
                <div className="flex flex-wrap gap-2">
                  {ord.status === "PENDING_PAYMENT" && (
                    <>
                      <Link
                        href={`/checkout?order_id=${encodeURIComponent(ord.id)}`}
                        className="rounded-xl bg-[#fe2c55] px-3 py-1.5 text-xs font-bold text-white"
                      >
                        Thanh toán MOCK
                      </Link>
                      <button
                        type="button"
                        onClick={() => void handleCancel(ord.id)}
                        className="rounded-xl border border-slate-300 px-3 py-1.5 text-xs font-semibold text-slate-700"
                      >
                        Hủy đơn
                      </button>
                    </>
                  )}
                  {(ord.payment_status === "SUCCEEDED" ||
                    ord.payment_status === "PARTIALLY_REFUNDED") && (
                    <button
                      type="button"
                      onClick={() => {
                        setActiveRefundOrder(ord);
                        setRefundAmount(
                          String(ord.payment?.refundable_remaining_vnd ?? ord.total_vnd),
                        );
                      }}
                      className="rounded-xl border border-amber-300 bg-amber-50 px-3 py-1.5 text-xs font-bold text-amber-900"
                    >
                      Yêu cầu hoàn tiền
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => setActiveComplaintOrder(ord.id)}
                    className="rounded-xl border border-slate-300 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700"
                  >
                    Gửi khiếu nại
                  </button>
                </div>
              </div>
            </article>
          ))}
        </div>
      )}

      {/* Complaint Modal */}
      {activeComplaintOrder && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
          <form
            onSubmit={handleSubmitComplaint}
            className="w-full max-w-md space-y-3 rounded-2xl bg-white p-5 shadow-xl"
          >
            <h2 className="text-sm font-extrabold">Gửi khiếu nại đơn hàng (CLAIM)</h2>
            <p className="text-[11px] text-slate-500">
              Nội dung khiếu nại và mã biên lai đính kèm được lưu dưới dạng lời khai (CLAIM), không
              tự động xác thực thanh toán.
            </p>
            <textarea
              required
              rows={3}
              value={complaintText}
              onChange={(e) => setComplaintText(e.target.value)}
              placeholder="Mô tả vấn đề bạn gặp phải..."
              className="w-full rounded-xl border border-slate-300 p-2.5 text-xs outline-none focus:border-[#fe2c55]"
            />
            <input
              value={receiptRef}
              onChange={(e) => setReceiptRef(e.target.value)}
              placeholder="Mã biên lai tham chiếu (tùy chọn, VD: RCPT-MOCK-01)"
              className="w-full rounded-xl border border-slate-300 px-3 py-2 text-xs outline-none"
            />
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setActiveComplaintOrder(null)}
                className="rounded-xl border border-slate-300 px-4 py-2 text-xs font-semibold"
              >
                Đóng
              </button>
              <button
                type="submit"
                className="rounded-xl bg-[#fe2c55] px-4 py-2 text-xs font-bold text-white"
              >
                Gửi khiếu nại
              </button>
            </div>
          </form>
        </div>
      )}

      {/* Refund Request Modal */}
      {activeRefundOrder && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
          <form
            onSubmit={handleSubmitRefund}
            className="w-full max-w-md space-y-3 rounded-2xl bg-white p-5 shadow-xl"
          >
            <h2 className="text-sm font-extrabold">
              Yêu cầu hoàn tiền ({activeRefundOrder.order_code})
            </h2>
            <p className="text-xs text-slate-600">
              Số dư có thể hoàn tối đa:{" "}
              <strong>
                {formatVnd(
                  activeRefundOrder.payment?.refundable_remaining_vnd ??
                    activeRefundOrder.total_vnd,
                )}
              </strong>
            </p>
            <input
              type="number"
              required
              min={1000}
              value={refundAmount}
              onChange={(e) => setRefundAmount(e.target.value)}
              placeholder="Số tiền yêu cầu hoàn (VND)"
              className="w-full rounded-xl border border-slate-300 px-3 py-2 text-xs outline-none"
            />
            <input
              required
              value={refundReason}
              onChange={(e) => setRefundReason(e.target.value)}
              placeholder="Lý do hoàn tiền..."
              className="w-full rounded-xl border border-slate-300 px-3 py-2 text-xs outline-none"
            />
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setActiveRefundOrder(null)}
                className="rounded-xl border border-slate-300 px-4 py-2 text-xs font-semibold"
              >
                Đóng
              </button>
              <button
                type="submit"
                className="rounded-xl bg-[#fe2c55] px-4 py-2 text-xs font-bold text-white"
              >
                Gửi yêu cầu hoàn tiền
              </button>
            </div>
          </form>
        </div>
      )}
    </StoreShell>
  );
}

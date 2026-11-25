"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import { StoreShell } from "@/components/StoreShell";
import {
  type CartDTO,
  type CustomerIdentityDTO,
  type OrderDTO,
  type PaymentDTO,
  commerceFetch,
  ensureCustomerBootstrap,
} from "@/lib/commerce-api";
import { formatVnd, orderStatusLabelVi, paymentStatusLabelVi } from "@/lib/commerce-utils";

function CheckoutContent() {
  const searchParams = useSearchParams();
  const directVariantId = searchParams.get("variant_id");
  const directQty = Number(searchParams.get("qty") || "1");
  const existingOrderId = searchParams.get("order_id");

  const [customer, setCustomer] = useState<CustomerIdentityDTO | null>(null);
  const [cart, setCart] = useState<CartDTO | null>(null);
  const [recipientName, setRecipientName] = useState("");
  const [recipientPhone, setRecipientPhone] = useState("");
  const [shippingAddress, setShippingAddress] = useState("");
  const [customerNote, setCustomerNote] = useState("");
  const [voucherCode, setVoucherCode] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [paying, setPaying] = useState(false);
  const [createdOrder, setCreatedOrder] = useState<OrderDTO | null>(null);
  const [createdPayment, setCreatedPayment] = useState<PaymentDTO | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [payNotice, setPayNotice] = useState<string | null>(null);

  // Stable idempotency key per checkout form attempt prevents duplicate orders on double-click
  const idempKeyRef = useRef<string>("");

  function getOrCreateIdempKey(): string {
    if (!idempKeyRef.current) {
      idempKeyRef.current = `IDEMP-WEB-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    }
    return idempKeyRef.current;
  }

  useEffect(() => {
    let mounted = true;
    async function init() {
      try {
        const { activeCustomer } = await ensureCustomerBootstrap();
        if (!mounted) return;
        setCustomer(activeCustomer);
        setRecipientName(activeCustomer.display_name);
        setRecipientPhone(activeCustomer.phone || "0901234567");
        setShippingAddress(activeCustomer.default_address || "123 Nguyễn Huệ, Q1, TP.HCM");

        if (existingOrderId) {
          const detail = await commerceFetch<{ order: OrderDTO }>(
            `/api/v1/store/orders/${existingOrderId}`,
          );
          if (mounted) {
            setCreatedOrder(detail.order);
            setCreatedPayment(detail.order.payment);
          }
          return;
        }
        const cdata = await commerceFetch<CartDTO>("/api/v1/store/cart");
        if (mounted) {
          setCart(cdata);
          if (cdata.applied_voucher_code) setVoucherCode(cdata.applied_voucher_code);
        }
      } catch (err) {
        if (mounted) setError(err instanceof Error ? err.message : "Không tải được trang thanh toán");
      }
    }
    void init();
    return () => {
      mounted = false;
    };
  }, [existingOrderId]);

  async function handleCreateOrder(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const body: Record<string, unknown> = {
        recipient_name: recipientName,
        recipient_phone: recipientPhone,
        shipping_address: shippingAddress,
        customer_note: customerNote,
        voucher_code: voucherCode.trim() || null,
        payment_method: "LOCAL_MOCK",
      };
      if (directVariantId) {
        body.direct_items = [{ variant_id: directVariantId, quantity: Math.max(1, directQty) }];
      } else if (cart) {
        body.expected_total_vnd = cart.total_vnd;
      }

      const res = await commerceFetch<{
        order: OrderDTO;
        payment: PaymentDTO;
        idempotent_replay: boolean;
      }>("/api/v1/store/checkout", {
        method: "POST",
        headers: { "X-Idempotency-Key": getOrCreateIdempKey() },
        body: JSON.stringify(body),
      });
      setCreatedOrder(res.order);
      setCreatedPayment(res.payment);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Không thể tạo đơn hàng");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleSimulateMockPayment(outcome: "SUCCEEDED" | "FAILED" | "PENDING" | "UNKNOWN") {
    if (!createdOrder || !createdPayment) return;
    setPaying(true);
    setError(null);
    try {
      // Customer-authenticated endpoint: backend verifies ownership & constructs provider callback server-side.
      // Browser NEVER holds X-Mock-Provider-Secret.
      const res = await commerceFetch<{ payment: PaymentDTO; orders_synced: number }>(
        `/api/v1/store/orders/${encodeURIComponent(createdOrder.id)}/mock-pay`,
        {
          method: "POST",
          body: JSON.stringify({
            outcome,
            auto_sync_order: true,
          }),
        },
      );
      setCreatedPayment(res.payment);
      const detail = await commerceFetch<{ order: OrderDTO }>(
        `/api/v1/store/orders/${createdOrder.id}`,
      );
      setCreatedOrder(detail.order);
      setPayNotice(
        outcome === "SUCCEEDED"
          ? "Thanh toán thử nghiệm (LOCAL_MOCK) thành công & đã đồng bộ trạng thái đơn hàng!"
          : `Đã ghi nhận kết quả thanh toán thử nghiệm: ${outcome}.`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Lỗi xử lý thanh toán thử nghiệm");
    } finally {
      setPaying(false);
    }
  }

  return (
    <StoreShell customerName={customer?.display_name}>
      <h1 className="mb-4 text-lg font-extrabold text-slate-900">Thanh toán đơn hàng</h1>

      {error && (
        <div className="mb-4 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-xs font-semibold text-rose-700">
          {error}
        </div>
      )}

      {createdOrder && createdPayment ? (
        <div className="mx-auto max-w-2xl space-y-4 rounded-2xl border border-slate-200 bg-white p-5">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 pb-3">
            <div>
              <p className="text-xs font-semibold text-slate-500">Mã đơn hàng</p>
              <p className="text-lg font-black text-slate-900">{createdOrder.order_code}</p>
            </div>
            <div className="text-right">
              <p className="text-xs text-slate-500">Tổng thanh toán xác thực</p>
              <p className="text-xl font-black text-[#fe2c55]">
                {formatVnd(createdOrder.total_vnd)}
              </p>
            </div>
          </div>

          <div className="grid gap-2 rounded-xl bg-slate-50 p-3 text-xs sm:grid-cols-2">
            <div>
              <span className="text-slate-500">Trạng thái đơn hàng: </span>
              <strong className="text-slate-900">{orderStatusLabelVi(createdOrder.status)}</strong>
            </div>
            <div>
              <span className="text-slate-500">Trạng thái thanh toán: </span>
              <strong className="text-slate-900">
                {paymentStatusLabelVi(createdPayment.status)}
              </strong>
            </div>
            <div>
              <span className="text-slate-500">Mã giao dịch MOCK: </span>
              <code className="font-mono text-[11px]">{createdPayment.provider_ref}</code>
            </div>
            <div>
              <span className="text-slate-500">Đồng bộ đơn: </span>
              <strong>{createdPayment.is_synced_to_order ? "Đã đồng bộ" : "Đang chờ worker"}</strong>
            </div>
          </div>

          {payNotice && (
            <div className="rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-xs font-semibold text-emerald-800">
              {payNotice}
            </div>
          )}

          {createdPayment.status !== "SUCCEEDED" && createdPayment.status !== "FAILED" && (
            <div className="rounded-2xl border border-amber-200 bg-amber-50/70 p-4">
              <p className="text-xs font-extrabold text-amber-900">
                Cổng Thanh Toán Thử Nghiệm (LOCAL_MOCK — Không phải tiền thật hay VNPAY)
              </p>
              <p className="mt-1 text-[11px] text-amber-800">
                Chọn kết quả callback mô phỏng từ cổng thanh toán để kiểm chứng luồng nghiệp vụ thực tế:
              </p>
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  type="button"
                  disabled={paying}
                  onClick={() => void handleSimulateMockPayment("SUCCEEDED")}
                  className="rounded-xl bg-[#fe2c55] px-4 py-2 text-xs font-extrabold text-white hover:bg-[#e02047] disabled:bg-slate-300"
                >
                  ✓ Thanh toán thành công (SUCCEEDED)
                </button>
                <button
                  type="button"
                  disabled={paying}
                  onClick={() => void handleSimulateMockPayment("PENDING")}
                  className="rounded-xl border border-slate-300 bg-white px-3 py-2 text-xs font-bold text-slate-700"
                >
                  Chờ xử lý (PENDING)
                </button>
                <button
                  type="button"
                  disabled={paying}
                  onClick={() => void handleSimulateMockPayment("FAILED")}
                  className="rounded-xl border border-rose-300 bg-white px-3 py-2 text-xs font-bold text-rose-700"
                >
                  Thất bại (FAILED)
                </button>
                <button
                  type="button"
                  disabled={paying}
                  onClick={() => void handleSimulateMockPayment("UNKNOWN")}
                  className="rounded-xl border border-amber-400 bg-white px-3 py-2 text-xs font-bold text-amber-800"
                >
                  Timeout (UNKNOWN)
                </button>
              </div>
            </div>
          )}

          <div className="flex justify-between pt-2">
            <Link href="/" className="text-xs font-semibold text-slate-600 hover:underline">
              ← Tiếp tục mua sắm
            </Link>
            <Link
              href="/orders"
              className="rounded-xl bg-[#161823] px-4 py-2 text-xs font-bold text-white"
            >
              Xem lịch sử đơn hàng →
            </Link>
          </div>
        </div>
      ) : (
        <form onSubmit={handleCreateOrder} className="grid gap-4 lg:grid-cols-3">
          <div className="space-y-4 rounded-2xl border border-slate-200 bg-white p-5 lg:col-span-2">
            <h2 className="text-sm font-extrabold text-slate-900">Thông tin nhận hàng</h2>
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block text-xs font-semibold text-slate-700">
                Họ tên người nhận
                <input
                  required
                  value={recipientName}
                  onChange={(e) => setRecipientName(e.target.value)}
                  className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2 text-sm outline-none focus:border-[#fe2c55]"
                />
              </label>
              <label className="block text-xs font-semibold text-slate-700">
                Số điện thoại
                <input
                  required
                  value={recipientPhone}
                  onChange={(e) => setRecipientPhone(e.target.value)}
                  className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2 text-sm outline-none focus:border-[#fe2c55]"
                />
              </label>
            </div>
            <label className="block text-xs font-semibold text-slate-700">
              Địa chỉ giao hàng
              <input
                required
                value={shippingAddress}
                onChange={(e) => setShippingAddress(e.target.value)}
                className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2 text-sm outline-none focus:border-[#fe2c55]"
              />
            </label>
            <label className="block text-xs font-semibold text-slate-700">
              Ghi chú giao hàng (tùy chọn)
              <input
                value={customerNote}
                onChange={(e) => setCustomerNote(e.target.value)}
                placeholder="VD: Giao giờ hành chính"
                className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2 text-sm outline-none focus:border-[#fe2c55]"
              />
            </label>

            <div className="border-t border-slate-100 pt-4">
              <h2 className="text-sm font-extrabold text-slate-900">Phương thức thanh toán</h2>
              <div className="mt-2.5 space-y-2">
                <label className="flex cursor-pointer items-center justify-between rounded-xl border-2 border-[#fe2c55] bg-rose-50/50 p-3">
                  <div className="flex items-center gap-2.5">
                    <input type="radio" name="pm" defaultChecked readOnly />
                    <div>
                      <p className="text-xs font-bold text-slate-900">
                        Thanh toán thử nghiệm nội bộ (LOCAL_MOCK)
                      </p>
                      <p className="text-[11px] text-slate-600">
                        Hỗ trợ mô phỏng SUCCEEDED / PENDING / FAILED / UNKNOWN an toàn
                      </p>
                    </div>
                  </div>
                  <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-bold text-emerald-800">
                    Khả dụng
                  </span>
                </label>

                <div className="flex items-center justify-between rounded-xl border border-slate-200 bg-slate-50 p-3 opacity-60">
                  <div className="flex items-center gap-2.5">
                    <input type="radio" name="pm" disabled />
                    <div>
                      <p className="text-xs font-bold text-slate-700">VNPAY Sandbox</p>
                      <p className="text-[11px] text-slate-500">
                        Chưa khả dụng ở Prompt 10 — sẽ tích hợp chính thức ở Prompt 11
                      </p>
                    </div>
                  </div>
                  <span className="rounded-full bg-slate-200 px-2 py-0.5 text-[10px] font-bold text-slate-600">
                    Sắp ra mắt
                  </span>
                </div>
              </div>
            </div>
          </div>

          <aside className="space-y-3 rounded-2xl border border-slate-200 bg-white p-5">
            <h2 className="text-sm font-extrabold text-slate-900">Tóm tắt đơn hàng</h2>
            {directVariantId ? (
              <p className="text-xs text-slate-600">
                Mua nhanh phân loại <code>{directVariantId}</code> × {directQty}. Giá cuối cùng sẽ
                được tính xác thực bởi backend.
              </p>
            ) : cart ? (
              <div className="space-y-2 text-xs">
                {cart.items.map((it) => (
                  <div key={it.variant_id} className="flex justify-between gap-2">
                    <span className="line-clamp-1 text-slate-700">
                      {it.product_title} × {it.quantity}
                    </span>
                    <span className="shrink-0 font-semibold">{formatVnd(it.line_total_vnd)}</span>
                  </div>
                ))}
                <div className="space-y-1.5 border-t border-slate-200 pt-2">
                  <div className="flex justify-between text-slate-600">
                    <span>Tạm tính</span>
                    <span>{formatVnd(cart.subtotal_vnd)}</span>
                  </div>
                  <div className="flex justify-between text-emerald-700">
                    <span>Giảm giá</span>
                    <span>−{formatVnd(cart.discount_vnd)}</span>
                  </div>
                  <div className="flex justify-between text-slate-600">
                    <span>Vận chuyển</span>
                    <span>{formatVnd(cart.shipping_fee_vnd)}</span>
                  </div>
                  <div className="flex justify-between border-t border-slate-200 pt-2 text-base font-black text-slate-900">
                    <span>Tổng cộng</span>
                    <span className="text-[#fe2c55]">{formatVnd(cart.total_vnd)}</span>
                  </div>
                </div>
              </div>
            ) : null}

            <button
              type="submit"
              disabled={submitting}
              className="w-full rounded-xl bg-[#fe2c55] py-3 text-sm font-extrabold text-white hover:bg-[#e02047] disabled:bg-slate-300"
            >
              {submitting ? "Đang tạo đơn..." : "Đặt hàng ngay"}
            </button>
          </aside>
        </form>
      )}
    </StoreShell>
  );
}

export default function CheckoutPage() {
  return (
    <Suspense fallback={<div className="p-8 text-center text-sm">Đang tải trang thanh toán...</div>}>
      <CheckoutContent />
    </Suspense>
  );
}

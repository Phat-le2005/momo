"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { StoreShell } from "@/components/StoreShell";
import {
  type CustomerIdentityDTO,
  type OrderDTO,
  type PaymentDTO,
  commerceFetch,
  ensureCustomerBootstrap,
} from "@/lib/commerce-api";
import {
  formatVnd,
  orderStatusLabelVi,
  paymentStatusLabelVi,
  reconciliationLabelVi,
} from "@/lib/commerce-utils";

interface ReturnVerifyResponse {
  return_signature_valid: boolean;
  return_response_code: string | null;
  authoritative_note_vi: string;
  order: OrderDTO;
  payment: PaymentDTO;
}

function VnpayReturnContent() {
  const searchParams = useSearchParams();
  const [customer, setCustomer] = useState<CustomerIdentityDTO | null>(null);
  const [result, setResult] = useState<ReturnVerifyResponse | null>(null);
  const queryString = searchParams.toString();
  const missingParams = !queryString;
  const [error, setError] = useState<string | null>(
    missingParams ? "Thiếu tham số phản hồi từ cổng thanh toán VNPAY Sandbox." : null,
  );
  const [loading, setLoading] = useState(!missingParams);

  useEffect(() => {
    if (!queryString) return;
    let mounted = true;
    async function verifyReturn() {
      try {
        const { activeCustomer } = await ensureCustomerBootstrap();
        if (!mounted) return;
        setCustomer(activeCustomer);
        const data = await commerceFetch<ReturnVerifyResponse>(
          `/api/v1/store/payments/vnpay/return-verify?${queryString}`,
        );
        if (mounted) setResult(data);
      } catch (err) {
        if (mounted) {
          setError(
            err instanceof Error
              ? err.message
              : "Không thể xác minh thông tin giao dịch VNPAY Sandbox",
          );
        }
      } finally {
        if (mounted) setLoading(false);
      }
    }
    void verifyReturn();
    return () => {
      mounted = false;
    };
  }, [queryString]);

  // Poll persisted authoritative order/payment state while PENDING or INITIATED
  useEffect(() => {
    if (!result) return;
    const pStatus = result.payment.status;
    if (pStatus !== "PENDING" && pStatus !== "INITIATED") return;

    const timer = setInterval(async () => {
      try {
        const detail = await commerceFetch<{ order: OrderDTO }>(
          `/api/v1/store/orders/${encodeURIComponent(result.order.id)}`,
        );
        if (detail.order.payment) {
          setResult((prev) =>
            prev
              ? {
                  ...prev,
                  order: detail.order,
                  payment: detail.order.payment as PaymentDTO,
                }
              : prev,
          );
        }
      } catch {
        // ignore transient poll errors
      }
    }, 3000);
    return () => clearInterval(timer);
  }, [result]);

  return (
    <StoreShell customerName={customer?.display_name}>
      <div className="mx-auto max-w-2xl space-y-4">
        <div className="rounded-2xl border border-slate-200 bg-white p-6 shadow-2xs">
          <span className="rounded-full bg-blue-50 px-3 py-1 text-[11px] font-bold text-blue-800">
            VNPAY SANDBOX — KẾT QUẢ CHUYỂN HƯỚNG (RETURN URL)
          </span>
          <h1 className="mt-3 text-lg font-extrabold text-slate-900">
            Trạng thái xác minh thanh toán
          </h1>

          {loading ? (
            <div className="mt-4 h-28 animate-pulse rounded-xl bg-slate-100" />
          ) : error ? (
            <div className="mt-4 rounded-xl border border-rose-200 bg-rose-50 p-4 text-xs font-semibold text-rose-700">
              {error}
            </div>
          ) : result ? (
            <div className="mt-4 space-y-4">
              <div className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900">
                <p className="font-bold">Lưu ý xác thực thanh toán:</p>
                <p className="mt-0.5">{result.authoritative_note_vi}</p>
              </div>

              <div className="grid gap-3 rounded-xl bg-slate-50 p-4 text-xs sm:grid-cols-2">
                <div>
                  <p className="text-slate-500">Mã đơn hàng</p>
                  <p className="font-black text-slate-900">{result.order.order_code}</p>
                </div>
                <div>
                  <p className="text-slate-500">Số tiền xác thực</p>
                  <p className="font-black text-[#fe2c55]">
                    {formatVnd(result.payment.amount_vnd)}
                  </p>
                </div>
                <div>
                  <p className="text-slate-500">Trạng thái đơn hàng (DB)</p>
                  <p className="font-bold text-slate-900">
                    {orderStatusLabelVi(result.order.status)}
                  </p>
                </div>
                <div>
                  <p className="text-slate-500">Trạng thái thanh toán (Authoritative)</p>
                  <p className="font-bold text-slate-900">
                    {paymentStatusLabelVi(result.payment.status)}
                  </p>
                </div>
                <div>
                  <p className="text-slate-500">Mã tham chiếu (vnp_TxnRef)</p>
                  <code className="font-mono text-[11px]">{result.payment.provider_ref}</code>
                </div>
                <div>
                  <p className="text-slate-500">Chữ ký ReturnURL</p>
                  <p className="font-bold">
                    {result.return_signature_valid ? "Hợp lệ (HMAC-SHA512)" : "Chưa xác thực / Không hợp lệ"}
                  </p>
                </div>
                <div>
                  <p className="text-slate-500">Nguồn xác nhận mới nhất</p>
                  <p className="font-semibold">{result.payment.observation_source}</p>
                </div>
                {reconciliationLabelVi(result.order.reconciliation_status) && (
                  <div>
                    <p className="text-slate-500">Đối soát</p>
                    <p className="font-bold text-amber-800">
                      {reconciliationLabelVi(result.order.reconciliation_status)}
                    </p>
                  </div>
                )}
              </div>

              {(result.payment.status === "PENDING" || result.payment.status === "INITIATED") && (
                <p className="text-center text-xs font-semibold text-blue-700">
                  Đang xác minh giao dịch qua IPN / QueryDr tự động... Trang sẽ tự cập nhật mỗi 3 giây.
                </p>
              )}

              <div className="flex flex-wrap justify-between gap-2 pt-2">
                <Link href="/" className="text-xs font-semibold text-slate-600 hover:underline">
                  ← Về trang chủ
                </Link>
                <Link
                  href="/orders"
                  className="rounded-xl bg-[#161823] px-4 py-2 text-xs font-bold text-white"
                >
                  Xem lịch sử đơn hàng →
                </Link>
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </StoreShell>
  );
}

export default function VnpayReturnPage() {
  return (
    <Suspense
      fallback={<div className="p-8 text-center text-sm">Đang kiểm tra kết quả VNPAY...</div>}
    >
      <VnpayReturnContent />
    </Suspense>
  );
}

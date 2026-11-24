"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { StoreShell } from "@/components/StoreShell";
import {
  type CartDTO,
  type VoucherDTO,
  commerceFetch,
  ensureCustomerBootstrap,
} from "@/lib/commerce-api";
import { formatVnd } from "@/lib/commerce-utils";

export default function CartPage() {
  const [cart, setCart] = useState<CartDTO | null>(null);
  const [vouchers, setVouchers] = useState<VoucherDTO[]>([]);
  const [voucherInput, setVoucherInput] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function loadCart() {
    const [c, v] = await Promise.all([
      commerceFetch<CartDTO>("/api/v1/store/cart"),
      commerceFetch<VoucherDTO[]>("/api/v1/store/vouchers"),
    ]);
    setCart(c);
    setVouchers(v);
  }

  useEffect(() => {
    let mounted = true;
    async function init() {
      try {
        await ensureCustomerBootstrap();
        if (mounted) await loadCart();
      } catch (err) {
        if (mounted) setError(err instanceof Error ? err.message : "Không tải được giỏ hàng");
      } finally {
        if (mounted) setLoading(false);
      }
    }
    void init();
    return () => {
      mounted = false;
    };
  }, []);

  async function updateQty(variantId: string, quantity: number) {
    setError(null);
    try {
      const updated = await commerceFetch<CartDTO>(`/api/v1/store/cart/items/${variantId}`, {
        method: "PATCH",
        body: JSON.stringify({ quantity }),
      });
      setCart(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Không cập nhật được số lượng");
    }
  }

  async function removeItem(variantId: string) {
    setError(null);
    const updated = await commerceFetch<CartDTO>(`/api/v1/store/cart/items/${variantId}`, {
      method: "DELETE",
    });
    setCart(updated);
  }

  async function applyVoucher(code: string) {
    setError(null);
    try {
      const updated = await commerceFetch<CartDTO>("/api/v1/store/cart/voucher", {
        method: "POST",
        body: JSON.stringify({ voucher_code: code }),
      });
      setCart(updated);
      setVoucherInput("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Mã giảm giá không hợp lệ");
    }
  }

  async function clearVoucher() {
    const updated = await commerceFetch<CartDTO>("/api/v1/store/cart/voucher", {
      method: "DELETE",
    });
    setCart(updated);
  }

  const cartCount = cart ? cart.items.reduce((s, i) => s + i.quantity, 0) : 0;

  return (
    <StoreShell cartCount={cartCount}>
      <h1 className="mb-4 text-lg font-extrabold text-slate-900">Giỏ hàng của bạn</h1>

      {error && (
        <div className="mb-4 rounded-xl border border-rose-200 bg-rose-50 px-4 py-2.5 text-xs font-semibold text-rose-700">
          {error}
        </div>
      )}

      {loading ? (
        <div className="h-48 animate-pulse rounded-2xl bg-slate-200" />
      ) : !cart || cart.items.length === 0 ? (
        <div className="rounded-2xl border border-slate-200 bg-white p-10 text-center">
          <p className="text-sm text-slate-600">Giỏ hàng đang trống.</p>
          <Link
            href="/"
            className="mt-4 inline-block rounded-xl bg-[#fe2c55] px-5 py-2.5 text-xs font-bold text-white"
          >
            Khám phá sản phẩm ngay
          </Link>
        </div>
      ) : (
        <div className="grid gap-4 lg:grid-cols-3">
          <div className="space-y-3 lg:col-span-2">
            {cart.items.map((item) => (
              <div
                key={item.variant_id}
                className="flex items-center gap-3 rounded-2xl border border-slate-200 bg-white p-3.5"
              >
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={item.image_url}
                  alt={item.product_title}
                  className="h-20 w-20 shrink-0 rounded-xl bg-slate-50 object-cover"
                />
                <div className="min-w-0 flex-1">
                  <Link
                    href={`/products/${item.product_id}`}
                    className="line-clamp-1 text-sm font-bold text-slate-900 hover:text-[#fe2c55]"
                  >
                    {item.product_title}
                  </Link>
                  <p className="text-xs text-slate-500">
                    {item.variant_title} · Kho: {item.available_qty}
                  </p>
                  <div className="mt-1 flex items-baseline gap-2">
                    <span className="text-sm font-extrabold text-[#fe2c55]">
                      {formatVnd(item.unit_price_vnd)}
                    </span>
                    {item.is_flash_sale_price && (
                      <span className="rounded bg-rose-50 px-1.5 py-0.5 text-[10px] font-bold text-[#fe2c55]">
                        Giá Flash Sale
                      </span>
                    )}
                  </div>
                </div>
                <div className="flex flex-col items-end gap-2">
                  <div className="inline-flex items-center rounded-lg border border-slate-300">
                    <button
                      type="button"
                      onClick={() =>
                        item.quantity > 1
                          ? void updateQty(item.variant_id, item.quantity - 1)
                          : void removeItem(item.variant_id)
                      }
                      className="px-2.5 py-1 text-xs font-bold"
                    >
                      −
                    </button>
                    <span className="min-w-6 text-center text-xs font-bold">{item.quantity}</span>
                    <button
                      type="button"
                      onClick={() => void updateQty(item.variant_id, item.quantity + 1)}
                      className="px-2.5 py-1 text-xs font-bold"
                    >
                      +
                    </button>
                  </div>
                  <button
                    type="button"
                    onClick={() => void removeItem(item.variant_id)}
                    className="text-[11px] font-semibold text-slate-400 hover:text-rose-600"
                  >
                    Xóa
                  </button>
                </div>
              </div>
            ))}
          </div>

          <aside className="space-y-4 rounded-2xl border border-slate-200 bg-white p-4">
            <h2 className="text-sm font-extrabold text-slate-900">Mã giảm giá (Voucher)</h2>
            <div className="flex gap-2">
              <input
                value={voucherInput}
                onChange={(e) => setVoucherInput(e.target.value)}
                placeholder="Nhập mã (VD: FINSHOP30K)"
                className="w-full rounded-xl border border-slate-300 px-3 py-2 text-xs outline-none focus:border-[#fe2c55]"
              />
              <button
                type="button"
                onClick={() => voucherInput.trim() && void applyVoucher(voucherInput.trim())}
                className="shrink-0 rounded-xl bg-[#161823] px-3.5 py-2 text-xs font-bold text-white"
              >
                Áp dụng
              </button>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {vouchers.map((v) => (
                <button
                  key={v.id}
                  type="button"
                  onClick={() => void applyVoucher(v.code)}
                  className="rounded-lg border border-amber-300 bg-amber-50 px-2 py-1 text-[11px] font-semibold text-amber-900 hover:bg-amber-100"
                >
                  {v.code}
                </button>
              ))}
            </div>
            {cart.applied_voucher_code && (
              <div className="flex items-center justify-between rounded-xl bg-emerald-50 px-3 py-2 text-xs text-emerald-800">
                <span>
                  Đang áp dụng: <strong>{cart.applied_voucher_code}</strong>
                </span>
                <button
                  type="button"
                  onClick={() => void clearVoucher()}
                  className="font-bold text-emerald-900 underline"
                >
                  Gỡ
                </button>
              </div>
            )}

            <div className="space-y-2 border-t border-slate-200 pt-3 text-sm">
              <div className="flex justify-between text-slate-600">
                <span>Tạm tính</span>
                <span>{formatVnd(cart.subtotal_vnd)}</span>
              </div>
              <div className="flex justify-between text-emerald-700">
                <span>Giảm giá voucher</span>
                <span>−{formatVnd(cart.discount_vnd)}</span>
              </div>
              <div className="flex justify-between text-slate-600">
                <span>Phí vận chuyển</span>
                <span>{cart.shipping_fee_vnd === 0 ? "Miễn phí" : formatVnd(cart.shipping_fee_vnd)}</span>
              </div>
              <div className="flex justify-between border-t border-slate-200 pt-2 text-base font-black text-slate-900">
                <span>Tổng thanh toán</span>
                <span className="text-[#fe2c55]">{formatVnd(cart.total_vnd)}</span>
              </div>
            </div>

            <Link
              href="/checkout"
              className="block w-full rounded-xl bg-[#fe2c55] py-3 text-center text-sm font-extrabold text-white hover:bg-[#e02047]"
            >
              Tiến hành thanh toán →
            </Link>
          </aside>
        </div>
      )}
    </StoreShell>
  );
}

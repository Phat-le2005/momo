"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { StoreShell } from "@/components/StoreShell";
import {
  type CartDTO,
  type ProductDTO,
  type VoucherDTO,
  commerceFetch,
  ensureCustomerBootstrap,
} from "@/lib/commerce-api";
import { formatDiscountBadge, formatVnd } from "@/lib/commerce-utils";

export default function ProductDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const [product, setProduct] = useState<ProductDTO | null>(null);
  const [selectedVarId, setSelectedVarId] = useState<string>("");
  const [qty, setQty] = useState<number>(1);
  const [vouchers, setVouchers] = useState<VoucherDTO[]>([]);
  const [cart, setCart] = useState<CartDTO | null>(null);
  const [loading, setLoading] = useState(true);
  const [feedback, setFeedback] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    async function load() {
      try {
        await ensureCustomerBootstrap();
        const [prod, vlist, cdata] = await Promise.all([
          commerceFetch<ProductDTO>(`/api/v1/store/products/${params.id}`),
          commerceFetch<VoucherDTO[]>("/api/v1/store/vouchers"),
          commerceFetch<CartDTO>("/api/v1/store/cart"),
        ]);
        if (!mounted) return;
        setProduct(prod);
        setSelectedVarId(prod.variants[0]?.id ?? "");
        setVouchers(vlist);
        setCart(cdata);
      } catch (err) {
        if (mounted) setFeedback(err instanceof Error ? err.message : "Không tải được sản phẩm");
      } finally {
        if (mounted) setLoading(false);
      }
    }
    void load();
    return () => {
      mounted = false;
    };
  }, [params.id]);

  const activeVariant = product?.variants.find((v) => v.id === selectedVarId) ?? product?.variants[0];
  const cartCount = cart ? cart.items.reduce((s, i) => s + i.quantity, 0) : 0;

  async function handleAddToCart() {
    if (!activeVariant) return;
    try {
      const updated = await commerceFetch<CartDTO>("/api/v1/store/cart/items", {
        method: "POST",
        body: JSON.stringify({ variant_id: activeVariant.id, quantity: qty }),
      });
      setCart(updated);
      setFeedback("Đã thêm sản phẩm vào giỏ hàng!");
    } catch (err) {
      setFeedback(err instanceof Error ? err.message : "Không thể thêm vào giỏ");
    }
  }

  function handleBuyNow() {
    if (!activeVariant) return;
    router.push(`/checkout?variant_id=${encodeURIComponent(activeVariant.id)}&qty=${qty}`);
  }

  return (
    <StoreShell cartCount={cartCount}>
      <div className="mb-3">
        <Link href="/" className="text-xs font-semibold text-slate-600 hover:text-[#fe2c55]">
          ← Quay lại Trang chủ
        </Link>
      </div>

      {loading ? (
        <div className="h-96 animate-pulse rounded-2xl bg-slate-200" />
      ) : !product || !activeVariant ? (
        <div className="rounded-2xl bg-white p-8 text-center text-sm text-slate-600">
          {feedback || "Sản phẩm không tồn tại."}
        </div>
      ) : (
        <div className="grid gap-6 rounded-2xl border border-slate-200 bg-white p-4 sm:p-6 md:grid-cols-2">
          <div>
            <div className="relative aspect-square overflow-hidden rounded-2xl bg-slate-50">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={activeVariant.image_url || product.image_urls[0]}
                alt={product.title}
                className="h-full w-full object-cover"
              />
              {activeVariant.is_flash_sale && (
                <span className="absolute left-3 top-3 rounded-lg bg-[#fe2c55] px-2.5 py-1 text-xs font-bold text-white">
                  ⚡ FLASH SALE LIVE
                </span>
              )}
            </div>
            <p className="mt-2 text-center text-[11px] text-slate-500">
              Dữ liệu sản phẩm mô phỏng (Test Data — không phải gian hàng thật)
            </p>
          </div>

          <div className="flex flex-col justify-between">
            <div>
              <span className="rounded-full bg-slate-100 px-2.5 py-1 text-[11px] font-semibold text-slate-600">
                {product.category}
              </span>
              <h1 className="mt-2 text-xl font-extrabold text-slate-900">{product.title}</h1>

              <div className="mt-3 flex items-baseline gap-2">
                <span className="text-2xl font-black text-[#fe2c55]">
                  {formatVnd(activeVariant.effective_price_vnd)}
                </span>
                {activeVariant.compare_at_price_vnd && (
                  <span className="text-sm text-slate-400 line-through">
                    {formatVnd(activeVariant.compare_at_price_vnd)}
                  </span>
                )}
                {activeVariant.compare_at_price_vnd &&
                  formatDiscountBadge(
                    activeVariant.compare_at_price_vnd,
                    activeVariant.effective_price_vnd,
                  ) && (
                    <span className="rounded bg-rose-50 px-1.5 py-0.5 text-xs font-bold text-[#fe2c55]">
                      {formatDiscountBadge(
                        activeVariant.compare_at_price_vnd,
                        activeVariant.effective_price_vnd,
                      )}
                    </span>
                  )}
              </div>

              <p className="mt-3 text-sm leading-relaxed text-slate-700">{product.description}</p>

              {/* Variant Selector */}
              <div className="mt-5">
                <p className="text-xs font-bold uppercase tracking-wide text-slate-500">
                  Phân loại ({activeVariant.sku})
                </p>
                <div className="mt-2 flex flex-wrap gap-2">
                  {product.variants.map((v) => {
                    const active = v.id === activeVariant.id;
                    return (
                      <button
                        key={v.id}
                        type="button"
                        onClick={() => {
                          setSelectedVarId(v.id);
                          setQty(1);
                        }}
                        className={`rounded-xl border px-3 py-2 text-xs font-semibold transition ${
                          active
                            ? "border-[#fe2c55] bg-rose-50 text-[#fe2c55]"
                            : "border-slate-200 bg-white text-slate-800 hover:border-slate-400"
                        }`}
                      >
                        {v.title} · Kho: {v.available_qty}
                      </button>
                    );
                  })}
                </div>
              </div>

              {/* Quantity Selector */}
              <div className="mt-5 flex items-center gap-4">
                <span className="text-xs font-bold uppercase tracking-wide text-slate-500">
                  Số lượng
                </span>
                <div className="inline-flex items-center rounded-xl border border-slate-300">
                  <button
                    type="button"
                    onClick={() => setQty((q) => Math.max(1, q - 1))}
                    className="px-3 py-1.5 text-sm font-bold"
                  >
                    −
                  </button>
                  <span className="min-w-8 text-center text-sm font-bold">{qty}</span>
                  <button
                    type="button"
                    onClick={() => setQty((q) => Math.min(Math.max(1, activeVariant.available_qty), q + 1))}
                    className="px-3 py-1.5 text-sm font-bold"
                  >
                    +
                  </button>
                </div>
                <span className="text-xs text-slate-500">
                  Khả dụng thực tế: <strong>{activeVariant.available_qty}</strong>
                </span>
              </div>

              {/* Eligible Vouchers */}
              {vouchers.length > 0 && (
                <div className="mt-5 rounded-xl border border-amber-200 bg-amber-50/60 p-3">
                  <p className="text-xs font-bold text-amber-900">Mã giảm giá áp dụng tại giỏ hàng</p>
                  <div className="mt-2 flex flex-wrap gap-2">
                    {vouchers.map((v) => (
                      <span
                        key={v.id}
                        className="rounded-lg border border-amber-300 bg-white px-2.5 py-1 text-[11px] font-semibold text-amber-900"
                      >
                        <strong>{v.code}</strong>: {v.title}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {feedback && (
                <p className="mt-3 rounded-xl bg-slate-100 px-3 py-2 text-xs font-semibold text-slate-800">
                  {feedback}
                </p>
              )}
            </div>

            <div className="mt-6 flex gap-3">
              <button
                type="button"
                disabled={activeVariant.available_qty <= 0}
                onClick={() => void handleAddToCart()}
                className="flex-1 rounded-xl border-2 border-[#fe2c55] bg-white py-3 text-sm font-bold text-[#fe2c55] hover:bg-rose-50 disabled:opacity-40"
              >
                Thêm vào giỏ
              </button>
              <button
                type="button"
                disabled={activeVariant.available_qty <= 0}
                onClick={handleBuyNow}
                className="flex-1 rounded-xl bg-[#fe2c55] py-3 text-sm font-bold text-white hover:bg-[#e02047] disabled:bg-slate-300"
              >
                {activeVariant.available_qty > 0 ? "Mua ngay" : "Hết hàng"}
              </button>
            </div>
          </div>
        </div>
      )}
    </StoreShell>
  );
}

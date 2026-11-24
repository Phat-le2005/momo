"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  type CartDTO,
  type CustomerIdentityDTO,
  type LiveProductItemDTO,
  type LiveSessionDTO,
  commerceFetch,
  ensureCustomerBootstrap,
} from "@/lib/commerce-api";
import { formatCountdown, formatDiscountBadge, formatVnd } from "@/lib/commerce-utils";

export default function LiveShoppingPage() {
  const router = useRouter();
  const [live, setLive] = useState<LiveSessionDTO | null>(null);
  const [customer, setCustomer] = useState<CustomerIdentityDTO | null>(null);
  const [cart, setCart] = useState<CartDTO | null>(null);
  const [sheetOpen, setSheetOpen] = useState(false);
  const [quickBuyProduct, setQuickBuyProduct] = useState<LiveProductItemDTO | null>(null);
  const [quickQty, setQuickQty] = useState(1);
  const [commentText, setCommentText] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());

  useEffect(() => {
    const t = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  async function refreshLive() {
    const [liveData, cartData] = await Promise.all([
      commerceFetch<LiveSessionDTO>("/api/v1/store/live"),
      commerceFetch<CartDTO>("/api/v1/store/cart"),
    ]);
    setLive(liveData);
    setCart(cartData);
  }

  useEffect(() => {
    let mounted = true;
    async function init() {
      try {
        const { activeCustomer } = await ensureCustomerBootstrap();
        if (!mounted) return;
        setCustomer(activeCustomer);
        await refreshLive();
      } catch (err) {
        if (mounted) setNotice(err instanceof Error ? err.message : "Không tải được phiên LIVE");
      }
    }
    void init();
    return () => {
      mounted = false;
    };
  }, []);

  async function handleAddCart(item: LiveProductItemDTO) {
    if (!live) return;
    try {
      const updated = await commerceFetch<CartDTO>("/api/v1/store/cart/items", {
        method: "POST",
        body: JSON.stringify({
          variant_id: item.variant_id,
          quantity: 1,
          live_session_id: live.id,
        }),
      });
      setCart(updated);
      setNotice(`Đã thêm ${item.product_title} vào giỏ!`);
      setTimeout(() => setNotice(null), 2200);
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Lỗi thêm giỏ hàng");
    }
  }

  async function handleInstantLiveCheckout() {
    if (!live || !quickBuyProduct || !customer) return;
    setBusy(true);
    try {
      const res = await commerceFetch<{
        order: { id: string; order_code: string; total_vnd: number; merchant_id: string };
        payment: { provider_ref: string; amount_vnd: number };
      }>("/api/v1/store/checkout", {
        method: "POST",
        headers: { "X-Idempotency-Key": `LIVE-BUY-${Date.now()}-${quickBuyProduct.variant_id}` },
        body: JSON.stringify({
          recipient_name: customer.display_name,
          recipient_phone: customer.phone || "0901234567",
          shipping_address: customer.default_address || "TP. Hồ Chí Minh",
          payment_method: "LOCAL_MOCK",
          direct_items: [
            {
              variant_id: quickBuyProduct.variant_id,
              quantity: quickQty,
              live_session_id: live.id,
            },
          ],
        }),
      });
      setQuickBuyProduct(null);
      setNotice(`Đã tạo đơn ${res.order.order_code}! Chuyển sang xác nhận thanh toán MOCK...`);
      router.push(`/checkout?order_id=${encodeURIComponent(res.order.id)}`);
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Không thể đặt hàng nhanh");
    } finally {
      setBusy(false);
    }
  }

  async function handleSendComment(e: React.FormEvent) {
    e.preventDefault();
    if (!live || !commentText.trim()) return;
    try {
      await commerceFetch(`/api/v1/store/live/${live.id}/comments`, {
        method: "POST",
        body: JSON.stringify({ message: commentText.trim() }),
      });
      setCommentText("");
      await refreshLive();
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Không gửi được bình luận");
    }
  }

  const cartCount = cart ? cart.items.reduce((s, i) => s + i.quantity, 0) : 0;

  return (
    <div className="min-h-screen bg-[#0b0c10] text-white">
      <div className="mx-auto flex min-h-screen max-w-6xl flex-col items-center justify-center gap-6 px-3 py-4 lg:flex-row lg:items-stretch">
        {/* 9:16 Vertical Immersive Live Stage */}
        <div className="relative flex aspect-[9/16] w-full max-w-[420px] flex-col justify-between overflow-hidden rounded-3xl border border-white/15 bg-[#161823] p-4 shadow-2xl">
          {/* Top Bar */}
          <div className="z-10 flex items-start justify-between gap-2">
            <div className="flex items-center gap-2 rounded-full bg-black/55 px-3 py-1.5 backdrop-blur">
              <span className="h-2.5 w-2.5 rounded-full bg-[#fe2c55]" />
              <div>
                <p className="text-xs font-bold leading-tight">
                  {live?.merchant_name || "FinShop Official"}
                </p>
                <p className="text-[10px] text-slate-300">
                  {live?.active_participants_count ?? 0} tài khoản test ·{" "}
                  {live?.session_orders_count ?? 0} đơn trong phiên
                </p>
              </div>
            </div>
            <div className="flex items-center gap-1.5">
              <Link
                href="/cart"
                className="rounded-full bg-black/55 px-3 py-1.5 text-xs font-bold backdrop-blur"
              >
                🛒 {cartCount}
              </Link>
              <Link
                href="/"
                className="rounded-full bg-black/55 px-3 py-1.5 text-xs font-semibold text-slate-200 backdrop-blur"
              >
                ✕
              </Link>
            </div>
          </div>

          {/* Center Simulated Live Visual Area (Honest Label!) */}
          <div className="my-auto flex flex-col items-center text-center">
            <span className="rounded-full border border-amber-400/50 bg-amber-500/20 px-3 py-1 text-[11px] font-bold text-amber-200">
              PHIÊN LIVE MÔ PHỎNG (SIMULATED LIVE COMMERCE)
            </span>
            <p className="mt-2 max-w-xs text-[11px] text-slate-300">
              {live?.media_label_vi ||
                "Đây là phiên mua sắm trực tiếp mô phỏng — không phải livestream video thật."}
            </p>
            {live?.pinned_product && (
              <div className="mt-5 w-52 overflow-hidden rounded-2xl border border-white/15 bg-white/10 p-3 backdrop-blur">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={live.pinned_product.image_url}
                  alt={live.pinned_product.product_title}
                  className="mx-auto h-40 w-40 rounded-xl bg-white object-cover"
                />
                <p className="mt-2 text-xs font-bold text-white">{live.host_name}</p>
                <p className="text-[11px] text-rose-300">
                  Đang ghim: {live.pinned_product.product_title}
                </p>
              </div>
            )}
          </div>

          {/* Bottom Overlay: Comments + Pinned Product Card + Controls */}
          <div className="z-10 space-y-2.5">
            {notice && (
              <div className="rounded-xl bg-[#fe2c55] px-3 py-1.5 text-center text-xs font-bold">
                {notice}
              </div>
            )}

            {/* Real persisted comments */}
            <div className="max-h-24 space-y-1 overflow-y-auto pr-1 text-xs">
              {(live?.recent_comments || []).slice(-4).map((c) => (
                <div key={c.id} className="rounded-lg bg-black/45 px-2.5 py-1 backdrop-blur">
                  <span className="font-bold text-rose-300">{c.author_name}: </span>
                  <span className="text-slate-100">{c.message}</span>
                </div>
              ))}
            </div>

            {/* Pinned Product Card */}
            {live?.pinned_product && (
              <div className="flex items-center gap-3 rounded-2xl bg-white p-2.5 text-[#161823] shadow-lg">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={live.pinned_product.image_url}
                  alt={live.pinned_product.product_title}
                  className="h-16 w-16 shrink-0 rounded-xl object-cover"
                />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <span className="rounded bg-[#fe2c55] px-1.5 py-0.5 text-[10px] font-bold text-white">
                      ĐANG GHIM
                    </span>
                    <span className="font-mono text-[10px] font-bold text-slate-600">
                      ⏱ {formatCountdown(live.pinned_product.flash_sale_ends_at, nowMs)}
                    </span>
                  </div>
                  <p className="mt-0.5 line-clamp-1 text-xs font-bold">
                    {live.pinned_product.product_title}
                  </p>
                  <div className="mt-0.5 flex items-baseline gap-1.5">
                    <span className="text-sm font-black text-[#fe2c55]">
                      {formatVnd(live.pinned_product.effective_price_vnd)}
                    </span>
                    <span className="text-[10px] text-slate-400 line-through">
                      {formatVnd(live.pinned_product.original_price_vnd)}
                    </span>
                    <span className="text-[10px] text-slate-500">
                      (Còn {live.pinned_product.available_qty})
                    </span>
                  </div>
                </div>
                <button
                  type="button"
                  disabled={live.pinned_product.available_qty <= 0}
                  onClick={() => {
                    setQuickBuyProduct(live.pinned_product);
                    setQuickQty(1);
                  }}
                  className="shrink-0 rounded-xl bg-[#fe2c55] px-3 py-2 text-xs font-extrabold text-white hover:bg-[#e02047] disabled:bg-slate-300"
                >
                  Mua ngay
                </button>
              </div>
            )}

            {/* Comment Input + Shopping Bag Sheet Trigger */}
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => setSheetOpen(true)}
                className="flex shrink-0 items-center gap-1.5 rounded-full bg-[#fe2c55] px-3.5 py-2 text-xs font-bold text-white"
              >
                🛍️ Giỏ LIVE ({live?.products.length ?? 0})
              </button>
              <form onSubmit={handleSendComment} className="flex flex-1 gap-1.5">
                <input
                  value={commentText}
                  onChange={(e) => setCommentText(e.target.value)}
                  placeholder="Nhắn tin trong phiên LIVE..."
                  className="w-full rounded-full bg-white/15 px-3 py-2 text-xs text-white placeholder-slate-300 outline-none"
                />
                <button
                  type="submit"
                  className="rounded-full bg-white/20 px-3 py-2 text-xs font-bold hover:bg-white/30"
                >
                  Gửi
                </button>
              </form>
            </div>
          </div>

          {/* Slide-Up Product Selection Bottom Sheet */}
          {sheetOpen && live && (
            <div className="absolute inset-0 z-30 flex flex-col justify-end bg-black/60">
              <div className="max-h-[75%] overflow-y-auto rounded-t-3xl bg-white p-4 text-[#161823]">
                <div className="mb-3 flex items-center justify-between">
                  <h2 className="text-sm font-extrabold">
                    Sản phẩm trong phiên LIVE ({live.products.length})
                  </h2>
                  <button
                    type="button"
                    onClick={() => setSheetOpen(false)}
                    className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-bold"
                  >
                    Đóng
                  </button>
                </div>
                <div className="space-y-2.5">
                  {live.products.map((item) => {
                    const badge = formatDiscountBadge(
                      item.original_price_vnd,
                      item.effective_price_vnd,
                    );
                    return (
                      <div
                        key={item.id}
                        className="flex items-center gap-3 rounded-2xl border border-slate-200 p-2.5"
                      >
                        {/* eslint-disable-next-line @next/next/no-img-element */}
                        <img
                          src={item.image_url}
                          alt={item.product_title}
                          className="h-16 w-16 shrink-0 rounded-xl object-cover"
                        />
                        <div className="min-w-0 flex-1">
                          <p className="line-clamp-1 text-xs font-bold">{item.product_title}</p>
                          <p className="text-[11px] text-slate-500">
                            {item.variant_title} · Kho: {item.available_qty}
                          </p>
                          <div className="mt-1 flex items-center gap-1.5">
                            <span className="text-sm font-black text-[#fe2c55]">
                              {formatVnd(item.effective_price_vnd)}
                            </span>
                            {badge && (
                              <span className="rounded bg-rose-50 px-1 text-[10px] font-bold text-[#fe2c55]">
                                {badge}
                              </span>
                            )}
                          </div>
                        </div>
                        <div className="flex flex-col gap-1">
                          <button
                            type="button"
                            disabled={item.available_qty <= 0}
                            onClick={() => {
                              setSheetOpen(false);
                              setQuickBuyProduct(item);
                              setQuickQty(1);
                            }}
                            className="rounded-lg bg-[#fe2c55] px-3 py-1.5 text-[11px] font-bold text-white disabled:bg-slate-300"
                          >
                            Mua ngay
                          </button>
                          <button
                            type="button"
                            disabled={item.available_qty <= 0}
                            onClick={() => void handleAddCart(item)}
                            className="rounded-lg border border-slate-300 px-3 py-1 text-[11px] font-semibold"
                          >
                            + Giỏ
                          </button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>
          )}

          {/* Quick Buy Sheet inside LIVE */}
          {quickBuyProduct && (
            <div className="absolute inset-0 z-40 flex flex-col justify-end bg-black/70">
              <div className="rounded-t-3xl bg-white p-4 text-[#161823]">
                <div className="mb-3 flex items-center justify-between">
                  <h3 className="text-sm font-extrabold">Mua nhanh trong phiên LIVE</h3>
                  <button
                    type="button"
                    onClick={() => setQuickBuyProduct(null)}
                    className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-bold"
                  >
                    ✕
                  </button>
                </div>
                <div className="flex items-center gap-3">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={quickBuyProduct.image_url}
                    alt={quickBuyProduct.product_title}
                    className="h-16 w-16 rounded-xl object-cover"
                  />
                  <div>
                    <p className="text-xs font-bold">{quickBuyProduct.product_title}</p>
                    <p className="text-[11px] text-slate-500">{quickBuyProduct.variant_title}</p>
                    <p className="mt-1 text-base font-black text-[#fe2c55]">
                      {formatVnd(quickBuyProduct.effective_price_vnd)}
                    </p>
                  </div>
                </div>
                <div className="mt-4 flex items-center justify-between">
                  <span className="text-xs font-semibold text-slate-600">
                    Số lượng (Kho khả dụng: {quickBuyProduct.available_qty})
                  </span>
                  <div className="inline-flex items-center rounded-xl border border-slate-300">
                    <button
                      type="button"
                      onClick={() => setQuickQty((q) => Math.max(1, q - 1))}
                      className="px-3 py-1 text-sm font-bold"
                    >
                      −
                    </button>
                    <span className="min-w-8 text-center text-sm font-bold">{quickQty}</span>
                    <button
                      type="button"
                      onClick={() =>
                        setQuickQty((q) =>
                          Math.min(Math.max(1, quickBuyProduct.available_qty), q + 1),
                        )
                      }
                      className="px-3 py-1 text-sm font-bold"
                    >
                      +
                    </button>
                  </div>
                </div>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void handleInstantLiveCheckout()}
                  className="mt-4 w-full rounded-xl bg-[#fe2c55] py-3 text-sm font-extrabold text-white hover:bg-[#e02047] disabled:bg-slate-300"
                >
                  {busy ? "Đang giữ hàng & tạo đơn..." : "Đặt hàng ngay (LOCAL_MOCK)"}
                </button>
              </div>
            </div>
          )}
        </div>

        {/* Desktop Side Panel: Live Product Carousel & Quick Info */}
        <aside className="hidden w-full max-w-md flex-col justify-between rounded-3xl border border-white/10 bg-white/5 p-5 lg:flex">
          <div>
            <h2 className="text-base font-extrabold text-white">Giỏ hàng phiên LIVE</h2>
            <p className="mt-1 text-xs text-slate-300">
              Giá Flash Sale & hạn mức tồn kho được kiểm soát nguyên tử bởi backend PostgreSQL.
            </p>
            <div className="mt-4 space-y-2.5">
              {(live?.products || []).map((item) => (
                <div
                  key={item.id}
                  className="flex items-center gap-3 rounded-2xl border border-white/10 bg-white/5 p-2.5"
                >
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={item.image_url}
                    alt={item.product_title}
                    className="h-14 w-14 rounded-xl bg-white object-cover"
                  />
                  <div className="min-w-0 flex-1">
                    <p className="line-clamp-1 text-xs font-bold">{item.product_title}</p>
                    <p className="text-[11px] text-slate-400">
                      Kho: {item.available_qty} · Deal còn:{" "}
                      {item.flash_sale_remaining ?? item.available_qty}
                    </p>
                    <p className="text-sm font-black text-[#fe2c55]">
                      {formatVnd(item.effective_price_vnd)}
                    </p>
                  </div>
                  <button
                    type="button"
                    disabled={item.available_qty <= 0}
                    onClick={() => {
                      setQuickBuyProduct(item);
                      setQuickQty(1);
                    }}
                    className="rounded-xl bg-[#fe2c55] px-3 py-1.5 text-xs font-bold text-white disabled:bg-slate-600"
                  >
                    Mua
                  </button>
                </div>
              ))}
            </div>
          </div>

          <div className="mt-4 flex items-center justify-between border-t border-white/10 pt-3 text-xs text-slate-300">
            <span>Khách đang chọn: {customer?.display_name || "—"}</span>
            <Link href="/orders" className="font-semibold text-[#fe2c55] hover:underline">
              Xem đơn hàng →
            </Link>
          </div>
        </aside>
      </div>
    </div>
  );
}

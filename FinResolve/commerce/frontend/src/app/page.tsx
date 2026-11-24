"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { StoreShell } from "@/components/StoreShell";
import {
  type CartDTO,
  type CustomerIdentityDTO,
  type LiveSessionDTO,
  type ProductDTO,
  commerceFetch,
  ensureCustomerBootstrap,
} from "@/lib/commerce-api";
import { formatCountdown, formatDiscountBadge, formatVnd } from "@/lib/commerce-utils";

export default function HomePage() {
  const [products, setProducts] = useState<ProductDTO[]>([]);
  const [recommended, setRecommended] = useState<ProductDTO[]>([]);
  const [categories, setCategories] = useState<string[]>([]);
  const [selectedCategory, setSelectedCategory] = useState<string>("Tất cả");
  const [searchText, setSearchText] = useState<string>("");
  const [liveSession, setLiveSession] = useState<LiveSessionDTO | null>(null);
  const [customer, setCustomer] = useState<CustomerIdentityDTO | null>(null);
  const [cart, setCart] = useState<CartDTO | null>(null);
  const [loading, setLoading] = useState(true);
  const [toast, setToast] = useState<string | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());

  useEffect(() => {
    const timer = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    let mounted = true;
    async function init() {
      try {
        const { bootstrap, activeCustomer } = await ensureCustomerBootstrap();
        if (!mounted) return;
        setCustomer(activeCustomer);
        setCategories(["Tất cả", ...bootstrap.categories]);

        const [prods, recs, live, cartData] = await Promise.all([
          commerceFetch<ProductDTO[]>("/api/v1/store/products"),
          commerceFetch<ProductDTO[]>("/api/v1/store/products?recommended=true"),
          commerceFetch<LiveSessionDTO>("/api/v1/store/live"),
          commerceFetch<CartDTO>("/api/v1/store/cart"),
        ]);
        if (!mounted) return;
        setProducts(prods);
        setRecommended(recs);
        setLiveSession(live);
        setCart(cartData);
      } catch (err) {
        if (mounted) setToast(err instanceof Error ? err.message : "Không tải được cửa hàng");
      } finally {
        if (mounted) setLoading(false);
      }
    }
    void init();
    return () => {
      mounted = false;
    };
  }, []);

  async function runFilter(cat: string, query: string) {
    setSelectedCategory(cat);
    const params = new URLSearchParams();
    if (cat && cat !== "Tất cả") params.set("category", cat);
    if (query.trim()) params.set("q", query.trim());
    const qs = params.toString() ? `?${params.toString()}` : "";
    try {
      const list = await commerceFetch<ProductDTO[]>(`/api/v1/store/products${qs}`);
      setProducts(list);
    } catch (err) {
      setToast(err instanceof Error ? err.message : "Lỗi tìm kiếm sản phẩm");
    }
  }

  async function quickAddToCart(variantId: string, liveSessionId?: string) {
    try {
      const updated = await commerceFetch<CartDTO>("/api/v1/store/cart/items", {
        method: "POST",
        body: JSON.stringify({
          variant_id: variantId,
          quantity: 1,
          live_session_id: liveSessionId ?? null,
        }),
      });
      setCart(updated);
      setToast("Đã thêm vào giỏ hàng!");
      setTimeout(() => setToast(null), 2200);
    } catch (err) {
      setToast(err instanceof Error ? err.message : "Không thể thêm vào giỏ");
    }
  }

  const cartCount = cart ? cart.items.reduce((s, i) => s + i.quantity, 0) : 0;

  return (
    <StoreShell cartCount={cartCount} customerName={customer?.display_name}>
      {toast && (
        <div className="fixed right-4 top-16 z-50 rounded-xl bg-[#161823] px-4 py-2.5 text-xs font-semibold text-white shadow-lg">
          {toast}
        </div>
      )}

      {/* Search Bar */}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void runFilter(selectedCategory, searchText);
        }}
        className="mb-4 flex items-center gap-2 rounded-2xl border border-slate-200 bg-white p-2 shadow-xs"
      >
        <input
          type="search"
          value={searchText}
          onChange={(e) => setSearchText(e.target.value)}
          placeholder="Tìm áo khoác UV, tai nghe ANC, serum B5, cà phê..."
          className="w-full bg-transparent px-2 py-1.5 text-sm outline-none"
        />
        <button
          type="submit"
          className="shrink-0 rounded-xl bg-[#fe2c55] px-4 py-2 text-xs font-bold text-white hover:bg-[#e02047]"
        >
          Tìm kiếm
        </button>
      </form>

      {/* Live Shopping Entry Banner */}
      {liveSession && (
        <section className="mb-5 overflow-hidden rounded-2xl bg-[#161823] p-4 text-white sm:p-5">
          <div className="flex flex-wrap items-center justify-between gap-4">
            <div className="max-w-xl">
              <div className="flex flex-wrap items-center gap-2">
                <span className="inline-flex items-center gap-1 rounded-full bg-[#fe2c55] px-2.5 py-0.5 text-[11px] font-bold">
                  ● LIVE MÔ PHỎNG
                </span>
                <span className="text-xs text-slate-300">
                  {liveSession.active_participants_count} tài khoản test trong phiên ·{" "}
                  {liveSession.session_orders_count} đơn thực tế
                </span>
              </div>
              <h1 className="mt-2 text-lg font-extrabold sm:text-xl">{liveSession.title}</h1>
              <p className="mt-1 text-xs text-slate-300">{liveSession.media_label_vi}</p>
            </div>
            <Link
              href="/live"
              className="rounded-xl bg-[#fe2c55] px-5 py-2.5 text-sm font-bold text-white hover:bg-[#e02047]"
            >
              Vào xem LIVE & Săn Deal →
            </Link>
          </div>
        </section>
      )}

      {/* Flash Sale Section */}
      {liveSession && liveSession.products.length > 0 && (
        <section className="mb-6 rounded-2xl border border-rose-100 bg-white p-4 shadow-xs">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <span className="text-base font-extrabold text-[#fe2c55]">⚡ FLASH SALE</span>
              <span className="rounded-lg bg-[#161823] px-2 py-0.5 font-mono text-xs font-bold text-white">
                {formatCountdown(liveSession.products[0]?.flash_sale_ends_at, nowMs)}
              </span>
            </div>
            <Link href="/live" className="text-xs font-semibold text-[#fe2c55] hover:underline">
              Xem tất cả trong LIVE →
            </Link>
          </div>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-5">
            {liveSession.products.map((lp) => {
              const badge = formatDiscountBadge(lp.original_price_vnd, lp.effective_price_vnd);
              return (
                <div
                  key={lp.id}
                  className="flex flex-col justify-between rounded-xl border border-slate-100 bg-slate-50/70 p-2.5"
                >
                  <Link href={`/products/${lp.product_id}`}>
                    <div className="relative aspect-square overflow-hidden rounded-lg bg-white">
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img
                        src={lp.image_url}
                        alt={lp.product_title}
                        className="h-full w-full object-cover"
                      />
                      {badge && (
                        <span className="absolute left-1.5 top-1.5 rounded-md bg-[#fe2c55] px-1.5 py-0.5 text-[10px] font-bold text-white">
                          {badge}
                        </span>
                      )}
                    </div>
                    <p className="mt-2 line-clamp-2 text-xs font-semibold text-slate-900">
                      {lp.product_title}
                    </p>
                  </Link>
                  <div className="mt-2">
                    <p className="text-sm font-extrabold text-[#fe2c55]">
                      {formatVnd(lp.effective_price_vnd)}
                    </p>
                    <p className="text-[11px] text-slate-400 line-through">
                      {formatVnd(lp.original_price_vnd)}
                    </p>
                    <p className="mt-1 text-[11px] font-medium text-slate-600">
                      Kho khả dụng: {lp.available_qty} · Deal còn:{" "}
                      {lp.flash_sale_remaining ?? lp.available_qty}
                    </p>
                    <button
                      type="button"
                      disabled={lp.available_qty <= 0}
                      onClick={() => void quickAddToCart(lp.variant_id, liveSession.id)}
                      className="mt-2 w-full rounded-lg bg-[#fe2c55] py-1.5 text-xs font-bold text-white hover:bg-[#e02047] disabled:bg-slate-300"
                    >
                      {lp.available_qty > 0 ? "Thêm giá LIVE" : "Hết hàng"}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </section>
      )}

      {/* Categories Strip */}
      <div className="mb-4 flex gap-2 overflow-x-auto pb-1">
        {categories.map((cat) => {
          const active = selectedCategory === cat;
          return (
            <button
              key={cat}
              type="button"
              onClick={() => void runFilter(cat, searchText)}
              className={`shrink-0 rounded-full px-3.5 py-1.5 text-xs font-semibold transition ${
                active
                  ? "bg-[#161823] text-white"
                  : "border border-slate-200 bg-white text-slate-700 hover:border-slate-400"
              }`}
            >
              {cat}
            </button>
          );
        })}
      </div>

      {/* Product Feed */}
      <section className="mb-8">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-base font-extrabold text-slate-900">
            {selectedCategory === "Tất cả" ? "Sản phẩm nổi bật (Dữ liệu Test)" : selectedCategory}
          </h2>
          <span className="text-xs text-slate-500">{products.length} sản phẩm</span>
        </div>

        {loading ? (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4">
            {Array.from({ length: 8 }).map((_, i) => (
              <div key={i} className="h-64 animate-pulse rounded-2xl bg-slate-200" />
            ))}
          </div>
        ) : products.length === 0 ? (
          <div className="rounded-2xl border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">
            Không tìm thấy sản phẩm phù hợp.
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4">
            {products.map((prod) => {
              const firstVar = prod.variants[0];
              const badge =
                firstVar && firstVar.compare_at_price_vnd
                  ? formatDiscountBadge(firstVar.compare_at_price_vnd, firstVar.effective_price_vnd)
                  : null;
              return (
                <article
                  key={prod.id}
                  className="flex flex-col justify-between overflow-hidden rounded-2xl border border-slate-200 bg-white p-3 shadow-2xs transition hover:border-slate-300"
                >
                  <Link href={`/products/${prod.id}`}>
                    <div className="relative aspect-square overflow-hidden rounded-xl bg-slate-50">
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img
                        src={prod.image_urls[0] || "/products/fash-tshirt.svg"}
                        alt={prod.title}
                        className="h-full w-full object-cover"
                      />
                      {badge && (
                        <span className="absolute left-2 top-2 rounded-md bg-[#fe2c55] px-1.5 py-0.5 text-[10px] font-bold text-white">
                          {badge}
                        </span>
                      )}
                      {prod.is_test_data && (
                        <span className="absolute bottom-1.5 right-1.5 rounded bg-black/60 px-1.5 py-0.5 text-[9px] font-semibold text-white">
                          TEST
                        </span>
                      )}
                    </div>
                    <p className="mt-2.5 text-[11px] font-medium text-slate-500">{prod.category}</p>
                    <h3 className="mt-0.5 line-clamp-2 text-sm font-bold text-slate-900">
                      {prod.title}
                    </h3>
                  </Link>

                  <div className="mt-3">
                    <div className="flex items-baseline gap-1.5">
                      <span className="text-base font-extrabold text-[#fe2c55]">
                        {formatVnd(prod.min_price_vnd)}
                      </span>
                      {firstVar?.compare_at_price_vnd && (
                        <span className="text-xs text-slate-400 line-through">
                          {formatVnd(firstVar.compare_at_price_vnd)}
                        </span>
                      )}
                    </div>
                    <p className="mt-0.5 text-[11px] text-slate-500">
                      Còn {prod.total_available_qty} sản phẩm trong kho
                    </p>
                    {firstVar && (
                      <div className="mt-2.5 flex gap-1.5">
                        <button
                          type="button"
                          disabled={firstVar.available_qty <= 0}
                          onClick={() => void quickAddToCart(firstVar.id)}
                          className="flex-1 rounded-xl border border-slate-300 bg-white py-1.5 text-xs font-bold text-slate-800 hover:border-[#fe2c55] hover:text-[#fe2c55] disabled:opacity-40"
                        >
                          + Giỏ
                        </button>
                        <Link
                          href={`/products/${prod.id}`}
                          className="flex-1 rounded-xl bg-[#fe2c55] py-1.5 text-center text-xs font-bold text-white hover:bg-[#e02047]"
                        >
                          Mua ngay
                        </Link>
                      </div>
                    )}
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </section>

      {/* Recommended Strip (Deterministic local strategy) */}
      {recommended.length > 0 && (
        <section className="rounded-2xl border border-slate-200 bg-white p-4">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-sm font-extrabold text-slate-900">Gợi ý hôm nay</h2>
            <span className="text-[11px] text-slate-500">
              Chọn lọc cục bộ: ưu tiên còn hàng & đa dạng danh mục (không dùng AI)
            </span>
          </div>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {recommended.slice(0, 4).map((p) => (
              <Link
                key={p.id}
                href={`/products/${p.id}`}
                className="flex items-center gap-2.5 rounded-xl border border-slate-100 p-2 hover:bg-slate-50"
              >
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={p.image_urls[0] || "/products/fash-tshirt.svg"}
                  alt={p.title}
                  className="h-14 w-14 shrink-0 rounded-lg object-cover"
                />
                <div className="min-w-0">
                  <p className="line-clamp-1 text-xs font-bold text-slate-900">{p.title}</p>
                  <p className="mt-0.5 text-xs font-extrabold text-[#fe2c55]">
                    {formatVnd(p.min_price_vnd)}
                  </p>
                  <p className="text-[10px] text-slate-500">Kho: {p.total_available_qty}</p>
                </div>
              </Link>
            ))}
          </div>
        </section>
      )}
    </StoreShell>
  );
}

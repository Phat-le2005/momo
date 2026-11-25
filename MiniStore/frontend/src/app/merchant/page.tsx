"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  type CommerceEventDTO,
  type ComplaintDTO,
  type LiveSessionDTO,
  type MerchantOverviewDTO,
  type OrderDTO,
  type PaymentDTO,
  type ProductDTO,
  type RefundRequestDTO,
  type VoucherDTO,
  clearStoredMerchantToken,
  commerceFetch,
  getStoredMerchantToken,
  loginMerchant,
  logoutMerchant,
} from "@/lib/commerce-api";
import {
  formatVnd,
  orderStatusLabelVi,
  paymentStatusLabelVi,
  reconciliationLabelVi,
} from "@/lib/commerce-utils";

type TabKey =
  | "overview"
  | "products"
  | "orders"
  | "inventory"
  | "payments"
  | "refunds"
  | "vouchers"
  | "live"
  | "events";

const SIDEBAR_TABS: Array<{ key: TabKey; label: string }> = [
  { key: "overview", label: "Tổng quan" },
  { key: "products", label: "Sản phẩm" },
  { key: "orders", label: "Đơn hàng & Đối soát" },
  { key: "inventory", label: "Kho hàng" },
  { key: "payments", label: "Thanh toán" },
  { key: "refunds", label: "Hoàn tiền & Khiếu nại" },
  { key: "vouchers", label: "Voucher" },
  { key: "live", label: "Phiên LIVE" },
  { key: "events", label: "Nhật ký & Outbox" },
];

export default function MerchantCenterPage() {
  const [tab, setTab] = useState<TabKey>("overview");
  const [merchantToken, setMerchantToken] = useState<string | null>(null);
  const [operatorId, setOperatorId] = useState<string>("");
  const [authChecking, setAuthChecking] = useState(true);
  const [loginUsername, setLoginUsername] = useState("merchant_owner");
  const [loginPassword, setLoginPassword] = useState("");
  const [loginError, setLoginError] = useState<string | null>(null);
  const [loggingIn, setLoggingIn] = useState(false);

  const [overview, setOverview] = useState<MerchantOverviewDTO | null>(null);
  const [products, setProducts] = useState<ProductDTO[]>([]);
  const [orders, setOrders] = useState<OrderDTO[]>([]);
  const [orderSearch, setOrderSearch] = useState("");
  const [payments, setPayments] = useState<PaymentDTO[]>([]);
  const [providerFilter, setProviderFilter] = useState<string>("ALL");
  const [refunds, setRefunds] = useState<RefundRequestDTO[]>([]);
  const [complaints, setComplaints] = useState<ComplaintDTO[]>([]);
  const [vouchers, setVouchers] = useState<VoucherDTO[]>([]);
  const [live, setLive] = useState<LiveSessionDTO | null>(null);
  const [events, setEvents] = useState<CommerceEventDTO[]>([]);
  const [notice, setNotice] = useState<string | null>(null);

  // Create product form
  const [pTitle, setPTitle] = useState("");
  const pCategory = "Thời trang";
  const [pSku, setPSku] = useState("");
  const [pPrice, setPPrice] = useState("199000");
  const pStock = "20";

  // Create voucher form
  const [vCode, setVCode] = useState("");
  const [vTitle, setVTitle] = useState("");
  const [vValue, setVValue] = useState("20000");

  async function refreshAll(tok: string, q?: string) {
    const orderUrl = q && q.trim()
      ? `/api/v1/merchant/orders?q=${encodeURIComponent(q.trim())}`
      : "/api/v1/merchant/orders";
    const [ov, prods, ords, pays, refs, cmps, vchs, lv, evs] = await Promise.all([
      commerceFetch<MerchantOverviewDTO>("/api/v1/merchant/overview", { merchantToken: tok }),
      commerceFetch<ProductDTO[]>("/api/v1/merchant/products", { merchantToken: tok }),
      commerceFetch<OrderDTO[]>(orderUrl, { merchantToken: tok }),
      commerceFetch<PaymentDTO[]>("/api/v1/merchant/payments", { merchantToken: tok }),
      commerceFetch<RefundRequestDTO[]>("/api/v1/merchant/refunds", { merchantToken: tok }),
      commerceFetch<ComplaintDTO[]>("/api/v1/merchant/complaints", { merchantToken: tok }),
      commerceFetch<VoucherDTO[]>("/api/v1/merchant/vouchers", { merchantToken: tok }),
      commerceFetch<LiveSessionDTO>("/api/v1/store/live"),
      commerceFetch<CommerceEventDTO[]>("/api/v1/merchant/events", { merchantToken: tok }),
    ]);
    setOverview(ov);
    setProducts(prods);
    setOrders(ords);
    setPayments(pays);
    setRefunds(refs);
    setComplaints(cmps);
    setVouchers(vchs);
    setLive(lv);
    setEvents(evs);
  }

  useEffect(() => {
    let mounted = true;
    async function checkExistingSession() {
      const existing = getStoredMerchantToken();
      if (!existing) {
        if (mounted) setAuthChecking(false);
        return;
      }
      try {
        const me = await commerceFetch<{ operator_id: string }>("/api/v1/merchant/auth/me", {
          merchantToken: existing,
        });
        if (!mounted) return;
        setMerchantToken(existing);
        setOperatorId(me.operator_id);
        await refreshAll(existing);
      } catch {
        clearStoredMerchantToken();
      } finally {
        if (mounted) setAuthChecking(false);
      }
    }
    void checkExistingSession();
    return () => {
      mounted = false;
    };
  }, []);

  async function handleLogin(e: React.FormEvent) {
    e.preventDefault();
    setLoginError(null);
    setLoggingIn(true);
    try {
      const res = await loginMerchant(loginUsername.trim(), loginPassword);
      setMerchantToken(res.merchant_token);
      setOperatorId(res.operator_id);
      setLoginPassword("");
      await refreshAll(res.merchant_token);
    } catch (err) {
      setLoginError(err instanceof Error ? err.message : "Đăng nhập thất bại");
    } finally {
      setLoggingIn(false);
    }
  }

  async function handleLogout() {
    await logoutMerchant();
    setMerchantToken(null);
    setOperatorId("");
    setOverview(null);
    setNotice(null);
  }

  async function runAction(fn: () => Promise<void>, successMsg: string) {
    if (!merchantToken) return;
    try {
      await fn();
      setNotice(successMsg);
      await refreshAll(merchantToken, orderSearch);
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Thao tác thất bại");
    }
  }

  if (authChecking) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-[#f4f5f7] text-sm text-slate-600">
        Đang kiểm tra phiên đăng nhập Kênh Người Bán...
      </div>
    );
  }

  if (!merchantToken) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center bg-[#f4f5f7] px-4 py-10 text-[#161823]">
        <div className="w-full max-w-md rounded-3xl border border-slate-200 bg-white p-7 shadow-sm">
          <div className="flex items-center gap-3">
            <span className="inline-flex h-10 w-10 items-center justify-center rounded-xl bg-[#161823] text-sm font-black text-white">
              MC
            </span>
            <div>
              <h1 className="text-lg font-extrabold">Đăng nhập Kênh Người Bán</h1>
              <p className="text-xs text-slate-500">
                FinShop LIVE Merchant Center · Xác thực phiên bảo mật
              </p>
            </div>
          </div>

          {loginError && (
            <div className="mt-4 rounded-xl border border-rose-200 bg-rose-50 px-3.5 py-2.5 text-xs font-semibold text-rose-700">
              {loginError}
            </div>
          )}

          <form onSubmit={handleLogin} className="mt-5 space-y-3.5">
            <label className="block text-xs font-bold text-slate-700">
              Tên đăng nhập
              <input
                required
                value={loginUsername}
                onChange={(e) => setLoginUsername(e.target.value)}
                autoComplete="username"
                className="mt-1 w-full rounded-xl border border-slate-300 px-3.5 py-2.5 text-sm outline-none focus:border-[#fe2c55]"
              />
            </label>
            <label className="block text-xs font-bold text-slate-700">
              Mật khẩu
              <input
                required
                type="password"
                value={loginPassword}
                onChange={(e) => setLoginPassword(e.target.value)}
                autoComplete="current-password"
                placeholder="Nhập mật khẩu quản trị cửa hàng"
                className="mt-1 w-full rounded-xl border border-slate-300 px-3.5 py-2.5 text-sm outline-none focus:border-[#fe2c55]"
              />
            </label>
            <button
              type="submit"
              disabled={loggingIn}
              className="w-full rounded-xl bg-[#fe2c55] py-3 text-sm font-extrabold text-white transition hover:bg-[#e02047] disabled:bg-slate-300"
            >
              {loggingIn ? "Đang xác thực..." : "Đăng nhập Kênh Người Bán"}
            </button>
          </form>

          <div className="mt-5 rounded-xl bg-slate-50 p-3 text-[11px] text-slate-600">
            <p className="font-bold text-slate-800">Lưu ý môi trường thử nghiệm (MOCK):</p>
            <p className="mt-0.5">
              Tài khoản thử nghiệm mặc định được cấu hình qua biến môi trường{" "}
              <code className="font-mono">COMMERCE_TEST_MERCHANT_USERNAME</code> /{" "}
              <code className="font-mono">COMMERCE_TEST_MERCHANT_PASSWORD</code>.
            </p>
          </div>

          <div className="mt-4 text-center">
            <Link href="/" className="text-xs font-semibold text-slate-600 hover:text-[#fe2c55]">
              ← Quay lại cửa hàng FinShop LIVE
            </Link>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-[#f4f5f7] text-[#161823]">
      <header className="border-b border-slate-200 bg-white px-6 py-3.5">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <span className="inline-flex h-8 w-8 items-center justify-center rounded-xl bg-[#161823] text-xs font-black text-white">
              MC
            </span>
            <div>
              <h1 className="text-base font-extrabold">
                FinShop LIVE — Kênh Người Bán (Merchant Center)
              </h1>
              <p className="text-[11px] text-slate-500">
                Quản trị viên: <strong>{operatorId || "merchant_owner"}</strong> · Dữ liệu thử
                nghiệm MOCK
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Link
              href="/"
              className="rounded-xl border border-slate-300 bg-white px-3.5 py-1.5 text-xs font-bold text-slate-800 hover:border-[#fe2c55] hover:text-[#fe2c55]"
            >
              ← Về cửa hàng khách
            </Link>
            <button
              type="button"
              onClick={() => void handleLogout()}
              className="rounded-xl bg-slate-900 px-3.5 py-1.5 text-xs font-bold text-white hover:bg-slate-700"
            >
              Đăng xuất
            </button>
          </div>
        </div>
      </header>

      <div className="mx-auto grid max-w-7xl gap-5 px-4 py-5 lg:grid-cols-[230px_1fr]">
        <aside className="rounded-2xl border border-slate-200 bg-white p-2.5">
          <nav className="space-y-1">
            {SIDEBAR_TABS.map((item) => (
              <button
                key={item.key}
                type="button"
                onClick={() => setTab(item.key)}
                className={`flex w-full items-center justify-between rounded-xl px-3 py-2 text-left text-xs font-bold transition ${
                  tab === item.key
                    ? "bg-[#fe2c55] text-white"
                    : "text-slate-700 hover:bg-slate-100"
                }`}
              >
                <span>{item.label}</span>
                {item.key === "orders" &&
                  overview &&
                  overview.reconciliation_required_count > 0 && (
                    <span className="rounded-full bg-amber-400 px-1.5 py-0.5 text-[10px] font-black text-slate-900">
                      {overview.reconciliation_required_count}
                    </span>
                  )}
              </button>
            ))}
          </nav>
        </aside>

        <main className="space-y-4">
          {notice && (
            <div className="rounded-xl border border-slate-200 bg-white px-4 py-2.5 text-xs font-semibold text-slate-800 shadow-2xs">
              {notice}
            </div>
          )}

          {tab === "overview" && overview && (
            <div className="space-y-4">
              <p className="text-xs text-slate-500">{overview.metrics_note_vi}</p>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                {[
                  {
                    label: "Doanh thu test đã xác nhận",
                    value: formatVnd(overview.confirmed_revenue_vnd),
                  },
                  { label: "Tổng số đơn hàng", value: String(overview.order_count) },
                  { label: "Đơn cần đối soát", value: String(overview.reconciliation_required_count) },
                  { label: "Đơn chờ đóng gói / giao", value: String(overview.pending_fulfillment) },
                  { label: "Thanh toán đang chờ", value: String(overview.pending_payments) },
                  { label: "Yêu cầu hoàn tiền chờ duyệt", value: String(overview.refund_requests) },
                  { label: "Khiếu nại đang mở", value: String(overview.open_complaints) },
                  {
                    label: "Outbox chờ / Dead-letter",
                    value: `${overview.outbox_pending_count} / ${overview.outbox_dead_letter_count}`,
                  },
                ].map((m) => (
                  <div
                    key={m.label}
                    className="rounded-2xl border border-slate-200 bg-white p-4 shadow-2xs"
                  >
                    <p className="text-[11px] font-semibold text-slate-500">{m.label}</p>
                    <p className="mt-1.5 text-xl font-black text-slate-900">{m.value}</p>
                  </div>
                ))}
              </div>
            </div>
          )}

          {tab === "products" && (
            <div className="space-y-4">
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void runAction(async () => {
                    await commerceFetch("/api/v1/merchant/products", {
                      method: "POST",
                      merchantToken,
                      body: JSON.stringify({
                        title: pTitle,
                        category: pCategory,
                        sku: pSku,
                        price_vnd: Number(pPrice),
                        initial_stock: Number(pStock),
                      }),
                    });
                    setPTitle("");
                    setPSku("");
                  }, "Đã tạo sản phẩm mới!");
                }}
                className="grid gap-2.5 rounded-2xl border border-slate-200 bg-white p-4 sm:grid-cols-5"
              >
                <input
                  required
                  value={pTitle}
                  onChange={(e) => setPTitle(e.target.value)}
                  placeholder="Tên sản phẩm mới"
                  className="rounded-xl border border-slate-300 px-3 py-2 text-xs sm:col-span-2"
                />
                <input
                  required
                  value={pSku}
                  onChange={(e) => setPSku(e.target.value)}
                  placeholder="SKU (VD: FS-NEW-01)"
                  className="rounded-xl border border-slate-300 px-3 py-2 text-xs"
                />
                <input
                  type="number"
                  required
                  value={pPrice}
                  onChange={(e) => setPPrice(e.target.value)}
                  placeholder="Giá VND"
                  className="rounded-xl border border-slate-300 px-3 py-2 text-xs"
                />
                <button
                  type="submit"
                  className="rounded-xl bg-[#fe2c55] px-4 py-2 text-xs font-bold text-white"
                >
                  + Thêm sản phẩm
                </button>
              </form>

              <div className="overflow-x-auto rounded-2xl border border-slate-200 bg-white">
                <table className="w-full text-left text-xs">
                  <thead className="border-b border-slate-200 bg-slate-50 text-slate-600">
                    <tr>
                      <th className="p-3">Sản phẩm</th>
                      <th className="p-3">Danh mục</th>
                      <th className="p-3">Giá bán</th>
                      <th className="p-3">Khả dụng</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {products.map((p) => (
                      <tr key={p.id}>
                        <td className="p-3 font-bold">{p.title}</td>
                        <td className="p-3 text-slate-600">{p.category}</td>
                        <td className="p-3 font-semibold text-[#fe2c55]">
                          {formatVnd(p.min_price_vnd)}
                        </td>
                        <td className="p-3">{p.total_available_qty}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {tab === "orders" && (
            <div className="space-y-3">
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  if (merchantToken) void refreshAll(merchantToken, orderSearch);
                }}
                className="flex gap-2 rounded-2xl border border-slate-200 bg-white p-3"
              >
                <input
                  value={orderSearch}
                  onChange={(e) => setOrderSearch(e.target.value)}
                  placeholder="Tìm theo mã đơn, tên khách hoặc số điện thoại..."
                  className="w-full rounded-xl border border-slate-300 px-3 py-1.5 text-xs outline-none focus:border-[#fe2c55]"
                />
                <button
                  type="submit"
                  className="shrink-0 rounded-xl bg-[#161823] px-4 py-1.5 text-xs font-bold text-white"
                >
                  Tìm kiếm
                </button>
              </form>

              {orders.length === 0 ? (
                <div className="rounded-2xl bg-white p-6 text-center text-xs text-slate-500">
                  Chưa có đơn hàng nào.
                </div>
              ) : (
                orders.map((o) => (
                  <div
                    key={o.id}
                    className={`flex flex-wrap items-center justify-between gap-3 rounded-2xl border bg-white p-4 text-xs ${
                      o.reconciliation_status === "RECONCILIATION_REQUIRED"
                        ? "border-amber-400 bg-amber-50/40"
                        : "border-slate-200"
                    }`}
                  >
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <p className="font-black text-slate-900">
                          {o.order_code} · {formatVnd(o.total_vnd)}
                        </p>
                        {reconciliationLabelVi(o.reconciliation_status) && (
                          <span className="rounded-full bg-amber-200 px-2 py-0.5 text-[10px] font-bold text-amber-950">
                            {reconciliationLabelVi(o.reconciliation_status)}
                          </span>
                        )}
                      </div>
                      <p className="text-slate-600">
                        {o.recipient_name} ({o.recipient_phone}) — {o.shipping_address}
                      </p>
                      <p className="mt-1">
                        Đơn: <strong>{orderStatusLabelVi(o.status)}</strong> · Thanh toán:{" "}
                        <strong>{paymentStatusLabelVi(o.payment_status)}</strong>
                      </p>
                      {o.reconciliation_reason && (
                        <p className="mt-1 text-[11px] font-medium text-amber-900">
                          Ghi chú đối soát: {o.reconciliation_reason}
                        </p>
                      )}
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      {o.reconciliation_status === "RECONCILIATION_REQUIRED" && (
                        <button
                          type="button"
                          onClick={() =>
                            void runAction(
                              () =>
                                commerceFetch(
                                  `/api/v1/merchant/orders/${o.id}/reconciliation/resolve`,
                                  {
                                    method: "POST",
                                    merchantToken,
                                    body: JSON.stringify({
                                      resolution_note:
                                        "Người bán đã đối soát giao dịch và xác nhận xử lý thủ công.",
                                    }),
                                  },
                                ),
                              `Đã đánh dấu hoàn tất đối soát cho đơn ${o.order_code}`,
                            )
                          }
                          className="rounded-lg bg-amber-600 px-3 py-1.5 font-bold text-white"
                        >
                          Xác nhận đã đối soát
                        </button>
                      )}
                      {o.status === "CONFIRMED" && (
                        <button
                          type="button"
                          onClick={() =>
                            void runAction(
                              () =>
                                commerceFetch(`/api/v1/merchant/orders/${o.id}/status`, {
                                  method: "PATCH",
                                  merchantToken,
                                  body: JSON.stringify({ status: "PROCESSING" }),
                                }),
                              `Đơn ${o.order_code} -> Đang đóng gói`,
                            )
                          }
                          className="rounded-lg bg-[#161823] px-3 py-1.5 font-bold text-white"
                        >
                          Đóng gói
                        </button>
                      )}
                      {o.status === "PROCESSING" && (
                        <button
                          type="button"
                          onClick={() =>
                            void runAction(
                              () =>
                                commerceFetch(`/api/v1/merchant/orders/${o.id}/status`, {
                                  method: "PATCH",
                                  merchantToken,
                                  body: JSON.stringify({ status: "SHIPPED" }),
                                }),
                              `Đơn ${o.order_code} -> Đang giao hàng`,
                            )
                          }
                          className="rounded-lg bg-[#fe2c55] px-3 py-1.5 font-bold text-white"
                        >
                          Giao vận chuyển
                        </button>
                      )}
                      {o.status === "SHIPPED" && (
                        <button
                          type="button"
                          onClick={() =>
                            void runAction(
                              () =>
                                commerceFetch(`/api/v1/merchant/orders/${o.id}/status`, {
                                  method: "PATCH",
                                  merchantToken,
                                  body: JSON.stringify({ status: "DELIVERED" }),
                                }),
                              `Đơn ${o.order_code} -> Đã giao thành công`,
                            )
                          }
                          className="rounded-lg bg-emerald-600 px-3 py-1.5 font-bold text-white"
                        >
                          Xác nhận đã giao
                        </button>
                      )}
                    </div>
                  </div>
                ))
              )}
            </div>
          )}

          {tab === "inventory" && (
            <div className="overflow-x-auto rounded-2xl border border-slate-200 bg-white">
              <table className="w-full text-left text-xs">
                <thead className="border-b border-slate-200 bg-slate-50 text-slate-600">
                  <tr>
                    <th className="p-3">SKU</th>
                    <th className="p-3">Phân loại</th>
                    <th className="p-3">Tồn vật lý</th>
                    <th className="p-3">Đang giữ chỗ</th>
                    <th className="p-3">Khả dụng</th>
                    <th className="p-3">Điều chỉnh nhanh</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {products.flatMap((p) =>
                    p.variants.map((v) => (
                      <tr key={v.id}>
                        <td className="p-3 font-mono">{v.sku}</td>
                        <td className="p-3 font-semibold">{v.title}</td>
                        <td className="p-3">{v.on_hand_qty}</td>
                        <td className="p-3">{v.reserved_qty}</td>
                        <td className="p-3 font-bold">{v.available_qty}</td>
                        <td className="p-3">
                          <button
                            type="button"
                            onClick={() =>
                              void runAction(
                                () =>
                                  commerceFetch(`/api/v1/merchant/inventory/${v.id}`, {
                                    method: "PATCH",
                                    merchantToken,
                                    body: JSON.stringify({ on_hand_qty: v.on_hand_qty + 5 }),
                                  }),
                                `Đã nhập thêm +5 cho ${v.sku}`,
                              )
                            }
                            className="rounded-lg border border-slate-300 px-2.5 py-1 font-bold hover:border-[#fe2c55]"
                          >
                            +5 tồn kho
                          </button>
                        </td>
                      </tr>
                    )),
                  )}
                </tbody>
              </table>
            </div>
          )}

          {tab === "payments" && (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex gap-1.5">
                  {(["ALL", "LOCAL_MOCK", "VNPAY_SANDBOX"] as const).map((prov) => (
                    <button
                      key={prov}
                      type="button"
                      onClick={() => setProviderFilter(prov)}
                      className={`rounded-xl px-3 py-1.5 text-xs font-bold ${
                        providerFilter === prov
                          ? "bg-[#fe2c55] text-white"
                          : "border border-slate-300 bg-white text-slate-700"
                      }`}
                    >
                      {prov === "ALL" ? "Tất cả cổng" : prov}
                    </button>
                  ))}
                </div>
                <button
                  type="button"
                  onClick={() =>
                    void runAction(
                      () =>
                        commerceFetch("/api/v1/merchant/payments/sync-now", {
                          method: "POST",
                          merchantToken,
                        }),
                      "Đã chạy đồng bộ thanh toán -> đơn hàng!",
                    )
                  }
                  className="rounded-xl bg-[#161823] px-4 py-2 text-xs font-bold text-white"
                >
                  Đồng bộ thanh toán chưa khớp ngay
                </button>
              </div>
              <div className="overflow-x-auto rounded-2xl border border-slate-200 bg-white">
                <table className="w-full text-left text-xs">
                  <thead className="border-b border-slate-200 bg-slate-50 text-slate-600">
                    <tr>
                      <th className="p-3">Cổng / Môi trường</th>
                      <th className="p-3">Mã GD / TxnNo</th>
                      <th className="p-3">Số tiền / Đã hoàn</th>
                      <th className="p-3">Trạng thái & Nguồn</th>
                      <th className="p-3">Đồng bộ / Thao tác</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {payments
                      .filter((p) => providerFilter === "ALL" || p.provider === providerFilter)
                      .map((p) => (
                        <tr key={p.id}>
                          <td className="p-3">
                            <span className="font-bold">{p.provider}</span>
                            <p className="text-[10px] text-slate-500">{p.provider_environment}</p>
                          </td>
                          <td className="p-3 font-mono text-[11px]">
                            <p>{p.provider_ref}</p>
                            {p.provider_txn_no && (
                              <p className="text-[10px] text-slate-500">
                                TxnNo: {p.provider_txn_no}
                              </p>
                            )}
                          </td>
                          <td className="p-3">
                            <p className="font-bold">{formatVnd(p.amount_vnd)}</p>
                            {p.refunded_amount_vnd > 0 && (
                              <p className="text-[10px] text-amber-800">
                                Hoàn: {formatVnd(p.refunded_amount_vnd)}
                              </p>
                            )}
                          </td>
                          <td className="p-3">
                            <p className="font-semibold">{paymentStatusLabelVi(p.status)}</p>
                            <p className="text-[10px] text-slate-500">
                              {p.observation_source} ·{" "}
                              {p.signature_verified ? "Chữ ký hợp lệ" : "Chưa xác thực chữ ký"}
                            </p>
                          </td>
                          <td className="p-3 space-y-1">
                            <div>
                              {p.reconciliation_required ? (
                                <span className="rounded-full bg-amber-100 px-2 py-0.5 font-bold text-amber-900">
                                  Cần đối soát
                                </span>
                              ) : p.is_synced_to_order ? (
                                "Đã đồng bộ"
                              ) : (
                                "Chưa đồng bộ"
                              )}
                            </div>
                            {p.provider === "VNPAY_SANDBOX" && (
                              <button
                                type="button"
                                onClick={() =>
                                  void runAction(
                                    () =>
                                      commerceFetch(`/api/v1/merchant/payments/${p.id}/querydr`, {
                                        method: "POST",
                                        merchantToken,
                                      }),
                                    `Đã gửi truy vấn QueryDr cho ${p.provider_ref}`,
                                  )
                                }
                                className="rounded-lg border border-blue-300 bg-blue-50 px-2 py-1 text-[10px] font-bold text-blue-800 hover:bg-blue-100"
                              >
                                Truy vấn QueryDr
                              </button>
                            )}
                          </td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {tab === "refunds" && (
            <div className="space-y-4">
              <section className="space-y-2">
                <h2 className="text-sm font-extrabold">Yêu cầu hoàn tiền ({refunds.length})</h2>
                {refunds.map((r) => (
                  <div
                    key={r.id}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-2xl border border-slate-200 bg-white p-4 text-xs"
                  >
                    <div>
                      <p className="font-bold">
                        {r.id} · Yêu cầu hoàn {formatVnd(r.requested_amount_vnd)} ({r.status})
                      </p>
                      <p className="text-slate-600">Lý do: {r.reason}</p>
                    </div>
                    {r.status === "REQUESTED" && (
                      <div className="flex gap-2">
                        <button
                          type="button"
                          onClick={() =>
                            void runAction(
                              () =>
                                commerceFetch(`/api/v1/merchant/refunds/${r.id}/decision`, {
                                  method: "POST",
                                  merchantToken,
                                  body: JSON.stringify({ decision: "APPROVE" }),
                                }),
                              `Đã duyệt & hoàn tiền MOCK cho ${r.id}`,
                            )
                          }
                          className="rounded-lg bg-[#fe2c55] px-3 py-1.5 font-bold text-white"
                        >
                          Duyệt hoàn tiền
                        </button>
                        <button
                          type="button"
                          onClick={() =>
                            void runAction(
                              () =>
                                commerceFetch(`/api/v1/merchant/refunds/${r.id}/decision`, {
                                  method: "POST",
                                  merchantToken,
                                  body: JSON.stringify({ decision: "REJECT" }),
                                }),
                              `Đã từ chối yêu cầu ${r.id}`,
                            )
                          }
                          className="rounded-lg border border-slate-300 px-3 py-1.5 font-bold"
                        >
                          Từ chối
                        </button>
                      </div>
                    )}
                  </div>
                ))}
              </section>

              <section className="space-y-2">
                <h2 className="text-sm font-extrabold">Khiếu nại khách hàng ({complaints.length})</h2>
                {complaints.map((c) => (
                  <div
                    key={c.id}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-2xl border border-slate-200 bg-white p-4 text-xs"
                  >
                    <div>
                      <p className="font-bold">
                        {c.id} · {c.category} · [{c.evidence_kind}] · {c.status}
                      </p>
                      <p className="text-slate-700">{c.description}</p>
                      {c.claimed_receipt_ref && (
                        <p className="text-[11px] text-amber-800">
                          Mã biên lai khách khai (chưa xác thực): {c.claimed_receipt_ref}
                        </p>
                      )}
                    </div>
                    {c.status !== "RESOLVED" && (
                      <button
                        type="button"
                        onClick={() =>
                          void runAction(
                            () =>
                              commerceFetch(`/api/v1/merchant/complaints/${c.id}`, {
                                method: "PATCH",
                                merchantToken,
                                body: JSON.stringify({
                                  status: "RESOLVED",
                                  merchant_response: "Người bán đã kiểm tra và phản hồi.",
                                }),
                              }),
                            `Đã xử lý khiếu nại ${c.id}`,
                          )
                        }
                        className="rounded-lg bg-emerald-600 px-3 py-1.5 font-bold text-white"
                      >
                        Đánh dấu đã xử lý
                      </button>
                    )}
                  </div>
                ))}
              </section>
            </div>
          )}

          {tab === "vouchers" && (
            <div className="space-y-4">
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void runAction(async () => {
                    await commerceFetch("/api/v1/merchant/vouchers", {
                      method: "POST",
                      merchantToken,
                      body: JSON.stringify({
                        code: vCode,
                        title: vTitle,
                        discount_type: "FIXED_VND",
                        discount_value: Number(vValue),
                        min_order_vnd: 100000,
                        usage_limit: 50,
                      }),
                    });
                    setVCode("");
                    setVTitle("");
                  }, "Đã tạo voucher mới!");
                }}
                className="grid gap-2.5 rounded-2xl border border-slate-200 bg-white p-4 sm:grid-cols-4"
              >
                <input
                  required
                  value={vCode}
                  onChange={(e) => setVCode(e.target.value)}
                  placeholder="Mã (VD: DEAL20K)"
                  className="rounded-xl border border-slate-300 px-3 py-2 text-xs"
                />
                <input
                  required
                  value={vTitle}
                  onChange={(e) => setVTitle(e.target.value)}
                  placeholder="Mô tả voucher"
                  className="rounded-xl border border-slate-300 px-3 py-2 text-xs"
                />
                <input
                  type="number"
                  required
                  value={vValue}
                  onChange={(e) => setVValue(e.target.value)}
                  placeholder="Giảm VND"
                  className="rounded-xl border border-slate-300 px-3 py-2 text-xs"
                />
                <button
                  type="submit"
                  className="rounded-xl bg-[#fe2c55] px-4 py-2 text-xs font-bold text-white"
                >
                  + Tạo Voucher
                </button>
              </form>

              <div className="space-y-2">
                {vouchers.map((v) => (
                  <div
                    key={v.id}
                    className="flex items-center justify-between rounded-2xl border border-slate-200 bg-white p-4 text-xs"
                  >
                    <div>
                      <p className="font-black text-slate-900">
                        {v.code} — {v.title}
                      </p>
                      <p className="text-slate-500">
                        Đơn tối thiểu: {formatVnd(v.min_order_vnd)} · Đã dùng: {v.used_count}/
                        {v.usage_limit}
                      </p>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {tab === "live" && live && (
            <div className="space-y-3 rounded-2xl border border-slate-200 bg-white p-4">
              <h2 className="text-sm font-extrabold">{live.title}</h2>
              <p className="text-xs text-slate-500">{live.media_label_vi}</p>
              <div className="space-y-2">
                {live.products.map((lp) => (
                  <div
                    key={lp.id}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-slate-200 p-3 text-xs"
                  >
                    <div>
                      <p className="font-bold">
                        {lp.is_pinned ? "[ĐANG GHIM] " : ""}
                        {lp.product_title} ({lp.variant_title})
                      </p>
                      <p className="text-slate-500">
                        Giá LIVE: <strong>{formatVnd(lp.effective_price_vnd)}</strong> · Giữ chỗ:{" "}
                        {lp.flash_sale_reserved_qty} · Đã bán: {lp.flash_sale_sold_qty}/
                        {lp.flash_sale_stock_limit ?? "∞"}
                      </p>
                    </div>
                    <button
                      type="button"
                      disabled={lp.is_pinned}
                      onClick={() =>
                        void runAction(
                          () =>
                            commerceFetch(
                              `/api/v1/merchant/live/${live.id}/products/${lp.id}`,
                              {
                                method: "PATCH",
                                merchantToken,
                                body: JSON.stringify({ is_pinned: true }),
                              },
                            ),
                          `Đã ghim ${lp.product_title} lên phiên LIVE!`,
                        )
                      }
                      className="rounded-lg bg-[#fe2c55] px-3 py-1.5 font-bold text-white disabled:bg-slate-300"
                    >
                      {lp.is_pinned ? "Đang ghim" : "Ghim lên LIVE"}
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}

          {tab === "events" && (
            <div className="space-y-2 rounded-2xl border border-slate-200 bg-white p-4">
              <h2 className="text-sm font-extrabold">
                Nhật ký sự kiện nghiệp vụ & Outbox ({events.length})
              </h2>
              <div className="space-y-2">
                {events.map((ev) => (
                  <div key={ev.id} className="rounded-xl border border-slate-100 bg-slate-50 p-3 text-xs">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="font-black text-slate-900">{ev.event_type}</span>
                      <div className="flex items-center gap-1.5">
                        {ev.failure_category && (
                          <span className="rounded-full bg-rose-100 px-2 py-0.5 text-[10px] font-bold text-rose-800">
                            {ev.failure_category}
                          </span>
                        )}
                        <span className="rounded-full bg-white px-2 py-0.5 text-[10px] font-bold text-slate-600">
                          Outbox: {ev.outbox_status || "—"}
                        </span>
                      </div>
                    </div>
                    <p className="mt-1 font-mono text-[11px] text-slate-500">
                      event_id={ev.event_id} · order_id={ev.order_id || "—"} ·{" "}
                      {new Date(ev.occurred_at).toLocaleString("vi-VN")}
                    </p>
                  </div>
                ))}
              </div>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}

"use client";

import { useEffect, useState } from "react";
import { StoreShell } from "@/components/StoreShell";
import {
  type CustomerIdentityDTO,
  clearStoredCustomerToken,
  commerceFetch,
  ensureCustomerBootstrap,
  setStoredCustomerToken,
} from "@/lib/commerce-api";

export default function ProfilePage() {
  const [active, setActive] = useState<CustomerIdentityDTO | null>(null);
  const [newName, setNewName] = useState("");
  const [newPhone, setNewPhone] = useState("0905556677");
  const [newAddress, setNewAddress] = useState("45 Lê Lợi, Quận 1, TP. Hồ Chí Minh");
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let mounted = true;
    async function init() {
      const { activeCustomer } = await ensureCustomerBootstrap();
      if (!mounted) return;
      setActive(activeCustomer);
    }
    void init();
    return () => {
      mounted = false;
    };
  }, []);

  async function createNewGuestCustomer(e: React.FormEvent) {
    e.preventDefault();
    if (!newName.trim()) return;
    setBusy(true);
    setNotice(null);
    try {
      const created = await commerceFetch<CustomerIdentityDTO>("/api/v1/store/customers/guest", {
        method: "POST",
        customerToken: null,
        body: JSON.stringify({
          display_name: newName.trim(),
          phone: newPhone.trim(),
          default_address: newAddress.trim() || "TP. Hồ Chí Minh",
        }),
      });
      setStoredCustomerToken(created.session_token);
      setActive(created);
      setNewName("");
      setNotice(
        `Đã tạo phiên khách hàng cô lập mới: ${created.display_name} (${created.id}). Giỏ hàng và đơn hàng hoàn toàn tách biệt.`,
      );
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Không thể tạo phiên khách mới");
    } finally {
      setBusy(false);
    }
  }

  async function handleLogoutAndReset() {
    setBusy(true);
    setNotice(null);
    try {
      await commerceFetch("/api/v1/store/customers/logout", { method: "POST" });
    } catch {
      // ignore if already expired
    }
    clearStoredCustomerToken();
    const { activeCustomer } = await ensureCustomerBootstrap();
    setActive(activeCustomer);
    setNotice("Đã đăng xuất phiên cũ và khởi tạo phiên khách hàng mới an toàn.");
    setBusy(false);
  }

  return (
    <StoreShell customerName={active?.display_name}>
      <div className="mx-auto max-w-2xl space-y-5">
        <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-2xs">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="rounded-full bg-emerald-50 px-2.5 py-0.5 text-[11px] font-bold text-emerald-700">
              PHIÊN KHÁCH HÀNG CÔ LẬP (EXPIRING SESSION)
            </span>
            <button
              type="button"
              disabled={busy}
              onClick={() => void handleLogoutAndReset()}
              className="rounded-xl border border-slate-300 px-3 py-1.5 text-xs font-bold text-slate-700 hover:border-rose-400 hover:text-rose-600 disabled:opacity-50"
            >
              Đăng xuất & Tạo phiên mới
            </button>
          </div>
          <h1 className="mt-3 text-lg font-extrabold text-slate-900">
            {active?.display_name || "Đang khởi tạo phiên..."}
          </h1>
          <p className="mt-1 text-xs text-slate-600">
            Mã khách: <code className="font-mono">{active?.id}</code> · SĐT: {active?.phone} · Địa
            chỉ: {active?.default_address}
          </p>
          <p className="mt-2 text-[11px] text-slate-500">
            Mỗi trình duyệt sở hữu một token phiên ngẫu nhiên có thời hạn và có thể thu hồi. Hệ
            thống không bao giờ công khai token của khách hàng khác trong API bootstrap.
          </p>
        </div>

        {notice && (
          <div className="rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-2.5 text-xs font-semibold text-emerald-800">
            {notice}
          </div>
        )}

        <form
          onSubmit={createNewGuestCustomer}
          className="space-y-3 rounded-2xl border border-slate-200 bg-white p-5 shadow-2xs"
        >
          <h2 className="text-sm font-extrabold text-slate-900">
            Tạo phiên khách hàng mới (kiểm thử mua đồng thời / cô lập đơn hàng)
          </h2>
          <div className="grid gap-3 sm:grid-cols-2">
            <input
              required
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="Tên hiển thị (VD: Lê Quốc Bảo)"
              className="rounded-xl border border-slate-300 px-3 py-2 text-xs outline-none focus:border-[#fe2c55]"
            />
            <input
              required
              value={newPhone}
              onChange={(e) => setNewPhone(e.target.value)}
              placeholder="Số điện thoại"
              className="rounded-xl border border-slate-300 px-3 py-2 text-xs outline-none focus:border-[#fe2c55]"
            />
          </div>
          <input
            required
            value={newAddress}
            onChange={(e) => setNewAddress(e.target.value)}
            placeholder="Địa chỉ giao hàng mặc định"
            className="w-full rounded-xl border border-slate-300 px-3 py-2 text-xs outline-none focus:border-[#fe2c55]"
          />
          <button
            type="submit"
            disabled={busy}
            className="rounded-xl bg-[#fe2c55] px-4 py-2.5 text-xs font-bold text-white hover:bg-[#e02047] disabled:bg-slate-300"
          >
            Tạo & chuyển sang phiên khách mới
          </button>
        </form>
      </div>
    </StoreShell>
  );
}

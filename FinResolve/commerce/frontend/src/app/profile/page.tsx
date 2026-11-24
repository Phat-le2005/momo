"use client";

import { useEffect, useState } from "react";
import { StoreShell } from "@/components/StoreShell";
import {
  type CustomerIdentityDTO,
  type StoreBootstrapDTO,
  commerceFetch,
  ensureCustomerBootstrap,
  setStoredCustomerToken,
} from "@/lib/commerce-api";

export default function ProfilePage() {
  const [bootstrap, setBootstrap] = useState<StoreBootstrapDTO | null>(null);
  const [active, setActive] = useState<CustomerIdentityDTO | null>(null);
  const [newName, setNewName] = useState("");
  const [newPhone, setNewPhone] = useState("0905556677");
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    async function init() {
      const { bootstrap: b, activeCustomer } = await ensureCustomerBootstrap();
      if (!mounted) return;
      setBootstrap(b);
      setActive(activeCustomer);
    }
    void init();
    return () => {
      mounted = false;
    };
  }, []);

  function switchIdentity(c: CustomerIdentityDTO) {
    setStoredCustomerToken(c.session_token);
    setActive(c);
    setNotice(`Đã chuyển sang tài khoản test: ${c.display_name} (cô lập hoàn toàn giỏ hàng & đơn hàng).`);
  }

  async function createNewTestCustomer(e: React.FormEvent) {
    e.preventDefault();
    if (!newName.trim()) return;
    const created = await commerceFetch<CustomerIdentityDTO>("/api/v1/store/customers/session", {
      method: "POST",
      body: JSON.stringify({
        display_name: newName.trim(),
        phone: newPhone.trim(),
        default_address: "TP. Hồ Chí Minh (Khách Test)",
      }),
    });
    setStoredCustomerToken(created.session_token);
    setActive(created);
    setNewName("");
    setNotice(`Đã tạo & kích hoạt danh tính khách test mới: ${created.display_name}`);
  }

  return (
    <StoreShell customerName={active?.display_name}>
      <div className="mx-auto max-w-2xl space-y-5">
        <div className="rounded-2xl border border-slate-200 bg-white p-5">
          <span className="rounded-full bg-slate-100 px-2.5 py-0.5 text-[11px] font-bold text-slate-600">
            DANH TÍNH KHÁCH HÀNG THỬ NGHIỆM (SESSION-SCOPED)
          </span>
          <h1 className="mt-2 text-lg font-extrabold text-slate-900">
            {active?.display_name || "Đang tải..."}
          </h1>
          <p className="mt-1 text-xs text-slate-600">
            Mã khách: <code>{active?.id}</code> · SĐT: {active?.phone} · Địa chỉ:{" "}
            {active?.default_address}
          </p>
          <p className="mt-2 text-[11px] text-slate-500">
            Mỗi khách hàng bị cô lập hoàn toàn về giỏ hàng, voucher và đơn hàng — khách hàng này
            không thể truy cập đơn hàng của khách hàng khác.
          </p>
        </div>

        {notice && (
          <div className="rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-2.5 text-xs font-semibold text-emerald-800">
            {notice}
          </div>
        )}

        <div className="rounded-2xl border border-slate-200 bg-white p-5">
          <h2 className="text-sm font-extrabold text-slate-900">
            Chuyển nhanh tài khoản khách thử nghiệm (để test mua đồng thời / cô lập đơn hàng)
          </h2>
          <div className="mt-3 space-y-2">
            {(bootstrap?.test_customers || []).map((c) => {
              const isCurrent = c.id === active?.id;
              return (
                <div
                  key={c.id}
                  className={`flex items-center justify-between rounded-xl border p-3 ${
                    isCurrent ? "border-[#fe2c55] bg-rose-50/40" : "border-slate-200"
                  }`}
                >
                  <div>
                    <p className="text-xs font-bold text-slate-900">{c.display_name}</p>
                    <p className="text-[11px] text-slate-500">
                      {c.id} · {c.phone}
                    </p>
                  </div>
                  <button
                    type="button"
                    disabled={isCurrent}
                    onClick={() => switchIdentity(c)}
                    className="rounded-xl bg-[#161823] px-3 py-1.5 text-xs font-bold text-white disabled:bg-slate-300"
                  >
                    {isCurrent ? "Đang dùng" : "Chọn"}
                  </button>
                </div>
              );
            })}
          </div>
        </div>

        <form
          onSubmit={createNewTestCustomer}
          className="space-y-3 rounded-2xl border border-slate-200 bg-white p-5"
        >
          <h2 className="text-sm font-extrabold text-slate-900">Tạo thêm khách hàng test mới</h2>
          <div className="grid gap-3 sm:grid-cols-2">
            <input
              required
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="Tên hiển thị (VD: Lê Quốc Bảo)"
              className="rounded-xl border border-slate-300 px-3 py-2 text-xs outline-none"
            />
            <input
              required
              value={newPhone}
              onChange={(e) => setNewPhone(e.target.value)}
              placeholder="Số điện thoại"
              className="rounded-xl border border-slate-300 px-3 py-2 text-xs outline-none"
            />
          </div>
          <button
            type="submit"
            className="rounded-xl bg-[#fe2c55] px-4 py-2 text-xs font-bold text-white"
          >
            Tạo danh tính test mới
          </button>
        </form>
      </div>
    </StoreShell>
  );
}

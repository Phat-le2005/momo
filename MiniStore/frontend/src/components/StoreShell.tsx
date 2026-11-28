"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { VNPAY_SANDBOX_STOREFRONT } from "@/lib/commerce-api";

type NavKey = "home" | "live" | "cart" | "orders" | "profile";

const NAV_ITEMS: Array<{ href: string; label: string; key: NavKey }> = [
  { href: "/", label: "Trang chủ", key: "home" },
  { href: "/live", label: "LIVE", key: "live" },
  { href: "/cart", label: "Giỏ hàng", key: "cart" },
  { href: "/orders", label: "Đơn hàng", key: "orders" },
  { href: "/profile", label: "Tài khoản", key: "profile" },
];

function NavIcon({ kind, active }: { kind: NavKey; active: boolean }) {
  const stroke = active ? "#fe2c55" : "currentColor";
  switch (kind) {
    case "home":
      return (
        <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke={stroke} strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M3 10.5L12 3l9 7.5V20a1 1 0 01-1 1h-5v-6H9v6H4a1 1 0 01-1-1v-9.5z" />
        </svg>
      );
    case "live":
      return (
        <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke={stroke} strokeWidth={2}>
          <rect x="3" y="6" width="14" height="12" rx="2.5" />
          <path strokeLinecap="round" strokeLinejoin="round" d="M17 10l4-2.5v9L17 14" />
          <circle cx="7.5" cy="10" r="1.5" fill={active ? "#fe2c55" : "#ef4444"} stroke="none" />
        </svg>
      );
    case "cart":
      return (
        <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke={stroke} strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M3 4h2l2.4 11.2a1 1 0 001 .8h9.7a1 1 0 001-.8L21 8H7" />
          <circle cx="9.5" cy="19.5" r="1.25" fill={stroke} stroke="none" />
          <circle cx="17.5" cy="19.5" r="1.25" fill={stroke} stroke="none" />
        </svg>
      );
    case "orders":
      return (
        <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke={stroke} strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M4 7.5L12 3l8 4.5v9L12 21l-8-4.5v-9z" />
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 12l8-4.5M12 12v9M12 12L4 7.5" />
        </svg>
      );
    case "profile":
      return (
        <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke={stroke} strokeWidth={2}>
          <circle cx="12" cy="8" r="3.5" />
          <path strokeLinecap="round" strokeLinejoin="round" d="M5 20a7 7 0 0114 0" />
        </svg>
      );
  }
}

export function StoreShell({
  children,
  cartCount = 0,
  customerName,
}: {
  children: ReactNode;
  cartCount?: number;
  customerName?: string;
}) {
  const pathname = usePathname();

  return (
    <div className="min-h-screen bg-[#f5f5f7] text-[#161823] pb-20 md:pb-10">
      <header className="sticky top-0 z-30 border-b border-slate-200/80 bg-white/95 backdrop-blur-md">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-3 px-4 py-3">
          <Link href="/" className="flex items-center gap-2.5">
            <span className="inline-flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-[#fe2c55] to-[#e01e48] text-sm font-black tracking-tight text-white shadow-sm">
              FS
            </span>
            <div className="leading-tight">
              <span className="text-base font-extrabold tracking-tight text-[#161823]">
                FinShop <span className="text-[#fe2c55]">LIVE</span>
              </span>
              <span className="ml-2 rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-bold tracking-wide text-slate-600">
                {VNPAY_SANDBOX_STOREFRONT ? "VNPAY SANDBOX" : "MOCK SANDBOX"}
              </span>
            </div>
          </Link>

          <nav className="hidden items-center gap-6 text-sm font-semibold text-slate-600 md:flex">
            {NAV_ITEMS.map((item) => {
              const active = pathname === item.href;
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`flex items-center gap-1.5 transition ${
                    active ? "text-[#fe2c55]" : "hover:text-[#161823]"
                  }`}
                >
                  {item.key === "live" && (
                    <span className="inline-block h-2 w-2 rounded-full bg-[#fe2c55]" />
                  )}
                  <span>{item.label}</span>
                  {item.href === "/cart" && cartCount > 0 && (
                    <span className="rounded-full bg-[#fe2c55] px-1.5 py-0.5 text-[11px] font-bold text-white">
                      {cartCount}
                    </span>
                  )}
                </Link>
              );
            })}
          </nav>

          <div className="flex items-center gap-2">
            {customerName && (
              <Link
                href="/profile"
                className="hidden items-center gap-1.5 rounded-full bg-slate-100 px-3 py-1.5 text-xs font-semibold text-slate-700 transition hover:bg-slate-200 sm:inline-flex"
              >
                <span className="h-2 w-2 rounded-full bg-emerald-500" />
                {customerName}
              </Link>
            )}
            {!VNPAY_SANDBOX_STOREFRONT && (
              <Link
                href="/merchant"
                className="rounded-full border border-slate-300 bg-white px-3.5 py-1.5 text-xs font-bold text-slate-800 transition hover:border-[#fe2c55] hover:text-[#fe2c55]"
              >
                Kênh Người Bán
              </Link>
            )}
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-3 py-5 sm:px-4">{children}</main>

      {/* Mobile Sticky Bottom Navigation with Crisp SVG Icons */}
      <nav
        aria-label="Điều hướng chính"
        className="fixed inset-x-0 bottom-0 z-40 border-t border-slate-200 bg-white/95 backdrop-blur-md md:hidden"
      >
        <div className="grid grid-cols-5">
          {NAV_ITEMS.map((item) => {
            const active = pathname === item.href;
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`relative flex flex-col items-center justify-center py-2 text-[11px] font-semibold transition ${
                  active ? "text-[#fe2c55]" : "text-slate-500"
                }`}
              >
                <NavIcon kind={item.key} active={active} />
                <span className="mt-0.5">{item.label}</span>
                {item.href === "/cart" && cartCount > 0 && (
                  <span className="absolute right-3 top-1 rounded-full bg-[#fe2c55] px-1.5 py-0.25 text-[10px] font-bold text-white">
                    {cartCount}
                  </span>
                )}
              </Link>
            );
          })}
        </div>
      </nav>
    </div>
  );
}

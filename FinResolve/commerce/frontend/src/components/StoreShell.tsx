"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

const NAV_ITEMS = [
  { href: "/", label: "Trang chủ", icon: "🏠" },
  { href: "/live", label: "LIVE", icon: "🔴" },
  { href: "/cart", label: "Giỏ hàng", icon: "🛒" },
  { href: "/orders", label: "Đơn hàng", icon: "📦" },
  { href: "/profile", label: "Tài khoản", icon: "👤" },
];

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
    <div className="min-h-screen bg-[#f7f7f9] pb-20 md:pb-8">
      <header className="sticky top-0 z-30 border-b border-slate-200 bg-white/95 backdrop-blur">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-3 px-4 py-3">
          <Link href="/" className="flex items-center gap-2">
            <span className="inline-flex h-8 w-8 items-center justify-center rounded-xl bg-[#fe2c55] text-sm font-black text-white">
              FS
            </span>
            <div className="leading-tight">
              <span className="text-base font-extrabold tracking-tight text-[#161823]">
                FinShop <span className="text-[#fe2c55]">LIVE</span>
              </span>
              <span className="ml-2 rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-semibold text-slate-600">
                MOCK TEST
              </span>
            </div>
          </Link>

          <nav className="hidden items-center gap-5 text-sm font-semibold text-slate-700 md:flex">
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
                className="hidden rounded-full bg-slate-100 px-3 py-1 text-xs font-medium text-slate-700 hover:bg-slate-200 sm:inline-block"
              >
                {customerName}
              </Link>
            )}
            <Link
              href="/merchant"
              className="rounded-full border border-slate-300 bg-white px-3 py-1.5 text-xs font-semibold text-slate-800 hover:border-[#fe2c55] hover:text-[#fe2c55]"
            >
              Kênh Người Bán
            </Link>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-3 py-4 sm:px-4">{children}</main>

      {/* Mobile Sticky Bottom Navigation */}
      <nav
        aria-label="Điều hướng chính"
        className="fixed inset-x-0 bottom-0 z-40 border-t border-slate-200 bg-white md:hidden"
      >
        <div className="grid grid-cols-5">
          {NAV_ITEMS.map((item) => {
            const active = pathname === item.href;
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`relative flex flex-col items-center justify-center py-2 text-[11px] font-semibold ${
                  active ? "text-[#fe2c55]" : "text-slate-500"
                }`}
              >
                <span className="text-base leading-none">{item.icon}</span>
                <span className="mt-1">{item.label}</span>
                {item.href === "/cart" && cartCount > 0 && (
                  <span className="absolute right-3 top-1 rounded-full bg-[#fe2c55] px-1.5 text-[10px] font-bold text-white">
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

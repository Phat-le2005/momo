"use client";

import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { SiteHeader } from "./SiteHeader";

/**
 * Renders the dedicated full-bleed layout for the Landing Page (`/`) and
 * Unified Guardian Command Center (`/experience`), while preserving the
 * existing SiteHeader + container layout on all legacy routes (`/demo`,
 * `/cases`, `/risks`, `/live`, `/judge`).
 */
export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const isFullBleed = pathname === "/" || pathname.startsWith("/experience");

  if (isFullBleed) {
    return (
      <main id="main" className="min-h-screen w-full">
        {children}
      </main>
    );
  }

  return (
    <>
      <SiteHeader />
      <main id="main" className="mx-auto w-full max-w-6xl px-4 py-8">
        {children}
      </main>
      <footer className="mx-auto w-full max-w-6xl px-4 pb-8 text-xs text-slate-500">
        Toàn bộ dữ liệu là mô phỏng (MOCK). Không kết nối hệ thống thanh toán thật, không hoàn
        tiền thật. Vui lòng không nhập thông tin tài khoản ngân hàng hay mật khẩu.
      </footer>
    </>
  );
}

import Link from "next/link";
import { ApiHealthIndicator } from "./ApiHealth";
import { MockBadge } from "./MockBadge";

const NAV = [
  { href: "/experience", label: "Command Center" },
  { href: "/demo", label: "Mission Control" },
  { href: "/cases", label: "Hồ sơ (Cases)" },
  { href: "/risks", label: "Rủi ro (Risks)" },
  { href: "/live", label: "Sự cố & Taskforce" },
  { href: "/judge", label: "Judge Lab" },
];

export function SiteHeader() {
  return (
    <header className="border-b border-[#E2E8F0] bg-white">
      <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center justify-between gap-3 px-4 py-3">
        <div className="flex items-center gap-3">
          <Link href="/" className="text-lg font-bold tracking-tight text-[#2563EB]">
            FinResolve AI
          </Link>
          <MockBadge compact />
        </div>
        <nav aria-label="Điều hướng chính">
          <ul className="flex flex-wrap gap-1 text-sm">
            {NAV.map((item) => (
              <li key={item.href}>
                <Link
                  href={item.href}
                  className="rounded-md px-3 py-1.5 text-slate-700 hover:bg-slate-100 hover:text-slate-900"
                >
                  {item.label}
                </Link>
              </li>
            ))}
          </ul>
        </nav>
        <ApiHealthIndicator />
      </div>
    </header>
  );
}

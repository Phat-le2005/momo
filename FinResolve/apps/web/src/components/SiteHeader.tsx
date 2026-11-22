import Link from "next/link";
import { ApiHealthIndicator } from "./ApiHealth";
import { MockBadge } from "./MockBadge";

const NAV = [
  { href: "/demo", label: "Demo" },
  { href: "/cases", label: "Case" },
  { href: "/risks", label: "Rủi ro" },
  { href: "/live", label: "Mega Live" },
];

export function SiteHeader() {
  return (
    <header className="border-b border-slate-200 bg-white">
      <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center justify-between gap-3 px-4 py-3">
        <div className="flex items-center gap-3">
          <Link href="/demo" className="text-lg font-bold text-indigo-700">
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

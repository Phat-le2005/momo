"use client";

export type GuardianVisualState =
  | "MONITORING"
  | "INVESTIGATING"
  | "RECOVERING"
  | "WAITING_APPROVAL"
  | "VERIFIED"
  | "DISCONNECTED";

const STATE_CONFIG: Record<
  GuardianVisualState,
  {
    label: string;
    ringClass: string;
    badgeClass: string;
    stroke: string;
    animateSpin?: boolean;
    animatePulse?: boolean;
  }
> = {
  MONITORING: {
    label: "Đang giám sát",
    ringClass: "border-blue-200 bg-blue-50/80",
    badgeClass: "bg-blue-50 text-blue-700 border-blue-200",
    stroke: "#2563EB",
    animatePulse: true,
  },
  INVESTIGATING: {
    label: "AI đang điều tra",
    ringClass: "border-sky-300 bg-sky-50",
    badgeClass: "bg-sky-50 text-sky-700 border-sky-200",
    stroke: "#0EA5E9",
    animateSpin: true,
  },
  RECOVERING: {
    label: "Đang phục hồi an toàn",
    ringClass: "border-blue-300 bg-blue-50",
    badgeClass: "bg-blue-50 text-blue-700 border-blue-300",
    stroke: "#2563EB",
    animateSpin: true,
  },
  WAITING_APPROVAL: {
    label: "Cần bạn xác nhận",
    ringClass: "border-amber-200 bg-amber-50",
    badgeClass: "bg-amber-50 text-amber-800 border-amber-200",
    stroke: "#F59E0B",
  },
  VERIFIED: {
    label: "Đã xác minh phục hồi",
    ringClass: "border-emerald-200 bg-emerald-50",
    badgeClass: "bg-emerald-50 text-emerald-700 border-emerald-200",
    stroke: "#10B981",
  },
  DISCONNECTED: {
    label: "Mất kết nối nguồn",
    ringClass: "border-red-200 bg-red-50",
    badgeClass: "bg-red-50 text-red-700 border-red-200",
    stroke: "#EF4444",
  },
};

/**
 * FinResolve Guardian Visual Identity: Shield + AI Network SVG with truthful backend states.
 * Never animates investigation unless backend actually reports active work.
 */
export function GuardianBadge({
  state,
  compact = false,
}: {
  state: GuardianVisualState;
  compact?: boolean;
}) {
  const cfg = STATE_CONFIG[state] ?? STATE_CONFIG.DISCONNECTED;

  return (
    <div
      className={`inline-flex items-center gap-2.5 rounded-full border px-3 py-1.5 text-xs font-semibold ${cfg.badgeClass}`}
      aria-label={`Trạng thái Guardian: ${cfg.label}`}
    >
      <span
        className={`relative flex h-6 w-6 items-center justify-center rounded-full border ${cfg.ringClass}`}
      >
        {cfg.animateSpin && (
          <span
            aria-hidden="true"
            className="fr-spin-slow absolute inset-0 rounded-full border-2 border-transparent border-t-blue-600"
          />
        )}
        <svg
          viewBox="0 0 24 24"
          fill="none"
          className={`h-4 w-4 ${cfg.animatePulse ? "fr-pulse-soft" : ""}`}
          aria-hidden="true"
        >
          <path
            d="M12 2.5L4.5 5.8V11.4C4.5 16.2 7.7 20.5 12 21.8C16.3 20.5 19.5 16.2 19.5 11.4V5.8L12 2.5Z"
            stroke={cfg.stroke}
            strokeWidth="1.75"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
          <circle cx="12" cy="10.2" r="1.4" fill={cfg.stroke} />
          <circle cx="9.2" cy="14" r="1.1" fill={cfg.stroke} />
          <circle cx="14.8" cy="14" r="1.1" fill={cfg.stroke} />
          <path
            d="M12 11.6L9.9 13.3M12 11.6L14.1 13.3M10.3 14H13.7"
            stroke={cfg.stroke}
            strokeWidth="1.2"
            strokeLinecap="round"
          />
        </svg>
      </span>
      {!compact && <span>{cfg.label}</span>}
    </div>
  );
}

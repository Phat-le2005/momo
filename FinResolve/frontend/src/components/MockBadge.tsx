type Props = { compact?: boolean };

/** Always-visible label: data in this build is synthetic, never a live payment provider. */
export function MockBadge({ compact = false }: Props) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border border-amber-400 bg-amber-100 font-bold uppercase tracking-wide text-amber-900 ${
        compact ? "px-2 py-0.5 text-[10px]" : "px-3 py-1 text-xs"
      }`}
      title="Dữ liệu mô phỏng — không phải giao dịch thật"
      data-testid="mock-badge"
    >
      MOCK{compact ? "" : " · Dữ liệu mô phỏng"}
    </span>
  );
}

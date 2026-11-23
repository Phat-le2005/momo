import type { Metadata } from "next";
import { RiskWorkspace } from "@/components/RiskWorkspace";

export const metadata: Metadata = { title: "Risk Center — FinResolve AI" };
export default function RisksPage() { return <RiskWorkspace />; }

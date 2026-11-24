import type { Metadata } from "next";
import { CasesWorkspace } from "@/components/CasesWorkspace";

export const metadata: Metadata = { title: "Case Center — FinResolve AI" };
export default function CasesPage() { return <CasesWorkspace />; }

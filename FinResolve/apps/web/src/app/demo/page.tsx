import type { Metadata } from "next";
import { DemoDashboard } from "@/components/DemoDashboard";

export const metadata: Metadata = { title: "Demo tự phục vụ — FinResolve AI" };
export default function DemoPage() { return <DemoDashboard />; }

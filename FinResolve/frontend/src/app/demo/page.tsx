import type { Metadata } from "next";
import { DemoDashboard } from "@/components/DemoDashboard";

export const metadata: Metadata = { title: "AI Mission Control — FinResolve AI" };
export default function DemoPage() { return <DemoDashboard />; }

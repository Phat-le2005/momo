import type { Metadata } from "next";
import { LiveWorkspace } from "@/components/LiveWorkspace";

export const metadata: Metadata = { title: "Mega Live — FinResolve AI" };
export default function LivePage() { return <LiveWorkspace />; }

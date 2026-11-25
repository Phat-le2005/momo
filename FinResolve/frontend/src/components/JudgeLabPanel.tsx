"use client";

import { useState } from "react";
import { Panel, StatusPill, primaryButton, secondaryButton } from "@/components/StatusPill";
import { API_BASE_URL } from "@/lib/api";

type BlindChallengeRun = {
  id: string;
  challenge_family: string;
  seed: number;
  state: string;
  affected_entities: Record<string, unknown>;
  hidden_ground_truth: Record<string, unknown>;
  expected_constraints: Record<string, unknown>;
  oracle_evaluation: Record<string, unknown>;
  created_at: string;
};

type HarnessResult = {
  seed: number;
  evaluation_mode: string;
  real_llm_status: string;
  disclaimer: string;
  baselines: Array<{
    configuration: string;
    trials_run: number;
    known_detection_recall: number;
    novel_anomaly_detection_rate: number;
    false_positive_rate: number;
    appropriate_action_rate: number;
    unsafe_action_count: number;
    verified_recovery_rate: number;
    total_tool_calls: number;
  }>;
};

const FAMILIES = [
  "C01",
  "C02",
  "C03",
  "C04",
  "C05",
  "C06",
  "C07",
  "C08",
  "NEGATIVE_CONTROL",
] as const;

export function JudgeLabPanel() {
  const [sessionToken, setSessionToken] = useState("");
  const [judgeToken, setJudgeToken] = useState("");
  const [family, setFamily] = useState<string>("C01");
  const [seed, setSeed] = useState<number>(42);
  const [randomMode, setRandomMode] = useState<boolean>(false);
  const [runs, setRuns] = useState<BlindChallengeRun[]>([]);
  const [harness, setHarness] = useState<HarnessResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const judgeFetch = async <T,>(path: string, method: "GET" | "POST" = "GET", body?: unknown): Promise<T> => {
    const res = await fetch(`${API_BASE_URL}${path}`, {
      method,
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-Demo-Session": sessionToken.trim(),
        "X-Judge-Token": judgeToken.trim(),
      },
      body: body ? JSON.stringify(body) : undefined,
      cache: "no-store",
    });
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      throw new Error(data?.error?.message || `HTTP ${res.status}`);
    }
    return data as T;
  };

  const refreshRuns = async () => {
    setBusy(true);
    setErr(null);
    try {
      const list = await judgeFetch<BlindChallengeRun[]>("/api/v1/judge/blind-challenges");
      setRuns(list);
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "Failed to load Judge runs");
    } finally {
      setBusy(false);
    }
  };

  const handleStart = async () => {
    setBusy(true);
    setErr(null);
    try {
      await judgeFetch<BlindChallengeRun>("/api/v1/judge/blind-challenges", "POST", {
        challenge_family: randomMode ? null : family,
        random_selection: randomMode,
        seed,
      });
      await refreshRuns();
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "Failed to start challenge");
      setBusy(false);
    }
  };

  const handleEvaluate = async (runId: string) => {
    setBusy(true);
    setErr(null);
    try {
      await judgeFetch(`/api/v1/judge/blind-challenges/${runId}/evaluate`, "POST");
      await refreshRuns();
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "Evaluation failed");
      setBusy(false);
    }
  };

  const handleAbort = async (runId: string) => {
    setBusy(true);
    setErr(null);
    try {
      await judgeFetch(`/api/v1/judge/blind-challenges/${runId}/abort`, "POST");
      await refreshRuns();
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "Abort failed");
      setBusy(false);
    }
  };

  const handleRunHarness = async () => {
    setBusy(true);
    setErr(null);
    try {
      const res = await judgeFetch<HarnessResult>(
        `/api/v1/judge/evaluation-harness/run?seed=${seed}`,
        "POST",
      );
      setHarness(res);
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "Harness failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-6">
      <Panel
        title="Open-World Test Lab & Independent Oracle"
        subtitle="Giao diện thử nghiệm độc lập (yêu cầu X-Judge-Token). C01–C08 chỉ là bộ kiểm thử hồi quy; FinResolve vận hành theo kiến trúc Open-World Autonomous Guardian."
      >
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className="block text-xs font-semibold text-slate-700">
                Target Demo Session Token (X-Demo-Session)
              </label>
              <input
                type="password"
                value={sessionToken}
                onChange={(e) => setSessionToken(e.target.value)}
                placeholder="dsk_..."
                className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-1.5 text-xs"
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-700">
                Judge Admin Secret (X-Judge-Token)
              </label>
              <input
                type="password"
                value={judgeToken}
                onChange={(e) => setJudgeToken(e.target.value)}
                placeholder="finresolve-judge-lab-..."
                className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-1.5 text-xs"
              />
            </div>
          </div>

          <div className="flex flex-wrap items-end gap-3">
            <div>
              <label className="block text-xs font-semibold text-slate-700">Challenge Family</label>
              <select
                value={family}
                disabled={randomMode}
                onChange={(e) => setFamily(e.target.value)}
                className="mt-1 rounded-lg border border-slate-300 px-3 py-1.5 text-xs"
              >
                {FAMILIES.map((f) => (
                  <option key={f} value={f}>
                    {f}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-700">Seed</label>
              <input
                type="number"
                value={seed}
                onChange={(e) => setSeed(Number(e.target.value) || 42)}
                className="mt-1 w-28 rounded-lg border border-slate-300 px-3 py-1.5 text-xs"
              />
            </div>
            <label className="flex items-center gap-1.5 text-xs text-slate-700">
              <input
                type="checkbox"
                checked={randomMode}
                onChange={(e) => setRandomMode(e.target.checked)}
              />
              Seeded Random
            </label>
            <button type="button" disabled={busy} onClick={handleStart} className={primaryButton}>
              Start Blind Challenge
            </button>
            <button type="button" disabled={busy} onClick={refreshRuns} className={secondaryButton}>
              Refresh Runs
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={handleRunHarness}
              className={secondaryButton}
            >
              Run Baseline Harness
            </button>
          </div>

          {err && (
            <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
              {err}
            </div>
          )}
        </div>
      </Panel>

      {harness && (
        <Panel
          title="Reproducible Baseline Evaluation (Seeded)"
          subtitle={`${harness.evaluation_mode} • ${harness.real_llm_status}`}
        >
          <p className="mb-3 text-xs text-slate-600">{harness.disclaimer}</p>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-slate-200 text-slate-500">
                  <th className="py-2 pr-3">Configuration</th>
                  <th className="py-2 pr-3">Known Recall</th>
                  <th className="py-2 pr-3">Novel Anomaly</th>
                  <th className="py-2 pr-3">False Positive</th>
                  <th className="py-2 pr-3">Tool Calls</th>
                  <th className="py-2 pr-3">Unsafe Actions</th>
                  <th className="py-2">Verified Recovery</th>
                </tr>
              </thead>
              <tbody>
                {harness.baselines.map((b) => (
                  <tr key={b.configuration} className="border-b border-slate-100">
                    <td className="py-2 pr-3 font-semibold text-slate-800">{b.configuration}</td>
                    <td className="py-2 pr-3">{(b.known_detection_recall * 100).toFixed(0)}%</td>
                    <td className="py-2 pr-3">{(b.novel_anomaly_detection_rate * 100).toFixed(0)}%</td>
                    <td className="py-2 pr-3">{(b.false_positive_rate * 100).toFixed(0)}%</td>
                    <td className="py-2 pr-3">{b.total_tool_calls}</td>
                    <td className="py-2 pr-3">{b.unsafe_action_count}</td>
                    <td className="py-2">{(b.verified_recovery_rate * 100).toFixed(0)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      )}

      <Panel title="Blind Challenge Runs (Hidden Ground Truth vs Independent Oracle)">
        {runs.length === 0 ? (
          <div className="text-xs text-slate-500">
            Chưa có challenge nào trong phiên. Nhập token và bấm Refresh Runs hoặc Start Blind Challenge.
          </div>
        ) : (
          <div className="space-y-3">
            {runs.map((r) => (
              <div key={r.id} className="rounded-xl border border-slate-200 bg-white p-3 text-xs space-y-2">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className="font-mono font-bold text-slate-900">{r.id}</span>
                    <StatusPill value={r.challenge_family} />
                    <StatusPill value={r.state} />
                    <span className="text-slate-500">seed={r.seed}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => handleEvaluate(r.id)}
                      className={primaryButton}
                    >
                      Run Independent Oracle
                    </button>
                    {r.state === "RUNNING" && (
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => handleAbort(r.id)}
                        className={secondaryButton}
                      >
                        Abort
                      </button>
                    )}
                  </div>
                </div>

                <div className="grid gap-3 md:grid-cols-2">
                  <div className="rounded-lg border border-amber-200 bg-amber-50 p-2.5">
                    <div className="mb-1 font-semibold text-amber-800">
                      Hidden Ground Truth (Judge-Only)
                    </div>
                    <pre className="max-h-40 overflow-auto font-mono text-[11px] text-amber-950">
                      {JSON.stringify(
                        {
                          affected_entities: r.affected_entities,
                          ground_truth: r.hidden_ground_truth,
                          constraints: r.expected_constraints,
                        },
                        null,
                        2,
                      )}
                    </pre>
                  </div>
                  <div className="rounded-lg border border-emerald-200 bg-emerald-50 p-2.5">
                    <div className="mb-1 font-semibold text-emerald-800">
                      Independent Oracle Verdict
                    </div>
                    <pre className="max-h-40 overflow-auto font-mono text-[11px] text-emerald-950">
                      {Object.keys(r.oracle_evaluation || {}).length > 0
                        ? JSON.stringify(r.oracle_evaluation, null, 2)
                        : "Chưa đánh giá — bấm 'Run Independent Oracle'."}
                    </pre>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </Panel>
    </div>
  );
}

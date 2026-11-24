import assert from "node:assert/strict";
import test from "node:test";
import {
  clearSessionRunId,
  loadSessionRunId,
  mergeSupervisorActivities,
  resolveCaseAiModeLabel,
  resolveRestoredInvestigationJob,
  resolveSelectedCaseId,
  resolveSupervisorExecutionBadge,
  resolveTaskforceModeLabel,
  saveSessionRunId,
  type StorageLike,
} from "./session-state.ts";

function makeMemoryStorage(): StorageLike & { dump(): Record<string, string> } {
  const map = new Map<string, string>();
  return {
    getItem: (key: string) => map.get(key) ?? null,
    setItem: (key: string, value: string) => {
      map.set(key, value);
    },
    removeItem: (key: string) => {
      map.delete(key);
    },
    dump: () => Object.fromEntries(map.entries()),
  };
}

test("STEP 2A: resolveSelectedCaseId drops stale cross-session Case IDs", () => {
  // Old session selected CASE-A-1; new session only has CASE-B-1 and CASE-B-2
  const next = resolveSelectedCaseId("CASE-A-1", "CASE-A-1", ["CASE-B-1", "CASE-B-2"]);
  assert.equal(next, "CASE-B-1");

  // New session with zero Cases clears selection to null
  assert.equal(resolveSelectedCaseId("CASE-A-1", "CASE-A-1", []), null);

  // Valid URL param inside current session is honored
  assert.equal(resolveSelectedCaseId(null, "CASE-B-2", ["CASE-B-1", "CASE-B-2"]), "CASE-B-2");
});

test("STEP 2B: scenario run_id storage is strictly scoped to session_id", () => {
  const storage = makeMemoryStorage();
  storage.setItem("finresolve_last_run_id_v1", "RUN-LEGACY-LEAK");

  saveSessionRunId(storage, "sess_A", "RUN-SYNC-AAA");
  assert.equal(loadSessionRunId(storage, "sess_A"), "RUN-SYNC-AAA");
  // Session B must NEVER see Session A's run_id or legacy key
  assert.equal(loadSessionRunId(storage, "sess_B"), null);
  assert.equal(storage.getItem("finresolve_last_run_id_v1"), null);

  // Clearing Session A removes only Session A's run
  saveSessionRunId(storage, "sess_B", "RUN-ML-BBB");
  clearSessionRunId(storage, "sess_A");
  assert.equal(loadSessionRunId(storage, "sess_A"), null);
  assert.equal(loadSessionRunId(storage, "sess_B"), "RUN-ML-BBB");
});

test("STEP 2C: honest AI labels distinguish uninvestigated, DEMO_RULES, LLM_API, and Taskforce", () => {
  const uninvestigated = resolveCaseAiModeLabel(null);
  assert.equal(uninvestigated.badge, "CHƯA ĐIỀU TRA");
  assert.equal(uninvestigated.isInvestigated, false);
  assert.equal(uninvestigated.isLlm, false);

  const demoRules = resolveCaseAiModeLabel("DEMO_RULES");
  assert.equal(demoRules.badge, "DEMO_RULES");
  assert.equal(demoRules.isInvestigated, true);
  assert.equal(demoRules.isLlm, false);

  const llmApi = resolveCaseAiModeLabel("LLM_API");
  assert.equal(llmApi.badge, "LLM_API");
  assert.equal(llmApi.isInvestigated, true);
  assert.equal(llmApi.isLlm, true);

  // Deterministic Taskforce must never be labeled as LLM reasoning
  const detTf = resolveTaskforceModeLabel("DETERMINISTIC_TASKFORCE_RULES", false);
  assert.equal(detTf.isLlm, false);
  assert.match(detTf.note, /0 LLM calls/);

  const llmTf = resolveTaskforceModeLabel("LLM_API", true);
  assert.equal(llmTf.isLlm, true);
});

test("Prompt 08.3.2 Scenario A: RUNNING investigation with active_run_id restores polling", () => {
  const res = resolveRestoredInvestigationJob({
    investigation_status: "RUNNING",
    active_run_id: "RUN-ACTIVE-42",
  });
  assert.equal(res.jobId, "RUN-ACTIVE-42");
  assert.equal(res.shouldPoll, true);
  assert.ok(res.notice && res.notice.includes("RUN-ACTIVE-42"));
});

test("Prompt 08.3.2 Scenario E: terminal/idle/missing statuses never poll or auto-start investigation", () => {
  for (const status of ["IDLE", "COMPLETED", "FAILED"] as const) {
    const res = resolveRestoredInvestigationJob({
      investigation_status: status,
      active_run_id: "RUN-STALE-IGNORED",
    });
    assert.equal(res.jobId, null);
    assert.equal(res.shouldPoll, false);
    assert.equal(res.notice, null);
  }

  // INTERRUPTED shows safe recovery notice and never polls
  const interrupted = resolveRestoredInvestigationJob({
    investigation_status: "INTERRUPTED",
    active_run_id: null,
  });
  assert.equal(interrupted.jobId, null);
  assert.equal(interrupted.shouldPoll, false);
  assert.ok(interrupted.notice && interrupted.notice.includes("INTERRUPTED"));

  // RUNNING without an active_run_id must not start polling
  const noRunId = resolveRestoredInvestigationJob({
    investigation_status: "RUNNING",
    active_run_id: "   ",
  });
  assert.equal(noRunId.jobId, null);
  assert.equal(noRunId.shouldPoll, false);

  // Null/undefined status never polls
  assert.deepEqual(resolveRestoredInvestigationJob(null), {
    jobId: null,
    shouldPoll: false,
    notice: null,
  });
});

test("Prompt 09 Part A.3: resolveSupervisorExecutionBadge honestly distinguishes configured vs invoked LLM", () => {
  // Configured key before any invocation must NOT claim LLM_CALL_SUCCEEDED or isThinking
  const configured = resolveSupervisorExecutionBadge("LLM_API_CONFIGURED", "LLM_API");
  assert.equal(configured.code, "LLM_API_CONFIGURED");
  assert.equal(configured.isLlmConfirmed, false);
  assert.equal(configured.isThinking, false);

  // Active reasoning call sets isThinking = true
  const started = resolveSupervisorExecutionBadge("LLM_CALL_STARTED", "LLM_API");
  assert.equal(started.code, "LLM_CALL_STARTED");
  assert.equal(started.isThinking, true);

  // Only actual completed LLM call sets isLlmConfirmed = true
  const succeeded = resolveSupervisorExecutionBadge("LLM_CALL_SUCCEEDED", "LLM_API");
  assert.equal(succeeded.code, "LLM_CALL_SUCCEEDED");
  assert.equal(succeeded.isLlmConfirmed, true);
  assert.equal(succeeded.isThinking, false);

  // Failed LLM call clearly reports fallback
  const failed = resolveSupervisorExecutionBadge("LLM_CALL_FAILED", "LLM_API");
  assert.equal(failed.code, "LLM_CALL_FAILED");
  assert.equal(failed.isLlmConfirmed, false);

  // Default deterministic fallback
  const fallback = resolveSupervisorExecutionBadge("DEMO_RULES_FALLBACK", "DEMO_RULES");
  assert.equal(fallback.code, "DEMO_RULES_FALLBACK");
  assert.equal(fallback.isLlmConfirmed, false);
});

test("Prompt 09 Part D: mergeSupervisorActivities deduplicates by seq and filters stale world_epoch", () => {
  const existing = [
    { id: "ACT-1", seq: 1, world_epoch: 1 },
    { id: "ACT-2", seq: 2, world_epoch: 1 },
  ];
  const incoming = [
    { id: "ACT-2", seq: 2, world_epoch: 1 },
    { id: "ACT-3", seq: 3, world_epoch: 1 },
  ];
  const merged = mergeSupervisorActivities(existing, incoming, 1);
  assert.deepEqual(
    merged.map((x) => x.seq),
    [3, 2, 1],
  );

  // After world reset (world_epoch = 2), epoch 1 events are dropped
  const epoch2 = mergeSupervisorActivities(
    merged,
    [{ id: "ACT-10", seq: 1, world_epoch: 2 }],
    2,
  );
  assert.equal(epoch2.length, 1);
  assert.equal(epoch2[0]?.id, "ACT-10");
});

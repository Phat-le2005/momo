import assert from "node:assert/strict";
import test from "node:test";
import {
  clearSessionRunId,
  loadSessionRunId,
  resolveCaseAiModeLabel,
  resolveSelectedCaseId,
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

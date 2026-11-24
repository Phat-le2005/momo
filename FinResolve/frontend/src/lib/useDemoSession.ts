"use client";

import { useCallback, useEffect, useState } from "react";
import { forgetSession, getOrCreateSession } from "./demo-session";
import type { DemoSession } from "./api";
import { readableError } from "./api";

export function useDemoSession() {
  const [session, setSession] = useState<DemoSession | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(async (forceNew = false): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      if (forceNew) forgetSession();
      setSession(await getOrCreateSession());
    } catch (err) {
      setSession(null);
      setError(readableError(err));
    } finally { setLoading(false); }
  }, []);
  useEffect(() => {
    let active = true;
    void getOrCreateSession()
      .then((next) => {
        if (active) {
          setSession(next);
          setError(null);
        }
      })
      .catch((err: unknown) => {
        if (active) {
          setSession(null);
          setError(readableError(err));
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, []);
  return { session, loading, error, reload };
}

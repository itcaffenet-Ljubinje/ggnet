import { useCallback, useState } from "react";

/**
 * Run one action at a time and keep its error message. Host operations
 * (zfs, targetcli) can take seconds, so the UI shows which one is busy
 * and blocks double clicks.
 */
export function useAsyncAction() {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(async (key: string, action: () => Promise<unknown>) => {
    setBusy(key);
    setError(null);
    try {
      await action();
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      setBusy(null);
    }
  }, []);

  return { busy, error, setError, run };
}

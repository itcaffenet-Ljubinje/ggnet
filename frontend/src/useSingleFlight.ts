import { useCallback, useRef } from "react";

/**
 * A refresh that never runs twice at once: a call while one runs waits for
 * it and asks for one more run after it (so what an action changed is read),
 * however many calls came meanwhile. On a slow server the 10 s polling and
 * the reload after an action no longer overlap.
 */
export function useSingleFlight(fn: () => Promise<void>): () => Promise<void> {
  const latest = useRef(fn);
  latest.current = fn;
  const running = useRef<Promise<void> | null>(null);
  const again = useRef(false);

  return useCallback(function call(): Promise<void> {
    if (running.current) {
      again.current = true;
      return running.current;
    }
    const run = (async () => {
      try {
        await latest.current();
      } finally {
        running.current = null;
        if (again.current) {
          again.current = false;
          void call();
        }
      }
    })();
    running.current = run;
    return run;
  }, []);
}

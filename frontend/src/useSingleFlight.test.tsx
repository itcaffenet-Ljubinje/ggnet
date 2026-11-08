import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { useSingleFlight } from "./useSingleFlight";

describe("useSingleFlight", () => {
  it("runs one refresh at a time and once more after it when asked meanwhile", async () => {
    let runs = 0;
    let finish: () => void = () => {};
    const { result } = renderHook(() =>
      useSingleFlight(() => {
        runs++;
        return new Promise<void>((resolve) => (finish = resolve));
      }),
    );
    const first = result.current();
    result.current();                 // the polling while the first runs
    result.current();                 // and the reload after an action
    expect(runs).toBe(1);
    finish();
    await first;
    await Promise.resolve();
    expect(runs).toBe(2);             // one more, not two
    finish();
  });
});

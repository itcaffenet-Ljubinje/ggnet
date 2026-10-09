import { describe, expect, it } from "vitest";

import { formatBytes, formatDuration, formatLink } from "./format";

describe("format", () => {
  it("formats bytes in binary units", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1536)).toBe("1.50 KB");
    expect(formatBytes(16 * 1024 ** 3)).toBe("16.0 GB");
  });

  it("formats link speeds", () => {
    expect(formatLink(100)).toBe("100 Mbit/s");
    expect(formatLink(1000)).toBe("1 Gbit/s");
    expect(formatLink(2500)).toBe("2.5 Gbit/s");
  });

  it("formats durations", () => {
    expect(formatDuration(5 * 60_000)).toBe("5 min");
    expect(formatDuration((2 * 60 + 5) * 60_000)).toBe("2 h 5 min");
    expect(formatDuration((3 * 24 + 4) * 3_600_000)).toBe("3 d 4 h");
    expect(formatDuration(-1000)).toBe("0 min");
  });
});

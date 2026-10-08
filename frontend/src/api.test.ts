import { describe, expect, it, vi } from "vitest";

import { api, ApiError, errorMessage } from "./api";

describe("errorMessage", () => {
  it("uses the backend's {detail: {error}} message", () => {
    expect(errorMessage(502, { detail: { error: "Creating zvol failed: out of space" } })).toBe(
      "Creating zvol failed: out of space",
    );
  });

  it("joins 422 validation errors with their field", () => {
    const body = {
      detail: [
        { loc: ["body", "name"], msg: "Value error, name: 1-32 characters [a-z0-9-]" },
        { loc: ["body", "size_gb"], msg: "Input should be greater than 0" },
      ],
    };
    expect(errorMessage(422, body)).toBe(
      "name: name: 1-32 characters [a-z0-9-]; size_gb: Input should be greater than 0",
    );
  });

  it("falls back to the HTTP status", () => {
    expect(errorMessage(500, null)).toBe("Request failed (HTTP 500)");
  });
});

describe("api", () => {
  it("sends JSON and returns the body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: 1, name: "cs2" }), { status: 201 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.createDisk("cs2", 10)).resolves.toMatchObject({ name: "cs2" });
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/game-disks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "cs2", size_gb: 10 }),
    });
  });

  it("throws ApiError with the backend message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: { error: "dataset is busy" } }), { status: 502 }),
      ),
    );
    const err = await api.applyWritebacks(3).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 502, message: "dataset is busy" });
  });

  it("handles 204 and non-JSON errors", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 204 })));
    await expect(api.deleteDisk(1)).resolves.toBeUndefined();

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("<html>Bad Gateway</html>", { status: 502 })),
    );
    await expect(api.listDisks()).rejects.toThrow("Request failed (HTTP 502)");
  });
});

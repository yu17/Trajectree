import { describe, it, expect, vi } from "vitest";
import { api, ApiError } from "./client";

describe("api client", () => {
  it("throws ApiError with detail and conflicts on 409", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "conflict", conflicts: [{ table_name: "event" }] }), { status: 409 })));
    await expect(api.revert("t1")).rejects.toMatchObject({ status: 409, detail: "conflict" });
    try { await api.revert("t1"); } catch (e) { expect((e as ApiError).body.conflicts[0].table_name).toBe("event"); }
  });
  it("sends timeline window as ISO query params", async () => {
    const f = vi.fn(async (_url: string) => new Response(JSON.stringify({ projects: [], events: [], todos: [], edges: [], pending: {}, live: [] })));
    vi.stubGlobal("fetch", f);
    await api.timeline(new Date("2026-01-01T00:00:00Z"), new Date("2026-02-01T00:00:00Z"));
    expect(String(f.mock.calls[0][0])).toContain("from=2026-01-01T00%3A00%3A00.000Z");
  });
});

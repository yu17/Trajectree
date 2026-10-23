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
  it("calls the routine and scheduler endpoints", async () => {
    const f = vi.fn(async (_url: string, _init?: RequestInit) => new Response(JSON.stringify({})));
    vi.stubGlobal("fetch", f);
    const call = (i: number) => [String(f.mock.calls[i][0]), f.mock.calls[i][1]?.method, f.mock.calls[i][1]?.body];
    await api.routines();
    await api.createRoutine({ name: "rev", trigger: "@daily", prompt_ref: "cross_project_review.md" });
    await api.patchRoutine("rev", { enabled: true, model: null });
    await api.runRoutine("meeting-prep", ["clmi"]);
    await api.runRoutine("rev");
    await api.routineRuns("rev", 5);
    await api.scheduler();
    expect(call(0)).toEqual(["/api/routines", "GET", undefined]);
    expect(call(1)).toEqual(["/api/routines", "POST", JSON.stringify({ name: "rev", trigger: "@daily", prompt_ref: "cross_project_review.md" })]);
    expect(call(2)).toEqual(["/api/routines/rev", "PATCH", JSON.stringify({ enabled: true, model: null })]);
    expect(call(3)).toEqual(["/api/routines/meeting-prep/run", "POST", JSON.stringify({ projects: ["clmi"] })]);
    expect(call(4)).toEqual(["/api/routines/rev/run", "POST", "{}"]);
    expect(call(5)).toEqual(["/api/routines/rev/runs?limit=5", "GET", undefined]);
    expect(call(6)).toEqual(["/api/scheduler", "GET", undefined]);
  });
});

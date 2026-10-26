// web/src/views/InboxView.test.tsx
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { InboxView } from "./InboxView";
import { FABLE, pickFable, testCatalog } from "../components/testCatalog";
import type { InspectionDue, Run, Txn } from "../api/types";

const txn = (id: string): Txn => ({ id, ts: "2026-01-05T10:00:00Z", actor_kind: "session", actor_ref: "owner", model: null, backend: null, tool: "mcp", tool_version: null, session_id: null, run_id: null, proposed_by_run: null, intent: `intent ${id}`, review: "pending", review_ts: null, reverts_txn: null, reverted_by: null });

const api = vi.hoisted(() => ({
  txns: vi.fn(), txn: vi.fn(), todos: vi.fn(), review: vi.fn(), revert: vi.fn(), runs: vi.fn(), acceptProposal: vi.fn(),
  inspections: vi.fn(), models: vi.fn(), inspect: vi.fn(), inspectAll: vi.fn(), reviewMany: vi.fn(), run: vi.fn(),
}));
const due = (id: string, over: Partial<InspectionDue> = {}): InspectionDue => ({ project_id: id, slug: id, title: id.toUpperCase(), stretches: 3, first_day: "2026-09-01", last_day: "2026-09-20", sessions: 5, commits: 7, file_days: 9, active_run: null, episode: null, episodes: null, ...over });
vi.mock("../api/client", () => ({ api, ApiError: class extends Error {} }));

const wrap = (ui: React.ReactElement) => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>);

describe("InboxView", () => {
  beforeEach(() => { vi.clearAllMocks(); api.todos.mockResolvedValue([]); api.review.mockResolvedValue(undefined); api.runs.mockResolvedValue([]);
    api.inspections.mockResolvedValue([]); api.models.mockResolvedValue(testCatalog); api.run.mockRejectedValue(new Error("no run")); });
  it("lists runs with open proposals", async () => {
    api.txns.mockResolvedValue([]);
    api.runs.mockResolvedValue([{ id: "r1", kind: "quick_add", status: "ok", result_md: "Found one.", error: null, created_at: "2026-01-05T10:00:00Z",
      proposals: [{ kind: "todo", project: "clmi", title: "Email Bob", reason: "asked", event_kind: "", when: null, due: null, body_md: "", status: "suggested", applied_txn: null, applied_id: null, dismissed: false }] }]);
    wrap(<InboxView />);
    expect(await screen.findByText("Email Bob")).toBeTruthy();
    expect(screen.getAllByRole("heading", { name: "Proposals" }).length).toBeGreaterThan(0);
    expect(api.runs).toHaveBeenCalledWith({ openProposals: true, limit: 50 });
  });
  it("shows No proposals when there are none", async () => {
    api.txns.mockResolvedValue([]);
    wrap(<InboxView />);
    expect(await screen.findByText("No proposals")).toBeTruthy();
  });
  it("shows the empty state", async () => {
    api.txns.mockResolvedValue([]);
    wrap(<InboxView />);
    expect(await screen.findByText("Nothing to review")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Accept all" })).toBeDisabled();
  });
  it("lists pending writes and accepts them all", async () => {
    api.txns.mockResolvedValue([txn("t1"), txn("t2")]);
    wrap(<InboxView />);
    expect(await screen.findByText("intent t1")).toBeTruthy();
    expect(api.txns).toHaveBeenCalledWith(200, undefined, { review: "pending" });
    fireEvent.click(screen.getByRole("button", { name: "Accept all" }));
    await waitFor(() => expect(api.review).toHaveBeenCalledTimes(2));
    expect(api.review).toHaveBeenCalledWith("t1", "accepted");
  });
  it("lists open todos by due date with undated last", async () => {
    api.txns.mockResolvedValue([]);
    api.todos.mockResolvedValue([
      { id: "a", project_id: null, title: "Later", body_md: "", due: "2026-02-01T00:00:00Z", state: "open", priority: 0, props: {}, created_at: null, updated_at: null },
      { id: "b", project_id: null, title: "Undated", body_md: "", due: null, state: "open", priority: 0, props: {}, created_at: null, updated_at: null },
      { id: "c", project_id: null, title: "Soon", body_md: "", due: "2026-01-10T00:00:00Z", state: "doing", priority: 0, props: {}, created_at: null, updated_at: null },
    ]);
    wrap(<InboxView />);
    const items = await screen.findAllByRole("listitem");
    expect(items.map(li => li.textContent?.split(" · ")[0])).toEqual(["Soon", "Later", "Undated"]);
  });
  it("lists projects due for inspection and starts one with the chosen model", async () => {
    api.txns.mockResolvedValue([]);
    api.inspections.mockResolvedValue([due("clmi")]);
    api.inspect.mockResolvedValue({ id: "run1" });
    wrap(<InboxView />);
    expect(await screen.findByRole("heading", { name: "Due for inspection (1)" })).toBeTruthy();
    expect(await screen.findByText("CLMI · 3 stretches · 2026-09-01 → 2026-09-20 · 5 segments, 7 commits, 9 file days")).toBeTruthy();
    await pickFable(await screen.findByRole("combobox", { name: "Model" }));
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() => expect(api.inspect).toHaveBeenCalledWith("clmi", FABLE));
  });
  it("shows the episode and run status of an active inspection", async () => {
    api.txns.mockResolvedValue([]);
    api.inspections.mockResolvedValue([due("clmi", { active_run: "run7", episode: 2, episodes: 4 })]);
    api.run.mockResolvedValue({ run: { id: "run7", status: "running" }, txn_ids: [] });
    wrap(<InboxView />);
    expect(await screen.findByText("episode 2 of 4")).toBeTruthy();
    expect(await screen.findByText("running")).toBeTruthy();
  });
  it("starts all due projects with one model", async () => {
    api.txns.mockResolvedValue([]);
    api.inspections.mockResolvedValue([due("clmi"), due("ccx")]);
    api.inspectAll.mockResolvedValue({ runs: [{ id: "r1" }, { id: "r2" }], skipped: [] });
    wrap(<InboxView />);
    await pickFable(await screen.findByRole("combobox", { name: "Model for all" }));
    fireEvent.click(screen.getByRole("button", { name: "Start all" }));
    await waitFor(() => expect(api.inspectAll).toHaveBeenCalledWith(FABLE));
  });
  it("groups pending writes by run and rejects a group newest first", async () => {
    // The list shows two of the run's writes; the group fetch finds a third beyond the list's cap.
    api.txns.mockImplementation(async (_limit: number, _before: unknown, filter?: { run_id?: string }) =>
      filter?.run_id ? [txn("t9"), txn("t2"), txn("t1")].map(t => ({ ...t, run_id: "run1" }))
        : [{ ...txn("t1"), run_id: "run1" }, { ...txn("t2"), run_id: "run1" }, txn("t3")]);
    api.run.mockResolvedValue({ run: { id: "run1", kind: "inspect", model: "claude-fable-5-1", created_at: "2026-10-20T10:00:00Z" } as Run, txn_ids: [] });
    api.reviewMany.mockResolvedValue({ done: ["t1", "t2"], failed: null, conflicts: [] });
    wrap(<InboxView />);
    expect(await screen.findByRole("heading", { name: new RegExp(`^inspect · claude-fable-5-1 · `) })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Other writes (1)" })).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "Reject all" })[0]);
    await waitFor(() => expect(api.reviewMany).toHaveBeenCalledWith(["t9", "t2", "t1"], "rejected"));
    expect(api.txns).toHaveBeenCalledWith(500, undefined, { review: "pending", run_id: "run1" });
  });
  it("groups the writes of one inspection series and rejects the whole series newest first", async () => {
    const run = (id: string, episode: number) => ({ id, kind: "inspect", model: "claude-fable-5-1", runner: "claude",
      created_at: `2026-10-2${episode}T10:00:00Z`, props: { series_id: "S", episode_no: episode, episodes: 2 } }) as unknown as Run;
    const byRun: Record<string, Txn[]> = {
      run1: [txn("t1"), txn("t0")].map(t => ({ ...t, run_id: "run1" })),
      run2: [txn("t5"), txn("t2")].map(t => ({ ...t, run_id: "run2" })),
    };
    api.txns.mockImplementation(async (_limit: number, _before: unknown, filter?: { run_id?: string }) =>
      filter?.run_id ? byRun[filter.run_id] ?? []
        : [{ ...txn("t2"), run_id: "run2" }, { ...txn("t1"), run_id: "run1" }, txn("t3")]);
    api.run.mockImplementation(async (id: string) => ({ run: run(id, id === "run1" ? 1 : 2), txn_ids: [] }));
    api.runs.mockImplementation(async (opts: { seriesId?: string }) => opts.seriesId === "S" ? [run("run2", 2), run("run1", 1)] : []);
    api.reviewMany.mockResolvedValue({ done: [], failed: null, conflicts: [] });
    wrap(<InboxView />);
    const heading = await screen.findByRole("heading", { name: /^inspection · claude-fable-5-1 · episode 1–2 of 2 · / });
    expect(screen.getAllByRole("heading", { level: 3 })).toHaveLength(2); // the series, and the other writes
    const group = heading.closest(".pending-group") as HTMLElement;
    expect([...group.querySelectorAll("button.intent")].map(b => b.textContent)).toEqual(["intent t1", "intent t2"]); // episodes in order
    fireEvent.click(screen.getAllByRole("button", { name: "Reject all" })[0]);
    await waitFor(() => expect(api.reviewMany).toHaveBeenCalledWith(["t5", "t2", "t1", "t0"], "rejected"));
    expect(api.runs).toHaveBeenCalledWith({ seriesId: "S", limit: 500 });
  });
});

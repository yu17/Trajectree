// web/src/views/InboxView.test.tsx
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { InboxView } from "./InboxView";
import type { Txn } from "../api/types";

const txn = (id: string): Txn => ({ id, ts: "2026-01-05T10:00:00Z", actor_kind: "session", actor_ref: "owner", model: null, backend: null, tool: "mcp", tool_version: null, session_id: null, run_id: null, proposed_by_run: null, intent: `intent ${id}`, review: "pending", review_ts: null, reverts_txn: null, reverted_by: null });

const api = vi.hoisted(() => ({
  txns: vi.fn(), txn: vi.fn(), todos: vi.fn(), review: vi.fn(), revert: vi.fn(), runs: vi.fn(), acceptProposal: vi.fn(),
}));
vi.mock("../api/client", () => ({ api, ApiError: class extends Error {} }));

const wrap = (ui: React.ReactElement) => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>);

describe("InboxView", () => {
  beforeEach(() => { vi.clearAllMocks(); api.todos.mockResolvedValue([]); api.review.mockResolvedValue(undefined); api.runs.mockResolvedValue([]); });
  it("lists runs with open proposals", async () => {
    api.txns.mockResolvedValue([]);
    api.runs.mockResolvedValue([{ id: "r1", kind: "quick_add", status: "ok", result_md: "Found one.", error: null, created_at: "2026-01-05T10:00:00Z",
      proposals: [{ kind: "todo", project: "clmi", title: "Email Bob", reason: "asked", event_kind: "", when: null, due: null, body_md: "", status: "suggested", applied_txn: null, applied_id: null }] }]);
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
});

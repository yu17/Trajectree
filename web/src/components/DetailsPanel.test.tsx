import { render, screen, fireEvent } from "@testing-library/react";
import { afterEach, describe, it, expect, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { DetailsPanel } from "./DetailsPanel";
import { api } from "../api/client";
import type { Run, Signal, TimelineResponse } from "../api/types";

vi.mock("../api/client", () => ({ api: {
  txn: vi.fn(async (id: string) => ({
    txn: { id, ts: "2026-01-08T10:00:00Z", actor_kind: "agent", actor_ref: "run_7", model: "claude-opus-5-5", backend: "claude", tool: "claude", tool_version: null, session_id: null, run_id: "run_7", proposed_by_run: null, intent: "x", review: "pending", review_ts: null, reverts_txn: null, reverted_by: null },
    changes: [],
  })),
  signals: vi.fn(async () => [{
    id: "sig1", project_id: "p1", resource_id: null, kind: "session", t_start: "2026-01-05T09:00:00Z", t_end: "2026-01-05T11:00:00Z",
    payload: { session_id: "abcd1234-ef56-7890-abcd-ef1234567890", cwd: "/home/u/proj", title: "Fix build", git_branch: "main", model: "claude-opus-5-5", prompts: 3, tokens_in: 100, tokens_out: 200, transcript_path: "/t.jsonl", backend: "claude", running: false },
    dedupe_key: "session:abcd1234", updated_at: "2026-01-05T11:00:00Z",
  }]),
  summarizeSession: vi.fn(),
  run: vi.fn(),
  acceptProposal: vi.fn(),
  dismissProposal: vi.fn(),
}, ApiError: class extends Error {} }));

const data: TimelineResponse = {
  projects: [{ id: "p1", slug: "clmi", title: "CLMI", kind: "research", status: "active", lane_order: 0, summary_md: "", recurrence: null, props: {}, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" }],
  events: [
    { id: "e1", project_id: "p1", kind: "decision", title: "Freeze", body_md: "Two-stage design.", t_start: "2026-01-05T10:00:00Z", t_end: null, status: "happened", source: "human", props: {}, created_txn: "t1", created_at: "2026-01-05T10:00:00Z", updated_at: "2026-01-05T10:00:00Z" },
    { id: "e2", project_id: "p1", kind: "milestone", title: "Draft v0", body_md: "", t_start: "2026-01-08T10:00:00Z", t_end: null, status: "suggested", source: "agent", props: {}, created_txn: "t2", created_at: "2026-01-08T10:00:00Z", updated_at: "2026-01-08T10:00:00Z" },
  ],
  todos: [],
  edges: [{ id: "x", src_type: "event", src_id: "e1", dst_type: "event", dst_id: "e2", rel: "led_to", props: {}, valid_from: null, valid_to: null, created_at: "2026-01-08T10:00:00Z" }],
  pending: { event: { e2: "t2" }, todo: {}, edge: {} }, strip: [], live: [], stale: [],
};

// Tests stub navigator.clipboard; put the original back so a stub never leaks into the next test.
const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");
afterEach(() => {
  if (clipboardDescriptor) Object.defineProperty(navigator, "clipboard", clipboardDescriptor);
  else delete (navigator as { clipboard?: unknown }).clipboard;
});

const wrap = (ui: ReactNode) => render(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>);

describe("DetailsPanel", () => {
  it("shows a pending event with author from its transaction and review buttons", async () => {
    const onReview = vi.fn();
    const onRevert = vi.fn();
    wrap(<DetailsPanel data={data} selected={{ type: "event", id: "e2" }} onReview={onReview} onRevert={onRevert} />);
    expect(screen.getByText("pending")).toBeTruthy();
    expect(screen.getByText("suggested")).toBeTruthy();
    expect(screen.getByText("t2")).toBeTruthy();
    await screen.findByText("agent run_7 · claude-opus-5-5 · via claude · tool claude");
    expect(screen.getByText("led_to")).toBeTruthy();
    fireEvent.click(screen.getByText("Accept"));
    expect(onReview).toHaveBeenCalledWith("t2", "accepted");
    fireEvent.click(screen.getByText("Reject"));
    expect(onReview).toHaveBeenCalledWith("t2", "rejected");
    expect(screen.queryByText("Revert transaction")).toBeNull();
  });
  it("offers revert for a reviewed event and an empty state with no selection", async () => {
    const onRevert = vi.fn();
    const { rerender } = wrap(<DetailsPanel data={data} selected={{ type: "event", id: "e1" }} onReview={() => {}} onRevert={onRevert} />);
    expect(screen.getByText("Two-stage design.")).toBeTruthy();
    await screen.findByText(/agent run_7/);
    expect(screen.queryByText("Accept")).toBeNull();
    fireEvent.click(screen.getByText("Revert transaction"));
    expect(onRevert).toHaveBeenCalledWith("t1");
    rerender(<QueryClientProvider client={new QueryClient()}><DetailsPanel data={data} selected={null} onReview={() => {}} onRevert={onRevert} /></QueryClientProvider>);
    expect(screen.getByText("Click a block to see its details.")).toBeTruthy();
  });
  it("reviews the pending transaction, not the creating one", async () => {
    // An agent updated e1 after it was created: the pending txn is the update, not the create.
    const updated: TimelineResponse = {
      ...data,
      events: data.events.map(e => (e.id === "e1" ? { ...e, created_txn: "txn-create" } : e)),
      pending: { event: { e1: "txn-pending" }, todo: {}, edge: {} },
    };
    const onReview = vi.fn();
    wrap(<DetailsPanel data={updated} selected={{ type: "event", id: "e1" }} onReview={onReview} onRevert={() => {}} />);
    expect(screen.getByText("pending")).toBeTruthy();
    expect(screen.getByText("txn-pending")).toBeTruthy();
    await screen.findByText(/agent run_7/);
    expect(api.txn).toHaveBeenCalledWith("txn-pending");
    expect(api.txn).not.toHaveBeenCalledWith("txn-create");
    fireEvent.click(screen.getByText("Accept"));
    expect(onReview).toHaveBeenCalledWith("txn-pending", "accepted");
    fireEvent.click(screen.getByText("Reject"));
    expect(onReview).toHaveBeenCalledWith("txn-pending", "rejected");
  });
  it("shows the pending badge and review buttons for a pending todo", () => {
    const d: TimelineResponse = {
      ...data,
      todos: [{ id: "td5", project_id: "p1", title: "Reply", body_md: "", due: null, state: "open", priority: 0, props: {}, created_at: null, updated_at: null }],
      pending: { event: {}, todo: { td5: "t5" }, edge: {} },
    };
    const onReview = vi.fn();
    wrap(<DetailsPanel data={d} selected={{ type: "todo", id: "td5" }} onReview={onReview} onRevert={() => {}} />);
    expect(screen.getByText("pending")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    expect(onReview).toHaveBeenCalledWith("t5", "accepted");
  });
  it("lists a bin's sessions and copies the resume command", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const start = Date.UTC(2026, 0, 5);
    const end = start + 86_400_000;
    wrap(<DetailsPanel data={data} selected={{ type: "bin", projectId: "p1", start, end }} onReview={() => {}} onRevert={() => {}} />);
    expect(screen.getByText("Activity")).toBeTruthy();
    await screen.findByText("Fix build");
    expect(api.signals).toHaveBeenCalledWith(["p1"], new Date(start), new Date(end - 1));
    expect(screen.getByText(/3 prompts/)).toBeTruthy();
    fireEvent.click(screen.getByText("Copy resume command"));
    expect(writeText).toHaveBeenCalledWith("cd /home/u/proj && claude --resume abcd1234-ef56-7890-abcd-ef1234567890");
    await screen.findByText("Copied");
  });
  it("shows the resume command and reports a failed copy", async () => {
    Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
    const start = Date.UTC(2026, 0, 5);
    const { container } = wrap(<DetailsPanel data={data} selected={{ type: "bin", projectId: "p1", start, end: start + 86_400_000 }} onReview={() => {}} onRevert={() => {}} />);
    await screen.findByText("Fix build");
    expect(screen.getByText("05 Jan 2026")).toBeTruthy();
    expect(container.querySelector("code.resume")!.textContent).toContain("claude --resume");
    fireEvent.click(screen.getByText("Copy resume command"));
    await screen.findByText(/Copy failed/);
  });
  it("renders files_changed, job_state and remote signals and summarizes a session", async () => {
    const user = userEvent.setup();
    const sig = (id: string, kind: Signal["kind"], payload: Record<string, unknown>): Signal => ({ id, project_id: "p1", resource_id: null, kind, t_start: "2026-01-05T09:00:00Z", t_end: null, payload, dedupe_key: id, updated_at: "2026-01-05T11:00:00Z" });
    vi.mocked(api.signals).mockResolvedValueOnce([
      sig("s1", "session", { session_id: "sess-1", cwd: "/home/u/proj", title: "Fix build", model: "claude-opus-5-5", prompts: 2 }),
      sig("f1", "files_changed", { count: 7, bytes: 1024, paths: ["a.py", "b.py", "c.py", "d.py", "e.py", "f.py", "g.py"], big_dirs: [] }),
      sig("j1", "job_state", { job_id: "4242", name: "train", state: "RUNNING", partition: "gpu", workdir: "/w" }),
      sig("r1", "remote", { count: 3, prs: [{ number: 12, title: "Add loader", state: "open", url: "u" }], issues: [{ number: 13, title: "Crash on start", state: "open", url: "u" }], releases: [{ tag: "v1.0", title: "First release", url: "u" }] }),
      sig("m1", "message", { ref: "x", summary: "Bob asked about the deadline", channel: "slack", participants: ["bob"] }),
    ]);
    const queued = { id: "run5", kind: "summarize_session", project_id: "p1", status: "queued", runner: "claude", backend: "claude", model: null, session_id: "sess-1", started: null, finished: null, tokens_in: 0, tokens_out: 0, tokens_cache: 0, est_usd: 0, turns: 0, duration_ms: 0, result_md: "", proposals: [], error: null, log_path: null, token_id: null, props: {}, routine_id: null, slot: "interactive", attempt: 1, created_at: "2026-01-05T12:00:00Z" } as Run;
    vi.mocked(api.summarizeSession).mockResolvedValue(queued);
    vi.mocked(api.run).mockResolvedValue({ run: { ...queued, status: "ok", result_md: "Fixed the build.", proposals: [{ kind: "todo", project: "clmi", title: "Add CI", reason: "flaky", event_kind: "", when: null, due: null, body_md: "", status: "suggested", applied_txn: null, applied_id: null, dismissed: false }] }, txn_ids: [] });
    const start = Date.UTC(2026, 0, 5);
    const { container } = wrap(<DetailsPanel data={data} selected={{ type: "bin", projectId: "p1", start, end: start + 86_400_000 }} onReview={() => {}} onRevert={() => {}} />);
    expect(await screen.findByText("7 files changed")).toBeTruthy();
    expect(screen.getByText("e.py")).toBeTruthy();
    expect(screen.queryByText("f.py")).toBeNull();
    expect(screen.getByText("job train RUNNING")).toBeTruthy();
    expect(screen.getByText("3 updates")).toBeTruthy();
    expect(screen.getByText("Add loader")).toBeTruthy();
    expect(screen.getByText("Crash on start")).toBeTruthy();
    expect(screen.getByText("First release")).toBeTruthy();
    expect(screen.getByText("slack: Bob asked about the deadline")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Summarize session" }));
    expect(api.summarizeSession).toHaveBeenCalledWith("sess-1", {});
    expect(await screen.findByText("Fixed the build.")).toBeTruthy();
    expect(container.querySelector(".answer")!.textContent).toBe("Fixed the build.");
    expect(container.querySelector("span.badge.run-ok")).not.toBeNull();
    expect(screen.getByText("Add CI")).toBeTruthy();
    expect(api.run).toHaveBeenCalledWith("run5");
  });
  it("shows the error when a session summary fails", async () => {
    const user = userEvent.setup();
    vi.mocked(api.summarizeSession).mockRejectedValue(new Error("runs are paused"));
    const start = Date.UTC(2026, 0, 5);
    wrap(<DetailsPanel data={data} selected={{ type: "bin", projectId: "p1", start, end: start + 86_400_000 }} onReview={() => {}} onRevert={() => {}} />);
    await user.click(await screen.findByRole("button", { name: "Summarize session" }));
    expect(await screen.findByText("runs are paused")).toBeTruthy();
  });
  it("opens the project of a selected event", async () => {
    const onOpenProject = vi.fn();
    wrap(<DetailsPanel data={data} selected={{ type: "event", id: "e1" }} onReview={() => {}} onRevert={() => {}} onOpenProject={onOpenProject} />);
    await screen.findByText(/agent run_7/);
    fireEvent.click(screen.getByRole("button", { name: "Open project" }));
    expect(onOpenProject).toHaveBeenCalledWith("p1");
  });
});

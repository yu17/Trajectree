import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { DetailsPanel } from "./DetailsPanel";
import { api } from "../api/client";
import type { TimelineResponse } from "../api/types";

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
} }));

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
  it("lists a bin's sessions and copies the resume command", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const start = Date.UTC(2026, 0, 5);
    const end = start + 86_400_000;
    wrap(<DetailsPanel data={data} selected={{ type: "bin", projectId: "p1", start, end }} onReview={() => {}} onRevert={() => {}} />);
    expect(screen.getByText("Activity")).toBeTruthy();
    await screen.findByText("Fix build");
    expect(api.signals).toHaveBeenCalledWith(["p1"], new Date(start), new Date(end));
    expect(screen.getByText(/3 prompts/)).toBeTruthy();
    fireEvent.click(screen.getByText("Copy resume command"));
    expect(writeText).toHaveBeenCalledWith("cd /home/u/proj && claude --resume abcd1234-ef56-7890-abcd-ef1234567890");
    await screen.findByText("Copied");
  });
});

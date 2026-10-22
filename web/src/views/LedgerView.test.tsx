import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { LedgerView } from "./LedgerView";

const txn = { id: "t1", ts: "2026-01-05T10:00:00Z", actor_kind: "agent", actor_ref: "run_1", model: "claude-opus-5-5", backend: "claude", tool: "claude", tool_version: null, session_id: null, run_id: "run_1", proposed_by_run: null, intent: "record event: draft", review: "pending", review_ts: null, reverts_txn: null, reverted_by: null };
vi.mock("../api/client", () => ({ api: {
  txns: vi.fn(async () => [txn]),
  txn: vi.fn(async () => ({ txn, changes: [{ txn_id: "t1", seq: 0, table_name: "event", op: "INSERT", pk: ["e1"], old: null, new: { title: "draft" } }] })),
  review: vi.fn(async () => ({ ...txn, review: "accepted" })),
  revert: vi.fn(),
} }));
import { api } from "../api/client";

describe("LedgerView", () => {
  it("lists transactions, expands the diff, and accepts a pending one", async () => {
    render(<QueryClientProvider client={new QueryClient()}><LedgerView /></QueryClientProvider>);
    await screen.findByText("record event: draft");
    expect(screen.getByText("claude-opus-5-5")).toBeTruthy();
    fireEvent.click(screen.getByText("record event: draft"));
    await screen.findByText("draft");
    fireEvent.click(screen.getByText("Accept"));
    await waitFor(() => expect(api.review).toHaveBeenCalledWith("t1", "accepted"));
  });
});

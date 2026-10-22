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

  it("lists only an UPDATE's changed columns, not its primary key", async () => {
    (api.txn as any).mockResolvedValueOnce({ txn, changes: [{ txn_id: "t1", seq: 0, table_name: "event", op: "UPDATE", pk: ["e1"], old: { id: "e1", title: "a" }, new: { title: "b" } }] });
    const { container } = render(<QueryClientProvider client={new QueryClient()}><LedgerView /></QueryClientProvider>);
    fireEvent.click(await screen.findByText("record event: draft"));
    await screen.findByText("b");
    const keys = [...container.querySelectorAll(".diff th[scope=row]")].map(th => th.textContent);
    expect(keys).toEqual(["title"]);
  });

  it("filters by review state and shows the revert chain", async () => {
    const revert = { ...txn, id: "t3", actor_kind: "human", actor_ref: "owner", model: null, intent: "revert t2", review: "none", reverts_txn: "abcdef123456" };
    const reverted = { ...txn, id: "t2", intent: "record event: old", review: "rejected", reverted_by: "999999fedcba" };
    (api.txns as any).mockImplementation(async () => [revert, reverted]);
    try {
      render(<QueryClientProvider client={new QueryClient()}><LedgerView /></QueryClientProvider>);
      expect(await screen.findByText("reverts 123456")).toBeTruthy();
      expect(screen.getByText("reverted by fedcba")).toBeTruthy();
      expect(api.txns).toHaveBeenLastCalledWith(50, undefined, {});
      const show = screen.getByRole("combobox", { name: "Show" });
      expect([...show.querySelectorAll("option")].map(o => o.textContent)).toEqual(["All", "Pending", "Humans", "Agents & sessions"]);
      fireEvent.change(show, { target: { value: "pending" } });
      await waitFor(() => expect(api.txns).toHaveBeenLastCalledWith(50, undefined, { review: "pending" }));
      fireEvent.change(show, { target: { value: "agents" } });
      await waitFor(() => expect(api.txns).toHaveBeenLastCalledWith(50, undefined, { actor_kind: "agent,session" }));
      fireEvent.change(show, { target: { value: "humans" } });
      await waitFor(() => expect(api.txns).toHaveBeenLastCalledWith(50, undefined, { actor_kind: "human" }));
      await screen.findByText("reverts 123456");
    } finally {
      (api.txns as any).mockImplementation(async () => [txn]);
    }
  });
});

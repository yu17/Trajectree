import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi } from "vitest";
import { Proposals } from "./Proposals";
import type { Run } from "../api/types";

const api = vi.hoisted(() => ({ acceptProposal: vi.fn(), dismissProposal: vi.fn() }));
vi.mock("../api/client", () => ({ api, ApiError: class extends Error {} }));
const base: Run = { id: "r1", kind: "quick_add", project_id: null, status: "ok", runner: "claude", backend: "claude", model: null, session_id: null, started: null, finished: null, tokens_in: 0, tokens_out: 0, tokens_cache: 0, est_usd: 0, turns: 0, duration_ms: 0, result_md: "", proposals: [
  { kind: "event", project: "clmi", title: "Decided X", reason: "decision", event_kind: "decision", when: "2026-01-05T10:00:00Z", due: null, body_md: "", status: "happened", applied_txn: null, applied_id: null, dismissed: false },
  { kind: "todo", project: "clmi", title: "Done one", reason: "r", event_kind: "note", when: null, due: null, body_md: "", status: "happened", applied_txn: "t9", applied_id: "x", dismissed: false },
], error: null, log_path: null, token_id: null, props: {}, created_at: "2026-01-05T10:00:00Z" };

describe("Proposals", () => {
  it("accepts a proposal and marks applied ones", async () => {
    api.acceptProposal.mockResolvedValue({ ...base, proposals: [{ ...base.proposals[0], applied_txn: "t1", applied_id: "e1" }, base.proposals[1]] });
    const onApplied = vi.fn();
    render(<QueryClientProvider client={new QueryClient()}><Proposals run={base} onApplied={onApplied} /></QueryClientProvider>);
    expect(screen.getAllByText("applied").length).toBe(1);
    expect(screen.getAllByRole("button", { name: "Accept" }).length).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    await waitFor(() => expect(api.acceptProposal).toHaveBeenCalledWith("r1", 0));
    await waitFor(() => expect(onApplied).toHaveBeenCalled());
  });
  it("dismisses a proposal and shows dismissed ones without buttons", async () => {
    api.dismissProposal.mockResolvedValue({ ...base, proposals: [{ ...base.proposals[0], dismissed: true }, base.proposals[1]] });
    const onApplied = vi.fn();
    const qc = new QueryClient();
    const invalidate = vi.spyOn(qc, "invalidateQueries");
    const { rerender } = render(<QueryClientProvider client={qc}><Proposals run={base} onApplied={onApplied} /></QueryClientProvider>);
    expect(screen.getAllByRole("button", { name: "Dismiss" }).length).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(api.dismissProposal).toHaveBeenCalledWith("r1", 0));
    await waitFor(() => expect(onApplied).toHaveBeenCalled());
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["runs"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["run", "r1"] });
    expect(api.acceptProposal).not.toHaveBeenCalled();
    const dismissed: Run = { ...base, proposals: [{ ...base.proposals[0], dismissed: true }, base.proposals[1]] };
    rerender(<QueryClientProvider client={qc}><Proposals run={dismissed} /></QueryClientProvider>);
    expect(screen.getByText("dismissed")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Dismiss" })).toBeNull();
  });
  it("shows the empty state", () => {
    render(<QueryClientProvider client={new QueryClient()}><Proposals run={{ ...base, proposals: [] }} /></QueryClientProvider>);
    expect(screen.getByText("No proposals")).toBeTruthy();
  });
});

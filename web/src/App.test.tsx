import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";

vi.mock("./api/client", async importOriginal => {
  const real = await importOriginal<typeof import("./api/client")>();
  const unauthorized = () => Promise.reject(new real.ApiError(401, "Unauthorized", { detail: "Unauthorized" }));
  return { ...real, api: { ...real.api, projects: vi.fn(unauthorized), timeline: vi.fn(unauthorized), txns: vi.fn(unauthorized) } };
});
import { api } from "./api/client";
import type { Txn } from "./api/types";

describe("App", () => {
  it("shows a login banner when the API answers 401", async () => {
    render(<QueryClientProvider client={new QueryClient()}><App /></QueryClientProvider>);
    const banner = await screen.findByRole("alert");
    expect(banner.textContent).toContain("Not logged in. Open the login URL printed by the daemon");
    expect(banner.textContent).toContain("docker compose logs trajectree | grep ready");
  });

  it("has an Inbox tab", () => {
    render(<QueryClientProvider client={new QueryClient()}><App /></QueryClientProvider>);
    expect(screen.getByRole("tab", { name: /Inbox/ })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Timeline" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Ledger" })).toBeTruthy();
  });

  it("shows the pending count on the Inbox tab", async () => {
    const pending = (id: string): Txn => ({ id, ts: "2026-01-05T10:00:00Z", actor_kind: "session", actor_ref: "owner", model: null, backend: null, tool: "mcp", tool_version: null, session_id: null, run_id: null, proposed_by_run: null, intent: id, review: "pending", review_ts: null, reverts_txn: null, reverted_by: null });
    vi.mocked(api.txns).mockResolvedValueOnce([pending("t1"), pending("t2")]);
    render(<QueryClientProvider client={new QueryClient()}><App /></QueryClientProvider>);
    expect(await screen.findByRole("tab", { name: "Inbox (2)" })).toBeTruthy();
    expect(api.txns).toHaveBeenCalledWith(200, undefined, { review: "pending" });
  });
});

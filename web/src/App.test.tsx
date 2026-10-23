import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";

vi.mock("./api/client", async importOriginal => {
  const real = await importOriginal<typeof import("./api/client")>();
  const unauthorized = () => Promise.reject(new real.ApiError(401, "Unauthorized", { detail: "Unauthorized" }));
  return { ...real, api: { ...real.api, projects: vi.fn(unauthorized), timeline: vi.fn(unauthorized), txns: vi.fn(unauthorized), runs: vi.fn(async () => []), reports: vi.fn(async () => []), routines: vi.fn(async () => []), scheduler: vi.fn(async () => ({ last_tick: null, interval_s: 60, next_fires: [] })), resources: vi.fn(async () => []), signals: vi.fn(async () => []), usage: vi.fn(async () => ({ days: [], by_kind: [], by_runner: [], by_routine: [], spent_today: 0, caps: { usd_per_run: 1, usd_per_day: 5, timeout_s: 600, paused: false } })), caps: vi.fn(async () => ({ usd_per_run: 1, usd_per_day: 5, timeout_s: 600, paused: false })), runnerStatus: vi.fn(async () => ({ claude: { available: false, version: null, authenticated: false }, codex: { available: false, version: null, authenticated: false }, backends: [] })) } };
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
    expect(screen.getAllByRole("tab").map(t => t.textContent)).toEqual(["Timeline", "Ledger", "Runs", "Reports", "Inbox"]);
  });

  it("renders the Runs tab", async () => {
    render(<QueryClientProvider client={new QueryClient()}><App /></QueryClientProvider>);
    fireEvent.click(screen.getByRole("tab", { name: "Runs" }));
    expect(await screen.findByText("Run history")).toBeTruthy();
  });

  it("renders the Reports tab", async () => {
    render(<QueryClientProvider client={new QueryClient()}><App /></QueryClientProvider>);
    fireEvent.click(screen.getByRole("tab", { name: "Reports" }));
    expect(await screen.findByRole("heading", { name: "Reports" }, { timeout: 4000 })).toBeTruthy();
    expect(await screen.findByText("No reports yet")).toBeTruthy();
  });

  it("shows the pending count on the Inbox tab", async () => {
    const pending = (id: string): Txn => ({ id, ts: "2026-01-05T10:00:00Z", actor_kind: "session", actor_ref: "owner", model: null, backend: null, tool: "mcp", tool_version: null, session_id: null, run_id: null, proposed_by_run: null, intent: id, review: "pending", review_ts: null, reverts_txn: null, reverted_by: null });
    vi.mocked(api.txns).mockResolvedValueOnce([pending("t1"), pending("t2")]);
    render(<QueryClientProvider client={new QueryClient()}><App /></QueryClientProvider>);
    expect(await screen.findByRole("tab", { name: "Inbox (2)" })).toBeTruthy();
    expect(api.txns).toHaveBeenCalledWith(200, undefined, { review: "pending" });
  });

  it("opens a Project tab from a lane label and closes it", async () => {
    const p = { id: "p1", slug: "clmi", title: "CLMI", kind: "research" as const, status: "active" as const, lane_order: 0, summary_md: "", recurrence: null, props: {}, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" };
    vi.mocked(api.projects).mockImplementation(async () => [p]);
    vi.mocked(api.timeline).mockImplementation(async () => ({ projects: [p], events: [], todos: [], edges: [], pending: { event: {}, todo: {}, edge: {} }, strip: [], live: [], stale: [] }));
    const { container } = render(<QueryClientProvider client={new QueryClient()}><App /></QueryClientProvider>);
    await waitFor(() => expect(container.querySelector("g.lab text.lbl-name")).not.toBeNull());
    fireEvent.click(container.querySelector("g.lab text.lbl-name")!);
    expect(screen.getByRole("tab", { name: "Project" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getAllByRole("tab").map(t => t.textContent)).toEqual(["Timeline", "Ledger", "Runs", "Reports", "Project", "Inbox"]);
    expect(await screen.findByRole("heading", { name: "CLMI" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("tab", { name: "Project" })).toBeNull();
    expect(screen.getByRole("tab", { name: "Timeline" }).getAttribute("aria-selected")).toBe("true");
  });
});

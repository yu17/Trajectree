import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { RunsView } from "./RunsView";
import type { Run } from "../api/types";

const run = (id: string, over: Partial<Run> = {}): Run => ({ id, kind: "ask", project_id: null, status: "ok", runner: "claude", backend: "claude", model: null, session_id: "s", started: "2026-01-05T10:00:00Z", finished: "2026-01-05T10:01:00Z", tokens_in: 100, tokens_out: 20, tokens_cache: 12, est_usd: 0.0123, turns: 2, duration_ms: 345, result_md: "Answer.", proposals: [], error: null, log_path: "/data/runs/r1.log", token_id: null, props: {}, created_at: "2026-01-05T10:00:00Z", ...over });
const api = vi.hoisted(() => ({ runs: vi.fn(), run: vi.fn(), usage: vi.fn(), caps: vi.fn(), setCaps: vi.fn(), runnerStatus: vi.fn(), projects: vi.fn(), acceptProposal: vi.fn() }));
vi.mock("../api/client", () => ({ api, ApiError: class extends Error {} }));
const wrap = (ui: React.ReactElement) => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>);
const caps = { usd_per_run: 1, usd_per_day: 5, timeout_s: 600, paused: false };

describe("RunsView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.projects.mockResolvedValue([]);
    api.runnerStatus.mockResolvedValue({ claude: { available: true, version: "2.1.309", authenticated: false }, codex: { available: false, version: null, authenticated: false }, backends: [] });
    api.usage.mockResolvedValue({ days: [{ day: "2026-01-04", est_usd: 1, runs: 1, tokens_in: 1, tokens_out: 1 }, { day: "2026-01-05", est_usd: 0.5, runs: 2, tokens_in: 1, tokens_out: 1 }], by_kind: [{ key: "ask", est_usd: 1.5, runs: 3 }], by_runner: [], spent_today: 0.5, caps });
    api.caps.mockResolvedValue(caps);
    api.setCaps.mockImplementation(async (c) => c);
  });
  it("renders usage bars, the cap line and the runner status", async () => {
    api.runs.mockResolvedValue([]);
    const { container } = wrap(<RunsView />);
    expect(await screen.findByText(/not logged in/)).toBeTruthy();
    await waitFor(() => expect(container.querySelectorAll("svg.usage-chart rect.bar").length).toBe(2));
    expect(container.querySelector("svg.usage-chart line.cap")).not.toBeNull();
    expect(screen.getByText(/spent today \$0\.50 of \$5/)).toBeTruthy();
    expect(await screen.findByText("No runs yet")).toBeTruthy();
  });
  it("saves caps and toggles pause", async () => {
    api.runs.mockResolvedValue([]);
    wrap(<RunsView />);
    const perDay = await screen.findByLabelText("USD per day");
    fireEvent.change(perDay, { target: { value: "7" } });
    fireEvent.click(screen.getByRole("button", { name: "Save caps" }));
    await waitFor(() => expect(api.setCaps).toHaveBeenCalledWith({ ...caps, usd_per_day: 7 }));
    fireEvent.click(screen.getByRole("button", { name: "Pause runs" }));
    await waitFor(() => expect(api.setCaps).toHaveBeenLastCalledWith(expect.objectContaining({ paused: true })));
  });
  it("refuses to save caps that are not positive numbers", async () => {
    api.runs.mockResolvedValue([]);
    wrap(<RunsView />);
    const perDay = await screen.findByLabelText("USD per day");
    fireEvent.change(perDay, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save caps" }));
    expect(await screen.findByText("enter positive numbers")).toBeTruthy();
    expect(api.setCaps).not.toHaveBeenCalled();
  });
  it("lists runs and expands one with its proposals", async () => {
    const r1 = run("r1", { proposals: [{ kind: "todo", project: "clmi", title: "Email Bob", reason: "owed", event_kind: "note", when: null, due: null, body_md: "", status: "happened", applied_txn: null, applied_id: null }] });
    api.runs.mockResolvedValue([r1]);
    api.run.mockResolvedValue({ run: r1, txn_ids: ["t1"] });
    const { container } = wrap(<RunsView />);
    const row = await screen.findByRole("button", { name: /ask/ });
    expect(container.querySelector("span.badge.run-ok")).not.toBeNull();
    fireEvent.click(row);
    expect(await screen.findByText("Email Bob")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
    expect(screen.getByText("t1")).toBeTruthy();
  });
});

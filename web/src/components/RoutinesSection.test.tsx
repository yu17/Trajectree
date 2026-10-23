import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { RoutinesSection } from "./RoutinesSection";
import type { Routine, Run } from "../api/types";

const api = vi.hoisted(() => ({ routines: vi.fn(), scheduler: vi.fn(), patchRoutine: vi.fn(), runRoutine: vi.fn(), run: vi.fn(), acceptProposal: vi.fn(), dismissProposal: vi.fn() }));
vi.mock("../api/client", () => ({ api, ApiError: class extends Error {} }));

const wrap = (ui: React.ReactElement) => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>);
const routine = (name: string, over: Partial<Routine> = {}): Routine => ({ id: `rt-${name}`, name, trigger: "0 9 * * 1", tz: "Europe/Paris", enabled: true, runner: null, model: null, prompt_ref: "weekly", scope: { projects: null, lookback_days: 7 }, cwd_mode: "data_dir", tool_policy: [], caps: { usd_per_run: 1, usd_per_day: 5 }, write_mode: "propose", delivery: "inbox", props: {}, next_fire: "2026-10-26T08:00:00Z", last_fired: null, last_run_id: null, last_error: null, paused_until: null, created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z", ...over });
const run = (id: string, status: Run["status"]): Run => ({ id, kind: "routine", project_id: null, status, runner: "claude", backend: "claude", model: null, session_id: null, started: null, finished: null, tokens_in: 0, tokens_out: 0, tokens_cache: 0, est_usd: 0, turns: 0, duration_ms: 0, result_md: "", proposals: [], error: null, log_path: null, token_id: null, props: {}, routine_id: "rt-rev", slot: "interactive", attempt: 1, created_at: "2026-10-22T10:00:00Z" });

describe("RoutinesSection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.scheduler.mockResolvedValue({ last_tick: null, interval_s: 60, next_fires: [] });
  });

  it("lists routines, toggles enable and runs now", async () => {
    const user = userEvent.setup();
    api.routines.mockResolvedValue([routine("rev", { enabled: false, next_fire: null }), routine("weekly", { last_run_id: "run-old" })]);
    api.run.mockImplementation(async (id: string) => ({ run: run(id, id === "run-old" ? "ok" : "queued"), txn_ids: [] }));
    api.patchRoutine.mockImplementation(async (name: string, patch: Partial<Routine>) => routine(name, patch));
    api.runRoutine.mockResolvedValue(run("run-new", "queued"));
    const { container } = wrap(<RoutinesSection />);
    expect(screen.getByRole("heading", { name: "Routines" })).toBeTruthy();
    expect(await screen.findByText("last tick never · every 60 s", { exact: false })).toBeTruthy();
    const rev = (await screen.findByText("rev")).closest("tr")!;
    expect(within(rev).getByText("0 9 * * 1 (Europe/Paris)")).toBeTruthy();
    const weekly = screen.getByText("weekly").closest("tr")!;
    expect(within(weekly).getByText(new Date("2026-10-26T08:00:00Z").toLocaleString())).toBeTruthy();
    await waitFor(() => expect(weekly.querySelector("span.badge.run-ok")).not.toBeNull());
    expect(within(weekly).getByRole("button", { name: "Disable" })).toBeTruthy();

    await user.click(within(rev).getByRole("button", { name: "Enable" }));
    expect(api.patchRoutine).toHaveBeenCalledWith("rev", { enabled: true });
    await waitFor(() => expect(api.routines).toHaveBeenCalledTimes(2));

    await user.click(within(rev).getByRole("button", { name: "Run now" }));
    expect(api.runRoutine).toHaveBeenCalledWith("rev");
    expect(await screen.findByText(/Run queued/)).toBeTruthy();
    expect(screen.getByText("Run queued (run-new)")).toBeTruthy();
    await waitFor(() => expect(container.querySelector("tr span.badge.run-queued")).not.toBeNull());
    expect(api.run).toHaveBeenCalledWith("run-new");
  });

  it("refreshes routines once a queued run settles", async () => {
    const user = userEvent.setup();
    api.routines.mockResolvedValue([routine("rev")]);
    api.runRoutine.mockResolvedValue(run("run-new", "queued"));
    api.run.mockResolvedValue({ run: run("run-new", "failed"), txn_ids: [] });
    const { container } = wrap(<RoutinesSection />);
    await user.click(await screen.findByRole("button", { name: "Run now" }));
    await waitFor(() => expect(container.querySelector("tr span.badge.run-failed")).not.toBeNull());
    await waitFor(() => expect(api.routines).toHaveBeenCalledTimes(2));
  });

  it("shows the empty state", async () => {
    api.routines.mockResolvedValue([]);
    api.scheduler.mockResolvedValue({ last_tick: "2026-10-22T09:00:00Z", interval_s: 30, next_fires: [] });
    wrap(<RoutinesSection />);
    expect(await screen.findByText("No routines")).toBeTruthy();
    expect(await screen.findByText(`last tick ${new Date("2026-10-22T09:00:00Z").toLocaleString()} · every 30 s`, { exact: false })).toBeTruthy();
  });
});

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { RoutinesSection } from "./RoutinesSection";
import { FABLE, pickFable, testCatalog } from "./testCatalog";
import type { Routine, Run } from "../api/types";

const api = vi.hoisted(() => ({ routines: vi.fn(), scheduler: vi.fn(), patchRoutine: vi.fn(), runRoutine: vi.fn(), run: vi.fn(), acceptProposal: vi.fn(), dismissProposal: vi.fn(), models: vi.fn() }));
vi.mock("../api/client", () => ({ api, ApiError: class extends Error {} }));

const wrap = (ui: React.ReactElement) => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>);
const routine = (name: string, over: Partial<Routine> = {}): Routine => ({ id: `rt-${name}`, name, trigger: "0 9 * * 1", tz: "Europe/Paris", enabled: true, runner: null, model: null, prompt_ref: "weekly", scope: { projects: null, lookback_days: 7 }, cwd_mode: "data_dir", tool_policy: [], caps: { usd_per_run: 1, usd_per_day: 5 }, write_mode: "propose", delivery: "inbox", props: {}, next_fire: "2026-10-26T08:00:00Z", last_fired: null, last_run_id: null, last_error: null, paused_until: null, created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z", ...over });
const run = (id: string, status: Run["status"]): Run => ({ id, kind: "routine", project_id: null, status, runner: "claude", backend: "claude", model: null, session_id: null, started: null, finished: null, tokens_in: 0, tokens_out: 0, tokens_cache: 0, est_usd: 0, turns: 0, duration_ms: 0, result_md: "", proposals: [], error: null, log_path: null, token_id: null, props: {}, routine_id: "rt-rev", slot: "interactive", attempt: 1, created_at: "2026-10-22T10:00:00Z" });

describe("RoutinesSection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.scheduler.mockResolvedValue({ last_tick: null, interval_s: 60, next_fires: [] });
    api.models.mockResolvedValue(testCatalog);
  });

  it("lists routines, toggles enable and runs now", async () => {
    const user = userEvent.setup();
    api.routines.mockResolvedValue([routine("rev", { enabled: false, next_fire: null, ...FABLE }), routine("weekly", { last_run_id: "run-old" })]);
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
    api.routines.mockResolvedValue([routine("rev", FABLE)]);
    api.runRoutine.mockResolvedValue(run("run-new", "queued"));
    api.run.mockResolvedValue({ run: run("run-new", "failed"), txn_ids: [] });
    const { container } = wrap(<RoutinesSection />);
    await user.click(await screen.findByRole("button", { name: "Run now" }));
    await waitFor(() => expect(container.querySelector("tr span.badge.run-failed")).not.toBeNull());
    await waitFor(() => expect(api.routines).toHaveBeenCalledTimes(2));
  });

  it("shows the stored model, saves a new choice and keeps Run now off without a model", async () => {
    api.routines.mockResolvedValue([routine("bare"), routine("set", { runner: "claude", model: "claude-sonnet-5-5" })]);
    api.patchRoutine.mockImplementation(async (name: string, patch: Partial<Routine>) => routine(name, patch));
    wrap(<RoutinesSection />);
    const bare = (await screen.findByText("bare")).closest("tr")!;
    const set = screen.getByText("set").closest("tr")!;
    const barePicker = within(bare).getByRole("combobox", { name: "Model" }) as HTMLSelectElement;
    expect(barePicker.value).toBe("");
    expect((within(bare).getByRole("button", { name: "Run now" }) as HTMLButtonElement).disabled).toBe(true);
    await waitFor(() => expect((within(set).getByRole("combobox", { name: "Model" }) as HTMLSelectElement).value).toBe("claude|claude-sonnet-5-5"));
    expect((within(set).getByRole("button", { name: "Run now" }) as HTMLButtonElement).disabled).toBe(false);
    await pickFable(barePicker);
    await waitFor(() => expect(api.patchRoutine).toHaveBeenCalledWith("bare", FABLE));
    await waitFor(() => expect(api.routines).toHaveBeenCalledTimes(2));
  });

  it("selects the unique runner for a model-only routine", async () => {
    api.routines.mockResolvedValue([routine("only", { model: "claude-sonnet-5-5" })]);
    wrap(<RoutinesSection />);
    const row = (await screen.findByText("only")).closest("tr")!;
    await waitFor(() => expect((within(row).getByRole("combobox", { name: "Model" }) as HTMLSelectElement).value).toBe("claude|claude-sonnet-5-5"));
    expect(within(row).queryByText(/stored:/)).toBeNull();
    expect((within(row).getByRole("button", { name: "Run now" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("shows the stored text and keeps Run now on for a model the catalog does not offer", async () => {
    api.routines.mockResolvedValue([routine("odd", { model: "gpt-9" }), routine("odd2", { runner: "codex", model: "gpt-9" })]);
    wrap(<RoutinesSection />);
    const row = (await screen.findByText("odd")).closest("tr")!;
    expect(await within(row).findByText("stored: gpt-9")).toBeTruthy();
    expect((within(row).getByRole("combobox", { name: "Model" }) as HTMLSelectElement).value).toBe("");
    expect((within(row).getByRole("button", { name: "Run now" }) as HTMLButtonElement).disabled).toBe(false);
    const row2 = screen.getByText("odd2").closest("tr")!;
    expect(await within(row2).findByText("stored: gpt-9 on codex")).toBeTruthy();
  });

  it("shows a runner-only routine as that runner's default and lets it run", async () => {
    api.routines.mockResolvedValue([routine("half", { runner: "claude" }), routine("gone", { runner: "codex" })]);
    wrap(<RoutinesSection />);
    const row = (await screen.findByText("half")).closest("tr")!;
    await waitFor(() => expect((within(row).getByRole("combobox", { name: "Model" }) as HTMLSelectElement).value).toBe("claude|"));
    expect((within(row).getByRole("button", { name: "Run now" }) as HTMLButtonElement).disabled).toBe(false);
    expect(within(row).queryByText(/stored:/)).toBeNull();
    const gone = screen.getByText("gone").closest("tr")!;
    expect(await within(gone).findByText("stored: default on codex")).toBeTruthy();
    expect((within(gone).getByRole("button", { name: "Run now" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("saves a runner's default as a null model", async () => {
    api.routines.mockResolvedValue([routine("bare")]);
    api.patchRoutine.mockImplementation(async (name: string, patch: Partial<Routine>) => routine(name, patch));
    wrap(<RoutinesSection />);
    const row = (await screen.findByText("bare")).closest("tr")!;
    const picker = within(row).getByRole("combobox", { name: "Model" });
    await waitFor(() => expect(picker.querySelector('option[value="claude|"]')).not.toBeNull());
    await userEvent.setup().selectOptions(picker, "claude|");
    await waitFor(() => expect(api.patchRoutine).toHaveBeenCalledWith("bare", { runner: "claude", model: null }));
  });

  it("shows the API's refusal when a routine without a model is enabled", async () => {
    const user = userEvent.setup();
    api.routines.mockResolvedValue([routine("bare", { enabled: false })]);
    api.patchRoutine.mockRejectedValue(new Error("no model or runner was chosen"));
    wrap(<RoutinesSection />);
    await user.click(await screen.findByRole("button", { name: "Enable" }));
    expect(await screen.findByText("no model or runner was chosen")).toBeTruthy();
  });

  it("shows the empty state", async () => {
    api.routines.mockResolvedValue([]);
    api.scheduler.mockResolvedValue({ last_tick: "2026-10-22T09:00:00Z", interval_s: 30, next_fires: [] });
    wrap(<RoutinesSection />);
    expect(await screen.findByText("No routines")).toBeTruthy();
    expect(await screen.findByText(`last tick ${new Date("2026-10-22T09:00:00Z").toLocaleString()} · every 30 s`, { exact: false })).toBeTruthy();
  });
});

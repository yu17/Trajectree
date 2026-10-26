import { render, screen, waitFor, within, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { ProjectView } from "./ProjectView";
import { FABLE, pickFable, testCatalog } from "../components/testCatalog";
import type { Event, Project, Routine, Run, TimelineResponse } from "../api/types";

const api = vi.hoisted(() => ({ projects: vi.fn(), timeline: vi.fn(), resources: vi.fn(), signals: vi.fn(), routines: vi.fn(), summarize: vi.fn(), inspect: vi.fn(), run: vi.fn(), models: vi.fn() }));
vi.mock("../api/client", () => ({ api, ApiError: class extends Error {} }));

const wrap = (ui: React.ReactElement) => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>);
const project = (id: string, slug: string, over: Partial<Project> = {}): Project => ({ id, slug, title: slug.toUpperCase(), kind: "research", status: "active", lane_order: 0, summary_md: "", recurrence: null, props: {}, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z", ...over });
const event = (id: string, t: string, title: string): Event => ({ id, project_id: "p1", kind: "decision", title, body_md: "", t_start: t, t_end: null, status: "happened", source: "human", props: {}, created_txn: null, created_at: t, updated_at: t });
const routine = (name: string, projects: string[] | null): Routine => ({ id: `rt-${name}`, name, trigger: "0 9 * * 1", tz: "UTC", enabled: true, runner: null, model: null, prompt_ref: "weekly", scope: { projects, lookback_days: 7 }, cwd_mode: "data_dir", tool_policy: [], caps: { usd_per_run: 1, usd_per_day: 5 }, write_mode: "propose", delivery: "inbox", props: {}, next_fire: null, last_fired: null, last_run_id: null, last_error: null, paused_until: null, created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z" });
const run = (id: string, status: Run["status"]): Run => ({ id, kind: "summarize", project_id: "p1", status, runner: "claude", backend: "claude", model: null, session_id: null, started: null, finished: null, tokens_in: 0, tokens_out: 0, tokens_cache: 0, est_usd: 0, turns: 0, duration_ms: 0, result_md: "", proposals: [], error: null, log_path: null, token_id: null, props: {}, routine_id: null, slot: "interactive", attempt: 1, created_at: "2026-10-22T10:00:00Z" });

const timeline = (over: Partial<TimelineResponse> = {}): TimelineResponse => ({
  projects: [project("p1", "clmi")],
  events: [event("e1", "2026-09-01T10:00:00Z", "Older decision"), event("e2", "2026-10-20T10:00:00Z", "Newest decision")],
  todos: [
    { id: "td1", project_id: "p1", title: "Email Bob", body_md: "", due: null, state: "open", priority: 0, props: {}, created_at: null, updated_at: null },
    { id: "td2", project_id: "p1", title: "Done thing", body_md: "", due: null, state: "done", priority: 0, props: {}, created_at: null, updated_at: null },
  ],
  edges: [], pending: { event: {}, todo: {}, edge: {} }, strip: [],
  live: [{ project_id: "p1", session_id: "s1", cwd: "/home/u/clmi", title: null, started: null, last_seen: "2026-10-22T10:00:00Z" }],
  stale: ["p1"], due: { p1: 3 },
  ...over,
});

describe("ProjectView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.projects.mockResolvedValue([project("p1", "clmi", { summary_md: "CLMI is a two-stage model." }), project("p2", "ccx")]);
    api.timeline.mockResolvedValue(timeline());
    api.resources.mockResolvedValue([
      { id: "r1", project_id: "p1", kind: "folder", locator: "/home/u/clmi", title: "clmi", props: {}, created_at: null, updated_at: null },
      { id: "r2", project_id: "p1", kind: "slurm_job", locator: "hpc:4242", title: "train", props: {}, created_at: null, updated_at: null },
    ]);
    api.signals.mockResolvedValue([
      { id: "sg1", project_id: "p1", resource_id: null, kind: "job_state", t_start: "2026-10-21T08:00:00Z", t_end: null, payload: { job_id: "4242", name: "train", state: "PENDING", partition: "gpu", workdir: "/w" }, dedupe_key: "a", updated_at: "2026-10-21T08:00:00Z" },
      { id: "sg2", project_id: "p1", resource_id: null, kind: "job_state", t_start: "2026-10-21T09:00:00Z", t_end: null, payload: { job_id: "4242", name: "train", state: "COMPLETED", partition: "gpu", workdir: "/w" }, dedupe_key: "b", updated_at: "2026-10-21T12:00:00Z" },
      { id: "sg3", project_id: "p1", resource_id: null, kind: "session", t_start: "2026-10-21T09:00:00Z", t_end: null, payload: { session_id: "x" }, dedupe_key: "c", updated_at: "2026-10-21T13:00:00Z" },
    ]);
    api.routines.mockResolvedValue([routine("everyone", null), routine("mine", ["clmi"]), routine("byid", ["p1"]), routine("theirs", ["ccx"])]);
    api.models.mockResolvedValue(testCatalog);
    api.summarize.mockResolvedValue(run("run9", "queued"));
    api.run.mockResolvedValue({ run: run("run9", "queued"), txn_ids: [] });
  });

  it("shows summary, resources with live hints and routines touching the project", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    wrap(<ProjectView projectId="p1" onClose={onClose} />);
    expect(await screen.findByRole("heading", { name: "CLMI" })).toBeTruthy();
    expect(screen.getByText("research · active")).toBeTruthy();
    for (const h of ["Summary", "Events", "Todos", "Resources", "Routines"]) expect(screen.getByRole("heading", { name: h })).toBeTruthy();
    expect(screen.getByText("CLMI is a two-stage model.").tagName).toBe("PRE");
    expect(await screen.findByText("3 stretches to inspect")).toBeTruthy();

    const events = screen.getByRole("list", { name: "Events" });
    const items = within(events).getAllByRole("listitem");
    expect(items[0].textContent).toContain("Newest decision");
    expect(items[1].textContent).toContain("Older decision");
    const todos = screen.getByRole("list", { name: "Todos" });
    expect(within(todos).getByText("Email Bob")).toBeTruthy();
    expect(within(todos).queryByText("Done thing")).toBeNull();

    const resources = screen.getByRole("list", { name: "Resources" });
    const folder = within(resources).getByText("/home/u/clmi").closest("li")!;
    expect(within(folder).getByText("running")).toBeTruthy();
    const job = within(resources).getByText("hpc:4242").closest("li")!;
    expect(await within(job).findByText("COMPLETED")).toBeTruthy();
    expect(within(job).queryByText("running")).toBeNull();

    const routines = screen.getByRole("list", { name: "Routines" });
    expect(within(routines).getByText("everyone", { exact: false })).toBeTruthy();
    expect(within(routines).getByText("mine", { exact: false })).toBeTruthy();
    expect(within(routines).getByText("byid", { exact: false })).toBeTruthy(); // scope names the project by id
    expect(within(routines).queryByText("theirs", { exact: false })).toBeNull();

    expect(api.signals).toHaveBeenCalledWith(["p1"], expect.any(Date), expect.any(Date), { kinds: ["job_state"], limit: 500 });
    expect(api.timeline).toHaveBeenCalledWith(expect.any(Date), expect.any(Date), ["p1"]);
    const [from, to] = api.timeline.mock.calls[0] as [Date, Date];
    expect(Math.round((to.getTime() - from.getTime()) / 86_400_000)).toBe(425);
    expect(api.resources).toHaveBeenCalledWith("p1");
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalled();
  });

  it("shows No summary yet without a summary", async () => {
    api.projects.mockResolvedValue([project("p1", "clmi")]);
    api.timeline.mockResolvedValue(timeline({ stale: [], due: {}, live: [] }));
    wrap(<ProjectView projectId="p1" onClose={() => {}} />);
    expect(await screen.findByText("No summary yet")).toBeTruthy();
    await screen.findByText("Email Bob");
    expect(screen.queryByText(/stretches to inspect/)).toBeNull();
    expect(screen.queryByText("running")).toBeNull();
  });

  it("regenerates the summary from the button", async () => {
    const user = userEvent.setup();
    wrap(<ProjectView projectId="p1" onClose={() => {}} />);
    const button = await screen.findByRole("button", { name: "Regenerate summary" });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    await pickFable();
    await user.click(button);
    expect(api.summarize).toHaveBeenCalledWith("p1", FABLE);
    expect(await screen.findByText("Summary regeneration queued (run run9)")).toBeTruthy();
    await waitFor(() => expect(api.run).toHaveBeenCalledWith("run9"));
  });

  it("inspects with the chosen model", async () => {
    const user = userEvent.setup();
    api.inspect.mockResolvedValue(run("run10", "queued"));
    wrap(<ProjectView projectId="p1" onClose={() => {}} />);
    await pickFable(await screen.findByRole("combobox", { name: "Inspection model" }));
    await user.click(screen.getByRole("button", { name: "Inspect" }));
    await waitFor(() => expect(api.inspect).toHaveBeenCalledWith("p1", FABLE));
    expect(await screen.findByText("Inspection queued (run run10)")).toBeTruthy();
  });

  it("re-inspects since a date", async () => {
    const user = userEvent.setup();
    api.inspect.mockResolvedValue(run("run11", "queued"));
    wrap(<ProjectView projectId="p1" onClose={() => {}} />);
    await pickFable(await screen.findByRole("combobox", { name: "Inspection model" }));
    fireEvent.change(screen.getByLabelText("Re-inspect since"), { target: { value: "2026-09-01" } });
    await user.click(screen.getByRole("button", { name: "Re-inspect" }));
    await waitFor(() => expect(api.inspect).toHaveBeenCalledWith("p1", FABLE, "2026-09-01"));
  });
});

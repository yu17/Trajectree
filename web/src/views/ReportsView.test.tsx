import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { ReportsView } from "./ReportsView";
import { FABLE, pickFable, testCatalog } from "../components/testCatalog";
import type { ReportIn, ReportOut, ReportPatch } from "../api/types";

const mocks = vi.hoisted(() => ({ reports: vi.fn(), createReport: vi.fn(), updateReport: vi.fn(), draftReport: vi.fn(), run: vi.fn(), projects: vi.fn(), models: vi.fn() }));
vi.mock("../api/client", async importOriginal => {
  const real = await importOriginal<typeof import("../api/client")>();
  return { ...real, api: { ...real.api, ...mocks } };
});
import { api } from "../api/client";

const wrap = (ui: React.ReactElement) => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>);
const report = (id: string, over: Partial<ReportOut> = {}): ReportOut => ({ id, title: id, kind: "deck", scope: { projects: [], start: null, end: null }, source_md: "", props: {}, created_at: "2026-10-01T10:00:00Z", updated_at: "2026-10-01T10:00:00Z", pending_txn: null, ...over });
const run = (status: string, props: Record<string, unknown> = {}) => ({ run: { id: "run1", kind: "report", status, result_md: "", proposals: [], error: null, project_id: null, runner: "claude", backend: "claude", model: null, session_id: null, started: null, finished: null, tokens_in: 0, tokens_out: 0, tokens_cache: 0, est_usd: 0, turns: 0, duration_ms: 0, log_path: null, token_id: null, props, routine_id: null, slot: "interactive", attempt: 1, created_at: "2026-10-22T10:00:00Z" }, txn_ids: [] });
const deferred = <T,>() => {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>(r => (resolve = r));
  return { promise, resolve };
};
const projects = [{ id: "p1", slug: "clmi", title: "CLMI", kind: "research", status: "active" }, { id: "p2", slug: "old", title: "Old", kind: "research", status: "paused" }];

let store: ReportOut[];

describe("ReportsView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    store = [];
    mocks.reports.mockImplementation(async () => store.map(r => ({ ...r })));
    mocks.projects.mockResolvedValue(projects);
    mocks.models.mockResolvedValue(testCatalog);
    mocks.createReport.mockImplementation(async (body: ReportIn) => {
      const r = report("r1", { title: body.title, kind: body.kind, source_md: "# Memo" });
      store = [r, ...store];
      return r;
    });
    mocks.updateReport.mockImplementation(async (id: string, patch: ReportPatch) => {
      store = store.map(r => (r.id === id ? { ...r, ...patch, updated_at: "2026-10-22T11:00:00Z" } as ReportOut : r));
      return store.find(r => r.id === id);
    });
  });

  it("creates, edits and saves a report", async () => {
    const user = userEvent.setup();
    wrap(<ReportsView />);
    expect(await screen.findByText("No reports yet")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "New report" }));
    await user.type(screen.getByLabelText("Title"), "Memo");
    await user.selectOptions(screen.getByLabelText("Kind"), "memo");
    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(api.createReport).toHaveBeenCalledWith({ title: "Memo", kind: "memo" }));
    const source = await screen.findByLabelText("Source");
    expect((source as HTMLTextAreaElement).value).toBe("# Memo");
    expect(screen.getByRole("button", { name: "Save report" })).toBeDisabled();
    await user.clear(source);
    await user.type(source, "# Draft");
    await user.click(screen.getByRole("button", { name: "Save report" }));
    await waitFor(() => expect(api.updateReport).toHaveBeenCalledWith("r1", { source_md: "# Draft" }));
    expect(screen.getByRole("link", { name: "Export HTML" }).getAttribute("href")).toBe("/api/reports/r1/export?format=html");
    expect(screen.getByRole("link", { name: "Export PDF" }).getAttribute("href")).toBe("/api/reports/r1/export?format=pdf");
    expect(screen.getByRole("link", { name: "Export PPTX" }).getAttribute("target")).toBe("_blank");
    expect(screen.getByRole("heading", { name: "Preview" })).toBeTruthy();
    await waitFor(() => expect(screen.getByRole("button", { name: "Save report" })).toBeDisabled());
    expect(screen.getByRole("button", { name: /Memo · memo/ })).toBeTruthy();
  });

  it("drafts with the agent and selects the result when the run finishes", async () => {
    const user = userEvent.setup();
    // "Busy" was edited after the draft was saved: the run's report_id, not the newest, picks the draft
    store = [report("old1", { title: "Older", source_md: "# Older" }), report("busy", { title: "Busy", updated_at: "2026-10-22T10:10:00Z", source_md: "# Busy" })];
    mocks.draftReport.mockImplementation(async () => {
      store = [...store, report("new1", { title: "Weekly", updated_at: "2026-10-22T10:05:00Z", source_md: "# Agent deck", pending_txn: "t9" })];
      return run("queued").run;
    });
    mocks.run.mockResolvedValueOnce(run("running")).mockResolvedValue(run("ok", { report_id: "new1" }));
    const { container } = wrap(<ReportsView />);
    expect(await screen.findByRole("button", { name: /Older · deck/ })).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Draft with agent" }));
    expect(screen.queryByRole("option", { name: "Old" })).toBeNull();
    await user.type(screen.getByLabelText("Title"), "Weekly");
    await user.selectOptions(await screen.findByLabelText("Projects"), ["clmi"]);
    expect(screen.getByRole("button", { name: "Draft" })).toBeDisabled();
    await pickFable();
    await user.click(screen.getByRole("button", { name: "Draft" }));
    await waitFor(() => expect(api.draftReport).toHaveBeenCalledWith(expect.objectContaining({ kind: "deck", title: "Weekly", projects: ["clmi"], ...FABLE })));
    await waitFor(() => expect((screen.getByLabelText("Source") as HTMLTextAreaElement).value).toBe("# Agent deck"), { timeout: 4000 });
    expect(container.querySelector(".badge.run-ok")).not.toBeNull();
    expect(container.querySelector(".badge.review-pending")).not.toBeNull();
  });

  it("keeps Save report disabled until the refetch after a save resolves", async () => {
    const user = userEvent.setup();
    store = [report("r1", { title: "One", source_md: "# A" })];
    wrap(<ReportsView />);
    await user.click(await screen.findByRole("button", { name: /One · deck/ }));
    const source = await screen.findByLabelText("Source");
    await user.type(source, "!");
    const refetch = deferred<ReportOut[]>();
    mocks.reports.mockImplementationOnce(() => refetch.promise);
    const button = screen.getByRole("button", { name: "Save report" });
    await user.click(button);
    await waitFor(() => expect(api.updateReport).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(mocks.reports).toHaveBeenCalledTimes(2)); // the refetch is in flight
    expect(button).toBeDisabled();
    await user.click(button); // a double click files nothing more
    expect(api.updateReport).toHaveBeenCalledTimes(1);
    refetch.resolve(store.map(r => ({ ...r })));
    await waitFor(() => expect(screen.getByRole("button", { name: /One · deck · / }).textContent).toContain(new Date("2026-10-22T11:00:00Z").toLocaleString()));
    expect(screen.getByRole("button", { name: "Save report" })).toBeDisabled();
    expect((screen.getByLabelText("Source") as HTMLTextAreaElement).value).toBe("# A!");
  });

  it("asks before discarding unsaved edits when switching reports", async () => {
    const user = userEvent.setup();
    store = [report("r1", { title: "One", source_md: "# One" }), report("r2", { title: "Two", source_md: "# Two" })];
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    wrap(<ReportsView />);
    await user.click(await screen.findByRole("button", { name: /One · deck/ }));
    await user.click(screen.getByRole("button", { name: /Two · deck/ })); // clean: no question
    expect(confirm).not.toHaveBeenCalled();
    await user.type(screen.getByLabelText("Source"), " edited");
    await user.click(screen.getByRole("button", { name: /One · deck/ }));
    expect(confirm).toHaveBeenCalledWith("Discard unsaved changes?");
    expect((screen.getByLabelText("Source") as HTMLTextAreaElement).value).toBe("# Two edited");
    expect(screen.getByRole("button", { name: /Two · deck/ }).getAttribute("aria-current")).toBe("true");
    await user.click(screen.getByRole("button", { name: "New report" })); // the forms ask too
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("button", { name: "Create" })).toBeNull();
    confirm.mockReturnValue(true);
    await user.click(screen.getByRole("button", { name: /One · deck/ }));
    expect((await screen.findByLabelText("Source") as HTMLTextAreaElement).value).toBe("# One");
    expect(screen.getByRole("button", { name: /One · deck/ }).getAttribute("aria-current")).toBe("true");
    confirm.mockRestore();
  });
});

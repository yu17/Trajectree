import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { TimelineView } from "./TimelineView";
import { FABLE, pickFable, testCatalog } from "../components/testCatalog";
import type { Project, Run, TimelineResponse } from "../api/types";

const api = vi.hoisted(() => ({ timeline: vi.fn(), inspect: vi.fn(), models: vi.fn(), txn: vi.fn(), review: vi.fn(), revert: vi.fn() }));
vi.mock("../api/client", () => ({ api, ApiError: class extends Error {} }));

const wrap = (ui: React.ReactElement) => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>);
const project = (id: string, slug: string): Project => ({ id, slug, title: slug.toUpperCase(), kind: "research", status: "active", lane_order: 0, summary_md: "", recurrence: null, props: {}, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" });
const data: TimelineResponse = {
  projects: [project("p1", "clmi"), project("p2", "ccx")],
  events: [], todos: [], edges: [], pending: { event: {}, todo: {}, edge: {} }, strip: [], live: [], stale: ["p1", "p2"],
};

describe("TimelineView", () => {
  let resolvers: ((r: Run) => void)[];
  beforeEach(() => {
    vi.clearAllMocks();
    resolvers = [];
    api.timeline.mockResolvedValue(data);
    api.models.mockResolvedValue(testCatalog);
    api.inspect.mockImplementation(() => new Promise<Run>(res => resolvers.push(res)));
  });

  const staleChips = async (container: HTMLElement) => {
    await waitFor(() => expect(container.querySelectorAll("g.chip.stale").length).toBe(2));
    return container.querySelectorAll("g.chip.stale");
  };

  it("allows a stale click on a second project while the first inspects", async () => {
    const { container } = wrap(<TimelineView />);
    const chips = await staleChips(container);
    await pickFable(await screen.findByRole("combobox", { name: "Inspection model" }));
    // A second click on the same project while its run is pending is ignored.
    fireEvent.click(chips[0]);
    fireEvent.click(chips[0]);
    await waitFor(() => expect(api.inspect).toHaveBeenCalledTimes(1));
    fireEvent.click(chips[0]);
    fireEvent.click(chips[1]);
    await waitFor(() => expect(api.inspect).toHaveBeenCalledTimes(2));
    expect(api.inspect).toHaveBeenNthCalledWith(1, "p1", FABLE);
    expect(api.inspect).toHaveBeenNthCalledWith(2, "p2", FABLE);
    resolvers[0]({ id: "run1" } as Run);
    expect(await screen.findByText("Inspection queued (run run1)")).toBeTruthy();
    // Settled: the project may be regenerated again.
    await waitFor(() => {
      fireEvent.click(container.querySelectorAll("g.chip.stale")[0]);
      expect(api.inspect).toHaveBeenCalledTimes(3);
    });
    expect(api.inspect).toHaveBeenLastCalledWith("p1", FABLE);
  });

  it("asks for a model instead of inspecting when none is picked", async () => {
    const { container } = wrap(<TimelineView />);
    const chips = await staleChips(container);
    fireEvent.click(chips[0]);
    expect(await screen.findByText("Choose a model in the toolbar to inspect")).toBeTruthy();
    expect(api.inspect).not.toHaveBeenCalled();
  });

  it("queues an inspection from the chip", async () => {
    api.inspect.mockResolvedValue({ id: "run5" } as Run);
    const { container } = wrap(<TimelineView />);
    const chips = await staleChips(container);
    await pickFable(await screen.findByRole("combobox", { name: "Inspection model" }));
    fireEvent.click(chips[1]);
    expect(await screen.findByText("Inspection queued (run run5)")).toBeTruthy();
    expect(api.inspect).toHaveBeenCalledWith("p2", FABLE);
  });

  it("opens a project from its lane label", async () => {
    const onOpenProject = vi.fn();
    const { container } = wrap(<TimelineView onOpenProject={onOpenProject} />);
    await staleChips(container);
    fireEvent.click(container.querySelectorAll("g.lab text.lbl-name")[1]);
    expect(onOpenProject).toHaveBeenCalledWith("p2");
  });
});

import { act, render, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { Timeline } from "./Timeline";
import type { TimelineResponse } from "../api/types";

const data: TimelineResponse = {
  projects: [{ id: "p1", slug: "clmi", title: "CLMI", kind: "research", status: "active", lane_order: 0, summary_md: "", recurrence: null, props: {}, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" },
             { id: "p2", slug: "ccx", title: "ccx", kind: "tooling", status: "active", lane_order: 1, summary_md: "", recurrence: null, props: {}, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" }],
  events: [{ id: "e1", project_id: "p1", kind: "decision", title: "Freeze", body_md: "", t_start: "2026-01-05T10:00:00Z", t_end: null, status: "happened", source: "human", props: {}, created_txn: "t1", created_at: "2026-01-05T10:00:00Z", updated_at: "2026-01-05T10:00:00Z" },
           { id: "e2", project_id: "p2", kind: "milestone", title: "v0.1", body_md: "", t_start: "2026-01-08T10:00:00Z", t_end: null, status: "suggested", source: "agent", props: {}, created_txn: "t2", created_at: "2026-01-08T10:00:00Z", updated_at: "2026-01-08T10:00:00Z" }],
  todos: [], edges: [{ id: "x", src_type: "event", src_id: "e1", dst_type: "event", dst_id: "e2", rel: "led_to", props: {}, valid_from: null, valid_to: null, created_at: "2026-01-08T10:00:00Z" }],
  pending: { event: { e2: "t2" }, todo: {}, edge: {} }, strip: [], live: [], stale: [],
};

describe("Timeline", () => {
  it("renders a lane per project and a block per event with status classes", () => {
    const { container } = render(<Timeline data={data} now={new Date("2026-01-10T00:00:00Z")} range={[new Date("2026-01-01T00:00:00Z"), new Date("2026-02-01T00:00:00Z")]} selected={null} onSelect={() => {}} />);
    expect(container.querySelectorAll("g.lab").length).toBe(2);
    expect(container.querySelectorAll("g.block").length).toBe(2);
    expect(container.querySelector("g.block.k-milestone.s-suggested circle.pending")).not.toBeNull();
  });
  it("selecting a block draws its wire and reports the selection", () => {
    const onSelect = vi.fn();
    const { container, rerender } = render(<Timeline data={data} now={new Date("2026-01-10T00:00:00Z")} range={[new Date("2026-01-01T00:00:00Z"), new Date("2026-02-01T00:00:00Z")]} selected={null} onSelect={onSelect} />);
    expect(container.querySelectorAll("path.wire").length).toBe(0);
    fireEvent.click(container.querySelector("g.block.k-decision")!);
    expect(onSelect).toHaveBeenCalledWith({ type: "event", id: "e1" });
    rerender(<Timeline data={data} now={new Date("2026-01-10T00:00:00Z")} range={[new Date("2026-01-01T00:00:00Z"), new Date("2026-02-01T00:00:00Z")]} selected={{ type: "event", id: "e1" }} onSelect={onSelect} />);
    expect(container.querySelectorAll("path.wire").length).toBe(1);
  });
  it("hovering shows wires and the tooltip; background click clears the selection", () => {
    const onSelect = vi.fn();
    const { container } = render(<Timeline data={data} now={new Date("2026-01-10T00:00:00Z")} range={[new Date("2026-01-01T00:00:00Z"), new Date("2026-02-01T00:00:00Z")]} selected={null} onSelect={onSelect} />);
    const block = container.querySelector("g.block.k-milestone")!;
    fireEvent.mouseEnter(block);
    expect(container.querySelectorAll("path.wire").length).toBe(1);
    expect(container.querySelector("text.wire-lbl")!.textContent).toBe("led_to");
    expect(container.querySelector("path.wire")!.getAttribute("marker-end")).toMatch(/^url\(#/);
    expect(container.querySelector("g.block.k-milestone")!.classList.contains("hover")).toBe(true);
    expect(container.querySelector("g.block.k-decision")!.classList.contains("linked")).toBe(true);
    fireEvent.mouseMove(block, { clientX: 10, clientY: 10 });
    const tip = container.querySelector<HTMLDivElement>("div.tip")!;
    expect(tip.style.display).toBe("block");
    expect(tip.textContent).toContain("v0.1");
    expect(tip.textContent).toContain("milestone");
    fireEvent.mouseLeave(block);
    expect(container.querySelectorAll("path.wire").length).toBe(0);
    expect(tip.style.display).toBe("none");
    fireEvent.click(container.querySelector("svg")!);
    expect(onSelect).toHaveBeenCalledWith(null);
  });
  it("the tooltip flips above and left of the cursor near the window's bottom-right corner", () => {
    const { container } = render(<Timeline data={data} now={new Date("2026-01-10T00:00:00Z")} range={[new Date("2026-01-01T00:00:00Z"), new Date("2026-02-01T00:00:00Z")]} selected={null} onSelect={() => {}} />);
    const block = container.querySelector("g.block.k-milestone")!;
    fireEvent.mouseMove(block, { clientX: 100, clientY: 100 });
    const tip = container.querySelector<HTMLDivElement>("div.tip")!;
    expect([tip.style.left, tip.style.top]).toEqual(["112px", "112px"]);
    fireEvent.mouseMove(block, { clientX: window.innerWidth - 2, clientY: window.innerHeight - 2 });
    expect(parseFloat(tip.style.left)).toBeLessThan(window.innerWidth - 2);
    expect(parseFloat(tip.style.top)).toBeLessThan(window.innerHeight - 2);
  });
  it("a horizontal or shift wheel pans the timeline", () => {
    const { container } = render(<Timeline data={data} now={new Date("2026-01-10T00:00:00Z")} range={[new Date("2026-01-01T00:00:00Z"), new Date("2026-02-01T00:00:00Z")]} selected={null} onSelect={() => {}} />);
    const x = () => container.querySelector("g.block.k-decision")!.getAttribute("transform");
    const before = x();
    const svg = container.querySelector("svg")!;
    const swipe = new WheelEvent("wheel", { deltaX: 120, deltaY: 0, bubbles: true, cancelable: true });
    act(() => { svg.dispatchEvent(swipe); });
    expect(swipe.defaultPrevented).toBe(true);
    const panned = x();
    expect(panned).not.toBe(before);
    expect(parseFloat(panned!.match(/translate\(([-\d.]+)/)![1])).toBeLessThan(parseFloat(before!.match(/translate\(([-\d.]+)/)![1]));
    act(() => { svg.dispatchEvent(new WheelEvent("wheel", { deltaY: 120, shiftKey: true, bubbles: true, cancelable: true })); });
    expect(x()).not.toBe(panned);
  });
  it("re-lays out blocks when the range changes", () => {
    const now = new Date("2026-01-10T00:00:00Z");
    const { container, rerender } = render(<Timeline data={data} now={now} range={[new Date("2026-01-01T00:00:00Z"), new Date("2026-02-01T00:00:00Z")]} selected={null} onSelect={() => {}} />);
    const before = container.querySelector("g.block.k-decision")!.getAttribute("transform");
    rerender(<Timeline data={data} now={now} range={[new Date("2026-01-04T00:00:00Z"), new Date("2026-01-12T00:00:00Z")]} selected={null} onSelect={() => {}} />);
    expect(container.querySelector("g.block.k-decision")!.getAttribute("transform")).not.toBe(before);
  });
  it("draws open todo due markers, the now line, the future zone and the axis", () => {
    const withTodos: TimelineResponse = {
      ...data,
      todos: [
        { id: "td1", project_id: "p1", title: "Reply", body_md: "", due: "2026-01-20T00:00:00Z", state: "open", priority: 0, props: {}, created_at: null, updated_at: null },
        { id: "td2", project_id: "p1", title: "Old", body_md: "", due: "2026-01-04T00:00:00Z", state: "done", priority: 0, props: {}, created_at: null, updated_at: null },
        { id: "td3", project_id: "p2", title: "Undated", body_md: "", due: null, state: "open", priority: 0, props: {}, created_at: null, updated_at: null },
      ],
    };
    const { container } = render(<Timeline data={withTodos} now={new Date("2026-01-10T00:00:00Z")} range={[new Date("2026-01-01T00:00:00Z"), new Date("2026-02-01T00:00:00Z")]} selected={null} onSelect={() => {}} />);
    expect(container.querySelectorAll("path.todo-marker").length).toBe(1);
    expect(container.querySelector("line.now")).not.toBeNull();
    expect(container.querySelector("rect.future")).not.toBeNull();
    expect(container.querySelector("g.axis")).not.toBeNull();
    expect(container.querySelector("g.block.k-decision circle.pending")).toBeNull();
    expect(container.querySelector("g.lab")!.textContent).toContain("CLMI");
    expect(container.querySelector("g.lab")!.textContent).toContain("research");
  });
  it("renders strip cells, a live marker and lane chips; clicking a cell selects the bin", () => {
    const onSelect = vi.fn();
    const d = { ...data, strip: [{ project_id: "p1", day: "2026-01-05", kind: "session" as const, count: 3 }], stale: ["p2"],
      live: [{ project_id: "p1", session_id: "s", cwd: "/c", title: null, started: null, last_seen: "2026-01-10T00:00:00Z" }] };
    const { container } = render(<Timeline data={d} now={new Date("2026-01-10T00:00:00Z")} range={[new Date("2026-01-01T00:00:00Z"), new Date("2026-02-01T00:00:00Z")]} selected={null} onSelect={onSelect} />);
    expect(container.querySelectorAll("rect.strip-cell").length).toBe(1);
    expect(container.querySelector("rect.strip-cell")!.classList.contains("l-2")).toBe(true);
    expect(container.querySelectorAll("circle.live").length).toBe(1);
    expect(container.querySelector("g.chip.running text")!.textContent).toBe("● running");
    expect(container.querySelector("g.chip.stale text")!.textContent).toBe("to inspect");
    fireEvent.click(container.querySelector("rect.strip-cell")!);
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ type: "bin", projectId: "p1" }));
  });
  it("clicking the stale chip reports it", () => {
    const onChip = vi.fn();
    const onSelect = vi.fn();
    const d = { ...data, stale: ["p1"] };
    const { container } = render(<Timeline data={d} now={new Date("2026-01-10T00:00:00Z")} range={[new Date("2026-01-01T00:00:00Z"), new Date("2026-02-01T00:00:00Z")]} selected={null} onSelect={onSelect} onChip={onChip} />);
    fireEvent.click(container.querySelector("g.chip.stale")!);
    expect(onChip).toHaveBeenCalledWith("p1", "stale");
    expect(onSelect).not.toHaveBeenCalled();
    expect(container.querySelector("g.chip.stale title")!.textContent).toBe("Inspect");
  });
  it("calls onLane when a lane label is clicked", () => {
    const onLane = vi.fn();
    const onChip = vi.fn();
    const onSelect = vi.fn();
    const d = { ...data, stale: ["p1"] };
    const { container } = render(<Timeline data={d} now={new Date("2026-01-10T00:00:00Z")} range={[new Date("2026-01-01T00:00:00Z"), new Date("2026-02-01T00:00:00Z")]} selected={null} onSelect={onSelect} onChip={onChip} onLane={onLane} />);
    const labels = container.querySelectorAll("g.lab");
    fireEvent.click(labels[1].querySelector("text.lbl-name")!);
    expect(onLane).toHaveBeenCalledWith("p2");
    fireEvent.click(labels[0].querySelector("text.lbl-kind")!);
    expect(onLane).toHaveBeenLastCalledWith("p1");
    expect(onSelect).not.toHaveBeenCalled();
    fireEvent.click(container.querySelector("g.chip.stale")!);
    expect(onChip).toHaveBeenCalledWith("p1", "stale");
    expect(onLane).toHaveBeenCalledTimes(2);
  });
  it("marks pending todos", () => {
    const d: TimelineResponse = {
      ...data,
      todos: [{ id: "td1", project_id: "p1", title: "Reply", body_md: "", due: "2026-01-20T00:00:00Z", state: "open", priority: 0, props: {}, created_at: null, updated_at: null }],
      pending: { event: {}, todo: { td1: "t9" }, edge: {} },
    };
    const { container } = render(<Timeline data={d} now={new Date("2026-01-10T00:00:00Z")} range={[new Date("2026-01-01T00:00:00Z"), new Date("2026-02-01T00:00:00Z")]} selected={null} onSelect={() => {}} />);
    expect(container.querySelector("path.todo-marker.pending")).not.toBeNull();
  });
  it("hides pills on a wide range and shows them on a narrow one", () => {
    const d: TimelineResponse = {
      ...data, edges: [],
      events: [
        { ...data.events[0], id: "b1", tier: "block" },
        { ...data.events[0], id: "pl1", title: "Tick", tier: "pill", t_start: "2026-01-06T10:00:00Z" },
      ],
    };
    const now = new Date("2026-01-10T00:00:00Z");
    const wide = render(<Timeline data={d} now={now} range={[new Date("2026-01-01T00:00:00Z"), new Date("2027-01-01T00:00:00Z")]} selected={null} onSelect={() => {}} />);
    expect(wide.container.querySelectorAll("g.block.tier-pill").length).toBe(0);
    expect(wide.container.querySelectorAll("g.block.tier-block").length).toBe(1);
    wide.unmount();
    const narrow = render(<Timeline data={d} now={now} range={[new Date("2026-01-01T00:00:00Z"), new Date("2026-01-21T00:00:00Z")]} selected={null} onSelect={() => {}} />);
    expect(narrow.container.querySelectorAll("g.block.tier-pill").length).toBe(1);
  });
  it("draws a block at its end date", () => {
    const d: TimelineResponse = {
      ...data, edges: [],
      events: [
        { ...data.events[0], id: "span", t_start: "2026-01-02T00:00:00Z", t_end: "2026-01-20T00:00:00Z", tier: "block" },
        { ...data.events[0], id: "pt", t_start: "2026-01-20T00:00:00Z", t_end: null, tier: "block" },
      ],
    };
    const { container } = render(<Timeline data={d} now={new Date("2026-01-10T00:00:00Z")} range={[new Date("2026-01-01T00:00:00Z"), new Date("2026-02-01T00:00:00Z")]} selected={null} onSelect={() => {}} />);
    const xs = [...container.querySelectorAll("g.block")].map(g => /translate\(([-\d.]+),/.exec(g.getAttribute("transform")!)![1]);
    expect(xs.length).toBe(2);
    expect(xs[0]).toBe(xs[1]);
  });
  it("does not draw part_of as a wire", () => {
    const d: TimelineResponse = {
      ...data,
      edges: [{ ...data.edges[0], rel: "part_of" }],
    };
    const { container } = render(<Timeline data={d} now={new Date("2026-01-10T00:00:00Z")} range={[new Date("2026-01-01T00:00:00Z"), new Date("2026-02-01T00:00:00Z")]} selected={{ type: "event", id: "e1" }} onSelect={() => {}} />);
    expect(container.querySelectorAll("path.wire").length).toBe(0);
  });
  it("shows the inspect count on the chip", () => {
    const d = { ...data, stale: ["p1"], due: { p1: 3 } };
    const { container } = render(<Timeline data={d} now={new Date("2026-01-10T00:00:00Z")} range={[new Date("2026-01-01T00:00:00Z"), new Date("2026-02-01T00:00:00Z")]} selected={null} onSelect={() => {}} />);
    expect(container.querySelector("g.chip.stale text")!.textContent).toBe("3 to inspect");
    expect(container.querySelector("g.chip.stale title")!.textContent).toBe("Inspect");
  });
});

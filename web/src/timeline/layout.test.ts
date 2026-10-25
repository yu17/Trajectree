import { describe, it, expect } from "vitest";
import { packLane, cullVisible, labelWidth, binStrip, laneItems, pillsVisible, type LaneItem } from "./layout";
import type { Event } from "../api/types";

const xz = (t: number) => t / 1000; // 1px per second
const item = (id: string, start: number, title = "t", end: number | null = null): LaneItem => ({ id, start, end, title, kind: "note", tier: "block" });

describe("packLane", () => {
  it("places non-overlapping items in row 0 with labels", () => {
    const { blocks, rows } = packLane([item("a", 0), item("b", 500_000)], xz, 3);
    expect(rows).toBe(1);
    expect(blocks.map(b => [b.id, b.row, b.label])).toEqual([["a", 0, true], ["b", 0, true]]);
  });
  it("moves an item whose label would collide to the next row", () => {
    const { blocks, rows } = packLane([item("a", 0, "a long title here"), item("b", 20_000)], xz, 3);
    expect(rows).toBe(2);
    expect(blocks.find(b => b.id === "b")!.row).toBe(1);
  });
  it("drops the outside label instead of exceeding maxRows", () => {
    const items = [0, 1, 2, 3].map(i => item(`i${i}`, i * 5_000, "a long title here"));
    const { blocks, rows } = packLane(items, xz, 3);
    expect(rows).toBe(4);
    expect(blocks.find(b => b.id === "i3")!.label).toBe(false);
  });
  it("never overlaps within a row", () => {
    const items = Array.from({ length: 2000 }, (_, i) => item(`x${i}`, (i * 7919) % 3_000_000, `title ${i}`));
    const { blocks } = packLane(items, xz, 6);
    const byRow = new Map<number, typeof blocks>();
    blocks.forEach(b => byRow.set(b.row, [...(byRow.get(b.row) ?? []), b]));
    for (const row of byRow.values()) {
      row.sort((a, b) => a.px - b.px);
      for (let i = 1; i < row.length; i++) expect(row[i].px).toBeGreaterThanOrEqual(row[i - 1].px + row[i - 1].w);
    }
  });
  it("uses minW for point events and label inside wide blocks", () => {
    const { blocks } = packLane([item("p", 0), item("w", 100_000, "wide", 400_000)], xz, 3);
    expect(blocks.find(b => b.id === "p")!.w).toBe(16);
    expect(blocks.find(b => b.id === "w")!.inside).toBe(true);
  });
});

describe("cullVisible", () => {
  it("drops offscreen items beyond the margin", () => {
    const items = [item("left", -1_000_000), item("in", 10_000), item("right", 5_000_000)];
    expect(cullVisible(items, xz, 1000).map(i => i.id)).toEqual(["in"]);
  });
});

describe("labelWidth", () => {
  it("caps at 170", () => { expect(labelWidth("x".repeat(100))).toBe(170); });
});

describe("binStrip", () => {
  const bins = [
    { project_id: "p", day: "2026-01-05", kind: "session" as const, count: 1 },
    { project_id: "p", day: "2026-01-06", kind: "session" as const, count: 2 },
    { project_id: "p", day: "2026-01-06", kind: "commits" as const, count: 5 },
    { project_id: "p", day: "2026-01-12", kind: "commits" as const, count: 1 },
  ];
  it("keeps days apart in day mode and levels by log2", () => {
    const cells = binStrip(bins, "day");
    expect(cells.map(c => [c.key, c.sessions, c.commits, c.level])).toEqual([["2026-01-05", 1, 0, 1], ["2026-01-06", 2, 5, 3], ["2026-01-12", 0, 1, 1]]);
    expect(cells[0].end - cells[0].start).toBe(86_400_000);
  });
  it("groups ISO weeks starting Monday", () => {
    const cells = binStrip(bins, "week");
    expect(cells.map(c => [c.key, c.total])).toEqual([["2026-W02", 8], ["2026-W03", 1]]);
    expect(new Date(cells[0].start).toISOString()).toBe("2026-01-05T00:00:00.000Z");
  });
  it("groups months and caps the level at 5", () => {
    const cells = binStrip([...bins, { project_id: "p", day: "2026-01-20", kind: "session" as const, count: 40 }], "month");
    expect(cells).toHaveLength(1);
    expect(cells[0].key).toBe("2026-01");
    expect(cells[0].level).toBe(5);
  });
});

const ev = (id: string, tier: "block" | "pill", t_start: string, t_end: string | null = null): Event => ({
  id, project_id: "p", kind: "note", title: id, body_md: "", t_start, t_end, status: "happened", source: "human",
  props: {}, created_txn: null, created_at: t_start, updated_at: t_start, tier,
});

describe("laneItems", () => {
  it("places a block at its end date and keeps pills only when visible", () => {
    const evs = [ev("b", "block", "2026-01-01T00:00:00Z", "2026-01-20T00:00:00Z"), ev("p", "pill", "2026-01-05T00:00:00Z")];
    const shown = laneItems(evs, true);
    const b = shown.find(i => i.id === "b")!;
    expect(b.start).toBe(Date.parse("2026-01-20T00:00:00Z"));
    expect(b.end).toBeNull();
    expect(shown.find(i => i.id === "p")!.start).toBe(Date.parse("2026-01-05T00:00:00Z"));
    expect(laneItems(evs, false).map(i => i.id)).toEqual(["b"]);
  });
});

describe("pillsVisible", () => {
  it("flips at PILL_ZOOM_DAYS", () => {
    expect(pillsVisible(10, 600)).toBe(true);
    expect(pillsVisible(5, 600)).toBe(false);
  });
});

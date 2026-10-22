import { describe, it, expect } from "vitest";
import { packLane, cullVisible, labelWidth, type LaneItem } from "./layout";

const xz = (t: number) => t / 1000; // 1px per second
const item = (id: string, start: number, title = "t", end: number | null = null): LaneItem => ({ id, start, end, title, kind: "note" });

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

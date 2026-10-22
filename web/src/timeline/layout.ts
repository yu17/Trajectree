// Pure lane packing and culling for the timeline. No DOM, no d3.
// Ported from docs/superpowers/specs/mockups/2026-10-21-timeline-lane-model-a.html.
import type { StripBin } from "../api/types";

export interface LaneItem {
  id: string;
  start: number; // epoch ms
  end: number | null; // epoch ms
  title: string;
  kind: string;
}

export interface PlacedItem extends LaneItem {
  px: number;
  w: number;
  row: number;
  label: boolean;
  inside: boolean;
}

export function labelWidth(title: string): number {
  return Math.min(title.length * 6.4 + 14, 170);
}

export function cullVisible<T extends Pick<LaneItem, "start" | "end">>(
  items: T[],
  xz: (t: number) => number,
  plotW: number,
  margin = 400,
): T[] {
  return items.filter(it => {
    const a = xz(it.start);
    const b = it.end ? xz(it.end) : a;
    return b >= -margin && a <= plotW + margin;
  });
}

export function packLane(
  items: LaneItem[],
  xz: (t: number) => number,
  maxRows: number,
  minW = 16,
): { blocks: PlacedItem[]; rows: number } {
  const prep = items
    .map(it => {
      const px = xz(it.start);
      const w = Math.max(it.end ? xz(it.end) - px : 0, minW);
      const lw = labelWidth(it.title);
      return { it, lw, placed: { ...it, px, w, row: 0, label: true, inside: w >= lw + 6 } as PlacedItem };
    })
    .sort((a, b) => a.placed.px - b.placed.px);

  const rowEnds: number[] = [];
  for (const { lw, placed: b } of prep) {
    const withLabel = b.w + (b.inside ? 0 : lw + 4);
    const r = rowEnds.findIndex(end => end + 3 <= b.px);
    if (r >= 0) {
      rowEnds[r] = b.px + withLabel;
      b.row = r;
    } else if (rowEnds.length < maxRows) {
      rowEnds.push(b.px + withLabel);
      b.row = rowEnds.length - 1;
    } else {
      // over budget: open a new row, outside label dropped
      rowEnds.push(b.px + b.w);
      b.row = rowEnds.length - 1;
      b.label = b.inside;
    }
  }

  return { blocks: prep.map(p => p.placed), rows: Math.max(1, rowEnds.length) };
}

export function binKey(_t: number, dayW: number): "day" | "week" | "month" {
  if (dayW >= 3) return "day";
  if (dayW * 7 >= 3) return "week";
  return "month";
}

export interface StripCell {
  key: string;
  start: number; // epoch ms, bucket start (inclusive)
  end: number; // epoch ms, bucket end (exclusive)
  sessions: number;
  commits: number;
  total: number;
  level: 1 | 2 | 3 | 4 | 5;
}

const DAY_MS = 86_400_000;
const pad2 = (n: number) => String(n).padStart(2, "0");

// The bucket holding a UTC day: the day itself, its ISO week (Monday start) or its calendar month.
function bucketOf(day: string, mode: "day" | "week" | "month"): { key: string; start: number; end: number } {
  const [y, m, d] = day.split("-").map(Number);
  const dayStart = Date.UTC(y, m - 1, d);
  if (mode === "day") return { key: day, start: dayStart, end: dayStart + DAY_MS };
  if (mode === "month") return { key: `${y}-${pad2(m)}`, start: Date.UTC(y, m - 1, 1), end: Date.UTC(y, m, 1) };
  const start = dayStart - ((new Date(dayStart).getUTCDay() + 6) % 7) * DAY_MS;
  const thursday = new Date(start + 3 * DAY_MS); // an ISO week belongs to its Thursday's year
  const isoYear = thursday.getUTCFullYear();
  const week = 1 + Math.floor((thursday.getTime() - Date.UTC(isoYear, 0, 1)) / DAY_MS / 7);
  return { key: `${isoYear}-W${pad2(week)}`, start, end: start + 7 * DAY_MS };
}

export function binStrip(bins: StripBin[], mode: "day" | "week" | "month"): StripCell[] {
  const cells = new Map<string, StripCell>();
  for (const b of bins) {
    const { key, start, end } = bucketOf(b.day, mode);
    let c = cells.get(key);
    if (!c) {
      c = { key, start, end, sessions: 0, commits: 0, total: 0, level: 1 };
      cells.set(key, c);
    }
    if (b.kind === "session") c.sessions += b.count;
    if (b.kind === "commits") c.commits += b.count;
    c.total += b.count;
  }
  for (const c of cells.values()) {
    c.level = Math.max(1, Math.min(5, Math.floor(Math.log2(c.total)) + 1)) as StripCell["level"];
  }
  return [...cells.values()].sort((a, b) => a.start - b.start);
}

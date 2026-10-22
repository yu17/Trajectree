// Pure lane packing and culling for the timeline. No DOM, no d3.
// Ported from docs/superpowers/specs/mockups/2026-10-21-timeline-lane-model-a.html.

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

export function cullVisible<T extends LaneItem>(
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

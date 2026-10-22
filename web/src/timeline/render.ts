// d3 join render for the timeline SVG (lane model A).
// Ported from render(), wirePath, mid, trunc, fmtWhen and showTip in
// docs/superpowers/specs/mockups/2026-10-21-timeline-lane-model-a.html.
// Every call is idempotent: layers are created by keyed joins, never cached.
import * as d3 from "d3";
import type { Edge, Event, TimelineResponse, Todo } from "../api/types";
import { cullVisible, labelWidth, packLane, type LaneItem, type PlacedItem } from "./layout";
import type { Selection } from "./Timeline";

export const GUTTER = 176;
export const AXIS_H = 34;
export const ROW_H = 28;
export const BLOCK_H = 22;
export const STRIP_SLOT = 18; // reserved below the rows for the activity strip; empty in phase 1
export const PAD = 8;
export const MIN_W = 16;
export const MAX_ROWS = 3;
const TODO_HALF = 6; // 12px diamond

export const glyph: Record<string, string> = {
  decision: "◆", meeting: "●", milestone: "▲", idea: "✦", note: "✦", task: "☐",
};

const fmtLong = d3.timeFormat("%a %d %b %Y, %H:%M");
const fmtTime = d3.timeFormat("%H:%M");

export function fmtWhen(start: Date, end: Date | null): string {
  return fmtLong(start) + (end ? " → " + fmtTime(end) : "");
}

export function trunc(t: string, w: number): string {
  const n = Math.floor(w / 6.4);
  return t.length > n ? t.slice(0, Math.max(1, n - 1)) + "…" : t;
}

interface Box { x: number; y: number; w: number; h: number }

function mid(b: Box) {
  return { x: b.x + b.w / 2, y: b.y + b.h / 2 };
}

export function wirePath(a: Box, b: Box): string {
  const forward = b.x >= a.x + a.w;
  const x1 = forward ? a.x + a.w : a.x;
  const x2 = forward ? b.x : b.x + b.w;
  const y1 = a.y + a.h / 2;
  const y2 = b.y + b.h / 2;
  if (Math.abs(y1 - y2) < 1) return `M${x1},${y1} Q${(x1 + x2) / 2},${y1 - ROW_H * 0.9} ${x2},${y2}`;
  const dx = Math.max(28, Math.abs(x2 - x1) / 2) * (forward ? 1 : -1);
  return `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
}

export interface RenderInput {
  data: TimelineResponse;
  now: Date;
  range: [Date, Date];
  width: number;
  transform: d3.ZoomTransform;
  selected: Selection | null;
  hovered: string | null;
  showAllWires: boolean;
  uid: string;
  onSelect: (s: Selection | null) => void;
  onHover: (id: string | null) => void;
}

interface Lane { id: string; title: string; kind: string; y: number; h: number; blocks: PlacedBlock[] }
interface PlacedBlock extends PlacedItem { y: number; ev: Event }
interface TodoMark { todo: Todo; x: number; y: number }

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Sel = d3.Selection<any, any, any, any>;

function layer(parent: Sel, tag: string, cls: string): Sel {
  return parent.selectChildren(`${tag}.${cls}`).data([0]).join(tag).attr("class", cls);
}

export function renderTimeline(
  svgEl: SVGSVGElement,
  tipEl: HTMLDivElement,
  wrapEl: HTMLDivElement,
  input: RenderInput,
): void {
  const { data, now, range, width, transform, selected, hovered, showAllWires, uid, onSelect, onHover } = input;
  const W = width;
  const plotW = Math.max(200, W - GUTTER);
  // Zoom acts in svg coordinates; shift the rescaled range back to plot coordinates.
  const base = d3.scaleTime().domain(range).range([GUTTER, GUTTER + plotW]);
  const zs = transform.rescaleX(base);
  const xz = zs.copy().range(zs.range().map(v => v - GUTTER));
  const xn = (t: number) => xz(new Date(t));

  const clipId = `tl-clip-${uid}`;
  const arrowId = `tl-arrow-${uid}`;
  const svg: Sel = d3.select(svgEl);
  const defs = layer(svg, "defs", "tl-defs");
  layer(defs, "clipPath", "tl-clip").attr("id", clipId);
  const marker = layer(defs, "marker", "tl-arrow")
    .attr("id", arrowId).attr("viewBox", "0 0 10 10").attr("refX", 9).attr("refY", 5)
    .attr("markerWidth", 6).attr("markerHeight", 6).attr("orient", "auto-start-reverse");
  layer(marker, "path", "tl-arrow-head").attr("d", "M0,0 L10,5 L0,10 z").attr("fill", "var(--wire)");
  const gBands = layer(svg, "g", "bands");
  const gPlot = layer(svg, "g", "plot")
    .attr("clip-path", `url(#${clipId})`).attr("transform", `translate(${GUTTER},${AXIS_H})`);
  const future = layer(gPlot, "rect", "future");
  const gGrid = layer(gPlot, "g", "grid");
  const gBlocks = layer(gPlot, "g", "blocks");
  const gTodos = layer(gPlot, "g", "todos");
  const gWires = layer(gPlot, "g", "wires");
  const gNow = layer(gPlot, "g", "now-g");
  const nowLine = layer(gNow, "line", "now");
  const nowLbl = layer(gNow, "text", "now-lbl").attr("y", 12).text("now");
  const gAxis = layer(svg, "g", "axis").attr("transform", `translate(${GUTTER},${AXIS_H})`);
  const gGutter = layer(svg, "g", "gutter");

  // ---- lane layout ----
  const pending = new Set(data.pending.event ?? []);
  const evById = new Map(data.events.map(e => [e.id, e]));
  const itemsByProject = new Map<string, LaneItem[]>();
  for (const e of data.events) {
    const list = itemsByProject.get(e.project_id) ?? [];
    list.push({
      id: e.id, start: Date.parse(e.t_start), end: e.t_end ? Date.parse(e.t_end) : null,
      title: e.title, kind: e.kind,
    });
    itemsByProject.set(e.project_id, list);
  }
  let y = 0;
  const lanes: Lane[] = data.projects.map(p => {
    const vis = cullVisible(itemsByProject.get(p.id) ?? [], xn, plotW);
    const lay = packLane(vis, xn, MAX_ROWS, MIN_W);
    const h = PAD + lay.rows * ROW_H + STRIP_SLOT + PAD;
    const laneY = y;
    y += h;
    const blocks = lay.blocks.map(b => ({
      ...b, y: laneY + PAD + b.row * ROW_H + (ROW_H - BLOCK_H) / 2, ev: evById.get(b.id)!,
    }));
    return { id: p.id, title: p.title, kind: p.kind, y: laneY, h, blocks };
  });
  const plotH = y;
  const totalH = AXIS_H + plotH + 8;
  svg.attr("width", W).attr("height", totalH);
  defs.select("clipPath").selectChildren("rect").data([0]).join("rect")
    .attr("width", plotW).attr("height", plotH);
  const nowX = xz(now);
  future.attr("x", nowX).attr("y", 0).attr("width", Math.max(0, plotW - nowX)).attr("height", plotH);

  // ---- bands and gutter ----
  gBands.selectAll<SVGRectElement, Lane>("rect.band").data(lanes, (d: Lane) => d.id).join("rect").attr("class", "band")
    .attr("x", 0).attr("y", (d: Lane) => AXIS_H + d.y).attr("width", W).attr("height", (d: Lane) => d.h)
    .style("opacity", (_d: Lane, i: number) => (i % 2 ? 1 : 0));
  gBands.selectAll<SVGLineElement, Lane>("line.lane-sep").data(lanes, (d: Lane) => d.id).join("line").attr("class", "lane-sep")
    .attr("x1", 0).attr("x2", W)
    .attr("y1", (d: Lane) => AXIS_H + d.y + d.h).attr("y2", (d: Lane) => AXIS_H + d.y + d.h);
  layer(gGutter, "line", "gutter-edge")
    .attr("x1", GUTTER - 0.5).attr("x2", GUTTER - 0.5).attr("y1", 0).attr("y2", totalH);
  const lab = gGutter.selectAll<SVGGElement, Lane>("g.lab").data(lanes, (d: Lane) => d.id).join((enter: Sel) => {
    const g = enter.append("g").attr("class", "lab");
    g.append("text").attr("class", "lbl-name");
    g.append("text").attr("class", "lbl-kind").attr("y", 14);
    return g;
  });
  lab.attr("transform", (d: Lane) => `translate(14,${AXIS_H + d.y + PAD + 15})`);
  lab.select(".lbl-name").text((d: Lane) => trunc(d.title, GUTTER - 28));
  lab.select(".lbl-kind").text((d: Lane) => d.kind);

  // ---- axis, grid, now ----
  const ticks = Math.max(3, Math.floor(plotW / 120));
  gAxis.call(d3.axisTop(xz).ticks(ticks).tickSizeOuter(0));
  gGrid.selectAll("line").data(xz.ticks(ticks)).join("line")
    .attr("x1", (d: Date) => xz(d)).attr("x2", (d: Date) => xz(d)).attr("y1", 0).attr("y2", plotH);
  nowLine.attr("x1", nowX).attr("x2", nowX).attr("y1", 0).attr("y2", plotH);
  nowLbl.attr("x", nowX + 5);

  // ---- tooltip ----
  const showTip = (ev: MouseEvent, title: string, sub: string) => {
    const b = document.createElement("b");
    b.textContent = title;
    tipEl.replaceChildren(b, document.createElement("br"), document.createTextNode(sub));
    tipEl.style.display = "block";
    const r = wrapEl.getBoundingClientRect();
    tipEl.style.left = `${ev.clientX - r.left + 12}px`;
    tipEl.style.top = `${ev.clientY - r.top + 12}px`;
  };
  const hideTip = () => {
    tipEl.style.display = "none";
  };

  // ---- blocks ----
  const pos = new Map<string, Box>();
  const flat: PlacedBlock[] = [];
  for (const l of lanes) for (const b of l.blocks) {
    pos.set(b.id, { x: b.px, y: b.y, w: b.w, h: BLOCK_H });
    flat.push(b);
  }
  const selEvent = selected?.type === "event" ? selected.id : null;
  const selTodo = selected?.type === "todo" ? selected.id : null;
  const focus = hovered ?? selEvent;
  const visEdges = data.edges.filter(e =>
    e.src_type === "event" && e.dst_type === "event" && pos.has(e.src_id) && pos.has(e.dst_id)
    && (showAllWires || e.src_id === focus || e.dst_id === focus));
  const linked = new Set<string>();
  for (const e of visEdges) { linked.add(e.src_id); linked.add(e.dst_id); }

  const blk = gBlocks.selectAll<SVGGElement, PlacedBlock>("g.block").data(flat, (d: PlacedBlock) => d.id).join((enter: Sel) => {
    const g = enter.append("g");
    g.append("rect").attr("class", "body").attr("rx", 4).attr("height", BLOCK_H);
    const t = g.append("text").attr("y", BLOCK_H / 2 + 4);
    t.append("tspan").attr("class", "g");
    t.append("tspan").attr("class", "t");
    return g;
  });
  blk.attr("class", (d: PlacedBlock) => `block k-${d.kind} s-${d.ev.status}`
      + (d.id === selEvent ? " sel" : "") + (d.id === hovered ? " hover" : "")
      + (linked.has(d.id) ? " linked" : ""))
    .attr("transform", (d: PlacedBlock) => `translate(${d.px},${d.y})`)
    .on("mouseenter", (_ev: MouseEvent, d: PlacedBlock) => onHover(d.id))
    .on("mousemove", (ev: MouseEvent, d: PlacedBlock) =>
      showTip(ev, d.title, `${fmtWhen(new Date(d.start), d.end ? new Date(d.end) : null)} · ${d.kind}`))
    .on("mouseleave", () => { hideTip(); onHover(null); })
    .on("click", (ev: MouseEvent, d: PlacedBlock) => { ev.stopPropagation(); onSelect({ type: "event", id: d.id }); });
  blk.select("rect.body").attr("width", (d: PlacedBlock) => d.w);
  const txt = blk.select("text")
    .attr("x", (d: PlacedBlock) => (d.inside ? 6 : d.w + 5))
    .style("display", (d: PlacedBlock) => (d.label ? null : "none"));
  txt.select("tspan.g").text((d: PlacedBlock) => glyph[d.kind] ?? "•");
  txt.select("tspan.t")
    .text((d: PlacedBlock) => " " + trunc(d.title, d.inside ? d.w - 22 : labelWidth(d.title)));
  blk.selectAll("circle.pending")
    .data((d: PlacedBlock) => (pending.has(d.id) ? [d] : []))
    .join("circle").attr("class", "pending").attr("r", 4)
    .attr("cx", (d: PlacedBlock) => d.w - 1).attr("cy", 1);

  // ---- todo due markers ----
  const laneById = new Map(lanes.map(l => [l.id, l]));
  const marks: TodoMark[] = [];
  for (const t of data.todos) {
    if (!t.due || t.state === "done" || t.state === "dropped" || !t.project_id) continue;
    const lane = laneById.get(t.project_id);
    if (!lane) continue;
    const x = xn(Date.parse(t.due));
    if (x < -400 || x > plotW + 400) continue;
    marks.push({ todo: t, x, y: lane.y + PAD + ROW_H / 2 });
  }
  gTodos.selectAll<SVGPathElement, TodoMark>("path.todo-marker").data(marks, (d: TodoMark) => d.todo.id).join("path")
    .attr("class", (d: TodoMark) => "todo-marker" + (d.todo.id === selTodo ? " sel" : ""))
    .attr("d", `M0,${-TODO_HALF} L${TODO_HALF},0 L0,${TODO_HALF} L${-TODO_HALF},0 Z`)
    .attr("transform", (d: TodoMark) => `translate(${d.x},${d.y})`)
    .on("mousemove", (ev: MouseEvent, d: TodoMark) =>
      showTip(ev, d.todo.title, `due ${fmtLong(new Date(d.todo.due!))} · todo`))
    .on("mouseleave", hideTip)
    .on("click", (ev: MouseEvent, d: TodoMark) => { ev.stopPropagation(); onSelect({ type: "todo", id: d.todo.id }); });

  // ---- wires ----
  const wire = gWires.selectAll<SVGGElement, Edge>("g.w").data(visEdges, (e: Edge) => e.id).join((enter: Sel) => {
    const g = enter.append("g").attr("class", "w");
    g.append("path").attr("class", "wire");
    g.append("text").attr("class", "wire-lbl").attr("text-anchor", "middle");
    return g;
  });
  wire.select("path.wire").attr("marker-end", `url(#${arrowId})`)
    .attr("d", (e: Edge) => wirePath(pos.get(e.src_id)!, pos.get(e.dst_id)!));
  wire.select("text.wire-lbl")
    .attr("x", (e: Edge) => (mid(pos.get(e.src_id)!).x + mid(pos.get(e.dst_id)!).x) / 2)
    .attr("y", (e: Edge) => (mid(pos.get(e.src_id)!).y + mid(pos.get(e.dst_id)!).y) / 2 - 4)
    .text((e: Edge) => e.rel);

  svg.on("click", () => onSelect(null));
}

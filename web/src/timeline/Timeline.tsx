import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import * as d3 from "d3";
import type { TimelineResponse } from "../api/types";
import { renderTimeline } from "./render";
import "./timeline.css";

export type Selection =
  | { type: "event" | "todo"; id: string }
  | { type: "bin"; projectId: string; start: number; end: number };

export interface TimelineProps {
  data: TimelineResponse;
  now: Date;
  range: [Date, Date];
  selected: Selection | null;
  onSelect: (s: Selection | null) => void;
  showAllWires?: boolean;
  onChip?: (projectId: string, kind: "running" | "stale") => void;
  onLane?: (projectId: string) => void;
}

const FALLBACK_W = 1200; // jsdom reports 0 for every layout measurement

function isHorizontalWheel(ev: WheelEvent): boolean {
  return ev.shiftKey || Math.abs(ev.deltaX) > Math.abs(ev.deltaY);
}

export function Timeline({ data, now, range, selected, onSelect, showAllWires = false, onChip, onLane }: TimelineProps) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const tipRef = useRef<HTMLDivElement>(null);
  const zoomRef = useRef<d3.ZoomBehavior<SVGSVGElement, unknown> | null>(null);
  const uid = useId().replace(/[^A-Za-z0-9_-]/g, "");
  const [width, setWidth] = useState(0);
  const [hovered, setHovered] = useState<string | null>(null);
  const [transform, setTransform] = useState<d3.ZoomTransform>(d3.zoomIdentity);

  const from = range[0].getTime();
  const to = range[1].getTime();
  const nowMs = now.getTime();
  const selType = selected?.type ?? null;
  const selId = selected && selected.type !== "bin" ? selected.id : null;
  const binProject = selected?.type === "bin" ? selected.projectId : null;
  const binStart = selected?.type === "bin" ? selected.start : null;
  const binEnd = selected?.type === "bin" ? selected.end : null;

  // Width: measured from the wrapper, kept current with a ResizeObserver when available.
  useLayoutEffect(() => {
    const el = wrapRef.current!;
    const measure = () => setWidth(el.clientWidth || FALLBACK_W);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Zoom on time only: the transform is applied through rescaleX in the render.
  useEffect(() => {
    const svgEl = svgRef.current!;
    const svg = d3.select(svgEl);
    const zoom = d3.zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.6, 120])
      .extent(() => [[0, 0], [Number(svgEl.getAttribute("width")) || FALLBACK_W, Number(svgEl.getAttribute("height")) || 1]])
      // A horizontal wheel (trackpad swipe, tilt wheel, shift+wheel) pans instead of zooming.
      .filter(ev => ev.type === "wheel" ? !isHorizontalWheel(ev) : !ev.button)
      .on("zoom", ev => setTransform(ev.transform));
    svg.call(zoom).on("dblclick.zoom", null);
    zoomRef.current = zoom;
    const onWheel = (ev: WheelEvent) => {
      if (!isHorizontalWheel(ev)) return;
      ev.preventDefault(); // keep the browser from scrolling the page or swiping back
      const unit = ev.deltaMode === 1 ? 16 : ev.deltaMode === 2 ? svgEl.clientWidth || FALLBACK_W : 1;
      const delta = (ev.deltaX || ev.deltaY) * unit;
      zoom.translateBy(svg, -delta / d3.zoomTransform(svgEl).k, 0);
    };
    svgEl.addEventListener("wheel", onWheel, { passive: false });
    return () => {
      svg.on(".zoom", null);
      svgEl.removeEventListener("wheel", onWheel);
      zoomRef.current = null;
    };
  }, []);

  // A new range is a new base scale: reset the zoom so the whole range is shown.
  const rangeKey = useRef<string | null>(null);
  useEffect(() => {
    const key = `${from}|${to}`;
    const prev = rangeKey.current;
    rangeKey.current = key;
    if (prev === null || prev === key) return;
    const zoom = zoomRef.current;
    if (zoom && svgRef.current) d3.select(svgRef.current).call(zoom.transform, d3.zoomIdentity);
  }, [from, to]);

  useEffect(() => {
    if (!width) return;
    renderTimeline(svgRef.current!, tipRef.current!, {
      data,
      now: new Date(nowMs),
      range: [new Date(from), new Date(to)],
      width,
      transform,
      selected: selType === "bin" && binProject !== null && binStart !== null && binEnd !== null
        ? { type: "bin", projectId: binProject, start: binStart, end: binEnd }
        : selType && selType !== "bin" && selId ? { type: selType, id: selId } : null,
      hovered,
      showAllWires,
      uid,
      onSelect,
      onHover: setHovered,
      onChip,
      onLane,
    });
  }, [data, nowMs, from, to, width, transform, selType, selId, binProject, binStart, binEnd, hovered, showAllWires, uid, onSelect, onChip, onLane]);

  return (
    <div className="tlwrap" ref={wrapRef}>
      <svg ref={svgRef} />
      <div className="tip" ref={tipRef} />
    </div>
  );
}

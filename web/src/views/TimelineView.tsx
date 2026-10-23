import { useCallback, useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { DetailsPanel } from "../components/DetailsPanel";
import { errorText } from "../components/errorText";
import { Timeline, type Selection } from "../timeline/Timeline";
import { ActionNotice } from "./ActionNotice";
import { useTxnActions } from "./useTxnActions";
import "./views.css";

const DAY = 86_400_000;
const PRESETS: [string, number][] = [["Week", 10], ["Month", 35], ["Quarter+", 150], ["All", 365]];

export function TimelineView() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 60_000);
    return () => clearInterval(id);
  }, []);
  const [range, setRange] = useState<[Date, Date]>(() => [
    new Date(now.getTime() - 120 * DAY), new Date(now.getTime() + 60 * DAY),
  ]);
  const [showAllWires, setShowAllWires] = useState(false);
  const [selected, setSelected] = useState<Selection | null>(null);
  const actions = useTxnActions();
  const [notice, setNotice] = useState<string | null>(null);
  const summarize = useMutation({
    mutationFn: (projectId: string) => api.summarize(projectId),
    onSuccess: run => setNotice(`Summary regeneration queued (run ${run.id})`),
    onError: err => setNotice(errorText(err)),
  });
  const { mutate: regenerate, isPending: regenerating } = summarize;
  const onChip = useCallback((projectId: string, kind: "running" | "stale") => {
    if (kind === "stale" && !regenerating) regenerate(projectId);
  }, [regenerate, regenerating]);

  const [from, to] = range;
  const q = useQuery({
    queryKey: ["timeline", from.toISOString(), to.toISOString()],
    queryFn: () => api.timeline(from, to),
    placeholderData: prev => prev,
  });

  const preset = (days: number) => {
    const span = days * DAY;
    setRange([new Date(now.getTime() - 0.7 * span), new Date(now.getTime() + 0.3 * span)]);
  };

  return (
    <section className="timeline-view">
      <div className="toolbar">
        {PRESETS.map(([label, days]) => (
          <button key={label} onClick={() => preset(days)}>{label}</button>
        ))}
        <label className="check">
          <input type="checkbox" checked={showAllWires} onChange={e => setShowAllWires(e.target.checked)} />
          Show all wires
        </label>
      </div>
      <ActionNotice conflicts={actions.conflicts} error={actions.error ?? notice}
        onDismiss={() => { actions.dismiss(); setNotice(null); }} />
      {q.isError && <p className="form-error">{errorText(q.error)}</p>}
      {q.data ? (
        <div className="tl-layout">
          <Timeline data={q.data} now={now} range={range} selected={selected}
            onSelect={setSelected} showAllWires={showAllWires} onChip={onChip} />
          <DetailsPanel data={q.data} selected={selected}
            onReview={actions.review} onRevert={actions.revert} />
        </div>
      ) : (
        !q.isError && <p className="muted">loading…</p>
      )}
    </section>
  );
}

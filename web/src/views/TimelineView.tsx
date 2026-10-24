import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { DetailsPanel } from "../components/DetailsPanel";
import type { ModelChoice } from "../api/types";
import { errorText } from "../components/errorText";
import { ModelPicker } from "../components/ModelPicker";
import { Timeline, type Selection } from "../timeline/Timeline";
import { ActionNotice } from "./ActionNotice";
import { useTxnActions } from "./useTxnActions";
import "./views.css";

const DAY = 86_400_000;
const PRESETS: [string, number][] = [["Week", 10], ["Month", 35], ["Quarter+", 150], ["All", 365]];

export function TimelineView({ onOpenProject }: { onOpenProject?: (id: string) => void } = {}) {
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
  // Projects with a regeneration in flight: a second stale click on one is ignored,
  // while a click on another lane still fires.
  const regeneratingRef = useRef<Set<string>>(new Set());
  const { mutate: regenerate } = useMutation({
    mutationFn: ({ projectId, choice }: { projectId: string; choice: ModelChoice }) => api.summarize(projectId, choice),
    onSuccess: run => setNotice(`Summary regeneration queued (run ${run.id})`),
    onError: err => setNotice(errorText(err)),
    onSettled: (_run, _err, { projectId }) => { regeneratingRef.current.delete(projectId); },
  });
  // A stale chip is not a button, so the model for its regeneration is picked in the toolbar.
  const [choice, setChoice] = useState<ModelChoice | null>(null);
  const onChip = useCallback((projectId: string, kind: "running" | "stale") => {
    if (kind !== "stale" || regeneratingRef.current.has(projectId)) return;
    if (!choice) {
      setNotice("Choose a model in the toolbar to regenerate a summary");
      return;
    }
    // Added before mutate: the mutation function starts a tick later.
    regeneratingRef.current.add(projectId);
    regenerate({ projectId, choice });
  }, [regenerate, choice]);

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
        <ModelPicker value={choice} onChange={setChoice} label="Summary model" />
      </div>
      <ActionNotice conflicts={actions.conflicts} error={actions.error ?? notice}
        onDismiss={() => { actions.dismiss(); setNotice(null); }} />
      {q.isError && <p className="form-error">{errorText(q.error)}</p>}
      {q.data ? (
        <div className="tl-layout">
          <Timeline data={q.data} now={now} range={range} selected={selected}
            onSelect={setSelected} showAllWires={showAllWires} onChip={onChip} onLane={onOpenProject} />
          <DetailsPanel data={q.data} selected={selected}
            onReview={actions.review} onRevert={actions.revert} onOpenProject={onOpenProject} />
        </div>
      ) : (
        !q.isError && <p className="muted">loading…</p>
      )}
    </section>
  );
}

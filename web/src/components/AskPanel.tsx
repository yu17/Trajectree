import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ModelChoice, Project } from "../api/types";
import { errorText } from "./errorText";
import { ModelPicker } from "./ModelPicker";
import { Proposals } from "./Proposals";
import { useRun } from "./useRun";

const DAY = 86_400_000;
const isoDay = (ms: number) => new Date(ms).toISOString().slice(0, 10);
const dayStart = (day: string) => new Date(`${day}T00:00:00Z`);

export function AskPanel({ projects }: { projects: Project[] }) {
  const [unchecked, setUnchecked] = useState<Set<string>>(new Set());
  const [from, setFrom] = useState(() => isoDay(Date.now() - 90 * DAY));
  const [to, setTo] = useState(() => isoDay(Date.now() + 30 * DAY));
  const [question, setQuestion] = useState("");
  const [runId, setRunId] = useState<string | null>(null);
  const [choice, setChoice] = useState<ModelChoice | null>(null);

  const active = projects.filter(p => p.status === "active");
  const slugs = active.filter(p => !unchecked.has(p.id)).map(p => p.slug);
  const startMs = dayStart(from).getTime();
  const endMs = dayStart(to).getTime() + DAY; // To is inclusive: end at the start of the next day
  const validDates = Number.isFinite(startMs) && Number.isFinite(endMs) && endMs > startMs;
  const pack = useQuery({
    queryKey: ["context", slugs, from, to],
    queryFn: () => api.context(slugs, new Date(startMs), new Date(endMs)),
    enabled: slugs.length > 0 && validDates,
  });
  const ask = useMutation({
    mutationFn: (c: ModelChoice) =>
      api.ask({ question, projects: slugs, start: new Date(startMs).toISOString(), end: new Date(endMs).toISOString(), ...c }),
    onSuccess: r => setRunId(r.id),
  });
  const detail = useRun(runId);
  const run = detail.data?.run;
  const inFlight = ask.isPending || (runId !== null && !detail.isError && (!run || run.status === "queued" || run.status === "running"));

  const toggle = (id: string) =>
    setUnchecked(cur => {
      const next = new Set(cur);
      if (!next.delete(id)) next.add(id);
      return next;
    });

  return (
    <div className="panel-form">
      <h2>Ask</h2>
      <div>
        {active.map(p => (
          <label key={p.id} className="check">
            <input type="checkbox" checked={!unchecked.has(p.id)} onChange={() => toggle(p.id)} /> {p.title}
          </label>
        ))}
      </div>
      <label htmlFor="ask-from">From</label>
      <input id="ask-from" type="date" value={from} onChange={e => setFrom(e.target.value)} />
      <label htmlFor="ask-to">To</label>
      <input id="ask-to" type="date" value={to} onChange={e => setTo(e.target.value)} />
      <label htmlFor="ask-q">Question</label>
      <textarea id="ask-q" rows={3} value={question} onChange={e => setQuestion(e.target.value)} />
      {pack.data && <p className="muted">pack ≈ {pack.data.tokens_est} tokens</p>}
      {pack.isError && <p className="form-error">{errorText(pack.error)}</p>}
      <ModelPicker value={choice} onChange={setChoice} />
      <button disabled={!choice || !question.trim() || inFlight || slugs.length === 0 || !validDates} onClick={() => choice && ask.mutate(choice)}>Ask</button>
      {ask.isError && <p className="form-error">{errorText(ask.error)}</p>}
      {detail.isError && <p className="form-error">{errorText(detail.error)}</p>}
      {inFlight && <p className="muted">running…</p>}
      {run && !inFlight && (
        <>
          {run.result_md && <div className="answer"><pre>{run.result_md}</pre></div>}
          {run.error && <p className="form-error">{run.error}</p>}
          <Proposals run={run} />
        </>
      )}
    </div>
  );
}

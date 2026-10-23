import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Project } from "../api/types";
import { errorText } from "./errorText";
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

  const slugs = projects.filter(p => !unchecked.has(p.id)).map(p => p.slug);
  const validDates = !Number.isNaN(dayStart(from).getTime()) && !Number.isNaN(dayStart(to).getTime());
  const pack = useQuery({
    queryKey: ["context", slugs, from, to],
    queryFn: () => api.context(slugs, dayStart(from), dayStart(to)),
    enabled: slugs.length > 0 && validDates,
  });
  const ask = useMutation({
    mutationFn: () =>
      api.ask({ question, projects: slugs, start: dayStart(from).toISOString(), end: dayStart(to).toISOString() }),
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
        {projects.map(p => (
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
      <button disabled={!question.trim() || inFlight} onClick={() => ask.mutate()}>Ask</button>
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

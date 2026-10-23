import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Resource, Signal } from "../api/types";
import { errorText } from "../components/errorText";
import { useRun } from "../components/useRun";
import { glyph } from "../timeline/render";
import { ActionNotice } from "./ActionNotice";
import "./views.css";

const DAY = 86_400_000;
const MAX_EVENTS = 30;
const OPEN_TODO = ["open", "doing"];

// The latest `job_state` per slurm job id; a job's resource locator is `<host>:<job id>`.
function latestJobStates(signals: Signal[]): Map<string, string> {
  const latest = new Map<string, Signal>();
  for (const s of signals) {
    if (s.kind !== "job_state" || typeof s.payload.job_id !== "string") continue;
    const prev = latest.get(s.payload.job_id);
    if (!prev || s.updated_at > prev.updated_at) latest.set(s.payload.job_id, s);
  }
  return new Map([...latest].map(([id, s]) => [id, String(s.payload.state ?? "")]));
}

const jobId = (r: Resource) => r.locator.slice(r.locator.indexOf(":") + 1);

export function ProjectView({ projectId, onClose }: { projectId: string; onClose: () => void }) {
  const qc = useQueryClient();
  const [[from, to]] = useState(() => {
    const now = Date.now();
    return [new Date(now - 365 * DAY), new Date(now + 60 * DAY)] as const;
  });
  const projects = useQuery({ queryKey: ["projects"], queryFn: api.projects });
  const timeline = useQuery({
    queryKey: ["timeline", "project", projectId, from.toISOString(), to.toISOString()],
    queryFn: () => api.timeline(from, to, [projectId]),
  });
  const resources = useQuery({ queryKey: ["resources", projectId], queryFn: () => api.resources(projectId) });
  const signals = useQuery({
    queryKey: ["signals", projectId, "project", from.toISOString(), to.toISOString()],
    queryFn: () => api.signals([projectId], from, to),
  });
  const routines = useQuery({ queryKey: ["routines"], queryFn: api.routines });

  const [notice, setNotice] = useState<string | null>(null);
  const summarize = useMutation({
    mutationFn: () => api.summarize(projectId),
    onSuccess: run => setNotice(`Summary regeneration queued (run ${run.id})`),
    onError: err => setNotice(errorText(err)),
  });
  // Refresh the summary and the stale flag once the queued run settles.
  const run = useRun(summarize.data?.id ?? null);
  const runStatus = run.data?.run.status;
  useEffect(() => {
    if (runStatus === undefined || runStatus === "queued" || runStatus === "running") return;
    void qc.invalidateQueries({ queryKey: ["projects"] });
    void qc.invalidateQueries({ queryKey: ["timeline"] });
  }, [qc, runStatus]);

  const project = projects.data?.find(p => p.id === projectId);
  const tl = timeline.data;
  const events = (tl?.events ?? [])
    .filter(e => e.project_id === projectId)
    .sort((a, b) => Date.parse(b.t_start) - Date.parse(a.t_start))
    .slice(0, MAX_EVENTS);
  const todos = (tl?.todos ?? []).filter(t => t.project_id === projectId && OPEN_TODO.includes(t.state));
  const stale = tl?.stale.includes(projectId) ?? false;
  const live = tl?.live.some(s => s.project_id === projectId) ?? false;
  const jobStates = latestJobStates(signals.data ?? []);
  const touching = (routines.data ?? []).filter(r =>
    r.scope.projects === null || (project !== undefined && r.scope.projects.includes(project.slug)));

  return (
    <section className="project">
      <div className="project-head">
        <div>
          <h1>{project?.title ?? (projects.isPending ? "loading…" : "Unknown project")}</h1>
          {project && <p className="muted">{`${project.kind} · ${project.status}`}</p>}
        </div>
        <button onClick={onClose}>Close</button>
      </div>
      {projects.isError && <p className="form-error">{errorText(projects.error)}</p>}
      <ActionNotice conflicts={null} error={notice} onDismiss={() => setNotice(null)} />

      <h2>Summary</h2>
      <div className="row-actions">
        {stale && <span className="badge stale">stale</span>}
        <button disabled={summarize.isPending} onClick={() => summarize.mutate()}>Regenerate summary</button>
      </div>
      {project && (project.summary_md
        ? <pre className="answer">{project.summary_md}</pre>
        : <p className="muted">No summary yet</p>)}

      <h2>Events</h2>
      {timeline.isError && <p className="form-error">{errorText(timeline.error)}</p>}
      {timeline.isSuccess && events.length === 0 && <p className="muted">No events</p>}
      {events.length > 0 && (
        <ul className="project-list" aria-label="Events">
          {events.map(e => (
            <li key={e.id}>
              <span className="muted">{new Date(e.t_start).toLocaleDateString()}</span>{" "}
              {glyph[e.kind] ?? "•"} {e.title} <span className="badge">{e.status}</span>
            </li>
          ))}
        </ul>
      )}

      <h2>Todos</h2>
      {timeline.isSuccess && todos.length === 0 && <p className="muted">No open todos</p>}
      {todos.length > 0 && (
        <ul className="project-list" aria-label="Todos">
          {todos.map(t => (
            <li key={t.id}>
              <span>{t.title}</span>{" "}
              <span className="muted">{t.due ? `due ${new Date(t.due).toLocaleDateString()}` : "no due"}</span>
            </li>
          ))}
        </ul>
      )}

      <h2>Resources</h2>
      {resources.isError && <p className="form-error">{errorText(resources.error)}</p>}
      {resources.isSuccess && resources.data.length === 0 && <p className="muted">No resources</p>}
      {resources.data && resources.data.length > 0 && (
        <ul className="project-list" aria-label="Resources">
          {resources.data.map(r => {
            const job = r.kind === "slurm_job" ? jobStates.get(jobId(r)) : undefined;
            return (
              <li key={r.id}>
                <span className="badge">{r.kind}</span> <code>{r.locator}</code>{" "}
                {r.title && <span className="muted">{r.title}</span>}{" "}
                {r.kind !== "slurm_job" && live && <span className="badge run-running">running</span>}
                {job && <span className="badge">{job}</span>}
              </li>
            );
          })}
        </ul>
      )}

      <h2>Routines</h2>
      {routines.isError && <p className="form-error">{errorText(routines.error)}</p>}
      {routines.isSuccess && touching.length === 0 && <p className="muted">No routines</p>}
      {touching.length > 0 && (
        <ul className="project-list" aria-label="Routines">
          {touching.map(r => (
            <li key={r.id}>
              {`${r.name} · ${r.trigger} (${r.tz})`}{" "}
              <span className="muted">{r.enabled ? "enabled" : "disabled"}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

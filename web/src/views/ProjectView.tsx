import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ModelChoice, Project, ProjectKind, ProjectStatus, Resource, Signal } from "../api/types";
import { errorText } from "../components/errorText";
import { KINDS } from "../components/ProjectForm";
import { ModelPicker } from "../components/ModelPicker";
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

const STATUSES: ProjectStatus[] = ["active", "paused", "done", "archived"];

// Title, kind and status; only the changed fields are sent.
function ProjectEdit({ project, onDone }: { project: Project; onDone: () => void }) {
  const qc = useQueryClient();
  const [title, setTitle] = useState(project.title);
  const [kind, setKind] = useState<ProjectKind>(project.kind);
  const [status, setStatus] = useState<ProjectStatus>(project.status);
  const save = useMutation({
    mutationFn: () => api.updateProject(project.id, {
      ...(title !== project.title ? { title } : {}),
      ...(kind !== project.kind ? { kind } : {}),
      ...(status !== project.status ? { status } : {}),
    }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["projects"] });
      void qc.invalidateQueries({ queryKey: ["timeline"] });
      void qc.invalidateQueries({ queryKey: ["txns"] });
      onDone();
    },
  });
  const unchanged = title === project.title && kind === project.kind && status === project.status;
  return (
    <form className="panel-form project-edit" onSubmit={e => { e.preventDefault(); save.mutate(); }}>
      <label htmlFor="pe-title">Title</label>
      <input id="pe-title" required value={title} onChange={e => setTitle(e.target.value)} />
      <label htmlFor="pe-kind">Kind</label>
      <select id="pe-kind" value={kind} onChange={e => setKind(e.target.value as ProjectKind)}>
        {KINDS.map(k => <option key={k} value={k}>{k}</option>)}
      </select>
      <label htmlFor="pe-status">Status</label>
      <select id="pe-status" value={status} onChange={e => setStatus(e.target.value as ProjectStatus)}>
        {STATUSES.map(s => <option key={s} value={s}>{s}</option>)}
      </select>
      {save.isError && <p role="alert" className="form-error">{errorText(save.error)}</p>}
      <div className="row-actions">
        <button type="submit" disabled={unchanged || title.trim() === "" || save.isPending}>Save</button>
        <button type="button" onClick={onDone}>Cancel</button>
      </div>
    </form>
  );
}

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
  // Only the job states feed the resource hints; ask for them alone so other kinds cannot crowd them out.
  const signals = useQuery({
    queryKey: ["signals", projectId, "project", "job_state", from.toISOString(), to.toISOString()],
    queryFn: () => api.signals([projectId], from, to, { kinds: ["job_state"], limit: 500 }),
  });
  const routines = useQuery({ queryKey: ["routines"], queryFn: api.routines });

  const [editing, setEditing] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [choice, setChoice] = useState<ModelChoice | null>(null);
  const summarize = useMutation({
    mutationFn: (c: ModelChoice) => api.summarize(projectId, c),
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

  const [inspectChoice, setInspectChoice] = useState<ModelChoice | null>(null);
  const [since, setSince] = useState("");
  const inspect = useMutation({
    mutationFn: (v: { c: ModelChoice; since?: string }) =>
      v.since ? api.inspect(projectId, v.c, v.since) : api.inspect(projectId, v.c),
    onSuccess: run => setNotice(`Inspection queued (run ${run.id})`),
    onError: err => setNotice(errorText(err)),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["inspections"] });
      void qc.invalidateQueries({ queryKey: ["runs"] });
      void qc.invalidateQueries({ queryKey: ["timeline"] });
    },
  });

  const project = projects.data?.find(p => p.id === projectId);
  const tl = timeline.data;
  const events = (tl?.events ?? [])
    .filter(e => e.project_id === projectId)
    .sort((a, b) => Date.parse(b.t_start) - Date.parse(a.t_start))
    .slice(0, MAX_EVENTS);
  const todos = (tl?.todos ?? []).filter(t => t.project_id === projectId && OPEN_TODO.includes(t.state));
  const toInspect = tl?.due?.[projectId] ?? 0;
  const live = tl?.live.some(s => s.project_id === projectId) ?? false;
  const jobStates = latestJobStates(signals.data ?? []);
  // A routine's scope may name a project by slug or by id.
  const touching = (routines.data ?? []).filter(r =>
    r.scope.projects === null ||
    (project !== undefined && r.scope.projects.some(ref => ref === project.slug || ref === project.id)));

  return (
    <section className="project">
      <div className="project-head">
        <div>
          <h1>{project?.title ?? (projects.isPending ? "loading…" : "Unknown project")}</h1>
          {project && <p className="muted">{`${project.kind} · ${project.status}`}</p>}
        </div>
        <div className="row-actions">
          {project && <button aria-expanded={editing} onClick={() => setEditing(e => !e)}>Edit</button>}
          <button onClick={onClose}>Close</button>
        </div>
      </div>
      {project && editing && <ProjectEdit project={project} onDone={() => setEditing(false)} />}
      {projects.isError && <p className="form-error">{errorText(projects.error)}</p>}
      <ActionNotice conflicts={null} error={notice} onDismiss={() => setNotice(null)} />

      <h2>Summary</h2>
      <div className="row-actions">
        {toInspect > 0 && <span className="badge stale">{`${toInspect} stretches to inspect`}</span>}
        <ModelPicker value={choice} onChange={setChoice} />
        <button disabled={!choice || summarize.isPending} onClick={() => choice && summarize.mutate(choice)}>Regenerate summary</button>
      </div>
      <div className="row-actions">
        <ModelPicker value={inspectChoice} onChange={setInspectChoice} label="Inspection model" />
        <button disabled={!inspectChoice || inspect.isPending}
          onClick={() => inspectChoice && inspect.mutate({ c: inspectChoice })}>Inspect</button>
        <label>
          Re-inspect since{" "}
          <input type="date" value={since} onChange={e => setSince(e.target.value)} />
        </label>
        <button disabled={!inspectChoice || since === "" || inspect.isPending}
          onClick={() => inspectChoice && inspect.mutate({ c: inspectChoice, since })}>Re-inspect</button>
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

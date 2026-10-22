import { useState, type FormEvent } from "react";
import { api } from "../api/client";
import type { EventStatus, Project } from "../api/types";
import { errorText } from "./errorText";

const KINDS = ["decision", "meeting", "milestone", "idea", "note", "blocker"];
const STATUSES: EventStatus[] = ["happened", "planned", "suggested"];

function localNow(): string {
  const d = new Date();
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
  return d.toISOString().slice(0, 16);
}

// datetime-local is local wall time; the API wants ISO UTC.
// Returns null for an empty or invalid value.
function toUtc(v: string): string | null {
  const d = new Date(v);
  return v && !Number.isNaN(d.getTime()) ? d.toISOString() : null;
}

const BAD_TIME = "Enter a valid date and time";

export function QuickCapture({ projects, onCreated }: { projects: Project[]; onCreated: () => void }) {
  const [projectId, setProjectId] = useState("");
  const [entry, setEntry] = useState<"event" | "todo">("event");
  const [kind, setKind] = useState("note");
  const [title, setTitle] = useState("");
  const [when, setWhen] = useState(localNow);
  const [end, setEnd] = useState("");
  const [status, setStatus] = useState<EventStatus>("happened");
  const [body, setBody] = useState("");
  const [intent, setIntent] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const isEvent = entry === "event";
  // Until the user picks one, an event defaults to the first project; a todo may have none.
  // A todo has no due date unless the user enters one; an event defaults to now.
  const changeEntry = (next: "event" | "todo") => {
    setEntry(next);
    setWhen(next === "todo" ? "" : localNow());
    setError(null);
  };
  const effectiveProject = projectId || (isEvent ? projects[0]?.id ?? "" : "");

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    const whenUtc = when ? toUtc(when) : null;
    const endUtc = end ? toUtc(end) : null;
    if ((isEvent && !whenUtc) || (when && !whenUtc) || (isEvent && end && !endUtc)) {
      setError(BAD_TIME);
      return;
    }
    setBusy(true);
    try {
      if (isEvent) {
        await api.createEvent({
          project_id: effectiveProject,
          kind,
          title,
          t_start: whenUtc!,
          ...(endUtc ? { t_end: endUtc } : {}),
          status,
          ...(body ? { body_md: body } : {}),
          ...(intent ? { intent } : {}),
        });
      } else {
        await api.createTodo({
          ...(effectiveProject ? { project_id: effectiveProject } : {}),
          title,
          ...(whenUtc ? { due: whenUtc } : {}),
          ...(body ? { body_md: body } : {}),
          ...(intent ? { intent } : {}),
        });
      }
      onCreated();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="panel-form" onSubmit={submit}>
      <h2>Quick capture</h2>
      <label htmlFor="qc-project">Project</label>
      <select id="qc-project" value={effectiveProject} onChange={e => setProjectId(e.target.value)}>
        {!isEvent && <option value="">(none)</option>}
        {projects.map(p => <option key={p.id} value={p.id}>{p.slug}</option>)}
      </select>
      <label htmlFor="qc-entry">Entry type</label>
      <select id="qc-entry" value={entry} onChange={e => changeEntry(e.target.value as "event" | "todo")}>
        <option value="event">event</option>
        <option value="todo">todo</option>
      </select>
      {isEvent && (
        <>
          <label htmlFor="qc-kind">Kind</label>
          <input id="qc-kind" list="kinds" required value={kind} onChange={e => setKind(e.target.value)} />
          <datalist id="kinds">{KINDS.map(k => <option key={k} value={k} />)}</datalist>
        </>
      )}
      <label htmlFor="qc-title">Title</label>
      <input id="qc-title" required value={title} onChange={e => setTitle(e.target.value)} />
      <label htmlFor="qc-when">When</label>
      <input id="qc-when" type="datetime-local" required={isEvent} value={when} onChange={e => setWhen(e.target.value)} />
      {isEvent && (
        <>
          <label htmlFor="qc-end">End</label>
          <input id="qc-end" type="datetime-local" value={end} onChange={e => setEnd(e.target.value)} />
          <label htmlFor="qc-status">Status</label>
          <select id="qc-status" value={status} onChange={e => setStatus(e.target.value as EventStatus)}>
            {STATUSES.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
        </>
      )}
      <label htmlFor="qc-body">Body</label>
      <textarea id="qc-body" rows={3} value={body} onChange={e => setBody(e.target.value)} />
      <label htmlFor="qc-intent">Intent</label>
      <input id="qc-intent" value={intent} onChange={e => setIntent(e.target.value)} />
      {error && <p role="alert" className="form-error">{error}</p>}
      <button type="submit" disabled={busy}>Save</button>
    </form>
  );
}

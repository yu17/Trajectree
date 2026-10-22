import { useState, type FormEvent } from "react";
import { api } from "../api/client";
import type { ProjectKind } from "../api/types";
import { errorText } from "./errorText";

const KINDS: ProjectKind[] = ["research", "tooling", "routine", "admin", "inbox"];

export function ProjectForm({ onCreated }: { onCreated: () => void }) {
  const [slug, setSlug] = useState("");
  const [title, setTitle] = useState("");
  const [kind, setKind] = useState<ProjectKind>("research");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.createProject({ slug, title, kind });
      onCreated();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="panel-form" onSubmit={submit}>
      <h2>New project</h2>
      <label htmlFor="pf-slug">Slug</label>
      <input id="pf-slug" required value={slug} onChange={e => setSlug(e.target.value)} />
      <label htmlFor="pf-title">Title</label>
      <input id="pf-title" required value={title} onChange={e => setTitle(e.target.value)} />
      <label htmlFor="pf-kind">Kind</label>
      <select id="pf-kind" value={kind} onChange={e => setKind(e.target.value as ProjectKind)}>
        {KINDS.map(k => <option key={k} value={k}>{k}</option>)}
      </select>
      {error && <p role="alert" className="form-error">{error}</p>}
      <button type="submit" disabled={busy}>Create</button>
    </form>
  );
}

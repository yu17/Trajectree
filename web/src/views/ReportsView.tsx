import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Project, ReportFormat, ReportKind, ReportOut } from "../api/types";
import { errorText } from "../components/errorText";
import { MarkdownEditor } from "../components/MarkdownEditor";
import { MarpPreview } from "../components/MarpPreview";
import { useRun } from "../components/useRun";

const DAY = 86_400_000;
const isoDay = (ms: number) => new Date(ms).toISOString().slice(0, 10);
const dayStart = (day: string) => new Date(`${day}T00:00:00Z`);
const EXPORTS: [ReportFormat, string][] = [["html", "Export HTML"], ["pdf", "Export PDF"], ["pptx", "Export PPTX"]];

const newest = (rs: ReportOut[]) => rs.reduce<ReportOut | null>((a, r) => (a && a.updated_at >= r.updated_at ? a : r), null);

function KindSelect({ id, value, onChange }: { id: string; value: ReportKind; onChange: (k: ReportKind) => void }) {
  return (
    <>
      <label htmlFor={id}>Kind</label>
      <select id={id} value={value} onChange={e => onChange(e.target.value as ReportKind)}>
        <option value="deck">deck</option>
        <option value="memo">memo</option>
      </select>
    </>
  );
}

function NewReportForm({ onCreated }: { onCreated: (id: string) => void }) {
  const qc = useQueryClient();
  const [title, setTitle] = useState("");
  const [kind, setKind] = useState<ReportKind>("deck");
  const create = useMutation({
    mutationFn: () => api.createReport({ title: title.trim(), kind }),
    onSuccess: async r => {
      await qc.invalidateQueries({ queryKey: ["reports"] });
      void qc.invalidateQueries({ queryKey: ["txns"] });
      onCreated(r.id);
    },
  });
  return (
    <form
      className="panel-form"
      onSubmit={e => {
        e.preventDefault();
        create.mutate();
      }}
    >
      <label htmlFor="report-title">Title</label>
      <input id="report-title" value={title} onChange={e => setTitle(e.target.value)} />
      <KindSelect id="report-kind" value={kind} onChange={setKind} />
      <button type="submit" disabled={!title.trim() || create.isPending}>Create</button>
      {create.isError && <p className="form-error">{errorText(create.error)}</p>}
    </form>
  );
}

function DraftForm({ projects, busy, onStarted }: { projects: Project[]; busy: boolean; onStarted: (runId: string) => void }) {
  const [title, setTitle] = useState("");
  const [kind, setKind] = useState<ReportKind>("deck");
  const [slugs, setSlugs] = useState<string[]>([]);
  const [from, setFrom] = useState(() => isoDay(Date.now() - 30 * DAY));
  const [to, setTo] = useState(() => isoDay(Date.now()));

  const active = projects.filter(p => p.status === "active");
  const startMs = dayStart(from).getTime();
  const endMs = dayStart(to).getTime() + DAY; // To is inclusive: end at the start of the next day
  const validDates = Number.isFinite(startMs) && Number.isFinite(endMs) && endMs > startMs;
  const draft = useMutation({
    mutationFn: () =>
      api.draftReport({ kind, title: title.trim(), projects: slugs, start: new Date(startMs).toISOString(), end: new Date(endMs).toISOString() }),
    onSuccess: r => onStarted(r.id),
  });

  return (
    <form
      className="panel-form"
      onSubmit={e => {
        e.preventDefault();
        draft.mutate();
      }}
    >
      <label htmlFor="draft-title">Title</label>
      <input id="draft-title" value={title} onChange={e => setTitle(e.target.value)} />
      <KindSelect id="draft-kind" value={kind} onChange={setKind} />
      <label htmlFor="draft-projects">Projects</label>
      <select id="draft-projects" multiple value={slugs} onChange={e => setSlugs(Array.from(e.target.selectedOptions, o => o.value))}>
        {active.map(p => (
          <option key={p.id} value={p.slug}>
            {p.title}
          </option>
        ))}
      </select>
      <label htmlFor="draft-from">From</label>
      <input id="draft-from" type="date" value={from} onChange={e => setFrom(e.target.value)} />
      <label htmlFor="draft-to">To</label>
      <input id="draft-to" type="date" value={to} onChange={e => setTo(e.target.value)} />
      <button type="submit" disabled={!title.trim() || slugs.length === 0 || !validDates || busy || draft.isPending}>
        Draft
      </button>
      {draft.isError && <p className="form-error">{errorText(draft.error)}</p>}
    </form>
  );
}


function ReportEditor({ report, onDirty }: { report: ReportOut; onDirty: (dirty: boolean) => void }) {
  const qc = useQueryClient();
  const [source, setSource] = useState(report.source_md);
  // `base` is the saved source the buffer started from; the buffer is dirty when it differs.
  const [base, setBase] = useState(report.source_md);
  const [stale, setStale] = useState(false);
  const dirty = source !== base;
  useEffect(() => onDirty(dirty), [dirty, onDirty]);
  useEffect(() => () => onDirty(false), [onDirty]);
  // A newer server copy (an agent rewrite, another tab) replaces a clean buffer; dirty edits are kept.
  useEffect(() => {
    if (report.source_md === base) return;
    if (dirty) {
      setStale(true);
    } else {
      setSource(report.source_md);
      setBase(report.source_md);
    }
  }, [report.source_md]);
  const reload = () => {
    setSource(report.source_md);
    setBase(report.source_md);
    setStale(false);
  };
  const save = useMutation({
    mutationFn: (sent: string) => api.updateReport(report.id, { source_md: sent }),
    // Stay pending until the list has the saved copy, so a second click cannot file a second txn.
    onSuccess: async (_saved, sent) => {
      setBase(sent);
      setStale(false);
      await qc.invalidateQueries({ queryKey: ["reports"] });
      void qc.invalidateQueries({ queryKey: ["txns"] });
    },
  });
  return (
    <>
      <section className="report-editor">
        <MarkdownEditor value={source} onChange={setSource} />
        {stale && (
          <p className="muted">
            Server copy changed; save to overwrite or reload{" "}
            <button className="linklike" onClick={reload}>Reload</button>
          </p>
        )}
        <div className="row-actions">
          <button disabled={!dirty || save.isPending} onClick={() => save.mutate(source)}>
            Save report
          </button>
          {EXPORTS.map(([fmt, name]) => (
            <a key={fmt} href={api.reportExportUrl(report.id, fmt)} target="_blank" rel="noopener">
              {name}
            </a>
          ))}
        </div>
        {save.isError && <p className="form-error">{errorText(save.error)}</p>}
      </section>
      <section className="preview">
        <h2>Preview</h2>
        <MarpPreview source={source} />
      </section>
    </>
  );
}

export function ReportsView() {
  const reports = useQuery({ queryKey: ["reports"], queryFn: api.reports });
  const projects = useQuery({ queryKey: ["projects"], queryFn: api.projects });
  const [form, setForm] = useState<"new" | "draft" | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draftRunId, setDraftRunId] = useState<string | null>(null);
  const draftRun = useRun(draftRunId);
  const runStatus = draftRun.data?.run.status ?? "queued";
  const drafting = draftRunId !== null && !draftRun.isError && (runStatus === "queued" || runStatus === "running");

  // Unsaved edits in the editor: switching reports (or opening a form) asks before discarding them.
  const dirty = useRef(false);
  const onDirty = useCallback((d: boolean) => {
    dirty.current = d;
  }, []);
  const mayDiscard = () => !dirty.current || window.confirm("Discard unsaved changes?");

  // When the agent draft finishes ok, refetch the list and select the draft: the report the run
  // saved (`props.report_id`), else the newest one.
  const qc = useQueryClient();
  const handled = useRef<string | null>(null);
  const draftReportId = draftRun.data?.run.props?.report_id;
  useEffect(() => {
    if (draftRunId === null || runStatus !== "ok" || handled.current === draftRunId) return;
    handled.current = draftRunId;
    void qc.invalidateQueries({ queryKey: ["txns"] });
    void qc.invalidateQueries({ queryKey: ["reports"] }).then(() => {
      const list = qc.getQueryData<ReportOut[]>(["reports"]) ?? [];
      const target = list.find(r => r.id === draftReportId) ?? newest(list);
      if (target && mayDiscard()) setSelectedId(target.id);
    });
  }, [draftRunId, runStatus, draftReportId, qc]);

  const selected = reports.data?.find(r => r.id === selectedId) ?? null;
  const toggle = (f: "new" | "draft") => {
    if (mayDiscard()) setForm(cur => (cur === f ? null : f));
  };
  const select = (id: string) => {
    if (id !== selectedId && mayDiscard()) setSelectedId(id);
  };
  const created = (id: string) => {
    setForm(null);
    if (mayDiscard()) setSelectedId(id);
  };

  return (
    <div className="reports">
      <aside className="report-list">
        <h2>Reports</h2>
        <div className="row-actions">
          <button aria-expanded={form === "new"} onClick={() => toggle("new")}>New report</button>
          <button aria-expanded={form === "draft"} onClick={() => toggle("draft")}>Draft with agent</button>
        </div>
        {form === "new" && <NewReportForm onCreated={created} />}
        {form === "draft" && <DraftForm projects={projects.data ?? []} busy={drafting} onStarted={setDraftRunId} />}
        {draftRunId && draftRun.isError && <p className="form-error">{errorText(draftRun.error)}</p>}
        {draftRunId && !draftRun.isError && (
          <p>
            agent draft <span className={`badge run-${runStatus}`}>{runStatus}</span>
            {draftRun.data?.run.error && <span className="form-error"> {draftRun.data.run.error}</span>}
          </p>
        )}
        {reports.isError && <p className="form-error">{errorText(reports.error)}</p>}
        {reports.data && reports.data.length === 0 && <p className="muted">No reports yet</p>}
        {reports.data && reports.data.length > 0 && (
          <ul className="ledger-list">
            {reports.data.map(r => (
              <li key={r.id} className="ledger-row">
                <button className="linklike" aria-current={r.id === selectedId || undefined} onClick={() => select(r.id)}>
                  {r.title} · {r.kind} · {new Date(r.updated_at).toLocaleString()}
                </button>
                {r.pending_txn && <span className="badge review-pending">pending</span>}
              </li>
            ))}
          </ul>
        )}
      </aside>
      {selected ? <ReportEditor key={selected.id} report={selected} onDirty={onDirty} /> :<p className="muted">Select or create a report</p>}
    </div>
  );
}

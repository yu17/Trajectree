import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Caps, Run, RunnerStatus, UsageDay, UsageSlice } from "../api/types";
import { Proposals } from "../components/Proposals";
import { useRun } from "../components/useRun";

const usd = (n: number) => `$${n.toFixed(2)}`;

function statusLine(s: RunnerStatus): string {
  if (!s.claude.available) return "claude: not installed";
  if (!s.claude.authenticated) return "claude: not logged in (run trajectree login)";
  return `claude ${s.claude.version ?? ""} · logged in`.replace("  ", " ");
}

const W = 600;
const H = 120;

function UsageChart({ days, cap }: { days: UsageDay[]; cap: number }) {
  const max = Math.max(cap, ...days.map(d => d.est_usd), 0.01);
  const slot = W / Math.max(days.length, 1);
  const capY = H - (cap / max) * H;
  return (
    <svg className="usage-chart" viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label="Daily spend, last 30 days">
      {days.map((d, i) => {
        const h = d.est_usd > 0 ? Math.max((d.est_usd / max) * H, 1) : 0;
        return (
          <rect key={d.day} className="bar" x={i * slot + 1} width={Math.max(slot - 2, 1)} y={H - h} height={h} fill="var(--strip-3)">
            <title>{`${d.day}: ${usd(d.est_usd)} (${d.runs} runs)`}</title>
          </rect>
        );
      })}
      <line className="cap" x1={0} x2={W} y1={capY} y2={capY} stroke="var(--now)" strokeWidth={1.5} strokeDasharray="4 3">
        <title>{`daily cap ${usd(cap)}`}</title>
      </line>
    </svg>
  );
}

function SliceTable({ title, rows }: { title: string; rows: UsageSlice[] }) {
  return (
    <table>
      <caption className="muted">{title}</caption>
      <tbody>
        {rows.map(r => (
          <tr key={r.key}>
            <td>{r.key}</td>
            <td>{usd(r.est_usd)}</td>
            <td>{r.runs} runs</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function CapsForm({ caps }: { caps: Caps }) {
  const qc = useQueryClient();
  const [perRun, setPerRun] = useState(String(caps.usd_per_run));
  const [perDay, setPerDay] = useState(String(caps.usd_per_day));
  const [timeout, setTimeoutS] = useState(String(caps.timeout_s));
  useEffect(() => {
    setPerRun(String(caps.usd_per_run));
    setPerDay(String(caps.usd_per_day));
    setTimeoutS(String(caps.timeout_s));
  }, [caps]);
  const [invalid, setInvalid] = useState(false);
  const save = useMutation({
    mutationFn: (c: Caps) => api.setCaps(c),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["caps"] });
      void qc.invalidateQueries({ queryKey: ["usage"] });
    },
  });
  return (
    <form
      className="panel-form"
      onSubmit={e => {
        e.preventDefault();
        const next = { usd_per_run: Number(perRun), usd_per_day: Number(perDay), timeout_s: Number(timeout) };
        const ok = Object.values(next).every(v => Number.isFinite(v) && v > 0);
        setInvalid(!ok);
        if (ok) save.mutate({ ...next, paused: caps.paused });
      }}
    >
      <label htmlFor="cap-run">USD per run</label>
      <input id="cap-run" type="number" min="0.01" step="0.01" value={perRun} onChange={e => setPerRun(e.target.value)} />
      <label htmlFor="cap-day">USD per day</label>
      <input id="cap-day" type="number" min="0.01" step="0.01" value={perDay} onChange={e => setPerDay(e.target.value)} />
      <label htmlFor="cap-timeout">Timeout (s)</label>
      <input id="cap-timeout" type="number" min="1" step="1" value={timeout} onChange={e => setTimeoutS(e.target.value)} />
      <div className="row-actions">
        <button type="submit" disabled={save.isPending}>Save caps</button>
        <button type="button" disabled={save.isPending} onClick={() => save.mutate({ ...caps, paused: !caps.paused })}>
          {caps.paused ? "Resume runs" : "Pause runs"}
        </button>
      </div>
      {invalid && <p className="form-error">enter positive numbers</p>}
      {save.isError && <p className="form-error">{(save.error as Error).message}</p>}
    </form>
  );
}

function RunDetailView({ id }: { id: string }) {
  const detail = useRun(id);
  if (detail.isError) return <p className="form-error">{(detail.error as Error).message}</p>;
  if (!detail.data) return <p className="muted">Loading</p>;
  const { run, txn_ids } = detail.data;
  return (
    <div className="run-detail">
      {run.result_md && <pre className="answer">{run.result_md}</pre>}
      {run.error && <p className="form-error">{run.error}</p>}
      <Proposals run={run} />
      {txn_ids.length > 0 && (
        <p>
          Changes: {txn_ids.map(t => <code key={t}>{t}</code>)}
        </p>
      )}
      {run.log_path && <span className="muted">{run.log_path}</span>}
    </div>
  );
}

function RunRow({ run, slug }: { run: Run; slug: string | null }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="run-row ledger-row">
      <div className="ledger-head">
        <button className="linklike" aria-expanded={open} onClick={() => setOpen(o => !o)}>
          {run.kind} · {new Date(run.created_at).toLocaleString()}
        </button>
        <span className={`badge run-${run.status}`}>{run.status}</span>
        {slug && <span className="muted">{slug}</span>}
        <span>{usd(run.est_usd)}</span>
        <span className="muted">{run.turns} turns</span>
        <span className="muted">{run.duration_ms} ms</span>
      </div>
      {open && <RunDetailView id={run.id} />}
    </li>
  );
}

export function RunsView() {
  const status = useQuery({ queryKey: ["runner-status"], queryFn: api.runnerStatus });
  const usage = useQuery({ queryKey: ["usage", 30], queryFn: () => api.usage(30) });
  const caps = useQuery({ queryKey: ["caps"], queryFn: api.caps });
  const runs = useQuery({ queryKey: ["runs", { limit: 50 }], queryFn: () => api.runs({ limit: 50 }) });
  const projects = useQuery({ queryKey: ["projects"], queryFn: api.projects });
  const slugOf = (id: string | null) => (id ? projects.data?.find(p => p.id === id)?.slug ?? null : null);

  return (
    <div className="runs inbox">
      {status.data && <p className="muted">{statusLine(status.data)}</p>}
      <h2>Usage</h2>
      {usage.data && (
        <>
          <p>
            spent today {usd(usage.data.spent_today)} of ${usage.data.caps.usd_per_day}
          </p>
          <UsageChart days={usage.data.days} cap={usage.data.caps.usd_per_day} />
          <SliceTable title="by kind" rows={usage.data.by_kind} />
          <SliceTable title="by runner" rows={usage.data.by_runner} />
        </>
      )}
      <h2>Caps</h2>
      {caps.data && <CapsForm caps={caps.data} />}
      <h2>Run history</h2>
      {runs.data && runs.data.length === 0 && <p className="muted">No runs yet</p>}
      {runs.data && runs.data.length > 0 && (
        <ul className="runs">
          {runs.data.map(r => (
            <RunRow key={r.id} run={r} slug={slugOf(r.project_id)} />
          ))}
        </ul>
      )}
    </div>
  );
}

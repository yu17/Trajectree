import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ConnectorStatus, RunnerCatalog, TokenCreate, TokenOut, TokenScope, TokenSecret } from "../api/types";
import { errorText } from "../components/errorText";
import { RunnerStatusList } from "../components/runnerStatus";

const local = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "never");

const PRESETS: Record<string, Pick<TokenCreate, "scope" | "actor_kind">> = {
  chatmaid: { scope: { projects: [], tools: ["chatmaid"], write: false }, actor_kind: "connector" },
  hooks: { scope: { projects: [], tools: ["hooks"], write: false } },
  "read-only": { scope: { write: false } },
  full: { scope: {} },
};

function scopeSummary(s: TokenScope): string {
  if (s.tools && s.tools.length > 0) return `tools: ${s.tools.join(",")}`;
  const projects = s.projects && s.projects.length > 0 ? `projects ${s.projects.join(",")}` : null;
  if (s.write === false) return projects ? `read-only: ${projects}` : "read-only";
  return `write: ${projects ?? "all projects"}`;
}

function Secret({ secret }: { secret: TokenSecret }) {
  const code = useRef<HTMLElement>(null);
  const [note, setNote] = useState<string | null>(null);
  const select = () => {
    if (code.current) window.getSelection()?.selectAllChildren(code.current);
  };
  const copy = () => {
    // navigator.clipboard is absent outside secure contexts (plain http on a LAN host).
    const clip = navigator.clipboard as Clipboard | undefined;
    if (!clip) {
      select();
      setNote("Copy failed, select the text");
      return;
    }
    clip.writeText(secret.secret).then(() => setNote("Copied"), () => { select(); setNote("Copy failed, select the text"); });
  };
  return (
    <div className="token-secret">
      <p>
        <code ref={code}>{secret.secret}</code> <button onClick={copy}>Copy</button>
      </p>
      <p className="muted">Shown once; store it now</p>
      {note && <p className="muted" role="status">{note}</p>}
    </div>
  );
}

function NewTokenForm({ onCreated }: { onCreated: (t: TokenSecret) => void }) {
  const qc = useQueryClient();
  const [label, setLabel] = useState("");
  const [preset, setPreset] = useState("chatmaid");
  const create = useMutation({
    mutationFn: () => api.createToken({ label: label.trim(), ...PRESETS[preset] }),
    onSuccess: t => {
      setLabel("");
      onCreated(t);
      void qc.invalidateQueries({ queryKey: ["tokens"] });
    },
  });
  return (
    <form
      className="panel-form token-form"
      onSubmit={e => {
        e.preventDefault();
        if (label.trim()) create.mutate();
      }}
    >
      <h3>New token</h3>
      <label htmlFor="token-label">Label</label>
      <input id="token-label" value={label} onChange={e => setLabel(e.target.value)} />
      <label htmlFor="token-scope">Scope</label>
      <select id="token-scope" value={preset} onChange={e => setPreset(e.target.value)}>
        {Object.keys(PRESETS).map(p => <option key={p} value={p}>{p}</option>)}
      </select>
      {create.isError && <p className="form-error">{errorText(create.error)}</p>}
      <button type="submit" disabled={create.isPending || !label.trim()}>Create</button>
    </form>
  );
}

function TokensSection() {
  const qc = useQueryClient();
  const tokens = useQuery({ queryKey: ["tokens"], queryFn: api.tokens });
  const [secret, setSecret] = useState<TokenSecret | null>(null);
  const revoke = useMutation({
    mutationFn: (t: TokenOut) => api.revokeToken(t.id),
    onSettled: () => void qc.invalidateQueries({ queryKey: ["tokens"] }),
  });
  const rows = tokens.data ?? [];
  return (
    <section>
      <h2>Tokens</h2>
      {tokens.isError && <p className="form-error">{errorText(tokens.error)}</p>}
      {revoke.isError && <p className="form-error">{errorText(revoke.error)}</p>}
      {tokens.isSuccess && rows.length === 0 && <p className="muted">No tokens</p>}
      {rows.length > 0 && (
        <table>
          <thead>
            <tr><th>label</th><th>scope</th><th>actor</th><th>created</th><th>expires</th><th /></tr>
          </thead>
          <tbody>
            {rows.map(t => (
              <tr key={t.id}>
                <td>{t.label}</td>
                <td>{scopeSummary(t.scope)}</td>
                <td>{t.actor_kind}</td>
                <td>{local(t.created_at)}</td>
                <td>{local(t.expires_at)}</td>
                <td>
                  <button
                    disabled={revoke.isPending}
                    onClick={() => {
                      if (window.confirm(`Revoke token "${t.label}"?`)) revoke.mutate(t);
                    }}
                  >
                    Revoke
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {secret && <Secret key={secret.id} secret={secret} />}
      <NewTokenForm onCreated={setSecret} />
    </section>
  );
}

function ConnectorsSection() {
  const connectors = useQuery({ queryKey: ["connectors"], queryFn: api.connectors });
  const rows: ConnectorStatus[] = connectors.data ?? [];
  return (
    <section>
      <h2>Connectors</h2>
      {connectors.isError && <p className="form-error">{errorText(connectors.error)}</p>}
      {connectors.isSuccess && !rows.some(r => r.configured) && <p className="muted">No connectors configured</p>}
      {rows.length > 0 && (
        <table>
          <thead>
            <tr><th>connector</th><th /><th>last run</th><th>error</th><th>detail</th></tr>
          </thead>
          <tbody>
            {rows.map(c => (
              <tr key={c.name}>
                <td>{c.name}</td>
                <td><span className="badge">{c.configured ? "configured" : "not configured"}</span></td>
                <td>{local(c.last_run)}</td>
                <td>{c.errors[0] && <span className="form-error">{c.errors[0]}</span>}</td>
                <td className="muted">{c.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function RunnerSection() {
  const status = useQuery({ queryKey: ["runner-status"], queryFn: api.runnerStatus });
  return (
    <section>
      <h2>Runners</h2>
      {status.isError && <p className="form-error">{errorText(status.error)}</p>}
      {status.data && <RunnerStatusList status={status.data} />}
    </section>
  );
}

function ModelsSection() {
  const qc = useQueryClient();
  const catalog = useQuery({ queryKey: ["models"], queryFn: api.models });
  const refresh = useMutation({
    mutationFn: api.refreshModels,
    onSuccess: c => qc.setQueryData(["models"], c),
  });
  const edit = useMutation({
    mutationFn: (body: { runner: string; add?: string[]; delete?: string[] }) => api.editModels(body),
    onSuccess: c => qc.setQueryData(["models"], c),
  });
  return (
    <section>
      <h2>Models</h2>
      <button disabled={refresh.isPending} onClick={() => refresh.mutate()}>Refresh models</button>
      {catalog.isError && <p className="form-error">{errorText(catalog.error)}</p>}
      {refresh.isError && <p className="form-error">{errorText(refresh.error)}</p>}
      {edit.isError && <p className="form-error">{errorText(edit.error)}</p>}
      {(catalog.data?.runners ?? []).map(r => (
        <RunnerModels key={r.runner} runner={r} pending={edit.isPending} onEdit={body => edit.mutate(body)} />
      ))}
    </section>
  );
}

function RunnerModels({ runner: r, pending, onEdit }: {
  runner: RunnerCatalog;
  pending: boolean;
  onEdit: (body: { runner: string; add?: string[]; delete?: string[] }) => void;
}) {
  const [name, setName] = useState("");
  const add = () => {
    const model = name.trim();
    if (!model) return;
    onEdit({ runner: r.runner, add: [model] });
    setName("");
  };
  return (
    <div>
      <h3>{r.runner}</h3>
      <p className="muted">{`${r.available ? "available" : "unavailable"} · refreshed ${local(r.refreshed_at)}`}</p>
      {r.error && <p className="form-error">{r.error}</p>}
      {r.models.length > 0 && (
        <ul className="models-list" aria-label={`${r.runner} models`}>
          {r.models.map(m => (
            <li key={m.model}>
              {m.display ?? m.model} <span className="badge">{m.source}</span>{" "}
              <button
                aria-label={`Remove ${m.model}`}
                disabled={pending}
                onClick={() => onEdit({ runner: r.runner, delete: [m.model] })}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}
      <form onSubmit={e => { e.preventDefault(); add(); }}>
        <input aria-label={`Add a model to ${r.runner}`} placeholder="model name" value={name} onChange={e => setName(e.target.value)} />{" "}
        <button type="submit" disabled={pending || !name.trim()}>Add</button>
      </form>
    </div>
  );
}

function MountsSection() {
  const mounts = useQuery({ queryKey: ["mounts"], queryFn: api.mounts });
  const m = mounts.data;
  return (
    <section>
      <h2>Mounts</h2>
      {mounts.isError && <p className="form-error">{errorText(mounts.error)}</p>}
      {m && (
        <dl className="mounts">
          <dt>Claude home</dt><dd>{m.claude_home ?? "not set"}</dd>
          <dt>Backends root</dt><dd>{m.backends_root ?? "not set"}</dd>
          <dt>Path map</dt>
          <dd>
            {m.path_map.length === 0 ? "none" : m.path_map.map(([host, container]) => <div key={host}>{`${host} -> ${container}`}</div>)}
          </dd>
          <dt>Data</dt><dd>{m.data_dir}</dd>
          <dt>Config</dt><dd>{m.cfg_dir}</dd>
          <dt>Exports</dt><dd>{m.exports_dir}</dd>
        </dl>
      )}
    </section>
  );
}

function SchedulerSection() {
  const scheduler = useQuery({ queryKey: ["scheduler"], queryFn: api.scheduler });
  const s = scheduler.data;
  return (
    <section>
      <h2>Scheduler</h2>
      {scheduler.isError && <p className="form-error">{errorText(scheduler.error)}</p>}
      {s && (
        <>
          <p className="muted">{`last tick ${local(s.last_tick)} · every ${s.interval_s} s`}</p>
          {s.next_fires.length === 0 ? (
            <p className="muted">No upcoming fires</p>
          ) : (
            <ul className="project-list">
              {s.next_fires.map(f => <li key={`${f.routine}@${f.at}`}>{`${f.routine} · ${new Date(f.at).toLocaleString()}`}</li>)}
            </ul>
          )}
        </>
      )}
    </section>
  );
}

export function SettingsView() {
  return (
    <div className="runs settings">
      <TokensSection />
      <ConnectorsSection />
      <RunnerSection />
      <ModelsSection />
      <MountsSection />
      <SchedulerSection />
    </div>
  );
}

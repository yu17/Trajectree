import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Catalog, ModelChoice, Routine } from "../api/types";
import { ActionNotice } from "../views/ActionNotice";
import { errorText } from "./errorText";
import { ModelPicker } from "./ModelPicker";
import { useRun } from "./useRun";

const ACTIVE = ["queued", "running"];
const local = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "never");

// The picker's value for a routine's stored model: the runner's option when it is in the catalog, or,
// with no runner stored, the one available runner offering the model. Otherwise null (the row shows the
// stored text and the daemon decides when it runs).
function storedChoice(routine: Routine, catalog: Catalog | undefined): ModelChoice | null {
  if (!routine.model || !catalog) return null;
  const offering = catalog.runners.filter(r => r.available && r.models.some(m => m.model === routine.model));
  if (routine.runner) return offering.some(r => r.runner === routine.runner) ? { runner: routine.runner, model: routine.model } : null;
  return offering.length === 1 ? { runner: offering[0].runner, model: routine.model } : null;
}

function RoutineRow({ routine, onNotice }: { routine: Routine; onNotice: (text: string) => void }) {
  const qc = useQueryClient();
  const [queued, setQueued] = useState<string | null>(null);
  // A run started here is shown until it settles; then the routine's last run takes over.
  const run = useRun(queued ?? routine.last_run_id);
  const status = run.data?.run.status;
  useEffect(() => {
    if (queued === null || status === undefined || ACTIVE.includes(status)) return;
    void qc.invalidateQueries({ queryKey: ["runs"] });
    void qc.invalidateQueries({ queryKey: ["routines"] });
  }, [qc, queued, status]);

  const toggle = useMutation({
    mutationFn: () => api.patchRoutine(routine.name, { enabled: !routine.enabled }),
    onError: err => onNotice(errorText(err)),
    onSettled: () => void qc.invalidateQueries({ queryKey: ["routines"] }),
  });
  // The routine's stored model is a setting, not a default: the picker shows it and saves changes.
  const setModel = useMutation({
    mutationFn: (c: ModelChoice) => api.patchRoutine(routine.name, c),
    onError: err => onNotice(errorText(err)),
    onSettled: () => void qc.invalidateQueries({ queryKey: ["routines"] }),
  });
  const catalog = useQuery({ queryKey: ["models"], queryFn: api.models });
  const stored = storedChoice(routine, catalog.data);
  const runNow = useMutation({
    // Run now uses the stored model.
    mutationFn: () => api.runRoutine(routine.name),
    onSuccess: r => {
      setQueued(r.id);
      onNotice(`Run queued (${r.id})`);
    },
    onError: err => onNotice(errorText(err)),
  });
  const busy = status !== undefined && queued !== null && ACTIVE.includes(status);

  return (
    <tr>
      <td>{routine.name}</td>
      <td>{`${routine.trigger} (${routine.tz})`}</td>
      <td>{routine.next_fire ? new Date(routine.next_fire).toLocaleString() : "—"}</td>
      <td>{status ? <span className={`badge run-${status}`}>{status}</span> : <span className="muted">—</span>}</td>
      <td>
        <ModelPicker value={setModel.isPending ? setModel.variables : stored} onChange={c => c && setModel.mutate(c)} />
        {routine.model && !stored && !setModel.isPending && (
          <span className="muted">{`stored: ${routine.model}${routine.runner ? ` on ${routine.runner}` : ""}`}</span>
        )}
      </td>
      <td>
        <button disabled={toggle.isPending} onClick={() => toggle.mutate()}>
          {routine.enabled ? "Disable" : "Enable"}
        </button>
      </td>
      <td>
        <button disabled={!routine.model || runNow.isPending || busy} onClick={() => runNow.mutate()}>Run now</button>
      </td>
    </tr>
  );
}

export function RoutinesSection() {
  const routines = useQuery({ queryKey: ["routines"], queryFn: api.routines });
  const scheduler = useQuery({ queryKey: ["scheduler"], queryFn: api.scheduler });
  const [notice, setNotice] = useState<string | null>(null);
  const rows = routines.data ?? [];

  return (
    <section className="routines">
      <h2>Routines</h2>
      {scheduler.data && (
        <p className="muted">{`Scheduler: last tick ${local(scheduler.data.last_tick)} · every ${scheduler.data.interval_s} s`}</p>
      )}
      <ActionNotice conflicts={null} error={notice} onDismiss={() => setNotice(null)} />
      {routines.isError && <p className="form-error">{errorText(routines.error)}</p>}
      {routines.isSuccess && rows.length === 0 && <p className="muted">No routines</p>}
      {rows.length > 0 && (
        <div className="table-scroll">
        <table>
          <thead>
            <tr><th>name</th><th>trigger</th><th>next fire</th><th>last run</th><th>model</th><th /><th /></tr>
          </thead>
          <tbody>
            {rows.map(r => <RoutineRow key={r.id} routine={r} onNotice={setNotice} />)}
          </tbody>
        </table>
        </div>
      )}
    </section>
  );
}

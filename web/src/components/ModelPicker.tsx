import { useEffect, useId } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ModelChoice } from "../api/types";
import { errorText } from "./errorText";

const SEP = "|";
const encode = (c: ModelChoice) => `${c.runner}${SEP}${c.model}`;
const decode = (v: string): ModelChoice => {
  const i = v.indexOf(SEP);
  return { runner: v.slice(0, i), model: v.slice(i + 1) };
};

// A long error would widen the select past a phone screen.
const LABEL_MAX = 48;
const unavailableLabel = (runner: string, error: string | null) => {
  const label = `${runner} (unavailable${error ? `: ${error}` : ""})`;
  return label.length > LABEL_MAX ? `${label.slice(0, LABEL_MAX - 1)}…` : label;
};

// Two levels in one native select: a runner is an optgroup, its models are the options. The first
// option is a disabled placeholder, so nothing is preselected and nothing is remembered between uses.
export function ModelPicker({ value, onChange, label = "Model" }: {
  value: ModelChoice | null;
  // null when the catalog no longer offers the held value: the owner has to pick again.
  onChange: (choice: ModelChoice | null) => void;
  label?: string;
}) {
  const id = useId();
  const catalog = useQuery({ queryKey: ["models"], queryFn: api.models });
  const offered = (c: ModelChoice) =>
    (catalog.data?.runners ?? []).some(r => r.available && r.runner === c.runner && r.models.some(m => m.model === c.model));
  const stale = value !== null && catalog.data !== undefined && !offered(value);
  // A refresh can drop the model a control holds; clear it rather than act on a vanished model.
  useEffect(() => {
    if (stale) onChange(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stale]);
  return (
    <div className="model-picker">
      <label htmlFor={id}>{label}</label>
      <select id={id} value={value && !stale ? encode(value) : ""} onChange={e => onChange(decode(e.target.value))}>
        <option value="" disabled>Choose a model…</option>
        {(catalog.data?.runners ?? []).map(r =>
          r.available ? (
            <optgroup key={r.runner} label={r.runner}>
              {r.models.map(m => <option key={m.model} value={encode(m)}>{m.display ?? m.model}</option>)}
            </optgroup>
          ) : (
            <optgroup key={r.runner} label={unavailableLabel(r.runner, r.error)} disabled />
          ))}
      </select>
      {catalog.isError && <p className="form-error">{errorText(catalog.error)}</p>}
    </div>
  );
}

import { useId } from "react";
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

// Two levels in one native select: a runner is an optgroup, its models are the options. The first
// option is a disabled placeholder, so nothing is preselected and nothing is remembered between uses.
export function ModelPicker({ value, onChange, label = "Model" }: {
  value: ModelChoice | null;
  onChange: (choice: ModelChoice) => void;
  label?: string;
}) {
  const id = useId();
  const catalog = useQuery({ queryKey: ["models"], queryFn: api.models });
  return (
    <div className="model-picker">
      <label htmlFor={id}>{label}</label>
      <select id={id} value={value ? encode(value) : ""} onChange={e => onChange(decode(e.target.value))}>
        <option value="" disabled>Choose a model…</option>
        {(catalog.data?.runners ?? []).map(r =>
          r.available ? (
            <optgroup key={r.runner} label={r.runner}>
              {r.models.map(m => <option key={m.model} value={encode(m)}>{m.display ?? m.model}</option>)}
            </optgroup>
          ) : (
            <optgroup key={r.runner} label={`${r.runner} (unavailable${r.error ? `: ${r.error}` : ""})`} disabled />
          ))}
      </select>
      {catalog.isError && <p className="form-error">{errorText(catalog.error)}</p>}
    </div>
  );
}

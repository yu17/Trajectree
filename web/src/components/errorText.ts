import { ApiError } from "../api/client";

// Server error text for inline display: a string detail, or FastAPI 422 messages joined.
export function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    const d = err.body?.detail;
    if (Array.isArray(d)) {
      return d.map(x => (typeof x === "string" ? x : x?.msg ?? JSON.stringify(x))).join("; ");
    }
    return err.detail || `HTTP ${err.status}`;
  }
  return err instanceof Error ? err.message : String(err);
}

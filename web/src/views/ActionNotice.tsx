import type { Conflict } from "../api/types";

// Dismissible notice for a failed revert (conflict rows) or review/revert error.
export function ActionNotice({
  conflicts, error, onDismiss,
}: { conflicts: Conflict[] | null; error: string | null; onDismiss: () => void }) {
  if (!conflicts && !error) return null;
  return (
    <div role="alert" className="notice">
      <button className="notice-close" aria-label="Dismiss" onClick={onDismiss}>×</button>
      {error && <p>{error}</p>}
      {conflicts && (
        <>
          <p>Revert conflicts: later changes touch these rows.</p>
          <ul>
            {conflicts.map((c, i) => (
              <li key={i}>
                {c.reason === "SQLITE_CHANGESET_FOREIGN_KEY"
                  ? "Other rows still reference rows this transaction created"
                  : <code>{c.table_name} {c.op} {c.pk.join(", ")}</code>}{" "}
                {c.reason}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

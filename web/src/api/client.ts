import type {
  Edge,
  EdgeIn,
  Event,
  EventIn,
  EventPatch,
  Project,
  ProjectIn,
  SearchHit,
  TimelineResponse,
  Todo,
  TodoIn,
  TodoPatch,
  Txn,
  TxnDetail,
} from "./types";

export class ApiError extends Error {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  constructor(public status: number, public detail: string, public body: any) {
    super(detail);
    this.name = "ApiError";
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let parsed: unknown = null;
    try {
      parsed = await res.json();
    } catch {
      parsed = null;
    }
    const d = (parsed as { detail?: unknown } | null)?.detail;
    throw new ApiError(res.status, typeof d === "string" ? d : res.statusText, parsed);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  timeline(from: Date, to: Date, projects?: string[]) {
    const q = new URLSearchParams({ from: from.toISOString(), to: to.toISOString() });
    if (projects && projects.length) q.set("projects", projects.join(","));
    return request<TimelineResponse>("GET", `/api/timeline?${q}`);
  },
  projects: () => request<Project[]>("GET", "/api/projects"),
  createProject: (body: ProjectIn) => request<Project>("POST", "/api/projects", body),
  createEvent: (body: EventIn) => request<Event>("POST", "/api/events", body),
  updateEvent: (id: string, patch: EventPatch) =>
    request<Event>("PATCH", `/api/events/${encodeURIComponent(id)}`, patch),
  createTodo: (body: TodoIn) => request<Todo>("POST", "/api/todos", body),
  updateTodo: (id: string, patch: TodoPatch) =>
    request<Todo>("PATCH", `/api/todos/${encodeURIComponent(id)}`, patch),
  createEdge: (body: EdgeIn) => request<Edge>("POST", "/api/edges", body),
  deleteEdge: (id: string) => request<void>("DELETE", `/api/edges/${encodeURIComponent(id)}`),
  txns(limit = 50, before?: string) {
    const q = new URLSearchParams({ limit: String(limit) });
    if (before) q.set("before", before);
    return request<Txn[]>("GET", `/api/ledger/txns?${q}`);
  },
  txn: (id: string) => request<TxnDetail>("GET", `/api/ledger/txns/${encodeURIComponent(id)}`),
  revert: (id: string, intent?: string) =>
    request<Txn>("POST", `/api/ledger/txns/${encodeURIComponent(id)}/revert`, { intent }),
  review: (id: string, state: "accepted" | "rejected") =>
    request<Txn>("POST", `/api/ledger/txns/${encodeURIComponent(id)}/review`, { state }),
  search: (q: string) =>
    request<SearchHit[]>("GET", `/api/search?${new URLSearchParams({ q })}`),
};

import type {
  AskRequest,
  Caps,
  Catalog,
  ConnectorStatus,
  ModelChoice,
  ContextPack,
  Mounts,
  QuickAddRequest,
  TokenCreate,
  TokenOut,
  TokenSecret,
  Report,
  ReportFormat,
  ReportIn,
  ReportOut,
  ReportPatch,
  ReportRequest,
  Run,
  RunDetail,
  RunKind,
  RunnerStatus,
  RunStatus,
  Routine,
  RoutineIn,
  RoutinePatch,
  SchedulerStatus,
  UsageReport,
  Edge,
  EdgeIn,
  Event,
  EventIn,
  EventPatch,
  IngestReport,
  InspectionDue,
  ReviewManyResult,
  Project,
  ProjectIn,
  Resource,
  Review,
  SearchHit,
  Signal,
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
  todos(states?: string[]) {
    const q = new URLSearchParams();
    if (states && states.length) q.set("states", states.join(","));
    return request<Todo[]>("GET", `/api/todos${q.size ? `?${q}` : ""}`);
  },
  // `actor_kind` is a comma-separated list, e.g. "agent,session".
  txns(limit = 50, before?: string, filter?: { review?: Review; actor_kind?: string; run_id?: string }) {
    const q = new URLSearchParams({ limit: String(limit) });
    if (before) q.set("before", before);
    if (filter?.review) q.set("review", filter.review);
    if (filter?.actor_kind) q.set("actor_kind", filter.actor_kind);
    if (filter?.run_id) q.set("run_id", filter.run_id);
    return request<Txn[]>("GET", `/api/ledger/txns?${q}`);
  },
  txn: (id: string) => request<TxnDetail>("GET", `/api/ledger/txns/${encodeURIComponent(id)}`),
  revert: (id: string, intent?: string) =>
    request<Txn>("POST", `/api/ledger/txns/${encodeURIComponent(id)}/revert`, { intent }),
  review: (id: string, state: "accepted" | "rejected") =>
    request<Txn>("POST", `/api/ledger/txns/${encodeURIComponent(id)}/review`, { state }),
  reviewMany: (ids: string[], state: "accepted" | "rejected") =>
    request<ReviewManyResult>("POST", "/api/ledger/review-many", { ids, state }),
  search: (q: string) =>
    request<SearchHit[]>("GET", `/api/search?${new URLSearchParams({ q })}`),
  // `kinds` narrows the signal kinds; `limit` overrides the server's default of 200.
  signals(projects: string[], from: Date, to: Date, opts: { kinds?: string[]; limit?: number } = {}) {
    const q = new URLSearchParams({ from: from.toISOString(), to: to.toISOString() });
    if (projects.length) q.set("projects", projects.join(","));
    if (opts.kinds?.length) q.set("kinds", opts.kinds.join(","));
    if (opts.limit !== undefined) q.set("limit", String(opts.limit));
    return request<Signal[]>("GET", `/api/signals?${q}`);
  },
  resources(projectId?: string) {
    const q = new URLSearchParams();
    if (projectId) q.set("projects", projectId);
    return request<Resource[]>("GET", `/api/resources${q.size ? `?${q}` : ""}`);
  },
  runIngest: () => request<IngestReport>("POST", "/api/ingest/run"),
  runs(opts: { limit?: number; kind?: RunKind; status?: RunStatus; openProposals?: boolean; seriesId?: string } = {}) {
    const q = new URLSearchParams();
    if (opts.limit !== undefined) q.set("limit", String(opts.limit));
    if (opts.kind) q.set("kind", opts.kind);
    if (opts.status) q.set("status", opts.status);
    if (opts.openProposals) q.set("open_proposals", "true");
    if (opts.seriesId) q.set("series_id", opts.seriesId);
    return request<Run[]>("GET", `/api/runs${q.size ? `?${q}` : ""}`);
  },
  run: (id: string) => request<RunDetail>("GET", `/api/runs/${encodeURIComponent(id)}`),
  models: () => request<Catalog>("GET", "/api/models"),
  refreshModels: () => request<Catalog>("POST", "/api/models/refresh"),
  editModels: (body: { runner: string; add?: string[]; delete?: string[] }) => request<Catalog>("POST", "/api/models/edit", body),
  summarize: (projectId: string, choice: ModelChoice) =>
    request<Run>("POST", `/api/projects/${encodeURIComponent(projectId)}/summarize`, choice),
  inspections: () => request<InspectionDue[]>("GET", "/api/inspections"),
  inspect: (projectId: string, choice: ModelChoice, since?: string) =>
    request<Run>("POST", `/api/projects/${encodeURIComponent(projectId)}/inspect`, { ...choice, ...(since ? { since } : {}) }),
  inspectAll: (choice: ModelChoice) =>
    request<{ runs: Run[]; skipped: string[] }>("POST", "/api/inspect", { projects: "all", ...choice }),
  summarizeSession: (sessionId: string, choice: ModelChoice) =>
    request<Run>("POST", `/api/sessions/${encodeURIComponent(sessionId)}/summarize`, choice),
  ask: (body: AskRequest) => request<Run>("POST", "/api/ask", body),
  quickAdd: (body: QuickAddRequest) => request<Run>("POST", "/api/quick-add", body),
  acceptProposal: (runId: string, index: number) =>
    request<Run>("POST", `/api/runs/${encodeURIComponent(runId)}/proposals/${index}/accept`),
  dismissProposal: (runId: string, index: number) =>
    request<Run>("POST", `/api/runs/${encodeURIComponent(runId)}/proposals/${index}/dismiss`),
  usage: (days = 30) => request<UsageReport>("GET", `/api/usage?days=${days}`),
  caps: () => request<Caps>("GET", "/api/caps"),
  setCaps: (caps: Caps) => request<Caps>("PUT", "/api/caps", caps),
  runnerStatus: () => request<RunnerStatus>("GET", "/api/runner/status"),
  routines: () => request<Routine[]>("GET", "/api/routines"),
  createRoutine: (body: RoutineIn) => request<Routine>("POST", "/api/routines", body),
  patchRoutine: (ref: string, patch: RoutinePatch) =>
    request<Routine>("PATCH", `/api/routines/${encodeURIComponent(ref)}`, patch),
  // Without a choice the routine runs on its stored model.
  runRoutine: (ref: string, opts: { choice?: ModelChoice | null; projects?: string[] } = {}) =>
    request<Run>("POST", `/api/routines/${encodeURIComponent(ref)}/run`, {
      ...(opts.choice ?? {}),
      ...(opts.projects ? { projects: opts.projects } : {}),
    }),
  routineRuns: (ref: string, limit = 20) =>
    request<Run[]>("GET", `/api/routines/${encodeURIComponent(ref)}/runs?limit=${limit}`),
  scheduler: () => request<SchedulerStatus>("GET", "/api/scheduler"),
  tokens: () => request<TokenOut[]>("GET", "/api/tokens"),
  createToken: (body: TokenCreate) => request<TokenSecret>("POST", "/api/tokens", body),
  revokeToken: (id: string) => request<void>("DELETE", `/api/tokens/${encodeURIComponent(id)}`),
  connectors: () => request<ConnectorStatus[]>("GET", "/api/connectors/status"),
  mounts: () => request<Mounts>("GET", "/api/mounts"),
  reports: () => request<ReportOut[]>("GET", "/api/reports"),
  report: (id: string) => request<Report>("GET", `/api/reports/${encodeURIComponent(id)}`),
  createReport: (body: ReportIn) => request<Report>("POST", "/api/reports", body),
  updateReport: (id: string, patch: ReportPatch) =>
    request<Report>("PATCH", `/api/reports/${encodeURIComponent(id)}`, patch),
  draftReport: (body: ReportRequest) => request<Run>("POST", "/api/reports/draft", body),
  // A plain URL for a link or download: the export is a file, not JSON.
  reportExportUrl: (id: string, fmt: ReportFormat) =>
    `/api/reports/${encodeURIComponent(id)}/export?format=${fmt}`,
  context(projects: string[], from: Date, to: Date, budget = 4000) {
    const q = new URLSearchParams({ from: from.toISOString(), to: to.toISOString(), budget: String(budget) });
    if (projects.length) q.set("projects", projects.join(","));
    return request<ContextPack>("GET", `/api/context?${q}`);
  },
};

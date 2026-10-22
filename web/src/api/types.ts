// Mirrors src/trajectree/models.py and the API response models. Datetimes are ISO strings.

export type ProjectKind = "research" | "tooling" | "routine" | "admin" | "inbox";
export type ProjectStatus = "active" | "paused" | "done" | "archived";
export type EventStatus = "happened" | "planned" | "suggested" | "dropped";
export type Source = "human" | "agent" | "connector";
export type TodoState = "open" | "doing" | "done" | "dropped";
export type Review = "none" | "pending" | "accepted" | "rejected";
export type ActorKind = "human" | "agent" | "session" | "connector";
export type EntityType = "project" | "event" | "todo" | "resource";
export type SignalKind = "commits" | "session" | "files_changed" | "job_state" | "message";

export interface Project {
  id: string;
  slug: string;
  title: string;
  kind: ProjectKind;
  status: ProjectStatus;
  lane_order: number;
  summary_md: string;
  recurrence: string | null;
  props: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface Event {
  id: string;
  project_id: string;
  kind: string;
  title: string;
  body_md: string;
  t_start: string;
  t_end: string | null;
  status: EventStatus;
  source: Source;
  props: Record<string, unknown>;
  created_txn: string | null;
  created_at: string;
  updated_at: string;
}

export interface Todo {
  id: string;
  project_id: string | null;
  title: string;
  body_md: string;
  due: string | null;
  state: TodoState;
  priority: number;
  props: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
}

export interface Edge {
  id: string;
  src_type: EntityType;
  src_id: string;
  dst_type: EntityType;
  dst_id: string;
  rel: string;
  props: Record<string, unknown>;
  valid_from: string | null;
  valid_to: string | null;
  created_at: string;
}

// `Txn` as served by the API (TxnOut): the raw changeset bytes are omitted.
export interface Txn {
  id: string;
  ts: string;
  actor_kind: ActorKind;
  actor_ref: string;
  model: string | null;
  backend: string | null;
  tool: string | null;
  tool_version: string | null;
  session_id: string | null;
  run_id: string | null;
  proposed_by_run: string | null;
  intent: string;
  review: Review;
  review_ts: string | null;
  reverts_txn: string | null;
  reverted_by: string | null;
}

export interface Change {
  txn_id: string;
  seq: number;
  table_name: string;
  op: string;
  pk: unknown[];
  old: Record<string, unknown> | null;
  new: Record<string, unknown> | null;
}

export interface Conflict {
  reason: string;
  table_name: string;
  op: string;
  pk: unknown[];
  old: Record<string, unknown> | null;
  new: Record<string, unknown> | null;
  conflict_row: Record<string, unknown> | null;
}

export interface Resource {
  id: string;
  project_id: string;
  kind: string;
  locator: string;
  title: string;
  props: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
}

export interface Signal {
  id: string;
  project_id: string;
  resource_id: string | null;
  kind: SignalKind;
  t_start: string;
  t_end: string | null;
  payload: Record<string, unknown>;
  dedupe_key: string;
  updated_at: string;
}

// One day's activity for a project lane's activity strip; `day` is "YYYY-MM-DD".
export interface StripBin {
  project_id: string;
  day: string;
  kind: SignalKind;
  // commits for the commits kind; signals otherwise
  count: number;
}

export interface LiveSession {
  project_id: string;
  session_id: string;
  cwd: string;
  title: string | null;
  started: string | null;
  last_seen: string;
}

export interface TimelineResponse {
  projects: Project[];
  events: Event[];
  todos: Todo[];
  edges: Edge[];
  // Per entity kind ("event", "todo", "edge"): row id -> id of the pending txn of its latest change.
  pending: Record<string, Record<string, string>>;
  strip: StripBin[];
  live: LiveSession[];
  stale: string[];
}

export interface ScanReport {
  files_seen: number;
  files_read: number;
  signals_written: number;
  live: LiveSession[];
}

export interface GitReport {
  repos: number;
  signals_written: number;
  errors: string[];
}

export interface IngestReport {
  started: string;
  duration_ms: number;
  scan: ScanReport | null;
  git: GitReport;
  stale: string[];
  exports_written: number;
}

export interface TxnDetail {
  txn: Txn;
  changes: Change[];
}

export interface SearchHit {
  id: string;
  kind: string;
  project_id: string | null;
  title: string;
  snippet: string;
}

// Request bodies (ProjectIn, EventIn, TodoIn, EdgeIn and their patches).
export interface ProjectIn {
  slug: string;
  title: string;
  kind: ProjectKind;
  status?: ProjectStatus;
  lane_order?: number;
  summary_md?: string;
  recurrence?: string | null;
  props?: Record<string, unknown>;
}

export interface EventIn {
  project_id: string;
  kind: string;
  title: string;
  body_md?: string;
  t_start: string;
  t_end?: string | null;
  status?: EventStatus;
  source?: Source;
  props?: Record<string, unknown>;
  intent?: string;
}

export interface TodoIn {
  project_id?: string | null;
  title: string;
  body_md?: string;
  due?: string | null;
  state?: TodoState;
  priority?: number;
  props?: Record<string, unknown>;
  intent?: string;
}

export interface EdgeIn {
  src_type: EntityType;
  src_id: string;
  dst_type: EntityType;
  dst_id: string;
  rel: string;
  props?: Record<string, unknown>;
  valid_from?: string | null;
  valid_to?: string | null;
}

export type EventPatch = Partial<EventIn>;
export type TodoPatch = Partial<TodoIn>;

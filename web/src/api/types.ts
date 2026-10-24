// Mirrors src/trajectree/models.py and the API response models. Datetimes are ISO strings.

export type ProjectKind = "research" | "tooling" | "routine" | "admin" | "inbox";
export type ProjectStatus = "active" | "paused" | "done" | "archived";
export type EventStatus = "happened" | "planned" | "suggested" | "dropped";
export type Source = "human" | "agent" | "connector";
export type TodoState = "open" | "doing" | "done" | "dropped";
export type Review = "none" | "pending" | "accepted" | "rejected";
export type ActorKind = "human" | "agent" | "session" | "connector";
export type EntityType = "project" | "event" | "todo" | "resource";
export type SignalKind = "commits" | "session" | "files_changed" | "job_state" | "message" | "remote";

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

export interface FoldersReport {
  folders: number;
  signals_written: number;
  errors: string[];
}

export interface RemoteReport {
  repos: number;
  signals_written: number;
  milestones_created: number;
  errors: string[];
}

export interface MaterializeReport {
  created: number;
  dropped: number;
  errors: string[];
}

export interface SlurmReport {
  jobs: number;
  resources: number;
  signals_written: number;
  errors: string[];
}

export interface IngestReport {
  started: string;
  duration_ms: number;
  scan: ScanReport | null;
  git: GitReport;
  folders?: FoldersReport | null;
  remote?: RemoteReport | null;
  slurm?: SlurmReport | null;
  recurrence?: MaterializeReport | null;
  stale: string[];
  exports_written: number;
  exports_errors: string[];
}

export type RunKind = "summarize" | "summarize_session" | "ask" | "quick_add" | "routine" | "report" | "starting_point";
export type RunStatus = "queued" | "running" | "ok" | "failed" | "capped" | "cancelled";

export interface Proposal {
  kind: "event" | "todo";
  project: string;
  title: string;
  reason: string;
  event_kind: string;
  when: string | null;
  due: string | null;
  body_md: string;
  status: "happened" | "planned" | "suggested";
  applied_txn: string | null;
  applied_id: string | null;
  dismissed: boolean;
}

export interface Run {
  id: string;
  kind: RunKind;
  project_id: string | null;
  status: RunStatus;
  runner: string;
  backend: string;
  model: string | null;
  session_id: string | null;
  started: string | null;
  finished: string | null;
  tokens_in: number;
  tokens_out: number;
  tokens_cache: number;
  est_usd: number;
  turns: number;
  duration_ms: number;
  result_md: string;
  proposals: Proposal[];
  error: string | null;
  log_path: string | null;
  token_id: string | null;
  props: Record<string, unknown>;
  routine_id: string | null;
  slot: "interactive" | "scheduled";
  attempt: number;
  created_at: string;
}

export interface RunDetail {
  run: Run;
  txn_ids: string[];
}

export interface Caps {
  usd_per_run: number;
  usd_per_day: number;
  timeout_s: number;
  paused: boolean;
}

export interface UsageDay {
  day: string;
  est_usd: number;
  runs: number;
  tokens_in: number;
  tokens_out: number;
}

export interface UsageSlice {
  key: string;
  est_usd: number;
  runs: number;
}

export interface UsageReport {
  days: UsageDay[];
  by_kind: UsageSlice[];
  by_runner: UsageSlice[];
  /** Keyed by routine name; `-` for runs of no routine. */
  by_routine: UsageSlice[];
  spent_today: number;
  caps: Caps;
}

export type WriteMode = "direct" | "propose";
export type Delivery = "none" | "inbox" | "log" | "chatmaid";
export type CwdMode = "project_folder" | "data_dir";

export interface RoutineScope {
  projects: string[] | null;
  lookback_days: number;
}

export interface RoutineCaps {
  usd_per_run: number;
  usd_per_day: number;
}

export interface RoutineIn {
  name: string;
  /** A cron expression or "session_end". */
  trigger: string;
  tz?: string;
  enabled?: boolean;
  runner?: string | null;
  model?: string | null;
  prompt_ref: string;
  scope?: RoutineScope;
  cwd_mode?: CwdMode;
  tool_policy?: string[];
  caps?: RoutineCaps;
  write_mode?: WriteMode;
  delivery?: Delivery;
  props?: Record<string, unknown>;
}

/** Only `runner` and `model` may be set to null. */
export type RoutinePatch = Partial<Omit<RoutineIn, "runner" | "model">> & {
  runner?: string | null;
  model?: string | null;
};

export interface Routine extends Required<RoutineIn> {
  id: string;
  next_fire: string | null;
  last_fired: string | null;
  last_run_id: string | null;
  last_error: string | null;
  paused_until: string | null;
  created_at: string;
  updated_at: string;
}

export interface NextFire {
  routine: string;
  at: string;
}

export interface SchedulerStatus {
  last_tick: string | null;
  interval_s: number;
  next_fires: NextFire[];
}

export interface SchedulerReport {
  ticked: string;
  fired: string[];
  skipped: string[];
  retried: string[];
  errors: string[];
}

export interface RunnerInfo {
  available: boolean;
  version: string | null;
  authenticated: boolean;
}

export interface BackendInfo {
  name: string;
  credentials: boolean;
}

export interface RunnerStatus {
  claude: RunnerInfo;
  codex: RunnerInfo;
  backends: BackendInfo[];
}

export interface SessionRow {
  id: string;
  cwd: string;
  project_id: string | null;
  transcript_path: string | null;
  backend: string;
  started_at: string;
  last_seen: string;
  ended_at: string | null;
  end_reason: string | null;
  source: string | null;
}

export interface ContextPack {
  markdown: string;
  tokens_est: number;
  dropped: string[];
  projects: string[];
}

export interface AskRequest {
  question: string;
  projects?: string[] | null;
  start?: string | null;
  end?: string | null;
  token_budget?: number;
  runner?: string | null;
  model?: string | null;
}

export interface QuickAddRequest {
  text: string;
  project?: string | null;
  runner?: string | null;
  model?: string | null;
}

export type ReportKind = "deck" | "memo";
export type ReportFormat = "md" | "html" | "pdf" | "pptx";

export interface ReportScope {
  /** Project ids. */
  projects: string[];
  start: string | null;
  end: string | null;
}

export interface ReportIn {
  title: string;
  kind: ReportKind;
  scope?: Partial<ReportScope>;
  /** Marp-flavoured Markdown: front matter, slides split on `---`, notes as HTML comments. */
  source_md?: string;
  props?: Record<string, unknown>;
}

export type ReportPatch = Partial<ReportIn>;

export interface Report {
  id: string;
  title: string;
  kind: ReportKind;
  scope: ReportScope;
  source_md: string;
  props: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface ReportOut extends Report {
  /** The pending transaction that last changed this report (an agent draft awaiting review). */
  pending_txn: string | null;
}

export interface ReportRequest {
  kind: ReportKind;
  title: string;
  /** Project slugs, at least one. */
  projects: string[];
  start?: string | null;
  end?: string | null;
  token_budget?: number;
  runner?: string | null;
  model?: string | null;
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

export interface TokenScope {
  projects?: string[] | null;
  tools?: string[] | null;
  write?: boolean;
}

export type TokenActorKind = "agent" | "session" | "connector";

export interface TokenCreate {
  label: string;
  scope: TokenScope;
  ttl_s?: number | null;
  actor_kind?: TokenActorKind;
}

export interface TokenOut {
  id: string;
  label: string;
  scope: TokenScope;
  actor_kind: string;
  expires_at: string | null;
  created_at: string;
}

export interface TokenSecret extends TokenOut {
  secret: string;
}

export type ConnectorName =
  | "claude-sessions" | "local-git" | "folders" | "github" | "gitlab" | "slurm" | "chatmaid" | "recurrence";

export interface ConnectorStatus {
  name: ConnectorName;
  configured: boolean;
  last_run: string | null;
  errors: string[];
  detail: string | null;
}

export interface Mounts {
  claude_home: string | null;
  backends_root: string | null;
  path_map: string[][];
  data_dir: string;
  cfg_dir: string;
  exports_dir: string;
}

/** What the owner picked in a model picker: the runner it runs on and the model name. */
export type ModelChoice = { runner: string; model: string };

export interface CatalogModel {
  runner: string;
  model: string;
  display: string | null;
  source: "api" | "cache" | "profile" | "builtin";
}

export interface RunnerCatalog {
  runner: string;
  available: boolean;
  models: CatalogModel[];
  refreshed_at: string | null;
  error: string | null;
}

export interface Catalog {
  runners: RunnerCatalog[];
}

export type EventPatch = Partial<EventIn>;
export type TodoPatch = Partial<TodoIn>;

// Shapes mirror symphony_mvp/dashboard/server.py response bodies. Kept
// hand-written rather than codegen'd to make the contract explicit and
// reviewable when the backend evolves.

export type RunStateStr =
  | "unclaimed"
  | "claimed"
  | "running"
  | "retry_queued"
  | "released";

export type ColumnKey =
  | "pending"
  | "claimed"
  | "running"
  | "retry_queued"
  | "released";

export type TerminalReasonStr =
  | "handoff"
  | "tracker_terminal"
  | "agent_finished"
  | "max_turns"
  | "stall_timeout"
  | "user_input_required"
  | "error"
  | "aborted"
  | "needs_human";  // Phase D3 — agent escalated via [HUMAN_REQUIRED]

export interface IssueDTO {
  id: string;
  identifier?: string;
  title?: string;
  description?: string;
  priority?: number;
  state?: string;
  branch_name?: string | null;
  url?: string;
  labels?: string[];
  blocked_by?: string[];
  created_at?: string | null;
  updated_at?: string | null;
}

export interface AttemptDTO {
  issue_id: string;
  attempt_number: number;
  state: RunStateStr;
  started_at: string | null;
  ended_at: string | null;
  terminal_reason: TerminalReasonStr | null;
  last_event_at: string | null;
  session_id: string | null;
  turns_consumed: number;
  cost_usd: number;
  error_message: string | null;
  retry_after: string | null;
  paused_until: string | null;
}

export interface AttemptHistoryRow {
  issue_id: string;
  attempt_number: number;
  state: string;
  started_at: string | null;
  ended_at: string | null;
  terminal_reason: string | null;
  last_event_at: string | null;
  session_id: string | null;
  turns_consumed: number;
  cost_usd: number;
  error_message: string | null;
}

export interface KanbanEntry {
  issue: IssueDTO;
  attempt: AttemptDTO | null;
  queue_rank: number | null;
}

export interface ConfigDTO {
  tracker_kind: string;
  tracker_repo: string | null;
  active_states: string[];
  terminal_states: string[];
  polling_interval_ms: number;
  workspace_root: string;
  max_concurrent_agents: number;
  max_turns: number;
  stall_timeout_ms: number;
  retry_max_attempts: number;
  handoff_state: string;
  runner_kind: string;
  runner_model: string;
  runner_provider?: string;
}

export interface StateSnapshot {
  tick_at: string;
  config: ConfigDTO;
  columns: Record<ColumnKey, KanbanEntry[]>;
  totals: {
    active_workers: number;
    released_today: number;
  };
}

export interface HintDTO {
  id: number;
  author: string;
  content: string;
  created_at: string;
  consumed: boolean;
  consumed_at: string | null;
  consumed_attempt: number | null;
}

export interface IssueDetail {
  issue: IssueDTO;
  current_attempt: AttemptDTO | null;
  all_attempts: AttemptHistoryRow[];
  hints: HintDTO[];
  rendered_prompt_preview: string | null;
  workspace_path: string | null;
  tracker_url: string | null;
}

export interface WorkspaceEntry {
  path: string;
  is_dir: boolean;
  size?: number | null;
}

export interface WorkspaceListing {
  workspace_path: string;
  exists: boolean;
  entries: WorkspaceEntry[];
}

export interface FilePreview {
  path: string;
  size: number;
  truncated: boolean;
  lines: string[];
}

export interface EventRecord {
  id: number;
  attempt_number: number;
  kind: string;
  timestamp: string;
  data: Record<string, unknown>;
}

export interface EventsResult {
  issue_id: string;
  attempt_number: number | null;
  events: EventRecord[];
}

export interface AttemptsResult {
  issue_id: string;
  current: AttemptDTO | null;
  history: AttemptHistoryRow[];
}

export interface WorkflowResource {
  path: string;
  content: string;
  config: ConfigDTO;
  last_load_error: string | null;
}

export interface WorkflowPutSuccess {
  ok: true;
  config: ConfigDTO;
}
export interface WorkflowPutFailure {
  ok: false;
  error: string;
  kept_previous: true;
}
export type WorkflowPutResult = WorkflowPutSuccess | WorkflowPutFailure;

// ---------- WebSocket message envelope ----------

// ---- Projects (Phase E1 — multi-project) --------------------------------

export interface ProjectDTO {
  id: string;
  name: string;
  created_at: string;
  archived_at: string | null;
}

export interface ListProjectsResult {
  projects: ProjectDTO[];
}

// ---- Tool API (external — used by the in-browser chat panel) ------------
// Mirrors symphony_mvp/dashboard/tool_api.py SubTaskSpec / SubmitTaskIn /
// SubmitTaskOut. Only the fields the chat panel actually sends/reads are
// modelled here — fully-typed schemas live in the backend's OpenAPI.

export interface SubTaskSpecDTO {
  task: string;
  depends_on?: number[];
}

export interface SubmitCodingTaskBody {
  task: string;
  repo?: string;
  subtasks?: SubTaskSpecDTO[];
  idempotency_key?: string;
  project_id?: string;
}

export interface SubmitCodingTaskResult {
  task_id: string;
  status: "pending";
  trace_id: string;
}

export type WSMessage =
  | {
      type: "agent_event";
      issue_id: string;
      replay?: boolean;
      event: { kind: string; timestamp: string; data: Record<string, unknown> };
    }
  | {
      type: "fsm_transition";
      issue_id: string;
      from: RunStateStr;
      to: RunStateStr;
    }
  | { type: "config_changed"; config: ConfigDTO }
  | { type: "workflow_reloaded"; ok: boolean; config: ConfigDTO }
  | { type: "state_snapshot"; snapshot: StateSnapshot }
  | {
      // Server pushes one heartbeat every ~15s on every open WS. SPAs treat
      // missing heartbeats (not `/healthz` polling) as the primary unhealthy
      // signal. `orchestrator_ticks` increments monotonically so a stuck
      // main loop is detectable even when the WS itself stays alive.
      type: "heartbeat";
      server_time: string;
      orchestrator_ticks: number;
    };

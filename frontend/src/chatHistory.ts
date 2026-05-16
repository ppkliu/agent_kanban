/** Per-project chat-conversation history (Phase E3).
 *
 * Persisted to localStorage keyed by project_id so switching project also
 * swaps the visible chat transcript. Each entry records the user-supplied
 * goal plus the server response (parent task_id, number of subtasks created)
 * so the user has a click-to-prefill list of "what did I do recently in
 * this project".
 *
 * Capacity: PER_PROJECT_CAP entries per project, FIFO. Backend remains the
 * source of truth for the actual task graph — this module just remembers
 * the conversation prompts the operator typed.
 */

export interface ChatConversation {
  id: string;
  /** ISO timestamp of when the cards were created. */
  created_at: string;
  /** The original user-typed high-level goal. */
  goal: string;
  /** The server-issued parent task_id. */
  parent_task_id: string;
  /** Number of child subtasks created under that parent. */
  subtasks_created: number;
}

const STORAGE_PREFIX = "symphony.chatHistory.";
const PER_PROJECT_CAP = 20;

function storageKey(project_id: string): string {
  return `${STORAGE_PREFIX}${project_id}`;
}

export function listConversations(project_id: string): ChatConversation[] {
  if (typeof window === "undefined") return [];
  const raw = window.localStorage.getItem(storageKey(project_id));
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    // Filter out malformed entries defensively.
    return parsed.filter(
      (c): c is ChatConversation =>
        c != null &&
        typeof c.id === "string" &&
        typeof c.goal === "string" &&
        typeof c.parent_task_id === "string",
    );
  } catch {
    return [];
  }
}

export function appendConversation(
  project_id: string,
  convo: ChatConversation,
): ChatConversation[] {
  const existing = listConversations(project_id);
  const next = [convo, ...existing].slice(0, PER_PROJECT_CAP);
  if (typeof window !== "undefined") {
    window.localStorage.setItem(storageKey(project_id), JSON.stringify(next));
  }
  return next;
}

export function clearConversations(project_id: string): void {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(storageKey(project_id));
}

/** Generate a stable client-side id for a conversation. Server's
 * parent_task_id is also unique but this lets us key React lists even if
 * the same parent_task_id appears twice somehow (it shouldn't). */
export function newConversationId(): string {
  // crypto.randomUUID is widely available; fall back to time-based id.
  if (
    typeof crypto !== "undefined" &&
    typeof crypto.randomUUID === "function"
  ) {
    return crypto.randomUUID();
  }
  return `conv_${Date.now().toString(36)}_${Math.random()
    .toString(36)
    .slice(2, 8)}`;
}

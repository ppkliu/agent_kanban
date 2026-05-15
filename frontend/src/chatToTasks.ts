/** Decomposition prompt template + LLM response parser.
 *
 * The chat panel asks the LLM to break a high-level coding goal into 2-8
 * concrete subtasks, returned as a JSON array. LLMs are inconsistent
 * about strict format, so the parser strips markdown code fences and
 * leading / trailing prose before JSON.parse. It is fail-closed: on any
 * shape mismatch it returns an empty list plus a human-readable error
 * the caller can surface in a toast.
 */
import type { ChatMessage } from "./llmClient";

export interface SubTaskSpec {
  task: string;
  depends_on: number[];
}

const SYSTEM_PROMPT = `You are a planning assistant for Symphony, a coding-task queue.

Decompose the user's high-level goal into 2 to 8 concrete subtasks that an autonomous coding agent can execute one at a time. Each subtask should be self-contained and describe what to do, not how.

Return ONLY a JSON array, no prose, no markdown fence. Each entry must have:
  - "task": string — the subtask description
  - "depends_on": int[] — sibling indices (0-based) that must finish first. Forward references only (idx < this entry's index).

Example output for "Build a TODO API":
[
  {"task": "Define data model with title and done fields", "depends_on": []},
  {"task": "Implement CRUD endpoints using the model", "depends_on": [0]},
  {"task": "Add integration tests for each endpoint", "depends_on": [1]}
]`;

/** Build the {system, user} messages for chatCompletion. */
export function decompositionPrompt(goal: string): ChatMessage[] {
  return [
    { role: "system", content: SYSTEM_PROMPT },
    { role: "user", content: goal },
  ];
}

export interface ParseResult {
  tasks: SubTaskSpec[];
  error: string | null;
}

/** Robust parser for LLM-emitted subtask JSON.
 *
 * Strips wrapping markdown code fences (```json ... ``` or ``` ... ```).
 * Then locates the outermost JSON array via the first '[' and the last
 * matching ']' — tolerates leading "Here is the plan:" prose.
 * Validates each entry has a non-empty `task` string and an integer-
 * array `depends_on` (defaulting to []). Forward-only references are NOT
 * enforced here; the backend enforces them at submit time and surfaces a
 * clear 4xx if violated.
 */
export function parseDecomposition(raw: string): ParseResult {
  if (typeof raw !== "string" || raw.trim().length === 0) {
    return { tasks: [], error: "empty response from LLM" };
  }

  let text = raw.trim();

  // Strip ```json ... ``` or ``` ... ``` fences.
  const fence = /^```(?:json)?\s*\n?([\s\S]*?)\n?```$/i.exec(text);
  if (fence) text = fence[1].trim();

  // Find first '[' and last ']' — tolerates LLM preamble / trailing prose.
  const start = text.indexOf("[");
  const end = text.lastIndexOf("]");
  if (start === -1 || end === -1 || end < start) {
    return {
      tasks: [],
      error: "no JSON array found in LLM response",
    };
  }
  const slice = text.slice(start, end + 1);

  let parsed: unknown;
  try {
    parsed = JSON.parse(slice);
  } catch (e) {
    return {
      tasks: [],
      error: `JSON parse failed: ${(e as Error).message}`,
    };
  }

  if (!Array.isArray(parsed)) {
    return { tasks: [], error: "LLM response is not a JSON array" };
  }
  if (parsed.length === 0) {
    return { tasks: [], error: "LLM returned an empty subtask list" };
  }

  const tasks: SubTaskSpec[] = [];
  for (let i = 0; i < parsed.length; i++) {
    const item = parsed[i] as Record<string, unknown>;
    if (!item || typeof item !== "object") {
      return { tasks: [], error: `entry ${i} is not an object` };
    }
    const task = item.task;
    if (typeof task !== "string" || task.trim().length === 0) {
      return {
        tasks: [],
        error: `entry ${i} is missing a non-empty "task" string`,
      };
    }
    const depRaw = item.depends_on;
    let depends_on: number[] = [];
    if (depRaw !== undefined && depRaw !== null) {
      if (!Array.isArray(depRaw)) {
        return {
          tasks: [],
          error: `entry ${i}.depends_on is not an array`,
        };
      }
      for (const v of depRaw) {
        if (typeof v !== "number" || !Number.isInteger(v)) {
          return {
            tasks: [],
            error: `entry ${i}.depends_on contains a non-integer value`,
          };
        }
      }
      depends_on = depRaw as number[];
    }
    tasks.push({ task: task.trim(), depends_on });
  }

  return { tasks, error: null };
}

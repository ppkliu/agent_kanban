import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  appendConversation,
  clearConversations,
  listConversations,
  newConversationId,
  type ChatConversation,
} from "./chatHistory";

const KEYS = [
  "symphony.chatHistory.default",
  "symphony.chatHistory.proj_a",
  "symphony.chatHistory.proj_b",
];

function clearAll() {
  for (const k of KEYS) window.localStorage.removeItem(k);
}

function mkConvo(overrides: Partial<ChatConversation> = {}): ChatConversation {
  return {
    id: overrides.id ?? newConversationId(),
    created_at: overrides.created_at ?? new Date().toISOString(),
    goal: overrides.goal ?? "Build a TODO API",
    parent_task_id: overrides.parent_task_id ?? "tsk_abc123",
    subtasks_created: overrides.subtasks_created ?? 3,
  };
}

beforeEach(clearAll);
afterEach(clearAll);

describe("chatHistory", () => {
  it("returns an empty list when nothing is stored", () => {
    expect(listConversations("default")).toEqual([]);
  });

  it("appendConversation prepends entries (newest first)", () => {
    appendConversation("default", mkConvo({ goal: "first", parent_task_id: "tsk_1" }));
    appendConversation("default", mkConvo({ goal: "second", parent_task_id: "tsk_2" }));
    const list = listConversations("default");
    expect(list.map((c) => c.goal)).toEqual(["second", "first"]);
  });

  it("isolates conversations per project_id", () => {
    appendConversation("proj_a", mkConvo({ goal: "alpha thing" }));
    appendConversation("proj_b", mkConvo({ goal: "beta thing" }));
    expect(listConversations("proj_a").map((c) => c.goal)).toEqual([
      "alpha thing",
    ]);
    expect(listConversations("proj_b").map((c) => c.goal)).toEqual([
      "beta thing",
    ]);
    expect(listConversations("default")).toEqual([]);
  });

  it("caps each project at 20 entries (FIFO)", () => {
    for (let i = 0; i < 25; i++) {
      appendConversation("proj_a", mkConvo({ goal: `goal-${i}` }));
    }
    const list = listConversations("proj_a");
    expect(list).toHaveLength(20);
    // Newest is `goal-24`; oldest retained is `goal-5` (dropped 0..4).
    expect(list[0].goal).toBe("goal-24");
    expect(list[list.length - 1].goal).toBe("goal-5");
  });

  it("recovers gracefully when localStorage holds corrupt JSON", () => {
    window.localStorage.setItem("symphony.chatHistory.default", "{not-json");
    expect(listConversations("default")).toEqual([]);
  });

  it("filters out malformed entries silently", () => {
    window.localStorage.setItem(
      "symphony.chatHistory.default",
      JSON.stringify([
        { id: "x", goal: "ok", parent_task_id: "tsk_1" },
        { goal: "missing id" }, // invalid — dropped
        null, // invalid — dropped
        { id: "y", goal: "ok2", parent_task_id: "tsk_2" },
      ]),
    );
    const list = listConversations("default");
    expect(list.map((c) => c.id)).toEqual(["x", "y"]);
  });

  it("clearConversations wipes a single project's history", () => {
    appendConversation("proj_a", mkConvo());
    appendConversation("proj_b", mkConvo());
    clearConversations("proj_a");
    expect(listConversations("proj_a")).toEqual([]);
    // proj_b untouched
    expect(listConversations("proj_b")).toHaveLength(1);
  });

  it("newConversationId returns a non-empty string each call (no collisions in a tight loop)", () => {
    const ids = new Set<string>();
    for (let i = 0; i < 50; i++) ids.add(newConversationId());
    expect(ids.size).toBe(50);
  });
});

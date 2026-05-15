import { describe, expect, it } from "vitest";
import { decompositionPrompt, parseDecomposition } from "./chatToTasks";

describe("decompositionPrompt", () => {
  it("returns a system + user message pair", () => {
    const msgs = decompositionPrompt("Build a TODO API");
    expect(msgs).toHaveLength(2);
    expect(msgs[0].role).toBe("system");
    expect(msgs[1].role).toBe("user");
    expect(msgs[1].content).toBe("Build a TODO API");
  });
});

describe("parseDecomposition", () => {
  it("parses a clean JSON array", () => {
    const raw = '[{"task":"a","depends_on":[]},{"task":"b","depends_on":[0]}]';
    const { tasks, error } = parseDecomposition(raw);
    expect(error).toBeNull();
    expect(tasks).toEqual([
      { task: "a", depends_on: [] },
      { task: "b", depends_on: [0] },
    ]);
  });

  it("strips ```json fences", () => {
    const raw = '```json\n[{"task":"x"}]\n```';
    const { tasks, error } = parseDecomposition(raw);
    expect(error).toBeNull();
    expect(tasks).toEqual([{ task: "x", depends_on: [] }]);
  });

  it("tolerates LLM preamble before the array", () => {
    const raw =
      'Here is the plan:\n\n[{"task":"first","depends_on":[]}]\n\nLet me know!';
    const { tasks, error } = parseDecomposition(raw);
    expect(error).toBeNull();
    expect(tasks).toEqual([{ task: "first", depends_on: [] }]);
  });

  it("fails closed on malformed JSON", () => {
    const { tasks, error } = parseDecomposition("[not json");
    expect(tasks).toEqual([]);
    expect(error).toMatch(/no JSON array found|JSON parse failed/i);
  });

  it("fails closed on empty input", () => {
    const { tasks, error } = parseDecomposition("");
    expect(tasks).toEqual([]);
    expect(error).toMatch(/empty/i);
  });

  it("fails closed on non-array JSON", () => {
    const { tasks, error } = parseDecomposition('{"task":"x"}');
    expect(tasks).toEqual([]);
    expect(error).toMatch(/no JSON array/i);
  });

  it("rejects entries missing the task field", () => {
    const { tasks, error } = parseDecomposition('[{"depends_on":[]}]');
    expect(tasks).toEqual([]);
    expect(error).toMatch(/missing.*task/i);
  });

  it("rejects non-integer depends_on values", () => {
    const raw = '[{"task":"a","depends_on":[]},{"task":"b","depends_on":["0"]}]';
    const { tasks, error } = parseDecomposition(raw);
    expect(tasks).toEqual([]);
    expect(error).toMatch(/depends_on/i);
  });

  it("defaults missing depends_on to empty array", () => {
    const { tasks, error } = parseDecomposition('[{"task":"a"}]');
    expect(error).toBeNull();
    expect(tasks).toEqual([{ task: "a", depends_on: [] }]);
  });
});

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  DEFAULT_LLM_CONFIG,
  getStoredLLMConfig,
  resetLLMConfig,
  saveLLMConfig,
  type LLMConfig,
} from "./llmConfig";

const STORAGE_KEY = "symphony.llmConfig";

beforeEach(() => {
  window.localStorage.removeItem(STORAGE_KEY);
});

afterEach(() => {
  window.localStorage.removeItem(STORAGE_KEY);
});

describe("llmConfig", () => {
  it("returns Ollama / localhost defaults when nothing is stored", () => {
    const cfg = getStoredLLMConfig();
    expect(cfg).toEqual(DEFAULT_LLM_CONFIG);
    expect(cfg.provider).toBe("ollama");
    expect(cfg.base_url).toBe("http://localhost:11434");
  });

  it("roundtrips saveLLMConfig → getStoredLLMConfig", () => {
    const next: LLMConfig = {
      provider: "anthropic",
      base_url: "https://api.anthropic.com",
      model: "claude-opus-4-7",
      api_key: "sk-secret",
    };
    saveLLMConfig(next);
    expect(getStoredLLMConfig()).toEqual(next);
  });

  it("falls back to defaults when localStorage contains corrupt JSON", () => {
    window.localStorage.setItem(STORAGE_KEY, "not-json{");
    const cfg = getStoredLLMConfig();
    expect(cfg).toEqual(DEFAULT_LLM_CONFIG);
  });

  it("replaces unknown provider with default Ollama", () => {
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ provider: "google-vertex", base_url: "x" }),
    );
    const cfg = getStoredLLMConfig();
    expect(cfg.provider).toBe("ollama");
    expect(cfg.base_url).toBe("x"); // valid field preserved
  });

  it("resetLLMConfig writes defaults and returns them", () => {
    saveLLMConfig({
      provider: "openai-compatible",
      base_url: "http://x",
      model: "m",
      api_key: "k",
    });
    const fresh = resetLLMConfig();
    expect(fresh).toEqual(DEFAULT_LLM_CONFIG);
    expect(getStoredLLMConfig()).toEqual(DEFAULT_LLM_CONFIG);
  });
});

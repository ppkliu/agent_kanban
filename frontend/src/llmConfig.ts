/** Browser-side LLM endpoint config — persisted to localStorage.
 *
 * This is separate from the *backend* runner config (which lives in
 * WORKFLOW.md + .env on the server). It's used by the in-browser chat
 * panel that decomposes a high-level goal into subtasks before posting
 * them to the Tool API. Default is a local LLM endpoint (Ollama) so the
 * dashboard works out of the box without any cloud key.
 */

export type LLMProvider = "ollama" | "openai-compatible" | "anthropic";

export interface LLMConfig {
  provider: LLMProvider;
  base_url: string;
  model: string;
  api_key: string;
}

export const DEFAULT_LLM_CONFIG: LLMConfig = {
  provider: "ollama",
  base_url: "http://localhost:11434",
  model: "qwen2.5-coder:32b",
  api_key: "",
};

const STORAGE_KEY = "symphony.llmConfig";

function isValidProvider(v: unknown): v is LLMProvider {
  return v === "ollama" || v === "openai-compatible" || v === "anthropic";
}

export function getStoredLLMConfig(): LLMConfig {
  if (typeof window === "undefined") return { ...DEFAULT_LLM_CONFIG };
  const raw = window.localStorage.getItem(STORAGE_KEY);
  if (!raw) return { ...DEFAULT_LLM_CONFIG };
  try {
    const parsed = JSON.parse(raw) as Partial<LLMConfig>;
    return {
      provider: isValidProvider(parsed.provider)
        ? parsed.provider
        : DEFAULT_LLM_CONFIG.provider,
      base_url:
        typeof parsed.base_url === "string" && parsed.base_url.length > 0
          ? parsed.base_url
          : DEFAULT_LLM_CONFIG.base_url,
      model:
        typeof parsed.model === "string" && parsed.model.length > 0
          ? parsed.model
          : DEFAULT_LLM_CONFIG.model,
      api_key:
        typeof parsed.api_key === "string"
          ? parsed.api_key
          : DEFAULT_LLM_CONFIG.api_key,
    };
  } catch {
    return { ...DEFAULT_LLM_CONFIG };
  }
}

export function saveLLMConfig(cfg: LLMConfig): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(cfg));
}

export function resetLLMConfig(): LLMConfig {
  const fresh: LLMConfig = { ...DEFAULT_LLM_CONFIG };
  saveLLMConfig(fresh);
  return fresh;
}

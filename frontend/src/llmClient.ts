/** Browser-side LLM client used by the chat panel.
 *
 * Calls the user-configured LLM endpoint directly from the browser; the
 * dashboard backend isn't in the loop here. Provider-specific request /
 * response shapes are abstracted into a single `chatCompletion` function
 * that returns the assistant's plain text reply.
 */
import type { LLMConfig } from "./llmConfig";

export type ChatRole = "system" | "user" | "assistant";

export interface ChatMessage {
  role: ChatRole;
  content: string;
}

export interface ChatCompletionOptions {
  signal?: AbortSignal;
  /** Optional override; defaults to 4096 for Anthropic, ignored elsewhere. */
  max_tokens?: number;
}

/** One-shot chat completion. Returns the assistant text content. Throws on
 * any HTTP error or malformed response (caller surfaces via toast). */
export async function chatCompletion(
  messages: ChatMessage[],
  config: LLMConfig,
  opts: ChatCompletionOptions = {},
): Promise<string> {
  const base = config.base_url.replace(/\/+$/, "");

  if (config.provider === "ollama") {
    const r = await fetch(`${base}/api/chat`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      signal: opts.signal,
      body: JSON.stringify({
        model: config.model,
        messages,
        stream: false,
      }),
    });
    if (!r.ok) throw new Error(`ollama ${r.status}: ${await safeText(r)}`);
    const j = (await r.json()) as { message?: { content?: string } };
    const text = j.message?.content;
    if (typeof text !== "string")
      throw new Error("ollama response missing message.content");
    return text;
  }

  if (config.provider === "vllm" || config.provider === "openai-compatible") {
    // vLLM serves the OpenAI Messages API at /v1/chat/completions. The
    // request / response shapes are identical; we only branch labels in
    // the UI. vLLM typically runs without an API key but accepts one if set.
    const r = await fetch(`${base}/v1/chat/completions`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...(config.api_key
          ? { Authorization: `Bearer ${config.api_key}` }
          : {}),
      },
      signal: opts.signal,
      body: JSON.stringify({
        model: config.model,
        messages,
        stream: false,
      }),
    });
    if (!r.ok)
      throw new Error(`${config.provider} ${r.status}: ${await safeText(r)}`);
    const j = (await r.json()) as {
      choices?: Array<{ message?: { content?: string } }>;
    };
    const text = j.choices?.[0]?.message?.content;
    if (typeof text !== "string")
      throw new Error(
        `${config.provider} response missing choices[0].message.content`,
      );
    return text;
  }

  // Anthropic — `system` moves out of `messages` into a top-level field.
  const system = messages.find((m) => m.role === "system")?.content ?? "";
  const nonSystem = messages.filter((m) => m.role !== "system");
  const r = await fetch(`${base}/v1/messages`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "anthropic-version": "2023-06-01",
      ...(config.api_key ? { "x-api-key": config.api_key } : {}),
      // Required by Anthropic for browser-origin requests:
      "anthropic-dangerous-direct-browser-access": "true",
    },
    signal: opts.signal,
    body: JSON.stringify({
      model: config.model,
      system: system || undefined,
      messages: nonSystem,
      max_tokens: opts.max_tokens ?? 4096,
    }),
  });
  if (!r.ok) throw new Error(`anthropic ${r.status}: ${await safeText(r)}`);
  const j = (await r.json()) as {
    content?: Array<{ type?: string; text?: string }>;
  };
  const text = j.content?.find((b) => b.type === "text")?.text;
  if (typeof text !== "string")
    throw new Error("anthropic response missing content[].text");
  return text;
}

async function safeText(r: Response): Promise<string> {
  try {
    const t = await r.text();
    return t.slice(0, 200);
  } catch {
    return "(no body)";
  }
}

/** Minimal verification round-trip — asks the configured endpoint to reply
 * with one word so the UI can confirm "yes, I can reach this LLM". Used by
 * the LLMSettings modal's "Test connection" button. Bounded by a 15-second
 * AbortController so the UI never spins forever. */
export interface TestConnectionResult {
  ok: boolean;
  /** assistant text on success, error message on failure (truncated). */
  message: string;
}

export async function testConnection(
  config: LLMConfig,
): Promise<TestConnectionResult> {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 15_000);
  try {
    const text = await chatCompletion(
      [
        {
          role: "user",
          content: "Reply with only the two-letter word: OK",
        },
      ],
      config,
      { signal: ctrl.signal, max_tokens: 8 },
    );
    return { ok: true, message: text.trim().slice(0, 120) };
  } catch (err) {
    const e = err as Error;
    const msg =
      e.name === "AbortError" ? "request timed out (15s)" : e.message;
    return { ok: false, message: msg.slice(0, 240) };
  } finally {
    clearTimeout(t);
  }
}

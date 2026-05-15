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

  if (config.provider === "openai-compatible") {
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
    if (!r.ok) throw new Error(`openai ${r.status}: ${await safeText(r)}`);
    const j = (await r.json()) as {
      choices?: Array<{ message?: { content?: string } }>;
    };
    const text = j.choices?.[0]?.message?.content;
    if (typeof text !== "string")
      throw new Error("openai response missing choices[0].message.content");
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

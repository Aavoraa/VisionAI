import { ApiError, type Schemas } from "@/lib/api/client";

export type VisionResponse = Schemas["VisionResponse"];
export type ModelTask = Schemas["ModelTask"];

/** Answers to questions are short; task output (a whole page of OCR text) is long. */
export const DEFAULT_MAX_TOKENS = { question: "150", task: "1024" } as const;

/** Reading text is what task models (Florence-2) are mostly used for here. */
export function defaultTask(tasks: ModelTask[]): string {
  return tasks.find((t) => t.task === "<OCR>")?.task ?? tasks[0]?.task ?? "";
}

/** Structured task results (boxes, labels) arrive as JSON text and read best in monospace. */
export function looksLikeJson(text: string): boolean {
  const trimmed = text.trim();
  if (!/^[[{]/.test(trimmed)) return false;
  try {
    JSON.parse(trimmed);
    return true;
  } catch {
    return false;
  }
}

// Mirrors the API's limits (it re-validates everything).
export const ACCEPTED_IMAGE_TYPES = ["image/jpeg", "image/png", "image/webp", "image/gif"] as const;
export const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
export const PROMPT_MAX = 8000;
export const MAX_TOKENS_MIN = 1;
export const MAX_TOKENS_MAX = 4096;
export const API_KEY_PATTERN = /^sk_live_[A-Za-z0-9_-]{20,}$/;

export type PlaygroundInput = {
  apiKey: string;
  prompt: string;
  maxTokens: string;
  file: File | null;
};

export type PlaygroundErrors = Partial<Record<keyof PlaygroundInput, string>>;

export function validatePlayground(input: PlaygroundInput): PlaygroundErrors {
  const errors: PlaygroundErrors = {};
  const key = input.apiKey.trim();
  if (!key) errors.apiKey = "Paste an API key from the API keys page.";
  else if (!API_KEY_PATTERN.test(key)) errors.apiKey = "API keys start with sk_live_ followed by the secret.";

  const prompt = input.prompt.trim();
  if (!prompt) errors.prompt = "Ask a question about the image.";
  else if (prompt.length > PROMPT_MAX) errors.prompt = `Use at most ${PROMPT_MAX.toLocaleString("en-US")} characters.`;

  const tokens = Number(input.maxTokens);
  if (!Number.isInteger(tokens) || tokens < MAX_TOKENS_MIN || tokens > MAX_TOKENS_MAX) {
    errors.maxTokens = `Enter a whole number from ${MAX_TOKENS_MIN} to ${MAX_TOKENS_MAX.toLocaleString("en-US")}.`;
  }

  const fileError = input.file ? validateImageFile(input.file) : "Choose an image to analyse.";
  if (fileError) errors.file = fileError;
  return errors;
}

export function validateImageFile(file: Pick<File, "type" | "size">): string | null {
  if (!(ACCEPTED_IMAGE_TYPES as readonly string[]).includes(file.type)) return "Use a JPEG, PNG, WebP or GIF image.";
  if (file.size > MAX_IMAGE_BYTES) return "Images can be at most 10 MB.";
  if (file.size === 0) return "This file is empty.";
  return null;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatSeconds(ms: number): string {
  return `${(ms / 1000).toFixed(1)} s`;
}

/**
 * POST /v1/vision with an API key (not the session), exactly as an external client would.
 * Goes through the dashboard's /api rewrite, so no CORS is involved.
 */
export async function analyzeImage(
  input: { apiKey: string; prompt: string; maxTokens: number; file: File },
  options: { fetch?: typeof fetch; signal?: AbortSignal; baseUrl?: string } = {},
): Promise<VisionResponse> {
  const body = new FormData();
  body.append("prompt", input.prompt.trim());
  body.append("max_tokens", String(input.maxTokens));
  body.append("images", input.file, input.file.name);

  const doFetch = options.fetch ?? fetch;
  let response: Response;
  try {
    response = await doFetch(`${options.baseUrl ?? "/api"}/v1/vision`, {
      method: "POST",
      headers: { Authorization: `Bearer ${input.apiKey.trim()}` },
      body,
      signal: options.signal,
      credentials: "omit", // authenticate with the key only, like a real API client
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(0, "network_error", "Could not reach the API. Is it running?");
  }
  if (!response.ok) throw await ApiError.fromResponse(response);
  return (await response.json()) as VisionResponse;
}

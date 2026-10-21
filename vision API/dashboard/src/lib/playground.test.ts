import { describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/client";

import {
  analyzeImage,
  defaultTask,
  formatBytes,
  looksLikeJson,
  validateImageFile,
  validatePlayground,
} from "./playground";

const KEY = "sk_live_TEST_fake_key_for_tests_000001";
const png = () => new File([new Uint8Array([137, 80, 78, 71])], "shot.png", { type: "image/png" });
const VALID = { apiKey: KEY, prompt: "Describe", maxTokens: "150", file: png() };

const RESPONSE = {
  id: "vis_1",
  object: "vision.response",
  model: "HuggingFaceTB/SmolVLM-500M-Instruct",
  created: 1,
  result: { text: "A red circle." },
  usage: { input_tokens: 881, output_tokens: 5 },
};

describe("validatePlayground", () => {
  it("accepts a complete request", () => {
    expect(validatePlayground(VALID)).toEqual({});
    expect(validatePlayground({ ...VALID, apiKey: `  ${KEY}\n` })).toEqual({});
  });

  it("explains each missing or malformed field", () => {
    const errors = validatePlayground({ apiKey: "", prompt: "  ", maxTokens: "", file: null });
    expect(Object.keys(errors).sort()).toEqual(["apiKey", "file", "maxTokens", "prompt"]);
    expect(validatePlayground({ ...VALID, apiKey: "Bearer sk_live_abc" }).apiKey).toMatch(/start with sk_live_/);
    expect(validatePlayground({ ...VALID, prompt: "x".repeat(8001) }).prompt).toMatch(/8,000/);
  });

  it.each(["0", "4097", "1.5", "abc"])("rejects max tokens %s", (maxTokens) => {
    expect(validatePlayground({ ...VALID, maxTokens }).maxTokens).toMatch(/1 to 4,096/);
  });
});

describe("validateImageFile", () => {
  it("allows the API's image types up to 10 MB", () => {
    expect(validateImageFile({ type: "image/jpeg", size: 10 * 1024 * 1024 })).toBeNull();
    expect(validateImageFile({ type: "image/webp", size: 1 })).toBeNull();
  });

  it("rejects other types, oversized and empty files", () => {
    expect(validateImageFile({ type: "application/pdf", size: 10 })).toMatch(/JPEG, PNG/);
    expect(validateImageFile({ type: "image/png", size: 10 * 1024 * 1024 + 1 })).toMatch(/10 MB/);
    expect(validateImageFile({ type: "image/png", size: 0 })).toMatch(/empty/);
  });
});

it("defaults task models to reading text", () => {
  const caption = { task: "<CAPTION>", description: "Short description" };
  const ocr = { task: "<OCR>", description: "Read all text" };
  expect(defaultTask([caption, ocr])).toBe("<OCR>");
  expect(defaultTask([caption])).toBe("<CAPTION>");
  expect(defaultTask([])).toBe("");
});

it("recognises JSON task results", () => {
  expect(looksLikeJson(' {"bboxes": [[1, 2, 3, 4]], "labels": ["cat"]} ')).toBe(true);
  expect(looksLikeJson("[1, 2]")).toBe(true);
  expect(looksLikeJson("{not json")).toBe(false);
  expect(looksLikeJson("Tax Invoice")).toBe(false);
});

it("formats file sizes", () => {
  expect(formatBytes(512)).toBe("512 B");
  expect(formatBytes(267_469)).toBe("261 KB");
  expect(formatBytes(3 * 1024 * 1024)).toBe("3.0 MB");
});

describe("analyzeImage", () => {
  it("posts multipart form data with the key as a bearer token and no cookies", async () => {
    const fetchMock = vi.fn().mockResolvedValue(Response.json(RESPONSE));
    const file = png();
    const result = await analyzeImage({ apiKey: ` ${KEY} `, prompt: " What? ", maxTokens: 80, file }, { fetch: fetchMock });

    expect(result).toEqual(RESPONSE);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/vision");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("omit");
    expect(init.headers).toEqual({ Authorization: `Bearer ${KEY}` });
    const body = init.body as FormData;
    expect(body.get("prompt")).toBe("What?");
    expect(body.get("max_tokens")).toBe("80");
    expect((body.get("images") as File).name).toBe("shot.png");
  });

  it("turns API errors into ApiError with the request id", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json(
        { error: { type: "authentication_error", message: "Invalid API key", param: null }, request_id: "req-1" },
        { status: 401 },
      ),
    );
    const error = await analyzeImage({ apiKey: KEY, prompt: "x", maxTokens: 1, file: png() }, { fetch: fetchMock }).catch(
      (caught: unknown) => caught,
    );
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 401, message: "Invalid API key", requestId: "req-1" });
  });

  it("reports an unreachable API clearly", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(
      analyzeImage({ apiKey: KEY, prompt: "x", maxTokens: 1, file: png() }, { fetch: fetchMock }),
    ).rejects.toMatchObject({ type: "network_error", message: expect.stringMatching(/Could not reach the API/) });
  });

  it("lets cancellation through unchanged", async () => {
    const abort = new DOMException("aborted", "AbortError");
    const fetchMock = vi.fn().mockRejectedValue(abort);
    await expect(analyzeImage({ apiKey: KEY, prompt: "x", maxTokens: 1, file: png() }, { fetch: fetchMock })).rejects.toBe(abort);
  });
});

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ GET: vi.fn() }));
vi.mock("@/lib/api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...original, api };
});

import { apiResponse } from "@/test-utils";

import PlaygroundPage from "./page";

const KEY = "sk_live_TEST_fake_key_for_tests_000001";
const fetchMock = vi.fn();
const CHAT_MODEL = { name: "HuggingFaceTB/SmolVLM-500M-Instruct", tasks: null };
const TASK_MODEL = {
  name: "florence-community/Florence-2-large",
  tasks: [
    { task: "<CAPTION>", description: "Short description" },
    { task: "<OCR>", description: "Read all text in the image" },
    { task: "<OD>", description: "Detect objects with bounding boxes (JSON)" },
  ],
};

beforeEach(() => {
  api.GET.mockReset();
  api.GET.mockReturnValue(apiResponse(200, CHAT_MODEL));
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  // jsdom has no object URLs.
  URL.createObjectURL = vi.fn(() => "blob:preview");
  URL.revokeObjectURL = vi.fn();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function fillForm(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByText(`Model: ${CHAT_MODEL.name}`);
  await user.type(screen.getByLabelText("API key"), KEY);
  const file = new File([new Uint8Array([1, 2, 3])], "invoice.jpg", { type: "image/jpeg" });
  await user.upload(screen.getByLabelText(/Choose an image/), file);
  const question = screen.getByLabelText("Question");
  await user.clear(question);
  await user.type(question, "What is the invoice number?");
}

describe("Playground", () => {
  it("validates before sending anything", async () => {
    const user = userEvent.setup();
    render(<PlaygroundPage />);
    await screen.findByLabelText("Question");
    await user.click(screen.getByRole("button", { name: "Analyse image" }));

    expect(screen.getByText("Paste an API key from the API keys page.")).toBeInTheDocument();
    expect(screen.getByText("Choose an image to analyse.")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects unsupported files at selection time", async () => {
    const user = userEvent.setup({ applyAccept: false });
    render(<PlaygroundPage />);
    await user.upload(screen.getByLabelText(/Choose an image/), new File(["%PDF"], "doc.pdf", { type: "application/pdf" }));
    expect(screen.getByText("Use a JPEG, PNG, WebP or GIF image.")).toBeInTheDocument();
    expect(screen.queryByAltText("Selected image preview")).not.toBeInTheDocument();
  });

  it("shows the preview, sends the request and displays the answer", async () => {
    fetchMock.mockResolvedValue(
      Response.json({
        id: "vis_abc",
        object: "vision.response",
        model: "HuggingFaceTB/SmolVLM-500M-Instruct",
        created: 1,
        result: { text: "The invoice number is CR/134/26-27." },
        usage: { input_tokens: 881, output_tokens: 30 },
      }),
    );
    const user = userEvent.setup();
    render(<PlaygroundPage />);
    await fillForm(user);

    expect(screen.getByAltText("Selected image preview")).toHaveAttribute("src", "blob:preview");
    expect(screen.getByText("invoice.jpg")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Analyse image" }));

    expect(await screen.findByText("The invoice number is CR/134/26-27.")).toBeInTheDocument();
    expect(screen.getByText("HuggingFaceTB/SmolVLM-500M-Instruct")).toBeInTheDocument();
    expect(screen.getByText("881 / 30")).toBeInTheDocument();
    const body = (fetchMock.mock.calls[0] as [string, RequestInit])[1].body as FormData;
    expect(body.get("prompt")).toBe("What is the invoice number?");
    expect(body.get("max_tokens")).toBe("150");
  });

  it("flags the key field when the API rejects the key", async () => {
    fetchMock.mockResolvedValue(
      Response.json(
        { error: { type: "authentication_error", message: "Invalid or revoked API key", param: null }, request_id: "req-9" },
        { status: 401 },
      ),
    );
    const user = userEvent.setup();
    render(<PlaygroundPage />);
    await fillForm(user);
    await user.click(screen.getByRole("button", { name: "Analyse image" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid or revoked API key");
    expect(screen.getByText("Request ID: req-9")).toBeInTheDocument();
    expect(screen.getByText("This key is invalid, revoked or expired.")).toBeInTheDocument();
  });

  it("can stop waiting for a slow request", async () => {
    fetchMock.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
        }),
    );
    const user = userEvent.setup();
    render(<PlaygroundPage />);
    await fillForm(user);
    await user.click(screen.getByRole("button", { name: "Analyse image" }));

    expect(screen.getByText(/Analysing the image/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Stop waiting" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Stopped waiting"));
    expect(screen.getByRole("button", { name: "Analyse image" })).toBeEnabled();
  });

  it("keeps the key hidden unless asked", async () => {
    const user = userEvent.setup();
    render(<PlaygroundPage />);
    const input = screen.getByLabelText("API key");
    expect(input).toHaveAttribute("type", "password");
    await user.click(screen.getByRole("button", { name: "Show API key" }));
    expect(input).toHaveAttribute("type", "text");
  });

  it("offers the model's tasks instead of a question box for task models", async () => {
    api.GET.mockReturnValue(apiResponse(200, TASK_MODEL));
    fetchMock.mockResolvedValue(
      Response.json({
        id: "vis_ocr",
        object: "vision.response",
        model: TASK_MODEL.name,
        created: 1,
        result: { text: "Tax Invoice\nTotal 49,969.00" },
        usage: { input_tokens: 2935, output_tokens: 1948 },
      }),
    );
    const user = userEvent.setup();
    render(<PlaygroundPage />);

    const picker = await screen.findByLabelText("Task");
    expect(picker).toHaveValue("<OCR>"); // reading text is the default, not the first entry
    expect(screen.queryByLabelText("Question")).not.toBeInTheDocument();
    expect(screen.getByText(/Long pages are read in strips/)).toBeInTheDocument();
    expect(screen.getByLabelText("Max answer length (tokens)")).toHaveValue(1024);

    await user.type(screen.getByLabelText("API key"), KEY);
    await user.upload(
      screen.getByLabelText(/Choose an image/),
      new File([new Uint8Array([1])], "invoice.jpg", { type: "image/jpeg" }),
    );
    await user.click(screen.getByRole("button", { name: "Analyse image" }));

    expect(await screen.findByText(/Total 49,969.00/)).toBeInTheDocument();
    const body = (fetchMock.mock.calls[0] as [string, RequestInit])[1].body as FormData;
    expect(body.get("prompt")).toBe("<OCR>");
    expect(body.get("max_tokens")).toBe("1024");

    await user.selectOptions(picker, "<OD>");
    expect(screen.queryByText(/Long pages are read in strips/)).not.toBeInTheDocument();
  });

  it("falls back to a question box if the model cannot be looked up", async () => {
    api.GET.mockReturnValue(apiResponse(502));
    render(<PlaygroundPage />);
    expect(await screen.findByLabelText("Question")).toBeInTheDocument();
  });
});

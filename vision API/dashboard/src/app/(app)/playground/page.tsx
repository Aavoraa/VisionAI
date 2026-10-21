"use client";

import { ImageUp, Sparkles, X } from "lucide-react";
import { useEffect, useId, useRef, useState, type ChangeEvent, type DragEvent, type FormEvent } from "react";

import { ApiKeyField } from "@/components/api-key-field";
import { CopyButton } from "@/components/copy-button";
import { Alert, Button, Card, Field, PageHeader, Skeleton } from "@/components/ui";
import { ApiError, api, unwrap } from "@/lib/api/client";
import {
  ACCEPTED_IMAGE_TYPES,
  DEFAULT_MAX_TOKENS,
  MAX_TOKENS_MAX,
  MAX_TOKENS_MIN,
  PROMPT_MAX,
  analyzeImage,
  defaultTask,
  formatBytes,
  formatSeconds,
  looksLikeJson,
  validateImageFile,
  validatePlayground,
  type ModelTask,
  type PlaygroundErrors,
  type VisionResponse,
} from "@/lib/playground";
import { useApiQuery } from "@/lib/use-api";

type Outcome =
  | { kind: "success"; response: VisionResponse; elapsedMs: number }
  | { kind: "error"; message: string; requestId: string | null };

function ImagePicker({
  file,
  previewUrl,
  error,
  disabled,
  onSelect,
  onClear,
}: {
  file: File | null;
  previewUrl: string | null;
  error?: string;
  disabled: boolean;
  onSelect: (file: File) => void;
  onClear: () => void;
}) {
  const inputId = useId();
  const [dragging, setDragging] = useState(false);

  function onChange(event: ChangeEvent<HTMLInputElement>) {
    const selected = event.target.files?.[0];
    if (selected) onSelect(selected);
    event.target.value = ""; // choosing the same file again still fires change
  }

  function onDrop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    setDragging(false);
    const dropped = event.dataTransfer.files?.[0];
    if (dropped && !disabled) onSelect(dropped);
  }

  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-sm font-medium text-fg" id={`${inputId}-label`}>
        Image
      </span>
      {file && previewUrl ? (
        <div className="flex items-center gap-3 rounded-lg border border-border p-2">
          {/* eslint-disable-next-line @next/next/no-img-element -- local blob preview, nothing to optimise */}
          <img src={previewUrl} alt="Selected image preview" className="size-20 shrink-0 rounded-md bg-surface-muted object-cover" />
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-medium text-fg">{file.name}</p>
            <p className="text-xs text-muted">{formatBytes(file.size)}</p>
          </div>
          <Button variant="ghost" onClick={onClear} disabled={disabled} aria-label={`Remove ${file.name}`}>
            <X aria-hidden className="size-4" />
          </Button>
        </div>
      ) : (
        <label
          htmlFor={inputId}
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          className={[
            "flex cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed px-4 py-8 text-center",
            "transition-colors duration-150 focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-primary",
            dragging ? "border-primary bg-primary-soft" : error ? "border-danger" : "border-border hover:border-muted",
          ].join(" ")}
        >
          <ImageUp aria-hidden className="size-6 text-muted" />
          <span className="text-sm text-fg">
            <span className="font-medium text-primary">Choose an image</span> or drag it here
          </span>
          <span className="text-xs text-muted">JPEG, PNG, WebP or GIF, up to 10 MB</span>
          <input
            id={inputId}
            type="file"
            accept={ACCEPTED_IMAGE_TYPES.join(",")}
            className="sr-only"
            onChange={onChange}
            disabled={disabled}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? `${inputId}-error` : undefined}
          />
        </label>
      )}
      {error && (
        <p id={`${inputId}-error`} className="text-xs text-danger">
          {error}
        </p>
      )}
    </div>
  );
}

function ResultPanel({ running, elapsedMs, outcome }: { running: boolean; elapsedMs: number; outcome: Outcome | null }) {
  if (running) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 py-12 text-center">
        <Sparkles aria-hidden className="size-6 animate-pulse text-primary" />
        <p className="text-sm font-medium text-fg">
          Analysing the image…{" "}
          {/* Ticking clock is visual only: the live region would otherwise re-announce it constantly. */}
          <span aria-hidden className="font-mono tabular-nums">
            {formatSeconds(elapsedMs)}
          </span>
        </p>
        <p className="max-w-xs text-xs text-muted">
          On a CPU this takes from about 30 seconds to a few minutes. With a GPU it takes a few seconds.
        </p>
      </div>
    );
  }
  if (!outcome) {
    return (
      <p className="py-12 text-center text-sm text-muted">
        The model&apos;s answer appears here after you run a request.
      </p>
    );
  }
  if (outcome.kind === "error") {
    return (
      <Alert>
        {outcome.message}
        {outcome.requestId && <span className="mt-1 block font-mono text-xs">Request ID: {outcome.requestId}</span>}
      </Alert>
    );
  }

  const { response, elapsedMs: took } = outcome;
  return (
    <div className="flex flex-col gap-4">
      <div className="max-h-[28rem] overflow-y-auto rounded-lg bg-surface-muted p-4">
        <p
          className={[
            "whitespace-pre-wrap break-words text-sm leading-relaxed text-fg",
            looksLikeJson(response.result.text) ? "font-mono text-xs" : "",
          ].join(" ")}
        >
          {response.result.text || "(empty answer)"}
        </p>
      </div>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-4">
        <div className="col-span-2 min-w-0">
          <dt className="text-xs text-muted">Model</dt>
          <dd className="break-all font-mono text-fg">{response.model}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted">Time</dt>
          <dd className="font-mono tabular-nums text-fg">{formatSeconds(took)}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted">Tokens in / out</dt>
          <dd className="font-mono tabular-nums text-fg">
            {response.usage.input_tokens} / {response.usage.output_tokens}
          </dd>
        </div>
      </dl>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-mono text-xs text-muted">{response.id}</span>
        <CopyButton value={response.result.text} label="Copy answer" />
      </div>
    </div>
  );
}

function TaskPicker({
  tasks,
  value,
  onChange,
  disabled,
}: {
  tasks: ModelTask[];
  value: string;
  onChange: (task: string) => void;
  disabled: boolean;
}) {
  const id = useId();
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-sm font-medium text-fg">
        Task
      </label>
      <select
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled}
        aria-describedby={`${id}-hint`}
        className="min-h-10 cursor-pointer rounded-lg border border-border bg-surface px-3 text-base text-fg hover:border-muted sm:text-sm"
      >
        {tasks.map((t) => (
          <option key={t.task} value={t.task}>
            {t.description} ({t.task})
          </option>
        ))}
      </select>
      <p id={`${id}-hint`} className="text-xs text-muted">
        This model performs fixed tasks instead of answering questions.
        {value === "<OCR>" && " Long pages are read in strips, so this can take a few minutes on a CPU."}
      </p>
    </div>
  );
}

export default function PlaygroundPage() {
  const model = useApiQuery(() => unwrap(api.GET("/v1/dashboard/model")));
  const tasks = model.data?.tasks ?? null;

  const [apiKey, setApiKey] = useState("");

  const [prompt, setPrompt] = useState("Describe this image.");
  // null = the default for the model's mode, until the user picks something.
  const [chosenTask, setChosenTask] = useState<string | null>(null);
  const [chosenMaxTokens, setChosenMaxTokens] = useState<string | null>(null);
  const task = tasks ? (chosenTask ?? defaultTask(tasks)) : null;
  const maxTokens = chosenMaxTokens ?? (tasks ? DEFAULT_MAX_TOKENS.task : DEFAULT_MAX_TOKENS.question);
  const effectivePrompt = task ?? prompt;
  const [file, setFile] = useState<File | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [errors, setErrors] = useState<PlaygroundErrors>({});
  const [running, setRunning] = useState(false);
  const [elapsedMs, setElapsedMs] = useState(0);
  const [outcome, setOutcome] = useState<Outcome | null>(null);

  const promptId = useId();
  const previewRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // On leaving the page: release the preview and stop waiting for a pending request.
  useEffect(
    () => () => {
      if (previewRef.current) URL.revokeObjectURL(previewRef.current);
      abortRef.current?.abort();
      if (timerRef.current) clearInterval(timerRef.current);
    },
    [],
  );

  function replacePreview(next: File | null) {
    if (previewRef.current) URL.revokeObjectURL(previewRef.current);
    previewRef.current = next ? URL.createObjectURL(next) : null;
    setPreviewUrl(previewRef.current);
    setFile(next);
  }

  function onSelectFile(selected: File) {
    const fileError = validateImageFile(selected);
    setErrors((current) => ({ ...current, file: fileError ?? undefined }));
    replacePreview(fileError ? null : selected);
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const found = validatePlayground({ apiKey, prompt: effectivePrompt, maxTokens, file });
    setErrors(found);
    if (Object.keys(found).length > 0 || !file) return;

    const controller = new AbortController();
    abortRef.current = controller;
    const started = performance.now();
    setElapsedMs(0);
    setOutcome(null);
    setRunning(true);
    timerRef.current = setInterval(() => setElapsedMs(performance.now() - started), 200);

    try {
      const response = await analyzeImage(
        { apiKey, prompt: effectivePrompt, maxTokens: Number(maxTokens), file },
        { signal: controller.signal },
      );
      setOutcome({ kind: "success", response, elapsedMs: performance.now() - started });
    } catch (error) {
      if (controller.signal.aborted) {
        setOutcome({ kind: "error", message: "Stopped waiting. The server may still finish (and bill) the request.", requestId: null });
      } else if (error instanceof ApiError) {
        if (error.status === 401) setErrors((current) => ({ ...current, apiKey: "This key is invalid, revoked or expired." }));
        setOutcome({ kind: "error", message: error.message, requestId: error.requestId });
      } else {
        setOutcome({ kind: "error", message: "Something went wrong. Please try again.", requestId: null });
      }
    } finally {
      if (timerRef.current) clearInterval(timerRef.current);
      timerRef.current = null;
      abortRef.current = null;
      setRunning(false);
    }
  }

  return (
    <>
      <PageHeader
        title="Playground"
        description="Try the vision model with one of your API keys: upload an image, ask a question (or pick a task) and read the result. Requests are billed like any other API call."
      />
      <div className="grid gap-6 lg:grid-cols-2 lg:items-start">
        <Card
          title="Request"
          description={model.data ? `Model: ${model.data.name}` : undefined}
        >
          <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
            <ApiKeyField value={apiKey} onChange={setApiKey} error={errors.apiKey} disabled={running} />

            <ImagePicker
              file={file}
              previewUrl={previewUrl}
              error={errors.file}
              disabled={running}
              onSelect={onSelectFile}
              onClear={() => replacePreview(null)}
            />

            {model.status === "loading" ? (
              <Skeleton className="h-[5.5rem]" />
            ) : tasks && task !== null ? (
              <TaskPicker tasks={tasks} value={task} onChange={setChosenTask} disabled={running} />
            ) : (
              <div className="flex flex-col gap-1.5">
                <label htmlFor={promptId} className="text-sm font-medium text-fg">
                  Question
                </label>
                <textarea
                  id={promptId}
                  rows={3}
                  maxLength={PROMPT_MAX}
                  value={prompt}
                  onChange={(event) => setPrompt(event.target.value)}
                  disabled={running}
                  aria-invalid={errors.prompt ? true : undefined}
                  aria-describedby={errors.prompt ? `${promptId}-error` : `${promptId}-hint`}
                  className={[
                    "rounded-lg border bg-surface px-3 py-2 text-base text-fg sm:text-sm",
                    "transition-colors duration-150",
                    errors.prompt ? "border-danger" : "border-border hover:border-muted",
                  ].join(" ")}
                />
                {errors.prompt ? (
                  <p id={`${promptId}-error`} className="text-xs text-danger">
                    {errors.prompt}
                  </p>
                ) : (
                  <p id={`${promptId}-hint`} className="text-xs text-muted">
                    Short, specific questions work best, e.g. &ldquo;What is the invoice number?&rdquo;
                  </p>
                )}
              </div>
            )}

            <Field
              className="max-w-40"
              label="Max answer length (tokens)"
              type="number"
              inputMode="numeric"
              min={MAX_TOKENS_MIN}
              max={MAX_TOKENS_MAX}
              value={maxTokens}
              onChange={(event) => setChosenMaxTokens(event.target.value)}
              error={errors.maxTokens}
              disabled={running}
            />

            <div className="flex flex-wrap gap-2">
              <Button type="submit" loading={running}>
                {running ? "Analysing…" : "Analyse image"}
              </Button>
              {running && (
                <Button variant="secondary" onClick={() => abortRef.current?.abort()}>
                  Stop waiting
                </Button>
              )}
            </div>
          </form>
        </Card>

        <Card title="Result">
          <div aria-live="polite">
            <ResultPanel running={running} elapsedMs={elapsedMs} outcome={outcome} />
          </div>
        </Card>
      </div>
    </>
  );
}

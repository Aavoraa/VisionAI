"use client";

import { ArrowLeft, FileText, FileUp, X } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useId, useRef, useState, type ChangeEvent, type DragEvent, type FormEvent } from "react";

import { ApiKeyField } from "@/components/api-key-field";
import { Alert, Button, Card, PageHeader, Skeleton } from "@/components/ui";
import { ApiError, api, unwrap } from "@/lib/api/client";
import { ACCEPTED_DOCUMENT_TYPES, startInvoiceExtraction, validateApiKey, validateInvoiceFile } from "@/lib/invoices";
import { formatBytes } from "@/lib/playground";
import { useApiQuery } from "@/lib/use-api";

type Errors = { apiKey?: string; file?: string };

function DocumentPicker({
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
    event.target.value = "";
  }

  function onDrop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    setDragging(false);
    const dropped = event.dataTransfer.files?.[0];
    if (dropped && !disabled) onSelect(dropped);
  }

  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-sm font-medium text-fg">Invoice</span>
      {file ? (
        <div className="flex items-center gap-3 rounded-lg border border-border p-2">
          {previewUrl ? (
            // eslint-disable-next-line @next/next/no-img-element -- local blob preview, nothing to optimise
            <img src={previewUrl} alt="Selected invoice preview" className="size-20 shrink-0 rounded-md bg-surface-muted object-cover" />
          ) : (
            <div className="flex size-20 shrink-0 items-center justify-center rounded-md bg-surface-muted">
              <FileText aria-hidden className="size-8 text-muted" />
            </div>
          )}
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-medium text-fg">{file.name}</p>
            <p className="text-xs text-muted">
              {file.type === "application/pdf" ? "PDF" : "Image"} · {formatBytes(file.size)}
            </p>
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
            "flex cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed px-4 py-10 text-center",
            "transition-colors duration-150 focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-primary",
            dragging ? "border-primary bg-primary-soft" : error ? "border-danger" : "border-border hover:border-muted",
          ].join(" ")}
        >
          <FileUp aria-hidden className="size-6 text-muted" />
          <span className="text-sm text-fg">
            <span className="font-medium text-primary">Choose an invoice</span> or drag it here
          </span>
          <span className="text-xs text-muted">PDF (up to 10 pages) or JPEG, PNG, WebP, GIF · up to 20 MB</span>
          <input
            id={inputId}
            type="file"
            accept={ACCEPTED_DOCUMENT_TYPES.join(",")}
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

export default function NewInvoicePage() {
  const router = useRouter();
  const model = useApiQuery(() => unwrap(api.GET("/v1/dashboard/model")));
  const [apiKey, setApiKey] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [errors, setErrors] = useState<Errors>({});
  const [uploading, setUploading] = useState(false);
  const [failure, setFailure] = useState<{ message: string; requestId: string | null } | null>(null);
  const previewRef = useRef<string | null>(null);

  useEffect(
    () => () => {
      if (previewRef.current) URL.revokeObjectURL(previewRef.current);
    },
    [],
  );

  function replaceFile(next: File | null) {
    if (previewRef.current) URL.revokeObjectURL(previewRef.current);
    previewRef.current = next && next.type.startsWith("image/") ? URL.createObjectURL(next) : null;
    setPreviewUrl(previewRef.current);
    setFile(next);
  }

  function onSelectFile(selected: File) {
    const fileError = validateInvoiceFile(selected);
    setErrors((current) => ({ ...current, file: fileError ?? undefined }));
    replaceFile(fileError ? null : selected);
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const found: Errors = {};
    const keyError = validateApiKey(apiKey);
    if (keyError) found.apiKey = keyError;
    if (!file) found.file = "Choose an invoice to read.";
    setErrors(found);
    if (Object.keys(found).length > 0 || !file) return;

    setUploading(true);
    setFailure(null);
    try {
      // Reading runs in the background; the review page shows progress and the result.
      const accepted = await startInvoiceExtraction({ apiKey, file });
      router.push(`/invoices/${accepted.id}`);
    } catch (error) {
      if (error instanceof ApiError) {
        if (error.status === 401) setErrors((current) => ({ ...current, apiKey: "This key is invalid, revoked or expired." }));
        setFailure({ message: error.message, requestId: error.requestId });
      } else {
        setFailure({ message: "Something went wrong. Please try again.", requestId: null });
      }
      setUploading(false);
    }
  }

  const unavailable = model.status === "success" && !model.data.invoice_extraction;

  return (
    <>
      <Link href="/invoices" className="mb-2 inline-flex items-center gap-1 text-sm text-muted hover:text-fg">
        <ArrowLeft aria-hidden className="size-4" /> Invoices
      </Link>
      <PageHeader
        title="Extract an invoice"
        description="Upload an invoice; its fields are read in the background and you land on the review page, which fills in when reading is done (a few minutes per page on a CPU, seconds on a GPU). Billed like an API request."
      />
      <Card title="Invoice" description={model.data ? `Model: ${model.data.name}` : undefined} className="max-w-xl">
        {model.status === "loading" ? (
          <Skeleton className="h-64" />
        ) : unavailable ? (
          <Alert>
            The serving model ({model.data?.name}) cannot read invoices. Start the API with PaddleOCR-VL (see
            HOW_TO_RUN.txt, &ldquo;Invoice extraction&rdquo;).
          </Alert>
        ) : (
          <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
            <ApiKeyField value={apiKey} onChange={setApiKey} error={errors.apiKey} disabled={uploading} />
            <DocumentPicker
              file={file}
              previewUrl={previewUrl}
              error={errors.file}
              disabled={uploading}
              onSelect={onSelectFile}
              onClear={() => replaceFile(null)}
            />
            {failure && (
              <Alert>
                {failure.message}
                {failure.requestId && <span className="mt-1 block font-mono text-xs">Request ID: {failure.requestId}</span>}
              </Alert>
            )}
            <div>
              <Button type="submit" loading={uploading}>
                {uploading ? "Uploading…" : "Extract fields"}
              </Button>
            </div>
          </form>
        )}
      </Card>
    </>
  );
}

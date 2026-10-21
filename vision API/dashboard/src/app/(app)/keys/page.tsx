"use client";

import { KeyRound, Pencil, Plus, RefreshCw, TriangleAlert } from "lucide-react";
import { useState, type FormEvent } from "react";

import { CopyButton } from "@/components/copy-button";
import { Dialog } from "@/components/dialog";
import { Alert, Badge, Button, Card, DataTable, Field, PageHeader, Skeleton, type Column } from "@/components/ui";
import { ApiError, api, unwrap, type ApiKey, type ApiKeyCreated } from "@/lib/api/client";
import { formatDate } from "@/lib/format";
import { EXPIRY_OPTIONS, keyStatus, validateKeyName } from "@/lib/keys";
import { useApiQuery } from "@/lib/use-api";

const STATUS_BADGE = {
  active: <Badge tone="success">Active</Badge>,
  expired: <Badge tone="warning">Expired</Badge>,
  revoked: <Badge tone="neutral">Revoked</Badge>,
};

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

// ---------------------------------------------------------------------------
// Create
// ---------------------------------------------------------------------------

function CreateKeyDialog({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: () => void }) {
  const [name, setName] = useState("");
  const [expiry, setExpiry] = useState("never");
  const [nameError, setNameError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);

  function close() {
    setName("");
    setExpiry("never");
    setNameError(null);
    setFormError(null);
    setCreated(null);
    onClose();
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    const problem = validateKeyName(name);
    setNameError(problem);
    if (problem) return;

    setSaving(true);
    try {
      const days = EXPIRY_OPTIONS.find((option) => option.value === expiry)?.days ?? null;
      const result = await unwrap(
        api.POST("/v1/dashboard/keys", { body: { name: name.trim(), expires_in_days: days } }),
      );
      setCreated(result);
      onCreated();
    } catch (error) {
      if (error instanceof ApiError && error.param === "name") setNameError(error.message);
      else setFormError(errorMessage(error, "Could not create the key."));
    } finally {
      setSaving(false);
    }
  }

  // Distinct keys: the reveal step is a new dialog, so focus moves into it (to "I've saved it")
  // instead of being lost when the form, and the button that had focus, unmount.
  if (created) {
    return (
      <Dialog
        key="reveal"
        open={open}
        title="Save your API key"
        description="This is the only time the full key is shown. Store it in a password manager or secret store now."
        onClose={close}
        dismissible={false}
      >
        <div className="flex flex-col gap-4">
          <div className="flex items-start gap-2 rounded-lg bg-accent-soft px-3 py-2.5 text-sm text-fg">
            <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-fg" />
            <p>If you lose it, revoke it and create a new one. We only store a hash and cannot show it again.</p>
          </div>
          <div>
            <p className="mb-1.5 text-sm font-medium text-fg">{created.key.name}</p>
            <div className="flex flex-wrap items-center gap-2">
              <code
                data-testid="new-secret"
                className="min-w-0 flex-1 break-all rounded-lg border border-border bg-surface-muted px-3 py-2 font-mono text-sm text-fg select-all"
              >
                {created.secret}
              </code>
              <CopyButton value={created.secret} />
            </div>
          </div>
          <Button onClick={close} data-autofocus className="self-end">
            I&apos;ve saved it
          </Button>
        </div>
      </Dialog>
    );
  }

  return (
    <Dialog key="form" open={open} title="Create API key" onClose={close} dismissible={!saving}>
      <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
        {formError && <Alert>{formError}</Alert>}
        <Field
          label="Name"
          value={name}
          maxLength={100}
          placeholder="e.g. production server"
          onChange={(event) => setName(event.target.value)}
          hint="Helps you recognise the key later."
          error={nameError}
          data-autofocus
        />
        <div className="flex flex-col gap-1.5">
          <label htmlFor="key-expiry" className="text-sm font-medium text-fg">
            Expires
          </label>
          <select
            id="key-expiry"
            value={expiry}
            onChange={(event) => setExpiry(event.target.value)}
            className="min-h-10 cursor-pointer rounded-lg border border-border bg-surface px-3 text-base text-fg sm:text-sm"
          >
            {EXPIRY_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
        <div className="mt-2 flex justify-end gap-2">
          <Button variant="secondary" onClick={close} disabled={saving}>
            Cancel
          </Button>
          <Button type="submit" loading={saving}>
            Create key
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Rename / revoke
// ---------------------------------------------------------------------------

function RenameKeyDialog({ apiKey, onClose, onDone }: { apiKey: ApiKey | null; onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState(apiKey?.name ?? "");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!apiKey) return;
    const problem = validateKeyName(name);
    setError(problem);
    if (problem) return;
    setSaving(true);
    try {
      await unwrap(
        api.PATCH("/v1/dashboard/keys/{key_id}", { params: { path: { key_id: apiKey.id } }, body: { name: name.trim() } }),
      );
      onDone();
    } catch (err) {
      setError(errorMessage(err, "Could not rename the key."));
      setSaving(false);
    }
  }

  return (
    <Dialog open={apiKey !== null} title="Rename API key" onClose={onClose} dismissible={!saving}>
      <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
        <Field label="Name" value={name} maxLength={100} onChange={(e) => setName(e.target.value)} error={error} data-autofocus />
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button type="submit" loading={saving}>
            Save
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function RevokeKeyDialog({ apiKey, onClose, onDone }: { apiKey: ApiKey | null; onClose: () => void; onDone: () => void }) {
  const [error, setError] = useState<string | null>(null);
  const [revoking, setRevoking] = useState(false);

  async function revoke() {
    if (!apiKey) return;
    setRevoking(true);
    setError(null);
    try {
      await unwrap(api.DELETE("/v1/dashboard/keys/{key_id}", { params: { path: { key_id: apiKey.id } } }));
      onDone();
    } catch (err) {
      setError(errorMessage(err, "Could not revoke the key."));
      setRevoking(false);
    }
  }

  return (
    <Dialog
      open={apiKey !== null}
      title="Revoke API key?"
      description={
        <>
          <span className="font-medium text-fg">{apiKey?.name}</span> ({apiKey?.prefix}…) stops working immediately.
          Any application using it will get authentication errors. This cannot be undone.
        </>
      }
      onClose={onClose}
      dismissible={!revoking}
    >
      <div className="flex flex-col gap-4">
        {error && <Alert>{error}</Alert>}
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose} disabled={revoking} data-autofocus>
            Cancel
          </Button>
          <Button variant="danger" onClick={revoke} loading={revoking}>
            Revoke key
          </Button>
        </div>
      </div>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function KeysPage() {
  const keys = useApiQuery(() => unwrap(api.GET("/v1/dashboard/keys")));
  const [creating, setCreating] = useState(false);
  const [renaming, setRenaming] = useState<ApiKey | null>(null);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);

  const columns: Column<ApiKey>[] = [
    {
      header: "Name",
      cell: (key) => (
        <div className="min-w-32">
          <p className="font-medium">{key.name}</p>
          <p className="font-mono text-xs text-muted">{key.prefix}…</p>
        </div>
      ),
    },
    { header: "Status", cell: (key) => STATUS_BADGE[keyStatus(key)] },
    {
      header: "Last used",
      cell: (key) =>
        key.last_used_at ? formatDate(key.last_used_at, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "Never",
    },
    { header: "Expires", cell: (key) => (key.expires_at ? formatDate(key.expires_at) : "Never"), hideBelow: "lg" },
    { header: "Created", cell: (key) => formatDate(key.created_at), hideBelow: "xl" },
    {
      header: "Actions",
      align: "right",
      cell: (key) => (
        <div className="flex justify-end gap-1 font-sans">
          <Button variant="ghost" className="px-2" aria-label={`Rename ${key.name}`} onClick={() => setRenaming(key)}>
            <Pencil aria-hidden className="size-4" />
          </Button>
          {/* Fixed-width slot keeps the rename buttons aligned on rows without a Revoke action. */}
          <span className="inline-flex w-20 justify-end">
            {key.is_active && (
              <Button
                variant="ghost"
                className="px-3 text-danger hover:text-danger"
                aria-label={`Revoke ${key.name}`}
                onClick={() => setRevoking(key)}
              >
                Revoke
              </Button>
            )}
          </span>
        </div>
      ),
    },
  ];

  return (
    <>
      <PageHeader
        title="API keys"
        description="Keys authenticate your applications. Keep them secret; anyone with a key can spend from your account."
        action={
          <Button onClick={() => setCreating(true)}>
            <Plus aria-hidden className="size-4" />
            Create key
          </Button>
        }
      />

      {keys.status === "loading" && <Skeleton className="h-56" />}
      {keys.status === "error" && (
        <div className="flex flex-col items-start gap-3">
          <Alert>Could not load your keys: {keys.error.message}</Alert>
          <Button variant="secondary" onClick={keys.reload}>
            <RefreshCw aria-hidden className="size-4" />
            Try again
          </Button>
        </div>
      )}
      {keys.status === "success" && (
        <Card title="Your keys" description="Active keys first. Revoked keys are kept so their usage history stays visible.">
          <DataTable
            caption="Your API keys"
            columns={columns}
            rows={keys.data.items}
            rowKey={(key) => key.id}
            empty={
              <span className="flex flex-col items-center gap-2 py-6">
                <KeyRound aria-hidden className="size-8 text-muted" />
                <span className="font-medium text-fg">No API keys yet</span>
                <span>Create one to start calling the API.</span>
              </span>
            }
          />
        </Card>
      )}

      <CreateKeyDialog open={creating} onClose={() => setCreating(false)} onCreated={keys.reload} />
      {/* `key` remounts the dialog per key so its form starts from that key's name. */}
      <RenameKeyDialog
        key={renaming?.id ?? "none"}
        apiKey={renaming}
        onClose={() => setRenaming(null)}
        onDone={() => {
          setRenaming(null);
          keys.reload();
        }}
      />
      <RevokeKeyDialog
        key={`revoke-${revoking?.id ?? "none"}`}
        apiKey={revoking}
        onClose={() => setRevoking(null)}
        onDone={() => {
          setRevoking(null);
          keys.reload();
        }}
      />
    </>
  );
}

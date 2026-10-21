"use client";

import { ChevronLeft, ChevronRight, RefreshCw, Search } from "lucide-react";
import { useState } from "react";

import { Dialog } from "@/components/dialog";
import { Alert, Badge, Button, Card, DataTable, PageHeader, Skeleton, type Column } from "@/components/ui";
import { api, unwrap, type AdminUser, type ApiKey, type Plan } from "@/lib/api/client";
import { formatDate, formatNumber } from "@/lib/format";
import { keyStatus } from "@/lib/keys";
import { useCurrentUser } from "@/lib/session";
import { useApiQuery } from "@/lib/use-api";
import { useDebounced } from "@/lib/use-debounced";

const PAGE_SIZE = 25;

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong.";
}

// ---------------------------------------------------------------------------
// Manage one user
// ---------------------------------------------------------------------------

function UserKeys({ user }: { user: AdminUser }) {
  const keys = useApiQuery(() =>
    unwrap(api.GET("/v1/admin/users/{user_id}/keys", { params: { path: { user_id: user.id } } })),
  );
  const [confirming, setConfirming] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function revoke(key: ApiKey) {
    setError(null);
    try {
      await unwrap(api.DELETE("/v1/admin/keys/{key_id}", { params: { path: { key_id: key.id } } }));
      setConfirming(null);
      keys.reload();
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  if (keys.status === "loading") return <Skeleton className="h-16" />;
  if (keys.status === "error") return <Alert>Could not load keys: {keys.error.message}</Alert>;
  if (keys.data.items.length === 0) return <p className="text-sm text-muted">No API keys.</p>;
  return (
    <div className="flex flex-col gap-2">
      {error && <Alert>{error}</Alert>}
      <ul className="divide-y divide-border rounded-lg border border-border">
        {keys.data.items.map((key) => {
          const status = keyStatus(key);
          return (
            <li key={key.id} className="flex items-center justify-between gap-3 px-3 py-2 text-sm">
              <div className="min-w-0">
                <p className="truncate font-medium text-fg">{key.name}</p>
                <p className="font-mono text-xs text-muted">
                  {key.prefix}… · {status === "active" ? "active" : status}
                </p>
              </div>
              {key.is_active &&
                (confirming === key.id ? (
                  <div className="flex shrink-0 gap-1">
                    <Button variant="ghost" className="px-2" onClick={() => setConfirming(null)}>
                      Cancel
                    </Button>
                    <Button variant="danger" className="px-3" onClick={() => void revoke(key)}>
                      Confirm revoke
                    </Button>
                  </div>
                ) : (
                  <Button
                    variant="ghost"
                    className="shrink-0 px-3 text-danger hover:text-danger"
                    aria-label={`Revoke ${key.name}`}
                    onClick={() => setConfirming(key.id)}
                  >
                    Revoke
                  </Button>
                ))}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function ManageUserDialog({
  user,
  plans,
  onClose,
  onChanged,
}: {
  user: AdminUser | null;
  plans: Plan[];
  onClose: () => void;
  onChanged: (updated: AdminUser) => void;
}) {
  const me = useCurrentUser();
  const [planId, setPlanId] = useState(user?.plan_id ?? "");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  if (!user) return null;
  const isSelf = user.id === me.id;
  const defaultPlan = plans.find((plan) => plan.is_default);

  async function update(action: string, body: { is_active?: boolean; role?: "user" | "admin"; plan_id?: string | null }) {
    if (!user) return;
    setBusy(action);
    setError(null);
    try {
      const updated = await unwrap(
        api.PATCH("/v1/admin/users/{user_id}", { params: { path: { user_id: user.id } }, body }),
      );
      onChanged(updated);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Dialog open title="Manage user" description={user.email} onClose={onClose} dismissible={busy === null}>
      <div className="flex flex-col gap-5">
        {error && <Alert>{error}</Alert>}

        <section className="flex flex-col gap-2">
          <h3 className="text-sm font-semibold text-fg">Access</h3>
          {isSelf && <p className="text-xs text-muted">You cannot deactivate or demote your own account.</p>}
          <div className="flex flex-wrap gap-2">
            <Button
              variant={user.is_active ? "danger" : "primary"}
              loading={busy === "status"}
              disabled={isSelf || busy !== null}
              onClick={() => void update("status", { is_active: !user.is_active })}
            >
              {user.is_active ? "Deactivate account" : "Reactivate account"}
            </Button>
            <Button
              variant="secondary"
              loading={busy === "role"}
              disabled={(isSelf && user.role === "admin") || busy !== null}
              onClick={() => void update("role", { role: user.role === "admin" ? "user" : "admin" })}
            >
              {user.role === "admin" ? "Remove administrator" : "Make administrator"}
            </Button>
          </div>
          {user.is_active && !isSelf && (
            <p className="text-xs text-muted">Deactivating signs the user out everywhere and suspends their API keys.</p>
          )}
        </section>

        <section className="flex flex-col gap-2">
          <label htmlFor="user-plan" className="text-sm font-semibold text-fg">
            Plan
          </label>
          <div className="flex flex-wrap gap-2">
            <select
              id="user-plan"
              value={planId}
              onChange={(event) => setPlanId(event.target.value)}
              className="min-h-10 flex-1 cursor-pointer rounded-lg border border-border bg-surface px-3 text-base text-fg sm:text-sm"
            >
              <option value="">Follow default{defaultPlan ? ` (currently ${defaultPlan.name})` : ""}</option>
              {plans.map((plan) => (
                <option key={plan.id} value={plan.id}>
                  {plan.name}
                </option>
              ))}
            </select>
            <Button
              variant="secondary"
              loading={busy === "plan"}
              disabled={planId === (user.plan_id ?? "") || busy !== null}
              onClick={() => void update("plan", { plan_id: planId || null })}
            >
              Save plan
            </Button>
          </div>
          <p className="text-xs text-muted">New prices apply to requests from now on; past charges do not change.</p>
        </section>

        <section className="flex flex-col gap-2">
          <h3 className="text-sm font-semibold text-fg">API keys</h3>
          <UserKeys user={user} />
        </section>
      </div>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function AdminUsersPage() {
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [managing, setManaging] = useState<AdminUser | null>(null);
  const term = useDebounced(search.trim(), 300);

  const users = useApiQuery(
    () =>
      unwrap(
        api.GET("/v1/admin/users", {
          params: { query: { search: term || undefined, limit: PAGE_SIZE, offset } },
        }),
      ),
    `${term}|${offset}`,
  );
  const plans = useApiQuery(() => unwrap(api.GET("/v1/admin/plans")));

  const columns: Column<AdminUser>[] = [
    {
      header: "User",
      cell: (user) => (
        <div className="min-w-40">
          <p className="font-medium">{user.email}</p>
          {!user.has_password && <p className="text-xs text-muted">API keys only (cannot sign in)</p>}
        </div>
      ),
    },
    {
      header: "Status",
      cell: (user) => (
        <div className="flex flex-wrap gap-1">
          {user.is_active ? <Badge tone="success">Active</Badge> : <Badge tone="danger">Deactivated</Badge>}
          {user.role === "admin" && <Badge tone="neutral">Admin</Badge>}
        </div>
      ),
    },
    { header: "Plan", cell: (user) => user.plan_name, hideBelow: "md" },
    { header: "Keys", cell: (user) => formatNumber(user.active_keys), align: "right", hideBelow: "xl" },
    { header: "30-day requests", cell: (user) => formatNumber(user.requests_30d), align: "right", hideBelow: "md" },
    {
      header: "Last sign-in",
      cell: (user) => (user.last_login_at ? formatDate(user.last_login_at) : "Never"),
      hideBelow: "xl",
    },
    {
      header: "Actions",
      align: "right",
      cell: (user) => (
        <Button variant="ghost" className="px-3 font-sans" aria-label={`Manage ${user.email}`} onClick={() => setManaging(user)}>
          Manage
        </Button>
      ),
    },
  ];

  const total = users.status === "success" ? users.data.total : 0;
  const shownFrom = total === 0 ? 0 : offset + 1;
  const shownTo = Math.min(offset + PAGE_SIZE, total);

  return (
    <>
      <PageHeader title="Users" description="Every account on the platform, newest first." />

      <div className="mb-4 max-w-sm">
        <label htmlFor="user-search" className="sr-only">
          Search by email
        </label>
        <div className="relative">
          <Search aria-hidden className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
          <input
            id="user-search"
            type="search"
            placeholder="Search by email"
            value={search}
            onChange={(event) => {
              setSearch(event.target.value);
              setOffset(0);
            }}
            className="min-h-10 w-full rounded-lg border border-border bg-surface pl-9 pr-3 text-base text-fg placeholder:text-muted sm:text-sm"
          />
        </div>
      </div>

      {users.status === "loading" && <Skeleton className="h-72" />}
      {users.status === "error" && (
        <div className="flex flex-col items-start gap-3">
          <Alert>Could not load users: {users.error.message}</Alert>
          <Button variant="secondary" onClick={users.reload}>
            <RefreshCw aria-hidden className="size-4" />
            Try again
          </Button>
        </div>
      )}
      {users.status === "success" && (
        <Card
          title="Accounts"
          description="Manage opens access, plan and API key controls for one user."
          className={users.refreshing ? "opacity-60 transition-opacity" : "transition-opacity"}
        >
          <DataTable
            caption="Users"
            columns={columns}
            rows={users.data.items}
            rowKey={(user) => user.id}
            empty={term ? `No users match "${term}".` : "No users yet."}
          />
          <nav aria-label="Pagination" className="mt-4 flex items-center justify-between gap-4 text-sm">
            <p className="text-muted" aria-live="polite">
              {shownFrom}–{shownTo} of {formatNumber(total)}
            </p>
            <div className="flex gap-2">
              <Button
                variant="secondary"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                aria-label="Previous page"
                className="px-3"
              >
                <ChevronLeft aria-hidden className="size-4" />
              </Button>
              <Button
                variant="secondary"
                disabled={offset + PAGE_SIZE >= total}
                onClick={() => setOffset(offset + PAGE_SIZE)}
                aria-label="Next page"
                className="px-3"
              >
                <ChevronRight aria-hidden className="size-4" />
              </Button>
            </div>
          </nav>
        </Card>
      )}

      <ManageUserDialog
        key={managing?.id ?? "none"}
        user={managing}
        plans={plans.status === "success" ? plans.data.items : []}
        onClose={() => setManaging(null)}
        onChanged={(updated) => {
          setManaging(updated);
          users.reload();
        }}
      />
    </>
  );
}

"use client";

import { useState, type FormEvent } from "react";

import { Alert, Button, Card, Field, PageHeader } from "@/components/ui";
import { ApiError, api, unwrap } from "@/lib/api/client";
import { formatDate } from "@/lib/format";
import { useCurrentUser, useSession } from "@/lib/session";
import { PASSWORD_MIN, validatePasswordChange, type PasswordChangeErrors } from "@/lib/validation";

function ChangePasswordCard() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [errors, setErrors] = useState<PasswordChangeErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaved(false);
    setFormError(null);
    const found = validatePasswordChange(current, next, confirm);
    setErrors(found);
    if (Object.keys(found).length > 0) return;

    setSaving(true);
    try {
      await unwrap(api.POST("/v1/auth/password", { body: { current_password: current, new_password: next } }));
      setCurrent("");
      setNext("");
      setConfirm("");
      setSaved(true);
    } catch (error) {
      if (error instanceof ApiError && error.status === 400) setErrors({ current: error.message });
      else if (error instanceof ApiError && error.param === "new_password") setErrors({ next: error.message });
      else setFormError(error instanceof Error ? error.message : "Could not change the password.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card title="Change password" description="Other signed-in sessions are signed out when you change it.">
      <form onSubmit={onSubmit} noValidate className="flex max-w-md flex-col gap-4">
        {formError && <Alert>{formError}</Alert>}
        {saved && <Alert tone="success">Password changed. Your other sessions were signed out.</Alert>}
        <Field
          label="Current password"
          type="password"
          autoComplete="current-password"
          value={current}
          onChange={(event) => setCurrent(event.target.value)}
          error={errors.current}
        />
        <Field
          label="New password"
          type="password"
          autoComplete="new-password"
          value={next}
          onChange={(event) => setNext(event.target.value)}
          hint={`At least ${PASSWORD_MIN} characters.`}
          error={errors.next}
        />
        <Field
          label="Confirm new password"
          type="password"
          autoComplete="new-password"
          value={confirm}
          onChange={(event) => setConfirm(event.target.value)}
          error={errors.confirm}
        />
        <Button type="submit" loading={saving} className="self-start">
          Change password
        </Button>
      </form>
    </Card>
  );
}

export default function SettingsPage() {
  const user = useCurrentUser();
  const { signOut } = useSession();
  const [signingOut, setSigningOut] = useState(false);

  return (
    <>
      <PageHeader title="Settings" description="Your account and sign-in details." />
      <div className="flex flex-col gap-6">
        <Card title="Account">
          <dl className="grid gap-4 text-sm sm:grid-cols-2">
            <div>
              <dt className="text-muted">Email</dt>
              <dd className="mt-0.5 font-medium text-fg">{user.email}</dd>
            </div>
            <div>
              <dt className="text-muted">Role</dt>
              <dd className="mt-0.5 font-medium text-fg">{user.role === "admin" ? "Administrator" : "Member"}</dd>
            </div>
            <div>
              <dt className="text-muted">Member since</dt>
              <dd className="mt-0.5 text-fg">{formatDate(user.created_at)}</dd>
            </div>
            <div>
              <dt className="text-muted">Last sign-in</dt>
              <dd className="mt-0.5 text-fg">
                {user.last_login_at ? formatDate(user.last_login_at, { dateStyle: "medium", timeStyle: "short" }) : "—"}
              </dd>
            </div>
          </dl>
        </Card>
        <ChangePasswordCard />
        <Card title="Sign out" description="End this session on this device.">
          <Button
            variant="secondary"
            loading={signingOut}
            onClick={async () => {
              setSigningOut(true);
              await signOut();
            }}
          >
            Sign out
          </Button>
        </Card>
      </div>
    </>
  );
}

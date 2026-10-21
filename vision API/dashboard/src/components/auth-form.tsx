"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState, type FormEvent } from "react";

import { Alert, Button, Field } from "@/components/ui";
import { ApiError, api, unwrap } from "@/lib/api/client";
import { safeNextPath } from "@/lib/format";
import { PASSWORD_MIN, validateCredentials, type CredentialErrors } from "@/lib/validation";

type Mode = "login" | "signup";

export function AuthForm({ mode }: { mode: Mode }) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fieldErrors, setFieldErrors] = useState<CredentialErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const isSignup = mode === "signup";

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    const errors = validateCredentials(mode, email, password);
    setFieldErrors(errors);
    if (errors.email || errors.password) return;

    setSubmitting(true);
    try {
      const body = { email: email.trim(), password };
      await unwrap(isSignup ? api.POST("/v1/auth/signup", { body }) : api.POST("/v1/auth/login", { body }));
      router.replace(safeNextPath(searchParams.get("next")));
      router.refresh();
    } catch (error) {
      if (error instanceof ApiError && (error.param === "email" || error.param === "password")) {
        setFieldErrors({ [error.param]: error.message });
      } else {
        setFormError(error instanceof Error ? error.message : "Something went wrong. Please try again.");
      }
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
      {formError && <Alert>{formError}</Alert>}
      <Field
        label="Email"
        type="email"
        name="email"
        autoComplete="email"
        inputMode="email"
        autoFocus
        value={email}
        onChange={(event) => setEmail(event.target.value)}
        error={fieldErrors.email}
      />
      <Field
        label="Password"
        type="password"
        name="password"
        autoComplete={isSignup ? "new-password" : "current-password"}
        value={password}
        onChange={(event) => setPassword(event.target.value)}
        hint={isSignup ? `At least ${PASSWORD_MIN} characters. A passphrase works well.` : undefined}
        error={fieldErrors.password}
      />
      <Button type="submit" loading={submitting} className="mt-2 w-full">
        {isSignup ? "Create account" : "Sign in"}
      </Button>
      <p className="text-center text-sm text-muted">
        {isSignup ? "Already have an account? " : "New to Vision AI? "}
        <Link
          href={isSignup ? "/login" : "/signup"}
          className="font-medium text-primary underline-offset-4 hover:underline"
        >
          {isSignup ? "Sign in" : "Create an account"}
        </Link>
      </p>
    </form>
  );
}

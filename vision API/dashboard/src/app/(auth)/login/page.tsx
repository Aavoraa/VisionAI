import type { Metadata } from "next";
import { Suspense } from "react";

import { AuthForm } from "@/components/auth-form";

export const metadata: Metadata = { title: "Sign in" };

export default function LoginPage() {
  return (
    <>
      <h1 className="mb-1 text-xl font-semibold text-fg">Sign in</h1>
      <p className="mb-6 text-sm text-muted">Manage your API keys, usage and billing.</p>
      {/* AuthForm reads ?next=, which needs a Suspense boundary for prerendering. */}
      <Suspense>
        <AuthForm mode="login" />
      </Suspense>
    </>
  );
}

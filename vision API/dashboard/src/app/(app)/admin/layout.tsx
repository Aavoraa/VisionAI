"use client";

import { ShieldAlert } from "lucide-react";
import Link from "next/link";

import { useCurrentUser } from "@/lib/session";

/**
 * Friendly guard for the admin area. It only avoids showing broken pages to regular users:
 * the API enforces the administrator role on every /v1/admin request.
 */
export default function AdminLayout({ children }: LayoutProps<"/admin">) {
  const user = useCurrentUser();
  if (user.role !== "admin") {
    return (
      <div className="mx-auto flex max-w-md flex-col items-center gap-3 py-16 text-center">
        <ShieldAlert aria-hidden className="size-10 text-muted" />
        <h1 className="text-xl font-semibold text-fg">Administrator access required</h1>
        <p className="text-sm text-muted">This area is only available to administrators.</p>
        <Link href="/overview" className="text-sm font-medium text-primary underline-offset-4 hover:underline">
          Back to overview
        </Link>
      </div>
    );
  }
  return children;
}

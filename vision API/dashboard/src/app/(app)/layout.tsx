"use client";

import { AppShell } from "@/components/app-shell";
import { SessionProvider } from "@/lib/session";

export default function AppLayout({ children }: LayoutProps<"/">) {
  return (
    <SessionProvider>
      <AppShell>{children}</AppShell>
    </SessionProvider>
  );
}

"use client";

import { LogOut, Menu, RefreshCw, X } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { Logo } from "@/components/logo";
import { isActive, navItemsFor, type NavItem } from "@/components/nav";
import { Alert, Button, Skeleton } from "@/components/ui";
import type { User } from "@/lib/api/client";
import { useSession } from "@/lib/session";

function NavLinks({ items, onNavigate }: { items: NavItem[]; onNavigate?: () => void }) {
  const pathname = usePathname() ?? "";
  return (
    <ul className="flex flex-col gap-1">
      {items.map(({ href, label, icon: Icon }) => {
        const active = isActive(pathname, href);
        return (
          <li key={href}>
            <Link
              href={href}
              onClick={onNavigate}
              aria-current={active ? "page" : undefined}
              className={`flex min-h-10 items-center gap-3 rounded-lg px-3 text-sm font-medium transition-colors duration-150 ${
                active ? "bg-primary-soft text-primary" : "text-muted hover:bg-surface-muted hover:text-fg"
              }`}
            >
              <Icon aria-hidden className="size-4 shrink-0" />
              {label}
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

export function Sidebar({ user, onNavigate }: { user: User; onNavigate?: () => void }) {
  const items = navItemsFor(user);
  const adminItems = items.filter((item) => item.adminOnly);
  return (
    <nav aria-label="Main" className="flex flex-col gap-6">
      <NavLinks items={items.filter((item) => !item.adminOnly)} onNavigate={onNavigate} />
      {adminItems.length > 0 && (
        <div>
          <h2 id="nav-admin" className="mb-2 px-3 text-xs font-semibold uppercase tracking-wide text-muted">
            Administration
          </h2>
          <NavLinks items={adminItems} onNavigate={onNavigate} />
        </div>
      )}
    </nav>
  );
}

function AccountMenu({ user, onSignOut }: { user: User; onSignOut: () => Promise<void> }) {
  const [signingOut, setSigningOut] = useState(false);
  return (
    <div className="flex items-center gap-3">
      <div className="hidden text-right sm:block">
        <p className="max-w-56 truncate text-sm font-medium text-fg">{user.email}</p>
        <p className="text-xs text-muted">{user.role === "admin" ? "Administrator" : "Member"}</p>
      </div>
      <Button
        variant="ghost"
        loading={signingOut}
        onClick={async () => {
          setSigningOut(true);
          await onSignOut();
        }}
        aria-label="Sign out"
        className="px-3"
      >
        {!signingOut && <LogOut aria-hidden className="size-4" />}
        <span className="hidden md:inline">Sign out</span>
      </Button>
    </div>
  );
}

function ShellSkeleton() {
  return (
    <div className="flex min-h-screen" aria-busy="true" aria-label="Loading">
      <div className="hidden w-60 border-r border-border bg-surface p-4 lg:block">
        <Skeleton className="mb-8 h-8 w-40" />
        <Skeleton className="mb-2 h-9 w-full" />
        <Skeleton className="h-9 w-full" />
      </div>
      <div className="flex-1 p-6 lg:p-8">
        <Skeleton className="mb-6 h-8 w-48" />
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-24" />
          ))}
        </div>
      </div>
    </div>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const session = useSession();
  const pathname = usePathname();
  // The drawer belongs to the page it was opened on: navigating (links, back/forward) closes it.
  const [menuOpenedOn, setMenuOpenedOn] = useState<string | null>(null);
  const menuOpen = menuOpenedOn !== null && menuOpenedOn === pathname;
  const setMenuOpen = (open: boolean) => setMenuOpenedOn(open ? pathname : null);

  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && setMenuOpenedOn(null);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [menuOpen]);

  if (session.status === "loading") return <ShellSkeleton />;
  if (session.status === "error") {
    return (
      <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-4 px-4">
        <Alert>Could not load your account: {session.error}</Alert>
        <Button variant="secondary" onClick={session.refresh}>
          <RefreshCw aria-hidden className="size-4" />
          Try again
        </Button>
      </main>
    );
  }

  const { user } = session;
  return (
    <div className="min-h-screen lg:flex">
      <a
        href="#content"
        className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded-lg focus:bg-surface focus:px-3 focus:py-2"
      >
        Skip to content
      </a>

      {/* Desktop sidebar */}
      <aside className="sticky top-0 hidden h-screen w-60 shrink-0 flex-col border-r border-border bg-surface px-3 py-4 lg:flex">
        <Link href="/overview" className="mb-6 px-2">
          <Logo />
        </Link>
        <Sidebar user={user} />
      </aside>

      {/* Mobile drawer */}
      {menuOpen && (
        <div className="fixed inset-0 z-40 lg:hidden" role="dialog" aria-modal="true" aria-label="Menu">
          <button
            type="button"
            aria-label="Close menu"
            className="absolute inset-0 cursor-pointer bg-fg/40"
            onClick={() => setMenuOpen(false)}
          />
          <div className="relative flex h-full w-72 max-w-[85%] flex-col bg-surface px-3 py-4 shadow-xl">
            <div className="mb-6 flex items-center justify-between px-2">
              <Logo />
              <Button variant="ghost" aria-label="Close menu" className="px-2" onClick={() => setMenuOpen(false)}>
                <X aria-hidden className="size-5" />
              </Button>
            </div>
            <Sidebar user={user} onNavigate={() => setMenuOpen(false)} />
          </div>
        </div>
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-16 items-center justify-between gap-4 border-b border-border bg-surface/95 px-4 backdrop-blur lg:px-8">
          <Button
            variant="ghost"
            aria-label="Open menu"
            aria-expanded={menuOpen}
            className="px-2 lg:hidden"
            onClick={() => setMenuOpen(true)}
          >
            <Menu aria-hidden className="size-5" />
          </Button>
          <div className="lg:hidden">
            <Logo />
          </div>
          <div className="ml-auto">
            <AccountMenu user={user} onSignOut={session.signOut} />
          </div>
        </header>
        <main id="content" className="mx-auto w-full max-w-6xl flex-1 px-4 py-6 lg:px-8 lg:py-8">
          {children}
        </main>
      </div>
    </div>
  );
}
